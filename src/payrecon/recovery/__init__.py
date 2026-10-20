"""Safe recovery execution: idempotent, guarded, approval-gated, audited."""
from .approvals import ApprovalDesk, ApprovalError, Role
from .config import RecoveryConfig
from .executor import Executor, RunSummary
from .guards import Guards, GuardResult, Verdict
from .models import ActionStatus, ActionType, Escalation, RecoveryAction
from .planner import PlanResult, plan, plan_from_data
from .store import ActionStore

__all__ = [
    "ActionStatus", "ActionStore", "ActionType", "ApprovalDesk", "ApprovalError", "Escalation", "Executor",
    "GuardResult", "Guards", "PlanResult", "RecoveryAction", "RecoveryConfig", "Role", "RunSummary", "Verdict",
    "plan", "plan_from_data",
]
