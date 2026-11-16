"""Thin, offline adapter for Asmit's pinned Stage 3 implementation."""
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parent
LOCK = threading.Lock()


def run(property_id, budget, reserve=None, suite=None, *, kit=None, kit_password=None, listing_id=None, space_id=None):
    if isinstance(property_id, bool) or not str(property_id or '').isdigit() or int(property_id) <= 0:
        raise ValueError('property_id must be a positive CoStar property ID.')
    def amount(value, label, allow_zero=False):
        if isinstance(value, bool): raise ValueError(f'{label} must be numeric.')
        try: number = float(value)
        except (ValueError, TypeError): raise ValueError(f'{label} must be numeric.') from None
        if not math.isfinite(number) or number < 0 or (not allow_zero and number == 0):
            raise ValueError(f'{label} must be finite and {"nonnegative" if allow_zero else "positive"}.')
        return number
    budget = amount(budget, 'budget')
    reserve = amount(reserve, 'reserve', True) if reserve is not None else None
    if suite is not None and (not isinstance(suite, str) or not suite.strip() or len(suite) > 200):
        raise ValueError('suite must be a nonempty Stage 3 space identifier.')
    env = os.environ.copy()
    env['JURISDICTION_OFFLINE'] = '1'
    env['NEIGHBORHOOD_OFFLINE'] = '1'
    if kit_password:
        env['COSTAR_KIT_PASSWORD'] = kit_password.decode() if isinstance(kit_password, bytes) else kit_password
    if (listing_id is None) != (space_id is None):
        raise ValueError('Both listing_id and space_id are required for a verified mapping.')
    payload = {'property_id': int(property_id), 'budget': budget, 'reserve': reserve, 'suite': suite, 'kit': str(kit) if kit else None,
               'listing_id': listing_id, 'space_id': space_id}
    with LOCK:
        # Reuse existing public Census response cache without altering its source.
        source = ROOT.parent / 'stage2' / 'output' / 'jurisdiction'
        destination = ROOT / 'vendor' / 'output' / 'jurisdiction'
        if source.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            for item in source.glob('*.json'):
                target = destination / item.name
                if not target.exists(): shutil.copyfile(item, target)
        try:
            result = subprocess.run([sys.executable, str(ROOT / 'worker.py')], input=json.dumps(payload),
                                    capture_output=True, text=True, env=env, timeout=180, check=False)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError('Stage 3 timed out while reading local evidence.') from error
    try: response = json.loads(result.stdout)
    except json.JSONDecodeError as error: raise RuntimeError('Stage 3 worker returned an invalid response.') from error
    if result.returncode:
        if response.get('error_type') == 'ValueError': raise ValueError(response.get('error', 'Stage 3 input is invalid.'))
        raise RuntimeError(f"Stage 3 engine failed ({response.get('error_type', 'unknown error')}).")
    output = response['result']
    if suite is not None and (output.get('inputs') or {}).get('suite') != suite:
        raise ValueError('The requested Stage 3 suite identifier was not found for this property.')
    return output
