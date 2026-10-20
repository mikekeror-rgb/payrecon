"""Tunable limits. Defaults are conservative; every one is a blast-radius control."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..policy import MAX_RETRIES


@dataclass(frozen=True)
class RecoveryConfig:
    max_retries: int = MAX_RETRIES  # sends per invoice, counted from the durable store
    auto_retry_max_amount: Decimal = Decimal("10000")  # a retry above this needs a human
    retry_cooldown_s: float = 15 * 60  # minimum gap between two pushes for one invoice
    recheck_interval_s: float = 10 * 60  # one recheck proposal per bucket
    max_rechecks: int = 6  # then it is a person's problem
    approval_max_age_s: float = 72 * 3600  # an old approval is not a blank cheque
    stale_after_s: float = 5 * 60  # an EXECUTING action older than this lost its worker
    max_claims: int = 5  # transient failures before giving up
    backoff_s: float = 60.0
    max_side_effects_per_run: int = 25  # blast-radius cap for money-moving actions
    breaker_threshold: int = 3  # consecutive hard failures before the executor halts itself
    max_auto_apply_amount: Decimal | None = None  # optional ceiling on unattended apply
