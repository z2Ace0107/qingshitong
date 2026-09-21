import { afterEach, describe, expect, it, vi } from "vitest";
import { consumeQueryStream } from "./queryStream";

function streamResponse(chunks: string[]) {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      chunks.forEach((chunk) => controller.enqueue(encoder.encode(chunk)));
      controller.close();
    },
  });
  return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

function event(type: string, sequence: number, terminal: boolean, payload: unknown, runId = "run-1") {
  return `event: ${type}\ndata: ${JSON.stringify({
    schema_version: "qst.stream.v1",
    event_id: `event-${sequence}`,
    run_id: runId,
    sequence,
    type,
    terminal,
    payload,
  })}\n\n`;
}

describe("consumeQueryStream", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("reassembles network chunks and returns the validated terminal response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        streamResponse([
          event("run.started", 1, false, { message: "正在准备查询" }).slice(0, 25),
          event("run.started", 1, false, { message: "正在准备查询" }).slice(25) +
            event("progress", 2, false, { message: "正在查找相关事项" }),
          event("run.completed", 3, true, { response: { kind: "answer", summary: "已找到事项" } }),
        ]),
      ),
    );

    const events: string[] = [];
    await expect(
      consumeQueryStream<{ kind: string; summary: string }>(
        { message: "查询活动场地", channel: "standalone_web" },
        { onEvent: (received) => events.push(received.type) },
      ),
    ).resolves.toEqual({ kind: "answer", summary: "已找到事项" });
    expect(events).toEqual(["run.started", "progress", "run.completed"]);
  });

  it("rejects a terminal event whose envelope is not marked terminal", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(streamResponse([event("run.completed", 1, false, {})])));

    await expect(consumeQueryStream({ message: "查询" })).rejects.toMatchObject({
      code: "stream_terminal_flag_invalid",
    });
  });

  it("rejects non-positive sequence numbers before they reach the UI", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(streamResponse([
      event("run.started", 1, false, {}),
      event("progress", 0, false, {}),
    ])));

    await expect(consumeQueryStream({ message: "查询" })).rejects.toMatchObject({
      code: "stream_sequence_invalid",
    });
  });

  it("requires run.started with sequence 1 as the first event", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(streamResponse([event("progress", 1, false, { message: "不应先到" })])));

    await expect(consumeQueryStream({ message: "查询" })).rejects.toMatchObject({
      code: "stream_started_required",
    });
  });

  it("rejects any event after the unique terminal event", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        streamResponse([
          event("run.started", 1, false, {}),
          event("run.completed", 2, true, { response: { kind: "answer" } }),
          event("progress", 3, false, { message: "不应继续" }),
        ]),
      ),
    );

    await expect(consumeQueryStream({ message: "查询" })).rejects.toMatchObject({
      code: "stream_event_after_terminal",
    });
  });

  it("rejects events that switch to another run", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        streamResponse([
          event("run.started", 1, false, { message: "正在准备查询" }),
          event("run.completed", 2, true, { response: { kind: "answer" } }, "run-2"),
        ]),
      ),
    );

    await expect(consumeQueryStream({ message: "查询" })).rejects.toMatchObject({
      code: "stream_run_id_invalid",
    });
  });

  it("rejects an oversized SSE frame before it reaches the UI", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(streamResponse([
      event("run.started", 1, false, {}),
      event("progress", 2, false, { message: "x".repeat(64 * 1024) }),
    ])));

    await expect(consumeQueryStream({ message: "查询" })).rejects.toMatchObject({
      code: "stream_event_too_large",
    });
  });

  it("rejects an oversized terminal response instead of rendering it", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        streamResponse([
          event("run.started", 1, false, {}),
          event("run.completed", 2, true, { response: { kind: "answer", summary: "x".repeat(128 * 1024) } }),
        ]),
      ),
    );

    await expect(consumeQueryStream({ message: "查询" })).rejects.toMatchObject({
      code: "stream_response_too_large",
    });
  });

  it("rejects a half-closed response without a terminal event", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(streamResponse([
      event("run.started", 1, false, {}),
      event("progress", 2, false, { message: "处理中" }),
      "data: {\"partial\":true}\n",
    ])));

    await expect(consumeQueryStream({ message: "查询" })).rejects.toMatchObject({
      code: "stream_incomplete",
    });
  });
});
