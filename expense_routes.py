import os
import re
import io
import csv
import json
import base64
from datetime import datetime, date, timedelta
from dateutil.relativedelta import relativedelta
from flask import Blueprint, request, jsonify, send_file, current_app
from werkzeug.utils import secure_filename
from mistralai.client import Mistral
from xml.sax.saxutils import escape as xml_escape

from models import (
    db,
    Bill,
    LineItem,
    Vendor,
    DocumentScan,
    ScanFieldResult,
    AuditLog,
    BusinessProfile,
    ExpenseCategory,
    get_business_profile,
    get_active_categories,
    add_custom_category,
    delete_custom_category,
    cleanup_and_merge_duplicate_categories,
    canonicalize_category,
    find_duplicate_bill,
    get_or_create_vendor,
    log_audit,
    DEFAULT_CATEGORIES,
    VALID_CATEGORIES,
)
from document_utils import (
    allowed_file,
    get_mime_type,
    convert_document_to_images,
    extract_json_from_text,
    assess_image_quality,
)

expense_bp = Blueprint("expense_bp", __name__, url_prefix="/api/expense")

MISTRAL_MODEL = "pixtral-12b-2409"
MISTRAL_TIMEOUT_SECONDS = 45


def get_mistral_api_key():
    return os.environ.get("MISTRAL_API_KEY", "").strip()


STANDARD_GST_SLABS = [0.0, 5.0, 12.0, 18.0, 28.0]
HALF_RATE_TO_FULL = {2.5: 5.0, 6.0: 12.0, 9.0: 18.0, 14.0: 28.0}


def snap_to_standard_gst_rate(rate, gst_amount=None, subtotal=None, cgst_rate=None, sgst_rate=None, igst_rate=None):
    """
    1. If CGST% and SGST% are present, combined rate = CGST% + SGST%.
    2. If IGST% is present, rate = IGST%.
    3. Never return a half-rate: if rate is 2.5, 6, 9, 14, convert to full combined rate (5, 12, 18, 28).
    4. Sanity-check that gst_amount / subtotal is close to the rate, and snap to a standard slab (0, 5, 12, 18, 28).
    """
    c_r, s_r, i_r = None, None, None
    try:
        if cgst_rate is not None and str(cgst_rate).strip():
            c_r = float(cgst_rate)
    except (ValueError, TypeError):
        pass
    try:
        if sgst_rate is not None and str(sgst_rate).strip():
            s_r = float(sgst_rate)
    except (ValueError, TypeError):
        pass
    try:
        if igst_rate is not None and str(igst_rate).strip():
            i_r = float(igst_rate)
    except (ValueError, TypeError):
        pass

    if c_r is not None and s_r is not None and (c_r > 0 or s_r > 0):
        r = c_r + s_r
    elif i_r is not None and i_r > 0:
        r = i_r
    else:
        try:
            r = float(rate) if rate is not None else 0.0
        except (ValueError, TypeError):
            r = 0.0

    # Never return a half-rate
    for half, full in HALF_RATE_TO_FULL.items():
        if abs(r - half) < 0.25:
            r = full
            break

    # Sanity check with gst_amount / subtotal
    sub = float(subtotal) if subtotal is not None else 0.0
    gst = float(gst_amount) if gst_amount is not None else 0.0

    if sub > 0 and gst >= 0:
        effective = (gst / sub) * 100.0
        closest_slab_eff = min(STANDARD_GST_SLABS, key=lambda s: abs(s - effective))
        if abs(effective - closest_slab_eff) <= 1.5:
            return closest_slab_eff
        closest_slab_r = min(STANDARD_GST_SLABS, key=lambda s: abs(s - r))
        if abs(r - closest_slab_r) <= 1.2:
            return closest_slab_r

    closest_slab = min(STANDARD_GST_SLABS, key=lambda s: abs(s - r))
    if abs(r - closest_slab) <= 1.0:
        return closest_slab

    return round(r, 1)


FOOD_KEYWORDS = [
    # Paneer, cheese & specialty gravies
    "paneer", "paneer butter masala", "cheese butter masala", "cheese", "kaju butter masala",
    "kaju curry", "shahi paneer", "palak paneer", "mutter paneer", "matar paneer", "paneer tikka",
    "paneer bhurji", "kadai paneer", "kadhai paneer", "handi paneer", "paneer angara",
    "paneer pasanda", "paneer lababdar", "paneer toofani", "paneer handi", "paneer kolhapuri",
    "paneer makhani", "paneer masala",
    # Sabji, curries & vegetables
    "sabji", "sabzi", "bhaji", "curry", "gravy", "sev tameta", "sev tomato", "sev bhaji",
    "veg kolhapuri", "veg jaipuri", "veg handi", "veg makhanwala", "veg kadai", "mix veg",
    "mixed veg", "chole", "chana masala", "rajma", "kofta", "malai kofta", "dum aloo",
    "aloo gobi", "aloo mutter", "bhindi", "baingan", "baingan bharta", "undhiyu",
    "dal fry", "dal tadka", "dal makhani", "dal", "sambar", "rasam",
    # Breads & Rice
    "roti", "chapati", "naan", "butter naan", "garlic naan", "tandoori roti", "butter roti",
    "paratha", "aloo paratha", "paneer paratha", "kulcha", "puri", "bhature",
    "rice", "jeera rice", "biryani", "veg biryani", "pulao", "veg pulao", "khichdi",
    "thali", "gujarati thali", "punjabi thali", "south indian thali",
    # Snacks & Street food
    "samosa", "kachori", "chaat", "panipuri", "pani puri", "bhel", "dosa", "masala dosa",
    "mysore dosa", "idli", "vada", "medu vada", "uttapam", "pav bhaji", "vada pav",
    "misal", "misal pav", "poha", "upma", "sandwich", "burger", "pizza", "pasta",
    "noodles", "hakka noodles", "fried rice", "manchurian", "veg manchurian", "momos",
    "spring roll", "bhurji", "tikka", "tandoori", "starter", "main course",
    # Sweets & Desserts
    "dessert", "sweet", "sweets", "mithai", "gulab jamun", "rasgulla", "kaju katli",
    "jalebi", "ice cream", "icecream", "kulfi", "pastry", "cake", "bakery", "halwa",
    # Beverages & Drinks
    "tea", "chai", "coffee", "lassi", "chaas", "buttermilk", "juice", "shake", "smoothie",
    "cold drink", "soft drink", "pepsi", "coca cola", "coke", "sprite", "thums up",
    "soda", "water bottle", "mineral water", "beverage",
    # Dining places & platforms
    "restaurant", "restro", "cafe", "cafeteria", "hotel dining", "hotel restaurant", "dhaba", "bhojanalay", "dining",
    "food court", "canteen", "mess", "tiffin", "breakfast", "lunch", "dinner",
    "meal", "food", "kitchen", "caterer", "swiggy", "zomato", "papad", "chutney"
]

PINS_KEYWORDS = [
    "pin", "pins", "u-clip", "paper clip", "paperclip", "stapler", "staple",
    "binder clip", "push pin", "pushpin", "board pin", "safety pin"
]

STATIONERY_KEYWORDS = [
    "pen", "pencil", "marker", "highlighter", "eraser", "sharpener", "ruler",
    "folder", "file folder", "notepad", "notebook", "a4 paper", "copier paper",
    "tape", "glue", "fevistick", "envelope", "stamp pad", "stationery", "stationers",
    "office supply", "office supplies", "sticky note", "register", "carbon paper"
]

RENT_KEYWORDS = [
    "rent", "rental", "lease", "office rent", "shop rent", "godown rent",
    "warehouse rent", "tenancy", "maintenance charges", "building maintenance"
]

TRAVEL_KEYWORDS = [
    "flight", "airline", "indigo", "air india", "spicejet", "vistara", "train",
    "railway", "irctc", "bus", "uber", "ola", "cab", "taxi", "auto fare",
    "petrol", "diesel", "fuel", "toll", "fastag", "travel", "trips", "lodging",
    "hotel stay", "conveyance", "boarding pass", "mileage"
]

FEES_KEYWORDS = [
    "fee", "fees", "consulting", "legal fee", "professional charges", "audit fee",
    "accounting charges", "certification fee", "registration fee", "filing fee",
    "license fee", "retainer", "membership fee", "court fee", "bank charges"
]

CUSTOMER_EXPENSE_KEYWORDS = [
    "client entertainment", "customer gift", "client meeting", "client dinner",
    "customer account", "client hospitality", "corporate gift", "customer promo",
    "business gift", "client lunch", "pr expense"
]

STOCK_EXPENSE_KEYWORDS = [
    "steel", "cement", "iron", "brick", "sand", "aggregate", "raw material",
    "stock", "inventory", "wholesale", "goods for resale", "pipe", "fittings",
    "plywood", "hardware", "machinery parts", "materials", "bar", "tmt bar"
]

UTILITIES_KEYWORDS = [
    "electricity", "power", "tata power", "torrent power", "adani electricity",
    "bescom", "water bill", "broadband", "wifi", "internet", "telephone",
    "mobile recharge", "airtel", "jio", "vodafone", "utility bill", "gas bill"
]


def infer_expense_category(raw_cat, line_items, vendor, notes, active_categories=None):
    """
    Intelligently infer category based on multi-signal domain analysis:
    - Office/shop rent & premises leases -> Rent
    - Electricity, water, power, broadband utilities -> Utilities
    - Food/sabji items, dining, restaurants, cafes -> Food
    - Travel, conveyance, cab/taxi, flights, fuel, hotel stays, travel utilities -> Travel Expenses
    - Office pins, paperclips, staplers -> Pins
    - Stationery, printing, office paper, notebooks -> Stationery
    - Legal, audit, professional & registration fees -> Fees
    - Client gifts, corporate hospitality & entertainment -> Customer Account Expense
    - Stock, raw materials, construction materials -> Stock Expense
    - Smart canonical normalization of raw_cat
    Never hallucinates or creates duplicate categories.
    """
    if active_categories is None:
        active_categories = get_active_categories()

    items_text = " ".join(str(it.get("name") or "") for it in (line_items or []) if isinstance(it, dict))
    body_corpus = f"{vendor or ''} {notes or ''} {items_text}".lower()
    full_corpus = f"{body_corpus} {raw_cat or ''}".lower()

    def find_active_cat(target_name):
        canonical_target = canonicalize_category(target_name, active_categories)
        for ac in active_categories:
            if ac.strip().lower() == canonical_target.strip().lower():
                return ac
        return canonical_target

    # 1. Rent Check (if invoice content indicates office/shop rent or lease)
    for kw in RENT_KEYWORDS:
        if re.search(rf"\b{re.escape(kw)}\b", body_corpus):
            return find_active_cat("Rent")

    # 2. Utilities Check (electricity, water, broadband, power)
    for kw in UTILITIES_KEYWORDS:
        if re.search(rf"\b{re.escape(kw)}\b", body_corpus):
            return find_active_cat("Utilities")

    # 3. Food Check (Dishes, sabjis, paneer butter masala, cheese butter masala, restaurants)
    for kw in FOOD_KEYWORDS:
        if kw in body_corpus or re.search(rf"\b{re.escape(kw)}\b", body_corpus):
            return find_active_cat("Food")

    # 4. Travel Check (cabs, flights, train, petrol/diesel, hotel stays, travel utilities, travel expenses)
    for kw in TRAVEL_KEYWORDS:
        if re.search(rf"\b{re.escape(kw)}\b", full_corpus):
            return find_active_cat("Travel Expenses")

    # 5. Pins Check
    for kw in PINS_KEYWORDS:
        if re.search(rf"\b{re.escape(kw)}\b", body_corpus):
            return find_active_cat("Pins")

    # 6. Stationery Check
    for kw in STATIONERY_KEYWORDS:
        if re.search(rf"\b{re.escape(kw)}\b", body_corpus):
            return find_active_cat("Stationery")

    # 7. Fees Check
    for kw in FEES_KEYWORDS:
        if re.search(rf"\b{re.escape(kw)}\b", body_corpus):
            return find_active_cat("Fees")

    # 8. Customer Account Expense Check
    for kw in CUSTOMER_EXPENSE_KEYWORDS:
        if re.search(rf"\b{re.escape(kw)}\b", body_corpus):
            return find_active_cat("Customer Account Expense")

    # 9. Stock / Inventory Expense Check
    for kw in STOCK_EXPENSE_KEYWORDS:
        if re.search(rf"\b{re.escape(kw)}\b", body_corpus):
            return find_active_cat("Stock Expense")

    # 10. Canonicalize the raw category string provided by AI or bill (maps 'travel', 'travel expense', etc.)
    if raw_cat:
        canonical = canonicalize_category(raw_cat, active_categories)
        if canonical and canonical.lower() != "other":
            return find_active_cat(canonical)

    return find_active_cat("Other")


def build_invoice_extraction_prompt(categories=None):
    if not categories:
        categories = get_active_categories()
    categories_list_str = ", ".join(categories)
    return f"""You are a forensic financial document and invoice OCR parser for a single enterprise/business.
Analyze the attached invoice/bill image(s) with extreme precision.
Extract ONLY authentic, clearly visible details.
DO NOT guess or invent numbers. If any value is not visible or illegible, set its value to null.

Output ONLY valid JSON adhering strictly to the schema below without markdown code blocks, backticks, or commentary:
{{
  "vendor": "<seller/store/restaurant/vendor name or null>",
  "invoice_no": "<string or null>",
  "date": "<YYYY-MM-DD string or null>",
  "category": "<one of: {categories_list_str}>",
  "line_items": [
    {{
      "name": "<string>",
      "qty": <number or 1>,
      "price": <unit price number or 0.0>,
      "total": <line amount number or 0.0>
    }}
  ],
  "subtotal": <number or null>,
  "cgst_rate": <number or null>,
  "sgst_rate": <number or null>,
  "igst_rate": <number or null>,
  "gst_rate": <combined total GST rate percentage or null>,
  "cgst_amount": <number or null>,
  "sgst_amount": <number or null>,
  "igst_amount": <number or null>,
  "gst_amount": <number or null>,
  "total": <number or null>,
  "confidence": {{
    "vendor": "<HIGH, MEDIUM, or LOW>",
    "invoice_no": "<HIGH, MEDIUM, or LOW>",
    "date": "<HIGH, MEDIUM, or LOW>",
    "total": "<HIGH, MEDIUM, or LOW>",
    "gst_amount": "<HIGH, MEDIUM, or LOW>"
  }},
  "notes": "<brief context about the vendor or bill items or null>"
}}

Strict Rules:
1. For date: Convert to YYYY-MM-DD (e.g. 2026-02-14). If only month/year or ambiguous, use null.
2. For numbers: Return raw floats/integers (e.g. 2450.50), NOT currency symbols like ₹, $, Rs.
3. Category: Choose the single best match among the categories: {categories_list_str}.
   CRITICAL CATEGORY RULES:
   - You MUST pick exactly one category from the allowed list: {categories_list_str}.
   - NEVER create, guess, or invent a new category name. Do NOT output variants, sub-categories, or adjectives.
   - For ANY travel, conveyance, taxi/cab (Uber/Ola), flights, fuel, train tickets, hotel stay, or travel utility expenses, ALWAYS use 'Travel Expenses'. NEVER output 'Travel', 'Travel Expense', or 'Travel Utilities'.
   - For office rent, shop lease, premises rent, warehouse lease, ALWAYS use 'Rent'.
   - For electricity, water, power, broadband, internet, telephone recharge, ALWAYS use 'Utilities'.
   - For food, dining, restaurant, cafe, snacks, beverages, or Indian dishes (paneer, sabji, roti, naan, dal, thali, biryani, meals), ALWAYS use 'Food'.
   - For office pins, paperclips, stapler pins, binder clips, ALWAYS use 'Pins'.
   - For paper, pens, notebooks, office stationery, printing, ALWAYS use 'Stationery'.
   - For legal, audit, consultation, license fees, accounting charges, ALWAYS use 'Fees'.
   - For client dinner, corporate gifts, client meeting expenses, ALWAYS use 'Customer Account Expense'.
   - For raw materials, cement, steel, bricks, inventory goods for resale, ALWAYS use 'Stock Expense'.
   - If none of the above fit, use 'Other'.
4. GST Rates and Taxes:
   - Extract CGST, SGST, and IGST amounts separately if visible.
   - For GST rate: Always report the COMBINED rate (CGST% + SGST% or IGST%), NEVER a half-rate. If CGST is 9% and SGST is 9%, the combined rate is 18%. Standard GST slabs in India are 0%, 5%, 12%, 18%, 28%.
5. Line items:
   - Extract each item with name, qty, unit price, and line amount total.
   - If unit price is missing or 0 but line amount is present, compute unit price = line amount / qty. Never leave unit price as 0 when a line amount exists.
   - If line items are not individually itemized, create one entry representing the main invoice description with the total.
6. Use null for anything not visible and never guess."""


def extract_single_invoice_data(image_paths, filename):
    api_key = get_mistral_api_key()
    if not api_key:
        raise RuntimeError("MISTRAL_API_KEY is not configured in .env. Please set it to enable extraction.")

    active_categories = get_active_categories()
    prompt_text = build_invoice_extraction_prompt(active_categories)
    content = [{"type": "text", "text": prompt_text}]

    for img_path in image_paths:
        with open(img_path, "rb") as f:
            encoded = base64.b64encode(f.read()).decode("utf-8")
        mime = get_mime_type(img_path)
        content.append({"type": "image_url", "image_url": f"data:{mime};base64,{encoded}"})

    client = Mistral(api_key=api_key)
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

    # Sanitize and normalize parsed fields
    vendor = parsed.get("vendor") or None
    if vendor:
        vendor = str(vendor).strip()

    invoice_no = parsed.get("invoice_no") or None
    if invoice_no:
        invoice_no = str(invoice_no).strip()

    inv_date = parsed.get("date") or None
    if inv_date:
        inv_date = str(inv_date).strip()
        # Verify format
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", inv_date):
            inv_date = None

    # Line items: if unit price is missing or 0 but line amount is present, set price = amount / qty
    raw_items = parsed.get("line_items") or []
    line_items = []
    items_sum = 0.0

    if isinstance(raw_items, list):
        for idx, it in enumerate(raw_items):
            if not isinstance(it, dict):
                continue
            name = str(it.get("name") or f"Item {idx + 1}").strip()
            try:
                qty = float(it.get("qty", 1.0) or 1.0)
            except (ValueError, TypeError):
                qty = 1.0
            if qty <= 0:
                qty = 1.0

            try:
                price = float(it.get("price", 0.0) or 0.0)
            except (ValueError, TypeError):
                price = 0.0

            try:
                tot = float(it.get("total", 0.0) or 0.0)
            except (ValueError, TypeError):
                tot = 0.0

            # Unit price check: if missing or 0 but line amount is present, price = amount / qty
            if (price == 0.0 or price is None) and tot > 0:
                price = round(tot / qty, 2)
            elif tot == 0.0 and price > 0:
                tot = round(qty * price, 2)

            price = round(price, 2)
            tot = round(tot, 2)
            items_sum += tot
            line_items.append({
                "name": name,
                "qty": qty,
                "price": price,
                "total": tot,
            })

    # Intelligently infer category (guarantees paneer butter masala, sabji, etc. -> Food)
    category = infer_expense_category(
        parsed.get("category"),
        line_items,
        vendor,
        parsed.get("notes"),
        active_categories,
    )

    # Numeric totals
    def to_float(val, fallback=None):
        if val is None:
            return fallback
        try:
            return round(float(val), 2)
        except (ValueError, TypeError):
            return fallback

    subtotal = to_float(parsed.get("subtotal"), round(items_sum, 2) if items_sum > 0 else 0.0)
    cgst_amount = to_float(parsed.get("cgst_amount"), 0.0) or 0.0
    sgst_amount = to_float(parsed.get("sgst_amount"), 0.0) or 0.0
    igst_amount = to_float(parsed.get("igst_amount"), 0.0) or 0.0

    # Compute gst_amount as sum of CGST + SGST + IGST if separate components exist
    if cgst_amount > 0 or sgst_amount > 0 or igst_amount > 0:
        gst_amount = round(cgst_amount + sgst_amount + igst_amount, 2)
    else:
        gst_amount = to_float(parsed.get("gst_amount"), 0.0) or 0.0

    total = to_float(parsed.get("total"), None)
    if total is None:
        if subtotal is not None:
            total = round(subtotal + (gst_amount or 0.0), 2)
        else:
            total = 0.0

    # Sanity-check and snap GST rate to standard slab (0, 5, 12, 18, 28)
    cgst_r = to_float(parsed.get("cgst_rate"), None)
    sgst_r = to_float(parsed.get("sgst_rate"), None)
    igst_r = to_float(parsed.get("igst_rate"), None)
    raw_rate = to_float(parsed.get("gst_rate"), None)

    gst_rate = snap_to_standard_gst_rate(
        raw_rate,
        gst_amount=gst_amount,
        subtotal=subtotal,
        cgst_rate=cgst_r,
        sgst_rate=sgst_r,
        igst_rate=igst_r,
    )

    # Validation check: subtotal + gst_amount vs total with Rs 0.05 threshold
    expected_total = round((subtotal or 0.0) + (gst_amount or 0.0), 2)
    diff = round(abs(expected_total - (total or 0.0)), 2)

    if diff > 0.05:
        validation_status = "Needs review"
        suggested_gst = round(max(0.0, (total or 0.0) - (subtotal or 0.0)), 2)
        validation_notes = f"Differs by ₹{diff:.2f}; suggested GST is ₹{suggested_gst:.2f}"
        math_valid = False
    else:
        validation_status = "OK"
        suggested_gst = gst_amount
        validation_notes = "Verified: Subtotal + GST = Total"
        math_valid = True

    # Confidence and missing flags
    confidence_map = parsed.get("confidence") or {}
    missing_fields = []
    low_confidence_fields = []

    for fld in ["vendor", "invoice_no", "date", "total", "gst_amount"]:
        val = vendor if fld == "vendor" else invoice_no if fld == "invoice_no" else inv_date if fld == "date" else total if fld == "total" else gst_amount
        conf = str(confidence_map.get(fld, "HIGH")).upper()
        if val is None or val == "" or (fld == "total" and val == 0.0):
            missing_fields.append(fld)
        elif conf in ("LOW", "MEDIUM"):
            low_confidence_fields.append(fld)

    # Check duplicate in database
    duplicate_match = find_duplicate_bill(vendor, invoice_no, total)
    is_duplicate = duplicate_match is not None
    duplicate_warning = None
    if is_duplicate:
        duplicate_warning = f"Duplicate bill detected: Bill #{duplicate_match.id} with Vendor '{duplicate_match.vendor}', Inv #{duplicate_match.invoice_no}, Total ₹{duplicate_match.total:,.2f} already exists."

    return {
        "filename": filename,
        "vendor": vendor or "",
        "invoice_no": invoice_no or "",
        "date": inv_date or date.today().strftime("%Y-%m-%d"),
        "category": category,
        "subtotal": subtotal or 0.0,
        "cgst_amount": cgst_amount,
        "sgst_amount": sgst_amount,
        "igst_amount": igst_amount,
        "gst_amount": gst_amount or 0.0,
        "gst_rate": gst_rate or 0.0,
        "total": total or 0.0,
        "line_items": line_items,
        "math_valid": math_valid,
        "validation_status": validation_status,
        "validation_notes": validation_notes,
        "suggested_gst": suggested_gst,
        "diff_amount": diff,
        "expected_total": expected_total,
        "missing_fields": missing_fields,
        "low_confidence_fields": low_confidence_fields,
        "is_duplicate": is_duplicate,
        "duplicate_warning": duplicate_warning,
        "notes": parsed.get("notes") or "",
        "raw_json": json.dumps(parsed),
    }


# --------------------------------------------------
# API: BUSINESS PROFILE & CATEGORY MANAGEMENT (SINGLE VENDOR)
# --------------------------------------------------

@expense_bp.route("/profile", methods=["GET"])
def api_get_profile():
    prof = get_business_profile()
    cats = get_active_categories()
    return jsonify({
        "success": True,
        "profile": prof.to_dict(),
        "categories": cats,
    })


@expense_bp.route("/profile", methods=["POST"])
def api_update_profile():
    data = request.get_json(silent=True) or {}
    prof = get_business_profile()

    if "business_name" in data and str(data["business_name"]).strip():
        prof.business_name = str(data["business_name"]).strip()
    if "owner_name" in data:
        prof.owner_name = str(data["owner_name"]).strip()
    if "gstin" in data:
        prof.gstin = str(data["gstin"]).strip().upper()
    if "phone" in data:
        prof.phone = str(data["phone"]).strip()
    if "email" in data:
        prof.email = str(data["email"]).strip()
    if "address" in data:
        prof.address = str(data["address"]).strip()
    if "notes" in data:
        prof.notes = str(data["notes"]).strip()
    prof.updated_at = datetime.utcnow()

    db.session.commit()
    log_audit(
        action="UPDATE",
        entity_type="BusinessProfile",
        entity_id=prof.id,
        summary=f"Updated business details for '{prof.business_name}'",
        payload=data,
        ip_address=request.remote_addr,
    )

    return jsonify({
        "success": True,
        "message": "Business profile updated successfully.",
        "profile": prof.to_dict(),
        "categories": get_active_categories(),
    })


@expense_bp.route("/categories", methods=["GET"])
def api_get_categories():
    return jsonify({
        "success": True,
        "categories": get_active_categories(),
    })


@expense_bp.route("/categories", methods=["POST"])
def api_add_category():
    data = request.get_json(silent=True) or {}
    name = data.get("name")
    color = data.get("color")
    if not name or not str(name).strip():
        return jsonify({"success": False, "error": "Category name is required."}), 400

    clean_name = str(name).strip()
    active_cats = get_active_categories()
    canonical = canonicalize_category(clean_name, active_cats)

    # Check if the requested name is an alias of an existing canonical category (excluding fallback 'Other')
    if canonical != "Other" and canonical in active_cats and canonical.lower() != clean_name.lower():
        return jsonify({
            "success": True,
            "message": f"'{clean_name}' is automatically mapped to canonical category '{canonical}'.",
            "category": {"name": canonical, "color": color or "#0D4C3C", "is_default": True},
            "categories": get_active_categories(),
        })

    ok, res = add_custom_category(name, color)
    if not ok:
        return jsonify({"success": False, "error": res}), 400

    log_audit(
        action="CREATE",
        entity_type="ExpenseCategory",
        entity_id=None,
        summary=f"Added category '{name}'",
        ip_address=request.remote_addr,
    )
    return jsonify({
        "success": True,
        "message": f"Category '{name}' added successfully.",
        "category": res,
        "categories": get_active_categories(),
    })


@expense_bp.route("/categories/<category_name>", methods=["DELETE"])
def api_delete_category(category_name):
    ok, msg = delete_custom_category(category_name)
    if not ok:
        return jsonify({"success": False, "error": msg}), 400

    log_audit(
        action="DELETE",
        entity_type="ExpenseCategory",
        entity_id=None,
        summary=f"Deleted category '{category_name}'",
        ip_address=request.remote_addr,
    )
    return jsonify({
        "success": True,
        "message": msg,
        "categories": get_active_categories(),
    })


# --------------------------------------------------
# API: EXTRACT MULTIPLE BILLS
# --------------------------------------------------

@expense_bp.route("/extract", methods=["POST"])
def api_extract_bills():
    """
    Upload one or many bills/invoices (PDF, JPG, PNG).
    Extracts with Mistral vision setup at temperature 0.
    """
    files = request.files.getlist("bills") or request.files.getlist("bills[]") or request.files.getlist("file")
    if not files or all(f.filename == "" for f in files):
        return jsonify({"success": False, "error": "No bill files were uploaded. Please select at least one document."}), 400

    incoming_dir = os.path.join(current_app.root_path, "incoming")
    os.makedirs(incoming_dir, exist_ok=True)

    extracted_results = []
    errors = []

    for file_obj in files:
        if not file_obj or file_obj.filename == "":
            continue

        if not allowed_file(file_obj.filename):
            errors.append(f"'{file_obj.filename}': Unsupported format. Supported: JPG, PNG, WEBP, PDF.")
            continue

        safe_name = secure_filename(file_obj.filename)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:19]
        unique_name = f"{timestamp}_{safe_name}"
        saved_path = os.path.join(incoming_dir, unique_name)

        try:
            file_obj.save(saved_path)
        except OSError as exc:
            errors.append(f"'{file_obj.filename}': Failed to save file: {exc}")
            continue

        # Convert PDF or load images
        try:
            rendered_images = convert_document_to_images(saved_path, max_pages=3)
        except Exception as exc:
            errors.append(f"'{file_obj.filename}': Could not convert document: {exc}")
            continue

        # Optional quality check on first page
        quality = assess_image_quality(rendered_images[0])
        preview_filename = os.path.basename(rendered_images[0])
        preview_url = f"/uploads/{preview_filename}"

        # Run extraction
        try:
            extracted_info = extract_single_invoice_data(rendered_images, file_obj.filename)
            extracted_info["file_path"] = saved_path
            extracted_info["preview_url"] = preview_url
            extracted_info["quality"] = quality
            extracted_results.append(extracted_info)
        except Exception as exc:
            errors.append(f"'{file_obj.filename}': Extraction failed: {str(exc)}")

    if not extracted_results and errors:
        return jsonify({"success": False, "error": " | ".join(errors)}), 400

    return jsonify({
        "success": True,
        "extracted_count": len(extracted_results),
        "bills": extracted_results,
        "errors": errors if errors else None,
    })


# --------------------------------------------------
# API: SAVE EXTRACTED / EDITED BILLS
# --------------------------------------------------

@expense_bp.route("/save", methods=["POST"])
def api_save_bills():
    """
    Save approved/corrected bills into SQLite database.
    Detects duplicates and stores line items.
    """
    data = request.get_json(silent=True) or {}
    bills_payload = data.get("bills")

    # Support single bill or array of bills
    if not bills_payload:
        if "vendor" in data or "total" in data:
            bills_payload = [data]
        else:
            return jsonify({"success": False, "error": "No bill data received for saving."}), 400

    saved_bills = []
    skipped_duplicates = []

    for item in bills_payload:
        vendor = str(item.get("vendor", "")).strip() or "Unknown Vendor"
        invoice_no = str(item.get("invoice_no", "")).strip() or "N/A"
        date_str = str(item.get("date", "")).strip() or date.today().strftime("%Y-%m-%d")
        category = str(item.get("category", "Other")).strip() or "Other"

        # Guarantee dishes/sabjis, travel, rent, utilities etc. map to canonical active categories
        active_cats = get_active_categories()
        inferred = infer_expense_category(
            raw_cat=category,
            line_items=item.get("line_items") or [],
            vendor=vendor,
            notes=item.get("notes"),
            active_categories=active_cats,
        )
        if inferred:
            category = inferred
        category = canonicalize_category(category, active_cats)

        try:
            total = round(float(item.get("total", 0.0) or 0.0), 2)
        except (ValueError, TypeError):
            total = 0.0

        try:
            subtotal = round(float(item.get("subtotal", 0.0) or 0.0), 2)
        except (ValueError, TypeError):
            subtotal = 0.0

        try:
            cgst_amount = round(float(item.get("cgst_amount", 0.0) or 0.0), 2)
        except (ValueError, TypeError):
            cgst_amount = 0.0

        try:
            sgst_amount = round(float(item.get("sgst_amount", 0.0) or 0.0), 2)
        except (ValueError, TypeError):
            sgst_amount = 0.0

        try:
            igst_amount = round(float(item.get("igst_amount", 0.0) or 0.0), 2)
        except (ValueError, TypeError):
            igst_amount = 0.0

        # Compute gst_amount as sum of CGST + SGST + IGST if present
        if cgst_amount > 0 or sgst_amount > 0 or igst_amount > 0:
            gst_amount = round(cgst_amount + sgst_amount + igst_amount, 2)
        else:
            try:
                gst_amount = round(float(item.get("gst_amount", 0.0) or 0.0), 2)
            except (ValueError, TypeError):
                gst_amount = 0.0

        try:
            raw_rate = round(float(item.get("gst_rate", 0.0) or 0.0), 2)
        except (ValueError, TypeError):
            raw_rate = 0.0

        gst_rate = snap_to_standard_gst_rate(
            raw_rate,
            gst_amount=gst_amount,
            subtotal=subtotal,
            cgst_rate=item.get("cgst_rate"),
            sgst_rate=item.get("sgst_rate"),
            igst_rate=item.get("igst_rate"),
        )

        # Validation check: subtotal + gst_amount vs total with Rs 0.05 threshold
        diff = round(abs((subtotal + gst_amount) - total), 2)
        if diff > 0.05:
            validation_status = "Needs review"
            suggested_gst = round(max(0.0, total - subtotal), 2)
            validation_notes = f"Differs by ₹{diff:.2f}; suggested GST is ₹{suggested_gst:.2f}"
        else:
            validation_status = "OK"
            validation_notes = "Verified: Subtotal + GST = Total"

        filename = str(item.get("filename") or "invoice_upload").strip()
        file_path = item.get("file_path") or ""
        preview_url = item.get("preview_url") or ""
        notes = str(item.get("notes") or "").strip()
        allow_duplicate = bool(item.get("allow_duplicate", False))

        # Check duplicate
        if not allow_duplicate:
            dup = find_duplicate_bill(vendor, invoice_no, total)
            if dup:
                skipped_duplicates.append({
                    "vendor": vendor,
                    "invoice_no": invoice_no,
                    "total": total,
                    "existing_id": dup.id,
                })
                continue

        # Link or create vendor
        vendor_rec = get_or_create_vendor(
            vendor,
            category=category,
            gstin=item.get("gstin") or item.get("vendor_gstin"),
            pan=item.get("pan") or item.get("vendor_pan"),
        )
        vendor_id = vendor_rec.id if vendor_rec else None

        # Parse invoice_date
        inv_date = None
        if date_str:
            try:
                inv_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            except Exception:
                inv_date = None

        bill = Bill(
            filename=filename,
            file_path=file_path,
            preview_url=preview_url,
            vendor_id=vendor_id,
            vendor=vendor,
            invoice_no=invoice_no,
            date=date_str,
            invoice_date=inv_date,
            currency="INR",
            subtotal=subtotal,
            cgst_amount=cgst_amount,
            sgst_amount=sgst_amount,
            igst_amount=igst_amount,
            gst_amount=gst_amount,
            gst_rate=gst_rate,
            total=total,
            category=category,
            payment_status=item.get("payment_status") or "Unpaid",
            validation_status=validation_status,
            validation_notes=validation_notes,
            notes=notes,
            raw_json=item.get("raw_json") or "",
        )

        db.session.add(bill)
        db.session.flush()  # get bill.id

        # Add line items: never leave unit price at 0 when line amount exists
        raw_items = item.get("line_items") or []
        for it in raw_items:
            it_name = str(it.get("name") or "Item").strip()
            try: it_qty = float(it.get("qty", 1.0) or 1.0)
            except: it_qty = 1.0
            if it_qty <= 0: it_qty = 1.0

            try: it_price = float(it.get("price", 0.0) or 0.0)
            except: it_price = 0.0
            try: it_total = float(it.get("total", 0.0) or 0.0)
            except: it_total = 0.0

            if (it_price == 0.0 or it_price is None) and it_total > 0:
                it_price = round(it_total / it_qty, 2)
            elif it_total == 0.0 and it_price > 0:
                it_total = round(it_qty * it_price, 2)

            li = LineItem(
                bill_id=bill.id,
                name=it_name,
                qty=it_qty,
                price=it_price,
                total=it_total,
            )
            db.session.add(li)

        saved_bills.append(bill)

    db.session.commit()

    # Record enterprise audit trail
    for b in saved_bills:
        log_audit(
            action="BILL_SAVED",
            entity_type="bill",
            entity_id=b.id,
            summary=f"Saved invoice #{b.invoice_no or b.id} from {b.vendor} (₹{b.total})",
            payload={"vendor": b.vendor, "total": b.total, "category": b.category},
            ip_address=request.remote_addr,
        )

    return jsonify({
        "success": True,
        "saved_count": len(saved_bills),
        "skipped_count": len(skipped_duplicates),
        "saved_bills": [b.to_dict() for b in saved_bills],
        "skipped_duplicates": skipped_duplicates,
    })


# --------------------------------------------------
# API: DASHBOARD DATA & FILTERED BILLS LIST
# --------------------------------------------------

@expense_bp.route("/data", methods=["GET"])
def api_get_dashboard_data():
    """
    Returns filtered bills for the single vendor, KPIs with top expense category,
    monthly spending, category comparison trend, category donut,
    category expense breakdown bar chart, and GST summary breakdown.
    """
    start_date = request.args.get("start_date", "").strip()
    end_date = request.args.get("end_date", "").strip()
    category = request.args.get("category", "all").strip()
    search = request.args.get("search", "").strip().lower()

    query = Bill.query

    if start_date:
        query = query.filter(Bill.date >= start_date)
    if end_date:
        query = query.filter(Bill.date <= end_date)
    if category and category.lower() != "all":
        canonical_filter = canonicalize_category(category)
        query = query.filter(
            db.or_(
                Bill.category == category,
                Bill.category == canonical_filter,
                db.func.lower(Bill.category) == canonical_filter.lower(),
                db.func.lower(Bill.category) == category.lower(),
            )
        )

    all_filtered = query.order_by(Bill.date.desc(), Bill.id.desc()).all()

    # Search filter across multiple fields
    if search:
        matching_bills = []
        for b in all_filtered:
            txt = f"{b.vendor or ''} {b.invoice_no or ''} {b.notes or ''} {b.category or ''}".lower()
            items_txt = " ".join((it.name or "") for it in b.line_items).lower()
            if search in txt or search in items_txt:
                matching_bills.append(b)
        all_filtered = matching_bills

    # Compute Total KPIs for Current Filtered Set
    total_spend = sum(b.total or 0.0 for b in all_filtered)
    total_gst = sum(b.gst_amount or 0.0 for b in all_filtered)
    bill_count = len(all_filtered)

    # Category Spends & Top Category for this single vendor (canonicalized)
    cat_spends = {}
    for b in all_filtered:
        c = canonicalize_category(b.category or "Other")
        cat_spends[c] = cat_spends.get(c, 0.0) + (b.total or 0.0)

    top_cat_name = "None"
    top_cat_spend = 0.0
    if cat_spends:
        sorted_cats = sorted(cat_spends.items(), key=lambda x: x[1], reverse=True)
        top_cat_name, top_cat_spend = sorted_cats[0]

    # Compute Previous Month comparison for KPIs
    ref_date = date.today()
    if all_filtered:
        for b in all_filtered:
            if b.date and len(b.date) == 10:
                try:
                    ref_date = datetime.strptime(b.date, "%Y-%m-%d").date()
                    break
                except ValueError:
                    pass

    current_month_start = date(ref_date.year, ref_date.month, 1)
    prev_month_start = current_month_start - relativedelta(months=1)
    prev_month_end = current_month_start - timedelta(days=1)

    cur_m_bills = [
        b for b in all_filtered
        if b.date and b.date >= current_month_start.strftime("%Y-%m-%d")
    ]
    cur_m_spend = sum(b.total or 0.0 for b in cur_m_bills)
    cur_m_gst = sum(b.gst_amount or 0.0 for b in cur_m_bills)
    cur_m_count = len(cur_m_bills)

    # Query previous month bills from DB (with same category filter)
    prev_query = Bill.query.filter(
        Bill.date >= prev_month_start.strftime("%Y-%m-%d"),
        Bill.date <= prev_month_end.strftime("%Y-%m-%d"),
    )
    if category and category.lower() != "all":
        prev_query = prev_query.filter(Bill.category == category)

    prev_m_bills = prev_query.all()
    prev_m_spend = sum(b.total or 0.0 for b in prev_m_bills)
    prev_m_gst = sum(b.gst_amount or 0.0 for b in prev_m_bills)
    prev_m_count = len(prev_m_bills)

    def calc_pct_change(curr, prev):
        if prev > 0:
            diff = ((curr - prev) / prev) * 100.0
            return round(diff, 1)
        elif curr > 0:
            return 100.0
        return 0.0

    spend_pct = calc_pct_change(cur_m_spend, prev_m_spend)
    gst_pct = calc_pct_change(cur_m_gst, prev_m_gst)
    count_pct = calc_pct_change(cur_m_count, prev_m_count)

    kpis = {
        "total_spend": round(total_spend, 2),
        "total_gst": round(total_gst, 2),
        "bill_count": bill_count,
        "top_category": {
            "name": top_cat_name,
            "spend": round(top_cat_spend, 2),
            "share_pct": round((top_cat_spend / total_spend * 100.0), 1) if total_spend > 0 else 0.0,
        },
        "top_vendor": {
            "name": top_cat_name,
            "spend": round(top_cat_spend, 2),
            "share_pct": round((top_cat_spend / total_spend * 100.0), 1) if total_spend > 0 else 0.0,
        },
        "spend_change": {
            "pct": abs(spend_pct),
            "direction": "up" if spend_pct > 0 else "down" if spend_pct < 0 else "flat",
            "signed_text": f"{'+' if spend_pct > 0 else ''}{spend_pct}% vs last month",
        },
        "gst_change": {
            "pct": abs(gst_pct),
            "direction": "up" if gst_pct > 0 else "down" if gst_pct < 0 else "flat",
            "signed_text": f"{'+' if gst_pct > 0 else ''}{gst_pct}% vs last month",
        },
        "count_change": {
            "pct": abs(count_pct),
            "direction": "up" if count_pct > 0 else "down" if count_pct < 0 else "flat",
            "signed_text": f"{'+' if count_pct > 0 else ''}{count_pct}% vs last month",
        },
        "month_label": current_month_start.strftime("%B %Y"),
        "prev_month_label": prev_month_start.strftime("%B %Y"),
    }

    # 1. Monthly Spending Chart Data (last 6-12 distinct months)
    monthly_data = {}
    for b in all_filtered:
        if not b.date or len(b.date) < 7:
            continue
        ym = b.date[:7]  # YYYY-MM
        if ym not in monthly_data:
            monthly_data[ym] = {"spend": 0.0, "gst": 0.0, "count": 0}
        monthly_data[ym]["spend"] += (b.total or 0.0)
        monthly_data[ym]["gst"] += (b.gst_amount or 0.0)
        monthly_data[ym]["count"] += 1

    sorted_months = sorted(monthly_data.keys())
    monthly_labels = []
    monthly_spends = []
    monthly_gsts = []
    for ym in sorted_months:
        try:
            m_dt = datetime.strptime(ym, "%Y-%m")
            monthly_labels.append(m_dt.strftime("%b %Y"))
        except ValueError:
            monthly_labels.append(ym)
        monthly_spends.append(round(monthly_data[ym]["spend"], 2))
        monthly_gsts.append(round(monthly_data[ym]["gst"], 2))

    # Active categories list
    active_categories = get_active_categories()
    all_known_cats = list(dict.fromkeys(active_categories + list(cat_spends.keys())))

    # 2. Spending Trend vs. Previous Month (By Category Comparison)
    cat_curr = {c: 0.0 for c in all_known_cats}
    cat_prev = {c: 0.0 for c in all_known_cats}

    for b in cur_m_bills:
        c = canonicalize_category(b.category or "Other", active_categories)
        cat_curr[c] = cat_curr.get(c, 0.0) + (b.total or 0.0)

    for b in prev_m_bills:
        c = canonicalize_category(b.category or "Other", active_categories)
        cat_prev[c] = cat_prev.get(c, 0.0) + (b.total or 0.0)

    trend_cats = [c for c in all_known_cats if cat_curr.get(c, 0) > 0 or cat_prev.get(c, 0) > 0]
    if not trend_cats:
        trend_cats = all_known_cats[:6]

    trend_comparison = {
        "categories": trend_cats,
        "current_month": [round(cat_curr.get(c, 0.0), 2) for c in trend_cats],
        "previous_month": [round(cat_prev.get(c, 0.0), 2) for c in trend_cats],
        "current_label": current_month_start.strftime("%b %Y"),
        "previous_label": prev_month_start.strftime("%b %Y"),
    }

    # 3. Category Donut Chart Data
    cat_labels = []
    cat_values = []
    for c, val in sorted(cat_spends.items(), key=lambda x: x[1], reverse=True):
        if val > 0:
            cat_labels.append(c)
            cat_values.append(round(val, 2))

    # 4. Category Expense Breakdown Bar Chart Data (Ranked for this single vendor)
    sorted_cat_breakdown = sorted(cat_spends.items(), key=lambda x: x[1], reverse=True)
    category_breakdown_labels = [x[0] for x in sorted_cat_breakdown if x[1] > 0]
    category_breakdown_values = [round(x[1], 2) for x in sorted_cat_breakdown if x[1] > 0]
    if not category_breakdown_labels:
        category_breakdown_labels = all_known_cats[:8]
        category_breakdown_values = [0.0] * len(category_breakdown_labels)

    # 5. GST Summary by Month and by Rate
    rate_groups = {0: {"subtotal": 0.0, "gst": 0.0, "count": 0},
                   5: {"subtotal": 0.0, "gst": 0.0, "count": 0},
                   12: {"subtotal": 0.0, "gst": 0.0, "count": 0},
                   18: {"subtotal": 0.0, "gst": 0.0, "count": 0},
                   28: {"subtotal": 0.0, "gst": 0.0, "count": 0},
                   "other": {"subtotal": 0.0, "gst": 0.0, "count": 0}}

    for b in all_filtered:
        r = b.gst_rate
        if r in (0, 0.0): target = 0
        elif r in (5, 5.0): target = 5
        elif r in (12, 12.0): target = 12
        elif r in (18, 18.0): target = 18
        elif r in (28, 28.0): target = 28
        else: target = "other"

        rate_groups[target]["subtotal"] += (b.subtotal or (b.total - (b.gst_amount or 0.0)))
        rate_groups[target]["gst"] += (b.gst_amount or 0.0)
        rate_groups[target]["count"] += 1

    gst_table = []
    for r_key in [0, 5, 12, 18, 28, "other"]:
        grp = rate_groups[r_key]
        lbl = f"{r_key}% GST" if r_key != "other" else "Other Rates"
        share = round((grp["gst"] / total_gst * 100.0), 1) if total_gst > 0 else 0.0
        gst_table.append({
            "rate_key": str(r_key),
            "label": lbl,
            "subtotal": round(grp["subtotal"], 2),
            "gst_collected": round(grp["gst"], 2),
            "count": grp["count"],
            "share_pct": share,
        })

    gst_stacked_rates = ["0%", "5%", "12%", "18%", "28%"]
    gst_stacked_by_month = {r: [0.0] * len(sorted_months) for r in gst_stacked_rates}

    for idx, ym in enumerate(sorted_months):
        m_bills = [b for b in all_filtered if b.date and b.date.startswith(ym)]
        for b in m_bills:
            r = int(round(b.gst_rate or 0))
            rk = f"{r}%"
            if rk not in gst_stacked_by_month:
                rk = "18%"
            gst_stacked_by_month[rk][idx] += round(b.gst_amount or 0.0, 2)

    db_all_bills = Bill.query.all()
    min_date = ""
    max_date = ""
    if db_all_bills:
        valid_dates = [b.date for b in db_all_bills if b.date and len(b.date) == 10]
        if valid_dates:
            min_date = min(valid_dates)
            max_date = max(valid_dates)

    business_profile = get_business_profile().to_dict()

    return jsonify({
        "success": True,
        "business_profile": business_profile,
        "kpis": kpis,
        "charts": {
            "monthly_spending": {
                "labels": monthly_labels,
                "spends": monthly_spends,
                "gsts": monthly_gsts,
            },
            "spending_trend": trend_comparison,
            "category_donut": {
                "labels": cat_labels,
                "values": cat_values,
            },
            "category_breakdown": {
                "labels": category_breakdown_labels,
                "values": category_breakdown_values,
            },
            "top_vendors": {
                "labels": category_breakdown_labels,
                "values": category_breakdown_values,
            },
            "gst_summary": {
                "table": gst_table,
                "months": monthly_labels,
                "stacked_datasets": [
                    {"label": "0% Rate", "data": [round(v, 2) for v in gst_stacked_by_month["0%"]]},
                    {"label": "5% Rate", "data": [round(v, 2) for v in gst_stacked_by_month["5%"]]},
                    {"label": "12% Rate", "data": [round(v, 2) for v in gst_stacked_by_month["12%"]]},
                    {"label": "18% Rate", "data": [round(v, 2) for v in gst_stacked_by_month["18%"]]},
                    {"label": "28% Rate", "data": [round(v, 2) for v in gst_stacked_by_month["28%"]]},
                ],
            },
        },
        "bills": [b.to_dict() for b in all_filtered],
        "filter_options": {
            "categories": all_known_cats,
            "business_name": business_profile.get("business_name", ""),
            "min_date": min_date,
            "max_date": max_date,
        },
    })


# --------------------------------------------------
# API: SINGLE BILL CRUD (VIEW, EDIT, DELETE)
# --------------------------------------------------

@expense_bp.route("/bill/<int:bill_id>", methods=["GET"])
def api_get_bill(bill_id):
    bill = Bill.query.get(bill_id)
    if not bill:
        return jsonify({"success": False, "error": f"Bill #{bill_id} not found."}), 404
    return jsonify({"success": True, "bill": bill.to_dict()})


@expense_bp.route("/bill/<int:bill_id>", methods=["PUT"])
def api_update_bill(bill_id):
    bill = Bill.query.get(bill_id)
    if not bill:
        return jsonify({"success": False, "error": f"Bill #{bill_id} not found."}), 404

    data = request.get_json(silent=True) or {}

    if "vendor" in data:
        v_name = str(data["vendor"]).strip() or "Unknown Vendor"
        bill.vendor = v_name
        v_rec = get_or_create_vendor(v_name, category=bill.category)
        bill.vendor_id = v_rec.id if v_rec else None
    if "invoice_no" in data:
        bill.invoice_no = str(data["invoice_no"]).strip() or "N/A"
    if "date" in data:
        date_str = str(data["date"]).strip() or date.today().strftime("%Y-%m-%d")
        bill.date = date_str
        try:
            bill.invoice_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except Exception:
            pass
    if "category" in data:
        active_cats = get_active_categories()
        cat = canonicalize_category(str(data["category"]).strip(), active_cats)
        bill.category = cat
    if "subtotal" in data:
        try: bill.subtotal = round(float(data["subtotal"]), 2)
        except: pass
    if "gst_amount" in data:
        try: bill.gst_amount = round(float(data["gst_amount"]), 2)
        except: pass
    if "gst_rate" in data:
        try: bill.gst_rate = round(float(data["gst_rate"]), 2)
        except: pass
    if "total" in data:
        try: bill.total = round(float(data["total"]), 2)
        except: pass
    if "notes" in data:
        bill.notes = str(data["notes"]).strip()

    # Update line items if provided
    if "line_items" in data and isinstance(data["line_items"], list):
        # Clear existing line items
        LineItem.query.filter_by(bill_id=bill.id).delete()
        for it in data["line_items"]:
            it_name = str(it.get("name") or "Item").strip()
            try: it_qty = float(it.get("qty", 1.0) or 1.0)
            except: it_qty = 1.0
            if it_qty <= 0: it_qty = 1.0

            try: it_price = float(it.get("price", 0.0) or 0.0)
            except: it_price = 0.0
            try: it_total = float(it.get("total", 0.0) or 0.0)
            except: it_total = 0.0

            # If unit price is missing or 0 but line amount is present, set price = amount / qty
            if (it_price == 0.0 or it_price is None) and it_total > 0:
                it_price = round(it_total / it_qty, 2)
            elif it_total == 0.0 and it_price > 0:
                it_total = round(it_qty * it_price, 2)

            li = LineItem(
                bill_id=bill.id,
                name=it_name,
                qty=it_qty,
                price=it_price,
                total=it_total,
            )
            db.session.add(li)

    # Recalculate validation status and snap GST rate
    bill.gst_rate = snap_to_standard_gst_rate(
        bill.gst_rate,
        gst_amount=bill.gst_amount,
        subtotal=bill.subtotal,
    )
    diff = round(abs(((bill.subtotal or 0.0) + (bill.gst_amount or 0.0)) - (bill.total or 0.0)), 2)
    if diff > 0.05:
        bill.validation_status = "Needs review"
        suggested_gst = round(max(0.0, (bill.total or 0.0) - (bill.subtotal or 0.0)), 2)
        bill.validation_notes = f"Differs by ₹{diff:.2f}; suggested GST is ₹{suggested_gst:.2f}"
    else:
        bill.validation_status = "OK"
        bill.validation_notes = "Verified: Subtotal + GST = Total"

    db.session.commit()

    log_audit(
        action="BILL_UPDATED",
        entity_type="bill",
        entity_id=bill.id,
        summary=f"Updated bill #{bill.invoice_no or bill.id} ({bill.vendor})",
        payload={"total": bill.total, "category": bill.category},
        ip_address=request.remote_addr,
    )

    return jsonify({"success": True, "bill": bill.to_dict()})


@expense_bp.route("/bill/<int:bill_id>", methods=["DELETE"])
def api_delete_bill(bill_id):
    bill = Bill.query.get(bill_id)
    if not bill:
        return jsonify({"success": False, "error": f"Bill #{bill_id} not found."}), 404

    b_id = bill.id
    b_desc = f"Bill #{bill.invoice_no or bill.id} ({bill.vendor}) ₹{bill.total}"
    db.session.delete(bill)
    db.session.commit()

    log_audit(
        action="BILL_DELETED",
        entity_type="bill",
        entity_id=b_id,
        summary=f"Deleted {b_desc}",
        ip_address=request.remote_addr,
    )
    return jsonify({"success": True, "message": f"Bill #{bill_id} deleted successfully."})


@expense_bp.route("/bills/clear-all", methods=["POST", "DELETE"])
@expense_bp.route("/clear-history", methods=["POST", "DELETE"])
def api_clear_all_bills():
    """Delete all bills, line items, and reset ledger history."""
    try:
        num_items = LineItem.query.delete()
        num_bills = Bill.query.delete()
        db.session.commit()

        log_audit(
            action="BILLS_CLEARED",
            entity_type="bill",
            entity_id=None,
            summary=f"Cleared all bills history ({num_bills} bills, {num_items} line items)",
            ip_address=request.remote_addr,
        )

        return jsonify({
            "success": True,
            "message": f"Successfully deleted {num_bills} bills and {num_items} line items.",
            "deleted_bills": num_bills,
            "deleted_items": num_items,
        })
    except Exception as exc:
        db.session.rollback()
        return jsonify({"success": False, "error": f"Failed to clear bills: {str(exc)}"}), 500


# --------------------------------------------------
# API: EXPORT CSV & EXCEL
# --------------------------------------------------

@expense_bp.route("/export/csv", methods=["GET"])
def api_export_csv():
    start_date = request.args.get("start_date", "").strip()
    end_date = request.args.get("end_date", "").strip()
    category = request.args.get("category", "all").strip()
    vendor = request.args.get("vendor", "all").strip()
    search = request.args.get("search", "").strip().lower()

    query = Bill.query
    if start_date: query = query.filter(Bill.date >= start_date)
    if end_date: query = query.filter(Bill.date <= end_date)
    if category and category.lower() != "all": query = query.filter(Bill.category == category)
    if vendor and vendor.lower() != "all": query = query.filter(Bill.vendor == vendor)

    bills = query.order_by(Bill.date.desc(), Bill.id.desc()).all()
    if search:
        bills = [
            b for b in bills
            if search in f"{b.vendor} {b.invoice_no} {b.notes} {b.category}".lower()
        ]

    output = io.StringIO()
    # Write UTF-8 BOM so Excel opens with correct rupee symbols and encoding
    output.write('\ufeff')
    writer = csv.writer(output)
    writer.writerow([
        "Bill ID", "Date", "Vendor", "Invoice No", "Category",
        "Subtotal (INR)", "GST Rate (%)", "GST Amount (INR)", "Total (INR)",
        "Validation Status", "Validation Notes",
        "Line Items", "Notes", "File Name", "Created At"
    ])

    for b in bills:
        items_summary = "; ".join(f"{it.name} (x{it.qty} @ Rs {it.price})" for it in b.line_items)
        sub = float(b.subtotal or 0.0)
        gst = float(b.gst_amount or 0.0)
        tot = float(b.total or 0.0)
        diff = round(abs((sub + gst) - tot), 2)
        val_status = b.validation_status or ("Needs review" if diff > 0.05 else "OK")
        val_notes = b.validation_notes or (
            f"Differs by ₹{diff:.2f}; suggested GST is ₹{max(0.0, tot - sub):.2f}"
            if diff > 0.05 else "Verified: Subtotal + GST = Total"
        )
        # Shorten notes to one line without newlines or excessive spacing
        short_notes = " ".join((b.notes or "").split())[:120]

        writer.writerow([
            b.id,
            b.date or "",
            b.vendor or "Unknown Vendor",
            b.invoice_no or "N/A",
            b.category or "Other",
            f"{sub:.2f}",
            f"{float(b.gst_rate or 0.0):.1f}",
            f"{gst:.2f}",
            f"{tot:.2f}",
            val_status,
            val_notes,
            items_summary,
            short_notes,
            b.filename,
            b.created_at.strftime("%Y-%m-%d %H:%M") if b.created_at else "",
        ])

    mem = io.BytesIO()
    mem.write(output.getvalue().encode("utf-8"))
    mem.seek(0)

    filename = f"expense_report_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    return send_file(
        mem,
        mimetype="text/csv",
        as_attachment=True,
        download_name=filename,
    )


@expense_bp.route("/export/excel", methods=["GET"])
def api_export_excel():
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    start_date = request.args.get("start_date", "").strip()
    end_date = request.args.get("end_date", "").strip()
    category = request.args.get("category", "all").strip()
    vendor = request.args.get("vendor", "all").strip()
    search = request.args.get("search", "").strip().lower()

    query = Bill.query
    if start_date: query = query.filter(Bill.date >= start_date)
    if end_date: query = query.filter(Bill.date <= end_date)
    if category and category.lower() != "all": query = query.filter(Bill.category == category)
    if vendor and vendor.lower() != "all": query = query.filter(Bill.vendor == vendor)

    bills = query.order_by(Bill.date.desc(), Bill.id.desc()).all()
    if search:
        bills = [
            b for b in bills
            if search in f"{b.vendor} {b.invoice_no} {b.notes} {b.category}".lower()
        ]

    wb = openpyxl.Workbook()

    # Sheet 1: Expenses Summary
    ws1 = wb.active
    ws1.title = "Expenses Summary"

    header_font = Font(name="Arial", size=11, bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="0D4C3C", end_color="0D4C3C", fill_type="solid")
    center_align = Alignment(horizontal="center", vertical="center")
    right_align = Alignment(horizontal="right", vertical="center")
    thin_border = Border(
        left=Side(style="thin", color="CCCCCC"),
        right=Side(style="thin", color="CCCCCC"),
        top=Side(style="thin", color="CCCCCC"),
        bottom=Side(style="thin", color="CCCCCC"),
    )

    status_ok_fill = PatternFill(start_color="EAF5E9", end_color="EAF5E9", fill_type="solid")
    status_ok_font = Font(name="Arial", size=10, bold=True, color="235418")
    status_review_fill = PatternFill(start_color="FFF3DC", end_color="FFF3DC", fill_type="solid")
    status_review_font = Font(name="Arial", size=10, bold=True, color="B87A1E")

    headers = [
        "Bill ID", "Date", "Vendor", "Invoice No", "Category",
        "Subtotal (Rs)", "GST Rate (%)", "GST Amount (Rs)", "Total (Rs)",
        "Validation Status", "Validation Notes", "Notes", "File Name"
    ]
    ws1.append(headers)

    for col_idx in range(1, len(headers) + 1):
        cell = ws1.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center_align

    row_num = 2
    for b in bills:
        sub = float(b.subtotal or 0.0)
        gst = float(b.gst_amount or 0.0)
        tot = float(b.total or 0.0)
        diff = round(abs((sub + gst) - tot), 2)
        val_status = b.validation_status or ("Needs review" if diff > 0.05 else "OK")
        val_notes = b.validation_notes or (
            f"Differs by ₹{diff:.2f}; suggested GST is ₹{max(0.0, tot - sub):.2f}"
            if diff > 0.05 else "Verified: Subtotal + GST = Total"
        )
        short_notes = " ".join((b.notes or "").split())[:120]

        ws1.append([
            b.id,
            b.date or "",
            b.vendor or "Unknown Vendor",
            b.invoice_no or "N/A",
            b.category or "Other",
            sub,
            float(b.gst_rate or 0.0),
            gst,
            tot,
            val_status,
            val_notes,
            short_notes,
            b.filename,
        ])
        for c_idx in range(1, len(headers) + 1):
            cell = ws1.cell(row=row_num, column=c_idx)
            cell.border = thin_border
            if c_idx in (6, 7, 8, 9):
                cell.alignment = right_align
                if c_idx in (6, 8, 9):
                    cell.number_format = "#,##0.00"
            elif c_idx == 10:  # Validation Status
                cell.alignment = center_align
                if val_status == "OK":
                    cell.fill = status_ok_fill
                    cell.font = status_ok_font
                else:
                    cell.fill = status_review_fill
                    cell.font = status_review_font

        row_num += 1

    # Auto-adjust column widths
    for col in ws1.columns:
        max_len = max(len(str(cell.value or "")) for cell in col)
        col_letter = get_column_letter(col[0].column)
        ws1.column_dimensions[col_letter].width = max(max_len + 3, 12)

    # Sheet 2: Line Items Detail
    ws2 = wb.create_sheet(title="Line Items Detail")
    li_headers = ["Bill ID", "Date", "Vendor", "Invoice No", "Category", "Item Name", "Quantity", "Unit Price (Rs)", "Item Total (Rs)"]
    ws2.append(li_headers)

    li_header_fill = PatternFill(start_color="2D5A4A", end_color="2D5A4A", fill_type="solid")
    for col_idx in range(1, len(li_headers) + 1):
        cell = ws2.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.fill = li_header_fill
        cell.alignment = center_align

    li_row = 2
    for b in bills:
        for it in b.line_items:
            ws2.append([
                b.id,
                b.date or "",
                b.vendor or "Unknown Vendor",
                b.invoice_no or "N/A",
                b.category or "Other",
                it.name,
                it.qty,
                it.price,
                it.total,
            ])
            for c_idx in range(1, len(li_headers) + 1):
                cell = ws2.cell(row=li_row, column=c_idx)
                cell.border = thin_border
                if c_idx in (7, 8, 9):
                    cell.alignment = right_align
                    if c_idx in (8, 9):
                        cell.number_format = "#,##0.00"
            li_row += 1

    for col in ws2.columns:
        max_len = max(len(str(cell.value or "")) for cell in col)
        col_letter = get_column_letter(col[0].column)
        ws2.column_dimensions[col_letter].width = max(max_len + 3, 12)

    mem = io.BytesIO()
    wb.save(mem)
    mem.seek(0)

    filename = f"expense_workbook_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    return send_file(
        mem,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )


# --------------------------------------------------
# API: EXPORT TO TALLY (XML, EXCEL, CSV)
# --------------------------------------------------

def _get_filtered_bills_for_export():
    """Helper to fetch and filter bills based on request query params."""
    start_date = request.args.get("start_date", "").strip()
    end_date = request.args.get("end_date", "").strip()
    category = request.args.get("category", "all").strip()
    vendor = request.args.get("vendor", "all").strip()
    search = request.args.get("search", "").strip().lower()

    query = Bill.query
    if start_date:
        query = query.filter(Bill.date >= start_date)
    if end_date:
        query = query.filter(Bill.date <= end_date)
    if category and category.lower() != "all":
        query = query.filter(Bill.category == category)
    if vendor and vendor.lower() != "all":
        query = query.filter(Bill.vendor == vendor)

    bills = query.order_by(Bill.date.desc(), Bill.id.desc()).all()
    if search:
        bills = [
            b for b in bills
            if search in f"{b.vendor} {b.invoice_no} {b.notes} {b.category}".lower()
        ]
    return bills


def _format_tally_xml_date(bill):
    """Format bill date as YYYYMMDD for Tally XML import."""
    if bill.invoice_date:
        return bill.invoice_date.strftime("%Y%m%d")
    if bill.date:
        d_str = str(bill.date).strip()
        digits = re.sub(r"[^\d]", "", d_str)
        if len(digits) == 8:
            if "-" in d_str:
                parts = [p for p in d_str.split("-") if p]
                if len(parts) == 3 and len(parts[0]) == 4:
                    return f"{parts[0]}{parts[1].zfill(2)}{parts[2].zfill(2)}"
            return digits
        try:
            dt = datetime.strptime(d_str[:10], "%Y-%m-%d")
            return dt.strftime("%Y%m%d")
        except Exception:
            pass
    if bill.created_at:
        return bill.created_at.strftime("%Y%m%d")
    return datetime.now().strftime("%Y%m%d")


def _format_tally_display_date(bill):
    """Format bill date as DD-MM-YYYY for Tally Excel and CSV."""
    if bill.invoice_date:
        return bill.invoice_date.strftime("%d-%m-%Y")
    if bill.date:
        d_str = str(bill.date).strip()
        if "-" in d_str:
            parts = [p for p in d_str.split("-") if p]
            if len(parts) == 3:
                if len(parts[0]) == 4:
                    return f"{parts[2].zfill(2)}-{parts[1].zfill(2)}-{parts[0]}"
                if len(parts[2]) == 4:
                    return f"{parts[0].zfill(2)}-{parts[1].zfill(2)}-{parts[2]}"
        try:
            dt = datetime.strptime(d_str[:10], "%Y-%m-%d")
            return dt.strftime("%d-%m-%Y")
        except Exception:
            return d_str
    if bill.created_at:
        return bill.created_at.strftime("%d-%m-%Y")
    return datetime.now().strftime("%d-%m-%Y")


def _resolve_bill_tax_breakdown(bill):
    """
    Resolve subtotal, cgst, sgst, igst, and vendor credit ensuring double-entry balance.
    """
    sub = round(float(bill.subtotal or 0.0), 2)
    cgst = round(float(bill.cgst_amount or 0.0), 2)
    sgst = round(float(bill.sgst_amount or 0.0), 2)
    igst = round(float(bill.igst_amount or 0.0), 2)
    tot = round(float(bill.total or 0.0), 2)
    gst_amt = round(float(bill.gst_amount or 0.0), 2)

    # If cgst/sgst/igst are not set individually, but total gst_amount > 0 exists
    if cgst == 0.0 and sgst == 0.0 and igst == 0.0 and gst_amt > 0.0:
        half = round(gst_amt / 2.0, 2)
        cgst = half
        sgst = round(gst_amt - half, 2)

    # If subtotal is zero but total is present
    if sub == 0.0 and tot > 0.0:
        sub = max(0.0, round(tot - (cgst + sgst + igst), 2))

    # Total debits = expense + taxes
    total_debits = round(sub + cgst + sgst + igst, 2)

    # Vendor credit: use total_debits to ensure strict double-entry balance (Total Debits == Total Credits)
    vendor_credit = total_debits if total_debits > 0.0 else tot

    return sub, cgst, sgst, igst, vendor_credit


@expense_bp.route("/export/tally/xml", methods=["GET"])
def api_export_tally_xml():
    """Export filtered purchase bills into Tally native XML import format."""
    bills = _get_filtered_bills_for_export()
    if not bills:
        return jsonify({
            "success": False,
            "error": "No bills found matching the selected filters to export to Tally XML."
        }), 404

    prof = get_business_profile()
    company_name = prof.business_name if (prof and prof.business_name) else "R K Construction"
    escaped_company = xml_escape(company_name)

    xml_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<ENVELOPE>',
        '  <HEADER>',
        '    <TALLYREQUEST>Import Data</TALLYREQUEST>',
        '  </HEADER>',
        '  <BODY>',
        '    <IMPORTDATA>',
        '      <REQUESTDESC>',
        '        <REPORTNAME>Vouchers</REPORTNAME>',
        '        <STATICVARIABLES>',
        f'          <SVCURRENTCOMPANY>{escaped_company}</SVCURRENTCOMPANY>',
        '        </STATICVARIABLES>',
        '      </REQUESTDESC>',
        '      <REQUESTDATA>',
    ]

    for b in bills:
        sub, cgst, sgst, igst, vendor_credit = _resolve_bill_tax_breakdown(b)
        date_ymd = _format_tally_xml_date(b)
        inv_no = xml_escape(str(b.invoice_no or "N/A").strip())
        vendor = xml_escape(str(b.vendor or "Unknown Vendor").strip())
        cat_name = xml_escape(f"{str(b.category or 'Other').strip()} Expenses")
        narration = xml_escape(str(b.notes or b.category or "Purchase Invoice").strip())

        xml_lines.append('        <!-- One TALLYMESSAGE block per bill -->')
        xml_lines.append('        <TALLYMESSAGE xmlns:UDF="TallyUDF">')
        xml_lines.append('          <VOUCHER VCHTYPE="Purchase" ACTION="Create">')
        xml_lines.append(f'            <DATE>{date_ymd}</DATE>')
        xml_lines.append('            <VOUCHERTYPENAME>Purchase</VOUCHERTYPENAME>')
        xml_lines.append(f'            <VOUCHERNUMBER>{inv_no}</VOUCHERNUMBER>')
        xml_lines.append(f'            <PARTYLEDGERNAME>{vendor}</PARTYLEDGERNAME>')
        xml_lines.append(f'            <EFFECTIVEDATE>{date_ymd}</EFFECTIVEDATE>')
        xml_lines.append(f'            <NARRATION>{narration}</NARRATION>')

        # DEBIT: Expense Ledger
        xml_lines.append('            <!-- DEBIT: Expense Ledger -->')
        xml_lines.append('            <ALLLEDGERENTRIES.LIST>')
        xml_lines.append(f'              <LEDGERNAME>{cat_name}</LEDGERNAME>')
        xml_lines.append('              <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>')
        xml_lines.append(f'              <AMOUNT>-{sub:.2f}</AMOUNT>')
        xml_lines.append('            </ALLLEDGERENTRIES.LIST>')

        # DEBIT: CGST (only if cgst_amount > 0)
        if cgst > 0.0:
            xml_lines.append('            <!-- DEBIT: CGST (only if cgst_amount > 0) -->')
            xml_lines.append('            <ALLLEDGERENTRIES.LIST>')
            xml_lines.append('              <LEDGERNAME>CGST Input</LEDGERNAME>')
            xml_lines.append('              <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>')
            xml_lines.append(f'              <AMOUNT>-{cgst:.2f}</AMOUNT>')
            xml_lines.append('            </ALLLEDGERENTRIES.LIST>')

        # DEBIT: SGST (only if sgst_amount > 0)
        if sgst > 0.0:
            xml_lines.append('            <!-- DEBIT: SGST (only if sgst_amount > 0) -->')
            xml_lines.append('            <ALLLEDGERENTRIES.LIST>')
            xml_lines.append('              <LEDGERNAME>SGST Input</LEDGERNAME>')
            xml_lines.append('              <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>')
            xml_lines.append(f'              <AMOUNT>-{sgst:.2f}</AMOUNT>')
            xml_lines.append('            </ALLLEDGERENTRIES.LIST>')

        # DEBIT: IGST (only if igst_amount > 0)
        if igst > 0.0:
            xml_lines.append('            <!-- DEBIT: IGST (only if igst_amount > 0) -->')
            xml_lines.append('            <ALLLEDGERENTRIES.LIST>')
            xml_lines.append('              <LEDGERNAME>IGST Input</LEDGERNAME>')
            xml_lines.append('              <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>')
            xml_lines.append(f'              <AMOUNT>-{igst:.2f}</AMOUNT>')
            xml_lines.append('            </ALLLEDGERENTRIES.LIST>')

        # CREDIT: Vendor Payable
        xml_lines.append('            <!-- CREDIT: Vendor Payable -->')
        xml_lines.append('            <ALLLEDGERENTRIES.LIST>')
        xml_lines.append(f'              <LEDGERNAME>{vendor}</LEDGERNAME>')
        xml_lines.append('              <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>')
        xml_lines.append(f'              <AMOUNT>{vendor_credit:.2f}</AMOUNT>')
        xml_lines.append('            </ALLLEDGERENTRIES.LIST>')

        xml_lines.append('          </VOUCHER>')
        xml_lines.append('        </TALLYMESSAGE>')

    xml_lines.extend([
        '      </REQUESTDATA>',
        '    </IMPORTDATA>',
        '  </BODY>',
        '</ENVELOPE>',
    ])

    xml_content = "\n".join(xml_lines)
    mem = io.BytesIO()
    mem.write(xml_content.encode("utf-8"))
    mem.seek(0)

    log_audit(
        action="EXPORT",
        entity_type="tally_xml",
        entity_id=0,
        summary=f"Exported {len(bills)} bills to Tally XML format",
        payload={"format": "tally_xml", "bill_count": len(bills), "company": company_name},
        ip_address=request.remote_addr,
    )

    filename = f"tally_export_{datetime.now().strftime('%Y%m%d')}.xml"
    return send_file(
        mem,
        mimetype="application/xml",
        as_attachment=True,
        download_name=filename,
    )


@expense_bp.route("/export/tally/excel", methods=["GET"])
def api_export_tally_excel():
    """Export filtered purchase bills into Tally-compatible Excel format."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    bills = _get_filtered_bills_for_export()
    if not bills:
        return jsonify({
            "success": False,
            "error": "No bills found matching the selected filters to export to Tally Excel."
        }), 404

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Tally Vouchers"

    # Styling
    header_font = Font(name="Arial", size=11, bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="0D4C3C", end_color="0D4C3C", fill_type="solid")
    center_align = Alignment(horizontal="center", vertical="center")
    right_align = Alignment(horizontal="right", vertical="center")
    left_align = Alignment(horizontal="left", vertical="center")

    thin_border = Border(
        left=Side(style="thin", color="CCCCCC"),
        right=Side(style="thin", color="CCCCCC"),
        top=Side(style="thin", color="CCCCCC"),
        bottom=Side(style="thin", color="CCCCCC"),
    )
    total_border = Border(
        left=Side(style="thin", color="CCCCCC"),
        right=Side(style="thin", color="CCCCCC"),
        top=Side(style="thin", color="0D4C3C"),
        bottom=Side(style="double", color="0D4C3C"),
    )
    total_fill = PatternFill(start_color="EAF5E9", end_color="EAF5E9", fill_type="solid")
    total_font = Font(name="Arial", size=11, bold=True, color="0D4C3C")

    headers = [
        "Date", "Voucher Type", "Voucher Number", "Party Ledger",
        "Expense Ledger", "Subtotal", "CGST Amount", "SGST Amount",
        "IGST Amount", "GST Rate (%)", "Total", "Narration"
    ]
    ws.append(headers)

    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center_align

    tot_sub = 0.0
    tot_cgst = 0.0
    tot_sgst = 0.0
    tot_igst = 0.0
    tot_total = 0.0

    row_num = 2
    for b in bills:
        sub, cgst, sgst, igst, vendor_credit = _resolve_bill_tax_breakdown(b)
        tot_sub += sub
        tot_cgst += cgst
        tot_sgst += sgst
        tot_igst += igst
        tot_total += vendor_credit

        d_str = _format_tally_display_date(b)
        narration = " ".join((b.notes or b.category or "").split())[:150]

        ws.append([
            d_str,
            "Purchase",
            b.invoice_no or "N/A",
            b.vendor or "Unknown Vendor",
            f"{b.category or 'Other'} Expenses",
            sub,
            cgst,
            sgst,
            igst,
            float(b.gst_rate or 0.0),
            vendor_credit,
            narration,
        ])

        for c_idx in range(1, len(headers) + 1):
            cell = ws.cell(row=row_num, column=c_idx)
            cell.border = thin_border
            if c_idx in (1, 2, 3, 10):
                cell.alignment = center_align
            elif c_idx in (6, 7, 8, 9, 11):
                cell.alignment = right_align
                cell.number_format = "#,##0.00"
            else:
                cell.alignment = left_align

        row_num += 1

    # TOTALS Row
    totals_row = [
        "TOTAL", "", "", "", "",
        round(tot_sub, 2),
        round(tot_cgst, 2),
        round(tot_sgst, 2),
        round(tot_igst, 2),
        "",
        round(tot_total, 2),
        ""
    ]
    ws.append(totals_row)
    for c_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=row_num, column=c_idx)
        cell.font = total_font
        cell.fill = total_fill
        cell.border = total_border
        if c_idx == 1:
            cell.alignment = center_align
        elif c_idx in (6, 7, 8, 9, 11):
            cell.alignment = right_align
            cell.number_format = "#,##0.00"

    # Auto-adjust column widths
    for col in ws.columns:
        max_len = max(len(str(cell.value or "")) for cell in col)
        col_letter = get_column_letter(col[0].column)
        ws.column_dimensions[col_letter].width = max(max_len + 4, 14)

    mem = io.BytesIO()
    wb.save(mem)
    mem.seek(0)

    log_audit(
        action="EXPORT",
        entity_type="tally_excel",
        entity_id=0,
        summary=f"Exported {len(bills)} bills to Tally Excel format",
        payload={"format": "tally_excel", "bill_count": len(bills)},
        ip_address=request.remote_addr,
    )

    filename = f"tally_export_{datetime.now().strftime('%Y%m%d')}.xlsx"
    return send_file(
        mem,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )


@expense_bp.route("/export/tally/csv", methods=["GET"])
def api_export_tally_csv():
    """Export filtered purchase bills into multi-row Tally CSV format."""
    bills = _get_filtered_bills_for_export()
    if not bills:
        return jsonify({
            "success": False,
            "error": "No bills found matching the selected filters to export to Tally CSV."
        }), 404

    output = io.StringIO()
    # Write UTF-8 BOM so Excel opens CSV with proper encoding
    output.write('\ufeff')
    writer = csv.writer(output)

    # Format: Date,Voucher Type,Voucher No,Party,Ledger,Amount,Narration
    writer.writerow([
        "Date", "Voucher Type", "Voucher No", "Party", "Ledger", "Amount", "Narration"
    ])

    for b in bills:
        sub, cgst, sgst, igst, vendor_credit = _resolve_bill_tax_breakdown(b)
        date_str = _format_tally_display_date(b)
        vch_type = "Purchase"
        vch_no = b.invoice_no or "N/A"
        party = b.vendor or "Unknown Vendor"
        expense_ledger = f"{b.category or 'Other'} Expenses"
        narration = " ".join((b.notes or b.category or "").split())[:150]

        # 1. DEBIT: Expense Ledger (negative amount)
        writer.writerow([
            date_str, vch_type, vch_no, party, expense_ledger, f"{-abs(sub):.2f}", narration
        ])

        # 2. DEBIT: CGST (only if cgst_amount > 0)
        if cgst > 0.0:
            writer.writerow([
                date_str, vch_type, vch_no, party, "CGST Input", f"{-abs(cgst):.2f}", narration
            ])

        # 3. DEBIT: SGST (only if sgst_amount > 0)
        if sgst > 0.0:
            writer.writerow([
                date_str, vch_type, vch_no, party, "SGST Input", f"{-abs(sgst):.2f}", narration
            ])

        # 4. DEBIT: IGST (only if igst_amount > 0)
        if igst > 0.0:
            writer.writerow([
                date_str, vch_type, vch_no, party, "IGST Input", f"{-abs(igst):.2f}", narration
            ])

        # 5. CREDIT: Vendor Payable (positive amount)
        writer.writerow([
            date_str, vch_type, vch_no, party, party, f"{abs(vendor_credit):.2f}", narration
        ])

    mem = io.BytesIO()
    mem.write(output.getvalue().encode("utf-8"))
    mem.seek(0)

    log_audit(
        action="EXPORT",
        entity_type="tally_csv",
        entity_id=0,
        summary=f"Exported {len(bills)} bills to Tally CSV format",
        payload={"format": "tally_csv", "bill_count": len(bills)},
        ip_address=request.remote_addr,
    )

    filename = f"tally_export_{datetime.now().strftime('%Y%m%d')}.csv"
    return send_file(
        mem,
        mimetype="text/csv",
        as_attachment=True,
        download_name=filename,
    )


# --------------------------------------------------
# API: VENDOR MASTER & AUDIT LOGS (ENTERPRISE POSTGRESQL)
# --------------------------------------------------

@expense_bp.route("/vendors", methods=["GET"])
def api_get_vendors():
    """Returns vendor master list with aggregated spend and bill counts."""
    vendors = Vendor.query.order_by(Vendor.name.asc()).all()
    return jsonify({
        "success": True,
        "count": len(vendors),
        "vendors": [v.to_dict() for v in vendors],
    })


@expense_bp.route("/audit-logs", methods=["GET"])
def api_get_audit_logs():
    """Returns compliance and activity audit logs."""
    limit = request.args.get("limit", 100, type=int)
    action = request.args.get("action", "").strip()
    query = AuditLog.query
    if action:
        query = query.filter(AuditLog.action == action)
    logs = query.order_by(AuditLog.id.desc()).limit(limit).all()
    return jsonify({
        "success": True,
        "count": len(logs),
        "logs": [l.to_dict() for l in logs],
    })
