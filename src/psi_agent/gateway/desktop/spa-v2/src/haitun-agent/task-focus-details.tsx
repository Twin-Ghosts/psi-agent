import {
  AlertCircle,
  Check,
  CheckCircle2,
  ChevronRight,
  FileArchive,
  FileText,
  History,
  ListTodo,
  MessageCircle,
  Sparkles,
  Zap,
} from "lucide-react";
import { plainTextFromMarkdown } from "../services/assistantDisplay";
import type { TodoSegmentSummary } from "../services/api";
import type { FocusHistoryItem, Task } from "./model";
import { ProgressRing, TreasureVisual } from "./primitives";
import { translate, useI18n, type Language } from "../i18n";
import { displayTaskStatusLabel, localizedTaskUpdated } from "../services/sessionBridge";

function FocusHistoryIcon({ item, task }: { item: FocusHistoryItem; task: Task | null }) {
  if (item.kind === "segment") return <ListTodo size={15} />;
  if (item.kind === "attention") return <AlertCircle size={15} />;
  if (item.kind === "delivery") return <TreasureVisual state="ready" size="mini" />;
  if (item.kind === "conversation") return <MessageCircle size={15} />;
  if (item.kind === "update") return <History size={15} />;
  if (task?.status === "completed") return <CheckCircle2 size={15} />;
  return <ProgressRing value={task?.progress ?? 0} continuous={task?.status === "continuous" || !!task?.progressIndeterminate} size="sm" showValue={false} label={task?.hasTodoTrack ? task.progressLabel : undefined} />;
}

function deliveryFileDescription(fileName: string, language: Language) {
  if (fileName.endsWith(".pdf")) return translate(language, "focus.fileDesc.pdf");
  if (fileName.endsWith(".xlsx")) return translate(language, "focus.fileDesc.xlsx");
  if (fileName.endsWith(".docx")) return translate(language, "focus.fileDesc.docx");
  return translate(language, "focus.fileDesc.generic");
}

function formatSegmentTime(iso: string, language: Language): string {
  const raw = iso.trim();
  if (!raw) return "";
  const d = new Date(raw);
  if (Number.isNaN(d.getTime())) return raw;
  return d.toLocaleString(language === "en-US" ? "en-US" : "zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function segmentProgressLabel(summary: TodoSegmentSummary["summary"] | undefined): string {
  if (!summary || summary.total <= 0) return "";
  return `${summary.completed}/${summary.total}`;
}

/**
 * Split-mode left pane: task context / history / deliverables.
 * Chat transcript lives on the right (FocusChatThread) — not duplicated here.
 */
export function TaskFocusDetails({
  task,
  tasks,
  todoSegments = [],
  selectedSegmentId = "live",
  onSelectTodoSegment,
  onOpenArtifact,
}: {
  task: Task | null;
  tasks: Task[];
  /** Session todo segments (newest first); drives「任务历史」clicks. */
  todoSegments?: TodoSegmentSummary[];
  /** ``live`` = current AppData todos; else a segment id. */
  selectedSegmentId?: string;
  onSelectTodoSegment?: (segmentId: string) => void;
  onOpenArtifact: (task: Task, fileName?: string) => void;
}) {
  const { language, t } = useI18n();
  const viewingHistory = !!task && selectedSegmentId !== "live";
  const workingStep = task?.steps.find((step) => step.state === "working");
  const checklistDone = !!task?.hasTodoTrack
    && !!task.steps.length
    && task.steps.every((step) => step.state === "done");
  const activeStep = viewingHistory
    ? (task?.hasTodoTrack
      ? (checklistDone
        ? t("focus.historySubtaskWithLabel", { label: task.progressLabel || t("focus.completed") })
        : t("focus.historySubtaskWithLabel", { label: task.progressLabel || workingStep?.label || t("focus.stepsUnit") }))
      : t("focus.historySubtask"))
    : task?.phase === "done"
    ? (task.hasTodoTrack
      ? (checklistDone ? t("focus.cleanedDone") : (workingStep?.label || t("focus.listLabel", { label: task.progressLabel || t("focus.notDone") })))
      : t("focus.roundDone"))
    : task?.phase === "deliver"
      ? t("focus.delivering")
      : task?.phase === "advance"
        ? (task.hasTodoTrack
          ? (workingStep?.detail?.trim()
            ? `${task.progressLabel ?? workingStep.label} · ${workingStep.detail.trim()}`
            : (workingStep?.label || task.progressLabel || t("focus.advancing")))
          : (workingStep?.label || t("focus.pending")))
        : workingStep
          ? workingStep.label
          : (task?.status === "completed" ? t("focus.cleanedDone") : t("focus.waitingNext"));
  const historyItems: FocusHistoryItem[] = task
    ? (todoSegments.length
      ? todoSegments.map((seg) => {
          const isOpen = !seg.closed_at;
          const nm = segmentProgressLabel(seg.summary);
          return {
            id: `seg-${seg.id}`,
            kind: "segment" as const,
            segmentId: isOpen ? "live" : seg.id,
            title: seg.label || t("focus.subtask"),
            detail: [isOpen ? t("focus.current") : t("focus.archived"), nm ? t("focus.listLabel", { label: nm }) : ""].filter(Boolean).join(" · "),
            time: formatSegmentTime(seg.updated_at || seg.created_at, language),
          };
        })
      : [{
          id: `status-${task.id}`,
          kind: "status" as const,
          title: task.hasTodoTrack
            ? (task.phase === "done"
              ? (checklistDone
                ? `${t("focus.roundDone")} · ${task.progressLabel || `${task.progress}%`}`
                : `${t("focus.roundReplied")} · ${task.progressLabel || `${task.progress}%`}`)
              : `${displayTaskStatusLabel(task.status, task.statusLabel, language)} · ${task.progressLabel || `${task.progress}%`}`)
            : (task.progressIndeterminate
              ? `${displayTaskStatusLabel(task.status, task.statusLabel, language)} · ${t("focus.processing")}`
              : task.phase === "done"
                ? (task.status === "completed" ? displayTaskStatusLabel(task.status, task.statusLabel, language) : t("focus.roundDone"))
                : displayTaskStatusLabel(task.status, task.statusLabel, language)),
          detail: "",
          time: localizedTaskUpdated(task.updated, language),
        }])
    : tasks.slice(0, 3).map((item) => ({
      id: `status-${item.id}`,
      kind: "status" as const,
      title: `${item.shortTitle} · ${displayTaskStatusLabel(item.status, item.statusLabel, language)}`,
      detail: "",
      time: localizedTaskUpdated(item.updated, language),
    }));

  const finiteTasks = tasks.filter((item) => item.status !== "continuous");
  const tracked = finiteTasks.filter((item) => item.hasTodoTrack);
  const overall = tracked.length
    ? Math.round(tracked.reduce((sum, item) => sum + item.progress, 0) / tracked.length)
    : Math.round((finiteTasks.filter((item) => item.phase === "done" || item.status === "completed").length / Math.max(finiteTasks.length, 1)) * 100);
  // Historical = all session deliverables (not only "new"/ready).
  const historicalDeliveryTasks = task
    ? (task.deliverables.length ? [task] : [])
    : tasks.filter((item) => item.deliverables.length);
  const generatingTasks = task
    ? []
    : tasks.filter((item) => item.deliveryState === "generating");
  const historicalFileCount = historicalDeliveryTasks.reduce((sum, item) => sum + item.deliverables.length, 0);
  const emptyDeliveryCopy = !task
    ? t("focus.emptyDeliveryAll")
    : task.status === "completed"
      ? t("focus.emptyDeliveryDone")
      : task.status === "continuous"
        ? t("focus.emptyDeliveryContinuous")
        : t("focus.emptyDeliveryDefault");

  return (
    <div className="focus-detail-panel">
      <section className="focus-state-banner">
        <div>
          <span><Sparkles size={13} /> {t("focus.summaryLabel")}</span>
          <strong>{task ? task.title : t("focus.allTasksContext")}</strong>
          <p>{task ? plainTextFromMarkdown(task.summary) : t("focus.summaryAll", { count: tasks.length, overall })}</p>
          {viewingHistory && onSelectTodoSegment ? (
            <p className="focus-history-viewing-hint">
              {t("focus.viewingHistoryHint")}
              <button type="button" className="focus-history-back-live" onClick={() => onSelectTodoSegment("live")}>
                {t("focus.backToLive")}
              </button>
            </p>
          ) : null}
        </div>
        <div className="focus-state-grid">
          <span><em>{t("focus.status")}</em><strong>{task ? displayTaskStatusLabel(task.status, task.statusLabel, language) : t("focus.taskCount", { count: tasks.length })}</strong></span>
          <span>
            <em>{task?.hasTodoTrack ? t("focus.stepsUnit") : task?.status === "continuous" ? t("focus.roundUnit") : t("focus.activityUnit")}</em>
            <strong>
              {task
                ? (task.hasTodoTrack
                  ? (task.progressLabel || "—")
                  : task.progressIndeterminate
                    ? t("focus.processing")
                    : task.phase === "done"
                      ? t("focus.completed")
                      : t("focus.pending"))
                : `${overall}%`}
            </strong>
          </span>
          <span><em>{t("focus.currentPhase")}</em><strong>{task ? activeStep : t("focus.attentionCount", { count: tasks.filter((item) => item.status === "attention").length })}</strong></span>
          <span><em>{t("focus.recentUpdate")}</em><strong>{task ? localizedTaskUpdated(task.updated, language) : t("focus.justSynced")}</strong></span>
        </div>
      </section>

      <section className="focus-execution-path" aria-label={task ? t("focus.executionPathAria") : t("focus.statusListAria")}>
        <header>
          <span>
            <Zap size={13} />
            {task ? (task.hasTodoTrack ? t("focus.executionSteps") : t("focus.activityStatus")) : t("focus.runningStatus")}
          </span>
        </header>
        <div>
          {(task ? task.steps : tasks.slice(0, 4).map((item) => ({ label: item.shortTitle, state: item.status === "completed" ? "done" as const : item.status === "attention" ? "waiting" as const : "working" as const, detail: undefined as string | undefined }))).map((step, index) => (
            <span className={step.state} key={`${index}-${step.label}`}>
              <i>{step.state === "done" ? <Check size={10} /> : null}</i>
              <strong>{step.label}</strong>
              <em>
                {step.state === "done"
                  ? t("focus.stepDone")
                  : step.state === "working"
                    ? (step.detail?.trim() || (task?.hasTodoTrack ? t("focus.stepInProgress") : t("focus.stepProcessing")))
                    : (step.detail?.trim() || t("focus.stepPending"))}
              </em>
            </span>
          ))}
        </div>
      </section>

      <div className="focus-detail-columns">
        <section className="focus-task-history">
          <header><div><History size={14} /><strong>{task ? t("focus.taskHistory") : t("focus.todayHistory")}</strong></div><span>{t("focus.recordCount", { count: historyItems.length })}</span></header>
          <div className="focus-history-list">
            {historyItems.map((item) => {
              const segKey = item.segmentId ?? item.id;
              const active = item.kind === "segment"
                && (
                  ((selectedSegmentId === "live") && segKey === "live")
                  || (selectedSegmentId !== "live" && segKey === selectedSegmentId)
                );
              const clickable = item.kind === "segment" && !!onSelectTodoSegment && !!item.segmentId;
              const inner = (
                <>
                  <span className="focus-history-icon"><FocusHistoryIcon item={item} task={task} /></span>
                  <div>
                    <strong>{item.title}</strong>
                    {item.detail.trim() ? <p>{item.detail}</p> : null}
                    {item.time.trim() ? <em>{item.time}</em> : null}
                  </div>
                </>
              );
              return clickable ? (
                <button
                  type="button"
                  className={`focus-history-item segment ${active ? "active" : ""}`}
                  key={item.id}
                  onClick={() => onSelectTodoSegment?.(item.segmentId!)}
                  aria-pressed={active}
                  aria-label={t("focus.viewSubtask", { title: item.title })}
                >
                  {inner}
                </button>
              ) : (
                <div className={`focus-history-item ${item.kind}`} key={item.id}>
                  {inner}
                </div>
              );
            })}
            {historyItems.length === 0 && (
              <div className="focus-history-empty">
                {task ? t("focus.noSegments") : t("focus.noExtraRecords")}
              </div>
            )}
          </div>
        </section>

        <section className="focus-delivery-history">
          <header><div><FileArchive size={14} /><strong>{task?.deliveryState === "generating" ? t("focus.deliveryProgress") : t("focus.historicalDeliveries")}</strong></div><span>{task?.deliveryState === "generating" ? t("focus.generating") : t("focus.fileCount", { count: historicalFileCount })}</span></header>
          {historicalDeliveryTasks.length ? (
            <div className="focus-delivery-groups">
              {historicalDeliveryTasks.map((owner) => {
                const hasNew = owner.newDeliverables.length > 0;
                const stateCopy = owner.deliveryState === "saved"
                  ? t("focus.savedToLibrary")
                  : hasNew
                    ? t("focus.hasNewDeliveries")
                    : owner.deliverables.length
                      ? t("focus.sessionDeliveries")
                      : t("focus.expectedDelivery");
                return (
                  <div className="focus-delivery-group" key={owner.id}>
                    {!task && <span className="focus-delivery-owner">{owner.shortTitle}</span>}
                    {owner.deliverables.map((file) => (
                      <button
                        type="button"
                        key={file}
                        onClick={() => onOpenArtifact(owner, file)}
                        aria-label={t("focus.viewDelivery", { file })}
                      >
                        <span className="focus-file-preview" aria-hidden="true"><FileText size={15} /><i /><i /><i /></span>
                        <span className="focus-file-copy"><strong>{file}</strong><em>{stateCopy} · {localizedTaskUpdated(owner.updated, language)}</em><small>{deliveryFileDescription(file, language)}</small></span>
                        <ChevronRight size={15} />
                      </button>
                    ))}
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="focus-delivery-empty"><TreasureVisual state="none" size="compact" /><p>{emptyDeliveryCopy}</p></div>
          )}
          {generatingTasks.map((owner) => (
            <div className="focus-pending-delivery" key={owner.id}><TreasureVisual state="generating" size="mini" /><span><strong>{owner.shortTitle}</strong><em>{t("focus.expectedCount", { count: owner.deliverables.length })}</em></span></div>
          ))}
        </section>
      </div>
    </div>
  );
}
