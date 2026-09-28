/**
 * Clean & Readable Dynamic Document Verification Controller
 */

// Matching Rules List
const CONSTRAINT_OPTIONS = [
    { value: "fuzzy", label: "Fuzzy Text (Tolerant)" },
    { value: "exact", label: "Exact Match" },
    { value: "amount", label: "Currency Amount (=)" },
    { value: "numeric_gte", label: "Minimum Amount (>=)" },
    { value: "numeric_lte", label: "Maximum Amount (<=)" },
    { value: "date", label: "Date (=)" },
    { value: "date_on_or_after", label: "Date On/After (>=)" },
    { value: "date_on_or_before", label: "Date On/Before (<=)" },
    { value: "contains", label: "Contains Keyword" },
    { value: "regex", label: "Pattern (PAN, GSTIN...)" },
    { value: "present", label: "Any Value (Presence Only)" },
];

let fieldsState = [];
let fieldCounter = 0;
let currentUploadedFileUrl = null;

document.addEventListener("DOMContentLoaded", () => {
    initTheme();
    setupDropzone();
    setupFormSubmission();
    setupImageModal();

    // Check if server provided active fields
    if (window.INITIAL_FIELDS && window.INITIAL_FIELDS.length > 0) {
        fieldsState = window.INITIAL_FIELDS.map(f => ({
            id: f.id || `f_${++fieldCounter}`,
            name: f.name || f.field || "",
            expected: f.expected || f.user_value || "",
            constraint: f.constraint || "fuzzy",
            required: f.required !== false
        }));
    } else {
        // Default blank rows for single-tab verification entry
        fieldsState = [
            { id: `f_${++fieldCounter}`, name: "", expected: "", constraint: "fuzzy", required: true },
            { id: `f_${++fieldCounter}`, name: "", expected: "", constraint: "exact", required: true },
            { id: `f_${++fieldCounter}`, name: "", expected: "", constraint: "date", required: true }
        ];
    }

    renderFields();
});

/**
 * Luxury Theme Switcher (Alabaster Luxe ⇄ Dark Emerald Luxe)
 */
function initTheme() {
    const saved = localStorage.getItem("docuverify_luxury_theme");
    if (saved === "dark-emerald") {
        document.documentElement.setAttribute("data-theme", "dark-emerald");
        updateThemeBtn(true);
    } else {
        document.documentElement.removeAttribute("data-theme");
        updateThemeBtn(false);
    }
}

function toggleLuxuryTheme() {
    const html = document.documentElement;
    const isCurrentlyDark = html.getAttribute("data-theme") === "dark-emerald";
    if (isCurrentlyDark) {
        html.removeAttribute("data-theme");
        localStorage.setItem("docuverify_luxury_theme", "alabaster");
        updateThemeBtn(false);
    } else {
        html.setAttribute("data-theme", "dark-emerald");
        localStorage.setItem("docuverify_luxury_theme", "dark-emerald");
        updateThemeBtn(true);
    }
}

function updateThemeBtn(isDark) {
    const icon = document.getElementById("themeToggleIcon");
    const label = document.getElementById("themeToggleLabel");
    if (icon && label) {
        icon.textContent = isDark ? "☀️" : "🌿";
        label.textContent = isDark ? "Alabaster Mode" : "Emerald Mode";
    }
}

/**
 * Quick Template Autofill in the same tab (Optional helper)
 */
function handlePresetSelect(key) {
    if (!key) return;
    if (window.PRESET_DATA && window.PRESET_DATA[key]) {
        const preset = window.PRESET_DATA[key];
        const docTypeInput = document.getElementById("document_type");
        if (docTypeInput) docTypeInput.value = preset.name;

        fieldsState = preset.fields.map(f => ({
            id: `f_${++fieldCounter}`,
            name: f.name,
            expected: f.expected,
            constraint: f.constraint || "fuzzy",
            required: f.required !== false
        }));
        renderFields();
    }
    const selectEl = document.getElementById("quickPresetSelect");
    if (selectEl) selectEl.value = "";
}

/**
 * Render Fields as Clean, Readable Rows
 */
function renderFields() {
    const container = document.getElementById("fieldsContainer");
    const countBadge = document.getElementById("fieldsCountBadge");
    if (!container) return;

    container.innerHTML = "";
    if (countBadge) {
        countBadge.textContent = `${fieldsState.length} ${fieldsState.length === 1 ? 'Field' : 'Fields'}`;
    }

    if (fieldsState.length === 0) {
        container.innerHTML = `
            <div style="text-align: center; padding: 32px; color: var(--text-muted); border: 1px dashed var(--border); border-radius: var(--radius-sm); font-size: 0.95rem;">
                No fields added yet. Click <strong>+ Add Field</strong> to enter fields to verify.
            </div>
        `;
        return;
    }

    fieldsState.forEach((field) => {
        const row = document.createElement("div");
        row.className = "field-row";
        row.dataset.id = field.id;

        const optionsHtml = CONSTRAINT_OPTIONS.map(opt =>
            `<option value="${opt.value}" ${field.constraint === opt.value ? 'selected' : ''}>${opt.label}</option>`
        ).join("");

        row.innerHTML = `
            <div>
                <input type="text" class="clean-input" placeholder="Field name, e.g. Firm Name" value="${escapeHtml(field.name)}" oninput="updateField('${field.id}', 'name', this.value)">
            </div>
            <div>
                <input type="text" class="clean-input" placeholder="Expected target value" value="${escapeHtml(field.expected)}" oninput="updateField('${field.id}', 'expected', this.value)">
            </div>
            <div>
                <select class="clean-select" onchange="updateField('${field.id}', 'constraint', this.value)">
                    ${optionsHtml}
                </select>
            </div>
            <div style="display: flex; justify-content: center;">
                <button type="button" class="field-delete-btn" onclick="removeField('${field.id}')" title="Delete field">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <line x1="18" y1="6" x2="6" y2="18"></line>
                        <line x1="6" y1="6" x2="18" y2="18"></line>
                    </svg>
                </button>
            </div>
        `;

        container.appendChild(row);
    });

    syncHiddenJson();
}

function addField() {
    fieldsState.push({
        id: `f_${++fieldCounter}`,
        name: "",
        expected: "",
        constraint: "fuzzy",
        required: true
    });
    renderFields();

    // Focus on the new field's input
    const container = document.getElementById("fieldsContainer");
    if (container) {
        const inputs = container.querySelectorAll(".clean-input");
        if (inputs.length >= 2) {
            inputs[inputs.length - 2].focus();
        }
    }
}

function removeField(id) {
    fieldsState = fieldsState.filter(f => f.id !== id);
    renderFields();
}

function clearAllFields() {
    if (confirm("Clear all verification fields?")) {
        fieldsState = [];
        renderFields();
    }
}

function updateField(id, key, value) {
    const field = fieldsState.find(f => f.id === id);
    if (field) {
        field[key] = value;
        syncHiddenJson();
    }
}

function syncHiddenJson() {
    const hiddenJson = document.getElementById("fields_json");
    if (hiddenJson) {
        hiddenJson.value = JSON.stringify(fieldsState);
    }
}

/**
 * File Dropzone Handling & Approval
 */
function setupDropzone() {
    const dropzone = document.getElementById("dropzoneContainer");
    const fileInput = document.getElementById("document_image");
    const fileCard = document.getElementById("selectedFileCard");
    const fileNameEl = document.getElementById("selectedFileName");
    const fileSizeEl = document.getElementById("selectedFileSize");
    const removeBtn = document.getElementById("removeFileBtn");
    const previewBtn = document.getElementById("previewFileBtn");

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
            fileInput.files = e.dataTransfer.files;
            showSelectedFile(e.dataTransfer.files[0]);
        }
    });

    fileInput.addEventListener("change", (e) => {
        if (e.target.files && e.target.files.length > 0) {
            showSelectedFile(e.target.files[0]);
        }
    });

    if (removeBtn) {
        removeBtn.addEventListener("click", (e) => {
            e.stopPropagation();
            fileInput.value = "";
            if (currentUploadedFileUrl) {
                URL.revokeObjectURL(currentUploadedFileUrl);
                currentUploadedFileUrl = null;
            }
            if (fileCard) fileCard.style.display = "none";
            if (dropzone) dropzone.style.display = "flex";
        });
    }

    if (previewBtn) {
        previewBtn.addEventListener("click", (e) => {
            e.stopPropagation();
            if (currentUploadedFileUrl) {
                openZoomModal(currentUploadedFileUrl);
            } else if (fileInput.files && fileInput.files[0]) {
                const url = URL.createObjectURL(fileInput.files[0]);
                window.open(url, "_blank");
            }
        });
    }

    function showSelectedFile(file) {
        if (fileNameEl) fileNameEl.textContent = file.name;
        if (fileSizeEl) fileSizeEl.textContent = formatFileSize(file.size);

        if (currentUploadedFileUrl) {
            URL.revokeObjectURL(currentUploadedFileUrl);
            currentUploadedFileUrl = null;
        }

        const thumb = document.getElementById("selectedFileThumb");
        if (thumb) {
            thumb.innerHTML = "";
            if (file.type.startsWith("image/")) {
                currentUploadedFileUrl = URL.createObjectURL(file);
                const img = document.createElement("img");
                img.src = currentUploadedFileUrl;
                img.alt = file.name;
                img.style.cursor = "pointer";
                img.onclick = () => openZoomModal(currentUploadedFileUrl);
                thumb.appendChild(img);
            } else {
                thumb.innerHTML = `
                    <div class="file-icon-pdf">
                        <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
                            <polyline points="14 2 14 8 20 8"></polyline>
                        </svg>
                    </div>
                `;
            }
        }

        dropzone.style.display = "none";
        if (fileCard) fileCard.style.display = "flex";
    }
}

function formatFileSize(bytes) {
    if (bytes === 0) return '0 Bytes';
    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
}

/**
 * Form Submission via AJAX with Clean Loading State
 */
function setupFormSubmission() {
    const form = document.getElementById("verificationForm");
    const modal = document.getElementById("scanOverlay");
    const ticker = document.getElementById("scanStepTicker");

    if (!form) return;

    form.addEventListener("submit", async (e) => {
        e.preventDefault();
        hideError();

        let docType = document.getElementById("document_type").value.trim();
        if (!docType) {
            docType = "Document";
        }

        const validFields = fieldsState.filter(f => f.name.trim() !== "");
        if (validFields.length === 0) {
            showError("Please enter at least one field to verify.");
            return;
        }

        const fileInput = document.getElementById("document_image");
        if (!fileInput.files || fileInput.files.length === 0) {
            showError("Please upload and approve a document file (PDF or image).");
            return;
        }

        // Show clean loading spinner
        modal.classList.add("active");
        ticker.textContent = "Scanning document and matching fields with Vision AI...";

        const formData = new FormData();
        formData.append("document_type", docType);
        formData.append("document_image", fileInput.files[0]);
        formData.append("fields_json", JSON.stringify(validFields));

        try {
            const res = await fetch("/api/verify", {
                method: "POST",
                body: formData
            });

            modal.classList.remove("active");
            const data = await res.json();

            if (!res.ok || !data.success) {
                showError(data.error || "Verification failed to complete.");
                return;
            }

            renderResults(data);

        } catch (err) {
            modal.classList.remove("active");
            showError("Network error: " + err.message);
        }
    });
}

/**
 * Render Verification Results Table
 */
function renderResults(data) {
    const container = document.getElementById("resultsContainer");
    if (!container) return;

    const summary = data.summary;
    const isPass = summary.overall_status === "PASS";
    const isPartial = summary.overall_status === "PARTIAL";
    const isIncomplete = summary.overall_status === "INCOMPLETE";
    const isUnreadable = summary.overall_status === "UNREADABLE";

    let statusClass = "fail";
    let statusText = "✕ Verification Failed (Data Mismatch)";

    if (isPass) {
        statusClass = "pass";
        statusText = "✓ Document Verified Successfully";
    } else if (isPartial) {
        statusClass = "partial";
        statusText = "⚠ Partially Verified (Optional Fields Missing/Mismatched)";
    } else if (isIncomplete) {
        statusClass = "partial";
        statusText = "⚠ Verification Incomplete: Required Fields Not Found in Document";
    } else if (isUnreadable) {
        statusClass = "fail";
        statusText = "✕ Inconclusive: Scan Quality Low or Text Illegible";
    }

    const STATUS_LABELS = {
        "PASS": "PASS",
        "FAIL": "FAIL",
        "NOT_FOUND": "NOT IN DOC",
        "OCR_AMBIGUITY": "OCR RESOLVED",
        "UNREADABLE": "UNREADABLE",
        "DATE_AMBIGUOUS": "DATE AMBIGUOUS",
        "LOW_CONFIDENCE": "NEEDS REVIEW",
        "WARNING": "WARNING"
    };

    let tableRows = "";
    data.results.forEach(r => {
        const isFieldPass = r.status === "PASS" || r.status === "OCR_AMBIGUITY";
        const valClass = isFieldPass ? "extracted-pass" : "extracted-fail";
        const badgeClass = r.status.toLowerCase();
        const displayStatus = STATUS_LABELS[r.status] || r.status;

        tableRows += `
            <tr>
                <td>
                    <div class="field-name-cell">${escapeHtml(r.field)}</div>
                    <span class="field-constraint-tag">${escapeHtml(r.constraint_label)}</span>
                    <div style="font-size: 0.8rem; color: var(--text-muted); margin-top: 5px;">
                        ${escapeHtml(r.detail)}
                    </div>
                    ${r.snippet ? `
                    <div style="font-size: 0.75rem; color: var(--text-muted); font-style: italic; margin-top: 4px; border-left: 2px solid var(--border); padding-left: 6px;">
                        Page ${r.page || 1}: "${escapeHtml(r.snippet)}"
                    </div>` : ''}
                </td>
                <td class="val-cell">${escapeHtml(r.user_value)}</td>
                <td class="val-cell ${valClass}">
                    ${r.status === 'NOT_FOUND' ? '<span style="color: var(--text-muted); font-style: italic;">Not visible in document</span>' : escapeHtml(r.extracted_value)}
                </td>
                <td>
                    <span class="status-chip ${badgeClass}">${displayStatus}</span>
                </td>
            </tr>
        `;
    });

    container.innerHTML = `
        <div class="results-area">
            <div class="verdict-banner ${statusClass}">
                <div class="verdict-title ${statusClass}">
                    <span>${statusText}</span>
                </div>
                <div class="verdict-score">
                    ${summary.passed_count}/${summary.total_fields} Passed (${summary.match_percentage}%)
                </div>
            </div>

            <div class="results-grid-box">
                <div class="results-preview-card">
                    <div style="font-weight: 600; font-size: 0.95rem; margin-bottom: 12px;">Document Preview</div>
                    <div class="preview-thumb" onclick="openZoomModal('${data.preview_url}')">
                        <img src="${data.preview_url}" alt="Uploaded Document">
                    </div>
                </div>

                <div class="results-table-card">
                    <table class="results-table">
                        <thead>
                            <tr>
                                <th>Field</th>
                                <th>Expected</th>
                                <th>Extracted</th>
                                <th>Status</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${tableRows}
                        </tbody>
                    </table>
                </div>
            </div>

            <div class="results-btn-bar">
                <button type="button" class="btn-secondary" onclick="window.print()">Print / Save PDF</button>
                <a href="/download/report" class="btn-secondary">Download Report (.txt)</a>
                <a href="/download/json" class="btn-secondary">Export JSON</a>
            </div>
        </div>
    `;

    container.scrollIntoView({ behavior: "smooth" });
}

/**
 * Image Zoom Modal
 */
function setupImageModal() {
    const modal = document.getElementById("zoomModal");
    const closeBtn = document.getElementById("zoomModalClose");
    if (!modal) return;

    if (closeBtn) closeBtn.addEventListener("click", () => modal.classList.remove("active"));
    modal.addEventListener("click", (e) => {
        if (e.target === modal) modal.classList.remove("active");
    });
}

function openZoomModal(src) {
    const modal = document.getElementById("zoomModal");
    const img = document.getElementById("zoomModalImg");
    if (modal && img) {
        img.src = src;
        modal.classList.add("active");
    }
}

/**
 * Error Alert Utilities
 */
function showError(msg) {
    const alert = document.getElementById("errorContainer");
    const text = document.getElementById("errorMessageText");
    if (alert && text) {
        text.textContent = msg;
        alert.style.display = "flex";
        alert.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
}

function hideError() {
    const alert = document.getElementById("errorContainer");
    if (alert) alert.style.display = "none";
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
