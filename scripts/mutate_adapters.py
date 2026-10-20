"""Mutation check for the adapters and the executor's last-line checks.

Copies the project, injects one deliberate bug at a time, runs the adapter and executor tests, and reports whether
any test failed. A MISSED line means a bug that no test notices. Exit code 1 if anything is missed.

    python scripts/mutate_adapters.py          # needs pytest, requests, cryptography; takes a few minutes

Note: the 24-of-24 result in the README was produced with a minimal stand-in test runner (no real pytest was installable
where I built this). This script runs the same mutants through real pytest, and that invocation has not been run yet.
"""
import pathlib, shutil, subprocess, sys, tempfile

SRC = pathlib.Path(__file__).resolve().parent.parent
A, R = "src/payrecon/adapters/", "src/payrecon/recovery/"
KEEP = ("test_adapters_sql.py", "test_adapters_gateway.py", "test_live_stack.py", "test_recovery_executor.py", "test_recovery_store.py")
M = [
 (A+"books.py", 'if claimed.rowcount == 0:\n                return False', 'if False:\n                return False', "books: apply twice under one key"),
 (A+"books.py", 'if total != inv.amount:\n                raise BooksConflict', 'if False:\n                raise BooksConflict', "books: accept parts that do not add up"),
 (A+"books.py", 'if owner is not None:\n                    raise BooksConflict', 'if False:\n                    raise BooksConflict', "books: reuse a payment (explicit check)"),
 (A+"books.py", 'if inv.internal_status is not InternalStatus.UNPAID:', 'if False:', "books: apply to an invoice that is not unpaid"),
 (A+"books.py", 'if changed.rowcount != 1:', 'if False:', "books: ignore a lost update"),
 (A+"ledger.py", 'if _RANK[t.status.value] > _RANK[old_status]:', 'if True:', "ledger: let a lagging copy downgrade a status"),
 (A+"ledger.py", 'return "conflict" if to_dec(old_amount) != t.amount else "duplicate"', 'return "duplicate"', "ledger: hide an amount conflict"),
 (A+"ledger.py", 't.result_desc, t.request_id, t.timestamp, t.source, datetime.now(timezone.utc)),', 't.result_desc, None, t.timestamp, t.source, datetime.now(timezone.utc)),', "ledger: drop request_id on insert"),
 (A+"gateway.py", 'raise SendOutcomeUnknown(f"HTTP {r.status_code} {self._safe(data)}")', 'raise NotSentError(f"HTTP {r.status_code}")', "gateway: treat 5xx as provably not sent"),
 (A+"gateway.py", 'if _provably_not_sent(e):', 'if True:', "gateway: treat every transport error as not sent"),
 (A+"gateway.py", 'if done:\n            return done[0]', 'if False:\n            return done[0]', "gateway: push twice under one key"),
 (A+"gateway.py", 'if amount <= 0 or amount != amount.to_integral_value():', 'if amount <= 0:', "gateway: send fractional amounts"),
 (A+"gateway.py", 'if not reference or len(reference) > ACCOUNT_REF_MAX:', 'if not reference:', "gateway: send an over-long AccountReference"),
 (A+"gateway.py", 'if row[0] == "SENDING":', 'if False:', "gateway: retry a reversal that may already be out"),
 (A+"gateway.py", '            if live:\n                raise NotSentError', '            if False:\n                raise NotSentError', "gateway: allow a second live reversal of one payment"),
 (A+"gateway.py", 'raise SendOutcomeUnknown(f"push {checkout} was accepted but could not be recorded: {type(e).__name__}: {e}") from e', 'pass', "gateway: swallow a failure to record an accepted push"),
 (A+"gateway.py", 'if code != "0":\n            raise NotSentError', 'if False:\n            raise NotSentError', "gateway: ignore a declined ResponseCode"),
 (A+"gateway.py", 'status = "COMPLETED" if code == "0" else "FAILED"', 'status = "COMPLETED"', "gateway: report a failed reversal as completed"),
 (A+"gateway.py", 'self._refund_update(idempotency_key, "REJECTED")  # a clean rejection stays retryable under a new key', 'pass', "gateway: leave a rejected reversal SENDING"),
 (A+"security.py", 'elif not allow_plaintext:', 'elif False:', "security: store phone numbers unencrypted by default"),
 (A+"gateway.py", 'raise NotSentError(f"no stored payer for request {retry_of}', 'raise SendOutcomeUnknown(f"no stored payer for request {retry_of}', "gateway: wrong class for unknown payer"),
 (R+"executor.py", 'if sum((amt for _, amt in parts), Decimal(0)) != total:', 'if False:', "executor: apply parts that no longer add up"),
 (R+"executor.py", 'if t is None or t.status is not TxnStatus.SUCCESS:', 'if t is None:', "executor: apply a part that is no longer a success"),
 (R+"executor.py", 'except NotSentError as e:\n            self.store.abort_side_effect(a.action_id, self.worker, str(e))', 'except NotSentError as e:\n            pass', "executor: a provably-not-sent call still counts as a send"),
]
def run(root):
    p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "--tb=no", *[str(root / "tests" / k) for k in KEEP]],
                       capture_output=True, text=True, timeout=900, cwd=root, env={"PYTHONPATH": str(root / "src"), "PATH": "/usr/bin:/bin"})
    out = p.stdout + p.stderr
    fails = sorted({l.split("::")[-1].split(" ")[0].split("[")[0] for l in out.splitlines() if l.startswith("FAILED ")})
    summ = [l for l in out.splitlines() if " passed" in l or " failed" in l][-1:]
    return (summ[0] if summ else "no summary"), fails, ("SyntaxError" in out)
caught = missed = 0
for f, old, new, label in M:
    root = pathlib.Path(tempfile.mkdtemp(prefix="payrecon-mut-")); shutil.rmtree(root, ignore_errors=True)
    shutil.copytree(SRC, root, ignore=shutil.ignore_patterns("__pycache__", ".git"))
    for t in (root / "tests").glob("test_*.py"):
        if t.name not in KEEP: t.unlink()
    path = root / f; s = path.read_text()
    if s.count(old) != 1:
        print(f"SKIP  ({s.count(old)} anchors) {label}", flush=True); continue
    path.write_text(s.replace(old, new))
    summ, fails, err = run(root)
    if err: print(f"BAD   (syntax) {label}", flush=True); continue
    if fails or "no summary" in summ:
        caught += 1; print(f"CAUGHT {label}  -> {fails[:2]}", flush=True)
    else:
        missed += 1; print(f"MISSED {label}  [{summ}]", flush=True)
print(f"\ncaught {caught}, missed {missed}", flush=True)
sys.exit(1 if missed else 0)
