"""Explicit business selection after the opportunity report, and the Stage 4 Finance Lab handoff.

The selection is accepted only when the saved Stage 1/2/3 evidence still validates, the current
opportunity report was built from this exact Stage 3 result, and the business is one of the
deterministic shortlist candidates. Stage 3 output is copied, never recomputed or reranked.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import opportunity_report

BASE = Path(__file__).resolve().parent
SELECTIONS_PATH = BASE / '.stage4-selections.json'
VERSION = 'helix.stage4-handoff.v1'
MARKETS = {'los_angeles': 'Los Angeles', 'houston': 'Houston'}


def _read_store():
    try:
        data = json.loads(SELECTIONS_PATH.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def read(investigation_id):
    return _read_store().get(opportunity_report._id(investigation_id))


def _save(investigation_id, record):
    store = _read_store()
    store[investigation_id] = record
    opportunity_report._atomic_json(SELECTIONS_PATH, store)


def _market(stage3, intake):
    market = MARKETS.get(str((stage3.get('inputs') or {}).get('market') or ''))
    if market: return market
    city = str(intake.get('city') or '').split(',')[0].strip()
    if city in MARKETS.values(): return city
    raise ValueError('The selected property market is not supported by Finance Lab.')


def build(investigation_id, business, report_id, *, stage1, handoff, stage3, report, report_stage3):
    """Validate the chain of evidence and return the Stage 4 handoff for one shortlisted business."""
    identity = opportunity_report.validate_sources(investigation_id, stage1, handoff, stage3)
    if not report or report.get('report_id') != report_id:
        raise ValueError('Review the current opportunity report before choosing a business.')
    opportunity_report.validate_report(report)
    current_hash = (stage3.get('reproduce') or {}).get('output_hash')
    if not isinstance(report_stage3, dict) or (report_stage3.get('reproduce') or {}).get('output_hash') != current_hash:
        raise ValueError('Business screening changed after the opportunity report. Regenerate the report before choosing.')
    order = [c.get('business') for c in stage3.get('shortlist') or []]
    if report['screening'].get('candidate_order') != order:
        raise ValueError('The opportunity report does not match the current shortlist.')
    candidate = next((c for c in stage3.get('shortlist') or [] if c.get('business') == business), None)
    if candidate is None:
        raise ValueError('Choose one of the shortlisted businesses.')
    s4 = candidate.get('stage4_inputs') or {}
    intake = handoff.get('intake') or {}
    listing = handoff.get('selected_listing') or {}
    raw_reserve = (stage3.get('inputs') or {}).get('reserve')
    reserve = None if raw_reserve in (None, '', 'unavailable') else float(raw_reserve)
    claims = next((c.get('claims') for c in report['narrative'].get('candidates') or [] if c.get('business') == business), [])
    return {
        'schema_version': VERSION,
        'investigation_id': investigation_id,
        'report_id': report_id,
        'selected_at': datetime.now(timezone.utc).isoformat(),
        'business': business,
        'label': candidate.get('label'),
        'rank': candidate.get('rank'),
        'identity': {**identity, 'address': s4.get('address') or listing.get('address')},
        'market': _market(stage3, intake),
        'area_sqft': s4.get('space_sf'),
        'area_source': 'Stage 3 selected space (CoStar listing)',
        'monthly_rent': s4.get('monthly_rent'),
        'rent_source': s4.get('rent_source') or 'unknown',
        'lease_basis': s4.get('lease_basis'),
        'build_months': s4.get('build_months'),
        'budget': float(intake.get('total_budget_usd')),
        'budget_source': 'Stage 0 intake',
        'reserve': reserve,
        'reserve_status': 'provided' if reserve is not None else 'unavailable',
        'financing_intake': intake.get('financing'),
        'stage3': {
            'output_hash': current_hash,
            'cash_to_open': s4.get('cash_to_open'),
            'unpriced': s4.get('unpriced') or [],
            'max_reserve_base_case': s4.get('max_reserve_base_case'),
            'conditions': candidate.get('conditions') or [],
            'unknowns': candidate.get('unknowns') or [],
            'verification_questions': candidate.get('verification_questions') or [],
        },
        'report_claims': claims,
        'latest_run': None,
    }


def select(investigation_id, business, report_id, **sources):
    record = build(opportunity_report._id(investigation_id), business, opportunity_report._id(report_id), **sources)
    _save(investigation_id, record)
    return record


def _close(a, b, tolerance=0.5):
    try: return abs(float(a) - float(b)) <= tolerance
    except (TypeError, ValueError): return a is None and b is None


def check_run(investigation_id, payload):
    """Reject a Finance Lab run that changes the selected property, space or business."""
    record = read(investigation_id)
    if not record:
        raise ValueError('No business has been selected for this investigation.')
    identity = record['identity']
    if str(payload.get('property_id')) != str(identity['property_id']) or str(payload.get('space_id')) != str(identity['space_id']):
        raise ValueError('Finance Lab property or space differs from the selected investigation.')
    if payload.get('business') != record['business']:
        raise ValueError('Finance Lab business differs from the selected business.')
    if not _close(payload.get('area_sqft'), record['area_sqft']):
        raise ValueError('Finance Lab area differs from the selected space.')
    return record


def record_run(investigation_id, payload, result):
    """Attach provenance to the result and save the latest run with the selection."""
    record = read(investigation_id)
    checks = [('budget', 'budget', 'Stage 0 budget'), ('reserve', 'reserve', 'Stage 0 reserve'),
              ('monthly_rent', 'monthly_rent', 'Stage 3 rent'), ('build_months', 'build_months', 'Stage 3 build months')]
    deviations = [{'field': field, 'handoff': record[key], 'used': payload.get(field), 'source': source}
                  for field, key, source in checks if not _close(payload.get(field), record[key])]
    ledger = ((result.get('ledger') or {}).get('cash_to_open')) or {}
    stage3_totals = record['stage3'].get('cash_to_open') or {}
    consistency = {kind: {'stage3': stage3_totals.get(kind), 'finance_lab': ledger.get(kind),
                          'matches': _close(stage3_totals.get(kind), ledger.get(kind), 1)} for kind in ('low', 'base', 'high')}
    result['handoff'] = {'investigation_id': investigation_id, 'report_id': record['report_id'], 'business': record['business'],
                         'identity': record['identity'], 'deviations': deviations, 'stage3_cash_to_open_consistency': consistency}
    record['latest_run'] = {'at': datetime.now(timezone.utc).isoformat(), 'request': payload, 'result': result}
    _save(investigation_id, record)
    return result
