"""Risoluzione dei valori: default applicativi -> database -> variabili d'ambiente.

L'ordine è quello, e l'ambiente vince su tutto di proposito: è la via di fuga
quando l'App non parte. Se un valore salvato nel database rende l'applicazione
inutilizzabile, si imposta APP__SEZIONE__CHIAVE nelle App Settings e si riparte,
senza dover aprire una console SQL.

Il livello database è in cache di processo: senza, ogni `cfg(...)` — e ce ne
sono decine per ogni analisi — sarebbe una query al warehouse.
"""

from __future__ import annotations

import os
import time

from .catalog import BY_KEY

SOURCE_DEFAULT = "default"
SOURCE_DB = "database"
SOURCE_ENV = "env"


def env_key_for(key: str) -> str:
    """Nome della variabile d'ambiente per una chiave: models.chat_primary ->
    APP__MODELS__CHAT_PRIMARY. Identico alla convenzione della vecchia `cfg`,
    che resta valida per chi la usa già nelle App Settings."""
    return "APP__" + key.upper().replace(".", "__")


class ConfigRepository:

    def __init__(self, defaults: dict, storage=None, cache_ttl_s: int = 30,
                 environ=None, logger=None):
        self._defaults = dict(defaults)
        self._storage = storage
        self._ttl = cache_ttl_s
        self._env = environ if environ is not None else os.environ
        self._log = logger
        self._cache: dict | None = None
        self._cache_at = 0.0
        self._warned = False

    # --- cablaggio ----------------------------------------------------------
    def attach_storage(self, storage) -> None:
        self._storage, self._cache, self._cache_at = storage, None, 0.0

    def invalidate(self) -> None:
        self._cache, self._cache_at = None, 0.0

    # --- livelli ------------------------------------------------------------
    def defaults(self) -> dict:
        return dict(self._defaults)

    def default_of(self, key: str):
        return self._defaults.get(key)

    def stored(self) -> dict:
        """Override salvati nel database, in cache. Se il database non risponde
        si continua sui default: un warehouse fermo non deve impedire all'App di
        partire, e nemmeno di mostrare il pannello delle impostazioni."""
        if self._cache is not None and (time.time() - self._cache_at) < self._ttl:
            return self._cache
        if self._storage is None:
            return {}
        try:
            self._cache = self._storage.load()
            self._warned = False
        except Exception as e:
            self._cache = {}
            if self._log and not self._warned:
                self._warned = True     # una riga di log, non una a ogni cfg()
                self._log.warning(f"impostazioni non leggibili dal database "
                                  f"({str(e)[:120]}); uso i default")
        self._cache_at = time.time()
        return self._cache

    def env_override(self, key: str):
        """(trovato, valore) dall'ambiente. La tipizzazione è quella storica."""
        from .validation import coerce_env
        name = env_key_for(key)
        if name in self._env:
            return True, coerce_env(self._env[name])
        return False, None

    # --- risoluzione --------------------------------------------------------
    def resolve_without_db(self, key: str, fallback=None):
        """Come `resolve`, ma senza toccare il database. Serve a chi viene
        eseguito PRIMA che il warehouse sia raggiungibile, o a chi non deve
        dipenderne: importare il modulo non deve costare una query."""
        found, value = self.env_override(key)
        if found:
            return value, SOURCE_ENV
        if key in self._defaults:
            return self._defaults[key], SOURCE_DEFAULT
        return fallback, SOURCE_DEFAULT

    def resolve(self, key: str, fallback=None):
        """(valore, provenienza) per una chiave del catalogo o anche fuori da
        esso: una chiave sconosciuta resta servibile da env o dal fallback del
        chiamante, esattamente come faceva la vecchia `cfg`."""
        found, value = self.env_override(key)
        if found:
            return value, SOURCE_ENV

        setting = BY_KEY.get(key)
        # Le impostazioni di bootstrap non passano MAI dal database: servono per
        # raggiungerlo, e leggerle da lì sarebbe una dipendenza circolare.
        if setting is not None and setting.is_editable:
            stored = self.stored()
            if key in stored:
                return stored[key], SOURCE_DB

        if key in self._defaults:
            return self._defaults[key], SOURCE_DEFAULT
        return fallback, SOURCE_DEFAULT
