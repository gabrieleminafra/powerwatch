/* Powerwatch — dashboard PWA */

const $ = (id) => document.getElementById(id);
const KEY_STORAGE = "pw_key";

// La chiave arriva nella URL (?k=...) al primo avvio: su iOS la web app
// installata ha uno storage separato da Safari, quindi start_url la reinietta
// e noi la salviamo nel jar della web app.
const urlKey = new URLSearchParams(location.search).get("k");
if (urlKey) {
  localStorage.setItem(KEY_STORAGE, urlKey);
  history.replaceState(null, "", location.pathname);
}
let apiKey = localStorage.getItem(KEY_STORAGE) || "";

// Il manifest porta con sé la chiave, così finisce dentro start_url.
const link = document.createElement("link");
link.rel = "manifest";
link.href = apiKey ? `/manifest.webmanifest?k=${encodeURIComponent(apiKey)}`
                   : "/manifest.webmanifest";
document.head.appendChild(link);

const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent);
const standalone = window.matchMedia("(display-mode: standalone)").matches
                || navigator.standalone === true;

async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers, { "X-PW-Key": apiKey });
  if (opts.body) headers["Content-Type"] = "application/json";
  const res = await fetch(path, Object.assign({}, opts, { headers }));
  if (res.status === 401) throw new Error("unauthorized");
  if (!res.ok) throw new Error(`http ${res.status}`);
  // Quando la sessione di Cloudflare Access scade, la richiesta viene
  // rediretta alla pagina di login: arriva un 200 di HTML, non JSON. Senza
  // questo controllo la dashboard resterebbe muta su dati vecchi.
  if (!(res.headers.get("content-type") || "").includes("application/json")) {
    throw new Error("session-expired");
  }
  return res.json();
}

// ---------- formattazione ----------
function human(sec) {
  sec = Math.max(0, Math.round(sec));
  const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600);
  const m = Math.floor(sec % 3600 / 60), s = sec % 60;
  if (d) return `${d}g ${h}h`;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${s}s`;
  return `${s}s`;
}
const stamp = (ts) => new Date(ts * 1000).toLocaleString("it-IT",
  { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });

// ---------- rendering ----------
let last = null;
// Offset fra orologio del server e del telefono: i "da quanto" restano corretti
// anche se i due non sono sincronizzati.
let skew = 0;

function render(d) {
  last = d;
  skew = d.server_time - Date.now() / 1000;
  $("site").textContent = d.site;
  $("timeout").textContent = human(d.timeout);

  const el = $("status");
  el.classList.toggle("ok", d.power === true);
  el.classList.toggle("bad", d.power === false);
  $("state-text").textContent =
    d.power === true ? "Corrente OK" : d.power === false ? "Corrente assente" : "In attesa…";

  const chips = [];
  for (const [name, on] of Object.entries(d.channels)) {
    chips.push(`<span class="chip ${on ? "on" : ""}">${name}${on ? " ✓" : " —"}</span>`);
  }
  chips.push(`<span class="chip ${d.push_subs ? "on" : ""}">${d.push_subs} dispositivi</span>`);
  if (d.pending_notifications) {
    chips.push(`<span class="chip">${d.pending_notifications} notifiche in coda</span>`);
  }
  $("chips").innerHTML = chips.join("");

  $("events").innerHTML = d.events.length
    ? d.events.map((e) => `
        <li>
          <span class="k ${e.kind}">${e.kind === "down" ? "Blackout" : "Ripristino"}</span>
          <span class="t">${stamp(e.ts)}</span>
          ${e.duration ? `<span class="d">durato ${human(e.duration)}</span>` : ""}
        </li>`).join("")
    : `<li class="t">Nessun evento registrato.</li>`;

  tick();
}

// Aggiorna i contatori ogni secondo senza richiedere nulla al server.
function tick() {
  if (!last) return;
  const now = Date.now() / 1000 + skew;
  if (last.power === false && last.down_since) {
    $("since").innerHTML = `Da <b>${human(now - last.down_since)}</b> · dalle ${stamp(last.down_since)}`;
  } else if (last.power === true && last.last_pulse) {
    $("since").innerHTML = `Tutto regolare`;
  } else {
    $("since").textContent = "Nessun pulse ricevuto finora";
  }
  $("pulse-age").textContent = last.last_pulse ? human(now - last.last_pulse) : "mai";
}

async function refresh() {
  try {
    const d = await api("/api/status");
    showApp();
    render(d);
  } catch (e) {
    if (e.message === "unauthorized") showUnlock();
    else if (e.message === "session-expired") {
      hint("Sessione scaduta: ricarica la pagina per rifare il login.", "bad");
    }
  }
}

function showUnlock() { $("unlock").hidden = false; $("app").hidden = true; }
function showApp() { $("unlock").hidden = true; $("app").hidden = false; }

$("key-save").onclick = async () => {
  apiKey = $("key-input").value.trim();
  localStorage.setItem(KEY_STORAGE, apiKey);
  try {
    await api("/api/status");
    location.reload();
  } catch { $("key-hint").hidden = false; }
};

// ---------- push ----------
function urlB64ToUint8Array(base64String) {
  const padding = "=".repeat((4 - base64String.length % 4) % 4);
  const raw = atob((base64String + padding).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
}

function hint(msg, cls = "") { const h = $("push-hint"); h.textContent = msg; h.className = "hint " + cls; }

async function updatePushButton() {
  const btn = $("btn-push");

  if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
    // iOS espone il Push solo alle web app aggiunte alla schermata Home.
    btn.disabled = true;
    hint(isIOS && !standalone
      ? "Su iPhone le notifiche funzionano solo dopo «Condividi → Aggiungi a Home». Apri l'app da lì e riprova."
      : "Questo browser non supporta le notifiche push.", "bad");
    return;
  }
  if (isIOS && !standalone) {
    btn.disabled = true;
    hint("Aggiungi l'app alla schermata Home (Condividi → Aggiungi a Home), poi aprila da lì per attivare le notifiche.", "bad");
    return;
  }
  if (Notification.permission === "denied") {
    btn.disabled = true;
    hint("Notifiche bloccate nelle impostazioni del browser.", "bad");
    return;
  }

  const reg = await navigator.serviceWorker.ready;
  const sub = await reg.pushManager.getSubscription();
  initPanel(!!sub);
  if (sub) {
    btn.textContent = "Disattiva le notifiche su questo dispositivo";
    btn.classList.remove("primary");
    hint("Notifiche attive su questo dispositivo.", "ok");
  } else {
    btn.textContent = "Attiva notifiche push";
    btn.classList.add("primary");
    hint("");
  }
  btn.disabled = false;
}

$("btn-push").onclick = async () => {
  const btn = $("btn-push");
  btn.disabled = true;
  try {
    const reg = await navigator.serviceWorker.ready;
    const existing = await reg.pushManager.getSubscription();

    if (existing) {
      if (!confirm("Disattivare le notifiche su questo dispositivo?\n" +
                   "Non riceverai piu' gli avvisi di blackout qui.")) {
        await updatePushButton();
        return;
      }
      await api("/api/unsubscribe", {
        method: "POST", body: JSON.stringify({ endpoint: existing.endpoint }),
      });
      await existing.unsubscribe();
      hint("Notifiche disattivate su questo dispositivo.");
    } else {
      // requestPermission deve partire da un gesto dell'utente: siamo dentro onclick.
      const perm = await Notification.requestPermission();
      if (perm !== "granted") { hint("Permesso negato.", "bad"); await updatePushButton(); return; }

      const status = last || await api("/api/status");
      if (!status.vapid_public_key) { hint("VAPID non configurato sul server.", "bad"); return; }

      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlB64ToUint8Array(status.vapid_public_key),
      });
      await api("/api/subscribe", {
        method: "POST",
        body: JSON.stringify({ subscription: sub.toJSON(), label: navigator.userAgent.slice(0, 80) }),
      });
      hint("Notifiche attive su questo dispositivo.", "ok");
    }
  } catch (e) {
    hint("Errore: " + e.message, "bad");
  }
  await updatePushButton();
  refresh();
};

$("btn-test").onclick = async () => {
  const btn = $("btn-test");
  btn.disabled = true;
  try {
    const r = await api("/api/test", { method: "POST" });
    hint(r.ok ? "Notifica di prova inviata." : "Nessun canale ha accettato la notifica.", r.ok ? "ok" : "bad");
  } catch (e) {
    hint("Errore: " + e.message, "bad");
  }
  btn.disabled = false;
};

// ---------- pannello notifiche richiudibile ----------
// Sta chiuso di default quando le notifiche sono gia' attive: e' allora che i
// bottoni servono di rado e fanno danno se premuti per sbaglio.
const COLLAPSE_KEY = "pw_push_collapsed";

function setPanel(open) {
  $("push-head").setAttribute("aria-expanded", open ? "true" : "false");
  try { localStorage.setItem(COLLAPSE_KEY, open ? "0" : "1"); } catch {}
}

$("push-head").onclick = () => {
  setPanel($("push-head").getAttribute("aria-expanded") !== "true");
};

function initPanel(subscribed) {
  let stored = null;
  try { stored = localStorage.getItem(COLLAPSE_KEY); } catch {}
  const open = stored === null ? !subscribed : stored === "0";
  $("push-head").setAttribute("aria-expanded", open ? "true" : "false");
  $("push-summary").textContent = subscribed ? "attive su questo dispositivo" : "non attive qui";
}

// ---------- statistiche di uptime ----------
let statsData = null;
let statsWindow = "30g";

function renderStats() {
  if (!statsData) return;
  const w = statsData.windows[statsWindow];
  if (!w) return;

  $("stats-tiles").innerHTML = `
    <div><span>Uptime</span><b class="big">${w.uptime_pct.toFixed(3)}%</b></div>
    <div><span>Blackout</span><b class="big">${w.outages}</b></div>
    <div><span>Tempo senza corrente</span><b>${w.downtime ? human(w.downtime) : "nessuno"}</b></div>
    <div><span>Il più lungo</span><b>${w.longest ? human(w.longest) : "—"}</b></div>`;

  $("stats-note").textContent = w.partial
    ? `Monitoraggio attivo da ${human(Date.now() / 1000 - statsData.monitoring_since)}: `
      + `la finestra di ${w.days} giorni non è ancora piena, `
      + `la percentuale è calcolata solo sul tempo osservato.`
    : "";
  $("stats-note").className = "hint";
}

function renderStrip() {
  if (!statsData) return;
  // Soglie invece di un gradiente: a colpo d'occhio conta "quanto", non il valore esatto.
  const cls = (s) => s === 0 ? "" : s < 300 ? "d1" : s < 3600 ? "d2" : "d3";
  $("stats-strip").innerHTML = statsData.daily.map((d) =>
    `<i class="${cls(d.down)}" title="${d.date} — ${d.down ? human(d.down) + " senza corrente" : "nessun blackout"}"></i>`
  ).join("");
}

document.querySelectorAll("#stats-range button").forEach((b) => {
  b.onclick = () => {
    document.querySelectorAll("#stats-range button").forEach((x) => x.classList.remove("on"));
    b.classList.add("on");
    statsWindow = b.dataset.w;
    renderStats();
  };
});

async function refreshStats() {
  try {
    statsData = await api("/api/stats");
    renderStats();
    renderStrip();
  } catch (e) { /* la dashboard resta utile anche senza statistiche */ }
}

// ---------- avvio ----------
(async () => {
  // Stato di partenza sensato anche se il push non e' disponibile
  // (browser senza service worker, iOS fuori dalla Home): il pannello resta
  // aperto, che e' giusto quando c'e' ancora qualcosa da configurare.
  initPanel(false);

  if ("serviceWorker" in navigator) {
    try {
      await navigator.serviceWorker.register("/sw.js");
      await updatePushButton();
    } catch (e) {
      hint("Service worker non registrato: " + e.message, "bad");
    }
  } else {
    await updatePushButton();
  }
  await refresh();
  await refreshStats();
  setInterval(refresh, 5000);
  setInterval(refreshStats, 60000);
  setInterval(tick, 1000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
})();
