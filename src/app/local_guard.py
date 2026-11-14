# -*- coding: utf-8 -*-
"""
Protezione del server locale (issue upstream #112).

Il server Flask ascolta su 127.0.0.1, ma "locale" non basta: qualunque pagina web
aperta nel browser dell'utente puo' mandargli richieste. Tre attacchi concreti:

- DNS REBINDING: un sito fa risolvere il proprio dominio in 127.0.0.1; per il
  browser la richiesta e' same-origin e la risposta (testo, dizionario, PDF) si
  legge. La difesa e' l'header Host: il browser manda il dominio dell'attaccante,
  che non e' nell'elenco degli host ammessi -> 403.
- CSRF: un form o una fetch da un altro sito verso http://127.0.0.1:5005/settings
  o /config. Il browser allega l'header Origin del sito che la fa -> 403; e
  comunque manca il token.
- Lettura dei documenti in memoria (/doc/<id>/...) da chi indovina o intercetta
  un doc_id: serve anche il token di sessione.

Il TOKEN e' casuale per processo (o fissato con PII_API_TOKEN, per chi usa l'API
da script). Arriva alla UI dentro la pagina servita su "/", che a sua volta si
ottiene solo con un Host ammesso: un sito terzo non puo' leggerla (same-origin).
La UI lo rimanda nell'header X-PII-Token; le immagini e i download di /doc/...,
che non possono portare header, lo passano in query (?t=...).

Questo modulo non importa il modello: e' testabile da solo.
"""

import hmac
import ipaddress
import os
import secrets
from urllib.parse import urlsplit

TOKEN_HEADER = "X-PII-Token"
TOKEN_QUERY = "t"

LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1"}

# Percorsi che non richiedono il token. Restano soggetti al controllo di Host e
# Origin. Nessuno restituisce contenuto dei documenti:
#  - la pagina e le sue risorse statiche (la pagina e' il canale del token);
#  - /health per orchestratori e HEALTHCHECK di Docker;
#  - GET /config, che Tauri (lib.rs, is_our_backend) interroga con una richiesta
#    TCP grezza, senza token, per riconoscere il proprio backend.
_OPEN_PREFIXES = ("/static/", "/assets/")
_OPEN_PATHS = {"/", "/favicon.ico", "/health", "/healthz"}
_OPEN_GET_PATHS = {"/config"}


def _env_token():
    t = os.environ.get("PII_API_TOKEN", "").strip()
    return t or None


API_TOKEN = _env_token() or secrets.token_urlsafe(32)


def token_required_enabled():
    """PII_REQUIRE_TOKEN=0 spegne SOLO il token (Host/Origin restano): per chi
    espone l'API dietro un proprio strato di autenticazione."""
    v = os.environ.get("PII_REQUIRE_TOKEN", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def is_loopback(host):
    """True per localhost e indirizzi di loopback (127.0.0.0/8, ::1)."""
    h = (host or "").strip().strip("[]").lower()
    if h in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def is_wildcard(host):
    h = (host or "").strip().strip("[]")
    return h in ("0.0.0.0", "::")


def _host_only(hostport):
    """'127.0.0.1:5005' -> '127.0.0.1'; '[::1]:5005' -> '::1'; minuscolo."""
    hp = (hostport or "").strip().lower()
    if not hp:
        return ""
    if hp.startswith("["):
        end = hp.find("]")
        return hp[1:end] if end > 0 else hp
    if hp.count(":") == 1:
        return hp.split(":", 1)[0]
    return hp           # IPv6 senza parentesi (non valido come Host, resta tale)


def allowed_hosts(bind_host=None):
    """Host ammessi nell'header Host / Origin.

    Sempre: localhost e loopback. In piu': l'host di bind cosi' com'e' (l'IP di
    LAN scelto apposta, oppure "0.0.0.0", che e' l'indirizzo con cui Tauri apre la
    finestra e interroga /config quando il bind e' su tutte le interfacce), e
    quelli in PII_ALLOWED_HOSTS (lista separata da virgole) per chi espone il
    server su 0.0.0.0 e ci arriva con un nome o un IP che il server non puo'
    indovinare. Un attacco di DNS rebinding manda nell'Host il dominio
    dell'attaccante, che qui non c'e'."""
    hosts = set(LOOPBACK_NAMES)
    if bind_host:
        hosts.add(bind_host.strip().strip("[]").lower())
    extra = os.environ.get("PII_ALLOWED_HOSTS", "")
    hosts.update(h.strip().strip("[]").lower() for h in extra.split(",") if h.strip())
    return hosts


def host_ok(host_header, bind_host=None):
    h = _host_only(host_header)
    return bool(h) and (h in allowed_hosts(bind_host) or is_loopback(h))


def origin_ok(origin, bind_host=None):
    """Origin assente = richiesta non da browser o same-origin GET: ammessa.
    "null" (iframe sandbox, file://) e origini con host non ammesso: rifiutate."""
    if origin is None or origin == "":
        return True
    if origin == "null":
        return False
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https"):
        return False
    h = (parts.hostname or "").lower()
    return bool(h) and (h in allowed_hosts(bind_host) or is_loopback(h))


def token_ok(supplied):
    return bool(supplied) and hmac.compare_digest(str(supplied), API_TOKEN)


def path_needs_token(method, path):
    if path in _OPEN_PATHS or path.startswith(_OPEN_PREFIXES):
        return False
    if method in ("GET", "HEAD") and path in _OPEN_GET_PATHS:
        return False
    return True


def check(method, path, headers, args, bind_host=None):
    """Ritorna None se la richiesta passa, altrimenti (status, messaggio)."""
    if not host_ok(headers.get("Host", ""), bind_host):
        return 403, "Host non ammesso."
    if not origin_ok(headers.get("Origin"), bind_host):
        return 403, "Origine non ammessa."
    if token_required_enabled() and path_needs_token(method, path):
        supplied = headers.get(TOKEN_HEADER)
        # in query solo per le GET di /doc/...: <img src> e i download non portano header
        if not supplied and method in ("GET", "HEAD") and path.startswith("/doc/"):
            supplied = args.get(TOKEN_QUERY)
        if not token_ok(supplied):
            return 403, "Token di sessione mancante o non valido."
    return None


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
}
