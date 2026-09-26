import { useState } from "react";
import { ApiError, api } from "../api";
import type { Analysis, FhirPersistResult } from "../types";

type PersistState =
  | { status: "idle" | "loading" }
  | { status: "done"; data: FhirPersistResult }
  | { status: "disabled" }
  | { status: "error" };

/** A prior persist (this session or a previous one, restored from the saved analysis) already succeeded. */
function fromReceipt(analysis: Analysis): PersistState | null {
  const r = analysis.fhirPersistence;
  if (!r) return null;
  return { status: "done", data: { status: r.status, detectedIssueIds: r.detectedIssueIds, riskAssessmentId: r.riskAssessmentId } };
}

/**
 * Explicit, separate write-back action -- never triggered automatically by Analyze/Re-run. Persists ONLY the
 * deterministic findings/risk assessment already shown above; the AI panel (Layer 3) is never read here.
 * Shown whenever a completed deterministic analysis exists (even a NONE-severity result has a qualitative risk
 * roll-up that is a legitimate, real deterministic output worth persisting) -- hidden before Analyze has run.
 *
 * The caller renders this with `key={analysis.analysisId}` so it remounts (and its state resets) whenever a
 * different analysis instance is shown -- a freshly re-run analysis always starts with a clean, enabled Save
 * action, while returning to a previously-persisted analysis immediately shows its already-saved state via
 * `analysis.fhirPersistence`, without another click or another FHIR write.
 */
export function PersistAction({ patientId, analysis }: { patientId: string; analysis: Analysis }) {
  const [state, setState] = useState<PersistState>(() => fromReceipt(analysis) ?? { status: "idle" });

  if (analysis.status !== "COMPLETED") return null;

  const onSave = async () => {
    setState({ status: "loading" });
    try {
      const data = await api.persist(patientId, analysis.analysisId);
      setState({ status: "done", data });
    } catch (e) {
      setState({ status: e instanceof ApiError && e.status === 409 ? "disabled" : "error" });
    }
  };

  const already = state.status === "done";

  return (
    <div className="persist-action">
      <div className="persist-row">
        {already ? (
          <button className="btn-secondary is-done" disabled aria-disabled="true">
            Saved to HealthLake
          </button>
        ) : (
          <button className="btn-secondary" onClick={onSave} disabled={state.status === "loading"}>
            {state.status === "loading" && <span className="spinner" aria-hidden />}
            {state.status === "loading" ? "Saving…" : "Save analysis results to HealthLake"}
          </button>
        )}
        <span className="muted persist-caption">Writes to the synthetic demo FHIR datastore only</span>
      </div>
      {state.status === "done" && (
        <div className="persist-result" role="status">
          <p className="persist-result-heading">Saved to the synthetic demo FHIR datastore</p>
          <p>
            DetectedIssue IDs:{" "}
            {state.data.detectedIssueIds.length > 0 ? (
              <span className="mono">{state.data.detectedIssueIds.join(", ")}</span>
            ) : (
              "none (no findings)"
            )}
          </p>
          <p>RiskAssessment ID: <span className="mono">{state.data.riskAssessmentId ?? "—"}</span></p>
        </div>
      )}
      {state.status === "disabled" && (
        <p className="persist-result muted" role="note">
          FHIR write-back is disabled in this deployment (the deterministic findings above are unaffected).
        </p>
      )}
      {state.status === "error" && (
        <p className="error" role="alert">
          Could not save to the clinical data store. The deterministic findings above are unaffected.
        </p>
      )}
    </div>
  );
}
