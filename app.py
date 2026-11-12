from flask import Flask, render_template, request, redirect, url_for, send_file, session, jsonify, flash, Response
import csv
import os
from datetime import datetime
import io
import zipfile
import bcrypt 
from werkzeug.utils import secure_filename
from supabase import create_client, Client
from dotenv import load_dotenv
import pandas as pd
import numpy as np
from cryptography.fernet import Fernet
import base64
import hashlib
import json
import mimetypes
import uuid
import urllib.request
import urllib.parse
import ssl
from concurrent.futures import ThreadPoolExecutor

# Load environment variables
load_dotenv()

NEXT_PUBLIC_SUPABASE_URL = os.getenv("SUPABASE_URL")
NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY = os.getenv("SUPABASE_KEY")

if not NEXT_PUBLIC_SUPABASE_URL or not NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY:
    raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set in environment variables.")

supabase: Client = create_client(NEXT_PUBLIC_SUPABASE_URL, NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY)

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "fallback-secret-key-replace-me")
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024  # 32MB limit
app.config['TEMPLATES_AUTO_RELOAD'] = True

@app.after_request
def add_no_cache(response):
    if response.content_type and 'text/html' in response.content_type:
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response

_cache_bust = str(int(__import__('time').time()))

@app.context_processor
def inject_cache_bust():
    return {'cache_bust': _cache_bust}

PWD_DELIM = "||"

def _fernet():
    key = base64.urlsafe_b64encode(hashlib.sha256(app.secret_key.encode()).digest())
    return Fernet(key)

def encrypt_pwd(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()

def decrypt_pwd(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()

# ------------------------- CONFIG -------------------------
def get_current_academic_year():
    now = datetime.now()
    if now.month >= 8:
        return f"{now.year}/{now.year + 1}"
    return f"{now.year - 1}/{now.year}"

def get_admin_password():
    try:
        response = supabase.table("admin_settings").select("setting_value").eq("setting_key", "admin_password").execute()
        if response.data and len(response.data) > 0:
            return response.data[0]["setting_value"]
        else:
            print("Admin password not found in Supabase.")
    except Exception as e:
        print(f"Error fetching admin password from Supabase: {e}")
    return None # No hardcoded fallback for production security

def set_admin_password(new_password):
    try:
        # Use upsert to ensure the row exists (insert if missing, update if exists)
        data = {
            "setting_key": "admin_password",
            "setting_value": new_password.strip()
        }
        supabase.table("admin_settings").update({"setting_value": new_password}).eq("setting_key", "admin_password").execute()
        return True
    except Exception as e:
        print(f"Error updating admin password in Supabase: {e}")
        return False

DATA_FILE = "data/observations.csv"
GROUPS_FILE = "data/groups.csv"
ARCHIVE_FOLDER = "data/archive"

os.makedirs("data", exist_ok=True)
os.makedirs(ARCHIVE_FOLDER, exist_ok=True)

UPLOAD_FOLDER = 'static/uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

NOTIF_UPLOAD_FOLDER = os.path.join('static', 'uploads', 'notifications')
os.makedirs(NOTIF_UPLOAD_FOLDER, exist_ok=True)

NOTIF_ALLOWED_EXTENSIONS = {"csv", "pdf", "ppt", "pptx", "doc", "docx", "xls", "xlsx", "txt", "zip", "png", "jpg", "jpeg"}

def allowed_notification_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in NOTIF_ALLOWED_EXTENSIONS

RESEARCH_UPLOAD_FOLDER = os.path.join('static', 'uploads', 'research')
os.makedirs(RESEARCH_UPLOAD_FOLDER, exist_ok=True)

RESEARCH_ALLOWED_EXTENSIONS = {"pdf", "doc", "docx", "txt", "ppt", "pptx", "xls", "xlsx", "zip", "csv"}

def allowed_research_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in RESEARCH_ALLOWED_EXTENSIONS

@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        # Safe get with default empty string in case it's missing
        username = request.form.get('username', '').strip().lower()
        if username == 'admin':
            session['user_role'] = 'admin'
            # return redirect(url_for("archive.html"))
            return render_template("admin_login.html")
        elif username == 'student':
            session['user_role'] = 'student'
            return redirect(url_for('index'))
        else:
            return render_template("login.html", error="Please specify your role")
    
    # Render the login form for GET requests
    return render_template("login.html")

# -------------------- HOME --------------------
@app.route("/home")
def index():
    if "user_role" not in session:
        return redirect(url_for('login'))
    return render_template("index.html")

# -------------------- CHECK GROUP ID --------------------
@app.route("/check-group/<group_id>")
def check_group(group_id):
    try:
        res = supabase.table("manage_groups").select("group_id").eq("group_id", group_id.strip()).execute()
        exists = len(res.data) > 0
        return {"exists": exists}
    except Exception as e:
        print("DB group check error:", e)
        return {"exists": False, "error": str(e)}

# ------------------------- LOG OBSERVATIONS -------------------------
@app.route("/form", methods=["GET", "POST"])
def form():
    if request.method == "POST":
        is_offline_sync = request.form.get("offline_sync") == "true"

        # ---- VERIFY GROUP ID ----
        group_id_val = request.form.get("group_id", "").strip()
        if group_id_val:
            try:
                res = supabase.table("manage_groups").select("group_id").eq("group_id", group_id_val).execute()
                if not res.data or len(res.data) == 0:
                    if is_offline_sync:
                        from flask import jsonify
                        return jsonify({"error": f"Invalid Group ID '{group_id_val}'"}), 400
                    return render_template("form.html", year=get_current_academic_year(), error=f"Invalid Group ID '{group_id_val}'. Please ask your administrator to register it."), 400
            except Exception as e:
                print("DB group check error:", e)

        # ---- HANDLE FILE UPLOADS ----
        # ── ADDED: handle both regular uploads and offline base64 photos ──
        saved_filenames = []

        if is_offline_sync:
            # Offline sync sends photos as base64 strings (offline_photo_0, offline_photo_1, ...)
            import base64, re
            i = 0
            while True:
                b64_data = request.form.get(f"offline_photo_{i}")
                if not b64_data:
                    break
                try:
                    # Strip the data:image/jpeg;base64, header
                    match = re.match(r'data:image/(\w+);base64,(.+)', b64_data)
                    if match:
                        ext = match.group(1)
                        raw = base64.b64decode(match.group(2))
                        unique_name = f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}_offline_{i}.{ext}"
                        save_path = os.path.join(app.config["UPLOAD_FOLDER"], unique_name)
                        with open(save_path, 'wb') as f:
                            f.write(raw)
                        saved_filenames.append(unique_name)
                except Exception as e:
                    print(f"Error saving offline photo {i}: {e}")
                i += 1
        else:
            # Regular online upload
            uploaded_files = request.files.getlist("photos")
            seen_names = {}
            for file in uploaded_files:
                if file and allowed_file(file.filename):
                    base_name = secure_filename(file.filename)
                    ts = datetime.now().strftime('%Y%m%d%H%M%S%f')
                    if base_name in seen_names:
                        seen_names[base_name] += 1
                        unique_name = f"{ts}_{seen_names[base_name]}_{base_name}"
                    else:
                        seen_names[base_name] = 0
                        unique_name = f"{ts}_{base_name}"
                    save_path = os.path.join(app.config["UPLOAD_FOLDER"], unique_name)
                    file.save(save_path)
                    saved_filenames.append(unique_name)

        photo_files_str = ";".join(saved_filenames)

        # ---- EXISTING DATA DICT, ADDING photo_files ----
        def get_val(k):
            val = request.form.get(k)
            return val.strip() if val and val.strip() != "" else None

        # ── ADDED: offline sync sends species as JSON strings, online sends lists ──
        import json

        if is_offline_sync:
            try:
                species_entries = json.loads(request.form.get("species_entries", "[]"))
                species_list = ", ".join(e.get("species", "") for e in species_entries) or None
                count_list   = ", ".join(str(e.get("count", "")) for e in species_entries) or None
                method_list  = ", ".join(e.get("method", "") for e in species_entries) or None
            except Exception:
                species_list = None
                count_list   = None
                method_list  = None

            try:
                new_species_entries = json.loads(request.form.get("new_species_entries", "[]"))
                species_manual = ", ".join(e.get("species", "") for e in new_species_entries) or None
                count_manual   = ", ".join(str(e.get("count", "")) for e in new_species_entries) or None
                method_manual  = ", ".join(e.get("method", "") for e in new_species_entries) or None
            except Exception:
                species_manual = None
                count_manual   = None
                method_manual  = None
        else:
            species_list   = ", ".join(request.form.getlist("species[]")) or None
            count_list     = ", ".join(request.form.getlist("count[]")) or None
            method_list    = ", ".join(request.form.getlist("method[]")) or None
            species_manual = ", ".join(request.form.getlist("species_manual")) or None
            count_manual   = ", ".join(request.form.getlist("count_manual")) or None
            method_manual  = ", ".join(request.form.getlist("method_manual")) or None

        data = {
            "year_group":      get_val("year_group"),
            "group_id":        get_val("group_id"),
            "member_name":     get_val("member_name"),
            "species_list":    species_list,
            "count_list":      count_list,
            "method_list":     method_list,
            "species_manual":  species_manual,
            "count_manual":    count_manual,
            "method_manual":   method_manual,
            "habitat":         get_val("habitat"),
            "location":        get_val("location"),
            "notes":           get_val("notes"),
            "latitude":        get_val("latitude"),
            "longitude":       get_val("longitude"),
            "survey_type":     get_val("survey_type"),
            "temperature":     get_val("temperature"),
            "humidity":        get_val("humidity"),
            "rainfall":        get_val("rainfall"),
            "wind_speed":      get_val("wind_speed"),
            "wind_direction":  get_val("wind_direction"),
            "light_intensity": get_val("light_intensity"),
            "canopy_cover":    get_val("canopy_cover"),
            "canopy_height":   get_val("canopy_height"),
            "site_location":   get_val("site_location"),
            "photo_files":     photo_files_str,
            "student_id":      get_val("student_id"),
            "timestamp":       datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        try:
            supabase.table("observations").insert(data).execute()
        except Exception as e:
            print(f"Supabase Insert Error: {e}")
            if is_offline_sync:
                from flask import jsonify
                return jsonify({"error": f"Database Save Error: {e}"}), 500
            return render_template("form.html", year=get_current_academic_year(), error=f"Database Save Error: {e}")

        # ── ADDED: offline sync expects a plain 200 OK, not a redirect ──
        if is_offline_sync:
            from flask import jsonify
            return jsonify({"status": "ok"}), 200

        return redirect(url_for("form", success="true"))

    success_val = request.args.get("success")
    return render_template("form.html", year=get_current_academic_year(), success=success_val)


# ------------------------- GROUP LOGIN -------------------------
@app.route("/group", methods=["GET", "POST"])
def group_login():
    error = None
    if request.method == "POST":
        group_id = request.form.get("group_id").strip()
        password = request.form.get("password").strip()
        valid = False

        if group_id and password:
            try:
                response = supabase.table("manage_groups").select("*").eq("group_id", group_id).execute()
                if response.data and len(response.data) > 0:
                    stored = response.data[0]["password"]
                    if PWD_DELIM in stored:
                        stored_hash = stored.split(PWD_DELIM)[1]
                    else:
                        stored_hash = stored
                if bcrypt.checkpw(password.encode('utf-8'), stored_hash.encode('utf-8')):
                    valid = True
            except Exception as e:
                print(f"Supabase Select Error: {e}")

        if valid:
            session["group_id"] = group_id
            return redirect(url_for("view_group"))
        else:
            error = "Invalid Group ID or Password"

    return render_template("group_login.html", error=error)
        

# ------------------------- VIEW GROUP DATA -------------------------
@app.route("/view_group")
def view_group():
    if "group_id" not in session:
        return redirect(url_for("group_login"))

    rows = []
    try:
        response = supabase.table("observations").select("*").eq("group_id", session["group_id"]).execute()
        for row in response.data:
            # --- Process Standard Species ---
            s_names = row.get("species_list") or ""
            s_names = str(s_names).split(", ") if s_names else []
            
            s_counts = row.get("count_list") or ""
            s_counts = str(s_counts).split(", ") if s_counts else []
            
            s_methods = row.get("method_list") or ""
            s_methods = str(s_methods).split(", ") if s_methods else []
            
            row['zipped_species'] = list(zip(s_names, s_counts, s_methods))
            
            # --- Process Manual Species ---
            m_names = row.get("species_manual") or ""
            m_names = str(m_names).split(", ") if m_names else []
            
            m_counts = row.get("count_manual") or ""
            m_counts = str(m_counts).split(", ") if m_counts else []
            
            m_methods = row.get("method_manual") or ""
            m_methods = str(m_methods).split(", ") if m_methods else []
            
            row['zipped_manual'] = list(zip(m_names, m_counts, m_methods))
            
            rows.append(row)
    except Exception as e:
        print(f"Error fetching group data: {e}")

    return render_template("group.html", rows=rows, group_id=session["group_id"])

# ------------------------- DOWNLOAD GROUP DATA -------------------------
@app.route("/download_group")
def download_group():
    if "group_id" not in session:
        return redirect(url_for("group_login"))

    filtered = []
    try:
        response = supabase.table("observations").select("*").eq("group_id", session["group_id"]).execute()
        filtered = response.data
    except Exception as e:
        print(f"Error downloading group data: {e}")

    if not filtered:
        return "No data available", 404

    exclude_cols = {"photo_files", "id", "created_at"}
    fieldnames = [k for k in filtered[0].keys() if k not in exclude_cols]

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction='ignore')
    writer.writeheader()
    writer.writerows(filtered)
    output.seek(0)

    return send_file(
        io.BytesIO(output.getvalue().encode()),
        as_attachment=True,
        download_name=f"{session['group_id']}_{get_current_academic_year()}.csv",
        mimetype="text/csv"
    )


# ------------------------- DOWNLOAD GROUP IMAGES (ZIP) -------------------------
@app.route("/download_group_images")
def download_group_images():
    if "group_id" not in session:
        return redirect(url_for("group_login"))

    try:
        response = supabase.table("observations").select("photo_files").eq("group_id", session["group_id"]).execute()
        rows = response.data or []
    except Exception as e:
        print(f"Error fetching photos: {e}")
        return "Error fetching photos", 500

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        added = set()
        for row in rows:
            photos = row.get("photo_files") or ""
            for fname in photos.split(";"):
                fname = fname.strip()
                if not fname or fname in added:
                    continue
                path = os.path.join(app.config["UPLOAD_FOLDER"], fname)
                if os.path.isfile(path):
                    zf.write(path, fname)
                    added.add(fname)

    if not added:
        return "No images found for your group.", 404

    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{session['group_id']}_images_{get_current_academic_year()}.zip"
    )


# ------------------------- MANAGE GROUPS -------------------------
@app.route("/manage_groups", methods=["GET", "POST"])
def manage_groups():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))

    error = None
    success = None

    if request.method == "POST":
        entered_admin = request.form.get("admin_password")
        new_group_id = request.form.get("group_id", "").strip()
        new_password = request.form.get("password", "").strip()

        if entered_admin != get_admin_password():
            error = "❌ Invalid admin password!"
        elif not new_group_id or not new_password:
            error = "❌ Provide both Group ID and Password."
        else:
            existing_ids = set()
            try:
                response = supabase.table("manage_groups").select("*").execute()
                for row in response.data:
                    existing_ids.add(row.get("group_id", "").strip())
            except Exception as e:
                error = f"Database read error: {e}"

            if error is None:
                if new_group_id in existing_ids:
                    error = f"❌ Group ID '{new_group_id}' already exists."
                else:
                    try:
                        hashed = bcrypt.hashpw(new_password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
                        enc = encrypt_pwd(new_password)
                        composite = f"{enc}{PWD_DELIM}{hashed}"

                        student_ids = request.form.getlist("student_id[]")
                        student_names = request.form.getlist("student_name[]")
                        student_count = 0

                        rows = []
                        for sid, sname in zip(student_ids, student_names):
                            sid = sid.strip()
                            sname = sname.strip()
                            if sid and sname:
                                rows.append({
                                    "group_id": new_group_id,
                                    "password": composite,
                                    "student_id": sid,
                                    "student_name": sname
                                })
                                student_count += 1

                        if not rows:
                            rows.append({"group_id": new_group_id, "password": composite})

                        supabase.table("manage_groups").insert(rows).execute()
                        success = f"{new_group_id} added successfully with {student_count} student(s)!"

                    except Exception as e:
                        error = f"Database insert error: {e}"

    return render_template("manage_groups.html", error=error, success=success, year=get_current_academic_year())

# ------------------------- BULK GROUP IMPORT (PDF) -------------------------
def _is_header_line(sid, sname):
    low_id = sid.lower().strip()
    low_name = sname.lower().strip()
    header_keywords_id = ["student", "id", "no", "number", "matric", "roll", "code"]
    header_keywords_name = ["student", "name", "full", "first", "last", "surname"]
    if any(kw in low_id for kw in header_keywords_id):
        if any(kw in low_name for kw in header_keywords_name):
            return True
    if low_id.replace(" ", "") in ("studentid", "studentno", "matricno", "rollno"):
        return True
    return False

def parse_student_pdf(pdf_stream):
    students = []
    try:
        from pypdf import PdfReader
        reader = PdfReader(pdf_stream)
        for page in reader.pages:
            text = page.extract_text()
            if not text:
                continue
            lines = text.strip().split('\n')
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                parts = None
                for sep in [',', '\t', '|', ';']:
                    if sep in line:
                        parts = line.split(sep, 1)
                        break
                if parts and len(parts) == 2:
                    sid = parts[0].strip()
                    sname = parts[1].strip()
                    if sid and sname and not _is_header_line(sid, sname):
                        students.append((sid, sname))
                        continue
                parts = line.split(None, 1)
                if len(parts) == 2:
                    sid = parts[0].strip()
                    sname = parts[1].strip()
                    if sid and sname and not _is_header_line(sid, sname):
                        students.append((sid, sname))
    except Exception as e:
        raise ValueError(f"Failed to parse PDF: {e}")
    if not students:
        raise ValueError("No student records found in PDF. Ensure the PDF contains Student ID and Name columns/lines.")
    return students

@app.route("/manage_groups/bulk-upload", methods=["POST"])
def manage_groups_bulk_upload():
    if not session.get("admin_logged_in"):
        return jsonify({"error": "Not authenticated"}), 401

    entered_admin = request.form.get("admin_password", "").strip()
    if entered_admin != get_admin_password():
        return jsonify({"error": "Invalid admin password"}), 403

    try:
        students_per_group = int(request.form.get("students_per_group", 0))
        if students_per_group < 1:
            raise ValueError
    except (ValueError, TypeError):
        return jsonify({"error": "Students per group must be a positive number"}), 400

    group_prefix = request.form.get("group_prefix", "Group").strip()
    if not group_prefix:
        group_prefix = "Group"

    pdf_file = request.files.get("pdf_file")
    if not pdf_file:
        return jsonify({"error": "No PDF file uploaded"}), 400

    try:
        pdf_stream = io.BytesIO(pdf_file.read())
        students = parse_student_pdf(pdf_stream)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    existing_nums = set()
    try:
        response = supabase.table("manage_groups").select("group_id").execute()
        for row in (response.data or []):
            gid = row.get("group_id", "").strip()
            if gid.startswith(group_prefix + " "):
                suffix = gid[len(group_prefix + " "):]
                if suffix.isdigit():
                    existing_nums.add(int(suffix))
    except Exception as e:
        return jsonify({"error": f"Database error: {e}"}), 500

    start_num = max(existing_nums) + 1 if existing_nums else 1
    import secrets

    groups_created = []
    for i in range(0, len(students), students_per_group):
        chunk = students[i:i + students_per_group]
        group_id = f"{group_prefix} {start_num}"
        start_num += 1

        raw_password = secrets.token_urlsafe(8)
        hashed = bcrypt.hashpw(raw_password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        enc = encrypt_pwd(raw_password)
        composite = f"{enc}{PWD_DELIM}{hashed}"

        rows = []
        for sid, sname in chunk:
            rows.append({
                "group_id": group_id,
                "password": composite,
                "student_id": sid,
                "student_name": sname
            })
        try:
            supabase.table("manage_groups").insert(rows).execute()
        except Exception as e:
            return jsonify({"error": f"Failed to create group {group_id}: {e}"}), 500
        groups_created.append({
            "group_id": group_id,
            "password": raw_password,
            "student_count": len(chunk),
            "students": [{"student_id": sid, "student_name": sname} for sid, sname in chunk]
        })

    return jsonify({
        "success": True,
        "groups_created": groups_created,
        "total_students": len(students),
        "total_groups": len(groups_created)
    })

@app.route("/manage_groups/update-password", methods=["POST"])
def manage_groups_update_password():
    if not session.get("admin_logged_in"):
        return jsonify({"error": "Not authenticated"}), 401

    entered_admin = request.form.get("admin_password", "").strip()
    if entered_admin != get_admin_password():
        return jsonify({"error": "Invalid admin password"}), 403

    group_id = request.form.get("group_id", "").strip()
    new_password = request.form.get("new_password", "").strip()

    if not group_id or not new_password:
        return jsonify({"error": "Group ID and new password are required"}), 400

    if len(new_password) < 4:
        return jsonify({"error": "Password must be at least 4 characters"}), 400

    try:
        hashed = bcrypt.hashpw(new_password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        enc = encrypt_pwd(new_password)
        composite = f"{enc}{PWD_DELIM}{hashed}"
        supabase.table("manage_groups").update({"password": composite}).eq("group_id", group_id).execute()
        return jsonify({"success": True, "group_id": group_id, "password": new_password})
    except Exception as e:
        return jsonify({"error": f"Failed to update password: {e}"}), 500

# ------------------------- VIEW GROUPS -------------------------
@app.route("/admin/view_groups")
def admin_view_groups():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))

    groups_data = []
    try:
        response = supabase.table("manage_groups").select("*").execute()
        rows = response.data or []

        from collections import defaultdict
        grouped = defaultdict(list)
        for row in rows:
            grouped[row["group_id"]].append(row)

        for gid, student_rows in grouped.items():
            students = []
            for r in student_rows:
                sid = r.get("student_id", "").strip()
                sname = r.get("student_name", "").strip()
                if sid and sname:
                    students.append({"id": r.get("id"), "student_id": sid, "student_name": sname})
            raw = student_rows[0].get("password", "")
            if PWD_DELIM in raw:
                enc_part = raw.split(PWD_DELIM)[0]
                try:
                    display_pw = decrypt_pwd(enc_part)
                except Exception:
                    display_pw = raw[:20] + "..."
            else:
                display_pw = raw[:20] + "..."
            groups_data.append({
                "group_id": gid,
                "password": display_pw,
                "students": students
            })
    except Exception as e:
        print(f"Error fetching groups: {e}")

    return render_template("admin_groups.html", groups=groups_data, groups_json=json.dumps(groups_data))

# ------------------------- DELETE GROUP -------------------------
@app.route("/admin/delete_group/<group_id>", methods=["POST"])
def delete_group(group_id):
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))

    try:
        supabase.table("manage_groups").delete().eq("group_id", group_id).execute()
    except Exception as e:
        print(f"Error deleting group: {e}")

    return redirect(url_for("admin_view_groups"))

# ------------------------- ADD STUDENT TO GROUP -------------------------
@app.route("/admin/add_student", methods=["POST"])
def admin_add_student():
    if not session.get("admin_logged_in"):
        return jsonify({"error": "Not authenticated"}), 401

    data = request.get_json() or {}
    group_id = data.get("group_id", "").strip()
    student_id = data.get("student_id", "").strip()
    student_name = data.get("student_name", "").strip()

    if not group_id or not student_id or not student_name:
        return jsonify({"error": "Group ID, Student ID, and Student Name are required"}), 400

    try:
        existing = supabase.table("manage_groups").select("password").eq("group_id", group_id).limit(1).execute()
        if not existing.data:
            return jsonify({"error": "Group not found"}), 404
        password = existing.data[0]["password"]

        supabase.table("manage_groups").insert({
            "group_id": group_id,
            "password": password,
            "student_id": student_id,
            "student_name": student_name
        }).execute()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ------------------------- REMOVE STUDENT FROM GROUP -------------------------
@app.route("/admin/remove_student", methods=["POST"])
def admin_remove_student():
    if not session.get("admin_logged_in"):
        return jsonify({"error": "Not authenticated"}), 401

    data = request.get_json() or {}
    row_id = data.get("id")

    if not row_id:
        return jsonify({"error": "Student row ID is required"}), 400

    try:
        supabase.table("manage_groups").delete().eq("id", row_id).execute()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/delete_notif/<notif_history>", methods=["POST"])
def delete_notif(notif_history):
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))
    
    try:
        resp = supabase.table("notifications").select("*").eq("id", notif_history).execute()
        if resp.data:
            n = resp.data[0]
            attachment_file = n.get("attachment_file")
            if attachment_file:
                file_path = os.path.join(NOTIF_UPLOAD_FOLDER, attachment_file)
                if os.path.exists(file_path):
                    os.remove(file_path)
        supabase.table("notifications").delete().eq("id", notif_history).execute()
    except Exception as e:
        print(f"Error deleting notification: {e}")
        
    return redirect(url_for("admin_dashboard"))

# ------------------------- ADMIN DASHBOARD -------------------------
@app.route("/admin/dashboard", methods=["GET", "POST"])
def admin_dashboard():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        msg_body = request.form.get("message", "").strip()
        if title and msg_body:
            try:
                data = {
                    "title": title,
                    "message": msg_body,
                    "created_at": datetime.now().isoformat()
                }
                attachment = request.files.get("attachment")
                if attachment and attachment.filename and allowed_notification_file(attachment.filename):
                    original_name = secure_filename(attachment.filename)
                    ts = datetime.now().strftime('%Y%m%d%H%M%S%f')
                    unique_name = f"{ts}_{original_name}"
                    save_path = os.path.join(NOTIF_UPLOAD_FOLDER, unique_name)
                    attachment.save(save_path)
                    data["attachment_file"] = unique_name
                    data["attachment_original_name"] = original_name
                supabase.table("notifications").insert(data).execute()
                session['_notif_msg'] = ("Notification sent successfully!", "success")
            except Exception as e:
                session['_notif_msg'] = (f"Failed to send notification: {e}", "error")
        else:
            session['_notif_msg'] = ("Please fill in both title and message.", "error")
        return redirect(url_for("admin_dashboard"))

    message = None
    message_category = None
    msg = session.pop('_notif_msg', None)
    if msg:
        message, message_category = msg

    groups = []
    try:
        response = supabase.table("manage_groups").select("*").execute()
        groups = response.data
    except Exception as e:
        print(f"Error fetching groups: {e}")

    seen_group_ids = set()
    total_students = 0
    group_names = []
    for g in groups:
        gid = g.get("group_id", "").strip()
        if gid and gid not in seen_group_ids:
            seen_group_ids.add(gid)
            group_names.append(gid)
        sid = g.get("student_id", "").strip()
        sname = g.get("student_name", "").strip()
        if sid and sname:
            total_students += 1
    total_groups = len(group_names)

    new_species_set = set()
    total_observations = 0
    total_images = 0
    entry_counts = {}
    try:
        response = supabase.table("observations").select("*").execute()
        for row in response.data:
            total_observations += 1
            s_manual = row.get("species_manual") or ""
            if s_manual:
                for n in s_manual.split(","):
                    name = n.strip()
                    if name:
                        new_species_set.add(name)
            photo_str = row.get("photo_files") or ""
            if photo_str:
                total_images += len([p for p in photo_str.split(";") if p.strip()])
            obs_sid = (row.get("student_id") or "").strip()
            obs_name = (row.get("member_name") or "").strip()
            if obs_sid:
                entry_counts[obs_sid] = entry_counts.get(obs_sid, 0) + 1
            elif obs_name:
                obs_key = obs_name.lower()
                entry_counts[obs_key] = entry_counts.get(obs_key, 0) + 1
    except Exception as e:
        print(f"Error fetching observations: {e}")

    student_roster = []
    for g in groups:
        gid = (g.get("group_id") or "").strip()
        sid = (g.get("student_id") or "").strip()
        sname = (g.get("student_name") or "").strip()
        if not sid or not sname:
            continue
        entries = entry_counts.get(sid, 0)
        if not entries:
            entries = entry_counts.get(sname.lower(), 0)
        student_roster.append({
            "group_id": gid,
            "student_id": sid,
            "student_name": sname,
            "entries": entries,
        })
    student_roster.sort(key=lambda s: (s["group_id"], s["student_name"]))

    archive_files = [a["filename"] for a in get_archives()]

    pending_research_count = 0
    try:
        pr = supabase.table("research_posts").select("id", count="exact").eq("status", "pending").execute()
        pending_research_count = pr.count if hasattr(pr, 'count') else 0
    except Exception:
        pass

    projects_count = 0
    try:
        pc = supabase.table("platform_projects").select("id", count="exact").execute()
        projects_count = pc.count if hasattr(pc, 'count') else 0
    except Exception:
        pass

    return render_template(
        "dashboard.html",
        year=get_current_academic_year(),
        message=message,
        message_category=message_category,
        total_groups=total_groups,
        total_students=total_students,
        group_names=group_names,
        total_observations=total_observations,
        total_images=total_images,
        new_species_count=len(new_species_set),
        new_species_list=sorted(new_species_set),
        archive_count=len(archive_files),
        archive_files=archive_files[:5],
        pending_research_count=pending_research_count,
        projects_count=projects_count,
        students=student_roster,
    )

# ------------------------- ADMIN SETTINGS -------------------------
@app.route("/admin/settings")
def admin_settings():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))
    return render_template("admin_settings.html", year=get_current_academic_year())


# ------------------------- ADMIN LOGIN -------------------------
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        entered = request.form.get("admin_password", "").strip()
        if entered == get_admin_password():
            session["admin_logged_in"] = True
            return redirect(url_for("admin_dashboard"))
        else:
            error = "Invalid admin password!"
    return render_template("admin_login.html", error=error)

# ------------------------- CHANGE ADMIN PASSWORD -------------------------
@app.route("/admin/change_password", methods=["POST"])
def change_admin_password():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))

    current_pw = request.form.get("current_password", "").strip()
    new_pw = request.form.get("new_password", "").strip()
    confirm_pw = request.form.get("confirm_password", "").strip()

    if current_pw != get_admin_password():
        return render_template("admin_settings.html", error="❌ Incorrect current password.", year=get_current_academic_year())
    if new_pw != confirm_pw:
        return render_template("admin_settings.html", error="❌ New passwords do not match.", year=get_current_academic_year())
    if len(new_pw) < 6:
        return render_template("admin_settings.html", error="❌ New password must be at least 6 characters long.", year=get_current_academic_year())

    if set_admin_password(new_pw):
        return render_template("admin_settings.html", success="✅ Admin password updated successfully!", year=get_current_academic_year())
    else:
        return render_template("admin_settings.html", error="❌ Failed to update password.", year=get_current_academic_year())

# ------------------------- ADMIN LOGOUT -------------------------
@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_logged_in", None)
    return render_template("index.html")


# ------------------------- GET ARCHIVES (Supabase + legacy CSV) -------------------------
def get_archives():
    """Return combined list from Supabase archives table and legacy local CSV files."""
    combined = []

    # 1. From Supabase archives table
    try:
        resp = supabase.table("archives").select("*").order("archived_at_ts", desc=True).execute()
        for r in (resp.data or []):
            r["_source"] = "supabase"
            combined.append(r)
    except Exception as e:
        print(f"Error fetching archives from Supabase: {e}")

    # 2. From legacy local CSV files (data/archive/)
    try:
        for fname in sorted(os.listdir(ARCHIVE_FOLDER), reverse=True):
            if fname.endswith(".csv"):
                combined.append({
                    "id": None,
                    "filename": fname,
                    "academic_year": "—",
                    "record_count": None,
                    "archived_at": "—",
                    "_source": "local"
                })
    except Exception as e:
        print(f"Error listing local archives: {e}")

    return combined


# ------------------------- ARCHIVE CURRENT DATA -------------------------
@app.route("/admin/archive", methods=["GET", "POST"])
def archive_data():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))

    msg = None
    if request.method == "POST":
        entered_admin = request.form.get("admin_password")

        if entered_admin != get_admin_password():
            msg = "❌ Invalid admin password! Data not archived."
        else:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            academic_year = get_current_academic_year()
            filename = f"observations_{academic_year.replace('/', '_')}_{timestamp}.csv"
            try:
                response = supabase.table("observations").select("*").execute()
                all_data = response.data
                
                if not all_data:
                    msg = "No data to archive."
                else:
                    # Sort data by group_id (natural sort) and then by timestamp
                    import re
                    def natural_sort_key(s):
                        if not s: return []
                        return [int(text) if text.isdigit() else text.lower()
                                for text in re.split('([0-9]+)', s)]
                    
                    all_data.sort(key=lambda x: (natural_sort_key(x.get('group_id', '')), x.get('timestamp', '')))

                    supabase.table("archives").insert({
                        "academic_year": academic_year,
                        "filename": filename,
                        "archived_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "data": all_data,
                        "record_count": len(all_data)
                    }).execute()
                    
                    supabase.table("observations").delete().neq("id", "0").execute()
                    
                    msg = f"Data archived successfully as {filename}"
            except Exception as e:
                msg = f"Error archiving data: {e}"
    
    archives_list = get_archives()

    return render_template("archive.html", message=msg, archives=archives_list, year=get_current_academic_year())


# ------------------------- VIEW ARCHIVE FILES -------------------------
@app.route("/admin/view_archive")
def view_archive():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))

    archives_list = get_archives()
    return render_template("archive.html", archives=archives_list, year=get_current_academic_year())


# ------------------------- DOWNLOAD ARCHIVE FILE (Supabase) -------------------------
@app.route("/admin/download_archive/<int:archive_id>")
def download_archive(archive_id):
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))

    try:
        resp = supabase.table("archives").select("*").eq("id", archive_id).execute()
        if not resp.data:
            return "Archive not found", 404
        archive = resp.data[0]
        all_data = archive.get("data", [])
        if not all_data:
            return "Archive is empty", 404

        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=all_data[0].keys())
        writer.writeheader()
        writer.writerows(all_data)
        output.seek(0)

        return send_file(
            io.BytesIO(output.getvalue().encode()),
            as_attachment=True,
            download_name=archive.get("filename", "archive.csv"),
            mimetype="text/csv"
        )
    except Exception as e:
        print(f"Error downloading archive: {e}")
        return "Error downloading archive", 500


# ------------------------- DOWNLOAD ARCHIVE FILE (legacy local CSV) -------------------------
@app.route("/admin/download_archive_file/<filename>")
def download_archive_file(filename):
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))

    path = os.path.join(ARCHIVE_FOLDER, secure_filename(filename))
    if os.path.isfile(path):
        return send_file(path, as_attachment=True, mimetype="text/csv")
    return "File not found", 404

# ------------------------- DELETE ARCHIVE FILE (Supabase) -------------------------
@app.route("/admin/delete_archive/<int:archive_id>", methods=["POST"])
def delete_archive(archive_id):
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))

    try:
        supabase.table("archives").delete().eq("id", archive_id).execute()
    except Exception as e:
        print(f"Error deleting archive: {e}")

    return redirect(url_for("view_archive"))


# ------------------------- DELETE ARCHIVE FILE (legacy local CSV) -------------------------
@app.route("/admin/delete_archive_file/<filename>", methods=["POST"])
def delete_archive_file(filename):
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))

    file_path = os.path.join(ARCHIVE_FOLDER, secure_filename(filename))
    if os.path.exists(file_path):
        os.remove(file_path)

    return redirect(url_for("view_archive"))


# ------------------------- DOWNLOAD ALL GROUP IMAGES (ZIP with group subfolders) -------------------------
@app.route("/admin/download_group_images_zip")
def download_group_images_zip():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))

    try:
        response = supabase.table("observations").select("group_id, photo_files").execute()
        rows = response.data or []
    except Exception as e:
        print(f"Error fetching data for images ZIP: {e}")
        return "Error fetching images", 500

    if not rows:
        return "No images found", 404

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        added = set()
        for row in rows:
            gid = (row.get("group_id") or "unknown").strip()
            photos = row.get("photo_files") or ""
            for fname in photos.split(";"):
                fname = fname.strip()
                if not fname or fname in added:
                    continue
                path = os.path.join(app.config["UPLOAD_FOLDER"], fname)
                if os.path.isfile(path):
                    arcname = f"{gid}/{fname}"
                    zf.write(path, arcname)
                    added.add(fname)

    if not added:
        return "No image files found on disk", 404

    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"Group_Images_{get_current_academic_year().replace('/', '_')}.zip"
    )


@app.route("/delete_entry/<entry_id>", methods=["POST"])
def delete_entry(entry_id):
    if "group_id" not in session:
        return redirect(url_for("group_login"))

    try:
        supabase.table("observations").delete().eq("id", entry_id).eq("group_id", session["group_id"]).execute()
    except Exception as e:
        print(f"Error deleting entry: {e}")

    return redirect(url_for("view_group"))

# ------------------------- PROFILE -------------------------
@app.route("/profile")
def profile():
    return render_template("profile.html")

# ------------------------- RESEARCH -------------------------
@app.route("/research", methods=["GET", "POST"])
def research():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        authors = request.form.get("authors", "").strip()
        email = request.form.get("email", "").strip()
        institution = request.form.get("institution", "").strip()
        abstract = request.form.get("abstract", "").strip()
        category = request.form.get("category", "").strip()

        if not title or not authors or not abstract:
            posts = supabase.table("research_posts").select("*").eq("status", "approved").order("created_at_ts", desc=True).execute()
            return render_template("research.html", posts=posts.data if posts.data else [],
                                   error="Title, authors, and abstract are required.")

        document_file = None
        document_original = None
        if "document" in request.files:
            file = request.files["document"]
            if file and file.filename and allowed_research_file(file.filename):
                document_original = secure_filename(file.filename)
                ext = document_original.rsplit(".", 1)[1].lower()
                unique_name = f"{uuid.uuid4().hex}.{ext}"
                save_path = os.path.join(RESEARCH_UPLOAD_FOLDER, unique_name)
                file.save(save_path)
                document_file = unique_name

        try:
            now = datetime.now().isoformat()
            insert_data = {
                "title": title,
                "authors": authors,
                "email": email,
                "institution": institution,
                "abstract": abstract,
                "category": category,
                "status": "pending",
                "created_at": now
            }
            if document_file:
                insert_data["document_file"] = document_file
                insert_data["document_original_name"] = document_original
            supabase.table("research_posts").insert(insert_data).execute()
        except Exception as e:
            posts = supabase.table("research_posts").select("*").eq("status", "approved").order("created_at_ts", desc=True).execute()
            return render_template("research.html", posts=posts.data if posts.data else [],
                                   error=f"Failed to submit: {e}")

        session['_research_msg'] = "Your research has been submitted for review. It will appear here once approved by an administrator."
        return redirect(url_for("research"))

    posts = supabase.table("research_posts").select("*").eq("status", "approved").order("created_at_ts", desc=True).execute()

    success_msg = session.pop('_research_msg', None)

    return render_template("research.html", posts=posts.data if posts.data else [],
                           success_msg=success_msg)

@app.route("/research/download/<int:post_id>")
def research_download(post_id):
    result = supabase.table("research_posts").select("*").eq("id", post_id).execute()
    if not result.data:
        return "Not found", 404
    post = result.data[0]
    if not post.get("document_file"):
        return "No document", 404
    file_path = os.path.join(RESEARCH_UPLOAD_FOLDER, post["document_file"])
    if not os.path.exists(file_path):
        return "File not found", 404
    original_name = post.get("document_original_name", post["document_file"])
    return send_file(file_path, as_attachment=True, download_name=original_name)

# ------------------------- EXPLORE -------------------------
_ctx = ssl._create_unverified_context()
_headers = {"User-Agent": "EcoFieldLogger/1.0 (educational project; contact: hello.miltonpixel@gmail.com)"}

def wikipedia_search(query, limit=5):
    try:
        url = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode({
            "action": "opensearch",
            "search": query,
            "limit": limit,
            "namespace": 0,
            "format": "json"
        })
        req = urllib.request.Request(url, headers=_headers)
        with urllib.request.urlopen(req, timeout=8, context=_ctx) as resp:
            data = json.loads(resp.read().decode())
        results = []
        for i in range(len(data[1])):
            results.append({
                "title": data[1][i],
                "description": data[2][i] if i < len(data[2]) else "",
                "url": data[3][i] if i < len(data[3]) else ""
            })
        return results
    except Exception as e:
        print(f"Wikipedia search error: {e}")
        return []

def wikipedia_summary(title):
    try:
        url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(title)}"
        req = urllib.request.Request(url, headers=_headers)
        with urllib.request.urlopen(req, timeout=8, context=_ctx) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"Wikipedia summary error: {e}")
        return None

@app.route("/explore")
def explore():
    data_path = os.path.join("static", "data.json")
    species_data = {}
    try:
        if os.path.exists(data_path):
            with open(data_path, "r", encoding="utf-8") as f:
                species_data = json.load(f)
    except Exception as e:
        print(f"Error loading data.json: {e}")
    return render_template("explore.html", species_data=json.dumps(species_data))

@app.route("/api/explore/species")
def explore_species():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"results": []})
    results = wikipedia_search(q)
    species_list = []
    for r in results:
        summary = wikipedia_summary(r["title"])
        if summary:
            species_list.append({
                "title": summary.get("title", r["title"]),
                "description": summary.get("extract", r["description"]),
                "thumbnail": summary.get("thumbnail", {}).get("source") if summary.get("thumbnail") else None,
                "url": summary.get("content_urls", {}).get("desktop", {}).get("page") if summary.get("content_urls") else r["url"],
                "type": summary.get("type", "standard")
            })
        else:
            species_list.append({
                "title": r["title"],
                "description": r["description"],
                "thumbnail": None,
                "url": r["url"],
                "type": "standard"
            })
    return jsonify({"results": species_list})

@app.route("/api/explore/chat", methods=["POST"])
def explore_chat():
    data = request.get_json()
    message = data.get("message", "").strip() if data else ""
    if not message:
        return jsonify({"reply": "Please ask a question about ecology, species, or conservation."})

    results = wikipedia_search(message, limit=3)
    if not results:
        return jsonify({"reply": "I couldn't find information on that topic. Try asking about a specific species, ecological concept, or conservation topic."})

    summaries = []
    for r in results[:2]:
        s = wikipedia_summary(r["title"])
        if s and s.get("extract"):
            txt = s["extract"]
            if len(txt) > 500:
                txt = txt[:500] + "..."
            summaries.append(f"**{s['title']}**: {txt}")

    if summaries:
        reply = "Here's what I found:\n\n" + "\n\n".join(summaries)
    else:
        reply = "I found some related topics but couldn't retrieve details. Try a more specific search."

    return jsonify({"reply": reply})

# ------------------------- AI MODELS -------------------------
@app.route("/models")
def models():
    return render_template("models.html", ai_available=bool(GEMINI_API_KEY))


# ------------------------- ABOUT -------------------------
@app.route("/about")
def about():
    return render_template("about.html")

# ------------------------- FAQ -------------------------
@app.route("/faq")
def faq():
    return render_template("faq.html")

# ------------------------- HELP -------------------------
@app.route("/help")
def help_page():
    return render_template("help.html")

# ------------------------- REPORT ISSUE -------------------------
@app.route("/report", methods=["GET", "POST"])
def report():
    if request.method == "POST":
        reporter_name = request.form.get("reporter_name", "").strip()
        student_id = request.form.get("student_id", "").strip()
        group_id = request.form.get("group_id", "").strip()
        category = request.form.get("category", "").strip()
        subject = request.form.get("subject", "").strip()
        description = request.form.get("description", "").strip()

        if not reporter_name or not category or not subject or not description:
            return render_template("report.html", error="Please fill in all required fields.")

        try:
            supabase.table("reports").insert({
                "reporter_name": reporter_name,
                "student_id": student_id,
                "group_id": group_id,
                "category": category,
                "subject": subject,
                "description": description,
                "status": "open",
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }).execute()
            return render_template("report.html", success="Your report has been submitted. Thank you for your feedback!")
        except Exception as e:
            print("Report submit error:", e)
            return render_template("report.html", error="Failed to submit report. Please try again later.")

    return render_template("report.html")

@app.route("/admin/reports")
def admin_reports():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))

    reports_list = []
    try:
        response = supabase.table("reports").select("*").order("created_at_ts", desc=True).execute()
        reports_list = response.data or []
    except Exception as e:
        print("Error fetching reports:", e)

    return render_template("admin_reports.html", reports=reports_list)

@app.route("/admin/reports/download")
def admin_reports_download():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))

    try:
        response = supabase.table("reports").select("*").order("created_at_ts", desc=True).execute()
        reports_list = response.data or []
    except Exception as e:
        print("Error fetching reports for download:", e)
        reports_list = []

    lines = []
    lines.append("=" * 72)
    lines.append("EcoField Logger — Issue Reports")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"Total Reports: {len(reports_list)}")
    lines.append("=" * 72)
    lines.append("")

    for i, r in enumerate(reports_list, 1):
        lines.append(f"Report #{i}")
        lines.append("-" * 72)
        lines.append(f"Date:       {r.get('created_at', 'N/A')}")
        lines.append(f"Name:       {r.get('reporter_name', 'N/A')}")
        lines.append(f"Student ID: {r.get('student_id', 'N/A')}")
        lines.append(f"Group ID:   {r.get('group_id', 'N/A')}")
        lines.append(f"Category:   {r.get('category', 'N/A')}")
        lines.append(f"Subject:    {r.get('subject', 'N/A')}")
        lines.append(f"Status:     {r.get('status', 'N/A')}")
        lines.append(f"Description: {r.get('description', 'N/A')}")
        lines.append("")

    text = "\n".join(lines)

    from flask import Response
    return Response(
        text,
        mimetype="text/plain",
        headers={"Content-Disposition": f"attachment; filename=ecofield_reports_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"}
    )

@app.route("/admin/reports/<int:report_id>/<status>", methods=["POST"])
def update_report_status(report_id, status):
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))

    if status not in ("open", "resolved", "closed"):
        return redirect(url_for("admin_reports"))

    try:
        supabase.table("reports").update({"status": status}).eq("id", report_id).execute()
    except Exception as e:
        print("Error updating report status:", e)

    return redirect(url_for("admin_reports"))

@app.route("/logout")
def group_logout():
    session.pop("group_id", None)
    return redirect(url_for("index"))

# ------------------------- ADMIN RESEARCH MANAGEMENT -------------------------
@app.route("/admin/research")
def admin_research():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))

    status_filter = request.args.get("status", "all")

    try:
        if status_filter in ("pending", "approved", "rejected"):
            response = supabase.table("research_posts").select("*").eq("status", status_filter).order("created_at_ts", desc=True).execute()
        else:
            response = supabase.table("research_posts").select("*").order("created_at_ts", desc=True).execute()
        posts = response.data or []
    except Exception as e:
        print(f"Error fetching research posts: {e}")
        posts = []

    pending_count = 0
    try:
        pc = supabase.table("research_posts").select("id", count="exact").eq("status", "pending").execute()
        pending_count = pc.count if hasattr(pc, 'count') else 0
    except Exception:
        try:
            all_posts = supabase.table("research_posts").select("*").execute()
            pending_count = len([p for p in (all_posts.data or []) if p.get("status") == "pending"])
        except Exception:
            pass

    return render_template("admin_research.html", posts=posts, status_filter=status_filter, pending_count=pending_count)

@app.route("/admin/research/<int:post_id>/approve", methods=["POST"])
def admin_research_approve(post_id):
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))
    try:
        supabase.table("research_posts").update({"status": "approved"}).eq("id", post_id).execute()
    except Exception as e:
        print(f"Error approving research: {e}")
    return redirect(url_for("admin_research", status=request.args.get("status", "all")))

@app.route("/admin/research/<int:post_id>/reject", methods=["POST"])
def admin_research_reject(post_id):
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))
    try:
        supabase.table("research_posts").update({"status": "rejected"}).eq("id", post_id).execute()
    except Exception as e:
        print(f"Error rejecting research: {e}")
    return redirect(url_for("admin_research", status=request.args.get("status", "all")))

@app.route("/admin/research/<int:post_id>/delete", methods=["POST"])
def admin_research_delete(post_id):
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))
    try:
        result = supabase.table("research_posts").select("document_file").eq("id", post_id).execute()
        if result.data and result.data[0].get("document_file"):
            file_path = os.path.join(RESEARCH_UPLOAD_FOLDER, result.data[0]["document_file"])
            if os.path.exists(file_path):
                os.remove(file_path)
        supabase.table("research_posts").delete().eq("id", post_id).execute()
    except Exception as e:
        print(f"Error deleting research: {e}")
    return redirect(url_for("admin_research", status=request.args.get("status", "all")))

# ------------------------- ADMIN PROJECTS MANAGEMENT -------------------------
@app.route("/admin/projects")
def admin_projects():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))
    try:
        response = supabase.table("platform_projects").select("*").order("sort_order", desc=False).order("created_at", desc=True).execute()
        projects = response.data or []
    except Exception as e:
        print(f"Error fetching projects: {e}")
        projects = []
    return render_template("admin_projects.html", projects=projects)

@app.route("/admin/projects/new", methods=["GET", "POST"])
def admin_project_new():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        organization = request.form.get("organization", "").strip()
        description = request.form.get("description", "").strip()
        status = request.form.get("status", "upcoming").strip()
        date_range = request.form.get("date_range", "").strip()
        location = request.form.get("location", "").strip()
        tags = request.form.get("tags", "").strip()
        is_featured = request.form.get("is_featured") == "on"
        sort_order = request.form.get("sort_order", 0, type=int)

        if not title:
            return render_template("admin_project_form.html", project=None, error="Title is required.")

        try:
            data = {
                "title": title,
                "organization": organization,
                "description": description,
                "status": status,
                "date_range": date_range,
                "location": location,
                "tags": tags,
                "is_featured": is_featured,
                "sort_order": sort_order,
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
            }
            supabase.table("platform_projects").insert(data).execute()
            return redirect(url_for("admin_projects"))
        except Exception as e:
            return render_template("admin_project_form.html", project=None, error=f"Failed to create project: {e}")

    return render_template("admin_project_form.html", project=None)

@app.route("/admin/projects/<int:project_id>/edit", methods=["GET", "POST"])
def admin_project_edit(project_id):
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))

    try:
        result = supabase.table("platform_projects").select("*").eq("id", project_id).execute()
        if not result.data:
            return redirect(url_for("admin_projects"))
        project = result.data[0]
    except Exception as e:
        print(f"Error fetching project: {e}")
        return redirect(url_for("admin_projects"))

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        organization = request.form.get("organization", "").strip()
        description = request.form.get("description", "").strip()
        status = request.form.get("status", "upcoming").strip()
        date_range = request.form.get("date_range", "").strip()
        location = request.form.get("location", "").strip()
        tags = request.form.get("tags", "").strip()
        is_featured = request.form.get("is_featured") == "on"
        sort_order = request.form.get("sort_order", 0, type=int)

        if not title:
            return render_template("admin_project_form.html", project=project, error="Title is required.")

        try:
            data = {
                "title": title,
                "organization": organization,
                "description": description,
                "status": status,
                "date_range": date_range,
                "location": location,
                "tags": tags,
                "is_featured": is_featured,
                "sort_order": sort_order,
                "updated_at": datetime.now().isoformat(),
            }
            supabase.table("platform_projects").update(data).eq("id", project_id).execute()
            return redirect(url_for("admin_projects"))
        except Exception as e:
            project.update(data)
            return render_template("admin_project_form.html", project=project, error=f"Failed to update project: {e}")

    return render_template("admin_project_form.html", project=project)

@app.route("/admin/projects/<int:project_id>/delete", methods=["POST"])
def admin_project_delete(project_id):
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))
    try:
        supabase.table("platform_projects").delete().eq("id", project_id).execute()
    except Exception as e:
        print(f"Error deleting project: {e}")
    return redirect(url_for("admin_projects"))

# ------------------------- PROJECTS (PUBLIC) -------------------------
@app.route("/projects")
def projects():
    upcoming_projects = []
    try:
        response = supabase.table("platform_projects").select("*").order("sort_order", desc=False).order("created_at", desc=True).execute()
        upcoming_projects = response.data or []
    except Exception as e:
        print(f"Error fetching projects: {e}")
    return render_template("projects.html", projects=upcoming_projects)


@app.route('/manifest.json')
def serve_manifest():
    return send_file('static/manifest.json', mimetype='application/manifest+json')

# ── ADDED: Service Worker scope header fix ───────────────────────────────────
# Without this, sw.js served from /static/ can only control /static/ pages.
# The header tells the browser to allow it to control all pages from root /.
@app.route('/sw.js')
def serve_sw():
    response = send_file('static/sw.js', mimetype='application/javascript')
    response.headers['Service-Worker-Allowed'] = '/'
    response.headers['Cache-Control'] = 'no-cache'
    return response

@app.route('/dashboard')
def go_to_stats():
    return redirect(url_for('metrics'))

@app.route('/metrics')
def metrics():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))
    return render_template("metrics.html")

@app.route('/api/metrics')
def api_metrics():
    if not session.get("admin_logged_in"):
        return jsonify({"error": "Unauthorized"}), 401

    try:
        resp = supabase.table("observations").select("*").execute()
        rows = resp.data or []
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # ── Process species data (same logic as eco_stats.py) ──
    processed = []
    for row in rows:
        s_list = str(row.get('species_list', '')).split(', ')
        c_list = str(row.get('count_list', '')).split(', ')
        m_spp  = str(row.get('species_manual', '')).split(', ')
        m_cnt  = str(row.get('count_manual', '')).split(', ')

        all_spp = s_list + m_spp
        all_cnt = c_list + m_cnt

        for name, cnt in zip(all_spp, all_cnt):
            name = str(name).strip()
            if name and name.lower() not in ('nan', 'none', '', 'other'):
                try:
                    processed.append({
                        'group_id':    row.get('group_id', 'N/A'),
                        'habitat':     row.get('habitat', 'Unknown'),
                        'survey_type': row.get('survey_type', 'N/A'),
                        'species': name,
                        'count': int(float(cnt)) if cnt and str(cnt).lower() != 'nan' else 0,
                        'date':        row.get('date'),
                        'temperature': row.get('temperature'),
                        'humidity':    row.get('humidity'),
                        'rainfall':    row.get('rainfall'),
                        'wind_speed':  row.get('wind_speed'),
                        'light_intensity': row.get('light_intensity'),
                        'canopy_cover':    row.get('canopy_cover'),
                        'latitude':    row.get('latitude'),
                        'longitude':   row.get('longitude'),
                    })
                except:
                    continue

    if not processed:
        return jsonify({"error": "No valid species data found"}), 400

    df = pd.DataFrame(processed)

    # ── Helper functions ──
    def simpson(counts):
        N = counts.sum()
        if N < 2:
            return 0.0
        num = sum(n * (n - 1) for n in counts)
        return round(1 - num / (N * (N - 1)), 3)

    def shannon(counts):
        props = counts / counts.sum()
        props = props[props > 0]
        return round(-np.sum(props * np.log(props)), 3)

    def evenness(sh, richness):
        if richness <= 1:
            return 0.0
        return round(sh / np.log(richness), 3)

    def clean_nan(obj):
        if isinstance(obj, float):
            return None if (np.isnan(obj) or np.isinf(obj)) else obj
        if isinstance(obj, dict):
            return {k: clean_nan(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [clean_nan(v) for v in obj]
        return obj

    # ── 1. Overview ──
    total_organisms  = int(df['count'].sum())
    species_richness = int(df['species'].nunique())
    habitats_list    = sorted(df['habitat'].unique())
    habitats_cnt     = len(habitats_list)
    active_groups    = int(df['group_id'].nunique())
    avg_per_species  = round(total_organisms / species_richness, 1) if species_richness else 0

    hab_comp = df.groupby('habitat', as_index=False)['count'].sum()
    hab_comp = hab_comp.sort_values('count', ascending=False).to_dict('records')

    top15 = df.groupby('species', as_index=False)['count'].sum()
    top15 = top15.nlargest(15, 'count').to_dict('records')

    # ── 2. Biodiversity ──
    diversity = []
    for hab in habitats_list:
        hdf   = df[df['habitat'] == hab]
        cnts  = hdf['count']
        rich  = hdf['species'].nunique()
        simp  = simpson(cnts)
        shan  = shannon(cnts)
        even  = evenness(shan, rich)
        diversity.append({
            'habitat': hab, 'richness': rich,
            'simpson': simp, 'shannon': shan, 'evenness': even,
        })

    # ── 3. Species Analysis ──
    top_spp = df.groupby('species', as_index=False)['count'].sum()
    top_spp = top_spp.sort_values('count', ascending=False).head(20).to_dict('records')

    # Species × Habitat heatmap (top 25 species)
    top25_spp = df.groupby('species')['count'].sum().nlargest(25).index
    hm_df = df[df['species'].isin(top25_spp)].pivot_table(
        index='species', columns='habitat', values='count', aggfunc='sum', fill_value=0
    )
    hm_species = list(hm_df.index)
    hm_habitats = list(hm_df.columns)
    hm_values = hm_df.values.tolist()

    # ── 4. Environment ──
    env_vars = ['temperature', 'humidity', 'rainfall', 'wind_speed',
                'light_intensity', 'canopy_cover']
    avail_vars = [v for v in env_vars if v in df.columns and df[v].notna().any()]

    env_summary = {}
    if avail_vars:
        for hab in habitats_list:
            hdf = df[df['habitat'] == hab]
            env_summary[hab] = {}
            for v in avail_vars:
                vals = hdf[v].dropna().astype(float)
                env_summary[hab][v] = {
                    'mean': round(vals.mean(), 2) if len(vals) else None,
                    'std':  round(vals.std(), 2)  if len(vals) > 1 else None,
                }

        corr = df[avail_vars].astype(float).corr().round(3)
        corr_labels = list(corr.columns)
        corr_matrix = corr.values.tolist()
    else:
        corr_labels = []
        corr_matrix = []

    # ── 5. Spatial ──
    spatial = df[['latitude', 'longitude', 'habitat', 'species', 'count']].dropna().to_dict('records')

    # ── 6. Raw Data ──
    raw_cols = df.columns.tolist()
    raw_rows = df.head(500).values.tolist()

    return jsonify(clean_nan({
        'overview': {
            'total_organisms':  total_organisms,
            'species_richness': species_richness,
            'habitats_surveyed': habitats_cnt,
            'active_groups':    active_groups,
            'avg_per_species':  avg_per_species,
            'habitat_composition': hab_comp,
            'species_distribution': top15,
        },
        'biodiversity': {
            'indices': diversity,
        },
        'species_analysis': {
            'top_species': top_spp,
            'heatmap': {
                'species': hm_species,
                'habitats': hm_habitats,
                'values': hm_values,
            },
        },
        'environment': {
            'available_vars': avail_vars,
            'summary': env_summary,
            'correlation': {
                'labels': corr_labels,
                'matrix': corr_matrix,
            },
        },
        'spatial': {
            'data': spatial,
        },
        'raw_data': {
            'columns': raw_cols,
            'rows': raw_rows,
        },
    }))

# ------------------------- VIEW NEW SPECIES -------------------------
@app.route("/add_species")
def add_species():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))
    
    new_species_rows = []
    unique_species = set()
    total_count = 0

    try:
        response = supabase.table("observations").select("*").execute()
        for row in response.data:
            s_manual = row.get("species_manual") or ""
            c_manual = row.get("count_manual") or ""
            
            m_names  = [n.strip() for n in str(s_manual).split(",") if n.strip()] if s_manual else []
            m_counts = [c.strip() for c in str(c_manual).split(",")   if c.strip()] if c_manual else []

            if not m_names:
                continue

            row["zipped_manual"] = list(zip(m_names, m_counts))

            for sp, cnt in row["zipped_manual"]:
                unique_species.add(sp)
                try:
                    total_count += int(cnt)
                except (ValueError, TypeError):
                    pass

            new_species_rows.append(row)
    except Exception as e:
        print(f"Error fetching manual species: {e}")

    return render_template(
        "add_species.html",
        new_species_rows=new_species_rows,
        group_id='All Groups',
        total_entries=len(new_species_rows),
        unique_species=unique_species,
        total_count=total_count,
    )
    
# ------------------------- NOTIFICATIONS -------------------------
@app.route("/admin/send_notification", methods=["POST"])
def send_notification():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))

    title = request.form.get("title", "").strip()
    msg_body = request.form.get("message", "").strip()
    if title and msg_body:
        try:
            data = {
                "title": title,
                "message": msg_body,
                "created_at": datetime.now().isoformat()
            }
            attachment = request.files.get("attachment")
            if attachment and attachment.filename and allowed_notification_file(attachment.filename):
                original_name = secure_filename(attachment.filename)
                ts = datetime.now().strftime('%Y%m%d%H%M%S%f')
                unique_name = f"{ts}_{original_name}"
                save_path = os.path.join(NOTIF_UPLOAD_FOLDER, unique_name)
                attachment.save(save_path)
                data["attachment_file"] = unique_name
                data["attachment_original_name"] = original_name
            supabase.table("notifications").insert(data).execute()
            flash("Notification sent successfully!", "success")
        except Exception as e:
            flash(f"Failed to send notification: {e}", "error")
    else:
        flash("Please fill in both title and message.", "error")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/sent_notifications")
def admin_sent_notifications():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))

    sent_notifications = []
    try:
        notif_response = supabase.table("notifications").select("*").execute()
        all_notifs = notif_response.data or []
        sent_notifications = sorted(all_notifs, key=lambda n: n.get("created_at") or "", reverse=True)
        for n in sent_notifications:
            created = n.get("created_at") or ""
            if isinstance(created, str):
                n["created_at"] = created[:19].replace("T", " ")
    except Exception as e:
        print(f"Error fetching sent notifications: {e}")

    return render_template(
        "admin_sent_notifications.html",
        sent_notifications=sent_notifications,
    )

@app.route("/notifications")
def notifications_page():
    if "user_role" not in session:
        return redirect(url_for('login'))
    return render_template("notifications.html")


@app.route("/api/notifications/<int:notification_id>/download")
def download_notification_attachment(notification_id):
    try:
        resp = supabase.table("notifications").select("*").eq("id", notification_id).execute()
        if not resp.data:
            return jsonify({"error": "Notification not found"}), 404
        n = resp.data[0]
        attachment_file = n.get("attachment_file")
        attachment_original_name = n.get("attachment_original_name")
        if not attachment_file:
            return jsonify({"error": "No attachment"}), 404
        file_path = os.path.join(NOTIF_UPLOAD_FOLDER, attachment_file)
        if not os.path.exists(file_path):
            return jsonify({"error": "File not found"}), 404
        mime = mimetypes.guess_type(attachment_original_name or attachment_file)[0] or "application/octet-stream"
        return send_file(
            file_path,
            as_attachment=True,
            download_name=attachment_original_name or attachment_file,
            mimetype=mime
        )
    except Exception as e:
        print(f"Error downloading attachment: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/notifications")
def get_notifications():
    group_id = session.get("group_id")
    try:
        notif_response = supabase.table("notifications").select("*").execute()
        notifications = []
        limit = request.args.get("limit", 20, type=int)
        if notif_response.data:
            notifications = sorted(notif_response.data, key=lambda x: x.get("created_at", ""), reverse=True)[:limit]

        if group_id:
            reads_response = supabase.table("notification_reads").select("notification_id").eq("group_id", group_id).execute()
            read_ids = {r["notification_id"] for r in reads_response.data} if reads_response.data else set()
        else:
            read_ids = set()

        unread_count = 0
        for n in notifications:
            n["read"] = n["id"] in read_ids
            if not n["read"]:
                unread_count += 1
            created = n.get("created_at") or ""
            if isinstance(created, str):
                n["created_at"] = created[:19].replace("T", " ")

        return jsonify({"notifications": notifications, "unread_count": unread_count})
    except Exception as e:
        print(f"Error fetching notifications: {e}")
        return jsonify({"notifications": [], "unread_count": 0})


@app.route("/api/notifications/<int:notification_id>/read", methods=["POST"])
def mark_notification_read(notification_id):
    group_id = session.get("group_id")
    if not group_id:
        return jsonify({"error": "Not logged in"}), 401

    try:
        supabase.table("notification_reads").insert({
            "notification_id": notification_id,
            "group_id": group_id,
            "read_at": datetime.now().isoformat()
        }).execute()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ------------------------- AI IDENTIFICATION (GEMINI) -------------------------

import base64 as _base64
import io as _io
import json
import os
import random as _random
import time as _time

import requests as _requests


# ---------------------------------------------------------------------------
# Gemini configuration
# ---------------------------------------------------------------------------

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

# Use a stable model as the primary model. You can override this in .env.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash").strip()

# Comma-separated fallback models in .env, for example:
# GEMINI_FALLBACK_MODELS=gemini-3.5-flash,gemini-flash-latest
#
# Strip whitespace and remove duplicates so values such as
# "model-a, model-b" work correctly.
GEMINI_FALLBACK_MODELS = [
    model.strip()
    for model in os.getenv(
        "GEMINI_FALLBACK_MODELS",
        "gemini-3.5-flash,gemini-flash-latest",
    ).split(",")
    if model.strip()
]

GEMINI_BASE_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)

GEMINI_MAX_IMAGE_DIM = int(os.getenv("GEMINI_MAX_IMAGE_DIM", "1024"))

# The flash model family spends output tokens on reasoning before emitting
# the JSON answer, so a small budget truncates the result mid-JSON.
GEMINI_MAX_OUTPUT_TOKENS = int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "8192"))

# Hard ceiling for automatic budget expansion after a MAX_TOKENS truncation.
GEMINI_MAX_OUTPUT_TOKENS_CAP = int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS_CAP", "32768"))

# Number of attempts for transient errors on the same model.
GEMINI_MAX_ATTEMPTS = int(os.getenv("GEMINI_MAX_ATTEMPTS", "3"))

# Initial exponential-backoff delay in seconds.
GEMINI_INITIAL_BACKOFF = float(os.getenv("GEMINI_INITIAL_BACKOFF", "1.0"))

# Maximum delay between retries.
GEMINI_MAX_BACKOFF = float(os.getenv("GEMINI_MAX_BACKOFF", "8.0"))

# HTTP timeout for one Gemini request. Larger token budgets need more time.
GEMINI_TIMEOUT = int(os.getenv("GEMINI_TIMEOUT", "60"))


# ---------------------------------------------------------------------------
# Custom errors
# ---------------------------------------------------------------------------

class GeminiTemporaryUnavailable(RuntimeError):
    """Gemini was temporarily unavailable after retries/fallbacks."""


class GeminiConfigurationError(RuntimeError):
    """Gemini configuration/authentication problem."""


class GeminiRequestError(RuntimeError):
    """Gemini rejected the request or returned an unexpected error."""


# ---------------------------------------------------------------------------
# Image preparation
# ---------------------------------------------------------------------------

def _prep_image(image_bytes, mime):
    """Downscale and re-encode an uploaded photo before sending it to Gemini.

    The image is capped at GEMINI_MAX_IMAGE_DIM on its longest edge.
    JPEG is used to keep request size reasonable while retaining useful
    visual detail for species/habitat identification.
    """
    if not image_bytes:
        return image_bytes, mime

    try:
        from PIL import Image as _Img

        img = _Img.open(_io.BytesIO(image_bytes))
        img.thumbnail(
            (GEMINI_MAX_IMAGE_DIM, GEMINI_MAX_IMAGE_DIM),
            _Img.LANCZOS,
        )

        # JPEG does not support transparency/paletted modes safely.
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")

        buf = _io.BytesIO()
        img.save(
            buf,
            format="JPEG",
            quality=82,
            optimize=True,
        )

        return buf.getvalue(), "image/jpeg"

    except Exception:
        # If preprocessing fails, send the original image rather than
        # turning an image-processing problem into an AI failure.
        return image_bytes, mime


# ---------------------------------------------------------------------------
# Retry helpers
# ---------------------------------------------------------------------------

def _retry_delay(attempt, response=None):
    """Calculate exponential backoff with jitter.

    attempt is zero-based:
        attempt 0 -> no delay before the first request
        attempt 1 -> ~1 second
        attempt 2 -> ~2 seconds
        attempt 3 -> ~4 seconds
        ...
    """
    if attempt <= 0:
        return 0.0

    # Respect Retry-After when the API provides it.
    if response is not None:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), GEMINI_MAX_BACKOFF)
            except (TypeError, ValueError):
                pass

    base = GEMINI_INITIAL_BACKOFF * (2 ** (attempt - 1))
    base = min(base, GEMINI_MAX_BACKOFF)

    # Add 0–25% jitter so simultaneous EcoField users do not all retry
    # at exactly the same moment.
    jitter = _random.uniform(0, base * 0.25)
    return min(base + jitter, GEMINI_MAX_BACKOFF)


def _model_list():
    """Return the primary model followed by unique fallback models."""
    models = [GEMINI_MODEL] + GEMINI_FALLBACK_MODELS

    # Preserve order while removing duplicates and empty values.
    return list(dict.fromkeys(m.strip() for m in models if m and m.strip()))


# ---------------------------------------------------------------------------
# Gemini request
# ---------------------------------------------------------------------------

def _gemini_generate(prompt, image_bytes=None, mime="image/jpeg"):
    """Send a prompt/image to Gemini with retries and model fallbacks.

    Important behavior:
    - 429/5xx/503 errors are treated as potentially temporary.
    - A transient error is retried with exponential backoff.
    - 404 moves to the next configured model.
    - 401/403 reports an API-key/permission problem.
    - 400 reports a request/prompt problem.
    - Temporary-unavailable errors are reported separately from
      "model not available for this account".
    """
    if not GEMINI_API_KEY:
        raise GeminiConfigurationError(
            "GEMINI_API_KEY is not configured. Add it to your environment."
        )

    contents = []

    if image_bytes:
        b64_img = _base64.b64encode(image_bytes).decode("ascii")

        contents = [{
            "parts": [
                {"text": prompt},
                {
                    "inline_data": {
                        "mime_type": mime,
                        "data": b64_img,
                    }
                },
            ]
        }]
    else:
        contents = [{
            "parts": [
                {"text": prompt},
            ]
        }]

    models_to_try = _model_list()

    # These statuses can be transient and are therefore retried.
    retryable_statuses = {429, 500, 502, 503, 504}

    last_error = None
    saw_temporary_failure = False

    # Grows when Gemini reports a MAX_TOKENS truncation so the retry has room.
    max_output_tokens = GEMINI_MAX_OUTPUT_TOKENS

    for model in models_to_try:
        for attempt in range(GEMINI_MAX_ATTEMPTS):
            try:
                delay = _retry_delay(attempt)

                if delay:
                    _time.sleep(delay)

                started = _time.monotonic()

                resp = _requests.post(
                    GEMINI_BASE_URL.format(model=model),
                    params={"key": GEMINI_API_KEY},
                    json={
                        "contents": contents,
                        "generationConfig": {
                            "maxOutputTokens": max_output_tokens,
                            "temperature": 0.4,
                            "responseMimeType": "application/json",
                        },
                    },
                    timeout=GEMINI_TIMEOUT,
                )

                elapsed = _time.monotonic() - started

                # Keep useful information in server logs without exposing
                # the API key or full response to the user.
                print(
                    f"[Gemini] model={model} "
                    f"status={resp.status_code} "
                    f"attempt={attempt + 1}/{GEMINI_MAX_ATTEMPTS} "
                    f"time={elapsed:.2f}s"
                )

            except _requests.exceptions.RequestException as e:
                last_error = f"{model}: network error: {e}"
                saw_temporary_failure = True
                print(f"[Gemini] {last_error}")

                # Retry the same model unless this was the final attempt.
                if attempt < GEMINI_MAX_ATTEMPTS - 1:
                    continue

                break

            status = resp.status_code

            # Success.
            if 200 <= status < 300:
                try:
                    data = resp.json()
                except ValueError:
                    raise GeminiRequestError(
                        "Gemini returned a response that was not valid JSON."
                    )

                candidate = (
                    data.get("candidates", [{}])[0]
                    if data.get("candidates")
                    else {}
                )
                parts = candidate.get("content", {}).get("parts", [])
                finish_reason = candidate.get("finishReason")

                texts = [
                    part.get("text", "")
                    for part in parts
                    if isinstance(part, dict) and part.get("text")
                ]
                joined_text = "".join(texts)

                if not joined_text:
                    # Sometimes the API can return a response without usable
                    # candidate text (for example, a blocked/empty response).
                    raise GeminiRequestError(
                        "Gemini returned no usable analysis result"
                        + (
                            f" (finish reason: {finish_reason})."
                            if finish_reason
                            else "."
                        )
                    )

                if finish_reason == "MAX_TOKENS":
                    last_error = (
                        f"{model}: output truncated at {max_output_tokens} "
                        "tokens (MAX_TOKENS)"
                    )
                    print(f"[Gemini] {last_error}; retrying with a larger budget")

                    if (
                        attempt < GEMINI_MAX_ATTEMPTS - 1
                        and max_output_tokens < GEMINI_MAX_OUTPUT_TOKENS_CAP
                    ):
                        max_output_tokens = min(
                            max_output_tokens * 2,
                            GEMINI_MAX_OUTPUT_TOKENS_CAP,
                        )
                        continue

                    raise GeminiRequestError(
                        "Gemini output was truncated (finish reason: "
                        "MAX_TOKENS) and remained incomplete after retries."
                    )

                return joined_text

            # Temporary service/rate-limit failures.
            if status in retryable_statuses:
                last_error = (
                    f"{model}: HTTP {status} - {resp.text[:300]}"
                )
                saw_temporary_failure = True
                print(f"[Gemini] {last_error}")

                # Retry this same model. After the final attempt, move to
                # the next configured model.
                continue

            # Model doesn't exist / isn't available through this endpoint.
            if status == 404:
                last_error = f"{model}: HTTP 404 - {resp.text[:300]}"
                print(f"[Gemini] Model unavailable: {last_error}")
                break

            # Authentication/permission problem.
            if status in {401, 403}:
                raise GeminiConfigurationError(
                    f"Gemini API authentication/permission error "
                    f"(HTTP {status}). Check GEMINI_API_KEY and API access."
                )

            # Bad request: retrying the same payload won't normally fix it.
            if status == 400:
                raise GeminiRequestError(
                    f"Gemini rejected the request (HTTP 400): "
                    f"{resp.text[:500]}"
                )

            # Other unexpected response.
            raise GeminiRequestError(
                f"Gemini returned HTTP {status}: {resp.text[:500]}"
            )

    # Do not claim that all models are unavailable "for this account" when
    # the actual problem was temporary service overload.
    if saw_temporary_failure:
        raise GeminiTemporaryUnavailable(
            "The Gemini AI service is temporarily unavailable or "
            "experiencing high demand. Please try again in a moment."
            + (f" Technical detail: {last_error}" if last_error else "")
        )

    raise GeminiConfigurationError(
        "None of the configured Gemini models could process the request."
        + (f" Last error: {last_error}" if last_error else "")
    )


# ---------------------------------------------------------------------------
# JSON extraction / repair
# ---------------------------------------------------------------------------

def _extract_json(text):
    """Best-effort extraction of a JSON object/array from Gemini output."""
    text = text.strip()
    candidates = [text]

    brace_start = text.find("{")
    brace_end = text.rfind("}")

    if brace_start != -1 and brace_end != -1 and brace_end > brace_start:
        candidates.append(text[brace_start:brace_end + 1])
        core = text[brace_start:brace_end + 1]
    else:
        core = None

    bracket_start = text.find("[")
    bracket_end = text.rfind("]")

    for candidate in candidates:
        try:
            json.loads(candidate)
            return candidate
        except Exception:
            continue

    if core is not None:
        repaired = _repair_truncated_json(core)
        if repaired is not None:
            return repaired

    if bracket_start != -1 and bracket_end != -1 and bracket_end > bracket_start:
        brackets = text[bracket_start:bracket_end + 1]

        try:
            json.loads(brackets)
            return brackets
        except Exception:
            brackets_repaired = _repair_truncated_json(brackets)
            if brackets_repaired is not None:
                return brackets_repaired

    if core is not None:
        return core

    if bracket_start != -1 and bracket_end != -1 and bracket_end > bracket_start:
        return text[bracket_start:bracket_end + 1]

    return text


def _repair_truncated_json(core):
    """Try to close JSON that was truncated by the output token limit."""
    stack = []
    in_string = False
    escaped = False

    for ch in core:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch in "[{":
            stack.append(ch)
        elif ch in "]}" and stack:
            stack.pop()

    if not stack:
        return None

    closing = {
        "{": "}",
        "[": "]",
    }

    def try_load(value):
        try:
            json.loads(value)
            return True
        except Exception:
            return False

    repaired = core

    while stack:
        if try_load(repaired):
            return repaired

        repaired += closing[stack.pop()]

    return repaired if try_load(repaired) else None


# ---------------------------------------------------------------------------
# AI prompts
# ---------------------------------------------------------------------------

def _species_prompt():
    return (
        "You are an expert ecologist and conservation biologist. "
        "Analyze the attached field photograph and identify the animal or "
        "plant species shown.\n\n"
        "Respond ONLY with a valid JSON object in exactly this structure:\n"
        '{"species":[{"common_name":"...","scientific_name":"...",'
        '"confidence":0.0,"family":"...","notes":"..."}],'
        '"summary":"..."}\n\n'
        "Provide 1 to 3 ranked candidate species. "
        "Confidence must be a number between 0 and 1 and represents the "
        "model's visual assessment, not scientific certainty. "
        "In notes, mention the key identifying traits visible in the image. "
        "Do not invent a species when the image does not provide enough "
        "evidence. If identification is not possible, return an empty "
        "species array and explain why in summary."
    )


def _habitat_prompt():
    return (
        "You are an expert ecologist. Analyze the attached field photograph "
        "and classify the visible habitat/ecosystem.\n\n"
        "Respond ONLY with a valid JSON object in exactly this structure:\n"
        '{"habitat_type":"...","vegetation":"...",'
        '"soil_or_substrate":"...","climate_indicators":"...",'
        '"confidence":0.0,"notes":"..."}\n\n'
        "Be specific when the image supports it, for example tropical "
        "rainforest, coastal savanna, wetland, montane forest, woodland, "
        "grassland, or forest floor. Confidence must be between 0 and 1. "
        "Do not claim environmental conditions that cannot reasonably be "
        "inferred from the photograph."
    )


def _species_and_habitat_prompt():
    return (
        "You are an expert ecologist and conservation biologist. "
        "Analyze the attached field photograph for BOTH species "
        "identification and habitat classification.\n\n"
        "Return ONLY one valid JSON object in exactly this structure:\n"
        "{"
        '"species":[{"common_name":"...","scientific_name":"...",'
        '"confidence":0.0,"family":"...","notes":"..."}],'
        '"species_summary":"...",'
        '"habitat":{'
        '"habitat_type":"...",'
        '"vegetation":"...",'
        '"soil_or_substrate":"...",'
        '"climate_indicators":"...",'
        '"confidence":0.0,'
        '"notes":"..."'
        "}"
        "}\n\n"
        "Species requirements:\n"
        "- Provide 1 to 3 ranked candidate species.\n"
        "- Confidence must be between 0 and 1.\n"
        "- Confidence is the model's visual assessment, not scientific "
        "certainty.\n"
        "- Mention visible identifying traits in notes.\n"
        "- Do not invent a species when the evidence is insufficient.\n"
        "- If identification is not possible, return an empty species array "
        "and explain why in species_summary.\n\n"
        "Habitat requirements:\n"
        "- Classify only what the photograph reasonably supports.\n"
        "- Confidence must be between 0 and 1.\n"
        "- Do not invent environmental conditions that cannot reasonably be "
        "inferred from the image."
    )


# ---------------------------------------------------------------------------
# Analysis functions
# ---------------------------------------------------------------------------

def identify_species_from_photo(image_bytes, mime):
    raw = _gemini_generate(
        _species_prompt(),
        image_bytes,
        mime,
    )
    return _extract_json(raw)


def classify_habitat_from_photo(image_bytes, mime):
    raw = _gemini_generate(
        _habitat_prompt(),
        image_bytes,
        mime,
    )
    return _extract_json(raw)


def identify_species_and_habitat_from_photo(image_bytes, mime):
    """Analyze species + habitat in ONE Gemini request."""
    raw = _gemini_generate(
        _species_and_habitat_prompt(),
        image_bytes,
        mime,
    )
    return _extract_json(raw)


# ---------------------------------------------------------------------------
# Pages / API
# ---------------------------------------------------------------------------

@app.route("/ai-identify")
def ai_identify():
    ai_available = bool(GEMINI_API_KEY)

    return render_template(
        "ai_identify.html",
        ai_available=ai_available,
        gemini_model=GEMINI_MODEL,
    )


@app.route("/api/ai-identify", methods=["POST"])
def api_ai_identify():
    if not GEMINI_API_KEY:
        return jsonify({
            "error": (
                "AI identification is not configured. "
                "Ask an administrator to set GEMINI_API_KEY."
            )
        }), 503

    analysis_type = request.form.get("analysis_type", "both").strip().lower()

    if analysis_type not in {"both", "species", "habitat"}:
        return jsonify({
            "error": "Invalid analysis type. Choose both, species or habitat."
        }), 400

    file = request.files.get("photo")

    if not file or not file.filename:
        return jsonify({"error": "Please upload a photo first."}), 400

    if not allowed_file(file.filename):
        return jsonify({
            "error": (
                "Unsupported file type. "
                "Use PNG, JPG, JPEG, GIF or WEBP."
            )
        }), 400

    mime = file.mimetype or (
        "image/jpeg"
        if file.filename.lower().endswith((".jpg", ".jpeg"))
        else "image/png"
    )

    image_bytes = file.read()

    if not image_bytes:
        return jsonify({"error": "The uploaded image is empty."}), 400

    image_bytes, mime = _prep_image(image_bytes, mime)

    result = {
        "analysis_type": analysis_type,
        "species": None,
        "habitat": None,
    }

    try:
        # IMPORTANT:
        # For "both", use ONE Gemini request instead of two simultaneous
        # requests. This reduces load, rate-limit pressure and the chance
        # that one half of the analysis fails while the other succeeds.
        if analysis_type == "both":
            raw = identify_species_and_habitat_from_photo(
                image_bytes,
                mime,
            )

            combined = json.loads(raw)

            result["species"] = combined.get("species", [])
            result["species_summary"] = combined.get("species_summary", "")
            result["habitat"] = combined.get("habitat")

        elif analysis_type == "species":
            raw = identify_species_from_photo(
                image_bytes,
                mime,
            )

            species = json.loads(raw)

            result["species"] = species.get("species", [])
            result["species_summary"] = species.get("summary", "")

        else:
            raw = classify_habitat_from_photo(
                image_bytes,
                mime,
            )

            habitat = json.loads(raw)

            result["habitat"] = habitat

    except GeminiTemporaryUnavailable as e:
        # 503/429/temporary network problems.
        # Tell the frontend this is temporary instead of incorrectly saying
        # that the user's account has no available models.
        print(f"[Gemini temporary unavailable] {e}")

        return jsonify({
            "error": (
                "AI analysis is temporarily unavailable because the "
                "AI service is experiencing high demand. "
                "Please try again in a moment."
            ),
            "error_type": "temporary_unavailable",
            "retryable": True,
        }), 503

    except GeminiConfigurationError as e:
        print(f"[Gemini configuration error] {e}")

        return jsonify({
            "error": (
                "The AI service is not configured correctly. "
                "Please contact an administrator."
            ),
            "error_type": "configuration",
            "retryable": False,
        }), 503

    except GeminiRequestError as e:
        print(f"[Gemini request error] {e}")

        return jsonify({
            "error": (
                "The AI could not process this image. "
                "Please try another clear photo."
            ),
            "error_type": "request_error",
            "retryable": True,
        }), 502

    except json.JSONDecodeError:
        print("[Gemini] Returned an unreadable JSON result.")

        return jsonify({
            "error": (
                "The AI returned an unreadable result. "
                "Please try again."
            ),
            "error_type": "invalid_ai_response",
            "retryable": True,
        }), 502

    except Exception as e:
        # Keep unexpected technical details in server logs, not in the UI.
        print(f"[Gemini unexpected identification error] {e}")

        return jsonify({
            "error": (
                "Something went wrong while analyzing the image. "
                "Please try again."
            ),
            "error_type": "unexpected",
            "retryable": True,
        }), 500

    return jsonify(result)



# ------------------------- ADMIN RESEARCH REPORTS -------------------------

def _report_filters(args):
    """Extract report filter values from a request args object."""
    return {
        "year": args.get("year", "").strip(),
        "group": args.get("group", "").strip(),
        "habitat": args.get("habitat", "").strip(),
        "survey_type": args.get("survey_type", "").strip(),
        "date_from": args.get("date_from", "").strip(),
        "date_to": args.get("date_to", "").strip(),
    }


def _apply_report_filters(records, filters):
    f = filters
    out = []
    for r in records:
        if f["year"] and r.get("academic_year", "") != f["year"]:
            continue
        if f["group"] and r.get("group_id", "") != f["group"]:
            continue
        if f["habitat"] and r.get("habitat", "") != f["habitat"]:
            continue
        if f["survey_type"] and r.get("survey_type", "") != f["survey_type"]:
            continue
        ts = str(r.get("timestamp") or "")[:10]
        if f["date_from"] and ts and ts < f["date_from"]:
            continue
        if f["date_to"] and ts and ts > f["date_to"]:
            continue
        out.append(r)
    return out


def _observation_records(rows):
    """Convert raw observation rows into tidy long records (one per species occurrence)."""
    records = []
    for row in rows:
        s_list = [x.strip() for x in str(row.get("species_list") or "").split(",") if x.strip()]
        c_list = [x.strip() for x in str(row.get("count_list") or "").split(",")]
        m_list = [x.strip() for x in str(row.get("method_list") or "").split(",")]
        manual_s = [x.strip() for x in str(row.get("species_manual") or "").split(",") if x.strip()]
        manual_c = [x.strip() for x in str(row.get("count_manual") or "").split(",")]
        manual_m = [x.strip() for x in str(row.get("method_manual") or "").split(",")]

        entries = []
        for i, sp in enumerate(s_list):
            if sp.lower() in ("", "nan", "none", "other"):
                continue
            try:
                cnt = int(float(c_list[i])) if i < len(c_list) and c_list[i] not in ("", "nan", "None") else 0
            except (ValueError, IndexError):
                cnt = 0
            entries.append((sp, cnt, m_list[i] if i < len(m_list) else "", "Standard"))
        for i, sp in enumerate(manual_s):
            if sp.lower() in ("", "nan", "none"):
                continue
            try:
                cnt = int(float(manual_c[i])) if i < len(manual_c) and manual_c[i] not in ("", "nan", "None") else 0
            except (ValueError, IndexError):
                cnt = 0
            entries.append((sp, cnt, manual_m[i] if i < len(manual_m) else "", "Manual"))

        if not entries:
            continue

        base = {
            "observation_id": row.get("id") or "",
            "timestamp": str(row.get("timestamp") or row.get("created_at") or ""),
            "academic_year": str(row.get("year_group") or ""),
            "group_id": str(row.get("group_id") or ""),
            "member_name": str(row.get("member_name") or ""),
            "student_id": str(row.get("student_id") or ""),
            "habitat": str(row.get("habitat") or ""),
            "location": str(row.get("location") or ""),
            "site_location": str(row.get("site_location") or ""),
            "latitude": str(row.get("latitude") or ""),
            "longitude": str(row.get("longitude") or ""),
            "survey_type": str(row.get("survey_type") or ""),
            "temperature": str(row.get("temperature") or ""),
            "humidity": str(row.get("humidity") or ""),
            "rainfall": str(row.get("rainfall") or ""),
            "wind_speed": str(row.get("wind_speed") or ""),
            "wind_direction": str(row.get("wind_direction") or ""),
            "light_intensity": str(row.get("light_intensity") or ""),
            "canopy_cover": str(row.get("canopy_cover") or ""),
            "canopy_height": str(row.get("canopy_height") or ""),
            "notes": str(row.get("notes") or ""),
            "photo_files": str(row.get("photo_files") or ""),
        }
        for sp, cnt, method, src in entries:
            rec = dict(base)
            rec["species"] = sp
            rec["count"] = cnt
            rec["method"] = method
            rec["species_source"] = src
            records.append(rec)
    return records


def _distinct_options(rows, field):
    opts = set()
    for r in rows:
        val = str(r.get(field) or "").strip()
        if val and val.lower() not in ("nan", "none"):
            opts.add(val)
    return sorted(opts)


def _research_report_data(records):
    """Compute summary statistics from tidy report records."""
    overview = {}
    species_summary = []
    habitat_summary = []
    env_summary = {}

    if not records:
        return overview, species_summary, habitat_summary, env_summary

    df = pd.DataFrame(records)
    df["count"] = pd.to_numeric(df["count"], errors="coerce").fillna(0).astype(int)

    def simpson(counts):
        N = int(counts.sum())
        if N < 2:
            return 0.0
        num = sum(n * (n - 1) for n in counts)
        return round(1 - num / (N * (N - 1)), 3)

    def shannon(counts):
        props = counts / counts.sum()
        props = props[props > 0]
        return round(-np.sum(props * np.log(props)), 3)

    def evenness(sh, rich):
        if rich <= 1:
            return 0.0
        return round(sh / np.log(rich), 3)

    total_individuals = int(df["count"].sum())
    species_richness = int(df["species"].nunique())
    habitats = sorted(set(h for h in df["habitat"] if h))
    groups = sorted(set(g for g in df["group_id"] if g))
    survey_types = sorted(set(s for s in df["survey_type"] if s))

    overview = {
        "observations": int(len(df)),
        "species_richness": species_richness,
        "total_individuals": total_individuals,
        "habitat_count": len(habitats),
        "habitats": habitats,
        "group_count": len(groups),
        "groups": groups,
        "survey_types": survey_types,
        "avg_per_species": round(total_individuals / species_richness, 1) if species_richness else 0,
    }

    grp = df.groupby("species")["count"]
    for sp, gr in grp:
        species_summary.append({
            "species": sp,
            "total_count": int(gr.sum()),
            "observations": int(gr.count()),
        })
    species_summary.sort(key=lambda x: x["total_count"], reverse=True)

    for hab, hdf in df.groupby("habitat"):
        if not hab:
            continue
        cnts = hdf["count"]
        rich = int(hdf["species"].nunique())
        sh = shannon(pd.to_numeric(cnts, errors="coerce").fillna(0))
        habitat_summary.append({
            "habitat": hab,
            "richness": rich,
            "total_count": int(cnts.sum()),
            "observations": int(len(hdf)),
            "groups": int(hdf["group_id"].nunique()),
            "simpson": simpson(pd.to_numeric(cnts, errors="coerce").fillna(0)),
            "shannon": sh,
            "evenness": evenness(sh, rich),
        })
    habitat_summary.sort(key=lambda x: x["total_count"], reverse=True)

    env_vars = ["temperature", "humidity", "rainfall", "wind_speed", "light_intensity", "canopy_cover"]
    for hab, hdf in df.groupby("habitat"):
        if not hab:
            continue
        env_summary[hab] = {}
        for v in env_vars:
            vals = pd.to_numeric(hdf[v], errors="coerce").dropna().astype(float)
            env_summary[hab][v] = {
                "mean": round(vals.mean(), 2) if len(vals) else None,
                "std": round(vals.std(), 2) if len(vals) > 1 else None,
            }

    return overview, species_summary, habitat_summary, env_summary


def _load_report_records(filters):
    """Fetch all observations, expand to tidy records, and apply filters."""
    try:
        resp = supabase.table("observations").select("*").execute()
        rows = resp.data or []
    except Exception as e:
        print("Error fetching observations for report:", e)
        rows = []
    records = _observation_records(rows)
    return _apply_report_filters(records, filters)


@app.route("/admin/research_reports")
def admin_research_reports():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))

    filters = _report_filters(request.args)
    records = _load_report_records(filters)
    overview, species_summary, habitat_summary, env_summary = _research_report_data(records)

    # Options for the filter dropdowns (based on all observations, not filtered)
    try:
        all_rows = supabase.table("observations").select("*").execute().data or []
    except Exception as e:
        print("Error fetching observations for filters:", e)
        all_rows = []

    years = _distinct_options(all_rows, "year_group")
    groups = _distinct_options(all_rows, "group_id")
    habitats = _distinct_options(all_rows, "habitat")
    survey_types = _distinct_options(all_rows, "survey_type")

    report_records = records[:200]

    return render_template(
        "admin_research_reports.html",
        filters=filters,
        overview=overview,
        species_summary=species_summary[:20],
        habitat_summary=habitat_summary,
        env_summary=env_summary,
        records=report_records,
        years=years,
        groups=groups,
        habitats=habitats,
        survey_types=survey_types,
        total_records=len(records),
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


def _common_export_context(filters):
    records = _load_report_records(filters)
    overview, species_summary, habitat_summary, env_summary = _research_report_data(records)
    return records, overview, species_summary, habitat_summary, env_summary


@app.route("/admin/research_reports/export.csv")
def admin_research_reports_export_csv():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))
    filters = _report_filters(request.args)
    records, overview, _, _, _ = _common_export_context(filters)

    import io as _io
    import csv as _csv

    buf = _io.StringIO()
    if records:
        columns = list(records[0].keys())
        writer = _csv.DictWriter(buf, fieldnames=columns)
        writer.writeheader()
        writer.writerows(records)
    else:
        buf.write("No records match the selected filters.\n")

    fname = f"ecofield_research_data_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )


@app.route("/admin/research_reports/export.xlsx")
def admin_research_reports_export_xlsx():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))
    filters = _report_filters(request.args)
    records, overview, species_summary, habitat_summary, env_summary = _common_export_context(filters)

    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    import io as _io

    wb = Workbook()

    header_fill = PatternFill("solid", fgColor="2E7D32")
    header_font = Font(color="FFFFFF", bold=True)

    def style_header(ws, row=1):
        for cell in ws[row]:
            cell.fill = header_fill
            cell.font = header_font

    # ── 1. Metadata sheet ──
    ws = wb.active
    ws.title = "Metadata"
    meta_rows = [
        ("Report", "EcoField Logger - Research Report"),
        ("Generated", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ("Institution", "University of Ghana - Department of Animal Biology and Conservation Science"),
        ("Academic Year", filters["year"] or "All"),
        ("Group", filters["group"] or "All"),
        ("Habitat", filters["habitat"] or "All"),
        ("Survey Type", filters["survey_type"] or "All"),
        ("Date From", filters["date_from"] or "Any"),
        ("Date To", filters["date_to"] or "Any"),
        ("Total Records (species occurrences)", overview.get("observations", 0)),
        ("Species Richness", overview.get("species_richness", 0)),
        ("Total Individuals", overview.get("total_individuals", 0)),
    ]
    for i, (k, v) in enumerate(meta_rows, 1):
        ws.cell(row=i, column=1, value=k).font = Font(bold=True)
        ws.cell(row=i, column=2, value=v)
    ws.column_dimensions["A"].width = 32
    ws.column_dimensions["B"].width = 60

    # ── 2. Summary sheet ──
    ws = wb.create_sheet("Summary")
    ws.append(["Metric", "Value"])
    ws.append(["Observations (occurrences)", overview.get("observations", 0)])
    ws.append(["Species richness", overview.get("species_richness", 0)])
    ws.append(["Total individuals", overview.get("total_individuals", 0)])
    ws.append(["Habitats surveyed", overview.get("habitat_count", 0)])
    ws.append(["Groups contributing", overview.get("group_count", 0)])
    style_header(ws)
    ws.column_dimensions["A"].width = 32
    ws.column_dimensions["B"].width = 20

    # ── 3. Species sheet ──
    ws = wb.create_sheet("By Species")
    ws.append(["Species", "Total Count", "Occurrences"])
    for row in species_summary:
        ws.append([row["species"], row["total_count"], row["observations"]])
    style_header(ws)
    for col in range(1, 4):
        ws.column_dimensions[get_column_letter(col)].width = 28

    # ── 4. Habitat sheet ──
    ws = wb.create_sheet("By Habitat")
    ws.append(["Habitat", "Richness", "Total Count", "Occurrences", "Groups", "Simpson", "Shannon", "Evenness"])
    for row in habitat_summary:
        ws.append([
            row["habitat"], row["richness"], row["total_count"], row["observations"],
            row["groups"], row["simpson"], row["shannon"], row["evenness"],
        ])
    style_header(ws)
    widths = [22, 10, 12, 12, 10, 10, 10, 10]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    # ── 5. Environment sheet ──
    ws = wb.create_sheet("Environment")
    ws.append(["Habitat", "Variable", "Mean", "Std Dev"])
    for hab, vars_map in env_summary.items():
        for var, stats in vars_map.items():
            ws.append([hab, var, stats.get("mean"), stats.get("std")])
    style_header(ws)
    for col in range(1, 5):
        ws.column_dimensions[get_column_letter(col)].width = 22

    # ── 6. Records sheet ──
    ws = wb.create_sheet("Records")
    if records:
        columns = list(records[0].keys())
        ws.append(columns)
        for rec in records:
            ws.append([rec.get(c, "") for c in columns])
        style_header(ws)
        for i, col in enumerate(columns, 1):
            ws.column_dimensions[get_column_letter(i)].width = max(14, min(34, len(col) + 2))

    buf = _io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    fname = f"ecofield_research_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return Response(
        buf.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )


@app.route("/admin/research_reports/export.pdf")
def admin_research_reports_export_pdf():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))
    filters = _report_filters(request.args)
    records, overview, species_summary, habitat_summary, env_summary = _common_export_context(filters)

    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    import io as _io

    buf = _io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=20*mm, rightMargin=20*mm,
        topMargin=18*mm, bottomMargin=18*mm,
        title="EcoField Research Report",
        author="EcoField Logger",
    )

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="TitleGreen", parent=styles["Title"], textColor=colors.HexColor("#2E7D32")))
    styles.add(ParagraphStyle(name="H2Green", parent=styles["Heading2"], textColor=colors.HexColor("#1B5E20")))
    styles.add(ParagraphStyle(name="SmallMeta", parent=styles["Normal"], fontSize=8.5, textColor=colors.grey))

    story = []

    def add_table(headers, rows, col_widths=None, header_bg=colors.HexColor("#2E7D32")):
        data = [headers] + [list(r) for r in rows]
        table = Table(data, colWidths=col_widths, repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), header_bg),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 7.5),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CCCCCC")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#E8F5E9")]),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        return table

    story.append(Paragraph("EcoField Logger — Research-Grade Report", styles["TitleGreen"]))
    story.append(Spacer(1, 4*mm))
    story.append(Paragraph(
        "University of Ghana &bull; School of Biological Sciences &bull; Department of Animal Biology and Conservation Science",
        styles["Normal"]))
    story.append(Spacer(1, 3*mm))
    story.append(Paragraph(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", styles["SmallMeta"]))
    story.append(Spacer(1, 6*mm))

    story.append(Paragraph("1. Report Scope &amp; Filters", styles["H2Green"]))
    scope_rows = [
        ("Academic Year", filters["year"] or "All"),
        ("Group", filters["group"] or "All"),
        ("Habitat", filters["habitat"] or "All"),
        ("Survey Type", filters["survey_type"] or "All"),
        ("Date range", (filters["date_from"] or "Any") + " → " + (filters["date_to"] or "Any")),
    ]
    story.append(add_table(["Filter", "Applied Value"], scope_rows, col_widths=[60*mm, 110*mm]))
    story.append(Spacer(1, 6*mm))

    story.append(Paragraph("2. Executive Summary", styles["H2Green"]))
    summary_rows = [
        ("Species occurrences (records)", overview.get("observations", 0)),
        ("Species richness", overview.get("species_richness", 0)),
        ("Total individuals observed", overview.get("total_individuals", 0)),
        ("Habitats surveyed", overview.get("habitat_count", 0)),
        ("Groups contributing", overview.get("group_count", 0)),
        ("Average individuals per species", overview.get("avg_per_species", 0)),
    ]
    story.append(add_table(["Metric", "Value"], summary_rows, col_widths=[110*mm, 60*mm]))
    story.append(Spacer(1, 6*mm))

    if species_summary:
        story.append(Paragraph("3. Species Summary (Top 20)", styles["H2Green"]))
        rows = [(i + 1, s["species"], s["total_count"], s["observations"]) for i, s in enumerate(species_summary[:20])]
        story.append(add_table(["#", "Species", "Total Count", "Occurrences"],
                               rows, col_widths=[15*mm, 100*mm, 35*mm, 35*mm]))
        story.append(Spacer(1, 6*mm))

    if habitat_summary:
        story.append(Paragraph("4. Habitat Summary &amp; Diversity Indices", styles["H2Green"]))
        rows = [(h["habitat"], h["richness"], h["total_count"], h["observations"],
                 h["simpson"], h["shannon"], h["evenness"]) for h in habitat_summary]
        story.append(add_table(
            ["Habitat", "Richness", "Count", "Occur.", "Simpson", "Shannon", "Evenness"],
            rows, col_widths=[45*mm, 20*mm, 22*mm, 22*mm, 20*mm, 20*mm, 21*mm]))
        story.append(Spacer(1, 4*mm))
        story.append(Paragraph(
            "<i>Simpson index: 1 − Σp² (1 = maximum diversity). Shannon index H′: higher = more diverse. "
            "Evenness ranges 0–1 (1 = evenly distributed).</i>", styles["SmallMeta"]))
        story.append(Spacer(1, 6*mm))

    if env_summary:
        story.append(Paragraph("5. Environmental Variable Summary", styles["H2Green"]))
        rows = []
        for hab, vars_map in env_summary.items():
            for var, stats in vars_map.items():
                rows.append((hab, var, stats.get("mean") if stats.get("mean") is not None else "—",
                             stats.get("std") if stats.get("std") is not None else "—"))
        story.append(add_table(["Habitat", "Variable", "Mean", "Std Dev"], rows,
                               col_widths=[50*mm, 55*mm, 35*mm, 35*mm]))
        story.append(Spacer(1, 4*mm))
        story.append(Paragraph(
            "Units follow the Site Details field definitions used in EcoField data entry. "
            "Blank cells indicate no data was recorded.", styles["SmallMeta"]))
        story.append(Spacer(1, 6*mm))

    story.append(Paragraph("6. Methodology &amp; Data Notes", styles["H2Green"]))
    story.append(Paragraph(
        "This report aggregates field observations entered into the EcoField Logger by student "
        "research groups. Each record represents a single species occurrence within an observation "
        "entry (species_list/species_manual with associated counts and survey method). Records were "
        "expanded into tidy long-format rows for analysis. Species richness, total abundance, "
        "Shannon–Weiner index, Simpson index and habitat evenness were computed per habitat. "
        "Data should be cited with the generation timestamp shown above and remains subject to "
        "verification for any records entered manually in the field.", styles["Normal"]))
    story.append(Spacer(1, 8*mm))
    story.append(Paragraph(
        "&copy; EcoField Logger by MiltonPixel — Department of Animal Biology and Conservation Science (UG).",
        styles["SmallMeta"]))

    doc.build(story)

    buf.seek(0)
    fname = f"ecofield_research_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    return Response(
        buf.getvalue(),
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )


# ------------------------- RUN APP -------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug_mode = os.getenv("FLASK_DEBUG", "False").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug_mode, threaded=True)
