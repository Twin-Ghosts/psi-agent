import { useState } from "react";
import { Check, ChevronRight } from "lucide-react";

export interface ExecutionStep {
  label: string;
  state: "done" | "working" | "waiting";
  detail?: string;
}

/** 聊天输入区上方的可折叠执行步骤面板。todo 清单存在时才由调用方渲染。 */
export function ExecutionStepsPanel({ steps }: { steps: ExecutionStep[] }) {
  const [open, setOpen] = useState(false);

  return (
    <section className={`execution-steps-panel${open ? " is-open" : ""}`} aria-label="执行步骤">
      <button
        type="button"
        className="execution-steps-toggle"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <ChevronRight size={14} className="execution-steps-chevron" aria-hidden />
        <span className="execution-steps-title">执行步骤</span>
        <em>{steps.length > 0 ? `${steps.length} 步` : "暂无步骤"}</em>
      </button>
      <div className="execution-steps-body" role="list" aria-label="任务执行步骤" aria-live="polite">
        {steps.length > 0 ? (
          steps.map((step, index) => (
            <div className={`execution-steps-card ${step.state}`} role="listitem" key={`${index}-${step.label}`}>
              <div className="execution-steps-main">
                <span className="execution-steps-name">{step.label}</span>
                {step.detail?.trim() ? <em className="execution-steps-detail">{step.detail.trim()}</em> : null}
              </div>
              <span className="execution-steps-check" aria-hidden="true">
                {step.state === "done" ? <Check size={12} /> : null}
              </span>
            </div>
          ))
        ) : (
          <div className="execution-steps-empty">暂无执行步骤</div>
        )}
      </div>
    </section>
  );
}
