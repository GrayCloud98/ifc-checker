from flask import Flask, render_template, request, redirect, flash, url_for, session, send_file, abort, send_from_directory
from io import BytesIO
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
import os, json, hashlib
from dotenv import load_dotenv
from openai import OpenAI
from werkzeug.utils import secure_filename
import ifcopenshell
from pdfminer.high_level import extract_text as pdf_extract_text
import requests
from bs4 import BeautifulSoup
import re, html as html_unescape
from io import BytesIO

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

UPLOAD_IFC_FOLDER = 'uploads/ifc'
UPLOAD_SRC_FOLDER = 'uploads/sources'   # local PDF sources
URL_CACHE_FOLDER  = 'url_cache'         # cache for URL fetches (HTML/PDF -> text)
ALLOWED_IFC_EXTENSIONS = {'ifc'}
ALLOWED_SRC_EXTENSIONS = {'pdf'}
STANDARDS_FILE = 'standards.json'

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")

app = Flask(__name__)
app.secret_key = 'supersecretkey'
app.config['UPLOAD_IFC_FOLDER'] = UPLOAD_IFC_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 300 * 1024 * 1024

# -----------------------------
# Detection config
# -----------------------------
TARGETS = [
    {
        "match": ["Schwelle"],
        "short": "Schwelle",
        "keys": {"Spurbreite (m)": ["Spurbreite", "Spurbereite"]},
    },
    {
        "match": ["Schiene 12210", "Schiene"],
        "short": "Schiene",
        "keys": {"Längsneigung (%)": ["Längsneigung", "Laengsneigung", "Neigung längs", "Neigung laengs"]},
    },
    {
        "match": ["ice DB_BSK_76_Pass:ProVI DB_BSK_76_Pass 0.7368:1030184", "Bahnsteig"],
        "short": "Bahnsteig",
        "keys": {"Bahnsteighöhe (m)": ["Bahnsteigshöhe", "Bahnsteig_hoehe", "Bahnsteig Höhe", "Bahnsteighöhe"]},
    },
    {
        "match": ["ice DB_Beleuchtungsmast_1_einseitig", "Beleuchtungsmast", "Mast"],
        "short": "Mast",
        "keys": {"Abstand Gleismitte (m)": ["Abstand_Gleismitte", "Abstand Gleismitte", "Gleismitte Abstand"]},
    },
    {
        "match": ["Rampe:Rampe max.100%:1274060:1", "Rampe"],
        "short": "Rampe",
        "keys": {
            "Breite (m)": ["Breite"],
            "Länge (m)": ["Länge", "Laenge"],
            "Neigung (%)": ["Neigung"],
        },
    },
]

# -----------------------------
# Helpers
# -----------------------------
def allowed_file(filename, allowed_exts):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed_exts

def _allowed_src_file(filename):
    return allowed_file(filename, ALLOWED_SRC_EXTENSIONS)

def _store_source_pdf(fs):
    """Save uploaded PDF to UPLOAD_SRC_FOLDER and return the saved filename."""
    if not fs or fs.filename == '':
        return None
    if not _allowed_src_file(fs.filename):
        return None
    os.makedirs(UPLOAD_SRC_FOLDER, exist_ok=True)
    base = secure_filename(fs.filename)
    name, ext = os.path.splitext(base)
    candidate = base
    i = 0
    while os.path.exists(os.path.join(UPLOAD_SRC_FOLDER, candidate)):
        i += 1
        candidate = f"{name}_{i}{ext}"
    path = os.path.join(UPLOAD_SRC_FOLDER, candidate)
    fs.save(path)
    return candidate

def load_standards():
    defaults = {
        "Breite (m)": None,
        "Länge (m)": None,
        "Neigung (%)": None,
        "Spurbreite (m)": None,
        "Längsneigung (%)": None,
        "Bahnsteighöhe min (m)": None,
        "Bahnsteighöhe max (m)": None,
        "Abstand Gleismitte (m)": None,
        "_sources": {}  # attribute -> {"link": str|None, "file": filename|None}
    }
    if os.path.exists(STANDARDS_FILE):
        try:
            with open(STANDARDS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "_sources" not in data:
                    data["_sources"] = {}
                defaults.update(data or {})
        except Exception:
            pass
    return defaults

def save_standards(data):
    os.makedirs(os.path.dirname(STANDARDS_FILE) or ".", exist_ok=True)
    with open(STANDARDS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def _num_from(val: str):
    if val is None:
        return None
    raw = val.strip().replace(",", ".")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None

def extract_id_daten_filtered(filepath):
    model = ifcopenshell.open(filepath)
    products = list(model.by_type("IfcProduct"))
    results = []

    def read_id_daten(elem):
        out = {}
        for rel in model.get_inverse(elem):
            if rel.is_a("IfcRelDefinesByProperties"):
                pdef = rel.RelatingPropertyDefinition
                if pdef.is_a("IfcPropertySet") and (pdef.Name or "").strip().lower() == "id-daten":
                    for prop in pdef.HasProperties or []:
                        try:
                            val = prop.NominalValue.wrappedValue
                        except Exception:
                            val = None
                        if isinstance(val, (int, float)):
                            val = round(float(val), 2)
                        out[prop.Name] = val
        return out

    for e in products:
        name = (getattr(e, "Name", "") or "")
        low = name.lower()

        for tgt in TARGETS:
            if any(sub.lower() in low for sub in tgt["match"]):
                id_daten_raw = read_id_daten(e)
                filtered = {}
                for col_label, candidates in tgt["keys"].items():
                    val = None
                    for c in candidates:
                        if c in id_daten_raw and id_daten_raw[c] is not None:
                            val = id_daten_raw[c]
                            break
                    filtered[col_label] = val

                has_value = any(v is not None for v in filtered.values())
                if not has_value:
                    break

                results.append({
                    "Short": tgt["short"],
                    "IfcType": e.is_a(),
                    "GlobalId": e.GlobalId,
                    "Name": name,
                    "Values": filtered,
                })
                break

    return results

def compute_table_columns(rows):
    cols = set()
    for r in rows:
        cols.update(r["Values"].keys())
    order_hint = ["Breite (m)", "Länge (m)", "Neigung (%)",
                  "Spurbreite (m)", "Längsneigung (%)", "Bahnsteighöhe (m)", "Abstand Gleismitte (m)"]
    return [c for c in order_hint if c in cols] + [c for c in sorted(cols) if c not in order_hint]

def _flatten_results_for_pdf(rows, standards):
    out = []
    for r in rows:
        vals = r.get("Values", {}) or {}
        checks = r.get("checks", {}) or {}
        for attr_label, value in vals.items():
            if value is None:
                continue
            standard_value = standards.get(attr_label)
            ok = checks.get(attr_label)
            status = "Correct" if ok is True else ("Wrong" if ok is False else "-")
            out.append({
                "attribute": attr_label,
                "value": value,
                "standard": standard_value if standard_value is not None else "-",
                "status": status,
            })
    return out

def _generate_results_pdf(results, title="IFC Check Results"):
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4)
    story = []
    styles = getSampleStyleSheet()

    story.append(Paragraph(title, styles["Title"]))
    story.append(Spacer(1, 12))

    data = [["Attribute", "Extracted Value", "Standard", "Status"]]
    for r in results:
        data.append([
            str(r.get("attribute", "")),
            str(r.get("value", "")),
            str(r.get("standard", "")),
            str(r.get("status", "")),
        ])

    table = Table(data)
    table.setStyle(TableStyle([
        ("GRID", (0,0), (-1,-1), 0.5, "black"),
        ("ALIGN", (0,0), (-1,-1), "CENTER"),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
    ]))

    story.append(table)
    doc.build(story)
    buf.seek(0)
    return buf

# -----------------------------
# Source text helpers (local PDF + URL fallback with cache)
# -----------------------------
def _pdf_text_from_local(filename: str) -> str | None:
    try:
        path = os.path.join(UPLOAD_SRC_FOLDER, filename)
        if not os.path.isfile(path):
            return None
        txt = pdf_extract_text(path) or ""
        return txt[:200_000]
    except Exception:
        return None

def _cache_name_for_url(url: str, suffix: str) -> str:
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()
    safe = f"{h}{suffix}"
    os.makedirs(URL_CACHE_FOLDER, exist_ok=True)
    return os.path.join(URL_CACHE_FOLDER, safe)

def _extract_visible_text_from_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "header", "footer", "svg"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    # collapse excessive whitespace
    lines = [ln.strip() for ln in text.splitlines()]
    cleaned = "\n".join(ln for ln in lines if ln)
    return cleaned

def _text_from_url(url: str) -> str | None:
    if not url:
        return None
    # cached plain text?
    txt_path = _cache_name_for_url(url, ".txt")
    if os.path.exists(txt_path):
        try:
            with open(txt_path, "r", encoding="utf-8") as f:
                cached = f.read()
                if cached:
                    return cached[:200_000]
        except Exception:
            pass

    headers = {"User-Agent": "IFC-Checker/1.0 (+https://example.local)"}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code != 200 or not resp.content:
            return None
        ctype = (resp.headers.get("content-type") or "").lower()
        # Treat as PDF if content-type says so OR URL endswith .pdf
        if "application/pdf" in ctype or url.lower().endswith(".pdf"):
            # cache raw PDF then extract
            pdf_path = _cache_name_for_url(url, ".pdf")
            with open(pdf_path, "wb") as pf:
                pf.write(resp.content)
            try:
                txt = pdf_extract_text(pdf_path) or ""
            except Exception:
                txt = ""
        else:
            # HTML or text
            raw = resp.text
            txt = _extract_visible_text_from_html(raw)

        if txt:
            with open(txt_path, "w", encoding="utf-8") as wf:
                wf.write(txt)
            return txt[:200_000]
    except Exception:
        return None
    return None

def _text_from_link(link: str, limit: int = 200_000) -> str | None:
    """
    Download a URL and return readable text.
    - If PDF: extract via pdfminer.
    - If HTML/other: strip tags to plain text.
    """
    if not link:
        return None
    try:
        resp = requests.get(link, timeout=12, headers={"User-Agent": "IFC-Checker/1.0"})
        if resp.status_code != 200 or not resp.content:
            return None

        content_type = (resp.headers.get("Content-Type") or "").lower()
        is_pdf = "pdf" in content_type or link.lower().endswith(".pdf")

        if is_pdf:
            # pdfminer can read from file-like objects
            try:
                text = pdf_extract_text(BytesIO(resp.content)) or ""
                return text[:limit]
            except Exception:
                return None

        # Fallback: treat as HTML -> strip tags
        enc = resp.encoding or "utf-8"
        try:
            html_text = resp.content.decode(enc, errors="ignore")
        except Exception:
            html_text = resp.text

        # Remove scripts/styles
        html_text = re.sub(r"(?is)<(script|style).*?>.*?(</\1>)", " ", html_text)
        # Strip all tags
        text = re.sub(r"(?s)<.*?>", " ", html_text)
        # Unescape entities and collapse whitespace
        text = html_unescape.unescape(text)
        text = re.sub(r"\s+", " ", text).strip()

        return text[:limit] if text else None

    except Exception:
        return None

# -----------------------------
# AI helpers
# -----------------------------
def _ai_extract_attr_from_text(client, attribute_label: str, text: str) -> dict | None:
    """
    Ask the model for a compact, structured extraction + a ready-to-show German sentence.
    """
    if not text or not attribute_label:
        return None

    prompt = f"""
Du bist ein Leser von technischen Normen im Bahnbereich. Extrahiere die eindeutige
numerische Vorgabe für das Attribut "{attribute_label}" aus dem KONTEXT. Wenn es mehrere
Werte gibt, nimm den prägnantesten/maßgeblichen. Antworte NUR als kompaktes JSON.

Gib zurück:
{{
  "attr": "{attribute_label}",
  "law": "Kurzname der Norm/Richtlinie/Gesetzes o. Quelle",
  "title": "Volltitel oder Dokumenttitel (falls vorhanden)",
  "section": "Abschnitt/Paragraph/Seite (falls erkennbar)",
  "rule": "min|max|range|target",
  "value": 1.23,        # nur bei min/max/target
  "min": 0.76,          # nur bei range
  "max": 0.96,          # nur bei range
  "unit": "m|%|…",
  "condition": "kurzer Kontext wofür das gilt (optional)",
  "modality": "must|should",     # normative Stärke
  "sentence_de": "Laut <law> (Abschnitt <section>): <muss/sollte> <attr> ...",  # <= 25 Wörter
  "quote": "wörtlicher Beleg <= 20 Wörter",
  "confidence": 0.0-1.0
}}

Beispiele:
{{
  "attr":"Breite (m)","law":"DIN 18040-1","title":"Barrierefreies Bauen",
  "section":"5.2","rule":"min","value":1.20,"unit":"m","condition":"öffentliche Rampen",
  "modality":"must","sentence_de":"Laut DIN 18040-1 (Abschnitt 5.2): Die Rampenbreite muss mindestens 1,20 m betragen (öffentliche Rampen).",
  "quote":"Mindestbreite der Rampen 1,20 m","confidence":0.87
}}
{{
  "attr":"Neigung (%)","law":"Ril 813","title":"Bahnsteige",
  "section":"Tab. 3","rule":"max","value":6,"unit":"%","modality":"should",
  "sentence_de":"Laut Ril 813 (Tabelle 3): Die Neigung sollte höchstens 6 % betragen.",
  "quote":"max. Neigung 6 %","confidence":0.8
}}

KONTEXT (gekürzt):
{text[:160_000]}
"""
    try:
        resp = client.responses.create(
            model="gpt-4o-mini",
            temperature=0,
            input=prompt
        )
        raw = (resp.output_text or "").strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0 and end > start:
            import json
            return json.loads(raw[start:end+1])
    except Exception:
        pass
    return None

def _ai_extract_for_results_local(rows: list, standards: dict) -> dict:
    """
    For each attribute in the current results, try to read from a local PDF OR a link
    and ask AI for a compact, human-friendly sentence + short quote.
    """
    out = {}
    sources = standards.get("_sources", {}) or {}

    needed = set()
    for r in rows:
        for k, v in (r.get("Values") or {}).items():
            if v is not None:
                needed.add(k)

    for attr in needed:
        src = sources.get(attr) or {}
        file = src.get("file")
        link = (src.get("link") or "").strip()

        # 1) Prefer local PDF if present
        text = _pdf_text_from_local(file) if file else None
        # 2) Otherwise try the link (PDF or HTML)
        if (not text) and link:
            text = _text_from_link(link)

        if not text:
            continue

        res = _ai_extract_attr_from_text(client, attr, text)
        if not res:
            continue

        # Build sentence if model didn't provide one
        unit = (res.get("unit") or "").strip()
        rule = (res.get("rule") or "").strip().lower()
        law = (res.get("law") or res.get("title") or "Quelle").strip()
        section = (res.get("section") or "").strip()
        modality = (res.get("modality") or "must").lower()
        verb = "muss" if modality == "must" else "sollte"
        condition = (res.get("condition") or "").strip()

        core = None
        if rule == "range" and res.get("min") is not None and res.get("max") is not None:
            core = f"{verb} {attr} zwischen {res['min']} und {res['max']} {unit} liegen"
        elif rule == "min" and res.get("value") is not None:
            core = f"{verb} mindestens {res['value']} {unit} betragen"
        elif rule == "max" and res.get("value") is not None:
            core = f"{verb} höchstens {res['value']} {unit} betragen"
        elif rule == "target" and res.get("value") is not None:
            core = f"soll {res['value']} {unit} betragen"

        if core and condition:
            core = f"{core} ({condition})"

        if res.get("sentence_de"):
            sentence = res["sentence_de"]
        elif core:
            sentence = f"Laut {law}" + (f" (Abschnitt {section})" if section else "") + f": {core}."
        else:
            sentence = f"Laut {law}" + (f" (Abschnitt {section})" if section else "") + " liegt eine relevante Vorgabe vor."

        out[attr] = {
            "summary": sentence,
            "evidence": res.get("quote") or res.get("evidence"),
            "confidence": res.get("confidence"),
        }

    return out

# -----------------------------
# Routes
# -----------------------------
@app.route('/')
def index():
    return render_template('index.html', results=None, columns=[])

@app.route('/upload', methods=['POST'])
def upload_ifc():
    if 'file' not in request.files or request.files['file'].filename == '':
        flash('Keine Datei ausgewählt.')
        return redirect(url_for('index'))

    file = request.files['file']
    if not (file and allowed_file(file.filename, ALLOWED_IFC_EXTENSIONS)):
        flash('Nur .ifc Dateien sind erlaubt.')
        return redirect(url_for('index'))

    filename = secure_filename(file.filename)
    filepath = os.path.join(app.config['UPLOAD_IFC_FOLDER'], filename)
    os.makedirs(app.config['UPLOAD_IFC_FOLDER'], exist_ok=True)
    file.save(filepath)

    try:
        rows = extract_id_daten_filtered(filepath)
        columns = compute_table_columns(rows)
        standards = load_standards()

        def approx_eq(v, target, tol=0.01):
            return abs(v - target) <= tol

        for r in rows:
            checks = {}
            vals = r["Values"]
            short = (r["Short"] or "").lower()

            if short == "rampe":
                b = vals.get("Breite (m)")
                l = vals.get("Länge (m)")
                n = vals.get("Neigung (%)")
                if standards.get("Breite (m)") is not None and b is not None:
                    checks["Breite (m)"] = b >= standards["Breite (m)"]
                if standards.get("Länge (m)") is not None and l is not None:
                    checks["Länge (m)"] = l >= standards["Länge (m)"]
                if standards.get("Neigung (%)") is not None and n is not None:
                    checks["Neigung (%)"] = n <= standards["Neigung (%)"]

            elif short == "bahnsteig":
                h = vals.get("Bahnsteighöhe (m)")
                hmin = standards.get("Bahnsteighöhe min (m)")
                hmax = standards.get("Bahnsteighöhe max (m)")
                if h is not None and (hmin is not None or hmax is not None):
                    ok = True
                    if hmin is not None:
                        ok = ok and (h >= hmin)
                    if hmax is not None:
                        ok = ok and (h <= hmax)
                    checks["Bahnsteighöhe (m)"] = ok

            elif short == "schiene":
                s = vals.get("Längsneigung (%)")
                lim = standards.get("Längsneigung (%)")
                if s is not None and lim is not None:
                    checks["Längsneigung (%)"] = s <= lim

            elif short == "schwelle":
                g = vals.get("Spurbreite (m)")
                tgt = standards.get("Spurbreite (m)")
                if g is not None and tgt is not None:
                    checks["Spurbreite (m)"] = approx_eq(g, tgt, tol=0.01)

            elif short == "mast":
                d = vals.get("Abstand Gleismitte (m)")
                min_d = standards.get("Abstand Gleismitte (m)")
                if d is not None and min_d is not None:
                    checks["Abstand Gleismitte (m)"] = d >= min_d

            r["checks"] = checks

        ai_sources = _ai_extract_for_results_local(rows, standards)

        flat_for_pdf = _flatten_results_for_pdf(rows, standards)
        session["last_results_pdf"] = flat_for_pdf

        return render_template(
            'index.html',
            results=rows,
            columns=columns,
            standards=standards,
            ai_sources=ai_sources
        )

    except Exception as e:
        flash(f"Fehler beim Lesen der IFC-Datei: {str(e)}")
        return redirect(url_for('index'))

@app.route('/admin', methods=["GET", "POST"])
def admin_upload():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect(url_for("admin_upload"))
        flash("Falsche Zugangsdaten.")
        return redirect(url_for('index'))

    if session.get("admin"):
        current_standards = load_standards()
        session.pop("admin", None)  # one-time access
        return render_template("admin.html", standards=current_standards)
    else:
        return redirect(url_for("index"))

@app.route('/upload_standard', methods=['POST'])
def upload_standard():
    """
    Reads the fields:
      comp_<Obj>_<AttrKey>, val_<Obj>_<AttrKey>, min_<Obj>_<AttrKey>, max_<Obj>_<AttrKey>
    PLUS optional sources:
      srclink_<Obj>_<AttrKey>  (URL string)
      srcfile_<Obj>_<AttrKey>  (PDF file)
    Writes:
      - flat numeric keys in standards.json
      - new "_sources" map: attribute label -> {"link":..., "file": <filename under uploads/sources> }
    """
    current = load_standards()
    sources = current.get("_sources", {}) or {}
    os.makedirs(UPLOAD_SRC_FOLDER, exist_ok=True)

    def _merge_source(attr_label, link, saved_filename):
        prev = sources.get(attr_label, {}) or {}
        link = (link or "").strip()
        new_link = link if link else prev.get("link")
        new_file = saved_filename or prev.get("file")
        sources[attr_label] = {"link": new_link or None, "file": new_file or None}

    # Helper to read a cell and apply a write function, then record sources
    def read_cell(obj, key, write_fn, attr_label_for_source):
        comp = request.form.get(f"comp_{obj}_{key}")
        val  = _num_from(request.form.get(f"val_{obj}_{key}"))
        mn   = _num_from(request.form.get(f"min_{obj}_{key}"))
        mx   = _num_from(request.form.get(f"max_{obj}_{key}"))

        # sources (both optional)
        link = request.form.get(f"srclink_{obj}_{key}", "")
        file_storage = request.files.get(f"srcfile_{obj}_{key}")
        saved_filename = _store_source_pdf(file_storage) if file_storage else None

        write_fn(comp, val, mn, mx)
        if (link and link.strip()) or saved_filename:
            _merge_source(attr_label_for_source, link, saved_filename)

    changed = 0

    # Rampe / Breite -> "Breite (m)"
    def w_rampe_breite(comp, val, mn, mx):
        nonlocal changed
        if comp == "range" and mn is not None and mx is not None:
            current["Breite (m)"] = round(mn, 3); changed += 1
        elif val is not None:
            current["Breite (m)"] = round(val, 3); changed += 1
    read_cell("Rampe", "Breite", w_rampe_breite, "Breite (m)")

    # Rampe / Länge -> "Länge (m)"
    def w_rampe_laenge(comp, val, mn, mx):
        nonlocal changed
        if comp == "range" and mn is not None and mx is not None:
            current["Länge (m)"] = round(mn, 3); changed += 1
        elif val is not None:
            current["Länge (m)"] = round(val, 3); changed += 1
    read_cell("Rampe", "Laenge", w_rampe_laenge, "Länge (m)")

    # Rampe / Neigung -> "Neigung (%)"
    def w_rampe_neigung(comp, val, mn, mx):
        nonlocal changed
        if comp == "range" and mn is not None and mx is not None:
            current["Neigung (%)"] = round(mx, 3); changed += 1
        elif val is not None:
            current["Neigung (%)"] = round(val, 3); changed += 1
    read_cell("Rampe", "Neigung", w_rampe_neigung, "Neigung (%)")

    # Schwelle / Spurbreite -> "Spurbreite (m)"
    def w_schwelle_spurbreite(comp, val, mn, mx):
        nonlocal changed
        if comp == "range" and mn is not None and mx is not None:
            mid = (mn + mx) / 2.0
            current["Spurbreite (m)"] = round(mid, 3); changed += 1
        elif val is not None:
            current["Spurbreite (m)"] = round(val, 3); changed += 1
    read_cell("Schwelle", "Spurbreite", w_schwelle_spurbreite, "Spurbreite (m)")

    # Schiene / Längsneigung -> "Längsneigung (%)"
    def w_schiene_laengsneigung(comp, val, mn, mx):
        nonlocal changed
        if comp == "range" and mn is not None and mx is not None:
            current["Längsneigung (%)"] = round(mx, 3); changed += 1
        elif val is not None:
            current["Längsneigung (%)"] = round(val, 3); changed += 1
    read_cell("Schiene", "Laengsneigung", w_schiene_laengsneigung, "Längsneigung (%)")

    # Bahnsteig / Bahnsteighöhe -> min/max
    def w_bahnsteig_hoehe(comp, val, mn, mx):
        nonlocal changed
        if comp == "range":
            if mn is not None:
                current["Bahnsteighöhe min (m)"] = round(mn, 3); changed += 1
            if mx is not None:
                current["Bahnsteighöhe max (m)"] = round(mx, 3); changed += 1
        else:
            if val is not None:
                current["Bahnsteighöhe min (m)"] = round(val, 3); changed += 1
    read_cell("Bahnsteig", "Bahnsteighoehe", w_bahnsteig_hoehe, "Bahnsteighöhe (m)")

    # Mast / Abstand Gleismitte -> "Abstand Gleismitte (m)"
    def w_mast_abstand(comp, val, mn, mx):
        nonlocal changed
        if comp == "range" and mn is not None and mx is not None:
            current["Abstand Gleismitte (m)"] = round(mn, 3); changed += 1
        elif val is not None:
            current["Abstand Gleismitte (m)"] = round(val, 3); changed += 1
    read_cell("Mast", "Abstand_Gleismitte", w_mast_abstand, "Abstand Gleismitte (m)")

    # persist numbers + sources in-memory
    current["_sources"] = sources

    # --- require a source for each Rampe attribute (link OR PDF) ---
    required_ramp_attrs = ["Breite (m)", "Länge (m)", "Neigung (%)"]
    missing = []
    for attr in required_ramp_attrs:
        src = (sources.get(attr) or {})
        has_link = bool((src.get("link") or "").strip())
        has_file = bool(src.get("file"))
        if not (has_link or has_file):
            missing.append(attr)
    if missing:
        flash("Bitte Quelle (URL oder PDF) für Rampe – " + ", ".join(missing) + " hinterlegen.", "error")
        return redirect(url_for('admin_upload'))

    # If truly nothing changed, show info
    if changed == 0 and sources == load_standards().get("_sources", {}):
        flash("Keine Änderungen erkannt – bestehende Werte/Quellen bleiben unverändert.", "info")
        return redirect(url_for('admin_upload'))

    save_standards(current)
    flash("Standards & Quellen gespeichert.", "success")
    return redirect(url_for('admin_upload'))

@app.route("/download_report")
def download_report():
    results = session.get("last_results_pdf")
    if not results:
        return abort(400, description="No results available to export. Upload and check an IFC file first.")
    pdf_buffer = _generate_results_pdf(results, title="IFC Check Results")
    return send_file(
        pdf_buffer,
        mimetype="application/pdf",
        as_attachment=True,
        download_name="ifc_check_results.pdf",
    )

# Serve uploaded source PDFs safely
@app.route("/sources/<path:filename>")
def get_source(filename):
    return send_from_directory(UPLOAD_SRC_FOLDER, filename, mimetype="application/pdf", as_attachment=False)

# -----------------------------
# Main
# -----------------------------
if __name__ == '__main__':
    os.makedirs(UPLOAD_IFC_FOLDER, exist_ok=True)
    os.makedirs(UPLOAD_SRC_FOLDER, exist_ok=True)
    os.makedirs(URL_CACHE_FOLDER, exist_ok=True)
    app.run(debug=True, use_reloader=True)
