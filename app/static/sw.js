/*
 * app/static/sw.js  (served as /sw.js so it controls the whole site)
 * -----------------------------------------------------------------
 * Offline support for STUDENTS (read-only):
 *   - Student pages (/student...): network first; if offline, the last
 *     copy saved in the cache is shown, else /offline.html.
 *   - Files (/files/<id>): served from the "Save offline" cache when present.
 *   - CSS/JS/icons (/static/...): cache first (file names carry ?v= versions).
 * Staff pages are never cached. Logging out clears every cache
 * (app.js deletes them, and also sends {type: "clear"} to this worker).
 */
"use strict";

const STATIC_CACHE = "tpa-static-v1";
const PAGE_CACHE = "tpa-pages";
const FILE_CACHE = "tpa-files";
const PRECACHE = ["/offline.html", "/static/app.css"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE).then((cache) => cache.addAll(PRECACHE)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  // Remove old versions of the static cache, then take control of open pages.
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k.startsWith("tpa-static-") && k !== STATIC_CACHE).map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

function isStudentPage(url) {
  return url.pathname === "/student" || url.pathname.startsWith("/student/");
}

async function networkFirstStudentPage(request) {
  const cache = await caches.open(PAGE_CACHE);
  try {
    const response = await fetch(request);
    // Only keep real student pages (not a redirect to /login).
    if (response.ok && !response.redirected && response.type === "basic") {
      cache.put(request, response.clone());
    }
    return response;
  } catch (error) {
    const cached = await cache.match(request, { ignoreSearch: true });
    return cached || (await caches.match("/offline.html")) || Response.error();
  }
}

async function savedFileOrNetwork(request) {
  const cache = await caches.open(FILE_CACHE);
  const cached = await cache.match(request, { ignoreSearch: true });
  if (cached) return cached;
  return fetch(request);
}

async function cacheFirstStatic(request) {
  const cache = await caches.open(STATIC_CACHE);
  const cached = await cache.match(request);
  if (cached) return cached;
  const response = await fetch(request);
  if (response.ok) cache.put(request, response.clone());
  return response;
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (url.pathname.startsWith("/static/")) {
    event.respondWith(cacheFirstStatic(request));
  } else if (url.pathname.startsWith("/files/")) {
    event.respondWith(savedFileOrNetwork(request));
  } else if (request.mode === "navigate") {
    if (isStudentPage(url)) {
      event.respondWith(networkFirstStudentPage(request));
    } else {
      event.respondWith(
        fetch(request).catch(async () => (await caches.match("/offline.html")) || Response.error())
      );
    }
  }
});

self.addEventListener("message", (event) => {
  if (event.data && event.data.type === "clear") {
    event.waitUntil(Promise.all([caches.delete(PAGE_CACHE), caches.delete(FILE_CACHE)]));
  }
});
