import { useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import {
  Activity,
  ArrowRight,
  CalendarDays,
  CheckCircle2,
  ChevronRight,
  CircleAlert,
  ClipboardList,
  Eye,
  FileCheck2,
  FileText,
  LoaderCircle,
  RotateCcw,
  Save,
  Search,
  Send,
  ShieldCheck,
  X,
} from "lucide-react";
import {
  BrowserRouter,
  Link,
  Navigate,
  NavLink,
  Outlet,
  Route,
  Routes,
  useLocation,
  useNavigate,
  useOutletContext,
  useParams,
} from "react-router-dom";
import {
  QueryClient,
  QueryClientProvider,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { ActivityPlanFields } from "./materials";
import { initialFieldValue } from "./formState";
import { consumeQueryStream, QueryStreamError } from "./queryStream";
import "./styles.css";

type Channel = "standalone_web" | "portal_sim" | "wecom_sim";
type AnyRecord = Record<string, unknown>;

interface ScenarioField {
  key: string;
  label: string;
  type: string;
  options?: string[];
  required?: boolean;
}

interface Scenario {
  id: string;
  item_id: string;
  name: string;
  description: string;
  fields: ScenarioField[];
  steps: string[];
}

interface ServiceItem {
  id: string;
  title: string;
  domain: string;
  summary: string;
  audience: string;
  materials: string[];
  steps: string[];
  entry_label: string;
  entry_url: string | null;
}

interface Bootstrap {
  items: ServiceItem[];
  scenarios: Scenario[];
  disclaimer: string;
}

interface GovernanceEvaluation {
  id?: string;
  status?: string;
  run_mode?: string;
  publication_id?: string | null;
  summary?: {
    quality_gate?: string;
    total?: number;
    counts?: Record<string, number>;
  };
}

interface GovernanceDashboard {
  latest_evaluation?: GovernanceEvaluation | null;
  evaluations?: GovernanceEvaluation[];
  bad_cases?: { status?: string; severity?: string }[];
  run_total?: number;
  feedback_total?: number;
  task_total?: number;
  dataset?: { version?: string; cases?: number };
}

interface GovernanceGate {
  decision?: string;
  fact_source_gate?: string;
  quality_gate?: string;
  responsibility_gate?: string;
  p0_open_count?: number;
  p1_open_count?: number;
  decision_reason?: string;
}

interface StudentResponse {
  kind: "answer" | "clarify" | "refused" | "degraded";
  title?: string;
  summary?: string;
  service_item?: { title?: string; domain?: string; icon?: string };
  audience?: string;
  responsible_party?: string;
  conditions?: string[];
  materials?: string[];
  steps?: string[];
  official_entry?: { label: string; url: string | null };
  freshness?: { state?: string; label?: string; message?: string; checked_at?: string | null };
  simulation_boundary?: { label?: string; real_integration?: boolean };
  simulation_disclaimer?: string;
  claims?: { text: string }[];
  clarifications?: { question: string }[];
  next_actions?: string[];
  evidence?: PublicEvidence[];
  degradation?: { message: string; recovery_action: string };
}

interface PublicEvidence {
  title?: string;
  source_title?: string;
  published_at?: string | null;
  retrieved_at?: string | null;
  freshness_state?: string;
}

interface StudentField {
  key: string;
  label: string;
  type: string;
  options?: string[];
  value?: unknown;
  required: boolean;
  error?: string | null;
}

interface StudentMaterial {
  id: string;
  title: string;
  required: boolean;
  applicability: string;
  type: string;
  content: AnyRecord;
  status: string;
  missing_fields: string[];
  correction_note?: string | null;
  revision: number;
}

interface StudentTask {
  task_ref: { task_id: string; route: string };
  scenario_title: string;
  channel: Channel;
  status: string;
  version: number;
  fields: StudentField[];
  materials: StudentMaterial[];
  rule_results: { label: string; status: string; detail: string }[];
  review_nodes: { role: string; result: string; note: string; next_step: string; at?: string }[];
  next_actions: { code: string; label: string; enabled: boolean; route: string | null }[];
  boundary_notice: string;
}

interface StudentPreview {
  task: StudentTask;
  preview: {
    preview_version: number;
    submission_version: number;
    proposed_status: string;
    fields: StudentField[];
    materials: StudentMaterial[];
    rules: { label: string; status: string; detail: string }[];
    boundary_notice: string;
  };
}

interface StudentBasis {
  title?: string | null;
  publisher?: string | null;
  published_at?: string | null;
  effective_from?: string | null;
  effective_to?: string | null;
  freshness_state?: string | null;
}

interface StudentReconfirmation {
  status: string;
  version: number;
  change_summary: string[];
  severity: string;
  old_basis: StudentBasis;
  new_basis: StudentBasis;
  saved_fields: StudentField[];
  saved_materials: StudentMaterial[];
  action: { code: string; label: string; required: boolean };
  boundary_notice: string;
}

interface ApiErrorPayload {
  error?: { code?: string; message?: string; recovery_action?: string; field_errors?: { field: string; msg: string }[] };
  detail?: { code?: string; message?: string } | string;
}

class ApiError extends Error {
  readonly code: string;
  readonly recoveryAction?: string;

  constructor(payload: ApiErrorPayload, status: number) {
    const detail = typeof payload.detail === "string" ? payload.detail : payload.detail?.message;
    super(payload.error?.message || detail || "请求未完成");
    this.code = payload.error?.code || (typeof payload.detail === "object" ? payload.detail?.code : undefined) || `HTTP_${status}`;
    this.recoveryAction = payload.error?.recovery_action;
  }
}

async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...options,
    headers: { Accept: "application/json", "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const payload = (await response.json().catch(() => ({}))) as ApiErrorPayload;
  if (!response.ok) throw new ApiError(payload, response.status);
  return payload as T;
}

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false, staleTime: 10_000 } },
});

const channelLabels: Record<Channel, string> = {
  standalone_web: "独立 Web",
  portal_sim: "校园门户工作台",
  wecom_sim: "移动工作台",
};

const statusLabels: Record<string, string> = {
  draft: "开始填写",
  collecting: "正在填写",
  precheck_failed: "需要补充或修改",
  ready_for_preview: "可以查看提交内容",
  awaiting_confirmation: "等待你的确认",
  submitted: "已提交",
  under_review: "审核中",
  needs_correction: "需要补正",
  approved: "已通过",
  rejected: "未通过",
  completed: "已完成",
  closed: "已结束",
  needs_reconfirmation: "办理依据发生变化",
  policy_changed: "办理依据发生变化",
  cancelled: "已结束",
};

const materialStatusLabels: Record<string, string> = {
  not_started: "未开始",
  editing: "编辑中",
  saved: "已保存",
  needs_completion: "待补充",
  submitted: "已提交",
  returned: "审核退回",
  revised: "已修订",
  approved: "审核通过",
  not_applicable: "不适用",
};

const optionLabels: Record<string, string> = {
  student_organization: "学生组织",
  class_group: "班级或年级",
  other: "其他组织",
  demo_plaza: "示例活动广场",
  medical: "病假",
  personal: "事假",
  official: "公假",
};

function labelForOption(value: string) {
  return optionLabels[value] || value;
}

function AppLayout() {
  const location = useLocation();
  const isLanding = location.pathname === "/";
  const [channel, setChannel] = useState<Channel>("standalone_web");
  const bootstrap = useQuery({ queryKey: ["student-bootstrap"], queryFn: () => api<Bootstrap>("/api/student/bootstrap"), enabled: !isLanding });
  const health = useQuery({ queryKey: ["health-ready"], queryFn: () => api<AnyRecord>("/api/health/ready"), enabled: !isLanding });
  const context = useMemo(() => ({ bootstrap: bootstrap.data, channel, setChannel, health: health.data }), [bootstrap.data, channel, health.data]);

  if (!isLanding && bootstrap.isPending) return <LoadingScreen message="正在准备事项服务" />;
  if (!isLanding && (bootstrap.isError || !bootstrap.data)) return <ErrorScreen message="事项服务暂时无法打开" onRetry={() => void bootstrap.refetch()} />;

  const serviceState = health.data?.status === "ready" ? "智能服务可用" : "基础事项服务可用";
  return (
    <div className={`app-shell ${isLanding ? "is-landing" : ""}`}>
      <header className="topbar">
        <Link className="brand" to={isLanding ? "/" : "/student"} aria-label="庆事通首页">
          <span className="brand-mark" aria-hidden="true"><FileText size={18} /></span>
          <span><strong>庆事通</strong><small>校园事务服务</small></span>
        </Link>
        <nav className="topnav" aria-label="主导航">
          <NavLink className={({ isActive }) => `nav-link ${isActive ? "is-active" : ""}`} to="/student">查事项</NavLink>
          <NavLink className={({ isActive }) => `nav-link ${isActive ? "is-active" : ""}`} to="/student#my-tasks">我的办理</NavLink>
          <NavLink className={({ isActive }) => `nav-link ${isActive ? "is-active" : ""}`} to="/public/summary">服务说明</NavLink>
        </nav>
        <div className="topbar-tools">
          {isLanding ? <Link className="button primary topbar-cta" to="/student">进入工作台 <ArrowRight size={15} /></Link> : <><span className="service-indicator"><span className="status-dot" />{serviceState}</span><div className="channel-switch" role="group" aria-label="服务入口外壳">{(Object.keys(channelLabels) as Channel[]).map((item) => <button key={item} className={channel === item ? "is-active" : ""} type="button" onClick={() => setChannel(item)}>{channelLabels[item]}</button>)}</div></>}
        </div>
      </header>

      <main className="main-shell"><Outlet context={context} /></main>
      {!isLanding && <footer className="site-footer"><ShieldCheck size={15} /><span>资料经过脱敏、改写或虚拟化处理；当前未接入学校真实业务系统。</span><Link to="/public/summary">了解服务边界</Link></footer>}
      {!isLanding && <Mascot />}
    </div>
  );
}

type AppContext = ReturnType<typeof useAppContext>;

function useAppContext() {
  return useOutletContext<{ bootstrap?: Bootstrap; channel: Channel; setChannel: (channel: Channel) => void; health?: AnyRecord }>();
}

function LoadingScreen({ message }: { message: string }) {
  return <div className="page-state"><LoaderCircle className="spin" size={26} /><p>{message}</p></div>;
}

function ErrorScreen({ message, onRetry }: { message: string; onRetry: () => void }) {
  return <div className="page-state"><CircleAlert size={28} /><h2>{message}</h2><button className="button primary" type="button" onClick={onRetry}>重新加载</button></div>;
}

function Mascot() {
  useEffect(() => {
    window.QSTMascot?.bind();
    window.QSTMascot?.setState("idle", "需要时叫我，我会陪你把事情办清楚。");
  }, []);
  return (
    <aside id="mascot" className="mascot" aria-live="polite">
      <button id="mascot-toggle" className="mascot-face" type="button" aria-label="展开或收起庆小通提示" aria-expanded="true">
        <span id="mascot-visual" className="mascot-visual" aria-hidden="true"><span className="mascot-fallback-mark"><span className="mascot-fallback-eyes" /><span className="mascot-fallback-mouth" /></span></span>
      </button>
      <div className="mascot-note">
        <div className="mascot-note-head"><strong>庆小通</strong><span id="mascot-state-label">待命</span></div>
        <span id="mascot-text">需要时叫我，我会陪你把事情办清楚。</span>
      </div>
    </aside>
  );
}

function StudentHome() {
  const { bootstrap, channel, health } = useAppContext();
  const navigate = useNavigate();
  const [message, setMessage] = useState("");
  const [answer, setAnswer] = useState<StudentResponse | null>(null);
  const [queryState, setQueryState] = useState<"idle" | "streaming" | "cancelling" | "success" | "error" | "cancelled">("idle");
  const [queryError, setQueryError] = useState<Error | null>(null);
  const [progress, setProgress] = useState("正在准备查询");
  const queryInputRef = useRef<HTMLInputElement | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const runRef = useRef<string | null>(null);
  const cancelKeyRef = useRef<string | null>(null);
  const cancelRequestedRef = useRef(false);
  const cancelErrorRef = useRef<Error | null>(null);
  const cancellationRequestRef = useRef<Promise<void> | null>(null);
  const requestNumberRef = useRef(0);
  const tasks = useQuery({ queryKey: ["student-tasks"], queryFn: () => api<{ tasks: StudentTask[] }>("/api/student/tasks") });
  const startMutation = useMutation({
    mutationFn: (scenarioId: string) => api<StudentTask>("/api/student/tasks", { method: "POST", body: JSON.stringify({ scenario_id: scenarioId, channel }) }),
    onSuccess: (task) => {
      void tasks.refetch();
      navigate(`${task.task_ref.route}/materials`);
    },
  });

  const answerScenario = answer?.service_item?.title
    ? bootstrap?.scenarios.find((scenario) => scenario.id === "scenario-venue-v1" && bootstrap.items.find((item) => item.id === scenario.item_id)?.title === answer.service_item?.title)
    : undefined;

  const isStreaming = queryState === "streaming" || queryState === "cancelling";

  useEffect(() => {
    if (queryState === "cancelled") queryInputRef.current?.focus();
  }, [queryState]);

  async function requestRunCancellation(runId: string) {
    if (!cancelRequestedRef.current || cancellationRequestRef.current) return;
    const request = api(`/api/runs/${runId}/cancel`, {
      method: "POST",
      headers: { "Idempotency-Key": cancelKeyRef.current || `student-stream-cancel-${Date.now()}` },
    }).then(() => undefined).catch((error) => {
      cancelErrorRef.current = error instanceof Error ? error : new Error("暂时无法确认查询已结束");
    });
    cancellationRequestRef.current = request;
    await request;
    if (cancellationRequestRef.current === request) cancellationRequestRef.current = null;
  }

  async function submitQueryValue(value: string) {
    const trimmed = value.trim();
    if (!trimmed || isStreaming) return;
    const controller = new AbortController();
    requestNumberRef.current += 1;
    abortRef.current = controller;
    runRef.current = null;
    cancelKeyRef.current = `student-stream-${requestNumberRef.current}-${Date.now()}`;
    cancelRequestedRef.current = false;
    cancelErrorRef.current = null;
    cancellationRequestRef.current = null;
    setAnswer(null);
    setQueryError(null);
    setProgress("正在准备查询");
    setQueryState("streaming");
    try {
      const nextAnswer = await consumeQueryStream<StudentResponse>(
        { message: trimmed, channel },
        {
          signal: controller.signal,
          onEvent: (event) => {
            runRef.current = event.run_id;
            if (cancelRequestedRef.current) void requestRunCancellation(event.run_id);
            if (event.type === "progress" && typeof event.payload === "object" && event.payload !== null && "message" in event.payload) {
              const eventMessage = (event.payload as { message?: unknown }).message;
              if (typeof eventMessage === "string" && eventMessage) setProgress(eventMessage);
            }
          },
        },
      );
      setAnswer(nextAnswer);
      setQueryState("success");
    } catch (error) {
      const streamError = error instanceof QueryStreamError ? error : new QueryStreamError("事项服务暂时无法打开", "stream_unknown_error");
      if (streamError.code === "cancelled") {
        if (cancelErrorRef.current) {
          setQueryError(cancelErrorRef.current);
          setQueryState("error");
          setProgress("取消请求未能确认，请检查连接后重试");
        } else {
          setQueryState("cancelled");
          setProgress("本次查询已结束，可以重新提交");
        }
      } else {
        setQueryError(streamError);
        setQueryState("error");
      }
    } finally {
      if (abortRef.current === controller) {
        abortRef.current = null;
        runRef.current = null;
        cancelKeyRef.current = null;
        cancelRequestedRef.current = false;
        cancellationRequestRef.current = null;
      }
    }
  }

  async function cancelQuery() {
    const controller = abortRef.current;
    if (!controller || queryState === "cancelling") return;
    cancelRequestedRef.current = true;
    setQueryState("cancelling");
    setProgress("正在结束本次查询");
    const runId = runRef.current;
    if (runId) await requestRunCancellation(runId);
    controller.abort();
  }

  const submitQuery = (event: FormEvent) => {
    event.preventDefault();
    void submitQueryValue(message);
  };

  return (
    <div className="page-width page-stack home-page">
      <section className="student-hero">
        <div className="student-hero-copy"><div className="breadcrumbs"><Link to="/">庆事通</Link><span className="crumb-divider">/</span><span>学生工作台</span></div><h1>今天想把哪件事办清楚？</h1><p>从依据开始，找到适用条件、准备材料和可以继续的下一步。</p></div>
        <div className="student-hero-status"><span className="status-dot" /><strong>{serviceState(health)}</strong><span>事项资料已加载</span></div>
      </section>

      <section className="query-panel" aria-label="事项查询">
        <div className="query-panel-heading"><div><span className="query-index">01</span><div><strong>描述你要办理的事情</strong><span>可以直接说你的目的、时间或遇到的问题</span></div></div><span className="query-channel">{channelLabels[channel]}</span></div>
        <form className="query-form" onSubmit={submitQuery}>
          <label className="sr-only" htmlFor="query-input">描述你要办理的校园事务</label>
          <Search className="query-icon" size={19} aria-hidden="true" />
          <input ref={queryInputRef} id="query-input" value={message} onChange={(event) => setMessage(event.target.value)} placeholder="例如：我想申请病假，需要准备什么？" />
          {isStreaming && <button className="button quiet" type="button" onClick={() => void cancelQuery()} disabled={queryState === "cancelling"}><X size={16} />{queryState === "cancelling" ? "正在结束" : "取消查询"}</button>}
          <button className="button primary" type="submit" disabled={isStreaming}><span>{isStreaming ? "查询中" : "查事项"}</span><ArrowRight size={16} /></button>
        </form>
        <div className="quick-queries" aria-label="常用事项">
          <span className="quick-label">常用事项</span>
          {[["病假材料", "我想申请病假，需要准备什么？"], ["勤工助学", "有没有适合我时间的勤工助学岗位？"], ["选课公告", "选课什么时候开始？"]].map(([label, value]) => <button key={label} type="button" disabled={isStreaming} onClick={() => { setMessage(value); void submitQueryValue(value); }}>{label}<ArrowRight size={13} /></button>)}
        </div>
      </section>

      <div className="home-grid">
        <main className="home-main">
          <section className="content-section answer-section" aria-live="polite">
            <div className="section-heading"><div><span className="section-index">02</span><div><p className="eyebrow">事项回应</p><h2>找到与你有关的规定</h2></div></div><span className="status-chip">{isStreaming ? progress : answer ? "已返回事项" : queryState === "cancelled" ? "查询已结束" : queryState === "error" ? "需要重试" : "等待输入"}</span></div>
            {isStreaming && <div className="stream-progress" role="status" aria-live="polite"><LoaderCircle className="spin" size={17} /><span>{progress}</span><span className="progress-track"><i /></span></div>}
            {queryState === "cancelled" && <div className="notice neutral stream-finished" role="status" aria-live="polite"><CheckCircle2 size={17} /><span>{progress}</span><button className="text-button" type="button" onClick={() => { setQueryState("idle"); setProgress("正在等待输入"); queryInputRef.current?.focus(); }}><RotateCcw size={15} />重新查询</button></div>}
            {queryError && <InlineError error={queryError} onRetry={() => void submitQueryValue(message)} />}
            {answer ? <AnswerCard answer={answer} scenario={answerScenario} onStart={(id) => startMutation.mutate(id)} /> : <EmptyPanel icon={<Search size={23} />} text="输入一个校园事务，系统会返回事项依据、办理材料和下一步。" />}
          </section>

          <section className="content-section" id="my-tasks">
            <div className="section-heading"><div><span className="section-index">03</span><div><p className="eyebrow">可恢复任务</p><h2>正在办理的事项</h2></div></div><span className="section-note">{channelLabels[channel]}</span></div>
            {tasks.isPending ? <LoadingInline /> : tasks.data?.tasks.length ? <div className="task-list">{tasks.data.tasks.map((task) => <Link className="task-list-item" key={task.task_ref.task_id} to={`${task.task_ref.route}/materials`}><div><span className="task-list-domain">办理任务</span><strong>{task.scenario_title}</strong><span className="task-list-status">{statusLabels[task.status] || "需要查看"}</span></div><ChevronRight size={18} /></Link>)}</div> : <EmptyPanel compact icon={<ClipboardList size={22} />} text="还没有正在办理的事项。可以从上面的事项卡开始。" />}
          </section>

          <section className="content-section" id="service-catalog">
            <div className="section-heading"><div><span className="section-index">04</span><div><p className="eyebrow">服务目录</p><h2>从常见事项开始</h2></div></div><span className="section-note">共 {bootstrap?.scenarios.length || 0} 项</span></div>
            <div className="catalog-list">{bootstrap?.scenarios.map((scenario, index) => {
              const item = bootstrap?.items.find((candidate) => candidate.id === scenario.item_id);
              const canStart = scenario.id === "scenario-venue-v1";
              return <article className="catalog-item" key={scenario.id}><span className="catalog-index">{String(index + 1).padStart(2, "0")}</span><div><h3>{item?.title || scenario.name.replace(/模拟$/, "")}</h3><p>{item?.domain || "校园服务"}</p></div><span className="catalog-description">{item?.summary || scenario.description}</span>{canStart ? <button className="text-button" type="button" onClick={() => startMutation.mutate(scenario.id)}>开始办理 <ArrowRight size={15} /></button> : <span className="catalog-readonly">当前提供事项说明</span>}</article>;
            })}</div>
          </section>
        </main>
        <aside className="home-rail" aria-label="服务提示">
          <section className="rail-section"><div className="rail-heading"><span>服务范围</span><CalendarDays size={15} /></div><div className="rail-list"><div><strong>查事项</strong><span>按你的问题找依据和路径</span></div><div><strong>填材料</strong><span>逐项准备并进行材料预检</span></div><div><strong>看进度</strong><span>回到任务查看办理状态</span></div></div></section>
          <section className="rail-section rail-section-soft"><div className="rail-heading"><span>当前服务入口</span><span className="rail-value">{channelLabels[channel]}</span></div><p>不同入口共享同一份事项事实和任务状态，切换入口不会丢失办理内容。</p></section>
          <section className="rail-section rail-section-soft"><div className="rail-heading"><span>服务状态</span><span className="state-label"><span className="status-dot" />{serviceState(health)}</span></div><p>学生端只展示办理所需信息，不展示工程追踪字段。</p></section>
        </aside>
      </div>
    </div>
  );
}

function serviceState(health?: AnyRecord) {
  return health?.status === "ready" ? "智能服务可用" : "基础服务可用";
}

function healthStateClass(health?: AnyRecord) {
  return health?.status === "ready" ? "text-teal" : "text-gold";
}

function AnswerCard({ answer, scenario, onStart }: { answer: StudentResponse; scenario?: Scenario; onStart: (scenarioId: string) => void }) {
  const refused = answer.kind === "refused";
  const caution = refused || answer.kind === "degraded";
  return (
    <div className="answer-grid" role="status" aria-live="polite">
      <article className="answer-card">
        <div className="answer-heading"><div><span className="answer-kicker">{caution ? "需要核验" : answer.kind === "clarify" ? "需要补充信息" : "事项依据"}</span><h3>{answer.title || "暂时没有足够依据"}</h3><span className="answer-audience">{answer.audience || "面向当前办理人的说明"}</span></div><span className={`status-chip ${caution ? "is-warn" : "is-live"}`}>{answer.freshness?.label || "已核验"}</span></div>
        <p className="answer-summary">{answer.summary}</p>
        <div className="boundary-box"><ShieldCheck size={16} /><div><strong>服务边界</strong><span>{answer.simulation_disclaimer || "当前未接入学校真实业务系统"}</span></div></div>
        {answer.clarifications?.length ? <InfoList title="需要确认" items={answer.clarifications.map((item) => item.question)} /> : null}
        {answer.claims?.length ? <InfoList title="事项结论" items={answer.claims.map((item) => item.text)} /> : null}
        {answer.conditions?.length ? <InfoList title="适用条件" items={answer.conditions} /> : null}
        {answer.materials?.length ? <InfoList title="办理材料" items={answer.materials} /> : null}
        {answer.steps?.length ? <InfoList title="办理步骤" items={answer.steps} /> : null}
        {answer.degradation && <div className="notice warning"><CircleAlert size={17} /><span>{answer.degradation.message}</span></div>}
        <div className="answer-actions">
          {scenario && !refused && <button className="button primary" type="button" onClick={() => onStart(scenario.id)}><ClipboardList size={17} />开始办理</button>}
          {answer.official_entry?.url && <a className="button secondary" href={answer.official_entry.url} target="_blank" rel="noreferrer"><ArrowRight size={16} />{answer.official_entry.label || "打开官方核验入口"}</a>}
        </div>
      </article>
      <aside className="evidence-panel"><div className="panel-heading"><div><span className="panel-index">依据</span><h4>这条回答基于</h4></div><span className="status-chip">业务资料</span></div>{answer.evidence?.length ? answer.evidence.map((item, index) => <div className="evidence-item" key={`${item.source_title}-${index}`}><span className="evidence-number">{String(index + 1).padStart(2, "0")}</span><div><strong>{item.source_title || item.title || "事项资料"}</strong><span>{freshnessLabel(item.freshness_state)}</span><small>用于说明当前事项的适用范围和办理路径</small></div></div>) : <div className="empty-evidence">没有足够依据时，系统不会替你补写学校规定。</div>}</aside>
    </div>
  );
}

function freshnessLabel(value?: string) {
  return ({ verified_current: "当前已核验", verified: "已核验", possibly_stale: "可能需要更新", pending_review: "正在核验", conflicted: "存在差异" } as Record<string, string>)[value || ""] || "需要核验";
}

function InfoList({ title, items }: { title: string; items: string[] }) {
  return <div className="info-block"><h4>{title}</h4><ul>{items.map((item, index) => <li key={`${item}-${index}`}>{item}</li>)}</ul></div>;
}

function EmptyPanel({ icon, text, compact = false }: { icon: ReactNode; text: string; compact?: boolean }) {
  return <div className={`empty-panel ${compact ? "compact" : ""}`}><span className="empty-icon">{icon}</span><p>{text}</p></div>;
}

function InlineError({ error, onRetry }: { error: Error; onRetry: () => void }) {
  return <div className="notice warning"><CircleAlert size={17} /><span>{error.message}</span><button className="text-button" type="button" onClick={onRetry}><RotateCcw size={15} />重试</button></div>;
}

function TaskLayout() {
  const { taskId } = useParams();
  const app = useAppContext();
  const location = useLocation();
  const taskQuery = useQuery({ queryKey: ["student-task", taskId], queryFn: () => api<StudentTask>(`/api/student/tasks/${taskId}`), enabled: Boolean(taskId) });
  useEffect(() => {
    window.scrollTo({ top: 0, left: 0, behavior: "auto" });
  }, [location.pathname]);
  if (taskQuery.isPending) return <div className="page-width page-state"><LoaderCircle className="spin" size={26} /><p>正在恢复办理内容</p></div>;
  if (taskQuery.isError || !taskQuery.data) return <div className="page-width page-state"><CircleAlert size={28} /><h2>这个办理任务暂时不可用</h2><Link className="button secondary" to="/student">回到事项服务</Link></div>;
  const context = { ...app, task: taskQuery.data, taskId: taskId || "", refresh: taskQuery.refetch };
  return <div className="page-width page-stack task-page"><div className="breadcrumbs"><Link to="/student">事项服务</Link><ChevronRight size={15} /><span>{taskQuery.data.scenario_title}</span></div><section className="task-header"><div><p className="eyebrow">办理工作区</p><h1>{taskQuery.data.scenario_title}</h1><p>{taskQuery.data.boundary_notice}</p></div><span className={`large-status ${taskQuery.data.status === "precheck_failed" || taskQuery.data.status === "needs_correction" ? "is-warn" : ""}`}>{statusLabels[taskQuery.data.status] || taskQuery.data.status}</span></section><div className="task-workspace"><aside className="task-progress-rail"><div className="rail-heading"><span>办理路径</span><ClipboardList size={15} /></div><ProgressList task={taskQuery.data} /><div className="rail-rule" /><div className="side-boundary"><ShieldCheck size={16} /><span>{taskQuery.data.boundary_notice}</span></div></aside><main className="task-main"><nav className="task-tabs" aria-label="办理内容"><NavLink to="materials">材料填写</NavLink><NavLink to="preview">提交预览</NavLink><NavLink to="status">办理状态</NavLink></nav><Outlet context={context} /></main></div></div>;
}

type TaskContext = AppContext & { task: StudentTask; taskId: string; refresh: () => Promise<unknown> };

function useTaskContext() {
  return useOutletContext<TaskContext>();
}

function MaterialsPage() {
  const { task, taskId, refresh } = useTaskContext();
  const client = useQueryClient();
  const [form, setForm] = useState<Record<string, unknown>>(() => Object.fromEntries(task.fields.map((field) => [field.key, initialFieldValue(field)])));
  const [materials, setMaterials] = useState<Record<string, AnyRecord>>(() => Object.fromEntries(task.materials.map((item) => [item.id, item.content || {}])));
  const [saveState, setSaveState] = useState<"idle" | "saving" | "saved" | "failed">("idle");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    setForm(Object.fromEntries(task.fields.map((field) => [field.key, initialFieldValue(field)])));
    setMaterials(Object.fromEntries(task.materials.map((item) => [item.id, item.content || {}])));
  }, [task.version]);

  const setField = (key: string, value: unknown) => setForm((current) => ({ ...current, [key]: value }));
  const setMaterial = (id: string, key: string, value: unknown) => setMaterials((current) => ({ ...current, [id]: { ...(current[id] || {}), [key]: value } }));
  const materialPayload = useMemo(() => Object.fromEntries(Object.entries(materials).map(([id, content]) => [id, normalizeMaterialContent(id, content)])), [materials]);

  async function saveMaterials(showMessage = true) {
    setSaveState("saving");
    try {
      const updated = await api<StudentTask>(`/api/student/tasks/${taskId}/materials`, { method: "PUT", body: JSON.stringify({ materials: materialPayload, form_data: form, expected_version: task.version }) });
      client.setQueryData(["student-task", taskId], updated);
      setSaveState("saved");
      if (showMessage) setTimeout(() => setSaveState("idle"), 1800);
      return updated;
    } catch (error) {
      setSaveState("failed");
      throw error;
    }
  }

  async function precheck() {
    setBusy(true);
    try {
      const checked = await api<StudentTask>(`/api/student/tasks/${taskId}/precheck`, { method: "POST", body: JSON.stringify({ form_data: { ...form, materials: materialPayload } }) });
      client.setQueryData(["student-task", taskId], checked);
      await refresh();
    } catch (error) {
      setSaveState("failed");
      setBusy(false);
      throw error;
    }
    setBusy(false);
  }

  return <div className="task-content-grid"><section className="work-card"><div className="card-heading"><div><p className="eyebrow">第一步</p><h2>填写事项信息</h2></div><span className="save-state">{saveState === "saving" ? "保存中" : saveState === "saved" ? "已保存" : saveState === "failed" ? "保存失败" : "服务端保存"}</span></div><div className="field-grid">{task.fields.map((field) => <FieldControl key={field.key} field={field} value={form[field.key]} onChange={(value) => setField(field.key, value)} />)}</div><div className="material-section"><div className="card-heading"><div><p className="eyebrow">第二步</p><h2>准备办理材料</h2></div><span className="section-note">按材料分别填写，不需要上传文件</span></div><div className="material-list">{task.materials.map((material) => <MaterialEditor key={material.id} material={material} value={materials[material.id] || {}} onChange={(key, value) => setMaterial(material.id, key, value)} />)}</div></div><div className="form-actions"><button className="button secondary" type="button" onClick={() => void saveMaterials()} disabled={saveState === "saving"}><Save size={16} />保存材料</button><button className="button primary" type="button" onClick={() => void precheck()} disabled={busy}><FileCheck2 size={16} />{busy ? "正在检查" : "运行材料预检"}</button></div></section><aside className="task-side-card"><h3>当前进度</h3><ProgressList task={task} /><div className="side-boundary"><ShieldCheck size={16} /><span>{task.boundary_notice}</span></div></aside></div>;
}

function FieldControl({ field, value, onChange }: { field: StudentField; value: unknown; onChange: (value: unknown) => void }) {
  const stringValue = value === null || value === undefined ? "" : String(value);
  if (field.type === "checkbox") return <label className="check-control"><input type="checkbox" checked={Boolean(value)} onChange={(event) => onChange(event.target.checked)} /><span>{field.label}{field.required ? "（必填）" : ""}</span></label>;
  if (field.type === "select") return <label className="field-control"><span>{field.label}{field.required ? "（必填）" : ""}</span><select value={stringValue} onChange={(event) => onChange(event.target.value)} aria-invalid={Boolean(field.error)}><option value="">请选择</option>{(field.options || []).map((option) => <option value={option} key={option}>{labelForOption(option)}</option>)}</select>{field.error && <small className="field-error">{field.error}</small>}</label>;
  if (field.type === "textarea") return <label className="field-control full"><span>{field.label}{field.required ? "（必填）" : ""}</span><textarea rows={3} value={stringValue} onChange={(event) => onChange(event.target.value)} aria-invalid={Boolean(field.error)} />{field.error && <small className="field-error">{field.error}</small>}</label>;
  return <label className="field-control"><span>{field.label}{field.required ? "（必填）" : ""}</span><input type={field.type === "number" ? "number" : field.type} value={stringValue} onChange={(event) => onChange(field.type === "number" ? Number(event.target.value) : event.target.value)} aria-invalid={Boolean(field.error)} />{field.error && <small className="field-error">{field.error}</small>}</label>;
}

function MaterialEditor({ material, value, onChange }: { material: StudentMaterial; value: AnyRecord; onChange: (key: string, value: unknown) => void }) {
  const notApplicable = material.status === "not_applicable";
  return <article className={`material-card ${notApplicable ? "is-muted" : ""}`}><div className="material-heading"><div><h3>{material.title}</h3><span>{material.required ? "需要准备" : "按事项情况填写"}</span></div><span className={`material-status status-${material.status}`}>{materialStatusLabels[material.status] || "待核验"}</span></div>{notApplicable ? <p className="material-help">当前事项不适用此材料。</p> : <><p className="material-help">{material.id === "venue_application" ? "此材料由上面的事项信息生成。" : "填写结构化内容，系统会在预检时检查缺口。"}</p>{material.id === "venue_application" ? null : <MaterialFields materialId={material.id} value={value} onChange={onChange} />}{material.missing_fields.length > 0 && <p className="missing-note"><CircleAlert size={15} />还需要填写：{material.missing_fields.join("、")}</p>}{material.correction_note && <p className="correction-note">处理意见：{material.correction_note}</p>}</>}</article>;
}

function MaterialFields({ materialId, value, onChange }: { materialId: string; value: AnyRecord; onChange: (key: string, value: unknown) => void }) {
  if (materialId === "activity_plan") return <ActivityPlanFields value={value} onChange={onChange} />;
  if (["activity_flow", "supply_list", "program_list"].includes(materialId)) {
    const key = materialId === "activity_flow" ? "steps" : "items";
    const raw = Array.isArray(value[key]) ? (value[key] as AnyRecord[]).map((item) => Object.values(item).join(" | ")).join("\n") : String(value[key] || "");
    return <label className="field-control full"><span>{materialId === "activity_flow" ? "流程安排" : "清单内容"}</span><textarea rows={5} value={raw} placeholder="每行填写一项，例如：09:00 | 签到 | 负责人" onChange={(event) => onChange(key, event.target.value)} /></label>;
  }
  if (materialId === "promotional_design") return <div className="material-fields"><label className="field-control"><span>展示文字</span><input value={String(value.text || "")} onChange={(event) => onChange("text", event.target.value)} /></label><label className="field-control"><span>规格与展示位置</span><input value={String(value.spec || "")} onChange={(event) => onChange("spec", event.target.value)} /></label></div>;
  if (materialId === "sponsor_agreement") return <div className="material-fields"><label className="field-control"><span>合作方名称</span><input value={String(value.counterparty || "")} onChange={(event) => onChange("counterparty", event.target.value)} /></label><label className="field-control"><span>合作范围</span><input value={String(value.scope || "")} onChange={(event) => onChange("scope", event.target.value)} /></label><label className="check-control"><input type="checkbox" checked={Boolean(value.terms_confirmed)} onChange={(event) => onChange("terms_confirmed", event.target.checked)} /><span>已确认合作事项和条件</span></label></div>;
  return <div className="material-fields"><label className="field-control full"><span>材料说明</span><textarea rows={3} value={String(value.content || "")} onChange={(event) => onChange("content", event.target.value)} /></label></div>;
}

function normalizeMaterialContent(id: string, content: AnyRecord): AnyRecord {
  if (["activity_flow", "supply_list", "program_list"].includes(id)) {
    const key = id === "activity_flow" ? "steps" : "items";
    if (typeof content[key] === "string") {
      const entries = String(content[key]).split("\n").map((line) => line.trim()).filter(Boolean).map((line) => {
        const parts = line.split("|").map((part) => part.trim());
        if (id === "activity_flow") return { time: parts[0] || "", label: parts[1] || parts[0] || "", owner: parts[2] || "" };
        return { name: parts[0] || "", quantity: parts[1] || "", purpose: parts[2] || "" };
      });
      return { ...content, [key]: entries };
    }
  }
  return content;
}

function ProgressList({ task }: { task: StudentTask }) {
  const steps = ["填写事项信息", "准备办理材料", "查看提交预览", "查看办理状态"];
  const current = task.status === "draft" || task.status === "collecting" || task.status === "precheck_failed" || task.status === "needs_correction" ? 0 : task.status === "ready_for_preview" || task.status === "awaiting_confirmation" ? 2 : 3;
  return <ol className="progress-list">{steps.map((step, index) => <li className={index < current ? "is-done" : index === current ? "is-current" : ""} key={step}><span>{index < current ? <CheckCircle2 size={16} /> : index + 1}</span><strong>{step}</strong></li>)}</ol>;
}

function PreviewPage() {
  const { task, taskId } = useTaskContext();
  const client = useQueryClient();
  const navigate = useNavigate();
  const [preview, setPreview] = useState<StudentPreview["preview"] | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const previewMutation = useMutation({
    mutationFn: () => api<StudentPreview>(`/api/student/tasks/${taskId}/preview`, { method: "POST" }),
    onSuccess: (payload) => setPreview(payload.preview),
  });
  const confirmMutation = useMutation({
    mutationFn: () => api<StudentTask>(`/api/student/tasks/${taskId}/confirm`, { method: "POST", body: JSON.stringify({ idempotency_key: `student-${taskId}-${preview?.preview_version}`, confirmed: true, expected_version: preview?.preview_version, preview_version: preview?.preview_version }) }),
    onSuccess: (updated) => {
      client.setQueryData(["student-task", taskId], updated);
      navigate("../status");
    },
  });

  useEffect(() => {
    if (!preview && task.status === "ready_for_preview") previewMutation.mutate();
  }, [task.status]);

  if (task.status !== "ready_for_preview" && !preview && !["awaiting_confirmation"].includes(task.status)) return <div className="work-card"><EmptyPanel icon={<Eye size={22} />} text="完成材料预检后，这里会显示提交内容。" compact /><Link className="button secondary" to="../materials">回到材料填写</Link></div>;
  if (previewMutation.isPending) return <div className="work-card"><LoadingInline /></div>;
  if (previewMutation.isError) return <div className="work-card"><InlineError error={previewMutation.error} onRetry={() => previewMutation.mutate()} /></div>;
  if (!preview) return null;
  return <div className="task-content-grid"><section className="work-card"><div className="card-heading"><div><p className="eyebrow">提交前确认</p><h2>查看提交内容</h2></div><span className="status-chip is-live">版本 {preview.preview_version}</span></div><div className="preview-notice"><Eye size={18} /><span>确认前请检查活动信息、材料和办理条件。确认后才会产生一次提交记录。</span></div><div className="preview-section"><h3>事项信息</h3><div className="preview-fields">{preview.fields.filter((field) => field.value !== "" && field.value !== null && field.value !== undefined).map((field) => <div key={field.key}><span>{field.label}</span><strong>{field.type === "checkbox" ? (field.value ? "是" : "否") : field.type === "select" ? labelForOption(String(field.value)) : String(field.value)}</strong></div>)}</div></div><div className="preview-section"><h3>办理材料</h3><div className="preview-materials">{preview.materials.filter((item) => item.status !== "not_applicable").map((item) => <div key={item.id}><span>{item.title}</span><strong>{materialStatusLabels[item.status] || item.status}</strong></div>)}</div></div><label className="confirm-check"><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} /><span>我已检查以上内容，并确认提交当前版本。</span></label>{confirmMutation.isError && <InlineError error={confirmMutation.error} onRetry={() => confirmMutation.mutate()} />}<div className="form-actions"><Link className="button secondary" to="../materials">返回修改</Link><button className="button primary" type="button" disabled={!confirmed || confirmMutation.isPending} onClick={() => confirmMutation.mutate()}><Send size={16} />{confirmMutation.isPending ? "正在提交" : "确认提交"}</button></div></section><aside className="task-side-card"><h3>确认规则</h3><ul className="plain-list"><li>预览只读，不会改变任务状态。</li><li>版本不一致时需要重新读取和检查。</li><li>提交后进入业务办理状态，不能由学生端直接改成通过。</li></ul><div className="side-boundary"><ShieldCheck size={16} /><span>{preview.boundary_notice}</span></div></aside></div>;
}

function ReconfirmationPage() {
  const { taskId, refresh } = useTaskContext();
  const client = useQueryClient();
  const navigate = useNavigate();
  const [confirmed, setConfirmed] = useState(false);
  const reconfirmation = useQuery({
    queryKey: ["student-reconfirmation", taskId],
    queryFn: () => api<StudentReconfirmation>(`/api/student/tasks/${taskId}/reconfirmation`),
  });
  const confirmMutation = useMutation({
    mutationFn: () => api<StudentTask>(`/api/student/tasks/${taskId}/reconfirmation`, {
      method: "POST",
      headers: { "Idempotency-Key": `student-reconfirmation-${taskId}-${reconfirmation.data?.version}` },
      body: JSON.stringify({ confirmed: true, expected_version: reconfirmation.data?.version }),
    }),
    onSuccess: async (updated) => {
      client.setQueryData(["student-task", taskId], updated);
      await refresh();
      navigate("../materials");
    },
  });

  if (reconfirmation.isPending) return <div className="work-card"><LoadingInline /></div>;
  if (reconfirmation.isError || !reconfirmation.data) return <div className="work-card"><InlineError error={reconfirmation.error || new Error("依据变化暂时无法读取")} onRetry={() => void reconfirmation.refetch()} /></div>;
  const detail = reconfirmation.data;
  return <div className="task-content-grid"><section className="work-card"><div className="card-heading"><div><p className="eyebrow">需要重新核对</p><h2>办理依据发生变化</h2></div><span className="large-status is-warn">{detail.severity}</span></div><div className="notice warning"><CircleAlert size={18} /><span>已有办理内容不会被覆盖。请先查看依据变化，再决定是否继续当前办理。</span></div><div className="preview-section"><h3>本次需要核对</h3><ul className="plain-list light-list">{detail.change_summary.map((item) => <li key={item}>{item}</li>)}</ul></div><div className="basis-grid"><BasisPanel label="原办理依据" basis={detail.old_basis} /><BasisPanel label="当前办理依据" basis={detail.new_basis} isCurrent /></div><div className="preview-section"><h3>已保存内容</h3><div className="preview-fields">{detail.saved_fields.filter((field) => field.value !== "" && field.value !== null && field.value !== undefined).map((field) => <div key={field.key}><span>{field.label}</span><strong>{field.type === "checkbox" ? (field.value ? "是" : "否") : field.type === "select" ? labelForOption(String(field.value)) : String(field.value)}</strong></div>)}</div><div className="preview-materials">{detail.saved_materials.filter((item) => item.status !== "not_applicable").map((item) => <div key={item.id}><span>{item.title}</span><strong>{materialStatusLabels[item.status] || item.status}</strong></div>)}</div></div><label className="confirm-check"><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} /><span>我已阅读当前办理依据，并确认继续检查材料。</span></label>{confirmMutation.isError && <InlineError error={confirmMutation.error} onRetry={() => confirmMutation.mutate()} />}<div className="form-actions"><Link className="button secondary" to="../status">返回办理状态</Link><button className="button primary" type="button" disabled={!confirmed || confirmMutation.isPending} onClick={() => confirmMutation.mutate()}><RotateCcw size={16} />{confirmMutation.isPending ? "正在确认" : "重新确认并继续"}</button></div></section><aside className="task-side-card"><h3>重新确认后</h3><ul className="plain-list"><li>当前任务会切换到新的办理依据。</li><li>已保存的字段和材料会继续保留。</li><li>需要重新预检后，才能再次查看提交内容。</li></ul><div className="side-boundary"><ShieldCheck size={16} /><span>{detail.boundary_notice}</span></div></aside></div>;
}

function BasisPanel({ label, basis, isCurrent = false }: { label: string; basis: StudentBasis; isCurrent?: boolean }) {
  return <section className={`basis-panel ${isCurrent ? "is-current" : ""}`}><span className="basis-label">{label}</span><h3>{basis.title || "办理资料"}</h3><p>{basis.publisher || "业务资料"}</p><small>{basis.published_at ? `发布时间：${basis.published_at}` : "发布时间待核验"}</small><small>{basis.freshness_state ? freshnessLabel(basis.freshness_state) : "当前性待核验"}</small></section>;
}

function StatusPage() {
  const { task } = useTaskContext();
  return <div className="task-content-grid"><section className="work-card"><div className="card-heading"><div><p className="eyebrow">办理进度</p><h2>{statusLabels[task.status] || "当前状态"}</h2></div><span className="status-chip is-live">业务状态</span></div><p className="status-description">办理状态由服务端根据当前任务和处理节点返回。学生端不能直接推进审核结果。</p>{task.review_nodes.length ? <div className="review-list">{task.review_nodes.map((node, index) => <div className="review-node" key={`${node.role}-${index}`}><span className="review-icon"><CheckCircle2 size={17} /></span><div><strong>{node.role}</strong><span>{node.result} · {node.note}</span><small>下一步：{node.next_step}</small><small>{formatDate(node.at)}</small></div></div>)}</div> : <div className="notice neutral"><ClipboardList size={17} /><span>{task.status === "submitted" ? "材料已提交，等待办理处理。" : "完成提交后，这里会显示办理节点和下一步。"}</span></div>}<div className="status-actions">{task.status === "needs_correction" ? <Link className="button primary" to="../materials"><RotateCcw size={16} />查看补正意见</Link> : task.status === "needs_reconfirmation" || task.status === "policy_changed" ? <Link className="button primary" to="../reconfirmation"><RotateCcw size={16} />重新检查办理依据</Link> : <Link className="button secondary" to="../materials">返回材料</Link>}</div></section><aside className="task-side-card"><h3>下一步</h3>{task.next_actions.map((action) => action.route ? <Link className="next-action" to={`../${action.route}`} key={action.code}><strong>{action.label}</strong><ChevronRight size={16} /></Link> : <div className="next-action" key={action.code}><strong>{action.label}</strong><ChevronRight size={16} /></div>)}<div className="side-boundary"><ShieldCheck size={16} /><span>{task.boundary_notice}</span></div></aside></div>;
}

function formatDate(value?: string) {
  if (!value) return "时间待记录";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { dateStyle: "medium", timeStyle: "short" });
}

function LoadingInline() {
  return <div className="loading-inline"><LoaderCircle className="spin" size={18} /><span>正在读取办理内容</span></div>;
}

function PublicSummary() {
  return <div className="page-width page-stack narrow"><section className="content-section"><p className="eyebrow">使用边界</p><h1>先看依据，再决定下一步。</h1><p className="lead">庆事通用脱敏、改写和虚拟化数据还原校园事务中的事项、材料、规则和状态变化，不代表已经接入学校身份、门户或审批系统。</p><div className="summary-grid"><article><ShieldCheck size={20} /><h2>学生端看到什么</h2><p>事项名称、适用范围、规定摘要、办理材料、下一步和办理状态。</p></article><article><FileCheck2 size={20} /><h2>办理如何发生</h2><p>先填写和预检，再查看提交内容，最后由你明确确认。审核节点由服务端业务状态返回。</p></article><article><CircleAlert size={20} /><h2>不会发生什么</h2><p>不会读取个人业务记录，不会保存真实凭据，也不会向学校真实系统提交申请。</p></article></div><Link className="button primary" to="/student">回到事项服务 <ArrowRight size={16} /></Link></section></div>;
}

function governanceLabel(value?: string | null) {
  return ({
    pass: "通过",
    fail: "失败",
    error: "错误",
    not_run: "尚未运行",
    needs_review: "需要复核",
    block: "阻断发布",
    release: "允许发布",
    completed: "已完成",
    partial: "部分完成",
    running: "运行中",
    queued: "排队中",
  } as Record<string, string>)[value || ""] || value || "尚未形成";
}

function GovernancePage() {
  const dashboard = useQuery({
    queryKey: ["governance-dashboard"],
    queryFn: () => api<GovernanceDashboard>("/api/governance/dashboard"),
  });
  const publicationId = dashboard.data?.latest_evaluation?.publication_id;
  const gate = useQuery({
    queryKey: ["governance-gate", publicationId],
    queryFn: () => api<GovernanceGate>(`/api/governance/releases/${publicationId}/gate`),
    enabled: Boolean(publicationId) && !dashboard.isError,
  });

  if (dashboard.isPending) return <LoadingScreen message="正在读取治理摘要" />;
  if (dashboard.isError || !dashboard.data) {
    const restricted = dashboard.error instanceof ApiError && ["governance_auth_required", "governance_role_forbidden"].includes(dashboard.error.code);
    return <div className="page-width page-stack narrow"><section className="content-section"><p className="eyebrow">受控入口</p><h1>治理工作台</h1><p className="lead">这里仅展示脱敏的质量、运行和发布摘要，不提供学生端治理操作。</p><div className="notice neutral"><ShieldCheck size={18} /><span>{restricted ? "需要受控治理会话。学生端不会提供角色切换或治理写入口。" : "治理摘要暂时无法读取，请稍后重试。"}</span></div><Link className="button secondary" to="/student">回到学生服务</Link></section></div>;
  }

  const latest = dashboard.data.latest_evaluation;
  const summary = latest?.summary;
  const openBadCases = (dashboard.data.bad_cases || []).filter((item) => !["closed", "rejected", "wont_fix"].includes(item.status || "")).length;
  return <div className="page-width page-stack"><section className="page-intro"><div className="breadcrumbs"><span>治理摘要</span><span className="crumb-divider">/</span><span>只读视图</span></div><div className="intro-row"><div><p className="eyebrow">受控治理</p><h1>质量与运行摘要。</h1><p className="hero-lede">此页面只读取服务端脱敏治理投影。评测、业务审核和发布写操作仍由各自绑定的治理角色负责。</p></div><div className="intro-aside"><span className="status-dot" /><strong>只读</strong><span>不改变业务状态</span></div></div></section><div className="summary-grid governance-summary"><article><ActivityIcon /><h2>最近质量门</h2><strong className="governance-metric">{governanceLabel(summary?.quality_gate)}</strong><p>{latest ? `${summary?.total || 0} 个案例 · ${governanceLabel(latest.status)}` : "还没有评测运行"}</p></article><article><CircleAlert size={20} /><h2>开放 Bad Case</h2><strong className="governance-metric">{openBadCases}</strong><p>只显示脱敏数量和状态摘要。</p></article><article><ClipboardList size={20} /><h2>运行记录</h2><strong className="governance-metric">{dashboard.data.run_total || 0}</strong><p>数据集：{dashboard.data.dataset?.version || "尚未登记"}</p></article></div><section className="content-section governance-detail"><div className="section-heading"><div><span className="section-index">01</span><div><p className="eyebrow">发布门</p><h2>当前版本的独立门结果</h2></div></div><span className={`status-chip ${gate.data?.decision === "release" ? "is-live" : "is-warn"}`}>{governanceLabel(gate.data?.decision)}</span></div>{gate.isPending ? <LoadingInline /> : gate.isError || !gate.data ? <div className="notice neutral"><CircleAlert size={17} /><span>当前没有可读取的发布门摘要。</span></div> : <div className="governance-gates"><div><span>事实源门</span><strong>{governanceLabel(gate.data.fact_source_gate)}</strong></div><div><span>质量门</span><strong>{governanceLabel(gate.data.quality_gate)}</strong></div><div><span>责任门</span><strong>{governanceLabel(gate.data.responsibility_gate)}</strong></div><div><span>P0 / P1</span><strong>{gate.data.p0_open_count || 0} / {gate.data.p1_open_count || 0}</strong></div></div>}{gate.data?.decision_reason && <p className="governance-reason">{gate.data.decision_reason}</p>}</section><Link className="button secondary" to="/student">回到学生服务</Link></div>;
}

function ActivityIcon() {
  return <Activity size={20} />;
}

function App() {
  return <QueryClientProvider client={queryClient}><BrowserRouter><Routes><Route element={<AppLayout />}><Route index element={<Navigate to="/student" replace />} /><Route path="student" element={<StudentHome />} /><Route path="student/tasks/:taskId" element={<TaskLayout />}><Route index element={<Navigate to="materials" replace />} /><Route path="materials" element={<MaterialsPage />} /><Route path="preview" element={<PreviewPage />} /><Route path="reconfirmation" element={<ReconfirmationPage />} /><Route path="status" element={<StatusPage />} /></Route><Route path="public/summary" element={<PublicSummary />} /><Route path="governance" element={<GovernancePage />} /></Route></Routes></BrowserRouter></QueryClientProvider>;
}

declare global {
  interface Window {
    QSTMascot?: { bind: () => void; setState: (state: string, text?: string) => void };
  }
}

// Keep the public entry simple: FastAPI serves the Vite output and the API remains the fact source.
import { createRoot } from "react-dom/client";
createRoot(document.getElementById("root")!).render(<App />);
