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
 * Show a stacking toast notification that auto-dismisses after 5 seconds.
 *
 * @param {string} message - Text to display.
 * @param {string} [type="error"] - "error" (red) or "info" (blue).
 */
function showToast(message, type) {
    type = type || 'error';
    var container = document.getElementById('toast-container');
    if (!container) {
        container = document.createElement('div');
        container.id = 'toast-container';
        container.style.cssText = 'position:fixed;bottom:16px;right:16px;display:flex;flex-direction:column;gap:8px;z-index:9999;pointer-events:none;';
        document.body.appendChild(container);
    }
    var toast = document.createElement('div');
    var bg = type === 'error' ? '#dc2626' : '#2563eb';
    toast.style.cssText = 'background:' + bg + ';color:#fff;padding:10px 16px;border-radius:6px;font-size:13px;box-shadow:0 2px 8px rgba(0,0,0,0.3);opacity:1;transition:opacity 0.4s;max-width:320px;pointer-events:auto;display:flex;align-items:center;gap:10px;';

    var text = document.createElement('span');
    text.style.flex = '1';
    text.textContent = message;
    toast.appendChild(text);

    var closeBtn = document.createElement('button');
    closeBtn.textContent = '✕';
    closeBtn.style.cssText = 'background:none;border:none;color:#fff;cursor:pointer;font-size:14px;padding:0;line-height:1;opacity:0.8;flex-shrink:0;';
    closeBtn.addEventListener('click', function () { dismiss(); });
    toast.appendChild(closeBtn);

    container.appendChild(toast);

    function dismiss() {
        toast.style.opacity = '0';
        setTimeout(function () { toast.remove(); }, 400);
    }

    var timer = setTimeout(dismiss, 5000);
    closeBtn.addEventListener('mouseenter', function () { clearTimeout(timer); });
    closeBtn.addEventListener('mouseleave', function () { timer = setTimeout(dismiss, 2000); });
}

/**
 * Convert a mouse event into the target's natural pixel coordinates.
 * Robust to any CSS zoom or transform applied to the target's ancestors,
 * which is what the PDF viewer's zoom controls rely on.
 *
 * @param {MouseEvent} e - The mouse event.
 * @param {HTMLCanvasElement|HTMLElement} target - Element to map into; canvas
 *   targets use canvas.width/height, others fall back to offsetWidth/Height.
 * @returns {{x: number, y: number}} Coordinates in target natural pixels.
 */
function eventToCanvasPixels(e, target) {
    var rect = target.getBoundingClientRect();
    var w = target.width || target.offsetWidth || rect.width || 1;
    var h = target.height || target.offsetHeight || rect.height || 1;
    var sx = rect.width ? w / rect.width : 1;
    var sy = rect.height ? h / rect.height : 1;
    return {
        x: (e.clientX - rect.left) * sx,
        y: (e.clientY - rect.top) * sy,
    };
}

// PDF viewer zoom controls.
var PDF_ZOOM_MIN = 0.5;
var PDF_ZOOM_MAX = 3;
var PDF_ZOOM_STEP = 0.1;

/**
 * Read the persisted PDF viewer zoom level (multiplier; 1 = unzoomed).
 *
 * @returns {number} Zoom level clamped to [PDF_ZOOM_MIN, PDF_ZOOM_MAX].
 */
function getPdfZoom() {
    var v = parseFloat(localStorage.getItem('pdfZoom'));
    if (!isFinite(v) || v <= 0) return 1;
    return Math.max(PDF_ZOOM_MIN, Math.min(PDF_ZOOM_MAX, v));
}

/**
 * Apply the current zoom level to a single rendered page container.
 * The wrapper is scaled with a CSS transform so detection boxes,
 * redaction overlays, and image overlays (all positioned absolutely
 * inside the wrapper) inherit the visual scale automatically. The
 * page-container is sized to the scaled dimensions to reserve layout
 * space without re-rendering pdf.js canvases.
 *
 * @param {HTMLElement} pageDiv - The .page-container element.
 */
function applyZoomToPage(pageDiv) {
    if (!pageDiv) return;
    var wrapper = pageDiv.querySelector('.canvas-wrapper');
    if (!wrapper) return;
    var canvas = wrapper.querySelector('.pdf-canvas');
    if (!canvas || !canvas.width) return;
    var zoom = getPdfZoom();
    if (zoom === 1) {
        wrapper.style.transform = '';
        wrapper.style.transformOrigin = '';
        pageDiv.style.width = '';
        pageDiv.style.height = '';
        return;
    }
    wrapper.style.transformOrigin = 'top left';
    wrapper.style.transform = 'scale(' + zoom + ')';
    var label = pageDiv.querySelector('.page-label');
    var labelH = label ? label.offsetHeight : 0;
    pageDiv.style.width = (canvas.width * zoom) + 'px';
    pageDiv.style.height = (labelH + canvas.height * zoom) + 'px';
}

/**
 * Re-apply the current zoom level to every rendered page and refresh
 * the toolbar percentage display.
 */
function applyZoomAll() {
    document.querySelectorAll('#pdf-viewer .page-container').forEach(applyZoomToPage);
    var pct = document.getElementById('zoom-pct');
    if (pct) pct.textContent = Math.round(getPdfZoom() * 100) + '%';
}

/**
 * Set and persist the PDF viewer zoom level.
 *
 * @param {number} zoom - Desired zoom multiplier; clamped and rounded.
 */
function setPdfZoom(zoom) {
    zoom = Math.max(PDF_ZOOM_MIN, Math.min(PDF_ZOOM_MAX, zoom));
    zoom = Math.round(zoom * 100) / 100;
    localStorage.setItem('pdfZoom', String(zoom));
    applyZoomAll();
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
