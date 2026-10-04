import os
import sqlite3
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime

SQLITE_PATH = "expenses.db"
PG_DSN = os.environ.get("DATABASE_URL", "postgresql://postgres:root@localhost:5432/ai_document_scanning")

def migrate():
    print(f"Starting migration from {SQLITE_PATH} to PostgreSQL ({PG_DSN})...")
    
    if not os.path.exists(SQLITE_PATH):
        print(f"Warning: {SQLITE_PATH} not found. Skipping migration.")
        return

    sqlite_conn = sqlite3.connect(SQLITE_PATH)
    sqlite_conn.row_factory = sqlite3.Row
    sqlite_cur = sqlite_conn.cursor()

    pg_conn = psycopg2.connect(PG_DSN)
    pg_cur = pg_conn.cursor(cursor_factory=RealDictCursor)

    # 1. Fetch bills from SQLite
    sqlite_cur.execute("SELECT * FROM bills ORDER BY id ASC")
    bills = [dict(r) for r in sqlite_cur.fetchall()]
    print(f"Found {len(bills)} bills in SQLite.")

    # 2. Extract and create vendors
    vendor_map = {} # vendor_name -> vendor_id
    for b in bills:
        v_name = (b.get("vendor") or "Unknown Vendor").strip()
        if v_name and v_name not in vendor_map:
            cat = b.get("category") or "Other"
            pg_cur.execute(
                """
                INSERT INTO vendors (name, category, created_at, updated_at)
                VALUES (%s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT (name) DO UPDATE SET updated_at = CURRENT_TIMESTAMP
                RETURNING id;
                """,
                (v_name, cat)
            )
            vendor_id = pg_cur.fetchone()["id"]
            vendor_map[v_name] = vendor_id

    print(f"Created/Mapped {len(vendor_map)} vendors in PostgreSQL.")

    # 3. Insert bills into PostgreSQL
    bill_id_map = {} # sqlite_id -> pg_id
    for b in bills:
        v_name = (b.get("vendor") or "Unknown Vendor").strip()
        v_id = vendor_map.get(v_name)
        
        # parse invoice_date
        date_str = b.get("date") or ""
        inv_date = None
        if date_str:
            try:
                inv_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                pass

        created_at = b.get("created_at")
        if isinstance(created_at, str):
            try:
                created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            except Exception:
                created_at = datetime.utcnow()
        elif not created_at:
            created_at = datetime.utcnow()

        pg_cur.execute(
            """
            INSERT INTO bills (
                id, filename, file_path, preview_url, vendor_id, vendor, invoice_no,
                date, invoice_date, currency, subtotal, cgst_amount, sgst_amount,
                igst_amount, gst_amount, gst_rate, total, category, payment_status,
                validation_status, validation_notes, notes, raw_json, created_at, updated_at
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s,
                %s, %s, 'INR', %s, %s, %s,
                %s, %s, %s, %s, %s, 'Unpaid',
                %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (id) DO UPDATE SET
                filename = EXCLUDED.filename,
                vendor = EXCLUDED.vendor,
                total = EXCLUDED.total,
                updated_at = CURRENT_TIMESTAMP
            RETURNING id;
            """,
            (
                b["id"],
                b.get("filename") or "uploaded_bill",
                b.get("file_path") or "",
                b.get("preview_url") or "",
                v_id,
                v_name,
                b.get("invoice_no") or "",
                date_str,
                inv_date,
                float(b.get("subtotal") or 0.0),
                float(b.get("cgst_amount") or 0.0),
                float(b.get("sgst_amount") or 0.0),
                float(b.get("igst_amount") or 0.0),
                float(b.get("gst_amount") or 0.0),
                float(b.get("gst_rate") or 0.0),
                float(b.get("total") or 0.0),
                b.get("category") or "Other",
                b.get("validation_status") or "OK",
                b.get("validation_notes") or "",
                b.get("notes") or "",
                b.get("raw_json") or "",
                created_at,
                created_at,
            )
        )
        pg_bill_id = pg_cur.fetchone()["id"]
        bill_id_map[b["id"]] = pg_bill_id

    # Reset sequence for bills.id so new serial inserts start after MAX(id)
    pg_cur.execute("SELECT setval('bills_id_seq', COALESCE((SELECT MAX(id) FROM bills), 1), true);")

    # 4. Fetch line items from SQLite
    sqlite_cur.execute("SELECT * FROM line_items ORDER BY id ASC")
    line_items = [dict(r) for r in sqlite_cur.fetchall()]
    print(f"Found {len(line_items)} line items in SQLite.")

    # 5. Insert line items into PostgreSQL
    for it in line_items:
        old_bill_id = it.get("bill_id")
        target_bill_id = bill_id_map.get(old_bill_id)
        if not target_bill_id:
            continue

        pg_cur.execute(
            """
            INSERT INTO line_items (
                id, bill_id, name, qty, unit, price, total, created_at
            )
            VALUES (%s, %s, %s, %s, 'PCS', %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name,
                qty = EXCLUDED.qty,
                price = EXCLUDED.price,
                total = EXCLUDED.total;
            """,
            (
                it["id"],
                target_bill_id,
                it.get("name") or "Item",
                float(it.get("qty") or 1.0),
                float(it.get("price") or 0.0),
                float(it.get("total") or 0.0),
            )
        )

    # Reset sequence for line_items.id
    pg_cur.execute("SELECT setval('line_items_id_seq', COALESCE((SELECT MAX(id) FROM line_items), 1), true);")

    # 6. Insert audit log
    pg_cur.execute(
        """
        INSERT INTO audit_logs (action, entity_type, summary, payload_json, created_at)
        VALUES ('DATA_MIGRATION', 'system', %s, %s, CURRENT_TIMESTAMP);
        """,
        (
            f"Successfully migrated {len(bills)} bills and {len(line_items)} line items from SQLite to PostgreSQL.",
            f'{{"bills_count": {len(bills)}, "line_items_count": {len(line_items)}, "vendors_count": {len(vendor_map)}}}'
        )
    )

    pg_conn.commit()
    sqlite_conn.close()
    pg_conn.close()
    print("Migration successfully completed and verified in PostgreSQL!")

if __name__ == "__main__":
    migrate()
