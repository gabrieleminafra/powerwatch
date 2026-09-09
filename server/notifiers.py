"""Canali di notifica: Web Push (primario) ed email SMTP (ridondanza)."""

import json
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate

from pywebpush import WebPushException, webpush


def log(msg):
    from datetime import datetime
    print(f"{datetime.now().isoformat(timespec='seconds')} {msg}", flush=True)


# ---------- Web Push ----------
# Le priorita' interne sono "normal" e "high", mappate sui valori che
# RFC 8030 ammette per l'header Urgency.
WEBPUSH_URGENCY = {"normal": "normal", "high": "high"}


def send_webpush(cfg, store, title, body, tag, priority="high"):
    """Manda a tutte le subscription registrate. Ritorna il numero di consegne.

    Le subscription rifiutate con 404/410 sono morte in modo definitivo
    (app disinstallata, permesso revocato): le cancelliamo.
    """
    if not cfg.vapid_private_key:
        return 0

    payload = json.dumps({"title": title, "body": body, "tag": tag})
    delivered = 0
    for sub in store.all_subs():
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=cfg.vapid_private_key,
                vapid_claims={"sub": cfg.vapid_subject},
                ttl=cfg.push_ttl,
                headers={"Urgency": WEBPUSH_URGENCY.get(priority, "normal")},
                timeout=20,
            )
            delivered += 1
        except WebPushException as e:
            status = getattr(e.response, "status_code", None)
            if status in (404, 410):
                store.del_sub(sub["endpoint"])
                log(f"[push] subscription scaduta, rimossa ({status})")
            else:
                log(f"[push] errore {status}: {e}")
        except Exception as e:
            log(f"[push] errore: {e!r}")
    return delivered


# ---------- Email ----------
def send_email(cfg, subject, body):
    if not (cfg.smtp_host and cfg.mail_to):
        return 0
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.mail_from
    msg["To"] = ", ".join(cfg.mail_to)
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(body)

    ctx = ssl.create_default_context()
    if cfg.smtp_tls == "ssl":
        server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=20, context=ctx)
    else:
        server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=20)
    with server:
        if cfg.smtp_tls == "starttls":
            server.starttls(context=ctx)
        if cfg.smtp_user:
            server.login(cfg.smtp_user, cfg.smtp_pass)
        # Con piu' destinatari send_message non solleva se almeno uno passa:
        # restituisce quelli rifiutati. Senza guardarli, un indirizzo sbagliato
        # resterebbe muto per sempre.
        refused = server.send_message(msg)

    if refused:
        for addr, (code, reason) in refused.items():
            log(f"[email] destinatario rifiutato: {addr} ({code} {reason})")
    delivered = len(cfg.mail_to) - len(refused)
    return delivered


# ---------- fan-out ----------
def notify_all(cfg, store, title, body, tag, priority="high"):
    """Best-effort su tutti i canali configurati. Non solleva mai.

    Ritorna True se almeno un canale ha consegnato.
    """
    results = {}
    for name, fn in (
        ("push", lambda: send_webpush(cfg, store, title, body, tag, priority)),
        ("email", lambda: send_email(cfg, f"Powerwatch - {title}", body)),
    ):
        try:
            n = fn()
            if n:
                results[name] = n
        except Exception as e:
            log(f"[notify] {name} fallito: {e!r}")

    if results:
        detail = ", ".join(f"{k}={v}" for k, v in results.items())
        log(f"[notify] '{title}' consegnato ({detail})")
    else:
        log(f"[notify] '{title}' NON consegnato da nessun canale")
    return bool(results)
