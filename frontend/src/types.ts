// Application JSON contract (data/phase0_v1_0/api/openapi.yaml + additive fields). Not FHIR.

export type Severity = "HIGH" | "MODERATE" | "LOW";
export type OverallStatus = Severity | "NONE" | "NEEDS_DATA";

export interface PatientSummary {
  id: string;
  name: string;
  age: number;
  sex: string;
  scenarioLabel?: string | null;
}

export interface SnapshotMedication {
  name: string;
  rxCui: string | null;
  dosage: string | null;
}

export interface SnapshotLab {
  name: string;
  loinc: string | null;
  value: number;
  unit: string | null;
  interpretation: string | null;
  effectiveDate: string;
  referenceRange?: { low?: number | null; high?: number | null } | null;
}

export interface SnapshotDocument {
  id: string;
  type: string;
  date: string | null;
  title?: string | null;
}

export interface Snapshot {
  patient: { id: string; name: string; age: number; sex: string };
  medications: SnapshotMedication[];
  labs: SnapshotLab[];
  documents: SnapshotDocument[];
}

export interface DocumentContent extends SnapshotDocument {
  text: string;
}

export interface EvidenceReference {
  evidenceId: string;
  title: string;
  source: string;
  section: string;
  sourceUrl: string;
  summary: string;
  accessed: string;
  sourceType: string;
  provider: string;
}

export interface Provenance {
  ruleVersion: string;
  trigger?: { operator: string; threshold: number; unit: string; note?: string | null } | null;
  fhirResources: string[];
  evidence: EvidenceReference[];
  severityDisclaimer?: string | null;
}

export interface EvidenceMedication {
  name: string;
  rxCui: string;
  dosage?: string | null;
}

export interface EvidenceLab {
  name: string;
  loinc: string;
  value: number;
  unit: string;
  interpretation?: string | null;
  effectiveDate?: string | null;
}

export interface Finding {
  findingId: string;
  ruleId: string;
  type: "DRUG_DRUG" | "DRUG_LAB";
  severity: Severity;
  title: string;
  evidence: { medications: EvidenceMedication[]; labs: EvidenceLab[] };
  risk: string;
  source: { type: string; provider: string; evidenceIds: string[] };
  provenance: Provenance;
  fhir?: { detectedIssueId: string } | null;
}

export interface DataGap {
  gapId: string;
  ruleId: string;
  type: "DATA_GAP";
  status: "NEEDS_DATA";
  title: string;
  finding: string;
  medication: EvidenceMedication;
  requiredLab: { name: string; loinc: string };
  lookbackDays: number;
  source: { type: string; provider: string; evidenceIds: string[] };
  provenance: Provenance;
}

export interface AIExplanation {
  text: string;
  summary: string;
  findingExplanations: { ruleId: string; explanation: string }[];
  dataGapExplanation: string | null;
  groundedInFindingsOnly: boolean;
  mode: "mock" | "llm";
  model?: string | null;
  /** Stable public category when the model was not used (never provider detail). */
  fallbackCode?: string | null;
  fallbackReason?: string | null;
}

export interface FhirPersistResult {
  status: "PERSISTED";
  detectedIssueIds: string[];
  riskAssessmentId: string | null;
}

/** Small receipt recorded on the analysis once persist succeeds -- ids + timestamp only, never a copy of the
 * DetectedIssue/RiskAssessment resources themselves (HealthLake remains the source of truth for those). */
export interface FhirPersistenceReceipt {
  status: "PERSISTED";
  detectedIssueIds: string[];
  riskAssessmentId: string | null;
  persistedAt: string;
}

export interface Analysis {
  analysisId: string;
  patientId: string;
  status: "COMPLETED" | "FAILED";
  overallSeverity: OverallStatus;
  summary: { totalFindings: number; high: number; moderate: number; low: number; dataGaps: number };
  findings: Finding[];
  dataGaps: DataGap[];
  riskAssessment?: { id: string; level: OverallStatus; rationale: string; basis: string[] } | null;
  aiExplanation?: AIExplanation | null;
  fhirPersistence?: FhirPersistenceReceipt | null;
  asOfDate: string;
  generatedAt: string;
  rulesVersion: string;
}
