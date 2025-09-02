from flask import Flask, render_template, request, redirect, flash, url_for, session, send_file, abort
from io import BytesIO
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
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
# Detection config
# -----------------------------
TARGETS = [
    {
        "match": ["Schwelle"],          # <-- simplified match
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

        flat_for_pdf = _flatten_results_for_pdf(rows, standards)
        session["last_results_pdf"] = flat_for_pdf

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
    """
    Reads the grid cells:
      comp_<Obj>_<AttrKey>, val_<Obj>_<AttrKey>, min_<Obj>_<AttrKey>, max_<Obj>_<AttrKey>
    and writes the same flat 'standards.json' used by the checker.
    """
    current = load_standards()

    # Helper to read a cell trio and apply a write function
    def read_cell(obj, key, write_fn):
        comp = request.form.get(f"comp_{obj}_{key}")
        val  = _num_from(request.form.get(f"val_{obj}_{key}"))
        mn   = _num_from(request.form.get(f"min_{obj}_{key}"))
        mx   = _num_from(request.form.get(f"max_{obj}_{key}"))
        write_fn(comp, val, mn, mx)

    changed = 0

    # Rampe / Breite -> "Breite (m)"
    def w_rampe_breite(comp, val, mn, mx):
        nonlocal changed
        if comp == "range" and mn is not None and mx is not None:
            # For backward-compat we still store only central 'Breite (m)' if present;
            # but since checker uses ≥, we prefer a single threshold. Range is unusual here.
            current["Breite (m)"] = round(mn, 3)
            changed += 1
        elif val is not None:
            current["Breite (m)"] = round(val, 3); changed += 1
    read_cell("Rampe", "Breite", w_rampe_breite)

    # Rampe / Länge -> "Länge (m)"
    def w_rampe_laenge(comp, val, mn, mx):
        nonlocal changed
        if comp == "range" and mn is not None and mx is not None:
            current["Länge (m)"] = round(mn, 3); changed += 1
        elif val is not None:
            current["Länge (m)"] = round(val, 3); changed += 1
    read_cell("Rampe", "Laenge", w_rampe_laenge)

    # Rampe / Neigung -> "Neigung (%)" (checker expects max value)
    def w_rampe_neigung(comp, val, mn, mx):
        nonlocal changed
        if comp == "range" and mn is not None and mx is not None:
            # Store max bound as checker threshold
            current["Neigung (%)"] = round(mx, 3); changed += 1
        elif val is not None:
            current["Neigung (%)"] = round(val, 3); changed += 1
    read_cell("Rampe", "Neigung", w_rampe_neigung)

    # Schwelle / Spurbreite -> "Spurbreite (m)" (treat central value)
    def w_schwelle_spurbreite(comp, val, mn, mx):
        nonlocal changed
        if comp == "range" and mn is not None and mx is not None:
            # store mid point
            mid = (mn + mx) / 2.0
            current["Spurbreite (m)"] = round(mid, 3); changed += 1
        elif val is not None:
            current["Spurbreite (m)"] = round(val, 3); changed += 1
    read_cell("Schwelle", "Spurbreite", w_schwelle_spurbreite)

    # Schiene / Längsneigung -> "Längsneigung (%)" (checker expects max)
    def w_schiene_laengsneigung(comp, val, mn, mx):
        nonlocal changed
        if comp == "range" and mn is not None and mx is not None:
            current["Längsneigung (%)"] = round(mx, 3); changed += 1
        elif val is not None:
            current["Längsneigung (%)"] = round(val, 3); changed += 1
    read_cell("Schiene", "Laengsneigung", w_schiene_laengsneigung)

    # Bahnsteig / Bahnsteighöhe -> two keys: min & max
    def w_bahnsteig_hoehe(comp, val, mn, mx):
        nonlocal changed
        if comp == "range":
            if mn is not None:
                current["Bahnsteighöhe min (m)"] = round(mn, 3); changed += 1
            if mx is not None:
                current["Bahnsteighöhe max (m)"] = round(mx, 3); changed += 1
        else:
            # single value provided: treat as 'min' if none present
            if val is not None:
                current["Bahnsteighöhe min (m)"] = round(val, 3); changed += 1
    read_cell("Bahnsteig", "Bahnsteighoehe", w_bahnsteig_hoehe)

    # Mast / Abstand Gleismitte -> "Abstand Gleismitte (m)" (checker expects min)
    def w_mast_abstand(comp, val, mn, mx):
        nonlocal changed
        if comp == "range" and mn is not None and mx is not None:
            current["Abstand Gleismitte (m)"] = round(mn, 3); changed += 1
        elif val is not None:
            current["Abstand Gleismitte (m)"] = round(val, 3); changed += 1
    read_cell("Mast", "Abstand_Gleismitte", w_mast_abstand)

    if changed == 0:
        flash("Nichts gespeichert – keine Änderungen erkannt.", "error")
        return redirect(url_for('admin_upload'))

    save_standards(current)
    flash("Standards gespeichert.", "success")
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

# -----------------------------
# Main
# -----------------------------
if __name__ == '__main__':
    os.makedirs(UPLOAD_IFC_FOLDER, exist_ok=True)
    app.run(debug=True, use_reloader=True)
