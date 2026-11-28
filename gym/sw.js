const SCOPE_SLUG = new URL(self.registration.scope).pathname.replace(/[^a-zA-Z0-9_-]/g, '_');
const CACHE_PREFIX = 'antigravity-gym-shell-' + SCOPE_SLUG + '-';
const CACHE_NAME = CACHE_PREFIX + 'v4';
const REQUIRED_SHELL = ['./', './index.html'];
const OPTIONAL_ASSETS = ['./holo.html', './apple-touch-icon.png', './icon-192.png', './icon-512.png', './manifest.json'];

async function cacheStrict(cache, url) {
  const res = await fetch(url, { cache: 'no-cache' });
  if (!res || !res.ok) throw new Error('PRECACHE_FAIL:' + url);
  await cache.put(url, res);
}

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE_NAME).then(async (cache) => {
      await Promise.all(REQUIRED_SHELL.map((u) => cacheStrict(cache, u)));
      await Promise.allSettled(OPTIONAL_ASSETS.map((u) => cacheStrict(cache, u)));
      await self.skipWaiting();
    })
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k.startsWith(CACHE_PREFIX) && k !== CACHE_NAME).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

function fetchTimeout(req, ms) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), ms);
  return fetch(req, { cache: 'no-store', signal: ctrl.signal }).finally(() => clearTimeout(t));
}

self.addEventListener('fetch', (e) => {
  if (e.request.method !== 'GET') return;
  const u = new URL(e.request.url);
  if (u.origin !== self.location.origin || u.pathname.endsWith('/beacon.json') || u.pathname.startsWith('/api/')) {
    return;
  }
  const isNav = e.request.mode === 'navigate' || u.pathname.endsWith('/') || u.pathname.endsWith('/index.html') || u.pathname.endsWith('/holo.html');
  if (isNav) {
    e.respondWith(
      fetchTimeout(e.request, 4500)
        .then((res) => {
          if (!res || !res.ok) throw new Error('HTTP_' + (res ? res.status : 0));
          const c = res.clone();
          e.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.put(e.request, c)).catch(() => {}));
          return res;
        })
        .catch(() => caches.open(CACHE_NAME).then((cache) => cache.match(e.request).then((m) => m || cache.match('./index.html'))))
    );
    return;
  }
  e.respondWith(
    caches.open(CACHE_NAME).then((cache) =>
      cache.match(e.request).then((cached) => {
        const net = fetchTimeout(e.request, 3500)
          .then((res) => (res && res.ok ? cache.put(e.request, res.clone()).catch(() => {}).then(() => res) : (cached || res)))
          .catch(() => cached);
        if (cached) { e.waitUntil(net); return cached; }
        return net;
      })
    )
  );
});
