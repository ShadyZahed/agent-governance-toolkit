"""Cost governance dashboard for the live Ollama + LiteLLM gateway.

Reads the same SQLite DB that custom_callback.py writes to (cost_events.db),
and writes governance parameter changes back into its config table, which
the gateway re-reads before every request (see _reload_config_from_db in
custom_callback.py) -- so changes made here take effect on the live gateway
without a proxy restart.

Run with: streamlit run examples/cost-governance/streamlit_app.py
"""

import os
import sqlite3
import time

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cost_events.db")

# -- Validated palette (see dataviz skill validate_palette.js) --------------
# Brand color is reserved for chrome/identity only, never reused for status.
BRAND_YELLOW = "#FFE600"
INK = "#1A1A1A"
MUTED = "#6B6B6B"
SURFACE = "#FFFFFF"
SURFACE_ALT = "#F7F7F5"

# Status palette -- validated: Lightness band PASS, Chroma floor PASS,
# CVD separation PASS (all-pairs), Contrast vs surface PASS.
STATUS_COLORS = {
    "OK": "#15803D",
    "WARNING": "#C2760C",
    "THROTTLE": "#C2760C",
    "CRITICAL": "#C0272D",
    "KILL": "#C0272D",
    "BLOCKED": "#C0272D",
}

# Per-agent categorical palette -- validated separately: all four checks PASS
# (all-pairs CVD separation, since any two agents may appear side by side).
AGENT_COLORS = ["#2563EB", "#0D9488", "#BE185D", "#92400E"]


# ── data layer ───────────────────────────────────────────────────────────────

def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def ensure_db():
    with get_conn() as conn:
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
                "VALUES (1, 1.20, 2.00, 3.00, 1, 0.95)"
            )
        conn.commit()


def load_events() -> pd.DataFrame:
    with get_conn() as conn:
        df = pd.read_sql_query("SELECT * FROM events ORDER BY timestamp ASC", conn)
    if not df.empty:
        df["time"] = pd.to_datetime(df["timestamp"], unit="s")
    return df


def load_config() -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT per_task_limit, per_agent_daily_limit, org_monthly_budget, "
            "auto_throttle, kill_switch_threshold FROM config WHERE id = 1"
        ).fetchone()
    keys = ["per_task_limit", "per_agent_daily_limit", "org_monthly_budget",
             "auto_throttle", "kill_switch_threshold"]
    return dict(zip(keys, row))


def save_config(per_task_limit, per_agent_daily_limit, org_monthly_budget,
                auto_throttle, kill_switch_threshold):
    with get_conn() as conn:
        conn.execute(
            "UPDATE config SET per_task_limit=?, per_agent_daily_limit=?, "
            "org_monthly_budget=?, auto_throttle=?, kill_switch_threshold=? WHERE id=1",
            (per_task_limit, per_agent_daily_limit, org_monthly_budget,
             int(auto_throttle), kill_switch_threshold),
        )
        conn.commit()


# ── styling ──────────────────────────────────────────────────────────────────

def inject_css():
    st.markdown(f"""
    <style>
        .block-container {{ padding-top: 1.5rem; max-width: 1200px; }}

        .brand-bar {{
            background: {INK};
            border-top: 6px solid {BRAND_YELLOW};
            padding: 1.1rem 1.5rem;
            border-radius: 10px;
            margin-bottom: 1.4rem;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }}
        .brand-bar h1 {{
            color: #FFFFFF;
            font-size: 1.4rem;
            font-weight: 700;
            margin: 0;
            letter-spacing: 0.01em;
        }}
        .brand-bar span {{
            color: {BRAND_YELLOW};
            font-weight: 700;
        }}
        .brand-tag {{
            background: {BRAND_YELLOW};
            color: {INK};
            font-size: 0.75rem;
            font-weight: 700;
            padding: 0.25rem 0.6rem;
            border-radius: 6px;
        }}

        .kpi-card {{
            background: {SURFACE};
            border: 1px solid #E8E8E5;
            border-top: 4px solid {BRAND_YELLOW};
            border-radius: 12px;
            padding: 1rem 1.1rem;
            box-shadow: 0 1px 3px rgba(0,0,0,0.04);
        }}
        .kpi-label {{
            color: {MUTED};
            font-size: 0.78rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.04em;
            margin-bottom: 0.3rem;
        }}
        .kpi-value {{
            color: {INK};
            font-size: 1.9rem;
            font-weight: 700;
            line-height: 1.1;
        }}
        .kpi-sub {{
            color: {MUTED};
            font-size: 0.8rem;
            margin-top: 0.2rem;
        }}

        .progress-track {{
            background: {SURFACE_ALT};
            border-radius: 8px;
            height: 14px;
            width: 100%;
            overflow: hidden;
            border: 1px solid #E8E8E5;
        }}
        .progress-fill {{
            height: 100%;
            border-radius: 8px 0 0 8px;
        }}

        section[data-testid="stSidebar"] {{
            background: {SURFACE_ALT};
            border-right: 1px solid #E8E8E5;
        }}
        .stButton>button {{
            color: {INK} !important;
            font-weight: 600 !important;
        }}

        /* Segmented-control look for the view switcher (st.radio) --
           st.tabs' active index isn't session_state-backed, so it silently
           resets to the first tab on every fragment auto-refresh. A keyed
           radio persists correctly across those reruns instead. */
        div[role="radiogroup"] {{
            background: {SURFACE_ALT};
            border: 1px solid #E8E8E5;
            border-radius: 10px;
            padding: 4px;
            gap: 4px;
        }}
        div[role="radiogroup"] label {{
            background: transparent;
            border-radius: 7px;
            padding: 0.3rem 0.8rem;
            margin: 0 !important;
        }}
        div[role="radiogroup"] label:has(input:checked) {{
            background: {SURFACE};
            box-shadow: 0 1px 2px rgba(0,0,0,0.08);
        }}
        div[role="radiogroup"] input {{
            accent-color: {BRAND_YELLOW};
        }}

        .status-pill {{
            display: inline-block;
            padding: 0.15rem 0.55rem;
            border-radius: 999px;
            font-size: 0.75rem;
            font-weight: 700;
            color: white;
        }}
    </style>
    """, unsafe_allow_html=True)


def status_color(status: str) -> str:
    for key, color in STATUS_COLORS.items():
        if key in status.upper():
            return color
    return STATUS_COLORS["OK"]


def kpi_card(label, value, sub=""):
    st.markdown(f"""
    <div class="kpi-card">
        <div class="kpi-label">{label}</div>
        <div class="kpi-value">{value}</div>
        <div class="kpi-sub">{sub}</div>
    </div>
    """, unsafe_allow_html=True)


def progress_bar(pct: float):
    pct_clamped = max(0.0, min(100.0, pct))
    color = STATUS_COLORS["OK"] if pct_clamped < 70 else STATUS_COLORS["WARNING"] if pct_clamped < 90 else STATUS_COLORS["CRITICAL"]
    st.markdown(f"""
    <div class="progress-track">
        <div class="progress-fill" style="width:{pct_clamped}%; background:{color};"></div>
    </div>
    """, unsafe_allow_html=True)


# ── page setup ───────────────────────────────────────────────────────────────

st.set_page_config(page_title="Cost Governance Dashboard", layout="wide", page_icon="◆")
inject_css()
ensure_db()

st.markdown(f"""
<div class="brand-bar">
    <h1>Cost Governance <span>Dashboard</span></h1>
    <div class="brand-tag">LIVE GATEWAY</div>
</div>
""", unsafe_allow_html=True)


# ── sidebar: governance parameters ──────────────────────────────────────────

with st.sidebar:
    st.markdown("### Governance Parameters")
    st.caption("Changes apply to the live gateway on its next request -- no restart needed.")

    cfg = load_config()

    with st.form("params_form"):
        per_task_limit = st.number_input(
            "Per-task limit ($)", min_value=0.01, max_value=1000.0,
            value=float(cfg["per_task_limit"]), step=0.10, format="%.2f",
        )
        per_agent_daily_limit = st.number_input(
            "Per-agent daily limit ($)", min_value=0.01, max_value=1000.0,
            value=float(cfg["per_agent_daily_limit"]), step=0.10, format="%.2f",
        )
        org_monthly_budget = st.number_input(
            "Org monthly budget ($)", min_value=0.01, max_value=100000.0,
            value=float(cfg["org_monthly_budget"]), step=0.50, format="%.2f",
        )
        kill_switch_threshold = st.slider(
            "Kill switch threshold", min_value=0.50, max_value=1.00,
            value=float(cfg["kill_switch_threshold"]), step=0.01,
        )
        auto_throttle = st.toggle("Auto-throttle enabled", value=bool(cfg["auto_throttle"]))

        submitted = st.form_submit_button("Apply to live gateway", use_container_width=True)
        if submitted:
            save_config(per_task_limit, per_agent_daily_limit, org_monthly_budget,
                        auto_throttle, kill_switch_threshold)
            st.success("Saved -- takes effect on the next gateway request.")

    st.divider()
    st.caption(f"DB: `{os.path.basename(DB_PATH)}`")


# ── main dashboard (auto-refreshing) ────────────────────────────────────────

@st.fragment(run_every="5s")
def dashboard():
    df = load_events()
    cfg = load_config()

    if df.empty:
        st.info("No requests have gone through the gateway yet. Send a curl request to see data here.")
        return

    completed = df[df["status"] != "BLOCKED"]
    blocked = df[df["status"] == "BLOCKED"]

    total_tokens = int(completed["tokens"].sum())
    total_cost = float(completed["cost"].sum())
    org_pct = (total_cost / cfg["org_monthly_budget"] * 100) if cfg["org_monthly_budget"] > 0 else 0.0
    active_agents = completed["agent_id"].nunique()

    st.markdown("#### Organization Overview")
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        kpi_card("Total Tokens", f"{total_tokens:,}", f"{len(completed)} requests")
    with c2:
        kpi_card("Total Cost", f"${total_cost:.2f}", f"of ${cfg['org_monthly_budget']:.2f} org budget")
    with c3:
        kpi_card("Active Agents", f"{active_agents}")
    with c4:
        kpi_card("Blocked Requests", f"{len(blocked)}", "over-budget rejections")

    st.markdown("###### Org budget utilization")
    progress_bar(org_pct)
    st.caption(f"{org_pct:.1f}% of ${cfg['org_monthly_budget']:.2f} monthly org budget consumed")

    st.markdown("---")

    # A keyed st.radio (not st.tabs) -- st.tabs' active index is pure
    # client-side state that resets to the first tab on every fragment
    # auto-refresh; a keyed widget persists correctly in session_state.
    view = st.radio(
        "View", ["Per-Agent", "Consumption Over Time", "Event Log"],
        horizontal=True, label_visibility="collapsed", key="active_view",
    )

    if view == "Per-Agent":
        per_agent = completed.groupby("agent_id").agg(
            tokens=("tokens", "sum"),
            cost=("cost", "sum"),
            requests=("task_id", "count"),
            last_util_pct=("util_pct", "last"),
        ).reset_index().sort_values("cost", ascending=False)

        col_table, col_chart = st.columns([1, 1])
        with col_table:
            st.dataframe(
                per_agent.rename(columns={
                    "agent_id": "Agent", "tokens": "Tokens", "cost": "Cost ($)",
                    "requests": "Requests", "last_util_pct": "Daily Util (%)",
                }),
                use_container_width=True, hide_index=True,
            )
        with col_chart:
            fig = go.Figure()
            for i, row in per_agent.iterrows():
                color = AGENT_COLORS[list(per_agent["agent_id"]).index(row["agent_id"]) % len(AGENT_COLORS)]
                fig.add_trace(go.Bar(
                    x=[row["agent_id"]], y=[row["cost"]],
                    marker_color=color, name=row["agent_id"],
                    hovertemplate="%{x}<br>$%{y:.4f}<extra></extra>",
                ))
            fig.update_layout(
                showlegend=len(per_agent) > 1, height=320,
                margin=dict(l=10, r=10, t=10, b=10),
                plot_bgcolor=SURFACE, paper_bgcolor=SURFACE,
                yaxis_title="Cost ($)", xaxis_title=None,
            )
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

    elif view == "Consumption Over Time":
        fig = go.Figure()
        for i, agent_id in enumerate(completed["agent_id"].unique()):
            sub = completed[completed["agent_id"] == agent_id].copy()
            sub["cumulative_cost"] = sub["cost"].cumsum()
            fig.add_trace(go.Scatter(
                x=sub["time"], y=sub["cumulative_cost"], mode="lines+markers",
                name=agent_id, line=dict(color=AGENT_COLORS[i % len(AGENT_COLORS)], width=2),
                marker=dict(size=6),
                hovertemplate="%{x|%H:%M:%S}<br>$%{y:.4f}<extra>" + agent_id + "</extra>",
            ))
        fig.update_layout(
            height=380, margin=dict(l=10, r=10, t=10, b=10),
            plot_bgcolor=SURFACE, paper_bgcolor=SURFACE,
            yaxis_title="Cumulative cost ($)", xaxis_title=None,
            legend=dict(orientation="h", yanchor="bottom", y=1.02),
            hovermode="x unified",
        )
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

    else:  # Event Log
        display_df = df.sort_values("timestamp", ascending=False)[
            ["time", "agent_id", "task_id", "tokens", "cost", "status", "util_pct"]
        ].rename(columns={
            "time": "Time", "agent_id": "Agent", "task_id": "Task",
            "tokens": "Tokens", "cost": "Cost ($)", "status": "Status", "util_pct": "Util (%)",
        })
        st.dataframe(display_df, use_container_width=True, hide_index=True)

    st.caption(f"Last refreshed {time.strftime('%H:%M:%S')} -- auto-refreshes every 5s")


dashboard()
