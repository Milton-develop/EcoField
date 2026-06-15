// ── 1. CACHE CONSTANTS ─────────────────────────────────────────
const CACHE_NAME = 'ecofield-cache-v6';
const urlsToCache = [
  '/',
  '/form',    
  '/home',                          // ← ADDED: cache the form page for offline use
  '/static/css/style.css',
  '/static/manifest.json',
  '/static/data.json',              // ← ADDED: needed for species dropdown offline
  '/static/images/header.jpg',
  '/static/images/icon-192x192.png',
  '/static/images/icon-512x512.png'
];

// ── 2. INDEXEDDB CONSTANTS ─────────────────────────────────────
const DB_NAME = 'EcoFieldDB';
const STORE = 'submissions';

// ── 3. INDEXEDDB FUNCTIONS ─────────────────────────────────────
function openDB() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 3); 
    req.onupgradeneeded = (e) => {
      const db = e.target.result;
      if (!db.objectStoreNames.contains(STORE)) {
        const store = db.createObjectStore(STORE, { keyPath: 'id', autoIncrement: true });
        store.createIndex('synced', 'synced', { unique: false });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function getUnsyncedRecords() {
  const db = await openDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, 'readonly');
    const store = tx.objectStore(STORE);
    const index = store.index('synced');
    const req = index.getAll(0);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function markSynced(id) {
  const db = await openDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, 'readwrite');
    const store = tx.objectStore(STORE);
    const req = store.get(id);
    req.onsuccess = () => {
      const record = req.result;
      if (record) {
        record.synced = 1;
        store.put(record);
      }
    };
    tx.oncomplete = resolve;
    tx.onerror = () => reject(tx.error);
  });
}

// ── 4. SYNC FUNCTION ───────────────────────────────────────────
let swSyncInProgress = false;

async function notifyClients(statusType, text) {
  const clientsList = await self.clients.matchAll({
    includeUncontrolled: true,
    type: 'window'
  });

  clientsList.forEach(client => {
    client.postMessage({
      type: 'SYNC_STATUS',
      statusType,
      text
    });
  });
}

async function syncToServerSW() {
  if (swSyncInProgress) return;
  swSyncInProgress = true;

  const records = await getUnsyncedRecords();
  if (records.length === 0) {
    swSyncInProgress = false;
    return;
  }

  console.log(`[SW] Syncing ${records.length} offline record(s)...`);
  await notifyClients('warning', `Syncing ${records.length} offline record(s)...`);

  let successCount = 0;

  for (const record of records) {
    try {
      const formData = new FormData();
      Object.entries(record).forEach(([key, val]) => {
        if (key === 'photos') {
          val.forEach((photo, i) => formData.append(`offline_photo_${i}`, photo.data));
        } else if (key === 'species_entries' || key === 'new_species_entries') {
          formData.append(key, JSON.stringify(val));
        } else {
          if (key !== 'id' && key !== 'synced' && key !== 'savedAt') {
            formData.append(key, val);
          }
        }
      });
      formData.append('offline_sync', 'true');

      const res = await fetch('/form', { method: 'POST', body: formData });
      if (res.ok) {
        await markSynced(record.id);
        successCount++;
        console.log(`[SW] Record ${record.id} synced successfully!`);
      } else {
        console.warn(`[SW] Record ${record.id} sync failed with status ${res.status}.`);
        await notifyClients('error', `Sync failed for one offline record. Server returned ${res.status}.`);
      }
    } catch (err) {
      console.warn(`[SW] Record ${record.id} sync failed (network error).`);
      await notifyClients('warning', 'Sync failed because the connection is not ready. It will retry later.');
      swSyncInProgress = false;
      throw err; 
    }
  }

  if (successCount > 0) {
    await notifyClients('success', `Successfully synced ${successCount} offline record(s).`);
  }

  swSyncInProgress = false;
}

// ── 5. INSTALL EVENT ───────────────────────────────────────────
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => {
        return cache.addAll(urlsToCache);
      })
  );
  self.skipWaiting(); // ← ADDED: activate SW immediately without waiting
});

// ── 6. ACTIVATE EVENT ──────────────────────────────────────────
self.addEventListener('activate', event => {
  const cacheWhitelist = [CACHE_NAME];
  event.waitUntil(
    caches.keys().then(cacheNames => {
      return Promise.all(
        cacheNames.map(cacheName => {
          if (cacheWhitelist.indexOf(cacheName) === -1) {
            return caches.delete(cacheName);
          }
        })
      );
    })
  );
  self.clients.claim(); // ← ADDED: take control of all open pages immediately
});

// ── 7. FETCH EVENT ─────────────────────────────────────────────
self.addEventListener('fetch', event => {
  // 1. Only cache GET requests
  if (event.request.method !== 'GET') {
    return;
  }

  // 2. Only handle http/https requests - ignore chrome-extension:// etc
  if (!event.request.url.startsWith('http://') && 
      !event.request.url.startsWith('https://')) {
    return;
  }

  // 3. DO NOT cache these dynamic data paths
  const url = new URL(event.request.url);
  const bypassCachePaths = [
    '/api/',
    '/group', 
    '/view_group',
    '/admin',
    '/manage_groups',
    '/add_species',
    '/delete_entry'
  ];
  
  if (bypassCachePaths.some(path => url.pathname.includes(path))) {
    return; 
  }

  event.respondWith(
    caches.match(event.request)
      .then(response => {
        if (response && url.pathname !== '/') {
          return response;
        }

        return fetch(event.request).then(
          function(response) {
            if(!response || response.status !== 200) {
              return response;
            }

            var responseToCache = response.clone();

            caches.open(CACHE_NAME)
              .then(function(cache) {
                cache.put(event.request, responseToCache);
              });

            return response;
          }
        ).catch(() => {
          // ← ADDED: if offline and navigating, serve the cached /form page
          if (event.request.mode === 'navigate') {
            return caches.match('/form');
          }
          return new Response('Offline', { status: 503, statusText: 'Service Unavailable' });
        });
      })
  );
});

// ── 8. BACKGROUND SYNC EVENT ───────────────────────────────────
self.addEventListener('sync', (event) => {
  if (event.tag === 'sync-ecofield') {
    console.log('[SW] Background sync event triggered');
    event.waitUntil(syncToServerSW());
  }
});
