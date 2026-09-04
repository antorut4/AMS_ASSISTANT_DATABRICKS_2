"""Lettura del blocco `env:` dell'app.yaml.

PERCHÉ. I tre valori che dicono dove si trova l'installazione (schema,
warehouse, host) sono dichiarati nell'app.yaml, ma quel file lo legge SOLO il
runtime delle Databricks Apps: in un notebook — dove si esegue il bootstrap —
nessuno lo guarda, e senza quei valori il modulo non saprebbe su quale schema
lavorare. Leggerlo qui rende l'app.yaml l'unica fonte di verità in tutti e due
i contesti, invece di costringere a ripetere le stesse righe a mano.

Le variabili GIÀ presenti nell'ambiente non vengono mai toccate: nell'App vince
quello che inietta la piattaforma (App Settings comprese), qui si riempiono solo
i buchi.

Il parsing è volutamente minimale e non usa PyYAML: serve un solo blocco, dalla
forma fissa, e non vale la pena aggiungere una dipendenza a un'immagine con i
requirements pinnati. Qualunque cosa non torni viene ignorata in silenzio — un
manifest che non si riesce a leggere non deve impedire l'avvio.
"""

from __future__ import annotations

import os


def _strip_comment(line: str) -> str:
    """Toglie il commento rispettando le virgolette: un valore può contenere '#'
    (per esempio un colore) e non va tagliato a metà."""
    out, quote = [], None
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            out.append(ch)
        elif ch == "#":
            break
        else:
            out.append(ch)
    return "".join(out).rstrip()


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def env_from_text(text: str) -> dict:
    """{nome: valore} dal blocco `env:` del manifest."""
    out, dentro, nome = {}, False, None
    for raw in (text or "").splitlines():
        line = _strip_comment(raw)
        if not line.strip():
            continue
        if not line[:1].isspace():                 # torna a indentazione zero
            dentro = line.strip().startswith("env:")
            nome = None
            continue
        if not dentro:
            continue
        s = line.strip()
        if s.startswith("- "):
            nome = None
            s = s[2:].strip()
        if s.startswith("name:"):
            nome = _unquote(s[5:])
        elif s.startswith("value:") and nome:
            out[nome] = _unquote(s[6:])
            nome = None
        elif s.startswith("valueFrom") and nome:
            # riferimento a un secret: lo risolve la piattaforma, non noi
            nome = None
    return out


def apply_env(path: str, environ=None) -> list:
    """Inietta i valori del manifest nell'ambiente, SENZA sovrascrivere quelli
    già presenti. Ritorna i nomi effettivamente aggiunti."""
    env = os.environ if environ is None else environ
    try:
        with open(path, encoding="utf-8") as f:
            valori = env_from_text(f.read())
    except Exception:
        return []
    aggiunti = []
    for k, v in valori.items():
        if k not in env:
            env[k] = v
            aggiunti.append(k)
    return aggiunti
