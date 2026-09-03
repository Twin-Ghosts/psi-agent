import {
  ArrowLeft,
  ArrowRight,
  Check,
  FileArchive,
  Paperclip,
  Plus,
  Search,
  Send,
  Sparkles,
  SquareStack,
  X,
} from "lucide-react";
import { type ClipboardEvent, type FormEvent, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { NEW_TASK_PRESETS } from "./demo-fixtures";
import { type Task, type TaskTemplate } from "./model";
import { AgentMark, BrandLogo } from "./primitives";
import { filesFromClipboard } from "../services/clipboardFiles";
import { useComposerFileDrop } from "../services/composerFileDrop";
import { onComposerEnterKey } from "../services/composerKeys";
import { useI18n } from "../i18n";

export function NewTaskWorkspace({
  draft,
  category,
  setDraft,
  setCategory,
  onBack,
  onOpenTemplates,
  onCreate,
  onViewTask,
  showTemplatesEntry = true,
  backLabel,
}: {
  draft: string;
  category: string;
  setDraft: (value: string) => void;
  setCategory: (value: string) => void;
  onBack: () => void;
  onOpenTemplates: () => void;
  /** Same path as overview chat: text + File[] go into the first Session chat turn. */
  onCreate: (title: string, category: string, files?: File[]) => Task | Promise<Task>;
  onViewTask: (task: Task) => void;
  /** When false, hide「从任务模板开始」(overview/templates temporarily off). */
  showTemplatesEntry?: boolean;
  /** Override back-button label (default: 返回任务总览). */
  backLabel?: string;
}) {
  const { t } = useI18n();
  const [typing, setTyping] = useState(false);
  const [attachments, setAttachments] = useState<File[]>([]);
  const attachmentRef = useRef<HTMLInputElement | null>(null);
  const presets = NEW_TASK_PRESETS.map((preset) => ({
    ...preset,
    label: t(`preset.${preset.id}.label`),
    prompt: t(`preset.${preset.id}.prompt`),
    category: t(`preset.${preset.id}.category`),
  }));
  const { isFileDragOver, dropProps } = useComposerFileDrop({
    enabled: !typing,
    onFiles: (files) => setAttachments((current) => [...current, ...files]),
  });

  const canSend = Boolean(draft.trim() || attachments.length);

  const submitConversation = async (event: FormEvent) => {
    event.preventDefault();
    const clean = draft.trim();
    if ((!clean && !attachments.length) || typing) return;

    const pendingFiles = attachments;
    setDraft("");
    setAttachments([]);
    setTyping(true);

    try {
      // First message creates the Session and jumps into split focus (left context + right chat).
      const task = await onCreate(clean, category, pendingFiles);
      onViewTask(task);
    } catch {
      setDraft(clean);
      setAttachments(pendingFiles);
      setTyping(false);
    }
  };

  return (
    <section className="new-task-workspace" aria-label={t("newTask.aria")}>
      <div className="new-task-ambient one" />
      <div className="new-task-ambient two" />

      <div className="new-task-center">
        <div className="new-task-brand" aria-hidden="true">
          <BrandLogo size="hero" />
          <span>HAITUN AGENT</span>
        </div>

        <div className="new-task-greeting">
          <span className="eyebrow">{t("newTask.eyebrow")}</span>
          <h1>{t("newTask.title")}</h1>
          <p>{t("newTask.desc")}</p>
        </div>

        <div
          className={`new-task-compose-block${isFileDragOver ? " is-file-drag-over" : ""}`}
          {...dropProps}
        >
          {!typing && (
            <div className="new-task-presets">
              {presets.map((preset) => {
                const Icon = preset.icon;
                return (
                  <button
                    type="button"
                    key={preset.label}
                    onClick={() => {
                      setDraft(preset.prompt);
                      setCategory(preset.category);
                    }}
                  >
                    <Icon size={14} />
                    <span>{preset.label}</span>
                  </button>
                );
              })}
            </div>
          )}

          {typing && (
            <div className="new-task-compose-status" aria-live="polite">
              <AgentMark /><span className="typing"><i /><i /><i /></span>
            </div>
          )}

          {attachments.length > 0 && (
            <div className="chat-pending-files new-task-pending-files" data-attach-control>
              {attachments.map((file, index) => (
                <span className="chat-pending-chip" key={`${file.name}-${file.size}-${index}`}>
                  <Paperclip size={13} />
                  <em>{file.name}</em>
                  <button
                    type="button"
                    data-attach-control
                    disabled={typing}
                    onClick={() => setAttachments((current) => current.filter((_, i) => i !== index))}
                    aria-label={t("app.removeFile", { name: file.name })}
                  >
                    <X size={12} />
                  </button>
                </span>
              ))}
            </div>
          )}

          <form className="new-task-composer-strip" onSubmit={submitConversation}>
            <button
              type="button"
              className="chat-attach-button"
              data-attach-control
              onClick={() => attachmentRef.current?.click()}
              aria-label={t("app.attach")}
              disabled={typing}
            >
              <Paperclip size={20} />
            </button>
            <input
              ref={attachmentRef}
              type="file"
              multiple
              hidden
              data-attach-control
              onChange={(event) => {
                const next = event.target.files ? Array.from(event.target.files) : [];
                if (next.length) setAttachments((current) => [...current, ...next]);
                event.target.value = "";
              }}
            />
            <textarea
              rows={1}
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onPaste={(event: ClipboardEvent<HTMLTextAreaElement>) => {
                if (typing) return;
                const files = filesFromClipboard(event.clipboardData);
                if (!files.length) return;
                setAttachments((current) => [...current, ...files]);
                const text = event.clipboardData.getData("text/plain");
                if (!text) event.preventDefault();
              }}
              onKeyDown={(event) => {
                if (typing) return;
                const el = event.currentTarget;
                onComposerEnterKey(event, draft, (next, cursor) => {
                  setDraft(next);
                  queueMicrotask(() => {
                    el.selectionStart = el.selectionEnd = cursor;
                  });
                });
              }}
              placeholder={typing ? t("newTask.creating") : t("newTask.placeholder")}
              aria-label={t("newTask.ariaComposer")}
              autoFocus
              disabled={typing}
            />
            <button type="submit" className="send-button" disabled={!canSend || typing} aria-label={t("newTask.sendAria")}>
              <Send size={16} />
            </button>
          </form>
        </div>

        <div className="new-task-secondary-actions">
          {showTemplatesEntry ? (
            <button type="button" onClick={onOpenTemplates} disabled={typing}><SquareStack size={15} /> {t("newTask.startFromTemplate")}</button>
          ) : <span />}
          <button type="button" onClick={onBack} disabled={typing}>
            <ArrowLeft size={15} /> {backLabel ?? t("app.backOverview")}
          </button>
        </div>
      </div>
    </section>
  );
}
export function TemplateLibrary({
  templates,
  initialSearch = "",
  onBack,
  onUseTemplate,
  onCreateTemplate,
}: {
  templates: TaskTemplate[];
  initialSearch?: string;
  onBack: () => void;
  onUseTemplate: (template: TaskTemplate) => void;
  onCreateTemplate: (title: string, category: string, prompt: string) => void;
}) {
  const { t } = useI18n();
  const [searchText, setSearchText] = useState(initialSearch);
  const [builderOpen, setBuilderOpen] = useState(false);
  const [templateTitle, setTemplateTitle] = useState("");
  const [templateCategory, setTemplateCategory] = useState(t("templates.custom"));
  const [templatePrompt, setTemplatePrompt] = useState("");
  const filteredTemplates = templates.filter((template) => `${template.title}${template.category}${template.description}`.includes(searchText.trim()));

  const saveTemplate = (event: FormEvent) => {
    event.preventDefault();
    if (!templateTitle.trim() || !templatePrompt.trim()) return;
    onCreateTemplate(templateTitle.trim(), templateCategory.trim() || t("templates.custom"), templatePrompt.trim());
    setTemplateTitle("");
    setTemplatePrompt("");
    setBuilderOpen(false);
  };

  return (
    <section className="template-library" aria-label={t("templates.aria")}>
      <header className="template-library-header">
        <div>
          <span className="eyebrow">{t("templates.eyebrow")}</span>
          <h1>{t("templates.title")}</h1>
          <p>{t("templates.desc")}</p>
        </div>
        <button type="button" className="template-create-button" onClick={() => setBuilderOpen(true)}><Plus size={17} /> {t("templates.create")}</button>
      </header>

      <div className="template-toolbar">
        <label><Search size={16} /><input value={searchText} onChange={(event) => setSearchText(event.target.value)} placeholder={t("templates.searchPlaceholder")} /></label>
        <span>{t("templates.count", { count: templates.length })}</span>
      </div>

      <div className="template-grid">
        {filteredTemplates.map((template) => {
          const Icon = template.icon;
          return (
            <article className="template-card" key={template.id}>
              <div className="template-card-top"><span className="template-icon"><Icon size={19} /></span><span className="template-category">{template.category}</span></div>
              <h2>{template.title}</h2>
              <p>{template.description}</p>
              <div className="template-output"><FileArchive size={14} /><span>{template.deliverables.join(" · ")}</span></div>
              <footer><span>{template.cadence}</span><button type="button" onClick={() => onUseTemplate(template)}>{t("templates.use")} <ArrowRight size={14} /></button></footer>
            </article>
          );
        })}
      </div>

      <button type="button" className="template-back-link" onClick={onBack}><ArrowLeft size={15} /> {t("app.backOverview")}</button>

      {builderOpen && createPortal(
        <div className="template-builder-layer" role="presentation">
          <button type="button" className="template-builder-scrim" onClick={() => setBuilderOpen(false)} aria-label={t("templates.closeBuilder")} />
          <aside className="template-builder" role="dialog" aria-modal="true" aria-label={t("templates.builderAria")}>
            <header><div><span className="eyebrow">{t("templates.builderEyebrow")}</span><h2>{t("templates.builderTitle")}</h2></div><button type="button" className="icon-button" onClick={() => setBuilderOpen(false)} aria-label={t("templates.close")}><X size={19} /></button></header>
            <form onSubmit={saveTemplate}>
              <label><span>{t("templates.nameLabel")}</span><input value={templateTitle} onChange={(event) => setTemplateTitle(event.target.value)} placeholder={t("templates.namePlaceholder")} autoFocus /></label>
              <label><span>{t("templates.sceneLabel")}</span><input value={templateCategory} onChange={(event) => setTemplateCategory(event.target.value)} /></label>
              <label><span>{t("templates.promptLabel")}</span><textarea value={templatePrompt} onChange={(event) => setTemplatePrompt(event.target.value)} placeholder={t("templates.promptPlaceholder")} /></label>
              <div className="template-builder-note"><Sparkles size={14} /> {t("templates.note")}</div>
              <button type="submit" className="primary-button" disabled={!templateTitle.trim() || !templatePrompt.trim()}><Check size={16} /> {t("templates.save")}</button>
            </form>
          </aside>
        </div>,
        document.body,
      )}
    </section>
  );
}
