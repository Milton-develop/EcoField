const DB_NAME = 'EcoFieldDB';
const STORE = 'submissions';

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
    const req = tx.objectStore(STORE).index('synced').getAll(0);
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

async function globalSync() {
  if (!navigator.onLine) return;

  const records = await getUnsyncedRecords();
  if (records.length === 0) return;

  console.log(`[Global Sync] Found ${records.length} pending records...`);
  
  // Show a simple notification banner
  let banner = document.createElement('div');
  banner.style.cssText = "position:fixed; top:0; left:0; width:100%; background:#fff3cd; color:#856404; text-align:center; padding:10px; z-index:10000; font-weight:bold; border-bottom:1px solid #ffeeba;";
  banner.innerText = `🔄 Syncing ${records.length} offline records...`;
  document.body.appendChild(banner);

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
        console.log(`[Global Sync] Record ${record.id} synced successfully!`);
      }
    } catch (err) {
      console.error(`[Global Sync] Network error during sync:`, err);
    }
  }

  if (successCount > 0) {
    banner.style.background = "#d4edda";
    banner.style.color = "#155724";
    banner.innerText = `✅ Successfully synced ${successCount} offline records!`;
    setTimeout(() => banner.remove(), 5000);
  } else {
    banner.remove();
  }
}

// Auto-trigger sync on load and when connection returns
window.addEventListener('load', globalSync);
window.addEventListener('online', globalSync);
