import { useEffect, useState, type CSSProperties } from "react";
import "./first-run-spotlight.css";
import { useI18n } from "../i18n";

type SpotlightStep = 1 | 2 | 3 | 4;

type Props = {
  step: SpotlightStep;
  onConfirm: () => void;
  onSkip: () => void;
};

const STEP_TARGETS: Record<SpotlightStep, string> = {
  1: ".signal-controls > button:nth-child(1)",
  2: ".signal-controls > button:nth-child(2)",
  3: ".user-hub-shortcuts > button:nth-child(1)",
  4: ".user-hub-shortcuts > button:nth-child(2)",
};

const TOOLTIP_WIDTH = 504;
const TOOLTIP_MARGIN = 16;
const TOOLTIP_HEIGHT = 244;

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

export default function FirstRunSpotlight({ step, onConfirm, onSkip }: Props) {
  const { t } = useI18n();
  const [rect, setRect] = useState<DOMRect | null>(null);

  useEffect(() => {
    const update = () => {
      const el = document.querySelector(STEP_TARGETS[step]);
      setRect(el ? el.getBoundingClientRect() : null);
    };
    update();
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    return () => {
      window.removeEventListener("resize", update);
      window.removeEventListener("scroll", update, true);
    };
  }, [step]);

  const copy = {
    title: t(`firstRunSpot.step${step}.title`),
    desc: t(`firstRunSpot.step${step}.desc`),
  };
  const below = step <= 2;
  const tooltipWidth = Math.min(TOOLTIP_WIDTH, window.innerWidth - TOOLTIP_MARGIN * 2);
  const tooltipStyle: CSSProperties = { left: TOOLTIP_MARGIN, top: TOOLTIP_MARGIN };
  if (rect) {
    const left = clamp(
      rect.left + rect.width / 2 - tooltipWidth / 2,
      TOOLTIP_MARGIN,
      Math.max(TOOLTIP_MARGIN, window.innerWidth - tooltipWidth - TOOLTIP_MARGIN),
    );
    const arrowLeft = clamp(rect.left + rect.width / 2 - left, 26, Math.max(26, tooltipWidth - 26));
    tooltipStyle.left = left;
    tooltipStyle.top = below
      ? rect.bottom + 18
      : clamp(rect.top - TOOLTIP_HEIGHT, TOOLTIP_MARGIN, Math.max(TOOLTIP_MARGIN, window.innerHeight - TOOLTIP_HEIGHT));
    (tooltipStyle as Record<string, string | number>)["--spotlight-arrow-left"] = `${arrowLeft}px`;
  }

  return (
    <div className="spotlight-layer" role="dialog" aria-modal="true" aria-label={copy.title}>
      <button type="button" className="spotlight-skip" onClick={onSkip}>
        {t("firstRunSpot.skip")}
      </button>
      {rect && (
        <div
          className="spotlight-hole"
          style={{
            left: rect.left - 6,
            top: rect.top - 6,
            width: rect.width + 12,
            height: rect.height + 12,
          }}
        />
      )}
      {rect && (
        <div className={`spotlight-tooltip ${below ? "below" : "above"}`} style={tooltipStyle}>
          <div className="spotlight-tooltip-body">
            <h3>{copy.title}</h3>
            <p>{copy.desc}</p>
          </div>
          <div className="spotlight-actions">
            <button type="button" className="spotlight-confirm" onClick={onConfirm}>
              {t("firstRunSpot.gotIt")}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
