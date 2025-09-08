# app.py  — SelfPhotoFinder (démo éthique) — dev: trhacknon
import os, time, sqlite3, re, io, base64
from pathlib import Path
from datetime import datetime, timedelta
from dotenv import load_dotenv
from flask import Flask, request, jsonify, render_template, send_from_directory, g, url_for
from flask_cors import CORS
import requests
from PIL import Image
import exifread
import pytesseract
import openai
from bs4 import BeautifulSoup

load_dotenv()

# --- CONFIG ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
BING_SEARCH_KEY = os.getenv("BING_SEARCH_KEY")
OCR_SPACE_KEY = os.getenv("OCR_SPACE_KEY")
DB_PATH = os.getenv("DATABASE_URL", "data.db")
UPLOAD_DIR = Path(os.getenv("UPLOAD_FOLDER", "uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
HOST_PUBLIC_URL = os.getenv("HOST_PUBLIC_URL", None)  # ex: https://my-app.example.com
PURGE_AFTER_DAYS = int(os.getenv("PURGE_AFTER_DAYS", "30"))

if OPENAI_API_KEY:
    openai.api_key = OPENAI_API_KEY

app = Flask(__name__, template_folder="templates", static_folder="static")
CORS(app)

# --- Simple DB ---
def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DB_PATH, check_same_thread=False)
        db.row_factory = sqlite3.Row
    return db

def init_db():
    db = get_db()
    db.execute('''CREATE TABLE IF NOT EXISTS consent_log (
                    id INTEGER PRIMARY KEY,
                    timestamp INTEGER,
                    ip TEXT,
                    user_agent TEXT,
                    action TEXT,
                    meta TEXT
                )''')
    db.execute('''CREATE TABLE IF NOT EXISTS uploads (
                    id INTEGER PRIMARY KEY,
                    filename TEXT,
                    timestamp INTEGER,
                    meta TEXT
                )''')
    db.commit()

with app.app_context():
    init_db()

@app.teardown_appcontext
def close_db(exc):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()

def log(action, meta=""):
    db = get_db()
    db.execute("INSERT INTO consent_log (timestamp, ip, user_agent, action, meta) VALUES (?, ?, ?, ?, ?)",
               (int(time.time()), request.remote_addr, request.headers.get("User-Agent",""), action, str(meta)))
    db.commit()

# --- Helpers ---
def secure_filename(fn):
    fn = re.sub(r"[^A-Za-z0-9_.-]", "_", fn)
    return fn

def save_upload(file_storage):
    filename = f"{int(time.time())}_{secure_filename(file_storage.filename)}"
    path = UPLOAD_DIR / filename
    file_storage.save(path)
    db = get_db()
    db.execute("INSERT INTO uploads (filename, timestamp, meta) VALUES (?, ?, ?)",
               (filename, int(time.time()), ""))
    db.commit()
    return filename

# EXIF extraction
def extract_exif(path: Path):
    try:
        with open(path, "rb") as f:
            tags = exifread.process_file(f, details=False)
        return {k: str(v) for k, v in tags.items()}
    except Exception as e:
        return {"error": str(e)}

# OCR (local pytesseract first, fallback to OCR.space if configured)
def do_ocr(path: Path):
    text = ""
    try:
        img = Image.open(path)
        text_local = pytesseract.image_to_string(img)
        if text_local and text_local.strip():
            text += text_local.strip()
    except Exception:
        text = ""

    if not text and OCR_SPACE_KEY:
        try:
            with open(path, "rb") as f:
                r = requests.post("https://api.ocr.space/parse/image",
                                  files={"file": f},
                                  data={"apikey": OCR_SPACE_KEY, "language": "eng"})
            j = r.json()
            parsed = " ".join([p.get("ParsedText","") for p in j.get("ParsedResults",[])]) if j.get("ParsedResults") else ""
            if parsed:
                text = parsed
        except Exception:
            pass
    return text

# OpenAI description/summary (non-identifying)
def openai_describe(ocr_text, notes=""):
    if not OPENAI_API_KEY:
        return "OpenAI non configuré."
    prompt = (
        "Tu es un assistant qui aide un utilisateur à vérifier si sa photo apparaît sur Internet.\n"
        "Ne tente **jamais** d'identifier une personne. Donne :\n"
        "1) Description non-identifiante de l'image (objets, contexte, couleurs). 2) Indique si du texte a été détecté et affiche-le. 3) 5 recommandations de sécurité/actions si la photo est trouvée ailleurs.\n\n"
        f"Texte OCR détecté:\n{ocr_text}\n\nNotes: {notes}\n\nRéponse concise."
    )
    try:
        resp = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=[{"role":"user","content":prompt}],
            max_tokens=450,
            temperature=0.1
        )
        return resp["choices"][0]["message"]["content"]
    except Exception as e:
        return f"Erreur OpenAI: {e}"

# --- Reverse image search: Bing Visual Search (recommended) ---
def bing_visual_search(path: Path):
    if not BING_SEARCH_KEY:
        return {"error": "BING_SEARCH_KEY not configured"}
    url = "https://api.bing.microsoft.com/v7.0/images/visualsearch"
    headers = {"Ocp-Apim-Subscription-Key": BING_SEARCH_KEY}
    with open(path, "rb") as f:
        files = {"image": ("image.jpg", f, "application/octet-stream")}
        try:
            r = requests.post(url, headers=headers, files=files, timeout=30)
            if r.status_code != 200:
                return {"error": "bing_error", "detail": r.text}
            data = r.json()
            # Extract useful results (pages)
            results = []
            # look under tags -> actions
            for tag in data.get("tags", []):
                for action in tag.get("actions", []):
                    # pages including may have 'webSearchUrl' etc
                    url_ = action.get("webSearchUrl") or action.get("hostPageDisplayUrl") or action.get("thumbnailUrl")
                    name = action.get("displayName") or ""
                    if url_:
                        results.append({"name": name, "url": url_})
            # Also try to parse 'visuallySimilarImages' or 'imageInsightsToken' pages
            return {"raw": data, "extracted": results}
        except Exception as e:
            return {"error": str(e)}

# --- Fallback helpers: provide direct search-by-image links ---
def url_for_image_search_by_url(image_url):
    # Yandex / Google / Bing (by url)
    links = {}
    if image_url:
        links["google"] = f"https://www.google.com/searchbyimage?image_url={image_url}"
        links["yandex"] = f"https://yandex.com/images/search?rpt=imageview&img_url={image_url}"
        # Bing can accept image URL as a parameter in web UI, but recommend API usage
        links["bing_web"] = f"https://www.bing.com/images/search?q=imgurl:{image_url}&view=detailv2"
    return links

# --- Routes ---
@app.route("/")
def index():
    return render_template("index.html", dev_name="trhacknon")

@app.route("/upload_photo", methods=["POST"])
def upload_photo():
    # consent
    consent = request.form.get("consent")
    if not consent or consent not in ("yes","true","on"):
        return jsonify({"error":"consent_required"}), 400
    f = request.files.get("photo")
    if not f:
        return jsonify({"error":"no_file"}), 400
    filename = save_upload(f)
    log("photo_upload", meta=filename)
    return jsonify({"status":"ok", "filename": filename, "url": url_for("uploads_static", filename=filename, _external=True)})

@app.route("/describe_and_search", methods=["POST"])
def describe_and_search():
    """
    JSON body: { "filename": "<saved filename>", "use_bing": true/false, "use_yandex": true/false, "consent": true }
    """
    data = request.get_json(force=True)
    if not data.get("consent"):
        return jsonify({"error":"consent_required"}), 400
    filename = data.get("filename")
    if not filename:
        return jsonify({"error":"missing_filename"}), 400
    path = UPLOAD_DIR / filename
    if not path.exists():
        return jsonify({"error":"not_found"}), 404

    log("describe_and_search", meta=filename)

    # EXIF
    exif = extract_exif(path)

    # OCR
    ocr_text = do_ocr(path)

    # OpenAI description/summary
    ai_desc = openai_describe(ocr_text=ocr_text, notes=f"filename={filename}")

    # Bing search
    bing_res = None
    if data.get("use_bing", True):
        bing_res = bing_visual_search(path)

    # Public URL (if available)
    public_url = None
    if HOST_PUBLIC_URL:
        public_url = f"{HOST_PUBLIC_URL.rstrip('/')}/{url_for('uploads_static', filename=filename).lstrip('/')}"
    else:
        public_url = url_for('uploads_static', filename=filename, _external=True)

    # Links for Google/Yandex by URL
    search_links = url_for_image_search_by_url(public_url)

    return jsonify({
        "filename": filename,
        "public_url": public_url,
        "exif": exif,
        "ocr_text": ocr_text,
        "openai_description": ai_desc,
        "bing": bing_res,
        "search_links": search_links
    })

@app.route("/uploads/<path:filename>")
def uploads_static(filename):
    return send_from_directory(UPLOAD_DIR, filename, as_attachment=False)

@app.route("/delete_data", methods=["POST"])
def delete_data():
    # For demo, delete all uploads & logs — in production, require auth
    db = get_db()
    rows = db.execute("SELECT filename FROM uploads").fetchall()
    deleted = []
    for r in rows:
        p = UPLOAD_DIR / r["filename"]
        if p.exists():
            try:
                p.unlink()
                deleted.append(r["filename"])
            except Exception:
                pass
    db.execute("DELETE FROM uploads")
    db.execute("DELETE FROM consent_log")
    db.commit()
    return jsonify({"status":"deleted", "files_deleted": deleted})

@app.route("/purge_old", methods=["POST"])
def purge_old():
    cutoff = int((datetime.utcnow() - timedelta(days=PURGE_AFTER_DAYS)).timestamp())
    db = get_db()
    rows = db.execute("SELECT id, filename, timestamp FROM uploads WHERE timestamp < ?", (cutoff,)).fetchall()
    deleted = []
    for r in rows:
        p = UPLOAD_DIR / r["filename"]
        if p.exists(): p.unlink()
        db.execute("DELETE FROM uploads WHERE id = ?", (r["id"],))
        deleted.append(r["filename"])
    db.commit()
    return jsonify({"deleted": deleted})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
