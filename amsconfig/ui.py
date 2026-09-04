"""Pannello Impostazioni: drawer laterale con l'icona a ingranaggio.

PERCHÉ UN DRAWER FATTO A MANO. Gradio 4.44 non ha né un componente drawer né un
modal (`gr.Sidebar` arriva con la serie 5, che qui non possiamo usare: il CSS
della chat è verificato su 4.44.1 e le versioni sono pinnate). Il pannello è
quindi una normale colonna, resa fissa a destra dal CSS: si apre e si chiude
cambiando `visible`, senza JavaScript e senza dipendere da classi interne di
Gradio. Se il CSS non venisse applicato, il pannello resterebbe una colonna in
fondo alla pagina: brutto, ma funzionante.

PERCHÉ TUTTI I CONTROLLI SONO COSTRUITI SUBITO. In Gradio 4 i componenti si
dichiarano all'avvio (`gr.render` è della serie 5): la ricerca e il filtro per
categoria non ricostruiscono la lista, mostrano e nascondono righe già presenti.
"""

from __future__ import annotations

import html as _html
import json
import os
import tempfile

from .catalog import CATEGORIES, SETTINGS
from .models import (BOOL, COLOR, DICT, ENUM, FLOAT, INT, JSON, LIST, NOW,
                     STRING, TEXT, URL)
from .validation import to_editor_text

# Ordine unico e stabile: gli handler ricevono e restituiscono i valori in
# questa sequenza. Un solo posto da cui dipende tutto il cablaggio.
KEYS = [s.key for s in SETTINGS]


# --- CSS: viene appeso a quello dell'app -------------------------------------
CSS = """
/* ---------- pannello Impostazioni (drawer) ---------- */
#ams-tabwrap { position:relative; }
#ams-gear { position:absolute; right:0; top:0; z-index:6; min-width:0 !important;
  width:38px; height:34px; padding:0 !important; font-size:16px; line-height:1;
  border:1px solid var(--ams-line) !important; background:var(--ams-ink800) !important;
  color:var(--ams-txt2) !important; border-radius:var(--ams-r); }
#ams-gear:hover { color:var(--ams-brand) !important; border-color:var(--ams-brand) !important; }

#ams-settings { position:fixed; top:0; right:0; bottom:0; width:min(760px, 96vw);
  z-index:1200; overflow-y:auto; padding:18px 20px 60px;
  background:var(--ams-ink900); border-left:1px solid var(--ams-line);
  box-shadow:-24px 0 60px #00000066; }
#ams-settings .ams-set { border:1px solid var(--ams-line-soft); border-radius:var(--ams-r);
  padding:12px 14px; margin-bottom:10px; background:var(--ams-ink800); }
#ams-settings .st-name { font:600 13px/1.3 var(--ams-sans); color:var(--ams-txt); }
#ams-settings .st-desc { font:400 12px/1.5 var(--ams-sans); color:var(--ams-txt2);
  margin-top:3px; }
#ams-settings .st-help { font:400 11.5px/1.55 var(--ams-sans); color:var(--ams-txt3);
  margin-top:5px; }
#ams-settings .st-meta { margin-top:7px; display:flex; gap:6px; flex-wrap:wrap;
  font:400 10px/1.6 var(--ams-mono); letter-spacing:.02em; }
#ams-settings .st-meta span { border:1px solid var(--ams-line); border-radius:4px;
  padding:1px 6px; color:var(--ams-txt3); }
#ams-settings .st-meta .on { color:var(--ams-brand); border-color:var(--ams-brand-mid); }
#ams-settings .st-meta .warn { color:var(--ams-hold); border-color:var(--ams-hold); }
#ams-settings .st-def { color:var(--ams-txt2); }
#ams-settings-head { display:flex; align-items:baseline; gap:10px; margin-bottom:2px; }
#ams-settings-head h2 { font:600 16px/1.2 var(--ams-sans); color:var(--ams-txt); margin:0; }
#ams-settings-head p { font:400 11.5px/1.5 var(--ams-sans); color:var(--ams-txt3); margin:0; }
"""


def _badge(text: str, kind: str = "") -> str:
    cls = f' class="{kind}"' if kind else ""
    return f"<span{cls}>{_html.escape(text)}</span>"


def _card_html(item) -> str:
    """La scheda di un'impostazione: nome, cosa fa, aiuto, tipo, default.
    Il valore corrente NON è qui: è nel controllo sotto, che è modificabile.
    Ripeterlo significherebbe averlo in due posti e vederli divergere."""
    s = item.setting
    tipo = _TYPE_LABEL.get(s.data_type, s.data_type)
    meta = [_badge(f"tipo: {tipo}")]
    if s.unit:
        meta.append(_badge(f"unità: {s.unit}"))
    if s.min_value is not None or s.max_value is not None:
        lo = "—" if s.min_value is None else _num(s.min_value)
        hi = "—" if s.max_value is None else _num(s.max_value)
        meta.append(_badge(f"ammesso: {lo} … {hi}"))
    if s.applies != NOW:
        meta.append(_badge(f"ha effetto {s.applies}", "warn"))
    if item.source == "database":
        meta.append(_badge("modificato", "on"))
    if item.source == "env":
        meta.append(_badge("forzato da variabile d'ambiente", "warn"))
    if not s.is_editable:
        meta.append(_badge("sola lettura", "warn"))
    default_txt = to_editor_text(s, item.default_value)
    if len(default_txt) > 160:
        default_txt = default_txt[:160] + "…"
    meta.append(f'<span class="st-def">default: '
                f'{_html.escape(default_txt) or "(vuoto)"}</span>')
    help_html = (f'<div class="st-help">{_html.escape(s.help_text)}</div>'
                 if s.help_text else "")
    return (f'<div class="st-name">{_html.escape(s.display_name)}</div>'
            f'<div class="st-desc">{_html.escape(s.description)}</div>'
            f'{help_html}'
            f'<div class="st-meta">{"".join(meta)}</div>')


_TYPE_LABEL = {
    STRING: "testo", TEXT: "testo lungo", INT: "numero intero", FLOAT: "numero",
    BOOL: "sì/no", ENUM: "scelta", LIST: "elenco", DICT: "oggetto JSON",
    JSON: "valore JSON", URL: "indirizzo web", COLOR: "colore",
}


def _num(x) -> str:
    return str(int(x)) if float(x).is_integer() else str(x)


def _label_for(item) -> str:
    origine = {"default": "valore di default", "database": "modificato e salvato",
               "env": "forzato dall'ambiente"}[item.source]
    return f"Valore corrente — {origine}"


def _value_for(item):
    """Il valore nella forma che il controllo si aspetta."""
    s = item.setting
    if s.is_secret:
        # Un segreto non si mostra nemmeno in sola lettura: il pannello è
        # visibile a chiunque apra l'App, e questo campo finirebbe anche negli
        # screenshot di un ticket.
        return "••••••" if item.current_value else ""
    if s.data_type == BOOL:
        return bool(item.current_value)
    if s.data_type in (INT, FLOAT):
        return item.current_value
    return to_editor_text(s, item.current_value)


def _make_control(gr, item):
    s = item.setting
    common = dict(label=_label_for(item), value=_value_for(item),
                  interactive=s.is_editable, show_label=True)
    if s.data_type == BOOL:
        return gr.Checkbox(**common)
    if s.data_type in (INT, FLOAT):
        return gr.Number(precision=0 if s.data_type == INT else None,
                         minimum=s.min_value, maximum=s.max_value, **common)
    if s.data_type == ENUM:
        scelte = list(s.allowed_values)
        # Un valore arrivato dall'ambiente o da una versione precedente può non
        # essere fra le scelte: aggiungerlo evita un menu che si apre vuoto.
        if item.current_value not in scelte and item.current_value is not None:
            scelte = scelte + [item.current_value]
        return gr.Dropdown(choices=scelte, allow_custom_value=True, **common)
    if s.data_type in (LIST, DICT, JSON, TEXT):
        righe = 8 if s.data_type in (DICT, TEXT) else 4
        return gr.Textbox(lines=righe, max_lines=20, **common)
    return gr.Textbox(lines=1, **common)


def mount(gr, service, ok_html, err_html, actor_provider):
    """Costruisce il pannello. Da chiamare DENTRO `with gr.Blocks()`.

    Ritorna (drawer, apri) dove `apri` è la funzione da collegare al click
    dell'ingranaggio: `gear.click(*apri)`.
    """
    comps, cards, rows = {}, {}, {}
    acc_by_cat, reset_btn_by_cat, reset_btn_by_key = {}, {}, {}

    with gr.Column(elem_id="ams-settings", visible=False) as drawer:
        with gr.Row():
            gr.HTML('<div id="ams-settings-head"><h2>Impostazioni</h2>'
                    '<p>Le modifiche restano salvate anche dopo un riavvio.</p></div>')
            chiudi = gr.Button("Chiudi ✕", scale=0)
        with gr.Row():
            cerca = gr.Textbox(placeholder="Cerca fra le impostazioni…",
                               show_label=False, scale=3)
            filtro = gr.Dropdown(choices=["Tutte"] + [c.display_name for c in CATEGORIES],
                                 value="Tutte", show_label=False, scale=2)
        with gr.Row():
            salva = gr.Button("Salva modifiche", variant="primary", scale=2)
            annulla = gr.Button("Annulla modifiche", scale=1)
            reset_tutto = gr.Button("Ripristina tutto", variant="stop", scale=1)
        with gr.Row():
            esporta = gr.Button("Esporta JSON", scale=1)
            file_import = gr.File(label="Importa JSON", file_count="single",
                                  file_types=[".json"], scale=2)
            importa = gr.Button("Applica il file", scale=1)
        file_export = gr.File(label="Configurazione esportata", visible=False)
        msg = gr.HTML()

        for cat in CATEGORIES:
            items = [service.item(s.key) for s in SETTINGS if s.category == cat.key]
            with gr.Accordion(f"⚙ {cat.display_name}  ·  {len(items)}",
                              open=False) as acc:
                gr.HTML(f'<div class="st-desc">{_html.escape(cat.description)}</div>')
                for it in items:
                    with gr.Group(elem_classes="ams-set") as riga:
                        cards[it.key] = gr.HTML(_card_html(it))
                        comps[it.key] = _make_control(gr, it)
                        if it.setting.is_editable:
                            reset_btn_by_key[it.key] = gr.Button(
                                "↺ Riporta al default", size="sm")
                        rows[it.key] = riga
                reset_btn_by_cat[cat.key] = gr.Button(
                    f"Ripristina «{cat.display_name}»", size="sm")
            acc_by_cat[cat.key] = acc

    ordered = [comps[k] for k in KEYS]
    ordered_cards = [cards[k] for k in KEYS]
    ordered_rows = [rows[k] for k in KEYS]
    ordered_accs = [acc_by_cat[c.key] for c in CATEGORIES]

    # --- funzioni di supporto ------------------------------------------------
    def _refresh_values():
        """Valori + etichette + schede, ricalcolati dal servizio."""
        out = []
        for k in KEYS:
            it = service.item(k)
            out.append(gr.update(value=_value_for(it), label=_label_for(it)))
        for k in KEYS:
            out.append(gr.update(value=_card_html(service.item(k))))
        return out

    def _apri():
        # Rileggere all'apertura evita di mostrare valori vecchi quando un'altra
        # replica dell'App (o un collega) ha salvato nel frattempo.
        service.warm()
        return [gr.update(visible=True), ""] + _refresh_values()

    def _chiudi():
        return gr.update(visible=False)

    def _salva(*vals):
        proposti = dict(zip(KEYS, vals))
        try:
            rep = service.save_config(proposti, actor_provider())
        except Exception as e:
            return [err_html(f"Salvataggio non riuscito: {str(e)[:200]}")] + _refresh_values()
        parti = []
        if rep["saved"]:
            parti.append(f"{len(rep['saved'])} impostazioni salvate")
        if rep["unchanged"]:
            parti.append(f"{len(rep['unchanged'])} invariate")
        if rep["env"]:
            parti.append(f"{len(rep['env'])} ignorate perché forzate da variabile "
                         f"d'ambiente ({', '.join(rep['env'][:3])})")
        testo = "; ".join(parti) or "Nessuna modifica da salvare."
        if rep["errors"]:
            elenco = "; ".join(f"{k}: {v}" for k, v in list(rep["errors"].items())[:5])
            return [err_html(f"{testo}. Non salvate per errore di validazione — "
                             f"{elenco}")] + _refresh_values()
        riavvii = [k for k in rep["saved"] if _needs_reload(k)]
        if riavvii:
            testo += ". Alcune modifiche (tema e intestazione) si vedono " \
                     "ricaricando la pagina."
        return [ok_html(testo)] + _refresh_values()

    def _annulla():
        return [ok_html("Modifiche annullate: rimessi i valori salvati.")] + _refresh_values()

    def _reset_tutto():
        try:
            n = service.reset_all()
        except Exception as e:
            return [err_html(str(e)[:200])] + _refresh_values()
        return [ok_html(f"Ripristinati i default applicativi ({n} valori "
                        f"personalizzati rimossi).")] + _refresh_values()

    def _reset_categoria(cat_key: str):
        def _fn():
            try:
                service.reset_category(cat_key)
            except Exception as e:
                return [err_html(str(e)[:200])] + _refresh_values()
            nome = next(c.display_name for c in CATEGORIES if c.key == cat_key)
            return [ok_html(f"Categoria «{nome}» riportata ai default.")] + _refresh_values()
        return _fn

    def _filtra(testo, categoria):
        cat_key = None
        for c in CATEGORIES:
            if c.display_name == categoria:
                cat_key = c.key
        visibili = {i.key for i in service.get_config(category=cat_key, query=testo)}
        righe = [gr.update(visible=(k in visibili)) for k in KEYS]
        # Una categoria senza risultati sparisce; con una ricerca in corso le
        # altre si aprono da sole, altrimenti si vedrebbero solo i titoli.
        acc = []
        for c in CATEGORIES:
            n = sum(1 for s in SETTINGS if s.category == c.key and s.key in visibili)
            aperto = bool((testo or "").strip()) and n > 0
            acc.append(gr.update(visible=n > 0, open=aperto,
                                 label=f"⚙ {c.display_name}  ·  {n}"))
        return righe + acc

    def _esporta():
        try:
            testo = service.export_config()
        except Exception as e:
            return gr.update(visible=False), err_html(str(e)[:200])
        d = tempfile.mkdtemp(prefix="amscfg-")
        p = os.path.join(d, f"impostazioni-{service.label}.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write(testo)
        n = len(json.loads(testo).get("values", {}))
        return (gr.update(value=p, visible=True),
                ok_html(f"Esportate {n} impostazioni personalizzate. I segreti non "
                        f"sono inclusi."))

    def _importa(file_obj):
        if not file_obj:
            return [err_html("Scegli prima un file JSON.")] + _refresh_values()
        try:
            with open(getattr(file_obj, "name", file_obj), encoding="utf-8") as f:
                rep = service.import_config(f.read(), actor_provider())
        except Exception as e:
            return [err_html(f"Import non riuscito: {str(e)[:200]}")] + _refresh_values()
        parti = [f"{len(rep['saved'])} impostazioni applicate"]
        if rep["unchanged"]:
            parti.append(f"{len(rep['unchanged'])} già uguali")
        if rep.get("ignored"):
            parti.append(f"{len(rep['ignored'])} chiavi sconosciute ignorate")
        if rep["errors"]:
            parti.append(f"{len(rep['errors'])} scartate per errore di validazione")
        return [ok_html("; ".join(parti) + ".")] + _refresh_values()

    def _reset_singola(key: str):
        def _fn():
            try:
                service.reset_config(key)
            except Exception as e:
                return [err_html(str(e)[:200])] + _refresh_values()
            nome = service.item(key).setting.display_name
            return [ok_html(f"«{nome}» riportata al valore di default.")] + _refresh_values()
        return _fn

    # --- cablaggio -----------------------------------------------------------
    tutti = [msg] + ordered + ordered_cards
    chiudi.click(_chiudi, None, drawer)
    salva.click(_salva, ordered, tutti)
    annulla.click(_annulla, None, tutti)
    reset_tutto.click(_reset_tutto, None, tutti)
    esporta.click(_esporta, None, [file_export, msg])
    importa.click(_importa, file_import, tutti)
    cerca.change(_filtra, [cerca, filtro], ordered_rows + ordered_accs)
    filtro.change(_filtra, [cerca, filtro], ordered_rows + ordered_accs)
    for cat_key, btn in reset_btn_by_cat.items():
        btn.click(_reset_categoria(cat_key), None, tutti)
    for key, btn in reset_btn_by_key.items():
        btn.click(_reset_singola(key), None, tutti)

    apri = (_apri, None, [drawer, msg] + ordered + ordered_cards)
    return drawer, apri


def _needs_reload(key: str) -> bool:
    from .catalog import BY_KEY
    s = BY_KEY.get(key)
    return bool(s and s.applies != NOW)
