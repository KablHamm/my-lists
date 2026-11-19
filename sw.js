/* My Lists service worker: caches the app shell so it opens offline, and shows reminder notifications.
   Data requests (fetch() calls to GitHub) are never touched; list data lives in the page's localStorage copy and the private repo. */
const CACHE = 'my-lists-1.3.0';   // keep in step with APP_VERSION in index.html
const SHELL = ['./', 'manifest.webmanifest', 'icons/icon-192.png', 'icons/icon-512.png', 'icons/apple-touch-icon.png'];
const FONT_HOSTS = ['fonts.googleapis.com', 'fonts.gstatic.com'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET' || req.destination === '') return;   // fetch()/XHR data calls: never cached
  const url = new URL(req.url);

  if (url.origin === location.origin) {
    // Network first so updates show up; fall back to the cached shell when offline.
    e.respondWith(fetch(req).then(res => {
      if (res.ok) { const copy = res.clone(); caches.open(CACHE).then(c => c.put(req.mode === 'navigate' ? './' : req, copy)); }
      return res;
    }).catch(() => caches.match(req.mode === 'navigate' ? './' : req, { ignoreSearch: true })));
    return;
  }

  if (FONT_HOSTS.includes(url.hostname)) {                // fonts: cache first, they never change
    e.respondWith(caches.match(req).then(hit => hit || fetch(req).then(res => {
      const copy = res.clone(); caches.open(CACHE).then(c => c.put(req, copy)); return res;
    })));
  }
});

/* Reminder pushes: the payload was encrypted for this device by the app; the browser has already decrypted it. */
self.addEventListener('push', e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (x) { d = {}; }
  e.waitUntil(self.registration.showNotification(d.title || 'My Lists', {
    body: d.body || 'You have a reminder',
    tag: d.tag || 'my-lists',
    icon: 'icons/icon-192.png',
    badge: 'icons/icon-192.png',
    data: { url: d.url || './' }
  }));
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  const target = new URL((e.notification.data && e.notification.data.url) || './', self.registration.scope).href;
  const task = new URL(target).searchParams.get('task');
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
    for (const c of list) {
      if (c.url.startsWith(self.registration.scope) && 'focus' in c) {
        if (task) c.postMessage({ type: 'open-task', task });
        return c.focus();
      }
    }
    return self.clients.openWindow(target);
  }));
});
