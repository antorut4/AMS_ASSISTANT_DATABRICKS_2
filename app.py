"""AMS Ticket Assistant — versione SINGLE-FILE allineata alla logica della V1.

Nessuna dipendenza da Unity Catalog: gira su hive_metastore con nomi a 2 livelli
(schema.tabella), come la V1. RAG semantico con embedding + cosine (come la V1),
allegati iniettati nel prompt, tutto via SQL Warehouse. Niente Vector Search, niente
Volumes, niente catalog a 3 livelli.

Migliorie mantenute rispetto alla V1: UI più leggibile, stima costi in €, analisi con
avanzamento in tempo reale.

NOVITÀ (SAL):
  - Dashboard: mostra SOLO i ticket lavorabili -> esclude chiusi/risolti E on hold/pending.
  - Tab SAL: KPI + 4 tabelle (aperti settimana, chiusi settimana, blocked settimana,
    blocked totale) sulla finestra lun 14:00 -> lun successivo 13:30 (ora Europe/Rome).
  - Ingestion: i ticket CHIUSI vengono ora SALVATI (servono al SAL) e filtrati a valle;
    vengono popolati opened_at / closed_at / updated_at / state_code (prima erano NULL).

PARAMETRICO: personalizza SOLO il blocco CONFIG. Ogni chiave è sovrascrivibile da env
var  APP__SEZIONE__CHIAVE.

USO:
  - Deploy Databricks App:  app.yaml -> `python app.py`  (avvia la UI)
  - Bootstrap tabelle:      da notebook  ->  import app; app.run_bootstrap()
    (obbligatorio dopo questo aggiornamento: aggiunge le colonne state_code/updated_at)
"""

from __future__ import annotations

import os
import io
import re
import json
import html
import time
import uuid
import base64
import logging
import concurrent.futures as _cf
from datetime import datetime, timezone, timedelta

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ams")

# ###########################################################################
# #  CONFIG — UNICO BLOCCO DA PERSONALIZZARE. Override via env: APP__A__B=... #
# ###########################################################################
CONFIG = {
    "project": {"name": "AMS Ticket Assistant", "slug": "ams", "language": "it"},

    # Schema hive_metastore che contiene le tabelle dell'app (nomi a 2 livelli).
    "storage": {"schema": "prod_user_t4v_data"},

    "compute": {"warehouse_id": "08fc303fb22cc2bb",
                "default_host": "https://dbc-256e0b12-39b5.cloud.databricks.com"},

    "models": {
        "chat_primary": "databricks-claude-sonnet-4-6",
        "chat_fallbacks": ["databricks-claude-haiku-4-5"],
        "embedding_endpoint": "databricks-qwen3-embedding-0-6b",
        "embedding_dim": 1024,
        "max_tokens": 4000, "temperature": 0.0,
        # Tetto DURO per ogni chiamata al modello: timeout in secondi e n. tentativi.
        # Con questi, anche un endpoint lento/impuntato viene mollato in fretta.
        "request_timeout_s": 60, "max_attempts": 2,
        # Prezzi INDICATIVI €/1M token (input, output) per la stima costi.
        "prices": {
            "databricks-claude-opus-4-8":   [5.00, 25.00],
            "databricks-claude-opus-4-7":   [5.00, 25.00],
            "databricks-claude-sonnet-4-6": [3.00, 15.00],
            "databricks-claude-haiku-4-5":  [1.00, 5.00],
            "databricks-gpt-5-6-sol":       [2.50, 15.00],
            "databricks-gpt-5-5-pro":       [30.0, 180.0],
            "databricks-qwen35-122b-a10b":  [0.40, 2.40],
            "databricks-gemini-3-5-flash":  [0.50, 3.00],
        },
        "default_price": [3.00, 15.00],
    },

    # doc_char_budget: caratteri di documentazione iniettati per intero nel contesto.
    # chunk_*: la RICERCA lavora sui chunk (un documento lungo è raggiungibile anche
    #   a metà); l'INIEZIONE nel contesto resta a documento intero, come prima.
    # index_cache_s: l'indice dei vettori resta in memoria per N secondi, invece di
    #   essere riscaricato dal warehouse a ogni analisi e a ogni messaggio di chat.
    "rag": {"top_k": 3, "min_score": 0.30, "doc_char_budget": 100000,
            "chunk_chars": 3000, "chunk_overlap": 300,
            "max_index_rows": 50000, "index_cache_s": 300,
            "reindex_every_s": 900, "reindex_batch": 20,
            # Tetto ai notebook importabili in un colpo solo: una cartella grossa
            # significa altrettante chiamate di embedding tutte insieme.
            "notebook_import_max": 50},

    "servicenow": {
        "enabled": True,
        "base_url": "https://kiko.service-now.com/api/now/table",
        "incident_table": "incident", "request_item_table": "sc_req_item",
        "assignment_group_ids": [
            "f1864b9cdbaf185012e52dcb0b961915",   # IT-APPL-Datalake_HD2
            "f8599b50fbce7210ae27f882beefdcad",   # IT-APPL-Datalake_HD1
        ],
        "page_size": 1000, "max_records": 50000,
        # Quanti giorni indietro andare a prendere i ticket NON attivi (chiusi/risolti/
        # cancellati). Serve al SAL "chiusi della settimana": tenerlo basso evita di
        # scaricare anni di storico a ogni sync. 60gg copre 8 settimane di SAL.
        "closed_lookback_days": 60,
        # Campi richiesti all'API (payload più leggero). [] = tutti i campi.
        "fields": ["number", "short_description", "description", "close_notes", "comments",
                   "state", "priority", "assignment_group", "opened_at", "closed_at",
                   "sys_created_on", "sys_updated_on", "caller_id", "requested_for", "active"],
        # CREDENZIALI — ordine di precedenza:
        #  1) 'user'/'password' qui sotto IN CHIARO (sconsigliato: finiscono nel codice);
        #  2) env var SNOW_USER / SNOW_PASS (App Settings > Environment);
        #  3) secret scope 'AutoApi' (valori base64, più sicuro).
        # Per usare il chiaro, valorizza user/password; lascia "" per usare env/secret.
        "user": "", "password": "",
        "secret_scope": "AutoApi", "secret_user": "servicenow-user",
        "secret_password": "servicenow-pass",
    },

    # Mappa colonne Excel -> campi canonici. Il match è case-insensitive e ignora gli
    # spazi ai bordi (vedi excel_import). Elenca qui gli alias delle intestazioni reali
    # dei file (es. export ServiceNow in italiano). Basta aggiungere una voce alla lista.
    "excel_column_map": {
        "number": ["Number", "Numero", "ID", "Task"],
        "short_description": ["Short description", "Breve descrizione", "Titolo", "Descrizione breve"],
        "description": ["Description", "Descrizione"],
        "state": ["State", "Stato"],
        "priority": ["Priority", "Priorità", "Impatto Opex", "Impatto Business"],
        "assignment_group": ["Assignment group", "Gruppo"],
        "assignee": ["Assignee", "Assegnatario", "Assegnato a", "Owner", "Assigned to"],
        "close_notes": ["Close notes", "Note di chiusura", "Note"],
        "caller": ["Caller", "Aperto da", "Requested for", "Richiesto da"],
        "opened_at": ["Opened", "Opened at", "Data apertura", "Data creazione", "Created"],
        "closed_at": ["Closed", "Closed at", "Data chiusura", "Data risoluzione", "Chiuso", "Resolved"],
        "updated_at": ["Updated", "Updated at", "Data aggiornamento", "Sys updated on"],
    },

    "ingestion": {
        # Stati considerati CHIUSI: match sottostringa, case-insensitive. Bilingue IT+EN
        # (es. "chius" copre Chiuso/Chiusa/Chiusi; "clos" copre Closed).
        "exclude_state_keywords": ["clos", "chius", "resol", "risol", "cancel", "annull",
                                   "fulfil", "evas", "complet"],
        # False = i chiusi vengono SALVATI comunque (il SAL ne ha bisogno) e nascosti
        # a valle da Dashboard/Repository. True = comportamento vecchio (li scarta).
        "drop_closed_on_ingest": False,
        "merge_batch_size": 300,
        # True = su un ticket già presente, i campi VUOTI ('' o NULL) del nuovo import
        # NON sovrascrivono i valori esistenti (evita di svuotare dati arricchiti da
        # un'altra sorgente ricaricando un file parziale). False = comportamento pieno.
        "preserve_on_empty": True,
        # True = ogni import Excel ELIMINA i ticket source='excel' non più presenti nel
        # file, TRANNE quelli con un'analisi. Non tocca ticket di altre sorgenti (ServiceNow).
        "excel_replace": True,
    },

    "dashboard": {
        # Stati NASCOSTI in Dashboard, oltre ai chiusi. Restano visibili nel tab SAL.
        "hide_state_keywords": ["hold", "pending", "attesa", "sospes", "await", "suspend"],
    },

    "sal": {
        "tz": "Europe/Rome",
        "week_start_weekday": 0,          # 0 = lunedì
        "start_hour": 14, "start_minute": 0,
        "end_hour": 13, "end_minute": 30,
        # Stati che contano come BLOCKED nel SAL.
        "blocked_state_keywords": ["hold", "pending", "attesa", "sospes", "await", "suspend"],
        "aging_alert_days": 30,
        "max_rows": 500,
        "weeks_selectable": 8,
    },

    # max_tool_iterations: passi max in analisi ticket · chat_max_iterations: passi max in
    # chat/assistente · tool_time_budget_s: tetto di tempo (sec) al loop agentico.
    "workflow": {"max_tool_iterations": 8, "chat_max_iterations": 4,
                 "tool_time_budget_s": 75, "self_critique": True},

    "taxonomy": {
        "problem_types": ["DATA_QUALITY", "PIPELINE_FAILURE", "ACCESS_REQUEST",
                          "CONFIGURATION", "PERFORMANCE", "OTHER"],
        "severities": ["HIGH", "MEDIUM", "LOW"],
    },

    "team": {"members": ["Marcello Porreca", "Gemma Piccirillo",
                         "Chiara Scoccimarro", "Antonio Ruta"]},

    "agent_tools": {
        "explorable_schemas": [
            "prod_common_silver", "prod_common_bronze", "prod_kiko_silver",
            "prod_kiko_bronze", "prod_kiko_gold", "prod_ops_silver", "prod_ops_bronze",
            "prod_ops_gold", "prod_eva_bronze", "prod_eva_integration",
            "prod_partner_silver", "prod_partner_bronze", "prod_user_t4v_data",
            "prod_user_crm_data", "prod_user_rtl_data",
        ],
        "allowed_workspace_paths": ["/Workspace/PROD_", "/Workspace/DEV_", "/Workspace/Shared"],
        "etl_roots": ["/Workspace/PROD_COMMON/ETL", "/Workspace/PROD_KIKO/ETL",
                      "/Workspace/PROD_OPS/ETL", "/Workspace/PROD_EVA/ETL"],
        # Tetti anti-blocco: quando explorable_schemas/etl_roots sono "*", limitano quante
        # cose scandire prima di fermarsi, così i tool non girano per minuti.
        "max_scan_schemas": 30,
        "max_scan_objects": 4000,
        # Timeout DURO (sec) per singola chiamata a strumento: una query/scan che si
        # impunta viene abbandonata e l'agente prosegue, invece di bloccarsi per minuti.
        "tool_call_timeout_s": 30,
        "forbidden_sql": ["DELETE", "UPDATE", "MERGE", "INSERT", "DROP", "ALTER",
                          "CREATE", "TRUNCATE", "GRANT", "REVOKE"],
    },

    "attachments": {"max_text_chars": 8000, "max_image_bytes": 4000000, "excel_max_rows": 200},

    "branding": {"header_title": "AMS Ticket Assistant",
                 "header_subtitle": "KIKO Cosmetics · Datalake Operations · Databricks",
                 "accent_color": "#EC008C"},

    "prompts": {
        "system_role": (
            "Sei un Senior Data Engineer AMS Lead su Databricks/Azure Datalake. Hai tool in "
            "SOLA LETTURA: search_tables, list_tables_in_schema, run_sql_query, describe_table, "
            "get_table_details, find_etl_for_table, read_notebook, check_recent_job_runs. "
            "Se non sei sicuro del nome esatto di una tabella, usa PRIMA search_tables: non "
            "indovinare. Sui problemi dati NON fermarti al sintomo: investiga la pipeline, leggi "
            "l'ETL, trova il punto esatto (join che duplica, filtro mancante, cast sbagliato). "
            "La root cause dev'essere concreta e verificabile. Dichiara una confidence onesta e "
            "spiega il ragionamento. Rispondi in italiano, tecnico e operativo."),
        "self_critique": (
            "Prima di concludere: hai EVIDENZA concreta (query o riga di ETL) per la root cause? "
            "Hai distinto sintomo da causa? Se manca evidenza, continua a investigare."),
    },
}

ATTACH_TEXT_EXT = {".txt", ".log", ".csv", ".tsv", ".json", ".sql", ".py", ".md", ".yaml", ".yml", ".xml", ".err", ".out"}
ATTACH_EXCEL_EXT = {".xlsx", ".xls", ".xlsm"}
ATTACH_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

# Marcatore dei fallimenti di estrazione: _extract_file_text non solleva eccezioni
# (per non far cadere l'analisi su un allegato illeggibile), quindi il chiamante ha
# bisogno di un modo NON ambiguo per distinguere "testo" da "messaggio d'errore".
EXTRACT_FAIL = "[!ESTRAZIONE_FALLITA]"


def _fail(msg: str) -> str:
    return f"{EXTRACT_FAIL} {msg}"


def extraction_failed(text) -> bool:
    return _s(text).lstrip().startswith(EXTRACT_FAIL)


def extraction_reason(text) -> str:
    return _s(text).lstrip()[len(EXTRACT_FAIL):].strip()


# --- accesso config -----------------------------------------------------------
def cfg(path: str, default=None):
    env_key = "APP__" + path.upper().replace(".", "__")
    if env_key in os.environ:
        return _coerce(os.environ[env_key])
    node = CONFIG
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def _coerce(v: str):
    low = v.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(v)
    except ValueError:
        try:
            return float(v)
        except ValueError:
            return v


def table(name: str) -> str:
    return f'{cfg("storage.schema")}.{name}'


ACTOR = os.environ.get("DATABRICKS_APP_USER", "operator")
_now = lambda: datetime.now(timezone.utc)


def _s(v) -> str:
    """Stringa sicura: None/NaN -> ''."""
    import pandas as pd
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return str(v)


# ===========================================================================
# AUTH
# ===========================================================================
_WC = {"c": None}


def wc():
    if _WC["c"] is None:
        from databricks.sdk import WorkspaceClient
        from databricks.sdk.core import Config
        cid, csec = os.environ.get("DATABRICKS_CLIENT_ID"), os.environ.get("DATABRICKS_CLIENT_SECRET")
        host = os.environ.get("DATABRICKS_HOST", "")
        if cid and csec:
            conf = Config(host=host if host.startswith("http") else f"https://{host}",
                          client_id=cid, client_secret=csec)
            _WC["c"] = WorkspaceClient(config=conf)
        else:
            _WC["c"] = WorkspaceClient()
    return _WC["c"]


def get_host() -> str:
    h = os.environ.get("DATABRICKS_HOST", "")
    if h:
        return h.rstrip("/") if h.startswith("http") else f"https://{h}"
    try:
        return wc().config.host.rstrip("/")
    except Exception:
        return cfg("compute.default_host", "")


def get_token() -> str:
    env_tok = os.environ.get("DATABRICKS_TOKEN", "")
    if env_tok:
        return env_tok
    try:
        return (wc().config.authenticate() or {}).get("Authorization", "").replace("Bearer ", "")
    except Exception as e:
        logger.warning(f"token: {e}")
        return ""


def snow_creds() -> tuple:
    # 1) credenziali in chiaro dalla config (se valorizzate)
    u, p = cfg("servicenow.user", ""), cfg("servicenow.password", "")
    if u and p:
        return u, p
    # 2) variabili d'ambiente
    u, p = os.environ.get("SNOW_USER", ""), os.environ.get("SNOW_PASS", "")
    if u and p:
        return u, p
    # 3) secret scope (base64)
    scope = cfg("servicenow.secret_scope", "")
    ku, kp = cfg("servicenow.secret_user", ""), cfg("servicenow.secret_password", "")
    try:
        w = wc()
        uu = w.secrets.get_secret(scope=scope, key=ku)
        pp = w.secrets.get_secret(scope=scope, key=kp)
        return (base64.b64decode(uu.value).decode("utf-8"),
                base64.b64decode(pp.value).decode("utf-8"))
    except Exception as e:
        logger.warning(f"secret scope '{scope}': {e}")
        return "", ""


# ===========================================================================
# SQL WAREHOUSE
# ===========================================================================
def _conn():
    from databricks import sql as dbsql
    hostname = get_host().replace("https://", "")
    http_path = f'/sql/1.0/warehouses/{cfg("compute.warehouse_id")}'
    env_tok = os.environ.get("DATABRICKS_TOKEN", "")
    if env_tok:
        return dbsql.connect(server_hostname=hostname, http_path=http_path, access_token=env_tok)
    client = wc()
    return dbsql.connect(server_hostname=hostname, http_path=http_path,
                         credentials_provider=lambda: (lambda: client.config.authenticate()))


def run_sql(query: str, max_rows: int = 1000):
    import pandas as pd
    for attempt in range(3):
        try:
            with _conn() as c, c.cursor() as cur:
                cur.execute(query)
                rows = cur.fetchmany(max_rows)
                cols = [d[0] for d in cur.description] if cur.description else []
                return pd.DataFrame(rows, columns=cols)
        except Exception as e:
            if attempt == 2:
                raise
            logger.warning(f"run_sql retry {attempt}: {str(e)[:120]}")
            time.sleep(1.5 * (attempt + 1))


def exec_sql(query: str):
    with _conn() as c, c.cursor() as cur:
        cur.execute(query)


class SqlNull:
    """NULL tipizzato: evita che un batch di sole NULL diventi di tipo void e
    faccia fallire la MERGE su colonne TIMESTAMP."""

    def __init__(self, typ: str = "STRING"):
        self.typ = typ


def ts_lit(dt) -> str:
    """Literal timestamp ESPLICITAMENTE in UTC: indipendente da
    spark.sql.session.timeZone del warehouse."""
    if dt is None:
        return "CAST(NULL AS TIMESTAMP)"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return "CAST('" + dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S") + "+00:00' AS TIMESTAMP)"


def sql_str(v) -> str:
    import pandas as pd
    if isinstance(v, SqlNull):
        return f"CAST(NULL AS {v.typ})"
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, datetime):
        return ts_lit(v)
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def _merge_set_expr(c: str, rows: list, preserve_on_empty: bool) -> str:
    """Espressione UPDATE SET per la colonna c. Con preserve_on_empty, i valori vuoti
    della sorgente non sovrascrivono l'esistente: NULLIF su stringhe (per gestire ''),
    COALESCE semplice su date/numeri (già NULL quando vuoti)."""
    if not preserve_on_empty:
        return f"t.{c} = s.{c}"
    string_col = True
    for r in rows:
        v = r.get(c)
        if isinstance(v, (datetime, int, float, bool)) or (isinstance(v, SqlNull) and v.typ.upper() != "STRING"):
            string_col = False
            break
    if string_col:
        return f"t.{c} = COALESCE(NULLIF(s.{c}, ''), t.{c})"
    return f"t.{c} = COALESCE(s.{c}, t.{c})"


def merge_values(tbl: str, key_cols: list, rows: list, preserve_on_empty: bool = False):
    """MERGE upsert per chiave. Con preserve_on_empty=True, sui record già presenti i
    valori vuoti ('' o NULL) NON sovrascrivono quelli esistenti (ricarichi parziali sicuri)."""
    if not rows:
        return
    cols = list(rows[0].keys())
    values = ",\n".join("(" + ", ".join(sql_str(r.get(c)) for c in cols) + ")" for r in rows)
    on = " AND ".join(f"t.{k} = s.{k}" for k in key_cols)
    upd = [c for c in cols if c not in key_cols]
    sets = ", ".join(_merge_set_expr(c, rows, preserve_on_empty) for c in upd)
    matched = f"WHEN MATCHED THEN UPDATE SET {sets}" if upd else ""
    exec_sql(f"""
        MERGE INTO {tbl} AS t
        USING (SELECT * FROM (VALUES {values}) AS v({', '.join(cols)})) AS s
        ON {on}
        {matched}
        WHEN NOT MATCHED THEN INSERT ({', '.join(cols)}) VALUES ({', '.join(f's.{c}' for c in cols)})
    """)


def insert_values(tbl: str, rows: list):
    if not rows:
        return
    cols = list(rows[0].keys())
    values = ",\n".join("(" + ", ".join(sql_str(r.get(c)) for c in cols) + ")" for r in rows)
    exec_sql(f"INSERT INTO {tbl} ({', '.join(cols)}) VALUES {values}")


# ===========================================================================
# STATI: predicati Python + clausole SQL (unica fonte di verità = CONFIG)
# ===========================================================================
def _kw_hit(value, keywords) -> bool:
    s = _s(value).lower()
    return bool(s) and any(str(k).lower() in s for k in (keywords or []))


def is_closed_state(state) -> bool:
    return _kw_hit(state, cfg("ingestion.exclude_state_keywords", []))


def is_blocked_state(state) -> bool:
    """On hold / pending: bloccato in attesa di terzi."""
    return (not is_closed_state(state)) and _kw_hit(state, cfg("sal.blocked_state_keywords", []))


def _kw_clause(col: str, keywords, negate: bool = False) -> str:
    """Clausola SQL su una colonna di stato. negate=True -> 'nessuna keyword'."""
    ks = [str(k).lower().replace("'", "") for k in (keywords or [])]
    if not ks:
        return "1=1" if negate else "1=0"
    if negate:
        return "(" + " AND ".join(f"lower(coalesce({col},'')) NOT LIKE '%{k}%'" for k in ks) + ")"
    return "(" + " OR ".join(f"lower(coalesce({col},'')) LIKE '%{k}%'" for k in ks) + ")"


def cl_not_closed(col: str = "t.state") -> str:
    return _kw_clause(col, cfg("ingestion.exclude_state_keywords", []), negate=True)


def cl_closed(col: str = "t.state") -> str:
    return _kw_clause(col, cfg("ingestion.exclude_state_keywords", []))


def cl_blocked(col: str = "t.state") -> str:
    return f"({_kw_clause(col, cfg('sal.blocked_state_keywords', []))} AND {cl_not_closed(col)})"


# ===========================================================================
# FINESTRA SAL — lun 14:00 -> lun successivo 13:30 (ora locale, DST-safe)
# ===========================================================================
_GG = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]


def _tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(cfg("sal.tz", "Europe/Rome"))
    except Exception as e:
        logger.warning(f"timezone '{cfg('sal.tz')}' non disponibile (serve il pacchetto "
                       f"tzdata): {e}. Fallback UTC: le finestre SAL saranno sfasate.")
        return timezone.utc


def sal_window(offset_weeks: int = 0) -> tuple:
    """Ritorna (start_utc, end_utc, label). offset_weeks=0 settimana corrente,
    -1 la precedente. I confini sono calcolati sull'orologio locale (wall clock),
    poi convertiti in UTC: corretti anche a cavallo del cambio ora."""
    tz = _tz()
    sh, sm = int(cfg("sal.start_hour", 14)), int(cfg("sal.start_minute", 0))
    eh, em = int(cfg("sal.end_hour", 13)), int(cfg("sal.end_minute", 30))
    wd = int(cfg("sal.week_start_weekday", 0))
    naive = datetime.now(tz).replace(tzinfo=None)
    anchor = (naive - timedelta(days=(naive.weekday() - wd) % 7)).replace(
        hour=sh, minute=sm, second=0, microsecond=0)
    if anchor > naive:                      # lunedì prima delle 14:00 -> settimana precedente
        anchor -= timedelta(days=7)
    start_n = anchor + timedelta(weeks=int(offset_weeks))
    end_n = (start_n + timedelta(days=7)).replace(hour=eh, minute=em, second=0, microsecond=0)
    label = (f"{_GG[start_n.weekday()]} {start_n:%d/%m %H:%M} → "
             f"{_GG[end_n.weekday()]} {end_n:%d/%m %H:%M} ({cfg('sal.tz', 'Europe/Rome')})")
    return (start_n.replace(tzinfo=tz).astimezone(timezone.utc),
            end_n.replace(tzinfo=tz).astimezone(timezone.utc), label)


# ===========================================================================
# ALLEGATI (come la V1): testo iniettato nel prompt, immagini come blocchi multimodali
# ===========================================================================
def process_attachments(files) -> tuple:
    if not files:
        return "", [], ""
    if not isinstance(files, (list, tuple)):
        files = [files]
    max_text = cfg("attachments.max_text_chars", 8000)
    max_img = cfg("attachments.max_image_bytes", 4000000)
    text_parts, image_blocks, notes = [], [], []
    for f in files:
        path = f if isinstance(f, str) else getattr(f, "name", None)
        if not path or not os.path.exists(path):
            continue
        fname = os.path.basename(path)
        ext = os.path.splitext(fname)[1].lower()
        try:
            if ext in ATTACH_IMAGE_EXT:
                size = os.path.getsize(path)
                if size > max_img:
                    notes.append(f"immagine '{fname}' troppo grande, scartata")
                    continue
                with open(path, "rb") as fh:
                    b64 = base64.b64encode(fh.read()).decode("utf-8")
                mime = "image/jpeg" if ext in (".jpg", ".jpeg") else f"image/{ext.lstrip('.')}"
                image_blocks.append({"type": "image_url",
                                     "image_url": {"url": f"data:{mime};base64,{b64}"}})
            else:
                # QUALSIASI altro file passa dall'estrattore unico e robusto.
                content = _extract_file_text(path)
                if extraction_failed(content):
                    notes.append(f"'{fname}' non estraibile")
                    content = extraction_reason(content)
                if len(content) > max_text:
                    content = content[:max_text] + "\n[...troncato...]"
                text_parts.append(f"--- Allegato: {fname} ---\n{content}")
        except Exception as e:
            notes.append(f"errore su '{fname}': {str(e)[:60]}")
    text = ("\n\nALLEGATI FORNITI DALL'UTENTE:\n" + "\n\n".join(text_parts)) if text_parts else ""
    return text, image_blocks, (" (" + "; ".join(notes) + ")") if notes else ""


def _user_content(text: str, image_blocks: list):
    if image_blocks:
        return [{"type": "text", "text": text}] + image_blocks
    return text


# ===========================================================================
# EMBEDDINGS (come la V1): serving endpoint + cosine in Python
# ===========================================================================
def embed_texts(texts: list) -> list:
    """Ritorna un vettore per ogni testo, oppure None dove l'embedding è fallito.
    La lunghezza dell'output è SEMPRE pari a quella dell'input."""
    import requests
    out = []
    endpoint = cfg("models.embedding_endpoint")
    for i in range(0, len(texts), 16):
        batch = [t[:6000] for t in texts[i:i + 16]]
        vecs = None
        for attempt in range(3):
            try:
                r = requests.post(
                    f"{get_host()}/serving-endpoints/{endpoint}/invocations",
                    headers={"Authorization": f"Bearer {get_token()}",
                             "Content-Type": "application/json"},
                    json={"input": batch}, timeout=60)
                if r.status_code == 429:
                    time.sleep(5 * (attempt + 1))
                    continue
                r.raise_for_status()
                vecs = [d["embedding"] for d in r.json()["data"]]
                break
            except Exception as e:
                logger.warning(f"embedding tentativo {attempt}: {str(e)[:100]}")
                time.sleep(3)
        if not vecs or len(vecs) != len(batch):
            logger.warning(f"embedding non disponibile per {len(batch)} testi")
            vecs = [None] * len(batch)
        out.extend(vecs)
    return out


def embed_text(text: str):
    return embed_texts([text])[0]


def _vec_ok(v) -> bool:
    """Un vettore è utilizzabile solo se non è None e non è tutto zeri."""
    return bool(v) and any(x != 0 for x in v)


def _emb_json(text: str):
    """Embedding serializzato, o None se non calcolabile. Mai zeri finti in tabella."""
    try:
        v = embed_text(text)
    except Exception as e:
        logger.warning(f"embedding: {str(e)[:100]}")
        return None
    return json.dumps(v) if _vec_ok(v) else None


def cosine(a, b) -> float:
    import numpy as np
    a, b = np.array(a), np.array(b)
    n = np.linalg.norm(a) * np.linalg.norm(b)
    return 0.0 if n == 0 else float(np.dot(a, b) / n)


# ===========================================================================
# REPOSITORY (Delta, hive_metastore)
# ===========================================================================
def upsert_tickets(rows: list) -> int:
    """Idempotente. Deduplica per 'number' (la MERGE fallisce con chiavi duplicate
    nella sorgente) e scrive a batch (una VALUES da 50k righe non passa)."""
    if not rows:
        return 0
    seen, uniq = set(), []
    for r in rows:
        n = _s(r.get("number")).strip()
        if not n or n in seen:
            continue
        seen.add(n)
        r.setdefault("ingested_at", _now())
        uniq.append(r)
    batch = int(cfg("ingestion.merge_batch_size", 300))
    keep = bool(cfg("ingestion.preserve_on_empty", True))
    for i in range(0, len(uniq), batch):
        merge_values(table("tickets"), ["number"], uniq[i:i + batch], preserve_on_empty=keep)
    return len(uniq)


def get_ticket(number: str):
    df = run_sql(f"SELECT * FROM {table('tickets')} WHERE number = {sql_str(number)} LIMIT 1")
    return None if df.empty else df.iloc[0].to_dict()


def list_tickets(assignee: str = "", text: str = "", limit: int = 300,
                 hide_blocked: bool = False, include_closed: bool = False):
    """Elenco ticket. hide_blocked=True nasconde on hold/pending (Dashboard).
    include_closed=True mostra anche chiusi/risolti (ricerca in Repository)."""
    where = []
    if assignee and assignee not in ("Tutti",):
        if assignee == "Da assegnare":
            where.append("a.assignee IS NULL")
        else:
            where.append(f"a.assignee = {sql_str(assignee)}")
    if text:
        esc = text.replace("'", "''")
        where.append(f"(t.number LIKE '%{esc}%' OR t.short_description LIKE '%{esc}%')")
    if not include_closed:
        where.append(cl_not_closed("t.state"))
    if hide_blocked:
        where.append(_kw_clause("t.state", cfg("dashboard.hide_state_keywords", []), negate=True))
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    return run_sql(f"""
        SELECT t.number, t.short_description, t.state, t.priority,
               t.assignment_group, a.assignee, t.opened_at
        FROM {table('tickets')} t
        LEFT JOIN {table('ticket_assignment')} a ON t.number = a.number
        {clause} ORDER BY t.opened_at DESC NULLS LAST LIMIT {limit}
    """, max_rows=limit)


# ===========================================================================
# SAL — KPI e tabelle sulla finestra settimanale
# ===========================================================================
_SAL_COLS = """t.number, t.ticket_type, t.short_description, t.state, t.priority,
               a.assignee, t.opened_at, t.closed_at, t.updated_at"""


def _sal_from() -> str:
    return (f"FROM {table('tickets')} t "
            f"LEFT JOIN {table('ticket_assignment')} a ON t.number = a.number")


def sal_opened(start, end, limit: int = None):
    limit = int(limit or cfg("sal.max_rows", 500))
    return run_sql(f"""
        SELECT {_SAL_COLS} {_sal_from()}
        WHERE t.opened_at IS NOT NULL
          AND t.opened_at >= {ts_lit(start)} AND t.opened_at <= {ts_lit(end)}
        ORDER BY t.opened_at DESC LIMIT {limit}""", max_rows=limit)


def sal_closed_week(start, end, limit: int = None):
    limit = int(limit or cfg("sal.max_rows", 500))
    return run_sql(f"""
        SELECT {_SAL_COLS},
               ROUND((unix_timestamp(t.closed_at) - unix_timestamp(t.opened_at)) / 86400.0, 1) AS giorni
        {_sal_from()}
        WHERE t.closed_at IS NOT NULL
          AND t.closed_at >= {ts_lit(start)} AND t.closed_at <= {ts_lit(end)}
        ORDER BY t.closed_at DESC LIMIT {limit}""", max_rows=limit)


def sal_blocked_week(start, end, limit: int = None):
    """Blocked 'della settimana': attualmente on hold/pending E con attività nella
    finestra (aperti o aggiornati). ServiceNow non espone un 'on_hold_since' via
    l'API table: sys_updated_on è la proxy disponibile."""
    limit = int(limit or cfg("sal.max_rows", 500))
    s, e = ts_lit(start), ts_lit(end)
    return run_sql(f"""
        SELECT {_SAL_COLS} {_sal_from()}
        WHERE {cl_blocked('t.state')}
          AND ((t.opened_at >= {s} AND t.opened_at <= {e})
            OR (t.updated_at >= {s} AND t.updated_at <= {e}))
        ORDER BY t.updated_at DESC NULLS LAST LIMIT {limit}""", max_rows=limit)


def sal_blocked_all(limit: int = None):
    limit = int(limit or cfg("sal.max_rows", 500))
    return run_sql(f"""
        SELECT {_SAL_COLS},
               ROUND((unix_timestamp(current_timestamp()) - unix_timestamp(t.opened_at)) / 86400.0, 1) AS eta_gg
        {_sal_from()}
        WHERE {cl_blocked('t.state')}
        ORDER BY t.opened_at ASC NULLS LAST LIMIT {limit}""", max_rows=limit)


def sal_kpi(start, end) -> dict:
    """Un solo passaggio sulla tabella: 8 KPI in una query."""
    s, e = ts_lit(start), ts_lit(end)
    nc, bl = cl_not_closed("state"), cl_blocked("state")
    aging = int(cfg("sal.aging_alert_days", 30))
    df = run_sql(f"""
        SELECT
          SUM(CASE WHEN opened_at BETWEEN {s} AND {e} THEN 1 ELSE 0 END) AS aperti,
          SUM(CASE WHEN closed_at BETWEEN {s} AND {e} THEN 1 ELSE 0 END) AS chiusi,
          SUM(CASE WHEN {bl} AND (opened_at BETWEEN {s} AND {e}
                               OR updated_at BETWEEN {s} AND {e}) THEN 1 ELSE 0 END) AS blocked_sett,
          SUM(CASE WHEN {bl} THEN 1 ELSE 0 END) AS blocked_tot,
          SUM(CASE WHEN {nc} THEN 1 ELSE 0 END) AS backlog,
          SUM(CASE WHEN {nc} AND opened_at < current_timestamp() - INTERVAL {aging} DAYS
                   THEN 1 ELSE 0 END) AS backlog_vecchio,
          AVG(CASE WHEN closed_at BETWEEN {s} AND {e} AND opened_at IS NOT NULL
                   THEN (unix_timestamp(closed_at) - unix_timestamp(opened_at)) / 86400.0 END) AS mttr_gg,
          SUM(CASE WHEN {nc} AND opened_at IS NULL THEN 1 ELSE 0 END) AS senza_data
        FROM {table('tickets')}""")
    if df is None or df.empty:
        return {}
    r = df.iloc[0].to_dict()
    out = {k: (0 if r.get(k) is None else r.get(k)) for k in r}
    out["net_flow"] = int(out.get("aperti", 0)) - int(out.get("chiusi", 0))
    return out


def next_analysis_version(number: str) -> int:
    df = run_sql(f"SELECT COALESCE(MAX(version),0)+1 v FROM {table('ticket_analysis')} "
                 f"WHERE number = {sql_str(number)}")
    return int(df.iloc[0]["v"]) if not df.empty else 1


def save_analysis_draft(ticket: dict, parsed: dict) -> dict:
    number = ticket.get("number", "")
    row = {
        "analysis_id": parsed.get("analysis_id") or str(uuid.uuid4()),
        "number": number, "version": next_analysis_version(number), "status": "draft",
        "summary": parsed.get("summary", ""), "problem_type": parsed.get("problem_type", "OTHER"),
        "severity": parsed.get("severity", "MEDIUM"), "hypothesis": parsed.get("hypothesis", "[]"),
        "steps": parsed.get("steps", "[]"), "proposed_solution": parsed.get("proposed_solution", ""),
        "sql_commands": parsed.get("sql_commands", "[]"),
        "root_cause_confirmed": bool(parsed.get("root_cause_confirmed", False)),
        "evidence": parsed.get("evidence", ""), "estimated_effort": parsed.get("estimated_effort", ""),
        "confidence": float(parsed.get("confidence", 0.0)), "reasoning": parsed.get("reasoning", ""),
        "sources_used": parsed.get("sources_used", "[]"),
        "model_name": cfg("models.chat_primary"), "created_by": ACTOR,
        "created_at": parsed.get("created_at") or _now(),
    }
    insert_values(table("ticket_analysis"), [row])
    audit("analysis", row["analysis_id"], "create", {"number": number})
    return row


def get_latest_analysis(number: str):
    df = run_sql(f"SELECT * FROM {table('ticket_analysis')} WHERE number = {sql_str(number)} "
                 f"ORDER BY version DESC LIMIT 1")
    return None if df.empty else df.iloc[0].to_dict()


def set_analysis_status(analysis_id: str, status: str, notes: str = ""):
    exec_sql(f"""UPDATE {table('ticket_analysis')} SET status = {sql_str(status)},
        reviewed_by = {sql_str(ACTOR)}, reviewed_at = current_timestamp(),
        review_notes = {sql_str(notes)} WHERE analysis_id = {sql_str(analysis_id)}""")
    audit("analysis", analysis_id, status, {"notes": notes})


def approve_analysis(number: str) -> str:
    a = get_latest_analysis(number)
    if not a:
        return f"Nessuna analisi per {number}."
    set_analysis_status(a["analysis_id"], "approved")
    content = "\n".join(filter(None, [a.get("summary", ""), a.get("evidence", ""),
                                      a.get("proposed_solution", "")]))
    emb = _emb_json(f"{a.get('summary','')} {a.get('evidence','')}")
    merge_values(table("kb_entries"), ["entry_id"], [{
        "entry_id": str(uuid.uuid4()), "number": number, "title": a.get("summary", "")[:120],
        "problem_type": a.get("problem_type", "OTHER"), "content": content,
        "proposed_solution": a.get("proposed_solution", ""), "evidence": a.get("evidence", ""),
        "embedding": emb, "approved_by": ACTOR, "approved_at": _now()}])
    return f"Analisi di {number} approvata e aggiunta alla Knowledge Base."


def teach_correction(number: str, root_cause: str, solution: str, problem_type: str = "OTHER") -> str:
    """Salva una correzione dell'operatore come conoscenza validata (kb_entries),
    così i ticket simili successivi la recuperano. È l'insegnamento esplicito."""
    content = "\n".join(filter(None, [f"Root cause: {root_cause}" if root_cause else "",
                                      f"Soluzione: {solution}" if solution else ""]))
    if not content.strip():
        raise ValueError("Inserisci almeno la root cause o la soluzione corretta.")
    emb = _emb_json(content)
    merge_values(table("kb_entries"), ["entry_id"], [{
        "entry_id": str(uuid.uuid4()), "number": number,
        "title": f"Correzione {number}: {(root_cause or solution)[:80]}",
        "problem_type": problem_type, "content": content,
        "proposed_solution": solution or "", "evidence": root_cause or "",
        "embedding": emb, "approved_by": ACTOR, "approved_at": _now()}])
    audit("analysis", number, "teach", {"root_cause": (root_cause or "")[:100]})
    return f"Correzione di {number} salvata nella knowledge base."


def append_chat(number: str, role: str, message: str, sources=None):
    insert_values(table("ticket_chat"), [{
        "chat_id": str(uuid.uuid4()), "number": number, "role": role, "message": message,
        "sources_used": json.dumps(sources or [], ensure_ascii=False), "created_at": _now()}])


def get_chat_history(number: str, limit: int = 100):
    df = run_sql(f"SELECT role, message FROM {table('ticket_chat')} "
                 f"WHERE number = {sql_str(number)} ORDER BY created_at ASC LIMIT {limit}")
    return df.to_dict("records") if not df.empty else []


def assign(number: str, assignee: str):
    merge_values(table("ticket_assignment"), ["number"], [{
        "number": number, "assignee": assignee, "assigned_by": ACTOR, "assigned_at": _now()}])
    audit("ticket", number, "assign", {"assignee": assignee})


def audit(entity: str, entity_id: str, action: str, detail: dict):
    try:
        insert_values(table("audit_log"), [{
            "event_id": str(uuid.uuid4()), "entity": entity, "entity_id": entity_id,
            "action": action, "actor": ACTOR,
            "detail": json.dumps(detail, ensure_ascii=False, default=str), "event_at": _now()}])
    except Exception:
        pass


def record_token_usage(model: str, it: int, ot: int):
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        exec_sql(f"""MERGE INTO {table('token_usage')} AS t
            USING (SELECT DATE('{today}') usage_date, {sql_str(model)} model_name,
                   {int(it)} it, {int(ot)} ot) AS s
            ON t.usage_date = s.usage_date AND t.model_name = s.model_name
            WHEN MATCHED THEN UPDATE SET t.input_tokens = t.input_tokens + s.it,
                t.output_tokens = t.output_tokens + s.ot, t.calls = t.calls + 1
            WHEN NOT MATCHED THEN INSERT (usage_date, model_name, input_tokens, output_tokens, calls)
                VALUES (s.usage_date, s.model_name, s.it, s.ot, 1)""")
    except Exception:
        pass


# ===========================================================================
# LLM (Model Serving)
# ===========================================================================
_MODEL = {"resolved": None}


def _llm_client():
    from openai import OpenAI
    # timeout per-chiamata esplicito + niente retry interni (li gestiamo noi): un
    # endpoint che si impunta viene mollato in fretta invece di attendere minuti.
    return OpenAI(api_key=get_token(), base_url=f"{get_host()}/serving-endpoints",
                  timeout=float(cfg("models.request_timeout_s", 60)), max_retries=0)


def llm_chat(messages, tools=None, response_format=None, max_tokens=None):
    max_tokens = max_tokens or cfg("models.max_tokens", 4000)
    cands = [cfg("models.chat_primary")] + list(cfg("models.chat_fallbacks", []))
    if _MODEL["resolved"]:
        cands = [_MODEL["resolved"]] + cands
    last = None
    attempts = int(cfg("models.max_attempts", 2))
    for model in dict.fromkeys(m for m in cands if m):
        for attempt in range(attempts):
            try:
                kw = dict(model=model, messages=messages, max_tokens=max_tokens,
                          temperature=cfg("models.temperature", 0.0))
                if tools:
                    kw["tools"] = tools; kw["tool_choice"] = "auto"
                if response_format:
                    kw["response_format"] = response_format
                resp = _llm_client().chat.completions.create(**kw)
                _MODEL["resolved"] = model
                _track(model, resp)
                return resp
            except Exception as e:
                last = e
                if "404" in str(e) or "not found" in str(e).lower():
                    break
                logger.warning(f"chat retry {attempt} {model}: {str(e)[:120]}")
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Nessun endpoint chat disponibile: {last}")


def _track(model, resp):
    try:
        u = getattr(resp, "usage", None)
        if u:
            record_token_usage(model, getattr(u, "prompt_tokens", 0) or 0,
                               getattr(u, "completion_tokens", 0) or 0)
    except Exception:
        pass


# ===========================================================================
# RAG (come la V1): semantico su kb_entries validate + fallback keyword
# ===========================================================================
def build_context(query: str, exclude_number: str = ""):
    top_k = cfg("rag.top_k", 3)
    min_score = cfg("rag.min_score", 0.30)
    parts, sources = [], []
    used = False
    try:
        df = run_sql(f"SELECT number, title, content, proposed_solution, evidence, embedding "
                     f"FROM {table('kb_entries')} WHERE embedding IS NOT NULL")
    except Exception:
        df = None
    if df is not None and not df.empty:
        try:
            qv = embed_text(query)
            if _vec_ok(qv):
                df = df[df["number"] != exclude_number].copy()
                df["vec"] = df["embedding"].apply(lambda x: json.loads(x) if x else None)
                df = df[df["vec"].notna()]
                if not df.empty:
                    df["score"] = df["vec"].apply(lambda v: cosine(qv, v))
                    top = df.nlargest(top_k, "score")
                    top = top[top["score"] > min_score]
                    if not top.empty:
                        parts.append("TICKET SIMILI VALIDATI (semantico):")
                        for _, r in top.iterrows():
                            parts.append(f"- [{r['number']} sim={r['score']:.2f}] {str(r['content'])[:200]}")
                            sources.append({"kind": "ticket", "ref": r["number"], "score": round(float(r["score"]), 3)})
                        used = True
        except Exception as e:
            logger.warning(f"RAG semantico: {str(e)[:120]}")
    if not used:
        for r in _keyword_fallback(query, exclude_number, top_k):
            if not any(p.startswith("TICKET SIMILI") for p in parts):
                parts.append("TICKET SIMILI VALIDATI (match testuale):")
            parts.append(f"- [{r.get('number')}] {str(r.get('content',''))[:200]}")
            sources.append({"kind": "ticket", "ref": r.get("number"), "score": None})

    # DOCUMENTAZIONE: i documenti RILEVANTI (già filtrati per similarità da
    # search_knowledge) vengono iniettati PER INTERO, così l'agente legge tutto il
    # documento e non solo l'inizio. Unico limite: un budget complessivo di caratteri
    # (rag.doc_char_budget) per non gonfiare il contesto se ci sono più documenti grossi.
    budget = int(cfg("rag.doc_char_budget", 100000))
    used = 0
    for title, content, score in search_knowledge(query):
        if used >= budget:
            break
        if not any(p.startswith("DOCUMENTAZIONE") for p in parts):
            parts.append("\nDOCUMENTAZIONE PERTINENTE (testo integrale):")
        tag = f" sim={score:.2f}" if score is not None else ""
        body = str(content)
        if len(body) > budget - used:
            body = body[:budget - used] + "\n[...documento troncato: limite di contesto raggiunto...]"
        used += len(body)
        parts.append(f"\n--- DOCUMENTO: {title}{tag} ---\n{body}\n--- fine documento ---")
        sources.append({"kind": "documento", "ref": title,
                        "score": round(score, 3) if score is not None else None})
    return "\n".join(parts), sources


# --- STORICO / DOCUMENTAZIONE (knowledge_docs) -------------------------------
def _chunks(text: str) -> list:
    """Spezza il testo in blocchi con sovrapposizione, tagliando su un confine di riga
    o di parola: un concetto a cavallo di due chunk resta leggibile in almeno uno."""
    size = max(int(cfg("rag.chunk_chars", 3000)), 500)
    ov = min(max(int(cfg("rag.chunk_overlap", 300)), 0), size // 2)
    t = text or ""
    if len(t) <= size:
        return [t]
    out, start = [], 0
    while start < len(t):
        end = min(start + size, len(t))
        if end < len(t):
            br = max(t.rfind("\n", start + size // 2, end),
                     t.rfind(" ", start + size // 2, end))
            if br > start:
                end = br
        out.append(t[start:end])
        if end >= len(t):
            break
        start = max(end - ov, start + 1)
    return out


def _chunk_sig() -> str:
    """Firma della configurazione di indicizzazione. Se cambia, i chunk già scritti sono
    stantii: il reindex lo deduce dalla firma, senza che nessuno debba ricordarsene."""
    return (f'{cfg("rag.chunk_chars", 3000)}:{cfg("rag.chunk_overlap", 300)}:'
            f'{cfg("models.embedding_endpoint")}')


def _index_document(doc_id: str, title: str, content: str) -> tuple:
    """(Ri)costruisce i chunk di un documento. Ritorna (chunk_con_embedding, chunk_totali)
    così il chiamante può DIRE all'operatore se l'indicizzazione è riuscita davvero."""
    try:
        exec_sql(f"DELETE FROM {table('knowledge_chunks')} WHERE doc_id = {sql_str(doc_id)}")
    except Exception as e:
        logger.warning(f"pulizia chunk: {str(e)[:120]}")
    parts, sig = _chunks(content), _chunk_sig()
    vecs = embed_texts([f"{title}\n{p}" for p in parts])
    rows = [{"chunk_id": str(uuid.uuid4()), "doc_id": doc_id, "chunk_index": i,
             "title": title, "content": p,
             "embedding": json.dumps(v) if _vec_ok(v) else None,
             "chunk_sig": sig, "created_at": _now()}
            for i, (p, v) in enumerate(zip(parts, vecs))]
    # batch piccolo: la VALUES contiene il testo dei chunk, non solo metadati.
    for i in range(0, len(rows), 50):
        insert_values(table("knowledge_chunks"), rows[i:i + 50])
    _kb_index_invalidate()
    return sum(1 for r in rows if r["embedding"]), len(rows)


def kb_add_document(title: str, content: str, tags: str = "") -> str:
    if not (content or "").strip():
        raise ValueError("Contenuto vuoto.")
    doc_id, title = str(uuid.uuid4()), (title or "(senza titolo)")
    insert_values(table("knowledge_docs"), [{
        "doc_id": doc_id, "title": title, "content": content, "tags": tags or "",
        # colonna mantenuta per retrocompatibilità; la ricerca usa knowledge_chunks.
        "embedding": _emb_json(f"{title}\n{content[:4000]}"),
        "created_by": ACTOR, "created_at": _now()}])
    ok, tot = _index_document(doc_id, title, content)
    if ok == 0:
        return (f"Documento salvato ma NON indicizzato ({tot} chunk senza embedding): "
                f"l'endpoint di embedding non ha risposto. È raggiungibile solo per "
                f"parola chiave finché non premi «Reindicizza mancanti».")
    if ok < tot:
        return f"Documento aggiunto: {ok}/{tot} chunk indicizzati (alcuni embedding falliti)."
    return f"Documento aggiunto ({tot} chunk indicizzati)."


def kb_stale_docs(limit: int = 2000):
    """Documenti da (ri)indicizzare: senza chunk validi (endpoint di embedding non
    disponibile al caricamento, o documenti anteriori al chunking) oppure indicizzati
    con una configurazione diversa da quella attuale."""
    sig = _chunk_sig()
    return run_sql(f"""
        SELECT d.doc_id, d.title, d.content
        FROM {table('knowledge_docs')} d
        LEFT JOIN (SELECT doc_id, COUNT(*) AS n, MAX(chunk_sig) AS sig
                   FROM {table('knowledge_chunks')}
                   WHERE embedding IS NOT NULL GROUP BY doc_id) c
               ON d.doc_id = c.doc_id
        WHERE COALESCE(c.n, 0) = 0 OR COALESCE(c.sig, '') <> {sql_str(sig)}
        LIMIT {limit}""", max_rows=limit)


def kb_reindex(only_stale: bool = True) -> str:
    """Ricostruisce l'indice dei chunk. only_stale=True (default) tocca solo i documenti
    che ne hanno bisogno: è sicuro rilanciarla a vuoto quanto si vuole. only_stale=False
    ricostruisce tutto da capo."""
    if only_stale:
        df = kb_stale_docs()
    else:
        df = run_sql(f"SELECT doc_id, title, content FROM {table('knowledge_docs')}",
                     max_rows=2000)
    if df is None or df.empty:
        return "Nessun documento da reindicizzare."
    ok = tot = 0
    for _, r in df.iterrows():
        a, b = _index_document(_s(r["doc_id"]), _s(r["title"]), _s(r["content"]))
        ok += a; tot += b
    return f"Reindicizzati {len(df)} documenti: {ok}/{tot} chunk con embedding."


_REINDEX = {"started": False}


def start_reindex_worker():
    """Riparazione automatica: ogni N secondi reindicizza i soli documenti stantii.
    Tocca esclusivamente documenti che la ricerca semantica GIÀ non vede, quindi non può
    peggiorare una risposta in corso. Idempotente: a regime non fa nulla."""
    import threading, random
    every = int(cfg("rag.reindex_every_s", 900))
    if every <= 0 or _REINDEX["started"]:
        return
    _REINDEX["started"] = True

    def loop():
        time.sleep(random.uniform(10, 60))          # sfasa le repliche dell'App
        while True:
            try:
                df = kb_stale_docs(limit=int(cfg("rag.reindex_batch", 20)))
                if df is not None and not df.empty:
                    ok = tot = 0
                    for _, r in df.iterrows():
                        a, b = _index_document(_s(r["doc_id"]), _s(r["title"]),
                                               _s(r["content"]))
                        ok += a; tot += b
                    logger.info(f"reindex automatico: {len(df)} documenti, "
                                f"{ok}/{tot} chunk con embedding")
            except Exception as e:
                logger.warning(f"reindex automatico: {str(e)[:150]}")
            time.sleep(every * random.uniform(1.0, 1.2))

    threading.Thread(target=loop, daemon=True, name="kb-reindex").start()


# --- import di notebook dal workspace come documentazione ---------------------
def _nb_source(path: str) -> str:
    """Sorgente di un notebook, nel formato SOURCE (il testo che si vede
    nell'editor, separatori di cella compresi). Stesso contratto di
    _extract_file_text: non solleva, e in caso di errore ritorna un messaggio
    marcato con EXTRACT_FAIL, così il chiamante non lo scambia per contenuto.
    A differenza di _t_read_nb (che tronca a 8000 per non gonfiare il contesto
    dell'agente) qui il testo serve INTERO: è documentazione da indicizzare."""
    if not _path_ok(path):
        allowed = ", ".join(cfg("agent_tools.allowed_workspace_paths", [])) or "(nessuno)"
        return _fail(f"Path non consentito: '{path}'. Consentiti: {allowed} "
                     f"(vedi agent_tools.allowed_workspace_paths).")
    try:
        from databricks.sdk.service.workspace import ExportFormat
        exp = wc().workspace.export(path=path, format=ExportFormat.SOURCE)
        txt = base64.b64decode(exp.content).decode("utf-8", errors="replace").strip()
    except Exception as e:
        return _fail(f"Notebook non leggibile '{path}': {str(e)[:150]}")
    return txt or _fail(f"Notebook vuoto: '{path}'.")


def _ws_kind(obj) -> str:
    """Tipo di oggetto del workspace come stringa. L'SDK ritorna un enum
    (ObjectType.DIRECTORY), ma versioni diverse lo serializzano diversamente:
    confrontare il testo evita di dipendere dalla forma esatta dell'enum."""
    return _s(getattr(obj, "object_type", "")).upper()


def _expand_notebook_paths(raw: str) -> tuple:
    """Da un elenco di path (uno per riga) alla lista dei notebook da importare:
    le cartelle vengono espanse ricorsivamente. Ritorna (paths, note): le note
    spiegano all'operatore cosa è stato saltato e perché, invece di sparire."""
    cap = int(cfg("rag.notebook_import_max", 50))
    out, notes = [], []
    for p in [x.strip() for x in (raw or "").splitlines() if x.strip()]:
        try:
            info = wc().workspace.get_status(p)
        except Exception as e:
            notes.append(f"'{p}' non trovato ({str(e)[:60]})")
            continue
        if "DIRECTORY" in _ws_kind(info) or "REPO" in _ws_kind(info):
            try:
                found = [o.path for o in wc().workspace.list(p, recursive=True)
                         if "NOTEBOOK" in _ws_kind(o)]
            except Exception as e:
                notes.append(f"cartella '{p}' non elencabile ({str(e)[:60]})")
                continue
            if not found:
                notes.append(f"nessun notebook in '{p}'")
            out += found
        else:
            out.append(_s(getattr(info, "path", "")) or p)
    out = list(dict.fromkeys(out))          # stesso notebook indicato due volte
    if len(out) > cap:
        notes.append(f"trovati {len(out)} notebook, importati i primi {cap} "
                     f"(alza rag.notebook_import_max)")
        out = out[:cap]
    return out, notes


def _kb_doc_ids_by_title(title: str) -> list:
    try:
        df = run_sql(f"SELECT doc_id FROM {table('knowledge_docs')} "
                     f"WHERE title = {sql_str(title)}", max_rows=100)
    except Exception as e:
        logger.warning(f"ricerca documento per titolo: {str(e)[:120]}")
        return []
    return [] if df is None or df.empty else [_s(r["doc_id"]) for _, r in df.iterrows()]


def _kb_docs_without_chunks(titles: list) -> int:
    """Quanti dei documenti indicati non hanno NEMMENO un chunk con embedding.
    Lo leggiamo dai dati invece che dai messaggi di kb_add_document: così l'esito
    che riportiamo all'operatore è quello vero, non una stringa da interpretare."""
    if not titles:
        return 0
    lst = ", ".join(sql_str(t) for t in titles)
    try:
        df = run_sql(f"""
            SELECT COUNT(*) AS n
            FROM {table('knowledge_docs')} d
            LEFT JOIN (SELECT doc_id, COUNT(*) AS c FROM {table('knowledge_chunks')}
                       WHERE embedding IS NOT NULL GROUP BY doc_id) k
                   ON d.doc_id = k.doc_id
            WHERE d.title IN ({lst}) AND COALESCE(k.c, 0) = 0""")
        return 0 if df is None or df.empty else int(df.iloc[0]["n"])
    except Exception as e:
        logger.warning(f"conteggio documenti non indicizzati: {str(e)[:120]}")
        return 0


def kb_add_notebooks(raw_paths: str, tags: str = "") -> str:
    """Importa notebook del workspace come documenti normali: da qui in poi sono
    documentazione a tutti gli effetti (chunk, ricerca, iniezione nel contesto).
    Il TITOLO è il path: è l'identità del notebook, quindi reimportarlo dopo una
    modifica SOSTITUISCE la versione precedente invece di lasciare due copie
    quasi identiche a contendersi i primi posti nella ricerca."""
    paths, notes = _expand_notebook_paths(raw_paths)
    if not paths:
        raise ValueError("; ".join(notes) or "Nessun notebook nei path indicati.")
    done = []
    for p in paths:
        src = _nb_source(p)
        if extraction_failed(src):
            notes.append(extraction_reason(src))
            continue
        for doc_id in _kb_doc_ids_by_title(p):
            delete_knowledge(doc_id)
        kb_add_document(p, src, tags or "")
        done.append(p)
    if not done:
        raise ValueError("; ".join(notes[:5]) or "Nessun notebook importato.")
    msg = f"Importati {len(done)} notebook su {len(paths)}."
    muti = _kb_docs_without_chunks(done)
    if muti:
        msg += (f" {muti} senza embedding: raggiungibili solo per parola chiave "
                f"finché non premi «Reindicizza mancanti».")
    return msg + (" Note: " + "; ".join(notes[:5]) if notes else "")


def list_knowledge(limit: int = 200):
    return run_sql(f"SELECT title, tags, length(content) AS caratteri, created_by, created_at "
                   f"FROM {table('knowledge_docs')} ORDER BY created_at DESC LIMIT {limit}", max_rows=limit)


def knowledge_choices(limit: int = 500):
    """Elenco (etichetta, doc_id) per il menu a tendina di consultazione/eliminazione."""
    try:
        df = run_sql(f"SELECT doc_id, title, created_at FROM {table('knowledge_docs')} "
                     f"ORDER BY created_at DESC LIMIT {limit}", max_rows=limit)
    except Exception:
        return []
    if df is None or df.empty:
        return []
    return [(f"{_s(r['title'])[:70]} — {_s(r['created_at'])[:19]}", _s(r["doc_id"]))
            for _, r in df.iterrows()]


def get_knowledge_doc(doc_id: str):
    df = run_sql(f"SELECT title, tags, content FROM {table('knowledge_docs')} "
                 f"WHERE doc_id = {sql_str(doc_id)} LIMIT 1")
    return None if df.empty else df.iloc[0].to_dict()


def delete_knowledge(doc_id: str):
    exec_sql(f"DELETE FROM {table('knowledge_chunks')} WHERE doc_id = {sql_str(doc_id)}")
    exec_sql(f"DELETE FROM {table('knowledge_docs')} WHERE doc_id = {sql_str(doc_id)}")
    _kb_index_invalidate()
    audit("knowledge", doc_id, "delete", {})


_KB_IDX = {"rows": None, "at": 0.0}


def _kb_index_invalidate():
    _KB_IDX["rows"], _KB_IDX["at"] = None, 0.0


def _kb_index():
    """Vettori dei chunk in cache di processo. Senza cache ogni analisi e ogni messaggio
    di chat riscaricherebbero l'intero indice (circa 12 KB per chunk)."""
    ttl = int(cfg("rag.index_cache_s", 300))
    if _KB_IDX["rows"] is not None and (time.time() - _KB_IDX["at"]) < ttl:
        return _KB_IDX["rows"]
    cap = int(cfg("rag.max_index_rows", 50000))
    try:
        df = run_sql(f"SELECT doc_id, embedding FROM {table('knowledge_chunks')} "
                     f"WHERE embedding IS NOT NULL LIMIT {cap}", max_rows=cap)
    except Exception as e:
        logger.warning(f"indice chunk non leggibile: {str(e)[:120]}")
        return []
    rows = []
    if df is not None and not df.empty:
        if len(df) >= cap:
            logger.warning(f"indice troncato a {cap} chunk: alza rag.max_index_rows")
        for _, r in df.iterrows():
            try:
                rows.append((_s(r["doc_id"]), json.loads(r["embedding"])))
            except Exception:
                continue
    _KB_IDX["rows"], _KB_IDX["at"] = rows, time.time()
    return rows


def search_knowledge(query: str, top_k: int = None) -> list:
    """Ritorna [(title, content, score|None)] con il testo INTEGRALE dei documenti più
    pertinenti. La similarità è calcolata sui CHUNK e collassata per documento (vince il
    chunk migliore), così anche il centro di un documento lungo è raggiungibile."""
    top_k = int(top_k or cfg("rag.top_k", 3))
    min_score = float(cfg("rag.min_score", 0.30))

    qv = None
    try:
        qv = embed_text(query)
    except Exception as e:
        logger.warning(f"embedding query: {str(e)[:120]}")

    best = {}
    if _vec_ok(qv):
        for doc_id, vec in _kb_index():
            try:
                s = cosine(qv, vec)
            except Exception:
                continue
            if s > best.get(doc_id, -1.0):
                best[doc_id] = s
    hits = [(d, s) for d, s in sorted(best.items(), key=lambda x: -x[1])[:top_k]
            if s > min_score]

    out, seen = [], set()
    if hits:
        ids = ", ".join(sql_str(d) for d, _ in hits)
        try:
            df = run_sql(f"SELECT doc_id, title, content FROM {table('knowledge_docs')} "
                         f"WHERE doc_id IN ({ids})")
            by_id = {_s(r["doc_id"]): r for _, r in df.iterrows()}
            for d, s in hits:
                if d in by_id:
                    out.append((by_id[d]["title"], by_id[d]["content"], float(s)))
                    seen.add(d)
        except Exception as e:
            logger.warning(f"lettura documenti: {str(e)[:120]}")

    # Fallback keyword IN SQL, attivo ogni volta che i risultati semantici non bastano
    # (non solo quando sono zero): è ciò che rende di nuovo raggiungibili i documenti
    # con embedding mancante, che la ricerca semantica non vedrebbe MAI.
    if len(out) < top_k:
        kws = list(dict.fromkeys(re.findall(r"[a-zà-ù0-9_]{5,}", query.lower())))[:4]
        if kws:
            likes = " OR ".join(f"lower(content) LIKE '%{k}%'" for k in kws)
            try:
                df = run_sql(f"SELECT doc_id, title, content FROM {table('knowledge_docs')} "
                             f"WHERE {likes} LIMIT {top_k * 3}", max_rows=top_k * 3)
                for _, r in df.iterrows():
                    if _s(r["doc_id"]) in seen:
                        continue
                    out.append((r["title"], r["content"], None))
                    seen.add(_s(r["doc_id"]))
                    if len(out) >= top_k:
                        break
            except Exception as e:
                logger.warning(f"fallback keyword: {str(e)[:120]}")
    return out[:top_k]


def _docx_to_text(path: str) -> str:
    """Estrae il testo da un .docx includendo ANCHE le tabelle (le celle), in ordine di
    lettura. Di default python-docx espone solo i paragrafi: le tabelle andrebbero perse."""
    import docx
    from docx.oxml.table import CT_Tbl
    from docx.oxml.text.paragraph import CT_P
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    doc = docx.Document(path)
    out = []
    for child in doc.element.body.iterchildren():
        if isinstance(child, CT_P):
            t = Paragraph(child, doc).text
            if t and t.strip():
                out.append(t)
        elif isinstance(child, CT_Tbl):
            for row in Table(child, doc).rows:
                seen, cells = set(), []
                for c in row.cells:              # celle unite: evita ripetizioni
                    tx = c.text.strip()
                    if tx and id(c._tc) not in seen:
                        seen.add(id(c._tc)); cells.append(tx)
                if cells:
                    out.append(" | ".join(cells))
    return "\n".join(out)


def _pptx_to_text(path: str) -> str:
    """Testo di un .pptx: testo delle slide, tabelle e note."""
    from pptx import Presentation
    prs = Presentation(path); out = []
    for i, slide in enumerate(prs.slides, 1):
        out.append(f"# Slide {i}")
        for shp in slide.shapes:
            if shp.has_text_frame:
                for p in shp.text_frame.paragraphs:
                    t = ("".join(r.text for r in p.runs) or p.text).strip()
                    if t:
                        out.append(t)
            if getattr(shp, "has_table", False):
                for row in shp.table.rows:
                    cells = [c.text.strip() for c in row.cells]
                    if any(cells):
                        out.append(" | ".join(cells))
        try:
            if slide.has_notes_slide:
                n = (slide.notes_slide.notes_text_frame.text or "").strip()
                if n:
                    out.append(f"[Note: {n}]")
        except Exception:
            pass
    return "\n".join(out)


def _html_to_text(raw: str) -> str:
    txt = re.sub(r"(?is)<(script|style)\b.*?>.*?</\1>", " ", raw)
    txt = re.sub(r"(?s)<[^>]+>", " ", txt)
    return re.sub(r"[ \t]{2,}", " ", html.unescape(txt)).strip()


def _bytes_texty(b: bytes) -> bool:
    """I dati sembrano testo? (niente byte null, pochi caratteri di controllo). Sui
    byte grezzi, così un binario non passa mascherato da caratteri di sostituzione."""
    if not b:
        return True
    if b"\x00" in b:
        return False
    ctrl = sum(1 for c in b if c < 9 or 13 < c < 32)
    return ctrl / len(b) < 0.05


def _try_markitdown(path: str):
    """Motore universale OPZIONALE: se markitdown è installato, converte quasi qualsiasi
    formato (anche .doc, email, immagini con OCR). Se non c'è, ritorna None."""
    try:
        from markitdown import MarkItDown
        return (MarkItDown().convert(path).text_content or "").strip() or None
    except Exception:
        return None


def _extract_file_text(path: str) -> str:
    """Estrattore UNICO e difensivo: prova il metodo migliore per estensione, con
    fallback, senza mai restituire binario illeggibile né sollevare eccezioni verso il
    chiamante. Usato sia per la documentazione sia per gli allegati ai ticket."""
    ext = os.path.splitext(path)[1].lower()
    name = os.path.basename(path)

    def _read_text():
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    try:
        if ext in ATTACH_TEXT_EXT:                       # txt/csv/tsv/json/md/log/...
            return _read_text()
        if ext == ".docx":
            return _docx_to_text(path)
        if ext == ".pptx":
            return _pptx_to_text(path)
        if ext in (".xlsx", ".xls", ".xlsm"):
            import pandas as pd
            xls = pd.ExcelFile(path)
            return "\n\n".join(f"# Foglio: {n}\n{xls.parse(n, nrows=1000).to_csv(index=False)}"
                               for n in xls.sheet_names)
        if ext == ".pdf":
            from pypdf import PdfReader
            txt = "\n".join((p.extract_text() or "") for p in PdfReader(path).pages).strip()
            if txt:
                return txt
            return (_try_markitdown(path) or
                    _fail(f"PDF '{name}' senza testo estraibile (probabile scansione o "
                          f"immagine). Fornisci una versione con testo, oppure allega gli "
                          f"screenshot direttamente nella chat."))
        if ext in (".html", ".htm"):
            return _html_to_text(_read_text())
        # formato non gestito qui: motore universale se presente, altrimenti testo se
        # sembra tale, altrimenti messaggio chiaro (mai binario 'sporco').
        mk = _try_markitdown(path)
        if mk:
            return mk
        with open(path, "rb") as fh:
            head = fh.read(8192)
        if _bytes_texty(head):
            return _read_text()
        return _fail(f"Formato non estraibile come testo: '{name}'. Supportati: testo/"
                     f"markdown/csv/json/log, PDF, Word (.docx), PowerPoint (.pptx), "
                     f"Excel, HTML. Per immagini/screenshot allegali nella chat.")
    except Exception as e:
        return _try_markitdown(path) or _fail(f"Impossibile leggere '{name}': {str(e)[:150]}")


def _keyword_fallback(query: str, exclude_number: str = "", top_k: int = 3) -> list:
    kws = list(dict.fromkeys(re.findall(r"[a-zà-ù0-9_]{5,}", query.lower())))[:4]
    if not kws:
        return []
    likes = " OR ".join(f"lower(content) LIKE '%{w}%'" for w in kws)
    excl = f"AND number <> {sql_str(exclude_number)}" if exclude_number else ""
    try:
        df = run_sql(f"SELECT number, title, content, proposed_solution FROM {table('kb_entries')} "
                     f"WHERE ({likes}) {excl} LIMIT {top_k}")
        return df.to_dict("records") if not df.empty else []
    except Exception:
        return []


# ===========================================================================
# TOOL AGENTE (read-only) + loop
# ===========================================================================
def _readonly(sql: str) -> bool:
    up = sql.upper()
    return not any(f in up.split() or up.strip().startswith(f) for f in cfg("agent_tools.forbidden_sql", []))


def _t_run_sql(a):
    q = a.get("query", "")
    if not _readonly(q):
        return "ERRORE: solo query in sola lettura."
    try:
        df = run_sql(q, max_rows=200)
        return df.to_csv(index=False) if not df.empty else "(nessuna riga)"
    except Exception as e:
        return f"ERRORE: {str(e)[:300]}"


def _t_describe(a):
    try:
        return run_sql(f"DESCRIBE {a['table_name']}", max_rows=500).to_csv(index=False)
    except Exception as e:
        return f"ERRORE: {str(e)[:200]}"


def _all_schemas() -> list:
    """Tutti gli schemi/database del metastore (per la modalità 'ovunque')."""
    try:
        df = run_sql("SHOW DATABASES", max_rows=10000)
        return [str(x) for x in df[df.columns[0]].tolist()]
    except Exception:
        return []


def _explorable_schemas() -> list:
    s = cfg("agent_tools.explorable_schemas", [])
    return _all_schemas() if (not s or "*" in s) else s


def _t_search_tables(a):
    kw, hits = a.get("keyword", ""), []
    schemas = _explorable_schemas()
    cap = int(cfg("agent_tools.max_scan_schemas", 30))
    scanned, truncated = 0, False
    for sch in schemas:
        if scanned >= cap:
            truncated = True
            break
        scanned += 1
        try:
            df = run_sql(f"SHOW TABLES IN {sch}", max_rows=1000)
            col = "tableName" if "tableName" in df.columns else df.columns[-1]
            hits += [f"{sch}.{t}" for t in df[col].tolist() if kw.lower() in str(t).lower()]
        except Exception:
            continue
        if len(hits) >= 50:
            break
    out = "\n".join(hits[:50]) or f"(nessuna tabella per '{kw}')"
    if truncated:
        out += (f"\n(scansione fermata ai primi {cap} schemi su {len(schemas)}; "
                f"restringi agent_tools.explorable_schemas per cercare in tutti)")
    return out


def _t_list_tables(a):
    try:
        tgt = f"{a.get('catalog','')}.{a['schema']}" if a.get("catalog") else a["schema"]
        return run_sql(f"SHOW TABLES IN {tgt}", max_rows=1000).to_csv(index=False)
    except Exception as e:
        return f"ERRORE: {str(e)[:200]}"


def _t_details(a):
    out = _t_describe(a)
    try:
        n = run_sql(f"SELECT COUNT(*) n FROM {a['table_name']}").iloc[0]["n"]
        out += f"\nrow_count: {n}"
    except Exception:
        pass
    return out


def _t_jobs(a):
    try:
        kw, rows = a.get("job_name_keyword", ""), []
        for j in wc().jobs.list():
            name = getattr(j.settings, "name", "") if j.settings else ""
            if kw.lower() in (name or "").lower():
                rows.append(f"{name} (id={j.job_id})")
        return "\n".join(rows[:20]) or f"(nessun job per '{kw}')"
    except Exception as e:
        return f"ERRORE: {str(e)[:200]}"


def _path_ok(p):
    allowed = cfg("agent_tools.allowed_workspace_paths", [])
    if not allowed or "*" in allowed:      # 'ovunque': nessuna restrizione
        return True
    return any(p.startswith(x) for x in allowed)


def _t_find_etl(a):
    tn, found = a.get("table_name", ""), []
    roots = cfg("agent_tools.etl_roots", [])
    if not roots or "*" in roots:          # 'ovunque': cerca da tutto /Workspace
        roots = ["/Workspace"]
    needle = tn.split(".")[-1].lower()
    cap = int(cfg("agent_tools.max_scan_objects", 4000))
    scanned, stopped = 0, False
    try:
        for root in roots:
            if stopped:
                break
            try:
                for o in wc().workspace.list(root, recursive=True):
                    scanned += 1
                    if needle and needle in (o.path or "").lower():
                        found.append(o.path)
                    if scanned >= cap or len(found) >= 20:
                        stopped = True
                        break
            except Exception:
                continue
    except Exception as e:
        return f"ERRORE: {str(e)[:150]}"
    out = "\n".join(found[:20]) or f"(nessun ETL per '{tn}')"
    if scanned >= cap:
        out += f"\n(ricerca fermata a {cap} elementi; restringi agent_tools.etl_roots)"
    return out


def _t_read_nb(a):
    p = a.get("path", "")
    if not _path_ok(p):
        return "ERRORE: path non consentito."
    try:
        from databricks.sdk.service.workspace import ExportFormat
        exp = wc().workspace.export(path=p, format=ExportFormat.SOURCE)
        return base64.b64decode(exp.content).decode("utf-8", errors="replace")[:8000]
    except Exception as e:
        return f"ERRORE: {str(e)[:200]}"


_TOOL_IMPL = {
    "run_sql_query": _t_run_sql, "describe_table": _t_describe, "search_tables": _t_search_tables,
    "list_tables_in_schema": _t_list_tables, "get_table_details": _t_details,
    "check_recent_job_runs": _t_jobs, "find_etl_for_table": _t_find_etl, "read_notebook": _t_read_nb,
}


def _tool_def(name, desc, props, req):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": req}}}


TOOLS = [
    _tool_def("run_sql_query", "Query SQL in sola lettura.", {"query": {"type": "string"}}, ["query"]),
    _tool_def("describe_table", "Schema tabella.", {"table_name": {"type": "string"}}, ["table_name"]),
    _tool_def("search_tables", "Cerca tabelle per keyword.", {"keyword": {"type": "string"}}, ["keyword"]),
    _tool_def("list_tables_in_schema", "Elenca tabelle di uno schema.",
              {"catalog": {"type": "string"}, "schema": {"type": "string"}}, ["schema"]),
    _tool_def("get_table_details", "Schema + row count.", {"table_name": {"type": "string"}}, ["table_name"]),
    _tool_def("check_recent_job_runs", "Cerca job per nome.", {"job_name_keyword": {"type": "string"}}, ["job_name_keyword"]),
    _tool_def("find_etl_for_table", "Trova ETL di una tabella.", {"table_name": {"type": "string"}}, ["table_name"]),
    _tool_def("read_notebook", "Legge un notebook consentito.", {"path": {"type": "string"}}, ["path"]),
]


# Pool per imporre un TIMEOUT DURO su ogni chiamata a strumento: una query pesante o
# uno scan del workspace che si impunta non deve più bloccare l'intera analisi. Se lo
# strumento sfora, lo abbandoniamo (il thread finisce da solo) e l'agente prosegue.
_TOOL_POOL = _cf.ThreadPoolExecutor(max_workers=8)


def _exec_tool(tc) -> str:
    try:
        args = json.loads(tc.function.arguments or "{}")
    except Exception:
        args = {}
    fn = _TOOL_IMPL.get(tc.function.name)
    if not fn:
        return f"ERRORE: tool sconosciuto '{tc.function.name}'."
    timeout = int(cfg("agent_tools.tool_call_timeout_s", 30))
    try:
        return str(_TOOL_POOL.submit(fn, args).result(timeout=timeout))[:6000]
    except _cf.TimeoutError:
        return (f"ERRORE: lo strumento {tc.function.name} ha superato {timeout}s ed è stato "
                f"interrotto. NON riprovare lo stesso strumento con gli stessi argomenti: "
                f"concludi con le informazioni già raccolte.")
    except Exception as e:
        return f"ERRORE tool {tc.function.name}: {str(e)[:200]}"


# Istruzioni iniettate nella chat: strumenti FACOLTATIVI, rispondi dal contesto.
_TOOL_POLICY = (
    "\n\nISTRUZIONI OPERATIVE: rispondi PRIMA usando la documentazione e i ticket nel "
    "contesto qui sopra. Gli strumenti su tabelle/workspace sono FACOLTATIVI: usali solo "
    "se la domanda lo richiede esplicitamente o se il contesto non basta. Appena hai "
    "elementi sufficienti, RISPONDI subito senza chiamare altri strumenti.")


def run_tool_loop(messages, max_iterations=8, time_budget_s=None):
    """Loop agentico BOUNDED: si ferma per numero di iterazioni O per tempo trascorso."""
    start = time.time()
    for _ in range(max_iterations):
        resp = llm_chat(messages, tools=TOOLS, max_tokens=2000)
        msg = resp.choices[0].message
        if not msg.tool_calls:
            messages.append({"role": "assistant", "content": msg.content or ""})
            return messages
        messages.append({"role": "assistant", "content": msg.content or "",
                         "tool_calls": [tc.model_dump() for tc in msg.tool_calls]})
        for tc in msg.tool_calls:
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": _exec_tool(tc)})
        if time_budget_s and (time.time() - start) > time_budget_s:
            break
    return messages


def _final_reply(messages) -> str:
    """Garantisce SEMPRE una risposta testuale: se il loop è finito su una chiamata a
    strumento (o per limite di iterazioni/tempo), forza un'ultima risposta SENZA
    strumenti — così non può ciclare né restituire output grezzi."""
    last = messages[-1] if messages else {}
    if (last.get("role") == "assistant" and not last.get("tool_calls")
            and (last.get("content") or "").strip()):
        return last["content"]
    try:
        resp = llm_chat(messages + [{"role": "user", "content":
            "Concludi ORA: rispondi all'utente in modo chiaro usando la documentazione e le "
            "informazioni già raccolte nel contesto. Se NON hai elementi sufficienti per "
            "rispondere con certezza, dillo esplicitamente e indica cosa servirebbe: non "
            "inventare. Non chiamare altri strumenti."}], tools=None)
        return (resp.choices[0].message.content or "").strip() or \
            "Non ho elementi sufficienti per una risposta completa; prova a riformulare la domanda."
    except Exception as e:
        return f"Non sono riuscito a completare la risposta ({str(e)[:120]})."


# ===========================================================================
# AGENTE (analisi con avanzamento) + CHAT
# ===========================================================================
def _response_schema():
    return {"type": "json_schema", "json_schema": {"name": "ticket_analysis", "strict": True,
        "schema": {"type": "object", "properties": {
            "summary": {"type": "string"},
            "type": {"type": "string", "enum": cfg("taxonomy.problem_types")},
            "severity": {"type": "string", "enum": cfg("taxonomy.severities")},
            "hypothesis": {"type": "array", "items": {"type": "string"}},
            "steps": {"type": "array", "items": {"type": "string"}},
            "proposed_solution": {"type": "string"},
            "sql_commands": {"type": "array", "items": {"type": "string"}},
            "root_cause_confirmed": {"type": "boolean"},
            "estimated_effort": {"type": "string"}, "evidence": {"type": "string"},
            "confidence": {"type": "number"}, "reasoning": {"type": "string"}},
        "required": ["summary", "type", "severity", "hypothesis", "steps", "proposed_solution",
                     "sql_commands", "root_cause_confirmed", "estimated_effort", "evidence",
                     "confidence", "reasoning"]}}}


def _render_ticket(t: dict) -> str:
    return (f"Ticket: {t.get('number','')} ({t.get('ticket_type','')})\n"
            f"Gruppo: {t.get('assignment_group','')}\nTitolo: {t.get('short_description','')}\n"
            f"Stato: {t.get('state','')} | Priorità: {t.get('priority','')}\n"
            f"Descrizione:\n{t.get('description','')}\n"
            f"Close notes:\n{t.get('close_notes') or '(nessuna)'}\n"
            f"Comments:\n{t.get('comments') or '(nessuno)'}")


def _feedback_from_chat(number: str) -> str:
    """Recupera la conversazione del ticket (correzioni dell'operatore) da iniettare
    nell'analisi: è ciò che permette l'auto-apprendimento (chatti la correzione → rianalisi)."""
    hist = get_chat_history(number)
    if not hist:
        return ""
    lines = [f"{'OPERATORE' if h.get('role') == 'user' else 'BOT'}: {h.get('message','')}"
             for h in hist][-20:]
    return ("\n\nCONVERSAZIONE PRECEDENTE CON L'OPERATORE SU QUESTO TICKET.\n"
            "Contiene CORREZIONI e indicazioni dell'operatore: RECEPISCILE e correggi nella "
            "nuova analisi gli errori segnalati. Non ripetere gli stessi errori.\n"
            + "\n".join(lines))


def analyze_ticket_stream(ticket: dict, attachments=None):
    """Esegue l'analisi trasmettendo l'avanzamento: yield ('status', msg) e infine ('done', row).
    Tiene conto della chat del ticket (correzioni dell'operatore) → auto-apprendimento."""
    query = f"{ticket.get('short_description','')} {str(ticket.get('description',''))[:300]}"
    yield ("status", "Cerco ticket simili validati…")
    ctx, sources = build_context(query, exclude_number=ticket.get("number", ""))
    system = cfg("prompts.system_role")
    if ctx:
        system += "\n\nCONTESTO RECUPERATO (RAG):\n" + ctx

    att_text, att_images, _ = process_attachments(attachments)
    fb = _feedback_from_chat(ticket.get("number", ""))
    user_text = _render_ticket(ticket) + fb + att_text
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": _user_content(user_text, att_images)}]

    max_iter = cfg("workflow.max_tool_iterations", 8)
    budget = cfg("workflow.tool_time_budget_s", 75)
    _start = time.time()
    investigated = False                          # True = l'agente ha concluso l'indagine da sé
    for i in range(max_iter):
        yield ("status", f"Investigo sui dati… (passo {i + 1}/{max_iter})")
        resp = llm_chat(messages, tools=TOOLS, max_tokens=2000)
        msg = resp.choices[0].message
        if not msg.tool_calls:
            messages.append({"role": "assistant", "content": msg.content or ""})
            investigated = True
            break
        names = ", ".join(tc.function.name for tc in msg.tool_calls)
        yield ("status", f"Eseguo strumenti: {names}")
        messages.append({"role": "assistant", "content": msg.content or "",
                         "tool_calls": [tc.model_dump() for tc in msg.tool_calls]})
        for tc in msg.tool_calls:
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": _exec_tool(tc)})
        if time.time() - _start > budget:
            yield ("status", "Tempo di indagine esaurito: concludo con quanto raccolto…")
            break

    if cfg("workflow.self_critique", True):
        yield ("status", "Verifico le evidenze (self-critique)…")
        messages.append({"role": "user", "content": cfg("prompts.self_critique")})
        messages = run_tool_loop(messages, 3, budget)

    yield ("status", "Redigo l'analisi finale…")
    honesty = ("Sii ONESTO E RIGOROSO: includi SOLO ciò che hai potuto VERIFICARE con "
               "evidenza (una query, una riga di ETL, la documentazione). Se l'evidenza per "
               "la root cause è insufficiente, imposta root_cause_confirmed=false e una "
               "confidence BASSA, e nel campo reasoning elenca cosa resta da verificare. "
               "Non inventare tabelle, colonne o valori.")
    if not investigated:
        honesty += (" L'indagine è stata interrotta per limite di tempo/passi: l'evidenza è "
                    "probabilmente parziale, quindi sii conservativo sulla confidence.")
    final = llm_chat(messages + [{"role": "user",
                     "content": "Fornisci l'analisi finale nel formato richiesto. " + honesty}],
                     response_format=_response_schema())
    parsed = _parse_analysis(final.choices[0].message.content)
    parsed["sources_used"] = json.dumps(sources, ensure_ascii=False)
    yield ("done", save_analysis_draft(ticket, parsed))


def analyze_ticket(ticket: dict, attachments=None) -> dict:
    row = None
    for kind, payload in analyze_ticket_stream(ticket, attachments):
        if kind == "done":
            row = payload
    return row


def _parse_analysis(raw: str) -> dict:
    try:
        d = json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw or "", re.DOTALL)
        d = json.loads(m.group(0)) if m else {}
    return {
        "analysis_id": str(uuid.uuid4()), "summary": d.get("summary", ""),
        "problem_type": d.get("type", "OTHER"), "severity": d.get("severity", "MEDIUM"),
        "hypothesis": json.dumps(d.get("hypothesis", []), ensure_ascii=False),
        "steps": json.dumps(d.get("steps", []), ensure_ascii=False),
        "proposed_solution": d.get("proposed_solution", ""),
        "sql_commands": json.dumps(d.get("sql_commands", []), ensure_ascii=False),
        "root_cause_confirmed": bool(d.get("root_cause_confirmed", False)),
        "evidence": d.get("evidence", ""), "estimated_effort": d.get("estimated_effort", ""),
        "confidence": float(d.get("confidence", 0.0)), "reasoning": d.get("reasoning", ""),
        "created_at": _now(),
    }


def chat_answer(number: str, user_message: str, attachments=None):
    ticket = get_ticket(number)
    if ticket is None:
        return f"Ticket {number} non trovato.", []
    query = f"{ticket.get('short_description','')} {str(ticket.get('description',''))[:200]}"
    ctx, sources = build_context(query, exclude_number=number)
    a = get_latest_analysis(number)
    actx = ""
    if a:
        rc = "CONFERMATA" if a.get("root_cause_confirmed") else "ipotesi"
        actx = f"\nPre-analisi: {a.get('summary','')}\nRoot cause: {rc}\nSoluzione: {a.get('proposed_solution','')}"
    system = (f"Sei un Senior Data Engineer AMS per il gruppo {ticket.get('assignment_group','')}.\n"
              f"Ticket {number}: {ticket.get('short_description','')}\n"
              f"Descrizione: {str(ticket.get('description',''))[:600]}{actx}\n{cfg('prompts.system_role')}"
              + _TOOL_POLICY)
    if ctx:
        system += "\n\nCONTESTO RECUPERATO (RAG):\n" + ctx
    messages = [{"role": "system", "content": system}]
    for h in get_chat_history(number):
        if h.get("role") in ("user", "assistant"):
            messages.append({"role": h["role"], "content": h.get("message", "")})
    att_text, att_images, _ = process_attachments(attachments)
    messages.append({"role": "user", "content": _user_content(user_message + att_text, att_images)})
    messages = run_tool_loop(messages, cfg("workflow.chat_max_iterations", 4),
                             cfg("workflow.tool_time_budget_s", 75))
    reply = _final_reply(messages)
    append_chat(number, "user", user_message)
    append_chat(number, "assistant", reply, sources)
    return reply, sources


def assistant_answer(user_message: str, history=None, attachments=None):
    """Chat GENERALE, non legata a un ticket: usa documentazione + ticket validati + tool."""
    ctx, sources = build_context(user_message)
    system = cfg("prompts.system_role") + _TOOL_POLICY
    if ctx:
        system += "\n\nCONTESTO RECUPERATO (RAG):\n" + ctx
    messages = [{"role": "system", "content": system}]
    for pair in (history or []):
        u, a = (pair + ["", ""])[:2]
        if u:
            messages.append({"role": "user", "content": u})
        if a:
            messages.append({"role": "assistant", "content": a})
    att_text, att_images, _ = process_attachments(attachments)
    messages.append({"role": "user", "content": _user_content(user_message + att_text, att_images)})
    messages = run_tool_loop(messages, cfg("workflow.chat_max_iterations", 4),
                             cfg("workflow.tool_time_budget_s", 75))
    return _final_reply(messages), sources


# ===========================================================================
# INGESTION (ServiceNow, Excel)
# ===========================================================================
_SNOW_DT_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M",
                    "%d-%m-%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S")


def _snow_dt(raw):
    """Parsa una data ServiceNow. Con sysparm_display_value=all il campo 'value' è
    sempre UTC in formato 'YYYY-MM-DD HH:MM:SS'."""
    s = _s(raw).strip()
    if not s:
        return None
    for f in _SNOW_DT_FORMATS:
        try:
            return datetime.strptime(s, f).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    logger.warning(f"data ServiceNow non parsata: '{s[:30]}'")
    return None


def _excel_dt(v):
    """Parsa una data da Excel. Se priva di timezone la interpreta come ora locale
    (sal.tz), perché gli export ServiceNow sono nel fuso dell'utente."""
    import pandas as pd
    if v is None or _s(v).strip() == "":
        return None
    try:
        ts = pd.to_datetime(v, dayfirst=True, errors="coerce")
    except Exception:
        return None
    if ts is None or pd.isna(ts):
        return None
    dt = ts.to_pydatetime()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz())
    return dt.astimezone(timezone.utc)


def snow_sync() -> str:
    """Due fetch per tabella, query encoded senza ambiguità AND/OR:
      1) attivi   -> assignment_groupIN...^active=true
      2) NON attivi aggiornati negli ultimi N giorni (servono al SAL "chiusi")
    I chiusi vengono SALVATI (non più scartati) e nascosti a valle."""
    import requests
    base = cfg("servicenow.base_url")
    user, pwd = snow_creds()
    if not user or not pwd:
        raise RuntimeError("Credenziali ServiceNow non disponibili (config/env/secret scope).")
    groups = [g for g in cfg("servicenow.assignment_group_ids", []) if g]
    grp_q = f"assignment_groupIN{','.join(groups)}" if groups else ""
    page_size, max_rec = cfg("servicenow.page_size", 1000), cfg("servicenow.max_records", 50000)
    fields = cfg("servicenow.fields", []) or []
    lookback = int(cfg("servicenow.closed_lookback_days", 60))
    since = (_now() - timedelta(days=lookback)).strftime("%Y-%m-%d %H:%M:%S")
    drop_closed = bool(cfg("ingestion.drop_closed_on_ingest", False))

    def fetch(api: str, extra: str) -> list:
        out, offset = [], 0
        query = "^".join(x for x in (grp_q, extra) if x)
        while len(out) < max_rec:
            q = {"sysparm_limit": page_size, "sysparm_offset": offset,
                 # 'all' -> ogni campo ha {value, display_value}: date UTC dal value,
                 # etichette leggibili (state, gruppo) dal display_value.
                 "sysparm_display_value": "all", "sysparm_exclude_reference_link": "true"}
            if query:
                q["sysparm_query"] = query
            if fields:
                q["sysparm_fields"] = ",".join(fields)
            r = requests.get(f"{base}/{api}", params=q, auth=(user, pwd),
                             headers={"Accept": "application/json"}, timeout=90)
            r.raise_for_status()
            batch = r.json().get("result", [])
            out.extend(batch)
            if len(batch) < page_size:
                break
            offset += page_size
        return out

    def gv(rec, k):     # valore raw (date in UTC, codici di stato)
        v = rec.get(k, "")
        return v.get("value", "") if isinstance(v, dict) else v

    def gd(rec, k):     # display value (etichette)
        v = rec.get(k, "")
        return v.get("display_value", "") if isinstance(v, dict) else v

    def build(rec, ttype) -> dict:
        state_lbl = _s(gd(rec, "state")) or _s(gv(rec, "state"))
        opened = _snow_dt(gv(rec, "opened_at")) or _snow_dt(gv(rec, "sys_created_on"))
        updated = _snow_dt(gv(rec, "sys_updated_on"))
        closed = _snow_dt(gv(rec, "closed_at"))
        # Alcuni record chiusi/annullati non hanno closed_at: proxy = ultimo update.
        if closed is None and (_s(gv(rec, "active")).lower() == "false"
                               or is_closed_state(state_lbl)):
            closed = updated
        return {
            "number": _s(gv(rec, "number")), "source": "servicenow", "ticket_type": ttype,
            "short_description": _s(gd(rec, "short_description")),
            "description": _s(gd(rec, "description")),
            "close_notes": _s(gd(rec, "close_notes")), "comments": _s(gd(rec, "comments")),
            "state": state_lbl, "state_code": _s(gv(rec, "state")),
            "priority": _s(gd(rec, "priority")),
            "assignment_group": _s(gd(rec, "assignment_group")),
            "caller": _s(gd(rec, "caller_id")) or _s(gd(rec, "requested_for")),
            "opened_at": opened or SqlNull("TIMESTAMP"),
            "closed_at": closed or SqlNull("TIMESTAMP"),
            "updated_at": updated or SqlNull("TIMESTAMP"),
            "source_updated_key": _s(gv(rec, "sys_updated_on")),
        }

    tot_open = tot_closed = 0
    for api, ttype in [(cfg("servicenow.incident_table"), "incident"),
                       (cfg("servicenow.request_item_table"), "request_item")]:
        rows = [build(r, ttype) for r in fetch(api, "active=true") if _s(gv(r, "number"))]
        rows = [r for r in rows if not (drop_closed and is_closed_state(r["state"]))]
        tot_open += upsert_tickets(rows)
        if not drop_closed:
            try:
                cl = [build(r, ttype) for r in fetch(api, f"active=false^sys_updated_on>={since}")
                      if _s(gv(r, "number"))]
                tot_closed += upsert_tickets(cl)
            except Exception as e:
                # Non far cadere l'intera sync se la query sui chiusi viene rifiutata.
                logger.warning(f"fetch chiusi {api} fallita: {str(e)[:200]}")
    return (f"Sincronizzati {tot_open} ticket attivi e {tot_closed} chiusi "
            f"(ultimi {lookback} gg) da ServiceNow.")


def _excel_replace_prune(keep_numbers) -> int:
    """Modalità replace: elimina i ticket source='excel' NON presenti nel file appena
    caricato, TRANNE quelli con un'analisi. Non tocca ticket di altre sorgenti."""
    if not keep_numbers:
        return 0
    keep = ", ".join(sql_str(n) for n in keep_numbers)
    cond = (f"source = 'excel' AND number NOT IN ({keep}) "
            f"AND number NOT IN (SELECT number FROM {table('ticket_analysis')})")
    try:
        df = run_sql(f"SELECT COUNT(*) c FROM {table('tickets')} WHERE {cond}")
        removed = int(df.iloc[0]["c"]) if df is not None and not df.empty else 0
    except Exception as e:
        logger.warning(f"replace prune count: {str(e)[:120]}")
        return 0
    if removed:
        exec_sql(f"DELETE FROM {table('tickets')} WHERE {cond}")
    return removed


def excel_import(file_path: str) -> str:
    """Import robusto: sceglie il foglio giusto (il primo che contiene una colonna
    'numero'), abbina le intestazioni in modo case-insensitive e tollerante agli spazi,
    e mappa gli alias definiti in excel_column_map. Gestisce file multi-foglio (es. con
    un foglio 'Legenda' iniziale) ed export ServiceNow in italiano."""
    import pandas as pd
    cmap = cfg("excel_column_map", {})
    number_syn = [str(s).strip().lower() for s in cmap.get("number", [])]

    xls = pd.ExcelFile(file_path)
    df = chosen = None
    for sh in xls.sheet_names:
        cand = xls.parse(sh)
        norm = {str(c).strip().lower(): c for c in cand.columns}
        if any(n in norm for n in number_syn):
            df, chosen = cand, sh
            break
    if df is None:
        raise ValueError("Nessun foglio con una colonna Numero/Number/ID trovato nel file "
                         f"(fogli: {', '.join(xls.sheet_names)}).")

    norm = {str(c).strip().lower(): c for c in df.columns}

    def col_for(cands):
        for c in cands:
            k = str(c).strip().lower()
            if k in norm:
                return norm[k]
        return None

    colmap = {f: col_for(cs) for f, cs in cmap.items()}

    def get(r, field):
        col = colmap.get(field)
        if not col:
            return None
        v = r.get(col)
        return None if (v is None or (isinstance(v, float) and pd.isna(v))) else v

    drop_closed = cfg("ingestion.drop_closed_on_ingest", False)
    rows, assign_rows, file_numbers = [], [], set()
    for _, r in df.iterrows():
        number = _s(get(r, "number")).strip()
        state = _s(get(r, "state"))
        if not number:
            continue
        file_numbers.add(number)
        if drop_closed and is_closed_state(state):
            continue
        rows.append({
            "number": number, "source": "excel", "ticket_type": "incident",
            "short_description": _s(get(r, "short_description")),
            "description": _s(get(r, "description")),
            "close_notes": _s(get(r, "close_notes")), "comments": "",
            "state": state, "state_code": "", "priority": _s(get(r, "priority")),
            "assignment_group": _s(get(r, "assignment_group")), "caller": _s(get(r, "caller")),
            "opened_at": _excel_dt(get(r, "opened_at")) or SqlNull("TIMESTAMP"),
            "closed_at": _excel_dt(get(r, "closed_at")) or SqlNull("TIMESTAMP"),
            "updated_at": _excel_dt(get(r, "updated_at")) or SqlNull("TIMESTAMP"),
            "source_updated_key": number})
        owner = _s(get(r, "assignee")).strip()
        if owner and any(c.isalpha() for c in owner):   # scarta '0'/numeri: solo nomi veri
            assign_rows.append({"number": number, "assignee": owner,
                                "assigned_by": "import-excel", "assigned_at": _now()})
    n = upsert_tickets(rows)
    # Assegnatario dal file (colonna OWNER/Assegnato a) -> ticket_assignment, così è
    # visibile in Dashboard e nelle tabelle SAL (che leggono a.assignee).
    if assign_rows:
        seen, uniq = set(), []
        for a in reversed(assign_rows):      # ultimo per numero vince
            if a["number"] in seen:
                continue
            seen.add(a["number"]); uniq.append(a)
        batch = int(cfg("ingestion.merge_batch_size", 300))
        for i in range(0, len(uniq), batch):
            merge_values(table("ticket_assignment"), ["number"], uniq[i:i + batch])
    removed = 0
    if cfg("ingestion.excel_replace", False):
        removed = _excel_replace_prune(file_numbers)
    extra = f" · rimossi {removed} obsoleti (analizzati mantenuti)" if removed else ""
    return f"Importati {n} ticket dal foglio '{chosen}'{extra}."


# ===========================================================================
# BOOTSTRAP (schema + 8 tabelle, hive_metastore)
# ===========================================================================
DDL = """
CREATE TABLE IF NOT EXISTS {s}.tickets (
    number STRING, source STRING, ticket_type STRING, short_description STRING,
    description STRING, close_notes STRING, comments STRING, state STRING, state_code STRING,
    priority STRING, severity STRING, assignment_group STRING, caller STRING,
    opened_at TIMESTAMP, closed_at TIMESTAMP, updated_at TIMESTAMP, raw STRING,
    source_updated_key STRING, ingested_at TIMESTAMP) USING DELTA;
CREATE TABLE IF NOT EXISTS {s}.ticket_analysis (
    analysis_id STRING, number STRING, version INT, status STRING, summary STRING,
    problem_type STRING, severity STRING, hypothesis STRING, steps STRING, proposed_solution STRING,
    sql_commands STRING, root_cause_confirmed BOOLEAN, evidence STRING, estimated_effort STRING,
    confidence DOUBLE, reasoning STRING, sources_used STRING, model_name STRING, created_by STRING,
    created_at TIMESTAMP, reviewed_by STRING, reviewed_at TIMESTAMP, review_notes STRING) USING DELTA;
CREATE TABLE IF NOT EXISTS {s}.kb_entries (
    entry_id STRING, number STRING, title STRING, problem_type STRING, content STRING,
    proposed_solution STRING, evidence STRING, embedding STRING, approved_by STRING,
    approved_at TIMESTAMP) USING DELTA;
CREATE TABLE IF NOT EXISTS {s}.ticket_chat (
    chat_id STRING, number STRING, role STRING, message STRING, sources_used STRING,
    created_at TIMESTAMP) USING DELTA;
CREATE TABLE IF NOT EXISTS {s}.ticket_assignment (
    number STRING, assignee STRING, assigned_by STRING, assigned_at TIMESTAMP) USING DELTA;
CREATE TABLE IF NOT EXISTS {s}.audit_log (
    event_id STRING, entity STRING, entity_id STRING, action STRING, actor STRING,
    detail STRING, event_at TIMESTAMP) USING DELTA;
CREATE TABLE IF NOT EXISTS {s}.token_usage (
    usage_date DATE, model_name STRING, input_tokens BIGINT, output_tokens BIGINT,
    calls BIGINT) USING DELTA;
CREATE TABLE IF NOT EXISTS {s}.knowledge_docs (
    doc_id STRING, title STRING, content STRING, tags STRING, embedding STRING,
    created_by STRING, created_at TIMESTAMP) USING DELTA;
CREATE TABLE IF NOT EXISTS {s}.knowledge_chunks (
    chunk_id STRING, doc_id STRING, chunk_index INT, title STRING,
    content STRING, embedding STRING, chunk_sig STRING,
    created_at TIMESTAMP) USING DELTA;
"""

# Migrazioni idempotenti su tabelle già esistenti (fallisce -> colonna già presente).
MIGRATIONS = [
    "ALTER TABLE {s}.kb_entries ADD COLUMNS (embedding STRING)",
    "ALTER TABLE {s}.tickets ADD COLUMNS (state_code STRING)",
    "ALTER TABLE {s}.tickets ADD COLUMNS (updated_at TIMESTAMP)",
]


def _ddl_exec(stmt: str):
    """Usa spark.sql nel notebook (dove il connettore SQL è oscurato dal runtime),
    altrimenti il SQL Warehouse (nell'app)."""
    try:
        from pyspark.sql import SparkSession
        sp = SparkSession.getActiveSession()
    except Exception:
        sp = None
    if sp is not None:
        sp.sql(stmt)
    else:
        exec_sql(stmt)


_ALREADY_EXISTS = ("already exists", "field_already_exists", "fields_already_exist",
                   "duplicate column", "cannot add column")


def run_bootstrap():
    """Crea schema + tabelle e applica le migrazioni. Idempotente.
    Le migrazioni NON vengono silenziate: se una ALTER fallisce per un motivo
    diverso da 'colonna già presente' (tipicamente permessi) la si vede nei log
    e a fine run viene rilanciata."""
    schema = cfg("storage.schema")
    try:
        _ddl_exec(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        logger.info(f"OK  CREATE SCHEMA {schema}")
    except Exception as e:
        logger.warning(f"schema: {str(e)[:150]}")
    for stmt in [x.strip() for x in DDL.format(s=schema).split(";") if x.strip()]:
        head = stmt.split("\n", 1)[0][:70]
        try:
            _ddl_exec(stmt)
            logger.info(f"OK  {head}")
        except Exception as e:
            logger.error(f"FAIL {head} -> {str(e)[:200]}")
            raise
    failed = []
    for stmt in MIGRATIONS:
        s = stmt.format(s=schema)
        try:
            _ddl_exec(s)
            logger.info(f"OK  {s}")
        except Exception as e:
            msg = str(e)
            if any(k in msg.lower() for k in _ALREADY_EXISTS):
                logger.info(f"SKIP {s} (colonna già presente)")
            else:
                logger.error(f"FAIL {s} -> {msg[:300]}")
                failed.append((s, msg[:300]))
    if failed:
        raise RuntimeError("Migrazioni non applicate (probabile mancanza di permessi ALTER "
                           "sulla tabella):\n" + "\n".join(f"- {s}: {m}" for s, m in failed))
    logger.info("Bootstrap completato.")


# ===========================================================================
# UI — DESIGN SYSTEM
# ===========================================================================
# Direzione: console operativa, non dashboard decorativa. Tre principi:
#   1. DENSITÀ — durante un incident conta quanti ticket stanno sopra la piega,
#      non quanto sono arrotondati gli angoli. Righe compatte, hairline, niente
#      card per riga.
#   2. GERARCHIA — il magenta KIKO è identità e focus, non colore di servizio.
#      Lo stato si legge dal "rail" (bordo colorato a sinistra della riga), in
#      visione periferica, senza leggere il testo.
#   3. NUMERI TABULARI — monospace e tabular-nums su ID, date e metriche:
#      le colonne si allineano e le cifre si confrontano a occhio.
# Il CSS custom sotto è volutamente basato su classi PROPRIE (.ams-*): non
# dipende dai nomi di classe interni di Gradio, che cambiano tra le versioni.
# Il tema (colori dei componenti Gradio) passa da gr.themes.Base().set(): niente
# più !important sui selettori interni.
# ===========================================================================

def _patch_gradio_client():
    """Workaround per lo schema JSON booleano di gradio_client.
    DEBITO NOTO: da rimuovere pinnando gradio/gradio_client in requirements.txt.
    Il pin ora c'è (gradio 4.44.1 / gradio_client 1.3.0): la rimozione si può
    valutare a parte, dopo aver verificato che su quella versione esatta lo
    schema booleano non si presenti più."""
    try:
        import gradio_client.utils as gcu
        _o1 = gcu.get_type
        gcu.get_type = lambda s: "bool" if isinstance(s, bool) else _o1(s)
        _o2 = gcu._json_schema_to_python_type
        gcu._json_schema_to_python_type = (
            lambda s, defs=None: "bool" if isinstance(s, bool) else _o2(s, defs))
    except Exception as e:
        logger.warning(f"patch gradio_client: {e}")


# --- design tokens (unica fonte di verità per colori/tipografia) -------------
T = {
    "brand": cfg("branding.accent_color", "#EC008C"),
    "ink900": "#16131A",   # canvas
    "ink800": "#1D1922",   # superficie
    "ink750": "#221D2A",   # riga hover
    "ink700": "#272130",   # header tabella / elevato
    "line": "#332C3D",     # hairline
    "lineSoft": "#241F2C",
    "txt": "#F2EDF5",
    "txt2": "#A99EB0",
    "txt3": "#6E6478",
    "open": "#4ADE80",     # nuovo / aperto
    "work": "#F472B6",     # in lavorazione (parente del brand)
    "hold": "#FBBF24",     # on hold / pending
    "closed": "#7C748A",   # chiuso / risolto
    "crit": "#F87171",     # P1 / attenzione
}

# Stack con fallback di sistema: se l'ambiente dell'App non raggiunge
# fonts.googleapis.com (frequente in tenant con egress ristretto) la UI resta
# leggibile invece di cadere su un serif di default.
FONT_SANS = ('"Instrument Sans", ui-sans-serif, -apple-system, "Segoe UI", '
             'Roboto, Helvetica, Arial, sans-serif')
FONT_MONO = ('"IBM Plex Mono", ui-monospace, "SF Mono", "Cascadia Mono", '
             'Menlo, Consolas, monospace')


def _theme():
    """Tema Gradio derivato dai token. Difensivo: se una variabile non esiste
    nella versione di Gradio installata, si ripiega sul Base senza far cadere
    l'app (un upgrade di Gradio non deve diventare un incident)."""
    import gradio as gr
    try:
        fonts = [gr.themes.GoogleFont("Instrument Sans"), "ui-sans-serif", "system-ui",
                 "sans-serif"]
        mono = [gr.themes.GoogleFont("IBM Plex Mono"), "ui-monospace", "monospace"]
        return gr.themes.Base(font=fonts, font_mono=mono).set(
            body_background_fill=T["ink900"],
            body_text_color=T["txt"],
            body_text_color_subdued=T["txt2"],
            background_fill_primary=T["ink800"],
            background_fill_secondary=T["ink900"],
            border_color_primary=T["line"],
            block_background_fill=T["ink800"],
            block_border_color=T["line"],
            block_label_text_color=T["txt2"],
            block_title_text_color=T["txt2"],
            input_background_fill=T["ink700"],
            input_border_color=T["line"],
            button_primary_background_fill=T["brand"],
            button_primary_text_color="#FFFFFF",
            button_secondary_background_fill=T["ink700"],
            button_secondary_text_color=T["txt"],
        )
    except Exception as e:
        logger.warning(f"tema non applicato ({e}); uso gr.themes.Base()")
        return gr.themes.Base()


def _css():
    b = T["brand"]
    return f"""
:root {{
  --ams-brand:{b}; --ams-brand-dim:{b}1F; --ams-brand-mid:{b}55;
  --ams-ink900:{T['ink900']}; --ams-ink800:{T['ink800']}; --ams-ink750:{T['ink750']};
  --ams-ink700:{T['ink700']}; --ams-line:{T['line']}; --ams-line-soft:{T['lineSoft']};
  --ams-txt:{T['txt']}; --ams-txt2:{T['txt2']}; --ams-txt3:{T['txt3']};
  --ams-open:{T['open']}; --ams-work:{T['work']}; --ams-hold:{T['hold']};
  --ams-closed:{T['closed']}; --ams-crit:{T['crit']};
  --ams-sans:{FONT_SANS}; --ams-mono:{FONT_MONO};
  --ams-r:8px;
}}

/* ---------- base ---------- */
.gradio-container {{ max-width:1560px !important; font-family:var(--ams-sans); }}
.gradio-container *::selection {{ background:var(--ams-brand-mid); }}
:where(button, a, input, select, textarea):focus-visible {{
  outline:2px solid var(--ams-brand); outline-offset:2px; border-radius:4px; }}
::-webkit-scrollbar {{ width:10px; height:10px; }}
::-webkit-scrollbar-track {{ background:transparent; }}
::-webkit-scrollbar-thumb {{ background:var(--ams-line); border-radius:6px;
  border:2px solid var(--ams-ink900); }}
::-webkit-scrollbar-thumb:hover {{ background:var(--ams-txt3); }}

/* ---------- header ---------- */
#ams-head {{ border-bottom:1px solid var(--ams-line); padding:2px 0 14px; margin-bottom:6px; }}
#ams-head .hd {{ display:flex; align-items:center; gap:12px; }}
#ams-head .mark {{ width:26px; height:26px; border-radius:7px; flex:0 0 auto;
  background:linear-gradient(150deg, var(--ams-brand), #7A1F6B);
  display:grid; place-items:center; color:#fff; font:600 12px/1 var(--ams-mono);
  letter-spacing:-.02em; box-shadow:0 0 0 1px #ffffff14 inset; }}
#ams-head h1 {{ font:600 17px/1.2 var(--ams-sans); letter-spacing:-.015em;
  color:var(--ams-txt); margin:0; }}
#ams-head .meta {{ margin-left:auto; display:flex; align-items:center; gap:14px;
  font:400 11px/1 var(--ams-mono); color:var(--ams-txt3); letter-spacing:.02em; }}
#ams-head .live {{ display:inline-flex; align-items:center; gap:6px; color:var(--ams-txt2); }}
#ams-head .live::before {{ content:''; width:6px; height:6px; border-radius:50%;
  background:var(--ams-open); box-shadow:0 0 0 3px {T['open']}22; }}

/* ---------- tab nav: etichette in maiuscoletto tracciato ---------- */
.tabs > .tab-nav {{ gap:2px; border-bottom:1px solid var(--ams-line) !important; }}
.tabs > .tab-nav button {{ font:600 10.5px/1 var(--ams-sans) !important;
  letter-spacing:.1em; text-transform:uppercase; padding:12px 14px !important;
  border-bottom:2px solid transparent !important; }}
.tabs > .tab-nav button.selected {{ color:var(--ams-brand) !important;
  border-bottom-color:var(--ams-brand) !important; }}

/* ---------- sezioni ---------- */
.ams-sec {{ display:flex; align-items:baseline; gap:12px; margin:26px 0 10px; }}
.ams-sec::after {{ content:''; flex:1; height:1px; background:var(--ams-line-soft); }}
.ams-sec b {{ font:600 13px/1 var(--ams-sans); letter-spacing:-.005em; color:var(--ams-txt); }}
.ams-sec span {{ font:400 11px/1 var(--ams-sans); color:var(--ams-txt3); }}
.ams-eyebrow {{ font:600 10px/1 var(--ams-mono); letter-spacing:.14em;
  text-transform:uppercase; color:var(--ams-txt3); }}

/* ---------- superfici ---------- */
.ams-surface {{ background:var(--ams-ink800); border:1px solid var(--ams-line);
  border-radius:var(--ams-r); overflow:hidden; }}
.ams-pad {{ padding:16px 18px; }}
.ams-note {{ font:400 11px/1.5 var(--ams-sans); color:var(--ams-txt3); margin-top:8px; }}
.ams-note code {{ font-family:var(--ams-mono); font-size:10.5px; color:var(--ams-txt2);
  background:var(--ams-ink700); padding:1px 5px; border-radius:4px; }}

/* ---------- tabella densa con status rail ---------- */
.ams-wrap {{ background:var(--ams-ink800); border:1px solid var(--ams-line);
  border-radius:var(--ams-r); overflow:auto; max-height:640px; }}
.ams-table {{ width:100%; border-collapse:separate; border-spacing:0;
  font:400 12.5px/1.45 var(--ams-sans); }}
.ams-table th {{ position:sticky; top:0; z-index:2; text-align:left;
  padding:9px 12px; background:var(--ams-ink700); color:var(--ams-txt2);
  font:600 9.5px/1 var(--ams-mono); letter-spacing:.11em; text-transform:uppercase;
  white-space:nowrap; border-bottom:1px solid var(--ams-line); }}
.ams-table th.c-n {{ text-align:right; }}
.ams-table td {{ padding:7px 12px; color:var(--ams-txt);
  border-bottom:1px solid var(--ams-line-soft); vertical-align:top; }}
.ams-table tbody tr:last-child td {{ border-bottom:0; }}
.ams-table tbody tr:hover td {{ background:var(--ams-ink750); }}
/* il rail: 2px sul bordo sinistro della prima cella = stato leggibile di lato */
.ams-table td:first-child {{ border-left:2px solid transparent; padding-left:12px; }}
.r-open  td:first-child {{ border-left-color:var(--ams-open); }}
.r-work  td:first-child {{ border-left-color:var(--ams-work); }}
.r-hold  td:first-child {{ border-left-color:var(--ams-hold); }}
.r-closed td:first-child {{ border-left-color:var(--ams-closed); }}
.c-num {{ font:500 12px/1.45 var(--ams-mono); color:var(--ams-brand);
  white-space:nowrap; letter-spacing:-.01em; }}
.c-desc {{ max-width:520px; }}
.c-dt {{ font:400 11.5px/1.45 var(--ams-mono); color:var(--ams-txt2);
  white-space:nowrap; font-variant-numeric:tabular-nums; }}
.c-n {{ text-align:right; font:400 12px/1.45 var(--ams-mono);
  font-variant-numeric:tabular-nums; }}
.c-state {{ font:500 11.5px/1.45 var(--ams-sans); white-space:nowrap; }}
.c-muted {{ color:var(--ams-txt2); }}
.ams-cap {{ display:flex; align-items:center; gap:10px; margin-top:8px;
  font:400 11px/1 var(--ams-mono); color:var(--ams-txt3); letter-spacing:.02em; }}
.ams-cap b {{ color:var(--ams-txt2); font-weight:500; }}

/* ---------- chip / persone ---------- */
.chip {{ display:inline-block; padding:1px 7px; border-radius:5px;
  font:600 10px/1.6 var(--ams-mono); letter-spacing:.03em; white-space:nowrap; }}
.chip-ghost {{ color:var(--ams-txt3); font-style:italic; font-family:var(--ams-sans);
  font-weight:400; font-size:11.5px; }}
.who {{ display:inline-flex; align-items:center; gap:6px;
  font:500 11.5px/1.45 var(--ams-sans); white-space:nowrap; }}
.who i {{ width:5px; height:5px; border-radius:50%; flex:0 0 auto; }}

/* ---------- strip metriche ---------- */
.ams-metrics {{ display:flex; flex-wrap:wrap; gap:1px; background:var(--ams-line);
  border:1px solid var(--ams-line); border-radius:var(--ams-r); overflow:hidden; }}
.m {{ flex:1 1 128px; background:var(--ams-ink800); padding:12px 14px; min-width:0; }}
.m-lead {{ flex:1 1 168px; }}
.m .v {{ font:600 26px/1.05 var(--ams-mono); letter-spacing:-.03em;
  font-variant-numeric:tabular-nums; color:var(--ams-txt); }}
.m-lead .v {{ font-size:32px; }}
.m .k {{ margin-top:5px; font:600 9.5px/1.3 var(--ams-mono); letter-spacing:.11em;
  text-transform:uppercase; color:var(--ams-txt3); }}
.m .h {{ margin-top:3px; font:400 10.5px/1.35 var(--ams-sans); color:var(--ams-txt3); }}

/* ---------- week strip (finestra SAL) ---------- */
.ws {{ background:var(--ams-ink800); border:1px solid var(--ams-line);
  border-radius:var(--ams-r); padding:14px 16px 12px; }}
.ws-top {{ display:flex; align-items:baseline; gap:10px; margin-bottom:11px;
  font:400 11px/1 var(--ams-mono); color:var(--ams-txt3); }}
.ws-top b {{ font:600 12px/1 var(--ams-sans); color:var(--ams-txt); letter-spacing:.01em; }}
.ws-bar {{ position:relative; height:6px; border-radius:3px;
  background:var(--ams-ink700); overflow:hidden; }}
.ws-fill {{ position:absolute; inset:0 auto 0 0;
  background:linear-gradient(90deg, var(--ams-brand-mid), var(--ams-brand)); }}
.ws-now {{ position:absolute; top:-5px; width:2px; height:16px;
  background:var(--ams-txt); border-radius:1px; }}
.ws-ends {{ display:flex; justify-content:space-between; margin-top:7px;
  font:400 10.5px/1 var(--ams-mono); color:var(--ams-txt3); }}

/* ---------- stato vuoto / messaggi ---------- */
.ams-empty {{ background:var(--ams-ink800); border:1px dashed var(--ams-line);
  border-radius:var(--ams-r); padding:26px 20px; text-align:center; }}
.ams-empty b {{ display:block; font:500 13px/1.4 var(--ams-sans); color:var(--ams-txt2); }}
.ams-empty span {{ display:block; margin-top:5px; font:400 11.5px/1.5 var(--ams-sans);
  color:var(--ams-txt3); }}
.ams-msg {{ display:flex; gap:10px; align-items:flex-start; padding:11px 14px;
  border-radius:var(--ams-r); font:400 12.5px/1.5 var(--ams-sans); margin-bottom:10px;
  border:1px solid var(--ams-line); background:var(--ams-ink800); }}
.ams-msg .bar {{ width:2px; align-self:stretch; border-radius:1px; flex:0 0 auto; }}
.ams-err {{ border-color:{T['crit']}44; }}
.ams-err .bar {{ background:var(--ams-crit); }}
.ams-ok .bar {{ background:var(--ams-open); }}
.ams-busy .bar {{ background:var(--ams-brand); }}
.ams-msg code {{ font-family:var(--ams-mono); font-size:11px; }}
.spin {{ width:12px; height:12px; flex:0 0 auto; margin-top:3px; border-radius:50%;
  border:2px solid var(--ams-brand-dim); border-top-color:var(--ams-brand);
  animation:amsspin .7s linear infinite; }}
@keyframes amsspin {{ to {{ transform:rotate(360deg); }} }}
@media (prefers-reduced-motion:reduce) {{ .spin {{ animation-duration:2.4s; }} }}

/* ---------- scheda analisi ---------- */
.an {{ background:var(--ams-ink800); border:1px solid var(--ams-line);
  border-radius:var(--ams-r); overflow:hidden; margin-bottom:12px; }}
.an-hd {{ display:flex; align-items:center; gap:10px; flex-wrap:wrap;
  padding:13px 16px; border-bottom:1px solid var(--ams-line);
  background:var(--ams-ink700); }}
.an-hd .id {{ font:600 13px/1 var(--ams-mono); color:var(--ams-brand); }}
.an-bd {{ padding:4px 16px 16px; }}
.an-row {{ padding:12px 0; border-bottom:1px solid var(--ams-line-soft); }}
.an-row:last-child {{ border-bottom:0; }}
.an-k {{ font:600 9.5px/1 var(--ams-mono); letter-spacing:.12em; text-transform:uppercase;
  color:var(--ams-txt3); margin-bottom:6px; }}
.an-v {{ font:400 13px/1.6 var(--ams-sans); color:var(--ams-txt); }}
.an-v ol, .an-v ul {{ margin:0; padding-left:18px; }}
.an-v li {{ margin:3px 0; }}
.an-v pre {{ margin:4px 0 0; padding:9px 11px; background:var(--ams-ink900);
  border:1px solid var(--ams-line); border-radius:6px; overflow-x:auto;
  font:400 11.5px/1.55 var(--ams-mono); color:var(--ams-txt); white-space:pre-wrap; }}
.an-src {{ padding:11px 16px; border-top:1px solid var(--ams-line);
  background:var(--ams-ink900); font:400 11px/1.7 var(--ams-sans); color:var(--ams-txt3); }}
.meter {{ display:inline-block; width:54px; height:4px; border-radius:2px;
  background:var(--ams-ink900); overflow:hidden; vertical-align:middle; margin-left:2px; }}
.meter i {{ display:block; height:100%; background:var(--ams-brand); }}

/* ---------- allegati compatti ---------- */
.ams-file {{ max-height:132px !important; overflow:auto; }}
.ams-file .center, .ams-file .wrap {{ min-height:0 !important; padding:7px !important; }}

/* ---------- responsive: sotto i 980px cadono le colonne accessorie ---------- */
@media (max-width:980px) {{
  .m {{ flex-basis:calc(50% - 1px); }}
  .col-2 {{ display:none; }}
  .c-desc {{ max-width:260px; }}
}}

/* ---------- gr.Chatbot — SELETTORI VERIFICATI SU GRADIO 4.44.1 ----------
   Unico punto del file che tocca classi interne di Gradio: la chat non usa le
   .ams-*, prende i colori dalle variabili del tema, che _theme() non copre.
   Invece di competere con la specificità dei selettori interni (che sono del
   tipo .message.svelte-<hash>, e l'hash cambia a ogni build) ridefiniamo QUELLE
   variabili dentro il solo contenitore della chat: nessun !important e nessuna
   dipendenza dall'hash. Classi lette dal CSS del pacchetto 4.44.1.
   Se un upgrade le rinomina, queste regole non agganciano più nulla e la chat
   torna ai default di Gradio: si imbruttisce, non si rompe. È qui che si
   guarda dopo un aggiornamento di gradio. */
.bubble-wrap, .panel-wrap, .message-wrap {{
  --background-fill-secondary:var(--ams-ink800);       /* fondo bolla bot */
  --color-accent-soft:var(--ams-brand-dim);            /* fondo bolla utente */
  --border-color-primary:var(--ams-line);              /* bordo bolla bot */
  --border-color-accent-subdued:var(--ams-brand-mid);  /* bordo bolla utente */
  --border-color-accent:var(--ams-brand-mid);
  --body-text-color:var(--ams-txt);                    /* testo dei messaggi */
  --color-text-link:var(--ams-brand);
  --code-background-fill:var(--ams-ink900);            /* blocchi pre e code */
  --panel-border-color:var(--ams-line);
}}
/* Area di scorrimento: nello slot light Gradio non le assegna uno sfondo, così
   erediterebbe quello del blocco. Nello slot dark lo assegna leggendo
   --background-fill-secondary, che qui sopra è già la nostra superficie: la
   chat resta leggibile anche se il pin del tema non dovesse agganciare. */
.bubble-wrap, .panel-wrap {{ background:var(--ams-ink900); }}
"""


# Gradio 4.44 decide il tema NEL BROWSER: usa `?__theme=` se c'è, altrimenti
# prefers-color-scheme del sistema. _theme() però scrive la palette scura SOLO
# negli slot light di gr.themes.Base(): chi ha il sistema in dark riceve i
# default scuri di Gradio su tutte le variabili non impostate, e gr.Chatbot —
# che non usa le classi .ams-* di _css() — finiva con testo chiaro su bolla
# chiara. Da qui il "solo per alcuni utenti".
# Fissiamo lo slot "light": controintuitivo ma corretto, è quello che contiene
# i NOSTRI colori scuri. In 4.44 non esiste una leva lato server (nessun
# GRADIO_THEME, nessun parametro di Blocks/launch): la query string è l'unico
# punto di aggancio. La guardia sul valore evita il loop di redirect.
FORCE_LIGHT_JS = """
() => {
    const url = new URL(window.location.href);
    if (url.searchParams.get('__theme') !== 'light') {
        url.searchParams.set('__theme', 'light');
        window.location.replace(url.toString());
    }
}
"""


# --- primitive di messaggio --------------------------------------------------
def _msg(kind: str, text: str, icon_html: str = "") -> str:
    return (f'<div class="ams-msg ams-{kind}"><span class="bar"></span>{icon_html}'
            f'<div>{text}</div></div>')


def _err(m):
    return _msg("err", f"<b>Non è stato possibile completare l'operazione.</b><br>"
                       f"<code>{html.escape(str(m))[:600]}</code>")


def _ok(m):
    return _msg("ok", html.escape(str(m)))


def _spinner(msg):
    return _msg("busy", f'<span style="color:var(--ams-txt2)">{html.escape(str(msg))}</span>',
                '<span class="spin"></span>')


def _empty(title: str, action: str = "") -> str:
    return (f'<div class="ams-empty"><b>{html.escape(title)}</b>'
            + (f"<span>{html.escape(action)}</span>" if action else "") + "</div>")


def _sec(title, sub=""):
    return f'<div class="ams-sec"><b>{html.escape(title)}</b><span>{html.escape(sub)}</span></div>'


# --- stato: un solo posto decide colore ed etichetta ------------------------
def _state_meta(s) -> tuple:
    """Ritorna (classe_riga, colore) per uno stato. Ordine di precedenza:
    closed batte blocked (es. 'Pending Closure')."""
    sl = _s(s).lower()
    if not sl:
        return "", T["txt3"]
    if is_closed_state(sl):
        return "r-closed", T["closed"]
    if is_blocked_state(sl):
        return "r-hold", T["hold"]
    if "progress" in sl or "lavoraz" in sl or "assigned" in sl:
        return "r-work", T["work"]
    return "r-open", T["open"]


def _state_cell(s) -> str:
    _, col = _state_meta(s)
    return f'<span class="c-state" style="color:{col}">{html.escape(_s(s) or "—")}</span>'


def _state_pill(s):     # compat: usata altrove
    return _state_cell(s)


def _pri_cell(p) -> str:
    """P1/P2 sono chip (richiedono attenzione), P3/P4 testo quieto: meno rumore
    visivo a parità di informazione."""
    raw = _s(p)
    if not raw:
        return '<span class="c-muted">—</span>'
    low = raw.lower()
    short = next((f"P{n}" for n in "1234" if n in raw), raw[:6].upper())
    if "1" in raw or "critical" in low or "critico" in low:
        c = T["crit"]
    elif "2" in raw or "high" in low or "alta" in low:
        c = T["hold"]
    else:
        return f'<span class="c-muted" style="font:500 11.5px var(--ams-mono)">{html.escape(short)}</span>'
    return f'<span class="chip" style="color:{c};background:{c}1F">{html.escape(short)}</span>'


_ASSIGNEE_PALETTE = ["#F48FB1", "#B39DDB", "#68D391", "#63B3ED", "#F6AD55",
                     "#4FD1C5", "#FC8181", "#F6E05E", "#FF5CB4", "#9F7AEA"]


def _assignee_color(name: str) -> str:
    """Colore stabile per persona: per posizione in team.members se presente,
    altrimenti derivato dal nome. Basta aggiungere i nomi in team.members."""
    members = cfg("team.members", [])
    idx = members.index(name) if name in members else sum(map(ord, name))
    return _ASSIGNEE_PALETTE[idx % len(_ASSIGNEE_PALETTE)]


def _assignee_pill(who):
    w = _s(who)
    if not w or "assegnare" in w.lower():
        return '<span class="chip-ghost">non assegnato</span>'
    c = _assignee_color(w)
    return (f'<span class="who" style="color:{c}"><i style="background:{c}"></i>'
            f"{html.escape(w)}</span>")


# --- tabelle ----------------------------------------------------------------
def _cell(kind, v) -> str:
    if kind == "num":
        return f'<td class="c-num">{html.escape(_s(v))}</td>'
    if kind == "desc":
        return f'<td class="c-desc">{html.escape(_s(v)[:300])}</td>'
    if kind == "state":
        return f"<td>{_state_cell(v)}</td>"
    if kind == "pri":
        return f"<td>{_pri_cell(v)}</td>"
    if kind == "who":
        return f"<td>{_assignee_pill(v)}</td>"
    if kind == "dt":
        return f'<td class="c-dt col-2">{html.escape(_s(v)[:16])}</td>'
    if kind == "n":
        return f'<td class="c-n">{html.escape(_s(v))}</td>'
    if kind == "type":
        t = _s(v).replace("request_item", "RITM").replace("incident", "INC").upper()
        return f'<td class="c-dt col-2">{html.escape(t)}</td>'
    return f'<td class="c-muted">{html.escape(_s(v))}</td>'


def _table_html(df, columns, empty_msg: str, footer: str = "", action: str = "") -> str:
    """columns = [(intestazione, chiave_df, tipo_cella)].
    La riga prende la classe del rail dallo stato, se la colonna 'state' c'è."""
    if df is None or df.empty:
        return _empty(empty_msg, action)
    head = "".join(
        f'<th class="{"c-n col-h" if k == "n" else ("col-2" if k in ("dt", "type") else "")}">'
        f"{html.escape(h)}</th>" for h, _, k in columns)
    rows = []
    for _, r in df.iterrows():
        cls, _c = _state_meta(r.get("state"))
        rows.append(f'<tr class="{cls}">'
                    + "".join(_cell(k, r.get(key)) for _, key, k in columns) + "</tr>")
    cap = (f'<div class="ams-cap"><b>{len(df)}</b> ticket'
           + (f" · {html.escape(footer)}" if footer else "") + "</div>")
    return (f'<div class="ams-wrap"><table class="ams-table"><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>{cap}')


DASH_COLS = [("Ticket", "number", "num"), ("Descrizione", "short_description", "desc"),
             ("Stato", "state", "state"), ("Pri", "priority", "pri"),
             ("Assegnatario", "assignee", "who")]


def _dashboard_table(df):
    return _table_html(
        df, DASH_COLS,
        "Nessun ticket lavorabile.",
        "esclusi chiusi/risolti e on hold/pending — li trovi nel tab SAL",
        "Sincronizza da ServiceNow o importa un Excel per popolare la lista.")


def _legend() -> str:
    items = [("Nuovo / aperto", T["open"]), ("In lavorazione", T["work"]),
             ("On hold / pending", T["hold"]), ("Chiuso / risolto", T["closed"])]
    dots = " ".join(
        f'<span style="display:inline-flex;align-items:center;gap:5px;margin-right:14px">'
        f'<i style="width:2px;height:11px;border-radius:1px;background:{c};display:inline-block">'
        f"</i>{html.escape(t)}</span>" for t, c in items)
    return f'<div class="ams-cap" style="margin:0 0 6px">{dots}</div>'


# --- metriche ---------------------------------------------------------------
def _m(label, value, color=None, hint="", lead=False) -> str:
    col = f' style="color:{color}"' if color else ""
    h = f'<div class="h">{html.escape(hint)}</div>' if hint else ""
    return (f'<div class="m{" m-lead" if lead else ""}"><div class="v"{col}>{value}</div>'
            f'<div class="k">{html.escape(label)}</div>{h}</div>')


def _metrics(cards: list) -> str:
    return f'<div class="ams-metrics">{"".join(cards)}</div>'


def _week_strip(start, end, label: str, k: dict) -> str:
    """Signature element: la finestra SAL (lun 14:00 → lun 13:30) con il marcatore
    di 'adesso'. Serve a rispondere a colpo d'occhio a 'quanto siamo dentro la
    settimana e come stiamo andando', che nessun dashboard generico mostra."""
    now = _now()
    span = max((end - start).total_seconds(), 1)
    pct = min(max((now - start).total_seconds() / span, 0.0), 1.0) * 100
    inside = start <= now <= end
    ap, ch = int(k.get("aperti", 0) or 0), int(k.get("chiusi", 0) or 0)
    bl = int(k.get("blocked_sett", 0) or 0)
    marker = (f'<span class="ws-now" style="left:calc({pct:.1f}% - 1px)"></span>'
              if inside else "")
    return (f'<div class="ws"><div class="ws-top"><b>Settimana di servizio</b>'
            f'<span>{html.escape(label)}</span>'
            f'<span style="margin-left:auto">{ap} aperti · {ch} chiusi · {bl} blocked</span></div>'
            f'<div class="ws-bar"><span class="ws-fill" style="width:{pct:.1f}%"></span>'
            f"{marker}</div>"
            f'<div class="ws-ends"><span>apertura finestra</span>'
            f'<span>{"in corso" if inside else "finestra chiusa"}</span>'
            f"<span>chiusura 13:30</span></div></div>")


def _sal_kpi_html(k: dict) -> str:
    aging = int(cfg("sal.aging_alert_days", 30))
    net = int(k.get("net_flow", 0) or 0)
    mttr = k.get("mttr_gg")
    mttr_txt = "—" if mttr is None else f"{float(mttr):.1f}<span style='font-size:14px'> gg</span>"
    cards = [
        _m("Aperti settimana", int(k.get("aperti", 0) or 0), T["brand"], lead=True),
        _m("Chiusi settimana", int(k.get("chiusi", 0) or 0), T["open"], lead=True),
        _m("Flusso netto", f"{net:+d}", T["crit"] if net > 0 else T["open"],
           "aperti − chiusi", lead=True),
        _m("Blocked settimana", int(k.get("blocked_sett", 0) or 0), T["hold"],
           "con attività nella finestra"),
        _m("Blocked totale", int(k.get("blocked_tot", 0) or 0), T["hold"]),
        _m("Backlog aperto", int(k.get("backlog", 0) or 0)),
        _m(f"Backlog > {aging} gg", int(k.get("backlog_vecchio", 0) or 0), T["crit"]),
        _m("MTTR settimana", mttr_txt, hint="media apertura→chiusura"),
    ]
    out = _metrics(cards)
    senza = int(k.get("senza_data", 0) or 0)
    if senza:
        out += _msg("err", f"<b>{senza} ticket aperti senza <code>opened_at</code>.</b> Non "
                           f"entrano nei conteggi settimanali: rilancia Sync ServiceNow.")
    return out


# --- scheda analisi ---------------------------------------------------------
def _sources_html(sources):
    if not sources:
        return ""
    b = " ".join(
        f'<span class="chip" style="color:var(--ams-txt2);background:var(--ams-ink700);'
        f'margin-right:5px">{html.escape(_s(x.get("kind")))} · {html.escape(_s(x.get("ref")))}'
        f"</span>" for x in sources)
    return f'<div class="an-src">Fonti consultate &nbsp;{b}</div>'


def _analysis_html(a):
    if not a:
        return _empty("Nessuna analisi per questo ticket.",
                      "Premi Analizza per generarne una.")

    def rows(key, val):
        return f'<div class="an-row"><div class="an-k">{key}</div><div class="an-v">{val}</div></div>'

    def lst(x, ordered=False):
        try:
            items = json.loads(x) or []
        except Exception:
            return html.escape(_s(x))
        if not items:
            return '<span class="c-muted">—</span>'
        tag = "ol" if ordered else "ul"
        return f"<{tag}>" + "".join(f"<li>{html.escape(str(i))}</li>" for i in items) + f"</{tag}>"

    def code(x):
        try:
            items = json.loads(x) or []
        except Exception:
            items = [_s(x)] if _s(x) else []
        if not items:
            return '<span class="c-muted">nessun comando proposto</span>'
        return "".join(f"<pre>{html.escape(str(i))}</pre>" for i in items)

    conf = float(a.get("confidence", 0) or 0)
    confirmed = bool(a.get("root_cause_confirmed"))
    rc_col = T["open"] if confirmed else T["hold"]
    rc_txt = "root cause confermata" if confirmed else "ipotesi da verificare"
    sev = _s(a.get("severity"))
    sev_col = {"HIGH": T["crit"], "MEDIUM": T["hold"]}.get(sev.upper(), T["txt2"])
    try:
        srcs = json.loads(a.get("sources_used") or "[]")
    except Exception:
        srcs = []
    return (
        '<div class="an"><div class="an-hd">'
        f'<span class="id">{html.escape(_s(a.get("number")))}</span>'
        f'<span class="chip" style="color:var(--ams-txt2);background:var(--ams-ink900)">'
        f'{html.escape(_s(a.get("problem_type")))}</span>'
        f'<span class="chip" style="color:{sev_col};background:{sev_col}1F">{html.escape(sev)}</span>'
        f'<span class="chip" style="color:{rc_col};background:{rc_col}1F">{rc_txt}</span>'
        f'<span style="margin-left:auto;font:400 10.5px var(--ams-mono);color:var(--ams-txt3)">'
        f'confidence {conf:.0%}<span class="meter"><i style="width:{conf*100:.0f}%"></i></span>'
        f'&nbsp;&nbsp;{html.escape(_s(a.get("status")))}</span></div><div class="an-bd">'
        + rows("Riassunto", html.escape(_s(a.get("summary"))))
        + rows("Evidenza", html.escape(_s(a.get("evidence")) or "—"))
        + rows("Ipotesi", lst(a.get("hypothesis", "[]")))
        + rows("Passi di diagnosi", lst(a.get("steps", "[]"), ordered=True))
        + rows("Soluzione proposta", html.escape(_s(a.get("proposed_solution"))))
        + rows("Comandi SQL", code(a.get("sql_commands", "[]")))
        + rows("Ragionamento", html.escape(_s(a.get("reasoning"))))
        + rows("Effort stimato", html.escape(_s(a.get("estimated_effort")) or "—"))
        + "</div>" + _sources_html(srcs) + "</div>")


SAL_OPEN_COLS = [("Ticket", "number", "num"), ("Tipo", "ticket_type", "type"),
                 ("Descrizione", "short_description", "desc"), ("Stato", "state", "state"),
                 ("Pri", "priority", "pri"), ("Assegnatario", "assignee", "who"),
                 ("Aperto il", "opened_at", "dt")]
SAL_CLOSED_COLS = [("Ticket", "number", "num"), ("Tipo", "ticket_type", "type"),
                   ("Descrizione", "short_description", "desc"), ("Stato", "state", "state"),
                   ("Assegnatario", "assignee", "who"), ("Aperto il", "opened_at", "dt"),
                   ("Chiuso il", "closed_at", "dt"), ("Giorni", "giorni", "n")]
SAL_BLOCKED_COLS = [("Ticket", "number", "num"), ("Descrizione", "short_description", "desc"),
                    ("Stato", "state", "state"), ("Pri", "priority", "pri"),
                    ("Assegnatario", "assignee", "who"), ("Aperto il", "opened_at", "dt"),
                    ("Ultimo update", "updated_at", "dt")]
SAL_BLOCKED_ALL_COLS = SAL_BLOCKED_COLS + [("Età gg", "eta_gg", "n")]


def sal_report_html(offset_weeks: int = 0) -> str:
    """Pagina SAL completa: week strip + KPI + 4 tabelle.
    Ogni sezione fallisce in isolamento: un errore su una non oscura le altre."""
    try:
        start, end, label = sal_window(offset_weeks)
    except Exception as e:
        return _err(f"Finestra SAL non calcolabile: {e}")

    out, k = [], {}
    try:
        k = sal_kpi(start, end)
    except Exception as e:
        out.append(_err(f"KPI non calcolabili (bootstrap eseguito?): {str(e)[:300]}"))
    out.insert(0, _week_strip(start, end, label, k))
    if k:
        out.append('<div style="height:12px"></div>' + _sal_kpi_html(k))

    def block(title, sub, fn, cols, empty, action=""):
        out.append(_sec(title, sub))
        try:
            out.append(_table_html(fn(), cols, empty, action=action))
        except Exception as e:
            out.append(_err(f"{title}: {str(e)[:250]}"))

    block("Aperti questa settimana", "data di apertura nella finestra",
          lambda: sal_opened(start, end), SAL_OPEN_COLS,
          "Nessun ticket aperto nella finestra.")
    block("Chiusi questa settimana", "data di chiusura nella finestra",
          lambda: sal_closed_week(start, end), SAL_CLOSED_COLS,
          "Nessun ticket chiuso nella finestra.",
          "Se ne aspettavi, verifica che la sync porti i non-attivi.")
    block("Blocked questa settimana", "on hold / pending con attività nella finestra",
          lambda: sal_blocked_week(start, end), SAL_BLOCKED_COLS,
          "Nessun ticket blocked nella finestra.")
    block("Blocked totale", "tutti gli on hold / pending, dal più vecchio",
          sal_blocked_all, SAL_BLOCKED_ALL_COLS, "Nessun ticket blocked.")

    out.append('<div class="ams-note">Le date sono in UTC come salvate; la finestra è calcolata '
               'sull\'orario locale di <code>sal.tz</code>. "Blocked" = stato che contiene una '
               'keyword di <code>sal.blocked_state_keywords</code>.</div>')
    return "".join(out)


def build_app():
    import gradio as gr
    _patch_gradio_client()
    members = cfg("team.members", [])
    week_choices = [("Settimana corrente", 0)] + [
        (f"Settimana −{i}", -i) for i in range(1, int(cfg("sal.weeks_selectable", 8)) + 1)]

    def h_dash(f):
        try:
            return _legend() + _dashboard_table(
                list_tickets(assignee=f or "Tutti", hide_blocked=True))
        except Exception as e:
            return _err(f"Ticket non leggibili (bootstrap eseguito?): {e}")

    def h_sync():
        return _spinner("Sincronizzazione ServiceNow in corso…")

    def h_sync_run(f):
        try:
            return _ok(snow_sync()), h_dash(f)
        except Exception as e:
            return _err(f"Sync fallita: {e}"), gr.update()

    def h_excel(file, f):
        if not file:
            return _err("Nessun file selezionato."), gr.update()
        try:
            return _ok(excel_import(file.name)), h_dash(f)
        except Exception as e:
            return _err(f"Import fallito: {e}"), gr.update()

    def h_assign(num, who, f):
        if not num or not who:
            return _err("Servono numero ticket e assegnatario."), gr.update()
        try:
            assign(num.strip(), who)
            return _ok(f"{num.strip()} assegnato a {who}."), h_dash(f)
        except Exception as e:
            return _err(str(e)), gr.update()

    def h_sal(week):
        try:
            return sal_report_html(int(week or 0))
        except Exception as e:
            logger.exception("sal")
            return _err(f"SAL non disponibile: {e}")

    def h_analyze(num, files):
        if not num:
            yield _err("Inserisci un numero ticket."); return
        t = get_ticket(num.strip())
        if not t:
            yield _err(f"Ticket {num.strip()} non trovato in tabella."); return
        try:
            for kind, payload in analyze_ticket_stream(t, files):
                yield _spinner(payload) if kind == "status" else _analysis_html(payload)
        except Exception as e:
            logger.exception("analyze")
            yield _err(f"Analisi fallita: {e}")

    def h_load_chat(num):
        if not num:
            return [], ""
        pairs, pend = [], None
        for h in get_chat_history(num.strip()):
            if h["role"] == "user":
                pend = h["message"]
            elif h["role"] == "assistant":
                pairs.append([pend or "", h["message"]]); pend = None
        return pairs, num.strip()

    def h_send(num, msg, hist, files):
        if not num:
            return hist, "Apri prima un ticket.", None
        if not msg and not files:
            return hist, "", None
        try:
            reply, _ = chat_answer(num.strip(), msg or "(vedi allegati)", files)
        except Exception as e:
            reply = f"⚠️ {e}"
        return (hist or []) + [[msg, reply]], "", None

    def h_gsend(msg, hist, files):
        hist = hist or []
        if not msg and not files:
            yield hist, "", None; return
        # feedback immediato: mostra il messaggio + un indicatore, poi la risposta finale
        yield hist + [[msg, "⏳ Sto cercando nei documenti e nei dati…"]], "", None
        try:
            reply, _ = assistant_answer(msg or "(vedi allegati)", hist, files)
        except Exception as e:
            reply = f"⚠️ {e}"
        yield hist + [[msg, reply]], "", None

    def h_chat_reanalyze(num):
        """Rigenera l'analisi del ticket TENENDO CONTO della chat (correzioni)."""
        if not num:
            yield _err("Apri prima un ticket."); return
        t = get_ticket(num.strip())
        if not t:
            yield _err(f"Ticket {num.strip()} non trovato."); return
        try:
            for kind, payload in analyze_ticket_stream(t):
                yield _spinner(payload) if kind == "status" else _analysis_html(payload)
        except Exception as e:
            logger.exception("reanalyze")
            yield _err(f"Analisi fallita: {e}")

    def h_teach(num, rc, sol):
        if not num:
            return _err("Inserisci un ticket.")
        try:
            return _ok(teach_correction(num.strip(), rc or "", sol or ""))
        except Exception as e:
            return _err(str(e))

    def h_load_analysis(num):
        return _analysis_html(get_latest_analysis(num.strip())) if num else _err("Inserisci un ticket.")

    def h_approve(num):
        if not num:
            return _err("Inserisci un ticket.")
        try:
            return _ok(approve_analysis(num.strip()))
        except Exception as e:
            return _err(str(e))

    def h_reject(num, motivo):
        if not num:
            return _err("Inserisci un ticket.")
        try:
            a = get_latest_analysis(num.strip())
            if not a:
                return _err("Nessuna analisi da rifiutare.")
            set_analysis_status(a["analysis_id"], "rejected", motivo or "")
            return _ok(f"Analisi di {num.strip()} rifiutata.")
        except Exception as e:
            return _err(str(e))

    def h_repo(text, inc_closed):
        try:
            return _table_html(
                list_tickets(text=text or "", include_closed=bool(inc_closed)), DASH_COLS,
                "Nessun ticket corrisponde alla ricerca.",
                "inclusi i chiusi" if inc_closed else "solo ticket aperti",
                "Prova con il numero del ticket o una parola della descrizione.")
        except Exception as e:
            return _err(str(e))

    def h_detail(num):
        if not num:
            return _err("Inserisci un ticket.")
        t = get_ticket(num.strip())
        if not t:
            return _err("Ticket non trovato.")
        _, col = _state_meta(t.get("state"))
        head = (f'<div class="an"><div class="an-hd">'
                f'<span class="id">{html.escape(_s(t.get("number")))}</span>'
                f'{_state_cell(t.get("state"))}{_pri_cell(t.get("priority"))}'
                f'<span style="margin-left:auto;font:400 10.5px var(--ams-mono);'
                f'color:var(--ams-txt3)">{html.escape(_s(t.get("assignment_group")))}</span>'
                f'</div><div class="an-bd">'
                f'<div class="an-row"><div class="an-k">Titolo</div><div class="an-v">'
                f'{html.escape(_s(t.get("short_description")))}</div></div>'
                f'<div class="an-row"><div class="an-k">Descrizione</div><div class="an-v">'
                f'{html.escape(_s(t.get("description"))[:2000])}</div></div></div></div>')
        a = get_latest_analysis(num.strip())
        return head + (_analysis_html(a) if a else
                       _empty("Nessuna analisi per questo ticket.",
                              "Generala dal tab Analisi."))

    def h_costs():
        try:
            df = run_sql(f"""SELECT usage_date, model_name, input_tokens, output_tokens, calls
                FROM {table('token_usage')} WHERE usage_date >= current_date() - INTERVAL 30 DAYS
                ORDER BY usage_date DESC, model_name""")
        except Exception as e:
            return _err(f"Metriche non disponibili: {e}")
        if df is None or df.empty:
            return _empty("Nessun consumo registrato negli ultimi 30 giorni.",
                          "I token si registrano usando Analisi e Chat.")
        prices, default = cfg("models.prices", {}), cfg("models.default_price", [3.0, 15.0])
        tc = ti = to = tr = 0
        body = []
        for _, r in df.iterrows():
            model = _s(r["model_name"])
            tin, tout, req = int(r["input_tokens"] or 0), int(r["output_tokens"] or 0), int(r["calls"] or 0)
            p_in, p_out = prices.get(model, default)
            cost = tin / 1e6 * p_in + tout / 1e6 * p_out
            tc += cost; ti += tin; to += tout; tr += req
            body.append(
                f'<tr><td class="c-dt">{html.escape(_s(r["usage_date"])[:10])}</td>'
                f'<td class="c-num">{html.escape(model)}</td>'
                f'<td class="c-n">{req:,}</td><td class="c-n">{tin:,}</td>'
                f'<td class="c-n">{tout:,}</td>'
                f'<td class="c-n" style="color:var(--ams-brand)">€{cost:,.2f}</td></tr>')
        kpis = _metrics([
            _m("Costo stimato 30 gg", f'<span style="font-size:20px">~€</span>{tc:,.2f}',
               T["brand"], lead=True),
            _m("Richieste", f"{tr:,}"), _m("Token input", f"{ti:,}"),
            _m("Token output", f"{to:,}")])
        tbl = ('<div class="ams-wrap" style="margin-top:12px"><table class="ams-table"><thead><tr>'
               '<th>Giorno</th><th>Modello</th><th class="c-n">Richieste</th>'
               '<th class="c-n">Token in</th><th class="c-n">Token out</th>'
               '<th class="c-n">Costo stim.</th></tr></thead>'
               f'<tbody>{"".join(body)}</tbody></table></div>')
        return kpis + tbl + ('<div class="ams-note">I token sono esatti; il costo in € è una stima '
                             'su prezzi indicativi per 1M token, da aggiornare in '
                             '<code>models.prices</code>.</div>')

    def h_kb_list():
        try:
            df = list_knowledge()
        except Exception as e:
            return _err(f"Documentazione non leggibile (bootstrap eseguito?): {e}")
        if df is None or df.empty:
            return _empty("Nessun documento in knowledge base.",
                          "Incolla del testo o carica un file qui sopra.")
        rows = "".join(
            f'<tr><td class="c-num">{html.escape(_s(r["title"]))}</td>'
            f'<td class="c-muted">{html.escape(_s(r["tags"]) or "—")}</td>'
            f'<td class="c-n">{int(r["caratteri"] or 0):,}</td>'
            f'<td class="c-dt">{html.escape(_s(r["created_at"])[:16])}</td></tr>'
            for _, r in df.iterrows())
        return ('<div class="ams-wrap"><table class="ams-table"><thead><tr><th>Titolo</th>'
                '<th>Tag</th><th class="c-n">Caratteri</th><th>Aggiunto</th></tr></thead>'
                f'<tbody>{rows}</tbody></table></div>')

    def h_kb_refresh():
        return h_kb_list(), gr.update(choices=knowledge_choices())

    def h_kb_add(title, content, tags, file):
        text = content or ""
        used_name = title
        if file:
            extracted = _extract_file_text(file.name)
            if extraction_failed(extracted):
                return (_err(extraction_reason(extracted)), h_kb_list(), content, gr.update())
            text = (text + "\n" + extracted).strip()
            used_name = title or os.path.basename(file.name)
        if not text.strip():
            return _err("Serve del testo o un file."), h_kb_list(), content, gr.update()
        try:
            msg = kb_add_document(used_name or "(senza titolo)", text, tags or "")
            return (_ok(msg), h_kb_list(), "", gr.update(choices=knowledge_choices()))
        except Exception as e:
            return _err(str(e)), h_kb_list(), content, gr.update()

    def h_kb_add_nb(paths, tags):
        if not (paths or "").strip():
            return _err("Incolla almeno un path."), h_kb_list(), gr.update(), paths
        try:
            msg = kb_add_notebooks(paths, tags or "")
            return (_ok(msg), h_kb_list(), gr.update(choices=knowledge_choices()), "")
        except Exception as e:
            return _err(str(e)), h_kb_list(), gr.update(), paths

    def h_kb_reindex():
        try:
            return _ok(kb_reindex(only_stale=True)), h_kb_list()
        except Exception as e:
            return _err(str(e)), h_kb_list()

    def h_kb_view(doc_id):
        if not doc_id:
            return _err("Seleziona un documento dal menu.")
        d = get_knowledge_doc(doc_id)
        if not d:
            return _err("Documento non trovato.")
        return (f'<div class="an"><div class="an-hd"><span class="id">'
                f'{html.escape(_s(d.get("title")))}</span>'
                f'<span class="chip" style="color:var(--ams-txt2);background:var(--ams-ink900)">'
                f'{html.escape(_s(d.get("tags")) or "senza tag")}</span></div>'
                f'<div class="an-bd"><div class="an-row"><div class="an-v"><pre>'
                f'{html.escape(_s(d.get("content")))}</pre></div></div></div></div>')

    def h_kb_delete(doc_id):
        if not doc_id:
            return _err("Seleziona un documento."), h_kb_list(), gr.update(), ""
        try:
            delete_knowledge(doc_id)
            return (_ok("Documento eliminato."), h_kb_list(),
                    gr.update(choices=knowledge_choices(), value=None), "")
        except Exception as e:
            return _err(str(e)), h_kb_list(), gr.update(), ""

    def h_kb_search(q):
        if not q:
            return ""
        res = search_knowledge(q)
        if not res:
            return _empty("Nessun risultato.", "Prova con termini più specifici.")
        return "".join(
            f'<div class="an"><div class="an-hd"><span class="id">{html.escape(_s(t))}</span>'
            + ("" if s is None else f'<span class="chip" style="color:var(--ams-txt2);'
                                    f'background:var(--ams-ink900)">sim {s:.2f}</span>')
            + f'</div><div class="an-bd"><div class="an-row"><div class="an-v">'
              f"{html.escape(str(c)[:500])}</div></div></div></div>" for t, c, s in res)

    with gr.Blocks(title=cfg("branding.header_title"), theme=_theme(), css=_css(),
                   js=FORCE_LIGHT_JS) as app:
        with gr.Row(elem_id="ams-head"):
            gr.HTML(
                f'<div class="hd"><span class="mark">AMS</span>'
                f'<h1>{html.escape(cfg("branding.header_title"))}</h1>'
                f'<span class="meta"><span>{html.escape(cfg("branding.header_subtitle", ""))}</span>'
                f'<span>{html.escape(_s(cfg("storage.schema")))}</span>'
                f'<span class="live">operativo</span></span></div>')
        with gr.Tabs():
            with gr.Tab("Dashboard"):
                gr.HTML('<div class="ams-note" style="margin:4px 0 10px">Solo ticket '
                        '<b style="color:var(--ams-txt2)">lavorabili</b>: esclusi chiusi/risolti '
                        'e on hold/pending. I blocked sono nel tab SAL.</div>')
                with gr.Row():
                    filtro = gr.Dropdown(["Tutti", "Da assegnare", *members], value="Tutti",
                                         label="Assegnatario", scale=3)
                    refresh = gr.Button("Aggiorna", scale=1)
                    syncb = gr.Button("Sincronizza ServiceNow", variant="primary", scale=2)
                excelb = gr.UploadButton("Importa Excel", file_types=[".xlsx", ".xls", ".xlsm"])
                status = gr.HTML()
                dash = gr.HTML()
                with gr.Accordion("Assegna un ticket", open=False):
                    with gr.Row():
                        an = gr.Textbox(label="Numero ticket", scale=2)
                        aw = gr.Dropdown(members, label="Assegna a", scale=2)
                        ab = gr.Button("Assegna", variant="primary", scale=1)
                refresh.click(h_dash, filtro, dash)
                filtro.change(h_dash, filtro, dash)
                syncb.click(h_sync, None, status).then(h_sync_run, filtro, [status, dash])
                excelb.upload(h_excel, [excelb, filtro], [status, dash])
                ab.click(h_assign, [an, aw, filtro], [status, dash])
            with gr.Tab("SAL"):
                gr.HTML('<div class="ams-note" style="margin:4px 0 10px">Settimana di servizio: '
                        'da <b style="color:var(--ams-txt2)">lunedì 14:00</b> al lunedì '
                        'successivo <b style="color:var(--ams-txt2)">13:30</b>, ora italiana.</div>')
                with gr.Row():
                    salw = gr.Dropdown(choices=week_choices, value=0, label="Settimana", scale=3)
                    salb = gr.Button("Calcola SAL", variant="primary", scale=1)
                salo = gr.HTML()
                salb.click(lambda: _spinner("Calcolo KPI e tabelle della settimana…"), None, salo) \
                    .then(h_sal, salw, salo)
                salw.change(lambda: _spinner("Calcolo KPI e tabelle della settimana…"), None, salo) \
                    .then(h_sal, salw, salo)
            with gr.Tab("Analisi"):
                with gr.Row():
                    ai = gr.Textbox(label="Numero ticket", scale=4,
                                    placeholder="es. INC0393819")
                    abtn = gr.Button("Analizza", variant="primary", scale=1)
                with gr.Accordion("Allega file — log, CSV, Excel, immagini", open=False):
                    afiles = gr.File(show_label=False, file_count="multiple", elem_classes="ams-file")
                ao = gr.HTML()
                with gr.Row():
                    aload = gr.Button("Mostra ultima analisi", scale=2)
                    aok = gr.Button("Approva", variant="primary", scale=1)
                    ako = gr.Button("Rifiuta", variant="stop", scale=1)
                amot = gr.Textbox(label="Motivo del rifiuto (opzionale)", lines=1)
                with gr.Accordion("Correggi e insegna — la tua risposta finisce in knowledge base",
                                  open=False):
                    with gr.Row():
                        atrc = gr.Textbox(label="Root cause corretta", scale=1)
                        atsol = gr.Textbox(label="Soluzione corretta", scale=1)
                    atbtn = gr.Button("Salva correzione", variant="primary")
                amsg = gr.HTML()
                abtn.click(h_analyze, [ai, afiles], ao)
                aload.click(h_load_analysis, ai, ao)
                aok.click(h_approve, ai, amsg)
                ako.click(h_reject, [ai, amot], amsg)
                atbtn.click(h_teach, [ai, atrc, atsol], amsg)
            with gr.Tab("Revisione"):
                gr.HTML('<div class="ams-note" style="margin:4px 0 10px">Apri la chat sul ticket, '
                        'digli cosa non torna, poi rigenera l\'analisi finché è corretta e '
                        'approvala.</div>')
                with gr.Row():
                    ci = gr.Textbox(label="Numero ticket", scale=4)
                    co = gr.Button("Apri chat", scale=1)
                cst = gr.State("")
                cb = gr.Chatbot(height=420, show_label=False)
                with gr.Accordion("Allega file", open=False):
                    cfiles = gr.File(show_label=False, file_count="multiple", elem_classes="ams-file")
                with gr.Row():
                    cm = gr.Textbox(label="Messaggio", scale=5)
                    cs = gr.Button("Invia", variant="primary", scale=1)
                co.click(h_load_chat, ci, [cb, cst])
                cs.click(h_send, [ci, cm, cb, cfiles], [cb, cm, cfiles])
                cm.submit(h_send, [ci, cm, cb, cfiles], [cb, cm, cfiles])
                gr.HTML(_sec("Rigenera con le correzioni", "tiene conto della chat qui sopra"))
                with gr.Row():
                    crb = gr.Button("Rigenera analisi", variant="primary", scale=2)
                    cok = gr.Button("Approva", scale=1)
                    cko = gr.Button("Rifiuta", variant="stop", scale=1)
                crev = gr.HTML()
                crb.click(h_chat_reanalyze, ci, crev)
                cok.click(h_approve, ci, crev)
                cko.click(lambda n: h_reject(n, "rifiutato da chat"), ci, crev)
            with gr.Tab("Assistente"):
                gr.HTML('<div class="ams-note" style="margin:4px 0 10px">Chat generale, non legata '
                        'a un ticket: usa documentazione, ticket validati e i dati con tool in '
                        'sola lettura.</div>')
                gcb = gr.Chatbot(height=460, show_label=False)
                with gr.Accordion("Allega file", open=False):
                    gfiles = gr.File(show_label=False, file_count="multiple", elem_classes="ams-file")
                with gr.Row():
                    gm = gr.Textbox(label="Domanda", scale=5, show_label=False,
                                    placeholder="Quali tabelle contengono lo stock EVA?")
                    gs = gr.Button("Invia", variant="primary", scale=1)
                gs.click(h_gsend, [gm, gcb, gfiles], [gcb, gm, gfiles])
                gm.submit(h_gsend, [gm, gcb, gfiles], [gcb, gm, gfiles])
            with gr.Tab("Repository"):
                with gr.Row():
                    pq = gr.Textbox(label="Cerca per numero o testo", scale=4)
                    pinc = gr.Checkbox(label="Includi chiusi", value=False, scale=1)
                    pb = gr.Button("Cerca", variant="primary", scale=1)
                pl = gr.HTML()
                with gr.Row():
                    pdn = gr.Textbox(label="Apri il dettaglio di un ticket", scale=4)
                    pdb = gr.Button("Apri", scale=1)
                pdet = gr.HTML()
                pb.click(h_repo, [pq, pinc], pl)
                pq.submit(h_repo, [pq, pinc], pl)
                pdb.click(h_detail, pdn, pdet)
            with gr.Tab("Documentazione"):
                gr.HTML('<div class="ams-note" style="margin:4px 0 10px">Documenti che l\'agente '
                        'consulta prima di ogni analisi, insieme ai ticket simili.</div>')
                with gr.Row():
                    kt = gr.Textbox(label="Titolo", scale=3)
                    ktags = gr.Textbox(label="Tag (opzionale)", scale=2)
                kc = gr.Textbox(label="Contenuto", lines=6, placeholder="Incolla qui il testo…")
                kfile = gr.File(label="…oppure carica un file (.md .txt .csv .pdf .docx .xlsx)",
                                file_count="single")
                kadd = gr.Button("Aggiungi documento", variant="primary")
                kmsg = gr.HTML()
                gr.HTML(_sec("Notebook dal workspace", "il path diventa il titolo del documento"))
                knb = gr.Textbox(label="Path (uno per riga; una cartella importa i notebook "
                                       "che contiene, anche nelle sottocartelle)",
                                 lines=3, placeholder="/Workspace/<cartella>/<notebook>")
                knbb = gr.Button("Importa notebook")
                gr.HTML(_sec("Documenti caricati"))
                with gr.Row():
                    kdoc = gr.Dropdown(label="Seleziona un documento", choices=[], scale=4)
                    krefresh = gr.Button("↻ Aggiorna", scale=1)
                    kview = gr.Button("Apri", scale=1)
                    kdel = gr.Button("Elimina", variant="stop", scale=1)
                kreidx = gr.Button("Reindicizza mancanti")
                kcontent = gr.HTML()
                klist = gr.HTML()
                gr.HTML(_sec("Prova la ricerca semantica", "come la vede l'agente"))
                with gr.Row():
                    ksq = gr.Textbox(label="Query", scale=4, show_label=False)
                    ksb = gr.Button("Cerca", scale=1)
                kso = gr.HTML()
                kadd.click(h_kb_add, [kt, kc, ktags, kfile], [kmsg, klist, kc, kdoc])
                knbb.click(h_kb_add_nb, [knb, ktags], [kmsg, klist, kdoc, knb])
                krefresh.click(h_kb_refresh, None, [klist, kdoc])
                kview.click(h_kb_view, kdoc, kcontent)
                kdel.click(h_kb_delete, kdoc, [kmsg, klist, kdoc, kcontent])
                kreidx.click(h_kb_reindex, None, [kmsg, klist])
                ksb.click(h_kb_search, ksq, kso)
            with gr.Tab("Costi"):
                cob = gr.Button("Aggiorna", variant="primary")
                coo = gr.HTML()
                cob.click(h_costs, None, coo)
        app.load(lambda: h_dash("Tutti"), None, dash)
        app.load(h_kb_refresh, None, [klist, kdoc])
    return app


def main():
    port = int(os.environ.get("DATABRICKS_APP_PORT", os.environ.get("PORT", "8080")))
    start_reindex_worker()
    build_app().launch(server_name="0.0.0.0", server_port=port, show_error=True)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "bootstrap":
        run_bootstrap()
    elif len(sys.argv) > 1 and sys.argv[1] == "reindex":
        # python app.py reindex        -> solo i documenti stantii
        # python app.py reindex all    -> ricostruisce tutto da capo
        print(kb_reindex(only_stale=(len(sys.argv) < 3 or sys.argv[2] != "all")))
    else:
        main()
