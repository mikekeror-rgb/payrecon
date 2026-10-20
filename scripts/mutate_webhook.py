"""Mutation check for the webhook receiver (and the thread-safe result fetch it depends on).

Copies the project, injects one deliberate bug at a time, runs the receiver tests, and reports whether any test
failed. A MISSED line means a bug that no test notices. Exit code 1 if anything is missed.

    python scripts/mutate_webhook.py     # needs pytest, requests, cryptography; takes several minutes

Set PAYRECON_STANDIN_RUNNER=/path/to/runner.py to run the tests through `python runner.py <project root>` instead of pytest
(the results quoted in the README came from such a stand-in runner, because real pytest was not installable there).
"""
import os, pathlib, shutil, subprocess, sys, tempfile

SRC = pathlib.Path(__file__).resolve().parent.parent
W, D, C = "src/payrecon/webhook.py", "src/payrecon/adapters/db.py", "src/payrecon/livecli.py"
KEEP = ("test_webhook.py", "test_adapters_sql.py", "test_cli_live.py")
M = [
 (W, 'or not hmac.compare_digest(parts[2].encode(), self.token)', 'or False', "webhook: accept any token"),
 (W, 'if method != "POST":', 'if False:', "webhook: accept any HTTP method"),
 (W, 'if (declared_length or 0) > self.max_body or len(body) > self.max_body:', 'if False:', "webhook: no size limit"),
 (W, 'if self.allow_ips and remote_addr not in self.allow_ips:', 'if False:', "webhook: ignore the IP allowlist"),
 (W, 'if row and row[0] not in ("RECEIVED", "ERROR"):', 'if False:', "webhook: reprocess a finished delivery"),
 (W, 'if key in self._busy:', 'if False:', "webhook: process one payload on several threads at once"),
 (W, 'if txn.amount != req.amount:', 'if False:', "webhook: accept a success with the wrong amount"),
 (W, 'if not self.verify_success:\n            return None', 'if True:\n            return None', "webhook: never verify a success with Daraja"),
 (W, 'if asked is TxnStatus.SUCCESS:\n            return None', 'if True:\n            return None', "webhook: accept whatever Daraja's query says"),
 (W, 'return Outcome("ERROR", "Daraja\'s query does not confirm the success yet; will retry")', 'return None', "webhook: accept a success Daraja calls pending"),
 (W, 'if req is None:\n            return Outcome("QUARANTINED", "success callback for a request we never recorded")', 'if False:\n            pass', "webhook: accept a success for a request we never made"),
 (W, 'if result == "conflict":', 'if False:', "webhook: ingest a conflicting amount silently"),
 (W, 'except Exception as e:  # noqa: BLE001 - the row records it; the sender still gets its 200', 'except ZeroDivisionError as e:', "webhook: let a processing error escape to the sender"),
 (W, '>= MAX_ATTEMPTS:', '>= 10**9:', "webhook: retry a failing item forever"),
 (W, 'out[k] = mask_msisdn(v)\n            elif k in _NAME_KEYS:', 'out[k] = v\n            elif k in _NAME_KEYS:', "webhook: store phone numbers unmasked"),
 (W, 'out[k] = "<removed>"', 'out[k] = v', "webhook: store customer names"),
 (W, 'elif k == "Value" and obj.get("Name") in _PHONE_KEYS:', 'elif False:', "webhook: store the phone number inside STK metadata"),
 (W, 'if applied else Outcome("QUARANTINED", "matches no pending reversal")', 'if applied else Outcome("INGESTED", "matches no pending reversal")', "webhook: call an unmatched reversal result ingested"),
 (W, 'return Outcome("ERROR", "could not verify the success yet: Daraja rate-limited the query; will retry")', 'return None', "webhook: accept a success when rate-limited"),
 (D, '    def execute(self, sql: str, params: Sequence[Any] = ()) -> Result:\n        with self._lock:', '    def execute(self, sql: str, params: Sequence[Any] = ()) -> Result:\n        if True:', "db: run statements without the lock"),
 (C, '''verify_success=os.environ.get("PAYRECON_VERIFY_SUCCESS", "1") != "0"''', 'verify_success=True', "cli: ignore PAYRECON_VERIFY_SUCCESS=0"),
 (C, '''    if isinstance(gw, OfflineGateway):
        raise SystemExit("no Daraja credentials in the environment (set the DARAJA_* variables): the receiver needs''', '''    if False:
        raise SystemExit("no Daraja credentials in the environment (set the DARAJA_* variables): the receiver needs''', "cli: serve without credentials"),
 (C, "        sys.stdout.reconfigure(line_buffering=True)", "        pass  # (", "cli: leave serve output buffered"),
]


def run(root):
    runner = os.environ.get("PAYRECON_STANDIN_RUNNER")
    if runner:
        p = subprocess.run([sys.executable, runner, str(root)], capture_output=True, text=True, timeout=900)
        out = p.stdout + p.stderr
        fails = sorted({l.split("::")[1].split("[")[0] for l in out.splitlines() if l.startswith("FAIL ") and "::" in l})
        summ = [l for l in out.splitlines() if l.endswith("failed") and "passed" in l][-1:]
    else:
        env = {k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"} | {"PYTHONPATH": str(root / "src")}
        p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=no", *[str(root / "tests" / k) for k in KEEP]],
                           capture_output=True, text=True, timeout=900, cwd=root, env=env)
        out = p.stdout + p.stderr
        fails = sorted({l.split("::")[-1].split(" ")[0].split("[")[0] for l in out.splitlines() if l.startswith("FAILED ")})
        summ = [l for l in out.splitlines() if " passed" in l or " failed" in l][-1:]
    return (summ[0] if summ else "no summary"), fails, ("SyntaxError" in out)


caught = missed = 0
for f, old, new, label in M:
    root = pathlib.Path(tempfile.mkdtemp(prefix="payrecon-mut-"))
    shutil.rmtree(root)
    shutil.copytree(SRC, root, ignore=shutil.ignore_patterns("__pycache__", ".git", "*.db"))
    for t in (root / "tests").glob("test_*.py"):
        if t.name not in KEEP:
            t.unlink()
    path = root / f
    s = path.read_text()
    if s.count(old) != 1:
        print(f"SKIP  ({s.count(old)} anchors) {label}", flush=True)
        shutil.rmtree(root, ignore_errors=True)
        continue
    path.write_text(s.replace(old, new))
    summ, fails, err = run(root)
    shutil.rmtree(root, ignore_errors=True)
    if err:
        print(f"BAD   (syntax) {label}", flush=True)
        continue
    if fails or "no summary" in summ:
        caught += 1
        print(f"CAUGHT {label}  -> {fails[:2]}", flush=True)
    else:
        missed += 1
        print(f"MISSED {label}  [{summ}]", flush=True)
print(f"\ncaught {caught}, missed {missed}", flush=True)
sys.exit(1 if missed else 0)
