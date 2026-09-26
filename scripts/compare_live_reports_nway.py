#!/usr/bin/env python
"""Compare N live-check reports (e.g. Anthropic, OpenAI, Gemini, Databricks) side by side.

    python scripts/compare_live_reports_nway.py live_report.json live_report_openai.json live_report_gemini.json live_report_databricks.json

Generalizes scripts/compare_live_reports.py (left untouched, still the right tool for a straight two-report
diff, e.g. Opus vs Sonnet) from 2 to N columns. Same acceptance logic: each report is judged on its OWN
0-FAIL/0-INCONCLUSIVE status, never a numeric cross-report comparison -- that judgment is left to the human
reading the table. Reports hold no credentials and only the first 600 characters of each explanation.
"""
from __future__ import annotations

import json
import re
import statistics
import sys
from pathlib import Path


def load(path: str) -> dict:
    return json.loads(Path(path).read_text())


def label_for(rep: dict, path: str) -> str:
    return f"{rep.get('provider', rep.get('model', '?'))} ({path})"


def status_map(rep: dict) -> dict[tuple[str, str], tuple[str, str]]:
    return {(r["check"], r["patient"]): (r["status"], r["detail"]) for r in rep["results"]}


def labelled_calls(rep: dict) -> list[tuple[str, dict]]:
    """Completed calls in harness order: one per patient, then one per injection case, then the truncation probe."""
    done = [c for c in rep["calls"] if "error" not in c]
    normal = [r["patient"] for r in rep["results"] if r["check"] == "live explanation"]
    inject = [r["patient"] for r in rep["results"] if r["check"] == "injection"]
    labels = normal + [f"inject-{p}" for p in inject] + ["truncated"] * max(0, len(done) - len(normal) - len(inject))
    return list(zip(labels, done))


def cost(rep: dict) -> float:
    m = re.search(r"[\d.]+", rep["totals"]["estimated_cost"])
    return float(m.group()) if m else float("nan")


def main(paths: list[str]) -> int:
    reports = [load(p) for p in paths]
    labels = [label_for(r, p) for r, p in zip(reports, paths)]
    for label, rep in zip(labels, reports):
        print(f"{label}: counts {rep['counts']}")

    print("\nCheck-by-check status per report; '*' marks a row that is not unanimous")
    maps = [status_map(r) for r in reports]
    keys = sorted(set().union(*(m.keys() for m in maps)) - {k for m in maps for k in m if k[0] == "explanation text"})
    header = f"  {'check':<28}{'patient':<8}" + "".join(f"{i:>14}" for i in range(1, len(reports) + 1))
    print(header)
    for key in keys:
        vals = [m.get(key, ("-", ""))[0] for m in maps]
        marker = "*" if len(set(vals)) > 1 else " "
        print(f"{marker} {key[0]:<28}{key[1]:<8}" + "".join(f"{v:>14}" for v in vals))

    print("\nCalls (input/output tokens, seconds) per report")
    call_maps = [dict(labelled_calls(r)) for r in reports]
    all_labels = sorted(set().union(*(m.keys() for m in call_maps)))
    for case in all_labels:
        row = " | ".join(
            f"{c['input_tokens']}/{c['output_tokens']} {c['seconds']}s" if (c := m.get(case)) else "-"
            for m in call_maps)
        print(f"  {case:<14} {row}")
    for label, rep, m in zip(labels, reports, call_maps):
        vals = [c["seconds"] for l, c in m.items() if l != "truncated"]
        med = f"{statistics.median(vals):.1f}s" if vals else "n/a"
        t = rep["totals"]
        print(f"  {label}: median latency {med}, totals {t['input_tokens']} in / {t['output_tokens']} out, {t['estimated_cost']}")

    print("\nCoverage / grounding (from 'live explanation' rows), pooled acceptance per report")
    for label, rep in zip(labels, reports):
        rows = [r for r in rep["results"] if r["check"] == "live explanation"]
        n = len(rows) or 1
        print(f"  {label}: accepted by guard & mode=llm {sum(r['status'] == 'PASS' for r in rows)}/{len(rows)} "
             f"({sum(r['status'] == 'PASS' for r in rows) / n:.0%}); "
             f"all findings covered {sum('covered=True' in r['detail'] for r in rows)}/{len(rows)}; "
             f"independent flags none {sum('flags=none' in r['detail'] for r in rows)}/{len(rows)}")

    print("\nAcceptance verdict per report (own 0-FAIL/0-INCONCLUSIVE status only -- not a cross-report comparison)")
    overall_ok = True
    for label, m in zip(labels, maps):
        bad = [f"{k[0]} {k[1]}: {v[0]}" for k, v in m.items() if v[0] in ("FAIL", "INCONCLUSIVE")]
        overall_ok &= not bad
        print(f"  {label}: " + ("MEETS all harness criteria" if not bad else "DOES NOT MEET: " + "; ".join(bad)))
    return 0 if overall_ok else 1


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    raise SystemExit(main(sys.argv[1:]))
