import { useEffect, useState } from "react";
import { api } from "../api";
import { FLAG_LABEL, formatRange } from "../format";
import type { DocumentContent, Snapshot } from "../types";
import { Chip } from "./common";

function Medications({ meds }: { meds: Snapshot["medications"] }) {
  return (
    <section aria-label="Active medications">
      <h4 className="subhead">Active medications</h4>
      {meds.length === 0 ? (
        <p className="empty">No active medications in the structured record.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Medication</th>
                <th>RxNorm (RxCUI)</th>
                <th>Dosage</th>
              </tr>
            </thead>
            <tbody>
              {meds.map((m) => (
                <tr key={`${m.rxCui}-${m.name}`}>
                  <td>
                    <strong>{m.name}</strong>
                  </td>
                  <td>{m.rxCui ? <Chip code>{m.rxCui}</Chip> : "—"}</td>
                  <td>{m.dosage ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function Labs({ labs }: { labs: Snapshot["labs"] }) {
  return (
    <section aria-label="Laboratory results">
      <h4 className="subhead">Laboratory results</h4>
      {labs.length === 0 ? (
        <p className="empty">No laboratory results in the structured record.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Lab</th>
                <th>LOINC</th>
                <th>Result</th>
                <th>Flag</th>
                <th>Reference</th>
                <th>Date</th>
              </tr>
            </thead>
            <tbody>
              {labs.map((l) => {
                const flag = l.interpretation ?? "";
                return (
                  <tr key={`${l.loinc}-${l.effectiveDate}`}>
                    <td>
                      <strong>{l.name}</strong>
                    </td>
                    <td>{l.loinc ? <Chip code>{l.loinc}</Chip> : "—"}</td>
                    <td className={`abn-${flag}`}>
                      <span className="val">{l.value}</span> <span className="muted">{l.unit}</span>
                    </td>
                    <td>
                      {flag ? (
                        <span className={`flag ${flag}`} title={FLAG_LABEL[flag] ?? flag}>
                          {flag}
                          <span className="sr-only"> ({FLAG_LABEL[flag] ?? flag})</span>
                        </span>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td className="muted">{formatRange(l.referenceRange)}</td>
                    <td>{l.effectiveDate}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

type DocState = { status: "loading" } | { status: "ready"; doc: DocumentContent } | { status: "error"; message: string };

function Notes({ patientId, documents }: { patientId: string; documents: Snapshot["documents"] }) {
  const [openId, setOpenId] = useState<string | null>(null);
  const [state, setState] = useState<DocState | null>(null);

  useEffect(() => {
    if (!openId) return;
    let cancelled = false;
    setState({ status: "loading" });
    api
      .document(patientId, openId)
      .then((doc) => !cancelled && setState({ status: "ready", doc }))
      .catch((e: Error) => !cancelled && setState({ status: "error", message: e.message }));
    return () => {
      cancelled = true;
    };
  }, [patientId, openId]);

  return (
    <section aria-label="Clinical notes">
      <h4 className="subhead">Unstructured clinical notes</h4>
      {documents.length === 0 ? (
        <p className="empty">No clinical note on file for this patient.</p>
      ) : (
        <>
          <div className="note-tabs">
            {documents.map((d) => (
              <button
                key={d.id}
                className="note-tab"
                aria-expanded={openId === d.id}
                onClick={() => setOpenId(openId === d.id ? null : d.id)}
              >
                {openId === d.id ? "Hide" : "Open"} {d.type}
                {d.date ? ` · ${d.date}` : ""}
              </button>
            ))}
          </div>
          {openId && state && (
            <div className="note-view">
              <div className="note-meta">
                <Chip>Context only</Chip>
                <span className="muted">
                  Notes never create findings, change severity or supply missing lab values.
                </span>
              </div>
              {state.status === "loading" && <p className="muted">Loading note…</p>}
              {state.status === "error" && <p className="error">{state.message}</p>}
              {state.status === "ready" && <blockquote>{state.doc.text}</blockquote>}
            </div>
          )}
        </>
      )}
    </section>
  );
}

export function ClinicalData({ snapshot }: { snapshot: Snapshot }) {
  return (
    <section className="layer data" aria-labelledby="layer-data">
      <header>
        <span className="step">Layer 1</span>
        <h3 id="layer-data">Clinical data</h3>
        <span className="hint">FHIR R4 data retrieved from AWS HealthLake through the application API</span>
      </header>
      <div className="body">
        <Medications meds={snapshot.medications} />
        <Labs labs={snapshot.labs} />
        <Notes patientId={snapshot.patient.id} documents={snapshot.documents} />
      </div>
    </section>
  );
}
