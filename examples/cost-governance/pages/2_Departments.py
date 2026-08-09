"""Departments page.

Two things live here:
1. Departments -- just names, the picklist used everywhere else.
2. Agent default departments -- a fallback mapping (agent_id -> department)
   used only when a request doesn't carry an explicit "department/agent_id"
   tag on its "user" field. The per-request tag (parsed in
   custom_callback.py) is always the source of truth when present; this
   mapping only fills the gap for agents that haven't started tagging yet.

Run via the main app: streamlit run examples/cost-governance/streamlit_app.py
(included via st.navigation() in streamlit_app.py -- does not call
st.set_page_config/inject_css/ensure_db itself, since those must only run
once, in the router).
"""

import streamlit as st

from shared import (
    delete_department, load_agent_department_map,
    load_departments, load_events, remove_agent_department, save_department,
    set_agent_department,
)


# ── modals ───────────────────────────────────────────────────────────────────

@st.dialog("Add Department")
def add_department_dialog():
    with st.form("add_department_form"):
        name = st.text_input("Department Name *", placeholder="e.g., Marketing")
        submitted = st.form_submit_button("Add Department", use_container_width=True)
        if submitted:
            if not name:
                st.error("Department Name is required.")
            else:
                try:
                    save_department(name.strip())
                    st.rerun()
                except Exception as e:
                    st.error(f"Could not add department (does it already exist?): {e}")


@st.dialog("Add Agent Default Department")
def add_mapping_dialog(departments_df, known_agent_ids):
    with st.form("add_mapping_form"):
        if known_agent_ids:
            agent_id = st.selectbox("Agent ID", known_agent_ids)
            st.caption("Or type an agent_id that hasn't sent a request yet:")
        agent_id_free = st.text_input("Agent ID (free text)", placeholder="e.g., default-agent")
        department_name = st.selectbox("Default Department", departments_df["name"].tolist())

        submitted = st.form_submit_button("Save Mapping", use_container_width=True)
        if submitted:
            final_agent_id = agent_id_free.strip() or (agent_id if known_agent_ids else "")
            if not final_agent_id:
                st.error("Enter or select an Agent ID.")
            else:
                department_id = int(
                    departments_df.loc[departments_df["name"] == department_name, "id"].iloc[0]
                )
                set_agent_department(final_agent_id, department_id)
                st.rerun()


# ── header ───────────────────────────────────────────────────────────────────

st.markdown("""
<div class="brand-bar">
    <h1>Department <span>Attribution</span></h1>
    <div class="brand-tag">TAG: "department/agent_id"</div>
</div>
""", unsafe_allow_html=True)

st.caption(
    "Tag a request's \"user\" field as \"department/agent_id\" (e.g. "
    "\"marketing/default-agent\") to attribute it directly -- that always wins. "
    "The mapping below is only a fallback default for requests that don't tag "
    "a department explicitly."
)

# ── centered "add new department" call to action ────────────────────────────

with st.container(border=True):
    st.markdown(
        '<div class="add-cta-label">Register a new department</div>',
        unsafe_allow_html=True,
    )
    _, cta_col, _ = st.columns([1.5, 1, 1.5])
    with cta_col:
        if st.button("＋ Add Department", use_container_width=True, type="primary"):
            add_department_dialog()

st.write("")

departments_df = load_departments()

st.markdown("#### Departments")
if departments_df.empty:
    st.info("No departments yet. Click **+ Add Department** above.")
else:
    for _, row in departments_df.iterrows():
        col_name, col_delete = st.columns([5, 1])
        with col_name:
            st.markdown(f"""
            <div class="model-card">
                <div class="name">{row['name']}</div>
            </div>
            """, unsafe_allow_html=True)
        with col_delete:
            if st.button("Delete", key=f"delete_dept_{int(row['id'])}", use_container_width=True):
                delete_department(int(row["id"]))
                st.rerun()

st.markdown("---")
st.markdown("#### Agent Default Departments (fallback)")

if departments_df.empty:
    st.caption("Add a department above first.")
else:
    with st.container(border=True):
        st.markdown(
            '<div class="add-cta-label">Set a fallback department for an agent</div>',
            unsafe_allow_html=True,
        )
        events_df = load_events()
        known_agent_ids = sorted(events_df["agent_id"].unique().tolist()) if not events_df.empty else []
        _, cta_col2, _ = st.columns([1.5, 1, 1.5])
        with cta_col2:
            if st.button("＋ Add Mapping", use_container_width=True, type="primary"):
                add_mapping_dialog(departments_df, known_agent_ids)

st.write("")

mapping_df = load_agent_department_map()
if mapping_df.empty:
    st.info(
        "No default mappings yet. Requests without an explicit department "
        "tag will show up as \"Unassigned\" until you add one, or until the "
        "caller starts tagging \"department/agent_id\" directly."
    )
else:
    for _, row in mapping_df.iterrows():
        col_map, col_remove = st.columns([5, 1])
        with col_map:
            st.markdown(f"""
            <div class="model-card">
                <div class="name">{row['agent_id']}</div>
                <div class="meta">Defaults to: {row['department_name']}</div>
            </div>
            """, unsafe_allow_html=True)
        with col_remove:
            if st.button("Remove", key=f"remove_map_{row['agent_id']}", use_container_width=True):
                remove_agent_department(row["agent_id"])
                st.rerun()
