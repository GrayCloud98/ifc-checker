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
#  - short: short label to display in the table
#  - keys:  { table_label : [acceptable ID-Daten property names] }
TARGETS = [
    {
        "match": ["Schwelle 30288"],
        "short": "Schwelle",
        "keys": {
            "Spurbreite (m)": ["Spurbreite", "Spurbereite"],  # tolerant to typos
        },
    },
    {
        "match": ["Schiene 12210"],
        "short": "Schiene",
        "keys": {
            "Längsneigung (%)": ["Längsneigung", "Laengsneigung", "Neigung längs"],
        },
    },
    {
        "match": ["ice DB_BSK_76_Pass:ProVI DB_BSK_76_Pass 0.7368:1030184"],
        "short": "Bahnsteig",
        "keys": {
            "Bahnsteighöhe (m)": ["Bahnsteigshöhe", "Bahnsteig_hoehe", "Bahnsteig Höhe"],
        },
    },
    {
        "match": ["ice DB_Beleuchtungsmast_1_einseitig"],
        "short": "Mast",
        "keys": {
            "Abstand Gleismitte (m)": ["Abstand_Gleismitte", "Abstand Gleismitte", "Gleismitte Abstand"],
        },
    },
    {
        "match": ["Rampe:Rampe max.100%:1274060:1", "Rampe"],  # exact first, generic second
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
    if os.path.exists(STANDARDS_FILE):
        with open(STANDARDS_FILE, "r") as f:
            return json.load(f)
    return {"Breite (m)": None, "Länge (m)": None, "Neigung (%)": None}

def save_standards(data):
    with open(STANDARDS_FILE, "w") as f:
        json.dump(data, f)

def extract_id_daten_filtered(filepath):
    """Scan all IfcProduct; for configured targets, pull only whitelisted ID-Daten keys.
       Numbers are rounded to 2 decimals."""
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

    # Build rows
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

                results.append({
                    "Short": tgt["short"],
                    "IfcType": e.is_a(),
                    "GlobalId": e.GlobalId,
                    "Name": name,           # full original (hover/tooltip if you want)
                    "Values": filtered,     # dict column_label -> value
                })
                break  # prevent duplicate matches for the same element

    return results

def compute_table_columns(rows):
    """Union of all column labels in 'Values' to build a dynamic table header."""
    cols = set()
    for r in rows:
        cols.update(r["Values"].keys())
    # Put ramp columns in a friendly order if present
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

        # Optional: perform checks for Rampe rows only
        for r in rows:
            if r["Short"].lower() == "rampe":
                b = r["Values"].get("Breite (m)")
                l = r["Values"].get("Länge (m)")
                n = r["Values"].get("Neigung (%)")
                r["Breite_ok"]  = (standards.get("Breite (m)")  is None) or (b is not None and b >= standards["Breite (m)"])
                r["Länge_ok"]   = (standards.get("Länge (m)")   is None) or (l is not None and l >= standards["Länge (m)"])
                r["Neigung_ok"] = (standards.get("Neigung (%)") is None) or (n is not None and n <= standards["Neigung (%)"])

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
        flash("Zugriff verweigert.")
        return redirect(url_for("index"))

@app.route('/upload_standard', methods=['POST'])
def upload_standard():
    # Note: align keys with dynamic columns used for ramps
    breite = request.form.get("breite", type=float)
    laenge = request.form.get("laenge", type=float)
    neigung = request.form.get("neigung", type=float)

    save_standards({"Breite (m)": breite, "Länge (m)": laenge, "Neigung (%)": neigung})
    flash("Standards gespeichert!")
    return redirect(url_for('index'))

# -----------------------------
# Main
# -----------------------------

if __name__ == '__main__':
    os.makedirs(UPLOAD_IFC_FOLDER, exist_ok=True)
    app.run(debug=True, use_reloader=True)
