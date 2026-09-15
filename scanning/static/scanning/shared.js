/**
 * Shared utility functions used by both viewer.js and process_viewer.js.
 *
 * All functions receive context (csrfToken, docId, etc.) as parameters
 * to avoid coupling to a specific viewer's closure.
 */

/**
 * Load a scan preview PDF, handling the "still processing" (HTTP 202) state.
 *
 * Fetches the URL instead of handing it straight to pdf.js so a non-PDF
 * "not ready" response becomes a friendly message rather than a hard error.
 * When the server says the preview isn't ready yet, it shows the message and
 * quietly re-checks on an interval until the PDF appears, then renders it.
 * The server only ever serves the small bitonal/OCR preview here, so loading
 * the whole file into memory is fine.
 *
 * @param {string} url - The scan PDF endpoint URL.
 * @param {Object} cb - Callbacks.
 * @param {Function} cb.onReady - (pdfDoc, previewKind) => void, the loaded
 *   pdf.js document plus the server's X-Scan-Preview header ("bitonal",
 *   "original", or null), so the caller can tell a lower-quality preview
 *   from the original.
 * @param {Function} cb.onNotReady - (message, data) => void, shown while
 *   processing; data is the server's JSON body (may be undefined), whose
 *   original_available flag says a "load the original" action is possible.
 * @param {Function} cb.onError - (error) => void, on a real failure.
 * @param {Function} [cb.isCurrent] - () => bool; return false to abort (e.g.
 *   the viewer switched to a different URL). Aborted work renders nothing.
 * @param {number} [cb.pollMs=4000] - Re-check interval while not ready.
 * @param {number} [cb.errorRetries=3] - Transient-error retries before giving
 *   up (covers e.g. an opinion PDF briefly 404ing right after generation).
 * @returns {{cancel: Function}} Handle; call cancel() to stop polling.
 */
function loadPreviewPdf(url, cb) {
    var cancelled = false;
    var timer = null;
    var pollMs = cb.pollMs || 4000;
    var maxErrorRetries = cb.errorRetries == null ? 3 : cb.errorRetries;
    var errorRetriesLeft = maxErrorRetries;
    // The last 202/409 JSON body. Retry messages pass it through, so a
    // "load the original" button offered by the previous answer does
    // not blink away during each retry.
    var lastNotReadyData = null;

    function current() {
        return !cancelled && (!cb.isCurrent || cb.isCurrent());
    }

    function onTransientError(err) {
        if (!current()) return;
        if (errorRetriesLeft > 0) {
            var n = maxErrorRetries - errorRetriesLeft + 1;
            errorRetriesLeft--;
            cb.onNotReady(
                'Connection problem. Trying again (' +
                n + ' of ' + maxErrorRetries + ')...',
                lastNotReadyData
            );
            timer = setTimeout(attempt, n * 1000);
            return;
        }
        cb.onError(err);
    }

    function attempt() {
        if (!current()) return;
        var previewKind = null;
        fetch(url, { headers: { Accept: 'application/pdf' } })
            .then(function (resp) {
                if (!current()) return null;
                if (resp.status === 202) {
                    // Transient: a preview is still being produced. A healthy
                    // poll response resets the retry budget so it caps
                    // *consecutive* transient errors, not total errors across
                    // a long (minutes-long) poll session. Then show the
                    // message and poll again.
                    errorRetriesLeft = maxErrorRetries;
                    return resp.json().then(function (d) {
                        if (!current()) return null;
                        lastNotReadyData = d;
                        cb.onNotReady((d && d.message) || 'Still processing…', d);
                        timer = setTimeout(attempt, pollMs);
                        return null;
                    });
                }
                if (resp.status === 409) {
                    // Terminal: no preview will ever appear (errored/
                    // unavailable). Show the message and stop -- do not poll.
                    return resp.json().then(function (d) {
                        if (!current()) return null;
                        lastNotReadyData = d;
                        cb.onNotReady((d && d.message) || 'No preview available.', d);
                        return null;
                    });
                }
                if (!resp.ok) throw new Error('HTTP ' + resp.status);
                previewKind = resp.headers.get('X-Scan-Preview');
                return resp.arrayBuffer();
            })
            .then(function (buf) {
                if (buf === null || !current()) return null;
                return pdfjsLib.getDocument({ data: buf }).promise.then(
                    function (pdf) { if (current()) cb.onReady(pdf, previewKind); }
                );
            })
            .catch(onTransientError);
    }

    attempt();
    return {
        cancel: function () {
            cancelled = true;
            if (timer) { clearTimeout(timer); timer = null; }
        },
    };
}

/**
 * Replace an element's contents with a single centered status message,
 * inserting the text safely (no HTML interpretation).
 *
 * @param {HTMLElement} container - The element to fill.
 * @param {string} message - The status text to show.
 */
function showViewerMessage(container, message) {
    var div = document.createElement('div');
    div.className = 'viewer-loading';
    div.textContent = message;
    container.replaceChildren(div);
}

/**
 * Show a wait/terminal message with an optional action button under it.
 *
 * Used for the "no preview yet" states (issue #185): the message explains
 * the stage, and the button offers to load the original scan instead.
 *
 * @param {HTMLElement} container - The element to fill.
 * @param {string} message - The status text to show.
 * @param {Object} [opts] - Optional action.
 * @param {string} [opts.buttonLabel] - Button text; no button if absent.
 * @param {string} [opts.note] - Small disclaimer under the button.
 * @param {Function} [opts.onButton] - Click handler for the button.
 */
function showViewerWait(container, message, opts) {
    opts = opts || {};
    var wrap = document.createElement('div');
    wrap.className = 'viewer-loading';
    wrap.style.cssText = 'display:flex;flex-direction:column;align-items:center;gap:10px;text-align:center;padding:24px;';

    var text = document.createElement('div');
    text.textContent = message;
    text.style.maxWidth = '460px';
    wrap.appendChild(text);

    if (opts.buttonLabel && opts.onButton) {
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'btn-outline text-sm';
        btn.textContent = opts.buttonLabel;
        btn.addEventListener('click', opts.onButton);
        wrap.appendChild(btn);
        if (opts.note) {
            var note = document.createElement('div');
            note.textContent = opts.note;
            note.style.cssText = 'font-size:11px;opacity:0.7;max-width:460px;';
            wrap.appendChild(note);
        }
    }
    container.replaceChildren(wrap);
}

/**
 * Load a scan's ORIGINAL PDF into pdf.js, straight from storage.
 *
 * Asks the server for a URL (a presigned S3 GET in prod, a local stream
 * in dev), then hands the URL to pdf.js. In prod pdf.js reads the
 * (multi-GB) file with HTTP range requests, so only the visible pages
 * cross the network and the web pod is not in the data path (issue #185).
 *
 * The S3 read needs a CORS rule on the bucket (GET/HEAD allowed, range
 * headers exposed — infrastructure #808). Without it the load fails in
 * the browser and onFail fires; callers show an explicit message with a
 * direct link, since a top-level navigation is not subject to CORS.
 *
 * @param {number|string} docId - Scan primary key.
 * @param {Object} cb - Callbacks.
 * @param {Function} cb.onReady - (pdfDoc) => void on success.
 * @param {Function} cb.onFail - (error, url) => void; url is the direct
 *   link to offer (null when even the URL fetch failed).
 */
function loadOriginalPdf(docId, cb, opts) {
    // opts.final asks for the corrected volume of the standing apply
    // run (#269), which step 2 shows; the review-1 space otherwise.
    var query = (opts && opts.final) ? '?space=final' : '';
    fetch('/scans/' + docId + '/original-url/' + query)
        .then(function (resp) {
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            return resp.json();
        })
        .then(function (d) {
            var opts = { url: d.url };
            if (!d.embedded_whole) {
                // Range-request mode: fetch only the parts pdf.js needs.
                // disableStream is load-bearing: with streaming on,
                // pdf.js reads the WHOLE response into the worker and
                // re-derives disableAutoFetch on its own, so without it
                // the full multi-GB file still downloads.
                opts.disableAutoFetch = true;
                opts.disableStream = true;
                opts.rangeChunkSize = 262144;
            }
            return pdfjsLib.getDocument(opts).promise.then(
                function (pdf) { cb.onReady(pdf); },
                function (err) {
                    console.error('Original PDF load failed:', err);
                    cb.onFail(err, d.url);
                }
            );
        })
        .catch(function (err) {
            console.error('Original PDF URL fetch failed:', err);
            cb.onFail(err, null);
        });
}

/**
 * Show the explicit in-page failure message for an original-PDF load,
 * with a direct link that opens the file in a new tab. A top-level
 * navigation is not subject to CORS, so the link works even when the
 * embedded read was blocked (bucket CORS not deployed yet).
 *
 * @param {HTMLElement} container - The viewer element to fill.
 * @param {string|null} url - Direct URL to the original, if known.
 */
function showOriginalLoadFailure(container, url) {
    var wrap = document.createElement('div');
    wrap.className = 'viewer-loading';
    wrap.style.cssText = 'display:flex;flex-direction:column;align-items:center;gap:10px;text-align:center;padding:24px;';

    var text = document.createElement('div');
    text.textContent = url
        ? 'Your browser could not load the original in this page. Open it in a new tab instead.'
        : 'The original PDF could not be loaded. Reload the page and try again.';
    text.style.maxWidth = '460px';
    wrap.appendChild(text);

    if (url) {
        var link = document.createElement('a');
        link.href = url;
        link.target = '_blank';
        link.rel = 'noopener';
        link.className = 'btn-outline text-sm';
        link.textContent = 'Open the original PDF in a new tab';
        wrap.appendChild(link);
    }
    container.replaceChildren(wrap);
}

/**
 * Show or hide the "lower-quality preview" banner above the viewer.
 *
 * Fills #preview-banner when the served PDF is the bitonal preview, so
 * the user knows the conversion finished and can load the original
 * (issue #185). Hides the banner for any other load (original, opinion
 * PDFs). No-op when the page has no banner element.
 *
 * @param {string|null} previewKind - The X-Scan-Preview header value.
 * @param {Function} onLoadOriginal - Called when the user asks for the
 *   original; the active viewer swaps its document in place.
 */
function renderPreviewBanner(previewKind, onLoadOriginal) {
    var banner = document.getElementById('preview-banner');
    if (!banner) return;
    if (previewKind !== 'bitonal') {
        banner.hidden = true;
        banner.replaceChildren();
        return;
    }

    var text = document.createElement('span');
    text.textContent =
        'You see a smaller, lower-quality preview, so pages load fast.';

    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'underline font-medium';
    btn.style.cssText = 'background:none;border:none;cursor:pointer;color:inherit;padding:0;font-size:inherit;';
    btn.textContent = 'Load the original scan (large, can be slow)';
    btn.addEventListener('click', function () {
        banner.hidden = true;
        onLoadOriginal();
    });

    var close = document.createElement('button');
    close.type = 'button';
    close.setAttribute('aria-label', 'Dismiss');
    close.textContent = '✕';
    close.style.cssText = 'background:none;border:none;cursor:pointer;color:inherit;margin-left:auto;padding:0 4px;';
    close.addEventListener('click', function () { banner.hidden = true; });

    banner.replaceChildren(text, btn, close);
    banner.hidden = false;
}

// What a curator may type as a page number, in the one place both
// viewers read (issue #319). The three shapes a book prints: one
// number; one number with one trailing letter, on the page the book
// adds between two numbered pages ("2094a"); and the range one
// physical page carries when the book prints several pages on it
// (#233). The server takes the same three and stores a range with a
// hyphen, whatever dash was typed, so the dashes here are the ones a
// reporter prints and a person pastes.
var PAGE_ENTRY_RE = /^(\d{1,4})(?:([A-Za-z])|\s*[-–—]\s*(\d{1,4}))?$/;

// The prompt, and the refusal. The server carries the same words in
// ``views_process.PAGE_NUMBER_ERROR``.
var PAGE_NUMBER_PROMPT =
    ' (a number with a trailing letter like 2094a, or a range like ' +
    '913-925 if this page carries several book pages; leave blank if ' +
    'it has no number):';
var PAGE_NUMBER_ERROR =
    'Page number must be a positive whole number, a number with one ' +
    'trailing letter like 2094a, or a range like 678-686.';

/**
 * Judge a page number a curator typed, before the request is sent.
 *
 * The one gate of the two viewers: both post to ``assign_page``, and
 * the server refuses what this refuses.
 *
 * @param {string} text - The trimmed entry. Never the empty string,
 *     which is the curator clearing the number.
 * @returns {boolean} Whether the entry is one of the three shapes.
 */
function isPageNumberEntry(text) {
    var parts = PAGE_ENTRY_RE.exec(text);
    if (!parts) return false;
    var first = parseInt(parts[1], 10);
    if (first < 1) return false;
    if (parts[3] === undefined) return true;
    return first < parseInt(parts[3], 10);
}

/**
 * Escape text for safe interpolation into an innerHTML string.
 *
 * A page label can be a person's typing: the printed number a curator
 * files an uploaded page under is free text, because printed numbers
 * are not always digits ("xiv", "A-3", "1075a"), and every viewer of
 * the scan sees it. Use this on any value that came from a person or
 * a server response before it is concatenated into markup.
 *
 * @param {*} value - The value to escape. Coerced to a string.
 * @returns {string} The value with the five markup characters escaped.
 */
function escapeHtml(value) {
    return String(value === null || value === undefined ? '' : value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

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
 *
 * On success this calls the optional `window.onPageEditSaved` hook, as
 * `undoDeletePage` does. Step 1 defines it to confirm the save to the
 * curator (issue #151); step 2 leaves it undefined and shows nothing.
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
            if (typeof window.refreshProcessActionBar === 'function') {
                window.refreshProcessActionBar();
            }
            if (typeof window.onPageEditSaved === 'function') {
                window.onPageEditSaved();
            }
        } else {
            // A locked review answers 409 with the reason (#224).
            showToast(data.error || 'Could not mark this page for deletion.');
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
        // The page edits are locked once review 1 is approved (#224),
        // and an applied deletion still stands, so this runs on load
        // for a locked volume too: it must not offer the one control
        // the endpoint refuses.
        var locked = typeof SCAN_CONFIG !== 'undefined' && SCAN_CONFIG.pageEditsLocked === true;
        label.innerHTML =
            '<span>' + labelPrefix + ' ' + pdfPage + ' &mdash; MARKED FOR DELETION</span> ' +
            (locked ? '' :
            '<button class="undo-delete-btn" style="pointer-events:auto;cursor:pointer;' +
            'background:#dc2626;color:white;border:none;border-radius:3px;padding:1px 6px;' +
            'font-size:10px;margin-left:4px">Undo Delete</button>');
        var undoBtn = label.querySelector('.undo-delete-btn');
        if (undoBtn) {
            undoBtn.addEventListener('click', function (e) {
                e.stopPropagation();
                undoDeletePage(csrfToken, docId, pdfPage, pageDiv, labelPrefix, onDelete);
            });
        }
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
            if (typeof window.refreshProcessActionBar === 'function') {
                window.refreshProcessActionBar();
            }
            if (typeof window.onPageEditSaved === 'function') {
                window.onPageEditSaved();
            }
        } else {
            showToast(data.error || 'Could not take the deletion back.');
        }
    });
}

/**
 * Show a stacking toast notification that auto-dismisses after 5 seconds.
 *
 * The same text in a toast that still stands restarts that toast's
 * timer and adds no second card (#322): a drag over four resize
 * handles saves four times, and four copies of one line would bury the
 * page.
 *
 * @param {string} message - Text to display.
 * @param {string} [type="error"] - "error" (red), "info" (blue),
 *     "success" (green), or "warning" (amber).
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
    var key = type + '\n' + message;
    for (var i = 0; i < container.children.length; i++) {
        var standing = container.children[i];
        if (standing.dataset.toastKey === key && standing._restart) {
            standing._restart();
            return;
        }
    }
    var toast = document.createElement('div');
    toast.dataset.toastKey = key;
    var bg = '#2563eb';
    if (type === 'error') { bg = '#dc2626'; }
    else if (type === 'success') { bg = '#059669'; }
    else if (type === 'warning') { bg = '#d97706'; }
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

    // The removal is held, because the restart below must cancel it: a
    // repeat that lands in the 400 ms of the fade would else come back
    // to full opacity and be taken off the page anyway (#322).
    var removal = null;

    function dismiss() {
        toast.style.opacity = '0';
        removal = setTimeout(function () { toast.remove(); }, 400);
    }

    var timer = setTimeout(dismiss, 5000);
    toast._restart = function () {
        clearTimeout(timer);
        clearTimeout(removal);
        toast.style.opacity = '1';
        timer = setTimeout(dismiss, 5000);
    };
    closeBtn.addEventListener('mouseenter', function () { clearTimeout(timer); });
    closeBtn.addEventListener('mouseleave', function () { timer = setTimeout(dismiss, 2000); });
}

// The key the saved line waits under while the page reloads (#322).
var SAVED_TOAST_KEY = 'scanning.savedToast';

/**
 * Show the server's success line as a toast (#322).
 *
 * Every write of review 2 answers ``{status: "ok", message}``, and the
 * message is the one record of what the server kept: a moved box stays
 * where the mouse left it, so the page looks the same after a save and
 * after a refusal. The text comes from the view, which knows what it
 * wrote; a call site adds none of its own.
 *
 * @param {Object} data - The parsed answer of the endpoint.
 */
function showSaved(data) {
    if (data && data.message) { showToast(data.message, 'success'); }
}

/**
 * Keep the server's success line over a page reload (#322).
 *
 * The boundary writes and the recompute reload the page, which would
 * throw a toast away. The line waits in ``sessionStorage`` and the
 * loaded page shows it. Session storage throws in a private window, so
 * every access is guarded; a lost message costs the reader the line,
 * not the page.
 *
 * @param {Object} data - The parsed answer of the endpoint.
 */
function showSavedAfterReload(data) {
    if (!data || !data.message) { return; }
    try {
        window.sessionStorage.setItem(SAVED_TOAST_KEY, data.message);
    } catch (e) {
        showToast(data.message, 'success');
    }
}

/**
 * Show the line a write left before it reloaded the page (#322).
 */
function flushSavedToast() {
    var message = null;
    try {
        message = window.sessionStorage.getItem(SAVED_TOAST_KEY);
        window.sessionStorage.removeItem(SAVED_TOAST_KEY);
    } catch (e) {
        return;
    }
    if (message) { showToast(message, 'success'); }
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', flushSavedToast);
} else {
    flushSavedToast();
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
    var naturalW = parseFloat(wrapper.style.width) || canvas.offsetWidth || canvas.width;
    var naturalH = parseFloat(wrapper.style.height) || canvas.offsetHeight || canvas.height;
    if (Math.abs(visualZoom - 1) < 0.001) {
        wrapper.style.transform = '';
        wrapper.style.transformOrigin = '';
        // Pin the page-container to the canvas width so that the page-label's
        // toolbar (Detections, Redact, etc.) cannot stretch the container
        // wider than the rendered page at small zooms.
        pageDiv.style.width = naturalW + 'px';
        pageDiv.style.height = '';
        return;
    }
    wrapper.style.transformOrigin = 'top left';
    wrapper.style.transform = 'scale(' + visualZoom + ')';
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

// Jump to a lazy-rendered page element without a scroll animation. Pages start
// at a placeholder height and grow to their true height once rasterized, which
// shifts everything below them; so after jumping we re-align until the target
// stops moving (capped) instead of animating through every page in between.
window.scrollPageIntoView = function (el) {
    if (!el) return;
    var tries = 0;
    (function settle() {
        var before = el.getBoundingClientRect().top;
        el.scrollIntoView({ block: 'start' });
        var after = el.getBoundingClientRect().top;
        if (++tries < 10 && Math.abs(after - before) > 2) {
            setTimeout(settle, 90);
        }
    })();
};
