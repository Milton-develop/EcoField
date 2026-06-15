# EcoField Logger

**A Progressive Web App (PWA) for offline-first field data collection**  
Built for the Department of Animal Biology and Conservation Science, University of Ghana — School of Biological Sciences.

---

## 📋 Table of Contents

- [Overview](#overview)
- [Features](#features)
- [Technology Stack](#technology-stack)
- [Progressive Web App (PWA)](#progressive-web-app-pwa)
  - [What is a PWA?](#what-is-a-pwa)
  - [How Offline Works](#how-offline-works)
  - [Service Worker](#service-worker)
  - [IndexedDB Storage](#indexeddb-storage)
  - [Background Sync](#background-sync)
  - [Install to Home Screen](#install-to-home-screen)
- [Project Structure](#project-structure)
- [Installation & Setup](#installation--setup)
- [Environment Variables](#environment-variables)
- [Deployment](#deployment)
- [How to Use](#how-to-use)
- [Field Use Guide for Students](#field-use-guide-for-students)
- [Admin Guide](#admin-guide)
- [Database Schema](#database-schema)
- [Offline Data Flow](#offline-data-flow)
- [Known Limitations](#known-limitations)
- [Contributing](#contributing)
- [License](#license)

---

## Overview

EcoField Logger is a field data collection platform designed for university students conducting ecological surveys in groups. It is built to work reliably in **low-connectivity or zero-connectivity environments** — which are common in field research locations such as Shai Hills, Achimota Forest, and the University of Ghana campus.

The core problem it solves: field data collection is frequently disrupted by poor internet connectivity and unstructured data entry. EcoField Logger solves this by:

- Allowing students to **collect and store data fully offline**
- **Automatically syncing** all collected data to the central database once a connection is available
- Organizing data by **student groups** for easy retrieval and download
- Working as an **installable app** on any Android or iOS device — no app store required

---

## Features

### Student Features
- 📝 **Multi-step data collection form** (5 steps covering all ecological variables)
- 📍 **GPS coordinate capture** directly from the device
- 📸 **Photo upload** — file upload or live camera capture
- 🌿 **Species selection** from a structured species database by location and survey type
- 🔬 **Survey method selection** per species entry with location-specific method libraries (Drone Transect, Sweep Net, Camera Trap, etc.)
- 🆕 **New species logging** for species not yet in the database
- 📶 **Offline-first** — works with no internet connection
- 🔄 **Auto-sync** — data syncs to Supabase automatically when back online
- 📲 **Installable PWA** — add to Home Screen for a native app experience

### Group Features
- 🔐 **Group login** with Group ID and password
- 📊 **View all group submissions** in a structured table (species, method, count per row)
- 📥 **Download group data** as a CSV file
- 🗑️ **Delete individual entries**

### Admin Features
- 🔒 **Admin login** with secure password
- 👥 **Manage student groups** — create with student rosters and delete groups
- 📦 **Archive data** — export all observations to Supabase JSONB and clear the database
- 📁 **View, download, and delete** archived datasets
- 🔑 **Change admin password**
- 🆕 **View new species** logged by all groups
- 📢 **Send broadcast notifications** to all student groups with read tracking
- 📋 **Manage issue reports** — view, update status (open/resolved/closed), download
- 📊 **Admin dashboard** with system-wide statistics (observations, groups, students, photos)

---

## Technology Stack

| Layer | Technology |
|---|---|
| Backend | Python 3, Flask |
| Database | Supabase (PostgreSQL) |
| Frontend | HTML5, CSS3, Vanilla JavaScript |
| PWA | Service Worker API, IndexedDB, Web App Manifest |
| File Storage | Local filesystem (`static/uploads/`) |
| Data Visualization | Streamlit (`eco_stats.py`) |
| Deployment | Render.com |

---

## Progressive Web App (PWA)

This is the most important part of EcoField Logger. The entire offline capability is built on three PWA technologies working together: the **Service Worker**, **IndexedDB**, and **Background Sync**.

### What is a PWA?

A Progressive Web App is a website that behaves like a native mobile app. It can be:
- **Installed** on a phone's Home Screen without an app store
- **Opened** like a native app with no browser bar
- **Used offline** with no internet connection
- **Synced** automatically when connectivity returns

EcoField Logger is a PWA, which means once a student installs it on their phone before going to the field, it will work completely without internet for the entire duration of the field trip.

---

### How Offline Works

The offline system has three layers:

```
LAYER 1 — Service Worker (sw.js)
    Intercepts all network requests
    Serves cached pages when offline
    Caches static assets on install

LAYER 2 — IndexedDB (in form.html script)
    Stores form submissions locally on the device
    Holds photos as base64 encoded strings
    Tracks which records have been synced

LAYER 3 — Background Sync (sw.js + form.html)
    Automatically triggers upload when connection returns
    Retries failed uploads
    Works even if the app is closed (on Android)
```

**The complete offline journey:**

```
Student has internet → visits app → SW installs and caches everything
                                              ↓
                              Student goes to field (no internet)
                                              ↓
                         Opens app from Home Screen → loads from cache ✅
                                              ↓
                              Fills form → clicks Submit
                                              ↓
                    JS detects offline → saves to IndexedDB locally ✅
                         "📥 Saved offline! Will sync when back online"
                                              ↓
                            Student returns to school (internet)
                                              ↓
                    Background Sync fires → syncToServer() runs
                                              ↓
                    Each saved record POSTed to Flask → inserted to Supabase ✅
                         Record marked as synced in IndexedDB ✅
```

---

### Service Worker

**File:** `static/sw.js`  
**Registered at:** `/sw.js` (served by Flask with correct scope headers)

The Service Worker is a background JavaScript file that runs separately from the main page. It acts as a **network proxy** — intercepting every request the app makes and deciding whether to serve it from cache or fetch it from the network.

#### What it caches on install:
```javascript
const urlsToCache = [
  '/form',                        // main data collection page
  '/home',                        // home page
  '/static/css/style.css',        // styles
  '/static/manifest.json',        // PWA manifest
  '/static/data.json',            // species database (CRITICAL for offline dropdowns)
  '/static/images/about.jpg',
  '/static/images/header.jpg',
  '/static/images/icon-192x192.png',
  '/static/images/icon-512x512.png'
];
```

#### Fetch strategy:
- **Static assets** (`/static/`): Cache first, fall back to network
- **POST requests**: Never intercepted — passed directly to Flask
- **Navigation when offline**: Serves cached `/form` page
- **Dynamic pages** (`/group`, `/admin`, `/view_group`): Always fetched from network (bypassed)

#### Why `/sw.js` is served by Flask and not directly:
Service Workers have a **scope restriction** — a SW file can only control pages within its own directory. Since the file lives in `/static/sw.js`, it would normally only control `/static/` pages. Flask serves it from the root `/sw.js` route with a special header:

```python
response.headers['Service-Worker-Allowed'] = '/'
response.headers['Cache-Control'] = 'no-cache'
```

This allows the SW to control all pages of the app including `/form`, `/home`, etc.

#### Cache versioning:
The cache is named `ecofield-cache-v3`. When the SW is updated, bumping this version number causes the old cache to be deleted and all assets to be re-downloaded fresh.

---

### IndexedDB Storage

**Defined in:** `templates/form.html` (inline script)  
**Database name:** `EcoFieldDB`  
**Object store:** `submissions`

IndexedDB is a browser-based NoSQL database built into every modern browser. It is used instead of `localStorage` because:
- It can store **much larger amounts of data** (hundreds of MB vs ~5MB)
- It supports **structured data** including nested objects
- It can store **binary data** (photos as base64)
- It is **asynchronous** and doesn't block the UI

#### Record structure stored in IndexedDB:
```javascript
{
  id: 1,                          // auto-incremented
  synced: 0,                      // 0 = unsynced, 1 = synced
  savedAt: "2026-01-15T10:30:00", // timestamp of local save

  // Step 1 — Researcher Info
  year_group: "2026",
  group_id: "GROUP_A",
  member_name: "John Doe",
  student_id: "10987654",

  // Step 2 — Location
  location: "Near the large river bank",
  survey_type: "Biodiversity",
  latitude: "5.650000",
  longitude: "-0.186700",
  photos: [                       // array of base64 encoded images
    { name: "photo1.jpg", data: "data:image/jpeg;base64,..." }
  ],

  // Step 3 — Species
  site_location: "Shai Hills",
  species_entries: [
    { species: "Panthera leo", count: "2", method: "Driving Line Transect" }
  ],
  new_species_entries: [
    { species: "Unknown Lizard", count: "1", method: "Visual and Acoustic Encounter" }
  ],
  habitat: "Wooded Grassland",

  // Step 4 — Microhabitat
  temperature: "32",
  humidity: "78",
  rainfall: "0",
  wind_speed: "2.5",
  wind_direction: "180",

  // Step 5 — More Microhabitat
  light_intensity: "45000",
  canopy_cover: "60",
  canopy_height: "12",
  notes: "Observed near the watering hole"
}
```

#### Key functions:
```javascript
openDB()              // opens/creates the IndexedDB database
saveToLocalDB(data)   // saves a new unsynced record
getUnsyncedRecords()  // retrieves all records where synced === false
markSynced(id)        // updates a record's synced flag to true
```

---

### Background Sync

**Registered tag:** `sync-ecofield`

Background Sync is a browser API that allows the app to defer actions until the device has a stable internet connection. When a student submits the form offline:

1. The record is saved to IndexedDB
2. A sync tag `sync-ecofield` is registered with the SW
3. When the browser detects connectivity, it fires a `sync` event on the SW
4. The SW sends a `SYNC_NOW` message to all open tabs
5. The tab runs `syncToServer()` which uploads all unsynced records

```javascript
// In sw.js — fires when connection returns
self.addEventListener('sync', (event) => {
  if (event.tag === 'sync-ecofield') {
    event.waitUntil(
      self.clients.matchAll().then(clients => {
        clients.forEach(client => client.postMessage({ type: 'SYNC_NOW' }));
      })
    );
  }
});
```

**iOS Safari note:** Background Sync is not supported on iOS Safari. EcoField Logger handles this with a fallback — the `online` event listener fires `syncToServer()` directly when the device reconnects, which achieves the same result as long as the app is open.

---

### Install to Home Screen

EcoField Logger is fully installable as a PWA on both Android and iOS.

**Web App Manifest** (`static/manifest.json`) defines the app's appearance when installed:
- App name and short name
- Icons (192x192 and 512x512)
- Theme color and background color
- Display mode: `standalone` (no browser bar)
- Start URL: `/form` (opens directly to the form)

#### Installing on Android (Chrome):
```
1. Open the app URL in Chrome
2. Tap the three-dot menu (⋮)
3. Tap "Add to Home Screen"
4. Tap "Install"
5. App icon appears on Home Screen ✅
```

Or tap the **"📲 Install App for Offline Use"** button that appears automatically in the navbar when the app is installable.

#### Installing on iPhone (Safari):
```
1. Open the app URL in Safari (must be Safari, not Chrome)
2. Tap the Share button (box with arrow)
3. Scroll down and tap "Add to Home Screen"
4. Tap "Add"
5. App icon appears on Home Screen ✅
```

Once installed, the app opens in standalone mode with no browser bar and works completely offline.

---

## Project Structure

```
EcoField Logger/
├── app.py                    # Flask application — all routes and logic
├── eco_stats.py              # Streamlit data visualization dashboard
├── requirements.txt          # Python dependencies
├── Procfile                  # Deployment start command for Render
├── README.md                 # This file
├── .gitignore                # Files excluded from Git
│
├── static/
│   ├── css/
│   │   ├── style.css         # Core styles
│   │   ├── form.css          # Form-specific styles
│   │   ├── group.css         # Group data table styles
│   │   ├── group_login.css   # Group login styles
│   │   ├── admin.css         # Admin interface styles
│   │   ├── index.css         # Home page styles
│   │   ├── faq.css           # FAQ page styles
│   │   ├── help.css          # Help page styles
│   │   ├── profile.css       # Profile page styles
│   │   └── manage_groups.css # Group management styles
│   ├── images/
│   │   ├── about.jpg         # Home page image
│   │   ├── header.jpg        # Header background
│   │   ├── milton.png        # Developer profile image
│   │   ├── Profile.jpg       # Profile page image
│   │   ├── icon-192x192.png  # PWA icon (small)
│   │   └── icon-512x512.png  # PWA icon (large)
│   ├── uploads/              # Student uploaded field photos
│   ├── data.json             # Species database by location, survey type, and methods
│   ├── manifest.json         # PWA web app manifest
│   └── sw.js                 # Service Worker — offline caching and sync
│
├── templates/
│   ├── _admin_sidebar.html   # Admin sidebar partial
│   ├── _notification_bell.html # Notification bell partial
│   ├── index.html            # Home page
│   ├── login.html            # Student/admin role selection
│   ├── form.html             # Main data collection form (5 steps) + offline JS
│   ├── group_login.html      # Group login page
│   ├── group.html            # Group data view
│   ├── admin_login.html      # Admin login
│   ├── admin_settings.html   # Admin password change
│   ├── dashboard.html        # Admin dashboard with statistics
│   ├── manage_groups.html    # Admin — manage groups
│   ├── admin_groups.html     # Admin — view all groups
│   ├── archive.html          # Admin — view/manage archived datasets
│   ├── add_species.html      # Admin — view new species logged
│   ├── report.html           # Student issue reporting
│   ├── admin_reports.html    # Admin report management
│   ├── metrics.html          # Biodiversity analytics dashboard
│   ├── profile.html          # Developer profile
│   ├── faq.html              # FAQ page
│   └── help.html             # Help and instructions page
│
├── schema.sql                # Full Supabase schema (6+ tables)
├── package.json              # Node dependencies (Supabase JS)
│
└── data/
    ├── observations.csv      # Local backup (legacy)
    ├── groups.csv            # Local backup (legacy)
    └── archive/              # Archived CSV exports (legacy)
```

---

## Installation & Setup

### Prerequisites
- Python 3.9 or higher
- pip
- A Supabase account and project

### Local Setup

```bash
# 1. Clone the repository
git clone https://github.com/Milton-develop/EcoField.git
cd EcoField

# 2. Create and activate virtual environment
python -m venv .venv

# Windows
.venv\Scripts\activate

# Mac/Linux
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set environment variables (see below)

# 5. Run the app
python app.py
```

Open `http://localhost:5000` in your browser.

---

## Environment Variables

Never hardcode credentials. Set these as environment variables:

```bash
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your_publishable_key
SECRET_KEY=your_flask_secret_key
```

**For local development** — create a `.env` file in the project root:
```
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your_publishable_key
SECRET_KEY=supersecretkey123
```

**For Render deployment** — add these in the Render dashboard under Environment Variables.

---

## Deployment

EcoField Logger is deployed on **Render.com** using the free tier.

### Deploy Steps

```
1. Push code to GitHub
2. Go to render.com → New → Web Service
3. Connect your GitHub repository
4. Configure:
      Name:           ecofield-logger
      Environment:    Python
      Build Command:  pip install -r requirements.txt
      Start Command:  gunicorn app:app
5. Add Environment Variables (SUPABASE_URL, SUPABASE_KEY, SECRET_KEY)
6. Click Deploy
```

The app will be live at `https://ecofield-logger.onrender.com` (or your chosen name).

**Important:** HTTPS is required for the Service Worker to function on real devices. Render provides HTTPS automatically on all deployments.

---

## How to Use

### Student Login
1. Go to the app URL
2. Enter `student` as the username
3. You are redirected to the Home page

### Submitting Field Data
1. Click **"+ Create New Entry"**
2. Fill in all 5 steps:
   - **Step 1:** Your year group, group ID, name, and student ID
   - **Step 2:** Location description, survey type, GPS coordinates, photos
   - **Step 3:** Site location, species observed (with survey method per entry), habitat type
   - **Step 4:** Temperature, humidity, rainfall, wind speed and direction
   - **Step 5:** Light intensity, canopy cover, canopy height, notes
3. Click **Submit**
4. If online — data goes directly to Supabase
5. If offline — data is saved locally and syncs when back online

### Viewing Your Group's Data
1. Click **"View My Data"**
2. Enter your Group ID and password
3. View all your group's submissions
4. Download as CSV if needed

---

## Field Use Guide for Students

> ⚠️ **Read this before going to the field**

### Before Leaving for the Field

```
✅ Open the app on your phone while on WiFi or mobile data
✅ Click "📲 Install App for Offline Use" in the navbar
✅ OR: tap browser menu → "Add to Home Screen"
✅ Open the app once from the Home Screen icon to confirm it loads
✅ You are now ready for offline field use
```

### In the Field (No Internet)

```
✅ Open EcoField Logger from your Home Screen icon
✅ The red banner "🔴 Offline — your data will be saved locally" confirms offline mode
✅ Fill the form as normal across all 5 steps
✅ Tap GPS button to capture your exact coordinates (GPS works without internet)
✅ Take photos with the camera button
✅ Click Submit — you will see "📥 Saved offline! Will sync when back online"
✅ Repeat for each observation
```

### Back at School (Internet Available)

```
✅ Open the app — the green banner "🟢 Online — saved records will sync now" appears
✅ All offline records automatically upload to the database
✅ Go to "View My Data" to confirm your submissions are there
```

### Important Notes for Students

- **Install the app before the field trip** — you cannot install it without internet
- **Multiple submissions are fine** — each form submission is saved as a separate record
- **Don't clear your browser data** before syncing — this will delete unsynchronized records
- **GPS works offline** — it uses your device's hardware GPS, not the internet
- **Photos are stored locally** until sync — large photos may use significant device storage

---

## Admin Guide

### Admin Login
```
Go to app URL → enter "admin" → enter admin password
Default password: admin123 (change immediately)
```

### Admin Dashboard
The dashboard at `/admin/dashboard` displays:
- Total observations, groups, students, and photos uploaded
- New species count and list
- Archive count
- Notification broadcast form
- Recent sent notifications with delete capability

### Managing Groups
1. Go to **Admin → Manage Groups**
2. Enter admin password to create a new group
3. Each group needs a unique Group ID and password
4. Optionally add student roster (name + ID pairs)
5. Share the Group ID and password with the student group
6. **View Groups** shows all registered groups with student details
7. Groups can be deleted from the view groups page

### Broadcasting Notifications
1. From the dashboard, fill in **Title** and **Message**
2. Click **Send Notification**
3. All student groups see the notification with an unread badge
4. Read status is tracked per group

### Managing Issue Reports
1. Go to **Admin → Reports**
2. View all student-submitted reports with category, subject, and description
3. Update status: **open → resolved → closed**
4. Download all reports as a text file

### Archiving Data
At the end of each academic year or field season:
1. Go to **Admin → Archive Data**
2. Enter admin password to confirm
3. All observations are archived to Supabase JSONB with an academic year label
4. The observations table is cleared for the new season
5. Archives can be **viewed, downloaded, or deleted** from the archives page

### Viewing New Species
Go to **Admin → New Species** to see all species that students have logged manually (not in the official species database). These can be reviewed and added to `data.json` for future field trips.

### Changing Admin Password
Go to **Admin → Settings** to change the admin password. Requires current password verification.

---

## Database Schema

### `observations` table (Supabase)

| Column | Type | Description |
|---|---|---|
| `id` | int | Auto-increment primary key |
| `year_group` | text | Student's year group |
| `group_id` | text | Student group identifier |
| `member_name` | text | Student's name |
| `student_id` | text | Student ID number |
| `location` | text | Text description of location |
| `survey_type` | text | Type of ecological survey |
| `latitude` | text | GPS latitude |
| `longitude` | text | GPS longitude |
| `site_location` | text | Named site (Shai Hills, etc.) |
| `species_list` | text | Comma-separated species names |
| `count_list` | text | Comma-separated species counts |
| `method_list` | text | Comma-separated survey methods per species |
| `species_manual` | text | Manually entered new species |
| `count_manual` | text | Counts for new species |
| `method_manual` | text | Survey methods for manually entered species |
| `habitat` | text | Habitat type |
| `temperature` | text | Temperature in °C |
| `humidity` | text | Humidity in % |
| `rainfall` | text | Rainfall in mm |
| `wind_speed` | text | Wind speed in m/s |
| `wind_direction` | text | Wind direction in degrees |
| `light_intensity` | text | Light intensity in lux |
| `canopy_cover` | text | Canopy cover in % |
| `canopy_height` | text | Canopy height in m |
| `notes` | text | Additional observations |
| `photo_files` | text | Semicolon-separated photo filenames |
| `timestamp` | text | Submission datetime |

### `manage_groups` table (Supabase)

| Column | Type | Description |
|---|---|---|
| `id` | int | Auto-increment primary key |
| `group_id` | text | Unique group identifier |
| `password` | text | Group login password |

### `manage_groups` table (Supabase) — extended

| Column | Type | Description |
|---|---|---|
| `student_id` | text | Student ID number |
| `student_name` | text | Student full name |

### `admin_settings` table (Supabase)

| Column | Type | Description |
|---|---|---|
| `setting_key` | text | Setting name (e.g. `admin_password`) |
| `setting_value` | text | Setting value |

### `notifications` table (Supabase)

| Column | Type | Description |
|---|---|---|
| `id` | int | Auto-increment primary key |
| `title` | text | Notification title |
| `message` | text | Notification body |
| `created_at` | text | Timestamp |

### `notification_reads` table (Supabase)

| Column | Type | Description |
|---|---|---|
| `id` | int | Auto-increment primary key |
| `notification_id` | bigint | Reference to notifications table |
| `group_id` | text | Group that read the notification |
| `read_at` | text | Read timestamp |

### `archives` table (Supabase)

| Column | Type | Description |
|---|---|---|
| `id` | int | Auto-increment primary key |
| `academic_year` | text | Academic year label |
| `filename` | text | Archive filename |
| `archived_at` | text | Archive timestamp |
| `data` | jsonb | Full observation data payload |
| `record_count` | integer | Number of records archived |

### `reports` table (Supabase)

| Column | Type | Description |
|---|---|---|
| `id` | int | Auto-increment primary key |
| `reporter_name` | text | Student name |
| `student_id` | text | Student ID |
| `group_id` | text | Student group |
| `category` | text | Report category |
| `subject` | text | Report subject |
| `description` | text | Report details |
| `status` | text | open / resolved / closed |
| `created_at` | text | Submission timestamp |

---

## Offline Data Flow

```
┌─────────────────────────────────────────────────────────┐
│                    STUDENT DEVICE                        │
│                                                          │
│  ┌──────────┐    Submit     ┌─────────────────────────┐ │
│  │   Form   │ ────────────▶ │   Online Check          │ │
│  │ (5 steps)│               │   navigator.onLine      │ │
│  └──────────┘               └──────────┬──────────────┘ │
│                                        │                  │
│                              ┌─────────┴──────────┐      │
│                           Online?              Offline?   │
│                              │                    │       │
│                    ┌─────────▼──────┐   ┌────────▼─────┐│
│                    │ POST to Flask  │   │  Save to     ││
│                    │ /form directly │   │  IndexedDB   ││
│                    └─────────┬──────┘   └────────┬─────┘│
│                              │                    │       │
│                    ┌─────────▼──────┐   ┌────────▼─────┐│
│                    │   Supabase     │   │ Register     ││
│                    │   Insert ✅    │   │ Background   ││
│                    └────────────────┘   │ Sync Tag     ││
│                                         └────────┬─────┘│
└─────────────────────────────────────────────────┼───────┘
                                                   │
                                    Connection returns
                                                   │
                                    ┌──────────────▼──────┐
                                    │  SW fires sync event │
                                    │  → SYNC_NOW message  │
                                    │  → syncToServer()    │
                                    │  → POST each record  │
                                    │  → markSynced(id)    │
                                    └──────────────────────┘
```

---

## Known Limitations

- **iOS Background Sync:** Apple does not support the Background Sync API on iOS Safari. The app uses the `online` event as a fallback, which works as long as the app is open when connectivity returns.
- **Render Free Tier Sleep:** The free tier on Render spins down after 15 minutes of inactivity. The first request after sleep takes ~30 seconds. Upgrade to a paid plan for production use.
- **Photo Storage:** Photos are stored on the server's local filesystem. On Render's free tier, the filesystem is ephemeral (resets on redeploy). For production, photos should be uploaded to Supabase Storage or an S3 bucket.
- **Offline Photos:** Photos stored offline are encoded as base64 strings in IndexedDB, which is ~33% larger than the original file size. Very large photos may approach IndexedDB storage limits on older devices.
- **Survey Methods — Location-Limited:** Survey method libraries are currently fully defined only for **Shai Hills**. University of Ghana and Achimota Forest have empty method stubs ready for population.
- **Herpetofauna Species:** The Herpetofauna survey type exists in the form dropdown but has no species entries in `data.json` yet — only methods are defined for it.

---

## Contributing

This project was built for the Department of Animal Biology and Conservation Science, University of Ghana. For contributions or bug reports, please open an issue or pull request on GitHub.

---

## License

Built by **MiltonPixel** for the Department of Animal Biology and Conservation Science, University of Ghana.  
© 2026 EcoField Logger. All rights reserved.
