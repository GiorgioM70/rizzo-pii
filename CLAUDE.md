# CLAUDE.md

Guida per Claude Code (claude.ai/code) quando lavora in questa repo. Panoramica e struttura
delle cartelle in **[README.md](README.md)**. Documenti di dettaglio:
**[docs/TASSONOMIA_TAG.md](docs/TASSONOMIA_TAG.md)** (i 22 tag),
**[docs/DATASET.md](docs/DATASET.md)** (composizione completa di train/validation) e
**[docs/TRAINING.md](docs/TRAINING.md)** (unione dei due dataset HF, accortezze sui tag,
iperparametri).

## Cos'è questo progetto

Pipeline per addestrare un modello **mmBERT** (`jhu-clsp/mmBERT-base`) a fare **token
classification di PII**, con focus su **testi legali italiani** (atti, contratti, sentenze)
ma con training **multilingue**. Obiettivo finale: anonimizzare documenti **in locale**
prima di mandarli a LLM closed (anonymize → placeholder + dizionario reversibile locale →
API → ricostruzione), per studi legali / compliance GDPR.

Scelta di mmBERT (encoder multilingue, architettura ModernBERT, context nativa 8192) e non
ModernBERT vanilla perché quest'ultimo è quasi solo inglese.

## Ambiente — vincoli critici e non ovvi

GPU **RTX 5060 Ti (Blackwell, sm_120)** su Windows; Python in `D:\programmi\python`.
Questi punti fanno fallire tutto se ignorati:

- **torch DEVE essere build cu128**: `torch 2.11.0+cu128` (da `https://download.pytorch.org/whl/cu128`).
  Le build cpu/cu121 non supportano sm_120 → `torch.cuda.is_available()` False o crash.
- **torchvision/torchaudio vanno disinstallati** se a versioni vecchie: rompono l'import di
  transformers con `operator torchvision::nms does not exist`. Non servono qui.
- **accelerate ≥ 1.14** (transformers 4.57 chiama `unwrap_model(keep_torch_compile=...)`).
- Windows: nel Trainer **`dataloader_num_workers=0`** (altrimenti `RuntimeError ... bootstrapping`).
- **seqeval non si compila** (bug setuptools_scm) → metriche entity-level (P/R/F1) calcolate
  a mano dentro gli script, nessuna dipendenza.
- Dipendenze extra installate: **`wandb`** (tracking) e **`python-dotenv`** (legge `.env`).
- **Log da PowerShell**: redirezioni `>` e `Tee-Object` scrivono **UTF-16** → leggerli con
  `Get-Content`/`python`, NON con lo strumento Read. Gli script forzano `sys.stdout` UTF-8.

## Mappa della repo

Struttura completa in [README.md](README.md). In breve: codice in `src/`, dati in `dataset/`,
modelli in `models/<versione>/`, artefatti dei run in `experiments/<run>/`, doc in `docs/`.
**I path negli script sono assoluti (risolti da `__file__`): girano da qualsiasi CWD.**

**Pipeline dati — `src/data_pipeline/` (ordine di esecuzione):**
- `llm_template_bank.py` — Gemini scrive documenti legali con soli segnaposto `{SLOT}` →
  `dataset/synthetic/legal_templates.json` (72 template). Guard scarta i template con nomi inline.
- `generate_synthetic_pii.py` — inietta nei template dati con **checksum validi** (CF/PIVA/IBAN)
  → `dataset/synthetic/synthetic_pii_it_200k.jsonl` (`tokens` + `bio_labels`).
- `augment_real_pii.py` — inietta entità sintetiche in **frasi reali** Ai4Privacy (it) in posizioni
  variabili → `dataset/synthetic/synthetic_pii_it_realaug.jsonl`. Spezza il legame template/posizione.
- `prepare_deepmount.py` — rimappa `DeepMount00/pii-masking-ita` (56 tipi Faker IT) sui 22 tag →
  `dataset/processed/deepmount_pii_it_{train,test}.jsonl`.
- `build_validation.py` — **unica validation reale** → `dataset/validation/validation_real.jsonl`.
- `build_subset.py` — subset stratificati (multilingua + tag) per smoke test →
  `dataset/subsets/{train_subset_10k,val_subset_5k}.jsonl`.

**Training — `src/training/`:**
- `train_pii.py` — fonde tutte le fonti, addestra, salva il modello, plotta la loss, stampa
  metriche train/val, logga su **W&B** (run `rizzo-pii:0.3B-v{VERSION}`). **Modello versionato**:
  ogni run grande salva in `models/rizzo-pii-0.3B-v{VERSION}/` (storico, niente sovrascrittura;
  `MODEL_VERSION` in cima al file o `--version`; storia in `models/registry.json`). Vedi "Parametri".
- `test_pii.py` — inferenza CLI sul modello salvato (entità + testo anonimizzato). Risolve in automatico
  l'**ultima** versione `rizzo-pii-0.3B-v*` (fallback al vecchio non versionato → legacy; override `PII_MODEL_DIR`).

**Ambiente di test sul Mac:** `~/Work/rizzo-pii-venv/venv` (Python 3.11 con `--system-site-packages` per torch,
+ `pymupdf flask pytest`). Suite: `venv/bin/python -m unittest discover -s tests` (pytest crasha sulla
cattura a chiusura: usare unittest). Baseline: 231 test OK. Le prove sul modello vero: `app.analyze(testo)`.

**Utility / ispezione (read-only) — `src/inspect/`:**
- `validate_checksums.py` — ricalcola i checksum CF/IBAN/PIVA; **blueprint della rete regex+checksum**
  da affiancare al modello in produzione.
- `inspect_ai4privacy.py` (conteggi lingue/tag), `inspect_lengths.py` (lunghezze), `inspect_no_iban.py`.

**App di anonimizzazione locale — `src/app/` (+ packaging in `docs/BUILD.md`):**
- `server_config.py` — **configurazione host/porta** (+ `prefs.json` con tag esclusi e stato del
  dizionario) condivisa tra tutti gli entry point e con Tauri.
  Catena di precedenza: **CLI `--host`/`--port` > env `PII_HOST`/`PII_PORT` > `config.json` > default
  `127.0.0.1:5005`**. Il config.json è in `%LOCALAPPDATA%\rizzo-pii\` (Windows) /
  `~/.local/share/rizzo-pii/` (Linux) / `~/Library/Application Support/rizzo-pii/` (macOS), lo
  stesso file letto/scritto da Tauri (lib.rs). Include `port_available()` (pre-bind check) e il
  codice di uscita `EXIT_PORT_CONFLICT = 76`: i 3 entry point escono con 76 se la porta è occupata
  **prima di caricare il modello** (evita secondi sprecati). Tauri riconosce il codice 76 e mostra
  il form di configurazione nello splash screen.
- `app.py` — server Flask + **UI** (HTML/CSS/JS in `static/`: `index.html`, `app.css`, `app.js`; modello caricato **pigro** con `load_model()`, chiamato dagli entry point prima di `app.run()`, sotto gunicorn con `PII_PRELOAD_MODEL=1`): testo, PDF o file `.md`/`.txt`, chunking con overlap, offset
  globali + dedup. Anonimizzazione **reversibile** (ogni PII → `[FULLNAME_1]`/`[IBAN_1]`… +
  dizionario locale; tab "Ripristina"). Affianca al modello una **rete regex/checksum** (EMAIL/
  TELEFONO/IBAN/CF/PIVA/carta/importo/targa/**URL**; IBAN/CF/PIVA/carta validati con checksum, che ha
  priorità sul modello). `URL` è un **23° tag solo-regex**: il modello non lo conosce; matcha schema,
  `www.` e domini nudi solo su una **lista chiusa di TLD** (senza, `p.iva`/`S.r.l.` diventerebbero
  domini). `APP_VERSION`. Endpoint: `GET /health` (readiness senza inference, 200/503),
  `POST /analyze`, `POST /pdf`, `POST /preview`, `POST /pdf/preview`, `GET /doc/<id>/page/<n>.png`,
  `GET /doc/<id>/file.pdf`, `GET/POST /settings` (alias storico `/tags`), `GET/POST /config`,
  `GET /port-check`; CLI `--host`/`--port`/`--exclude-tags`/`--no-mapping`.
- **Offline sempre**: `app.py` fa `os.environ.setdefault` di `HF_HUB_OFFLINE=1` e
  `TRANSFORMERS_OFFLINE=1` prima di importare transformers (come il Docker; `HF_HUB_OFFLINE=0` per
  riattivare la rete).
- **Protezione del server locale** (`local_guard.py`, issue upstream #112): `Host`/`Origin` non di
  loopback (o dell'host di bind, o in `PII_ALLOWED_HOSTS`) → 403 (DNS rebinding, CSRF). Token di
  sessione per processo (`PII_API_TOKEN` per fissarlo) iniettato nella pagina (`<meta name="pii-token">`):
  la UI lo manda in `X-PII-Token`, `<img>`/download di `/doc/...` in `?t=`. Liberi dal token solo `/`,
  `/static/*`, `/assets/*`, `/health`, `GET /config` (sonda di Tauri). `POST /config` con host non di
  loopback richiede `confirm_expose: true` (409 altrimenti); `/port-check` solo su indirizzi locali.
  `PII_REQUIRE_TOKEN=0` spegne solo il token. I test usano `client()` di `tests/test_ora.py`.
- **Numerazione dei segnaposto per sessione**: campo `session_id` in `/analyze`, `/pdf`,
  `/pdf/preview` → la numerazione continua tra le analisi (`[IBAN_2]` per un IBAN nuovo, `[IBAN_1]` per
  uno gia' visto) e il PDF ha gli stessi segnaposto del testo. Stato in memoria (LRU `MAX_SESSIONS`),
  chiavi HMAC senza valori in chiaro; `POST /session/reset`. Senza `session_id`: comportamento storico.
  La UI genera l'id (sessionStorage), accumula `MAP` e lo rinnova con "Pulisci".
- **Anteprima del PDF a video, prima e dopo** — le due colonne mostrano il documento *renderizzato*,
  non solo il testo. A sinistra, appena si carica un PDF: `POST /preview` lo tiene in memoria e
  ritorna testo + numero di pagine, e la card offre due viste (**Anteprima PDF** default, **Testo**).
  A destra, dopo l'anonimizzazione, una **terza vista** accanto ad Anteprima/Testo: **PDF censurato**,
  cioè lo stesso documento redatto con i placeholder al posto delle PII.
  Il render è **server-side con PyMuPDF** (`page.get_pixmap` → PNG per pagina): dentro Tauri non c'è
  un viewer PDF affidabile (WebView2/WKWebView) e l'app è offline, quindi niente pdf.js da CDN.
  Le pagine sono **pigre** (`loading=lazy`) e messe in cache: un PDF di 200 pagine non costa 200 render.
  I documenti stanno in una **LRU in memoria** (`_DOCS`, `MAX_DOCS=6`) e **mai su disco**: valgono
  quanto il documento stesso e muoiono col processo.
  `POST /pdf/preview` fa lo stesso lavoro di `/pdf` ma lascia il binario nello store e ritorna
  `{doc_id, n_pages, residual, skipped}`: l'anteprima a destra e il bottone "Scarica PDF" usano
  **la stessa copia**, quindi guardare il PDF e poi scaricarlo costa **una sola** anonimizzazione
  (il download passa da `/doc/<id>/file.pdf`). `/pdf` resta invariato per chi usa l'API.
- **Pagine scansionate → OCR (solo macOS)** — `ocr.py` (niente fitz/torch: testabile con finti).
  `plan_pages()` sceglie le pagine da leggere: **vuota** (immagini e < 40 caratteri, soglia della PR
  upstream #105), **illeggibile** (testo che non passa il controllo di qualità: > 10% di glifi senza
  mappa Unicode, < 55% di lettere/cifre, oppure da 20 parole latine in su < 28% di vocali, < 3% di
  parole comuni o > 35% di parole impossibili — soglie e misure commentate in testa al modulo),
  **invisibile** (immagini e ≥ 50% di caratteri in render mode 3 = l'OCR dello scanner sotto
  l'immagine). Il caso tipico dell'atto scansionato: layer dello scanner con caratteri senza senso,
  che prima superava la soglia dei 40 caratteri e passava per testo, senza avviso.
  Motore: **`rizzo-ocr`**, eseguibile Swift col framework **Vision** (`src/app/ocr_macos/main.swift`,
  livello accurato, it-IT + en-US): PNG della pagina (PyMuPDF, 300 dpi) sullo stdin → JSON con righe,
  parole e riquadri normalizzati (origine in alto a sinistra). Un processo per pagina, 4 in parallelo.
  Scelto al posto di pyobjc: nessuna dipendenza Python, niente hook PyInstaller con decine di `.so` da
  firmare, confine PNG/JSON facile da sostituire nei test. `build_macos.sh` lo compila con `swiftc` in
  `build/ocr/`, `build_sidecar.spec` lo mette in `_internal/ocr/` (su macOS è obbligatorio: senza, la
  build si ferma). `ocr.helper_path()` lo cerca in `PII_OCR_HELPER`, nel bundle, in `build/ocr/`.
  `PII_OCR=0` lo spegne. Su Windows/Linux non c'è: nessun errore, solo l'avviso.
  In `app.py` il risultato sta in una **LRU in memoria** per hash del PDF (`_OCR_CACHE`): `/preview`,
  `/analyze`, `/pdf` sullo stesso file fanno l'OCR una volta. L'analisi usa il testo letto
  (`pdf_text.collect_readable_text(page_texts=...)`). In `pdf_export.redact_pdf(ocr_pages=,
  ocr_verify=)` sulle pagine OCR: (0) via il **testo invisibile** dello scanner (anche "rovinato" può
  tenere le cifre di un IBAN), (0b) se resta testo **visibile** illeggibile la pagina diventa
  un'**immagine** a 300 dpi (`_flatten_to_image`, report `flattened`): in quei font (es. Type 3
  dell'atto, ogni carattere largo quanto il corpo) `apply_redactions()` toglierebbe anche le lettere
  accanto alla casella ("numero" → "nume"), sull'immagine sbianca solo i pixel dentro,
  (1) redazione normale del layer, (2) redazione sui **riquadri delle
  parole** dell'OCR con margine (`OCR_PAD`), ma i bordi verso le parole vicine si **allineano all'inchiostro** del dato
  (`_Ink.snap`, render in grigi a 300 dpi): i riquadri di Vision sono larghi e la casella copriva
  l'ultima lettera prima ("Signor" -> "Signo"); anche il bordo da 1 pt che `apply_redactions()`
  disegna attorno alla casella e' compensato (`REDACT_STROKE_HALF`), `apply_redactions()` sbianca i pixel dell'immagine sotto,
  (3) **rilettura con l'OCR** del PDF in uscita: valore ancora leggibile → `residual`; pagina non
  riletta → `ocr_unverified` (avviso nell'UI).
  Risposte: `pages_ocr` (pagine lette), `pages_no_text` (pagine senza testo leggibile **non** lette:
  le PII restano), `ocr_unverified`; header `X-PII-OCR-Pages`, `X-PII-OCR-Unverified`,
  `X-PII-NoText-Pages`. `/health` ha `"ocr": true/false`. Test: `tests/test_ocr.py` (motore finto
  che legge i pixel veri, pagine ruotate comprese) e `tests/macos/test_ocr_vision.py` (Vision vero,
  fuori dalla discovery, lo lancia il workflow `ocr-macos`).
- **Preferenze di anonimizzazione** → `prefs.json` nella config dir (`EXCLUDED_TAGS`,
  `MAPPING_ENABLED`). File **separato da `config.json`** perché Tauri riscrive quest'ultimo per
  intero quando si cambia porta dallo splash. Precedenza: campo nella richiesta > CLI > env > file.
- **Tag disattivabili**: i tag deselezionati vengono rilevati ma **non** sostituiti (restano in
  chiaro) — serve a confrontare gli importi o a tenere età/sesso in un caso clinico. Env
  `PII_EXCLUDE_TAGS`, campo `exclude_tags`. UI: icona 🏷️ nell'header → legenda dei 23 tag con
  toggle. La legenda (descrizioni IT/EN + esempi) vive in `TAGS` in cima a `app.py`.
- **Dizionario reversibile on/off** (`MAPPING_ENABLED`, default **on**; env `PII_MAPPING=0`, campo
  `include_mapping`): con off l'anonimizzazione è **definitiva**. Non è cosmetico — `analyze()` non
  costruisce la mappa, la risposta non ha `mapping`, e i segmenti-entità **perdono il campo `t`**
  (altrimenti il dizionario si ricostruirebbe dal payload); l'UI non scrive `pii_map` in
  sessionStorage (dove ora vive il dizionario: muore con la finestra) e nasconde il download. La **numerazione resta** (`[FULLNAME_1]` due volte = stesso
  soggetto): è utile all'LLM e da sola non riporta al valore. UI: switch nella card di input, ambra
  quando è off. `source_text` continua a tornare — è il documento che hai appena mandato tu, non una
  chiave di ripristino.
- `pdf_export.py` + endpoint **`POST /pdf`** — **download del PDF anonimizzato** (issue #7, punto 1),
  bottone "📄 Scarica PDF anonimizzato" nella card Risultato. Due modalità: se l'input era un **PDF**
  si fa **redazione vera** del documento originale (`apply_redactions()`: il testo esce dal content
  stream, non ci si disegna sopra un rettangolo) con i placeholder al posto delle PII e il layout
  intatto; altrimenti (testo incollato, `.md`/`.txt`) si **ricostruisce** un PDF impaginando da zero
  il solo testo anonimizzato.
  **Il dizionario non viaggia sulla rete in nessuna direzione**: `/pdf` prende lo stesso input di
  `/analyze`, richiama `analyze(..., mapping_enabled=True)` internamente e la mappa muore con la
  richiesta. Per questo il bottone funziona identico anche con il **dizionario reversibile
  disattivato**, dove il client non ne ha nessuno (il costo è una seconda inferenza).
  Matching **char-preciso** con confini di parola (indice per carattere da `rawdict` + regex ancorate
  `(?<!\w)…(?!\w)`), tollerante alla **sillabazione a fine riga** e agli spazi tra token: "DE" non
  viene redatto dentro "CORDELLA". Unica eccezione alle ancore: se il valore ha **punteggiatura ai
  bordi** il modello ha tagliato a metà una parola spezzata a fine riga (su "Fran-\ncesco Cordella"
  etichetta `-\ncesco Cordella`) — lì il confine di parola è proprio ciò che impedirebbe di trovarlo,
  quindi si toglie, ma **solo** se il resto ha ≥ 4 caratteri alfanumerici (altrimenti `.it`
  matcherebbe "it" dentro ogni parola).
  Oltre al testo di pagina ripulisce ciò che `apply_redactions()` **non** tocca e in cui la PII
  sopravviverebbe: metadati + XMP, contenuto delle **annotazioni**, valore dei **campi modulo**,
  titoli dei **segnalibri**; gli **allegati incorporati** vengono rimossi in blocco.
  **Due casi in cui un valore resta in chiaro, entrambi segnalati con un avviso nell'UI** (header
  `X-PII-Residual` e `X-PII-Skipped`): (a) *residui* = valore ancora leggibile nell'output alla
  verifica finale; (b) *saltati* = valori con < 2 caratteri alfanumerici o di 2 sole cifre (es. "45"),
  non cercabili senza devastare il documento. Se in tutto il PDF non si trova **nessuna** occorrenza
  → `422`, non un PDF "anonimizzato" che non lo è (caso tipico: scansione senza OCR, cioè fuori da
  macOS → punti 2-4 della issue). Nessuna dipendenza dal modello: `pdf_export.py` è testabile in isolamento.
- **Numeri e date in lettere, codici lunghi, firmatari** — `words_it.py` (solo stringhe, niente
  torch/fitz) + `detectors.detect_extras()`. Il modello etichettava il solo "maggio" di "quindici
  maggio millenovecentoottanta" e non vedeva "quindicimila/00". Parser dei cardinali italiani
  (composti incollati `duemilaventitre`, `millenovecentoottanta`, forma spaziata, grammatica verificata:
  "cento due due" non e' un numero). **Una parola-numero da sola non e' mai PII** ("tre rate", "sei
  mesi"): serve l'**ancora** — DATE = giorno + mese (+ anno), mese + anno, "anno/nel <anno in lettere>";
  AMOUNT = numero accanto a euro/EUR/€, forma legale `/00`, `3 mila euro`, `2,5 milioni di euro`,
  `quindicimilaeuro`, "euro e N centesimi" (un solo importo); cifre dette una a una (>= 7) = telefono
  (con contesto) o DOCID. Anche: `DATE` con anno a 2 cifre (`1/3/24`: il modello vedeva `3/24` e
  lasciava `1/` in chiaro), **DOCID** per codici numerici dopo la loro parola (`pratica`, `contratto`,
  `cliente`, `polizza`, `matricola`, `n.`... ≥ 6 cifre, solo le cifre nello span; score 0.85 cosi' telefono/
  IBAN vincono) e per numeri nudi di ≥ 12 cifre; **firmatario** dopo `F.to`/`firmato da`/`firma:`/`in fede`
  (`detect_signers`, FULLNAME soft: rete per i nomi che il modello non conosce; lista `_SIG_STOP` per le
  didascalie dei moduli — `Firma Cliente`, `Firma Contraente` non sono nomi — e `firma` senza due punti
  vuole ≥ 2 parole).
  **`extend_same_label()` (in `_merge`)**: due span SOVRAPPOSTE dello stesso tipo, di cui una marcata
  `union` (AMOUNT, `DATE` a 2 cifre, i regex in lettere), diventano l'unione. Senza, la fusione greedy
  teneva una sola e il pezzo dell'altra restava in chiaro (`"[AMOUNT_2] euro"`). `DATE` numerica
  col suo orario NON e' `union`: il modello separa data e `TIME`, e unirle lasciava `[DATE_1]:10`.
  `balance_parens()` chiude la parentesi lasciata fuori dalla span (`[AMOUNT_1])`).
  **Prestazioni**: `extend_same_label` e' un'unica passata per tipo (O(n log n)); la prima versione,
  quadratica, impiegava 50 s su 20.000 righe di chat (la stessa trappola che `_merge` ha gia' evitato con
  bisect). `detect_regex` costa ~2,5x di prima (1,4 s su 1,7 MB) per `words_it`: irrilevante vs l'inferenza.
- **Etichette del modello coerenti col valore** — `detectors.fix_model_labels()` (prima del filtro dei
  tag esclusi): il modello dava `IBAN` a `12345678901234` e `BUILDINGNUM` a `123456`. Si **cambia
  l'etichetta, non si scarta** (errore per eccesso = l'unico accettabile): `IBAN`/`PIVA`/`CREDITCARDNUMBER`
  senza checksum, senza la forma e senza il contesto giusto (`conto`, `p.iva`, `carta`...) → `DOCID`;
  `BUILDINGNUM` con > 5 cifre o dopo "matricola/pratica/contratto..." → `DOCID`; `ID_DOC` dopo
  "cliente/contratto/pratica..." → `DOCID`. Un `conto 12345678` resta `IBAN` (CONTO→IBAN in tassonomia).
  Il ✓ nella legenda `TAGS` vale solo per i match col checksum: il testo ora lo dice ("✓ = checksum verificato").
- **Lettere spaziate nelle intestazioni** (`pdf_text.despace_line` / `raw_lines` / `page_text`). Gli studi
  legali scrivono l'intestazione con la spaziatura fra i caratteri (`A V V .  M A R I O  R O S S I`):
  PyMuPDF da' una lettera per volta, **perde i confini di parola**, il modello non vede il nome e
  l'avvocato restava in chiaro. Le parole si ricostruiscono dalla **geometria** (scarto fra le lettere = Tc;
  fra le parole circa il doppio). Stessa ricostruzione per l'analisi (`page_text`, usata da
  `collect_readable_text`) e per la redazione (`pdf_export._page_char_index`, ora costruito da
  `raw_lines`): cosi' il valore del modello combacia coi caratteri della pagina. **Trappola**: lo scarto da
  solo non distingue una spaziatura da uno spazio vero, quindi si fonde solo una sequenza di ≥ 5 lettere
  isolate che copre ≥ 80% della riga ("e a b c d" dentro una frase resta com'e'); prefiltro
  `looks_letterspaced` sul testo di `get_text()` perche' il caso normale resti identico a prima.
- **Cognome in MAIUSCOLO davanti al nome** (`detectors.extend_caps_surname`, in coda a `_merge`): "CAMPRA
  Teresa, nata a" — un cognome raro il modello lo salta o lo scambia per una citta' (`[CITY_3]`).
  L'ancora e' un FULLNAME seguito da virgola / nato-a / residente / C.F.; la parola in maiuscolo adiacente
  (anche su riga a parte, come nei PDF con una parola per riga) entra nel nome, e se il modello l'aveva
  etichettata CITY diventa parte del nome. Lista `_CAPS_STOP` (RICORRENTE, TRIBUNALE, CONTRO...): senza
  ancora una parola maiuscola non basta. `drop_generic_org`: "STUDIO LEGALE" da solo non e' un ORG.
- **Etichette sulle caselle del PDF on/off** (`redact_pdf(labels=)`, pref `pdf_labels` in `prefs.json`,
  env `PII_PDF_LABELS`, campo `pdf_labels` di `/pdf` e `/pdf/preview`, switch "Etichette sul PDF" nella card
  di input). Spento = solo il rettangolo colorato, senza `[DATE_1]`/`[FIRMA]`. Cambia solo cio' che si vede:
  la rimozione resta vera. Il testo anonimizzato a fianco non cambia. Chiave a parte da `load_prefs()`
  (`server_config.load_pdf_labels/save_pdf_labels`) per non cambiare la forma di quel dizionario.
- **Firme nel PDF** (`pdf_export._redact_signature_shapes` / `_redact_signature_images` /
  `_scrub_signature_data`). Il nome sotto "F.to" lo trova il modello; la firma vera no, perche' non e'
  testo. Si tolgono PER FORMA: campi `/Sig`, annotazioni **Ink** e **Stamp** (sempre); tracciati a mano
  libera (≥ 5 curve, pochi segmenti dritti, non un'ellisse chiusa: i pulsanti radio restano) e
  **immagini** piccole (≤ 15% pagina) solo se vicini a una dicitura di firma **breve** (riga ≤ 40
  caratteri: `Firma:`, `F.to`, `Il Venditore`; una frase come "Il sottoscritto Mario Rossi dichiara..."
  non conta, altrimenti sparisce il logo d'intestazione) e, per le immagini, anche se coprono un nome
  appena redatto. Al loro posto una casella `[FIRMA]` (nessun nuovo tag: non e' una PII del testo).
  **Firma digitale (PAdES)**: cancellare il widget non basta — il nome sta in `/V /Name`, il certificato
  (col codice fiscale `TINIT-...`) in `/Contents`, e restano `/DSS`, `/Perms` e, se il campo e' un
  "figlio", il padre in `/Fields` col suo `/V`. `_scrub_signature_data` azzera `/V`, `/Lock`, `/SV` di
  ogni campo `/FT /Sig`, svuota i dizionari `/Type /Sig` o con `/ByteRange` e toglie `/DSS` e `/Perms`
  (verificato: senza, nome e codice fiscale sopravvivono nel file anche dopo `garbage=3`).
  Report: `signatures`, `signature_data`; header `X-PII-Signatures`; campo `signatures` di `/pdf/preview`;
  toast nella UI. **Trappola**: i tracciati vettoriali escono solo con
  `apply_redactions(graphics=REMOVE_IF_TOUCHED)` (con "se coperto" restano sotto la casella) e quel
  parametro vale per tutta la pagina → due fasi: forme e tracciati si applicano da soli, per primi;
  le immagini vengono dopo i nomi (cosi' i bordi di tabella toccati dalle caselle dei nomi restano).
  **Limite noto**: la firma a mano dentro una pagina **scansionata** e' un pixel fra gli altri (Vision
  non legge la calligrafia): non si distingue in modo affidabile, la UI lo dice.
- `serve.py` — entry **headless** (solo Flask, niente browser): è il backend dell'app Tauri; log su
  `backend.log` nella **cartella dati** di `server_config.config_dir()` (la stessa di `config.json`,
  mai nella home; anche `tauri.log` di lib.rs). Pre-check porta + `sys.exit(76)` se occupata.
  `desktop_app.py` — entry PyInstaller legacy (apre il browser); stesso pre-check.
  `assets/` — mascotte (il riccio) + icone. `smoke_app.py`, `make_test_pdf.py`.

**App desktop Tauri — `tauri/`:** finestra nativa **Rizzo PII** (WebView2) che lancia il backend
`serve.py` impacchettato come **sidecar** (`build_sidecar.spec` → `tauri/src-tauri/backend/`).
All'avvio legge `config.json` (host/porta), passa i valori al sidecar via env `PII_HOST`/`PII_PORT`,
attende il server sulla porta configurata e mostra l'UI. Se il sidecar esce con codice **76** (porta
occupata), lo splash mostra un form di configurazione (host + porta) con "Salva e riprova": Tauri
scrive `config.json`, rilancia il sidecar e riprova. Splash con badge UE/GDPR + versione. `npx tauri build`
→ installer NSIS per-utente. Dettagli e comandi in `docs/BUILD.md`. Comandi Tauri esposti allo splash:
`save_config(host, port)` e `retry_backend`.

**Config:** `.env` (segreti W&B, **gitignorato**), `.gitignore`, `build.spec`/`build_sidecar.spec`/
`installer.iss` + `tauri/` (packaging).
**Artefatti non versionati** (gitignored): `dataset/`, `models/`, `experiments/`, `wandb/`, `build_env/`,
`dist/`, `build/`, `tauri/node_modules/`, `tauri/src-tauri/{target,gen,backend}/`.

## Idea architetturale chiave: "LLM autore, codice etichettatore"

Il cuore della generazione sintetica: **l'LLM scrive solo la prosa con segnaposto, il codice
inietta i dati**. Risolve insieme tre problemi: label BIO esatte (sappiamo dove iniettiamo),
checksum matematicamente validi, nessuna PII reale rigurgitata dall'LLM. **Non far mai scrivere
all'LLM i dati sensibili veri.**

## Tassonomia: 22 tag (dettaglio in TASSONOMIA_TAG.md)

`TAG_MAP` + `DROP_TYPES` + `normalize_labels()` in `train_pii.py` rimappano **al caricamento**
(file grezzi intatti). Per cambiare la tassonomia si edita **solo** `TAG_MAP`/`DROP_TYPES`.

Fusioni: nomi+ruoli legali → `FULLNAME`; `SEX`→`GENDER`; `TAXNUM`→`PIVA`; `PEC`→`EMAIL`;
`RG`→`DOCID`; `IDCARDNUM`/`PASSPORTNUM`/`DRIVERLICENSENUM`/`SOCIALNUM`→`ID_DOC`; `CONTO`→`IBAN`.
Rimossi (→`O`): `TITLE` (appellativo), `TRIBUNAL` (ente pubblico, non PII).
Tag aggiunti via sintetico: `ORG`, `DOCID`, `CATASTO`, `CONTO`(→IBAN), `PROVINCE`.

I 5 tag legali IT-specifici (`CF`, `PIVA`, `CATASTO`, `DOCID`, `PROVINCE`) non esistono come
dato reale da nessuna parte → vengono solo dai sintetici.

## Fonti dati (dettaglio in DATASET.md)

Quattro fonti, tutte ricondotte ai 22 tag al caricamento:
1. **Ai4Privacy** `open-pii-masking-500k` — reale, **8 lingue**, ~464k righe train (it ~55k).
   `hf download ai4privacy/open-pii-masking-500k-ai4privacy`. Ha `mbert_tokens`+`mbert_token_classes`.
2. **Sintetico da template** — `generate_synthetic_pii.py`, 200k righe, copre i tag legali IT.
3. **Augment** — `augment_real_pii.py`, 40k, entità sintetiche in frasi reali it.
4. **DeepMount** `DeepMount00/pii-masking-ita` — Faker IT, 41k; dà contesto reale a IBAN/ORG/AMOUNT/TARGA.

Train pool ≈ **745k righe** (multilingue; italiano rinforzato al ~45%). ~38% sintetico.

## Validation: UNA validation reale unificata

`validation_real.jsonl` (7k righe, **solo italiano**, da `build_validation.py`):
- base reale held-out = Ai4Privacy val (it) + DeepMount test;
- i 5 tag senza dato reale sono **iniettati in frasi reali held-out** (frasi della validation
  Ai4, non nel training) → contesto reale, niente leakage.

Tutti i sintetici e il DeepMount **train** stanno nel pool di training; il DeepMount **test**
è consumato solo nella validation. Scelta: validation italiana perché l'uso reale è il dominio
legale IT (il training resta multilingue; le altre lingue non sono validate).

## Parametri di training importanti (`src/training/train_pii.py`)

- `LANG = None` → **multilingue** (8 lingue Ai4Privacy); `"it"` = solo italiano. Synth/DeepMount
  sempre inclusi (già italiani, rinforzo).
- `MAX_LEN = 768` → copre i sintetici (max 771 subword) e DeepMount (660); con 512 si troncava il
  **33% dei sintetici**. Padding dinamico.
- `BATCH = 16` + `GRAD_ACCUM = 2` (batch **effettivo 32**) → a MAX_LEN 768 su una 16 GB **condivisa
  col desktop**, batch più grandi saturano la VRAM e causano **thrashing** (allocatore CUDA che
  libera/ri-alloca ad ogni batch lungo → 24-37 s/step). Con 16 il picco di attivazioni resta ~8-9 GB.
  Altre mitigazioni: `group_by_length=True` (`LengthGroupedTrainer` usa lunghezze precalcolate per
  non ri-tokenizzare il dataset lazy) e `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`.
  Chiudere le app che usano la GPU (Chrome/WhatsApp/…) libera 1-2 GB.
- `EPOCHS = 1`. L'eval **non** gira durante il training: train loss a ogni step, **metriche P/R/F1
  ALLA FINE** su validation e su un campione di train (`TRAIN_EVAL_N`).
- **Modalità del run** (`--type {full,subset}`, default `full`; anche `PII_SUBSET=1` forza subset):
  `subset` = smoke test / tuning sui subset `dataset/subsets/` (10k/5k), MAX_LEN 256, BATCH 32,
  ~3 min → `experiments/subset_smoke/`. `full` = run grande.
- Output run grande: `models/rizzo-pii-0.3B-v{VERSION}/` (+ tokenizer) e `experiments/full_run_v{VERSION}/`
  (loss + out/) + append a `models/registry.json`. La versione viene da `MODEL_VERSION` o `--version`.

## Weights & Biases

`train_pii.py` carica `.env` con `load_dotenv()`; se c'è `WANDB_API_KEY` attiva
`report_to=["wandb"]` (progetto da `WANDB_PROJECT`, default impostato a `pii-mmbert-it`).
Logga la **train loss live a ogni step** e le **metriche finali** (`final/train_*`, `final/val_*`).
Il `.env` è **gitignorato**: non committarlo. Senza chiave, W&B si disattiva da solo.

## Comandi

Tutti gli script forzano UTF-8 e risolvono i path da soli (girano da qualsiasi CWD; i comandi
sotto si lanciano dalla root per comodità).

```powershell
# 1) (opz.) template legali via Gemini   (richiede GEMINI_API_KEY)
python src/data_pipeline/llm_template_bank.py --per-type 5 --append

# 2) sintetico da template (200k)
python src/data_pipeline/generate_synthetic_pii.py -n 200000 --out dataset/synthetic/synthetic_pii_it_200k.jsonl

# 3) augment in testo reale (40k)
python src/data_pipeline/augment_real_pii.py -n 40000 --out dataset/synthetic/synthetic_pii_it_realaug.jsonl

# 4) DeepMount rimappato 56->22 tag  (richiede login HF: hf auth login)
python src/data_pipeline/prepare_deepmount.py

# 5) validation reale unificata (7k)
python src/data_pipeline/build_validation.py

# 6) subset stratificati per smoke test (10k/5k)
python src/data_pipeline/build_subset.py

# 7a) smoke test / tuning sul subset (~3 min)  -> experiments/subset_smoke/
python src/training/train_pii.py --type subset
# 7b) run grande su tutto  -> models/rizzo-pii-0.3B-v{VERSION}/ + experiments/full_run_v{VERSION}/ + W&B
python src/training/train_pii.py --type full                  # usa MODEL_VERSION nel file
python src/training/train_pii.py --type full --version 1.2.0  # oppure versione esplicita

# inferenza / app
python src/training/test_pii.py "Mi chiamo Mario Rossi, IBAN ..."
python src/app/app.py            # http://127.0.0.1:5005  (PII_PORT per override; 5000 = AirPlay su macOS)

# ispezione read-only
python src/inspect/inspect_ai4privacy.py
python src/inspect/inspect_lengths.py
```

## Limiti noti / aspettative oneste

- **Overfit strutturale**: i tag che vengono solo dai template rischiano di imparare la
  *struttura* invece dell'entità. Mitigazioni in atto: 72 template (non più ~29), augment in
  testo reale, e il contesto vario di DeepMount per IBAN/ORG/AMOUNT/TARGA.
- **Validation solo italiana**: non misura le 7 lingue non-it (scelta voluta).
- **Tag legali IT-only** (`CF`/`PIVA`/`CATASTO`/`DOCID`/`PROVINCE`): in validation sono entità
  generate in frasi reali → buon proxy, non eval completamente cieco. `PROVINCE` nel train ha
  poca diversità di contesto (quasi solo dai template).
- **Sbilanciamento di classe**: FULLNAME ≫ CREDITCARDNUMBER (~66×) → i tag rari sono più rumorosi.
- **Valori off-domain di DeepMount**: nomi/indirizzi USA; utili per forma/contesto, non come
  valori italiani.
- In produzione affiancare **sempre** la rete regex+checksum (`src/inspect/validate_checksums.py`) al modello.
