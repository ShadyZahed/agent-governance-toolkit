"""Entry point / router for the cost-governance Streamlit app.

Uses st.navigation(position="top") instead of Streamlit's default automatic
pages/ sidebar list, so the page switcher (Dashboard / Models / Departments)
renders as a horizontal bar at the top of the main content area -- not
stacked above the governance parameter controls in the sidebar, which is
where Streamlit's default page list would otherwise inject itself.

st.set_page_config/inject_css/ensure_db must each run exactly once, so they
live here rather than in the individual page files.

Run with: streamlit run examples/cost-governance/streamlit_app.py
"""

import streamlit as st

from shared import ensure_db, inject_css

st.set_page_config(page_title="Cost Governance Dashboard", layout="wide", page_icon="◆")
inject_css()
ensure_db()

pg = st.navigation(
    [
        st.Page("dashboard_page.py", title="Dashboard", default=True),
        st.Page("pages/1_Models.py", title="Models"),
        st.Page("pages/2_Departments.py", title="Departments"),
    ],
    position="top",
)
pg.run()
