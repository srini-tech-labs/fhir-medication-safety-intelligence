import type { AIExplanation, Analysis, DocumentContent, FhirPersistResult, PatientSummary, Snapshot } from "./types";

// Empty in dev (Vite proxies /v1). Set VITE_API_BASE_URL to the API Gateway URL for deployment.
const BASE = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "") ?? "";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, init);
  } catch {
    throw new ApiError(0, "Cannot reach the API. Is the backend running?");
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<T>;
}

export const api = {
  health: () => request<{ status: string }>("/v1/health"),
  listPatients: async () => (await request<{ patients: PatientSummary[] }>("/v1/patients")).patients,
  snapshot: (id: string) => request<Snapshot>(`/v1/patients/${encodeURIComponent(id)}/snapshot`),
  /** Deterministic result only; returns without waiting for any AI. */
  analyze: (id: string) => request<Analysis>(`/v1/patients/${encodeURIComponent(id)}/analyses`, { method: "POST" }),
  /** This patient's most recently saved analysis (whatever is already stored server-side), if any. Never runs
   * the rules or calls the AI provider -- a plain read, used to restore state across navigation/refresh. Throws
   * ApiError with status 404 when no analysis has been run for this patient yet (not an error condition). */
  latest: (id: string) => request<Analysis>(`/v1/patients/${encodeURIComponent(id)}/analyses/latest`),
  /** AI explanation of a saved analysis, requested separately (may be slow or unavailable). */
  explain: (patientId: string, analysisId: string) =>
    request<AIExplanation>(
      `/v1/patients/${encodeURIComponent(patientId)}/analyses/${encodeURIComponent(analysisId)}/explanation`,
      { method: "POST" },
    ),
  document: (patientId: string, docId: string) =>
    request<DocumentContent>(`/v1/patients/${encodeURIComponent(patientId)}/documents/${encodeURIComponent(docId)}`),
  /** Explicit write-back: persists ONLY the deterministic findings/risk assessment of a saved analysis to the
   * clinical FHIR store. Never called automatically -- a distinct, deliberate user action. */
  persist: (patientId: string, analysisId: string) =>
    request<FhirPersistResult>(
      `/v1/patients/${encodeURIComponent(patientId)}/analyses/${encodeURIComponent(analysisId)}/persist`,
      { method: "POST" },
    ),
};
