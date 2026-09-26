#!/usr/bin/env python
"""Compare two live_claude_check reports (e.g. Opus baseline vs Sonnet) on the same cases.

    python scripts/compare_live_reports.py live_report_opus.json live_report_sonnet.json

Compares: check-by-check status, per-call tokens and latency, totals and cost, coverage/grounding flags, and
the stored explanation excerpts. Prints an objective acceptance verdict for the candidate; it does not decide
the model. Reports hold no credentials and only the first 600 characters of each explanation.
"""
from __future__ import annotations

import json
import re
import statistics
import sys
from pathlib import Path


def load(path: str) -> dict:
    return json.loads(Path(path).read_text())


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


def main(a_path: str, b_path: str) -> int:
    a, b = load(a_path), load(b_path)
    print(f"A = {a['model']}  ({a_path})\nB = {b['model']}  ({b_path})")
    print(f"A counts {a['counts']}\nB counts {b['counts']}\n")

    sa, sb = status_map(a), status_map(b)
    print("Check-by-check (status A -> B); '*' marks a difference")
    for key in sorted(set(sa) | set(sb)):
        x, y = sa.get(key, ("-", ""))[0], sb.get(key, ("-", ""))[0]
        if key[0] == "explanation text":
            continue
        print(f"  {'*' if x != y else ' '} {key[0]:<28} {key[1]:<6} {x:>12} -> {y}")

    print("\nCalls (input / output tokens, seconds)")
    ca, cb = dict(labelled_calls(a)), dict(labelled_calls(b))
    print(f"  {'case':<14}{'A in/out':>16}{'B in/out':>16}{'A s':>7}{'B s':>7}{'out B/A':>9}")
    for label in ca:
        x, y = ca[label], cb.get(label)
        if not y:
            continue
        print(f"  {label:<14}{x['input_tokens']:>8}/{x['output_tokens']:<7}{y['input_tokens']:>8}/{y['output_tokens']:<7}"
              f"{x['seconds']:>7}{y['seconds']:>7}{y['output_tokens'] / max(1, x['output_tokens']):>9.2f}")
    lat = lambda d: statistics.median(c["seconds"] for l, c in d.items() if l != "truncated")
    print(f"\n  median latency (excluding truncation probe): A {lat(ca):.1f}s  B {lat(cb):.1f}s")
    ta, tb = a["totals"], b["totals"]
    print(f"  totals A: {ta['input_tokens']} in / {ta['output_tokens']} out, {ta['estimated_cost']}")
    print(f"  totals B: {tb['input_tokens']} in / {tb['output_tokens']} out, {tb['estimated_cost']}   (cost B/A = {cost(b) / cost(a):.2f})")

    print("\nCoverage / grounding (from 'live explanation' rows)")
    for name, rep in (("A", a), ("B", b)):
        rows = [r for r in rep["results"] if r["check"] == "live explanation"]
        print(f"  {name}: accepted by guard & mode=llm {sum(r['status'] == 'PASS' for r in rows)}/{len(rows)}; "
              f"all findings covered {sum('covered=True' in r['detail'] for r in rows)}/{len(rows)}; "
              f"independent flags none {sum('flags=none' in r['detail'] for r in rows)}/{len(rows)}")

    print("\nExplanation excerpts (first 600 chars stored by the harness), A then B")
    for (chk, pid), (_, detail) in sa.items():
        if chk == "explanation text" and (chk, pid) in sb:
            print(f"\n  [{pid}] A: {detail[:420]}\n  [{pid}] B: {sb[(chk, pid)][1][:420]}")

    bad = [f"{k[0]} {k[1]}: {v[0]}" for k, v in sb.items() if v[0] in ("FAIL", "INCONCLUSIVE")]
    print("\nACCEPTANCE (candidate B): " + ("MEETS all harness criteria (0 FAIL / 0 INCONCLUSIVE)" if not bad else "DOES NOT MEET: " + "; ".join(bad)))
    return 0 if not bad else 1


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    raise SystemExit(main(*sys.argv[1:]))
