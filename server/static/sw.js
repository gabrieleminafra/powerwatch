/* Powerwatch — service worker: riceve le push e mostra la notifica.
   Nessuna cache: la dashboard deve mostrare dati freschi, non una copia. */

self.addEventListener("install", (e) => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let data = { title: "Powerwatch", body: "Aggiornamento stato corrente", tag: "power" };
  try {
    if (event.data) data = Object.assign(data, event.data.json());
  } catch {
    if (event.data) data.body = event.data.text();
  }

  event.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      tag: data.tag,              // le notifiche con lo stesso tag si sostituiscono
      renotify: true,
      icon: "/static/icons/icon-192.png",
      badge: "/static/icons/icon-192.png",
      timestamp: Date.now(),
      data: { url: "/" },
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil((async () => {
    const all = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const c of all) {
      if ("focus" in c) return c.focus();
    }
    if (self.clients.openWindow) return self.clients.openWindow("/");
  })());
});
