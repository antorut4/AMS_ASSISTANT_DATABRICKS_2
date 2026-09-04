"""Persistenza delle impostazioni: la tabella `app_config`.

Il modulo NON importa app.py: riceve dall'esterno le funzioni per parlare col
warehouse (`run_sql`, `exec_sql`, `sql_str`) e il nome dello schema. Serve a
tenere il sistema di configurazione provabile senza un workspace Databricks —
i test usano `MemoryStorage` — e a evitare l'import circolare, visto che app.py
chiama il ConfigService fin dalla prima riga utile.

Cosa c'è in tabella. `config_value` è l'unico campo autorevole: è il valore
scelto dall'operatore, serializzato in JSON. Le altre colonne (display_name,
description, help_text, default_value, validation_rules, allowed_values) sono
una FOTOGRAFIA dei metadati del catalogo al momento del salvataggio: servono a
leggere la tabella in SQL e capire cosa si sta guardando senza aprire il codice.
La fonte di verità dei metadati resta il catalogo: se un domani cambia una
descrizione, la riga in tabella la aggiorna al salvataggio successivo, e nel
frattempo nessuno la usa per decidere qualcosa.
"""

from __future__ import annotations

from .validation import dumps, loads

TABLE = "app_config"

DDL = """CREATE TABLE IF NOT EXISTS {schema}.app_config (
    category STRING, config_key STRING, config_value STRING, config_type STRING,
    display_name STRING, description STRING, help_text STRING,
    default_value STRING, validation_rules STRING, allowed_values STRING,
    updated_at TIMESTAMP, updated_by STRING) USING DELTA"""


class ConfigStorage:
    """Interfaccia. `load` ritorna {chiave: valore}, già deserializzati."""

    def load(self) -> dict:
        return {}

    def upsert(self, entries: list, actor: str) -> None:
        raise NotImplementedError

    def delete(self, keys: list) -> None:
        raise NotImplementedError

    def delete_all(self) -> None:
        raise NotImplementedError


class MemoryStorage(ConfigStorage):
    """Storage di servizio: usato dai test e quando il database non è ancora
    collegato, così l'App parte comunque sui default invece di non partire."""

    def __init__(self, initial: dict | None = None):
        self._d = dict(initial or {})

    def load(self) -> dict:
        return dict(self._d)

    def upsert(self, entries: list, actor: str) -> None:
        for setting, value, _default in entries:
            self._d[setting.key] = value

    def delete(self, keys: list) -> None:
        for k in keys:
            self._d.pop(k, None)

    def delete_all(self) -> None:
        self._d.clear()


class SqlStorage(ConfigStorage):
    """Storage su Delta, via SQL Warehouse.

    `schema_provider` è una funzione e non una stringa apposta: lo schema si
    risolve da default ed env (mai dal database, che è quello che stiamo per
    raggiungere), e va letto al momento dell'uso, non al momento del cablaggio.
    """

    def __init__(self, run_sql, exec_sql, sql_str, schema_provider, logger=None,
                 batch: int = 40):
        self._run, self._exec, self._q = run_sql, exec_sql, sql_str
        self._schema, self._log, self._batch = schema_provider, logger, batch

    @property
    def fqn(self) -> str:
        return f"{self._schema()}.{TABLE}"

    def load(self) -> dict:
        df = self._run(f"SELECT config_key, config_value FROM {self.fqn}",
                       max_rows=5000)
        out = {}
        if df is None or df.empty:
            return out
        for _, r in df.iterrows():
            key = r["config_key"]
            if key is None:
                continue
            # Una riga malformata (JSON scritto a mano male) non deve far
            # perdere TUTTE le impostazioni salvate: si salta quella.
            try:
                out[str(key)] = loads(r["config_value"])
            except Exception as e:
                if self._log:
                    self._log.warning(f"config '{key}' illeggibile: {str(e)[:100]}")
        return out

    def upsert(self, entries: list, actor: str) -> None:
        if not entries:
            return
        self.delete([s.key for s, _v, _d in entries])
        cols = ("category, config_key, config_value, config_type, display_name, "
                "description, help_text, default_value, validation_rules, "
                "allowed_values, updated_at, updated_by")
        rows = [self._row(s, v, d, actor) for s, v, d in entries]
        for i in range(0, len(rows), self._batch):
            chunk = ", ".join(rows[i:i + self._batch])
            self._exec(f"INSERT INTO {self.fqn} ({cols}) VALUES {chunk}")

    def delete(self, keys: list) -> None:
        if not keys:
            return
        lst = ", ".join(self._q(k) for k in keys)
        self._exec(f"DELETE FROM {self.fqn} WHERE config_key IN ({lst})")

    def delete_all(self) -> None:
        self._exec(f"DELETE FROM {self.fqn}")

    def _row(self, setting, value, default_value, actor: str) -> str:
        vals = [
            self._q(setting.category), self._q(setting.key),
            self._q(dumps(value)), self._q(setting.data_type),
            self._q(setting.display_name), self._q(setting.description),
            self._q(setting.help_text), self._q(dumps(default_value)),
            self._q(dumps(setting.validation_rules())),
            self._q(dumps(list(setting.allowed_values))),
            "current_timestamp()", self._q(actor),
        ]
        return "(" + ", ".join(vals) + ")"
