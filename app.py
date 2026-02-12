# app.py
from __future__ import annotations

import time
import pandas as pd
import streamlit as st
import requests
from dateutil import parser as dtparser

from asana_api import AsanaClient, build_task_tree

st.set_page_config(page_title="Asana Task Explorer", layout="wide")
st.title("Asana – Tareas + Subtareas (Streamlit)")

# --- Usuarios fijos (checkboxes) ---
usuarios = [
    {"nombre": "Nicolás Billia", "user_id": "1189529235923826"},
    {"nombre": "María Victoria Álvarez", "user_id": "1201119369347294"},
    {"nombre": "Hernán Soto", "user_id": "1200096899262902"},
    {"nombre": "Matías Rodríguez", "user_id": "1202908906082645"},
    {"nombre": "Agustín Gutiérrez", "user_id": "1202035356704206"},
    {"nombre": "Hernán Hambra", "user_id": "1204817729532326"},
    {"nombre": "Rodrigo Santos", "user_id": "1202118026104219"},
    {"nombre": "Gabriel Forero", "user_id": "1210174488536972"},
]

# --- Secrets ---
token = st.secrets.get("ASANA_ACCESS_TOKEN", "")
workspace_id = st.secrets.get("ASANA_WORKSPACE_ID", "")

if not token or not workspace_id:
    st.error("Faltan secrets: ASANA_ACCESS_TOKEN y/o ASANA_WORKSPACE_ID.")
    st.stop()

client = AsanaClient(token)

# --- Health check visible ---
try:
    me = client.get_me()
    user = me["data"]
    st.caption(f"Auth OK: {user.get('name')} ({user.get('email', 'sin email')})")
except requests.HTTPError as e:
    st.error("Falla de autenticación/permiso con Asana. Detalle real:")
    st.code(str(e))
    st.stop()


@st.cache_data(ttl=300)
def get_projects(workspace_gid: str):
    projs = client.list_projects(workspace_gid)
    projs = [p for p in projs if not p.get("archived")]
    return sorted(projs, key=lambda x: (x.get("name", ""), x.get("gid", "")))


def parse_dt(x):
    if not x or pd.isna(x):
        return pd.NaT
    try:
        return dtparser.parse(str(x))
    except Exception:
        return pd.NaT


# --- Sidebar filtros ---
with st.sidebar:
    st.header("Filtros")

    # Proyecto opcional
    filter_by_project = st.checkbox("Filtrar por proyecto", value=True)

    selected_project_gid = None
    if filter_by_project:
        projects = get_projects(workspace_id)
        if not projects:
            st.warning("No se encontraron proyectos en el workspace.")
            st.stop()

        project_options = [(p["gid"], f'{p["name"]} ({p["gid"]})') for p in projects]
        project_label_by_gid = {gid: label for gid, label in project_options}

        selected_project_gid = st.selectbox(
            "Proyecto",
            options=[gid for gid, _ in project_options],
            format_func=lambda gid: project_label_by_gid.get(gid, gid),
        )
    else:
        st.info("Modo workspace: se usa búsqueda avanzada por usuarios (si está disponible).")

    # Usuarios por checkbox (multi-selección)
    st.subheader("Responsables")
    selected_user_ids = []
    with st.expander("Elegir usuarios", expanded=True):
        for u in usuarios:
            key = f"user_{u['user_id']}"
            if st.checkbox(u["nombre"], key=key, value=False):
                selected_user_ids.append(u["user_id"])

    # Profundidad
    max_depth = st.slider("Profundidad (niveles)", min_value=1, max_value=5, value=3)

    # Estado
    completed_filter = st.selectbox(
        "Estado",
        options=["Todas", "Solo incompletas", "Solo completadas"],
        index=0,
    )

    # Deadline
    deadline_mode = st.selectbox(
        "Campo de deadline",
        options=["due_on (fecha)", "due_at (fecha+hora)"],
        index=0,
    )
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

# Si no filtrás por proyecto, obligamos al menos 1 usuario para no traerse medio Asana
if not filter_by_project and len(selected_user_ids) == 0:
    st.warning("Si no filtrás por proyecto, seleccioná al menos 1 usuario (para evitar una carga enorme).")
    st.stop()


# --- Helpers: construir roots según modo (proyecto vs workspace search) ---
def load_roots() -> list[dict]:
    if filter_by_project:
        return client.list_project_tasks(selected_project_gid)

    # Workspace search: filtra por usuarios y (si aplica) por fechas/estado
    params: dict = {}

    # assignee.any admite múltiples IDs separados por coma
    params["assignee.any"] = ",".join(selected_user_ids)

    # Estado
    if completed_filter == "Solo incompletas":
        # Preferimos completed=false (más directo)
        params["completed"] = "false"
    elif completed_filter == "Solo completadas":
        params["completed"] = "true"

    # Deadline (estos filtros son típicos del search endpoint)
    if d_from is not None:
        if use_due_at:
            params["due_at.after"] = str(d_from)
        else:
            params["due_on.after"] = str(d_from)

    if d_to is not None:
        if use_due_at:
            params["due_at.before"] = str(d_to)
        else:
            params["due_on.before"] = str(d_to)

    return client.search_tasks_for_workspace(workspace_id, params)


# --- Progreso throttled (evita SessionInfo / saturación UI) ---
progress_bar = st.progress(0, text="Iniciando…")
status_box = st.empty()

try:
    with st.spinner("Cargando tareas raíz…"):
        roots = load_roots()

    total_roots = len(roots)
    if total_roots == 0:
        st.warning("No se encontraron tareas con esos filtros.")
        st.stop()

    approx_total = max(total_roots, 1)

    last_ui_ts = time.time()
    ui_every_items = 25
    ui_every_secs = 0.35

    def progress_cb(info: dict):
        nonlocal last_ui_ts, approx_total

        visited = int(info.get("visited", 0))
        depth = int(info.get("depth", 0))
        name = str(info.get("current_name", ""))

        now = time.time()
        if (visited % ui_every_items != 0) and ((now - last_ui_ts) < ui_every_secs):
            return

        if visited > approx_total:
            approx_total = int(visited * 1.5)

        pct = min(int((visited / max(approx_total, 1)) * 100), 99)
        progress_bar.progress(pct, text=f"Procesando… {visited} items")
        status_box.write(f"Nivel: {depth} | Actual: {name}")

        last_ui_ts = now

    with st.spinner("Expandiendo subtareas…"):
        flat = build_task_tree(
            client,
            roots,
            max_depth=max_depth,
            sleep_ms=1,  # 1ms ayuda a “ceder” y mantener vivo el websocket
            progress_cb=progress_cb,
        )

    progress_bar.progress(100, text="Listo ✅")
    status_box.success(f"Completado: {len(flat)} items (tareas + subtareas).")

except requests.HTTPError as e:
    st.error("Error llamando a Asana. Detalle real:")
    st.code(str(e))
    st.stop()

df = pd.DataFrame(flat)

if df.empty:
    st.warning("No hay tareas para mostrar.")
    st.stop()

# --- Normalización ---
df["assignee_name"] = df.get("assignee").apply(lambda a: a.get("name") if isinstance(a, dict) else None)
df["assignee_gid"] = df.get("assignee").apply(lambda a: a.get("gid") if isinstance(a, dict) else None)

df["due_on_dt"] = pd.to_datetime(df.get("due_on"), errors="coerce")
df["due_at_dt"] = df.get("due_at").apply(parse_dt)

# --- Filtro por usuarios (aplica siempre, incluso en modo proyecto) ---
if len(selected_user_ids) > 0:
    df = df[df["assignee_gid"].isin(selected_user_ids)]

# --- Filtro por estado (para modo proyecto, ya que workspace search filtra server-side) ---
if filter_by_project:
    if completed_filter == "Solo incompletas":
        df = df[df["completed"] == False]
    elif completed_filter == "Solo completadas":
        df = df[df["completed"] == True]

# --- Filtro por deadline (para modo proyecto, ya que workspace search filtra server-side) ---
if filter_by_project:
    base = df["due_at_dt"] if use_due_at else df["due_on_dt"]
    if d_from is not None:
        df = df[base >= pd.Timestamp(d_from)]
    if d_to is not None:
        df = df[base <= pd.Timestamp(d_to)]

# --- Presentación ---
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

csv = df[cols].to_csv(index=False).encode("utf-8")
st.download_button("Descargar CSV", data=csv, file_name="asana_tasks.csv", mime="text/csv")
