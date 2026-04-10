/**
 * Shared utility functions used by both viewer.js and process_viewer.js.
 *
 * All functions receive context (csrfToken, docId, etc.) as parameters
 * to avoid coupling to a specific viewer's closure.
 */

/**
 * Mark a PDF page for deletion during reprocessing.
 * Shows a confirmation dialog before proceeding.
 *
 * @param {string} csrfToken - CSRF token for POST requests.
 * @param {number} docId - Scan primary key.
 * @param {number} pdfPage - 1-based PDF page number.
 * @param {HTMLElement} pageDiv - The page container element.
 * @param {string} [labelPrefix="Page"] - Prefix for the deletion label.
 * @param {Function} [onDelete] - Callback to re-bind delete handler after undo.
 */
function deletePage(csrfToken, docId, pdfPage, pageDiv, labelPrefix, onDelete) {
    labelPrefix = labelPrefix || 'Page';
    if (!confirm('Mark ' + labelPrefix + ' ' + pdfPage + ' for deletion?')) {
        return;
    }
    fetch('/scans/' + docId + '/delete-page/', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
        body: JSON.stringify({ pdf_page: pdfPage }),
    })
    .then(function (r) { return r.json(); })
    .then(function (data) {
        if (data.status === 'ok') {
            markPageAsDeleted(pageDiv, pdfPage, labelPrefix, csrfToken, docId, onDelete);
        }
    });
}

/**
 * Apply the "marked for deletion" visual treatment to a page and
 * add an Undo button.
 *
 * @param {HTMLElement} pageDiv - The page container element.
 * @param {number} pdfPage - 1-based PDF page number.
 * @param {string} labelPrefix - Prefix for the label text.
 * @param {string} csrfToken - CSRF token for POST requests.
 * @param {number} docId - Scan primary key.
 * @param {Function} [onDelete] - Callback to re-bind delete handler after undo.
 */
function markPageAsDeleted(pageDiv, pdfPage, labelPrefix, csrfToken, docId, onDelete) {
    pageDiv.style.opacity = '0.3';
    pageDiv.style.pointerEvents = 'none';
    var label = pageDiv.querySelector('.page-label');
    if (label) {
        // Save original content for undo
        if (!label.dataset.originalHtml) {
            label.dataset.originalHtml = label.innerHTML;
        }
        label.innerHTML =
            '<span>' + labelPrefix + ' ' + pdfPage + ' &mdash; MARKED FOR DELETION</span> ' +
            '<button class="undo-delete-btn" style="pointer-events:auto;cursor:pointer;' +
            'background:#dc2626;color:white;border:none;border-radius:3px;padding:1px 6px;' +
            'font-size:10px;margin-left:4px">Undo Delete</button>';
        var undoBtn = label.querySelector('.undo-delete-btn');
        undoBtn.addEventListener('click', function (e) {
            e.stopPropagation();
            undoDeletePage(csrfToken, docId, pdfPage, pageDiv, labelPrefix, onDelete);
        });
    }
}

/**
 * Undo a page deletion, restoring the page to its original state.
 *
 * @param {string} csrfToken - CSRF token for POST requests.
 * @param {number} docId - Scan primary key.
 * @param {number} pdfPage - 1-based PDF page number.
 * @param {HTMLElement} pageDiv - The page container element.
 * @param {string} [labelPrefix="Page"] - Prefix for the label text.
 * @param {Function} [onDelete] - Callback to re-bind the delete handler.
 */
function undoDeletePage(csrfToken, docId, pdfPage, pageDiv, labelPrefix, onDelete) {
    labelPrefix = labelPrefix || 'Page';
    fetch('/scans/' + docId + '/undo-delete-page/', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
        body: JSON.stringify({ pdf_page: pdfPage }),
    })
    .then(function (r) { return r.json(); })
    .then(function (data) {
        if (data.status === 'ok') {
            pageDiv.style.opacity = '1';
            pageDiv.style.pointerEvents = 'auto';
            var label = pageDiv.querySelector('.page-label');
            if (label && label.dataset.originalHtml) {
                label.innerHTML = label.dataset.originalHtml;
                // Re-attach delete button handler (innerHTML destroys listeners)
                var deleteBtn = label.querySelector('.delete-btn');
                if (deleteBtn && onDelete) {
                    deleteBtn.addEventListener('click', function () {
                        onDelete(pdfPage, pageDiv);
                    });
                }
            }
        }
    });
}

/**
 * Draw existing redaction rectangles on a canvas overlay.
 *
 * @param {HTMLCanvasElement} overlay - The canvas element.
 * @param {Array} pageRedactions - List of {x, y, width, height, fill} objects.
 * @param {number} scale - Scale factor for coordinates.
 */
function drawExistingRedactions(overlay, pageRedactions, scale) {
    var ctx = overlay.getContext('2d');
    pageRedactions.forEach(function (r) {
        if (r.fill === 'white') {
            ctx.fillStyle = 'rgba(255,255,255,0.9)';
            ctx.fillRect(r.x * scale, r.y * scale, r.width * scale, r.height * scale);
            ctx.strokeStyle = '#3b82f6';
            ctx.lineWidth = 1;
            ctx.setLineDash([4, 3]);
            ctx.strokeRect(r.x * scale, r.y * scale, r.width * scale, r.height * scale);
            ctx.setLineDash([]);
        } else {
            ctx.fillStyle = 'rgba(0,0,0,0.85)';
            ctx.fillRect(r.x * scale, r.y * scale, r.width * scale, r.height * scale);
            ctx.strokeStyle = '#ef4444';
            ctx.lineWidth = 1;
            ctx.strokeRect(r.x * scale, r.y * scale, r.width * scale, r.height * scale);
        }
    });
}
