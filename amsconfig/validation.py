"""Livello di validazione: è l'unico punto che trasforma testo in valore e che
decide se un valore è accettabile.

Tre direzioni, tenute separate apposta:
  - env  -> valore   (`coerce_env`): stringhe di ambiente, tipizzate a naso come
    faceva la vecchia `_coerce`, perché il contratto delle env var non cambia.
  - testo UI -> valore (`parse_raw`): qui il tipo lo sappiamo dal catalogo, e
    l'errore deve essere leggibile dall'operatore.
  - valore -> testo per il DB (`dumps` / `loads`): JSON, così un intero resta un
    intero e una lista resta una lista anche dopo un giro in tabella.
"""

from __future__ import annotations

import json
import re

from .models import (BOOL, COLOR, DICT, ENUM, FLOAT, INT, JSON, LIST, TEXT,
                     URL, Setting, ValidationError)

_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


def coerce_env(v: str):
    """Tipizzazione delle env var, identica alla vecchia `_coerce`: la
    retrocompatibilità di APP__SEZIONE__CHIAVE passa da qui e non va cambiata.
    Le stringhe che sembrano JSON (liste/oggetti) vengono decodificate, così
    APP__TEAM__MEMBERS='["Anna","Luca"]' continua a dare una lista."""
    low = v.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    s = v.strip()
    if s[:1] in ("[", "{"):
        try:
            return json.loads(s)
        except Exception:
            return v
    return v


def dumps(value) -> str:
    """Serializzazione per la colonna config_value: sempre JSON, mai `str()`.
    Con `str()` una lista tornerebbe indietro come stringa "['a', 'b']"."""
    return json.dumps(value, ensure_ascii=False)


def loads(text: str):
    """Inverso di `dumps`, tollerante: se in tabella qualcuno ha scritto a mano
    un valore non-JSON lo trattiamo come stringa invece di far cadere tutto."""
    try:
        return json.loads(text)
    except Exception:
        return text


def parse_raw(setting: Setting, raw):
    """Da quello che arriva dalla UI al valore tipizzato del `setting`.
    Solleva ValidationError con un messaggio mostrabile all'operatore."""
    t = setting.data_type
    if t == BOOL:
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("true", "1", "sì", "si", "yes", "on")
    if t in (INT, FLOAT):
        if isinstance(raw, bool):
            raise ValidationError(setting.key, "Serve un numero.")
        txt = str(raw).strip().replace(",", ".")
        if txt == "":
            raise ValidationError(setting.key, "Serve un numero: il campo è vuoto.")
        try:
            return int(float(txt)) if t == INT else float(txt)
        except ValueError:
            atteso = "un numero intero" if t == INT else "un numero"
            raise ValidationError(setting.key, f"'{raw}' non è {atteso}.")
    if t == LIST:
        if isinstance(raw, (list, tuple)):
            return [str(x) for x in raw]
        txt = str(raw or "").strip()
        if not txt:
            return []
        if txt[:1] == "[":
            try:
                v = json.loads(txt)
            except Exception as e:
                raise ValidationError(setting.key, f"JSON non valido: {str(e)[:80]}")
            if not isinstance(v, list):
                raise ValidationError(setting.key, "Serve una lista.")
            return [str(x) for x in v]
        # una voce per riga: è il modo in cui l'editor lista presenta il valore
        return [r.strip() for r in txt.splitlines() if r.strip()]
    if t == DICT:
        if isinstance(raw, dict):
            return raw
        txt = str(raw or "").strip()
        if not txt:
            return {}
        try:
            v = json.loads(txt)
        except Exception as e:
            raise ValidationError(setting.key, f"JSON non valido: {str(e)[:80]}")
        if not isinstance(v, dict):
            raise ValidationError(setting.key, "Serve un oggetto JSON { ... }.")
        return v
    if t == JSON:
        # Qualunque valore JSON: liste di numeri, numeri, oggetti. Nessuna
        # conversione a stringa, altrimenti [3.0, 15.0] tornerebbe come testo e
        # il calcolo dei costi si troverebbe a moltiplicare stringhe.
        if not isinstance(raw, str):
            return raw
        txt = raw.strip()
        if not txt:
            raise ValidationError(setting.key, "Serve un valore JSON.")
        try:
            return json.loads(txt)
        except Exception as e:
            raise ValidationError(setting.key, f"JSON non valido: {str(e)[:80]}")
    return "" if raw is None else str(raw)


def validate(setting: Setting, value):
    """Controlli di dominio sul valore già tipizzato. Ritorna il valore
    normalizzato (es. URL senza spazi ai bordi) oppure solleva."""
    k, t = setting.key, setting.data_type

    if not setting.is_editable:
        raise ValidationError(k, setting.editable_note or
                              "Impostazione non modificabile dall'interfaccia.")

    if t in (INT, FLOAT):
        if setting.min_value is not None and value < setting.min_value:
            raise ValidationError(k, f"Il minimo ammesso è {_num(setting.min_value)}.")
        if setting.max_value is not None and value > setting.max_value:
            raise ValidationError(k, f"Il massimo ammesso è {_num(setting.max_value)}.")
        return value

    if t == ENUM:
        if setting.allowed_values and value not in setting.allowed_values:
            raise ValidationError(k, f"Valori ammessi: {', '.join(setting.allowed_values)}.")
        return value

    if t == URL:
        v = str(value).strip()
        # Vuoto è legittimo: è il modo di dire "integrazione non configurata"
        # (la versione ENI NDP parte esattamente così).
        if v and not re.match(r"^https?://", v):
            raise ValidationError(k, "L'indirizzo deve iniziare con http:// o https://.")
        return v

    if t == COLOR:
        v = str(value).strip()
        if not _COLOR_RE.match(v):
            raise ValidationError(k, "Serve un colore esadecimale, es. #EC008C.")
        return v.upper()

    if t == LIST:
        if setting.allowed_values:
            fuori = [x for x in value if x not in setting.allowed_values]
            if fuori:
                raise ValidationError(k, f"Valori non ammessi: {', '.join(fuori)}.")
        if setting.min_value is not None and len(value) < setting.min_value:
            raise ValidationError(k, f"Servono almeno {int(setting.min_value)} voci.")
        return value

    if t in (TEXT,):
        v = str(value)
        if setting.min_value is not None and len(v.strip()) < setting.min_value:
            raise ValidationError(k, f"Servono almeno {int(setting.min_value)} caratteri.")
        return v

    return value


def parse_and_validate(setting: Setting, raw):
    return validate(setting, parse_raw(setting, raw))


def to_editor_text(setting: Setting, value) -> str:
    """Valore -> testo da mostrare nel controllo della UI. Liste una per riga
    (si editano a occhio), oggetti in JSON indentato (si leggono)."""
    t = setting.data_type
    if t == LIST:
        return "\n".join(str(x) for x in (value or []))
    if t == DICT:
        return json.dumps(value or {}, ensure_ascii=False, indent=2)
    if t == JSON:
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return ""
    return str(value)


def _num(x) -> str:
    return str(int(x)) if float(x).is_integer() else str(x)
