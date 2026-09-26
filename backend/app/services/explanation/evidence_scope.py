"""Rule-specific evidence context for the explanation model.

The evidence catalog is shared between rules: EVID-001 ("The warfarin label lists aspirin among antiplatelet agents and
ibuprofen among NSAIDs that increase bleeding risk when used with warfarin.") supports both DDI-001 (warfarin + aspirin)
and DDI-002 (warfarin + ibuprofen). A model explaining DDI-002 must not receive text about aspirin, or it will quote it and
the unsupplied-drug guard (correctly) rejects the explanation.

So the model gets, per finding, only the evidence text that concerns that finding's own medications:
  * text naming no other catalog drug is sent unchanged ("full");
  * a list item about another drug ("<drug> among <class>") is cut out, never reworded ("pruned");
  * if it cannot be pruned cleanly, the text is left out and only the citation (id/source/section) is sent ("withheld").
The full catalog stays intact for provenance and display; only what is *sent to the model* is scoped. Guarantee, enforced
here and property-tested: the scoped text never mentions a catalog drug outside the finding's medications.
"""
from __future__ import annotations

import re

from app.models.contract import EvidenceReference
from app.terminology import Terminology


def _mentions(text: str, name: str) -> bool:
    return re.search(rf"\b{re.escape(name)}\b", text, re.I) is not None


def scope_summary(summary: str, allowed: set[str], universe: set[str]) -> tuple[str | None, str]:
    """Return (text or None, "full" | "pruned" | "withheld") for one evidence summary."""
    allowed_l = {a.lower() for a in allowed}
    banned = sorted((d for d in universe if d.lower() not in allowed_l), key=len, reverse=True)
    if not any(_mentions(summary, d) for d in banned):
        return summary, "full"

    pruned = summary
    for drug in banned:  # cut "<drug> among <...>" up to and including a following " and ", or up to the sentence end
        pruned = re.sub(rf"\b{re.escape(drug)}\s+among\b.+?(?:\s+and\s+|(?=\s*[.;,]))", "", pruned, flags=re.I)
    pruned = re.sub(r"\s+and\s*(?=[.;,]|$)", "", pruned)  # dangling " and" left when the last item was removed
    pruned = re.sub(r"\s{2,}", " ", pruned).strip()
    pruned = re.sub(r"\s+([.;,])", r"\1", pruned)

    # Pruning is only meaningful if a whole list item about an allowed drug survives; otherwise we would send a stub such as
    # "The warfarin label lists." -- withhold the text instead and send the citation only.
    if any(_mentions(pruned, d) for d in banned) or len(pruned.split()) < 4 or not re.search(r"\bamong\b", pruned, re.I):
        return None, "withheld"
    return pruned, "pruned"


def scoped_evidence(evidence: list[EvidenceReference], medications: list[str], terms: Terminology) -> list[dict]:
    """Evidence entries for one finding/gap as sent to the model (summary scoped to `medications`)."""
    universe = set(terms.drugs.values())
    out = []
    for e in evidence:
        text, scope = scope_summary(e.summary, set(medications), universe)
        out.append({"evidenceId": e.evidence_id, "source": e.source, "section": e.section,
                    "summary": text, "summaryScope": scope})
    return out
