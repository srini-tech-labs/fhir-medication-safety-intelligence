import { OP_WORDS, TYPE_LABEL } from "../format";
import type { Analysis, Finding, Severity } from "../types";
import { Chip, EvidenceLinks, Provenance, SeverityBadge } from "./common";
import { PersistAction } from "./PersistAction";

const ORDER: Severity[] = ["HIGH", "MODERATE", "LOW"];

export function FindingCard({ finding: f }: { finding: Finding }) {
  const t = f.provenance.trigger;
  return (
    <article className={`card sev-${f.severity}`} aria-label={`${f.severity} finding ${f.ruleId}`}>
      <div className="card-head">
        <SeverityBadge level={f.severity} />
        <h5>{f.title}</h5>
        <Chip code title="Deterministic rule ID">
          {f.ruleId}
        </Chip>
        <Chip>{TYPE_LABEL[f.type]}</Chip>
      </div>
      <div className="card-body">
        <p className="risk">{f.risk}</p>
        <dl className="kv">
          <dt>Medication</dt>
          <dd>
            {f.evidence.medications.map((m) => (
              <span key={m.rxCui}>
                <strong>{m.name}</strong> <Chip code>RxNorm {m.rxCui}</Chip>
              </span>
            ))}
          </dd>
          {f.evidence.labs.length > 0 && (
            <>
              <dt>Lab</dt>
              <dd>
                {f.evidence.labs.map((l) => (
                  <span key={l.loinc}>
                    <strong>{l.name}</strong> <Chip code>LOINC {l.loinc}</Chip> measured{" "}
                    <strong>
                      {l.value} {l.unit}
                    </strong>
                    {l.effectiveDate ? ` on ${l.effectiveDate}` : ""}
                  </span>
                ))}
              </dd>
            </>
          )}
          {t && (
            <>
              <dt>Threshold</dt>
              <dd>
                {f.evidence.labs[0]?.name} {OP_WORDS[t.operator] ?? t.operator} {t.threshold} {t.unit}
              </dd>
            </>
          )}
          <dt>Evidence</dt>
          <dd>
            <EvidenceLinks evidence={f.provenance.evidence} />
          </dd>
        </dl>
      </div>
      <Provenance
        ruleVersion={f.provenance.ruleVersion}
        fhirResources={f.provenance.fhirResources}
        evidence={f.provenance.evidence}
        trigger={t}
        severityDisclaimer={f.provenance.severityDisclaimer}
        ids={[
          ["Finding ID", f.findingId],
          ["Rule ID", f.ruleId],
          ["FHIR DetectedIssue", f.fhir?.detectedIssueId ?? "—"],
        ]}
      />
    </article>
  );
}

function OverallResult({ analysis: a }: { analysis: Analysis }) {
  const s = a.summary;
  const message: Record<string, string> = {
    HIGH: "Highest severity among configured rules that fired.",
    MODERATE: "Highest severity among configured rules that fired.",
    LOW: "Highest severity among configured rules that fired.",
    NONE: "No configured rule fired. This is not a statement that the regimen is clinically safe — only a limited prototype rule set was checked.",
    NEEDS_DATA: "No clinical finding was produced, but a configured check could not be completed because required data are missing.",
  };
  return (
    <div className={`overall sev-${a.overallSeverity}`} role="status" aria-live="polite">
      <span className="label">Overall result</span>
      <SeverityBadge level={a.overallSeverity} />
      <div className="counts">
        <Chip>{s.totalFindings} finding{s.totalFindings === 1 ? "" : "s"}</Chip>
        <Chip>{s.high} high</Chip>
        <Chip>{s.moderate} moderate</Chip>
        <Chip>{s.low} low</Chip>
        <Chip>{s.dataGaps} data gap{s.dataGaps === 1 ? "" : "s"}</Chip>
      </div>
      <p className="msg muted">{message[a.overallSeverity]}</p>
    </div>
  );
}

export function FindingsLayer({
  analysis,
  loading,
  error,
  onAnalyze,
}: {
  analysis: Analysis | null;
  loading: boolean;
  error: string | null;
  onAnalyze: () => void;
}) {
  return (
    <section className="layer findings" aria-labelledby="layer-findings">
      <header>
        <span className="step">Layer 2A</span>
        <h3 id="layer-findings">Deterministic safety findings</h3>
        <span className="hint">Rule engine output — decides risk, severity and data gaps</span>
      </header>
      <div className="body">
        <div style={{ display: "flex", alignItems: "center", gap: 14, flexWrap: "wrap" }}>
          <button className="btn-primary" onClick={onAnalyze} disabled={loading}>
            {loading && <span className="spinner" aria-hidden />}
            {loading ? "Analyzing…" : analysis ? "Re-run analysis" : "Analyze Medication Safety"}
          </button>
          {analysis && (
            <span className="analysis-meta">
              <span className="analysis-meta-main">
                Rules v{analysis.rulesVersion} · data as of {analysis.asOfDate}
              </span>
              <span className="mono analysis-meta-id">{analysis.analysisId}</span>
            </span>
          )}
        </div>
        {error && <p className="error">{error}</p>}
        {!analysis && !error && (
          <p className="placeholder">
            Run the analysis to evaluate the configured drug–drug, drug–lab and missing-data rules against the
            structured record above.
          </p>
        )}
        {analysis && (
          <>
            <OverallResult analysis={analysis} />
            <PersistAction key={analysis.analysisId} patientId={analysis.patientId} analysis={analysis} />
            {analysis.findings.length === 0 && (
              <p className="empty">No clinical findings were produced by the configured rules.</p>
            )}
            {ORDER.map((sev) => {
              const group = analysis.findings.filter((f) => f.severity === sev);
              if (!group.length) return null;
              return (
                <div className="sev-group" key={sev} role="group" aria-label={`${sev} findings`}>
                  <h4>
                    <SeverityBadge level={sev} /> <span className="muted">{group.length} finding{group.length === 1 ? "" : "s"}</span>
                  </h4>
                  <div className="cards">
                    {group.map((f) => (
                      <FindingCard key={f.findingId} finding={f} />
                    ))}
                  </div>
                </div>
              );
            })}
          </>
        )}
      </div>
    </section>
  );
}
