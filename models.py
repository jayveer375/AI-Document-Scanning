import os
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

VALID_CATEGORIES = ["Food", "Travel", "Utilities", "Stationery", "Inventory", "Other"]


class Bill(db.Model):
    __tablename__ = "bills"

    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)
    file_path = db.Column(db.String(512), nullable=True)
    preview_url = db.Column(db.String(512), nullable=True)
    vendor = db.Column(db.String(255), nullable=True)
    invoice_no = db.Column(db.String(100), nullable=True)
    date = db.Column(db.String(20), nullable=True)  # Stored as YYYY-MM-DD
    subtotal = db.Column(db.Float, default=0.0)
    cgst_amount = db.Column(db.Float, default=0.0)
    sgst_amount = db.Column(db.Float, default=0.0)
    igst_amount = db.Column(db.Float, default=0.0)
    gst_amount = db.Column(db.Float, default=0.0)
    gst_rate = db.Column(db.Float, default=0.0)
    total = db.Column(db.Float, nullable=False, default=0.0)
    category = db.Column(db.String(50), nullable=False, default="Other")
    validation_status = db.Column(db.String(30), default="OK")  # 'OK' or 'Needs review'
    validation_notes = db.Column(db.String(255), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    raw_json = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

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
            "vendor": self.vendor or "Unknown Vendor",
            "invoice_no": self.invoice_no or "N/A",
            "date": self.date or "",
            "subtotal": sub,
            "cgst_amount": round(float(self.cgst_amount or 0.0), 2),
            "sgst_amount": round(float(self.sgst_amount or 0.0), 2),
            "igst_amount": round(float(self.igst_amount or 0.0), 2),
            "gst_amount": gst,
            "gst_rate": round(float(self.gst_rate or 0.0), 2),
            "total": tot,
            "category": self.category if self.category in VALID_CATEGORIES else "Other",
            "validation_status": val_status,
            "validation_notes": val_notes,
            "notes": self.notes or "",
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M") if self.created_at else "",
            "line_items": [item.to_dict() for item in self.line_items],
        }


class LineItem(db.Model):
    __tablename__ = "line_items"

    id = db.Column(db.Integer, primary_key=True)
    bill_id = db.Column(db.Integer, db.ForeignKey("bills.id"), nullable=False)
    name = db.Column(db.String(255), nullable=False)
    qty = db.Column(db.Float, default=1.0)
    price = db.Column(db.Float, default=0.0)
    total = db.Column(db.Float, default=0.0)

    def to_dict(self):
        q = float(self.qty or 1.0)
        if q <= 0:
            q = 1.0
        tot = round(float(self.total or 0.0), 2)
        pr = round(float(self.price or 0.0), 2)
        # If price is missing or 0 but line amount is present, set price = amount / qty
        if (pr == 0.0 or pr is None) and tot > 0:
            pr = round(tot / q, 2)
        elif tot == 0.0 and pr > 0:
            tot = round(q * pr, 2)

        return {
            "id": self.id,
            "name": self.name or "Item",
            "qty": round(q, 2),
            "price": pr,
            "total": tot,
        }


def ensure_db_schema():
    """Ensure database schema is up-to-date with new columns."""
    try:
        from sqlalchemy import text
        with db.engine.connect() as conn:
            # Check bills columns
            res = conn.execute(text("PRAGMA table_info(bills)")).fetchall()
            existing_cols = [r[1] for r in res] if res else []
            if existing_cols:
                if "cgst_amount" not in existing_cols:
                    conn.execute(text("ALTER TABLE bills ADD COLUMN cgst_amount FLOAT DEFAULT 0.0"))
                if "sgst_amount" not in existing_cols:
                    conn.execute(text("ALTER TABLE bills ADD COLUMN sgst_amount FLOAT DEFAULT 0.0"))
                if "igst_amount" not in existing_cols:
                    conn.execute(text("ALTER TABLE bills ADD COLUMN igst_amount FLOAT DEFAULT 0.0"))
                if "validation_status" not in existing_cols:
                    conn.execute(text("ALTER TABLE bills ADD COLUMN validation_status VARCHAR(30) DEFAULT 'OK'"))
                if "validation_notes" not in existing_cols:
                    conn.execute(text("ALTER TABLE bills ADD COLUMN validation_notes VARCHAR(255) DEFAULT ''"))
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

    candidates = query.all()
    for b in candidates:
        if not b.vendor or not b.invoice_no or b.total is None:
            continue
        if (
            b.vendor.strip().lower() == clean_vendor
            and b.invoice_no.strip().lower() == clean_inv
            and abs(float(b.total) - total_float) < 1.0
        ):
            return b

    return None
