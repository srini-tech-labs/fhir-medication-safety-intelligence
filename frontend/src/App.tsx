import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import { AiExplanationLayer, type ExplanationState } from "./components/AiExplanation";
import { ClinicalData } from "./components/ClinicalData";
import { DataGapsLayer } from "./components/DataGaps";
import { FindingsLayer } from "./components/Findings";
import { PatientList } from "./components/PatientList";
import { capitalize } from "./format";
import type { Analysis, PatientSummary, Snapshot } from "./types";

type Async<T> = { status: "idle" | "loading" | "error" | "ready"; data: T | null; error: string | null };
const idle = <T,>(): Async<T> => ({ status: "idle", data: null, error: null });

// Remembers which patient was last selected, per browser -- a viewer convenience only (not app state, never
// read by the server). Lets a page refresh land back on the same patient so its restored analysis (which DOES
// come from the backend, see the patient-select effect below) is visibly still there after a reload, not just
// after in-app navigation. Wrapped in try/catch: storage can throw or be unavailable (private browsing, blocked
// site data) and must never break patient selection.
const LAST_PATIENT_KEY = "medsafety.lastPatientId";
const readLastPatientId = (): string | null => {
  try {
    return localStorage.getItem(LAST_PATIENT_KEY);
  } catch {
    return null;
  }
};
const writeLastPatientId = (id: string) => {
  try {
    localStorage.setItem(LAST_PATIENT_KEY, id);
  } catch {
    /* per-viewer convenience only -- fine to silently skip */
  }
};

export default function App() {
  const [patients, setPatients] = useState<PatientSummary[]>([]);
  const [apiState, setApiState] = useState<"checking" | "ok" | "down">("checking");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [snapshot, setSnapshot] = useState<Async<Snapshot>>(idle());
  const [analysis, setAnalysis] = useState<Async<Analysis>>(idle());
  const [explanation, setExplanation] = useState<ExplanationState>({ status: "idle" });
  const selectedRef = useRef<string | null>(null);
  const analysisRef = useRef<string | null>(null); // the analysis whose explanation we are waiting for

  const selectPatient = (id: string) => {
    setSelectedId(id);
    writeLastPatientId(id);
  };

  useEffect(() => {
    api
      .listPatients()
      .then((list) => {
        setPatients(list);
        setApiState("ok");
        if (!list.length) return;
        const last = readLastPatientId();
        setSelectedId(last && list.some((p) => p.id === last) ? last : list[0].id);
      })
      .catch(() => setApiState("down"));
  }, []);

  useEffect(() => {
    selectedRef.current = selectedId;
    analysisRef.current = null;
    setAnalysis(idle());
    setExplanation({ status: "idle" });
    if (!selectedId) return;
    let cancelled = false;
    setSnapshot({ status: "loading", data: null, error: null });
    api
      .snapshot(selectedId)
      .then((data) => !cancelled && setSnapshot({ status: "ready", data, error: null }))
      .catch((e: Error) => !cancelled && setSnapshot({ status: "error", data: null, error: e.message }));

    // Restore this patient's latest saved analysis, if one exists -- a plain read of what the backend already
    // has (works the same after a page refresh as after switching patients and back). Never re-runs the rules
    // and never calls the AI provider: if a stored explanation exists it is shown as-is, otherwise the AI panel
    // just stays unrequested until the user explicitly analyzes/re-runs.
    api
      .latest(selectedId)
      .then((data) => {
        if (cancelled || selectedRef.current !== selectedId) return;
        setAnalysis({ status: "ready", data, error: null });
        if (data.aiExplanation) setExplanation({ status: "ready", data: data.aiExplanation });
      })
      .catch(() => {
        // 404 means "no analysis yet" (not an error); anything else (e.g. the store is briefly unreachable) is
        // a restore-time convenience failing, not a hard error -- either way the panel just stays in its normal
        // not-yet-analyzed state and never blocks the user from running a fresh analysis.
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  // Step 1: deterministic analysis. Shown as soon as it returns; it never waits for the AI.
  async function analyze() {
    const id = selectedId;
    if (!id) return;
    analysisRef.current = null;
    setExplanation({ status: "idle" });
    setAnalysis({ status: "loading", data: null, error: null });
    let data: Analysis;
    try {
      data = await api.analyze(id);
    } catch (e) {
      if (selectedRef.current === id) setAnalysis({ status: "error", data: null, error: (e as Error).message });
      return;
    }
    if (selectedRef.current !== id) return;
    setAnalysis({ status: "ready", data, error: null });
    if (data.aiExplanation) setExplanation({ status: "ready", data: data.aiExplanation });
    else void requestExplanation(id, data.analysisId);
  }

  // Step 2: the explanation, requested separately. Failure or delay here cannot touch the findings above.
  async function requestExplanation(patientId: string, analysisId: string) {
    analysisRef.current = analysisId;
    setExplanation({ status: "loading" });
    const current = () => selectedRef.current === patientId && analysisRef.current === analysisId;
    try {
      const data = await api.explain(patientId, analysisId);
      if (current()) setExplanation({ status: "ready", data });
    } catch {
      if (current()) setExplanation({ status: "error" }); // fixed UI message: never render server/provider text
    }
  }

  const retryExplanation = () => {
    if (selectedId && analysis.data) void requestExplanation(selectedId, analysis.data.analysisId);
  };

  const patient = snapshot.data?.patient;

  return (
    <>
      <header className="topbar">
        <div className="logo" aria-hidden>
          <svg viewBox="0 0 32 32">
            <path d="M13 7h6v6h6v6h-6v6h-6v-6H7v-6h6z" />
          </svg>
        </div>
        <div>
          <h1>Medication Safety Intelligence</h1>
          <div className="sub">Deterministic rules · grounded AI explanation · synthetic FHIR R4 data</div>
        </div>
        <div className="spacer" />
        <div className={`api-status ${apiState === "checking" ? "" : apiState}`} role="status">
          <i /> {apiState === "ok" ? "Live API connected" : apiState === "down" ? "API unreachable" : "Checking API…"}
        </div>
        {apiState === "ok" && <div className="env-badge">AWS HealthLake</div>}
      </header>
      <div className="disclaimer" role="note">
        Synthetic demonstration data. Medication-safety findings are generated by a limited prototype rule set and are
        not intended for patient care.
      </div>

      <div className="layout">
        <PatientList patients={patients} selectedId={selectedId} onSelect={selectPatient} />
        <main className="main">
          {apiState === "down" && (
            <p className="error">
              Cannot reach the API. Start the backend (<span className="mono">uvicorn app.main:app --port 8000</span>{" "}
              in <span className="mono">backend/</span>) and reload.
            </p>
          )}
          {snapshot.status === "loading" && <p className="placeholder">Loading patient…</p>}
          {snapshot.status === "error" && <p className="error">{snapshot.error}</p>}
          {snapshot.data && patient && (
            <>
              <div className="patient-head">
                <h2>{patient.name}</h2>
                <span className="meta">
                  {patient.id} · {patient.age} years · {capitalize(patient.sex)}
                </span>
              </div>
              <ClinicalData key={patient.id} snapshot={snapshot.data} />
              <FindingsLayer
                analysis={analysis.data}
                loading={analysis.status === "loading"}
                error={analysis.error}
                onAnalyze={analyze}
              />
              <DataGapsLayer analysis={analysis.data} />
              <AiExplanationLayer analysis={analysis.data} explanation={explanation} onRetry={retryExplanation} />
            </>
          )}
        </main>
      </div>
    </>
  );
}
