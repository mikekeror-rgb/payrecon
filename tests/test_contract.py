"""The cases payrecon and payroll-console (the TypeScript approval console) must both pass, so the console shows
exactly what payrecon would decide. The same file is in payroll-console/contract/; keep the two copies identical."""
import json
from pathlib import Path

import pytest

from payrecon.adapters import SQLITE_DDL, Database
from payrecon.adapters.security import PiiBox
from payrecon.payroll import PayrollService, check_csv, names_match

CASES = json.loads((Path(__file__).parent / "contract" / "payroll-cases.json").read_text())


@pytest.mark.parametrize("case", CASES["csv"], ids=[c["id"] for c in CASES["csv"]])
def test_the_file_checks_agree(case):
    c = check_csv(case["csv"])
    assert c.errors == case["errors"]
    if not case["errors"]:
        assert [ln.phone for ln in c.lines] == case["phones"] and c.total == case["total"]


@pytest.mark.parametrize("case", CASES["review"], ids=[c["id"] for c in CASES["review"]])
def test_the_number_checks_agree(case):
    db = Database.sqlite()
    db.script(SQLITE_DDL)
    s = PayrollService(db, None, PiiBox(None, allow_plaintext=True))
    hist = case["history"]
    if hist:  # the history, as payments that went through
        rows = "\n".join(f"{h['name']},{h['phone']},100" for h in hist)
        assert not s.create_batch(hist[0]["batch"], "name,phone,amount\n" + rows + "\n", "mike").errors
        db.execute("UPDATE payouts SET status = 'PAID', receipt = payout_id")
    assert not s.create_batch("NOW", case["csv"], "mike").errors
    found = s.review("NOW")
    assert {f["name"]: f["flag"] for f in found} == case["flags"]
    for name, why in case.get("why", {}).items():
        assert [f["why"] for f in found if f["name"] == name] == [why]


@pytest.mark.parametrize("listed, registered, same", CASES["names"])
def test_the_name_checks_agree(listed, registered, same):
    assert names_match(listed, registered) is same
