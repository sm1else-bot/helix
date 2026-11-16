"""Stage 2 adapter for Jayant's building-only compliance preflight (PR #17).

This intentionally exposes only `preflight`; `check_business` is not imported or called.
The rule/output contract follows playground/compliance_engine.py at cb68f3d.
"""
from __future__ import annotations

import sys
from pathlib import Path

STAGE2_DIR = Path(__file__).resolve().parent
if str(STAGE2_DIR) not in sys.path:
    sys.path.insert(0, str(STAGE2_DIR))


def run_property_preflight(handoff, costar_facts, identity_conflict=False):
    try:
        from ._jayant_compliance import preflight
        context = {"handoff_id": handoff.get("handoff_id"),
                   "selected_listing": handoff.get("selected_listing"),
                   "property_facts": costar_facts or {},
                   "identity_conflict": bool(identity_conflict),
                   "jurisdiction": {"code": None, "verified": False, "evidence_url": None,
                                    "note": "Awaiting coordinate-based jurisdiction resolution."}}
        # The Stage 1 handoff carries coordinates in property_facts. The existing
        # adapter/resolver uses Census boundaries, never postal-city inference.
        return {"status": "available", "report": preflight(context),
                "implementation": "Jayant playground.compliance_engine.preflight",
                "business_specific_checks_invoked": False}
    except Exception as error:
        return {"status": "temporarily_unavailable", "report": None,
                "implementation": "Jayant playground.compliance_engine.preflight",
                "business_specific_checks_invoked": False,
                "error": type(error).__name__,
                "note": "Property-level preflight adapter unavailable; regulatory findings remain unknown."}
