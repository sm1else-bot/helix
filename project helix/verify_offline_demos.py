#!/usr/bin/env python3
"""Verify a private Helix capture archive without making network requests.

An integrity pass is not an offline-demo pass: incomplete captures cause a
non-zero exit. Raw sponsor data and credentials must not be committed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ARCHIVE = Path(__file__).resolve().parent / '.offline-archive'
SCENARIOS = ('la-south-broadway', 'la-west-pico', 'la-panama',
             'houston-hardy', 'houston-wynnwood')


def verify_scenario(root: Path, scenario: str) -> list[str]:
    directory = root / 'scenarios' / scenario
    issues = []
    try:
        manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        return [f'manifest unavailable or invalid ({type(exc).__name__})']
    if manifest.get('scenario') != scenario:
        issues.append('scenario identity does not match manifest')
    for key in ('property_id', 'listing_id', 'space_id'):
        if not str(manifest.get(key) or '').isdigit():
            issues.append(f'missing or invalid {key}')
    if manifest.get('status') != 'VERIFIED':
        issues.append(f"scenario is {manifest.get('status', 'unmarked')}, not verified complete")
    inventory = manifest.get('assets')
    if not isinstance(inventory, list) or not inventory:
        issues.append('checksummed asset inventory missing')
        return issues
    for item in inventory:
        name = item.get('path') if isinstance(item, dict) else None
        expected = item.get('sha256') if isinstance(item, dict) else None
        if not isinstance(name, str) or not isinstance(expected, str):
            issues.append('invalid asset inventory entry')
            continue
        path = (directory / name).resolve()
        if not path.is_relative_to(directory.resolve()) or not path.is_file():
            issues.append(f'asset missing or outside scenario: {name}')
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            issues.append(f'checksum mismatch: {name}')
        if path.suffix == '.json':
            try:
                json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                issues.append(f'invalid JSON: {name}')
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, default=ARCHIVE)
    args = parser.parse_args()
    failed = False
    for name in SCENARIOS:
        problems = verify_scenario(args.archive, name)
        if problems:
            failed = True
            print(f'{name}: INCOMPLETE')
            for problem in problems:
                print(f'  - {problem}')
        else:
            print(f'{name}: manifest and checksums pass')
    if not failed:
        print('Checksum checks passed; full offline and identity verification still requires a browser-level audit.')
    return int(failed)


if __name__ == '__main__':
    raise SystemExit(main())
