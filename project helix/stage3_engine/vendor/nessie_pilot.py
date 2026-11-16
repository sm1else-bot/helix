"""Nessie pilot: can the buyer afford to get this building to its new use?

Runs a month-by-month cash simulation for each redevelopment scenario in a
JSON file (e.g. keep a warehouse as-is vs convert it to an indoor sports
facility), optionally with a permit delay, and reports the first month the
buyer's cash falls below their minimum reserve and how much extra funding
would cover it.

The buyer's starting cash and recurring bills can come from Capital One's
Nessie sandbox (simulated bank data) instead of the JSON file:

  --seed-nessie        create a simulated buyer (customer, checking account,
                       opening deposit, recurring bills) and print the IDs
  --nessie-account ID  read balance + recurring bills from that account

The simulation itself is deterministic and runs offline. Nessie stores
records; it does not project balances forward, so projection happens here.
"""
import argparse
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

NESSIE = "https://api.nessieisreal.com"


# --- Nessie ---------------------------------------------------------------

def nessie(method, path, body=None):
    key = os.environ.get("NESSIE_API_KEY")
    if not key:
        raise RuntimeError("Set NESSIE_API_KEY (from the Nessie developer portal) to use Nessie.")
    url = f"{NESSIE}{path}?{urlencode({'key': key})}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    try:
        with urlopen(Request(url, data=data, headers=headers, method=method), timeout=20) as r:
            return json.loads(r.read() or b"null")
    except HTTPError as error:
        detail = error.read().decode(errors="replace")[:300]
        raise RuntimeError(f"Nessie {method} {path}: HTTP {error.code} {detail}") from None
    except URLError as error:
        raise RuntimeError(f"Could not reach Nessie: {error.reason}") from None


def created_id(response):
    # Nessie wraps created objects as {"code": 201, "message": ..., "objectCreated": {...}}.
    obj = (response or {}).get("objectCreated") or response or {}
    return obj.get("_id") or obj.get("id")


def seed_buyer(buyer):
    customer = created_id(nessie("POST", "/customers", {
        "first_name": buyer["first_name"], "last_name": buyer["last_name"],
        "address": buyer["address"],
    }))
    account = created_id(nessie("POST", f"/customers/{customer}/accounts", {
        "type": "Checking", "nickname": buyer["account_nickname"], "rewards": 0,
        "balance": buyer["starting_cash"],
    }))
    for bill in buyer["recurring_bills"]:
        nessie("POST", f"/accounts/{account}/bills", {
            "status": "recurring", "payee": bill["payee"], "nickname": bill["payee"],
            "payment_date": bill.get("start_date", "2026-12-01"),
            "recurring_date": bill.get("day_of_month", 1),
            "payment_amount": bill["monthly"],
        })
    return customer, account


def buyer_from_nessie(account_id):
    account = nessie("GET", f"/accounts/{account_id}")
    bills = nessie("GET", f"/accounts/{account_id}/bills") or []
    recurring = [
        {"payee": b.get("payee") or b.get("nickname") or "bill", "monthly": float(b["payment_amount"])}
        for b in bills if b.get("status") == "recurring"
    ]
    return {"starting_cash": float(account["balance"]), "recurring_bills": recurring,
            "source": f"Nessie account {account_id}"}


# --- Simulation -----------------------------------------------------------

def simulate(scenario, buyer, months, reserve, permit_delay=0):
    """Monthly cash balance for one scenario. Month 1 is the first month after purchase."""
    fixed = sum(b["monthly"] for b in buyer["recurring_bills"])
    permit_end = scenario.get("permit_months", 0) + permit_delay
    build_months = scenario.get("build_months", 0)
    build_cost = scenario.get("build_cost", 0)
    opening = permit_end + build_months + 1  # first month with operating income
    cash = buyer["starting_cash"] - scenario.get("upfront_cost", 0)
    rows = []
    for month in range(1, months + 1):
        outflow = fixed + scenario.get("monthly_carry", 0)
        if permit_end < month <= permit_end + build_months:
            outflow += build_cost / build_months
        inflow = 0.0
        if month >= opening:
            ramp = scenario.get("ramp_months", 0)
            share = 1.0 if not ramp else min(1.0, (month - opening + 1) / ramp)
            inflow = scenario.get("monthly_income", 0) * share
            outflow += scenario.get("monthly_operating_cost", 0)
        cash += inflow - outflow
        rows.append({"month": month, "inflow": round(inflow), "outflow": round(outflow),
                     "cash": round(cash)})
    lowest = min(rows, key=lambda r: r["cash"])
    breach = next((r["month"] for r in rows if r["cash"] < reserve), None)
    return {
        "scenario": scenario["name"], "permit_delay_months": permit_delay,
        "opening_month": opening, "rows": rows,
        "lowest_cash": lowest["cash"], "lowest_month": lowest["month"],
        "first_month_below_reserve": breach,
        "extra_funding_needed": max(0, reserve - lowest["cash"]),
    }


def report(result, reserve):
    print(f"\n== {result['scenario']}  (permit delay +{result['permit_delay_months']} mo, "
          f"opens month {result['opening_month']})")
    print(f"{'mo':>3} {'in':>10} {'out':>10} {'cash':>12}")
    for r in result["rows"]:
        flag = "  < reserve" if r["cash"] < reserve else ""
        print(f"{r['month']:>3} {r['inflow']:>10,} {r['outflow']:>10,} {r['cash']:>12,}{flag}")
    if result["first_month_below_reserve"]:
        print(f"-> drops below the ${reserve:,.0f} reserve in month {result['first_month_below_reserve']}; "
              f"lowest ${result['lowest_cash']:,} in month {result['lowest_month']}; "
              f"needs ${result['extra_funding_needed']:,} extra funding")
    else:
        print(f"-> stays above the ${reserve:,.0f} reserve (lowest ${result['lowest_cash']:,})")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scenario_file", type=Path)
    parser.add_argument("--delay", type=int, default=3, help="Permit delay in months for the stress case.")
    parser.add_argument("--seed-nessie", action="store_true")
    parser.add_argument("--nessie-account")
    parser.add_argument("--json", type=Path, help="Also write results as JSON for the UI.")
    args = parser.parse_args()

    config = json.loads(args.scenario_file.read_text())
    buyer = {**config["buyer"], "source": "scenario file"}
    try:
        if args.seed_nessie:
            customer, account = seed_buyer(config["buyer"])
            print(f"Seeded Nessie buyer: customer {customer}, account {account}")
            args.nessie_account = account
        if args.nessie_account:
            buyer = buyer_from_nessie(args.nessie_account)
    except RuntimeError as error:
        parser.exit(1, f"{error}\n")

    reserve, months = config["min_cash_reserve"], config["months"]
    print(f"Property: {config['property']['address']}  |  buyer cash ${buyer['starting_cash']:,.0f} "
          f"({buyer['source']})  |  reserve ${reserve:,.0f}")
    print("All costs, rents and timings are scenario ASSUMPTIONS, not market data.")
    results = []
    for scenario in config["scenarios"]:
        delayable = scenario.get("permit_months") or scenario.get("build_months")
        for delay in sorted({0, args.delay if delayable else 0}):
            result = simulate(scenario, buyer, months, reserve, delay)
            report(result, reserve)
            results.append(result)
    if args.json:
        args.json.write_text(json.dumps({"buyer": buyer, "reserve": reserve, "results": results}, indent=2))


if __name__ == "__main__":
    sys.exit(main())
