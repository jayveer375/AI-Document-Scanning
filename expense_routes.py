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

from models import db, Bill, LineItem, find_duplicate_bill, VALID_CATEGORIES
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


def build_invoice_extraction_prompt():
    return """You are a forensic financial document and invoice OCR parser.
Analyze the attached invoice/bill image(s) with extreme precision.
Extract ONLY authentic, clearly visible details.
DO NOT guess or invent numbers. If any value is not visible or illegible, set its value to null.

Output ONLY valid JSON adhering strictly to the schema below without markdown code blocks, backticks, or commentary:
{
  "vendor": "<string or null>",
  "invoice_no": "<string or null>",
  "date": "<YYYY-MM-DD string or null>",
  "category": "<one of: Food, Travel, Utilities, Stationery, Inventory, Other>",
  "line_items": [
    {
      "name": "<string>",
      "qty": <number or 1>,
      "price": <unit price number or 0.0>,
      "total": <line amount number or 0.0>
    }
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
  "confidence": {
    "vendor": "<HIGH, MEDIUM, or LOW>",
    "invoice_no": "<HIGH, MEDIUM, or LOW>",
    "date": "<HIGH, MEDIUM, or LOW>",
    "total": "<HIGH, MEDIUM, or LOW>",
    "gst_amount": "<HIGH, MEDIUM, or LOW>"
  },
  "notes": "<brief context about the vendor or bill items or null>"
}

Strict Rules:
1. For date: Convert to YYYY-MM-DD (e.g. 2026-02-14). If only month/year or ambiguous, use null.
2. For numbers: Return raw floats/integers (e.g. 2450.50), NOT currency symbols like ₹, $, Rs.
3. Category: Choose the single best match among: Food, Travel, Utilities, Stationery, Inventory, Other.
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

    prompt_text = build_invoice_extraction_prompt()
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

    category = parsed.get("category") or "Other"
    if category not in VALID_CATEGORIES:
        category = "Other"

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
        category = str(item.get("category", "Other")).strip()
        if category not in VALID_CATEGORIES:
            category = "Other"

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

        bill = Bill(
            filename=filename,
            file_path=file_path,
            preview_url=preview_url,
            vendor=vendor,
            invoice_no=invoice_no,
            date=date_str,
            subtotal=subtotal,
            cgst_amount=cgst_amount,
            sgst_amount=sgst_amount,
            igst_amount=igst_amount,
            gst_amount=gst_amount,
            gst_rate=gst_rate,
            total=total,
            category=category,
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
    Returns filtered bills, KPIs with previous-month % changes,
    monthly spending, spending trend vs previous month, category donut,
    top vendors, and GST summary breakdown.
    """
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

    # Top Vendor in filtered set
    vendor_spends = {}
    for b in all_filtered:
        v = (b.vendor or "Unknown").strip()
        vendor_spends[v] = vendor_spends.get(v, 0.0) + (b.total or 0.0)

    top_vendor_name = "None"
    top_vendor_spend = 0.0
    if vendor_spends:
        sorted_vendors = sorted(vendor_spends.items(), key=lambda x: x[1], reverse=True)
        top_vendor_name, top_vendor_spend = sorted_vendors[0]

    # Compute Previous Month comparison for KPIs
    # Determine the reference month: latest month in filtered bills, or current month
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

    # Query previous month bills from DB (with same category/vendor filters)
    prev_query = Bill.query.filter(
        Bill.date >= prev_month_start.strftime("%Y-%m-%d"),
        Bill.date <= prev_month_end.strftime("%Y-%m-%d"),
    )
    if category and category.lower() != "all":
        prev_query = prev_query.filter(Bill.category == category)
    if vendor and vendor.lower() != "all":
        prev_query = prev_query.filter(Bill.vendor == vendor)

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
        "top_vendor": {
            "name": top_vendor_name,
            "spend": round(top_vendor_spend, 2),
            "share_pct": round((top_vendor_spend / total_spend * 100.0), 1) if total_spend > 0 else 0.0,
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
    # Format labels e.g. "Jan 2026"
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

    # 2. Spending Trend vs. Previous Month (By Category Comparison)
    cat_curr = {c: 0.0 for c in VALID_CATEGORIES}
    cat_prev = {c: 0.0 for c in VALID_CATEGORIES}

    for b in cur_m_bills:
        c = b.category if b.category in VALID_CATEGORIES else "Other"
        cat_curr[c] += (b.total or 0.0)

    for b in prev_m_bills:
        c = b.category if b.category in VALID_CATEGORIES else "Other"
        cat_prev[c] += (b.total or 0.0)

    trend_comparison = {
        "categories": VALID_CATEGORIES,
        "current_month": [round(cat_curr[c], 2) for c in VALID_CATEGORIES],
        "previous_month": [round(cat_prev[c], 2) for c in VALID_CATEGORIES],
        "current_label": current_month_start.strftime("%b %Y"),
        "previous_label": prev_month_start.strftime("%b %Y"),
    }

    # 3. Category Donut Chart Data
    cat_totals = {}
    for b in all_filtered:
        c = b.category if b.category in VALID_CATEGORIES else "Other"
        cat_totals[c] = cat_totals.get(c, 0.0) + (b.total or 0.0)

    cat_labels = []
    cat_values = []
    for c in VALID_CATEGORIES:
        if c in cat_totals and cat_totals[c] > 0:
            cat_labels.append(c)
            cat_values.append(round(cat_totals[c], 2))

    # 4. Top Vendors Horizontal Bar Chart Data (Top 8)
    sorted_top_vendors = sorted(vendor_spends.items(), key=lambda x: x[1], reverse=True)[:8]
    vendor_labels = [x[0] for x in sorted_top_vendors]
    vendor_values = [round(x[1], 2) for x in sorted_top_vendors]

    # 5. GST Summary by Month and by Rate
    # Table breakdown by GST Rate
    rate_groups = {0: {"subtotal": 0.0, "gst": 0.0, "count": 0},
                   5: {"subtotal": 0.0, "gst": 0.0, "count": 0},
                   12: {"subtotal": 0.0, "gst": 0.0, "count": 0},
                   18: {"subtotal": 0.0, "gst": 0.0, "count": 0},
                   28: {"subtotal": 0.0, "gst": 0.0, "count": 0},
                   "other": {"subtotal": 0.0, "gst": 0.0, "count": 0}}

    for b in all_filtered:
        r = b.gst_rate
        if r in (0, 0.0):
            target = 0
        elif r in (5, 5.0):
            target = 5
        elif r in (12, 12.0):
            target = 12
        elif r in (18, 18.0):
            target = 18
        elif r in (28, 28.0):
            target = 28
        else:
            target = "other"

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

    # Stacked GST by month (Rate breakdown across months)
    gst_stacked_rates = ["0%", "5%", "12%", "18%", "28%"]
    gst_stacked_by_month = {r: [0.0] * len(sorted_months) for r in gst_stacked_rates}

    for idx, ym in enumerate(sorted_months):
        m_bills = [b for b in all_filtered if b.date and b.date.startswith(ym)]
        for b in m_bills:
            r = int(round(b.gst_rate or 0))
            rk = f"{r}%"
            if rk not in gst_stacked_by_month:
                rk = "18%"  # fallback
            gst_stacked_by_month[rk][idx] += round(b.gst_amount or 0.0, 2)

    # Distinct values for filter dropdowns
    db_all_bills = Bill.query.all()
    distinct_vendors = sorted(list(set((b.vendor or "").strip() for b in db_all_bills if b.vendor)))
    distinct_categories = VALID_CATEGORIES

    min_date = ""
    max_date = ""
    if db_all_bills:
        valid_dates = [b.date for b in db_all_bills if b.date and len(b.date) == 10]
        if valid_dates:
            min_date = min(valid_dates)
            max_date = max(valid_dates)

    return jsonify({
        "success": True,
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
            "top_vendors": {
                "labels": vendor_labels,
                "values": vendor_values,
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
            "categories": distinct_categories,
            "vendors": distinct_vendors,
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
        bill.vendor = str(data["vendor"]).strip() or "Unknown Vendor"
    if "invoice_no" in data:
        bill.invoice_no = str(data["invoice_no"]).strip() or "N/A"
    if "date" in data:
        bill.date = str(data["date"]).strip() or date.today().strftime("%Y-%m-%d")
    if "category" in data:
        cat = str(data["category"]).strip()
        bill.category = cat if cat in VALID_CATEGORIES else "Other"
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
    return jsonify({"success": True, "bill": bill.to_dict()})


@expense_bp.route("/bill/<int:bill_id>", methods=["DELETE"])
def api_delete_bill(bill_id):
    bill = Bill.query.get(bill_id)
    if not bill:
        return jsonify({"success": False, "error": f"Bill #{bill_id} not found."}), 404

    db.session.delete(bill)
    db.session.commit()
    return jsonify({"success": True, "message": f"Bill #{bill_id} deleted successfully."})


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
