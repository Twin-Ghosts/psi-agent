import { useCallback, useEffect, useMemo, useState } from "react";
import { generateTitle, revealWorkspacePath } from "./api";
import { ArtifactDrawer } from "./components/artifact-drawer";
import { ChatView } from "./components/chat-view";
import { DeliveryPreviewModal } from "./components/delivery-preview-modal";
import { NewDeliveriesPanel } from "./components/new-deliveries-panel";
import { TaskFocusDetails } from "./components/task-focus-details";
import { TasksView } from "./components/tasks-view";
import { useAuth } from "./hooks/useAuth";
import { useChatTurn } from "./hooks/useChatTurn";
import { useSessionHistory, useSessions } from "./hooks/useSessions";
import { useTasks } from "./hooks/useTasks";
import { mapHistory } from "./services/historyMap";
import "./styles.css";

type View = "tasks" | "chat";

/**
 * 应用装配层。
 *
 * 有意保持薄: 状态在 hooks/ 里 (会话 / 流式一轮 / 任务派生), 渲染在 components/ 里,
 * 这里只做「哪个视图 + 谁连谁」。PR 版是 829 行的单文件, 登录、会话列表、历史加载、
 * 流式收发、任务过滤全在一个组件里互相串状态, 所以整体重做而不是搬。
 *
 * 登录: ``useAuth`` 走飞书 JSSDK 免登, 未就绪时渲染 ``LoginGate`` 而非放行。会话列表走
 * ``/feishu/sessions``(服务端按身份过滤), 「新建任务」开的是全新 session + 全新 jsonl。
 */

/**
 * 登录门禁。免登失败时给**可见的重试入口** —— code 只活几分钟, 从后台切回来时上一个
 * 大概率已过期; 没有重试按钮用户只能刷页面。绝不静默放行成某个默认身份。
 */
function LoginGate({
  status,
  error,
  onRetry,
}: {
  status: string;
  error: string;
  onRetry: () => void;
}) {
  return (
    <div className="ht-app ht-login-gate">
      {status === "loading" ? (
        <p>正在通过飞书登录…</p>
      ) : (
        <>
          <p role="alert">登录失败: {error || "未知原因"}</p>
          <p className="ht-card-hint">请在飞书客户端内打开本应用。若已在客户端内, 点下方重试。</p>
          <button type="button" className="ht-btn" onClick={onRetry}>
            重试登录
          </button>
        </>
      )}
    </div>
  );
}

/**
 * 开发旁路告警条 —— 当前身份是后端 ``PSI_FEISHU_DEV_OPEN_ID`` 发的, 不是真免登。
 *
 * 为什么值得占一条通栏: 旁路态与真免登态页面原本长得一模一样, 有人在旁路态下把功能测完
 * 会以为自己验过了免登链路(而那一环本机根本没有 JSAPI, 压根没跑)。后端每次登录打
 * WARNING, 但只有看日志的人能看见。判据来自后端的 ``via_dev_bypass``, 前端不自己推断。
 */
function DevBypassBanner({ openId }: { openId: string }) {
  return (
    <div className="ht-dev-bypass" role="status">
      <strong>开发旁路身份: {openId}</strong>
      <span>
        后端 PSI_FEISHU_DEV_OPEN_ID 已开启, 这不是真的飞书免登 —— 免登链路本机测不了。
      </span>
    </div>
  );
}

export function App() {
  const auth = useAuth();
  if (auth.status !== "ready") {
    return <LoginGate status={auth.status} error={auth.error} onRetry={auth.retry} />;
  }
  return (
    <AuthedApp
      userName={auth.me?.name || ""}
      devBypassOpenId={auth.me?.via_dev_bypass ? auth.me.open_id : ""}
    />
  );
}

function AuthedApp({
  userName,
  devBypassOpenId,
}: {
  userName: string;
  devBypassOpenId: string;
}) {
  const [view, setView] = useState<View>("tasks");
  const [input, setInput] = useState("");
  const [pendingFiles, setPendingFiles] = useState<File[]>([]);
  const [selectedSegment, setSelectedSegment] = useState("live");
  const [artifactTaskId, setArtifactTaskId] = useState("");
  const [previewFile, setPreviewFile] = useState("");
  const [showNewDeliveries, setShowNewDeliveries] = useState(false);

  const sessions = useSessions();
  const tasks = useTasks(sessions.sessions, sessions.titles);
  const history = useSessionHistory(sessions.currentId);
  const turn = useChatTurn();

  // 历史到了就铺进消息列表 (附件路径一起接管)。流式增量之后只改 turn.messages。
  useEffect(() => {
    const { messages, filePaths } = mapHistory(history.raw);
    turn.setMessages(messages);
    turn.setFilePaths((prev) => ({ ...prev, ...filePaths }));
  }, [history.raw, turn.setMessages, turn.setFilePaths]);

  const currentTask = useMemo(
    () => tasks.tasks.find((t) => t.id === sessions.currentId),
    [tasks.tasks, sessions.currentId],
  );
  const artifactTask = useMemo(
    () => tasks.tasks.find((t) => t.id === artifactTaskId),
    [tasks.tasks, artifactTaskId],
  );
  const newDeliveryTasks = useMemo(
    () => tasks.tasks.filter((t) => t.newDeliverables.length > 0),
    [tasks.tasks],
  );

  const openChat = useCallback(
    (id: string) => {
      sessions.setCurrentId(id);
      setSelectedSegment("live");
      setView("chat");
    },
    [sessions],
  );

  const handleNewTask = useCallback(async () => {
    const id = await sessions.create();
    if (id) openChat(id);
  }, [sessions, openChat]);

  const handleSend = useCallback(async () => {
    const sessionId = sessions.currentId;
    if (!sessionId) return;
    const text = input;
    const files = pendingFiles;
    setInput("");
    setPendingFiles([]);
    await turn.send(sessionId, text, files);

    // 首轮结束后补标题, 否则列表里一直是「未命名任务」。
    if (!sessions.titles[sessionId]) {
      const assistant = turn.messages.at(-1)?.text || "";
      try {
        const { title } = await generateTitle(sessionId, text, assistant);
        sessions.setTitles((prev) => ({ ...prev, [sessionId]: title }));
      } catch {
        // 标题生成失败不影响对话本身。
      }
    }
    void tasks.refresh();
  }, [sessions, input, pendingFiles, turn, tasks]);

  const handleOpenFile = useCallback((name: string) => setPreviewFile(name), []);
  const handleReveal = useCallback((path: string) => {
    void revealWorkspacePath(path).catch(() => undefined);
  }, []);

  const listError = sessions.error || history.error;

  return (
    <div className="ht-app">
      {/* 两个视图共用这层 wrapper, 所以告警条挂这里就在任务列表和会话里都在。 */}
      {devBypassOpenId ? <DevBypassBanner openId={devBypassOpenId} /> : null}
      {view === "tasks" ? (
        <main className="ht-desktop">
          {listError ? <div className="ht-error" role="alert">{listError}</div> : null}
          <TasksView
            tasks={tasks.tasks}
            filtered={tasks.filtered}
            counts={tasks.counts}
            selected={currentTask}
            filter={tasks.filter}
            search={tasks.search}
            onFilter={tasks.setFilter}
            onSearch={tasks.setSearch}
            onSelect={sessions.setCurrentId}
            onDelete={(id) => void sessions.remove(id)}
            onOpenChat={openChat}
            onOpenNewDeliverables={() => setShowNewDeliveries(true)}
            newDeliveryCount={newDeliveryTasks.length}
            onNewTask={() => void handleNewTask()}
          />
        </main>
      ) : (
        <main className="ht-focus">
          <header className="ht-focus-top">
            <button type="button" className="ht-btn" onClick={() => setView("tasks")}>
              返回任务
            </button>
            <h2>{sessions.titles[sessions.currentId] || "未命名任务"}</h2>
          </header>
          <div className="ht-focus-split">
            <ChatView
              messages={turn.messages}
              userName={userName}
              input={input}
              sending={turn.sending}
              error={turn.error || history.error}
              pendingFiles={pendingFiles}
              emptyHint={history.loading ? "正在加载历史…" : undefined}
              onInput={setInput}
              onSend={() => void handleSend()}
              onStop={turn.stop}
              onAddFiles={(files) => setPendingFiles((prev) => [...prev, ...files])}
              onRemoveFile={(i) => setPendingFiles((prev) => prev.filter((_, idx) => idx !== i))}
              onFeedback={(index, kind) =>
                turn.setMessages((prev) =>
                  prev.map((m, i) =>
                    i === index ? { ...m, feedback: m.feedback === kind ? undefined : kind } : m,
                  ),
                )
              }
              onRegenerate={(index) => {
                const user = turn.messages[index - 1];
                if (user?.role === "user") void turn.send(sessions.currentId, user.text);
              }}
              onOpenFile={handleOpenFile}
              onRevealFile={handleReveal}
              filePathOf={turn.filePathOf}
            />
            <TaskFocusDetails
              task={currentTask || null}
              todoSegments={tasks.segments[sessions.currentId] || []}
              selectedSegmentId={selectedSegment}
              onSelectTodoSegment={setSelectedSegment}
              onOpenArtifact={(task, fileName) => {
                setArtifactTaskId(task.id);
                if (fileName) setPreviewFile("");
              }}
            />
          </div>
        </main>
      )}

      {showNewDeliveries && (
        <NewDeliveriesPanel
          tasks={newDeliveryTasks}
          onOpen={(taskId) => {
            setShowNewDeliveries(false);
            setArtifactTaskId(taskId);
          }}
          onClose={() => setShowNewDeliveries(false)}
        />
      )}

      {artifactTask && (
        <ArtifactDrawer
          taskTitle={artifactTask.title}
          files={artifactTask.files}
          filePathOf={turn.filePathOf}
          onClose={() => setArtifactTaskId("")}
        />
      )}

      {previewFile && (
        <DeliveryPreviewModal
          name={previewFile}
          path={turn.filePathOf(previewFile)}
          onClose={() => setPreviewFile("")}
        />
      )}
    </div>
  );
}
