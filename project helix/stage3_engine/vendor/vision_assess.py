"""Bounded AI layer over a compact physical profile of a building (Jessenth's Helix Stage 2: Matterport + Vexcel).

The profile is a small JSON file per CoStar property, dropped at playground/output/profiles/<property_id>.json:

    {"property_id": 4809147, "source": "Helix Stage 2", "generated_at": "2026-11-15T...",
     "observations": [
        {"id": "o1", "fact": "clear_height_ft", "value": 16, "unit": "ft", "source": "matterport",
         "source_id": "<model id>", "captured_at": "2025-03-27", "confidence": 0.6, "note": "..."},
        {"id": "o2", "fact": "entrances", "value": 2, "source": "vexcel", "source_id": "<image id>", ...}]}

Two layers, in order:
1. Deterministic (always): every observation becomes an evidence record in the team contract. Facts CoStar also
   records (clear height, area, floors, parking) are compared with CoStar: agrees / conflict / unknown.
2. AI (opt-in, AI_ASSESS=1): ONE capped call per (profile, business). Temperature 0, the prompt holds only the
   business's space needs and the observations (truncated), max 400 output tokens, strict JSON back. Each
   constraint must cite observation ids that exist, or it is dropped. Results are cached on disk by a hash of the
   inputs, so the demo replays offline and nothing is billed twice.

AI findings are labelled "AI assessment, not verified", are always status unknown + needs_human, and can only add
"needs a check" or "unknown" reasons that inform: they never support or block, never touch the cash or compliance
gates, and never decide the verdict.
"""
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from neighborhood_industries import INDUSTRIES
from reconcile import record

ROOT = Path(__file__).parent / "output"
PROFILES = ROOT / "profiles"
CACHE = ROOT / "ai"
MAX_OBS, MAX_PROMPT_CHARS, MAX_TOKENS = 40, 6000, 400
LABEL = "AI assessment, not verified"
# CoStar facts the profile can be checked against, with a tolerance for "agrees".
COMPARABLE = {"clear_height_ft": ("ceiling_ft", 1.0), "area_sqft": ("gba", 0.1), "floors": ("stories", 0),
              "parking_spaces": ("parking", 0.15)}


def load_profile(property_id):
    path = PROFILES / f"{int(property_id)}.json"
    if not path.exists():
        return None
    profile = json.loads(path.read_text())
    obs = profile.get("observations") or []
    for i, o in enumerate(obs):
        o.setdefault("id", f"o{i + 1}")
    profile["observations"] = obs[:MAX_OBS]
    return profile


def _close(a, b, tol):
    if tol < 1 and b:  # relative tolerance
        return abs(a - b) <= tol * abs(b)
    return abs(a - b) <= tol


def deterministic(profile, facts):
    """Evidence records for every observation; CoStar-comparable facts get agrees / conflict."""
    out = []
    for o in profile.get("observations", []):
        fact, value = o.get("fact"), o.get("value")
        status, note = "unknown", o.get("note") or "Observed in the scans; no CoStar field to compare."
        if fact in COMPARABLE and isinstance(value, (int, float)):
            costar_key, tol = COMPARABLE[fact]
            costar = (facts or {}).get(costar_key)
            if isinstance(costar, (int, float)) and costar:
                status = "agrees" if _close(float(value), float(costar), tol) else "conflict"
                note = (f"Scans show {value:g}{' ' + o['unit'] if o.get('unit') else ''}; CoStar records {costar:g}. "
                        + ("Within tolerance." if status == "agrees" else "Confirm on site before relying on either."))
        out.append(record(f"scan.{fact}", value, o.get("source") or profile.get("source") or "scan", o.get("source_id"),
                          o.get("captured_at"), float(o.get("confidence") or 0.5), o.get("url"), status,
                          needs_human=status != "agrees", note=f"[{o['id']}] {note}"))
    return out


def _prompt(profile, key):
    need = INDUSTRIES[key]["space"]
    needs = {k: v for k, v in need.items() if k in ("min_sf", "max_sf", "min_clear_ft", "ground_floor",
                                                     "prefers_street_level", "drive_in", "checks", "unit",
                                                     "per_unit_sf", "outdoor_sf_per_unit")}
    obs = [{k: o.get(k) for k in ("id", "fact", "value", "unit", "source", "confidence", "note")}
           for o in profile.get("observations", [])]
    body = json.dumps({"business": INDUSTRIES[key]["label"], "space_needs": needs, "observations": obs},
                      default=str)[:MAX_PROMPT_CHARS]
    system = ("You assess whether a building's observed physical features raise constraints for a business. Use ONLY "
              "the observations given; cite their ids. Never state facts not in them. Reply with JSON only: "
              '{"constraints":[{"text":"...","effect":"check"|"unknown","cites":["o1"],"uncertainty":"low"|"medium"|"high"}]} '
              "with at most 4 constraints. Use effect \"unknown\" when the observations can't settle it.")
    return [{"role": "system", "content": system}, {"role": "user", "content": body}]


def ai_assess(profile, key, chat=None):
    """Capped, cached AI constraints with citations. Returns (records, meta). Never raises."""
    messages = _prompt(profile, key)
    digest = hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).hexdigest()[:24]
    path = CACHE / f"{digest}.json"
    meta = {"label": LABEL, "cached": True, "model": None, "ran": False}
    if path.exists():
        saved = json.loads(path.read_text())
    elif os.environ.get("AI_ASSESS") == "1":
        try:
            from neighborhood import chat as default_chat, llm_config
            reply = (chat or default_chat)(messages, max_tokens=MAX_TOKENS)
            m = re.search(r"\{.*\}", reply, re.S)
            saved = {"constraints": (json.loads(m.group(0)) if m else {}).get("constraints", []),
                     "model": llm_config()["model"], "at": datetime.now(timezone.utc).isoformat()}
            CACHE.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(saved))
            meta["cached"] = False
        except Exception as error:  # noqa: BLE001  the AI layer is optional; the review never depends on it
            return [], {**meta, "error": f"{type(error).__name__}: {str(error)[:120]}"}
    else:
        return [], {**meta, "error": "AI layer off (set AI_ASSESS=1 after the team approves the model and cost)"}
    ids = {o["id"] for o in profile.get("observations", [])}
    out = []
    for c in (saved.get("constraints") or [])[:4]:
        cites = [x for x in (c.get("cites") or []) if x in ids]
        if not cites or c.get("effect") not in ("check", "unknown"):
            continue  # uncited or out-of-contract output is dropped, never shown
        out.append(record(f"ai.{key}.constraint", c.get("text", "")[:300], f"ai:{saved.get('model')}",
                          ",".join(cites), (saved.get("at") or "")[:10] or None, 0.3, None, "unknown",
                          needs_human=True, note=f"{LABEL} ({c.get('uncertainty', 'unknown')} uncertainty). "
                                                 f"Cites {', '.join(cites)}. effect={c['effect']}"))
    return out, {**meta, "model": saved.get("model"), "ran": True}
