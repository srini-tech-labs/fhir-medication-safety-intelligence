import { statusLabel } from "../format";
import type { EvidenceReference, OverallStatus } from "../types";

export function SeverityBadge({ level }: { level: OverallStatus }) {
  return <span className={`sev-badge sev-${level}`}>{statusLabel(level)}</span>;
}

export function Chip({ children, code, title }: { children: React.ReactNode; code?: boolean; title?: string }) {
  return (
    <span className={`chip${code ? " code" : ""}`} title={title}>
      {children}
    </span>
  );
}

/** Evidence chips: "DailyMed · EVID-002", each linking to the saved source. */
export function EvidenceLinks({ evidence }: { evidence: EvidenceReference[] }) {
  return (
    <>
      {evidence.map((e) => (
        <a
          key={e.evidenceId}
          className="chip link"
          href={e.sourceUrl}
          target="_blank"
          rel="noopener noreferrer"
          title={`${e.title} — ${e.section}`}
        >
          {e.provider} · <span className="mono">{e.evidenceId}</span> ↗
        </a>
      ))}
    </>
  );
}

export function Provenance({
  ruleVersion,
  fhirResources,
  evidence,
  trigger,
  severityDisclaimer,
  ids,
}: {
  ruleVersion: string;
  fhirResources: string[];
  evidence: EvidenceReference[];
  trigger?: { operator: string; threshold: number; unit: string; note?: string | null } | null;
  severityDisclaimer?: string | null;
  ids: [string, string][];
}) {
  return (
    <details className="prov">
      <summary>Provenance &amp; evidence</summary>
      <div className="prov-body">
        <dl className="kv">
          {ids.map(([k, v]) => (
            <div key={k} style={{ display: "contents" }}>
              <dt>{k}</dt>
              <dd className="mono">{v}</dd>
            </div>
          ))}
          <dt>Rule version</dt>
          <dd className="mono">{ruleVersion}</dd>
          {trigger?.note && (
            <>
              <dt>Threshold note</dt>
              <dd>{trigger.note}</dd>
            </>
          )}
          <dt>Source FHIR resources</dt>
          <dd>
            {fhirResources.map((r) => (
              <Chip key={r} code>
                {r}
              </Chip>
            ))}
          </dd>
        </dl>
        {evidence.map((e) => (
          <div className="evid" key={e.evidenceId}>
            <div className="evid-title">
              <span className="mono">{e.evidenceId}</span> — {e.title}
            </div>
            <div className="muted">
              {e.source} · {e.section} · accessed {e.accessed}
            </div>
            <p>{e.summary}</p>
            <a href={e.sourceUrl} target="_blank" rel="noopener noreferrer">
              Open saved source ↗
            </a>
          </div>
        ))}
        {severityDisclaimer && <p className="muted">{severityDisclaimer}</p>}
      </div>
    </details>
  );
}
