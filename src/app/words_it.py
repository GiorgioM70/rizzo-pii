# -*- coding: utf-8 -*-
"""
Numeri e date SCRITTI IN LETTERE ("quindicimila/00", "il dodici giugno
duemilaventitre", "tre tre tre uno due tre ...").

Il modello li vede male: di "quindici maggio millenovecentoottanta" etichetta il
solo "maggio", e un importo in lettere non lo etichetta affatto. Come `detectors`,
questo modulo lavora solo su stringhe (niente torch/fitz) ed e' testabile da solo.

REGOLA CHIAVE: una parola-numero da sola NON e' mai una PII ("tre rate", "sei
mesi", "venti giorni"). Si redige solo con un ANCORA di contesto:
  - DATE:   giorno + nome del mese (+ anno), "mese + anno in lettere",
            "anno <anno in lettere>";
  - AMOUNT: numero in lettere accanto a euro/EUR/EUR, oppure la forma legale
            "quindicimila/00";
  - sequenze di >= 7 cifre dette una a una ("tre tre tre uno ...") = numero
            di telefono (con contesto) o codice.

Il numero in lettere si legge con un piccolo parser: ogni parola si scompone in
atomi ("duemilaventitre" = due+mila+venti+tre), e la sequenza deve rispettare la
grammatica dei numeri italiani ("duecento" si', "cento due due" no).
"""

import re
from functools import lru_cache

# --------------------------------------------------------------------------- #
# Atomi lessicali. Tipo -> (valore, [forme]). Le forme "elise" (vent, trent,
# cent...) servono per ventuno, ventotto, centotto, ottantotto.
# --------------------------------------------------------------------------- #
_ATOMS = {}


def _reg(kind, value, *forms):
    for f in forms:
        _ATOMS[f] = (kind, value)


for _v, _f in enumerate(("uno un una", "due", "tre tré", "quattro", "cinque", "sei",
                         "sette", "otto", "nove"), start=1):
    _reg("U", _v, *_f.split())
_reg("Z", 0, "zero")
for _v, _f in ((10, "dieci"), (11, "undici"), (12, "dodici"), (13, "tredici"),
               (14, "quattordici"), (15, "quindici"), (16, "sedici"),
               (17, "diciassette"), (18, "diciotto"), (19, "diciannove")):
    _reg("T", _v, _f)
for _v, _f in ((20, "venti vent"), (30, "trenta trent"), (40, "quaranta quarant"),
               (50, "cinquanta cinquant"), (60, "sessanta sessant"),
               (70, "settanta settant"), (80, "ottanta ottant"), (90, "novanta novant")):
    _reg("D", _v, *_f.split())
_reg("C", 100, "cento", "cent")
_reg("M", 1000, "mille")
_reg("m", 1000, "mila")
_reg("X", 10 ** 6, "milione", "milioni")
_reg("Y", 10 ** 9, "miliardo", "miliardi")

_BY_LEN = sorted(_ATOMS, key=len, reverse=True)
_UNITS = {w for w, (k, _) in _ATOMS.items() if k in ("U", "Z")}
_DIGIT_WORDS = {"zero": "0", "uno": "1", "due": "2", "tre": "3", "tré": "3",
                "quattro": "4", "cinque": "5", "sei": "6", "sette": "7",
                "otto": "8", "nove": "9"}


# prefiltro: una parola e' un numero solo se e' fatta SOLO di atomi. Scarta quasi tutto il
# testo con una sola regex, prima della scomposizione (che costa di piu').
_ALL_ATOMS = re.compile("(?:" + "|".join(map(re.escape, _BY_LEN)) + ")+")


@lru_cache(maxsize=65536)
def _tokens(word):
    """Scompone una parola in atomi (tupla di tipi) o None se non e' un numero."""
    w = word.lower()
    if not w or len(w) > 40 or not _ALL_ATOMS.fullmatch(w):
        return None

    @lru_cache(maxsize=None)
    def rec(i):
        if i == len(w):
            return ()
        for a in _BY_LEN:
            if w.startswith(a, i):
                rest = rec(i + len(a))
                if rest is not None:
                    return ((a,) + rest)
        return None

    atoms = rec(0)
    if not atoms:
        return None
    return atoms


_CHUNK = re.compile(r"^(?:UC|C)?(?:DU?|T|U)?$")


def _below_1000(kinds_vals):
    """kinds_vals = [(tipo, valore)...] senza scale. Vuoto -> None; ok -> valore."""
    if not kinds_vals:
        return None
    s = "".join(k for k, _ in kinds_vals)
    if "Z" in s or not _CHUNK.match(s):
        return None
    total, i = 0, 0
    if s.startswith("UC"):
        total, i = kinds_vals[0][1] * 100, 2
    elif s.startswith("C"):
        total, i = 100, 1
    return total + sum(v for _, v in kinds_vals[i:])


def parse_number(atoms):
    """Valore di una sequenza di atomi, o None se non e' un numero ben formato."""
    kv = [_ATOMS[a] for a in atoms]
    if not kv:
        return None
    if len(kv) == 1 and kv[0][0] == "Z":
        return 0
    total, i = 0, 0
    for kind, mult in (("Y", 10 ** 9), ("X", 10 ** 6)):
        idx = next((j for j in range(i, len(kv)) if kv[j][0] == kind), None)
        if idx is not None:
            part = kv[i:idx]
            n = 1 if not part else _below_1000(part)
            if n is None:
                return None
            total += n * mult
            i = idx + 1
    idx = next((j for j in range(i, len(kv)) if kv[j][0] in ("M", "m")), None)
    if idx is not None:
        part = kv[i:idx]
        if kv[idx][0] == "M":
            if part:
                return None
            n = 1
        else:
            n = _below_1000(part)
            if n is None or n < 2:
                return None
        total += n * 1000
        i = idx + 1
    if i < len(kv):
        rest = _below_1000(kv[i:])
        if rest is None:
            return None
        total += rest
    elif total == 0:
        return None
    return total


_WORD = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)?")


class _W:
    __slots__ = ("text", "start", "end", "atoms")

    def __init__(self, m):
        self.text, self.start, self.end = m.group(0), m.start(), m.end()
        self.atoms = _tokens(self.text) if "'" not in self.text and "’" not in self.text else None


def _words(text):
    return [_W(m) for m in _WORD.finditer(text)]


def _gap_ok(text, a_end, b_start):
    """Fra due parole-numero della stessa cifra: solo spazi (anche a capo)."""
    return text[a_end:b_start].strip() == "" and "\n\n" not in text[a_end:b_start]


def _runs(text, words):
    """Gruppi di parole-numero consecutive (con "e" fra l'una e l'altra).
    Ritorna [(i_prima, i_ultima_inclusa)]."""
    runs, i, n = [], 0, len(words)
    while i < n:
        if words[i].atoms is None:
            i += 1
            continue
        j = i
        while j + 1 < n:
            nxt = words[j + 1]
            if nxt.atoms is not None and _gap_ok(text, words[j].end, nxt.start):
                j += 1
            elif (nxt.text.lower() == "e" and j + 2 < n and words[j + 2].atoms is not None
                  and _gap_ok(text, words[j].end, nxt.start)
                  and _gap_ok(text, nxt.end, words[j + 2].start)):
                j += 2
            else:
                break
        runs.append((i, j))
        i = j + 1
    return runs


def _value(words, i, j):
    atoms = []
    for w in words[i:j + 1]:
        if w.atoms:
            atoms.extend(w.atoms)
    return parse_number(atoms)


def _best_prefix(words, i, j, accept=lambda v: True):
    """Il prefisso piu' lungo words[i..k] (k<=j) che e' un numero valido accettato."""
    for k in range(j, i - 1, -1):
        if words[k].atoms is None:           # "e" di collegamento in coda
            continue
        v = _value(words, i, k)
        if v is not None and accept(v):
            return k, v
    return None, None


def _segments(words, i, j, accept=lambda v: True):
    """Spezza un gruppo in numeri validi consecutivi: [(prima, ultima, valore)].
    "duemilacinquecento e cinquecento" sono DUE numeri, non uno malformato."""
    out = []
    while i <= j:
        if words[i].atoms is None:
            i += 1
            continue
        k, v = _best_prefix(words, i, j, accept)
        if k is None:
            i += 1
            continue
        out.append((i, k, v))
        i = k + 1
    return out


def _best_suffix(words, i, j, accept=lambda v: True):
    for k in range(i, j + 1):
        if words[k].atoms is None:
            continue
        v = _value(words, k, j)
        if v is not None and accept(v):
            return k, v
    return None, None


# --------------------------------------------------------------------------- #
# Ancore
# --------------------------------------------------------------------------- #
_MONTHS = ("gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|settembre|"
           "ottobre|novembre|dicembre")
_MONTH_RX = re.compile(r"\b(?:" + _MONTHS + r")\b", re.I)
_CUR_AFTER = re.compile(r"[ \t]*(?:di[ \t]+)?(?:euro|eur|€)(?![A-Za-z])", re.I)
_CUR_BEFORE = re.compile(r"(?<![A-Za-z])(?:euro|eur|€)[ \t]*$", re.I)
_LEGAL_CENTS = re.compile(r"[ \t]*/[ \t]*(?:\d{2}|=|cento)(?!\d)", re.I)
_YEAR_DIGITS = re.compile(r"(?:19|20)\d{2}(?!\d)")
_YEAR_PREFIX = re.compile(
    r"(?:(?:l['’]|nell['’]|dell['’]|dall['’]|all['’])?anno|nel|del|dal|il)[ \t]+$", re.I)
_DAY_DIGITS_LEFT = re.compile(
    r"(?<![\d/.\-])(?P<d>[0-3]?\d[°º]?|primo)[ \t]+(?:(?:del[ \t]+mese[ \t]+)?di[ \t]+)?$", re.I)
_DAY_GAP = re.compile(r"[ \t]+(?:(?:del[ \t]+mese[ \t]+)?di[ \t]+)?$", re.I)


def _ent(label, start, end):
    return {"label": label, "start": start, "end": end, "score": 0.95,
            "validated": False, "source": "regex", "union": True}


# Una cifra "da anno": contiene mille/mila (millenovecentoottanta, duemilaventi)
def _looks_year(words, i, k):
    return any(a in ("mille", "mila") for w in words[i:k + 1] if w.atoms for a in w.atoms)


def detect_dates(text):
    ents = []
    words = _words(text)
    runs = _runs(text, words)
    by_end = {words[j].end: (i, j) for i, j in runs}
    by_start = {words[i].start: (i, j) for i, j in runs}

    def year_right(pos):
        """Anno (cifre o lettere) che inizia dopo `pos`; ritorna end o None."""
        m = re.compile(r"[ \t]+(?:(?:dell['’]\s*)?anno[ \t]+|del[ \t]+|nel[ \t]+)?", re.I) \
            .match(text, pos)
        p = m.end() if m else pos
        if not m:
            return None
        my = _YEAR_DIGITS.match(text, p)
        if my:
            return my.end()
        r = by_start.get(p)
        if r:
            k, _ = _best_prefix(words, r[0], r[1], lambda v: 1900 <= v <= 2099)
            if k is not None:
                return words[k].end
        return None

    for mm in _MONTH_RX.finditer(text):
        s, e = mm.start(), mm.end()
        # giorno a sinistra: cifre ("12", "1°", "primo") o parole ("quindici")
        left = text[max(0, s - 40):s]
        md = _DAY_DIGITS_LEFT.search(left)
        if md:
            s = s - len(left) + md.start("d")
        else:
            gap = _DAY_GAP.search(left) or None
            gstart = s - (len(left) - gap.start()) if gap else s
            r = by_end.get(gstart)
            if r is None and gap is None:
                r = by_end.get(s)
            if r:
                k, v = _best_suffix(words, r[0], r[1], lambda v: 1 <= v <= 31)
                if k is not None:
                    s = words[k].start
        ye = year_right(e)
        if ye is not None:
            e = ye
        elif s == mm.start():
            continue        # solo il mese, senza giorno ne' anno: lo lascia al modello
        ents.append(_ent("DATE", s, e))

    # "nell'anno duemilaventi", "nel millenovecentosettantacinque": anno in sole
    # lettere dopo una parola-ancora (la cifra da sola e' ambigua, qui no)
    for i, k, v in [(a, b, v) for i, j in runs
                    for a, b, v in _segments(words, i, j, lambda v: 1900 <= v <= 2099)]:
        if not _looks_year(words, i, k):
            continue
        before = text[max(0, words[i].start - 20):words[i].start]
        if _YEAR_PREFIX.search(before):
            ents.append(_ent("DATE", words[i].start, words[k].end))
    return ents


def _cents_right(text, words, by_start, pos):
    """Dopo l'importo: ",50", "/00", "virgola cinquanta", "e cinquanta centesimi".
    Ritorna la nuova fine (o pos)."""
    m = re.compile(r"[ \t]*/[ \t]*(?:\d{2}|=|cento)(?!\d)", re.I).match(text, pos)
    if m:
        return m.end()
    m = re.compile(r"[ \t]+virgola[ \t]+", re.I).match(text, pos)
    if m:
        r = by_start.get(m.end())
        if r:
            k, _ = _best_prefix(words, r[0], r[1], lambda v: v < 100)
            if k is None:                       # "zero zero"
                k, v = _best_prefix(words, r[0], r[1])
            if k is not None:
                return words[k].end
        md = re.compile(r"\d{1,2}(?!\d)").match(text, m.end())
        if md:
            return md.end()
    return pos


def detect_amounts(text):
    ents = []
    words = _words(text)
    runs = _runs(text, words)
    by_start = {words[i].start: (i, j) for i, j in runs}

    segs = [(a, b, v) for i, j in runs for a, b, v in _segments(words, i, j)]
    for i, k, v in segs:
        s, e = words[i].start, words[k].end
        before = text[max(0, s - 12):s]
        e2 = _cents_right(text, words, by_start, e)
        if e2 == e:
            # "e cinquanta centesimi" dopo l'importo
            mc = re.compile(r"[ \t]+e[ \t]+", re.I).match(text, e)
            if mc:
                r = by_start.get(mc.end())
                if r:
                    kk, vv = _best_prefix(words, r[0], r[1], lambda v: v < 100)
                    if kk is not None and re.compile(r"[ \t]*centesim", re.I).match(
                            text, words[kk].end):
                        e2 = words[kk].end
        legal = e2 > e and text[e:e2].lstrip().startswith("/")
        e = e2
        after_cur = _CUR_AFTER.match(text, e)
        before_cur = _CUR_BEFORE.search(before)
        if after_cur:
            e = after_cur.end()
            ents.append(_ent("AMOUNT", s, e))
        elif before_cur:
            s = s - len(before) + before_cur.start()
            ents.append(_ent("AMOUNT", s, e))
        elif legal:
            ents.append(_ent("AMOUNT", s, e))
        elif re.compile(r"[ \t]*centesim", re.I).match(text, e):
            ents.append(_ent("AMOUNT", s, re.compile(r"[ \t]*centesim\w*", re.I).match(text, e).end()))
    # "sei euro e cinquanta centesimi": un solo importo, non due segnaposto
    ents.sort(key=lambda x: x["start"])
    out = []
    for x in ents:
        if (out and "centesim" in text[x["start"]:x["end"]].lower()
                and re.fullmatch(r"[ \t]+e[ \t]+", text[out[-1]["end"]:x["start"]], re.I)):
            out[-1]["end"] = x["end"]
            continue
        out.append(x)
    return out


# cifre + moltiplicatore + valuta: "3 mila euro", "2,5 milioni di euro", "10mila euro"
_DIGIT_MAG = re.compile(
    r"(?<![\w.,])\d{1,3}(?:[.,]\d{1,3})?[ \t]*"
    r"(?:mila|mille|milioni|milione|miliardi|miliardo|mln|mld)[ \t]*(?:di[ \t]+)?"
    r"(?:euro|eur|€)(?![A-Za-z])", re.I)
# valuta attaccata a parole-numero senza spazio: "quindicimilaeuro"
_GLUED_EURO = re.compile(r"\b[^\W\d_]{3,40}?(?:euro|eur)\b", re.I)


def detect_glued_euro(text):
    ents = []
    for m in _GLUED_EURO.finditer(text):
        w = m.group(0)
        stem = re.sub(r"(?:euro|eur)$", "", w, flags=re.I)
        atoms = _tokens(stem)
        if atoms and parse_number(atoms) is not None:
            ents.append(_ent("AMOUNT", m.start(), m.end()))
    return ents


def detect_digit_words(text):
    """>= 7 cifre dette una per una ("tre tre tre uno due tre quattro..."):
    un numero di telefono (se il contesto lo dice) o un codice."""
    ents = []
    words = _words(text)
    i, n = 0, len(words)
    while i < n:
        w = words[i].text.lower()
        if w not in _DIGIT_WORDS:
            i += 1
            continue
        j = i
        while (j + 1 < n and words[j + 1].text.lower() in _DIGIT_WORDS
               and text[words[j].end:words[j + 1].start].strip(" \t-,.") == ""):
            j += 1
        if j - i + 1 >= 7:
            ctx = text[max(0, words[i].start - 40):words[i].start].lower()
            label = ("TELEPHONENUM"
                     if re.search(r"tel|cell|fax|whatsapp|chiam|numero", ctx) else "DOCID")
            ents.append({**_ent(label, words[i].start, words[j].end), "union": False})
        i = j + 1
    return ents


def detect_words(text):
    """Tutte le entita' scritte in lettere (e le forme cifre+magnitudine)."""
    ents = detect_dates(text) + detect_amounts(text) + detect_glued_euro(text) \
        + detect_digit_words(text)
    for m in _DIGIT_MAG.finditer(text):
        ents.append(_ent("AMOUNT", m.start(), m.end()))
    return ents
