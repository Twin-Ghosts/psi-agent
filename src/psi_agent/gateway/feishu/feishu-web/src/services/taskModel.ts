import type { SessionInfo, SessionTodo, TodoSegmentSummary, TodoSummary } from "../api";
import type { Task } from "../types";

/**
 * 会话 + todo 汇总 → UI 的 Task 模型。
 *
 * 「任务」在后端没有独立实体: 一个会话就是一个任务, 进度来自它的 todo 汇总。这里是纯函数,
 * 数据获取在 useTasks —— PR 版把这套推导直接写在 App.tsx 的 render 里, 每次输入都重算。
 */

export function progressOf(summary: TodoSummary | undefined): { progress: number; indeterminate: boolean } {
  const total = summary?.total || 0;
  if (!total) return { progress: 0, indeterminate: false };
  const done = (summary?.completed || 0) + (summary?.cancelled || 0);
  return { progress: Math.round((done / total) * 100), indeterminate: false };
}

export function statusOf(summary: TodoSummary | undefined): string {
  const total = summary?.total || 0;
  if (!total) return "待开始";
  const done = (summary?.completed || 0) + (summary?.cancelled || 0);
  if (done >= total) return "已完成";
  if (summary?.in_progress) return "进行中";
  return "待处理";
}

/** ISO 时间戳 → 列表里显示的相对时间。 */
export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "";
  const mins = Math.floor((Date.now() - then) / 60000);
  if (mins < 1) return "刚刚";
  if (mins < 60) return `${mins} 分钟前`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours} 小时前`;
  return `${Math.floor(hours / 24)} 天前`;
}

function stepStateOf(status: string): "done" | "working" | "waiting" {
  if (status === "completed") return "done";
  if (status === "in_progress") return "working";
  return "waiting";
}

export interface TaskSource {
  session: SessionInfo;
  title: string;
  summary?: string;
  todos: TodoSummary | undefined;
  todoItems: SessionTodo[];
  segments: TodoSegmentSummary[];
  files: string[];
  newDeliverables: string[];
  /** 会话是否来自 IM(``from_im``)。 */
  fromIm: boolean;
}

export function buildTask(src: TaskSource): Task {
  const { progress, indeterminate } = progressOf(src.todos);
  const status = statusOf(src.todos);
  const latest = src.segments.at(-1);
  const activeItems = (src.todoItems || []).filter((todo) => todo.status !== "cancelled");
  const todoSteps = activeItems.map((todo) => ({
    t: todo.content,
    s: stepStateOf(todo.status),
    ...(todo.status === "in_progress" ? { detail: todo.content } : {}),
  }));
  const working = todoSteps.some((step) => step.s === "working");
  const idle = activeItems.length === 0 && src.files.length === 0;
  const steps = activeItems.length
    ? todoSteps
    : src.files.length
      ? [{ t: "本轮已完成", s: "done" as const }]
      : [{ t: "待继续", s: "waiting" as const, detail: "等待你的下一条" }];
  const phase = idle || activeItems.length ? ("advance" as const) : ("done" as const);
  const phaseLabel = idle
    ? "待继续"
    : activeItems.length
      ? (latest?.label || "推进中")
      : "本轮已完成";
  return {
    id: src.session.id,
    title: src.title || "未命名任务",
    ...(src.summary ? { summary: src.summary } : {}),
    status,
    newDeliverables: src.newDeliverables,
    deliveryState: src.newDeliverables.length ? "ready" : src.files.length ? "saved" : "none",
    progress,
    indeterminate,
    ...(src.todos?.total ? { progressLabel: `${src.todos.completed}/${src.todos.total}` } : {}),
    hasTodoTrack: activeItems.length > 0 || src.segments.length > 0,
    sop: latest?.label || "自动流程",
    owner: "海豚",
    updated: relativeTime(latest?.updated_at) || (idle ? "待继续" : working ? "进行中" : activeItems.length ? "已同步" : "本轮回复已完成"),
    files: src.files,
    steps,
    phase,
    phaseLabel,
    fromIm: src.fromIm,
    // 上下文将满只对 IM 那条有意义: 网页新建的会话各有独立 jsonl, 不会替别人长。
    // 判据先用「历史消息条数」的替身 —— 后端目前不下发 token 用量, 故以 ``from_im``
    // 为唯一触发条件, 提示文案写成「这条会话与飞书对话共用, 会一直变长」。
    contextWarning: src.fromIm,
  };
}

export const TASK_FILTERS = ["all", "working", "attention", "done"] as const;
export type TaskFilter = (typeof TASK_FILTERS)[number];

export function filterTasks(tasks: Task[], filter: string, search: string): Task[] {
  const q = search.trim().toLowerCase();
  return tasks.filter((t) => {
    if (filter === "working" && t.status !== "进行中") return false;
    if (filter === "attention" && t.status !== "待处理") return false;
    if (filter === "done" && t.status !== "已完成") return false;
    if (!q) return true;
    return (
      t.title.toLowerCase().includes(q) ||
      (t.summary || "").toLowerCase().includes(q) ||
      (t.sop || "").toLowerCase().includes(q) ||
      (t.owner || "").toLowerCase().includes(q) ||
      (t.files || []).some((f) => f.toLowerCase().includes(q))
    );
  });
}

export function countTasks(tasks: Task[]): Record<string, number> {
  return {
    all: tasks.length,
    working: tasks.filter((t) => t.status === "进行中").length,
    attention: tasks.filter((t) => t.status === "待处理").length,
    done: tasks.filter((t) => t.status === "已完成").length,
  };
}
