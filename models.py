import os
import re
import json
from datetime import datetime, date
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text

db = SQLAlchemy()

DEFAULT_CATEGORIES = [
    "Food",
    "Pins",
    "Fees",
    "Rent",
    "Travel Expenses",
    "Customer Account Expense",
    "Stock Expense",
    "Utilities",
    "Stationery",
    "Other",
]
VALID_CATEGORIES = DEFAULT_CATEGORIES

# Semantic alias dictionary mapping lowercased stems/phrases to canonical category names
CANONICAL_CATEGORY_ALIASES = {
    # 1. Travel Expenses: all travel, conveyance, transport, flights, cabs, fuel, hotels, etc.
    "travel": "Travel Expenses",
    "travels": "Travel Expenses",
    "travel expense": "Travel Expenses",
    "travel expenses": "Travel Expenses",
    "traveling": "Travel Expenses",
    "travelling": "Travel Expenses",
    "travel utility": "Travel Expenses",
    "travel utilities": "Travel Expenses",
    "travel & conveyance": "Travel Expenses",
    "travel and conveyance": "Travel Expenses",
    "travel/conveyance": "Travel Expenses",
    "tour & travel": "Travel Expenses",
    "tour & travels": "Travel Expenses",
    "tour and travel": "Travel Expenses",
    "tours and travels": "Travel Expenses",
    "conveyance": "Travel Expenses",
    "conveyance charges": "Travel Expenses",
    "transport": "Travel Expenses",
    "transportation": "Travel Expenses",
    "cab": "Travel Expenses",
    "cabs": "Travel Expenses",
    "cab fare": "Travel Expenses",
    "taxi": "Travel Expenses",
    "taxi fare": "Travel Expenses",
    "taxi charges": "Travel Expenses",
    "uber": "Travel Expenses",
    "ola": "Travel Expenses",
    "flight": "Travel Expenses",
    "flights": "Travel Expenses",
    "airline": "Travel Expenses",
    "air ticket": "Travel Expenses",
    "air tickets": "Travel Expenses",
    "indigo": "Travel Expenses",
    "air india": "Travel Expenses",
    "vistara": "Travel Expenses",
    "spicejet": "Travel Expenses",
    "train": "Travel Expenses",
    "train ticket": "Travel Expenses",
    "train tickets": "Travel Expenses",
    "railway": "Travel Expenses",
    "irctc": "Travel Expenses",
    "bus": "Travel Expenses",
    "bus fare": "Travel Expenses",
    "bus ticket": "Travel Expenses",
    "petrol": "Travel Expenses",
    "diesel": "Travel Expenses",
    "fuel": "Travel Expenses",
    "fuel expense": "Travel Expenses",
    "toll": "Travel Expenses",
    "toll tax": "Travel Expenses",
    "fastag": "Travel Expenses",
    "hotel": "Travel Expenses",
    "hotel stay": "Travel Expenses",
    "lodging": "Travel Expenses",
    "boarding": "Travel Expenses",
    "mileage": "Travel Expenses",
    "car rental": "Travel Expenses",
    "vehicle rental": "Travel Expenses",

    # 2. Rent
    "rent": "Rent",
    "rental": "Rent",
    "rent expense": "Rent",
    "lease": "Rent",
    "leasehold": "Rent",
    "office rent": "Rent",
    "shop rent": "Rent",
    "godown rent": "Rent",
    "warehouse rent": "Rent",
    "premises rent": "Rent",
    "building rent": "Rent",
    "commercial rent": "Rent",
    "property rent": "Rent",
    "room rent": "Rent",
    "flat rent": "Rent",
    "monthly rent": "Rent",
    "tenancy": "Rent",

    # 3. Utilities
    "utility": "Utilities",
    "utilities": "Utilities",
    "utility expense": "Utilities",
    "utility bill": "Utilities",
    "electricity": "Utilities",
    "electric": "Utilities",
    "electric bill": "Utilities",
    "electricity bill": "Utilities",
    "power": "Utilities",
    "power bill": "Utilities",
    "tata power": "Utilities",
    "torrent power": "Utilities",
    "adani electricity": "Utilities",
    "bescom": "Utilities",
    "msedcl": "Utilities",
    "water": "Utilities",
    "water bill": "Utilities",
    "water charges": "Utilities",
    "broadband": "Utilities",
    "wifi": "Utilities",
    "internet": "Utilities",
    "internet bill": "Utilities",
    "telephone": "Utilities",
    "phone": "Utilities",
    "phone bill": "Utilities",
    "mobile recharge": "Utilities",
    "airtel": "Utilities",
    "jio": "Utilities",
    "vodafone": "Utilities",
    "gas": "Utilities",
    "gas bill": "Utilities",
    "piped gas": "Utilities",
    "lpg": "Utilities",
    "cylinder": "Utilities",
    "sewage": "Utilities",

    # 4. Food
    "food": "Food",
    "food & beverages": "Food",
    "food and beverages": "Food",
    "food expense": "Food",
    "dining": "Food",
    "restaurant": "Food",
    "cafe": "Food",
    "cafeteria": "Food",
    "canteen": "Food",
    "mess": "Food",
    "tiffin": "Food",
    "dhaba": "Food",
    "bhojanalay": "Food",
    "meals": "Food",
    "meal": "Food",
    "breakfast": "Food",
    "lunch": "Food",
    "dinner": "Food",
    "snacks": "Food",
    "tea": "Food",
    "chai": "Food",
    "coffee": "Food",
    "refreshments": "Food",
    "bakery": "Food",
    "sweets": "Food",
    "swiggy": "Food",
    "zomato": "Food",
    "catering": "Food",

    # 5. Stationery
    "stationery": "Stationery",
    "stationary": "Stationery",
    "stationery expense": "Stationery",
    "office stationery": "Stationery",
    "office supplies": "Stationery",
    "printing": "Stationery",
    "printing & stationery": "Stationery",
    "pen": "Stationery",
    "pens": "Stationery",
    "pencil": "Stationery",
    "pencils": "Stationery",
    "marker": "Stationery",
    "markers": "Stationery",
    "notebook": "Stationery",
    "notebooks": "Stationery",
    "notepad": "Stationery",
    "a4 paper": "Stationery",
    "copier paper": "Stationery",
    "envelopes": "Stationery",
    "envelope": "Stationery",
    "files": "Stationery",
    "folders": "Stationery",

    # 6. Pins
    "pin": "Pins",
    "pins": "Pins",
    "paper pin": "Pins",
    "paper pins": "Pins",
    "paper clip": "Pins",
    "paper clips": "Pins",
    "paperclip": "Pins",
    "paperclips": "Pins",
    "u-clip": "Pins",
    "u-clips": "Pins",
    "u clip": "Pins",
    "stapler": "Pins",
    "stapler pin": "Pins",
    "stapler pins": "Pins",
    "staple": "Pins",
    "staple pin": "Pins",
    "staple pins": "Pins",
    "binder clip": "Pins",
    "binder clips": "Pins",
    "push pin": "Pins",
    "push pins": "Pins",
    "board pin": "Pins",
    "board pins": "Pins",
    "safety pin": "Pins",
    "safety pins": "Pins",

    # 7. Fees
    "fee": "Fees",
    "fees": "Fees",
    "legal fee": "Fees",
    "legal fees": "Fees",
    "audit fee": "Fees",
    "audit fees": "Fees",
    "professional fees": "Fees",
    "professional charges": "Fees",
    "consulting fee": "Fees",
    "consulting fees": "Fees",
    "consultancy": "Fees",
    "accounting charges": "Fees",
    "accounting fees": "Fees",
    "certification fee": "Fees",
    "registration fee": "Fees",
    "filing fee": "Fees",
    "license fee": "Fees",
    "court fee": "Fees",
    "bank charges": "Fees",
    "service fee": "Fees",

    # 8. Customer Account Expense
    "customer account": "Customer Account Expense",
    "customer account expense": "Customer Account Expense",
    "client entertainment": "Customer Account Expense",
    "client dinner": "Customer Account Expense",
    "client lunch": "Customer Account Expense",
    "client meeting": "Customer Account Expense",
    "client meeting expense": "Customer Account Expense",
    "customer gift": "Customer Account Expense",
    "customer gifts": "Customer Account Expense",
    "client hospitality": "Customer Account Expense",
    "corporate gift": "Customer Account Expense",
    "corporate gifts": "Customer Account Expense",
    "customer promo": "Customer Account Expense",
    "business gift": "Customer Account Expense",
    "client expense": "Customer Account Expense",
    "pr expense": "Customer Account Expense",

    # 9. Stock Expense
    "stock": "Stock Expense",
    "stock expense": "Stock Expense",
    "inventory": "Stock Expense",
    "inventory expense": "Stock Expense",
    "raw material": "Stock Expense",
    "raw materials": "Stock Expense",
    "cement": "Stock Expense",
    "steel": "Stock Expense",
    "iron": "Stock Expense",
    "brick": "Stock Expense",
    "bricks": "Stock Expense",
    "sand": "Stock Expense",
    "aggregate": "Stock Expense",
    "wholesale goods": "Stock Expense",
    "resale goods": "Stock Expense",
    "hardware materials": "Stock Expense",
    "plywood": "Stock Expense",

    # 10. Other
    "other": "Other",
    "others": "Other",
    "misc": "Other",
    "miscellaneous": "Other",
    "general": "Other",
}


def canonicalize_category(raw_category, active_categories=None):
    """
    Intelligently maps any raw category string, synonym, or variant to a canonical active category.
    Prevents category fragmentation (e.g., 'Travel', 'travel expense', 'travel utilities' -> 'Travel Expenses').
    Never invents new categories or hallucinates.
    """
    if not raw_category:
        return "Other"

    clean = str(raw_category).strip()
    if not clean or clean.lower() in ("null", "none", "n/a", "undefined", ""):
        return "Other"

    clean_lower = clean.lower()

    # 1. Exact case-insensitive match against canonical default categories first
    for def_cat in DEFAULT_CATEGORIES:
        if clean_lower == def_cat.lower():
            return def_cat

    # 2. Check canonical aliases dictionary
    if clean_lower in CANONICAL_CATEGORY_ALIASES:
        return CANONICAL_CATEGORY_ALIASES[clean_lower]

    # 3. Check for word / stem matches in priority order using word boundaries
    # Travel / Conveyance (including 'travel utilities', 'travel expense', 'cab fare', etc.)
    if re.search(r"\b(travel|travels|traveling|travelling|conveyance|transport|flight|flights|airline|airlines|train|trains|railway|irctc|bus|cab|cabs|taxi|taxis|uber|ola|fuel|petrol|diesel|toll|fastag|hotel|lodging|mileage)\b", clean_lower):
        return "Travel Expenses"

    # Rent / Lease
    if re.search(r"\b(rent|rental|lease|leasehold|tenancy)\b", clean_lower):
        return "Rent"

    # Utilities
    if re.search(r"\b(utility|utilities|electricity|electric|power|broadband|wifi|internet|telephone|water bill)\b", clean_lower):
        return "Utilities"

    # Food
    if re.search(r"\b(food|dining|restaurant|cafe|meal|meals|lunch|dinner|breakfast|snack|snacks|tea|coffee|chai)\b", clean_lower):
        return "Food"

    # Stationery
    if re.search(r"\b(stationery|stationary|pen|pens|pencil|pencils|marker|markers|notebook|notebooks|notepad|notepads|paper|printing|folder|folders|envelope|envelopes)\b", clean_lower):
        return "Stationery"

    # Pins
    if re.search(r"\b(pin|pins|clip|clips|paperclip|paperclips|stapler|staplers|staple|staples)\b", clean_lower):
        return "Pins"

    # Fees
    if re.search(r"\b(fee|fees|audit|consulting|consultancy|legal|license)\b", clean_lower):
        return "Fees"

    # Customer Account Expense
    if re.search(r"\b(customer|client|hospitality|corporate gift|pr expense)\b", clean_lower):
        return "Customer Account Expense"

    # Stock Expense
    if re.search(r"\b(stock|inventory|raw material|cement|steel|iron|brick|bricks|sand|aggregate)\b", clean_lower):
        return "Stock Expense"

    # 4. If custom categories exist in active_categories, check if any matches
    if active_categories:
        for ac in active_categories:
            if ac.strip().lower() == clean_lower:
                return ac

    # 5. Default to Other
    return "Other"


# ============================================================
# 0. BUSINESS PROFILE & EXPENSE CATEGORY MODELS (SINGLE VENDOR)
# ============================================================
class BusinessProfile(db.Model):
    __tablename__ = "business_profile"

    id = db.Column(db.Integer, primary_key=True)
    business_name = db.Column(db.String(255), nullable=False, default="R K Construction")
    owner_name = db.Column(db.String(255), nullable=False, default="Business Owner")
    gstin = db.Column(db.String(20), nullable=True)
    phone = db.Column(db.String(50), nullable=True)
    email = db.Column(db.String(255), nullable=True)
    address = db.Column(db.Text, nullable=True)
    notes = db.Column(db.Text, nullable=True)
    updated_at = db.Column(db.DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "business_name": self.business_name or "My Business",
            "owner_name": self.owner_name or "Business Owner",
            "gstin": self.gstin or "",
            "phone": self.phone or "",
            "email": self.email or "",
            "address": self.address or "",
            "notes": self.notes or "",
            "updated_at": self.updated_at.strftime("%Y-%m-%d %H:%M") if self.updated_at else "",
        }


class ExpenseCategory(db.Model):
    __tablename__ = "expense_categories"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True, index=True)
    color = db.Column(db.String(30), default="#0D4C3C")
    is_default = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime(timezone=True), default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "color": self.color or "#0D4C3C",
            "is_default": bool(self.is_default),
        }


def get_business_profile():
    profile = BusinessProfile.query.first()
    if not profile:
        profile = BusinessProfile(
            business_name="R K Construction",
            owner_name="Primary Vendor",
            gstin="24ABCDE1234F1Z5",
        )
        db.session.add(profile)
        db.session.commit()
    return profile


def get_active_categories():
    try:
        db_cats = [c.name for c in ExpenseCategory.query.all()]
        if db_cats:
            # Deduplicate by canonical representation
            seen_canonical = set()
            clean_db_cats = []
            for c in db_cats:
                canonical = canonicalize_category(c)
                effective = canonical if canonical in DEFAULT_CATEGORIES else c
                if effective.lower() not in seen_canonical:
                    seen_canonical.add(effective.lower())
                    clean_db_cats.append(effective)

            sorted_cats = [c for c in DEFAULT_CATEGORIES if c in clean_db_cats]
            custom_cats = [c for c in clean_db_cats if c not in DEFAULT_CATEGORIES]
            return sorted_cats + sorted(custom_cats)
    except Exception:
        pass
    return list(DEFAULT_CATEGORIES)


def add_custom_category(name, color=None):
    if not name or not str(name).strip():
        return False, "Category name cannot be empty."
    clean_name = str(name).strip()

    # Check if this maps to an existing canonical category (e.g. user typed "Travel" or "travel expense")
    canonical = canonicalize_category(clean_name)
    if canonical != "Other":
        existing_cat = ExpenseCategory.query.filter(db.func.lower(ExpenseCategory.name) == canonical.lower()).first()
        if existing_cat:
            # It maps to an existing active category (e.g. "Travel" -> "Travel Expenses")
            return True, existing_cat.to_dict()

    existing = ExpenseCategory.query.filter(db.func.lower(ExpenseCategory.name) == clean_name.lower()).first()
    if existing:
        return True, existing.to_dict()

    cat = ExpenseCategory(name=clean_name.title(), color=color or "#0D4C3C", is_default=False)
    db.session.add(cat)
    db.session.commit()
    return True, cat.to_dict()


def delete_custom_category(name):
    if not name or not str(name).strip():
        return False, "Category name cannot be empty."
    clean_name = str(name).strip().lower()
    cat = ExpenseCategory.query.filter(db.func.lower(ExpenseCategory.name) == clean_name).first()
    if not cat:
        return False, f"Category '{name}' not found."

    # Reassign any bills with this category to 'Other'
    bills = Bill.query.filter(db.func.lower(Bill.category) == clean_name).all()
    for b in bills:
        b.category = "Other"

    db.session.delete(cat)
    db.session.commit()
    return True, f"Category '{cat.name}' removed."


def cleanup_and_merge_duplicate_categories():
    """
    Cleans up any redundant or duplicate categories in the database.
    Specifically merges duplicate aliases like 'Travel' into 'Travel Expenses'.
    Reassigns any existing bills to the canonical category.
    """
    try:
        all_cats = ExpenseCategory.query.all()
        canonical_map = {}
        to_delete = []

        for c in all_cats:
            canonical_name = canonicalize_category(c.name)
            if canonical_name in DEFAULT_CATEGORIES and c.name.lower() != canonical_name.lower():
                canonical_map[c.name.lower()] = canonical_name
                to_delete.append(c)

        # Update all bills with duplicate/alias categories
        bills = Bill.query.all()
        updated_bills = 0
        for b in bills:
            raw_c = (b.category or "").strip()
            canonical_for_bill = canonicalize_category(raw_c)
            if raw_c.lower() in canonical_map:
                b.category = canonical_map[raw_c.lower()]
                updated_bills += 1
            elif canonical_for_bill and canonical_for_bill != raw_c:
                b.category = canonical_for_bill
                updated_bills += 1

        for dup in to_delete:
            db.session.delete(dup)

        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        print("cleanup_and_merge_duplicate_categories error:", exc)


def seed_default_categories():
    try:
        existing = {c.name.lower() for c in ExpenseCategory.query.all()}
        for def_cat in DEFAULT_CATEGORIES:
            if def_cat.lower() not in existing:
                cat = ExpenseCategory(name=def_cat, is_default=True)
                db.session.add(cat)
        db.session.commit()
        # Automatically clean up duplicate/alias categories like 'Travel' -> 'Travel Expenses'
        cleanup_and_merge_duplicate_categories()
    except Exception as exc:
        db.session.rollback()
        print("seed_default_categories error:", exc)



# ============================================================
# 1. VENDOR MASTER MODEL
# ============================================================
class Vendor(db.Model):
    __tablename__ = "vendors"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False, unique=True, index=True)
    gstin = db.Column(db.String(15), nullable=True, index=True)
    pan = db.Column(db.String(10), nullable=True)
    email = db.Column(db.String(255), nullable=True)
    phone = db.Column(db.String(50), nullable=True)
    category = db.Column(db.String(50), default="Other")
    address = db.Column(db.Text, nullable=True)
    is_verified = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime(timezone=True), default=datetime.utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    bills = db.relationship("Bill", backref="vendor_rel", lazy="select")

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "gstin": self.gstin or "",
            "pan": self.pan or "",
            "email": self.email or "",
            "phone": self.phone or "",
            "category": self.category or "Other",
            "address": self.address or "",
            "is_verified": bool(self.is_verified),
            "bills_count": len(self.bills) if self.bills else 0,
            "total_spend": round(sum(b.total or 0.0 for b in self.bills), 2) if self.bills else 0.0,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M") if self.created_at else "",
        }


# ============================================================
# 2. INVOICE & BILL MODEL
# ============================================================
class Bill(db.Model):
    __tablename__ = "bills"

    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)
    file_path = db.Column(db.String(512), nullable=True)
    preview_url = db.Column(db.String(512), nullable=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey("vendors.id", ondelete="SET NULL"), nullable=True, index=True)
    vendor = db.Column(db.String(255), nullable=True, index=True)
    invoice_no = db.Column(db.String(100), nullable=True, index=True)
    date = db.Column(db.String(20), nullable=True, index=True)  # YYYY-MM-DD
    invoice_date = db.Column(db.Date, nullable=True)
    currency = db.Column(db.String(10), default="INR")
    subtotal = db.Column(db.Float, default=0.0)
    cgst_amount = db.Column(db.Float, default=0.0)
    sgst_amount = db.Column(db.Float, default=0.0)
    igst_amount = db.Column(db.Float, default=0.0)
    gst_amount = db.Column(db.Float, default=0.0)
    gst_rate = db.Column(db.Float, default=0.0)
    total = db.Column(db.Float, nullable=False, default=0.0)
    category = db.Column(db.String(50), nullable=False, default="Other", index=True)
    payment_status = db.Column(db.String(30), default="Unpaid")
    validation_status = db.Column(db.String(30), default="OK", index=True)  # 'OK', 'Needs review', 'Rejected'
    validation_notes = db.Column(db.String(500), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    raw_json = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=datetime.utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    line_items = db.relationship(
        "LineItem", backref="bill", cascade="all, delete-orphan", lazy="joined"
    )

    def to_dict(self):
        # Auto-compute validation status if not explicitly set
        sub = round(float(self.subtotal or 0.0), 2)
        gst = round(float(self.gst_amount or 0.0), 2)
        tot = round(float(self.total or 0.0), 2)
        diff = abs(round((sub + gst) - tot, 2))
        val_status = self.validation_status or ("Needs review" if diff > 0.05 else "OK")
        val_notes = self.validation_notes or (
            f"Differs by ₹{diff:.2f}; suggested GST is ₹{max(0.0, tot - sub):.2f}"
            if diff > 0.05 else "Verified: Subtotal + GST = Total"
        )

        return {
            "id": self.id,
            "filename": self.filename,
            "file_path": self.file_path,
            "preview_url": self.preview_url,
            "vendor_id": self.vendor_id,
            "vendor": self.vendor or "Unknown Vendor",
            "invoice_no": self.invoice_no or "N/A",
            "date": self.date or "",
            "currency": self.currency or "INR",
            "subtotal": sub,
            "cgst_amount": round(float(self.cgst_amount or 0.0), 2),
            "sgst_amount": round(float(self.sgst_amount or 0.0), 2),
            "igst_amount": round(float(self.igst_amount or 0.0), 2),
            "gst_amount": gst,
            "gst_rate": round(float(self.gst_rate or 0.0), 2),
            "total": tot,
            "category": self.category or "Other",
            "payment_status": self.payment_status or "Unpaid",
            "validation_status": val_status,
            "validation_notes": val_notes,
            "notes": self.notes or "",
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M") if self.created_at else "",
            "line_items": [item.to_dict() for item in self.line_items],
        }


# ============================================================
# 3. LINE ITEM MODEL
# ============================================================
class LineItem(db.Model):
    __tablename__ = "line_items"

    id = db.Column(db.Integer, primary_key=True)
    bill_id = db.Column(db.Integer, db.ForeignKey("bills.id", ondelete="CASCADE"), nullable=False, index=True)
    item_code = db.Column(db.String(50), nullable=True)
    name = db.Column(db.String(255), nullable=False)
    qty = db.Column(db.Float, default=1.0)
    unit = db.Column(db.String(20), default="PCS")
    price = db.Column(db.Float, default=0.0)
    tax_rate = db.Column(db.Float, default=0.0)
    total = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime(timezone=True), default=datetime.utcnow)

    def to_dict(self):
        q = float(self.qty or 1.0)
        if q <= 0:
            q = 1.0
        tot = round(float(self.total or 0.0), 2)
        pr = round(float(self.price or 0.0), 2)
        if (pr == 0.0 or pr is None) and tot > 0:
            pr = round(tot / q, 2)
        elif tot == 0.0 and pr > 0:
            tot = round(q * pr, 2)

        return {
            "id": self.id,
            "item_code": self.item_code or "",
            "name": self.name or "Item",
            "qty": round(q, 2),
            "unit": self.unit or "PCS",
            "price": pr,
            "tax_rate": round(float(self.tax_rate or 0.0), 2),
            "total": tot,
        }


# ============================================================
# 4. DOCUMENT SCAN & VERIFICATION AUDIT MODEL
# ============================================================
class DocumentScan(db.Model):
    __tablename__ = "document_scans"

    id = db.Column(db.Integer, primary_key=True)
    document_type = db.Column(db.String(100), nullable=False, index=True)
    document_name = db.Column(db.String(255), nullable=True)
    source_filename = db.Column(db.String(255), nullable=False)
    storage_path = db.Column(db.String(512), nullable=True)
    preview_url = db.Column(db.String(512), nullable=True)
    page_count = db.Column(db.Integer, default=1)
    document_header = db.Column(db.String(255), nullable=True)
    issuing_entity = db.Column(db.String(255), nullable=True)
    overall_status = db.Column(db.String(50), nullable=False, default="PASSED", index=True)
    passed_count = db.Column(db.Integer, default=0)
    total_fields = db.Column(db.Integer, default=0)
    match_percentage = db.Column(db.Float, default=0.0)
    image_quality_passed = db.Column(db.Boolean, default=True)
    image_quality_details = db.Column(db.Text, nullable=True)
    report_text = db.Column(db.Text, nullable=True)
    summary_json = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=datetime.utcnow, index=True)

    field_results = db.relationship(
        "ScanFieldResult", backref="scan", cascade="all, delete-orphan", lazy="joined"
    )

    def to_dict(self):
        summary = {}
        if self.summary_json:
            try:
                summary = json.loads(self.summary_json)
            except Exception:
                pass
        
        quality = {}
        if self.image_quality_details:
            try:
                quality = json.loads(self.image_quality_details)
            except Exception:
                pass

        return {
            "id": self.id,
            "document_type": self.document_type,
            "document_name": self.document_name or self.source_filename,
            "source_filename": self.source_filename,
            "storage_path": self.storage_path,
            "preview_url": self.preview_url,
            "page_count": self.page_count,
            "document_header": self.document_header or "",
            "issuing_entity": self.issuing_entity or "",
            "overall_status": self.overall_status,
            "passed_count": self.passed_count,
            "total_fields": self.total_fields,
            "match_percentage": round(float(self.match_percentage or 0.0), 1),
            "image_quality_passed": self.image_quality_passed,
            "image_quality": quality,
            "summary": summary,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M:%S") if self.created_at else "",
            "fields": [f.to_dict() for f in self.field_results],
        }


# ============================================================
# 5. SCAN FIELD RESULT EVIDENCE MODEL
# ============================================================
class ScanFieldResult(db.Model):
    __tablename__ = "scan_field_results"

    id = db.Column(db.Integer, primary_key=True)
    scan_id = db.Column(db.Integer, db.ForeignKey("document_scans.id", ondelete="CASCADE"), nullable=False, index=True)
    field_key = db.Column(db.String(100), nullable=True)
    field_name = db.Column(db.String(255), nullable=False)
    constraint_type = db.Column(db.String(50), nullable=False, default="fuzzy")
    constraint_label = db.Column(db.String(100), nullable=True)
    expected_value = db.Column(db.Text, nullable=True)
    extracted_value = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(30), nullable=False, default="PASS")
    confidence = db.Column(db.String(20), nullable=True)
    page = db.Column(db.Integer, default=1)
    snippet = db.Column(db.Text, nullable=True)
    evaluation_detail = db.Column(db.Text, nullable=True)
    is_required = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime(timezone=True), default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "scan_id": self.scan_id,
            "field": self.field_name,
            "field_key": self.field_key or "",
            "constraint": self.constraint_type,
            "constraint_label": self.constraint_label or self.constraint_type.capitalize(),
            "user_value": self.expected_value or "",
            "extracted_value": self.extracted_value or "",
            "status": self.status,
            "confidence": self.confidence or "MEDIUM",
            "page": self.page,
            "snippet": self.snippet or "",
            "detail": self.evaluation_detail or "",
            "required": self.is_required,
        }


# ============================================================
# 6. ENTERPRISE ACTIVITY & AUDIT LOG MODEL
# ============================================================
class AuditLog(db.Model):
    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    action = db.Column(db.String(50), nullable=False, index=True)
    entity_type = db.Column(db.String(50), nullable=False, index=True)
    entity_id = db.Column(db.Integer, nullable=True)
    summary = db.Column(db.String(255), nullable=False)
    payload_json = db.Column(db.Text, nullable=True)
    ip_address = db.Column(db.String(45), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=datetime.utcnow, index=True)

    def to_dict(self):
        payload = {}
        if self.payload_json:
            try:
                payload = json.loads(self.payload_json)
            except Exception:
                pass
        return {
            "id": self.id,
            "action": self.action,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "summary": self.summary,
            "payload": payload,
            "ip_address": self.ip_address or "",
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M:%S") if self.created_at else "",
        }


# ============================================================
# UTILITIES & HELPERS
# ============================================================

def log_audit(action, entity_type=None, entity_id=None, summary=None, payload=None, ip_address=None, **kwargs):
    """Convenience helper to record audit logs."""
    try:
        if "ip" in kwargs and not ip_address:
            ip_address = kwargs["ip"]
        if "payload_data" in kwargs and payload is None:
            payload = kwargs["payload_data"]

        payload_str = json.dumps(payload) if payload is not None and not isinstance(payload, str) else (payload or None)
        log = AuditLog(
            action=str(action or ""),
            entity_type=str(entity_type or ""),
            entity_id=entity_id if isinstance(entity_id, int) else None,
            summary=str(summary or ""),
            payload_json=payload_str,
            ip_address=str(ip_address) if ip_address else None,
            created_at=datetime.utcnow(),
        )
        db.session.add(log)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        print(f"Audit log warning: {exc}")


def get_or_create_vendor(vendor_name, category=None, gstin=None, pan=None):
    """
    Find existing vendor by name (case-insensitive) or create new vendor record.
    """
    if not vendor_name or not str(vendor_name).strip():
        return None

    clean_name = str(vendor_name).strip()
    # Check existing
    vendor = Vendor.query.filter(db.func.lower(Vendor.name) == clean_name.lower()).first()
    if not vendor:
        vendor = Vendor(
            name=clean_name,
            category=category if category in VALID_CATEGORIES else "Other",
            gstin=str(gstin).strip().upper() if gstin else None,
            pan=str(pan).strip().upper() if pan else None,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.session.add(vendor)
        db.session.flush()
    else:
        # Update category if changed
        if category and category in VALID_CATEGORIES and vendor.category == "Other":
            vendor.category = category
        if gstin and not vendor.gstin:
            vendor.gstin = str(gstin).strip().upper()
        if pan and not vendor.pan:
            vendor.pan = str(pan).strip().upper()
        vendor.updated_at = datetime.utcnow()

    return vendor


def ensure_db_schema():
    """Ensure database schema is up-to-date across PostgreSQL or SQLite."""
    try:
        db.create_all()
        seed_default_categories()
        get_business_profile()
        insp = inspect(db.engine)
        if "bills" in insp.get_table_names():
            existing_cols = [c["name"] for c in insp.get_columns("bills")]
            with db.engine.connect() as conn:
                if "cgst_amount" not in existing_cols:
                    conn.execute(text("ALTER TABLE bills ADD COLUMN cgst_amount FLOAT DEFAULT 0.0"))
                if "sgst_amount" not in existing_cols:
                    conn.execute(text("ALTER TABLE bills ADD COLUMN sgst_amount FLOAT DEFAULT 0.0"))
                if "igst_amount" not in existing_cols:
                    conn.execute(text("ALTER TABLE bills ADD COLUMN igst_amount FLOAT DEFAULT 0.0"))
                if "validation_status" not in existing_cols:
                    conn.execute(text("ALTER TABLE bills ADD COLUMN validation_status VARCHAR(30) DEFAULT 'OK'"))
                if "validation_notes" not in existing_cols:
                    conn.execute(text("ALTER TABLE bills ADD COLUMN validation_notes VARCHAR(500) DEFAULT ''"))
                if "vendor_id" not in existing_cols:
                    conn.execute(text("ALTER TABLE bills ADD COLUMN vendor_id INTEGER"))
                if "invoice_date" not in existing_cols:
                    conn.execute(text("ALTER TABLE bills ADD COLUMN invoice_date DATE"))
                if "currency" not in existing_cols:
                    conn.execute(text("ALTER TABLE bills ADD COLUMN currency VARCHAR(10) DEFAULT 'INR'"))
                if "payment_status" not in existing_cols:
                    conn.execute(text("ALTER TABLE bills ADD COLUMN payment_status VARCHAR(30) DEFAULT 'Unpaid'"))
                if "updated_at" not in existing_cols:
                    conn.execute(text("ALTER TABLE bills ADD COLUMN updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP"))
                conn.commit()
    except Exception as exc:
        print("Schema migration note:", exc)


def find_duplicate_bill(vendor, invoice_no, total, exclude_id=None):
    """
    Detect duplicates by vendor + invoice_no + total.
    Returns the matching Bill instance or None.
    """
    if not vendor or not invoice_no or total is None:
        return None

    clean_vendor = str(vendor).strip().lower()
    clean_inv = str(invoice_no).strip().lower()

    try:
        total_float = float(total)
    except (ValueError, TypeError):
        return None

    query = Bill.query
    if exclude_id:
        query = query.filter(Bill.id != exclude_id)

    candidates = query.filter(
        db.func.lower(Bill.vendor) == clean_vendor,
        db.func.lower(Bill.invoice_no) == clean_inv,
    ).all()

    for b in candidates:
        if b.total is not None and abs(float(b.total) - total_float) < 1.0:
            return b

    return None
