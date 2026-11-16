"""Demo replay: serve a saved investigation instantly instead of re-running slow providers.

Enabled only with HELIX_DEMO_REPLAY=1. When an intake matches a saved, complete investigation
(same city, budget and funding), Stage 1 returns that investigation's saved discovery response and
Stage 2 returns its saved dossier, so the rest of the journey (Stage 3 screening, the saved
opportunity report, business choice, Finance Lab, memorandum) runs on real saved data.
Every replayed response is labelled; nothing is presented as a fresh live search.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import opportunity_report

BASE = Path(__file__).resolve().parent


def enabled():
    return os.environ.get('HELIX_DEMO_REPLAY') == '1'


def _key(intake):
    try: budget = round(float(intake.get('total_budget_usd')))
    except (TypeError, ValueError): budget = None
    return (str(intake.get('city') or ''), budget, str(intake.get('financing') or ''))


def find(intake, handoffs, stage3_results):
    """Most recent complete saved investigation whose intake matches."""
    want = _key(intake)
    best = None
    for iid, handoff in handoffs.items():
        if not handoff.get('dossier') or iid not in stage3_results: continue
        if not (BASE / '.opportunity-reports' / iid / 'current.json').exists(): continue
        stage1 = opportunity_report.read_stage1(iid)
        if not stage1 or _key(stage1.get('intake') or {}) != want: continue
        if best is None or handoff.get('created_at', '') > best[1].get('created_at', ''):
            best = (iid, handoff, stage1)
    return best


def discovery_response(intake, handoffs, stage3_results):
    """Saved Stage 1 response for a matching investigation, labelled as a replay, or None."""
    if not enabled(): return None
    match = find(intake, handoffs, stage3_results)
    if not match: return None
    iid, handoff, stage1 = match
    response = json.loads(json.dumps(stage1['report'], ensure_ascii=False))
    response['replay'] = {'investigation_id': iid, 'saved_at': handoff.get('created_at'),
                          'selected_stable_id': handoff.get('selected_stable_id'),
                          'address': (handoff.get('selected_listing') or {}).get('address'),
                          'note': 'Demo replay of a saved CoStar discovery and building investigation.'}
    return {'investigation_id': iid, 'intake': stage1.get('intake') or intake, 'response': response}


def saved_stage3(handoff_id, reserve, stage3_results):
    """Saved Stage 3 result when the requested reserve matches it, so replays never drift."""
    if not enabled(): return None
    saved = stage3_results.get(handoff_id)
    if not saved: return None
    have = (saved.get('inputs') or {}).get('reserve')
    try: want = None if reserve in (None, '') else float(reserve)
    except (TypeError, ValueError): return None
    same = (want is None and have in (None, 'unavailable')) or (want is not None and have not in (None, 'unavailable') and float(have) == want)
    return saved if same else None
