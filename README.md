# Powerwatch — blackout casalingo: push, dashboard, email

ESP32 su presa **non** protetta da UPS → manda un pulse UDP ogni 10 s.
Server sotto UPS → se i pulse spariscono, la corrente è via.

```
[presa normale]  ESP32 ──UDP :9999──▶ server (UPS) ──┬─▶ Web Push  → iPhone
                                                      ├─▶ email    (ridondanza)
                                                      └─▶ dashboard PWA
```

Rete, ONT e server sono sotto UPS: durante il blackout il watchdog è vivo e ha
ancora internet per notificare. È tutto lì il meccanismo.

**La push parte dal server verso Apple/Google, non dal telefono verso casa**:
arriva anche quando sei all'estero e non riesci a raggiungere la rete di casa.
La dashboard invece ti serve solo quando vuoi guardarla, ed è lì che decidi
quanto esporla (vedi *Esposizione*).

## Cosa c'è

| | |
|---|---|
| [esp32-pulse.ino](esp32-pulse/esp32-pulse.ino) | Il pulse. Portale di configurazione + un pacchetto UDP ogni 10 s. |
| [powerwatch.py](server/powerwatch.py) | Watchdog, macchina a stati, API JSON. |
| [notifiers.py](server/notifiers.py) | Web Push e SMTP. Fan-out best-effort. |
| [store.py](server/store.py) | SQLite: stato, storico eventi, subscription. |
| [static/](server/static) | La PWA: dashboard, service worker, manifest, icone. |

## 1. ESP32

Il flash si fa una volta sola; WiFi e indirizzo del watchdog **non** stanno nel
sorgente, la board te li chiede al primo avvio.

```bash
arduino-cli core install esp32:esp32
arduino-cli lib install WiFiManager
arduino-cli compile -b esp32:esp32:esp32 esp32-pulse
arduino-cli upload -b esp32:esp32:esp32 -p /dev/cu.usbserial-0001 esp32-pulse
```

Poi, al primo avvio:

1. La board crea la rete **`powerwatch-setup`** (password `powerwatch`)
2. Collegati col telefono: si apre il portale di configurazione
3. Scegli il tuo WiFi, inserisci la password, e compila **IP del watchdog**,
   **porta UDP** (9999) e **token** (lo stesso di `PW_TOKEN`)
4. Salva: la board si riavvia, si collega e inizia a mandare i pulse

Tutto finisce in NVS e sopravvive ai riavvii.

### Il LED dice se l'indirizzo e' giusto

Il watchdog risponde `ack` a ogni pulse valido, e il LED di bordo lo riporta:

| LED | Significato |
|---|---|
| un lampeggio breve per pulse | ack ricevuto, il server ti sente |
| tre lampeggi rapidi | nessuna risposta: IP o porta sbagliati, o server spento |

Serve perche' l'UDP e' a senso unico: senza ack la board non avrebbe modo di
accorgersi di star parlando nel vuoto, ed e' l'errore piu' facile da fare.

### Riconfigurare

**Tieni premuto BOOT per 3 s con la board gia' accesa** (il LED lampeggia, poi
riavvia nel portale). Non tenerlo premuto durante il reset: GPIO0 basso
all'avvio manda l'ESP32 nel bootloader ROM e lo sketch non parte nemmeno.

Oppure via **seriale a 115200**, comodo se la board e' attaccata al PC:

```
help                      elenco comandi
show                      configurazione attuale
host 192.168.1.10         cambia il target
port 9999
token la-tua-stringa
portal                    cancella tutto e riapre il portale
```

Poi attaccalo a una presa **non** sotto UPS, meglio se sullo stesso quadro di
frigo e telecamere.

## 2. Server

```bash
cd server
cp powerwatch.env.example powerwatch.env
python3 gen_vapid.py                 # incolla le due chiavi nel .env
openssl rand -hex 16                 # -> PW_UI_TOKEN
docker compose up -d --build
```

Le chiavi VAPID identificano il server verso i push service. Se le rigeneri, i
dispositivi già iscritti smettono di ricevere e vanno reiscritti.

## 3. iPhone

Su iOS il Web Push funziona **solo** per le web app aggiunte alla schermata
Home: da Safari non arriva nulla, ed è il motivo per cui la dashboard è una PWA
e non una pagina qualsiasi.

1. Apri `https://tuo-dominio/?k=IL_TUO_PW_UI_TOKEN` in **Safari**
2. Condividi → **Aggiungi a Home**
3. Apri l'app **dall'icona sulla Home**, non da Safari
4. → **Attiva notifiche push** → concedi il permesso
5. → **Manda una notifica di prova** per confermare

La chiave viaggia dentro `start_url` del manifest: su iOS la web app installata
ha uno storage separato da Safari, ed è l'unico modo perché si autentichi da
sola al primo avvio. Ripeti la procedura su ogni dispositivo che vuoi avvisare.

## Esposizione

La PWA richiede **HTTPS** (i service worker non partono su http, tranne che su
localhost). Due strade sensate:

- **Cloudflare Tunnel** — nessuna porta aperta sul router, certificato gestito,
  raggiungibile da ovunque. Se ci metti davanti Cloudflare Access, `PW_UI_TOKEN`
  diventa una seconda serratura invece che l'unica.
- **Tailscale Serve** — HTTPS su `*.ts.net`, niente di pubblico. Il più sicuro,
  ma la dashboard si apre solo col VPN attivo. Le push arrivano lo stesso, anche
  fuori dal tailnet: le manda il server, non la raggiungi tu.

Non lasciare `PW_UI_TOKEN` vuoto se la dashboard è raggiungibile da internet:
senza chiave chiunque la trovi può leggere quando la tua casa è al buio.

## Parametri che contano

| Variabile | Default | Cosa fa |
|---|---|---|
| `PW_TIMEOUT` | 90 | Secondi senza pulse prima di dichiarare il blackout. Tienilo ≥ 4-5× l'intervallo di pulse: UDP perde pacchetti e il WiFi si riconnette. |
| `PW_RESTORE_CONFIRM` | 20 | Secondi di pulse continui prima di dichiarare il ripristino. Evita raffiche se la corrente sfarfalla. |
| `PW_PUSH_TTL` | 86400 | Se il telefono è spento o offline, il push service tiene la notifica per questo tempo e la consegna appena torna. |
| `PW_REMIND_EVERY` | 1800 | Ogni quanti secondi ripetere la push finché il blackout dura (0 disattiva). Solo push, non email. Sapendo che arriva ogni 30 minuti, il suo silenzio dice che l'UPS si è scaricato. |
| `PW_VAPID_SUBJECT` | — | Il tuo recapito come operatore del server, dentro il JWT che firma ogni push (RFC 8292). Serve ad Apple/Google per contattarti se il tuo endpoint fa danni. Dev'essere un `mailto:` o `https:`, ma non viene verificato: col placeholder tutto funziona, semplicemente quell'avviso non ti arriverebbe. Nota che il valore viene spedito ai push service a ogni invio. |
| `PW_UI_TOKEN` | — | Chiave della dashboard. Vuota = nessuna autenticazione. |
| `PW_MAIL_TO` | — | Destinatari email, separati da virgola: `io@x.it,altro@y.it`. Un indirizzo rifiutato viene loggato e non blocca gli altri. |

## Comportamento ai bordi

- **Stato persistente** (SQLite): riavvii il server durante un blackout e non
  ricevi una seconda notifica di down; il ripristino riporta comunque la durata
  corretta.
- **Avvio a freddo durante un blackout**: dopo `PW_TIMEOUT` notifica lo stesso.
- **Notifica non consegnata** (ISP giù insieme alla corrente): resta in coda,
  ritentata ogni 60 s con prefisso `(ritardata)`.
- **Subscription morte** (app disinstallata, permesso revocato): il push service
  risponde 404/410 e la subscription viene cancellata da sola.
- **Falsi positivi**: se muore solo l'ESP32 o il suo WiFi, ricevi un falso
  allarme. È il compromesso del best-effort: costa molto meno di un blackout non
  visto.

## API

Tutte richiedono `X-PW-Key` (o `?k=`) se `PW_UI_TOKEN` è impostato.

| | |
|---|---|
| `GET /api/status` | Stato corrente, ultimi 20 eventi, canali attivi. |
| `POST /api/subscribe` | Registra una subscription push. |
| `POST /api/unsubscribe` | La rimuove. |
| `POST /api/test` | Manda una notifica di prova su tutti i canali. |
| `GET /healthz` | Senza autenticazione, per monitoraggio esterno. |

## Statistiche di uptime

La tabella `events` (`ts`, `kind`, `duration`) registra già ogni transizione: è
la base per le statistiche, quando le vorrai. La dashboard per ora mostra solo
gli ultimi 20 eventi.

## Verifica

Stacca fisicamente l'ESP32 dalla presa. Entro `PW_TIMEOUT` deve arrivare la
push (e la mail); riattaccalo e entro ~30 s quella di ripristino. Log con
`docker compose logs -f powerwatch`.
