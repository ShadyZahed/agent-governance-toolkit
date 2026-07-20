from agent_sre.cost import CostGuard, CostAnomalyDetector
import requests

# Constants for token cost and Ollama API
TOKEN_COST = 0.001  # Cost per token in USD (informational display only)
OLLAMA_API_URL = "http://localhost:11434/api/chat"  # Replace with your Ollama endpoint
OLLAMA_MODEL = "gemma4:latest"  # Must match a model from `ollama list`

# Fixed per-task cost fed to CostGuard for the budget decision. Real Ollama
# token costs vary with response length, so a flat charge is used here to
# deterministically demonstrate the daily limit tripping on the 3rd prompt
# (same technique as the original demo's flat $1.80/task).
BUDGET_COST_PER_TASK = 0.05


def query_ollama(prompt):
    """Send a prompt to the Ollama model and return the response and token usage."""
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "stream": False,
    }
    response = requests.post(OLLAMA_API_URL, json=payload)
    response.raise_for_status()
    data = response.json()
    response_text = data["message"]["content"]
    tokens_used = data.get("eval_count", 0) + data.get("prompt_eval_count", 0)
    return response_text, tokens_used


def main():
    print("=" * 60)
    print("  Ollama Token Consumption Tracker")
    print("=" * 60)

    # ---------------------------------------------------------------
    # 1. Budget Setup
    # ---------------------------------------------------------------
    print("\n--- Budget Setup ---")
    guard = CostGuard(
        per_task_limit=2.00,  # Max cost per request
        per_agent_daily_limit=0.12,  # Deliberately low: 2 tasks fit, 3rd is blocked
        org_monthly_budget=100.00,  # Max monthly cost for the organization
        auto_throttle=True,
        kill_switch_threshold=0.95,
    )
    print(f"  Per-task limit:    ${guard.per_task_limit:.2f}")
    print(f"  Daily agent limit: ${guard.per_agent_daily_limit:.2f}")
    print(f"  Org monthly:       ${guard.org_monthly_budget:.2f}")

    # ---------------------------------------------------------------
    # 2. Track Token Consumption (hits the daily limit on prompt 3)
    # ---------------------------------------------------------------
    print("\n--- Tracking Token Consumption ---")
    prompts = [
        "What is the capital of France?",
        "Explain the theory of relativity.",
        "Generate a summary of the latest AI trends.",
    ]

    rows = []
    for i, prompt in enumerate(prompts):
        task_id = f"task-{i + 1:03d}"

        allowed, reason = guard.check_task("ollama-agent", estimated_cost=BUDGET_COST_PER_TASK)
        if not allowed:
            rows.append({
                "task": task_id,
                "prompt": prompt if len(prompt) <= 32 else prompt[:29] + "...",
                "tokens": "-",
                "cost": "-",
                "spent_today": guard.get_budget("ollama-agent").spent_today_usd,
                "util_pct": guard.get_budget("ollama-agent").utilization_percent,
                "status": "BLOCKED",
                "response": f"(skipped — {reason})",
            })
            continue

        response, tokens_used = query_ollama(prompt)
        real_cost = tokens_used * TOKEN_COST

        alerts = guard.record_cost("ollama-agent", task_id, cost_usd=BUDGET_COST_PER_TASK)
        budget = guard.get_budget("ollama-agent")

        if alerts:
            alert = alerts[0]
            action_str = f"[{alert.action.value}]" if alert.action.value != "alert" else ""
            status = f"{alert.severity.value.upper()} {action_str}".strip()
        else:
            status = "OK"

        rows.append({
            "task": task_id,
            "prompt": prompt if len(prompt) <= 32 else prompt[:29] + "...",
            "tokens": tokens_used,
            "cost": real_cost,
            "spent_today": budget.spent_today_usd,
            "util_pct": budget.utilization_percent,
            "status": status,
            "response": response,
        })

    col_widths = {"task": 9, "prompt": 32, "tokens": 8, "cost": 9, "spent_today": 12, "util_pct": 8, "status": 14}
    header = (f"{'Task':<{col_widths['task']}} {'Prompt':<{col_widths['prompt']}} "
              f"{'Tokens':>{col_widths['tokens']}} {'Cost($)':>{col_widths['cost']}} "
              f"{'Spent($)':>{col_widths['spent_today']}} {'Util(%)':>{col_widths['util_pct']}} "
              f"{'Status':<{col_widths['status']}}")
    print(header)
    print("-" * len(header))
    for row in rows:
        cost_str = f"{row['cost']:.4f}" if isinstance(row["cost"], float) else row["cost"]
        tokens_str = str(row["tokens"])
        print(f"{row['task']:<{col_widths['task']}} {row['prompt']:<{col_widths['prompt']}} "
              f"{tokens_str:>{col_widths['tokens']}} {cost_str:>{col_widths['cost']}} "
              f"{row['spent_today']:>{col_widths['spent_today']}.2f} {row['util_pct']:>{col_widths['util_pct']}.1f} "
              f"{row['status']:<{col_widths['status']}}")

    print("\n--- Responses ---")
    for row in rows:
        print(f"\n[{row['task']}] {row['prompt']}\n  {row['response']}")

    # ---------------------------------------------------------------
    # 3. Organization Budget
    # ---------------------------------------------------------------
    print("\n--- Organization Budget ---")
    org_guard = CostGuard(
        per_agent_daily_limit=50.00,
        org_monthly_budget=100.00,
        kill_switch_threshold=0.95,
    )

    org_rows = []
    for agent in ["agent-a", "agent-b"]:
        alerts = org_guard.record_cost(agent, "task-1", cost_usd=48.00)
        budget = org_guard.get_budget(agent)
        org_alerts = [a.message for a in alerts if "org" in a.message.lower()]
        org_rows.append({
            "agent": agent,
            "spent_today": budget.spent_today_usd,
            "alert": org_alerts[0] if org_alerts else "-",
        })

    print(f"{'Agent':<10} {'Spent($)':>10}  {'Org Alert'}")
    print("-" * 60)
    for r in org_rows:
        print(f"{r['agent']:<10} {r['spent_today']:>10.2f}  {r['alert']}")

    print(f"\n{'Agent':<10} {'Next Task Status'}")
    print("-" * 30)
    for agent in ["agent-a", "agent-b", "agent-c"]:
        allowed, reason = org_guard.check_task(agent, estimated_cost=0.01)
        status = "ALLOWED" if allowed else "BLOCKED"
        print(f"{agent:<10} {status} ({reason})")

    # ---------------------------------------------------------------
    # 4. Anomaly Detection
    # ---------------------------------------------------------------
    print("\n--- Cost Anomaly Detection ---")
    detector = CostAnomalyDetector()

    # Feed normal token usage
    for i in range(20):
        detector.ingest(1.0 + (i % 3) * 0.2, agent_id="ollama-agent")

    # Check an anomalous token usage
    result = detector.ingest(50.0, agent_id="ollama-agent")

    print(f"{'Metric':<20} {'Value':<20}")
    print("-" * 40)
    print(f"{'Normal cost range':<20} {'$1.00 - $1.40':<20}")
    print(f"{'Checked cost':<20} {'$50.00':<20}")
    print(f"{'Anomaly detected':<20} {str(bool(result)):<20}")
    if result:
        print(f"{'Severity':<20} {result.severity.value:<20}")

    print(f"\n{'=' * 60}")
    print("  Demo complete!")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()