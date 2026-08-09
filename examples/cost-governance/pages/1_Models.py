"""Model registry page -- placeholder metadata storage for AI model/endpoint
configurations. No live connection logic yet: adding a model here just saves
its metadata to the models table in cost_events.db. The main dashboard's
model selector (see dashboard_page.py) reads from the same table, and writes
the selected models' ids (multi-select) into config.active_model_ids for the
gateway to read from later (same placeholder pattern as Estimator Mode).

Run via the main app: streamlit run examples/cost-governance/streamlit_app.py
(included via st.navigation() in streamlit_app.py -- does not call
st.set_page_config/inject_css/ensure_db itself, since those must only run
once, in the router).
"""

import datetime as dt

import streamlit as st

from shared import (
    AUTH_METHODS, PROVIDERS,
    delete_model, load_models, save_model, update_model,
)


# ── modals (must be defined before the buttons that open them) ─────────────

@st.dialog("Add Model")
def add_model_dialog():
    with st.form("add_model_form"):
        col1, col2 = st.columns(2)
        with col1:
            name = st.text_input("Model Name *", placeholder="e.g., Production Gemma4")
        with col2:
            provider = st.selectbox("Provider", PROVIDERS)

        model = st.text_input("Model", placeholder="e.g., gemma4:latest")
        description = st.text_area("Description", placeholder="Describe this model/endpoint...")
        endpoint_url = st.text_input("Endpoint URL *", placeholder="http://localhost:11434/api/chat")

        st.markdown("**Authentication**")
        auth_method = st.selectbox("Method", AUTH_METHODS, label_visibility="collapsed")
        if auth_method != "None":
            st.text_input(
                f"{auth_method} value", type="password",
                placeholder="Not stored -- placeholder only, see note below",
                disabled=True,
                help=(
                    "Credential values aren't persisted in this placeholder phase. "
                    "cost_events.db is a local, unencrypted SQLite file -- storing "
                    "real secrets here would need proper secrets management first. "
                    "Only the auth method itself is saved as metadata."
                ),
            )

        labels = st.text_input(
            "Labels", placeholder="key:value, key:value  (e.g. use_case:rag, environment:dev)",
        )

        submitted = st.form_submit_button("Add Model", use_container_width=True)
        if submitted:
            if not name or not endpoint_url:
                st.error("Model Name and Endpoint URL are required.")
            else:
                save_model(name, provider, model, description, endpoint_url, auth_method, labels)
                st.rerun()


@st.dialog("Edit Model")
def edit_model_dialog(row):
    model_id = int(row["id"])
    with st.form(f"edit_form_{model_id}"):
        ecol1, ecol2 = st.columns(2)
        with ecol1:
            e_name = st.text_input("Model Name *", value=row["name"])
        with ecol2:
            e_provider = st.selectbox(
                "Provider", PROVIDERS,
                index=PROVIDERS.index(row["provider"]) if row["provider"] in PROVIDERS else 0,
            )

        e_model = st.text_input("Model", value=row["model"] or "")
        e_description = st.text_area("Description", value=row["description"] or "")
        e_endpoint_url = st.text_input("Endpoint URL *", value=row["endpoint_url"])
        e_auth_method = st.selectbox(
            "Authentication Method", AUTH_METHODS,
            index=AUTH_METHODS.index(row["auth_method"]) if row["auth_method"] in AUTH_METHODS else 0,
        )
        e_labels = st.text_input("Labels", value=row["labels"] or "")

        save_col, cancel_col = st.columns(2)
        with save_col:
            save_clicked = st.form_submit_button("Save Changes", use_container_width=True)
        with cancel_col:
            cancel_clicked = st.form_submit_button("Cancel", use_container_width=True)

        if save_clicked:
            if not e_name or not e_endpoint_url:
                st.error("Model Name and Endpoint URL are required.")
            else:
                update_model(model_id, e_name, e_provider, e_model,
                              e_description, e_endpoint_url, e_auth_method, e_labels)
                st.rerun()
        if cancel_clicked:
            st.rerun()


# ── header ───────────────────────────────────────────────────────────────────

st.markdown("""
<div class="brand-bar">
    <h1>Model <span>Registry</span></h1>
    <div class="brand-tag">PLACEHOLDER -- METADATA ONLY</div>
</div>
""", unsafe_allow_html=True)

st.caption(
    "This only stores metadata for now -- there's no live connection test and "
    "the gateway doesn't route to these yet. Pick which one to track from the "
    "dropdown on the main dashboard."
)

# ── centered "add new model" call to action ─────────────────────────────────

with st.container(border=True):
    st.markdown(
        '<div class="add-cta-label">Register a new model or endpoint</div>',
        unsafe_allow_html=True,
    )
    _, cta_col, _ = st.columns([1.5, 1, 1.5])
    with cta_col:
        if st.button("＋ Add Model", use_container_width=True, type="primary"):
            add_model_dialog()

st.write("")

# ── registered models (top of page, per feedback) ──────────────────────────

st.markdown("#### Registered Models")
models_df = load_models()

if models_df.empty:
    st.info("No models registered yet. Click **+ Add Model** above.")
else:
    for _, row in models_df.iterrows():
        label_pills = "".join(
            f'<span class="label-pill">{lbl.strip()}</span>'
            for lbl in (row["labels"] or "").split(",") if lbl.strip()
        )
        created = dt.datetime.fromtimestamp(row["created_at"]).strftime("%b %d, %Y")
        model_id = int(row["id"])

        col_card, col_edit, col_delete = st.columns([5, 1, 1])
        with col_card:
            st.markdown(f"""
            <div class="model-card">
                <div class="name">{row['name']}</div>
                <div class="meta">
                    {row['provider']} · {row['model'] or 'no model set'} · {row['endpoint_url']}
                </div>
                <div class="meta">Auth: {row['auth_method']} · Added {created}</div>
                <div style="margin-top:0.4rem;">{label_pills}</div>
            </div>
            """, unsafe_allow_html=True)
        with col_edit:
            if st.button("Edit", key=f"edit_{model_id}", use_container_width=True):
                edit_model_dialog(row)
        with col_delete:
            if st.button("Delete", key=f"delete_{model_id}", use_container_width=True):
                delete_model(model_id)
                st.rerun()
