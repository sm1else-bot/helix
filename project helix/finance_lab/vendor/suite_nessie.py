"""Capital One Nessie as the suite's money source: the user's cash and recurring bills come from a Nessie sandbox
account (simulated bank data), then feed the budget, the cash-to-open check and the 12-month view.

Uses Jayant's nessie_pilot.py client as is. Every successful read is cached in playground/output/nessie/ (gitignored)
with its retrieval time, so the demo replays offline; a cached answer is labelled as cached, never as live.

    python3 playground/with_env.py python3 playground/suite_nessie.py --seed   # create the demo persona once
    python3 playground/with_env.py python3 playground/suite_nessie.py          # read it (and cache it)
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from nessie_pilot import buyer_from_nessie, nessie, seed_buyer

CACHE = Path(__file__).parent / "output" / "nessie"
DEMO = CACHE / "demo-account.json"
SOURCE = "Capital One Nessie sandbox (simulated bank data)"
PERSONA = {"first_name": "Demo", "last_name": "Founder", "account_nickname": "Venture Suite demo: savings to invest",
           "address": {"street_number": "1", "street_name": "Main St", "city": "Houston", "state": "TX",
                       "zip": "77002"},
           "starting_cash": 500000,
           "recurring_bills": [{"payee": "Personal living expenses", "monthly": 3000, "day_of_month": 1,
                                "start_date": "2026-12-01"}]}


def seed():
    """Create the sandbox persona (once) and remember its account id."""
    customer, account = seed_buyer(PERSONA)
    CACHE.mkdir(parents=True, exist_ok=True)
    DEMO.write_text(json.dumps({"customer_id": customer, "account_id": account,
                                "created_at": datetime.now(timezone.utc).isoformat()}))
    return account


def demo_account_id():
    return json.loads(DEMO.read_text())["account_id"] if DEMO.exists() else None


def read(account_id=None):
    """{account_id, nickname, balance, bills, monthly_bills, retrieved_at, cached, source}; live first, then cache."""
    account_id = account_id or demo_account_id()
    if not account_id:
        raise RuntimeError("No Nessie demo account yet: run suite_nessie.py --seed once online.")
    path = CACHE / f"account-{account_id}.json"
    try:
        buyer = buyer_from_nessie(account_id)
        nickname = (nessie("GET", f"/accounts/{account_id}") or {}).get("nickname")
        out = {"account_id": account_id, "nickname": nickname, "balance": buyer["starting_cash"],
               "bills": buyer["recurring_bills"], "monthly_bills": round(sum(b["monthly"] for b in buyer["recurring_bills"]), 2),
               "retrieved_at": datetime.now(timezone.utc).isoformat(), "source": SOURCE}
        CACHE.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out))
        return {**out, "cached": False}
    except RuntimeError as error:  # no key, offline, or Nessie down: the last real answer, labelled as cached
        if path.exists():
            return {**json.loads(path.read_text()), "cached": True, "why_cached": str(error)[:160]}
        raise


if __name__ == "__main__":
    if "--seed" in sys.argv:
        print("seeded account", seed())
    print(json.dumps(read(), indent=2))
