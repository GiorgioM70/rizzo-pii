# -*- coding: utf-8 -*-
"""
Rete REGEX + CHECKSUM che affianca il modello (EMAIL, TELEFONO, IBAN, CF, PIVA,
carta di credito, importo, targa, URL, indirizzo IP).

Come `pdf_export`, questo modulo lavora solo su stringhe: nessun import di
torch/transformers/fitz, quindi e' testabile in isolamento e senza il modello.
`app.py` ne ri-esporta i nomi, il comportamento pubblico non cambia.

Le regex accettano i separatori con cui gli identificativi sono STAMPATI nei
documenti, non solo la forma compatta: un IBAN su una fattura o su una carta
intestata e' raggruppato a quattro, una carta di credito e' separata da spazi,
trattini o punti, un telefono da spazi, punti o trattini. I validatori
normalizzano gia' i separatori: se la regex non gliene passa mai uno il
checksum non viene nemmeno interrogato e, dove `strict=True`, il valore resta
in chiaro senza alcun fallback.
"""

import bisect
import re

import words_it


def iban_ok(s):
    # Si normalizzano gli stessi separatori che la regex ammette: spazio (anche
    # non-breaking, e gli a-capo del testo estratto da un PDF), punto, trattino.
    s = re.sub(r"[\s.\-]", "", s).upper()
    if not (15 <= len(s) <= 34):
        return False
    r = s[4:] + s[:4]
    try:
        n = int("".join(str(ord(c) - 55) if c.isalpha() else c for c in r))
    except ValueError:
        return False
    return n % 97 == 1


def piva_ok(p):
    p = re.sub(r"\D", "", p)
    if len(p) != 11:
        return False
    t = 0
    for i, c in enumerate(map(int, p[:10])):
        if i % 2 == 0:
            t += c
        else:
            x = c * 2
            t += x - 9 if x > 9 else x
    return (10 - t % 10) % 10 == int(p[10])


_CF_ODD = {"0": 1, "1": 0, "2": 5, "3": 7, "4": 9, "5": 13, "6": 15, "7": 17, "8": 19,
           "9": 21, "A": 1, "B": 0, "C": 5, "D": 7, "E": 9, "F": 13, "G": 15, "H": 17,
           "I": 19, "J": 21, "K": 2, "L": 4, "M": 18, "N": 20, "O": 11, "P": 3, "Q": 6,
           "R": 8, "S": 12, "T": 14, "U": 16, "V": 10, "W": 22, "X": 25, "Y": 24, "Z": 23}


def cf_ok(c):
    c = c.strip().upper()
    if len(c) != 16 or not c.isalnum():
        return False
    b = c[:15]
    try:
        t = sum((_CF_ODD[ch] if i % 2 == 0
                 else (int(ch) if ch.isdigit() else ord(ch) - 65))
                for i, ch in enumerate(b))
    except KeyError:
        return False
    return chr(65 + t % 26) == c[15]


def luhn_ok(s):
    d = re.sub(r"\D", "", s)
    if not (13 <= len(d) <= 19):
        return False
    tot, alt = 0, False
    for ch in reversed(d):
        n = int(ch)
        if alt:
            n *= 2
            if n > 9:
                n -= 9
        tot += n
        alt = not alt
    return tot % 10 == 0


# Ogni detector: (label, regex, validatore-o-None, strict).
#   validatore None  -> match accettato sulla sola forma (validated=False).
#   strict=True      -> il match si scarta se il checksum FALLISCE (forma troppo generica:
#                       IBAN/PIVA/carta -> servono i numeri giusti per non avere falsi positivi).
#   strict=False     -> si redige comunque (forma molto specifica, es. CF: meglio nascondere);
#                       validated=True solo se il checksum passa (mette il ✓).
_PHONE_SEP = r"[ \t .\-]"
_PHONE_RX = re.compile(
    r"(?<![\w.+(])(?:"
    # internazionale: +39 / 0039 / (+39) / (39), poi il numero (prefisso di zona
    # facoltativo tra parentesi, gruppi da 1 a 4 cifre)
    r"(?P<intl>(?:\+|00)\d{1,3}|\(\+?\d{1,3}\))" + _PHONE_SEP + r"?"
    r"(?P<rest>(?:\(\d{1,4}\)" + _PHONE_SEP + r"?)?\d{1,4}(?:" + _PHONE_SEP + r"?\d{1,4}){1,5})"
    # nazionale italiano: cellulare 3xx o fisso 0x.. (anche "(011)")
    r"|(?P<nat>(?:3\d{2}|\(0\d{1,3}\)|0\d{1,3})(?:" + _PHONE_SEP + r"?\d{2,5}){1,4})"
    r")(?!\w)(?![.\-]\d)")

# una data scritta con punti o trattini ("01.02.2024", "03-04-25") ha la stessa
# forma di un fisso a gruppi: va esclusa esplicitamente
_PHONE_DATE = re.compile(r"\(?\d{1,2}\)?[ .\-]\d{1,2}[ .\-]\d{2,4}")


def _digits(s):
    return sum(c.isdigit() for c in s)


def _it_national_ok(num):
    """Numero nazionale italiano: cellulare 3xx di 9-10 cifre, fisso 0x di 6-11."""
    d = "".join(c for c in num if c.isdigit())
    if d.startswith("3"):
        return 9 <= len(d) <= 10
    if d.startswith("0"):
        return 6 <= len(d) <= 11
    return False


def _phone_shape_ok(m):
    """Filtro sul match di _PHONE_RX: conta le cifre (la regex non sa contarle) e
    scarta le date. Non e' un checksum: il candidato resta non validato."""
    if m.group("intl"):
        cc = "".join(c for c in m.group("intl") if c.isdigit())
        rest = m.group("rest")
        if cc == "39":
            return _it_national_ok(rest)
        n = _digits(rest)
        return 6 <= n <= 12 and len(cc) + n <= 15
    nat = m.group("nat")
    if _PHONE_DATE.fullmatch(nat):
        return False
    return _it_national_ok(nat)


_SHORT_DATE_RX = re.compile(r"(?<![\d/.\-])(?:0?[1-9]|[12]\d|3[01])[/\-]"
                            r"(?:0?[1-9]|1[0-2])[/\-]\d{2}(?![\d/.\-]?\d)")

DETECTORS = [
    ("EMAIL",
     re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
     None, True),
    ("CF",
     re.compile(r"\b[A-Za-z]{6}\d{2}[A-Za-z]\d{2}[A-Za-z]\d{3}[A-Za-z]\b"),
     cf_ok, False),
    # CF OMOCODICO: quando due contribuenti collidono, l'Agenzia sostituisce le
    # cifre da destra con una lettera (0->L 1->M 2->N 3->P 4->Q 5->R 6->S 7->T
    # 8->U 9->V), quindi la forma sopra non lo trova. Voce separata e non classe
    # allargata sulla precedente: qui la forma da sola e' troppo generica (sette
    # posizioni che accettano lettere), percio' strict=True e il checksum
    # diventa obbligatorio. `cf_ok` gia' calcola l'omocodia. La forma canonica
    # matcha entrambe le voci, ma sullo stesso span: `_merge` tiene il candidato
    # validato e scarta il duplicato.
    ("CF",
     re.compile(r"\b[A-Za-z]{6}[\dLMNPQRSTUVlmnpqrstuv]{2}[A-Za-z]"
                r"[\dLMNPQRSTUVlmnpqrstuv]{2}[A-Za-z]"
                r"[\dLMNPQRSTUVlmnpqrstuv]{3}[A-Za-z]\b"),
     cf_ok, True),
    # IBAN, forma compatta: copre qualsiasi paese, anche fuori dal registro ISO.
    # Il formato di stampa a gruppi NON e' una regex ma `detect_iban()` qui sotto,
    # perche' per sapere dove finisce serve la lunghezza prevista per il paese.
    ("IBAN",
     re.compile(r"\b[A-Za-z]{2}\d{2}[A-Za-z0-9]{11,30}\b"),
     iban_ok, True),
    # Carta: al gruppo dei separatori si aggiunge il punto ("4111.1111...").
    # Qui NON si usa \s: a differenza dell'IBAN il vincolo e' il solo Luhn (una
    # sequenza qualunque lo supera una volta su dieci) e con l'a-capo una
    # colonna di numeri in tabella diventerebbe un candidato.
    # Il match ora finisce per forza su una CIFRA: con il separatore in coda
    # ("(?:\d[ .\-]?){13,19}") il punto che chiude la frase entrerebbe nello
    # span e finirebbe dentro il placeholder.
    ("CREDITCARDNUMBER",
     re.compile(r"(?<!\d)\d(?:[ .\-]?\d){12,18}(?!\d)"),
     luhn_ok, True),
    ("PIVA",
     re.compile(r"(?<!\d)\d{11}(?!\d)"),
     piva_ok, True),
    # Telefono: forma e confini qui, conteggio delle cifre in _phone_shape_ok().
    # Copre: cellulare e fisso italiani in qualunque raggruppamento ("333 12 34 567",
    # "02 1234 5678", "011 123 45 67"), prefisso tra parentesi ("(011) 1234567",
    # "(+39) 333 1234567"), prefisso internazionale con + o 00 ("+41 79 123 45 67",
    # "0044 20 7946 0958", "+1 (555) 123-4567"). Separatori: spazio, tab, spazio
    # unificatore, punto, trattino; mai l'a-capo (una colonna di numeri in tabella
    # non e' un telefono). Il "/" resta fuori: e' il separatore delle date.
    ("TELEPHONENUM", _PHONE_RX, None, True),
    # Importo. Due vincoli, entrambi perche' lo span sbagliato e' peggio di un
    # mancato match: i centesimi restano in chiaro accanto al placeholder, oppure
    # una parola vicina ci finisce dentro.
    #   - La valuta e' una parola intera. Senza, "euro" combacia dentro "neuro"
    #     ("neuro 1500" diventava un importo) e "EUR" dentro "europeo"
    #     ("12500,00 europeo" si mangiava anche la parola: l'allargamento ai
    #     confini di parola prosegue sulle lettere). Una cifra puo' stare
    #     attaccata ("EUR1500"): il confine vieta solo un'altra lettera.
    #   - Piu' di tre cifre senza separatore di migliaia. "euro 1500,00" si
    #     fermava a "euro 1500": la virgola non e' un carattere di parola, quindi
    #     l'allargamento non raggiunge i centesimi e ",00" resta in chiaro.
    #     I gruppi (punto o spazio, "€ 12.500,00" / "€ 1 250,00") restano.
    ("AMOUNT",
     re.compile(
         r"(?:€|(?<!\w)(?:EUR|euro)(?![A-Za-z]))\s?"
         r"\d{1,3}(?:[.\s]\d{3})+(?:,\d{2})?"
         r"|(?:€|(?<!\w)(?:EUR|euro)(?![A-Za-z]))\s?"
         r"\d+(?:,\d{2})?"
         r"|(?<!\d)\d{1,3}(?:\.\d{3})+(?:,\d{2})\s?"
         r"(?:€|(?:EUR|euro)(?![A-Za-z]))"
         r"|(?<!\d)\d+(?:,\d{2})\s?"
         r"(?:€|(?:EUR|euro)(?![A-Za-z]))",
         re.IGNORECASE,
     ),
     None, True),
    # il trattino e' un separatore quanto lo spazio ("AB-123-CD" nei moduli e negli
    # export): senza, la targa scritta cosi' non viene vista da nessuno dei due lati,
    # perche' nemmeno il modello la riconosce, e resta in chiaro.
    ("TARGA",
     re.compile(r"\b[A-Za-z]{2}[\s-]?\d{3}[\s-]?[A-Za-z]{2}\b"),
     None, True),
    # URL: tre forme, dalla piu' esplicita alla piu' rischiosa.
    #   1) con schema (http/https/ftp)     -> sempre un URL
    #   2) www.<dominio>                   -> sempre un URL
    #   3) dominio nudo, ma SOLO con un TLD della lista chiusa qui sotto.
    # Il dominio nudo generico (r"\w+\.\w{2,}") non si puo' usare: nel legalese
    # italiano prenderebbe "p.iva", "n.ro", "S.r.l." e simili. Con la lista chiusa
    # "p.iva" non matcha ("iva" non e' un TLD) e i falsi positivi crollano.
    # Prezzo del compromesso: un dominio con TLD esotico resta in chiaro.
    ("URL",
     re.compile(r"(?:https?|ftp)://[^\s<>\"']+"
                r"|www\.[A-Za-z0-9\-._~%]+\.[A-Za-z]{2,}(?:/[^\s<>\"']*)?"
                r"|\b(?:[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?\.)+"
                r"(?:it|com|net|org|eu|info|io|dev|app|gov|edu|cloud|online|site|blog)"
                r"\b(?:/[^\s<>\"']*)?", re.IGNORECASE),
     None, True),
    # IPADDR: quattro ottetti 0-255 separati da punto. Il vincolo sull'ottetto (non
    # un \d{1,3} generico) e' cio' che tiene fuori le versioni software tipo "2.3.55.987":
    # 987 non e' un ottetto valido, quindi l'intera stringa non matcha come IP.
    # Copre anche le due notazioni con cui un IP compare in un log/config:
    #   - CIDR:   192.168.1.0/24        (prefisso 0-32)
    #   - range:  192.168.1.1-192.168.1.20
    # Resta un limite noto (non risolvibile a regex, lo segnala anche l'issue): una
    # versione software con tutti gli ottetti per caso <= 255 (es. "1.2.3.4") e'
    # indistinguibile da un IP vero. Nessun falso negativo pero' sulle versioni con
    # un numero fuori range, che sono la stragrande maggioranza dei casi reali.
    ("IPADDR",
     re.compile(r"\b(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
                r"(?:\.(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}"
                r"(?:/(?:3[0-2]|[12]?\d))?"
                r"(?:\s*-\s*(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
                r"(?:\.(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3})?"
                r"\b"),
     None, True),
    # DOCID: il codice di un atto e' scritto sempre dopo la sua sigla ("R.G. 1234/2024",
    # "Prot. 123/2024", "Rep. 45"). E' la sigla a renderlo riconoscibile: il numero da
    # solo ("1234/2024") non si distingue da una frazione o da un articolo di legge,
    # quindi la si pretende sempre e la si include nello span.
    # Il "n." fra la sigla e il numero e' opzionale e vale per TUTTE le sigle: si scrive
    # "Prot. 456/2024" quanto "Prot. n. 456/2024", e negli atti la seconda e' la forma
    # piu' frequente. Tenendolo attaccato alla sola parola "protocollo" - com'era - la
    # forma abbreviata piu' comune restava in chiaro.
    ("DOCID",
     re.compile(r"\b(?:R\.?G\.?\s*N\.?R\.?|R\.?G\.?|RG|Prot\.?|protocollo"
                r"|Rep\.?|repertorio)"
                r"(?:\s*(?:n\.?|num\.?|nro\.?))?"
                r"\s*\d{1,8}(?:[/\-]\d{2,4})?\b",
                re.IGNORECASE),
     None, True),
    # Date numeriche: forma specifica ma SENZA validatore possibile (una data non ha
    # checksum). Sta in SOFT_REGEX_LABELS -> in fusione non eredita la priorita' della
    # rete regex, quindi il modello puo' sovrascriverla: "06-11-2014" dentro un
    # riferimento di laboratorio resta un falso positivo accettabile, non un verdetto.
    ("DATE",
     re.compile(r"(?<!\d)(?:0?[1-9]|[12]\d|3[01])[/.\-]"
                r"(?:0?[1-9]|1[0-2])[/.\-](?:19|20)\d{2}"
                r"(?:\s+\d{1,2}[:.]\d{2})?(?!\d)"),
     None, True),
    # Data con anno a DUE cifre ("1/3/24", "05-06-25"). Il modello ne vede solo la coda
    # ("3/24") e lasciava "1/" in chiaro accanto al placeholder. Solo "/" e "-":
    # col punto "1.2.24" e' anche una numerazione di paragrafo.
    ("DATE", _SHORT_DATE_RX, None, True),
    # CODICE numerico lungo dopo la sua parola ("pratica 20240001234567", "n. cliente
    # 4455667788", "polizza n. 123456"): e' un identificativo di rapporto, per la
    # tassonomia un DOCID (CIG/CUP/POLIZZA/MATRICOLA -> DOCID). Prima restava in chiaro
    # o il modello lo etichettava IBAN. L'ancora e' obbligatoria: sei cifre da sole
    # sono un CAP, un importo, un anno e mezzo.
    ("DOCID",
     re.compile(r"(?i:\b(?:pratica|contratto|codice|cliente|polizza|matricola|fascicolo"
                r"|ordine|fattura|ticket|tessera|utenza|rapporto|pos(?:izione)?"
                r"|n\.?|nr\.?|num\.?|numero)"
                r"(?:[ \t]+(?:cliente|pratica|contratto|polizza|fascicolo|ordine|fattura"
                r"|utenza|tessera|di[ \t]+\w+))?"
                r"[ \t]*(?:n\.?|nr\.?|n°|nº|num\.?|numero)?[ \t]*[:#]?[ \t]*)"
                r"(?<![\d/])(\d{6,})(?![\d/.,\-]?\d)"),
     None, True),
]

# Label della rete regex SENZA validatore forte: la forma da sola non basta a dire
# "e' certamente questo campo". In _merge non ereditano la priorita' della rete regex,
# cosi' il modello puo' sovrascriverle.
SOFT_REGEX_LABELS = {"DATE", "FULLNAME"}

# Punteggiatura che chiude la frase e non fa parte dell'URL: "vedi https://x.it/pagina."
_URL_TRAIL = ".,;:!?)]}»\"'"


# ISO 13616: lunghezza dell'IBAN per paese. Serve a sapere DOVE finisce quando e'
# scritto a gruppi: indovinare il confine significa inghiottire le parole vicine o
# fermarsi a meta' lasciando la coda dell'IBAN in chiaro.
IBAN_LEN = {c[:2]: int(c[2:]) for c in (
    "AD24 AE23 AL28 AT20 AZ28 BA20 BE16 BG22 BH22 BI27 BR29 BY28 CH21 CR22 CY28 CZ24 "
    "DE22 DJ27 DK18 DO28 EE20 EG29 ES24 FI18 FK18 FO18 FR27 GB22 GE22 GI23 GL18 GR27 "
    "GT28 HN28 HR21 HU28 IE22 IL23 IQ23 IS26 IT27 JO30 KW30 KZ20 LB28 LC32 LI21 LT20 "
    "LU20 LV21 LY25 MC27 MD24 ME22 MK19 MN20 MR27 MT31 MU30 NI28 NL18 NO15 OM23 PK24 "
    "PL28 PS29 PT25 QA29 RO24 RS22 RU33 SA24 SC31 SD18 SE24 SI19 SK24 SM27 SO23 ST25 "
    "SV28 TL23 TN24 TR26 UA29 VA22 VG24 XK20 YE30").split()}

# sigla e cifre di controllo attaccate: sono sempre stampate insieme, e pretenderlo
# evita che "il 17 luglio 2008" o "RO 48 2897 ..." si aggancino come IBAN.
_IBAN_HEAD = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{2}\d{2}")
_IBAN_MAX_SEP = 3                      # separatori di fila (PDF giustificati, tabelle)
_IBAN_RUN = 5                          # caratteri per gruppo: piu' lunghi sono parole


def _iban_char(c):
    """ASCII: senza isascii() la scansione inghiotte "eta'", "piu'", ..."""
    return c.isascii() and c.isalnum()


def detect_iban(text):
    """IBAN nel formato di stampa a gruppi ("IT60 X054 2811 ...").

    Dalla sigla del paese consuma esattamente i caratteri previsti da IBAN_LEN,
    tollerando i separatori; poi decide il mod-97.

    Uno scanner e non una regex perche' il confine destro non e' deducibile dalla
    forma: senza la lunghezza attesa il match o inghiotte la parola dopo, o si ferma
    a meta' e lascia la coda dell'IBAN in chiaro sotto un placeholder rassicurante.
    """
    ents = []
    for m in _IBAN_HEAD.finditer(text):
        n, i = IBAN_LEN.get(m.group()[:2].upper()), m.start()
        if not n or (i and text[i - 1].isalnum()):
            continue
        chars, sep, run, nl, grp, start = [], 0, 0, 0, False, i
        while i < len(text) and len(chars) < n:
            c = text[i]
            if _iban_char(c):
                chars.append(c)
                run += 1
                sep = 0
            elif sep < _IBAN_MAX_SEP and run <= _IBAN_RUN and (c in ".-" or c.isspace()):
                sep, run, grp = sep + 1, 0, True
                nl += c.isspace() and c not in " \t "
                if nl > 1:
                    break              # un IBAN va a capo una volta; una colonna a ogni cella
            else:
                break
            i += 1
        if len(chars) < n or (i < len(text) and _iban_char(text[i])):
            continue                    # troncato, oppure il codice prosegue oltre
        if grp:
            g = [x for x in re.split(r"[\s.\-]+", text[start:i]) if x]
            if len(set(map(len, g[:-1]))) > 1 or len(g[-1]) > len(g[0]):
                continue                # gruppi disuguali: e' prosa, non un IBAN stampato
        if iban_ok(text[start:i]):
            ents.append({"label": "IBAN", "start": start, "end": i,
                         "score": 1.0, "validated": True, "source": "regex"})
    # due candidati sovrapposti: uno e' l'artefatto di una scansione partita da un codice
    # vicino, ma quale dei due non e' decidibile dal testo. Si maschera l'unione, perche'
    # scartarne uno lascia in chiaro un pezzo dell'altro sotto un [IBAN_1] rassicurante.
    ents.sort(key=lambda e: e["start"])
    for a, b in zip(ents, ents[1:]):
        if b["start"] < a["end"]:
            b["start"], b["end"] = a["start"], max(a["end"], b["end"])
            a["end"] = a["start"]       # assorbito in b
    return [e for e in ents if e["end"] > e["start"]]


# CF nel formato di STAMPA: le voci CF di DETECTORS vedono solo i sedici caratteri
# attaccati, ma su tesserini, moduli e intestazioni il codice e' scritto a gruppi
# ("RSS MRA 85H12 F205Y", "RSSMRA.85H12.F205Y"), a lettere spaziate, o spezzato da
# un a-capo del testo estratto. Il modello non compensa: su queste forme resta in
# chiaro, per intero o in parte accanto a un [CF_1] rassicurante.
#
# Ogni carattere ha la sua classe (le cifre ammettono le lettere dell'omocodia) e fra
# due caratteri si tollera un separatore: fino a tre spazi (PDF giustificati), un
# punto o un trattino, un a-capo. Tre vincoli tengono fuori la prosa:
#   - i gruppi devono essere quelli con cui il codice si stampa davvero (_CF_GRUPPI);
#     un solo a-capo invece puo' cadere ovunque, e non conta come confine di gruppo;
#   - il checksum e' obbligatorio, come per la voce omocodica: con i separatori la
#     forma da sola e' troppo larga per bastare;
#   - almeno tre delle sette cifre devono essere cifre vere. Le lettere dell'omocodia
#     rendono "furono trattenute" (6 + 10) un CF di forma valida, e il checksum passa
#     una volta su 26: su 470M caratteri di Wikipedia e testo legale sono 4 casi.
#     L'omocodia sostituisce da destra e raramente piu' di una o due cifre.
_CF_SEP = r"(?:[^\S\r\n]{1,3}|[^\S\r\n]?[.\-][^\S\r\n]?|[^\S\r\n]*\r?\n[^\S\r\n]*)?"
_CF_L, _CF_D = r"[A-Za-z]", r"[\dLMNPQRSTUVlmnpqrstuv]"
_CF_STAMPA = re.compile(r"(?<![A-Za-z0-9])" + _CF_SEP.join(
    [_CF_L] * 6 + [_CF_D] * 2 + [_CF_L] + [_CF_D] * 2 + [_CF_L] + [_CF_D] * 3 + [_CF_L])
    + r"(?![A-Za-z0-9])")
# (16,) e' il codice attaccato che va a capo: tolto l'a-capo, un gruppo solo
_CF_GRUPPI = {(16,), (3, 3, 5, 5), (6, 5, 5), (6, 10), (6, 2, 1, 2, 4, 1),
              (3, 3, 2, 1, 2, 4, 1), (1,) * 16}
_CF_CIFRE_VERE = 3


def detect_cf_stampato(text):
    """CF scritto con separatori; la forma compatta resta alle voci di DETECTORS."""
    ents = []
    for m in _CF_STAMPA.finditer(text):
        s = m.group()
        if s.isalnum():
            continue                    # compatto: gia' coperto, con le sue regole
        if s.count("\n") > 1:
            continue                    # un codice va a capo una volta; una colonna a ogni cella
        # l'a-capo non separa un gruppo: si toglie prima di contare i gruppi
        gruppi = tuple(len(g) for g in re.split(r"[^A-Za-z0-9]+", re.sub(r"\s*\n\s*", "", s)))
        if gruppi not in _CF_GRUPPI:
            continue
        codice = re.sub(r"[^A-Za-z0-9]", "", s)
        if sum(c.isdigit() for c in codice) >= _CF_CIFRE_VERE and cf_ok(codice):
            ents.append({"label": "CF", "start": m.start(), "end": m.end(),
                         "score": 1.0, "validated": True, "source": "regex"})
    return ents


# Dalla piu' lunga: le stesse lunghezze che accetta luhn_ok, cosi' un PAN non viene
# tagliato a una lunghezza piu' corta che passa il Luhn per caso (con 17 fuori dalla
# lista, di un PAN di 17 cifre se ne coprivano 16 e l'ultima restava in chiaro).
_CARD_LEN = (19, 18, 17, 16, 15, 14, 13)
_CARD_CODA = 4                     # cifre di coda ammesse: la scadenza, "12/26" o "12 26"


def _card_scadenza(coda):
    """La coda deve somigliare a una scadenza: due-quattro cifre, col mese fra 1 e 12.
    E' il vincolo che tiene bassi i falsi positivi: senza, un numero lungo qualsiasi
    trova per caso un prefisso che passa il Luhn. Una sola cifra non e' mai una
    scadenza, e ammetterla faceva passare 9 code su 10."""
    return 2 <= len(coda) <= 4 and 1 <= int(coda[:2]) <= 12


def _card_senza_scadenza(text, start, end):
    """La scadenza attaccata al numero ("4111 1111 1111 1111 12/26") entra nel match:
    il Luhn fallisce, strict lo scarta e la carta resta IN CHIARO. Qui si riprova
    tagliando la coda, ma solo su un separatore gia' presente e solo se quel che resta
    fuori e' un mese plausibile."""
    s = text[start:end]
    d = [i for i, c in enumerate(s) if c.isdigit()]
    for n in _CARD_LEN:
        if not 0 < len(d) - n <= _CARD_CODA:
            continue
        taglio = d[n - 1] + 1
        if (s[taglio] in " .-" and _card_scadenza(re.sub(r"\D", "", s[taglio:]))
                and luhn_ok(s[:taglio])):
            return start + taglio, True
    return end, False


def detect_regex(text):
    """Entita' della rete regex. validated=True solo quando il checksum passa."""
    ents = detect_iban(text) + detect_cf_stampato(text)
    for label, rx, validator, strict in DETECTORS:
        for m in rx.finditer(text):
            start, end = m.start(), m.end()
            codice = label == "DOCID" and m.re.groups     # solo le cifre del codice
            if codice:
                start, end = m.span(1)
            if label == "TELEPHONENUM" and not _phone_shape_ok(m):
                continue
            if label == "URL":
                while end > start and text[end - 1] in _URL_TRAIL:
                    end -= 1
                if end <= start:
                    continue
            ok = validator(m.group(0)) if validator else False
            if not ok and label == "CREDITCARDNUMBER":
                end, ok = _card_senza_scadenza(text, start, end)
            if validator and strict and not ok:
                continue
            ents.append({
                "label": label,
                "start": start,
                "end": end,
                # il codice ancorato cede a qualunque forma piu' specifica (telefono, IBAN...)
                "score": 1.0 if ok else (0.85 if codice else 0.9),
                "validated": ok,
                "source": "regex",
                "union": label == "AMOUNT" or rx is _SHORT_DATE_RX,
            })
    visti = {(e["label"], e["start"], e["end"]) for e in ents}
    return ents + [e for e in detect_extras(text)
                   if (e["label"], e["start"], e["end"]) not in visti]


# --------------------------------------------------------------------------- #
# Forme che il modello e le regex "a cifre" non coprono
# --------------------------------------------------------------------------- #
# Codice di 12+ cifre senza ancora (nessun IBAN/carta/telefono ha questa forma
# quando il checksum non passa): un identificativo di rapporto -> DOCID.
_LONG_NUMBER = re.compile(r"(?<![\w.,/+\-€])\d{12,}(?![\w.,/\-]?\d)")

# Il firmatario: dopo "F.to", "firmato da", "firma:", "in fede" c'e' quasi sempre
# il nome di chi firma. Il modello di solito lo trova; questa e' la rete per i nomi
# che non conosce (stranieri, rari). Il nome e' scritto con le iniziali maiuscole
# sulla STESSA riga; i titoli ("Dott.", "Avv.") restano fuori dallo span; le parole
# della lista sotto fermano la lettura ("Firma digitale", "Firma del legale...").
_SIG_CUE = re.compile(
    r"(?P<cue>(?i:(?<![\w])(?:f[./][ \t]?(?:to|ta)|firmato(?:[ \t]+da)?|firmata(?:[ \t]+da)?"
    r"|firma(?:[ \t]+leggibile)?|in[ \t]+fede)(?![\w])[ \t]*(?P<sep>[:,\-–]?)[ \t]*))"
    r"(?:(?i:(?:dott(?:\.ssa|ssa|\.)?|dr\.?|avv\.?|ing\.?|arch\.?|geom\.?"
    r"|prof(?:\.ssa|ssa|\.)?|sig(?:\.ra|\.na|ra|\.)?|rag\.?|notaio)[ \t]+))*"
    r"(?P<nome>(?:[A-ZÀ-Ý][A-Za-zÀ-ÿ'’\-]+|[A-Z]\.)"
    r"(?:[ \t]+(?:[A-ZÀ-Ý][A-Za-zÀ-ÿ'’\-]+|[A-Z]\.)){0,3})")
_SIG_STOP = {
    "digitale", "digitalmente", "elettronica", "elettronico", "qualificata", "autografa",
    "leggibile", "illeggibile", "il", "la", "lo", "del", "della", "per", "data", "luogo",
    "timbro", "firma", "firmato", "firmata", "notaio", "legale", "rappresentante",
    "comune", "tribunale", "studio", "societa", "srl", "spa", "avvocato", "giudice",
    "cancelliere", "perito", "testimone", "parte", "parti", "richiedente", "dichiarante",
    "compratore", "venditore", "committente", "appaltatore", "locatore", "conduttore",
    "cedente", "cessionario", "mandante", "mandatario", "pagina", "fine", "presente",
    "documento", "originale", "copia", "conforme", "congiunta", "congiunto",
    # didascalie dei moduli: "Firma Cliente", "Firma Contraente"... non sono nomi
    "cliente", "contraente", "contraenti", "autorizzata", "autorizzato", "responsabile",
    "assicurato", "assicuratore", "beneficiario", "titolare", "delegato", "delegante",
    "incaricato", "fornitore", "proprietario", "inquilino", "acquirente", "mittente",
    "destinatario", "genitore", "tutore", "amministratore", "socio", "direttore",
    "presidente", "garante", "fideiussore", "debitore", "creditore", "ricevente",
    "consegna", "accettazione", "approvazione", "azienda", "ditta", "ente", "ufficio",
    "operatore", "paziente", "medico", "lavoratore", "datore", "dipendente", "utente",
    "richiedente", "sottoscrittore", "committente", "consenso", "informato", "digit",
}


def detect_signers(text):
    ents = []
    for m in _SIG_CUE.finditer(text):
        a, b = m.span("nome")
        words = list(re.finditer(r"\S+", m.group("nome")))
        end = None
        for w in words:
            if re.sub(r"[^a-z]", "", w.group(0).lower()) in _SIG_STOP:
                break
            end = a + w.end()
        if end is not None:
            n_words = len(m.group("nome")[:end - a].split())
            # "Firma Cliente", "Firma Contraente": senza i due punti, una sola parola dopo
            # "firma" e' quasi sempre la didascalia di un modulo, non un nome
            if m.group("cue").lower().startswith("firma") and not m.group("sep") \
                    and n_words < 2:
                continue
            ents.append({"label": "FULLNAME", "start": a, "end": end, "score": 0.8,
                         "validated": False, "source": "regex", "union": True})
    return ents


def detect_extras(text):
    """Numeri/date in lettere, codici lunghi, firmatari."""
    ents = words_it.detect_words(text) + detect_signers(text)
    for m in _LONG_NUMBER.finditer(text):
        ents.append({"label": "DOCID", "start": m.start(), "end": m.end(), "score": 0.8,
                     "validated": False, "source": "regex", "union": False})
    return ents


def extend_same_label(cands):
    """Fonde le span SOVRAPPOSTE dello stesso tipo, quando una e' marcata `union`.

    Il modello e la regex vedono pezzi diversi dello stesso dato ("quindici [maggio]
    millenovecentoottanta", "1/[3/24]"). Nella fusione greedy ne sopravvive una sola
    e il resto restava in chiaro. Qui entrambe diventano l'unione, quindi chiunque
    vinca copre tutto. Si modificano le span sul posto; ritorna la stessa lista.
    Solo sovrapposizioni vere (non adiacenze) e solo tra candidati dello stesso tipo:
    si maschera di piu', mai di meno.

    Un solo passaggio per tipo su candidati ordinati per inizio: O(n log n). Un
    confronto di ognuno con tutti gli altri impiegava 50 s su 20.000 righe di chat
    con una data ciascuna (la stessa trappola che _merge ha gia' evitato)."""
    if not any(e.get("union") for e in cands):
        return cands
    by_label = {}
    for e in cands:
        by_label.setdefault(e["label"], []).append(e)
    for grp in by_label.values():
        if not any(e.get("union") for e in grp) or len(grp) < 2:
            continue
        grp = sorted(grp, key=lambda e: (e["start"], e["end"]))
        cluster, c_end = [grp[0]], grp[0]["end"]
        for e in grp[1:] + [None]:
            if e is not None and e["start"] < c_end:        # sovrapposta al gruppo
                cluster.append(e)
                c_end = max(c_end, e["end"])
                continue
            if len(cluster) > 1 and any(x.get("union") for x in cluster):
                lo = min(x["start"] for x in cluster)
                for x in cluster:
                    x["start"], x["end"] = lo, c_end
            if e is not None:
                cluster, c_end = [e], e["end"]
    return cands


def balance_parens(ents, text):
    """Una span con una parentesi aperta e non chiusa ("euro 15.000,00 (quindicimila/00")
    lascia la chiusa accanto al segnaposto: "[AMOUNT_1])". Se la parentesi mancante e'
    subito fuori dalla span, la si include. Sul posto; ritorna la stessa lista."""
    for e in ents:
        seg = text[e["start"]:e["end"]]
        if seg.count("(") > seg.count(")") and text[e["end"]:e["end"] + 1] == ")":
            e["end"] += 1
        elif seg.count(")") > seg.count("(") and e["start"] and text[e["start"] - 1] == "(":
            e["start"] -= 1
    return ents


# --------------------------------------------------------------------------- #
# Etichette del modello che non reggono alla prova dei fatti
#
# Il modello assegna IBAN a "12345678901234" e BUILDINGNUM a "123456": entita' che
# vanno mascherate comunque, ma col segnaposto sbagliato. "[IBAN_1]" per un numero di
# pratica e' fuorviante per chi legge (e per l'LLM a cui si manda il testo). Qui si
# CAMBIA l'etichetta, non si scarta mai l'entita': l'errore per eccesso e' l'unico
# accettabile in un anonimizzatore. Il codice di un rapporto e' un DOCID
# (CIG/CUP/POLIZZA/MATRICOLA -> DOCID nella tassonomia).
# Va chiamata PRIMA del filtro dei tag esclusi: cosi' un tag disattivato non
# "nasconde" o "resuscita" entita' che hanno cambiato tipo.
# --------------------------------------------------------------------------- #
_CTX_IBAN = re.compile(r"iban|\bc/c\b|c\.c\.|conto|rapporto|banc|filiale", re.I)
_CTX_PIVA = re.compile(r"p\.?\s?iva|partita|\bvat\b|\biva\b|codice\s+fiscale|c\.?f\.?", re.I)
_CTX_CARD = re.compile(r"carta|card|visa|mastercard|amex|bancomat|\bpan\b|circuito", re.I)
_CTX_CODE = re.compile(r"matricola|pratica|contratto|polizza|cliente|fascicolo|ordine|"
                       r"fattura|ticket|tessera|utenza|rapporto|codice", re.I)


def _n_digits(v):
    return sum(c.isdigit() for c in v)


def fix_model_labels(ents, text):
    """Corregge sul posto le etichette del MODELLO palesemente incoerenti col valore.
    Tocca solo le entita' con source == "modello"; ritorna la stessa lista."""
    for e in ents:
        if e.get("source") != "modello":
            continue
        val = text[e["start"]:e["end"]]
        ctx = text[max(0, e["start"] - 30):e["start"]]
        label, nd = e["label"], _n_digits(val)
        # Le tre etichette "a checksum": se il checksum non passa e il valore non ha
        # nemmeno la forma, il contesto deve giustificare l'etichetta; altrimenti e' un
        # codice generico. (Un IBAN valido o una carta valida non arrivano qui.)
        if label == "IBAN":
            if not (iban_ok(val) or re.match(r"[A-Za-z]{2}\d{2}", val.strip())
                    or _CTX_IBAN.search(ctx)) and nd >= 5:
                e["label"] = "DOCID"
        elif label == "PIVA":
            if not (piva_ok(val) or _CTX_PIVA.search(ctx)) and nd >= 5:
                e["label"] = "DOCID"
        elif label == "CREDITCARDNUMBER":
            if not (luhn_ok(val) or _CTX_CARD.search(ctx)) and nd >= 5:
                e["label"] = "DOCID"
        elif label == "BUILDINGNUM":
            # un civico ha al massimo 4-5 cifre ("12/B", "1024"); sei cifre sono un codice
            if nd > 5 or (nd >= 3 and _CTX_CODE.search(ctx)):
                e["label"] = "DOCID"
        elif label == "ID_DOC":
            if _CTX_CODE.search(ctx) and not re.search(r"identit|passaporto|patente|carta", ctx, re.I):
                e["label"] = "DOCID"
    return ents


# ORA: il modello taglia i minuti ("09:50" -> "09:", "18:28" -> "18") e la meta' che
# resta finisce in chiaro accanto al placeholder: "ore 18[TIME_1]28".
# La regex NON gira da sola sul testo: due numeri separati da due punti sono anche una
# scala catastale ("1:25"), un versetto ("Giovanni 3:16"), una coordinata; col punto,
# una versione ("1.30") o un importo ("euro 10.30"). Parte solo DOVE IL MODELLO HA GIA'
# VISTO UN'ORA, e serve a completarne i confini. Il contesto lo porta il modello, la
# regex porta la forma esatta: cosi' si puo' accettare anche il punto ("ore 18.30").
# Guardie: niente cifra, ne' "cifra+separatore", subito prima o subito dopo. Tengono
# fuori i pezzi di "15.03.2026" o di "1.2.30.4", ma lasciano passare l'intervallo col
# trattino attaccato ("9:00-12:30").
_TIME_RX = re.compile(r"(?<!\d)(?<!\d[.:])(?:[01]?\d|2[0-3])[:.][0-5]\d(?:[:.][0-5]\d)?"
                      r"(?!\d)(?![.:]\d)")


def complete_time(ents, text):
    """Estende le span TIME DEL MODELLO all'ora intera che le contiene o le tocca.

    Solo allargamenti: una span del modello non si restringe mai e non ne nasce una
    nuova dove il modello non ha visto un'ora. Un'ora che il modello non trova affatto
    resta quindi al modello (e' il prezzo di non mascherare "Scala 1:25").
    Le entita' vengono modificate sul posto; ritorna la stessa lista."""
    anchors = [e for e in ents if e["label"] == "TIME"]
    if not anchors:
        return ents
    spans = [(m.start(), m.end()) for m in _TIME_RX.finditer(text)]
    starts = [s for s, _ in spans]
    for e in anchors:
        # i match non si sovrappongono: quelli che toccano la span sono gli ultimi che
        # iniziano prima della sua fine, e si trovano risalendo da bisect.
        j = bisect.bisect_left(starts, e["end"]) - 1
        while j >= 0 and spans[j][1] > e["start"]:
            e["start"] = min(e["start"], spans[j][0])
            e["end"] = max(e["end"], spans[j][1])
            j -= 1
    return ents
