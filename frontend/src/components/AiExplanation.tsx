import { providerLabel } from "../format";
import type { AIExplanation, Analysis } from "../types";
import { Chip, SeverityBadge } from "./common";

export type ExplanationState =
  | { status: "idle" | "loading" }
  | { status: "ready"; data: AIExplanation }
  | { status: "error" };

/**
 * The explanation is requested separately from the deterministic analysis, so this panel has its own
 * pending / unavailable / ready states. Whatever happens here, the findings above are already final.
 */
export function AiExplanationLayer({
  analysis,
  explanation,
  onRetry,
}: {
  analysis: Analysis | null;
  explanation: ExplanationState;
  onRetry: () => void;
}) {
  const ai = explanation.status === "ready" ? explanation.data : null;
  const severityOf = (ruleId: string) => analysis?.findings.find((f) => f.ruleId === ruleId)?.severity;

  return (
    <section className="layer ai" aria-labelledby="layer-ai">
      <header>
        <span className="step">Layer 3</span>
        <h3 id="layer-ai">AI explanation</h3>
        {ai && (
          <span className="ai-mode">{ai.mode === "llm" ? providerLabel(ai.model) : "Mock explanation · no model used"}</span>
        )}
        <span className="hint">AI explanation generated only from the deterministic findings above</span>
      </header>
      <div className="body">
        {!analysis && <p className="placeholder">Shown after the deterministic analysis completes.</p>}
        {analysis && explanation.status === "loading" && (
          <p className="placeholder" role="status">
            <span className="spinner" aria-hidden /> Generating the explanation… The deterministic findings above are
            already complete.
          </p>
        )}
        {analysis && explanation.status === "error" && (
          <div className="ai-fallback" role="alert">
            <p>
              The AI explanation could not be retrieved. The deterministic findings above are complete and
              unaffected.
            </p>
            <button className="note-tab" onClick={onRetry}>
              Retry explanation
            </button>
          </div>
        )}
        {ai && (
          <>
            {ai.fallbackReason && (
              <div className="ai-fallback" role="note">
                <p>{ai.fallbackReason} A deterministic summary is shown instead.</p>
                <button className="note-tab" onClick={onRetry}>
                  Retry explanation
                </button>
              </div>
            )}
            <div className="ai-block">
              <h4>Summary</h4>
              <p>{ai.summary}</p>
            </div>
            {ai.findingExplanations.map((e) => {
              const sev = severityOf(e.ruleId);
              return (
                <div className="ai-block" key={e.ruleId}>
                  <h4>
                    <Chip code>{e.ruleId}</Chip>
                    {sev && <SeverityBadge level={sev} />}
                    <span className="muted" style={{ fontWeight: 400 }}>
                      severity is set by the rule engine
                    </span>
                  </h4>
                  <p>{e.explanation}</p>
                </div>
              );
            })}
            {ai.dataGapExplanation && (
              <div className="ai-block">
                <h4>Data gap</h4>
                <p>{ai.dataGapExplanation}</p>
              </div>
            )}
            <p className="ai-note">
              AI explains existing findings and supporting evidence only. It cannot create findings, change severity,
              infer missing values, or independently make treatment recommendations. Clinical notes are used as
              context only.
            </p>
          </>
        )}
      </div>
    </section>
  );
}
