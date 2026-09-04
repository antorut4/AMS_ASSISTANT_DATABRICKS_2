"""Verifica del sistema di configurazione, eseguibile ovunque.

    python -m amsconfig.selftest

Non serve né un workspace Databricks né Gradio: il database è sostituito da
MemoryStorage e l'interfaccia da un finto `gr` che registra i collegamenti. Il
controllo più importante è proprio quello sull'interfaccia: in Gradio un handler
che restituisce un numero di valori diverso da quello degli output dichiarati
fallisce SOLO al click, quindi qui li chiamiamo tutti e li contiamo.
"""

from __future__ import annotations

import json
import sys

from .catalog import BY_KEY, CATEGORIES, SETTINGS
from .models import ValidationError
from .service import build_service
from .storage import MemoryStorage, SqlStorage

_ok, _ko = 0, 0


def check(nome: str, cond: bool, extra: str = "") -> None:
    global _ok, _ko
    if cond:
        _ok += 1
    else:
        _ko += 1
        print(f"  FALLITO: {nome}" + (f" — {extra}" if extra else ""))


def svc(env=None, storage=None, defaults=None):
    return build_service(storage=storage or MemoryStorage(), defaults=defaults,
                         environ=env if env is not None else {}, cache_ttl_s=0)


# --- 1. catalogo -------------------------------------------------------------
def test_catalogo():
    chiavi = [s.key for s in SETTINGS]
    check("chiavi uniche", len(chiavi) == len(set(chiavi)))
    cat_note = {c.key for c in CATEGORIES}
    orfane = [s.key for s in SETTINGS if s.category not in cat_note]
    check("nessuna categoria orfana", not orfane, str(orfane))
    vuote = [c.key for c in CATEGORIES
             if not any(s.category == c.key for s in SETTINGS)]
    check("nessuna categoria vuota", not vuote, str(vuote))
    senza = [s.key for s in SETTINGS if not s.description]
    check("ogni impostazione ha una descrizione", not senza, str(senza))
    for s in SETTINGS:
        if s.data_type == "enum":
            check(f"enum con valori ({s.key})", bool(s.allowed_values))


# --- 1b. semi ----------------------------------------------------------------
def test_semi():
    """I semi devono restare applicabili: sono l'unico modo di riportare un
    ambiente nuovo a una configurazione nota senza ricompilare il pannello."""
    from . import seed
    from .catalog import BY_KEY
    nomi = seed.available()
    check("semi trovati", bool(nomi), str(nomi))
    for n in nomi:
        with open(seed.path(n), encoding="utf-8") as f:
            testo = f.read()
        try:
            doc = json.loads(testo)
        except Exception as e:
            check(f"seme {n}: JSON valido", False, str(e)[:80])
            continue
        valori = doc.get("values", doc)
        check(f"seme {n}: ha valori", isinstance(valori, dict) and bool(valori))
        ignote = [k for k in valori if k not in BY_KEY]
        check(f"seme {n}: chiavi tutte nel catalogo", not ignote, str(ignote))
        s = svc()
        rep = s.import_config(testo, "test")
        check(f"seme {n}: si applica senza errori", not rep["errors"],
              str(rep["errors"]))
        check(f"seme {n}: applicato per intero",
              len(rep["saved"]) + len(rep["unchanged"]) == len(valori))
    check("seme inesistente -> percorso vuoto", seed.path("boh_non_esiste") == "")


# --- 1c. manifest ------------------------------------------------------------
def test_manifest():
    """Il blocco env: dell'app.yaml deve valere anche fuori dall'App (notebook,
    job), senza mai sovrascrivere ciò che l'ambiente ha già."""
    from .manifest import apply_env, env_from_text
    testo = (
        '# commento iniziale\n'
        'command: ["python", "app.py"]\n'
        '\n'
        '# DOVE SI TROVA QUESTA INSTALLAZIONE\n'
        'env:\n'
        '  - name: APP__STORAGE__SCHEMA\n'
        '    value: "mio_schema"      # commento in coda\n'
        '  - name: APP__COMPUTE__WAREHOUSE_ID\n'
        '    value: abc123\n'
        '  - name: APP__BRANDING__ACCENT_COLOR\n'
        '    value: "#EC008C"\n'
        '  - name: SEGRETO\n'
        '    valueFrom: qualche-secret\n'
        'altro_blocco:\n'
        '  - name: NON_MI_RIGUARDA\n'
        '    value: "x"\n')
    v = env_from_text(testo)
    check("manifest: legge le variabili",
          v.get("APP__STORAGE__SCHEMA") == "mio_schema", str(v))
    check("manifest: valore senza virgolette",
          v.get("APP__COMPUTE__WAREHOUSE_ID") == "abc123", str(v))
    check("manifest: '#' dentro le virgolette non è un commento",
          v.get("APP__BRANDING__ACCENT_COLOR") == "#EC008C", str(v))
    check("manifest: ignora i riferimenti a secret", "SEGRETO" not in v, str(v))
    check("manifest: ignora gli altri blocchi", "NON_MI_RIGUARDA" not in v, str(v))

    finto = {"APP__STORAGE__SCHEMA": "gia_presente"}
    import os as _os
    import tempfile
    d = tempfile.mkdtemp()
    p = _os.path.join(d, "app.yaml")
    with open(p, "w", encoding="utf-8") as f:
        f.write(testo)
    aggiunti = apply_env(p, environ=finto)
    check("manifest: non sovrascrive l'ambiente",
          finto["APP__STORAGE__SCHEMA"] == "gia_presente")
    check("manifest: riempie solo i buchi",
          "APP__COMPUTE__WAREHOUSE_ID" in aggiunti
          and "APP__STORAGE__SCHEMA" not in aggiunti, str(aggiunti))
    check("manifest: file inesistente non solleva",
          apply_env(_os.path.join(d, "non-esiste.yaml"), environ={}) == [])


# --- 2. priorità dei livelli -------------------------------------------------
def test_priorita():
    s = svc()
    check("default applicativo", s.get_config_value("rag.top_k") == 3)
    # Senza ambiente lo schema non ha un valore: è la condizione che fa fermare
    # l'App all'avvio invece di costruire query su ".tickets".
    check("schema vuoto se non lo dà l'ambiente",
          s.get_config_value("storage.schema") == "")
    s_env = svc(env={"APP__STORAGE__SCHEMA": "mio_schema"})
    check("schema dall'ambiente",
          s_env.get_config_value("storage.schema") == "mio_schema")

    s.update_config("rag.top_k", "7", "tester")
    check("il database vince sul default", s.get_config_value("rag.top_k") == 7)
    check("provenienza database", s.item("rag.top_k").source == "database")

    s2 = svc(env={"APP__RAG__TOP_K": "9"}, storage=MemoryStorage({"rag.top_k": 7}))
    check("l'ambiente vince sul database", s2.get_config_value("rag.top_k") == 9)
    check("provenienza env", s2.item("rag.top_k").source == "env")

    # chiave fuori dal catalogo: deve comportarsi come la vecchia cfg
    check("chiave ignota -> default del chiamante",
          s.get_config_value("pippo.pluto", 42) == 42)
    s3 = svc(env={"APP__PIPPO__PLUTO": "true"})
    check("chiave ignota da env", s3.get_config_value("pippo.pluto", 42) is True)

    # sottoalbero, come faceva il dizionario CONFIG
    sub = s.get_config_value("workflow")
    check("lettura di un ramo", isinstance(sub, dict)
          and sub.get("max_tool_iterations") == 8, str(sub)[:80])


def test_bootstrap_non_dal_db():
    """Le chiavi di bootstrap non devono MAI arrivare dal database: servono per
    raggiungerlo. È anche la garanzia contro la ricorsione table()->cfg()."""
    st = MemoryStorage({"storage.schema": "schema_finto"})
    s = svc(storage=st, env={"APP__STORAGE__SCHEMA": "schema_vero"})
    check("schema ignora il database",
          s.get_config_value("storage.schema") == "schema_vero")
    try:
        s.update_config("storage.schema", "altro", "tester")
        check("schema non modificabile", False, "nessun errore sollevato")
    except ValidationError:
        check("schema non modificabile", True)

    # bootstrap_value non deve MAI leggere il database: è ciò che permette di
    # importare app.py in un notebook, dove il connettore SQL non è disponibile.
    letture = {"n": 0}

    class _StorageSpia(MemoryStorage):
        def load(self):
            letture["n"] += 1
            return {"branding.accent_color": "#000000"}

    s2 = svc(storage=_StorageSpia())
    check("bootstrap_value non tocca il database",
          s2.bootstrap_value("branding.accent_color") == "#EC008C"
          and letture["n"] == 0, f"letture={letture['n']}")
    check("cfg normale invece lo legge",
          s2.get_config_value("branding.accent_color") == "#000000")
    s3 = svc(env={"APP__BRANDING__ACCENT_COLOR": "#123456"}, storage=_StorageSpia())
    check("bootstrap_value rispetta l'ambiente",
          s3.bootstrap_value("branding.accent_color") == "#123456")


# --- 3. validazione ----------------------------------------------------------
def test_validazione():
    s = svc()
    casi_ko = [
        ("rag.top_k", "99", "fuori range"),
        ("rag.top_k", "abc", "non è un numero"),
        ("rag.min_score", "2.5", "fuori range"),
        ("servicenow.base_url", "kiko.service-now.com", "manca lo schema http"),
        ("branding.accent_color", "magenta", "non esadecimale"),
        ("models.prices", "{non json}", "JSON rotto"),
        ("prompts.system_role", "corto", "sotto la lunghezza minima"),
        ("servicenow.password", "segreto", "non modificabile"),
    ]
    for key, raw, perche in casi_ko:
        try:
            s.validate_config(key, raw)
            check(f"rifiuta {key} ({perche})", False, "accettato")
        except ValidationError:
            check(f"rifiuta {key} ({perche})", True)

    casi_ok = [
        ("rag.top_k", "5", 5),
        ("rag.min_score", "0,45", 0.45),                  # virgola decimale
        ("workflow.self_critique", False, False),
        ("team.members", "Anna Rossi\nLuca Bianchi", ["Anna Rossi", "Luca Bianchi"]),
        ("servicenow.base_url", " https://x.service-now.com/api/now/table ",
         "https://x.service-now.com/api/now/table"),
        ("branding.accent_color", "#ec008c", "#EC008C"),
        ("models.default_price", "[2.5, 12]", [2.5, 12]),
        ("excel_column_map", '{"number": ["A"]}', {"number": ["A"]}),
    ]
    for key, raw, atteso in casi_ok:
        try:
            got = s.validate_config(key, raw)
            check(f"accetta {key}", got == atteso, f"ottenuto {got!r}")
        except ValidationError as e:
            check(f"accetta {key}", False, e.message)


# --- 4. salvataggio, ripristino, export/import -------------------------------
def test_persistenza():
    st = MemoryStorage()
    s = svc(storage=st)
    rep = s.save_config({"rag.top_k": "5", "sal.max_rows": "700",
                         "rag.min_score": "abc"}, "tester")
    check("due salvate", sorted(rep["saved"]) == ["rag.top_k", "sal.max_rows"], str(rep))
    check("una in errore", list(rep["errors"]) == ["rag.min_score"], str(rep["errors"]))
    check("valore persistito", s.get_config_value("sal.max_rows") == 700)

    # un valore uguale al default cancella l'override invece di scriverlo
    s.update_config("rag.top_k", "3", "tester")
    check("valore = default -> nessun override", "rag.top_k" not in st.load())
    check("torna al default", s.item("rag.top_k").source == "default")

    s.update_config("rag.top_k", "6", "tester")
    s.reset_config("rag.top_k")
    check("ripristino singolo", s.get_config_value("rag.top_k") == 3)

    s.save_config({"sal.max_rows": "800", "sal.aging_alert_days": "10"}, "tester")
    n = s.reset_category("sal")
    check("ripristino categoria", s.get_config_value("sal.max_rows") == 500 and n > 0)

    s.update_config("rag.top_k", "6", "tester")
    s.reset_all()
    check("ripristino totale", st.load() == {})

    # env: salvare è inutile, e va detto invece che fingere
    s4 = svc(env={"APP__RAG__TOP_K": "9"})
    rep = s4.save_config({"rag.top_k": "4"}, "tester")
    check("salvataggio ignorato se forzato da env", rep["env"] == ["rag.top_k"], str(rep))


def test_export_import():
    s = svc()
    s.save_config({"rag.top_k": "5", "team.members": "Anna\nLuca"}, "tester")
    testo = s.export_config()
    doc = json.loads(testo)
    check("export contiene solo le modifiche",
          set(doc["values"]) == {"rag.top_k", "team.members"}, str(doc["values"]))
    check("export senza segreti",
          not any(k.startswith("servicenow.pass") for k in doc["values"]))

    s2 = svc()
    rep = s2.import_config(testo, "tester")
    check("import applica i valori", s2.get_config_value("rag.top_k") == 5)
    check("import: team", s2.get_config_value("team.members") == ["Anna", "Luca"])

    rep = s2.import_config('{"values": {"chiave.inesistente": 1, "rag.top_k": 4}}', "t")
    check("import ignora le chiavi sconosciute",
          rep["ignored"] == ["chiave.inesistente"] and rep["saved"] == ["rag.top_k"],
          str(rep))
    check("export completo più grande",
          len(json.loads(s2.export_config(include_defaults=True))["values"])
          > len(json.loads(s2.export_config())["values"]))


# --- 5. SQL generato ---------------------------------------------------------
def test_sql():
    eseguite = []

    def fake_exec(q):
        eseguite.append(q)

    def fake_run(q, max_rows=1000):
        eseguite.append(q)
        return None

    st = SqlStorage(run_sql=fake_run, exec_sql=fake_exec,
                    sql_str=lambda v: "'" + str(v).replace("'", "''") + "'",
                    schema_provider=lambda: "sch")
    s = build_service(storage=st, environ={}, cache_ttl_s=0)
    s.update_config("branding.header_title", "L'App di Anna", "tester")
    ins = [q for q in eseguite if q.startswith("INSERT")]
    check("una INSERT", len(ins) == 1, str(eseguite))
    check("apice raddoppiato", "L''App di Anna" in ins[0], ins[0][:200])
    check("tabella giusta", "sch.app_config" in ins[0])
    check("dodici colonne", ins[0].count(",") >= 11)
    st.delete(["a", "b"])
    check("DELETE per chiave",
          eseguite[-1] == "DELETE FROM sch.app_config WHERE config_key IN ('a', 'b')",
          eseguite[-1])


# --- 6. interfaccia: cablaggio degli handler ---------------------------------
class _FakeComp:
    def __init__(self, **kw):
        self.__dict__.update(kw)
        self.value = kw.get("value")
        self.binds = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def click(self, fn, inputs=None, outputs=None):
        self.binds.append(("click", fn, inputs, outputs))
        _FakeGr.BINDS.append((fn, inputs, outputs))

    def change(self, fn, inputs=None, outputs=None):
        self.binds.append(("change", fn, inputs, outputs))
        _FakeGr.BINDS.append((fn, inputs, outputs))


class _FakeGr:
    BINDS: list = []

    def __getattr__(self, nome):
        if nome == "update":
            return lambda **kw: dict(kw)
        return lambda *a, **kw: _FakeComp(**kw)


def test_ui():
    from . import ui as uimod
    _FakeGr.BINDS = []
    gr = _FakeGr()
    s = svc()
    drawer, apri = uimod.mount(gr, s, lambda m: f"OK:{m}", lambda m: f"ERR:{m}",
                               lambda: "tester")
    fn_apri, _in, out_apri = apri
    check("l'apertura restituisce quanti output dichiara",
          len(fn_apri()) == len(out_apri), f"{len(fn_apri())} vs {len(out_apri)}")

    controlli = [b for b in _FakeGr.BINDS]
    check("collegamenti presenti", len(controlli) > len(SETTINGS), str(len(controlli)))

    valori = [c.value for c in out_apri[2:2 + len(uimod.KEYS)]]
    for fn, inp, out in controlli:
        n_in = 0 if inp is None else (len(inp) if isinstance(inp, list) else 1)
        try:
            if n_in == 0:
                res = fn()
            elif isinstance(inp, list) and len(inp) == len(uimod.KEYS):
                res = fn(*valori)
            elif n_in == 1:
                res = fn(None)
            else:
                res = fn(*([""] * n_in))
        except Exception as e:
            check(f"handler {getattr(fn, '__name__', '?')} eseguibile", False,
                  f"{type(e).__name__}: {e}")
            continue
        n_out = len(out) if isinstance(out, list) else 1
        n_res = len(res) if isinstance(res, (list, tuple)) else 1
        check(f"handler {getattr(fn, '__name__', '?')}: output coerenti",
              n_res == n_out, f"restituiti {n_res}, dichiarati {n_out}")


def main() -> int:
    for f in (test_catalogo, test_semi, test_manifest, test_priorita,
              test_bootstrap_non_dal_db,
              test_validazione, test_persistenza, test_export_import, test_sql,
              test_ui):
        print(f"• {f.__name__}")
        f()
    print(f"\n{_ok} controlli superati, {_ko} falliti "
          f"({len(SETTINGS)} impostazioni, {len(CATEGORIES)} categorie)")
    return 1 if _ko else 0


if __name__ == "__main__":
    sys.exit(main())
