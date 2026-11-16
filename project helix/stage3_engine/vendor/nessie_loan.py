"""Capital One Nessie business loan: borrow the gap instead of paying with cash alone.

When the user's Nessie balance can't cover cash to open plus their reserve, this works out how much to borrow,
the monthly payment for an assumed APR and term, and month-by-month interest vs principal. With --create it stores
the loan on the user's Nessie sandbox account (POST /accounts/{id}/loans), so the loan is a real Nessie record the
12-month view can read back.

Nessie stores amount, monthly payment, credit score, type and status. It has no interest rate field, so the payment
is computed here from an assumed APR and the APR and term go into the loan's description. Every loan is simulated
sandbox data: not a credit offer, not a Capital One rate.

Reads and creates are cached in playground/output/nessie/ (gitignored), so the demo replays offline. A cached
answer is labelled cached, never live.

    python3 playground/nessie_loan.py --need 560000 --balance 500000 --reserve 50000         # offline math only
    python3 playground/nessie_loan.py --account ID --need 560000 --reserve 50000 --create      # store it in Nessie
    python3 playground/nessie_loan.py --account ID                                             # read it back
"""
import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path

CACHE = Path(__file__).parent / "output" / "nessie"
SOURCE = "Capital One Nessie sandbox (simulated loan)"
LABEL = "Simulated sandbox loan; assumed APR and term, not a credit offer or a Capital One rate"
DEFAULT_APR, DEFAULT_TERM, DEFAULT_SCORE = 9.0, 60, 720
ROUND_TO = 5000  # people borrow round numbers
PAYEE = "Nessie business loan payment"


def payment(principal, apr, months):
    """Fixed monthly payment for an amortized loan."""
    if principal <= 0:
        return 0.0
    rate = apr / 100 / 12
    if rate == 0:
        return principal / months
    return principal * rate / (1 - (1 + rate) ** -months)


def amount_to_borrow(cash_needed, balance, reserve=0.0, round_to=ROUND_TO):
    """Smallest round loan that covers cash to open and keeps the reserve untouched at opening. 0 if cash covers it."""
    gap = cash_needed + reserve - balance
    return 0 if gap <= 0 else int(math.ceil(gap / round_to) * round_to)


def schedule(principal, apr, months, show=12):
    """Interest vs principal per month for the first `show` months (the '12-month view')."""
    pay, rate, left, rows = payment(principal, apr, months), apr / 100 / 12, float(principal), []
    for month in range(1, min(show, months) + 1):
        interest = left * rate
        left = max(0.0, left - (pay - interest))
        rows.append({"month": month, "payment": round(pay, 2), "interest": round(interest, 2),
                     "principal": round(pay - interest, 2), "balance_left": round(left, 2)})
    return rows


def plan(cash_needed, balance, reserve=0.0, apr=DEFAULT_APR, months=DEFAULT_TERM):
    """Everything the UI shows for the loan option; pure math, no network."""
    amount = amount_to_borrow(cash_needed, balance, reserve)
    pay = payment(amount, apr, months)
    rows = schedule(amount, apr, months)
    return {"needed": amount > 0, "amount": amount, "apr": apr, "term_months": months,
            "monthly_payment": round(pay, 2), "first_year_interest": round(sum(r["interest"] for r in rows), 2),
            "total_interest": round(pay * months - amount, 2), "schedule": rows,
            "cash_needed": cash_needed, "balance": balance, "reserve": reserve, "label": LABEL}


def with_loan(buyer, loan):
    """Buyer for nessie_pilot.simulate: loan proceeds land in cash, the payment becomes a recurring bill."""
    if not loan or not loan.get("amount"):
        return buyer
    bill = {"payee": f"{PAYEE} ({loan['apr']:g}% APR assumed, {loan['term_months']} mo)",
            "monthly": loan["monthly_payment"]}
    return {**buyer, "starting_cash": buyer["starting_cash"] + loan["amount"],
            "recurring_bills": [*buyer["recurring_bills"], bill]}


# --- Nessie ---------------------------------------------------------------

def _nessie(*args):
    from nessie_pilot import nessie  # imported lazily so the math works without the client
    return nessie(*args)


def _cache(account_id):
    return CACHE / f"loan-{account_id}.json"


def _description(loan):
    return f"Venture Suite business loan: {loan['apr']:g}% APR assumed, {loan['term_months']} months (simulated)"


def _parse(record):
    """Nessie loan record -> our loan dict. APR/term come back from the description we wrote."""
    desc = record.get("description") or ""
    apr = term = None
    try:
        apr = float(desc.split("% APR")[0].split(":")[-1].strip())
        term = int(desc.split("assumed, ")[1].split(" months")[0])
    except (IndexError, ValueError):
        pass
    return {"loan_id": record.get("_id") or record.get("id"), "amount": float(record.get("amount") or 0),
            "monthly_payment": float(record.get("monthly_payment") or 0), "apr": apr, "term_months": term,
            "credit_score": record.get("credit_score"), "status": record.get("status"), "type": record.get("type"),
            "description": desc}


def _save(account_id, loan, cached=False, why=None):
    out = {**loan, "account_id": account_id, "source": SOURCE, "label": LABEL,
           "retrieved_at": datetime.now(timezone.utc).isoformat()}
    CACHE.mkdir(parents=True, exist_ok=True)
    _cache(account_id).write_text(json.dumps(out))
    return {**out, "cached": cached, **({"why_cached": why} if why else {})}


def create(account_id, amount, apr=DEFAULT_APR, months=DEFAULT_TERM, credit_score=DEFAULT_SCORE):
    """Store the loan on the Nessie account. Status stays 'pending': nothing is approved."""
    loan = {"amount": int(amount), "apr": apr, "term_months": months,
            "monthly_payment": round(payment(amount, apr, months), 2)}
    response = _nessie("POST", f"/accounts/{account_id}/loans", {
        "type": "small business", "status": "pending", "credit_score": int(credit_score),
        "monthly_payment": loan["monthly_payment"], "amount": loan["amount"], "description": _description(loan)})
    record = (response or {}).get("objectCreated") or response or {}
    return _save(account_id, {**_parse(record), "apr": apr, "term_months": months})


def read(account_id):
    """Latest Venture Suite loan on the account; live first, then the cached copy (labelled cached). None if none."""
    try:
        records = [r for r in (_nessie("GET", f"/accounts/{account_id}/loans") or [])
                   if "Venture Suite" in (r.get("description") or "")]
        if not records:
            return None
        return _save(account_id, _parse(records[-1]))
    except RuntimeError as error:  # no key, offline, or Nessie down
        path = _cache(account_id)
        if path.exists():
            return {**json.loads(path.read_text()), "cached": True, "why_cached": str(error)[:160]}
        raise


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--account", help="Nessie account id (balance is read from it)")
    p.add_argument("--balance", type=float, help="cash on hand, if no account")
    p.add_argument("--need", type=float, help="cash to open")
    p.add_argument("--reserve", type=float, default=0)
    p.add_argument("--apr", type=float, default=DEFAULT_APR)
    p.add_argument("--term", type=int, default=DEFAULT_TERM)
    p.add_argument("--create", action="store_true", help="store the loan on the Nessie account")
    a = p.parse_args()
    if a.need is None:
        if not a.account:
            p.error("--need or --account is required")
        print(json.dumps(read(a.account), indent=2))
        return
    balance = a.balance
    if balance is None:
        if not a.account:
            p.error("--balance or --account is required")
        balance = float(_nessie("GET", f"/accounts/{a.account}")["balance"])
    result = plan(a.need, balance, a.reserve, a.apr, a.term)
    if a.create:
        if not a.account:
            p.error("--create needs --account")
        result["nessie_loan"] = create(a.account, result["amount"], a.apr, a.term) if result["needed"] else None
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
