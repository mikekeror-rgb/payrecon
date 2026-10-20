from payrecon.evaluate import evaluate, main
from payrecon.matcher import reconcile
from payrecon.models import Action, Category
from payrecon.synthetic import SCENARIOS, generate


def test_generator_is_deterministic():
    a, b = generate(200, seed=3), generate(200, seed=3)
    assert a.invoices == b.invoices and a.txns == b.txns and a.truth == b.truth


def test_different_seeds_differ():
    assert generate(200, seed=1).txns != generate(200, seed=2).txns


def test_every_scenario_is_covered():
    ds = generate(500, seed=7)
    assert set(ds.scenario.values()) == {name for name, _ in SCENARIOS}


def test_no_real_phone_numbers_are_generated():
    ds = generate(200, seed=7)
    assert all(t.msisdn_masked and "****" in t.msisdn_masked for t in ds.txns)


def test_engine_never_proposes_an_unsafe_automatic_action():
    for seed in range(5):
        ds = generate(400, seed=seed)
        report = reconcile(ds.invoices, ds.txns)
        for iid, r in report.invoices.items():
            if r.action is Action.AUTO_APPLY:
                assert ds.truth[iid] is Category.UNRECORDED_PAYMENT, (seed, iid)
            if r.action is Action.RETRY_ELIGIBLE:
                assert ds.truth[iid] is Category.FAILED, (seed, iid)


def test_baseline_results_and_its_known_blind_spot():
    ds = generate(500, seed=7)
    res = evaluate(ds, reconcile(ds.invoices, ds.txns))

    # Every unlinkable transaction is surfaced for review, and nothing else is.
    assert res["orphans"]["precision"] == 1.0 and res["orphans"]["recall"] == 1.0
    # Redelivered callbacks and messy references must not be misread.
    for scenario in ("clean", "messy_reference", "redelivered_callback", "duplicate", "delayed", "failed",
                     "amount_mismatch", "partial_failure", "missing", "callback_lost"):
        assert res["scenario_accuracy"][scenario] == 1.0, scenario
    # Documents the deterministic baseline's limit: payments with an unusable reference.
    # A fuzzy / AI matcher should raise this; update this assertion when it does.
    assert res["scenario_accuracy"]["missing_reference"] == 0.0
    assert res["safety"]["unsafe_total"] == 0


def test_cli_exit_code_is_zero_when_safe():
    assert main(["--n", "200", "--seed", "1"]) == 0
