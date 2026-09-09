#!/usr/bin/env python3
"""
Powerwatch — watchdog blackout + dashboard PWA.

Gira sul nodo sotto UPS. Un thread ascolta i pulse UDP dell'ESP32 (alimentato
dalla rete non protetta); quando spariscono, la corrente è via. Le transizioni
finiscono su Web Push (primario) ed email (ridondanza), e su SQLite per lo
storico. Flask serve la dashboard PWA e una manciata di endpoint JSON.
"""

import functools
import os
import secrets
import socket
import threading
import time
from datetime import datetime, timedelta

from flask import Flask, Response, jsonify, request, send_from_directory

import notifiers
from notifiers import log
from store import Store

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")


class Config:
    def __init__(self):
        e = os.environ.get
        self.site = e("PW_SITE", "casa")

        # pulse
        self.bind_host = e("PW_BIND_HOST", "0.0.0.0")
        self.pulse_port = int(e("PW_PULSE_PORT", "9999"))
        self.token = e("PW_TOKEN", "cambiami")
        # Secondi senza pulse prima di dichiarare il blackout. Tienilo >= 4-5x
        # l'intervallo di pulse: UDP perde pacchetti e il WiFi si riconnette.
        self.timeout = int(e("PW_TIMEOUT", "90"))
        # Secondi di pulse continui prima di dichiarare il ripristino: evita
        # raffiche di notifiche se la corrente sfarfalla.
        self.restore_confirm = int(e("PW_RESTORE_CONFIRM", "20"))

        # http
        self.http_port = int(e("PW_HTTP_PORT", "8080"))
        self.ui_token = e("PW_UI_TOKEN", "")  # vuoto = nessuna autenticazione

        # web push
        self.vapid_private_key = e("PW_VAPID_PRIVATE_KEY", "")
        self.vapid_public_key = e("PW_VAPID_PUBLIC_KEY", "")
        self.vapid_subject = e("PW_VAPID_SUBJECT", "mailto:admin@example.com")
        # TTL alto: se il telefono è spento o offline, la push viene comunque
        # consegnata quando torna online entro questo tempo.
        self.push_ttl = int(e("PW_PUSH_TTL", "86400"))

        # email
        self.smtp_host = e("PW_SMTP_HOST", "")
        self.smtp_port = int(e("PW_SMTP_PORT", "587"))
        self.smtp_user = e("PW_SMTP_USER", "")
        self.smtp_pass = e("PW_SMTP_PASS", "")
        self.smtp_tls = e("PW_SMTP_TLS", "starttls").lower()
        self.mail_from = e("PW_MAIL_FROM", self.smtp_user)
        self.mail_to = [a.strip() for a in e("PW_MAIL_TO", "").split(",") if a.strip()]

        self.db_path = e("PW_DB", os.path.join(HERE, "data", "powerwatch.db"))


cfg = Config()
store = Store(cfg.db_path)


# ---------- utility ----------
def human(seconds):
    seconds = int(max(0, seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}g {h}h"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def stamp(ts):
    return datetime.fromtimestamp(ts).strftime("%d/%m/%Y %H:%M:%S")


# ---------- monitor ----------
class Monitor(threading.Thread):
    daemon = True

    def __init__(self):
        super().__init__(name="monitor")
        st = store.get_state()
        self.power = st.get("power")          # True / False / None (sconosciuto)
        self.down_since = st.get("down_since")
        self.last_pulse = None
        self.up_since = None
        self.started = time.time()
        self.pending = []                     # notifiche da ritentare
        self._last_retry = 0.0
        self._bad_token_logged = {}           # per limitare il log del rumore

    # --- lo stato che la dashboard legge ---
    def snapshot(self):
        now = time.time()
        return {
            "site": cfg.site,
            "power": self.power,
            "down_since": self.down_since,
            "last_pulse": self.last_pulse,
            "pulse_age": (now - self.last_pulse) if self.last_pulse else None,
            "timeout": cfg.timeout,
            "server_time": now,
            "push_subs": store.count_subs(),
            "pending_notifications": len(self.pending),
            "channels": {
                "push": bool(cfg.vapid_private_key),
                "email": bool(cfg.smtp_host and cfg.mail_to),
            },
        }

    def _notify(self, title, body, tag, priority="high"):
        if not notifiers.notify_all(cfg, store, title, body, tag, priority):
            self.pending.append((title, body, tag, priority))

    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((cfg.bind_host, cfg.pulse_port))
        sock.settimeout(1.0)
        log(f"[pulse] in ascolto su {cfg.bind_host}:{cfg.pulse_port}, timeout={cfg.timeout}s")

        while True:
            now = time.time()

            try:
                data, addr = sock.recvfrom(256)
                if data.decode("utf-8", "replace").strip() == cfg.token:
                    # Rispondiamo solo ai pulse validi: l'ack dice all'ESP32 che
                    # l'indirizzo configurato e' quello giusto, e non offre un
                    # bersaglio a chi scansiona la porta a caso.
                    sock.sendto(b"ack", addr)
                    if self.up_since is None:
                        log(f"[pulse] pulse da {addr[0]}")
                        self.up_since = now
                    self.last_pulse = now
                else:
                    # Un vicino chiacchierone (un Nest Hub, un discovery
                    # qualsiasi) non deve poterci riempire il log: una riga
                    # ogni 5 minuti per sorgente.
                    if now - self._bad_token_logged.get(addr[0], 0) > 300:
                        self._bad_token_logged[addr[0]] = now
                        log(f"[pulse] token errato da {addr[0]}, ignorato")
            except socket.timeout:
                pass
            except Exception as e:
                log(f"[pulse] socket: {e!r}")
                time.sleep(1)

            reference = self.last_pulse or self.started
            stale = now - reference >= cfg.timeout

            # --- corrente ripristinata ---
            if (
                not stale
                and self.up_since is not None
                and now - self.up_since >= cfg.restore_confirm
                and self.power is not True
            ):
                if self.power is False and self.down_since:
                    dur = now - self.down_since
                    store.add_event("up", ts=now, duration=dur)
                    self._notify(
                        "Corrente ripristinata",
                        f"Tornata alle {stamp(now)}.\n"
                        f"Blackout iniziato alle {stamp(self.down_since)}.\n"
                        f"Durata: {human(dur)}.",
                        tag="power",
                        priority="normal",
                    )
                else:
                    log("[state] primo aggancio: corrente OK")
                self.power = True
                self.down_since = None
                store.set_state(power=True, down_since=None)

            # --- blackout ---
            elif stale and self.power is not False:
                self.down_since = reference
                self.power = False
                self.up_since = None
                store.add_event("down", ts=reference)
                detail = (
                    f"Ultimo pulse: {stamp(self.last_pulse)}."
                    if self.last_pulse
                    else "Nessun pulse ricevuto dall'avvio del watchdog."
                )
                self._notify(
                    "CORRENTE ASSENTE",
                    f"Nessun pulse dall'ESP32 da {human(now - reference)}.\n"
                    f"{detail}\nFrigo e telecamere probabilmente non alimentati.",
                    tag="power",
                    priority="high",
                )
                store.set_state(power=False, down_since=self.down_since)

            if stale:
                self.up_since = None

            # --- ritenta le notifiche non consegnate (es. ISP giù col blackout) ---
            if self.pending and now - self._last_retry > 60:
                self._last_retry = now
                still = []
                for title, body, tag, prio in self.pending:
                    if not notifiers.notify_all(
                        cfg, store, f"(ritardata) {title}", body, tag, prio
                    ):
                        still.append((title, body, tag, prio))
                self.pending = still


# ---------- statistiche di uptime ----------
def outage_intervals():
    """Ricostruisce gli intervalli di blackout (inizio, fine) dagli eventi.

    Gli eventi 'up' portano la durata del blackout appena concluso, quindi
    l'intervallo si ricava all'indietro. Un blackout ancora in corso non ha
    un 'up': lo chiudiamo su adesso.
    """
    intervals = []
    for e in store.all_events():
        if e["kind"] == "up" and e["duration"]:
            intervals.append((e["ts"] - e["duration"], e["ts"]))
    if monitor.power is False and monitor.down_since:
        intervals.append((monitor.down_since, time.time()))
    return intervals


def window_stats(intervals, start, end):
    """Statistiche su una finestra, contando solo la parte di blackout che ci
    cade dentro (un blackout a cavallo del bordo non va contato tutto)."""
    down = 0.0
    count = 0
    longest = 0.0
    for a, b in intervals:
        lo, hi = max(a, start), min(b, end)
        if hi > lo:
            down += hi - lo
            count += 1
            longest = max(longest, b - a)   # durata piena dell'episodio
    span = max(1.0, end - start)
    return {
        "uptime_pct": round(100.0 * (1.0 - down / span), 4),
        "outages": count,
        "downtime": round(down, 1),
        "longest": round(longest, 1),
        "span": round(span, 1),
    }


def daily_downtime(intervals, days=30):
    """Secondi di blackout per giorno solare locale, dal piu' vecchio a oggi."""
    midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    out = []
    for i in range(days - 1, -1, -1):
        d0 = (midnight - timedelta(days=i))
        a0, a1 = d0.timestamp(), (d0 + timedelta(days=1)).timestamp()
        secs = sum(max(0.0, min(b, a1) - max(a, a0)) for a, b in intervals)
        out.append({"date": d0.strftime("%Y-%m-%d"), "down": round(secs, 1)})
    return out


monitor = Monitor()


# ---------- http ----------
app = Flask(__name__, static_folder=None)


def authorized():
    if not cfg.ui_token:
        return True
    given = request.headers.get("X-PW-Key") or request.args.get("k", "")
    return secrets.compare_digest(given, cfg.ui_token)


def protected(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if not authorized():
            return jsonify({"error": "unauthorized"}), 401
        return fn(*a, **kw)

    return wrapper


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    return resp


def asset_version():
    """Impronta degli asset, per il cache-busting."""
    stamp = 0.0
    for name in ("app.js", "index.html"):
        try:
            stamp = max(stamp, os.path.getmtime(os.path.join(STATIC, name)))
        except OSError:
            pass
    return str(int(stamp))


@app.get("/")
def index():
    # La pagina è servita sempre: senza chiave valida il JS mostra la schermata
    # di sblocco invece dei dati. Gli endpoint dati restano protetti.
    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
        html = f.read()
    # Un CDN davanti (Cloudflare) puo' cachare i .js ignorando il no-cache:
    # con la versione nell'URL un deploy nuovo e' un URL nuovo, e la cache
    # vecchia smette di poter far danni.
    html = html.replace("/static/app.js", f"/static/app.js?v={asset_version()}")
    return Response(html, mimetype="text/html",
                    headers={"Cache-Control": "no-store"})


@app.get("/sw.js")
def service_worker():
    # Il service worker deve stare in root per avere scope su tutto il sito.
    with open(os.path.join(STATIC, "sw.js"), "rb") as f:
        body = f.read()
    return Response(body, mimetype="text/javascript", headers={
        "Cache-Control": "no-cache",
        "Service-Worker-Allowed": "/",
    })


@app.get("/static/<path:filename>")
def static_files(filename):
    resp = send_from_directory(STATIC, filename)
    resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


@app.get("/manifest.webmanifest")
def manifest():
    # start_url porta con sé la chiave: su iOS la web app installata ha uno
    # storage separato da Safari, quindi è l'unico modo perché si autentichi
    # da sola al primo avvio.
    k = request.args.get("k", "")
    start = f"/?k={k}" if k else "/"
    return jsonify({
        "name": f"Powerwatch — {cfg.site}",
        "short_name": "Powerwatch",
        "description": "Stato della corrente di casa",
        "start_url": start,
        "scope": "/",
        "display": "standalone",
        "background_color": "#0b1220",
        "theme_color": "#0b1220",
        "icons": [
            {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "/static/icons/icon-512-maskable.png", "sizes": "512x512",
             "type": "image/png", "purpose": "maskable"},
        ],
    })


@app.get("/api/status")
@protected
def api_status():
    data = monitor.snapshot()
    data["events"] = store.recent_events(20)
    data["vapid_public_key"] = cfg.vapid_public_key
    return jsonify(data)


@app.post("/api/subscribe")
@protected
def api_subscribe():
    body = request.get_json(silent=True) or {}
    sub = body.get("subscription")
    if not sub or "endpoint" not in sub:
        return jsonify({"error": "subscription mancante"}), 400
    store.add_sub(sub, label=body.get("label"))
    log(f"[push] nuova subscription ({store.count_subs()} totali)")
    return jsonify({"ok": True, "subs": store.count_subs()})


@app.post("/api/unsubscribe")
@protected
def api_unsubscribe():
    body = request.get_json(silent=True) or {}
    endpoint = body.get("endpoint")
    if not endpoint:
        return jsonify({"error": "endpoint mancante"}), 400
    store.del_sub(endpoint)
    return jsonify({"ok": True, "subs": store.count_subs()})


@app.post("/api/test")
@protected
def api_test():
    ok = notifiers.notify_all(
        cfg, store,
        "Notifica di prova",
        f"Se leggi questo, il canale funziona. {stamp(time.time())}",
        tag="test", priority="normal",
    )
    return jsonify({"ok": ok, "subs": store.count_subs()})


@app.get("/api/stats")
@protected
def api_stats():
    now = time.time()
    intervals = outage_intervals()
    since = store.get_state().get("monitoring_since") or now

    windows = {}
    for label, days in (("7g", 7), ("30g", 30), ("90g", 90), ("365g", 365)):
        start = max(now - days * 86400, since)
        windows[label] = window_stats(intervals, start, now)
        # Giorni nominali della finestra: 'span' e' tagliato all'inizio del
        # monitoraggio e non va usato per etichettarla.
        windows[label]["days"] = days
        # Dice se la finestra e' piena o se il monitoraggio e' piu' giovane:
        # una uptime% su 30 giorni misurata in 2 non va letta allo stesso modo.
        windows[label]["partial"] = (now - since) < days * 86400

    return jsonify({
        "monitoring_since": since,
        "windows": windows,
        "daily": daily_downtime(intervals, 30),
    })


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "power": monitor.power})


def main():
    if cfg.ui_token:
        log("[http] autenticazione attiva (PW_UI_TOKEN)")
    else:
        log("[http] ATTENZIONE: nessun PW_UI_TOKEN, dashboard aperta a chiunque la raggiunga")
    if not cfg.vapid_private_key:
        log("[push] VAPID non configurato: le push sono disattivate (usa gen_vapid.py)")

    if not store.get_state().get("monitoring_since"):
        # Se il database ha gia' eventi (aggiornamento da una versione senza
        # questa chiave), il monitoraggio e' iniziato allora, non adesso:
        # altrimenti l'uptime% userebbe un denominatore troppo corto.
        events = store.all_events()
        store.set_state(monitoring_since=events[0]["ts"] if events else time.time())

    monitor.start()
    from waitress import serve
    log(f"[http] dashboard su :{cfg.http_port}")
    serve(app, host="0.0.0.0", port=cfg.http_port, threads=8, _quiet=True)


if __name__ == "__main__":
    main()
