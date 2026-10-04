-- ============================================================
-- Enterprise AI Document Scanning & Forensic Audit Database
-- Database: ai_document_scanning
-- RDBMS: PostgreSQL 18.x
-- ============================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- 1. VENDORS MASTER TABLE
CREATE TABLE IF NOT EXISTS vendors (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL UNIQUE,
    gstin VARCHAR(15),
    pan VARCHAR(10),
    email VARCHAR(255),
    phone VARCHAR(50),
    category VARCHAR(50) DEFAULT 'Other',
    address TEXT,
    is_verified BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_vendors_name ON vendors (name);
CREATE INDEX IF NOT EXISTS ix_vendors_gstin ON vendors (gstin);

-- 2. BILLS / INVOICES TABLE
CREATE TABLE IF NOT EXISTS bills (
    id SERIAL PRIMARY KEY,
    filename VARCHAR(255) NOT NULL,
    file_path VARCHAR(512),
    preview_url VARCHAR(512),
    vendor_id INTEGER REFERENCES vendors(id) ON DELETE SET NULL,
    vendor VARCHAR(255),
    invoice_no VARCHAR(100),
    date VARCHAR(20),
    invoice_date DATE,
    currency VARCHAR(10) DEFAULT 'INR',
    subtotal NUMERIC(12, 2) DEFAULT 0.00,
    cgst_amount NUMERIC(12, 2) DEFAULT 0.00,
    sgst_amount NUMERIC(12, 2) DEFAULT 0.00,
    igst_amount NUMERIC(12, 2) DEFAULT 0.00,
    gst_amount NUMERIC(12, 2) DEFAULT 0.00,
    gst_rate NUMERIC(5, 2) DEFAULT 0.00,
    total NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    category VARCHAR(50) NOT NULL DEFAULT 'Other',
    payment_status VARCHAR(30) DEFAULT 'Unpaid',
    validation_status VARCHAR(30) DEFAULT 'OK',
    validation_notes VARCHAR(500),
    notes TEXT,
    raw_json TEXT,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_bills_vendor ON bills (vendor);
CREATE INDEX IF NOT EXISTS ix_bills_vendor_id ON bills (vendor_id);
CREATE INDEX IF NOT EXISTS ix_bills_invoice_no ON bills (invoice_no);
CREATE INDEX IF NOT EXISTS ix_bills_date ON bills (date);
CREATE INDEX IF NOT EXISTS ix_bills_category ON bills (category);
CREATE INDEX IF NOT EXISTS ix_bills_validation_status ON bills (validation_status);

-- 3. LINE ITEMS TABLE
CREATE TABLE IF NOT EXISTS line_items (
    id SERIAL PRIMARY KEY,
    bill_id INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    item_code VARCHAR(50),
    name VARCHAR(255) NOT NULL,
    qty NUMERIC(10, 2) DEFAULT 1.00,
    unit VARCHAR(20) DEFAULT 'PCS',
    price NUMERIC(12, 2) DEFAULT 0.00,
    tax_rate NUMERIC(5, 2) DEFAULT 0.00,
    total NUMERIC(12, 2) DEFAULT 0.00,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_line_items_bill_id ON line_items (bill_id);

-- 4. DOCUMENT SCANS (FORENSIC VERIFICATION AUDIT TRAIL)
CREATE TABLE IF NOT EXISTS document_scans (
    id SERIAL PRIMARY KEY,
    document_type VARCHAR(100) NOT NULL,
    document_name VARCHAR(255),
    source_filename VARCHAR(255) NOT NULL,
    storage_path VARCHAR(512),
    preview_url VARCHAR(512),
    page_count INTEGER DEFAULT 1,
    document_header VARCHAR(255),
    issuing_entity VARCHAR(255),
    overall_status VARCHAR(50) NOT NULL DEFAULT 'PASSED',
    passed_count INTEGER DEFAULT 0,
    total_fields INTEGER DEFAULT 0,
    match_percentage NUMERIC(5, 2) DEFAULT 0.00,
    image_quality_passed BOOLEAN DEFAULT TRUE,
    image_quality_details TEXT,
    report_text TEXT,
    summary_json TEXT,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_doc_scans_type ON document_scans (document_type);
CREATE INDEX IF NOT EXISTS ix_doc_scans_status ON document_scans (overall_status);
CREATE INDEX IF NOT EXISTS ix_doc_scans_created_at ON document_scans (created_at DESC);

-- 5. SCAN FIELD RESULTS (EVIDENCE BREAKDOWN)
CREATE TABLE IF NOT EXISTS scan_field_results (
    id SERIAL PRIMARY KEY,
    scan_id INTEGER NOT NULL REFERENCES document_scans(id) ON DELETE CASCADE,
    field_key VARCHAR(100),
    field_name VARCHAR(255) NOT NULL,
    constraint_type VARCHAR(50) NOT NULL DEFAULT 'fuzzy',
    constraint_label VARCHAR(100),
    expected_value TEXT,
    extracted_value TEXT,
    status VARCHAR(30) NOT NULL DEFAULT 'PASS',
    confidence VARCHAR(20),
    page INTEGER DEFAULT 1,
    snippet TEXT,
    evaluation_detail TEXT,
    is_required BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_scan_fields_scan_id ON scan_field_results (scan_id);

-- 6. AUDIT LOGS (ENTERPRISE ACTIVITY & COMPLIANCE LOG)
CREATE TABLE IF NOT EXISTS audit_logs (
    id SERIAL PRIMARY KEY,
    action VARCHAR(50) NOT NULL,
    entity_type VARCHAR(50) NOT NULL,
    entity_id INTEGER,
    summary VARCHAR(255) NOT NULL,
    payload_json TEXT,
    ip_address VARCHAR(45),
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_audit_action ON audit_logs (action);
CREATE INDEX IF NOT EXISTS ix_audit_entity ON audit_logs (entity_type, entity_id);
CREATE INDEX IF NOT EXISTS ix_audit_created_at ON audit_logs (created_at DESC);
