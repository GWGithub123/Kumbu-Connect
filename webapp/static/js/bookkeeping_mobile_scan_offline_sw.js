const CACHE_NAME = 'kumbu-bookkeeping-mobile-scan-shell-v1';

function scopeUrl() {
  return new URL(self.registration.scope);
}

function normalizedScopePath() {
  const scope = scopeUrl();
  return scope.pathname.endsWith('/') ? scope.pathname.slice(0, -1) : scope.pathname;
}

function appUrl() {
  const scope = scopeUrl();
  return new URL(normalizedScopePath(), scope.origin).toString();
}

function shellUrls() {
  const scope = scopeUrl();
  return [
    appUrl(),
    new URL('/static/css/main.css', scope.origin).toString(),
    new URL('/static/js/bookkeeping_mobile_scan_offline_app.js', scope.origin).toString(),
  ];
}

async function cacheShell() {
  const cache = await caches.open(CACHE_NAME);
  for (const url of shellUrls()) {
    try {
      const response = await fetch(new Request(url, {
        cache: 'reload',
        credentials: 'same-origin',
      }));
      if (response && response.ok) {
        await cache.put(url, response.clone());
      }
    } catch (error) {
      /* Keep any existing cached shell assets when offline. */
    }
  }
}

async function cleanupOldCaches() {
  const names = await caches.keys();
  await Promise.all(names.filter(function (name) {
    return name !== CACHE_NAME;
  }).map(function (name) {
    return caches.delete(name);
  }));
}

async function networkFirst(request) {
  const cache = await caches.open(CACHE_NAME);
  try {
    const response = await fetch(request);
    if (response && response.ok) {
      await cache.put(request.url, response.clone());
    }
    return response;
  } catch (error) {
    return (await cache.match(request.url)) || (await cache.match(appUrl())) || Response.error();
  }
}

async function staleWhileRevalidate(request) {
  const cache = await caches.open(CACHE_NAME);
  const cached = await cache.match(request.url);
  const networkPromise = fetch(request).then(async function (response) {
    if (response && response.ok) {
      await cache.put(request.url, response.clone());
    }
    return response;
  }).catch(function () {
    return null;
  });

  if (cached) {
    return cached;
  }

  const networkResponse = await networkPromise;
  return networkResponse || Response.error();
}

self.addEventListener('install', function (event) {
  event.waitUntil(cacheShell().then(function () {
    return self.skipWaiting();
  }));
});

self.addEventListener('activate', function (event) {
  event.waitUntil((async function () {
    await cleanupOldCaches();
    await cacheShell();
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', function (event) {
  if (event.request.method !== 'GET') {
    return;
  }

  const requestUrl = new URL(event.request.url);
  const scope = scopeUrl();
  const scopePath = normalizedScopePath();
  if (requestUrl.origin !== scope.origin) {
    return;
  }

  const inScope = requestUrl.pathname === scopePath || requestUrl.pathname.startsWith(`${scopePath}/`);
  const isShellAsset = requestUrl.pathname === '/static/css/main.css' || requestUrl.pathname === '/static/js/bookkeeping_mobile_scan_offline_app.js';

  if (event.request.mode === 'navigate' && inScope) {
    event.respondWith(networkFirst(event.request));
    return;
  }

  if (isShellAsset) {
    event.respondWith(staleWhileRevalidate(event.request));
  }
});

self.addEventListener('sync', function (event) {
  if (event.tag !== 'bookkeeping-mobile-scan-sync') {
    return;
  }
  event.waitUntil((async function () {
    const clients = await self.clients.matchAll({
      type: 'window',
      includeUncontrolled: true,
    });
    for (const client of clients) {
      client.postMessage({ type: 'BOOKKEEPING_MOBILE_SCAN_SYNC' });
    }
  })());
});