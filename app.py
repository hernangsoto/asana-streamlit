# app.py
from __future__ import annotations

import pandas as pd
import streamlit as st
from dateutil import parser as dtparser

from asana_api import AsanaClient, build_task_tree


st.set_page_config(page_title="Asana Task Explorer", layout="wide")

st.title("Asana – Tareas + Subtareas (Streamlit)")

# --- Secrets ---
token = st.secrets.get("ASANA_ACCESS_TOKEN", "")
workspace_id = st.secrets.get("ASANA_WORKSPACE_ID", "")

if not token or not workspace_id:
    st.error("Faltan secrets: ASANA_ACCESS_TOKEN y/o ASANA_WORKSPACE_ID.")
    st.stop()

client = AsanaClient(token)


@st.cache_data(ttl=300)
def get_projects(workspace_gid: str):
    projs = client.list_projects(workspace_gid)
    # Filtra archivados por defecto
    return sorted([p for p in projs if not p.get("archived")], key=lambda x: x.get("name", ""))


@st.cache_data(ttl=300)
def get_tasks_flat(project_gid: str, max_depth: int):
    roots = client.list_project_tasks(project_gid)
    flat = build_task_tree(client, roots, max_depth=max_depth, sleep_ms=0)
    return flat


# --- Sidebar filtros ---
with st.sidebar:
    st.header("Filtros")

    projects = get_projects(workspace_id)
    if not projects:
        st.warning("No se encontraron proyectos en el workspace.")
        st.stop()

    proj_name_to_gid = {p["name"]: p["gid"] for p in projects}
    project_name = st.selectbox("Proyecto", options=list(proj_name_to_gid.keys()))
    project_gid = proj_name_to_gid[project_name]

    max_depth = st.slider("Profundidad (niveles)", min_value=1, max_value=5, value=3)

    completed_filter = st.selectbox(
        "Estado",
        options=["Todas", "Solo incompletas", "Solo completadas"],
        index=0,
    )

    # Rango de deadline
    deadline_mode = st.selectbox("Campo de deadline", options=["due_on (fecha)", "due_at (fecha+hora)"], index=0)
    use_due_at = deadline_mode.startswith("due_at")

    col1, col2 = st.columns(2)
    with col1:
        d_from = st.date_input("Deadline desde", value=None)
    with col2:
        d_to = st.date_input("Deadline hasta", value=None)

    st.divider()
    run = st.button("Cargar / Actualizar", type="primary")


if not run:
    st.info("Elegí filtros en la barra lateral y tocá **Cargar / Actualizar**.")
    st.stop()

# --- Data ---
with st.spinner("Cargando tareas y expandiendo subtareas..."):
    flat = get_tasks_flat(project_gid, max_depth=max_depth)

df = pd.DataFrame(flat)

if df.empty:
    st.warning("No hay tareas para mostrar.")
    st.stop()

# Normalización de campos
df["assignee_name"] = df.get("assignee").apply(lambda a: a.get("name") if isinstance(a, dict) else None)
df["assignee_gid"] = df.get("assignee").apply(lambda a: a.get("gid") if isinstance(a, dict) else None)

def parse_dt(x):
    if not x or pd.isna(x):
        return pd.NaT
    try:
        return dtparser.parse(str(x))
    except Exception:
        return pd.NaT

df["due_on_dt"] = pd.to_datetime(df.get("due_on"), errors="coerce")
df["due_at_dt"] = df.get("due_at").apply(parse_dt)

# Filtro por completadas
if completed_filter == "Solo incompletas":
    df = df[df["completed"] == False]
elif completed_filter == "Solo completadas":
    df = df[df["completed"] == True]

# Filtro por deadline
if use_due_at:
    base = df["due_at_dt"]
else:
    base = df["due_on_dt"]

if d_from is not None:
    df = df[base >= pd.Timestamp(d_from)]
if d_to is not None:
    df = df[base <= pd.Timestamp(d_to)]

# Responsable (se calcula después de filtrar lo anterior)
assignees = sorted([a for a in df["assignee_name"].dropna().unique().tolist()])
assignee_sel = st.selectbox("Responsable", options=["Todos"] + assignees, index=0)

if assignee_sel != "Todos":
    df = df[df["assignee_name"] == assignee_sel]

# Orden y columnas
df["indent_name"] = df.apply(lambda r: ("— " * int(r.get("level", 0))) + str(r.get("name", "")), axis=1)

cols = [
    "indent_name",
    "level",
    "completed",
    "due_on",
    "due_at",
    "assignee_name",
    "permalink_url",
]
for c in cols:
    if c not in df.columns:
        df[c] = None

st.subheader("Resultados")

st.dataframe(
    df[cols].rename(columns={"indent_name": "tarea"}),
    use_container_width=True,
    hide_index=True,
)

# Export
csv = df[cols].to_csv(index=False).encode("utf-8")
st.download_button("Descargar CSV", data=csv, file_name="asana_tasks.csv", mime="text/csv")
