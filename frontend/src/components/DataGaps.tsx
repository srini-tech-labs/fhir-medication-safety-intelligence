import type { Analysis } from "../types";
import { Chip, EvidenceLinks, Provenance, SeverityBadge } from "./common";

/** Data gaps are an application status (NEEDS_DATA), never a clinical severity, so they get their own panel. */
export function DataGapsLayer({ analysis }: { analysis: Analysis | null }) {
  return (
    <section className="layer gaps" aria-labelledby="layer-gaps">
      <header>
        <span className="step">Layer 2B</span>
        <h3 id="layer-gaps">Data gaps</h3>
        <span className="hint">Configured checks that could not be completed — no value is ever estimated</span>
      </header>
      <div className="body">
        {!analysis && <p className="placeholder">Shown after the analysis has run.</p>}
        {analysis && analysis.dataGaps.length === 0 && <p className="empty">No data gaps were reported.</p>}
        {analysis?.dataGaps.map((g) => (
          <article className="card gap" key={g.gapId} aria-label={`Data gap ${g.ruleId}`}>
            <div className="card-head">
              <SeverityBadge level="NEEDS_DATA" />
              <h5>{g.title}</h5>
              <Chip code title="Deterministic rule ID">
                {g.ruleId}
              </Chip>
            </div>
            <div className="card-body">
              <p className="risk">{g.finding}</p>
              <dl className="kv">
                <dt>Medication</dt>
                <dd>
                  <strong>{g.medication.name}</strong> <Chip code>RxNorm {g.medication.rxCui}</Chip>
                </dd>
                <dt>Required lab</dt>
                <dd>
                  <strong>{g.requiredLab.name}</strong> <Chip code>LOINC {g.requiredLab.loinc}</Chip> within the last{" "}
                  {g.lookbackDays} days — <em>not available</em>
                </dd>
                <dt>Evidence</dt>
                <dd>
                  <EvidenceLinks evidence={g.provenance.evidence} />
                </dd>
              </dl>
            </div>
            <Provenance
              ruleVersion={g.provenance.ruleVersion}
              fhirResources={g.provenance.fhirResources}
              evidence={g.provenance.evidence}
              ids={[
                ["Gap ID", g.gapId],
                ["Rule ID", g.ruleId],
              ]}
            />
          </article>
        ))}
      </div>
    </section>
  );
}
