from flask import Flask, render_template, request, redirect, url_for, send_file, session, jsonify, flash
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

@app.context_processor
def inject_cache_bust():
    return {'cache_bust': '3'}

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
        supabase.table("admin_settings").upsert(data).execute()
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
        download_name=f"{session['group_id']}_{get_current_academic_year()}.csv"
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
                    students.append({"student_id": sid, "student_name": sname})
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
    except Exception as e:
        print(f"Error fetching observations: {e}")

    archive_files = [a["filename"] for a in get_archives()]

    sent_notifications = []
    try:
        notif_response = supabase.table("notifications").select("*").execute()
        all_notifs = notif_response.data or []
        sent_notifications = sorted(all_notifs, key=lambda n: n.get("created_at", ""), reverse=True)[:50]
        for n in sent_notifications:
            created = n.get("created_at") or ""
            if isinstance(created, str):
                n["created_at"] = created[:19].replace("T", " ")
    except Exception as e:
        print(f"Error fetching notifications: {e}")

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
        sent_notifications=sent_notifications,
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
            download_name=archive.get("filename", "archive.csv")
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
        return send_file(path, as_attachment=True)
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
        return send_file(
            file_path,
            as_attachment=True,
            download_name=attachment_original_name or attachment_file
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

# ------------------------- RUN APP -------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug_mode = os.getenv("FLASK_DEBUG", "False").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug_mode)
