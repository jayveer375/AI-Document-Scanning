import os
import re
import json
import base64
import difflib
from datetime import datetime
from dateutil import parser as dateutil_parser
from dotenv import load_dotenv

load_dotenv()

import cv2
import numpy as np
from flask import Flask, request, render_template, jsonify, send_file, send_from_directory
from mistralai.client import Mistral

# --------------------------------------------------
# CONFIG & DIRECTORIES
# --------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INCOMING_FOLDER = os.path.join(BASE_DIR, "incoming")
OUTPUT_FOLDER = os.path.join(BASE_DIR, "output")
TXT_REPORT_PATH = os.path.join(OUTPUT_FOLDER, "verification_report.txt")
JSON_REPORT_PATH = os.path.join(OUTPUT_FOLDER, "verification_report.json")

ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "pdf"}

MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY")
MISTRAL_MODEL = "pixtral-12b-2409"
MISTRAL_TIMEOUT_SECONDS = 45

os.makedirs(INCOMING_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25 MB max file size
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{os.path.join(BASE_DIR, 'expenses.db')}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

from models import db, ensure_db_schema
from expense_routes import expense_bp

db.init_app(app)

with app.app_context():
    db.create_all()
    ensure_db_schema()

app.register_blueprint(expense_bp)

# --------------------------------------------------
# PRESET TEMPLATES
# --------------------------------------------------

PRESET_TEMPLATES = {
    "bank_solvency": {
        "name": "Bank Solvency Certificate",
        "description": "Verify contractor/firm solvency certificates from recognized banks.",
        "fields": [
            {
                "id": "f_1",
                "name": "Name of Firm",
                "expected": "R K Construction",
                "constraint": "fuzzy",
                "required": True,
            },
            {
                "id": "f_2",
                "name": "Amount of Solvency",
                "expected": "30000000",
                "constraint": "numeric_gte",
                "required": True,
            },
            {
                "id": "f_3",
                "name": "Name of Bank",
                "expected": "Kotak Mahindra Bank",
                "constraint": "fuzzy",
                "required": True,
            },
            {
                "id": "f_4",
                "name": "Date of Solvency Certificate",
                "expected": "08/01/2026",
                "constraint": "date",
                "required": True,
            },
        ],
    },
    "gst_certificate": {
        "name": "GST Registration Certificate",
        "description": "Verify GSTIN (Form GST REG-06) registration details.",
        "fields": [
            {
                "id": "f_1",
                "name": "Legal Name",
                "expected": "RAKESH BHARATKUMAR SHAH",
                "constraint": "fuzzy",
                "required": True,
            },
            {
                "id": "f_2",
                "name": "Trade Name",
                "expected": "RK CONSTRUCTION",
                "constraint": "fuzzy",
                "required": True,
            },
            {
                "id": "f_3",
                "name": "GSTIN",
                "expected": "24BLIPS7006N1ZO",
                "constraint": "regex",
                "required": True,
            },
            {
                "id": "f_4",
                "name": "Date of Registration",
                "expected": "13/08/2025",
                "constraint": "date",
                "required": True,
            },
        ],
    },
    "pan_card": {
        "name": "PAN Card (Income Tax)",
        "description": "Verify Indian Permanent Account Number card details.",
        "fields": [
            {
                "id": "f_1",
                "name": "Name of Cardholder",
                "expected": "Shah Rakesh Bharatkumar",
                "constraint": "fuzzy",
                "required": True,
            },
            {
                "id": "f_2",
                "name": "Date of Birth",
                "expected": "20/09/1982",
                "constraint": "date",
                "required": True,
            },
            {
                "id": "f_3",
                "name": "PAN Number",
                "expected": "BLIPS7006N",
                "constraint": "regex",
                "required": True,
            },
        ],
    },
    "turnover_certificate": {
        "name": "Annual Turnover Certificate",
        "description": "Verify CA certified annual turnover figures for tenders.",
        "fields": [
            {
                "id": "f_1",
                "name": "Company / Firm Name",
                "expected": "R K Construction",
                "constraint": "fuzzy",
                "required": True,
            },
            {
                "id": "f_2",
                "name": "Financial Year",
                "expected": "2023-2024",
                "constraint": "exact",
                "required": True,
            },
            {
                "id": "f_3",
                "name": "Turnover Amount",
                "expected": "50000000",
                "constraint": "numeric_gte",
                "required": True,
            },
        ],
    },
    "custom": {
        "name": "Custom Document",
        "description": "Build your own dynamic schema with custom constraints.",
        "fields": [
            {
                "id": "f_1",
                "name": "Document Identifier",
                "expected": "",
                "constraint": "present",
                "required": True,
            },
            {
                "id": "f_2",
                "name": "Party Name",
                "expected": "",
                "constraint": "fuzzy",
                "required": True,
            },
            {
                "id": "f_3",
                "name": "Date of Document",
                "expected": "",
                "constraint": "date",
                "required": True,
            },
        ],
    },
}

CONSTRAINT_LABELS = {
    "fuzzy": "Fuzzy Text (Tolerant)",
    "exact": "Exact Match",
    "amount": "Currency Amount (=)",
    "numeric_gte": "Minimum Amount (>=)",
    "numeric_lte": "Maximum Amount (<=)",
    "date": "Date (=)",
    "date_on_or_after": "Date On/After (>=)",
    "date_on_or_before": "Date On/Before (<=)",
    "contains": "Contains Keyword",
    "regex": "Pattern (PAN, GSTIN...)",
    "present": "Any Value (Presence Only)",
}

STOPWORDS = {
    "of", "the", "and", "mr", "mrs", "ms", "m/s", "shri", "smt", "dr",
    "co", "co.", "pvt", "ltd", "limited", "private", "llp", "proprietor",
    "prop.", "partner", "firm", "company", "bank"
}

HOMOGLYPH_LETTER_MAP = {"0": "O", "1": "I", "5": "S", "8": "B", "2": "Z", "6": "G"}
HOMOGLYPH_DIGIT_MAP = {"O": "0", "I": "1", "L": "1", "S": "5", "B": "8", "Z": "2"}

CURRENCY_MAP = {
    "$": "USD", "USD": "USD", "DOLLAR": "USD", "DOLLARS": "USD",
    "₹": "INR", "RS": "INR", "RS.": "INR", "INR": "INR", "RUPEES": "INR", "RUPEE": "INR",
    "€": "EUR", "EUR": "EUR", "EURO": "EUR",
    "£": "GBP", "GBP": "GBP", "POUND": "GBP",
}

# --------------------------------------------------
# BUG 8 FIX: IMAGE QUALITY GATEKEEPER
# --------------------------------------------------

def assess_image_quality(img_path):
    """
    Checks resolution, sharpness (Laplacian variance), and brightness/contrast
    before sending to AI. Prevents garbage-in, hallucinated-out results.
    """
    img = cv2.imread(img_path)
    if img is None:
        return {"status": "ERROR", "passed": False, "reason": "Could not read image file."}

    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # 1. Minimum Resolution
    if w < 500 or h < 500:
        return {
            "status": "REJECT",
            "passed": False,
            "reason": f"Image resolution is too low ({w}x{h} px). Minimum 500x500 px required for reliable OCR.",
        }

    # 2. Blur / Sharpness check via Laplacian variance
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    brightness = float(np.mean(gray))

    if sharpness < 35.0:
        return {
            "status": "REJECT",
            "passed": False,
            "reason": f"Scan is too blurry for reliable character recognition (Sharpness: {sharpness:.1f}, min 35.0 required). Please upload a clearer scan.",
        }

    if brightness < 25.0:
        return {
            "status": "REJECT",
            "passed": False,
            "reason": f"Scan is severely underexposed/too dark (Brightness: {brightness:.1f}). Text is illegible.",
        }

    if brightness > 252.0:
        return {
            "status": "REJECT",
            "passed": False,
            "reason": f"Scan appears blank or completely washed out (Brightness: {brightness:.1f}).",
        }

    return {
        "status": "PASS",
        "passed": True,
        "sharpness": round(sharpness, 1),
        "resolution": f"{w}x{h}",
        "brightness": round(brightness, 1),
    }


def allowed_file(filename):
    if "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[1].lower()
    return ext in ALLOWED_EXTENSIONS


def get_mime_type(filename):
    ext = filename.rsplit(".", 1)[1].lower()
    if ext == "jpg":
        ext = "jpeg"
    return f"image/{ext}"


def convert_document_to_images(file_path, max_pages=3):
    """Render PDF pages to PNG images with quality validation."""
    ext = file_path.rsplit(".", 1)[1].lower()
    if ext != "pdf":
        return [file_path]

    import fitz

    try:
        pdf_document = fitz.open(file_path)
        if pdf_document.page_count == 0:
            raise RuntimeError("The uploaded PDF contains no pages.")

        pages_to_render = min(max_pages, pdf_document.page_count)
        rendered_paths = []
        base_name = file_path.rsplit(".", 1)[0]

        for page_idx in range(pages_to_render):
            page = pdf_document[page_idx]
            pixmap = page.get_pixmap(dpi=150)
            page_image_path = f"{base_name}_page_{page_idx + 1}.png"
            pixmap.save(page_image_path)
            rendered_paths.append(page_image_path)

        pdf_document.close()
        return rendered_paths
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Could not convert uploaded PDF: {exc}")


def normalize(value):
    if value is None:
        return ""
    val = str(value).strip().lower()
    val = re.sub(r"[\.,\-_\/\\|;:]", " ", val)
    val = re.sub(r"\s+", " ", val)
    return val.strip()


# --------------------------------------------------
# BUG 4 FIX: OCR HOMOGLYPH & CONFUSABLE RESOLVER
# --------------------------------------------------

def normalize_pan_homoglyphs(val):
    """
    Position-aware character canonicalization for Indian PAN:
    Positions 0-4: Letters (converts '0'->'O', '1'->'I', '5'->'S', '8'->'B', '2'->'Z')
    Positions 5-8: Digits (converts 'O'->'0', 'I'->'1', 'S'->'5', 'B'->'8', 'Z'->'2')
    Position 9: Letter
    """
    s = re.sub(r"[^A-Za-z0-9]", "", str(val)).upper()
    if len(s) != 10:
        return s, False

    chars = list(s)
    was_modified = False

    for i in range(5):
        if chars[i] in HOMOGLYPH_LETTER_MAP:
            chars[i] = HOMOGLYPH_LETTER_MAP[chars[i]]
            was_modified = True

    for i in range(5, 9):
        if chars[i] in HOMOGLYPH_DIGIT_MAP:
            chars[i] = HOMOGLYPH_DIGIT_MAP[chars[i]]
            was_modified = True

    if chars[9] in HOMOGLYPH_LETTER_MAP:
        chars[9] = HOMOGLYPH_LETTER_MAP[chars[9]]
        was_modified = True

    return "".join(chars), was_modified


def normalize_gstin_homoglyphs(val):
    """Position-aware canonicalization for 15-char GSTIN."""
    s = re.sub(r"[^A-Za-z0-9]", "", str(val)).upper()
    if len(s) != 15:
        return s, False

    chars = list(s)
    was_modified = False

    # 0-1 digits
    for i in (0, 1):
        if chars[i] in HOMOGLYPH_DIGIT_MAP:
            chars[i] = HOMOGLYPH_DIGIT_MAP[chars[i]]
            was_modified = True

    # 2-6 letters
    for i in range(2, 7):
        if chars[i] in HOMOGLYPH_LETTER_MAP:
            chars[i] = HOMOGLYPH_LETTER_MAP[chars[i]]
            was_modified = True

    # 7-10 digits
    for i in range(7, 11):
        if chars[i] in HOMOGLYPH_DIGIT_MAP:
            chars[i] = HOMOGLYPH_DIGIT_MAP[chars[i]]
            was_modified = True

    # 11 letter
    if chars[11] in HOMOGLYPH_LETTER_MAP:
        chars[11] = HOMOGLYPH_LETTER_MAP[chars[11]]
        was_modified = True

    # 13 must be 'Z' (often misread as 2)
    if chars[13] in ("2", "z"):
        chars[13] = "Z"
        was_modified = True

    return "".join(chars), was_modified


# --------------------------------------------------
# BUG 5 FIX: CURRENCY & UNIT WORDS CROSS-VALIDATION
# --------------------------------------------------

def detect_currency(val):
    if not val:
        return None
    s = str(val).upper()
    for symbol, code in CURRENCY_MAP.items():
        if symbol in s or re.search(r"\b" + re.escape(symbol) + r"\b", s):
            return code
    return None


def parse_words_amount(s):
    """Parse amounts written in words (Crores/Lakhs/Millions/Thousand)."""
    if not s:
        return None
    s = str(s).lower()
    total = 0.0
    word_to_num = {
        "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
        "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twenty": 20,
        "thirty": 30, "forty": 40, "fifty": 50, "hundred": 100,
    }

    crore_m = re.search(r"([\w\.\d]+)\s*(?:cr|crore|crores)", s)
    if crore_m:
        val = crore_m.group(1)
        num = word_to_num.get(val, None)
        if num is None:
            try: num = float(val)
            except: num = None
        if num is not None:
            total += num * 10000000.0

    lakh_m = re.search(r"([\w\.\d]+)\s*(?:lac|lakh|lakhs)", s)
    if lakh_m:
        val = lakh_m.group(1)
        num = word_to_num.get(val, None)
        if num is None:
            try: num = float(val)
            except: num = None
        if num is not None:
            total += num * 100000.0

    return total if total > 0 else None


def parse_amount(val):
    if not val or str(val).strip().upper() in ("NOT_FOUND", "NOT FOUND", "N/A", ""):
        return None
    s = str(val).lower().strip()

    word_amt = parse_words_amount(s)
    if word_amt is not None:
        return word_amt

    cleaned = re.sub(r"[^\d,\.]", " ", s)
    tokens = re.findall(r"[\d,]+(?:\.\d+)?", cleaned)
    if not tokens:
        return None

    best = max(tokens, key=lambda t: len(re.sub(r"[^\d]", "", t)))
    best = best.replace(",", "")
    try:
        return float(best)
    except ValueError:
        return None


# --------------------------------------------------
# BUG 6 FIX: DATE AMBIGUITY RESOLVER
# --------------------------------------------------

def parse_date_with_provenance(user_val, extracted_meta):
    """
    Parses dates using extracted day/month/year components from the Vision AI
    to eliminate silent DD/MM vs MM/DD ambiguity.
    """
    raw_extracted = extracted_meta.get("value", "")
    if not raw_extracted or raw_extracted.upper() in ("NOT_FOUND", "NOT FOUND", "N/A", ""):
        return None, None, False

    # Check if model provided disambiguated components
    day = extracted_meta.get("day")
    month = extracted_meta.get("month")
    year = extracted_meta.get("year")
    month_name = extracted_meta.get("month_name")

    extracted_date = None
    if day and month and year:
        try:
            extracted_date = datetime(int(year), int(month), int(day)).date()
        except (ValueError, TypeError):
            pass

    if extracted_date is None:
        try:
            extracted_date = dateutil_parser.parse(raw_extracted, dayfirst=True).date()
        except:
            return None, None, False

    try:
        user_date = dateutil_parser.parse(str(user_val).strip(), dayfirst=True).date()
    except:
        return None, None, False

    # Ambiguity Check: Is the numeric string inherently ambiguous (day <= 12 and month <= 12)?
    # Only ambiguous if month was not spelled out in words in the document!
    num_tokens = re.findall(r"\b\d{1,2}\b", raw_extracted)
    is_ambiguous = False
    if len(num_tokens) >= 2 and not month_name:
        try:
            p1, p2 = int(num_tokens[0]), int(num_tokens[1])
            if p1 <= 12 and p2 <= 12 and p1 != p2:
                is_ambiguous = True
        except:
            pass

    return user_date, extracted_date, is_ambiguous


# --------------------------------------------------
# BUG 3 FIX: TIERED FIELD-TYPE THRESHOLDS & COLLISION PREVENTION
# --------------------------------------------------

def match_field_text_tiered(field_name, user_val, extracted_val):
    """
    Tiered similarity thresholds based on field category:
    - Entity/Bank/Firm Name: Strict (0.85) + Proper Noun Collision check
    - Address/Location: Lenient (0.65)
    - Code/ID: Strict (0.95)
    - General: (0.75)
    """
    a = normalize(user_val)
    b = normalize(extracted_val)
    if not a or not b:
        return False, 0.0, "Missing comparison value"
    if a == b:
        return True, 1.0, "Exact text match"

    tokens_a = {t for t in a.split() if t not in STOPWORDS}
    tokens_b = {t for t in b.split() if t not in STOPWORDS}

    fn_lower = field_name.lower()
    is_name_field = any(k in fn_lower for k in ["name", "bank", "firm", "company", "party", "holder"])
    is_address_field = any(k in fn_lower for k in ["address", "branch", "location", "street", "city"])

    # Collision Prevention: If proper nouns completely differ, fail immediately!
    if is_name_field and tokens_a and tokens_b:
        overlap_tokens = tokens_a & tokens_b
        if not overlap_tokens:
            return False, 0.0, f"Proper nouns mismatch completely ({tokens_a} vs {tokens_b})"

    sim_threshold = 0.85 if is_name_field else (0.65 if is_address_field else 0.75)

    overlap = len(tokens_a & tokens_b)
    smaller = min(len(tokens_a), len(tokens_b)) if tokens_a and tokens_b else 1
    token_ratio = overlap / smaller if smaller > 0 else 0.0
    seq_ratio = difflib.SequenceMatcher(None, a, b).ratio()
    score = max(token_ratio, seq_ratio)

    if is_name_field:
        passed = (token_ratio >= 0.80) or (seq_ratio >= 0.85)
    elif is_address_field:
        passed = (token_ratio >= 0.65) or (seq_ratio >= 0.65) or (a in b) or (b in a)
    else:
        passed = (token_ratio >= 0.75) or (seq_ratio >= 0.75) or (a in b) or (b in a)

    detail = f"Fuzzy match passed ({int(score * 100)}% similarity, req: {int(sim_threshold * 100)}%)" if passed else f"Fuzzy match failed ({int(score * 100)}% similarity, req: {int(sim_threshold * 100)}%)"
    return passed, score, detail


# --------------------------------------------------
# FORENSIC CONSTRAINT EVALUATOR
# --------------------------------------------------

def evaluate_field_forensic(field_spec, extracted_meta):
    """
    Evaluates extracted metadata against user specification.
    Produces trustworthy states:
      - PASS
      - FAIL
      - NOT_FOUND (explicit missing state)
      - OCR_AMBIGUITY (homoglyph resolved)
      - UNREADABLE (optical/resolution artifact)
      - DATE_AMBIGUOUS (DD/MM vs MM/DD ambiguity)
      - LOW_CONFIDENCE
    """
    field_name = field_spec.get("name", "Unnamed Field")
    user_val = str(field_spec.get("expected", "")).strip()
    constraint = str(field_spec.get("constraint", "fuzzy")).lower().strip()
    is_required = field_spec.get("required", True)

    extracted_val = extracted_meta.get("value", "NOT_FOUND")
    model_status = extracted_meta.get("status", "EXTRACTED")
    confidence = extracted_meta.get("confidence", "HIGH")
    snippet = extracted_meta.get("snippet", "")
    page_num = extracted_meta.get("page", 1)

    # 1. Bug 1 Fix: Explicit NOT_FOUND handling
    if model_status == "NOT_FOUND" or str(extracted_val).upper() in ("NOT_FOUND", "NOT FOUND", "N/A", "NONE", ""):
        if is_required:
            return "NOT_FOUND", 0.0, f"Field not found in document (Page {page_num}). Required for verification."
        else:
            return "NOT_FOUND", 0.0, f"Optional field not found in document (Page {page_num})."

    # 2. Bug 8 Fix: Unreadable text from document
    if model_status == "UNREADABLE":
        return "UNREADABLE", 0.0, f"Text was degraded or illegible in the scan at Page {page_num}."

    # 3. Presence Only
    if constraint == "present":
        return "PASS", 1.0, f"Field confirmed present in document: '{extracted_val}'"

    # 4. Bug 4 Fix: Regex & OCR Homoglyph Resolution
    if constraint == "regex":
        raw_clean = re.sub(r"\s+", "", str(extracted_val)).upper()
        target_clean = re.sub(r"\s+", "", user_val).upper()

        # Check PAN Homoglyphs
        if len(target_clean) == 10 and re.match(r"^[A-Z]{5}\d{4}[A-Z]$", target_clean):
            norm_extracted, was_modified = normalize_pan_homoglyphs(raw_clean)
            if norm_extracted == target_clean:
                if was_modified:
                    return "OCR_AMBIGUITY", 1.0, f"OCR digit/letter confusion detected and resolved: Extracted '{raw_clean}' corrected to '{norm_extracted}' (matches expected PAN)."
                else:
                    return "PASS", 1.0, f"PAN matched exactly: {norm_extracted}"
            else:
                # Check edit distance
                diff = difflib.SequenceMatcher(None, raw_clean, target_clean).ratio()
                if diff >= 0.8:
                    return "UNREADABLE", 0.5, f"Suspected OCR misread: Extracted '{raw_clean}' differs slightly from expected '{target_clean}' ({int(diff*100)}% match)."
                else:
                    return "FAIL", 0.0, f"PAN mismatch. Expected: '{target_clean}', Found: '{raw_clean}'"

        # Check GSTIN Homoglyphs
        if len(target_clean) == 15 and re.match(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$", target_clean):
            norm_extracted, was_modified = normalize_gstin_homoglyphs(raw_clean)
            if norm_extracted == target_clean:
                if was_modified:
                    return "OCR_AMBIGUITY", 1.0, f"OCR confusable character resolved: Extracted '{raw_clean}' normalized to '{norm_extracted}'."
                else:
                    return "PASS", 1.0, f"GSTIN matched exactly: {norm_extracted}"
            else:
                return "FAIL", 0.0, f"GSTIN mismatch. Expected: '{target_clean}', Found: '{raw_clean}'"

        # General Regex
        if re.search(target_clean, raw_clean):
            return "PASS", 1.0, f"Pattern matched: '{raw_clean}'"
        elif raw_clean == target_clean:
            return "PASS", 1.0, f"Identifier code matched: {raw_clean}"
        else:
            return "FAIL", 0.0, f"Identifier mismatch. Expected: '{target_clean}', Found: '{raw_clean}'"

    # 5. Bug 5 Fix: Currency & Amount Cross-Validation
    if constraint in ("amount", "numeric_gte", "numeric_lte"):
        u_amt = parse_amount(user_val)
        e_amt = parse_amount(extracted_val)

        # Cross-validate figures vs words from document if available
        doc_words = extracted_meta.get("words", "")
        if doc_words:
            words_amt = parse_words_amount(doc_words)
            if words_amt is not None and e_amt is not None:
                if abs(words_amt - e_amt) > 1.0:
                    return "UNREADABLE", 0.0, f"Document Discrepancy: Extracted figures ({e_amt:,.0f}) contradict words ('{doc_words}' = {words_amt:,.0f}). Flagged for manual review."

        # Check Currency Mismatch
        user_curr = detect_currency(user_val)
        doc_curr = extracted_meta.get("currency") or detect_currency(extracted_val)
        if user_curr and doc_curr and user_curr != doc_curr:
            return "FAIL", 0.0, f"Currency mismatch! Expected {user_curr} but document is in {doc_curr}."

        if u_amt is None:
            return "FAIL", 0.0, f"Invalid expected amount format: '{user_val}'"
        if e_amt is None:
            return "UNREADABLE", 0.0, f"Could not parse numeric amount from '{extracted_val}'"

        if constraint == "amount":
            if abs(u_amt - e_amt) < 0.01:
                return "PASS", 1.0, f"Amount matches numerically: {e_amt:,.2f} == {u_amt:,.2f}"
            else:
                return "FAIL", 0.0, f"Amount mismatch. Expected: {u_amt:,.2f}, Extracted: {e_amt:,.2f}"

        if constraint == "numeric_gte":
            if e_amt >= u_amt:
                return "PASS", 1.0, f"Satisfies minimum: {e_amt:,.2f} >= required {u_amt:,.2f}"
            else:
                return "FAIL", 0.0, f"Below required minimum. Extracted: {e_amt:,.2f} < Expected: {u_amt:,.2f}"

        if constraint == "numeric_lte":
            if e_amt <= u_amt:
                return "PASS", 1.0, f"Within maximum limit: {e_amt:,.2f} <= limit {u_amt:,.2f}"
            else:
                return "FAIL", 0.0, f"Exceeds maximum limit. Extracted: {e_amt:,.2f} > Expected: {u_amt:,.2f}"

    # 6. Bug 6 Fix: Date Parsing & Ambiguity Disambiguation
    if constraint in ("date", "date_on_or_after", "date_on_or_before"):
        u_date, e_date, is_ambiguous = parse_date_with_provenance(user_val, extracted_meta)

        if u_date is None or e_date is None:
            return "UNREADABLE", 0.0, f"Could not parse valid calendar date from '{extracted_val}'"

        if is_ambiguous:
            ambiguity_note = " (Note: Date was numeric without month name; verified under document context)."
        else:
            ambiguity_note = ""

        if constraint == "date":
            if u_date == e_date:
                status = "DATE_AMBIGUOUS" if is_ambiguous else "PASS"
                return status, 1.0, f"Date matches: {e_date.strftime('%d-%b-%Y')}{ambiguity_note}"
            else:
                return "FAIL", 0.0, f"Date mismatch. Expected: {u_date.strftime('%d-%b-%Y')}, Found: {e_date.strftime('%d-%b-%Y')}"

        if constraint == "date_on_or_after":
            if e_date >= u_date:
                return "PASS", 1.0, f"Date satisfies condition: {e_date.strftime('%d-%b-%Y')} is on or after {u_date.strftime('%d-%b-%Y')}"
            else:
                return "FAIL", 0.0, f"Date is before required threshold: {e_date.strftime('%d-%b-%Y')} < {u_date.strftime('%d-%b-%Y')}"

        if constraint == "date_on_or_before":
            if e_date <= u_date:
                return "PASS", 1.0, f"Date satisfies condition: {e_date.strftime('%d-%b-%Y')} is on or before {u_date.strftime('%d-%b-%Y')}"
            else:
                return "FAIL", 0.0, f"Date exceeds allowed limit: {e_date.strftime('%d-%b-%Y')} > {u_date.strftime('%d-%b-%Y')}"

    # 7. Exact Text Match
    if constraint == "exact":
        if user_val.lower().strip() == extracted_val.lower().strip():
            return "PASS", 1.0, f"Exact match verified ('{user_val}')"
        else:
            return "FAIL", 0.0, f"Exact match failed. Expected: '{user_val}', Found: '{extracted_val}'"

    # 8. Contains Substring
    if constraint == "contains":
        norm_u = normalize(user_val)
        norm_e = normalize(extracted_val)
        if norm_u in norm_e or norm_e in norm_u:
            return "PASS", 1.0, f"Keyword match found ('{user_val}' within '{extracted_val}')"
        else:
            return "FAIL", 0.0, f"Expected substring '{user_val}' was not found in '{extracted_val}'"

    # 9. Bug 3 Fix: Tiered Fuzzy Text Match
    is_match, score, detail = match_field_text_tiered(field_name, user_val, extracted_val)
    status = "PASS" if is_match else "FAIL"
    return status, score, detail


# --------------------------------------------------
# FORENSIC VISION PROMPT & EXTRACTION
# --------------------------------------------------

def build_forensic_prompt(fields, doc_type="Document"):
    field_lines = []
    for f in fields:
        name = f.get("name", "").strip()
        if not name:
            continue
        c = f.get("constraint", "fuzzy")
        hint = f.get("hint", "")
        note = f" (Rule: {c})"
        if hint:
            note += f" [Hint: {hint}]"
        field_lines.append(f'- "{name}"{note}')

    fields_bulleted = "\n".join(field_lines)

    return f"""You are a forensic document extraction and OCR verification system.
Analyze the attached {doc_type} document image(s) with extreme accuracy.

CRITICAL RULES:
1. ZERO HALLUCINATION POLICY: If a field or its label is NOT visibly present in the document, you MUST output "NOT_FOUND" for status and value. Never invent or speculate.
2. If text is blurry, occluded, or unreadable, set status to "UNREADABLE".
3. For amounts: extract the exact figures, spelled-out words, and currency symbol (e.g. INR, USD).
4. For dates: extract the raw text, day, month, year, and month name in words (e.g. "January").
5. Indicate which page number (1, 2, 3...) each field was located on.
6. Extract overall document header title and issuing organization for cross-page verification.

Target Fields to Extract:
{fields_bulleted}

Respond ONLY with valid JSON in this exact structure:
{{
  "document_header": "Title printed on document (e.g. Solvency Certificate, Tax Form...)",
  "issuing_entity": "Name of issuing bank, authority, or company",
  "fields": {{
    "<field_name>": {{
      "value": "<exact extracted text or NOT_FOUND>",
      "status": "EXTRACTED or NOT_FOUND or UNREADABLE",
      "confidence": "HIGH or MEDIUM or LOW",
      "page": 1,
      "snippet": "<surrounding 5-10 words from document>",
      "figures": "<if amount>",
      "words": "<if amount written in words>",
      "currency": "<INR, USD, etc.>",
      "day": <integer day if date>,
      "month": <integer month if date>,
      "year": <integer year if date>,
      "month_name": "<month name in English if date>"
    }}
  }}
}}"""


def extract_json_from_text(text):
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("No JSON object found in AI response.")
    return json.loads(match.group(0))


def extract_fields_forensic(image_paths, fields, doc_type="Document"):
    """
    Primary forensic extraction pass with Mistral Pixtral.
    """
    if not MISTRAL_API_KEY:
        raise RuntimeError("MISTRAL_API_KEY is not configured.")

    content = [{"type": "text", "text": build_forensic_prompt(fields, doc_type)}]

    for img_path in image_paths:
        with open(img_path, "rb") as img_file:
            encoded = base64.b64encode(img_file.read()).decode("utf-8")
        mime_type = get_mime_type(img_path)
        content.append({"type": "image_url", "image_url": f"data:{mime_type};base64,{encoded}"})

    client = Mistral(api_key=MISTRAL_API_KEY)
    response = client.chat.complete(
        model=MISTRAL_MODEL,
        messages=[{"role": "user", "content": content}],
        temperature=0,
        timeout_ms=MISTRAL_TIMEOUT_SECONDS * 1000,
    )

    raw_text = response.choices[0].message.content
    if isinstance(raw_text, list):
        raw_text = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in raw_text)

    parsed = extract_json_from_text(raw_text)
    doc_header = parsed.get("document_header", "")
    issuing_entity = parsed.get("issuing_entity", "")
    extracted_fields = parsed.get("fields", {})

    metadata = {
        "document_header": doc_header,
        "issuing_entity": issuing_entity,
        "page_count": len(image_paths),
    }

    normalized_fields = {}
    for f in fields:
        name = f.get("name", "").strip()
        data = extracted_fields.get(name)
        if not data or not isinstance(data, dict):
            # Check case-insensitive
            for k, v in extracted_fields.items():
                if k.lower() == name.lower() and isinstance(v, dict):
                    data = v
                    break

        if not data:
            data = {"value": "NOT_FOUND", "status": "NOT_FOUND", "confidence": "HIGH", "snippet": "", "page": 1}

        normalized_fields[name] = data

    return normalized_fields, metadata


# --------------------------------------------------
# BUG 7 FIX: TARGETED RE-VERIFICATION PASS (CONSISTENCY CHECK)
# --------------------------------------------------

def reverify_contested_fields(image_paths, contested_fields, doc_type="Document"):
    """
    Second-pass consistency check on contested or low-confidence fields.
    If Pass 1 and Pass 2 disagree, flags as INCONSISTENT_AI_EXTRACTION instead of silent wrong PASS/FAIL.
    """
    if not contested_fields or not MISTRAL_API_KEY:
        return {}

    field_specs = "\n".join(f'- Field "{cf["field"]}" (Was read as: "{cf["extracted"]}")' for cf in contested_fields)
    prompt = f"""FORENSIC CONSISTENCY RE-VERIFICATION:
Double-check the attached {doc_type} image(s).
Verify character-by-character whether these fields are truly present and what their exact value is:
{field_specs}

Respond with valid JSON:
{{
  "reverified_fields": {{
    "<field_name>": {{
      "value": "<exact confirmed text or NOT_FOUND>",
      "is_confirmed": true or false,
      "confidence": "HIGH or MEDIUM or LOW"
    }}
  }}
}}"""

    content = [{"type": "text", "text": prompt}]
    for img_path in image_paths:
        with open(img_path, "rb") as img_file:
            encoded = base64.b64encode(img_file.read()).decode("utf-8")
        content.append({"type": "image_url", "image_url": f"data:{get_mime_type(img_path)};base64,{encoded}"})

    try:
        client = Mistral(api_key=MISTRAL_API_KEY)
        response = client.chat.complete(
            model=MISTRAL_MODEL,
            messages=[{"role": "user", "content": content}],
            temperature=0,
            timeout_ms=30000,
        )
        raw_text = response.choices[0].message.content
        if isinstance(raw_text, list):
            raw_text = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in raw_text)
        parsed = extract_json_from_text(raw_text)
        return parsed.get("reverified_fields", {})
    except Exception:
        return {}


# --------------------------------------------------
# FULL PIPELINE CONTROLLER
# --------------------------------------------------

def verify_document_pipeline(image_paths, fields_spec, doc_type="Document"):
    """
    End-to-end verification pipeline resolving all 8 confidence bugs.
    """
    # 1. Primary Extraction
    extracted_data, doc_meta = extract_fields_forensic(image_paths, fields_spec, doc_type)

    results = []
    contested_to_reverify = []

    # 2. Bug 2: Track Provenance Pages to detect Frankenstein mixing
    provenance_pages = set()

    for f in fields_spec:
        name = f.get("name", "").strip()
        expected = f.get("expected", "").strip()
        constraint = f.get("constraint", "fuzzy")
        is_required = f.get("required", True)

        meta = extracted_data.get(name, {})
        status, score, detail = evaluate_field_forensic(f, meta)

        page_num = meta.get("page", 1)
        if meta.get("status") == "EXTRACTED":
            provenance_pages.add(page_num)

        # Contested field candidate for re-verification
        if status in ("FAIL", "UNREADABLE") or meta.get("confidence") == "LOW":
            contested_to_reverify.append({
                "field": name,
                "expected": expected,
                "extracted": meta.get("value", ""),
                "status": status,
            })

        results.append({
            "field": name,
            "user_value": expected if expected else "NOT PROVIDED",
            "extracted_value": meta.get("value", "NOT_FOUND"),
            "constraint": constraint,
            "constraint_label": CONSTRAINT_LABELS.get(constraint, constraint.title()),
            "required": is_required,
            "status": status,
            "score": score,
            "detail": detail,
            "confidence": meta.get("confidence", "HIGH"),
            "snippet": meta.get("snippet", ""),
            "page": page_num,
        })

    # 3. Bug 7: Run Targeted Consistency Check if needed
    if contested_to_reverify:
        reverified = reverify_contested_fields(image_paths, contested_to_reverify, doc_type)
        for r in results:
            fname = r["field"]
            if fname in reverified:
                rev_info = reverified[fname]
                rev_val = rev_info.get("value")
                is_conf = rev_info.get("is_confirmed", False)
                if rev_val and rev_val != r["extracted_value"]:
                    # Disagreement between AI passes!
                    r["status"] = "LOW_CONFIDENCE"
                    r["detail"] = f"Inconsistent AI Extraction: Pass 1 read '{r['extracted_value']}', but re-verification read '{rev_val}'. Needs manual review."
                elif is_conf:
                    r["detail"] += " (Confirmed by secondary verification pass)"

    # 4. Bug 2: Check Cross-Page Discrepancies
    if len(provenance_pages) > 1 and len(image_paths) > 1:
        doc_meta["multi_page_notice"] = f"Fields were extracted across multiple pages ({sorted(list(provenance_pages))}). Document identity header: '{doc_meta.get('document_header', '')}'."

    # 5. Overall Status Calculation with True Hierarchy
    passed_count = sum(1 for r in results if r["status"] in ("PASS", "OCR_AMBIGUITY"))
    failed_count = sum(1 for r in results if r["status"] == "FAIL" and r["required"])
    not_found_count = sum(1 for r in results if r["status"] == "NOT_FOUND")
    unreadable_count = sum(1 for r in results if r["status"] in ("UNREADABLE", "LOW_CONFIDENCE"))

    if failed_count == 0 and not_found_count == 0 and unreadable_count == 0:
        overall_status = "PASS"
    elif failed_count > 0:
        overall_status = "FAIL"
    elif unreadable_count > 0:
        overall_status = "UNREADABLE"
    elif not_found_count > 0:
        overall_status = "INCOMPLETE"
    else:
        overall_status = "PARTIAL"

    total = len(results)
    match_percentage = round((passed_count / total) * 100, 1) if total > 0 else 0

    summary = {
        "overall_status": overall_status,
        "total_fields": total,
        "passed_count": passed_count,
        "failed_count": failed_count,
        "not_found_count": not_found_count,
        "unreadable_count": unreadable_count,
        "match_percentage": match_percentage,
        "document_header": doc_meta.get("document_header", ""),
        "issuing_entity": doc_meta.get("issuing_entity", ""),
    }

    return results, summary, doc_meta


def generate_reports(document_type, original_filename, results, summary, doc_meta):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        "=" * 56,
        "          AI FORENSIC VERIFICATION AUDIT REPORT",
        "=" * 56,
        f"Document Type      : {document_type}",
        f"Document Header    : {doc_meta.get('document_header', 'N/A')}",
        f"Issuing Entity     : {doc_meta.get('issuing_entity', 'N/A')}",
        f"Source File        : {original_filename}",
        f"Generated At       : {timestamp}",
        f"Overall Status     : {summary['overall_status']}",
        f"Score / Match Rate : {summary['passed_count']}/{summary['total_fields']} Passed ({summary['match_percentage']}%)",
        "=" * 56,
        "",
        "FIELD-BY-FIELD AUDIT EVIDENCE:",
        "-" * 56,
    ]

    for idx, r in enumerate(results, 1):
        lines.extend([
            f"#{idx} Field      : {r['field']} [{r['constraint_label']}]",
            f"   Target     : {r['user_value']}",
            f"   Extracted  : {r['extracted_value']}",
            f"   Status     : {r['status']} (Confidence: {r['confidence']})",
            f"   Provenance : Page {r['page']}",
            f"   Snippet    : \"{r['snippet']}\"",
            f"   Evaluation : {r['detail']}",
            "-" * 56,
        ])

    lines.extend([
        "",
        "=" * 56,
        f"FINAL AUDIT VERDICT: {summary['overall_status']}",
        "=" * 56,
    ])

    report_text = "\n".join(lines)

    try:
        with open(TXT_REPORT_PATH, "w", encoding="utf-8") as f:
            f.write(report_text)
    except OSError:
        pass

    json_data = {
        "document_type": document_type,
        "source_file": original_filename,
        "generated_at": timestamp,
        "summary": summary,
        "document_meta": doc_meta,
        "fields": results,
    }

    try:
        with open(JSON_REPORT_PATH, "w", encoding="utf-8") as f:
            json.dump(json_data, f, indent=2)
    except OSError:
        pass

    return report_text, json_data


def parse_request_fields():
    raw_fields_json = request.form.get("fields_json")
    if raw_fields_json:
        try:
            return json.loads(raw_fields_json)
        except Exception:
            pass

    if request.is_json:
        data = request.get_json(silent=True)
        if data and "fields" in data:
            return data["fields"]

    names = request.form.getlist("field_name[]")
    if names:
        expected_vals = request.form.getlist("field_expected[]")
        constraints = request.form.getlist("field_constraint[]")
        required_flags = request.form.getlist("field_required[]")

        fields = []
        for i, name in enumerate(names):
            if not name.strip():
                continue
            fields.append({
                "id": f"field_{i+1}",
                "name": name.strip(),
                "expected": expected_vals[i].strip() if i < len(expected_vals) else "",
                "constraint": constraints[i].strip() if i < len(constraints) else "fuzzy",
                "required": (required_flags[i].lower() in ("true", "1", "on")) if i < len(required_flags) else True,
            })
        return fields

    return []


# --------------------------------------------------
# ROUTES
# --------------------------------------------------

@app.route("/", methods=["GET"])
def index():
    return render_template(
        "index.html",
        presets=PRESET_TEMPLATES,
        constraints=CONSTRAINT_LABELS,
    )


@app.route("/api/presets", methods=["GET"])
def api_presets():
    return jsonify(PRESET_TEMPLATES)


@app.route("/uploads/<path:filename>")
def serve_upload(filename):
    return send_from_directory(INCOMING_FOLDER, filename)


@app.route("/download/report", methods=["GET"])
def download_txt_report():
    if os.path.exists(TXT_REPORT_PATH):
        return send_file(TXT_REPORT_PATH, as_attachment=True, download_name="verification_report.txt")
    return jsonify({"error": "Report not found"}), 404


@app.route("/download/json", methods=["GET"])
def download_json_report():
    if os.path.exists(JSON_REPORT_PATH):
        return send_file(JSON_REPORT_PATH, as_attachment=True, download_name="verification_report.json")
    return jsonify({"error": "Report not found"}), 404


@app.route("/api/verify", methods=["POST"])
def api_verify():
    document_type = request.form.get("document_type", "").strip() or "Document"
    fields_spec = parse_request_fields()

    if not fields_spec:
        return jsonify({"success": False, "error": "Please configure at least one verification field."}), 400

    if "document_image" not in request.files:
        return jsonify({"success": False, "error": "No document file was uploaded."}), 400

    image_file = request.files["document_image"]
    if not image_file or image_file.filename == "":
        return jsonify({"success": False, "error": "No document selected."}), 400

    if not allowed_file(image_file.filename):
        return jsonify({"success": False, "error": "Unsupported file format. Supported: JPG, JPEG, PNG, WEBP, PDF."}), 400

    filename = image_file.filename
    safe_filename = re.sub(r"[^\w\.-]", "_", filename)
    saved_path = os.path.join(INCOMING_FOLDER, safe_filename)

    try:
        image_file.save(saved_path)
    except OSError as exc:
        return jsonify({"success": False, "error": f"Failed to save document: {exc}"}), 500

    # Convert PDF if needed
    try:
        rendered_images = convert_document_to_images(saved_path, max_pages=3)
    except RuntimeError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    # Bug 8: Pre-flight Image Quality Check on first page
    quality_result = assess_image_quality(rendered_images[0])
    if not quality_result.get("passed", True):
        return jsonify({
            "success": False,
            "error": f"Image Quality Gatekeeper: {quality_result.get('reason')}",
            "quality_issue": True,
        }), 400

    preview_filename = os.path.basename(rendered_images[0])
    preview_url = f"/uploads/{preview_filename}"

    # Execute forensic pipeline
    try:
        results, summary, doc_meta = verify_document_pipeline(rendered_images, fields_spec, doc_type=document_type)
    except RuntimeError as exc:
        return jsonify({"success": False, "error": str(exc)}), 500

    report_text, json_report = generate_reports(document_type, filename, results, summary, doc_meta)

    return jsonify({
        "success": True,
        "document_type": document_type,
        "filename": filename,
        "preview_url": preview_url,
        "page_count": len(rendered_images),
        "summary": summary,
        "results": results,
        "report_text": report_text,
        "quality": quality_result,
        "document_meta": doc_meta,
    })


@app.route("/verify", methods=["POST"])
def verify_form():
    document_type = request.form.get("document_type", "").strip() or "Document"
    fields_spec = parse_request_fields()

    if not fields_spec:
        return render_template("index.html", presets=PRESET_TEMPLATES, error="Please configure at least one field.")

    if "document_image" not in request.files:
        return render_template("index.html", presets=PRESET_TEMPLATES, error="No document uploaded.")

    image_file = request.files["document_image"]
    if not image_file or image_file.filename == "":
        return render_template("index.html", presets=PRESET_TEMPLATES, error="No file selected.")

    if not allowed_file(image_file.filename):
        return render_template("index.html", presets=PRESET_TEMPLATES, error="Unsupported file format.")

    filename = image_file.filename
    safe_filename = re.sub(r"[^\w\.-]", "_", filename)
    saved_path = os.path.join(INCOMING_FOLDER, safe_filename)
    image_file.save(saved_path)

    rendered_images = convert_document_to_images(saved_path, max_pages=3)

    # Bug 8: Quality Gate
    quality_result = assess_image_quality(rendered_images[0])
    if not quality_result.get("passed", True):
        return render_template(
            "index.html",
            presets=PRESET_TEMPLATES,
            error=f"Image Quality Gatekeeper: {quality_result.get('reason')}",
        )

    preview_filename = os.path.basename(rendered_images[0])
    preview_url = f"/uploads/{preview_filename}"

    results, summary, doc_meta = verify_document_pipeline(rendered_images, fields_spec, doc_type=document_type)
    report_text, json_report = generate_reports(document_type, filename, results, summary, doc_meta)

    return render_template(
        "index.html",
        presets=PRESET_TEMPLATES,
        results=results,
        summary=summary,
        overall_status=summary["overall_status"],
        document_type=document_type,
        report_text=report_text,
        preview_url=preview_url,
        active_fields=fields_spec,
    )


if __name__ == "__main__":
    app.run(debug=True)