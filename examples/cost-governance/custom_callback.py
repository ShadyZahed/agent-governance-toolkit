"""LiteLLM custom callback wiring real proxy traffic into agent_sre's
CostGuard and CostAnomalyDetector, so any client hitting this proxy
(curl, a chat UI, another script) gets governed the same way the
test_ollama.py demo governs its own direct calls.
"""

from fastapi import HTTPException

from litellm.integrations.custom_logger import CustomLogger
from agent_sre.cost import CostGuard, CostAnomalyDetector

TOKEN_COST = 0.0025  # USD per token, matches test_ollama.py

# Rough conservative estimate used only for the pre-call budget gate, since
# real token usage isn't known until Ollama actually responds. The real cost
# recorded after the call still uses tokens_used * TOKEN_COST.
PRECHECK_ESTIMATE_COST = 0.15

# Shared across every request that passes through this proxy process --
# including org_monthly_budget below, which CostGuard aggregates across
# every agent_id passed to record_cost/check_task on this same instance.
# Lowered from 100.00 so it's actually reachable in a demo: run curl from
# two terminals with different "user" values (e.g. "agent-a" / "agent-b")
# and both draw down the *same* org-wide cap, visible from either terminal.
guard = CostGuard(
    per_task_limit=1.20,
    per_agent_daily_limit=2.00,
    org_monthly_budget=3.00,
    auto_throttle=True,
    kill_switch_threshold=0.95,
)
detector = CostAnomalyDetector()


def _get_usage(response_obj):
    if isinstance(response_obj, dict):
        return response_obj.get("usage")
    return getattr(response_obj, "usage", None)


def _get_total_tokens(usage):
    if isinstance(usage, dict):
        return usage.get("total_tokens", 0)
    return getattr(usage, "total_tokens", 0)


def _get_response_id(response_obj):
    if isinstance(response_obj, dict):
        return response_obj.get("id", "unknown-task")
    return getattr(response_obj, "id", "unknown-task")


# Same column layout as the table in test_ollama_claude.py, so the proxy's
# live output reads the same way the standalone demo's summary table does.
COL_WIDTHS = {"agent": 14, "task": 24, "tokens": 8, "cost": 9, "spent_today": 12, "util_pct": 8, "status": 14}


def _print_table_header():
    header = (f"{'Agent':<{COL_WIDTHS['agent']}} {'Task':<{COL_WIDTHS['task']}} "
              f"{'Tokens':>{COL_WIDTHS['tokens']}} {'Cost($)':>{COL_WIDTHS['cost']}} "
              f"{'Spent($)':>{COL_WIDTHS['spent_today']}} {'Util(%)':>{COL_WIDTHS['util_pct']}} "
              f"{'Status':<{COL_WIDTHS['status']}}")
    print(header)
    print("-" * len(header))


# Printed lazily before the first real row (not at import time), so it
# doesn't get buried under LiteLLM's own startup banner/logs.
_header_printed = False


class CostGovernanceLogger(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        """Runs before Ollama is ever called. Blocks the request outright if
        CostGuard says the agent (or the shared org budget) is out of room.
        """
        agent_id = (data.get("user") if data else None) or "default-agent"
        allowed, reason = guard.check_task(agent_id, estimated_cost=PRECHECK_ESTIMATE_COST)
        if not allowed:
            print(f"[CostGuard] BLOCKED {agent_id}: {reason}")
            raise HTTPException(status_code=429, detail=f"Blocked by CostGuard: {reason}")
        return data

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        global _header_printed
        if not _header_printed:
            _print_table_header()
            _header_printed = True

        usage = _get_usage(response_obj)
        if not usage:
            return

        total_tokens = _get_total_tokens(usage)
        cost = total_tokens * TOKEN_COST

        # Pass "user": "<agent-name>" in the request body to attribute spend
        # to a specific agent; requests without it fall under "default-agent".
        agent_id = kwargs.get("user") or "default-agent"
        task_id = _get_response_id(response_obj)
        task_display = task_id if len(task_id) <= COL_WIDTHS["task"] else task_id[:COL_WIDTHS["task"] - 3] + "..."

        alerts = guard.record_cost(agent_id, task_id, cost_usd=cost)
        budget = guard.get_budget(agent_id)

        if alerts:
            alert = alerts[0]
            action_str = f"[{alert.action.value}]" if alert.action.value != "alert" else ""
            status = f"{alert.severity.value.upper()} {action_str}".strip()
        else:
            status = "OK"

        print(f"{agent_id:<{COL_WIDTHS['agent']}} {task_display:<{COL_WIDTHS['task']}} "
              f"{total_tokens:>{COL_WIDTHS['tokens']}} {cost:>{COL_WIDTHS['cost']}.4f} "
              f"{budget.spent_today_usd:>{COL_WIDTHS['spent_today']}.2f} "
              f"{budget.utilization_percent:>{COL_WIDTHS['util_pct']}.1f} "
              f"{status:<{COL_WIDTHS['status']}}")

        for alert in alerts:
            action_str = f" [{alert.action.value}]" if alert.action.value != "alert" else ""
            print(f"  -> [CostGuard] ALERT [{alert.severity.value.upper()}]{action_str} {alert.message}")

        # Org-wide totals: shared across every agent_id that hits this same
        # guard instance, so this reflects combined spend from ALL terminals
        # talking to this proxy, not just the one that made this request.
        print(f"  -> [Org] spent_month=${guard.org_spent_month:.2f} "
              f"remaining=${guard.org_remaining_month:.2f} "
              f"of ${guard.org_monthly_budget:.2f}")

        anomaly = detector.ingest(cost, agent_id=agent_id)
        if anomaly:
            print(f"  -> [AnomalyDetector] Anomalous cost: ${cost:.4f} "
                  f"(severity={anomaly.severity.value})")


# LiteLLM's config resolves callbacks to an already-instantiated object.
proxy_handler_instance = CostGovernanceLogger()
