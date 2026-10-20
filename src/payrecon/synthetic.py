"""Seeded synthetic data with ground truth.

Every invoice gets a scenario and a *true* outcome. The truth is what really
happened in the (fake) world, not what the matcher is expected to say, so the
evaluation shows where the matcher is blind.

What is deliberately hard here:

* `missing_reference` payments are real payments the deterministic matcher cannot
  link. They come in kinds (blank, bare prefix, letter-O typo, digits only, the
  customer's name, a mangled/nicknamed name) so you can see which ones a scorer or
  an LLM can recover and which nobody can.
* Half the invoices use a handful of standard amounts (500, 1,000, 1,500 ...), as
  rent, fees and subscriptions do, so "same amount" alone is often ambiguous.
* Failed payments carry the provider's free-text message. Most use known Daraja
  codes; some use an unknown code with a message the keyword rules do and do not
  cover.

All data is fake: names come from small pools, phone numbers are generated already
masked.
"""
from __future__ import annotations

import argparse
import csv
import random
import string
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from .models import Category, InternalStatus, Invoice, ProviderTxn, TxnStatus
from .providers.base import EAT
from .providers.csv_source import write_invoices_csv, write_transactions_csv

BASE_TIME = datetime(2026, 10, 1, 8, 0, tzinfo=EAT)

# (scenario, share of invoices)
SCENARIOS: list[tuple[str, float]] = [
    ("clean", 0.37),
    ("messy_reference", 0.10),
    ("redelivered_callback", 0.05),
    ("duplicate", 0.07),
    ("delayed", 0.07),
    ("failed", 0.08),
    ("amount_mismatch", 0.05),
    ("partial_failure", 0.04),
    ("missing", 0.05),
    ("missing_reference", 0.08),
    ("callback_lost", 0.04),
]

STANDARD_AMOUNTS = [500, 1000, 1500, 2500, 5000]

FIRST = ["John", "Mary", "Peter", "Grace", "James", "Faith", "Joseph", "Esther", "Samuel", "Ann",
         "David", "Lucy", "Daniel", "Rose", "Paul", "Jane", "Brian", "Mercy", "Kevin", "Janet"]
LAST = ["Kamau", "Wanjiru", "Otieno", "Achieng", "Mwangi", "Njeri", "Odhiambo", "Wambui", "Kiptoo", "Chebet",
        "Mutua", "Nyambura", "Omondi", "Akinyi", "Karanja", "Atieno", "Kimani", "Wafula", "Barasa", "Naliaka"]
NICKNAMES = {"John": "Johnie", "Joseph": "Joe", "Samuel": "Sam", "Daniel": "Dan", "Peter": "Pete",
             "James": "Jim", "David": "Dave", "Paul": "Pauly", "Kevin": "Kev", "Brian": "Bri"}

MISSING_REF_KINDS = ["blank", "prefix_only", "typo_ref", "digits_only", "name", "name_hard"]

# Known Daraja-style codes: code -> (true reason, provider message).
KNOWN_FAILURES = {
    "1037": ("timeout", "DS timeout user cannot be reached"),
    "1032": ("user_cancelled", "Request cancelled by user"),
    "1": ("insufficient_funds", "The balance is insufficient for the transaction"),
    "2001": ("wrong_pin", "The initiator information is invalid"),
    "1001": ("subscriber_busy", "Unable to lock subscriber, a transaction is already in process for the current subscriber"),
}
# Unknown code, free text: (true reason, message, hard). "hard" messages avoid the keyword rules.
NOVEL_FAILURES = [
    ("insufficient_funds", "Customer balance too low to complete the payment", False),
    ("user_cancelled", "Customer cancelled the payment prompt", False),
    ("timeout", "The request timed out before the customer responded", False),
    ("wrong_pin", "Customer entered an incorrect PIN", False),
    ("system_error", "Safaricom system busy, please try again later", False),
    ("insufficient_funds", "Wallet could not cover the requested amount", True),
    ("timeout", "Customer let the prompt lapse", True),
    ("user_cancelled", "Customer declined to authorise the charge", True),
    ("wrong_pin", "Authentication failed for the subscriber", True),
    ("system_error", "Upstream gateway returned an unexpected response", True),
]


@dataclass
class Dataset:
    invoices: list[Invoice]
    txns: list[ProviderTxn]
    truth: dict[str, Category]  # invoice_id -> what really happened
    scenario: dict[str, str]  # invoice_id -> scenario name
    unlinkable_txn_ids: set[str]  # txns no reference-based matcher can link (should surface as orphans)
    link_truth: dict[str, str] = field(default_factory=dict)  # unlinkable txn -> the invoice it really pays
    orphan_kind: dict[str, str] = field(default_factory=dict)  # unlinkable txn -> kind (or "stray")
    fail_reason: dict[str, str] = field(default_factory=dict)  # failed txn -> true reason
    fail_kind: dict[str, str] = field(default_factory=dict)  # failed txn -> known | novel_easy | novel_hard


def _messy(ref: str, rng: random.Random) -> str:
    num = ref.split("-")[1]
    n = int(num)
    return rng.choice([f"inv {n}", f"INV{num}", f"Inv-{n}", f"  inv-{num} ", f"INV {num}", f"inv{n}"])


def _mangle(token: str) -> str:
    return token[:-2] + token[-1] if len(token) >= 5 else token[:-1]


def _bad_reference(kind: str, i: int, first: str, last: str, rng: random.Random) -> str | None:
    if kind == "blank":
        return rng.choice([None, ""])
    if kind == "prefix_only":
        return "INV"
    if kind == "typo_ref":
        return f"INVO{i:04d}"  # letter O instead of a hyphen
    if kind == "digits_only":
        return str(i)
    if kind == "name":
        return rng.choice([f"{first} {last}", f"{first[0]} {last}", f"{last.upper()} {first[0]}", last])
    if kind == "name_hard":
        return f"{NICKNAMES.get(first) or _mangle(first)} {_mangle(last)}"
    raise AssertionError(kind)


def _failure(rng: random.Random) -> tuple[str, str, str, str]:
    """-> (result_code, result_desc, true_reason, kind)"""
    if rng.random() < 0.7:
        code = rng.choice(list(KNOWN_FAILURES))
        reason, desc = KNOWN_FAILURES[code]
        return code, desc, reason, "known"
    reason, desc, hard = rng.choice(NOVEL_FAILURES)
    return "9999", desc, reason, "novel_hard" if hard else "novel_easy"


def _amount(rng: random.Random) -> int:
    return rng.choice(STANDARD_AMOUNTS) if rng.random() < 0.5 else rng.randrange(100, 20001, 50)


def generate(n_invoices: int = 500, seed: int = 7, stray_rate: float = 0.04) -> Dataset:
    rng = random.Random(seed)

    counts = {name: int(share * n_invoices) for name, share in SCENARIOS}
    counts["clean"] += n_invoices - sum(counts.values())
    plan = [name for name, c in counts.items() for _ in range(c)]
    rng.shuffle(plan)

    used_ids: set[str] = set()

    def new_txn_id() -> str:
        while True:
            tid = "".join(rng.choices(string.ascii_uppercase + string.digits, k=10))
            if tid not in used_ids:
                used_ids.add(tid)
                return tid

    def msisdn() -> str:
        return f"2547****{rng.randint(0, 999):03d}"

    invoices: list[Invoice] = []
    txns: list[ProviderTxn] = []
    truth: dict[str, Category] = {}
    scenario_of: dict[str, str] = {}
    unlinkable: set[str] = set()
    link_truth: dict[str, str] = {}
    orphan_kind: dict[str, str] = {}
    fail_reason: dict[str, str] = {}
    fail_kind: dict[str, str] = {}

    for i, scenario in enumerate(plan, start=1):
        amount = _amount(rng)
        first, last = rng.choice(FIRST), rng.choice(LAST)
        created = BASE_TIME + timedelta(minutes=7 * i)
        invoice_id = f"I{i:05d}"
        ref = f"INV-{i:04d}"
        internal = InternalStatus.UNPAID

        def txn(status, amt, ref_raw, minutes, code=None, source="stk_callback", desc=None):
            return ProviderTxn(
                provider_txn_id=new_txn_id(),
                amount=Decimal(amt),
                status=status,
                timestamp=created + timedelta(minutes=minutes),
                reference_raw=ref_raw,
                msisdn_masked=msisdn(),
                result_code=code,
                source=source,
                result_desc=desc,
            )

        def failed(amt, minutes):
            code, desc, reason, kind = _failure(rng)
            t = txn(TxnStatus.FAILED, amt, ref, minutes, code, desc=desc)
            fail_reason[t.provider_txn_id] = reason
            fail_kind[t.provider_txn_id] = kind
            return t

        m = rng.randint(1, 180)
        if scenario == "clean":
            internal = InternalStatus.PAID
            txns.append(txn(TxnStatus.SUCCESS, amount, ref, m, "0"))
            true = Category.MATCHED
        elif scenario == "messy_reference":
            internal = InternalStatus.PAID
            txns.append(txn(TxnStatus.SUCCESS, amount, _messy(ref, rng), m, "0", "c2b_confirmation"))
            true = Category.MATCHED
        elif scenario == "redelivered_callback":
            internal = InternalStatus.PAID
            first_copy = txn(TxnStatus.SUCCESS, amount, ref, m, "0")
            txns += [first_copy, replace(first_copy, reference_raw=None, source="statement")]
            true = Category.MATCHED
        elif scenario == "duplicate":
            internal = InternalStatus.PAID
            txns += [
                txn(TxnStatus.SUCCESS, amount, ref, m, "0"),
                txn(TxnStatus.SUCCESS, amount, ref, m + rng.randint(1, 5), "0"),
            ]
            true = Category.DUPLICATE_PAYMENT
        elif scenario == "delayed":
            txns.append(txn(TxnStatus.PENDING, amount, ref, m))
            true = Category.PENDING
        elif scenario == "failed":
            for j in range(rng.choice([1, 2])):
                txns.append(failed(amount, m + 10 * j))
            true = Category.FAILED
        elif scenario == "amount_mismatch":
            paid = amount // 2 if rng.random() < 0.5 else amount + 500
            txns.append(txn(TxnStatus.SUCCESS, paid, ref, m, "0"))
            true = Category.AMOUNT_MISMATCH
        elif scenario == "partial_failure":
            part = amount // 2
            txns += [txn(TxnStatus.SUCCESS, part, ref, m, "0"), failed(amount - part, m + 15)]
            true = Category.PARTIAL_PAYMENT
        elif scenario == "missing":
            true = Category.MISSING_PAYMENT
        elif scenario == "missing_reference":
            kind = rng.choice(MISSING_REF_KINDS)
            t = txn(TxnStatus.SUCCESS, amount, _bad_reference(kind, i, first, last, rng), m, "0", "c2b_confirmation")
            txns.append(t)
            unlinkable.add(t.provider_txn_id)
            link_truth[t.provider_txn_id] = invoice_id
            orphan_kind[t.provider_txn_id] = kind
            true = Category.UNRECORDED_PAYMENT  # really paid, we just cannot link it
        elif scenario == "callback_lost":
            txns.append(txn(TxnStatus.SUCCESS, amount, ref, m, "0"))
            true = Category.UNRECORDED_PAYMENT
        else:  # pragma: no cover
            raise AssertionError(scenario)

        invoices.append(Invoice(invoice_id, ref, Decimal(amount), internal, created, f"{first} {last}"))
        truth[invoice_id] = true
        scenario_of[invoice_id] = scenario

    # Payments that belong to no invoice at all (wrong paybill account, rent, etc.)
    for _ in range(int(n_invoices * stray_rate)):
        t = ProviderTxn(
            provider_txn_id=new_txn_id(),
            amount=Decimal(_amount(rng)),
            status=TxnStatus.SUCCESS,
            timestamp=BASE_TIME + timedelta(minutes=rng.randint(0, 7 * n_invoices)),
            reference_raw=rng.choice(["ACC 99123", "RENT", "", None, "7780", f"{rng.choice(FIRST)} {rng.choice(LAST)}"]),
            msisdn_masked=msisdn(),
            result_code="0",
            source="c2b_confirmation",
        )
        txns.append(t)
        unlinkable.add(t.provider_txn_id)
        orphan_kind[t.provider_txn_id] = "stray"

    rng.shuffle(txns)
    return Dataset(invoices, txns, truth, scenario_of, unlinkable, link_truth, orphan_kind, fail_reason, fail_kind)


def write_dataset(ds: Dataset, out_dir: str | Path) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    write_invoices_csv(ds.invoices, out / "invoices.csv")
    write_transactions_csv(ds.txns, out / "transactions.csv")
    with (out / "truth.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["invoice_id", "scenario", "true_category"])
        for iid, cat in ds.truth.items():
            w.writerow([iid, ds.scenario[iid], cat.value])
    with (out / "link_truth.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["provider_txn_id", "invoice_id", "orphan_kind"])
        for tid, iid in ds.link_truth.items():
            w.writerow([tid, iid, ds.orphan_kind[tid]])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate synthetic reconciliation data with ground truth.")
    ap.add_argument("--n", type=int, default=500, help="number of invoices")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", type=Path, default=Path("data"))
    args = ap.parse_args(argv)
    ds = generate(args.n, args.seed)
    write_dataset(ds, args.out)
    print(f"wrote {len(ds.invoices)} invoices, {len(ds.txns)} transactions to {args.out}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
