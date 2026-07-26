# Cost Governance Example

Demonstrates AGT's cost governance: tiered budget enforcement, per-agent and
organization-wide caps, auto-throttle, kill switches, and anomaly detection.

## Prerequisites

- Python 3.10+
- No API keys required

```bash
pip install agent-sre
```

## How to Run

```bash
python examples/cost-governance/cost_governance_demo.py
```

## Expected Output

```
  Budget Setup: per-task $2.00, daily $20.00, org $100.00
  Pre-task checks: $1.50 allowed, $5.00 blocked (over limit)
  Alert escalation: WARNING at 50%, THROTTLE at 85%, KILL at 95%
  Org budget: cross-agent spending tracked, kill switch applied
  Anomaly detection: $50.00 flagged against $1.00-$1.40 baseline
```

## What This Demo Shows

1. **Budget Setup**: Per-task, per-agent, and org-wide limits
2. **Pre-Task Checks**: Validate cost before execution
3. **Alert Escalation**: Warnings at 50/75/90%, throttle at 85%, kill at 95%
4. **Organization Budget**: Global cap across all agents
5. **Anomaly Detection**: Flag unusual spending patterns

## Learn More

- [Tutorial 51: Cost Governance](../../docs/tutorials/51-cost-governance.md)
- [ADR-0012: Cost Governance](../../docs/adr/0012-cost-governance-observability-policies.md)
- [API: cost/guard.py](../../agent-governance-python/agent-sre/src/agent_sre/cost/guard.py)

---

## Live Ollama + LiteLLM Gateway Demo

The demo above (`cost_governance_demo.py`) exercises `CostGuard` /
`CostAnomalyDetector` with hardcoded, synthetic costs — there's no real LLM
call anywhere in it. This section adds a **live** version: a real local
Ollama model, sitting behind a LiteLLM gateway, with the same governance
primitives wired in as actual middleware that runs on every real request.

### Files

| File | Role |
|---|---|
| `test_ollama_claude.py` | Standalone script. Calls Ollama directly, tracks cost with its own local `CostGuard`/`CostAnomalyDetector`. **Not connected to the gateway** — running this never touches LiteLLM or `custom_callback.py`. |
| `litellm_config.yaml` | Tells LiteLLM which Ollama model to expose (`gemma4` → `ollama/gemma4:latest` at `http://localhost:11434`), and registers `custom_callback.py` as the proxy's callback handler. |
| `custom_callback.py` | The actual governance middleware. Runs inside the LiteLLM proxy process and fires on every real request/response that passes through it, regardless of which client sent it. |

### Prerequisites

- Ollama running locally with a model pulled (`ollama list` to check; `ollama pull <model>` if needed).
- A Python ≥3.10 interpreter for LiteLLM's proxy — the machine's default `python3` may be too old (3.9), so this was run under `pyenv`'s `python3.12`:
  ```bash
  /Users/shady/.pyenv/shims/python3.12 -m pip install 'litellm[proxy]'
  ```
- `agent_sre` importable under that same interpreter (`python3.12 -c "import agent_sre"` to confirm).

### Running it

From the repo root:
```bash
litellm --config examples/cost-governance/litellm_config.yaml --port 8000
```
Leave that terminal running. In a separate terminal, send a request through the gateway (not directly to Ollama's `11434`):
```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "gemma4", "messages": [{"role": "user", "content": "What is the capital of France?"}]}'
```
The server terminal should print an aligned table row for the request (Agent / Task / Tokens / Cost / Spent / Util / Status), plus any alert/org/anomaly lines underneath it.

To stop the proxy: `Ctrl+C` in that terminal. If restarting hits `address already in use`, something's still bound to port 8000 — find and stop it first:
```bash
lsof -nP -iTCP:8000 -sTCP:LISTEN
kill <PID>
```

**Note:** all state (`CostGuard`'s budgets, org spend, cost history; `CostAnomalyDetector`'s samples) lives in memory inside the running proxy process. Restarting the proxy resets everything back to zero.

### What `litellm_config.yaml` does

```yaml
model_list:
  - model_name: gemma4
    litellm_params:
      model: ollama/gemma4:latest
      api_base: http://localhost:11434

litellm_settings:
  callbacks: custom_callback.proxy_handler_instance
```
- `model_list` maps the friendly name clients request (`"model": "gemma4"`) to the real Ollama model tag and where Ollama is actually running.
- `litellm_settings.callbacks` points LiteLLM at the already-instantiated `CostGovernanceLogger` object in `custom_callback.py`, so it gets invoked for every request that flows through this proxy.

### What `custom_callback.py` does

A single `CostGuard` instance (`per_task_limit=1.20`, `per_agent_daily_limit=2.00`, `org_monthly_budget=3.00`, `kill_switch_threshold=0.95`) and one `CostAnomalyDetector` are created **once**, at module import, and shared across every request the proxy handles for as long as it stays up.

**`async_pre_call_hook`** — runs *before* Ollama is contacted:
- Reads `agent_id` from the request's `"user"` field (defaults to `"default-agent"` if not set).
- Calls `guard.check_task(agent_id, estimated_cost=0.15)` — a real `CostGuard` method that checks per-task, per-agent-daily, *and* the shared org-wide budget in one call.
- If not allowed, raises `HTTPException(429, ...)` — the request is rejected outright and Ollama is never called. This is the actual blocking mechanism; the estimate (`0.15`) is a rough stand-in since real token cost isn't known until after the model responds.

**`async_log_success_event`** — runs *after* Ollama responds successfully:
- Pulls `total_tokens` from the response's `usage` block and computes real cost (`tokens * TOKEN_COST`).
- Calls `guard.record_cost(agent_id, task_id, cost_usd=cost)` to actually charge that cost, and prints one aligned table row per request.
- Prints any `CostAlert`s CostGuard returns (threshold warnings, throttle, kill).
- Prints the **org-wide** running total (`guard.org_spent_month` / `org_remaining_month` / `org_monthly_budget`) — this is shared across every `agent_id` that hits this same guard instance, so it reflects combined spend from *all* clients talking to the proxy, not just the current request's agent.
- Feeds the cost into `detector.ingest(cost, agent_id=agent_id)` and prints an `[AnomalyDetector]` line if it's flagged as an outlier.

### Demonstrating specific behaviors

**Per-agent/org blocking:** send enough requests that `per_agent_daily_limit` ($2.00) or the shared `org_monthly_budget` ($3.00) is exceeded — subsequent requests get rejected with an HTTP 429 before Ollama is ever called, and `[CostGuard] BLOCKED ...` prints server-side.

**Org budget across multiple "agents":** run curl from two terminals with different `"user"` values so they're tracked as separate agents, but both draw down the *same* org cap:
```bash
curl ... -d '{"model": "gemma4", "user": "agent-a", "messages": [...]}'
curl ... -d '{"model": "gemma4", "user": "agent-b", "messages": [...]}'
```
Watch the `[Org] spent_month=...` line climb from *either* terminal's requests — and once it crosses the kill threshold, both agents get blocked, even one that never spent much itself.

**Anomaly detection:** `CostAnomalyDetector.ingest()` requires at least 10 samples before it evaluates anything (returns `None` unconditionally before that). In one continuous run (no proxy restart in between, since history resets on restart), send ~10 similar cheap prompts to build a baseline, then one prompt engineered to produce a much longer response:
```bash
for i in $(seq 1 10); do
  curl -s http://localhost:8000/v1/chat/completions -H "Content-Type: application/json" \
    -d '{"model": "gemma4", "messages": [{"role": "user", "content": "What is the capital of France?"}]}' > /dev/null
done

curl http://localhost:8000/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model": "gemma4", "messages": [{"role": "user", "content": "Write a very long, detailed 800-word essay about the history of the Roman Empire."}]}'
```

### Known limitations

- **`CostGuard`'s own runtime state (spend counters, throttle/kill flags) is in-memory only** and resets on every proxy restart. Events and governance *parameters* now persist separately in `cost_events.db` (see below), but the live guard's cumulative spend does not.
- **Pre-check isn't atomic with the real charge.** `check_task` (pre-call, estimated cost) and `record_cost` (post-call, real cost) are two separate calls, so under concurrent requests it's possible to slightly overshoot a limit. `CostGuard` has an atomic `check_and_charge` method for budget-critical paths, but it needs the final cost known up front — not used here since real token cost is only known after Ollama responds.
- **`TOKEN_COST`/token is an arbitrary simulated rate** — Ollama itself is free/local; this exists purely to give the demo a non-zero dollar cost to govern against.

## Streamlit Dashboard

`streamlit_app.py` is a live dashboard for the gateway above: overall and
per-agent token/cost consumption, an org-wide budget bar, a consumption-over-
time chart, and a settings panel that adjusts `CostGuard`'s parameters on the
*running* gateway without a restart.

### How it connects to the gateway

`custom_callback.py` writes one row per request to a local SQLite DB
(`cost_events.db`) and reads a `config` table from that same DB before every
request. The dashboard reads `events` to render its charts, and writes to
`config` when you submit the sidebar form — the gateway picks up the new
values on its very next request. The two processes never talk directly to
each other; the DB file is the entire integration surface.

### Running it

```bash
pip install streamlit plotly pandas   # or: pip install -r examples/cost-governance/requirements.txt
streamlit run examples/cost-governance/streamlit_app.py
```

Open the URL Streamlit prints (defaults to `http://localhost:8501`). The
dashboard auto-refreshes its data every 5 seconds; the sidebar's parameter
form is outside that auto-refresh loop so it won't reset mid-edit.

### Notes

- `per_task_limit`, `org_monthly_budget`, `auto_throttle`, and
  `kill_switch_threshold` changes apply to *all* agents immediately.
  `per_agent_daily_limit` changes are also propagated to agents already seen
  this session (patched directly onto their existing budget), not just new
  ones — see the comment in `_reload_config_from_db`.
- `cost_events.db` (and its `-shm`/`-wal` WAL-mode sidecar files) are
  local runtime data, not committed to the repo.
