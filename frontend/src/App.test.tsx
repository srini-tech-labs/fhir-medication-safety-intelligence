import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import fx from "./test/fixtures.json";

type Handler = (init?: RequestInit) => Promise<Response> | Response;

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

function mockApi(overrides: Record<string, Handler> = {}) {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    for (const [pattern, handler] of Object.entries(overrides)) if (url.includes(pattern)) return handler(init);
    let m: RegExpMatchArray | null;
    if (url === "/v1/patients") return json(fx.patients);
    if ((m = url.match(/^\/v1\/patients\/(P\d+)\/snapshot$/))) return json((fx.snapshots as any)[m[1]]);
    if ((m = url.match(/^\/v1\/patients\/(P\d+)\/analyses$/))) return json((fx.analyses as any)[m[1]]); // aiExplanation: null
    if ((m = url.match(/^\/v1\/patients\/(P\d+)\/analyses\/AN-[^/]+\/explanation$/))) return json((fx.explanations as any)[m[1]]);
    if ((m = url.match(/^\/v1\/patients\/P\d+\/documents\/(.+)$/))) return json((fx.documents as any)[m[1]]);
    return json({ detail: "not found" }, 404);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

async function selectPatient(name: string) {
  await userEvent.click(await screen.findByRole("button", { name: new RegExp(name) }));
}
const analyzeButton = () => screen.findByRole("button", { name: /Analyze Medication Safety/ });

beforeEach(() => {
  localStorage.clear(); // the app remembers the last-selected patient per browser; tests must not leak it across each other
  mockApi(); // not returned: vitest would treat a returned function as a teardown hook
});
afterEach(() => vi.unstubAllGlobals());

describe("clinical data layer", () => {
  it("lists patients and auto-loads the first with medications and flagged labs", async () => {
    render(<App />);
    expect(await screen.findByRole("heading", { name: "Alex Demo" })).toBeInTheDocument();
    const meds = screen.getByRole("region", { name: "Active medications" });
    expect(within(meds).getByText("Lisinopril")).toBeInTheDocument();
    expect(within(meds).getByText("29046")).toBeInTheDocument();
    expect(within(meds).getByText("20 mg once daily")).toBeInTheDocument();
    const labs = screen.getByRole("region", { name: "Laboratory results" });
    expect(within(labs).getByText("2823-3")).toBeInTheDocument();
    expect(within(labs).getByText("5.8")).toBeInTheDocument();
    expect(within(labs).getByText(/High/, { selector: ".sr-only" })).toBeInTheDocument(); // H flag, accessible
  });

  it("opens a clinical note as context-only", async () => {
    render(<App />);
    await selectPatient("Lisa Demo");
    await userEvent.click(await screen.findByRole("button", { name: /Open Discharge Summary/ }));
    expect(await screen.findByText(/Potassium was elevated on follow-up testing/)).toBeInTheDocument();
    expect(screen.getByText("Context only")).toBeInTheDocument();
  });

  it("says so when a patient has no note", async () => {
    render(<App />);
    expect(await screen.findByText("No clinical note on file for this patient.")).toBeInTheDocument();
  });
});

describe("analysis", () => {
  it("shows three visibly separate layers plus a separate data-gaps panel", async () => {
    render(<App />);
    await screen.findByRole("heading", { name: "Alex Demo" });
    expect(screen.getByRole("heading", { name: "Clinical data" }).closest("section")).toHaveClass("layer", "data");
    expect(screen.getByRole("heading", { name: "Deterministic safety findings" }).closest("section")).toHaveClass("findings");
    expect(screen.getByRole("heading", { name: "Data gaps" }).closest("section")).toHaveClass("gaps");
    expect(screen.getByRole("heading", { name: "AI explanation" }).closest("section")).toHaveClass("ai");
  });

  it("P008: two findings, HIGH grouped before MODERATE, with full provenance fields", async () => {
    render(<App />);
    await selectPatient("Lisa Demo");
    await userEvent.click(await analyzeButton());

    // <details> also has an implicit "group" role, so select by accessible name
    const groups = await screen.findAllByRole("group", { name: /findings$/ });
    expect(groups.map((g) => g.getAttribute("aria-label"))).toEqual(["HIGH findings", "MODERATE findings"]);

    const high = screen.getByRole("article", { name: "HIGH finding DL-001" });
    expect(high).toHaveTextContent("Lisinopril with elevated potassium");
    expect(high).toHaveTextContent("RxNorm 29046");
    expect(high).toHaveTextContent("LOINC 2823-3");
    expect(high).toHaveTextContent("5.9 mmol/L");
    expect(high).toHaveTextContent("Potassium > 5.7 mmol/L");
    expect(high).toHaveTextContent("EVID-002");
    expect(high).toHaveTextContent("MedicationRequest/medreq-p008-01");
    expect(high).toHaveTextContent("Observation/obs-p008-01");
    expect(within(high).getByRole("link", { name: /EVID-002/ })).toHaveAttribute(
      "href", expect.stringContaining("dailymed.nlm.nih.gov"));

    const moderate = screen.getByRole("article", { name: "MODERATE finding DDI-003" });
    expect(moderate).toHaveTextContent("Ibuprofen");
    expect(moderate).toHaveTextContent("EVID-003");

    expect(screen.getByText("2 findings")).toBeInTheDocument();
  });

  it("renders the AI explanation in its own panel, labelled as mock, with rule-engine severities", async () => {
    render(<App />);
    await selectPatient("Lisa Demo");
    await userEvent.click(await analyzeButton());
    const ai = (await screen.findByRole("heading", { name: "AI explanation" })).closest("section")!;
    expect(await within(ai).findByText(/Mock explanation/)).toBeInTheDocument(); // arrives via the separate request
    expect(within(ai).getByText(/cannot create findings, change severity/)).toBeInTheDocument();
    expect(within(ai).getAllByText("severity is set by the rule engine")).toHaveLength(2);
    expect(within(ai).getByText("DL-001")).toBeInTheDocument();
    // AI text lives only in the AI panel, not inside the findings layer
    const findings = screen.getByRole("heading", { name: "Deterministic safety findings" }).closest("section")!;
    expect(within(findings).queryByText(/Observed data:/)).toBeNull();
  });

  it("P009 negative control: no findings, says no rule fired, never claims safety", async () => {
    render(<App />);
    await selectPatient("Jordan Demo");
    await userEvent.click(await analyzeButton());
    expect(await screen.findByText(/No clinical findings were produced/)).toBeInTheDocument();
    await screen.findByText(/Mock explanation/);
    expect(screen.queryAllByRole("article")).toHaveLength(0);
    const text = document.body.textContent ?? "";
    expect(text).toMatch(/No configured rule fired/);
    expect(text).toMatch(/not a statement that the regimen is clinically safe/);
    expect(text).not.toMatch(/safe medication regimen|clinically validated|approved treatment/i);
  });

  it("P010: data gap in its own panel, NEEDS DATA status, no fabricated potassium", async () => {
    render(<App />);
    await selectPatient("Nina Demo");
    expect(await screen.findByText("No laboratory results in the structured record.")).toBeInTheDocument();
    await userEvent.click(await analyzeButton());

    const gap = await screen.findByRole("article", { name: "Data gap DG-001" });
    await screen.findByText(/Mock explanation/);
    expect(gap).toHaveTextContent("NEEDS DATA");
    expect(gap).toHaveTextContent("LOINC 2823-3");
    expect(gap).toHaveTextContent("not available");
    expect(screen.queryByRole("group", { name: /findings$/ })).toBeNull(); // no severity groups
    expect(screen.getByText("0 findings")).toBeInTheDocument();
    expect(screen.getAllByText("NEEDS DATA").length).toBeGreaterThan(0);
    expect(document.body.textContent).not.toMatch(/\d\.\d\s*mmol\/L/);
  });

  it("ignores an analysis response that arrives after the user switched patients", async () => {
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    mockApi({
      "/v1/patients/P001/analyses": async () => {
        await gate;
        return json(fx.analyses.P001);
      },
    });
    render(<App />);
    await userEvent.click(await analyzeButton()); // P001 in flight
    await selectPatient("Lisa Demo");
    release();
    await screen.findByRole("heading", { name: "Lisa Demo" });
    await waitFor(() => expect(screen.queryByText("AN-P001-001")).toBeNull());
    expect(screen.queryAllByRole("article")).toHaveLength(0);
  });

  it("shows an error and keeps the page usable when the analysis call fails", async () => {
    mockApi({ "/v1/patients/P001/analyses": () => json({ detail: "boom" }, 500) });
    render(<App />);
    await userEvent.click(await analyzeButton());
    expect(await screen.findByText("boom")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Analyze Medication Safety/ })).toBeEnabled();
    expect(screen.queryByText(/Generating the explanation/)).toBeNull(); // no explanation is requested for a failed analysis
  });
});

describe("explicit save to the clinical FHIR store (never automatic)", () => {
  const saveButton = () => screen.findByRole("button", { name: /Save analysis results to HealthLake/ });

  it("is hidden until a deterministic analysis has run", async () => {
    render(<App />);
    await screen.findByRole("heading", { name: "Alex Demo" });
    expect(screen.queryByRole("button", { name: /Save analysis results to HealthLake/ })).toBeNull();
  });

  it("persists on click, never automatically, shows ids in clearly separated lines, then disables the button", async () => {
    // A custom mock, not mockApi(overrides): a pattern override matching "/v1/patients/P001/analyses" would also
    // match its own /latest and /explanation sub-paths (both contain that prefix), so the analyze POST needs an
    // exact-path check to return a fresh id per call without swallowing those other endpoints.
    let persistCalls = 0;
    let analyzeCalls = 0;
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      let m: RegExpMatchArray | null;
      if (url === "/v1/patients") return json(fx.patients);
      if ((m = url.match(/^\/v1\/patients\/(P\d+)\/snapshot$/))) return json((fx.snapshots as any)[m[1]]);
      if (url === "/v1/patients/P001/analyses" && init?.method === "POST") {
        analyzeCalls++;
        return json({ ...(fx.analyses as any).P001, analysisId: `AN-P001-00${analyzeCalls}` }); // a real re-run gets a new id
      }
      if ((m = url.match(/^\/v1\/patients\/(P\d+)\/analyses\/AN-[^/]+\/explanation$/))) return json((fx.explanations as any)[m[1]]);
      if (url.includes("/persist")) {
        persistCalls++;
        return json({ status: "PERSISTED", detectedIssueIds: ["di-p001-dl001"], riskAssessmentId: "ra-p001-001" });
      }
      return json({ detail: "not found" }, 404); // includes .../analyses/latest: nothing restored yet
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<App />);
    await userEvent.click(await analyzeButton());
    await screen.findByRole("heading", { name: "AI explanation" }); // analysis fully rendered
    expect(persistCalls).toBe(0); // never triggered by Analyze itself

    await userEvent.click(await saveButton());
    expect(await screen.findByText("Saved to the synthetic demo FHIR datastore")).toBeInTheDocument();
    expect(await screen.findByText(/DetectedIssue IDs:/)).toHaveTextContent("di-p001-dl001");
    expect(await screen.findByText(/RiskAssessment ID:/)).toHaveTextContent("ra-p001-001");
    expect(persistCalls).toBe(1);

    // Saving again is not offered: the button becomes a disabled, non-actionable "Saved to HealthLake".
    const savedButton = await screen.findByRole("button", { name: "Saved to HealthLake" });
    expect(savedButton).toBeDisabled();
    expect(screen.queryByRole("button", { name: /Save analysis results to HealthLake/ })).toBeNull();
    await userEvent.click(savedButton);
    expect(persistCalls).toBe(1); // no second write

    // Re-running gets an independent analysis with a fresh, enabled save action.
    await userEvent.click(await screen.findByRole("button", { name: /Re-run analysis/ }));
    await screen.findByRole("heading", { name: "AI explanation" });
    expect(await saveButton()).toBeEnabled();
  });

  it("shows a clear message, not an error, when write-back is disabled (409)", async () => {
    mockApi({ "/persist": () => json({ detail: "disabled" }, 409) });
    render(<App />);
    await userEvent.click(await analyzeButton());
    await userEvent.click(await saveButton());
    expect(await screen.findByText(/FHIR write-back is disabled/)).toBeInTheDocument();
  });

  it("shows a generic error without leaking store detail when persistence fails", async () => {
    mockApi({ "/persist": () => json({ detail: "HealthLakeAuthError 403 AccessDenied" }, 503) });
    render(<App />);
    await userEvent.click(await analyzeButton());
    await userEvent.click(await saveButton());
    expect(await screen.findByText(/Could not save to the clinical data store/)).toBeInTheDocument();
    expect(screen.queryByText(/AccessDenied/)).toBeNull();
  });
});

describe("navigating back to a patient restores its saved analysis (durable state, not React memory)", () => {
  const storedP001Analysis = {
    ...(fx.analyses as any).P001,
    aiExplanation: (fx.explanations as any).P001,
    fhirPersistence: {
      status: "PERSISTED",
      detectedIssueIds: ["di-p001-dl001"],
      riskAssessmentId: "ra-p001-001",
      persistedAt: "2026-09-23T12:00:00+00:00",
    },
  };

  it("reloads the stored analysis, explanation and persistence receipt on initial load and after switching away and back -- never re-running rules, calling the AI provider or re-persisting", async () => {
    let analyzeCalls = 0;
    let explainCalls = 0;
    let persistCalls = 0;
    mockApi({
      "/v1/patients/P001/analyses/latest": () => json(storedP001Analysis),
      "/v1/patients/P001/analyses": (init) => {
        if (init?.method === "POST") analyzeCalls++;
        return json({ detail: "unexpected analyze call" }, 500);
      },
      "/explanation": () => {
        explainCalls++;
        return json({ detail: "unexpected explain call" }, 500);
      },
      "/persist": () => {
        persistCalls++;
        return json({ detail: "unexpected persist call" }, 500);
      },
    });
    render(<App />);

    // Initial load: P001 is auto-selected, and its previously-saved analysis (with AI explanation and FHIR
    // persistence receipt) appears immediately -- this is also what a browser refresh would do, since it is
    // exactly the same code path as the initial mount.
    expect(await screen.findByRole("heading", { name: "AI explanation" })).toBeInTheDocument();
    expect(screen.getByText("AN-P001-001")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Saved to HealthLake" })).toBeDisabled();
    expect(screen.getByText(/DetectedIssue IDs:/)).toHaveTextContent("di-p001-dl001");
    expect(screen.getByRole("button", { name: /Re-run analysis/ })).toBeInTheDocument(); // not "Analyze" -- a result already exists

    // Switch away to a patient with no saved analysis: resets to the not-yet-analyzed placeholder.
    await selectPatient("Emma Demo");
    await screen.findByRole("heading", { name: "Emma Demo" });
    expect(screen.getByRole("button", { name: /Analyze Medication Safety/ })).toBeInTheDocument();
    expect(screen.getByText("Shown after the deterministic analysis completes.")).toBeInTheDocument();

    // Switch back: the same saved analysis, explanation and receipt reappear.
    await selectPatient("Alex Demo");
    expect(await screen.findByRole("heading", { name: "AI explanation" })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Saved to HealthLake" })).toBeDisabled();
    expect(screen.getByText(/DetectedIssue IDs:/)).toHaveTextContent("di-p001-dl001");

    expect(analyzeCalls).toBe(0);
    expect(explainCalls).toBe(0);
    expect(persistCalls).toBe(0);
  });

  it("survives a real page refresh: the last-viewed patient and its saved analysis both come back (localStorage + backend, not React memory)", async () => {
    const storedP008Analysis = {
      ...(fx.analyses as any).P008,
      aiExplanation: (fx.explanations as any).P008,
      fhirPersistence: {
        status: "PERSISTED",
        detectedIssueIds: ["di-p008-dl001", "di-p008-ddi003"],
        riskAssessmentId: "ra-p008-001",
        persistedAt: "2026-09-23T12:00:00+00:00",
      },
    };
    mockApi({ "/v1/patients/P008/analyses/latest": () => json(storedP008Analysis) });

    const { unmount } = render(<App />);
    await selectPatient("Lisa Demo"); // P008
    expect(await screen.findByRole("button", { name: "Saved to HealthLake" })).toBeDisabled();
    unmount(); // a real reload discards all React state; only localStorage and the backend survive

    render(<App />);
    expect(await screen.findByRole("heading", { name: "Lisa Demo" })).toBeInTheDocument(); // still on P008, not back to the default P001
    expect(await screen.findByRole("button", { name: "Saved to HealthLake" })).toBeDisabled();
    expect(screen.getByText(/DetectedIssue IDs:/)).toHaveTextContent("di-p008-dl001");
    expect(screen.getByText(/RiskAssessment ID:/)).toHaveTextContent("ra-p008-001");
  });
});

describe("explanation is requested separately from the deterministic analysis", () => {
  const explainCalls = (fetchMock: ReturnType<typeof vi.fn>) =>
    fetchMock.mock.calls.filter(([u]) => String(u).endsWith("/explanation")).length;

  it("shows deterministic results immediately while the explanation is still pending", async () => {
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    const fetchMock = mockApi({
      "/explanation": async () => {
        await gate;
        return json(fx.explanations.P001);
      },
    });
    render(<App />);
    await userEvent.click(await analyzeButton());

    // findings and overall result are on screen while the model request is still in flight
    expect(await screen.findByRole("article", { name: "HIGH finding DL-001" })).toBeInTheDocument();
    expect(screen.getAllByText("1 finding").length).toBeGreaterThan(0); // summary chip and group header
    expect(await screen.findByText(/Generating the explanation/)).toBeInTheDocument();
    expect(screen.queryByText("Summary")).toBeNull();
    expect(explainCalls(fetchMock)).toBe(1);

    release();
    expect(await screen.findByText("Summary")).toBeInTheDocument();
    expect(screen.queryByText(/Generating the explanation/)).toBeNull();
    expect(screen.getByRole("article", { name: "HIGH finding DL-001" })).toBeInTheDocument(); // unchanged
  });

  it("keeps the findings and offers Retry when the explanation is unavailable", async () => {
    let attempts = 0;
    mockApi({
      "/explanation": () => {
        attempts += 1;
        return attempts === 1
          ? json({ detail: "The AI explanation service is unavailable." }, 503)
          : json(fx.explanations.P001);
      },
    });
    render(<App />);
    await userEvent.click(await analyzeButton());

    expect(await screen.findByRole("alert")).toHaveTextContent("The AI explanation could not be retrieved");
    expect(screen.getByRole("article", { name: "HIGH finding DL-001" })).toBeInTheDocument(); // deterministic result intact
    expect(screen.getByRole("button", { name: /Analyze Medication Safety|Re-run analysis/ })).toBeEnabled();

    await userEvent.click(screen.getByRole("button", { name: "Retry explanation" }));
    expect(await screen.findByText("Summary")).toBeInTheDocument();
    expect(attempts).toBe(2);
    expect(screen.getByRole("article", { name: "HIGH finding DL-001" })).toBeInTheDocument();
  });

  it("does not render server or provider text when the explanation request fails", async () => {
    mockApi({ "/explanation": () => json({ detail: "AuthenticationError: Error code: 401 claude-opus-5 sk-ant-xxxx" }, 500) });
    render(<App />);
    await userEvent.click(await analyzeButton());
    await screen.findByRole("alert");
    expect(document.body.textContent).not.toMatch(/AuthenticationError|Error code|401|claude-|sk-ant/);
  });

  it("shows only the public fallback message when the model was not used", async () => {
    mockApi({
      "/explanation": () =>
        json({
          ...fx.explanations.P001,
          fallbackCode: "EXPLANATION_TEMPORARILY_UNAVAILABLE",
          fallbackReason: "The AI explanation service is temporarily unavailable. Please try again.",
        }),
    });
    render(<App />);
    await userEvent.click(await analyzeButton());
    expect(await screen.findByText(/temporarily unavailable\. Please try again\. A deterministic summary is shown instead/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry explanation" })).toBeInTheDocument();
    expect(screen.getByText("Summary")).toBeInTheDocument(); // the deterministic-text explanation is still shown
    expect(document.body.textContent).not.toMatch(/AuthenticationError|Error code|claude-|sk-ant/);
  });

  it("ignores a late explanation after the user switched patients", async () => {
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    mockApi({
      "/explanation": async () => {
        await gate;
        return json({ ...fx.explanations.P001, summary: "STALE P001 EXPLANATION" });
      },
    });
    render(<App />);
    await userEvent.click(await analyzeButton());
    await screen.findByText(/Generating the explanation/);
    await selectPatient("Lisa Demo");
    release();
    await screen.findByRole("heading", { name: "Lisa Demo" });
    await waitFor(() => expect(screen.queryByText(/Generating the explanation/)).toBeNull());
    expect(screen.queryByText("STALE P001 EXPLANATION")).toBeNull();
  });

  it("re-running the analysis discards the previous explanation and requests a new one", async () => {
    const fetchMock = mockApi();
    render(<App />);
    await userEvent.click(await analyzeButton());
    await screen.findByText("Summary");
    await userEvent.click(await screen.findByRole("button", { name: "Re-run analysis" }));
    await screen.findByText("Summary");
    expect(explainCalls(fetchMock)).toBe(2);
  });
});

describe("api unavailable", () => {
  it("explains how to start the backend", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new TypeError("network"))));
    render(<App />);
    expect(await screen.findByText(/Cannot reach the API/)).toBeInTheDocument();
    expect(screen.getByText(/API unreachable/)).toBeInTheDocument();
  });
});
