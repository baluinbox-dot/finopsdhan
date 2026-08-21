// Minimal service worker — exists only so the app satisfies Chrome/Android's
// "installable PWA" criteria (manifest + a service worker that controls the
// start_url and registers a fetch handler). It deliberately does NOT cache
// anything.
//
// Why: this app already hit a real bug once from a browser silently serving
// a stale static JS/CSS file after a deploy (see app.templating.static_url,
// which cache-busts every static asset with a ?v=<mtime> query string
// specifically because of that). A caching service worker is the same bug
// class with a much bigger blast radius — it could serve stale HTML *and*
// JS/CSS indefinitely, even across a hard reload. Don't add caching here
// without solving cache invalidation on deploy first.

self.addEventListener("install", (event) => {
    self.skipWaiting();
});

self.addEventListener("activate", (event) => {
    event.waitUntil(
        caches.keys().then((names) => Promise.all(names.map((name) => caches.delete(name))))
    );
    self.clients.claim();
});

self.addEventListener("fetch", (event) => {
    // Pure passthrough — always go to the network, never serve from a cache.
    event.respondWith(fetch(event.request));
});
