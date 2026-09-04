"""Sistema di configurazione dell'AMS Ticket Assistant.

    catalog.py     COSA esiste: chiavi, tipi, testi, default applicativi
    models.py      Setting / ConfigItem / errori
    validation.py  testo -> valore, e se quel valore è ammissibile
    storage.py     la tabella app_config (e uno storage in memoria per i test)
    repository.py  la risoluzione: default -> database -> variabili d'ambiente
    service.py     l'API usata dall'applicazione
    seed.py        configurazioni iniziali pronte da applicare (seeds/*.json)
    manifest.py    legge il blocco env: dell'app.yaml (serve fuori dall'App)
    ui.py          il pannello Impostazioni (importa Gradio: si importa a parte)

Non esistono file di configurazione per progetto: le tre chiavi che servono per
raggiungere il database (schema, warehouse, host) arrivano dalle variabili
d'ambiente, tutto il resto si imposta dal pannello e vive nella tabella.

`ui` NON è importato qui di proposito: il resto del sistema deve poter girare —
e essere provato — senza Gradio installato.
"""

from .manifest import apply_env as apply_manifest_env
from .models import (ConfigError, ConfigItem, Setting, ValidationError)
from .seed import available as available_seeds
from .seed import path as seed_path
from .service import ConfigService, build_service
from .storage import DDL as APP_CONFIG_DDL
from .storage import MemoryStorage, SqlStorage

__all__ = [
    "ConfigService", "build_service", "ConfigItem", "Setting",
    "ConfigError", "ValidationError",
    "SqlStorage", "MemoryStorage", "APP_CONFIG_DDL",
    "seed_path", "available_seeds", "apply_manifest_env",
]
