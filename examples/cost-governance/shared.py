"""Shared DB access, styling, and helpers for the cost-governance Streamlit
app and its pages/ (multi-page app). Imported by streamlit_app.py and by
pages/1_Models.py -- kept here instead of duplicated in each page.
"""

import os
import sqlite3

import pandas as pd
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

ESTIMATOR_MODES = ["Conservative", "Balanced", "Aggressive"]

# Models page -- see pages/1_Models.py. Placeholder metadata only; no live
# connection logic and no credential values are persisted (see auth_method
# handling in save_model).
PROVIDERS = [
    "Ollama (Local)", "OpenAI", "Azure OpenAI", "Azure AI Services",
    "Anthropic Claude", "Google Gemini", "xAI Grok", "Custom Q&A API", "Custom",
]
AUTH_METHODS = [
    "None", "API Key", "Basic (username/password)",
    "Bearer token (static)", "OAuth2 (client credentials)",
]


# ── data layer ───────────────────────────────────────────────────────────────

def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _migrate_config_columns(conn):
    """Add columns introduced after the original config table shipped, for
    DBs created before they existed. SQLite has no "ADD COLUMN IF NOT
    EXISTS", so check PRAGMA table_info first.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(config)").fetchall()}
    if "estimator_mode" not in existing:
        conn.execute("ALTER TABLE config ADD COLUMN estimator_mode TEXT DEFAULT 'Balanced'")
    if "active_model_id" not in existing:
        conn.execute("ALTER TABLE config ADD COLUMN active_model_id INTEGER")
    if "active_model_ids" not in existing:
        # Superseded active_model_id (single) -- multi-select needs a list.
        # Stored as a comma-separated string of model ids, same lightweight
        # approach as models.labels, rather than a separate join table.
        conn.execute("ALTER TABLE config ADD COLUMN active_model_ids TEXT DEFAULT ''")
        old_row = conn.execute("SELECT active_model_id FROM config WHERE id = 1").fetchone()
        if old_row and old_row[0] is not None:
            conn.execute("UPDATE config SET active_model_ids = ? WHERE id = 1", (str(old_row[0]),))


def _migrate_events_columns(conn):
    existing = {row[1] for row in conn.execute("PRAGMA table_info(events)").fetchall()}
    if "estimated_cost" not in existing:
        # Placeholder for a future real per-request estimator (see
        # custom_callback.py's PRECHECK_ESTIMATE_COST) -- until that exists,
        # every row gets the same flat estimate, so "Estimate vs Actual"
        # will look trivial (a vertical line of points) rather than a real
        # calibration plot. The column/plumbing is real; the value isn't yet.
        conn.execute("ALTER TABLE events ADD COLUMN estimated_cost REAL DEFAULT 0.0")
    if "department" not in existing:
        # Resolved once per request in custom_callback.py: explicit
        # "department/agent_id" tag on the "user" field wins; falls back to
        # the agent_departments default mapping; else "Unassigned". Existing
        # rows predate this feature entirely, so they backfill to
        # "Unassigned" -- there's no way to retroactively know which
        # department a historical request belonged to.
        conn.execute("ALTER TABLE events ADD COLUMN department TEXT DEFAULT 'Unassigned'")


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
                util_pct REAL NOT NULL,
                estimated_cost REAL DEFAULT 0.0,
                department TEXT DEFAULT 'Unassigned'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                per_task_limit REAL NOT NULL,
                per_agent_daily_limit REAL NOT NULL,
                org_monthly_budget REAL NOT NULL,
                auto_throttle INTEGER NOT NULL,
                kill_switch_threshold REAL NOT NULL,
                estimator_mode TEXT DEFAULT 'Balanced',
                active_model_id INTEGER,
                active_model_ids TEXT DEFAULT ''
            )
        """)
        # Models: placeholder metadata for an AI model/endpoint to eventually
        # route the gateway to. No secret/credential VALUES are stored here
        # -- only which auth method was selected -- since this is a local,
        # unencrypted SQLite file with no secrets-management story yet.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS models (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT,
                description TEXT,
                endpoint_url TEXT NOT NULL,
                auth_method TEXT NOT NULL,
                labels TEXT,
                created_at REAL NOT NULL
            )
        """)
        # Departments: the source of truth for department attribution is the
        # per-request "department/agent_id" tag on the "user" field (parsed
        # in custom_callback.py) -- these tables just (a) let the dashboard
        # offer a picker of known department names, and (b) provide a
        # fallback default department for an agent_id when a request didn't
        # carry an explicit tag. Not authoritative once a request tags itself.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS departments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                created_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_departments (
                agent_id TEXT PRIMARY KEY,
                department_id INTEGER NOT NULL REFERENCES departments(id)
            )
        """)
        _migrate_config_columns(conn)
        _migrate_events_columns(conn)
        row = conn.execute("SELECT id FROM config WHERE id = 1").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO config (id, per_task_limit, per_agent_daily_limit, "
                "org_monthly_budget, auto_throttle, kill_switch_threshold, "
                "estimator_mode, active_model_id) "
                "VALUES (1, 1.20, 2.00, 3.00, 1, 0.95, 'Balanced', NULL)"
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
            "auto_throttle, kill_switch_threshold, estimator_mode, active_model_ids "
            "FROM config WHERE id = 1"
        ).fetchone()
    keys = ["per_task_limit", "per_agent_daily_limit", "org_monthly_budget",
             "auto_throttle", "kill_switch_threshold", "estimator_mode", "active_model_ids"]
    cfg = dict(zip(keys, row))
    cfg["estimator_mode"] = cfg["estimator_mode"] or "Balanced"
    raw_ids = cfg.pop("active_model_ids") or ""
    cfg["active_model_ids"] = [int(x) for x in raw_ids.split(",") if x.strip().isdigit()]
    return cfg


def save_config(per_task_limit, per_agent_daily_limit, org_monthly_budget,
                auto_throttle, kill_switch_threshold, estimator_mode):
    with get_conn() as conn:
        conn.execute(
            "UPDATE config SET per_task_limit=?, per_agent_daily_limit=?, "
            "org_monthly_budget=?, auto_throttle=?, kill_switch_threshold=?, "
            "estimator_mode=? WHERE id=1",
            (per_task_limit, per_agent_daily_limit, org_monthly_budget,
             int(auto_throttle), kill_switch_threshold, estimator_mode),
        )
        conn.commit()


def save_active_models(model_ids):
    """Placeholder -- gateway doesn't route by this yet, same pattern as
    estimator_mode. Just persists which models the dashboard is "tracking"
    (multi-select) so a future change can read it from here instead of
    adding new plumbing.
    """
    ids_str = ",".join(str(int(mid)) for mid in model_ids)
    with get_conn() as conn:
        conn.execute("UPDATE config SET active_model_ids=? WHERE id=1", (ids_str,))
        conn.commit()


def load_models() -> pd.DataFrame:
    with get_conn() as conn:
        return pd.read_sql_query("SELECT * FROM models ORDER BY created_at DESC", conn)


def save_model(name, provider, model, description, endpoint_url, auth_method, labels):
    import time as _time
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO models (name, provider, model, description, endpoint_url, "
            "auth_method, labels, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (name, provider, model, description, endpoint_url, auth_method, labels, _time.time()),
        )
        conn.commit()


def update_model(model_id, name, provider, model, description, endpoint_url, auth_method, labels):
    with get_conn() as conn:
        conn.execute(
            "UPDATE models SET name=?, provider=?, model=?, description=?, "
            "endpoint_url=?, auth_method=?, labels=? WHERE id=?",
            (name, provider, model, description, endpoint_url, auth_method, labels, model_id),
        )
        conn.commit()


# ── departments ──────────────────────────────────────────────────────────────

def load_departments() -> pd.DataFrame:
    with get_conn() as conn:
        return pd.read_sql_query("SELECT * FROM departments ORDER BY name ASC", conn)


def save_department(name):
    import time as _time
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO departments (name, created_at) VALUES (?, ?)", (name, _time.time()),
        )
        conn.commit()


def delete_department(department_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM departments WHERE id=?", (department_id,))
        conn.execute("DELETE FROM agent_departments WHERE department_id=?", (department_id,))
        conn.commit()


def load_agent_department_map() -> pd.DataFrame:
    """agent_id -> default department name, for agents that don't (or
    haven't yet started to) send an explicit department tag per request."""
    with get_conn() as conn:
        return pd.read_sql_query(
            "SELECT ad.agent_id, d.id AS department_id, d.name AS department_name "
            "FROM agent_departments ad JOIN departments d ON ad.department_id = d.id "
            "ORDER BY ad.agent_id ASC",
            conn,
        )


def set_agent_department(agent_id, department_id):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO agent_departments (agent_id, department_id) VALUES (?, ?) "
            "ON CONFLICT(agent_id) DO UPDATE SET department_id=excluded.department_id",
            (agent_id, department_id),
        )
        conn.commit()


def remove_agent_department(agent_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM agent_departments WHERE agent_id=?", (agent_id,))
        conn.commit()


def delete_model(model_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM models WHERE id=?", (model_id,))
        # Drop it out of the multi-select tracking list if it's in there.
        row = conn.execute("SELECT active_model_ids FROM config WHERE id=1").fetchone()
        if row and row[0]:
            remaining = [x for x in row[0].split(",") if x.strip() and int(x) != model_id]
            conn.execute(
                "UPDATE config SET active_model_ids=? WHERE id=1", (",".join(remaining),),
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
            overflow: visible;
            border: 1px solid #E8E8E5;
            position: relative;
        }}
        .progress-fill {{
            height: 100%;
            border-radius: 8px 0 0 8px;
            overflow: hidden;
        }}
        .progress-marker {{
            position: absolute;
            top: -3px;
            bottom: -3px;
            width: 2px;
            background: {INK};
        }}
        .progress-marker-label {{
            position: absolute;
            top: -18px;
            transform: translateX(-50%);
            font-size: 0.7rem;
            font-weight: 700;
            color: {INK};
            white-space: nowrap;
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

        .model-card {{
            background: {SURFACE};
            border: 1px solid #E8E8E5;
            border-left: 4px solid {BRAND_YELLOW};
            border-radius: 10px;
            padding: 0.9rem 1.1rem;
            margin-bottom: 0.6rem;
        }}
        .model-card .name {{
            font-weight: 700;
            color: {INK};
            font-size: 1rem;
        }}
        .model-card .meta {{
            color: {MUTED};
            font-size: 0.82rem;
            margin-top: 0.15rem;
        }}
        .label-pill {{
            display: inline-block;
            background: {SURFACE_ALT};
            border: 1px solid #E8E8E5;
            color: {INK};
            font-size: 0.72rem;
            padding: 0.1rem 0.5rem;
            border-radius: 999px;
            margin-right: 0.3rem;
        }}

        /* Centered "add new thing" CTA card (Models/Departments pages) */
        .add-cta-label {{
            text-align: center;
            color: {MUTED};
            font-size: 0.85rem;
            margin-bottom: 0.7rem;
        }}
        div[data-testid="stVerticalBlockBorderWrapper"] {{
            border-radius: 12px !important;
        }}
        button[kind="primary"] {{
            background: {BRAND_YELLOW} !important;
            border-color: {BRAND_YELLOW} !important;
            color: {INK} !important;
            font-weight: 700 !important;
        }}
        button[kind="primary"]:hover {{
            filter: brightness(0.95);
        }}
    </style>
    """, unsafe_allow_html=True)


def agent_color_map(agent_ids) -> dict:
    """Stable agent_id -> color mapping, independent of sort/insertion order,
    so the same agent gets the same color in the bar chart, pie chart, and
    time-series chart even as cost rankings or first-seen order shift."""
    return {aid: AGENT_COLORS[i % len(AGENT_COLORS)] for i, aid in enumerate(sorted(agent_ids))}


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


def progress_bar(pct: float, yellow_threshold: float, red_threshold: float = 90.0):
    """Org budget utilization bar.

    - Green below `yellow_threshold`.
    - Yellow from `yellow_threshold` up to `red_threshold`.
    - Red at/above `red_threshold`.
    `red_threshold` is marked on the track with a vertical line + label.
    """
    pct_clamped = max(0.0, min(100.0, pct))
    if pct_clamped < yellow_threshold:
        color = STATUS_COLORS["OK"]
    elif pct_clamped < red_threshold:
        color = STATUS_COLORS["WARNING"]
    else:
        color = STATUS_COLORS["CRITICAL"]

    marker_pct = max(0.0, min(100.0, red_threshold))
    st.markdown(f"""
    <div class="progress-track">
        <div class="progress-fill" style="width:{pct_clamped}%; background:{color};"></div>
        <div class="progress-marker" style="left:{marker_pct}%;"></div>
        <div class="progress-marker-label" style="left:{marker_pct}%;">{red_threshold:.0f}%</div>
    </div>
    """, unsafe_allow_html=True)


def brand_bar(subtitle_tag: str = "LIVE GATEWAY"):
    st.markdown(f"""
    <div class="brand-bar">
        <h1>Cost Governance <span>Dashboard</span></h1>
        <div class="brand-tag">{subtitle_tag}</div>
    </div>
    """, unsafe_allow_html=True)
