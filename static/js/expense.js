/**
 * DocuVerify - Invoice & Expense Tracker Controller
 * Handles Multi-file uploads, Mistral Vision extraction review,
 * SQLAlchemy database persistence, Chart.js analytics, and filtering.
 */

let queuedFiles = [];
let reviewedBillsState = [];
let chartInstances = {};
let searchDebounceTimer = null;

// Currency Formatter for Indian Rupee System (e.g. ₹1,25,000)
function formatINR(val) {
    if (val === null || val === undefined || isNaN(val)) return "₹0";
    return new Intl.NumberFormat("en-IN", {
        style: "currency",
        currency: "INR",
        maximumFractionDigits: 2,
        minimumFractionDigits: 0,
    }).format(val);
}

function escapeHtml(str) {
    if (!str) return "";
    return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

document.addEventListener("DOMContentLoaded", () => {
    setupExpenseDropzone();
    setupExpenseUploadForm();
    loadDashboardData();
});

// --------------------------------------------------
// 1. MULTI-FILE QUEUE & UPLOAD SETUP
// --------------------------------------------------

function setupExpenseDropzone() {
    const dropzone = document.getElementById("expenseDropzone");
    const fileInput = document.getElementById("expenseFileInput");
    if (!dropzone || !fileInput) return;

    dropzone.addEventListener("click", () => fileInput.click());

    dropzone.addEventListener("dragover", (e) => {
        e.preventDefault();
        dropzone.classList.add("dragover");
    });

    dropzone.addEventListener("dragleave", () => {
        dropzone.classList.remove("dragover");
    });

    dropzone.addEventListener("drop", (e) => {
        e.preventDefault();
        dropzone.classList.remove("dragover");
        if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
            addFilesToQueue(Array.from(e.dataTransfer.files));
        }
    });

    fileInput.addEventListener("change", (e) => {
        if (e.target.files && e.target.files.length > 0) {
            addFilesToQueue(Array.from(e.target.files));
            fileInput.value = ""; // reset for next pick
        }
    });
}

function addFilesToQueue(files) {
    const validExtensions = ["pdf", "jpg", "jpeg", "png", "webp"];
    let addedCount = 0;

    files.forEach(file => {
        const ext = file.name.split(".").pop().toLowerCase();
        if (validExtensions.includes(ext)) {
            // Check if already in queue
            const alreadyExists = queuedFiles.some(f => f.name === file.name && f.size === file.size);
            if (!alreadyExists) {
                queuedFiles.push(file);
                addedCount++;
            }
        }
    });

    renderQueueGrid();
}

function removeQueuedFile(index) {
    if (index >= 0 && index < queuedFiles.length) {
        queuedFiles.splice(index, 1);
        renderQueueGrid();
    }
}

function clearExpenseQueue() {
    queuedFiles = [];
    renderQueueGrid();
}

function renderQueueGrid() {
    const wrapper = document.getElementById("expenseQueueWrapper");
    const grid = document.getElementById("expenseQueueGrid");
    const badge = document.getElementById("queueCountBadge");
    if (!wrapper || !grid || !badge) return;

    if (queuedFiles.length === 0) {
        wrapper.style.display = "none";
        grid.innerHTML = "";
        return;
    }

    wrapper.style.display = "block";
    badge.textContent = `${queuedFiles.length} ${queuedFiles.length === 1 ? 'Bill Selected' : 'Bills Selected'}`;
    grid.innerHTML = "";

    queuedFiles.forEach((file, idx) => {
        const card = document.createElement("div");
        card.className = "queue-file-card";

        const isImg = file.type.startsWith("image/");
        let thumbHtml = "";
        if (isImg) {
            const url = URL.createObjectURL(file);
            thumbHtml = `<img src="${url}" alt="${escapeHtml(file.name)}">`;
        } else {
            thumbHtml = `
                <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="#BA3939" stroke-width="2">
                    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
                    <polyline points="14 2 14 8 20 8"></polyline>
                </svg>
            `;
        }

        card.innerHTML = `
            <div class="queue-file-thumb">${thumbHtml}</div>
            <div class="queue-file-meta">
                <div class="queue-file-name" title="${escapeHtml(file.name)}">${escapeHtml(file.name)}</div>
                <div class="queue-file-size">${formatFileSize(file.size)}</div>
            </div>
            <button type="button" class="queue-file-remove" onclick="removeQueuedFile(${idx})" title="Remove file">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <line x1="18" y1="6" x2="6" y2="18"></line>
                    <line x1="6" y1="6" x2="18" y2="18"></line>
                </svg>
            </button>
        `;

        grid.appendChild(card);
    });
}

function setupExpenseUploadForm() {
    const form = document.getElementById("expenseUploadForm");
    const modal = document.getElementById("scanOverlay");
    const ticker = document.getElementById("scanStepTicker");
    if (!form) return;

    form.addEventListener("submit", async (e) => {
        e.preventDefault();
        hideError();

        if (queuedFiles.length === 0) {
            showError("Please select at least one bill/invoice to extract.");
            return;
        }

        // Show loading overlay
        if (modal) {
            modal.classList.add("active");
            if (ticker) ticker.textContent = `Extracting ${queuedFiles.length} ${queuedFiles.length === 1 ? 'invoice' : 'invoices'} with Mistral Vision AI...`;
        }

        const formData = new FormData();
        queuedFiles.forEach(file => {
            formData.append("bills", file);
        });

        try {
            const res = await fetch("/api/expense/extract", {
                method: "POST",
                body: formData,
            });

            if (modal) modal.classList.remove("active");
            const data = await res.json();

            if (!res.ok || !data.success) {
                showError(data.error || "Invoice extraction failed.");
                return;
            }

            // Append or load bills for review
            reviewedBillsState = data.bills || [];
            clearExpenseQueue();
            renderReviewSection();

            // Scroll to review section
            const reviewSec = document.getElementById("reviewContainer");
            if (reviewSec) reviewSec.scrollIntoView({ behavior: "smooth" });

        } catch (err) {
            if (modal) modal.classList.remove("active");
            showError("Network error during extraction: " + err.message);
        }
    });
}

// --------------------------------------------------
// 2. EDITABLE REVIEW & PRE-SAVE VERIFICATION
// --------------------------------------------------

function renderReviewSection() {
    const container = document.getElementById("reviewContainer");
    const list = document.getElementById("reviewBillsList");
    if (!container || !list) return;

    if (reviewedBillsState.length === 0) {
        container.style.display = "none";
        list.innerHTML = "";
        return;
    }

    container.style.display = "block";
    list.innerHTML = "";

    reviewedBillsState.forEach((bill, bIdx) => {
        const card = document.createElement("div");
        card.className = "review-bill-card";
        if (bill.is_duplicate) card.classList.add("has-duplicate");
        else if (!bill.math_valid || (bill.missing_fields && bill.missing_fields.length > 0)) {
            card.classList.add("has-warning");
        }

        const isMissingVendor = !bill.vendor;
        const isMissingInv = !bill.invoice_no;
        const isMissingDate = !bill.date;
        const isMissingTotal = !bill.total;

        // Line items HTML
        let itemsRows = "";
        (bill.line_items || []).forEach((it, itIdx) => {
            itemsRows += `
                <tr>
                    <td>
                        <input type="text" value="${escapeHtml(it.name)}" oninput="updateReviewItem(${bIdx}, ${itIdx}, 'name', this.value)">
                    </td>
                    <td style="width: 70px;">
                        <input type="number" step="0.5" value="${it.qty || 1}" oninput="updateReviewItem(${bIdx}, ${itIdx}, 'qty', this.value)">
                    </td>
                    <td style="width: 100px;">
                        <input type="number" step="0.01" value="${it.price || 0}" oninput="updateReviewItem(${bIdx}, ${itIdx}, 'price', this.value)">
                    </td>
                    <td style="width: 100px;">
                        <input type="number" step="0.01" value="${it.total || 0}" oninput="updateReviewItem(${bIdx}, ${itIdx}, 'total', this.value)">
                    </td>
                    <td style="width: 36px; text-align: center;">
                        <button type="button" class="queue-file-remove" onclick="removeReviewItem(${bIdx}, ${itIdx})" title="Delete item">
                            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <line x1="18" y1="6" x2="6" y2="18"></line>
                                <line x1="6" y1="6" x2="18" y2="18"></line>
                            </svg>
                        </button>
                    </td>
                </tr>
            `;
        });

        // Math status pill (Rs 0.05 threshold)
        const subtotalNum = parseFloat(bill.subtotal || 0);
        const gstNum = parseFloat(bill.gst_amount || 0);
        const totalNum = parseFloat(bill.total || 0);
        const expectedTotal = parseFloat((subtotalNum + gstNum).toFixed(2));
        const diff = parseFloat(Math.abs(expectedTotal - totalNum).toFixed(2));
        const mathMatches = diff <= 0.05;

        let mathBadge = "";
        if (mathMatches) {
            mathBadge = `<span class="badge-status-pill ok" style="padding: 5px 12px; font-size: 0.85rem;">✓ OK (${formatINR(totalNum)})</span>`;
        } else {
            const suggestedGst = Math.max(0, parseFloat((totalNum - subtotalNum).toFixed(2)));
            mathBadge = `
                <span class="badge-status-pill needs-review" style="padding: 5px 12px; font-size: 0.85rem;">
                    ⚠ Needs review (Diff ₹${diff.toFixed(2)})
                    <button type="button" class="btn-chip" style="margin-left: 6px; padding: 3px 8px; font-size: 0.78rem;" onclick="applySuggestedGst(${bIdx}, ${suggestedGst})">Suggest GST = ₹${suggestedGst.toFixed(2)}</button>
                </span>
            `;
        }

        // Duplicate banner
        let dupBanner = "";
        if (bill.is_duplicate) {
            dupBanner = `
                <div class="review-alert danger" style="margin-top: 10px; font-size: 0.88rem;">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <circle cx="12" cy="12" r="10"></circle>
                        <line x1="12" y1="8" x2="12" y2="12"></line>
                        <line x1="12" y1="16" x2="12.01" y2="16"></line>
                    </svg>
                    <span>Duplicate invoice detected in records.</span>
                    <label style="margin-left: auto; display: flex; align-items: center; gap: 6px; font-size: 0.85rem; cursor: pointer;">
                        <input type="checkbox" onchange="toggleAllowDuplicate(${bIdx}, this.checked)" ${bill.allow_duplicate ? 'checked' : ''}>
                        Save Anyway
                    </label>
                </div>
            `;
        }

        card.innerHTML = `
            <div class="review-bill-header">
                <div class="review-bill-title">
                    <div class="review-thumb-btn" onclick="openZoomModal('${bill.preview_url}')" title="Click to enlarge document preview">
                        <img src="${bill.preview_url}" alt="Bill Preview">
                    </div>
                    <div>
                        <div style="font-size: 1.05rem; font-weight: 800; color: var(--text-primary);">${escapeHtml(bill.vendor || 'Unknown Vendor')}</div>
                        <div style="font-size: 0.82rem; color: var(--text-muted);">${escapeHtml(bill.filename)}</div>
                    </div>
                </div>
                <div style="display: flex; align-items: center; gap: 8px;">
                    ${mathBadge}
                    <button type="button" class="btn-add" style="padding: 7px 14px; font-size: 0.85rem;" onclick="saveSingleReviewedBill(${bIdx})">
                        Save Bill
                    </button>
                    <button type="button" class="btn-clear" style="font-size: 0.85rem;" onclick="discardReviewBill(${bIdx})">
                        Discard
                    </button>
                </div>
            </div>

            ${dupBanner}

            <!-- Editable fields grid -->
            <div class="review-fields-grid">
                <div class="review-field-item">
                    <label class="review-field-label">
                        Vendor Name
                        ${isMissingVendor ? '<span class="field-flag-dot missing" title="Missing field"></span>' : ''}
                    </label>
                    <input type="text" class="clean-input ${isMissingVendor ? 'input-missing' : ''}" value="${escapeHtml(bill.vendor)}" oninput="updateReviewField(${bIdx}, 'vendor', this.value)">
                </div>

                <div class="review-field-item">
                    <label class="review-field-label">
                        Invoice Number
                        ${isMissingInv ? '<span class="field-flag-dot missing" title="Missing field"></span>' : ''}
                    </label>
                    <input type="text" class="clean-input ${isMissingInv ? 'input-missing' : ''}" value="${escapeHtml(bill.invoice_no)}" oninput="updateReviewField(${bIdx}, 'invoice_no', this.value)">
                </div>

                <div class="review-field-item">
                    <label class="review-field-label">
                        Invoice Date
                        ${isMissingDate ? '<span class="field-flag-dot missing" title="Missing field"></span>' : ''}
                    </label>
                    <input type="date" class="clean-input ${isMissingDate ? 'input-missing' : ''}" value="${escapeHtml(bill.date)}" oninput="updateReviewField(${bIdx}, 'date', this.value)">
                </div>

                <div class="review-field-item">
                    <label class="review-field-label">Category (AI Inferred)</label>
                    <select class="clean-select" onchange="updateReviewField(${bIdx}, 'category', this.value)">
                        <option value="Food" ${bill.category === 'Food' ? 'selected' : ''}>Food</option>
                        <option value="Travel" ${bill.category === 'Travel' ? 'selected' : ''}>Travel</option>
                        <option value="Utilities" ${bill.category === 'Utilities' ? 'selected' : ''}>Utilities</option>
                        <option value="Stationery" ${bill.category === 'Stationery' ? 'selected' : ''}>Stationery</option>
                        <option value="Inventory" ${bill.category === 'Inventory' ? 'selected' : ''}>Inventory</option>
                        <option value="Other" ${bill.category === 'Other' ? 'selected' : ''}>Other</option>
                    </select>
                </div>

                <div class="review-field-item">
                    <label class="review-field-label">Subtotal (₹)</label>
                    <input type="number" step="0.01" class="clean-input" value="${bill.subtotal || 0}" oninput="updateReviewField(${bIdx}, 'subtotal', this.value)">
                </div>

                <div class="review-field-item">
                    <label class="review-field-label">GST Rate (%)</label>
                    <select class="clean-select" onchange="updateReviewGstRate(${bIdx}, this.value)">
                        <option value="0" ${bill.gst_rate === 0 ? 'selected' : ''}>0%</option>
                        <option value="5" ${bill.gst_rate === 5 ? 'selected' : ''}>5%</option>
                        <option value="12" ${bill.gst_rate === 12 ? 'selected' : ''}>12%</option>
                        <option value="18" ${bill.gst_rate === 18 ? 'selected' : ''}>18%</option>
                        <option value="28" ${bill.gst_rate === 28 ? 'selected' : ''}>28%</option>
                    </select>
                </div>

                <div class="review-field-item">
                    <label class="review-field-label">GST Amount (₹)</label>
                    <input type="number" step="0.01" class="clean-input" value="${bill.gst_amount || 0}" oninput="updateReviewField(${bIdx}, 'gst_amount', this.value)">
                </div>

                <div class="review-field-item">
                    <label class="review-field-label">
                        Total Amount (₹)
                        ${isMissingTotal ? '<span class="field-flag-dot missing" title="Missing field"></span>' : ''}
                    </label>
                    <input type="number" step="0.01" class="clean-input ${isMissingTotal ? 'input-missing' : ''}" value="${bill.total || 0}" oninput="updateReviewField(${bIdx}, 'total', this.value)">
                </div>
            </div>

            <!-- Line Items section -->
            <div class="line-items-wrapper">
                <div class="line-items-header">
                    <h4>Extracted Line Items (${(bill.line_items || []).length})</h4>
                    <button type="button" class="btn-chip" onclick="addReviewItem(${bIdx})">+ Add Item</button>
                </div>
                <table class="line-items-table">
                    <thead>
                        <tr>
                            <th>Item Name</th>
                            <th style="width: 70px;">Qty</th>
                            <th style="width: 100px;">Price (₹)</th>
                            <th style="width: 100px;">Total (₹)</th>
                            <th style="width: 36px;"></th>
                        </tr>
                    </thead>
                    <tbody>
                        ${itemsRows}
                    </tbody>
                </table>
            </div>
        `;

        list.appendChild(card);
    });
}

function snapToStandardGstRate(rate, gstAmount, subtotal) {
    let r = parseFloat(rate) || 0;
    // CGST/SGST half-rate doubled
    if (r === 2.5) r = 5.0;
    else if (r === 6.0) r = 12.0;
    else if (r === 9.0) r = 18.0;
    else if (r === 14.0) r = 28.0;

    const slabs = [0.0, 5.0, 12.0, 18.0, 28.0];
    if (subtotal > 0 && gstAmount > 0) {
        const eff = (gstAmount / subtotal) * 100;
        let closestEff = slabs[0];
        let minDiff = Math.abs(eff - closestEff);
        for (const s of slabs) {
            const d = Math.abs(eff - s);
            if (d < minDiff) { minDiff = d; closestEff = s; }
        }
        if (minDiff <= 2.5) return closestEff;
    }

    let closest = slabs[0];
    let minD = Math.abs(r - closest);
    for (const s of slabs) {
        const d = Math.abs(r - s);
        if (d < minD) { minD = d; closest = s; }
    }
    return closest;
}

function applySuggestedGst(bIdx, suggestedGst) {
    if (!reviewedBillsState[bIdx]) return;
    const b = reviewedBillsState[bIdx];
    b.gst_amount = suggestedGst;
    if (b.subtotal > 0) {
        b.gst_rate = snapToStandardGstRate(b.gst_rate, b.gst_amount, b.subtotal);
    }
    b.expected_total = parseFloat(((b.subtotal || 0) + b.gst_amount).toFixed(2));
    b.validation_status = "OK";
    b.validation_notes = "Adjusted GST to match total";
    b.math_valid = true;
    renderReviewSection();
}

function updateReviewField(bIdx, key, val) {
    if (!reviewedBillsState[bIdx]) return;
    if (key === "total" || key === "subtotal" || key === "gst_amount" || key === "gst_rate") {
        val = parseFloat(val) || 0;
    }
    reviewedBillsState[bIdx][key] = val;

    // Recalculate math validity with 0.05 threshold
    const b = reviewedBillsState[bIdx];
    b.expected_total = parseFloat(((b.subtotal || 0) + (b.gst_amount || 0)).toFixed(2));
    const diff = Math.abs(b.expected_total - (b.total || 0));
    b.math_valid = diff <= 0.05;
    b.validation_status = b.math_valid ? "OK" : "Needs review";
}

function updateReviewGstRate(bIdx, rateVal) {
    if (!reviewedBillsState[bIdx]) return;
    const r = snapToStandardGstRate(rateVal, reviewedBillsState[bIdx].gst_amount || 0, reviewedBillsState[bIdx].subtotal || 0);
    reviewedBillsState[bIdx].gst_rate = r;
    // Auto recalculate GST amount based on subtotal
    const sub = reviewedBillsState[bIdx].subtotal || 0;
    if (sub > 0 && r > 0) {
        const gst = parseFloat(((sub * r) / 100).toFixed(2));
        reviewedBillsState[bIdx].gst_amount = gst;
        reviewedBillsState[bIdx].total = parseFloat((sub + gst).toFixed(2));
    }
    renderReviewSection();
}

function autoBalanceBill(bIdx) {
    if (!reviewedBillsState[bIdx]) return;
    const b = reviewedBillsState[bIdx];
    b.total = parseFloat(((b.subtotal || 0) + (b.gst_amount || 0)).toFixed(2));
    b.math_valid = true;
    b.validation_status = "OK";
    renderReviewSection();
}

function toggleAllowDuplicate(bIdx, checked) {
    if (!reviewedBillsState[bIdx]) return;
    reviewedBillsState[bIdx].allow_duplicate = checked;
}

function addReviewItem(bIdx) {
    if (!reviewedBillsState[bIdx]) return;
    if (!reviewedBillsState[bIdx].line_items) reviewedBillsState[bIdx].line_items = [];
    reviewedBillsState[bIdx].line_items.push({
        name: "New Item",
        qty: 1,
        price: 0,
        total: 0
    });
    renderReviewSection();
}

function removeReviewItem(bIdx, itIdx) {
    if (!reviewedBillsState[bIdx] || !reviewedBillsState[bIdx].line_items) return;
    reviewedBillsState[bIdx].line_items.splice(itIdx, 1);
    recalcBillFromItems(bIdx);
    renderReviewSection();
}

function updateReviewItem(bIdx, itIdx, key, val) {
    const b = reviewedBillsState[bIdx];
    if (!b || !b.line_items || !b.line_items[itIdx]) return;
    const it = b.line_items[itIdx];
    if (key === "qty" || key === "price" || key === "total") {
        it[key] = parseFloat(val) || 0;
        if (key === "qty" || key === "price") {
            it.total = parseFloat((it.qty * it.price).toFixed(2));
        } else if (key === "total" && (!it.price || it.price === 0) && it.qty > 0) {
            it.price = parseFloat((it.total / it.qty).toFixed(2));
        }
    } else {
        it[key] = val;
    }
    // Unit price check: if missing or 0 but amount exists, compute price = amount / qty
    if (it.total > 0 && (!it.price || it.price === 0) && it.qty > 0) {
        it.price = parseFloat((it.total / it.qty).toFixed(2));
    }
    recalcBillFromItems(bIdx);
}

function recalcBillFromItems(bIdx) {
    const b = reviewedBillsState[bIdx];
    if (!b) return;
    (b.line_items || []).forEach(it => {
        if (it.total > 0 && (!it.price || it.price === 0) && it.qty > 0) {
            it.price = parseFloat((it.total / it.qty).toFixed(2));
        }
    });
    const itemsSum = (b.line_items || []).reduce((acc, curr) => acc + (parseFloat(curr.total) || 0), 0);
    b.subtotal = parseFloat(itemsSum.toFixed(2));
    const r = b.gst_rate || 0;
    if (r > 0) {
        b.gst_amount = parseFloat(((b.subtotal * r) / 100).toFixed(2));
    }
    b.total = parseFloat(((b.subtotal || 0) + (b.gst_amount || 0)).toFixed(2));
    b.math_valid = true;
    b.validation_status = "OK";
}

function discardReviewBill(bIdx) {
    reviewedBillsState.splice(bIdx, 1);
    renderReviewSection();
}

function discardAllReviews() {
    if (confirm("Discard all extracted bills from review?")) {
        reviewedBillsState = [];
        renderReviewSection();
    }
}

// --------------------------------------------------
// SAVE BILLS TO SQLITE
// --------------------------------------------------

async function saveSingleReviewedBill(bIdx) {
    const bill = reviewedBillsState[bIdx];
    if (!bill) return;

    // Check difference with Rs 0.05 threshold - do not save silently
    const sub = parseFloat(bill.subtotal || 0);
    const gst = parseFloat(bill.gst_amount || 0);
    const tot = parseFloat(bill.total || 0);
    const diff = Math.abs((sub + gst) - tot);
    if (diff > 0.05) {
        if (!confirm(`Warning: Subtotal (₹${sub.toFixed(2)}) + GST (₹${gst.toFixed(2)}) differs from Total (₹${tot.toFixed(2)}) by ₹${diff.toFixed(2)} (> ₹0.05).\n\nStatus will be saved as "Needs review". Proceed?`)) {
            return;
        }
        bill.validation_status = "Needs review";
    } else {
        bill.validation_status = "OK";
    }

    try {
        const res = await fetch("/api/expense/save", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ bills: [bill] }),
        });
        const data = await res.json();
        if (!res.ok || !data.success) {
            showError(data.error || "Failed to save bill.");
            return;
        }

        if (data.skipped_count > 0 && !bill.allow_duplicate) {
            bill.is_duplicate = true;
            bill.duplicate_warning = "Duplicate detected! Enable 'Save Anyway' to save this invoice.";
            renderReviewSection();
            showError("Duplicate detected. Please review or check 'Save Anyway'.");
            return;
        }

        // Success: remove from review state
        reviewedBillsState.splice(bIdx, 1);
        renderReviewSection();
        loadDashboardData();

    } catch (err) {
        showError("Save error: " + err.message);
    }
}

async function saveAllReviewedBills() {
    if (reviewedBillsState.length === 0) return;

    // Check if any bill differs by > Rs 0.05 - do not save silently
    const mismatches = reviewedBillsState.filter(b => {
        const sub = parseFloat(b.subtotal || 0);
        const gst = parseFloat(b.gst_amount || 0);
        const tot = parseFloat(b.total || 0);
        return Math.abs((sub + gst) - tot) > 0.05;
    });

    if (mismatches.length > 0) {
        if (!confirm(`${mismatches.length} invoice(s) have Subtotal + GST differing from Total by more than ₹0.05.\n\nThey will be saved with status "Needs review". Proceed?`)) {
            return;
        }
    }

    try {
        const res = await fetch("/api/expense/save", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ bills: reviewedBillsState }),
        });
        const data = await res.json();
        if (!res.ok || !data.success) {
            showError(data.error || "Failed to save bills.");
            return;
        }

        if (data.skipped_count > 0) {
            // Keep the duplicates in review
            const skippedMap = {};
            (data.skipped_duplicates || []).forEach(d => {
                skippedMap[d.vendor + d.invoice_no] = true;
            });
            reviewedBillsState = reviewedBillsState.filter(b => skippedMap[b.vendor + b.invoice_no]);
            reviewedBillsState.forEach(b => {
                b.is_duplicate = true;
                b.duplicate_warning = "Duplicate detected in database. Check 'Save Anyway' if intended.";
            });
            renderReviewSection();
            showError(`Saved ${data.saved_count} bills. ${data.skipped_count} duplicates need your review.`);
        } else {
            reviewedBillsState = [];
            renderReviewSection();
        }

        loadDashboardData();

    } catch (err) {
        showError("Save error: " + err.message);
    }
}

// --------------------------------------------------
// 3. DASHBOARD ANALYTICS & CHART.JS
// --------------------------------------------------

async function loadDashboardData() {
    const sDate = document.getElementById("filterStartDate")?.value || "";
    const eDate = document.getElementById("filterEndDate")?.value || "";
    const cat = document.getElementById("filterCategory")?.value || "all";
    const ven = document.getElementById("filterVendor")?.value || "all";
    const srch = document.getElementById("ledgerSearchInput")?.value || "";

    const params = new URLSearchParams({
        start_date: sDate,
        end_date: eDate,
        category: cat,
        vendor: ven,
        search: srch,
    });

    try {
        const res = await fetch(`/api/expense/data?${params.toString()}`);
        const data = await res.json();
        if (!res.ok || !data.success) {
            console.error("Dashboard fetch error:", data);
            return;
        }

        renderKPICards(data.kpis);
        renderVendorFilterOptions(data.filter_options.vendors);
        renderCharts(data.charts);
        renderGSTTable(data.charts.gst_summary.table);
        renderBillsTable(data.bills);

    } catch (err) {
        console.error("Failed to load dashboard data:", err);
    }
}

function renderKPICards(kpis) {
    if (!kpis) return;

    // 1. Total Spend
    const spendEl = document.getElementById("kpiTotalSpend");
    if (spendEl) spendEl.textContent = formatINR(kpis.total_spend);

    const spendBadge = document.getElementById("kpiSpendTrendBadge");
    if (spendBadge) {
        spendBadge.className = `badge-trend ${kpis.spend_change.direction}`;
        const arrow = kpis.spend_change.direction === "up" ? "▲ +" : kpis.spend_change.direction === "down" ? "▼ -" : "";
        spendBadge.textContent = `${arrow}${kpis.spend_change.pct}%`;
    }

    // 2. Total GST
    const gstEl = document.getElementById("kpiTotalGst");
    if (gstEl) gstEl.textContent = formatINR(kpis.total_gst);

    const gstBadge = document.getElementById("kpiGstTrendBadge");
    if (gstBadge) {
        gstBadge.className = `badge-trend ${kpis.gst_change.direction}`;
        const arrow = kpis.gst_change.direction === "up" ? "▲ +" : kpis.gst_change.direction === "down" ? "▼ -" : "";
        gstBadge.textContent = `${arrow}${kpis.gst_change.pct}%`;
    }

    // 3. Bill Count
    const countEl = document.getElementById("kpiBillCount");
    if (countEl) countEl.textContent = kpis.bill_count;

    const countBadge = document.getElementById("kpiCountTrendBadge");
    if (countBadge) {
        countBadge.className = `badge-trend ${kpis.count_change.direction}`;
        const arrow = kpis.count_change.direction === "up" ? "▲ +" : kpis.count_change.direction === "down" ? "▼ -" : "";
        countBadge.textContent = `${arrow}${kpis.count_change.pct}%`;
    }

    // 4. Top Vendor
    const topVenName = document.getElementById("kpiTopVendorName");
    const topVenSpend = document.getElementById("kpiTopVendorSpend");
    const topVenShare = document.getElementById("kpiTopVendorShare");
    if (topVenName) topVenName.textContent = kpis.top_vendor.name || "None";
    if (topVenSpend) topVenSpend.textContent = formatINR(kpis.top_vendor.spend);
    if (topVenShare) topVenShare.textContent = `(${kpis.top_vendor.share_pct}% of spend)`;
}

function renderVendorFilterOptions(vendors) {
    const sel = document.getElementById("filterVendor");
    if (!sel) return;
    const currentVal = sel.value;

    sel.innerHTML = `<option value="all">All Vendors</option>`;
    (vendors || []).forEach(v => {
        const opt = document.createElement("option");
        opt.value = v;
        opt.textContent = v;
        if (v === currentVal) opt.selected = true;
        sel.appendChild(opt);
    });
}

function renderCharts(chartsData) {
    if (typeof Chart === "undefined") {
        console.warn("Chart.js not yet loaded.");
        return;
    }

    const isDark = document.documentElement.getAttribute("data-theme") === "dark-emerald";
    const textPrimaryColor = isDark ? "#F4F1EB" : "#0D261F";
    const textMutedColor = isDark ? "#82A396" : "#627F75";
    const gridLineColor = isDark ? "rgba(244, 241, 235, 0.08)" : "rgba(45, 90, 74, 0.1)";

    Chart.defaults.color = textMutedColor;
    Chart.defaults.font.family = "'Plus Jakarta Sans', system-ui, sans-serif";
    Chart.defaults.font.size = 13;

    // Palette Colors
    const cDarkEmerald = isDark ? "#7AA05A" : "#0D4C3C";
    const cSlateGreen = isDark ? "#5C8C70" : "#2D5A4A";
    const cMossGreen = "#7AA05A";
    const cAmber = "#C29B38";
    const cTerracotta = "#B85D38";
    const cSage = "#94B49F";

    // 1. Monthly Spending Line/Bar Chart
    const ctxMonthly = document.getElementById("chartMonthlySpend")?.getContext("2d");
    if (ctxMonthly && chartsData.monthly_spending) {
        if (chartInstances.monthly) chartInstances.monthly.destroy();

        const labels = chartsData.monthly_spending.labels;
        const spends = chartsData.monthly_spending.spends;
        const gsts = chartsData.monthly_spending.gsts;

        chartInstances.monthly = new Chart(ctxMonthly, {
            type: "bar",
            data: {
                labels: labels,
                datasets: [
                    {
                        type: "bar",
                        label: "Total Spend",
                        data: spends,
                        backgroundColor: cDarkEmerald,
                        borderRadius: 6,
                        barPercentage: 0.5,
                        categoryPercentage: 0.65,
                    },
                    {
                        type: "line",
                        label: "GST Amount",
                        data: gsts,
                        borderColor: cAmber,
                        backgroundColor: "transparent",
                        borderWidth: 2.8,
                        pointBackgroundColor: cAmber,
                        pointRadius: 4,
                        pointHoverRadius: 6,
                        tension: 0.35,
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        position: "top",
                        labels: { color: textPrimaryColor, usePointStyle: true, pointStyle: "circle", font: { size: 13, weight: "600" }, padding: 18 }
                    },
                    tooltip: {
                        padding: 12,
                        cornerRadius: 8,
                        titleFont: { size: 13, weight: "700" },
                        bodyFont: { size: 13 },
                        callbacks: {
                            label: (ctx) => `${ctx.dataset.label}: ${formatINR(ctx.raw)}`
                        }
                    }
                },
                scales: {
                    x: {
                        grid: { display: false },
                        ticks: { color: textMutedColor, font: { size: 12, weight: "500" } }
                    },
                    y: {
                        grid: { color: gridLineColor },
                        ticks: { color: textMutedColor, font: { size: 12, weight: "500" }, callback: (val) => formatINR(val) }
                    }
                }
            }
        });
    }

    // 2. Spending Trend vs. Previous Month (By Category)
    const ctxTrend = document.getElementById("chartSpendTrend")?.getContext("2d");
    if (ctxTrend && chartsData.spending_trend) {
        if (chartInstances.trend) chartInstances.trend.destroy();

        const tr = chartsData.spending_trend;
        const trendLabelEl = document.getElementById("trendMonthsLabel");
        if (trendLabelEl) {
            trendLabelEl.textContent = `${tr.current_label} vs ${tr.previous_label}`;
        }

        chartInstances.trend = new Chart(ctxTrend, {
            type: "bar",
            data: {
                labels: tr.categories,
                datasets: [
                    {
                        label: tr.current_label || "Current Month",
                        data: tr.current_month,
                        backgroundColor: cDarkEmerald,
                        borderRadius: 5,
                        barPercentage: 0.6,
                        categoryPercentage: 0.7,
                    },
                    {
                        label: tr.previous_label || "Previous Month",
                        data: tr.previous_month,
                        backgroundColor: isDark ? "rgba(255,255,255,0.2)" : "rgba(45, 90, 74, 0.25)",
                        borderRadius: 5,
                        barPercentage: 0.6,
                        categoryPercentage: 0.7,
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        position: "top",
                        labels: { color: textPrimaryColor, usePointStyle: true, pointStyle: "circle", font: { size: 13, weight: "600" }, padding: 18 }
                    },
                    tooltip: {
                        padding: 12,
                        cornerRadius: 8,
                        titleFont: { size: 13, weight: "700" },
                        bodyFont: { size: 13 },
                        callbacks: {
                            label: (ctx) => `${ctx.dataset.label}: ${formatINR(ctx.raw)}`
                        }
                    }
                },
                scales: {
                    x: {
                        grid: { display: false },
                        ticks: { color: textMutedColor, font: { size: 12, weight: "500" } }
                    },
                    y: {
                        grid: { color: gridLineColor },
                        ticks: { color: textMutedColor, font: { size: 12, weight: "500" }, callback: (val) => formatINR(val) }
                    }
                }
            }
        });
    }

    // 3. Category Donut Chart
    const ctxDonut = document.getElementById("chartCategoryDonut")?.getContext("2d");
    if (ctxDonut && chartsData.category_donut) {
        if (chartInstances.donut) chartInstances.donut.destroy();

        const dLabels = chartsData.category_donut.labels;
        const dVals = chartsData.category_donut.values;

        chartInstances.donut = new Chart(ctxDonut, {
            type: "doughnut",
            data: {
                labels: dLabels.length ? dLabels : ["No Data"],
                datasets: [{
                    data: dVals.length ? dVals : [1],
                    backgroundColor: dVals.length
                        ? [cDarkEmerald, cMossGreen, cSlateGreen, cAmber, cTerracotta, cSage]
                        : ["rgba(0,0,0,0.1)"],
                    borderWidth: 2,
                    borderColor: isDark ? "#0E332A" : "#FFFFFF",
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                cutout: "60%",
                plugins: {
                    legend: {
                        position: "right",
                        labels: { color: textPrimaryColor, usePointStyle: true, pointStyle: "circle", font: { size: 13, weight: "600" }, padding: 16 }
                    },
                    tooltip: {
                        padding: 12,
                        cornerRadius: 8,
                        titleFont: { size: 13, weight: "700" },
                        bodyFont: { size: 13 },
                        callbacks: {
                            label: (ctx) => `${ctx.label}: ${formatINR(ctx.raw)}`
                        }
                    }
                }
            }
        });
    }

    // 4. Top Vendors Horizontal Bar Chart
    const ctxVendors = document.getElementById("chartTopVendors")?.getContext("2d");
    if (ctxVendors && chartsData.top_vendors) {
        if (chartInstances.vendors) chartInstances.vendors.destroy();

        chartInstances.vendors = new Chart(ctxVendors, {
            type: "bar",
            data: {
                labels: chartsData.top_vendors.labels,
                datasets: [{
                    label: "Spend Amount",
                    data: chartsData.top_vendors.values,
                    backgroundColor: cSlateGreen,
                    borderRadius: 5,
                    barPercentage: 0.6,
                }]
            },
            options: {
                indexAxis: "y",
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        padding: 12,
                        cornerRadius: 8,
                        titleFont: { size: 13, weight: "700" },
                        bodyFont: { size: 13 },
                        callbacks: {
                            label: (ctx) => `Spend: ${formatINR(ctx.raw)}`
                        }
                    }
                },
                scales: {
                    x: {
                        grid: { color: gridLineColor },
                        ticks: { color: textMutedColor, font: { size: 12, weight: "500" }, callback: (val) => formatINR(val) }
                    },
                    y: {
                        grid: { display: false },
                        ticks: { color: textPrimaryColor, font: { size: 13, weight: "600" } }
                    }
                }
            }
        });
    }

    // 5. GST Summary Stacked Bar Chart
    const ctxGst = document.getElementById("chartGstStacked")?.getContext("2d");
    if (ctxGst && chartsData.gst_summary) {
        if (chartInstances.gst) chartInstances.gst.destroy();

        const colors = [
            "rgba(148, 180, 159, 0.75)", // 0%
            "rgba(122, 160, 90, 0.85)",  // 5%
            "rgba(45, 90, 74, 0.85)",    // 12%
            "rgba(13, 76, 60, 0.92)",    // 18%
            "rgba(184, 93, 56, 0.88)",   // 28%
        ];

        const datasets = (chartsData.gst_summary.stacked_datasets || []).map((ds, idx) => ({
            ...ds,
            backgroundColor: colors[idx % colors.length],
            borderRadius: 4,
            barPercentage: 0.55,
            categoryPercentage: 0.7,
        }));

        chartInstances.gst = new Chart(ctxGst, {
            type: "bar",
            data: {
                labels: chartsData.gst_summary.months,
                datasets: datasets,
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        position: "top",
                        labels: { color: textPrimaryColor, usePointStyle: true, pointStyle: "circle", font: { size: 13, weight: "600" }, padding: 18 }
                    },
                    tooltip: {
                        padding: 12,
                        cornerRadius: 8,
                        titleFont: { size: 13, weight: "700" },
                        bodyFont: { size: 13 },
                        callbacks: {
                            label: (ctx) => `${ctx.dataset.label}: ${formatINR(ctx.raw)}`
                        }
                    }
                },
                scales: {
                    x: {
                        stacked: true,
                        grid: { display: false },
                        ticks: { color: textMutedColor, font: { size: 12, weight: "500" } }
                    },
                    y: {
                        stacked: true,
                        grid: { color: gridLineColor },
                        ticks: { color: textMutedColor, font: { size: 12, weight: "500" }, callback: (val) => formatINR(val) }
                    }
                }
            }
        });
    }
}

function renderGSTTable(tableData) {
    const tbody = document.getElementById("gstTableBody");
    if (!tbody) return;

    if (!tableData || tableData.length === 0) {
        tbody.innerHTML = `<tr><td colspan="5" style="text-align: center; color: var(--text-muted); padding: 20px;">No GST data available</td></tr>`;
        return;
    }

    tbody.innerHTML = tableData.map(row => `
        <tr>
            <td style="font-weight: 700;">${escapeHtml(row.label)}</td>
            <td>${formatINR(row.subtotal)}</td>
            <td style="font-weight: 700; color: var(--c-dark-emerald);">${formatINR(row.gst_collected)}</td>
            <td>${row.count}</td>
            <td>${row.share_pct}%</td>
        </tr>
    `).join("");
}

// --------------------------------------------------
// 4. BILLS LEDGER TABLE
// --------------------------------------------------

function renderBillsTable(bills) {
    const tbody = document.getElementById("billsTableBody");
    const countBadge = document.getElementById("ledgerCountBadge");
    if (!tbody) return;

    if (countBadge) {
        countBadge.textContent = `${(bills || []).length} ${(bills || []).length === 1 ? 'Bill' : 'Bills'}`;
    }

    if (!bills || bills.length === 0) {
        tbody.innerHTML = `
            <tr>
                <td colspan="10" style="text-align: center; padding: 48px 20px; color: var(--text-muted); font-size: 0.95rem;">
                    No invoices found matching your filters.
                </td>
            </tr>
        `;
        return;
    }

    tbody.innerHTML = bills.map(b => {
        const catClass = (b.category || "other").toLowerCase();
        const itemsSummary = (b.line_items || []).map(it => it.name).slice(0, 3).join(", ");
        const moreCount = (b.line_items || []).length > 3 ? ` +${b.line_items.length - 3} more` : "";

        // Validation status
        const sub = parseFloat(b.subtotal || 0);
        const gst = parseFloat(b.gst_amount || 0);
        const tot = parseFloat(b.total || 0);
        const diff = Math.abs((sub + gst) - tot);
        const valStatus = b.validation_status || (diff <= 0.05 ? "OK" : "Needs review");
        const valClass = valStatus === "OK" ? "ok" : "needs-review";

        return `
            <tr>
                <td style="white-space: nowrap; font-family: var(--font-mono); font-size: 0.9rem;">${escapeHtml(b.date)}</td>
                <td style="font-weight: 700; font-size: 1rem;">${escapeHtml(b.vendor)}</td>
                <td style="font-family: var(--font-mono); font-size: 0.9rem; color: var(--text-muted);">${escapeHtml(b.invoice_no)}</td>
                <td><span class="category-chip ${catClass}">${escapeHtml(b.category)}</span></td>
                <td><span class="badge-status-pill ${valClass}" title="${escapeHtml(b.validation_notes || '')}">${escapeHtml(valStatus)}</span></td>
                <td style="color: var(--text-secondary); max-width: 260px; font-size: 0.92rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;" title="${escapeHtml(itemsSummary)}">
                    ${escapeHtml(itemsSummary || 'General Expense')}${moreCount}
                </td>
                <td style="font-family: var(--font-mono); font-size: 0.95rem;">${formatINR(b.subtotal)}</td>
                <td style="font-family: var(--font-mono); font-size: 0.95rem;">${b.gst_rate}% (${formatINR(b.gst_amount)})</td>
                <td style="font-family: var(--font-mono); font-weight: 800; color: var(--text-primary); font-size: 1.08rem;">${formatINR(b.total)}</td>
                <td>
                    <div class="table-action-btns">
                        <button type="button" class="btn-tbl-action" onclick="openEditModal(${b.id})" title="View / Edit Invoice">
                            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path>
                                <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path>
                            </svg>
                            <span>Edit</span>
                        </button>
                        <button type="button" class="btn-tbl-action danger" onclick="deleteBill(${b.id})" title="Delete Invoice">
                            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <polyline points="3 6 5 6 21 6"></polyline>
                                <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
                            </svg>
                        </button>
                    </div>
                </td>
            </tr>
        `;
    }).join("");
}

// --------------------------------------------------
// 5. FILTER HANDLERS
// --------------------------------------------------

function applyFilters() {
    loadDashboardData();
}

function handleSearchDebounced(val) {
    if (searchDebounceTimer) clearTimeout(searchDebounceTimer);
    searchDebounceTimer = setTimeout(() => {
        loadDashboardData();
    }, 300);
}

function setQuickDateFilter(rangeType, btnEl) {
    document.querySelectorAll(".quick-date-btns .btn-chip").forEach(b => b.classList.remove("active"));
    if (btnEl) btnEl.classList.add("active");

    const startInput = document.getElementById("filterStartDate");
    const endInput = document.getElementById("filterEndDate");
    if (!startInput || !endInput) return;

    const today = new Date();
    const pad = (n) => String(n).padStart(2, '0');
    const toYMD = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;

    if (rangeType === "all") {
        startInput.value = "";
        endInput.value = "";
    } else if (rangeType === "this_month") {
        const start = new Date(today.getFullYear(), today.getMonth(), 1);
        startInput.value = toYMD(start);
        endInput.value = toYMD(today);
    } else if (rangeType === "last_month") {
        const start = new Date(today.getFullYear(), today.getMonth() - 1, 1);
        const end = new Date(today.getFullYear(), today.getMonth(), 0);
        startInput.value = toYMD(start);
        endInput.value = toYMD(end);
    } else if (rangeType === "this_quarter") {
        const qMonth = Math.floor(today.getMonth() / 3) * 3;
        const start = new Date(today.getFullYear(), qMonth, 1);
        startInput.value = toYMD(start);
        endInput.value = toYMD(today);
    } else if (rangeType === "this_year") {
        const start = new Date(today.getFullYear(), 0, 1);
        startInput.value = toYMD(start);
        endInput.value = toYMD(today);
    }

    applyFilters();
}

function resetAllFilters() {
    const sDate = document.getElementById("filterStartDate");
    const eDate = document.getElementById("filterEndDate");
    const cat = document.getElementById("filterCategory");
    const ven = document.getElementById("filterVendor");
    const srch = document.getElementById("ledgerSearchInput");

    if (sDate) sDate.value = "";
    if (eDate) eDate.value = "";
    if (cat) cat.value = "all";
    if (ven) ven.value = "all";
    if (srch) srch.value = "";

    document.querySelectorAll(".quick-date-btns .btn-chip").forEach(b => b.classList.remove("active"));
    const allChip = document.querySelector(".quick-date-btns .btn-chip");
    if (allChip) allChip.classList.add("active");

    applyFilters();
}

function exportData(format) {
    const sDate = document.getElementById("filterStartDate")?.value || "";
    const eDate = document.getElementById("filterEndDate")?.value || "";
    const cat = document.getElementById("filterCategory")?.value || "all";
    const ven = document.getElementById("filterVendor")?.value || "all";
    const srch = document.getElementById("ledgerSearchInput")?.value || "";

    const params = new URLSearchParams({
        start_date: sDate,
        end_date: eDate,
        category: cat,
        vendor: ven,
        search: srch,
    });

    const endpoint = format === "excel" ? "/api/expense/export/excel" : "/api/expense/export/csv";
    window.location.href = `${endpoint}?${params.toString()}`;
}

// --------------------------------------------------
// 6. VIEW / EDIT MODAL & DELETE ACTION
// --------------------------------------------------

let modalCurrentLineItems = [];

async function openEditModal(billId) {
    const modal = document.getElementById("editBillModal");
    if (!modal) return;

    try {
        const res = await fetch(`/api/expense/bill/${billId}`);
        const data = await res.json();
        if (!res.ok || !data.success) {
            showError("Could not fetch bill details: " + (data.error || ""));
            return;
        }

        const b = data.bill;
        document.getElementById("modalBillId").value = b.id;
        document.getElementById("modalVendor").value = b.vendor;
        document.getElementById("modalInvoiceNo").value = b.invoice_no;
        document.getElementById("modalDate").value = b.date;
        document.getElementById("modalCategory").value = b.category;
        document.getElementById("modalSubtotal").value = b.subtotal;
        document.getElementById("modalGstRate").value = b.gst_rate;
        document.getElementById("modalGstAmount").value = b.gst_amount;
        document.getElementById("modalTotal").value = b.total;
        document.getElementById("modalNotes").value = b.notes || "";
        document.getElementById("editModalTitle").textContent = `Edit Bill #${b.id} (${b.vendor})`;

        modalCurrentLineItems = b.line_items || [];
        renderModalLineItems();

        modal.classList.add("active");

    } catch (err) {
        showError("Error opening bill modal: " + err.message);
    }
}

function closeEditModal() {
    const modal = document.getElementById("editBillModal");
    if (modal) modal.classList.remove("active");
}

function renderModalLineItems() {
    const tbody = document.getElementById("modalLineItemsBody");
    if (!tbody) return;

    tbody.innerHTML = modalCurrentLineItems.map((it, idx) => `
        <tr>
            <td>
                <input type="text" value="${escapeHtml(it.name)}" oninput="modalCurrentLineItems[${idx}].name=this.value">
            </td>
            <td>
                <input type="number" step="0.5" value="${it.qty || 1}" oninput="updateModalItem(${idx}, 'qty', this.value)">
            </td>
            <td>
                <input type="number" step="0.01" value="${it.price || 0}" oninput="updateModalItem(${idx}, 'price', this.value)">
            </td>
            <td>
                <input type="number" step="0.01" value="${it.total || 0}" oninput="updateModalItem(${idx}, 'total', this.value)">
            </td>
            <td style="text-align: center;">
                <button type="button" class="queue-file-remove" onclick="removeModalLineItem(${idx})">&times;</button>
            </td>
        </tr>
    `).join("");
}

function addModalLineItem() {
    modalCurrentLineItems.push({ name: "New Item", qty: 1, price: 0, total: 0 });
    renderModalLineItems();
}

function removeModalLineItem(idx) {
    modalCurrentLineItems.splice(idx, 1);
    recalcModalMathFromItems();
    renderModalLineItems();
}

function updateModalItem(idx, key, val) {
    const it = modalCurrentLineItems[idx];
    if (!it) return;
    it[key] = parseFloat(val) || 0;
    if (key === "qty" || key === "price") {
        it.total = parseFloat((it.qty * it.price).toFixed(2));
    }
    recalcModalMathFromItems();
}

function recalcModalMathFromItems() {
    const sum = modalCurrentLineItems.reduce((acc, it) => acc + (parseFloat(it.total) || 0), 0);
    const subEl = document.getElementById("modalSubtotal");
    if (subEl) subEl.value = sum.toFixed(2);
    recalcModalMath();
}

function recalcModalMath() {
    const sub = parseFloat(document.getElementById("modalSubtotal")?.value) || 0;
    const rate = parseFloat(document.getElementById("modalGstRate")?.value) || 0;
    let gst = parseFloat(document.getElementById("modalGstAmount")?.value) || 0;

    if (rate > 0) {
        gst = parseFloat(((sub * rate) / 100).toFixed(2));
        const gstEl = document.getElementById("modalGstAmount");
        if (gstEl) gstEl.value = gst;
    }

    const totEl = document.getElementById("modalTotal");
    if (totEl) totEl.value = (sub + gst).toFixed(2);
}

async function handleSaveEditedBill(e) {
    e.preventDefault();
    const id = document.getElementById("modalBillId").value;
    if (!id) return;

    // Ensure line items have unit price calculated if total exists
    modalCurrentLineItems.forEach(it => {
        if (it.total > 0 && (!it.price || it.price === 0) && it.qty > 0) {
            it.price = parseFloat((it.total / it.qty).toFixed(2));
        }
    });

    const sub = parseFloat(document.getElementById("modalSubtotal").value) || 0;
    const gstAmt = parseFloat(document.getElementById("modalGstAmount").value) || 0;
    const rawRate = parseFloat(document.getElementById("modalGstRate").value) || 0;
    const snappedRate = snapToStandardGstRate(rawRate, gstAmt, sub);

    const payload = {
        vendor: document.getElementById("modalVendor").value.trim(),
        invoice_no: document.getElementById("modalInvoiceNo").value.trim(),
        date: document.getElementById("modalDate").value.trim(),
        category: document.getElementById("modalCategory").value,
        subtotal: sub,
        gst_rate: snappedRate,
        gst_amount: gstAmt,
        total: parseFloat(document.getElementById("modalTotal").value) || 0,
        notes: document.getElementById("modalNotes").value.trim(),
        line_items: modalCurrentLineItems,
    };

    try {
        const res = await fetch(`/api/expense/bill/${id}`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (!res.ok || !data.success) {
            showError("Update failed: " + (data.error || ""));
            return;
        }

        closeEditModal();
        loadDashboardData();

    } catch (err) {
        showError("Save error: " + err.message);
    }
}

async function deleteBill(billId) {
    if (!confirm(`Are you sure you want to delete Bill #${billId}? This cannot be undone.`)) {
        return;
    }

    try {
        const res = await fetch(`/api/expense/bill/${billId}`, {
            method: "DELETE",
        });
        const data = await res.json();
        if (!res.ok || !data.success) {
            showError("Delete failed: " + (data.error || ""));
            return;
        }

        loadDashboardData();

    } catch (err) {
        showError("Delete error: " + err.message);
    }
}

// --------------------------------------------------
// 7. TAB NAVIGATION CONTROLLER (VERIFY ⇄ EXPENSE)
// --------------------------------------------------

function switchAppTab(tabId) {
    const btnVerify = document.getElementById("tabBtnVerify");
    const btnExpense = document.getElementById("tabBtnExpense");
    const paneVerify = document.getElementById("tabContentVerify");
    const paneExpense = document.getElementById("tabContentExpense");

    if (tabId === "expense") {
        if (btnVerify) {
            btnVerify.classList.remove("active");
            btnVerify.setAttribute("aria-selected", "false");
        }
        if (btnExpense) {
            btnExpense.classList.add("active");
            btnExpense.setAttribute("aria-selected", "true");
        }
        if (paneVerify) paneVerify.style.display = "none";
        if (paneExpense) {
            paneExpense.style.display = "block";
            loadDashboardData();
        }
        window.location.hash = "expense";
    } else {
        if (btnExpense) {
            btnExpense.classList.remove("active");
            btnExpense.setAttribute("aria-selected", "false");
        }
        if (btnVerify) {
            btnVerify.classList.add("active");
            btnVerify.setAttribute("aria-selected", "true");
        }
        if (paneExpense) paneExpense.style.display = "none";
        if (paneVerify) paneVerify.style.display = "block";
        window.location.hash = "verify";
    }
}

// Check initial hash on load
window.addEventListener("load", () => {
    if (window.location.hash === "#expense") {
        switchAppTab("expense");
    }
});
