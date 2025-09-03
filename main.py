from flask import Flask, render_template, request, redirect, flash, url_for, session, send_file, abort, send_from_directory
from io import BytesIO
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import BaseDocTemplate, PageTemplate, Frame
from reportlab.pdfgen.canvas import Canvas
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth
import os, json, hashlib
from dotenv import load_dotenv
from openai import OpenAI
from werkzeug.utils import secure_filename
import ifcopenshell
from pdfminer.high_level import extract_text as pdf_extract_text
import requests
from bs4 import BeautifulSoup
import re, html as html_unescape

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

UPLOAD_IFC_FOLDER = 'uploads/ifc'
UPLOAD_SRC_FOLDER = 'uploads/sources'   # local PDF sources
URL_CACHE_FOLDER  = 'url_cache'         # cache for URL fetches (HTML/PDF -> text)
ALLOWED_IFC_EXTENSIONS = {'ifc'}
ALLOWED_SRC_EXTENSIONS = {'pdf'}
STANDARDS_FILE = 'standards.json'
STATIC_IMG_DIR = os.path.join(os.path.dirname(__file__), "static", "img")
DB_LOGO_PATH   = os.path.join(STATIC_IMG_DIR, "db-infrago.png")
UDE_LOGO_PATH  = os.path.join(STATIC_IMG_DIR, "ude-logo.png")

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

def _load_logo(path):
    try:
        if os.path.isfile(path):
            return ImageReader(path)
    except Exception:
        pass
    return None

DB_LOGO_IMG  = _load_logo(DB_LOGO_PATH)
UDE_LOGO_IMG = _load_logo(UDE_LOGO_PATH)

def _wrap_to_width(text, max_width_pt, font="Helvetica", size=7):
    """Greedy wrap into lines that fit max_width_pt with the given font/size."""
    words = (text or "").split()
    lines, cur = [], ""
    for w in words:
        cand = (cur + " " + w).strip()
        if cur and stringWidth(cand, font, size) > max_width_pt:
            lines.append(cur)
            cur = w
        else:
            cur = cand
    if cur:
        lines.append(cur)
    return lines

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
        "_sources": {},  # attribute -> {"link": str|None, "file": filename|None}
        "_ops": {},
        "_ranges": {}
    }
    if os.path.exists(STANDARDS_FILE):
        try:
            with open(STANDARDS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "_sources" not in data:
                    data["_sources"] = {}
                if "_ops" not in data:
                    data["_ops"] = {}
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

LEGAL_FOOTER = (
    "DB InfraGO AG | Sitz: Frankfurt am Main | Registergericht: Frankfurt am Main HRB 50879 | "
    "USt-IdNr.: DE 199861757 | Vorsitz des Aufsichtsrats: Berthold Huber | "
    "Vorstand: Dr. Philipp Nagl (Vorsitz), Jens Bergmann, Dr. Christian Gruß, "
    "Heike Junge-Latz, Klaus Müller, Heinz Siegmund, Ralf Thieme"
)

def _short_gid(gid: str) -> str:
    return gid[:8] + "…" if gid and len(gid) > 9 else (gid or "")

def _status_mark(ok):
    if ok is True:  return "✓"
    if ok is False: return "✗"
    return "–"

def _fit_ellipsis(text, max_width_pt, font_name="Helvetica", font_size=9, ellipsis="…"):
    """
    Return text that fits into max_width_pt (points) using the given font,
    truncating with an ellipsis if needed.
    """
    if text is None:
        return ""
    text = str(text)
    if stringWidth(text, font_name, font_size) <= max_width_pt:
        return text
    # Make sure even just the ellipsis fits
    if stringWidth(ellipsis, font_name, font_size) > max_width_pt:
        return ""  # nothing fits cleanly
    # Binary search for the longest prefix that fits with ellipsis
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        candidate = text[:mid] + ellipsis
        if stringWidth(candidate, font_name, font_size) <= max_width_pt:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + ellipsis

def _collect_summary(rows):
    total = len(rows)
    ok_elems = fail_elems = missing_elems = 0
    for r in rows:
        checks = r.get("checks") or {}
        vals   = r.get("Values") or {}
        considered = {k: checks.get(k) for k, v in vals.items() if v is not None}
        if not considered:
            missing_elems += 1
        elif all(v is True for v in considered.values()):
            ok_elems += 1
        elif any(v is False for v in considered.values()):
            fail_elems += 1
        else:
            missing_elems += 1
    return total, ok_elems, fail_elems, missing_elems

def _flatten_rows_for_detailed_table(rows, standards, ops_map, ranges_map):
    out = []
    for r in rows:
        short   = r.get("Short") or ""
        ifctype = r.get("IfcType") or ""
        gid     = _short_gid(r.get("GlobalId") or "")
        vals    = r.get("Values") or {}
        checks  = r.get("checks") or {}
        for attr, val in vals.items():
            if val is None:
                continue
            op = (ops_map.get(attr) or "").strip()
            std_txt = "-"
            if attr == "Bahnsteighöhe (m)" and op == "range":
                mn = standards.get("Bahnsteighöhe min (m)")
                mx = standards.get("Bahnsteighöhe max (m)")
                if mn is not None or mx is not None:
                    std_txt = f"{mn if mn is not None else '–'}–{mx if mx is not None else '–'}"
            elif op == "range":
                rng = ranges_map.get(attr) or {}
                mn, mx = rng.get("min"), rng.get("max")
                if mn is not None or mx is not None:
                    std_txt = f"{mn if mn is not None else '–'}–{mx if mx is not None else '–'}"
            else:
                v = standards.get(attr)
                if v is not None:
                    std_txt = f"{v}"
            mark = _status_mark(checks.get(attr))
            out.append([short, ifctype, gid, attr, val, std_txt, (op or "—"), mark])
    return out

from reportlab.lib.pagesizes import A4  # make sure this import exists at top

def _make_header_footer(page_count):
    def _header_footer(canvas: Canvas, doc):
        canvas.saveState()
        w, h = A4
        # --- draw logos ---
        # Left: DB InfraGO
        if DB_LOGO_IMG:
            # ~22mm wide, ~8mm high, keep aspect
            canvas.drawImage(
                DB_LOGO_IMG,
                15*mm,               # x
                h - 14*mm,           # y (top area)
                width=22*mm,
                height=8*mm,
                preserveAspectRatio=True,
                mask='auto'
            )

        # Right: UDE logo
        if UDE_LOGO_IMG:
            # ~28mm wide, ~8mm high, keep aspect
            canvas.drawImage(
                UDE_LOGO_IMG,
                w - 15*mm - 28*mm,   # x (flush right with 15mm margin)
                h - 14*mm,           # y
                width=28*mm,
                height=8*mm,
                preserveAspectRatio=True,
                mask='auto'
            )
        # top rule
        canvas.setStrokeColorRGB(0.90, 0.90, 0.92)
        canvas.setLineWidth(0.6)
        canvas.line(15*mm, h-15*mm, w-15*mm, h-15*mm)
        # repeating small title
        canvas.setFont("Helvetica", 9)
        canvas.setFillColorRGB(0.10, 0.10, 0.10)
        canvas.drawString(15*mm, h-19*mm, "Prüfbericht: Automatisierte fachliche Prüfung (IFC-BIM)")
        # --- footer: line, wrapped legal, page X of Y on its own line ---
        canvas.setStrokeColorRGB(0.90, 0.90, 0.92)
        canvas.line(15*mm, 18*mm, w-15*mm, 18*mm)  # move line up for breathing room

        legal_font = "Helvetica"
        legal_size = 6.8
        canvas.setFont(legal_font, legal_size)
        canvas.setFillColorRGB(0.30, 0.30, 0.30)

        left_x   = 15*mm
        right_x  = w - 15*mm
        max_legal_width = right_x - left_x

        legal_lines = _wrap_to_width(LEGAL_FOOTER, max_legal_width, legal_font, legal_size)[:2]
        # draw legal lines
        if legal_lines:
            canvas.drawString(left_x, 12*mm, legal_lines[0])
        if len(legal_lines) > 1:
            canvas.drawString(left_x, 9*mm, legal_lines[1])

        # page number on its own line, right aligned
        canvas.setFont("Helvetica", 8)
        canvas.setFillColorRGB(0.20, 0.20, 0.20)
        y_total = page_count if page_count else ""
        page_txt = f"Seite {canvas.getPageNumber()} von {y_total}".strip()
        canvas.drawRightString(right_x, 9*mm, page_txt)

    return _header_footer

def _generate_results_pdf_report(payload, title="Prüfbericht: Automatisierte fachliche Prüfung (IFC-BIM)"):
    from reportlab.platypus import Paragraph, Spacer, Table, TableStyle, BaseDocTemplate, PageTemplate, Frame
    styles = getSampleStyleSheet()

    rows       = payload.get("rows") or []
    standards  = payload.get("standards") or {}
    ops_map    = payload.get("_ops") or {}
    ranges_map = payload.get("_ranges") or {}

    total, ok_elems, fail_elems, missing_elems = _collect_summary(rows)

    # Styles
    h1 = styles['Title']; h1.fontName="Helvetica-Bold"; h1.fontSize=18; h1.leading=22
    p  = styles['BodyText']; p.fontName="Helvetica"; p.fontSize=10.5; p.leading=14
    small = styles['BodyText'].clone('small'); small.fontSize=9; small.leading=12; small.textColor=colors.HexColor("#555")

    # Build a FRESH story every time (important: flowables are stateful!)
    def _build_story():
        story = []
        story.append(Paragraph(title, h1))
        story.append(Spacer(1, 6))
        story.append(Paragraph("Sehr geehrte Damen und Herren,", p))
        story.append(Spacer(1, 4))
        story.append(Paragraph(
            f"Dieser Bericht fasst die Ergebnisse der automatisierten fachlichen Prüfung des IFC-Modells zusammen. "
            f"Von insgesamt <b>{total}</b> geprüften Elementen erfüllen <b>{ok_elems}</b> die vorgegebenen Anforderungen. "
            f"<b>{fail_elems}</b> Elemente erfüllen die Anforderungen nicht. "
            f"Für <b>{missing_elems}</b> Elemente fehlen erforderliche Werte oder klare Vergleichsregeln.", p))
        story.append(Spacer(1, 8))
        story.append(Paragraph(
            "Prüfgrundlage: Die automatischen Grenzwerte orientieren sich an den hinterlegten Normen/Regelwerken "
            "im Adminbereich; die genaue rechtliche Bewertung obliegt der zuständigen Aufsichtsbehörde.", small))
        story.append(Spacer(1, 10))

        legend = Table([["Legende", "✓ = konform", "✗ = nicht konform", "– = nicht bewertet"]],
                       style=TableStyle([
                           ("FONTNAME", (0,0), (-1,-1), "Helvetica"),
                           ("FONTSIZE", (0,0), (-1,-1), 9),
                           ("TEXTCOLOR", (0,0), (0,0), colors.HexColor("#111")),
                           ("TEXTCOLOR", (1,0), (-1,0), colors.HexColor("#444")),
                           ("BACKGROUND", (0,0), (0,0), colors.HexColor("#F2F4F7")),
                           ("LINEABOVE", (0,0), (-1,0), 0.25, colors.HexColor("#E5E7EB")),
                           ("LINEBELOW", (0,0), (-1,0), 0.25, colors.HexColor("#E5E7EB")),
                           ("LEFTPADDING", (0,0), (-1,-1), 6),
                           ("RIGHTPADDING", (0,0), (-1,-1), 6),
                           ("TOPPADDING", (0,0), (-1,-1), 4),
                           ("BOTTOMPADDING", (0,0), (-1,-1), 4),
                       ]))
        story.append(legend)
        story.append(Spacer(1, 12))

        table_data = [["Objekt", "IFC-Typ", "GlobalId", "Attribut", "Wert", "Grenzwert", "Operator", "Ergebnis"]]
        table_data += _flatten_rows_for_detailed_table(rows, standards, ops_map, ranges_map)

        # --- column widths (points) ---
        col_widths = [22*mm, 26*mm, 28*mm, 50*mm, 18*mm, 24*mm, 18*mm, 14*mm]

        # --- truncate IFC-Typ with ellipsis to fit the column ---
        ifc_col_idx = 1  # "IFC-Typ"
        # Table paddings are ~4pt left + 4pt right -> subtract a bit
        available_pt = col_widths[ifc_col_idx] - 8
        for i in range(1, len(table_data)):  # skip the header row
            cell_text = table_data[i][ifc_col_idx]
            table_data[i][ifc_col_idx] = _fit_ellipsis(cell_text, available_pt, font_name="Helvetica", font_size=9)

        # --- build table ---
        tbl = Table(table_data, repeatRows=1, colWidths=col_widths)
        tbl.setStyle(TableStyle([
            ("FONTNAME", (0,0), (-1,-1), "Helvetica"),
            ("FONTSIZE", (0,0), (-1,-1), 9),
            ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#F2F4F7")),
            ("TEXTCOLOR", (0,0), (-1,0), colors.HexColor("#111111")),
            ("LINEABOVE", (0,0), (-1,0), 0.75, colors.HexColor("#E5E7EB")),
            ("LINEBELOW", (0,0), (-1,0), 0.75, colors.HexColor("#E5E7EB")),
            ("GRID", (0,0), (-1,-1), 0.25, colors.HexColor("#E5E7EB")),
            ("ALIGN", (4,1), (5,-1), "RIGHT"),
            ("ALIGN", (6,1), (7,-1), "CENTER"),
            ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
            ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, colors.HexColor("#FBFBFD")]),
            ("LEFTPADDING", (0,0), (-1,-1), 4),
            ("RIGHTPADDING", (0,0), (-1,-1), 4),
            ("TOPPADDING", (0,0), (-1,-1), 3),
            ("BOTTOMPADDING", (0,0), (-1,-1), 3),
        ]))
        story.append(tbl)
        story.append(Spacer(1, 12))
        story.append(Paragraph("Mit freundlichen Grüßen", p))
        story.append(Spacer(1, 14))
        return story

    def _render(flowables, page_count_override=None):
        buf = BytesIO()
        doc = BaseDocTemplate(buf, pagesize=A4,
                              leftMargin=18*mm, rightMargin=18*mm,
                              topMargin=30*mm, bottomMargin=26*mm)
        frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id='normal')
        doc.addPageTemplates([
            PageTemplate(id='All', frames=[frame], onPage=_make_header_footer(page_count_override))
        ])
        doc.build(flowables)
        pages = doc.canv.getPageNumber()
        buf.seek(0)
        return buf, pages

    # Pass 1: count pages (fresh story)
    tmp_buf, total_pages = _render(_build_story(), page_count_override=None)

    # Pass 2: final render with "von Y" (fresh story)
    final_buf, _ = _render(_build_story(), page_count_override=total_pages)
    final_buf.seek(0)
    return final_buf

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
            text = _text_from_url(link)

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
        ops_map = standards.get('_ops', {}) or {}
        ranges_map = standards.get('_ranges', {}) or {}

        def approx_eq(v, target, tol=0.01):
            return abs(v - target) <= tol

        def check_value(attr_label: str, value: float) -> bool | None:
            """
            Uses saved operator + stored values/ranges to evaluate a single attribute.
            Returns True/False or None if insufficient data.
            """
            if value is None:
                return None

            op = (ops_map.get(attr_label) or "").strip()
            # Bahnsteighöhe uses dedicated min/max keys
            if attr_label == "Bahnsteighöhe (m)":
                if op == "range":
                    mn = standards.get("Bahnsteighöhe min (m)")
                    mx = standards.get("Bahnsteighöhe max (m)")
                    if mn is None and mx is None: return None
                    ok = True
                    if mn is not None: ok = ok and (value >= mn)
                    if mx is not None: ok = ok and (value <= mx)
                    return ok
                # fallback to flat compare if someone set >=/<= or ≈ on Bahnsteighöhe
                target = standards.get("Bahnsteighöhe min (m)")  # use min as anchor
                if target is None: return None
                if op == ">=": return value >= target
                if op == "<=": return value <= target
                if op == "≈":  return approx_eq(value, target, tol=0.01)
                # default: no rule
                return None

            # Non-Bahnsteig attributes
            if op == "range":
                rng = ranges_map.get(attr_label) or {}
                mn, mx = rng.get("min"), rng.get("max")
                if mn is None and mx is None: return None
                ok = True
                if mn is not None: ok = ok and (value >= mn)
                if mx is not None: ok = ok and (value <= mx)
                return ok

            if op == "≈":
                target = standards.get(attr_label)
                if target is None: return None
                # small default tol; tweak per-unit if you want
                tol = 0.001 if attr_label.endswith("(m)") else 0.1
                return approx_eq(value, target, tol=tol)

            if op in (">=", "<="):
                target = standards.get(attr_label)
                if target is None: return None
                return (value >= target) if op == ">=" else (value <= target)

            # No operator saved -> keep legacy defaults (>= for Breite/Länge/Abstand, <= for Neigung/Längsneigung, ≈ for Spurbreite)
            target = standards.get(attr_label)
            if target is None:
                # Bahnsteighöhe legacy handled above via min/max
                return None
            if attr_label in ("Breite (m)", "Länge (m)", "Abstand Gleismitte (m)"):
                return value >= target
            if attr_label in ("Neigung (%)", "Längsneigung (%)"):
                return value <= target
            if attr_label == "Spurbreite (m)":
                return approx_eq(value, target, tol=0.001)
            return None
        
        for r in rows:
            checks = {}
            vals = r["Values"]
            short = (r["Short"] or "").lower()

            if short == "rampe":
                if vals.get("Breite (m)") is not None:
                    checks["Breite (m)"] = check_value("Breite (m)", vals["Breite (m)"])
                if vals.get("Länge (m)") is not None:
                    checks["Länge (m)"] = check_value("Länge (m)", vals["Länge (m)"])
                if vals.get("Neigung (%)") is not None:
                    checks["Neigung (%)"] = check_value("Neigung (%)", vals["Neigung (%)"])

            elif short == "bahnsteig":
                if vals.get("Bahnsteighöhe (m)") is not None:
                    checks["Bahnsteighöhe (m)"] = check_value("Bahnsteighöhe (m)", vals["Bahnsteighöhe (m)"])

            elif short == "schiene":
                if vals.get("Längsneigung (%)") is not None:
                    checks["Längsneigung (%)"] = check_value("Längsneigung (%)", vals["Längsneigung (%)"])

            elif short == "schwelle":
                if vals.get("Spurbreite (m)") is not None:
                    checks["Spurbreite (m)"] = check_value("Spurbreite (m)", vals["Spurbreite (m)"])

            elif short == "mast":
                if vals.get("Abstand Gleismitte (m)") is not None:
                    checks["Abstand Gleismitte (m)"] = check_value("Abstand Gleismitte (m)", vals["Abstand Gleismitte (m)"])

            r["checks"] = checks

        ai_sources = _ai_extract_for_results_local(rows, standards)
        
        # Stash everything needed for the PDF report
        session["report_payload"] = {
        "rows": rows,
        "standards": standards,
        "_ops": ops_map,
        "_ranges": ranges_map,
        }

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
      - new "_ops" map: attribute label -> comparator string (">=", "<=", "≈", "range")
    """
    current = load_standards()
    sources = current.get("_sources", {}) or {}
    ops_prev = current.get("_ops", {}) or {}
    ranges_prev = current.get("_ranges", {}) or {}
    ranges = dict(ranges_prev)
    ops = dict(ops_prev)  # start from previous; overwrite as we read
    ops_changed = False
    os.makedirs(UPLOAD_SRC_FOLDER, exist_ok=True)

    def _merge_source(attr_label, link, saved_filename):
        prev = sources.get(attr_label, {}) or {}
        link = (link or "").strip()
        new_link = link if link else prev.get("link")
        new_file = saved_filename or prev.get("file")
        sources[attr_label] = {"link": new_link or None, "file": new_file or None}

    # Helper to read a cell and apply a write function, then record sources and comparator
    def read_cell(obj, key, write_fn, attr_label_for_source):
        nonlocal ops_changed
        comp = request.form.get(f"comp_{obj}_{key}")
        val  = _num_from(request.form.get(f"val_{obj}_{key}"))
        mn   = _num_from(request.form.get(f"min_{obj}_{key}"))
        mx   = _num_from(request.form.get(f"max_{obj}_{key}"))

        # sources (both optional)
        link = request.form.get(f"srclink_{obj}_{key}", "")
        file_storage = request.files.get(f"srcfile_{obj}_{key}")
        saved_filename = _store_source_pdf(file_storage) if file_storage else None

        write_fn(comp, val, mn, mx)

        # persist comparator for this attribute
        if comp:
            if ops.get(attr_label_for_source) != comp:
                ops_changed = True
            ops[attr_label_for_source] = comp

        # persist ranges if needed (Bahnsteighöhe already uses dedicated min/max keys)
        if comp == "range" and attr_label_for_source != "Bahnsteighöhe (m)":
            ranges[attr_label_for_source] = {"min": mn, "max": mx}
        else:
            # clear any previous range when not in 'range' mode
            ranges.pop(attr_label_for_source, None)

        if (link and link.strip()) or saved_filename:
            _merge_source(attr_label_for_source, link, saved_filename)

    changed = 0

    # Rampe / Breite -> "Breite (m)"
    def w_rampe_breite(comp, val, mn, mx):
        nonlocal changed
        if comp == "range":
            # don't write any flat value; range will be handled separately
            return
        if val is not None:
            current["Breite (m)"] = round(val, 3); changed += 1
    read_cell("Rampe", "Breite", w_rampe_breite, "Breite (m)")

    # Rampe / Länge -> "Länge (m)"
    def w_rampe_laenge(comp, val, mn, mx):
        nonlocal changed
        if comp == "range":
            return
        if val is not None:
            current["Länge (m)"] = round(val, 3); changed += 1
    read_cell("Rampe", "Laenge", w_rampe_laenge, "Länge (m)")

    # Rampe / Neigung -> "Neigung (%)"
    def w_rampe_neigung(comp, val, mn, mx):
        nonlocal changed
        if comp == "range":
            return
        if val is not None:
            current["Neigung (%)"] = round(val, 3); changed += 1
    read_cell("Rampe", "Neigung", w_rampe_neigung, "Neigung (%)")

    # Schwelle / Spurbreite -> "Spurbreite (m)"
    def w_schwelle_spurbreite(comp, val, mn, mx):
        nonlocal changed
        if comp == "range":
            return
        if val is not None:
            current["Spurbreite (m)"] = round(val, 3); changed += 1
    read_cell("Schwelle", "Spurbreite", w_schwelle_spurbreite, "Spurbreite (m)")

    # Schiene / Längsneigung -> "Längsneigung (%)"
    def w_schiene_laengsneigung(comp, val, mn, mx):
        nonlocal changed
        if comp == "range":
            return
        if val is not None:
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
        if comp == "range":
            return
        if val is not None:
            current["Abstand Gleismitte (m)"] = round(val, 3); changed += 1
    read_cell("Mast", "Abstand_Gleismitte", w_mast_abstand, "Abstand Gleismitte (m)")

    # persist numbers + sources + comparators
    current["_sources"] = sources
    current["_ops"] = ops
    current["_ranges"] = ranges

    # (Sources are optional now) — removed the prior "require a source" block.

    # If truly nothing numeric or comparator changed, say so; still save for idempotency
    if changed == 0 and not ops_changed:
        flash("Keine Änderungen erkannt – bestehende Werte/Quellen/Operatoren bleiben unverändert.", "info")
        # Still write to ensure _ops/_sources keys exist consistently
        save_standards(current)
        return redirect(url_for('admin_upload'))

    save_standards(current)
    flash("Standards & Quellen gespeichert.", "success")
    return redirect(url_for('admin_upload'))

@app.route("/download_report")
def download_report():
    payload = session.get("report_payload")
    if not payload:
        return abort(400, description="No results available to export. Upload and check an IFC file first.")
    pdf_buffer = _generate_results_pdf_report(payload)
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

@app.route('/delete_source', methods=['POST'])
def delete_source():
    """
    Remove the stored source for an attribute.
    Accepts:
      - attr: exact attribute label, e.g. "Breite (m)"
      - kind: "file" or "link"
    """
    attr = (request.form.get('attr') or '').strip()
    kind = (request.form.get('kind') or '').strip()

    if not attr or kind not in ('file', 'link'):
        flash('Ungültige Anfrage.', 'error')
        return redirect(url_for('admin_upload'))

    current = load_standards()
    sources = current.get('_sources', {}) or {}
    entry = sources.get(attr, {}) or {}

    # remove file from disk if exists
    if kind == 'file':
        fname = entry.get('file')
        if fname:
            try:
                path = os.path.join(UPLOAD_SRC_FOLDER, fname)
                if os.path.isfile(path):
                    os.remove(path)
            except Exception:
                # ignore disk errors; we still clear the pointer
                pass
        entry['file'] = None

    elif kind == 'link':
        entry['link'] = None

    sources[attr] = entry
    current['_sources'] = sources
    save_standards(current)
    flash('Quelle entfernt.', 'success')
    return redirect(url_for('admin_upload'))

# -----------------------------
# Main
# -----------------------------
if __name__ == '__main__':
    os.makedirs(UPLOAD_IFC_FOLDER, exist_ok=True)
    os.makedirs(UPLOAD_SRC_FOLDER, exist_ok=True)
    os.makedirs(URL_CACHE_FOLDER, exist_ok=True)
    app.run(debug=True, use_reloader=True)
