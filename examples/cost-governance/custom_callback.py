"""LiteLLM custom callback wiring real proxy traffic into agent_sre's
CostGuard and CostAnomalyDetector, so any client hitting this proxy
(curl, a chat UI, another script) gets governed the same way the
test_ollama.py demo governs its own direct calls.

Also persists every event to a local SQLite DB (cost_events.db) and
re-reads governance parameters from a shared config table before each
request, so a separate Streamlit dashboard can display consumption and
adjust budget parameters on the live gateway without a proxy restart.
"""

import os
import sqlite3
import threading
import time

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
# These are just the defaults used the very first time cost_events.db is
# created -- after that, the Streamlit dashboard's saved config wins (see
# _reload_config_from_db below).
guard = CostGuard(
    per_task_limit=1.20,
    per_agent_daily_limit=2.00,
    org_monthly_budget=3.00,
    auto_throttle=True,
    kill_switch_threshold=0.95,
)
detector = CostAnomalyDetector()

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cost_events.db")
_db_lock = threading.Lock()


def _get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_db():
    with _db_lock, _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                agent_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                tokens INTEGER NOT NULL,
                cost REAL NOT NULL,
                status TEXT NOT NULL,
                spent_today REAL NOT NULL,
                util_pct REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                per_task_limit REAL NOT NULL,
                per_agent_daily_limit REAL NOT NULL,
                org_monthly_budget REAL NOT NULL,
                auto_throttle INTEGER NOT NULL,
                kill_switch_threshold REAL NOT NULL
            )
        """)
        row = conn.execute("SELECT id FROM config WHERE id = 1").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO config (id, per_task_limit, per_agent_daily_limit, "
                "org_monthly_budget, auto_throttle, kill_switch_threshold) "
                "VALUES (1, ?, ?, ?, ?, ?)",
                (guard.per_task_limit, guard.per_agent_daily_limit, guard.org_monthly_budget,
                 int(guard.auto_throttle), guard.kill_switch_threshold),
            )


def _reload_config_from_db():
    """Pick up whatever the Streamlit dashboard last saved and apply it to
    the live guard. Called before every request, so a parameter change takes
    effect on the very next call -- no proxy restart needed.
    """
    with _db_lock, _get_conn() as conn:
        row = conn.execute(
            "SELECT per_task_limit, per_agent_daily_limit, org_monthly_budget, "
            "auto_throttle, kill_switch_threshold FROM config WHERE id = 1"
        ).fetchone()
    if row is None:
        return
    per_task_limit, per_agent_daily_limit, org_monthly_budget, auto_throttle, kill_switch_threshold = row

    guard.per_task_limit = per_task_limit
    guard.org_monthly_budget = org_monthly_budget
    guard.auto_throttle = bool(auto_throttle)
    guard.kill_switch_threshold = kill_switch_threshold

    # per_agent_daily_limit is snapshotted onto each AgentBudget the moment
    # it's first created (see CostGuard._get_or_create_budget_locked), so an
    # agent already seen this session won't pick up a change on its own --
    # patch existing budgets directly when the value actually moves.
    if per_agent_daily_limit != guard.per_agent_daily_limit:
        guard.per_agent_daily_limit = per_agent_daily_limit
        with guard._lock:
            for budget in guard._budgets.values():
                budget.daily_limit_usd = per_agent_daily_limit


def _log_event(agent_id, task_id, tokens, cost, status, spent_today, util_pct):
    with _db_lock, _get_conn() as conn:
        conn.execute(
            "INSERT INTO events (timestamp, agent_id, task_id, tokens, cost, status, spent_today, util_pct) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), agent_id, task_id, tokens, cost, status, spent_today, util_pct),
        )


_init_db()


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
        _reload_config_from_db()

        agent_id = (data.get("user") if data else None) or "default-agent"
        allowed, reason = guard.check_task(agent_id, estimated_cost=PRECHECK_ESTIMATE_COST)
        if not allowed:
            print(f"[CostGuard] BLOCKED {agent_id}: {reason}")
            budget = guard.get_budget(agent_id)
            _log_event(agent_id, "blocked", 0, 0.0, "BLOCKED",
                       budget.spent_today_usd, budget.utilization_percent)
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

        _log_event(agent_id, task_id, total_tokens, cost, status,
                   budget.spent_today_usd, budget.utilization_percent)

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
