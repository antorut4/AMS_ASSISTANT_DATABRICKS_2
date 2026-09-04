"""Catalogo delle impostazioni: l'unica fonte di verità su COSA esiste.

Qui non ci sono valori di ambiente, solo il default applicativo e i metadati che
rendono un'impostazione comprensibile a chi non ha letto il codice. I valori che
cambiano da tenant a tenant (schema, warehouse, ServiceNow, team, path del
workspace, sottotitolo) stanno in tenants.py: questo file resta identico per
tutti, e la differenza fra un ambiente e l'altro è leggibile in un colpo d'occhio
invece di essere sparsa in due copie di app.py.

Regola pratica per chi aggiunge un'impostazione: si aggiunge UNA riga qui, e
nient'altro. La UI, la validazione, il salvataggio e l'export la prendono da qui.
"""

from __future__ import annotations

from .models import (BOOL, COLOR, DICT, ENUM, FLOAT, INT, JSON, LIST, RELOAD,
                     RESTART, STRING, TEXT, URL, Category, Setting)

# --- categorie, nell'ordine in cui compaiono nel pannello --------------------
CATEGORIES = (
    Category("general", "Generale",
             "Identità dell'applicazione e collegamento all'ambiente Databricks."),
    Category("models", "Modelli AI",
             "Quale modello scrive le analisi, con quali limiti e a che costo."),
    Category("embedding", "Embedding",
             "Il modello che trasforma i testi in vettori per la ricerca semantica."),
    Category("rag", "RAG e documentazione",
             "Come la documentazione viene spezzata, cercata e iniettata nel contesto."),
    Category("servicenow", "ServiceNow",
             "Collegamento all'istanza ServiceNow da cui arrivano i ticket."),
    Category("ingestion", "Importazione ticket",
             "Come vengono interpretati e salvati i ticket, da ServiceNow o da Excel."),
    Category("dashboard", "Dashboard",
             "Che cosa resta visibile nella schermata operativa."),
    Category("sal", "SAL",
             "La finestra settimanale del SAL e le soglie dei suoi indicatori."),
    Category("workflow", "Workflow agente",
             "Quanto a lungo e quanto in profondità l'agente può lavorare su un ticket."),
    Category("agent_tools", "Agent Tools",
             "Dove l'agente può guardare: schemi, notebook, limiti di scansione."),
    Category("taxonomy", "Tassonomia analisi",
             "Le classificazioni ammesse per problema e gravità."),
    Category("prompts", "Prompt dell'agente",
             "Le istruzioni permanenti date al modello. Cambiarle cambia le analisi."),
    Category("team", "Team",
             "Le persone a cui si possono assegnare i ticket."),
    Category("attachments", "Allegati",
             "Quanto testo e quanta immagine accettare dagli allegati."),
    Category("branding", "Branding",
             "Nome, sottotitolo e colore d'accento dell'interfaccia."),
)


def S(key, category, display_name, description, **kw) -> Setting:
    return Setting(key=key, category=category, display_name=display_name,
                   description=description, **kw)


# Testo riusato: le impostazioni di bootstrap non possono stare nel database,
# perché servono PER RAGGIUNGERLO. Se fossero modificabili da qui, un valore
# sbagliato renderebbe irraggiungibile anche il pannello per correggerlo.
_BOOTSTRAP_NOTE = (
    "Si cambia dal codice o da variabile d'ambiente, non da qui: è l'indirizzo con "
    "cui l'App raggiunge il database, e serve PRIMA di poter leggere qualsiasi "
    "impostazione salvata. Se fosse modificabile da questo pannello, un valore "
    "sbagliato lascerebbe l'App senza il pannello stesso per rimediare.")

_SECRET_NOTE = (
    "Non si imposta da qui: finirebbe in chiaro in una tabella Delta leggibile da "
    "chiunque abbia SELECT sullo schema. Usa le variabili d'ambiente SNOW_USER / "
    "SNOW_PASS nelle App Settings, oppure il secret scope indicato qui sotto.")

SETTINGS: tuple = (

    # ---------------------------------------------------------------- Generale
    S("project.name", "general", "Nome del progetto",
      "Etichetta interna dell'applicazione.",
      help_text="Non compare nell'interfaccia: il titolo mostrato in alto è "
                "«Titolo dell'intestazione», nella categoria Branding.",
      data_type=STRING, default="AMS Ticket Assistant"),
    S("project.slug", "general", "Sigla del progetto",
      "Identificativo breve, usato come prefisso tecnico.",
      help_text="Serve a distinguere l'installazione in log e nomi generati. "
                "Solo lettere minuscole, senza spazi.",
      data_type=STRING, default="ams"),
    S("project.language", "general", "Lingua",
      "Lingua delle risposte e dell'interfaccia.",
      help_text="Le istruzioni al modello chiedono già esplicitamente l'italiano "
                "(vedi Prompt dell'agente): questa chiave è informativa.",
      data_type=STRING, default="it"),
    S("storage.schema", "general", "Schema delle tabelle",
      "Schema che contiene le tabelle dell'applicazione.",
      help_text=_BOOTSTRAP_NOTE + " Dopo un cambio serve rieseguire il bootstrap "
                "per creare le tabelle nel nuovo schema.",
      data_type=STRING, default="", applies=RESTART,
      is_editable=False, editable_note=_BOOTSTRAP_NOTE),
    S("compute.warehouse_id", "general", "SQL Warehouse",
      "Identificativo del warehouse che esegue tutte le query.",
      help_text=_BOOTSTRAP_NOTE,
      data_type=STRING, default="", applies=RESTART,
      is_editable=False, editable_note=_BOOTSTRAP_NOTE),
    S("compute.default_host", "general", "Host del workspace",
      "Indirizzo del workspace Databricks, usato se l'ambiente non lo fornisce.",
      help_text=_BOOTSTRAP_NOTE + " In esecuzione come Databricks App il valore "
                "arriva già dalla variabile DATABRICKS_HOST.",
      data_type=URL, default="", applies=RESTART,
      is_editable=False, editable_note=_BOOTSTRAP_NOTE),

    # ------------------------------------------------------------- Modelli AI
    S("models.chat_primary", "models", "Modello AI principale",
      "Modello usato per generare le analisi dei ticket e le risposte in chat.",
      help_text="Se l'endpoint non risponde o è sovraccarico, l'App riprova con i "
                "modelli di riserva elencati qui sotto. Modelli più grandi scrivono "
                "analisi migliori ma costano di più e sono più lenti: il costo per "
                "milione di token è nel listino prezzi di questa stessa categoria.",
      data_type=ENUM, default="databricks-claude-sonnet-4-6",
      allowed_values=("databricks-claude-opus-4-8", "databricks-claude-opus-4-7",
                      "databricks-claude-sonnet-4-6", "databricks-claude-haiku-4-5",
                      "databricks-gpt-5-6-sol", "databricks-gpt-5-5-pro",
                      "databricks-qwen35-122b-a10b", "databricks-gemini-3-5-flash")),
    S("models.chat_fallbacks", "models", "Modelli di riserva",
      "Modelli usati, nell'ordine, se il principale non risponde.",
      help_text="Una voce per riga. Metti per primo il più economico: qui ci si "
                "arriva solo quando il principale è già fallito, e l'obiettivo è "
                "dare comunque una risposta. Lista vuota = nessun ripiego.",
      data_type=LIST, default=["databricks-claude-haiku-4-5"]),
    S("models.max_tokens", "models", "Lunghezza massima della risposta",
      "Tetto ai token che il modello può produrre in una singola risposta.",
      help_text="Alzarlo permette analisi più lunghe e costa di più; abbassarlo "
                "troppo tronca le analisi a metà. 4000 token ≈ 3 pagine.",
      data_type=INT, default=4000, min_value=256, max_value=32000, unit="token"),
    S("models.temperature", "models", "Temperatura",
      "Quanto il modello si concede di variare la risposta.",
      help_text="0 = deterministico, la stessa domanda dà la stessa analisi: è ciò "
                "che serve su un incident. Valori alti rendono il testo più vario "
                "ma anche più inventivo, quindi meno affidabile sulle root cause.",
      data_type=FLOAT, default=0.0, min_value=0.0, max_value=2.0),
    S("models.request_timeout_s", "models", "Timeout per chiamata",
      "Quanto si aspetta una singola risposta del modello prima di mollarla.",
      help_text="È un tetto DURO: serve a non lasciare l'operatore davanti a una "
                "rotella infinita quando un endpoint si impunta.",
      data_type=INT, default=60, min_value=5, max_value=600, unit="secondi"),
    S("models.max_attempts", "models", "Tentativi per chiamata",
      "Quante volte riprovare la stessa chiamata prima di passare al ripiego.",
      help_text="Ogni tentativo in più allunga l'attesa dell'operatore nel caso "
                "peggiore: tempo massimo ≈ tentativi × timeout.",
      data_type=INT, default=2, min_value=1, max_value=5),
    S("models.prices", "models", "Listino prezzi dei modelli",
      "Prezzo indicativo in € per milione di token, per la stima dei costi.",
      help_text="Oggetto JSON: per ogni endpoint, [prezzo input, prezzo output]. "
                "Serve solo alla schermata Costi: non influenza le analisi. Va "
                "aggiornato a mano quando cambiano i listini.",
      data_type=DICT,
      default={"databricks-claude-opus-4-8": [5.00, 25.00],
               "databricks-claude-opus-4-7": [5.00, 25.00],
               "databricks-claude-sonnet-4-6": [3.00, 15.00],
               "databricks-claude-haiku-4-5": [1.00, 5.00],
               "databricks-gpt-5-6-sol": [2.50, 15.00],
               "databricks-gpt-5-5-pro": [30.0, 180.0],
               "databricks-qwen35-122b-a10b": [0.40, 2.40],
               "databricks-gemini-3-5-flash": [0.50, 3.00]}),
    S("models.default_price", "models", "Prezzo di riserva",
      "Prezzo usato per un modello che non compare nel listino.",
      help_text="Lista JSON di due numeri: [prezzo input, prezzo output] per "
                "milione di token. Evita che un modello nuovo risulti gratis.",
      data_type=JSON, default=[3.00, 15.00]),

    # --------------------------------------------------------------- Embedding
    S("models.embedding_endpoint", "embedding", "Endpoint di embedding",
      "Modello che trasforma documenti e ticket in vettori per la ricerca.",
      help_text="Cambiarlo rende STANTII tutti i vettori già calcolati: i vecchi e "
                "i nuovi non sono confrontabili. L'App se ne accorge da sola (la "
                "firma dei chunk cambia) e li ricostruisce, ma finché non ha finito "
                "la ricerca semantica sui documenti vecchi resta parziale.",
      data_type=STRING, default="databricks-qwen3-embedding-0-6b"),
    S("models.embedding_dim", "embedding", "Dimensione dei vettori",
      "Numero di componenti del vettore prodotto dall'endpoint.",
      help_text="Non più letta dal codice: la dimensione arriva direttamente dalla "
                "risposta dell'endpoint. Resta per compatibilità.",
      data_type=INT, default=1024, min_value=1, max_value=8192),

    # --------------------------------------------------------------------- RAG
    S("rag.top_k", "rag", "Documenti richiamati",
      "Quanti documenti e ticket simili allegare al contesto di un'analisi.",
      help_text="Più alto = l'agente vede più materiale, ma il contesto si riempie "
                "e il costo per analisi sale. Oltre 5 il rumore tende a superare "
                "il beneficio.",
      data_type=INT, default=3, min_value=1, max_value=10),
    S("rag.min_score", "rag", "Somiglianza minima",
      "Soglia sotto la quale un documento è considerato non pertinente.",
      help_text="Da 0 a 1. Alzarla riduce i falsi accostamenti; abbassarla troppo "
                "riempie il contesto di documenti che non c'entrano. Se la ricerca "
                "non trova mai nulla, prova prima ad abbassare questa soglia.",
      data_type=FLOAT, default=0.30, min_value=0.0, max_value=1.0),
    S("rag.doc_char_budget", "rag", "Budget di documentazione",
      "Quanti caratteri di documentazione iniettare, in totale, in un'analisi.",
      help_text="I documenti pertinenti entrano nel contesto INTERI, non a "
                "spezzoni: questo è il tetto complessivo. Alzarlo dà all'agente "
                "più materiale e aumenta i token spesi a ogni analisi.",
      data_type=INT, default=100000, min_value=1000, max_value=1000000,
      unit="caratteri"),
    S("rag.chunk_chars", "rag", "Dimensione dei chunk",
      "In quanti caratteri viene spezzato un documento ai fini della ricerca.",
      help_text="La ricerca lavora sui chunk, così anche il centro di un manuale "
                "lungo è raggiungibile. Chunk piccoli = ricerca più precisa ma più "
                "vettori da calcolare e conservare. Cambiarlo rende stantio "
                "l'indice: l'App lo ricostruisce da sé, un po' alla volta.",
      data_type=INT, default=3000, min_value=500, max_value=20000,
      unit="caratteri"),
    S("rag.chunk_overlap", "rag", "Sovrapposizione fra chunk",
      "Quanti caratteri ogni chunk ripete da quello precedente.",
      help_text="Serve a non spezzare un concetto a metà: con la sovrapposizione "
                "resta leggibile in almeno uno dei due chunk. Viene comunque "
                "limitata a metà della dimensione del chunk.",
      data_type=INT, default=300, min_value=0, max_value=5000, unit="caratteri"),
    S("rag.max_index_rows", "rag", "Tetto dell'indice in memoria",
      "Quanti chunk al massimo tenere in memoria per la ricerca.",
      help_text="Il calcolo delle somiglianze è in Python: oltre qualche decina di "
                "migliaia di chunk conviene passare a Databricks Vector Search. "
                "Se l'indice viene troncato, l'App lo scrive nei log.",
      data_type=INT, default=50000, min_value=1000, max_value=500000, unit="chunk"),
    S("rag.index_cache_s", "rag", "Durata della cache dell'indice",
      "Per quanto tempo l'indice dei vettori resta in memoria prima di essere "
      "riletto dal database.",
      help_text="Senza cache ogni analisi e ogni messaggio di chat riscaricherebbero "
                "l'intero indice. Con più repliche dell'App, un documento aggiunto "
                "su una replica diventa visibile alle altre entro questo tempo.",
      data_type=INT, default=300, min_value=0, max_value=3600, unit="secondi"),
    S("rag.reindex_every_s", "rag", "Frequenza del reindex automatico",
      "Ogni quanto il processo in background ripara i documenti non indicizzati.",
      help_text="0 disattiva il worker: fallo se esegui più repliche dell'App e "
                "preferisci un Job dedicato, perché due repliche che reindicizzano "
                "lo stesso documento insieme possono lasciare chunk duplicati.",
      data_type=INT, default=900, min_value=0, max_value=86400, unit="secondi"),
    S("rag.reindex_batch", "rag", "Documenti per ciclo di reindex",
      "Quanti documenti reindicizzare a ogni giro del processo automatico.",
      help_text="Tenerlo basso evita che un cambio di configurazione scateni "
                "centinaia di chiamate di embedding in un colpo solo: il lavoro si "
                "riassorbe in più cicli.",
      data_type=INT, default=20, min_value=1, max_value=500, unit="documenti"),
    S("rag.notebook_import_max", "rag", "Notebook per importazione",
      "Quanti notebook al massimo importare in una singola operazione.",
      help_text="Vale anche quando si indica una cartella: una cartella grossa "
                "significa altrettante chiamate di embedding tutte insieme, dentro "
                "una sola richiesta del browser.",
      data_type=INT, default=50, min_value=1, max_value=500, unit="notebook"),

    # -------------------------------------------------------------- ServiceNow
    S("servicenow.enabled", "servicenow", "Integrazione attiva",
      "Abilita la sincronizzazione dei ticket da ServiceNow.",
      help_text="Se disattivata, i ticket si caricano solo da file Excel e il "
                "pulsante di sincronizzazione resta inutilizzabile.",
      data_type=BOOL, default=False),
    S("servicenow.base_url", "servicenow", "Indirizzo delle API",
      "URL di base delle API Table di ServiceNow.",
      help_text="Termina di norma con /api/now/table. Vuoto significa integrazione "
                "non configurata.",
      data_type=URL, default=""),
    S("servicenow.incident_table", "servicenow", "Tabella degli incident",
      "Nome della tabella ServiceNow che contiene gli incident.",
      help_text="Si cambia solo su istanze personalizzate: il valore standard è "
                "«incident».",
      data_type=STRING, default="incident"),
    S("servicenow.request_item_table", "servicenow", "Tabella delle request",
      "Nome della tabella ServiceNow che contiene le request item.",
      help_text="Valore standard: «sc_req_item».",
      data_type=STRING, default="sc_req_item"),
    S("servicenow.assignment_group_ids", "servicenow", "Gruppi di assegnazione",
      "Identificativi (sys_id) dei gruppi di cui scaricare i ticket.",
      help_text="Uno per riga. Sono i sys_id dei gruppi, non i loro nomi: si "
                "leggono dall'URL della pagina del gruppo in ServiceNow. Elenco "
                "vuoto = nessun ticket scaricato.",
      data_type=LIST, default=[]),
    S("servicenow.page_size", "servicenow", "Ticket per pagina",
      "Quanti record chiedere a ogni chiamata API.",
      help_text="Pagine grandi = meno chiamate ma risposte più pesanti. Alcune "
                "istanze rifiutano valori sopra il migliaio.",
      data_type=INT, default=1000, min_value=1, max_value=10000),
    S("servicenow.max_records", "servicenow", "Tetto per sincronizzazione",
      "Quanti ticket al massimo scaricare in una sincronizzazione.",
      help_text="Protegge da una sincronizzazione infinita se i filtri sono troppo "
                "larghi.",
      data_type=INT, default=50000, min_value=1, max_value=1000000),
    S("servicenow.closed_lookback_days", "servicenow", "Storico dei ticket chiusi",
      "Quanti giorni indietro andare a prendere i ticket già chiusi.",
      help_text="Servono al SAL «chiusi della settimana». Tenerlo basso evita di "
                "riscaricare anni di storico a ogni sincronizzazione: 60 giorni "
                "coprono 8 settimane di SAL.",
      data_type=INT, default=60, min_value=0, max_value=3650, unit="giorni"),
    S("servicenow.fields", "servicenow", "Campi richiesti",
      "Elenco dei campi da chiedere all'API.",
      help_text="Uno per riga. Chiedere solo i campi utili rende le risposte molto "
                "più leggere. Elenco vuoto = tutti i campi.",
      data_type=LIST, default=["number", "short_description", "description",
                               "close_notes", "comments", "state", "priority",
                               "assignment_group", "opened_at", "closed_at",
                               "sys_created_on", "sys_updated_on", "caller_id",
                               "requested_for", "active"]),
    S("servicenow.user", "servicenow", "Utente (in chiaro)",
      "Utente ServiceNow, se non si usano variabili d'ambiente o secret.",
      help_text=_SECRET_NOTE, data_type=STRING, default="", is_secret=True,
      is_editable=False, editable_note=_SECRET_NOTE),
    S("servicenow.password", "servicenow", "Password (in chiaro)",
      "Password ServiceNow, se non si usano variabili d'ambiente o secret.",
      help_text=_SECRET_NOTE, data_type=STRING, default="", is_secret=True,
      is_editable=False, editable_note=_SECRET_NOTE),
    S("servicenow.secret_scope", "servicenow", "Secret scope",
      "Nome dello scope Databricks che contiene le credenziali.",
      help_text="È il modo consigliato di conservare le credenziali: i valori "
                "restano fuori dal codice e fuori dal database.",
      data_type=STRING, default="AutoApi"),
    S("servicenow.secret_user", "servicenow", "Chiave del segreto: utente",
      "Nome della chiave, dentro lo scope, che contiene l'utente.",
      help_text="Il valore atteso è codificato in base64.",
      data_type=STRING, default="servicenow-user"),
    S("servicenow.secret_password", "servicenow", "Chiave del segreto: password",
      "Nome della chiave, dentro lo scope, che contiene la password.",
      help_text="Il valore atteso è codificato in base64.",
      data_type=STRING, default="servicenow-pass"),

    # -------------------------------------------------------- Importazione
    S("ingestion.exclude_state_keywords", "ingestion", "Parole degli stati chiusi",
      "Frammenti di testo che identificano uno stato come «chiuso».",
      help_text="Uno per riga, confronto per sottostringa e senza distinzione fra "
                "maiuscole e minuscole: «chius» copre Chiuso, Chiusa e Chiusi; "
                "«clos» copre Closed. Serve a far funzionare l'App con istanze in "
                "italiano e in inglese senza toccare il codice.",
      data_type=LIST, default=["clos", "chius", "resol", "risol", "cancel",
                               "annull", "fulfil", "evas", "complet"]),
    S("ingestion.drop_closed_on_ingest", "ingestion", "Scarta i chiusi in ingresso",
      "Se attivo, i ticket già chiusi non vengono nemmeno salvati.",
      help_text="Tenerlo disattivo: i chiusi servono al SAL della settimana e "
                "vengono comunque nascosti da Dashboard e Repository. Attivarlo "
                "svuota il SAL «chiusi della settimana».",
      data_type=BOOL, default=False),
    S("ingestion.merge_batch_size", "ingestion", "Ticket per scrittura",
      "Quanti ticket scrivere in una singola istruzione SQL.",
      help_text="Lotti grandi sono più veloci ma producono istruzioni SQL enormi, "
                "che alcuni warehouse rifiutano.",
      data_type=INT, default=300, min_value=1, max_value=5000),
    S("ingestion.preserve_on_empty", "ingestion", "Non svuotare con campi vuoti",
      "Un import che porta campi vuoti non cancella i valori già presenti.",
      help_text="Protegge dai file parziali: ricaricare un Excel con meno colonne "
                "non azzera i dati arricchiti da un'altra sorgente.",
      data_type=BOOL, default=True),
    S("ingestion.excel_replace", "ingestion", "L'Excel sostituisce l'elenco",
      "Ogni import Excel elimina i ticket Excel non più presenti nel file.",
      help_text="I ticket che hanno già un'analisi non vengono mai eliminati, e i "
                "ticket arrivati da ServiceNow non vengono toccati.",
      data_type=BOOL, default=True),
    S("excel_column_map", "ingestion", "Alias delle colonne Excel",
      "Come riconoscere le colonne del file, qualunque intestazione abbiano.",
      help_text="Oggetto JSON: per ogni campo dell'App, l'elenco delle "
                "intestazioni che valgono come quel campo. Il confronto ignora "
                "maiuscole e spazi ai bordi. Per far accettare un nuovo export "
                "basta aggiungere la sua intestazione alla lista giusta.",
      data_type=DICT,
      default={"number": ["Number", "Numero", "ID", "Task"],
               "short_description": ["Short description", "Breve descrizione",
                                     "Titolo", "Descrizione breve"],
               "description": ["Description", "Descrizione"],
               "state": ["State", "Stato"],
               "priority": ["Priority", "Priorità", "Impatto Opex",
                            "Impatto Business"],
               "assignment_group": ["Assignment group", "Gruppo"],
               "assignee": ["Assignee", "Assegnatario", "Assegnato a", "Owner",
                            "Assigned to"],
               "close_notes": ["Close notes", "Note di chiusura", "Note"],
               "caller": ["Caller", "Aperto da", "Requested for", "Richiesto da"],
               "opened_at": ["Opened", "Opened at", "Data apertura",
                             "Data creazione", "Created"],
               "closed_at": ["Closed", "Closed at", "Data chiusura",
                             "Data risoluzione", "Chiuso", "Resolved"],
               "updated_at": ["Updated", "Updated at", "Data aggiornamento",
                              "Sys updated on"]}),

    # ---------------------------------------------------------------- Dashboard
    S("dashboard.hide_state_keywords", "dashboard", "Stati nascosti in Dashboard",
      "Stati che spariscono dalla Dashboard perché non lavorabili adesso.",
      help_text="Uno per riga, confronto per sottostringa. I chiusi sono già "
                "esclusi a parte. Restano tutti visibili nel tab SAL: qui si "
                "decide solo cosa sta davanti agli occhi durante il lavoro.",
      data_type=LIST, default=["hold", "pending", "attesa", "sospes", "await",
                               "suspend"]),

    # --------------------------------------------------------------------- SAL
    S("sal.tz", "sal", "Fuso orario",
      "Fuso usato per calcolare l'inizio e la fine della settimana.",
      help_text="Nome IANA, es. Europe/Rome. Da questo dipende in quale settimana "
                "finisce un ticket aperto di sera.",
      data_type=STRING, default="Europe/Rome"),
    S("sal.week_start_weekday", "sal", "Giorno di inizio settimana",
      "Giorno in cui comincia la settimana del SAL.",
      help_text="0 = lunedì, 6 = domenica.",
      data_type=INT, default=0, min_value=0, max_value=6),
    S("sal.start_hour", "sal", "Ora di inizio",
      "Ora in cui si apre la finestra settimanale.",
      help_text="Insieme ai minuti definisce l'istante esatto di inizio: con 14:00 "
                "la settimana parte dal lunedì alle 14.",
      data_type=INT, default=14, min_value=0, max_value=23),
    S("sal.start_minute", "sal", "Minuti di inizio",
      "Minuti dell'ora di inizio della finestra.",
      data_type=INT, default=0, min_value=0, max_value=59),
    S("sal.end_hour", "sal", "Ora di fine",
      "Ora in cui si chiude la finestra settimanale.",
      help_text="Con 13:30 la settimana si chiude il lunedì successivo alle 13:30, "
                "mezz'ora prima della riunione di SAL.",
      data_type=INT, default=13, min_value=0, max_value=23),
    S("sal.end_minute", "sal", "Minuti di fine",
      "Minuti dell'ora di fine della finestra.",
      data_type=INT, default=30, min_value=0, max_value=59),
    S("sal.blocked_state_keywords", "sal", "Parole degli stati bloccati",
      "Stati che nel SAL contano come «bloccati».",
      help_text="Uno per riga, confronto per sottostringa. È l'elenco che alimenta "
                "le tabelle dei bloccati della settimana e del totale.",
      data_type=LIST, default=["hold", "pending", "attesa", "sospes", "await",
                               "suspend"]),
    S("sal.aging_alert_days", "sal", "Soglia di invecchiamento",
      "Da quanti giorni un ticket aperto va segnalato come vecchio.",
      data_type=INT, default=30, min_value=1, max_value=3650, unit="giorni"),
    S("sal.max_rows", "sal", "Righe per tabella",
      "Quante righe mostrare al massimo in ciascuna tabella del SAL.",
      help_text="Tetto di sicurezza: una settimana anomala non deve produrre una "
                "pagina da migliaia di righe.",
      data_type=INT, default=500, min_value=10, max_value=10000, unit="righe"),
    S("sal.weeks_selectable", "sal", "Settimane consultabili",
      "Quante settimane passate restano selezionabili nel menu del SAL.",
      data_type=INT, default=8, min_value=1, max_value=52, unit="settimane"),

    # ---------------------------------------------------------------- Workflow
    S("workflow.max_tool_iterations", "workflow", "Passi massimi in analisi",
      "Quante volte l'agente può usare uno strumento durante un'analisi.",
      help_text="Ogni passo è una query o una lettura di notebook. Più passi "
                "permettono indagini più profonde e costano tempo e token: il "
                "limite serve a non lasciare l'agente a girare a vuoto.",
      data_type=INT, default=8, min_value=1, max_value=30, unit="passi"),
    S("workflow.chat_max_iterations", "workflow", "Passi massimi in chat",
      "Quante volte l'agente può usare uno strumento rispondendo in chat.",
      help_text="Tenuto più basso dell'analisi: in chat ci si aspetta una risposta "
                "in pochi secondi.",
      data_type=INT, default=4, min_value=1, max_value=20, unit="passi"),
    S("workflow.tool_time_budget_s", "workflow", "Tempo massimo di indagine",
      "Tetto complessivo di tempo per il lavoro con gli strumenti.",
      help_text="Scaduto il tempo, l'agente chiude con quello che ha trovato "
                "invece di continuare. È ciò che tiene prevedibile l'attesa.",
      data_type=INT, default=75, min_value=10, max_value=600, unit="secondi"),
    S("workflow.self_critique", "workflow", "Autocritica prima di concludere",
      "L'agente rilegge le proprie conclusioni cercando le prove che mancano.",
      help_text="Riduce le root cause dichiarate senza evidenza, al prezzo di "
                "qualche token in più per analisi. Il testo della verifica è in "
                "Prompt dell'agente.",
      data_type=BOOL, default=True),

    # ------------------------------------------------------------- Agent Tools
    S("agent_tools.explorable_schemas", "agent_tools", "Schemi esplorabili",
      "Gli schemi in cui l'agente può cercare tabelle.",
      help_text="Uno per riga. Un elenco preciso rende le ricerche molto più "
                "rapide e mirate. Il valore «*» significa «tutto il metastore»: "
                "comodo all'inizio, lento e rumoroso a regime.",
      data_type=LIST, default=["*"]),
    S("agent_tools.allowed_workspace_paths", "agent_tools", "Percorsi consentiti",
      "I rami del workspace da cui si possono leggere notebook.",
      help_text="Uno per riga, confronto sul prefisso del percorso. Vale sia per "
                "l'agente sia per l'importazione di notebook nella "
                "documentazione. «*» toglie ogni restrizione.",
      data_type=LIST, default=["*"]),
    S("agent_tools.etl_roots", "agent_tools", "Radici degli ETL",
      "Da dove parte la ricerca dell'ETL che alimenta una tabella.",
      help_text="Uno per riga. Indicare le cartelle giuste è la differenza fra "
                "trovare l'ETL in un secondo e scandire mezzo workspace.",
      data_type=LIST, default=["*"]),
    S("agent_tools.max_scan_schemas", "agent_tools", "Schemi per scansione",
      "Quanti schemi al massimo attraversare in una ricerca di tabelle.",
      help_text="Serve quando gli schemi esplorabili sono «*»: senza questo tetto "
                "una ricerca potrebbe girare per minuti.",
      data_type=INT, default=30, min_value=1, max_value=1000, unit="schemi"),
    S("agent_tools.max_scan_objects", "agent_tools", "Oggetti per scansione",
      "Quanti oggetti del workspace attraversare cercando un ETL.",
      help_text="Stesso scopo del tetto sugli schemi, applicato ai notebook.",
      data_type=INT, default=4000, min_value=10, max_value=100000, unit="oggetti"),
    S("agent_tools.tool_call_timeout_s", "agent_tools", "Timeout per strumento",
      "Quanto può durare una singola chiamata a uno strumento.",
      help_text="Scaduto, la chiamata viene abbandonata e l'agente prosegue con il "
                "resto invece di bloccarsi.",
      data_type=INT, default=30, min_value=5, max_value=300, unit="secondi"),
    S("agent_tools.forbidden_sql", "agent_tools", "Comandi SQL vietati",
      "Parole chiave che rendono una query non eseguibile dall'agente.",
      help_text="Una per riga. È la garanzia che l'agente resti in sola lettura: "
                "toglierne una gli permette di modificare i dati. Non farlo.",
      data_type=LIST, default=["DELETE", "UPDATE", "MERGE", "INSERT", "DROP",
                               "ALTER", "CREATE", "TRUNCATE", "GRANT", "REVOKE"]),

    # ---------------------------------------------------------------- Tassonomia
    S("taxonomy.problem_types", "taxonomy", "Tipi di problema",
      "Le categorie fra cui l'analisi deve classificare il problema.",
      help_text="Una per riga. Il modello è obbligato a sceglierne una: aggiungerne "
                "di nuove cambia le statistiche, quindi conviene farlo di rado e "
                "con nomi stabili.",
      data_type=LIST, default=["DATA_QUALITY", "PIPELINE_FAILURE",
                               "ACCESS_REQUEST", "CONFIGURATION", "PERFORMANCE",
                               "OTHER"]),
    S("taxonomy.severities", "taxonomy", "Livelli di gravità",
      "I livelli di gravità ammessi in un'analisi.",
      help_text="Una per riga, dal più grave al meno grave.",
      data_type=LIST, default=["HIGH", "MEDIUM", "LOW"]),

    # ------------------------------------------------------------------ Prompt
    S("prompts.system_role", "prompts", "Istruzioni permanenti dell'agente",
      "Chi è l'agente, quali strumenti ha e come deve ragionare.",
      help_text="È il testo che precede OGNI analisi e OGNI risposta: è la leva "
                "più potente e anche la più rischiosa di tutto il pannello. "
                "Toglierne pezzi (per esempio l'obbligo di cercare il nome esatto "
                "di una tabella prima di usarla) peggiora le analisi in modo "
                "silenzioso. Modificalo su una copia e confronta i risultati.",
      data_type=TEXT, min_value=50,
      default=("Sei un Senior Data Engineer AMS Lead su Databricks/Azure Datalake. "
               "Hai tool in SOLA LETTURA: search_tables, list_tables_in_schema, "
               "run_sql_query, describe_table, get_table_details, "
               "find_etl_for_table, read_notebook, check_recent_job_runs. Se non "
               "sei sicuro del nome esatto di una tabella, usa PRIMA "
               "search_tables: non indovinare. Sui problemi dati NON fermarti al "
               "sintomo: investiga la pipeline, leggi l'ETL, trova il punto esatto "
               "(join che duplica, filtro mancante, cast sbagliato). La root cause "
               "dev'essere concreta e verificabile. Dichiara una confidence onesta "
               "e spiega il ragionamento. Rispondi in italiano, tecnico e "
               "operativo.")),
    S("prompts.self_critique", "prompts", "Testo dell'autocritica",
      "La domanda che l'agente si pone prima di chiudere un'analisi.",
      help_text="Usato solo se l'autocritica è attiva (categoria Workflow).",
      data_type=TEXT, min_value=20,
      default=("Prima di concludere: hai EVIDENZA concreta (query o riga di ETL) "
               "per la root cause? Hai distinto sintomo da causa? Se manca "
               "evidenza, continua a investigare.")),

    # -------------------------------------------------------------------- Team
    S("team.members", "team", "Componenti del team",
      "Le persone a cui si possono assegnare i ticket.",
      help_text="Una per riga, nome e cognome come devono comparire nei menu e "
                "nelle etichette colorate della Dashboard.",
      data_type=LIST, default=[]),

    # ---------------------------------------------------------------- Allegati
    S("attachments.max_text_chars", "attachments", "Testo massimo per allegato",
      "Quanti caratteri tenere di ogni allegato testuale.",
      help_text="Oltre questa soglia l'allegato viene troncato con un avviso. "
                "Alzarlo dà al modello più contesto e consuma più token.",
      data_type=INT, default=8000, min_value=500, max_value=200000,
      unit="caratteri"),
    S("attachments.max_image_bytes", "attachments", "Peso massimo per immagine",
      "Oltre questa dimensione uno screenshot viene scartato.",
      help_text="Le immagini viaggiano codificate nel prompt: una troppo grande "
                "fa fallire la chiamata al modello. 4 MB è un buon compromesso "
                "per uno screenshot a schermo intero.",
      data_type=INT, default=4000000, min_value=100000, max_value=20000000,
      unit="byte"),
    S("attachments.excel_max_rows", "attachments", "Righe da un Excel allegato",
      "Quante righe leggere da un foglio allegato a un ticket.",
      help_text="Non più letta dal codice: l'estrattore unico legge fino a 1000 "
                "righe per foglio. Resta per compatibilità.",
      data_type=INT, default=200, min_value=1, max_value=100000, unit="righe"),

    # ---------------------------------------------------------------- Branding
    S("branding.header_title", "branding", "Titolo dell'intestazione",
      "Il titolo mostrato in alto a sinistra e nella scheda del browser.",
      data_type=STRING, default="AMS Ticket Assistant", applies=RELOAD),
    S("branding.header_subtitle", "branding", "Sottotitolo dell'intestazione",
      "La riga sotto il titolo: di norma cliente e ambiente.",
      data_type=STRING, default="", applies=RELOAD),
    S("branding.accent_color", "branding", "Colore d'accento",
      "Il colore usato per identità, stati di lavorazione e messa a fuoco.",
      help_text="Esadecimale, es. #EC008C. Entra nel tema e nel CSS generati "
                "all'avvio della pagina: dopo il salvataggio serve ricaricare.",
      data_type=COLOR, default="#EC008C", applies=RELOAD),
)

BY_KEY = {s.key: s for s in SETTINGS}
CATEGORY_BY_KEY = {c.key: c for c in CATEGORIES}


def settings_of(category: str) -> list:
    return [s for s in SETTINGS if s.category == category]


def default_config() -> dict:
    """I default applicativi come dizionario piatto chiave -> valore."""
    return {s.key: s.default for s in SETTINGS}
