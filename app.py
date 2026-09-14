import os
import re
import json
import base64
import difflib
from datetime import datetime
from dateutil import parser as dateutil_parser
from dotenv import load_dotenv

load_dotenv()

from flask import Flask, request, render_template
from mistralai.client import Mistral

# --------------------------------------------------
# CONFIG
# --------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INCOMING_FOLDER = os.path.join(BASE_DIR, "incoming")
OUTPUT_FOLDER = os.path.join(BASE_DIR, "output")
REPORT_PATH = os.path.join(OUTPUT_FOLDER, "verification_report.txt")

ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "pdf"}

# Only these fields are extracted and verified.
FIELDS = ["Name of Firm", "Amount of Solvency", "Name of Bank", "Address of Bank", "Date of Solvency Certificate"]

# A field counts as a match if enough of its words overlap with the
# extracted value (handles different word order / extra words), instead
# of requiring an exact match.
TOKEN_MATCH_THRESHOLD = 0.6

# Stricter fallback: only used when token overlap isn't conclusive, to catch
# close spelling variants without letting short, similar-looking-but-different
# strings (e.g. "Bank of Baroda" vs "Bank of India") match.
SIMILARITY_THRESHOLD = 0.8

MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY")
MISTRAL_MODEL = "pixtral-12b-2409"
MISTRAL_TIMEOUT_SECONDS = 30

os.makedirs(INCOMING_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

app = Flask(__name__)


# --------------------------------------------------
# HELPERS
# --------------------------------------------------

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


def convert_pdf_to_image(pdf_path):
    """Render the first page of a PDF to a PNG and return the new file path."""
    import fitz  # PyMuPDF

    try:
        pdf_document = fitz.open(pdf_path)
        if pdf_document.page_count == 0:
            raise RuntimeError("The uploaded PDF has no pages.")
        page = pdf_document[0]
        pixmap = page.get_pixmap(dpi=200)
        image_path = pdf_path.rsplit(".", 1)[0] + "_converted.png"
        pixmap.save(image_path)
        pdf_document.close()
        return image_path
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Could not read the uploaded PDF (it may be corrupted): {exc}")


def normalize(value):
    """Lowercase, collapse whitespace, strip leading/trailing spaces."""
    if value is None:
        return ""
    value = str(value).strip().lower()
    value = re.sub(r"\s+", " ", value)
    return value


STOPWORDS = {"of", "the", "and", "mr", "mrs", "ms", "m/s", "shri", "smt", "dr"}


DATE_FIELD = "Date of Solvency Certificate"
AMOUNT_FIELD = "Amount of Solvency"


def parse_date(value):
    """
    Try to parse a date string into a datetime.date object.
    Returns None if parsing fails.
    Handles formats like: 15/03/2025, 15-03-2025, 15 March 2025,
    March 15, 2025, 2025-03-15, etc.
    """
    if not value or normalize(value) in ("not found", "not provided", ""):
        return None
    try:
        return dateutil_parser.parse(value, dayfirst=True).date()
    except (ValueError, OverflowError):
        return None


def date_match(user_value, extracted_value):
    """
    Return True only if both values parse to the exact same calendar date.
    """
    user_date = parse_date(user_value)
    extracted_date = parse_date(extracted_value)
    if user_date is None or extracted_date is None:
        return False
    return user_date == extracted_date


def parse_amount(value):
    """
    Extract a numeric amount from a string, handling formats like:
      - 30000000
      - 3,00,00,000
      - Rs. 3,00,00,000/-
      - Rs. 3,00,00,000/- (Rupees Three Crores Only)
      - INR 30,000,000
    Returns an int/float or None if no number can be parsed.
    """
    if not value or normalize(value) in ("not found", "not provided", ""):
        return None
    # Remove currency symbols, slashes, letters (except digits and separators)
    cleaned = re.sub(r"[^\d,\.]", " ", value)
    # Find all number-like tokens (with optional commas/decimals)
    tokens = re.findall(r"[\d,]+(?:\.\d+)?", cleaned)
    if not tokens:
        return None
    # Pick the token with the most digits — that's the amount
    best = max(tokens, key=lambda t: len(re.sub(r"[^\d]", "", t)))
    # Remove commas (Indian or international formatting)
    best = best.replace(",", "")
    try:
        return float(best)
    except ValueError:
        return None


def amount_match(user_value, extracted_value):
    """
    Return True if both values represent the same numeric amount,
    regardless of formatting (commas, currency symbols, words, etc.).
    """
    user_amount = parse_amount(user_value)
    extracted_amount = parse_amount(extracted_value)
    if user_amount is None or extracted_amount is None:
        return False
    return user_amount == extracted_amount


def fuzzy_match(user_value, extracted_value):
    """
    Return True if two values are close enough to count as a match.
    Handles cases like "Vora Jayveer Prakashbhai" vs "Jayveer Vora" (word
    order differs and one has extra words), not just identical strings.
    Generic filler words (bank, of, the, etc.) are ignored so distinct
    names like "Bank of Baroda" vs "Bank of India" don't falsely match.
    """
    a = normalize(user_value)
    b = normalize(extracted_value)

    if not a or not b:
        return False
    if a == b:
        return True

    tokens_a = {t for t in a.split() if t not in STOPWORDS}
    tokens_b = {t for t in b.split() if t not in STOPWORDS}
    if not tokens_a:
        tokens_a = set(a.split())
    if not tokens_b:
        tokens_b = set(b.split())

    if tokens_a and tokens_b:
        overlap = len(tokens_a & tokens_b)
        smaller_set_size = min(len(tokens_a), len(tokens_b))
        token_overlap_ratio = overlap / smaller_set_size
        if token_overlap_ratio >= TOKEN_MATCH_THRESHOLD:
            return True

    similarity_ratio = difflib.SequenceMatcher(None, a, b).ratio()
    return similarity_ratio >= SIMILARITY_THRESHOLD


def build_prompt():
    field_list = "\n".join(f"- {field}" for field in FIELDS)
    return (
        "Analyze the uploaded document image.\n"
        "Locate only the following fields:\n"
        f"{field_list}\n\n"
        "Return only values that are clearly visible in the image. "
        "Do not guess. Do not hallucinate.\n"
        "If a field is missing or not clearly visible, return the exact string \"NOT FOUND\" for it.\n\n"
        "Respond with structured JSON only, no explanation, no markdown formatting, "
        "using exactly this shape:\n"
        "{\n"
        + ",\n".join(f'  "{field}": "..."' for field in FIELDS)
        + "\n}"
    )


def extract_json_from_text(text):
    """Extract a JSON object from the model response, tolerating stray text/markdown fences."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("No JSON object found in Mistral AI response.")
    return json.loads(match.group(0))


def extract_fields_with_mistral(image_path, filename):
    """Send the image to Mistral AI and return a dict of extracted field values."""
    if not MISTRAL_API_KEY:
        raise RuntimeError("MISTRAL_API_KEY environment variable is not set.")

    try:
        with open(image_path, "rb") as image_file:
            encoded_image = base64.b64encode(image_file.read()).decode("utf-8")
    except OSError as exc:
        raise RuntimeError(f"Could not read uploaded image: {exc}")

    mime_type = get_mime_type(filename)
    data_uri = f"data:{mime_type};base64,{encoded_image}"

    client = Mistral(api_key=MISTRAL_API_KEY)

    try:
        response = client.chat.complete(
            model=MISTRAL_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": build_prompt()},
                        {"type": "image_url", "image_url": data_uri},
                    ],
                }
            ],
            temperature=0,
            timeout_ms=MISTRAL_TIMEOUT_SECONDS * 1000,
        )
    except Exception as exc:
        raise RuntimeError(f"Mistral AI request failed or timed out: {exc}")

    try:
        raw_text = response.choices[0].message.content
        if isinstance(raw_text, list):
            raw_text = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in raw_text
            )
    except (AttributeError, IndexError) as exc:
        raise RuntimeError(f"Unexpected Mistral AI response format: {exc}")

    try:
        parsed = extract_json_from_text(raw_text)
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not parse Mistral AI response as JSON: {exc}")

    extracted = {}
    for field in FIELDS:
        value = parsed.get(field, "NOT FOUND")
        if value is None or str(value).strip() == "":
            value = "NOT FOUND"
        extracted[field] = str(value).strip()

    return extracted


def compare_fields(user_data, extracted_data):
    """Compare each field and return a list of result dicts plus overall status."""
    results = []
    overall_status = "PASS"

    for field in FIELDS:
        user_value = user_data.get(field, "").strip()
        extracted_value = extracted_data.get(field, "NOT FOUND")

        if extracted_value == "NOT FOUND":
            status = "FAIL"
            overall_status = "FAIL"
        elif field == DATE_FIELD:
            # Dates must match exactly (same calendar day), regardless of format
            if date_match(user_value, extracted_value):
                status = "PASS"
            else:
                status = "FAIL"
                overall_status = "FAIL"
        elif field == AMOUNT_FIELD:
            # Amounts must match numerically (ignores Rs., commas, words like "Rupees Three Crores Only")
            if amount_match(user_value, extracted_value):
                status = "PASS"
            else:
                status = "FAIL"
                overall_status = "FAIL"
        elif not fuzzy_match(user_value, extracted_value):
            status = "FAIL"
            overall_status = "FAIL"
        else:
            status = "PASS"

        results.append(
            {
                "field": field,
                "user_value": user_value if user_value else "NOT PROVIDED",
                "extracted_value": extracted_value,
                "status": status,
            }
        )

    return results, overall_status


def generate_report(document_type, results, overall_status):
    lines = []
    lines.append("=" * 38)
    lines.append("DOCUMENT VERIFICATION REPORT")
    lines.append("=" * 38)
    lines.append("")
    lines.append("Document Type:")
    lines.append(document_type)
    lines.append("")
    lines.append("-" * 38)

    for result in results:
        lines.append("")
        lines.append("Field:")
        lines.append(result["field"])
        lines.append("")
        lines.append("Entered:")
        lines.append(result["user_value"])
        lines.append("")
        lines.append("Extracted:")
        lines.append(result["extracted_value"])
        lines.append("")
        lines.append("Status:")
        lines.append(result["status"])
        lines.append("")
        lines.append("-" * 38)

    lines.append("")
    lines.append("Generated At:")
    lines.append(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    lines.append("")
    lines.append("=" * 38)
    lines.append("")
    lines.append("Overall Status:")
    lines.append(overall_status)
    lines.append("")
    lines.append("=" * 38)

    report_text = "\n".join(lines)

    with open(REPORT_PATH, "w", encoding="utf-8") as report_file:
        report_file.write(report_text)

    return report_text


# --------------------------------------------------
# ROUTES
# --------------------------------------------------

@app.route("/", methods=["GET"])
def index():
    return render_template("index.html", fields=FIELDS)


@app.route("/verify", methods=["POST"])
def verify():
    document_type = request.form.get("document_type", "").strip()
    if not document_type:
        return render_template("index.html", fields=FIELDS, error="Document type is required.")

    user_data = {}
    for field in FIELDS:
        user_data[field] = request.form.get(field, "").strip()

    if all(value == "" for value in user_data.values()):
        return render_template("index.html", fields=FIELDS, error="Please fill in at least one field.")

    if "document_image" not in request.files:
        return render_template("index.html", fields=FIELDS, error="No image uploaded.")

    image_file = request.files["document_image"]

    if image_file.filename == "":
        return render_template("index.html", fields=FIELDS, error="No image selected.")

    if not allowed_file(image_file.filename):
        return render_template(
            "index.html",
            fields=FIELDS,
            error="Unsupported file format. Allowed types: jpg, jpeg, png, webp, pdf.",
        )

    filename = image_file.filename
    saved_path = os.path.join(INCOMING_FOLDER, filename)

    try:
        image_file.save(saved_path)
    except OSError as exc:
        return render_template("index.html", fields=FIELDS, error=f"Could not save uploaded image: {exc}")

    if not os.path.exists(saved_path) or os.path.getsize(saved_path) == 0:
        return render_template("index.html", fields=FIELDS, error="Uploaded file appears to be corrupted or empty.")

    extraction_path = saved_path
    extraction_filename = filename

    if filename.rsplit(".", 1)[1].lower() == "pdf":
        try:
            extraction_path = convert_pdf_to_image(saved_path)
            extraction_filename = os.path.basename(extraction_path)
        except RuntimeError as exc:
            return render_template("index.html", fields=FIELDS, error=str(exc))

    try:
        extracted_data = extract_fields_with_mistral(extraction_path, extraction_filename)
    except RuntimeError as exc:
        return render_template("index.html", fields=FIELDS, error=str(exc))

    results, overall_status = compare_fields(user_data, extracted_data)
    report_text = generate_report(document_type, results, overall_status)

    return render_template(
        "index.html",
        fields=FIELDS,
        results=results,
        overall_status=overall_status,
        document_type=document_type,
        report_text=report_text,
    )


if __name__ == "__main__":
    app.run(debug=True)