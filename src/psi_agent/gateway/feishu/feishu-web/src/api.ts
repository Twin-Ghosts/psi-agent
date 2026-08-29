/**
 * 数据层 —— 只封装**后端确实存在**的路由。
 *
 * 判据是 ``gateway`` 下的 ``add_get/add_post/add_delete`` 声明, 不是 PR 里写了什么:
 * PR 版打的 ``POST /auth/feishu`` 全库零实现 (唯一命中是文档里那句「不做」), 所以这里
 * 没有任何登录相关函数。飞书免登 (JSSDK 换 code、身份隔离) 归任务 5fef7, 落地后在这里
 * 补 login 一族即可 —— 本文件的其余部分不受影响。
 */

export interface AiInfo {
  id: string;
  provider: string;
  model: string;
  base_url?: string;
}

export interface SessionInfo {
  id: string;
  backend_type?: string;
  backend_id?: string;
  workspace?: string;
  agent?: string;
  ai_id?: string;
}

export interface HistoryMessage {
  role: string;
  text: string;
  reasoning?: string;
  tools?: Array<{ name: string; arguments?: string }>;
  sends?: string[];
  files?: Array<{ name: string; path?: string }>;
}

export interface SessionTodo {
  id: string;
  content: string;
  status: string;
}

export interface TodoSummary {
  total: number;
  pending: number;
  in_progress: number;
  completed: number;
  cancelled: number;
}

export interface SessionTodosResponse {
  todos: SessionTodo[];
  summary: TodoSummary;
}

export interface TodoSegmentSummary {
  id: string;
  label: string;
  created_at: string;
  updated_at: string;
  closed_at: string | null;
  source: string;
  summary: TodoSummary;
}

export interface TodoSegmentDetail extends TodoSegmentSummary {
  todos: SessionTodo[];
}

export interface WorkspaceFile {
  name: string;
  data: string;
  path: string;
}

interface ApiError {
  error?: string;
}

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(url, init);
  const data = (await resp.json().catch(() => ({}))) as T & ApiError;
  if (!resp.ok) throw new Error((data as ApiError).error || `HTTP ${resp.status}`);
  return data;
}

function jsonPost(body: unknown): RequestInit {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

/** 后端列表端点有 ``[...]`` 与 ``{value: [...]}`` 两种形状, 统一成数组。 */
function asList<T>(data: T[] | { value?: T[] }): T[] {
  return Array.isArray(data) ? data : data.value || [];
}

// ---- GET /ais ----------------------------------------------------------

export async function listAis(): Promise<AiInfo[]> {
  return asList(await requestJson<AiInfo[] | { value?: AiInfo[] }>("/ais"));
}

// ---- /sessions ---------------------------------------------------------

export async function listSessions(): Promise<SessionInfo[]> {
  return asList(await requestJson<SessionInfo[] | { value?: SessionInfo[] }>("/sessions"));
}

export async function createSession(backendId: string): Promise<SessionInfo> {
  return requestJson<SessionInfo>(
    "/sessions",
    jsonPost({ backend_type: "ai", backend_id: backendId, workspace: "" }),
  );
}

export async function deleteSession(id: string): Promise<void> {
  await requestJson<unknown>(`/sessions/${encodeURIComponent(id)}`, { method: "DELETE" });
}

export async function getSessionHistory(id: string): Promise<HistoryMessage[]> {
  const data = await requestJson<HistoryMessage[] | { value?: HistoryMessage[] }>(
    `/sessions/${encodeURIComponent(id)}/history`,
  );
  return asList(data);
}

// ---- 标题 / 摘要 -------------------------------------------------------

export async function listTitles(): Promise<Record<string, string>> {
  return requestJson<Record<string, string>>("/titles");
}

export async function generateTitle(
  id: string,
  userText: string,
  assistantText: string,
): Promise<{ id: string; title: string }> {
  return requestJson<{ id: string; title: string }>(
    "/titles/generate",
    jsonPost({ id, user_text: userText, assistant_text: assistantText }),
  );
}

export async function listSummaries(): Promise<Record<string, string>> {
  return requestJson<Record<string, string>>("/summaries");
}

// ---- todo (任务进度的数据源) -------------------------------------------

export async function getSessionTodos(sessionId: string): Promise<SessionTodosResponse> {
  return requestJson<SessionTodosResponse>(`/sessions/${encodeURIComponent(sessionId)}/todos`);
}

export async function listTodoSegments(sessionId: string): Promise<TodoSegmentSummary[]> {
  const data = await requestJson<TodoSegmentSummary[] | { value?: TodoSegmentSummary[] }>(
    `/sessions/${encodeURIComponent(sessionId)}/todo-segments`,
  );
  return asList(data);
}

export async function getTodoSegment(
  sessionId: string,
  segmentId: string,
): Promise<TodoSegmentDetail> {
  return requestJson<TodoSegmentDetail>(
    `/sessions/${encodeURIComponent(sessionId)}/todo-segments/${encodeURIComponent(segmentId)}`,
  );
}

// ---- workspace ---------------------------------------------------------

export async function readWorkspaceFile(path: string): Promise<WorkspaceFile> {
  const params = new URLSearchParams({ path });
  return requestJson<WorkspaceFile>(`/workspace/file?${params.toString()}`);
}

export async function revealWorkspacePath(path: string): Promise<{ path: string }> {
  return requestJson<{ path: string }>("/workspace/reveal", jsonPost({ path }));
}
