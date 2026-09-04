"""Semi: configurazioni iniziali pronte da applicare.

Un seme NON è un default e non partecipa alla risoluzione dei valori: è un file
JSON che qualcuno applica una volta, deliberatamente, per portare un ambiente
nuovo alla configurazione di uno esistente invece di ricompilare a mano decine
di campi nel pannello. Applicato, i valori vivono nella tabella app_config e il
file non serve più.

I file stanno in amsconfig/seeds/ e hanno il formato prodotto da
`ConfigService.export_config()`: l'export di un ambiente funzionante è già un
seme valido per un altro. Il modulo si chiama `seed` al singolare proprio per
non collidere con quella cartella.
"""

from __future__ import annotations

import os

DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seeds")


def available() -> list:
    """Nomi dei semi disponibili (i file .json presenti nella cartella)."""
    if not os.path.isdir(DIR):
        return []
    return sorted(f[:-5] for f in os.listdir(DIR) if f.endswith(".json"))


def path(name: str) -> str:
    """Percorso del seme, o "" se non esiste."""
    p = os.path.join(DIR, f"{name}.json")
    return p if os.path.exists(p) else ""
