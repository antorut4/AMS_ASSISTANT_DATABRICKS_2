"""Modelli del sistema di configurazione.

PERCHÉ DATACLASS E NON PYDANTIC. L'App gira su Databricks con requirements
pinnati (gradio 4.44.1 / gradio_client 1.3.0); pydantic entra solo come
dipendenza transitiva di gradio, quindi pinnarlo a parte significa esporsi a un
conflitto di versioni a ogni upgrade — in cambio di una validazione che qui è
tutta di dominio (range, URL, enumerazioni, JSON, liste) e non di forma. Quella
validazione vive in validation.py: se un domani servisse davvero Pydantic, è
l'unico modulo da riscrivere, perché il resto del sistema parla solo di Setting
e ConfigItem.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# --- tipi di dato ------------------------------------------------------------
# Determinano sia la validazione (validation.py) sia il controllo mostrato nella
# UI (ui.py): un solo posto decide "che cos'è" un'impostazione.
STRING = "string"      # riga singola
TEXT = "text"          # testo lungo -> textarea
INT = "int"
FLOAT = "float"
BOOL = "bool"          # -> checkbox
ENUM = "enum"          # -> dropdown, valori da allowed_values
LIST = "list"          # lista di stringhe -> editor lista (una per riga)
DICT = "dict"          # oggetto complesso -> editor JSON
JSON = "json"          # QUALSIASI valore JSON (es. [3.0, 15.0]) -> editor JSON
URL = "url"
COLOR = "color"

ALL_TYPES = (STRING, TEXT, INT, FLOAT, BOOL, ENUM, LIST, DICT, JSON, URL, COLOR)

# --- quando ha effetto una modifica ------------------------------------------
# Non è un dettaglio estetico: quasi tutte le impostazioni sono lette a ogni uso
# e cambiano comportamento subito, ma alcune vengono lette una volta sola (i
# colori del tema finiscono nel CSS generato all'avvio della pagina). Dirlo
# nella UI evita il classico "ho salvato e non è cambiato niente".
NOW = "subito"
RELOAD = "al ricaricamento della pagina"
RESTART = "al riavvio dell'App"


@dataclass(frozen=True)
class Category:
    """Sezione delle Impostazioni. L'ordine è quello di visualizzazione."""
    key: str
    display_name: str
    description: str


@dataclass(frozen=True)
class Setting:
    """Metadati completi di una singola impostazione.

    `default` è il default APPLICATIVO: il valore che vale se nessuno lo tocca.
    Il default effettivo di un tenant può sovrascriverlo (vedi tenants.py), ed è
    quello che la UI mostra come "valore di default", perché è quello a cui
    riporta il pulsante di ripristino.
    """
    key: str                       # path puntato, es. "models.chat_primary"
    category: str                  # chiave di Category
    display_name: str
    description: str               # una riga: cosa fa
    help_text: str = ""            # il dettaglio: quando toccarlo, cosa rischi
    data_type: str = STRING
    default: Any = None
    allowed_values: tuple = ()
    min_value: float | None = None
    max_value: float | None = None
    unit: str = ""                 # "secondi", "caratteri", ... mostrato in UI
    applies: str = NOW
    is_editable: bool = True
    is_secret: bool = False
    editable_note: str = ""        # perché NON è modificabile, se non lo è

    @property
    def section(self) -> str:
        return self.key.split(".")[0]

    def validation_rules(self) -> dict:
        """Regole in forma serializzabile: finiscono nella colonna omonima di
        app_config, così le regole sono ispezionabili anche in SQL."""
        r: dict = {"type": self.data_type}
        if self.min_value is not None:
            r["min"] = self.min_value
        if self.max_value is not None:
            r["max"] = self.max_value
        if self.allowed_values:
            r["allowed"] = list(self.allowed_values)
        if not self.is_editable:
            r["editable"] = False
        return r


@dataclass
class ConfigItem:
    """Vista runtime di un'impostazione: metadati + valore risolto + provenienza.
    È quello che la UI disegna e che export_config() serializza."""
    setting: Setting
    current_value: Any
    default_value: Any
    source: str                    # "default" | "database" | "env"

    @property
    def key(self) -> str:
        return self.setting.key

    @property
    def is_overridden(self) -> bool:
        return self.source != "default"

    def to_dict(self, include_secret: bool = False) -> dict:
        s = self.setting
        val = None if (s.is_secret and not include_secret) else self.current_value
        return {
            "category": s.category,
            "key": s.key,
            "display_name": s.display_name,
            "description": s.description,
            "help_text": s.help_text,
            "data_type": s.data_type,
            "default_value": self.default_value,
            "current_value": val,
            "allowed_values": list(s.allowed_values),
            "validation_rules": s.validation_rules(),
            "is_editable": s.is_editable,
            "source": self.source,
        }


class ConfigError(Exception):
    """Errore d'uso del sistema di configurazione (chiave inesistente, ecc.)."""


class ValidationError(ConfigError):
    """Il valore proposto non è ammissibile. Il messaggio è pensato per essere
    mostrato TALE E QUALE all'operatore: niente traceback, niente gergo."""

    def __init__(self, key: str, message: str):
        self.key, self.message = key, message
        super().__init__(f"{key}: {message}")
