"""Severity ordering and overall-result logic (handoff section 9)."""
from __future__ import annotations

from app.models.contract import OverallStatus, Severity

# HIGH > MODERATE > LOW > NONE. NEEDS_DATA is an application status, not a clinical severity.
SEVERITY_RANK: dict[str, int] = {"HIGH": 3, "MODERATE": 2, "LOW": 1, "NONE": 0}


def overall_status(finding_severities: list[Severity], data_gap_count: int) -> OverallStatus:
    if finding_severities:
        return max(finding_severities, key=SEVERITY_RANK.__getitem__)
    return "NEEDS_DATA" if data_gap_count else "NONE"
