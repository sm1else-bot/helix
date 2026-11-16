"""Run the pinned Stage 3 engine in its own Python interpreter."""
import json
import sys
from pathlib import Path
from zipfile import ZipFile
import os

sys.path.insert(0, str(Path(__file__).resolve().parent / 'vendor'))
import stage3

try:
    args = json.load(sys.stdin)
    if args.get('listing_id') is not None or args.get('space_id') is not None:
        listing_id, space_id = str(args.get('listing_id') or ''), str(args.get('space_id') or '')
        if not listing_id.isdigit() or not space_id.isdigit():
            raise ValueError('The selected listing and space IDs are required to identify a suite.')
        matches, keys = [], {}
        with ZipFile(args['kit']) as archive, archive.open('data/commercial-listing-summary.jsonl', pwd=os.environ.get('COSTAR_KIT_PASSWORD','').encode() or None) as stream:
            for line in stream:
                row = json.loads(line)
                if row.get('listingStatus') != 1: continue
                for space in (row.get('forLeaseSummary') or {}).get('spaces') or []:
                    if str(space.get('propertyId')) != str(args['property_id']) or not space.get('squareFeetMax'): continue
                    key = stage3.nb.space_value({'suite':space.get('suite'),'listing_id':row.get('id'),'square_feet':space['squareFeetMax']})
                    identity = (str(row.get('id')), str(space.get('id')))
                    keys.setdefault(key, set()).add(identity)
                    if identity == (listing_id, space_id): matches.append(key)
        if len(set(matches)) != 1:
            raise ValueError('The selected CoStar space is not uniquely available to Stage 3.')
        suite = matches[0]
        if len(keys.get(suite, ())) != 1:
            raise ValueError('Multiple CoStar spaces share this Stage 3 suite key; selection cannot be verified.')
        building, _ = __import__('opportunity').find_building(args['property_id'], args.get('kit'))
        listed = [s for s in (building or {}).get('spaces', []) if s.get('value') == suite]
        if len(listed) != 1 or str(listed[0].get('listing_id')) != listing_id or str(listed[0].get('space_id')) != space_id:
            raise ValueError('Stage 3 inventory does not match the selected CoStar space.')
        args['suite'] = suite
    result = stage3.run(args['property_id'], args['budget'], reserve=args.get('reserve'),
                        suite=args.get('suite'), kit=args.get('kit'), save=False)
    # stage3._git_rev() sees the enclosing Helix checkout, not the vendored code.
    # The underlying engine and its dependencies are pinned to this commit.
    result['reproduce']['code_version'] = '1cc494d'
    json.dump({'result': result}, sys.stdout, default=str)
except Exception as error:
    json.dump({'error': str(error), 'error_type': type(error).__name__}, sys.stdout)
    sys.exit(1)
