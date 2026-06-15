-- ============================================================
-- EcoField Logger - Supabase Schema
-- Run this in Supabase Dashboard -> SQL Editor
-- ============================================================

-- 1. admin_settings: stores admin password
CREATE TABLE IF NOT EXISTS admin_settings (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  setting_key TEXT NOT NULL UNIQUE,
  setting_value TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 2. manage_groups: groups and student rosters (one row per student)
CREATE TABLE IF NOT EXISTS manage_groups (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  group_id TEXT NOT NULL,
  password TEXT NOT NULL,
  student_id TEXT,
  student_name TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 3. observations: field observation records
CREATE TABLE IF NOT EXISTS observations (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  year_group TEXT,
  group_id TEXT,
  member_name TEXT,
  species_list TEXT,
  count_list TEXT,
  method_list TEXT,
  species_manual TEXT,
  count_manual TEXT,
  method_manual TEXT,
  habitat TEXT,
  location TEXT,
  notes TEXT,
  latitude TEXT,
  longitude TEXT,
  survey_type TEXT,
  temperature TEXT,
  humidity TEXT,
  rainfall TEXT,
  wind_speed TEXT,
  wind_direction TEXT,
  light_intensity TEXT,
  canopy_cover TEXT,
  canopy_height TEXT,
  site_location TEXT,
  photo_files TEXT,
  student_id TEXT,
  timestamp TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 4. notifications: admin broadcast messages
CREATE TABLE IF NOT EXISTS notifications (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  title TEXT NOT NULL,
  message TEXT NOT NULL,
  created_at TEXT,
  created_at_ts TIMESTAMPTZ DEFAULT NOW()
);

-- 5. notification_reads: tracks read status per group
CREATE TABLE IF NOT EXISTS notification_reads (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  notification_id BIGINT NOT NULL,
  group_id TEXT NOT NULL,
  read_at TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 6. archives: stores archived observation data instead of local CSV files
CREATE TABLE IF NOT EXISTS archives (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  academic_year TEXT NOT NULL,
  filename TEXT NOT NULL,
  archived_at TEXT,
  archived_at_ts TIMESTAMPTZ DEFAULT NOW(),
  data JSONB NOT NULL,
  record_count INTEGER DEFAULT 0
);

-- 7. reports: student-submitted issue reports
CREATE TABLE IF NOT EXISTS reports (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  reporter_name TEXT NOT NULL,
  student_id TEXT,
  group_id TEXT,
  category TEXT NOT NULL,
  subject TEXT NOT NULL,
  description TEXT NOT NULL,
  status TEXT DEFAULT 'open',
  created_at TEXT,
  created_at_ts TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- Seed: set default admin password (change after first login)
-- ============================================================
INSERT INTO admin_settings (setting_key, setting_value)
VALUES ('admin_password', 'admin123')
ON CONFLICT (setting_key) DO NOTHING;

-- ============================================================
-- Enable Row Level Security (recommended for safety)
-- ============================================================
ALTER TABLE admin_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE manage_groups ENABLE ROW LEVEL SECURITY;
ALTER TABLE observations ENABLE ROW LEVEL SECURITY;
ALTER TABLE notifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE notification_reads ENABLE ROW LEVEL SECURITY;
ALTER TABLE archives ENABLE ROW LEVEL SECURITY;
ALTER TABLE reports ENABLE ROW LEVEL SECURITY;

-- Allow public read/write access (matching current app behavior)
CREATE POLICY "Allow all" ON admin_settings FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Allow all" ON manage_groups FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Allow all" ON observations FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Allow all" ON notifications FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Allow all" ON notification_reads FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Allow all" ON archives FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Allow all" ON reports FOR ALL USING (true) WITH CHECK (true);
