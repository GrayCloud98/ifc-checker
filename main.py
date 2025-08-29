from flask import Flask, render_template, request, redirect, flash, url_for, session
import os, json
from dotenv import load_dotenv
from openai import OpenAI
from werkzeug.utils import secure_filename
import ifcopenshell

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

UPLOAD_IFC_FOLDER = 'uploads/ifc'
ALLOWED_IFC_EXTENSIONS = {'ifc'}
STANDARDS_FILE = 'standards.json'

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")

app = Flask(__name__)
app.secret_key = 'supersecretkey'
app.config['UPLOAD_IFC_FOLDER'] = UPLOAD_IFC_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 300 * 1024 * 1024

# -----------------------------
# Config: what to detect + which ID-Daten keys to show
# -----------------------------
# Each entry:
#  - match: list of substrings that should match the element Name
#  - short: short label to display
#  - keys:  { table_label : [acceptable ID-Daten property names] }
TARGETS = [
    {
        "match": ["Schwelle 30288"],
        "short": "Schwelle",
        "keys": {"Spurbreite (m)": ["Spurbreite", "Spurbereite"]},
    },
    {
        "match": ["Schiene 12210"],
        "short": "Schiene",
        "keys": {"Längsneigung (%)": ["Längsneigung", "Laengsneigung", "Neigung längs"]},
    },
    {
        "match": ["ice DB_BSK_76_Pass:ProVI DB_BSK_76_Pass 0.7368:1030184"],
        "short": "Bahnsteig",
        "keys": {"Bahnsteighöhe (m)": ["Bahnsteigshöhe", "Bahnsteig_hoehe", "Bahnsteig Höhe"]},
    },
    {
        "match": ["ice DB_Beleuchtungsmast_1_einseitig"],
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
    }
    if os.path.exists(STANDARDS_FILE):
        try:
            with open(STANDARDS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                defaults.update(data or {})
        except Exception:
            pass
    return defaults

def save_standards(data):
    os.makedirs(os.path.dirname(STANDARDS_FILE) or ".", exist_ok=True)
    with open(STANDARDS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def _num(name: str):
    """Parse request.form[name] as float; accept '1,23' or '1.23'. Return None if empty/invalid."""
    raw = request.form.get(name, "").strip()
    if not raw:
        return None
    raw = raw.replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None

def extract_id_daten_filtered(filepath):
    """Scan all IfcProduct; for configured targets, pull only whitelisted ID-Daten keys.
       Rows with no measurable attributes are skipped."""
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

                # Whitelist only the relevant keys per target
                filtered = {}
                for col_label, candidates in tgt["keys"].items():
                    val = None
                    for c in candidates:
                        if c in id_daten_raw and id_daten_raw[c] is not None:
                            val = id_daten_raw[c]
                            break
                    filtered[col_label] = val  # keep None if missing

                # --- NEW: skip elements that have no measurable values at all ---
                has_value = any(v is not None for v in filtered.values())
                if not has_value:
                    break  # don't add this element; go to next product

                results.append({
                    "Short": tgt["short"],
                    "IfcType": e.is_a(),
                    "GlobalId": e.GlobalId,
                    "Name": name,
                    "Values": filtered,
                })
                break  # prevent duplicate matches for this element

    return results

def compute_table_columns(rows):
    cols = set()
    for r in rows:
        cols.update(r["Values"].keys())
    order_hint = ["Breite (m)", "Länge (m)", "Neigung (%)",
                  "Spurbreite (m)", "Längsneigung (%)", "Bahnsteighöhe (m)", "Abstand Gleismitte (m)"]
    return [c for c in order_hint if c in cols] + [c for c in sorted(cols) if c not in order_hint]

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

        # --- compute checks for ALL types ---
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

            r["checks"] = checks  # attach generic checks for template

        return render_template('index.html', results=rows, columns=columns, standards=standards)

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
        return redirect(url_for("index"))

    if session.get("admin"):
        current_standards = load_standards()
        session.pop("admin", None)  # one-time access
        return render_template("admin.html", standards=current_standards)
    else:
        return redirect(url_for("index"))

@app.route('/upload_standard', methods=['POST'])
def upload_standard():
    current = load_standards()
    updates = {
        "Breite (m)": _num("breite"),
        "Länge (m)": _num("laenge"),
        "Neigung (%)": _num("neigung"),
        "Spurbreite (m)": _num("spurbreite"),
        "Längsneigung (%)": _num("laengsneigung"),
        "Bahnsteighöhe min (m)": _num("bahnsteig_min"),
        "Bahnsteighöhe max (m)": _num("bahnsteig_max"),
        "Abstand Gleismitte (m)": _num("abstand_gleismitte"),
    }
    changed = 0
    for k, v in updates.items():
        if v is not None:
            current[k] = round(v, 3)
            changed += 1

    if changed == 0:
        flash("Nichts gespeichert – leere oder ungültige Eingaben.", "error")
        return redirect(url_for('admin_upload'))

    save_standards(current)
    flash("Standards gespeichert.", "success")
    return redirect(url_for('admin_upload'))

# -----------------------------
# Main
# -----------------------------
if __name__ == '__main__':
    os.makedirs(UPLOAD_IFC_FOLDER, exist_ok=True)
    app.run(debug=True, use_reloader=True)
