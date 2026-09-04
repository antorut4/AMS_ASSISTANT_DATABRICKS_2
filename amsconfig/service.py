"""ConfigService: l'unico punto da cui il resto dell'applicazione parla con la
configurazione. `cfg(...)` in app.py è un guscio sottile su `get_config_value`.

Una scelta che vale la pena dichiarare: salvare un valore uguale al default
CANCELLA l'override invece di scriverlo. Così la tabella contiene solo ciò che
qualcuno ha davvero voluto cambiare, «ripristina» e «salva il valore di default»
finiscono nello stesso stato, e un domani, se il default applicativo cambia,
l'installazione lo eredita invece di restare inchiodata al valore vecchio.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .catalog import BY_KEY, CATEGORIES, CATEGORY_BY_KEY, SETTINGS
from .models import ConfigError, ConfigItem, ValidationError
from .repository import SOURCE_DB, SOURCE_ENV, ConfigRepository, env_key_for
from .storage import MemoryStorage
from .validation import parse_and_validate


class ConfigService:

    def __init__(self, defaults: dict, storage=None,
                 cache_ttl_s: int = 30, environ=None, logger=None,
                 actor: str = "operator"):
        self.actor = actor
        self._log = logger
        self._repo = ConfigRepository(defaults, storage=storage,
                                      cache_ttl_s=cache_ttl_s,
                                      environ=environ, logger=logger)

    @property
    def label(self) -> str:
        """Come si chiama QUESTA installazione nei messaggi e nei nomi di file.
        È lo schema del database: l'unica cosa che distingue davvero un ambiente
        dall'altro, e che si legge senza database."""
        return str(self._repo.resolve_without_db("storage.schema", "")[0] or
                   "impostazioni")

    # --- cablaggio ----------------------------------------------------------
    def attach_storage(self, storage) -> None:
        """Il database si collega dopo la creazione del servizio: all'avvio
        `cfg()` viene già chiamata (per esempio per sapere QUALE schema
        interrogare), e a quel punto valgono default ed ambiente."""
        self._repo.attach_storage(storage)

    def warm(self) -> bool:
        """Carica subito gli override, prima di costruire l'interfaccia: i colori
        del tema finiscono nel CSS generato una volta sola. Ritorna False se il
        database non risponde — l'App parte comunque sui default."""
        self._repo.invalidate()
        try:
            self._repo.stored()
            return True
        except Exception:
            return False

    # --- lettura ------------------------------------------------------------
    def get_config_value(self, path: str, default=None):
        """Valore risolto. Accetta sia una chiave esatta sia un ramo:
        `cfg("models.prices")` e `cfg("excel_column_map")` sono chiavi vere,
        `cfg("models")` ricompone il sottoalbero come faceva il vecchio
        dizionario CONFIG."""
        if path in BY_KEY or self._repo.default_of(path) is not None:
            value, _src = self._repo.resolve(path, default)
            return value
        found, value = self._repo.env_override(path)
        if found:
            return value
        sub = self._subtree(path)
        if sub:
            return sub
        return default

    def bootstrap_value(self, path: str, default=None):
        """Valore risolto SENZA leggere il database: default applicativo, default
        del progetto, variabile d'ambiente.

        Da usare a livello di modulo, dove una query sarebbe fuori posto: `import
        app` non deve dipendere dal warehouse — in un notebook Databricks il
        connettore SQL è oscurato dal runtime e quella query fallirebbe sempre.
        Il valore aggiornato lo rilegge chi costruisce la pagina, con `cfg`.
        """
        value, _src = self._repo.resolve_without_db(path, default)
        return value

    def get_config(self, category: str | None = None, query: str = "") -> list:
        """Le impostazioni come le vede la UI: metadati + valore + provenienza."""
        q = (query or "").strip().lower()
        out = []
        for s in SETTINGS:
            if category and s.category != category:
                continue
            if q and not self._matches(s, q):
                continue
            out.append(self.item(s.key))
        return out

    def item(self, key: str) -> ConfigItem:
        s = BY_KEY.get(key)
        if s is None:
            raise ConfigError(f"Impostazione sconosciuta: '{key}'.")
        value, source = self._repo.resolve(key)
        return ConfigItem(setting=s, current_value=value,
                          default_value=self._repo.default_of(key), source=source)

    def categories(self) -> list:
        return list(CATEGORIES)

    def env_name(self, key: str) -> str:
        return env_key_for(key)

    # --- scrittura ----------------------------------------------------------
    def validate_config(self, key: str, raw):
        """Valore proposto -> valore tipizzato e valido, o ValidationError."""
        s = BY_KEY.get(key)
        if s is None:
            raise ConfigError(f"Impostazione sconosciuta: '{key}'.")
        return parse_and_validate(s, raw)

    def update_config(self, key: str, raw, actor: str = "") -> ConfigItem:
        """Salva UNA impostazione. Solleva ValidationError se il valore non va."""
        value = self.validate_config(key, raw)
        self._persist([(BY_KEY[key], value)], actor)
        return self.item(key)

    def save_config(self, values: dict, actor: str = "") -> dict:
        """Salva un blocco di impostazioni, tipicamente tutto il pannello.

        Ritorna un resoconto invece di sollevare: su una schermata con decine di
        campi, un errore in uno non deve buttare via gli altri.
        `{"saved": [...], "unchanged": [...], "errors": {k: msg}, "env": [...]}`
        """
        report = {"saved": [], "unchanged": [], "errors": {}, "env": []}
        da_scrivere = []
        for key, raw in (values or {}).items():
            s = BY_KEY.get(key)
            if s is None or not s.is_editable:
                continue
            try:
                value = self.validate_config(key, raw)
            except ValidationError as e:
                report["errors"][key] = e.message
                continue
            corrente, source = self._repo.resolve(key)
            if source == SOURCE_ENV:
                # Scriverlo sarebbe un salvataggio senza effetto visibile:
                # meglio dirlo che lasciar credere che sia stato applicato.
                report["env"].append(key)
                continue
            if value == corrente:
                report["unchanged"].append(key)
                continue
            da_scrivere.append((s, value))
            report["saved"].append(key)
        if da_scrivere:
            self._persist(da_scrivere, actor)
        return report

    def reset_config(self, key: str) -> ConfigItem:
        """Riporta una impostazione al default: cancella l'override."""
        if key not in BY_KEY:
            raise ConfigError(f"Impostazione sconosciuta: '{key}'.")
        self._storage_or_fail().delete([key])
        self._repo.invalidate()
        return self.item(key)

    def reset_category(self, category: str) -> int:
        if category not in CATEGORY_BY_KEY:
            raise ConfigError(f"Categoria sconosciuta: '{category}'.")
        keys = [s.key for s in SETTINGS if s.category == category]
        self._storage_or_fail().delete(keys)
        self._repo.invalidate()
        return len(keys)

    def reset_all(self) -> int:
        n = len(self._repo.stored())
        self._storage_or_fail().delete_all()
        self._repo.invalidate()
        return n

    # --- export / import ----------------------------------------------------
    def export_config(self, include_defaults: bool = False) -> str:
        """JSON delle impostazioni. Di default esporta SOLO gli scostamenti: è
        quello che serve per replicare una configurazione su un altro ambiente
        senza portarsi dietro ottanta valori identici al default.
        I segreti non vengono mai esportati."""
        valori = {}
        for s in SETTINGS:
            if s.is_secret:
                continue
            item = self.item(s.key)
            if include_defaults or item.source == SOURCE_DB:
                valori[s.key] = item.current_value
        doc = {
            "_meta": {
                "schema": self.label,
                "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "kind": "tutte le impostazioni" if include_defaults
                        else "solo le modifiche rispetto al default",
                "note": "I segreti non sono inclusi. Le impostazioni di bootstrap "
                        "(schema, warehouse, host) non sono importabili.",
            },
            "values": valori,
        }
        return json.dumps(doc, ensure_ascii=False, indent=2)

    def import_config(self, text: str, actor: str = "") -> dict:
        """Applica un JSON prodotto da `export_config` (o un semplice
        {chiave: valore}). Valida tutto prima di scrivere il singolo valore, e
        ignora in silenzio le chiavi che non esistono più, così un export di una
        versione precedente resta importabile."""
        try:
            doc = json.loads(text or "")
        except Exception as e:
            raise ConfigError(f"JSON non valido: {str(e)[:120]}")
        if isinstance(doc, dict) and isinstance(doc.get("values"), dict):
            doc = doc["values"]
        if not isinstance(doc, dict):
            raise ConfigError("Formato non riconosciuto: atteso un oggetto JSON.")
        noti = {k: v for k, v in doc.items() if k in BY_KEY}
        report = self.save_config(noti, actor)
        report["ignored"] = sorted(set(doc) - set(noti))
        return report

    # --- interni ------------------------------------------------------------
    def _persist(self, coppie: list, actor: str = "") -> None:
        st = self._storage_or_fail()
        da_scrivere, da_togliere = [], []
        for s, value in coppie:
            if value == self._repo.default_of(s.key):
                da_togliere.append(s.key)
            else:
                da_scrivere.append((s, value, self._repo.default_of(s.key)))
        if da_togliere:
            st.delete(da_togliere)
        if da_scrivere:
            st.upsert(da_scrivere, actor or self.actor)
        self._repo.invalidate()

    def _storage_or_fail(self):
        st = getattr(self._repo, "_storage", None)
        if st is None:
            raise ConfigError("Nessun archivio collegato: impossibile salvare.")
        return st

    def _subtree(self, prefix: str):
        out: dict = {}
        for s in SETTINGS:
            if not s.key.startswith(prefix + "."):
                continue
            resto = s.key[len(prefix) + 1:].split(".")
            nodo = out
            for p in resto[:-1]:
                nodo = nodo.setdefault(p, {})
            nodo[resto[-1]] = self.get_config_value(s.key)
        return out

    @staticmethod
    def _matches(s, q: str) -> bool:
        return any(q in (t or "").lower() for t in
                   (s.key, s.display_name, s.description, s.help_text))


def build_service(storage=None, logger=None, actor: str = "operator",
                  environ=None, cache_ttl_s: int = 30, defaults=None) -> ConfigService:
    """Costruisce il servizio sui default applicativi del catalogo.

    Le tre chiavi che servono per raggiungere il database (schema, warehouse,
    host) non hanno un default utile: arrivano dalle variabili d'ambiente
    APP__STORAGE__SCHEMA e compagne, impostate nell'app.yaml o nelle App
    Settings. `defaults` esiste per i test e per chi volesse precaricare valori
    diversi senza passare dall'ambiente.

    Senza storage l'App funziona lo stesso, in sola lettura sui default: è il
    modo in cui parte prima che il warehouse sia raggiungibile.
    """
    from .catalog import default_config
    base = default_config()
    if defaults:
        base.update(defaults)
    return ConfigService(defaults=base, storage=storage, logger=logger,
                         actor=actor, environ=environ, cache_ttl_s=cache_ttl_s)


__all__ = ["ConfigService", "build_service", "MemoryStorage", "ConfigError",
           "ValidationError"]
