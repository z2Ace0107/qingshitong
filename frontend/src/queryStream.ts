export type StreamTerminalType =
  | "run.completed"
  | "run.degraded"
  | "run.needs_review"
  | "run.refused"
  | "run.cancelled"
  | "run.failed";

export interface QueryStreamEvent<TPayload = unknown> {
  schema_version: "qst.stream.v1";
  event_id: string;
  run_id: string;
  sequence: number;
  type: "run.started" | "progress" | StreamTerminalType;
  terminal: boolean;
  payload: TPayload;
}

export interface QueryStreamRequest {
  message: string;
  channel?: "standalone_web" | "portal_sim" | "wecom_sim";
  task_id?: string | null;
}

export interface QueryStreamOptions<TPayload> {
  signal?: AbortSignal;
  onEvent?: (event: QueryStreamEvent<TPayload>) => void;
}

export class QueryStreamError extends Error {
  readonly code: string;
  readonly terminalType?: StreamTerminalType;

  constructor(message: string, code: string, terminalType?: StreamTerminalType) {
    super(message);
    this.name = "QueryStreamError";
    this.code = code;
    this.terminalType = terminalType;
  }
}

const terminalTypes = new Set<StreamTerminalType>([
  "run.completed",
  "run.degraded",
  "run.needs_review",
  "run.refused",
  "run.cancelled",
  "run.failed",
]);

const MAX_SSE_EVENT_BYTES = 64 * 1024;
const MAX_FINAL_RESPONSE_BYTES = 128 * 1024;
const textEncoder = new TextEncoder();

function utf8JsonBytes(value: unknown): number {
  return textEncoder.encode(JSON.stringify(value)).byteLength;
}

function parseEventData<TPayload>(eventName: string | undefined, data: string): QueryStreamEvent<TPayload> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(data);
  } catch {
    throw new QueryStreamError("查询结果格式暂时无法读取。", "stream_invalid_json");
  }
  if (!parsed || typeof parsed !== "object") throw new QueryStreamError("查询结果格式暂时无法读取。", "stream_invalid_event");
  const event = parsed as Partial<QueryStreamEvent<TPayload>>;
  if (
    event.schema_version !== "qst.stream.v1" ||
    typeof event.event_id !== "string" ||
    typeof event.run_id !== "string" ||
    typeof event.sequence !== "number" ||
    typeof event.type !== "string" ||
    typeof event.terminal !== "boolean" ||
    (eventName && eventName !== event.type)
  ) {
    throw new QueryStreamError("查询结果格式暂时无法读取。", "stream_invalid_event");
  }
  if (event.type !== "run.started" && event.type !== "progress" && !terminalTypes.has(event.type as StreamTerminalType)) {
    throw new QueryStreamError("查询结果格式暂时无法读取。", "stream_unknown_event");
  }
  const isTerminalType = terminalTypes.has(event.type as StreamTerminalType);
  if (isTerminalType !== event.terminal) {
    throw new QueryStreamError("查询结果状态异常，请重新提交。", "stream_terminal_flag_invalid");
  }
  return event as QueryStreamEvent<TPayload>;
}

function terminalMessage(type: StreamTerminalType): string {
  if (type === "run.cancelled") return "本次查询已结束。";
  if (type === "run.failed") return "暂时无法完成查询，请稍后重试。";
  if (type === "run.refused") return "当前依据不足，暂时不能给出确定回答。";
  if (type === "run.needs_review") return "这项信息正在核验，请先查看提示。";
  return "查询结果暂时无法读取。";
}

function makeLineProcessor<TPayload>(onFrame: (eventName: string | undefined, data: string, frameBytes: number) => void) {
  let eventName: string | undefined;
  let dataLines: string[] = [];
  let buffer = "";
  let frameBytes = 0;

  const flushFrame = () => {
    if (dataLines.length > 0) onFrame(eventName, dataLines.join("\n"), frameBytes);
    eventName = undefined;
    dataLines = [];
    frameBytes = 0;
  };

  const processLine = (line: string, lineEndingBytes: number) => {
    frameBytes += textEncoder.encode(line).byteLength + lineEndingBytes;
    if (line === "") {
      flushFrame();
      return;
    }
    if (line.startsWith(":")) return;
    const separator = line.indexOf(":");
    const field = separator === -1 ? line : line.slice(0, separator);
    let value = separator === -1 ? "" : line.slice(separator + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") eventName = value;
    if (field === "data") dataLines.push(value);
  };

  return {
    push(text: string) {
      buffer += text;
      while (true) {
        const match = buffer.match(/\r\n|\r|\n/);
        if (!match || match.index === undefined) break;
        if (match[0] === "\r" && match.index + 1 === buffer.length) break;
        processLine(buffer.slice(0, match.index), match[0].length);
        buffer = buffer.slice(match.index + match[0].length);
      }
    },
    finish() {
      if (buffer) processLine(buffer, 0);
      if (dataLines.length > 0 || eventName) throw new QueryStreamError("查询连接中断，请重新提交。", "stream_incomplete");
    },
  };
}

export async function consumeQueryStream<TPayload>(
  request: QueryStreamRequest,
  options: QueryStreamOptions<TPayload> = {},
): Promise<TPayload> {
  let response: Response;
  try {
    response = await fetch("/api/query/stream", {
      method: "POST",
      headers: { Accept: "text/event-stream", "Content-Type": "application/json" },
      body: JSON.stringify(request),
      signal: options.signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw new QueryStreamError("本次查询已结束。", "cancelled", "run.cancelled");
    throw new QueryStreamError("暂时无法连接事项服务，请稍后重试。", "stream_network_error");
  }

  if (!response.ok || !response.body) {
    throw new QueryStreamError("暂时无法连接事项服务，请稍后重试。", "stream_http_error");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let lastSequence = 0;
  let terminalCount = 0;
  let terminalResponse: TPayload | undefined;
  let terminalType: StreamTerminalType | undefined;
  let streamRunId: string | undefined;
  let started = false;
  const parser = makeLineProcessor<TPayload>((eventName, data, frameBytes) => {
    const event = parseEventData<TPayload>(eventName, data);
    if (!started) {
      if (event.type !== "run.started" || event.terminal || event.sequence !== 1) {
        throw new QueryStreamError("查询状态未正确开始，请重新提交。", "stream_started_required");
      }
      started = true;
    }
    if (!Number.isInteger(event.sequence) || event.sequence <= 0 || event.sequence <= lastSequence) {
      throw new QueryStreamError("查询结果顺序异常，请重新提交。", "stream_sequence_invalid");
    }
    if (terminalCount > 0) {
      throw new QueryStreamError("查询结果状态异常，请重新提交。", "stream_event_after_terminal");
    }
    if (streamRunId && event.run_id !== streamRunId) {
      throw new QueryStreamError("查询结果运行状态异常，请重新提交。", "stream_run_id_invalid");
    }
    streamRunId = event.run_id;
    lastSequence = event.sequence;
    if (event.terminal) {
      terminalCount += 1;
      if (terminalCount > 1 || !terminalTypes.has(event.type as StreamTerminalType)) throw new QueryStreamError("查询结果状态异常，请重新提交。", "stream_terminal_invalid");
      terminalType = event.type as StreamTerminalType;
      const payload = event.payload as { response?: TPayload } | TPayload;
      terminalResponse = typeof payload === "object" && payload !== null && "response" in payload ? payload.response : (payload as TPayload);
      if (terminalResponse !== undefined && utf8JsonBytes(terminalResponse) > MAX_FINAL_RESPONSE_BYTES) {
        throw new QueryStreamError("查询结果过大，请稍后重试。", "stream_response_too_large", terminalType);
      }
    }
    if (frameBytes > MAX_SSE_EVENT_BYTES && !event.terminal) {
      throw new QueryStreamError("查询结果过大，请稍后重试。", "stream_event_too_large");
    }
    options.onEvent?.(event);
  });

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      parser.push(decoder.decode(value, { stream: true }));
    }
    parser.push(decoder.decode());
    parser.finish();
  } catch (error) {
    if (error instanceof QueryStreamError) throw error;
    if (error instanceof DOMException && error.name === "AbortError") throw new QueryStreamError("本次查询已结束。", "cancelled", "run.cancelled");
    throw new QueryStreamError("查询连接中断，请重新提交。", "stream_read_error");
  } finally {
    reader.releaseLock();
  }

  if (!terminalType || terminalCount !== 1) throw new QueryStreamError("查询连接中断，请重新提交。", "stream_terminal_missing");
  if (terminalType === "run.cancelled") throw new QueryStreamError(terminalMessage(terminalType), "cancelled", terminalType);
  if (terminalType === "run.failed") throw new QueryStreamError(terminalMessage(terminalType), "stream_failed", terminalType);
  if (terminalType === "run.refused" || terminalType === "run.needs_review") {
    if (terminalResponse === undefined) throw new QueryStreamError(terminalMessage(terminalType), "stream_terminal_invalid", terminalType);
  }
  if (terminalResponse === undefined) throw new QueryStreamError("查询结果暂时无法读取。", "stream_terminal_invalid", terminalType);
  return terminalResponse;
}
