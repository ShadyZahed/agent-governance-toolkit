"""Main dashboard page body.

Reads the same SQLite DB that custom_callback.py writes to (cost_events.db),
and writes governance parameter changes back into its config table, which
the gateway re-reads before every request (see _reload_config_from_db in
custom_callback.py) -- so changes made here take effect on the live gateway
without a proxy restart.

See pages/1_Models.py for the model registry (placeholder metadata store);
this file's sidebar model-selector dropdown reads from the same table.

This file is included via st.navigation() in streamlit_app.py (the actual
entry point) -- it does not call st.set_page_config/inject_css/ensure_db
itself, since those must only run once, in the router.
"""

import datetime as dt
import time

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from shared import (
    AGENT_COLORS, DB_PATH, ESTIMATOR_MODES, MUTED, STATUS_COLORS, SURFACE,
    agent_color_map, brand_bar, kpi_card, load_config,
    load_events, load_models, progress_bar, save_active_models, save_config,
)
import os


brand_bar()


# ── sidebar: governance parameters + model selector ─────────────────────────

with st.sidebar:
    st.markdown("### Models to Track")
    models_df = load_models()
    cfg = load_config()

    if models_df.empty:
        st.caption("No models registered yet. Add one on the **Models** page.")
    else:
        options = models_df["id"].tolist()
        labels_map = {row["id"]: f"{row['name']} ({row['provider']})" for _, row in models_df.iterrows()}
        # Drop any stale ids (e.g. a tracked model that's since been deleted)
        # so a leftover reference never gets passed as a default.
        current_ids = [mid for mid in cfg["active_model_ids"] if mid in options]

        summary = f"{len(current_ids)} model{'s' if len(current_ids) != 1 else ''} selected" if current_ids else "Select models"
        with st.popover(summary, use_container_width=True):
            new_selection = []
            for mid in options:
                checked = st.checkbox(labels_map[mid], value=(mid in current_ids), key=f"model_track_{mid}")
                if checked:
                    new_selection.append(mid)
            if set(new_selection) != set(current_ids):
                save_active_models(new_selection)
                st.rerun()
        st.caption("Placeholder -- the gateway doesn't route to this yet.")

    st.divider()

    st.markdown("### Governance Parameters")
    st.caption("Changes apply to the live gateway on its next request -- no restart needed.")

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

        # Placeholder -- doesn't change any cost math yet. The gateway
        # (custom_callback.py) already reads this into CURRENT_ESTIMATOR_MODE
        # on every request; a future change would use it there to bias
        # estimated vs. actual cost calculations.
        estimator_mode = st.selectbox(
            "Estimator mode", ESTIMATOR_MODES,
            index=ESTIMATOR_MODES.index(cfg["estimator_mode"]),
            help="Placeholder for now -- will guide how the gateway estimates cost.",
        )

        submitted = st.form_submit_button("Apply to live gateway", use_container_width=True)
        if submitted:
            save_config(per_task_limit, per_agent_daily_limit, org_monthly_budget,
                        auto_throttle, kill_switch_threshold, estimator_mode)
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

    # Department filter -- "All (Company-wide)" is the existing unfiltered
    # rollup behavior; a specific department filters everything below it
    # (KPIs, charts, tables) to just that slice. Options come from whatever
    # departments actually appear in the data (including "Unassigned"), not
    # just the registered list on the Departments page, so nothing is hidden
    # just because it wasn't formally registered.
    departments_seen = sorted(df["department"].fillna("Unassigned").unique().tolist())
    dept_col, _ = st.columns([1, 3])
    with dept_col:
        selected_department = st.selectbox(
            "Department", ["All (Company-wide)"] + departments_seen, key="department_filter",
        )
    if selected_department != "All (Company-wide)":
        df = df[df["department"].fillna("Unassigned") == selected_department]
        if df.empty:
            st.info(f"No requests logged for \"{selected_department}\" yet.")
            return

    completed = df[df["status"] != "BLOCKED"]
    blocked = df[df["status"] == "BLOCKED"]

    total_tokens = int(completed["tokens"].sum())
    total_cost = float(completed["cost"].sum())
    org_pct = (total_cost / cfg["org_monthly_budget"] * 100) if cfg["org_monthly_budget"] > 0 else 0.0

    # "Active Agents" = distinct agents with >=1 successful request TODAY
    # (local calendar day), not a live/real-time connection count and not
    # all-time history. Compared against the raw epoch timestamp column
    # (not the derived "time" datetime, which is naive-UTC) so the boundary
    # lines up with the machine's local midnight.
    today_start_ts = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    active_today = completed[completed["timestamp"] >= today_start_ts]
    active_agents_today = active_today["agent_id"].nunique()

    overview_title = (
        "Organization Overview" if selected_department == "All (Company-wide)"
        else f"{selected_department} Overview"
    )
    st.markdown(f"#### {overview_title}")
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        kpi_card("Total Tokens", f"{total_tokens:,}", f"{len(completed)} requests")
    with c2:
        kpi_card("Total Cost", f"${total_cost:.2f}", f"of ${cfg['org_monthly_budget']:.2f} org budget")
    with c3:
        kpi_card("Active Agents (Today)", f"{active_agents_today}", "agents with a request today")
    with c4:
        kpi_card("Blocked Requests", f"{len(blocked)}", "over-budget rejections")

    st.markdown("###### Org budget utilization")

    # Yellow starts 1 std-dev-of-per-request-cost (as a % of org budget)
    # before the fixed 90% red line -- a single unusually expensive request
    # could plausibly swing spend by that much, so warn before you're
    # actually that close, not only once you've already crossed 90%.
    RED_THRESHOLD = 90.0
    cost_std = completed["cost"].std()
    if pd.isna(cost_std):
        cost_std = 0.0
    std_as_pct = (cost_std / cfg["org_monthly_budget"] * 100) if cfg["org_monthly_budget"] > 0 else 0.0
    yellow_threshold = max(0.0, RED_THRESHOLD - std_as_pct)

    progress_bar(org_pct, yellow_threshold=yellow_threshold, red_threshold=RED_THRESHOLD)
    st.caption(
        f"{org_pct:.1f}% of ${cfg['org_monthly_budget']:.2f} monthly org budget consumed "
        f"· yellow from {yellow_threshold:.1f}% (90% − 1 std-dev of per-request cost) "
        f"· red at {RED_THRESHOLD:.0f}%"
    )

    st.markdown("---")

    # A keyed st.radio (not st.tabs) -- st.tabs' active index is pure
    # client-side state that resets to the first tab on every fragment
    # auto-refresh; a keyed widget persists correctly in session_state.
    view = st.radio(
        "View", ["Per-Agent", "Estimate vs Actual", "Event Log"],
        horizontal=True, label_visibility="collapsed", key="active_view",
    )

    if view == "Per-Agent":
        # Scope selector -- the daily-limit line only means what it looks
        # like it means when the bars are also scoped to a single day.
        # Comparing an ever-growing all-time total against a per-day cap is
        # misleading past the first day, so "All Time" simply omits the line.
        scope = st.radio(
            "Scope", ["Today", "All Time"],
            horizontal=True, label_visibility="collapsed", key="per_agent_scope",
        )
        scope_df = active_today if scope == "Today" else completed

        if scope_df.empty:
            st.info("No requests yet today. Switch to \"All Time\" to see full history.")
        else:
            per_agent = scope_df.groupby("agent_id").agg(
                tokens=("tokens", "sum"),
                cost=("cost", "sum"),
                requests=("task_id", "count"),
                last_util_pct=("util_pct", "last"),
            ).reset_index().sort_values("cost", ascending=False)
            colors = agent_color_map(per_agent["agent_id"])

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
                chart_type = st.radio(
                    "Chart type", ["Bar", "Pie", "Radar"],
                    horizontal=True, label_visibility="collapsed", key="per_agent_chart_type",
                )

                if chart_type == "Pie":
                    fig = go.Figure(data=[go.Pie(
                        labels=per_agent["agent_id"],
                        values=per_agent["cost"],
                        marker=dict(colors=[colors[a] for a in per_agent["agent_id"]]),
                        textinfo="label+percent",
                        hovertemplate="%{label}<br>$%{value:.4f} (%{percent})<extra></extra>",
                        sort=False,
                    )])
                    fig.update_layout(
                        height=320, margin=dict(l=10, r=10, t=10, b=10),
                        paper_bgcolor=SURFACE,
                        showlegend=len(per_agent) > 4,
                    )

                elif chart_type == "Radar":
                    # Multi-metric profile per agent -- each axis normalized
                    # to % of the max value across agents shown, since Cost
                    # ($), Tokens (count), Requests (count), and Daily Util
                    # (%) are on incompatible scales and a radar chart needs
                    # comparable axes to not visually mislead.
                    axes = ["Cost", "Tokens", "Requests", "Daily Util %"]
                    metric_cols = ["cost", "tokens", "requests", "last_util_pct"]
                    raw = per_agent.set_index("agent_id")[metric_cols]
                    maxes = raw.max().replace(0, 1)  # avoid divide-by-zero
                    normalized = raw / maxes * 100

                    fig = go.Figure()
                    for agent_id in per_agent["agent_id"]:
                        vals = normalized.loc[agent_id, metric_cols].tolist()
                        fig.add_trace(go.Scatterpolar(
                            r=vals + [vals[0]], theta=axes + [axes[0]], name=agent_id,
                            line=dict(color=colors[agent_id], width=2),
                            fill="toself", opacity=0.55,
                            hovertemplate="%{theta}: %{r:.0f}% of max<extra>" + agent_id + "</extra>",
                        ))
                    fig.update_layout(
                        height=340, margin=dict(l=40, r=40, t=20, b=20),
                        paper_bgcolor=SURFACE,
                        polar=dict(radialaxis=dict(visible=True, range=[0, 100], ticksuffix="%")),
                        showlegend=len(per_agent) > 1,
                    )

                else:  # Bar
                    fig = go.Figure()
                    for _, row in per_agent.iterrows():
                        fig.add_trace(go.Bar(
                            x=[row["agent_id"]], y=[row["cost"]],
                            marker_color=colors[row["agent_id"]], name=row["agent_id"],
                            hovertemplate="%{x}<br>$%{y:.4f}<extra></extra>",
                        ))
                    fig.update_layout(
                        showlegend=len(per_agent) > 1, height=320,
                        margin=dict(l=10, r=10, t=10, b=10),
                        plot_bgcolor=SURFACE, paper_bgcolor=SURFACE,
                        yaxis_title="Cost ($)", xaxis_title=None,
                    )

                    # Daily-limit reference line -- only shown in "Today"
                    # scope (an all-time total isn't comparable to a daily
                    # cap), and only once a bar is within reach of it
                    # (>=50% of the limit) so a distant limit doesn't
                    # stretch the axis and shrink every bar to a sliver.
                    daily_limit = cfg["per_agent_daily_limit"]
                    max_bar = per_agent["cost"].max() if not per_agent.empty else 0.0
                    if scope == "Today" and daily_limit > 0 and max_bar >= daily_limit * 0.5:
                        fig.add_hline(
                            y=daily_limit, line_dash="dash", line_width=1.5,
                            line_color=STATUS_COLORS["CRITICAL"],
                            annotation_text=f"Daily limit ${daily_limit:.2f}",
                            annotation_position="top left",
                            annotation_font_color=STATUS_COLORS["CRITICAL"],
                        )
                        fig.update_yaxes(range=[0, max(max_bar, daily_limit) * 1.15])

                st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
                if chart_type == "Radar":
                    st.caption("Each axis is normalized to % of the max value across the agents shown.")

        # Time-evolution chart lives on the same page now, directly below
        # the bar/pie/radar chart -- always uses full history regardless of
        # the Today/All Time scope toggle above, since a single day's line
        # doesn't show much "evolution."
        st.markdown("---")
        st.markdown("###### Cost Evolution Over Time")
        st.caption("Always shows full history, independent of the Today/All Time scope above.")

        colors = agent_color_map(completed["agent_id"].unique())
        fig_time = go.Figure()
        for agent_id in completed["agent_id"].unique():
            sub = completed[completed["agent_id"] == agent_id].copy()
            sub["cumulative_cost"] = sub["cost"].cumsum()
            fig_time.add_trace(go.Scatter(
                x=sub["time"], y=sub["cumulative_cost"], mode="lines+markers",
                name=agent_id, line=dict(color=colors[agent_id], width=2),
                marker=dict(size=6),
                hovertemplate="%{x|%H:%M:%S}<br>$%{y:.4f}<extra>" + agent_id + "</extra>",
            ))
        fig_time.update_layout(
            height=420, margin=dict(l=10, r=10, t=10, b=10),
            plot_bgcolor=SURFACE, paper_bgcolor=SURFACE,
            yaxis_title="Cumulative cost ($)", xaxis_title=None,
            legend=dict(orientation="h", yanchor="bottom", y=1.02),
            hovermode="x unified",
            dragmode="zoom",
        )
        # Range slider for brushing/dragging to a narrower time window, plus
        # the zoom/pan/reset toolbar (hidden elsewhere in the app, but this
        # is the one chart where "zoom into a closer timeframe" matters).
        fig_time.update_xaxes(rangeslider_visible=True)
        st.plotly_chart(
            fig_time, use_container_width=True,
            config={
                "displayModeBar": True,
                "modeBarButtonsToRemove": ["lasso2d", "select2d"],
                "scrollZoom": True,
            },
        )
        st.caption("Drag on the chart to zoom in, drag the bar below the x-axis to pan, or double-click to reset.")

    elif view == "Estimate vs Actual":
        st.caption(
            "Estimated cost is currently a flat placeholder (see custom_callback.py's "
            "PRECHECK_ESTIMATE_COST) -- this will look like a vertical line of points "
            "until real per-request estimation logic exists. The plumbing is real; the "
            "estimate isn't yet."
        )
        colors = agent_color_map(completed["agent_id"].unique())
        fig_est = go.Figure()
        for agent_id in completed["agent_id"].unique():
            sub = completed[completed["agent_id"] == agent_id]
            fig_est.add_trace(go.Scatter(
                x=sub["estimated_cost"], y=sub["cost"], mode="markers",
                name=agent_id, marker=dict(color=colors[agent_id], size=9, opacity=0.75),
                hovertemplate="Estimated $%{x:.4f}<br>Actual $%{y:.4f}<extra>" + agent_id + "</extra>",
            ))
        axis_max = max(completed["estimated_cost"].max(), completed["cost"].max(), 0.01) * 1.15
        fig_est.add_trace(go.Scatter(
            x=[0, axis_max], y=[0, axis_max], mode="lines",
            line=dict(color=MUTED, dash="dot", width=1.5),
            name="Perfect estimate", hoverinfo="skip",
        ))
        fig_est.update_layout(
            height=420, margin=dict(l=10, r=10, t=10, b=10),
            plot_bgcolor=SURFACE, paper_bgcolor=SURFACE,
            xaxis_title="Estimated cost ($)", yaxis_title="Actual cost ($)",
            legend=dict(orientation="h", yanchor="bottom", y=1.02),
        )
        st.plotly_chart(fig_est, use_container_width=True, config={"displayModeBar": False})
        st.caption("Points above the dotted line were under-estimated; below were over-estimated.")

        mean_est = completed["estimated_cost"].mean()
        mean_actual = completed["cost"].mean()
        mae = (completed["cost"] - completed["estimated_cost"]).abs().mean()
        b1, b2, b3 = st.columns(3)
        with b1:
            kpi_card("Avg Estimated", f"${mean_est:.4f}")
        with b2:
            kpi_card("Avg Actual", f"${mean_actual:.4f}")
        with b3:
            kpi_card("Avg Bias (Actual − Estimated)", f"${mean_actual - mean_est:+.4f}", f"MAE ${mae:.4f}")

    else:  # Event Log
        display_df = df.sort_values("timestamp", ascending=False)[
            ["time", "agent_id", "task_id", "tokens", "cost", "estimated_cost", "status", "util_pct"]
        ].rename(columns={
            "time": "Time", "agent_id": "Agent", "task_id": "Task",
            "tokens": "Tokens", "cost": "Cost ($)", "estimated_cost": "Est. Cost ($)",
            "status": "Status", "util_pct": "Util (%)",
        })
        st.dataframe(display_df, use_container_width=True, hide_index=True)

    st.caption(f"Last refreshed {time.strftime('%H:%M:%S')} -- auto-refreshes every 5s")


dashboard()
