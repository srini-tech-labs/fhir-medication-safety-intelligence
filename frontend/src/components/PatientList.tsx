import type { PatientSummary } from "../types";

export function PatientList({
  patients,
  selectedId,
  onSelect,
}: {
  patients: PatientSummary[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <nav className="sidebar" aria-label="Patients">
      <h2>Synthetic patients</h2>
      <ul className="patient-list">
        {patients.map((p) => (
          <li key={p.id}>
            <button className="patient-btn" aria-current={p.id === selectedId} onClick={() => onSelect(p.id)}>
              <span className="row">
                <span className="name">
                  {p.name}
                  {p.id === selectedId && <span className="selected-tag">Selected</span>}
                </span>
                <span className="pid">{p.id}</span>
              </span>
              <span className="row muted" style={{ fontSize: 12.5 }}>
                {p.age} y · {p.sex}
              </span>
              {p.scenarioLabel && <div className="scenario">{p.scenarioLabel}</div>}
            </button>
          </li>
        ))}
      </ul>
    </nav>
  );
}
