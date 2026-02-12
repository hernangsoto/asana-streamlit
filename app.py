# app.py
from __future__ import annotations

import time
import re
import pandas as pd
import streamlit as st
import requests
from dateutil import parser as dtparser

from asana_api import AsanaClient, build_task_tree

st.set_page_config(page_title="Asana Task Explorer", layout="wide")
st.title("Asana – Tareas + Subtareas (Streamlit)")

CUSTOM_FIELD_NAME = "Medios | Tiempo de tarea"

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

token = st.secrets.get("ASANA_ACCESS_TOKEN", "")
workspace_id = st.secrets.get("ASANA_WORKSPACE_ID", "")

if not token or not workspace_id:
    st.error("Faltan secrets: ASANA_ACCESS_TOKEN y/o ASANA_WORKSPACE_ID.")
    st.stop()

client = AsanaClient(token)

# Health check
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


def extraer_medio(nombre_tarea: str) -> str:
    if not nombre_tarea:
        return "Sin medio"
    match = re.search(r"\[(.*?)\]", nombre_tarea)
    if match:
        contenido = match.group(1)
        if "-" in contenido:
            return contenido.split("-")[0].strip()
        return contenido.strip()
    return "Sin medio"


def extraer_proyectos(task: dict) -> str:
    """
    Devuelve una lista de proyectos (nombres) desde memberships.
    """
    memberships = task.get("memberships") or []
    names = []
    for m in memberships:
        if isinstance(m, dict):
            p = m.get("project")
            if isinstance(p, dict) and p.get("name"):
                names.append(p["name"])
    # unique manteniendo orden
    seen = set()
    uniq = []
    for n in names:
        if n not in seen:
            seen.add(n)
            uniq.append(n)
    return ", ".join(uniq) if uniq else ""


def extraer_custom_field(task: dict, field_name: str) -> str:
    """
    Busca el custom field por nombre y devuelve display_value o fallback.
    """
    cfs = task.get("custom_fields") or []
    for cf in cfs:
        if isinstance(cf, dict) and cf.get("name") == field_name:
            # Preferimos display_value (ya viene human-readable)
            dv = cf.get("display_value")
            if dv not in (None, ""):
                return str(dv)
            # fallbacks
            if cf.get("text_value") not in (None, ""):
                return str(cf["text_value"])
            if cf.get("number_value") not in (None, ""):
                return str(cf["number_value"])
            ev = cf.get("enum_value")
            if isinstance(ev, dict) and ev.get("name"):
                return str(ev["name"])
            return ""
    return ""


with st.sidebar:
    st.header("Filtros")

    filter_by_project = st.checkbox("Filtrar por proyecto", value=True)

    selected_project_gid = None
    selected_project_label = ""
    if filter_by_project:
        projects = get_projects(workspace_id)
        if not projects:
            st.warning("No se encontraron proyectos en el workspace.")
            st.stop()

        project_options = [(p["gid"], f'{p["name"]} ({p["gid"]})') for p in projects]
        label_by_gid = {gid: label for gid, label in project_options}

        selected_project_gid = st.selectbox(
            "Proyecto",
            options=[gid for gid, _ in project_options],
            format_func=lambda gid: label_by_gid.get(gid, gid),
        )
        selected_project_label = label_by_gid.get(selected_project_gid, "")
    else:
        st.info("Modo workspace: se usa búsqueda avanzada por usuarios (si está disponible).")

    st.subheader("Responsables")
    selected_user_ids = []
    with st.expander("Elegir usuarios", expanded=True):
        for u in usuarios:
            if st.checkbox(u["nombre"], key=f"user_{u['user_id']}", value=False):
                selected_user_ids.append(u["user_id"])

    max_depth = st.slider("Profundidad (niveles)", 1, 5, 3)

    completed_filter = st.selectbox("Estado", ["Todas", "Solo incompletas", "Solo completadas"], 0)

    deadline_mode = st.selectbox("Campo de deadline", ["due_on (fecha)", "due_at (fecha+hora)"], 0)
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

if not filter_by_project and len(selected_user_ids) == 0:
    st.warning("Si no filtrás por proyecto, seleccioná al menos 1 usuario (para evitar una carga enorme).")
    st.stop()


def load_roots() -> list[dict]:
    if filter_by_project:
        return client.list_project_tasks(selected_project_gid)

    # Workspace search
    params: dict = {}

    # Validar user_ids seleccionados (evita "Not a recognized ID")
    valid_user_ids = []
    invalid_user_ids = []

    for uid in selected_user_ids:
        try:
            u = client.get_user(uid)["data"]
            ws_gids = {w.get("gid") for w in (u.get("workspaces") or []) if isinstance(w, dict)}
            if workspace_id in ws_gids:
                valid_user_ids.append(uid)
            else:
                invalid_user_ids.append(uid)
        except requests.HTTPError:
            invalid_user_ids.append(uid)

    if invalid_user_ids:
        st.warning(
            "Estos user_id no son válidos o no pertenecen al workspace y se omitieron:\n- "
            + "\n- ".join(invalid_user_ids)
        )

    if not valid_user_ids:
        st.error("No quedó ningún usuario válido para buscar en este workspace.")
        st.stop()

    params["assignee.any"] = ",".join(valid_user_ids)

    if completed_filter == "Solo incompletas":
        params["completed"] = "false"
    elif completed_filter == "Solo completadas":
        params["completed"] = "true"

    if d_from is not None:
        params["due_at.after" if use_due_at else "due_on.after"] = str(d_from)
    if d_to is not None:
        params["due_at.before" if use_due_at else "due_on.before"] = str(d_to)

    return client.search_tasks_for_workspace(workspace_id, params)


progress_bar = st.progress(0, text="Iniciando…")
status_box = st.empty()

try:
    with st.spinner("Cargando tareas raíz…"):
        roots = load_roots()

    total_roots = len(roots)
    if total_roots == 0:
        st.warning("No se encontraron tareas con esos filtros.")
        st.stop()

    state = {
        "approx_total": max(total_roots, 1),
        "last_ui_ts": time.time(),
        "ui_every_items": 25,
        "ui_every_secs": 0.35,
    }

    def progress_cb(info: dict):
        visited = int(info.get("visited", 0))
        depth = int(info.get("depth", 0))
        name = str(info.get("current_name", ""))

        now = time.time()
        if (visited % state["ui_every_items"] != 0) and ((now - state["last_ui_ts"]) < state["ui_every_secs"]):
            return

        if visited > state["approx_total"]:
            state["approx_total"] = int(visited * 1.5)

        pct = min(int((visited / max(state["approx_total"], 1)) * 100), 99)
        progress_bar.progress(pct, text=f"Procesando… {visited} items")
        status_box.write(f"Nivel: {depth} | Actual: {name}")

        state["last_ui_ts"] = now

    with st.spinner("Expandiendo subtareas…"):
        flat = build_task_tree(
            client,
            roots,
            max_depth=max_depth,
            sleep_ms=1,
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

# Normalización básica
df["assignee_name"] = df.get("assignee").apply(lambda a: a.get("name") if isinstance(a, dict) else None)
df["assignee_gid"] = df.get("assignee").apply(lambda a: a.get("gid") if isinstance(a, dict) else None)

df["created_at_dt"] = df.get("created_at").apply(parse_dt)
df["completed_at_dt"] = df.get("completed_at").apply(parse_dt)

df["due_on_dt"] = pd.to_datetime(df.get("due_on"), errors="coerce")
df["due_at_dt"] = df.get("due_at").apply(parse_dt)

# Campos pedidos
df["medio"] = df.get("name").fillna("").apply(extraer_medio)

# proyecto desde memberships (puede ser múltiple)
df["proyecto"] = df.apply(lambda r: extraer_proyectos(r.to_dict()), axis=1)

# fallback: si estás filtrando por un proyecto y no hay memberships, usa el seleccionado
if filter_by_project and selected_project_label:
    df.loc[df["proyecto"].fillna("").str.strip() == "", "proyecto"] = selected_project_label.split(" (")[0].strip()

df["tiempo_tarea"] = df.apply(lambda r: extraer_custom_field(r.to_dict(), CUSTOM_FIELD_NAME), axis=1)

# Filtrado por usuarios siempre
if len(selected_user_ids) > 0:
    df = df[df["assignee_gid"].isin(selected_user_ids)]

# Filtros client-side extra cuando el modo es proyecto
if filter_by_project:
    if completed_filter == "Solo incompletas":
        df = df[df["completed"] == False]
    elif completed_filter == "Solo completadas":
        df = df[df["completed"] == True]

    base = df["due_at_dt"] if use_due_at else df["due_on_dt"]
    if d_from is not None:
        df = df[base >= pd.Timestamp(d_from)]
    if d_to is not None:
        df = df[base <= pd.Timestamp(d_to)]

# Presentación
df["indent_name"] = df.apply(lambda r: ("— " * int(r.get("level", 0))) + str(r.get("name", "")), axis=1)

# Columnas para UI
cols_ui = [
    "medio",
    "proyecto",
    "indent_name",
    "level",
    "completed",
    "created_at",
    "completed_at",
    "due_on",
    "due_at",
    "assignee_name",
    "tiempo_tarea",
    "permalink_url",
]
for c in cols_ui:
    if c not in df.columns:
        df[c] = None

st.subheader("Resultados")

st.dataframe(
    df[cols_ui].rename(columns={"indent_name": "tarea"}),
    use_container_width=True,
    hide_index=True,
)

# Export CSV con lo pedido
cols_export = [
    "medio",
    "proyecto",
    "name",
    "level",
    "completed",
    "created_at",
    "completed_at",
    "due_on",
    "due_at",
    "assignee_name",
    "tiempo_tarea",
    "permalink_url",
]
for c in cols_export:
    if c not in df.columns:
        df[c] = None

csv = df[cols_export].to_csv(index=False).encode("utf-8")
st.download_button("Descargar CSV", data=csv, file_name="asana_tasks.csv", mime="text/csv")
