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
 * Convert a mouse event into the target's CSS-pixel layout coordinates,
 * relative to the target's top-left corner. Robust to any CSS zoom or
 * transform applied to ancestors, since the conversion is derived from
 * the displayed bounding rect rather than the bitmap dimensions.
 *
 * For canvas targets, returns coords in the canvas's CSS layout space
 * (matching style.width/height, or the bitmap size when style is unset).
 * Pair with `pageScale(pageDiv)` to convert to PDF points.
 *
 * @param {MouseEvent} e - The mouse event.
 * @param {HTMLCanvasElement|HTMLElement} target - Element to map into.
 * @returns {{x: number, y: number}} Coordinates in target CSS layout pixels.
 */
function eventToCanvasPixels(e, target) {
    var rect = target.getBoundingClientRect();
    var w = target.offsetWidth || target.width || rect.width || 1;
    var h = target.offsetHeight || target.height || rect.height || 1;
    var sx = rect.width ? w / rect.width : 1;
    var sy = rect.height ? h / rect.height : 1;
    return {
        x: (e.clientX - rect.left) * sx,
        y: (e.clientY - rect.top) * sy,
    };
}

// PDF viewer zoom controls. The active viewer (step1 vs step2/3) sets
// `window.__pdfZoomKey` at script-load time so each viewer persists its
// own zoom level independently in localStorage.
var PDF_ZOOM_MIN = 0.5;
var PDF_ZOOM_MAX = 3;
var PDF_ZOOM_STEP = 0.1;

function _pdfZoomKey() { return window.__pdfZoomKey || 'pdfZoom'; }

/**
 * Read the persisted PDF viewer zoom level (multiplier; 1 = unzoomed).
 *
 * @returns {number} Zoom level clamped to [PDF_ZOOM_MIN, PDF_ZOOM_MAX].
 */
function getPdfZoom() {
    var v = parseFloat(localStorage.getItem(_pdfZoomKey()));
    if (!isFinite(v) || v <= 0) return 1;
    return Math.max(PDF_ZOOM_MIN, Math.min(PDF_ZOOM_MAX, v));
}

/**
 * Effective render scale at which a page's canvas was rasterized,
 * i.e. SCALE_base * renderedZoom. Used as the divisor when converting
 * canvas-CSS pixels back to PDF points.
 *
 * @param {HTMLElement} pageDiv - The .page-container element.
 * @param {number} [fallback=1] - Returned when the page hasn't been
 *   rendered yet (no `data-scale` attribute).
 * @returns {number} The effective scale.
 */
function pageScale(pageDiv, fallback) {
    var v = pageDiv ? parseFloat(pageDiv.dataset.scale) : NaN;
    return isFinite(v) && v > 0 ? v : (fallback || 1);
}

/**
 * The zoom level at which a page was last rasterized. Used to compute
 * the visual transform ratio when the active zoom diverges from the
 * rendered zoom (e.g. before a re-render lands).
 *
 * @param {HTMLElement} pageDiv - The .page-container element.
 * @returns {number} The rendered zoom (defaults to 1).
 */
function pageRenderedZoom(pageDiv) {
    var v = pageDiv ? parseFloat(pageDiv.dataset.renderedZoom) : NaN;
    return isFinite(v) && v > 0 ? v : 1;
}

/**
 * Ratio of an element's visual size on screen to its CSS layout size,
 * i.e. the cumulative scale of any CSS transforms on its ancestors.
 * Use this to convert screen-pixel mouse deltas into the natural CSS
 * pixel space the element's `style.left/top/width/height` live in,
 * for drag handlers that operate while a wrapper is mid-zoom (between
 * the immediate visual transform and the post-debounce re-render).
 *
 * @param {HTMLElement} el - The element being dragged.
 * @returns {number} visual / natural ratio (1 if no transform).
 */
function cssToVisualScale(el) {
    if (!el) return 1;
    var rect = el.getBoundingClientRect();
    var nat = el.offsetWidth || 1;
    return rect.width && nat ? rect.width / nat : 1;
}

/**
 * Apply the active zoom level to a single rendered page container by
 * applying a CSS `transform: scale(visualZoom)` to its canvas wrapper.
 * `visualZoom` is the ratio between the active zoom and the zoom at
 * which the page was last rasterized, so when a re-render lands at the
 * new zoom the transform reduces to identity (no extra scaling).
 *
 * Detection boxes, redaction overlays, and image overlays all live as
 * absolutely-positioned children of the wrapper, so they inherit the
 * visual scale automatically.
 *
 * @param {HTMLElement} pageDiv - The .page-container element.
 */
function applyZoomToPage(pageDiv) {
    if (!pageDiv) return;
    var wrapper = pageDiv.querySelector('.canvas-wrapper');
    if (!wrapper) return;
    var canvas = wrapper.querySelector('.pdf-canvas');
    if (!canvas || !canvas.width) return;
    var visualZoom = getPdfZoom() / pageRenderedZoom(pageDiv);
    if (Math.abs(visualZoom - 1) < 0.001) {
        wrapper.style.transform = '';
        wrapper.style.transformOrigin = '';
        pageDiv.style.width = '';
        pageDiv.style.height = '';
        return;
    }
    wrapper.style.transformOrigin = 'top left';
    wrapper.style.transform = 'scale(' + visualZoom + ')';
    var naturalW = parseFloat(wrapper.style.width) || canvas.offsetWidth || canvas.width;
    var naturalH = parseFloat(wrapper.style.height) || canvas.offsetHeight || canvas.height;
    var label = pageDiv.querySelector('.page-label');
    var labelH = label ? label.offsetHeight : 0;
    pageDiv.style.width = (naturalW * visualZoom) + 'px';
    pageDiv.style.height = (labelH + naturalH * visualZoom) + 'px';
}

/**
 * Re-apply the active zoom level to every rendered page and refresh
 * the toolbar percentage display.
 */
function applyZoomAll() {
    document.querySelectorAll('#pdf-viewer .page-container').forEach(applyZoomToPage);
    var pct = document.getElementById('zoom-pct');
    if (pct) pct.textContent = Math.round(getPdfZoom() * 100) + '%';
}

/**
 * Set and persist the PDF viewer zoom level. Applies the visual
 * transform to all rendered pages immediately for instant feedback,
 * then asks the active viewer to re-rasterize at the new resolution
 * (debounced inside the viewer).
 *
 * @param {number} zoom - Desired zoom multiplier; clamped and rounded.
 */
function setPdfZoom(zoom) {
    zoom = Math.max(PDF_ZOOM_MIN, Math.min(PDF_ZOOM_MAX, zoom));
    zoom = Math.round(zoom * 100) / 100;
    localStorage.setItem(_pdfZoomKey(), String(zoom));
    applyZoomAll();
    if (typeof window.requestPdfRerender === 'function') {
        window.requestPdfRerender();
    }
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
