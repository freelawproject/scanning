/**
 * PDF viewer for reviewing blackletter processing output.
 * Supports switching between PDFs, redacted/unredacted toggle,
 * and drawing black/white redaction rectangles.
 */

// Steps 2 and 3 share this viewer and a single persisted zoom level,
// kept separate from step 1's zoom (page-number review benefits from
// a different zoom than opinion review).
window.__pdfZoomKey = 'pdfZoom_step2';

document.addEventListener('DOMContentLoaded', function () {
    var container = document.getElementById('pdf-viewer');
    if (!container) return;

    var initialPdfUrl = container.dataset.pdfUrl;
    var documentId = container.dataset.documentId;
    var csrfToken = document.querySelector('[name=csrfmiddlewaretoken]').value;
    var viewOnly = container.dataset.viewOnly === 'true';
    var opinionEditMode = container.dataset.opinionEdit === 'true';
    // The page edits are locked once review 1 is approved (#224).
    // Step 2 must not offer a control the endpoint refuses, the
    // rule of the step-1 bar (#151). A legacy volume is not locked
    // and keeps the control.
    var pageEditsLocked = typeof SCAN_CONFIG !== 'undefined'
        && SCAN_CONFIG.pageEditsLocked === true;
    // The page shows the corrected volume of the standing apply run
    // (#269): the original load and the crops address its pages, so
    // both routes are told which space the index is in.
    var finalSpace = typeof SCAN_CONFIG !== 'undefined'
        && SCAN_CONFIG.finalSpace === true;
    var spaceQuery = finalSpace ? '&space=final' : '';
    // The full-quality crops of the IMAGE detections are off (#278).
    // Each one is a request to /original-crop/, which pulls the
    // multi-GB original to the web pod and renders a page region at up
    // to 300 dpi; a page with several images fires them all at once.
    // The route and its code stay; this flag is the only thing that
    // stops the calls until the route is made cheaper.
    var ORIGINAL_CROPS_ENABLED = false;
    var pageMap = JSON.parse(container.dataset.pageMap || '[]');
    var flaggedIndices = JSON.parse(container.dataset.flaggedIndices || '[]');
    var ocrByPage = JSON.parse(container.dataset.ocrByPage || '{}');

    // Map page_index → logical_number from pageMap (display only)
    var _pageIndexToLogical = {};
    pageMap.forEach(function(entry) {
        if (entry.type === 'pdf_page' && entry.pdf_index !== undefined) {
            _pageIndexToLogical[entry.pdf_index] = entry.logical_number;
        }
    });
    function _pageNumForIndex(pageIndex) {
        return _pageIndexToLogical[pageIndex] || (pageIndex + 1);
    }
    // Logical numbers can repeat (e.g. unnumbered front matter falls back
    // to its PDF position, colliding with the real pages), so anything
    // keyed by page_index must resolve its page div by pdf index, never
    // through 'pv-page-<logical>' ids.
    function _pageDivForIndex(pdfIndex) {
        return container.querySelector('.lazy-page[data-pdf-index="' + pdfIndex + '"]');
    }

    // Stamp an overlay box with a stable, human-readable identity so a box
    // that looks wrong can be reported (and found in the DB) without
    // measuring pixels: hover shows it, devtools shows it, and
    // `data-box-id` is greppable in a screenshot or a bug report.
    //
    // The page number matches the on-screen "PDF p.N" label (1-based);
    // `data-pdf-index` carries the 0-based index used by the
    // Redaction rows and the detections.
    function _boxSlug(value) {
        return String(value || 'box').toLowerCase().replace(/[^a-z0-9]+/g, '_');
    }

    function _tagOverlayBox(el, kind, type, pdfIndex, seq, rect, units) {
        var pdfPage = pdfIndex + 1;
        var slug = _boxSlug(type);
        var boxId = kind + (slug ? '-' + slug : '') + '-p' + pdfPage + '-' + seq;
        el.dataset.boxId = boxId;
        el.dataset.kind = kind;
        el.dataset.type = type || '';
        el.dataset.pdfPage = pdfPage;
        el.dataset.pdfIndex = pdfIndex;
        el.dataset.seq = seq;
        var coords = '';
        if (rect) {
            coords = [rect.x0, rect.y0, rect.x1, rect.y1]
                .map(function (v) { return Math.round(v); })
                .join(',');
            el.dataset.rect = coords;
            // Detection rects are stored in image pixels, redaction rows
            // in PDF points (#240); say which so a reported number can
            // be matched against the JSON without guessing.
            el.dataset.units = units || 'px';
            // The row's primary key, when the box is one (#240).
            if (rect.id !== undefined) el.dataset.id = rect.id;
        }
        return boxId + (coords ? '  [' + coords + ' ' + (units || 'px') + ']' : '');
    }

    // Console helper for the review loop: given a box id read off a
    // tooltip, scroll to that box and outline it. Pages render lazily, so a
    // box only exists once its page has been in view and its overlay is on.
    window.findBox = function (boxId) {
        var el = container.querySelector('[data-box-id="' + boxId + '"]');
        if (!el) {
            console.warn(
                'No box "' + boxId + '" is rendered. Scroll its page into ' +
                'view with the overlay enabled, then try again.'
            );
            return null;
        }
        if (window.scrollPageIntoView) window.scrollPageIntoView(el);
        var previous = el.style.outline;
        el.style.outline = '3px solid #f59e0b';
        setTimeout(function () { el.style.outline = previous; }, 4000);
        console.log(boxId, Object.assign({}, el.dataset));
        return el;
    };

    var viewerPanel = container.closest('.viewer-panel') || container.parentElement;
    var viewerHeight = viewerPanel ? viewerPanel.clientHeight : (window.innerHeight - 200);
    // Scale so one full page (792pt letter height) fits within the viewer, never bigger
    var SCALE = Math.min(1.0, (viewerHeight - 16) / 792);
    var PLACEHOLDER_HEIGHT = Math.round(792 * SCALE);
    var pdfPages = {};  // cached PDF.js page objects for coordinate conversion
    var defaultPageWidth = 918;
    var pdfDoc = null;
    var renderedPages = {};
    var observer = null;
    var currentUrl = '';
    var _viewingOpinion = false;

    // Redaction state
    var activeRedactionDiv = null;
    var activeRedactionFill = 'black';
    var isDrawing = false;
    var startX = 0, startY = 0;

    // Detection overlay state
    var allDetections = null; // loaded once from API
    var detectionsVisible = {}; // { pdfIndex: true/false }

    // Per-page detection index so each page render doesn't scan the whole
    // detection list. Rebuilt when allDetections is reassigned or grows.
    var _detIndex = null, _detIndexSrc = null, _detIndexLen = -1;
    function _detectionsForPage(pageIdx) {
        if (!allDetections) return [];
        if (_detIndexSrc !== allDetections || _detIndexLen !== allDetections.length) {
            _detIndex = {};
            for (var i = 0; i < allDetections.length; i++) {
                var d = allDetections[i];
                (_detIndex[d.page_index] || (_detIndex[d.page_index] = [])).push(d);
            }
            _detIndexSrc = allDetections;
            _detIndexLen = allDetections.length;
        }
        return _detIndex[pageIdx] || [];
    }
    var cachedImgW = 0, cachedImgH = 0; // persist image dimensions across reloads

    // Draw detection mode state
    var activeDrawPageDiv = null;
    var activeDrawPageNum = 0;
    var isDetDrawing = false;
    var detDrawStartX = 0, detDrawStartY = 0;
    var detDrawPreview = null;
    var detDrawPopup = null;
    var detDrawDragState = null;
    var detDrawDragStartX = 0, detDrawDragStartY = 0;
    var detDrawDragInitRect = null;
    var detDrawRect = { left: 0, top: 0, width: 0, height: 0 };

    var LABEL_IDS = {
        KEY_ICON: 0, DIVIDER: 1, PAGE_HEADER: 2, CASE_CAPTION: 3,
        FOOTNOTES: 4, HEADNOTE_BRACKET: 5, CASE_METADATA: 6, CASE_SEQUENCE: 7,
        PAGE_NUMBER: 8, STATE_ABBREVIATION: 9, IMAGE: 10, HEADNOTE: 11,
        BACKGROUND: 12, SYLLABUS: 13, EDITORIAL: 14, JUDGES: 15,
        TEXT_COLUMN: 16, DOCKET: 17, DATE: 18, COURT: 19, CITATION: 20,
    };

    // Global drag handlers for draw detection resize/move
    document.addEventListener('mousemove', function (e) {
        if (!detDrawDragState || !detDrawPreview || !detDrawDragInitRect) return;
        // detDrawRect is in wrapper-natural pixels; clientX deltas are in
        // visual pixels, so divide by the wrapper's current visual scale
        // (which may differ from getPdfZoom() between zoom click and re-render).
        var z = cssToVisualScale(detDrawPreview);
        var dx = (e.clientX - detDrawDragStartX) / z;
        var dy = (e.clientY - detDrawDragStartY) / z;
        var r = Object.assign({}, detDrawDragInitRect);
        if (detDrawDragState.type === 'move') {
            r.left += dx; r.top += dy;
        } else {
            var h = detDrawDragState.handle;
            if (h.indexOf('e') >= 0) { r.width = Math.max(20, r.width + dx); }
            if (h.indexOf('s') >= 0) { r.height = Math.max(20, r.height + dy); }
            if (h.indexOf('w') >= 0) { var nw = Math.max(20, r.width - dx); r.left += r.width - nw; r.width = nw; }
            if (h.indexOf('n') >= 0) { var nh = Math.max(20, r.height - dy); r.top += r.height - nh; r.height = nh; }
        }
        detDrawRect = r;
        _updatePreviewPos();
        _updatePopupPos();
    });
    document.addEventListener('mouseup', function () {
        if (detDrawDragState) { detDrawDragState = null; detDrawDragInitRect = null; }
    });

    pdfjsLib.GlobalWorkerOptions.workerSrc =
        'https://cdn.jsdelivr.net/npm/pdfjs-dist@3.11.174/build/pdf.worker.min.js';

    if (initialPdfUrl) loadPdf(initialPdfUrl);

    // Preload data for instant overlays (skip in view-only mode)
    if (!viewOnly && !opinionEditMode) {
        (function() {
            fetch('/scans/' + documentId + '/detections/')
                .then(function(r) { if (r.ok) return r.json(); return []; })
                .then(function(data) {
                    allDetections = data;
                    if (data && data.length > 0) {
                        cachedImgW = data[0].img_width || 0;
                        cachedImgH = data[0].img_height || 0;
                    }
                })
                .catch(function() {});
            // One list, in PDF points, rects and margin strips together
            // (#240): the rows the compute wrote and the curator edited.
            fetch('/scans/' + documentId + '/redactions/')
                .then(function(r) { if (r.ok) return r.json(); return []; })
                .then(function(data) { redactionRects = data; if (redactionsVisible) drawRedactionOverlays(); })
                .catch(function() {});
        })();
    }

    var _pdfLoadHandle = null;

    function showPdf(pdf) {
        pdfDoc = pdf;
        container.innerHTML = '';
        createPlaceholders(pdf.numPages);
        setupLazyLoading();
    }

    // Swap the viewer to the original scan (issue #185). Cancels the
    // preview load first: the user chose the original, so a bitonal that
    // finishes later must not render over it. The sentinel URL lets a
    // later tab click load a real URL again.
    function startOriginalLoad() {
        if (_pdfLoadHandle) { _pdfLoadHandle.cancel(); _pdfLoadHandle = null; }
        currentUrl = '__original__';
        renderPreviewBanner(null, startOriginalLoad);
        if (observer) { observer.disconnect(); observer = null; }
        renderedPages = {};
        pdfDoc = null;
        showViewerMessage(container, 'Loading the original PDF...');
        loadOriginalPdf(documentId, {
            onReady: function (pdf) {
                if (currentUrl !== '__original__') return;
                showPdf(pdf);
            },
            onFail: function (err, url) {
                if (currentUrl !== '__original__') return;
                showOriginalLoadFailure(container, url);
            },
        }, { final: finalSpace });
    }

    if (window.ocrTextInit) ocrTextInit();

    function loadPdf(url) {
        if (url === currentUrl && url.indexOf('?t=') === -1) return;
        currentUrl = url;
        // The text overlay reads the volume, so it must not draw on
        // the pages of one opinion's PDF (#262). One flag on the
        // container, because the module knows nothing of the tabs.
        container.dataset.ocrText = _viewingOpinion ? 'off' : 'on';

        if (_pdfLoadHandle) { _pdfLoadHandle.cancel(); _pdfLoadHandle = null; }
        if (observer) { observer.disconnect(); observer = null; }
        renderedPages = {};
        pdfDoc = null;
        // Hide the previous document's banner now, not only on success:
        // a failed or pending load (an opinion tab mid-regeneration)
        // must not keep the old bitonal banner visible over it.
        renderPreviewBanner(null, startOriginalLoad);
        showViewerMessage(container, 'Loading PDF...');

        _pdfLoadHandle = loadPreviewPdf(url, {
            // Abort if a newer loadPdf() switched URLs (e.g. opinion tabs),
            // so a slow/pending load never renders over the current one.
            isCurrent: function () { return url === currentUrl; },
            onReady: function (pdf, previewKind) {
                showPdf(pdf);
                renderPreviewBanner(previewKind, startOriginalLoad);
            },
            onNotReady: function (message, data) {
                if (data && data.original_available) {
                    showViewerWait(container, message, {
                        buttonLabel: 'Load the original PDF',
                        note: 'The original file is large. It can load slowly.',
                        onButton: startOriginalLoad,
                    });
                } else {
                    showViewerMessage(container, message);
                }
            },
            onError: function (err) {
                showViewerMessage(container, 'Error loading PDF: ' + err.message);
            },
        });
    }

    function createPlaceholders(numPages) {
        // Build entries from pageMap or fallback to simple page list.
        // When viewing an individual opinion PDF, ignore the full-scan pageMap
        // (it has more entries than the opinion PDF has pages).
        var entries = (!_viewingOpinion && pageMap.length > 0) ? pageMap : [];
        if (entries.length === 0) {
            for (var i = 0; i < numPages; i++) {
                entries.push({ type: 'pdf_page', pdf_index: i, logical_number: i + 1 });
            }
        }

        entries.forEach(function (entry) {
            if (entry.type === 'missing') {
                createMissingPlaceholder(entry);
            } else if (entry.type === 'inserted') {
                createInsertedPlaceholder(entry);
            } else {
                createPdfPlaceholder(entry);
            }
        });
    }

    function createPdfPlaceholder(entry) {
        var pageNum = entry.logical_number || (entry.pdf_index + 1);
        var pdfPage = entry.pdf_index + 1;
        var div = document.createElement('div');
        div.className = 'page-container lazy-page';
        div.id = 'pv-page-' + pageNum;
        div.dataset.pdfIndex = entry.pdf_index;
        div.dataset.pageNum = pageNum;

        // Flagged? (match by pdf_index: logical numbers can repeat, e.g.
        // unnumbered front matter borrowing the real pages' numbers)
        if (flaggedIndices.indexOf(entry.pdf_index) !== -1) {
            div.classList.add('flagged');
        }
        // Duplicate?
        if (entry.duplicate) {
            div.classList.add('duplicate-page');
        }

        // OCR label
        var ocr = ocrByPage[String(pdfPage)];
        var ocrLabel = '';
        if (ocr) {
            var editable = pageEditsLocked ? '' : ' editable-page';
            var lockedTitle = 'The page review of this volume is approved, ' +
                'so its page numbers are fixed.';
            if (ocr.detected) {
                var tag = ocr.type === 'range' ? 'Range ' : '#';
                // The corrected volume's labels carry who read the
                // number and no score (#269).
                var detail = ocr.score ? ocr.zone + ' ' + ocr.score.toFixed(2) : ocr.zone;
                ocrLabel = '<span class="ocr-tag' + editable + '" data-pdf-page="' + pdfPage + '" ' +
                    'title="' + (pageEditsLocked ? lockedTitle : 'Click to correct page number') + '">' + tag + ocr.detected +
                    ' <small>(' + detail + ')</small></span>';
            } else {
                ocrLabel = '<span class="ocr-tag miss' + editable + '" data-pdf-page="' + pdfPage + '" ' +
                    'title="' + (pageEditsLocked ? lockedTitle : 'Click to assign a page number') + '">[no page # found]</span>';
            }
        }

        div.innerHTML =
            '<div class="page-label">' +
            '  <span>PDF p.' + pdfPage + (ocrLabel ? ' &rarr; ' + ocrLabel : '') +
                 (entry.duplicate ? ' <span class="dupe-badge">DUPLICATE</span>' : '') + '</span>' +
            (viewOnly ? '' :
            '  <span class="page-tools">' +
            (opinionEditMode ? '' :
            '    <button class="detect-btn" title="Show/hide detections">Detections</button>' +
            '    <button class="draw-det-btn" title="Draw a detection box">Draw</button>') +
            '    <button class="redact-btn" data-fill="black" title="Draw a black redaction">Redact</button>' +
            '    <button class="whiteout-btn" data-fill="white" title="Draw a white redaction">Whiteout</button>' +
            // '    <button class="delete-page-btn" title="Delete this page">Delete</button>' +
            '  </span>') +
            '</div>' +
            '<div class="canvas-wrapper" style="width:' + defaultPageWidth + 'px;height:' + PLACEHOLDER_HEIGHT + 'px;background:#f0f0f0">' +
            '  <canvas class="pdf-canvas"></canvas>' +
            (viewOnly ? '' : '  <canvas class="redaction-overlay"></canvas>') +
            '</div>';

        if (!viewOnly) {
            // Editable page number
            var editBtn = div.querySelector('.editable-page');
            if (editBtn) {
                (function (btn, pp) {
                    btn.addEventListener('click', function () {
                        var current = ocr && ocr.detected ? ocr.detected : '';
                        var num = prompt(
                            'Page number for PDF page ' + pp +
                            ' (leave blank if this page has no number):',
                            current
                        );
                        if (num === null) return; // cancelled
                        var trimmed = num.trim();
                        if (trimmed && (!/^\d+$/.test(trimmed) || parseInt(trimmed, 10) < 1)) {
                            alert('Page number must be a positive whole number, or blank for none.');
                            return;
                        }
                        fetch('/scans/' + documentId + '/assign-page/', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
                            body: JSON.stringify({
                                pdf_page: pp,
                                page_number: trimmed === '' ? null : trimmed,
                            }),
                        })
                        .then(function (r) {
                            return r.json().then(function (d) { return { ok: r.ok, data: d }; });
                        })
                        .then(function (res) {
                            if (!res.ok || res.data.status !== 'ok') {
                                alert((res.data && res.data.error) || 'Could not update the page number.');
                                return;
                            }
                            ocr.detected = res.data.detected;
                            if (res.data.detected) {
                                btn.className = 'ocr-tag editable-page';
                                btn.innerHTML = '#' + res.data.detected + ' <small>(manual)</small>';
                            } else {
                                btn.className = 'ocr-tag miss editable-page';
                                btn.innerHTML = '[no page # found]';
                            }
                        });
                    });
                })(editBtn, pdfPage);
            }

            var detectBtn = div.querySelector('.detect-btn');
            var drawDetBtn = div.querySelector('.draw-det-btn');
            var redactBtn = div.querySelector('.redact-btn');
            var whiteoutBtn = div.querySelector('.whiteout-btn');
            (function (pageDiv, pIdx) {
                if (detectBtn) detectBtn.addEventListener('click', function () { toggleDetections(pageDiv, pIdx); });
                if (drawDetBtn) drawDetBtn.addEventListener('click', function () { activateDrawMode(pageDiv, pIdx); });
                redactBtn.addEventListener('click', function () { toggleRedactionMode(pageDiv, pIdx, 'black'); });
                whiteoutBtn.addEventListener('click', function () { toggleRedactionMode(pageDiv, pIdx, 'white'); });
            })(div, entry.pdf_index);
        }

        container.appendChild(div);
    }

    function createMissingPlaceholder(entry) {
        var div = document.createElement('div');
        div.className = 'page-container missing-page';
        div.id = 'pv-page-' + entry.logical_number;
        div.style.width = defaultPageWidth + 'px';
        div.innerHTML =
            '<div class="page-label">Page ' + entry.logical_number + ' &mdash; MISSING</div>' +
            '<div class="missing-placeholder">' +
            '  <p>This page was not found in the document.</p>' +
            '  <p>Upload a scan or image to fill this gap:</p>' +
            '  <form class="insert-form" enctype="multipart/form-data">' +
            '    <input type="hidden" name="page_number" value="' + entry.logical_number + '">' +
            '    <label class="upload-btn">' +
            '      Choose Image' +
            '      <input type="file" name="image" accept="image/*,.pdf" style="display:none">' +
            '    </label>' +
            '  </form>' +
            '</div>';
        var fileInput = div.querySelector('input[type="file"]');
        (function (pageDiv, logNum) {
            fileInput.addEventListener('change', function () {
                if (fileInput.files.length > 0) {
                    uploadPageInsert(logNum, fileInput.files[0], pageDiv);
                }
            });
        })(div, entry.logical_number);
        container.appendChild(div);
    }

    function createInsertedPlaceholder(entry) {
        var div = document.createElement('div');
        div.className = 'page-container inserted-page';
        div.id = 'pv-page-' + entry.logical_number;
        div.innerHTML =
            '<div class="page-label">Page ' + entry.logical_number + ' &mdash; INSERTED</div>' +
            '<div class="canvas-wrapper">' +
            '  <img src="' + entry.insert_url + '" class="inserted-image" style="max-width:100%">' +
            '</div>';
        container.appendChild(div);
    }

    function uploadPageInsert(pageNumber, file, pageDiv) {
        var formData = new FormData();
        formData.append('page_number', pageNumber);
        formData.append('image', file);
        var placeholder = pageDiv.querySelector('.missing-placeholder');
        if (placeholder) placeholder.innerHTML = '<p>Uploading...</p>';
        fetch('/scans/' + documentId + '/insert/', {
            method: 'POST',
            headers: { 'X-CSRFToken': csrfToken },
            body: formData,
        })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (data.status === 'ok') {
                pageDiv.className = 'page-container inserted-page';
                pageDiv.innerHTML =
                    '<div class="page-label">Page ' + pageNumber + ' &mdash; INSERTED</div>' +
                    '<div class="canvas-wrapper">' +
                    '  <img src="' + data.insert_url + '" class="inserted-image" style="max-width:100%">' +
                    '</div>';
            } else {
                if (placeholder) placeholder.innerHTML = '<p>Error: ' + (data.message || 'Upload failed') + '</p>';
            }
        })
        .catch(function (err) {
            if (placeholder) placeholder.innerHTML = '<p>Error: ' + err + '</p>';
        });
    }

    // Defined in shared.js: deletePage(csrfToken, docId, pdfPage, pageDiv, labelPrefix)

    function setupLazyLoading() {
        observer = new IntersectionObserver(function (entries) {
            entries.forEach(function (obsEntry) {
                if (!obsEntry.isIntersecting) return;
                var pageDiv = obsEntry.target;
                var pdfIndex = parseInt(pageDiv.dataset.pdfIndex);
                if (renderedPages[pdfIndex]) return;
                renderedPages[pdfIndex] = true;
                renderPage(pageDiv, pdfIndex);
            });
        }, {
            root: document.querySelector('.viewer-panel'),
            rootMargin: '800px 0px',
        });

        container.querySelectorAll('.lazy-page').forEach(function (el) {
            observer.observe(el);
        });
    }

    // --- Zoom-driven re-render ---
    // When zoom changes, rasterize visible/nearby pages at the new resolution
    // and discard pages outside the viewport zone so they re-render fresh
    // when scrolled back into view.

    function isPageNearViewport(pageDiv, margin) {
        margin = margin || 800;
        var viewer = document.querySelector('.viewer-panel');
        if (!viewer) return false;
        var vRect = viewer.getBoundingClientRect();
        var pRect = pageDiv.getBoundingClientRect();
        return pRect.bottom > vRect.top - margin && pRect.top < vRect.bottom + margin;
    }

    function discardPage(pageDiv, pdfIndex) {
        if (pageDiv._renderTask) {
            try { pageDiv._renderTask.cancel(); } catch (_e) {}
            pageDiv._renderTask = null;
        }
        renderedPages[pdfIndex] = false;
        delete pageDiv.dataset.scale;
        delete pageDiv.dataset.renderedZoom;
        var canvas = pageDiv.querySelector('.pdf-canvas');
        if (canvas) { canvas.width = 0; canvas.height = 0; }
        var overlay = pageDiv.querySelector('.redaction-overlay');
        if (overlay) { overlay.width = 0; overlay.height = 0; }
        var wrapper = pageDiv.querySelector('.canvas-wrapper');
        if (wrapper) {
            wrapper.style.transform = '';
            wrapper.style.transformOrigin = '';
            wrapper.style.width = defaultPageWidth + 'px';
            wrapper.style.height = PLACEHOLDER_HEIGHT + 'px';
            wrapper.style.background = '#f0f0f0';
            wrapper.querySelectorAll(
                '.detection-box, .redaction-overlay-box, .redaction-delete-btn, ' +
                '.image-overlay, .opinion-bounds-overlay, .margin-overlay-box, .opinion-dim-overlay'
            ).forEach(function (el) { el.remove(); });
        }
        pageDiv.style.width = '';
        pageDiv.style.height = '';
        // The overlay boxes hold the scale of the render they were
        // drawn for (#262).
        if (window.ocrTextClear) ocrTextClear(pageDiv);
    }

    function rerenderForCurrentZoom() {
        var currentZoom = getPdfZoom();
        container.querySelectorAll('.lazy-page').forEach(function (pageDiv) {
            var pdfIndex = parseInt(pageDiv.dataset.pdfIndex);
            if (!renderedPages[pdfIndex]) return;
            if (Math.abs(pageRenderedZoom(pageDiv) - currentZoom) < 0.001) return;
            if (isPageNearViewport(pageDiv)) {
                renderPage(pageDiv, pdfIndex);
            } else {
                discardPage(pageDiv, pdfIndex);
            }
        });
    }

    window.requestPdfRerender = (function () {
        var t = null;
        return function () {
            if (t) clearTimeout(t);
            t = setTimeout(function () { t = null; rerenderForCurrentZoom(); }, 150);
        };
    })();

    function renderPage(pageDiv, pdfIndex) {
        var zoom = getPdfZoom();
        var effScale = SCALE * zoom;
        pdfDoc.getPage(pdfIndex + 1).then(function (page) {
            pdfPages[pdfIndex] = page;  // cache for overlay coordinate conversion
            var viewport = page.getViewport({ scale: effScale });
            var canvas = pageDiv.querySelector('.pdf-canvas');
            canvas.width = viewport.width;
            canvas.height = viewport.height;

            var wrapper = pageDiv.querySelector('.canvas-wrapper');
            wrapper.style.width = viewport.width + 'px';
            wrapper.style.height = viewport.height + 'px';
            wrapper.style.background = '';
            pageDiv.style.width = viewport.width + 'px';
            pageDiv.dataset.scale = effScale;
            pageDiv.dataset.renderedZoom = zoom;

            // Track default page width at base scale so the comparison stays
            // valid across zoom changes.
            var widthAtBaseScale = viewport.width / zoom;
            if (defaultPageWidth === 918 && widthAtBaseScale !== 918) {
                defaultPageWidth = widthAtBaseScale;
                container.querySelectorAll('.lazy-page').forEach(function (el) {
                    if (!renderedPages[parseInt(el.dataset.pdfIndex)]) {
                        var w = el.querySelector('.canvas-wrapper');
                        if (w) {
                            w.style.width = defaultPageWidth + 'px';
                            el.style.width = defaultPageWidth + 'px';
                        }
                    }
                });
            }

            if (pageDiv._renderTask) {
                try { pageDiv._renderTask.cancel(); } catch (_e) {}
                pageDiv._renderTask = null;
            }
            var task = page.render({ canvasContext: canvas.getContext('2d'), viewport: viewport });
            pageDiv._renderTask = task;
            task.promise.then(function () {
                if (pageDiv._renderTask === task) pageDiv._renderTask = null;
            }, function () { /* swallow cancel */ });

            if (!viewOnly) {
                // Setup redaction overlay
                var overlay = pageDiv.querySelector('.redaction-overlay');
                if (overlay) {
                    overlay.width = viewport.width;
                    overlay.height = viewport.height;
                }

                // Redraw overlays (skip when viewing individual opinion PDFs)
                if (!_viewingOpinion) {
                    if (redactionsVisible && redactionRects) {
                        drawRedactionOverlaysForPage(pdfIndex);
                    }
                    if (overlayMode === 'bounds' && _boundsPageOwners) {
                        _drawBoundsForPage(pageDiv);
                    }
                }
            }
            if (!viewOnly && _globalDetections && allDetections) {
                detectionsVisible[pdfIndex] = true;
                drawDetectionOverlay(pageDiv, pdfIndex);
            }

            // Overlay original PDF crops for IMAGE detections (off, #278)
            if (ORIGINAL_CROPS_ENABLED && allDetections && !_viewingOpinion) {
                var pageIdx = parseInt(pageDiv.dataset.pdfIndex);
                var imgDets = _detectionsForPage(pageIdx).filter(function(d) {
                    return d.label === 'IMAGE';
                });
                if (imgDets.length > 0) {
                    var canvasW = viewport.width;
                    var canvasH = viewport.height;
                    var imgW = imgDets[0].img_width || 1;
                    var imgH = imgDets[0].img_height || 1;
                    var sx = canvasW / imgW;
                    var sy = canvasH / imgH;
                    var pdfPtW = viewport.width / effScale;
                    var pdfPtH = viewport.height / effScale;
                    var pxToPtX = pdfPtW / imgW;
                    var pxToPtY = pdfPtH / imgH;

                    imgDets.forEach(function(d) {
                        var ptX0 = d.bbox[0] * pxToPtX;
                        var ptY0 = d.bbox[1] * pxToPtY;
                        var ptX1 = d.bbox[2] * pxToPtX;
                        var ptY1 = d.bbox[3] * pxToPtY;
                        var displayW = (d.bbox[2] - d.bbox[0]) * sx;
                        var displayH = (d.bbox[3] - d.bbox[1]) * sy;
                        var cropPtW = ptX1 - ptX0;
                        var dpi = Math.round((displayW / cropPtW) * 72);
                        dpi = Math.min(Math.max(dpi, 72), 300);

                        var img = document.createElement('img');
                        img.className = 'image-overlay';
                        img.style.position = 'absolute';
                        img.style.left = (d.bbox[0] * sx) + 'px';
                        img.style.top = (d.bbox[1] * sy) + 'px';
                        img.style.width = displayW + 'px';
                        img.style.height = displayH + 'px';
                        img.style.zIndex = '3';
                        img.style.pointerEvents = 'none';
                        img.src = '/scans/' + documentId + '/original-crop/' +
                            '?page=' + pageIdx +
                            '&x0=' + ptX0.toFixed(2) +
                            '&y0=' + ptY0.toFixed(2) +
                            '&x1=' + ptX1.toFixed(2) +
                            '&y1=' + ptY1.toFixed(2) +
                            '&dpi=' + dpi + spaceQuery;
                        wrapper.appendChild(img);
                    });
                }
            }
            applyZoomToPage(pageDiv);
            // The text overlay (#262). Here and not in the observer:
            // a page is rendered only near the viewport, so this is
            // the viewport rule, and the zoom re-render follows it.
            if (window.ocrTextPaint) ocrTextPaint(pageDiv, pdfIndex);
        });
    }

    // --- Redaction drawing ---

    function toggleRedactionMode(pageDiv, pdfIndex, fill) {
        var overlay = pageDiv.querySelector('.redaction-overlay');
        var blackBtn = pageDiv.querySelector('.redact-btn');
        var whiteBtn = pageDiv.querySelector('.whiteout-btn');

        if (activeRedactionDiv === pageDiv && activeRedactionFill === fill) {
            overlay.style.cursor = 'default';
            overlay.style.pointerEvents = 'none';
            blackBtn.classList.remove('active');
            whiteBtn.classList.remove('active');
            overlay.onmousedown = null; overlay.onmousemove = null; overlay.onmouseup = null;
            activeRedactionDiv = null;
            return;
        }

        if (activeRedactionDiv) {
            var prev = activeRedactionDiv;
            prev.querySelector('.redaction-overlay').style.cursor = 'default';
            prev.querySelector('.redaction-overlay').style.pointerEvents = 'none';
            prev.querySelector('.redact-btn').classList.remove('active');
            prev.querySelector('.whiteout-btn').classList.remove('active');
            prev.querySelector('.redaction-overlay').onmousedown = null;
            prev.querySelector('.redaction-overlay').onmousemove = null;
            prev.querySelector('.redaction-overlay').onmouseup = null;
        }

        overlay.style.cursor = 'crosshair';
        overlay.style.pointerEvents = 'auto';
        overlay.oncontextmenu = function (e) { e.preventDefault(); };
        activeRedactionDiv = pageDiv;
        activeRedactionFill = fill;
        if (fill === 'white') { whiteBtn.classList.add('active'); }
        else { blackBtn.classList.add('active'); }

        overlay.onmousedown = function (e) {
            isDrawing = true;
            var pt = eventToCanvasPixels(e, overlay);
            startX = pt.x;
            startY = pt.y;
        };
        overlay.onmousemove = function (e) {
            if (!isDrawing) return;
            var pt = eventToCanvasPixels(e, overlay);
            var curX = pt.x;
            var curY = pt.y;
            var ctx = overlay.getContext('2d');
            ctx.clearRect(0, 0, overlay.width, overlay.height);
            ctx.fillStyle = activeRedactionFill === 'white' ? 'rgba(255,255,255,0.5)' : 'rgba(255,0,0,0.3)';
            ctx.strokeStyle = activeRedactionFill === 'white' ? '#3b82f6' : 'red';
            ctx.lineWidth = 2;
            var x = Math.min(startX, curX), y = Math.min(startY, curY);
            var w = Math.abs(curX - startX), h = Math.abs(curY - startY);
            ctx.fillRect(x, y, w, h);
            ctx.strokeRect(x, y, w, h);
        };
        overlay.onmouseup = function (e) {
            if (!isDrawing) return;
            isDrawing = false;
            var pt = eventToCanvasPixels(e, overlay);
            var endX = pt.x;
            var endY = pt.y;
            var scale = pageScale(pageDiv, SCALE);
            var pdfX = Math.min(startX, endX) / scale;
            var pdfY = Math.min(startY, endY) / scale;
            var pdfW = Math.abs(endX - startX) / scale;
            var pdfH = Math.abs(endY - startY) / scale;

            if (pdfW < 5 || pdfH < 5) {
                var ctx = overlay.getContext('2d');
                ctx.clearRect(0, 0, overlay.width, overlay.height);
                return;
            }

            if (opinionEditMode && _viewingOpinion) {
                // Step 4: apply rect directly to opinion PDF file
                var opinionPk = window._currentOpinionPk;
                if (!opinionPk) { alert('No opinion selected'); return; }
                fetch('/scans/' + documentId + '/opinion-edit/' + opinionPk + '/apply-rect/', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
                    body: JSON.stringify({
                        page_index: parseInt(pageDiv.dataset.pdfIndex),
                        x0: pdfX, y0: pdfY,
                        x1: pdfX + pdfW, y1: pdfY + pdfH,
                        fill: activeRedactionFill,
                    }),
                })
                .then(function (r) { return r.json(); })
                .then(function (data) {
                    if (data.status === 'ok') {
                        // Force reload with cache bust
                        var savedUrl = currentUrl.split('?')[0];
                        currentUrl = '';
                        loadPdf(savedUrl + '?t=' + Date.now());
                    }
                });
            } else {
                // Step 2: one human row, in the PDF points the drag already
                // gave us (#240). No render size is needed any more.
                var ctxDone = overlay.getContext('2d');
                ctxDone.clearRect(0, 0, overlay.width, overlay.height);
                fetch('/scans/' + documentId + '/redactions/add/', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
                    body: JSON.stringify({
                        page_index: pdfIndex,
                        x0: pdfX, y0: pdfY, x1: pdfX + pdfW, y1: pdfY + pdfH,
                        fill: activeRedactionFill,
                    }),
                })
                .then(function (r) { return r.json(); })
                .then(function (data) {
                    if (!data || data.status !== 'ok') {
                        showToast((data && data.message) || 'Failed to save the box');
                        return;
                    }
                    showToast('Saved. The redactions are not recomputed from this yet.', 'success');
                    refreshOverlays();
                    if (window.refreshFindings) window.refreshFindings();
                });
            }
        };
    }

    // --- Detection overlay ---

    var LABEL_COLORS = {
        KEY_ICON: '#ef4444',
        CASE_CAPTION: '#22c55e',
        HEADNOTE: '#a855f7',
        HEADNOTE_BRACKET: '#a855f7',
        PAGE_NUMBER: '#3b82f6',
        PAGE_HEADER: '#f59e0b',
        DIVIDER: '#6b7280',
        FOOTNOTES: '#06b6d4',
        CASE_METADATA: '#ec4899',
        BACKGROUND: '#9ca3af',
        IMAGE: '#f97316',
        SYLLABUS: '#eab308',
        STATE_ABBREVIATION: '#14b8a6',
        CASE_SEQUENCE: '#d946ef',
        EDGES: '#78716c',
        EDITORIAL: '#334155',
        JUDGES: '#64748b',
        TEXT_COLUMN: '#93c5fd',
        DOCKET: '#4ade80',
        DATE: '#fbbf24',
        COURT: '#2dd4bf',
        CITATION: '#f87171',
    };

    function loadDetections(callback) {
        if (allDetections !== null) { callback(); return; }
        fetch('/scans/' + documentId + '/detections/')
            .then(function (r) { return r.json(); })
            .then(function (data) {
                allDetections = data;
                callback();
            });
    }

    var _globalDetections = false;

    function toggleDetections(pageDiv, pdfIndex) {
        _globalDetections = !_globalDetections;
        _showDetectionHelp(_globalDetections);

        if (_globalDetections) {
            loadDetections(function () {
                // Show on all rendered pages
                document.querySelectorAll('.lazy-page').forEach(function(pd) {
                    var pIdx = parseInt(pd.dataset.pdfIndex);
                    var canvas = pd.querySelector('.pdf-canvas');
                    if (!canvas || canvas.width < 10) return;
                    detectionsVisible[pIdx] = true;
                    var btn = pd.querySelector('.detect-btn');
                    if (btn) btn.classList.add('active');
                    drawDetectionOverlay(pd, pIdx);
                });
            });
        } else {
            document.querySelectorAll('.lazy-page').forEach(function(pd) {
                var pIdx = parseInt(pd.dataset.pdfIndex);
                detectionsVisible[pIdx] = false;
                var btn = pd.querySelector('.detect-btn');
                if (btn) btn.classList.remove('active');
                clearDetectionOverlay(pd, pIdx);
            });
        }
    }

    function drawDetectionOverlay(pageDiv, pdfIndex) {
        var wrapper = pageDiv.querySelector('.canvas-wrapper');
        // Remove old detection divs
        wrapper.querySelectorAll('.detection-box').forEach(function (el) { el.remove(); });

        // Only show labels that affect the pipeline (pairing, redaction, headnotes, layout)
        var USED_LABELS = {
            CASE_CAPTION: true, KEY_ICON: true,
            STATE_ABBREVIATION: true, PAGE_HEADER: true, PAGE_NUMBER: true,
            DIVIDER: true, HEADNOTE_BRACKET: true, EDITORIAL: true,
            CASE_SEQUENCE: true, HEADNOTE: true, CASE_METADATA: true,
            FOOTNOTES: true, IMAGE: true, TEXT_COLUMN: true,
            BACKGROUND: true, SYLLABUS: true, JUDGES: true,
        };
        // A column box is the full height of its column, so it would cover the
        // detections inside it and take their clicks. Draw those first, and the
        // .layout class puts them a layer down, so what sits inside them stays
        // on top and stays selectable.
        var pageDets = _detectionsForPage(pdfIndex)
            .filter(function (d) {
                return d.manual || USED_LABELS[d.label];
            })
            .sort(function (a, b) {
                return (a.label === 'TEXT_COLUMN' ? 0 : 1) - (b.label === 'TEXT_COLUMN' ? 0 : 1);
            });
        if (!pageDets.length) return;

        // Scale: detections are in pixel coords (img_width x img_height)
        // The PDF canvas is rendered at SCALE (1.5x of PDF points)
        // Detection pixels → PDF display: need (pixel / img_width) * canvas_width
        var canvas = pageDiv.querySelector('.pdf-canvas');
        var canvasW = canvas.width;
        var canvasH = canvas.height;
        var imgW = pageDets[0].img_width || 1;
        var imgH = pageDets[0].img_height || 1;
        var sx = canvasW / imgW;
        var sy = canvasH / imgH;

        var detSeq = {};
        pageDets.forEach(function (d) {
            var box = document.createElement('div');
            box.className = 'detection-box';
            var color = LABEL_COLORS[d.label] || '#999';
            box.style.left = (d.bbox[0] * sx) + 'px';
            box.style.top = (d.bbox[1] * sy) + 'px';
            box.style.width = ((d.bbox[2] - d.bbox[0]) * sx) + 'px';
            box.style.height = ((d.bbox[3] - d.bbox[1]) * sy) + 'px';
            box.dataset.sx = sx;
            box.dataset.sy = sy;
            box.dataset.imgWidth = imgW;
            box.dataset.imgHeight = imgH;
            box.style.borderColor = color;
            detSeq[d.label] = (detSeq[d.label] || 0) + 1;
            var detTag = _tagOverlayBox(
                box, 'detection', d.label, pdfIndex, detSeq[d.label],
                {x0: d.bbox[0], y0: d.bbox[1], x1: d.bbox[2], y1: d.bbox[3]}
            );
            box.title = d.label + ' (' + d.confidence + ')\n' + detTag;

            if (d.manual) box.classList.add('manual');
            if (d.label === 'TEXT_COLUMN') box.classList.add('layout');

            var label = document.createElement('span');
            label.className = 'detection-label';
            label.style.background = color;
            label.style.display = 'none';
            label.textContent = d.label + (d.manual ? ' (manual)' : ' ' + d.confidence);
            box.appendChild(label);

            // Double-click to select (shows the handles and Dismiss)
            (function(det, detBox) {
                detBox.addEventListener('dblclick', function(e) {
                    e.stopPropagation();
                    _selectDetectionBox(detBox, det);
                });
                // The anchor pick mode of the sidebar (#240 PR C): a
                // single click on a caption or a key icon box sets the
                // anchor. The mode yields first, or the click would
                // fall through to the page.
                detBox.addEventListener('click', function(e) {
                    if (window.boundaryPickTarget && window.boundaryPickTarget(det)) {
                        e.stopPropagation();
                        e.preventDefault();
                    }
                });
            })(d, box);

            wrapper.appendChild(box);
        });
    }

    function clearDetectionOverlay(pageDiv, pdfIndex) {
        var wrapper = pageDiv.querySelector('.canvas-wrapper');
        wrapper.querySelectorAll('.detection-box').forEach(function (el) { el.remove(); });
    }

    // --- Draw Detection Mode ---

    function _updatePreviewPos() {
        if (!detDrawPreview) return;
        detDrawPreview.style.left = detDrawRect.left + 'px';
        detDrawPreview.style.top = detDrawRect.top + 'px';
        detDrawPreview.style.width = detDrawRect.width + 'px';
        detDrawPreview.style.height = detDrawRect.height + 'px';
    }

    function _updatePopupPos() {
        if (!detDrawPopup || !detDrawPreview) return;
        var wrapper = detDrawPreview.parentElement;
        var popupH = detDrawPopup.offsetHeight || 110;
        var popupW = detDrawPopup.offsetWidth || 190;
        var top = detDrawRect.top + detDrawRect.height + 8;
        if (top + popupH > wrapper.clientHeight - 4) top = detDrawRect.top - popupH - 8;
        top = Math.max(4, top);
        var left = Math.min(detDrawRect.left, wrapper.clientWidth - popupW - 4);
        left = Math.max(4, left);
        detDrawPopup.style.left = left + 'px';
        detDrawPopup.style.top = top + 'px';
    }

    function _cancelDetDraw() {
        if (detDrawPreview) { detDrawPreview.remove(); detDrawPreview = null; }
        if (detDrawPopup) { detDrawPopup.remove(); detDrawPopup = null; }
        detDrawDragState = null;
    }

    function activateDrawMode(pageDiv, pdfIndex) {
        if (activeDrawPageDiv === pageDiv) {
            _deactivateDrawMode();
            return;
        }
        if (activeDrawPageDiv) _deactivateDrawMode();

        // Deactivate redaction mode if active
        if (activeRedactionDiv) {
            var prevOv = activeRedactionDiv.querySelector('.redaction-overlay');
            prevOv.style.cursor = 'default';
            prevOv.style.pointerEvents = 'none';
            activeRedactionDiv.querySelector('.redact-btn').classList.remove('active');
            activeRedactionDiv.querySelector('.whiteout-btn').classList.remove('active');
            prevOv.onmousedown = null; prevOv.onmousemove = null; prevOv.onmouseup = null;
            activeRedactionDiv = null;
        }

        activeDrawPageDiv = pageDiv;
        activeDrawPageNum = pdfIndex;
        pageDiv.querySelector('.draw-det-btn').classList.add('active');

        // Preload detections so img dimensions are available for coordinate conversion
        if (allDetections === null) loadDetections(function () {});

        var overlay = pageDiv.querySelector('.redaction-overlay');
        overlay.style.cursor = 'crosshair';
        overlay.style.pointerEvents = 'auto';

        overlay.onmousedown = function (e) {
            if (detDrawPreview) return; // wait for user to confirm/cancel existing
            isDetDrawing = true;
            var pt = eventToCanvasPixels(e, overlay);
            detDrawStartX = pt.x;
            detDrawStartY = pt.y;
        };
        overlay.onmousemove = function (e) {
            if (!isDetDrawing) return;
            var pt = eventToCanvasPixels(e, overlay);
            var curX = pt.x, curY = pt.y;
            var ctx = overlay.getContext('2d');
            ctx.clearRect(0, 0, overlay.width, overlay.height);
            var x = Math.min(detDrawStartX, curX), y = Math.min(detDrawStartY, curY);
            ctx.strokeStyle = '#22c55e'; ctx.lineWidth = 2; ctx.setLineDash([6, 3]);
            ctx.strokeRect(x, y, Math.abs(curX - detDrawStartX), Math.abs(curY - detDrawStartY));
            ctx.setLineDash([]);
        };
        overlay.onmouseup = function (e) {
            if (!isDetDrawing) return;
            isDetDrawing = false;
            var pt = eventToCanvasPixels(e, overlay);
            var endX = pt.x, endY = pt.y;
            var x = Math.min(detDrawStartX, endX), y = Math.min(detDrawStartY, endY);
            var w = Math.abs(endX - detDrawStartX), h = Math.abs(endY - detDrawStartY);
            var ctx = overlay.getContext('2d');
            ctx.clearRect(0, 0, overlay.width, overlay.height);
            if (w < 10 || h < 10) return;
            _showDetDrawPreview(pageDiv, pdfIndex, x, y, w, h);
        };
    }

    function _deactivateDrawMode() {
        if (!activeDrawPageDiv) return;
        var overlay = activeDrawPageDiv.querySelector('.redaction-overlay');
        overlay.style.cursor = 'default';
        overlay.style.pointerEvents = 'none';
        overlay.onmousedown = null; overlay.onmousemove = null; overlay.onmouseup = null;
        activeDrawPageDiv.querySelector('.draw-det-btn').classList.remove('active');
        activeDrawPageDiv = null;
        _cancelDetDraw();
    }

    function _showDetDrawPreview(pageDiv, pdfIndex, x, y, w, h) {
        _cancelDetDraw();
        var wrapper = pageDiv.querySelector('.canvas-wrapper');
        detDrawRect = { left: x, top: y, width: w, height: h };

        var preview = document.createElement('div');
        preview.className = 'det-draw-preview';
        _updatePreviewPos();
        detDrawPreview = preview;

        // 8 resize handles
        ['nw', 'n', 'ne', 'w', 'e', 'sw', 's', 'se'].forEach(function (hName) {
            var hEl = document.createElement('div');
            hEl.className = 'det-draw-handle ' + hName;
            hEl.addEventListener('mousedown', function (e) {
                e.preventDefault(); e.stopPropagation();
                detDrawDragState = { type: 'resize', handle: hName };
                detDrawDragStartX = e.clientX; detDrawDragStartY = e.clientY;
                detDrawDragInitRect = Object.assign({}, detDrawRect);
            });
            preview.appendChild(hEl);
        });

        // Move: drag on preview body (not handles)
        preview.addEventListener('mousedown', function (e) {
            if (e.target !== preview) return;
            e.preventDefault();
            detDrawDragState = { type: 'move' };
            detDrawDragStartX = e.clientX; detDrawDragStartY = e.clientY;
            detDrawDragInitRect = Object.assign({}, detDrawRect);
        });

        wrapper.appendChild(preview);
        _showDetDrawPopup(pageDiv, pdfIndex);
    }

    function _showDetDrawPopup(pageDiv, pdfIndex) {
        var wrapper = pageDiv.querySelector('.canvas-wrapper');
        var popup = document.createElement('div');
        popup.className = 'det-draw-popup';
        detDrawPopup = popup;

        var labelOpts = [
            'CASE_CAPTION', 'KEY_ICON', 'HEADNOTE', 'HEADNOTE_BRACKET', 'TEXT_COLUMN',
            'PAGE_NUMBER', 'PAGE_HEADER', 'STATE_ABBREVIATION', 'CASE_SEQUENCE',
            'DIVIDER', 'BACKGROUND', 'SYLLABUS', 'JUDGES', 'EDITORIAL',
            'FOOTNOTES', 'CASE_METADATA', 'IMAGE',
        ];
        popup.innerHTML =
            '<div class="det-draw-popup-title">Add Detection</div>' +
            '<select class="det-draw-label-select">' +
            labelOpts.map(function (l) {
                return '<option value="' + l + '"' + (l === 'CASE_CAPTION' ? ' selected' : '') + '>' + l + '</option>';
            }).join('') +
            '</select>' +
            '<div class="det-draw-popup-btns">' +
            '  <button class="det-draw-confirm">Add</button>' +
            '  <button class="det-draw-cancel-btn">Cancel</button>' +
            '</div>';

        wrapper.appendChild(popup);
        _updatePopupPos();

        popup.querySelector('.det-draw-cancel-btn').addEventListener('click', _cancelDetDraw);
        popup.querySelector('.det-draw-confirm').addEventListener('click', function () {
            var labelName = popup.querySelector('.det-draw-label-select').value;
            _confirmDetDraw(pageDiv, pdfIndex, labelName);
        });
    }

    function _confirmDetDraw(pageDiv, pdfIndex, labelName) {
        var canvas = pageDiv.querySelector('.pdf-canvas');
        var canvasW = canvas.width, canvasH = canvas.height;

        // Get img dimensions from existing detections, or use cached/defaults
        var pageIdx = pdfIndex;
        var imgW = cachedImgW || 1700, imgH = cachedImgH || 2200;
        if (allDetections) {
            var pd = allDetections.find(function (d) { return d.page_index === pageIdx; });
            if (pd) { imgW = pd.img_width || imgW; imgH = pd.img_height || imgH; }
        }

        var sx = imgW / canvasW, sy = imgH / canvasH;
        var bx1 = Math.round(detDrawRect.left * sx);
        var by1 = Math.round(detDrawRect.top * sy);
        var bx2 = Math.round((detDrawRect.left + detDrawRect.width) * sx);
        var by2 = Math.round((detDrawRect.top + detDrawRect.height) * sy);
        var labelId = LABEL_IDS[labelName] !== undefined ? LABEL_IDS[labelName] : -1;

        var detData = {
            page_index: pageIdx,
            page_number: _pageNumForIndex(pdfIndex),
            label: labelName,
            label_id: labelId,
            confidence: 1.0,
            bbox: [bx1, by1, bx2, by2],
            img_width: imgW,
            img_height: imgH,
        };

        // The rows are the only store (#240): the server answers with
        // the id of the row that holds the box, new or approved, so
        // the next edit of this box can address it.
        fetch('/scans/' + documentId + '/add-single-detection/', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
            body: JSON.stringify(detData),
        }).then(function (r) { return r.json(); })
        .then(function (data) {
            // A refusal (409, 400) must not draw a box no row backs (#240).
            if (!data || data.status === 'error' || data.error) {
                _cancelDetDraw();
                showToast((data && (data.message || data.error)) || 'Failed to add detection');
                return;
            }
            if (!allDetections) allDetections = [];
            if (data.added === false) {
                // The server approved a box that is in the list already:
                // change that entry, and draw no second box over it.
                for (var ai = 0; ai < allDetections.length; ai++) {
                    if (allDetections[ai].id === data.detection_id) {
                        allDetections[ai].confidence = 1.0;
                        break;
                    }
                }
            } else {
                if (data.detection_id !== undefined) detData.id = data.detection_id;
                detData.manual = true;
                allDetections.push(detData);
            }
            _cancelDetDraw();
            detectionsVisible[pdfIndex] = true;
            pageDiv.querySelector('.detect-btn').classList.add('active');
            drawDetectionOverlay(pageDiv, pdfIndex);

            // A caption or a key icon changes the opinion pairing, and
            // with it the redaction rects and the margin strips. That
            // whole computation now runs on the daemon and takes the
            // volume out of review while it does (#196), so it is not
            // started here: an auto re-pair on every added box would
            // interrupt the reviewer in the middle of their edits. And
            // re-pairing on request is off for now, so say what the
            // edit did and did not change.
            var _pLabels = ['CASE_CAPTION', 'KEY_ICON'];
            if (_pLabels.indexOf(labelName) >= 0) {
                showToast('Saved. The redactions are not recomputed from this yet.', 'success');
            }
        });
    }

    // --- Public API ---

    window.loadFullRedacted = function () {
        document.querySelectorAll('.opinion-card').forEach(function (c) { c.classList.remove('selected'); });
        document.querySelectorAll('.toggle-redacted').forEach(function (b) { b.classList.add('active'); });
        document.querySelectorAll('.toggle-unredacted').forEach(function (b) { b.classList.remove('active'); });
        _viewingOpinion = false;
        loadPdf(initialPdfUrl);
    };

    window.loadOpinionUrl = function (url) {
        _viewingOpinion = true;
        clearOverlaysByClass('redaction-overlay-box');
        clearOverlaysByClass('margin-overlay-box');
        loadPdf(url);
    };

    window.loadOpinion = function (filename, card) {
        document.querySelectorAll('.opinion-card').forEach(function (c) { c.classList.remove('selected'); });
        if (card) card.classList.add('selected');
        var toggleBtn = card ? card.querySelector('.toggle-redacted') : null;
        if (toggleBtn) toggleBtn.classList.add('active');
        var unToggle = card ? card.querySelector('.toggle-unredacted') : null;
        if (unToggle) unToggle.classList.remove('active');

        _viewingOpinion = true;
        clearOverlaysByClass('redaction-overlay-box');
        clearOverlaysByClass('margin-overlay-box');
        var url = '/scans/' + documentId + '/opinion/' + filename + '/';
        loadPdf(url);
    };

    window.toggleOpinionView = function (btn, mode) {
        var card = btn.closest('.opinion-card');
        var filename = card.dataset.filename;
        card.querySelector('.toggle-redacted').classList.toggle('active', mode === 'redacted');
        card.querySelector('.toggle-unredacted').classList.toggle('active', mode === 'unredacted');

        // Select this card
        document.querySelectorAll('.opinion-card').forEach(function (c) { c.classList.remove('selected'); });
        card.classList.add('selected');

        var url;
        if (mode === 'unredacted') {
            url = '/scans/' + documentId + '/unredacted/' + filename + '/';
        } else {
            url = '/scans/' + documentId + '/opinion/' + filename + '/';
        }
        loadPdf(url);
    };

    // Refresh all overlays from DB
    window.refreshOverlays = refreshOverlays;
    function refreshOverlays() {
        redactionRects = null;
        // The detections carry the render size the detection overlay
        // scales by; the redaction rows are in points and need none.
        fetch('/scans/' + documentId + '/detections/')
            .then(function(r) { if (r.ok) return r.json(); return []; })
            .then(function(data) {
                allDetections = data;
                if (data && data.length > 0) {
                    cachedImgW = data[0].img_width || cachedImgW;
                    cachedImgH = data[0].img_height || cachedImgH;
                }
            }).catch(function() {});
        loadRedactionRows(function() { if (redactionsVisible) drawRedactionOverlays(); });
    }

    // One GET for the rects and the margin strips (#240): the rows the
    // compute wrote and the curator edited, in PDF points.
    function loadRedactionRows(done) {
        fetch('/scans/' + documentId + '/redactions/')
            .then(function(r) { if (r.ok) return r.json(); return []; })
            .then(function(data) { redactionRects = data; if (done) done(); })
            .catch(function() { redactionRects = []; if (done) done(); });
    }

    // ── Unified overlay toggle ──
    // overlayMode cycles: 'off' → 'bounds' → 'transparent' → 'solid' → 'off'
    var overlayMode = 'off';
    var _boundsColors = [
        '#3b82f6', '#f97316', '#10b981', '#a855f7',
        '#ec4899', '#eab308', '#06b6d4', '#ef4444',
    ];
    function _hexToRgba(hex, alpha) {
        var r = parseInt(hex.slice(1,3), 16);
        var g = parseInt(hex.slice(3,5), 16);
        var b = parseInt(hex.slice(5,7), 16);
        return 'rgba(' + r + ',' + g + ',' + b + ',' + alpha + ')';
    }

    // Cached page ownership map built from _opinionsData, keyed by pdf index
    var _boundsPageOwners = null;
    // Cached outside_rects grouped by pdf index: {pdfIndex: [{opIdx, x0, y0, x1, y1}, ...]}
    var _boundsOutsideByPage = null;

    function _buildBoundsCache() {
        _boundsPageOwners = {};
        _boundsOutsideByPage = {};
        _opinionsData.forEach(function(op, idx) {
            // A dismissed boundary keeps its card (with its undo) and
            // draws nothing (#240 PR C).
            if (op.dismissed) return;
            var startIdx = op.caption_page;
            var endIdx = (op.key_page !== undefined) ? op.key_page : op.caption_page;
            for (var p = startIdx; p <= endIdx; p++) {
                if (!_boundsPageOwners[p]) _boundsPageOwners[p] = [];
                _boundsPageOwners[p].push({idx: idx, isFirst: p === startIdx, isLast: p === endIdx});
            }
            (op.outside_rects || []).forEach(function(r) {
                if (!_boundsOutsideByPage[r.page_index]) _boundsOutsideByPage[r.page_index] = [];
                _boundsOutsideByPage[r.page_index].push({opIdx: idx, x0: r.x0, y0: r.y0, x1: r.x1, y1: r.y1});
            });
        });
    }

    function _drawBoundsForPage(pageDiv) {
        if (!_boundsPageOwners) return;
        var num = parseInt(pageDiv.dataset.pdfIndex);
        var wrapper = pageDiv.querySelector('.canvas-wrapper');
        if (!wrapper) return;

        // Remove existing bounds on this page
        wrapper.querySelectorAll('.opinion-bounds-overlay').forEach(function(el) { el.remove(); });

        var owners = _boundsPageOwners[num];
        if (!owners || owners.length === 0) {
            var gap = document.createElement('div');
            gap.className = 'opinion-bounds-overlay';
            gap.style.cssText = 'position:absolute;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.08);z-index:4;pointer-events:none;';
            wrapper.appendChild(gap);
            return;
        }

        owners.forEach(function(own) {
            var color = _boundsColors[own.idx % _boundsColors.length];

            var strip = document.createElement('div');
            strip.className = 'opinion-bounds-overlay';
            strip.style.cssText = 'position:absolute;top:0;left:0;width:5px;height:100%;background:' + color + ';z-index:5;pointer-events:none;';
            wrapper.appendChild(strip);

            if (own.isFirst) {
                var topBar = document.createElement('div');
                topBar.className = 'opinion-bounds-overlay';
                topBar.style.cssText = 'position:absolute;top:0;left:0;width:100%;height:2px;background:' + color + ';z-index:5;pointer-events:none;';
                wrapper.appendChild(topBar);

                var lbl = document.createElement('span');
                lbl.className = 'opinion-bounds-overlay';
                lbl.style.cssText = 'position:absolute;top:4px;left:10px;font-size:11px;font-weight:700;padding:1px 6px;border-radius:3px;color:white;z-index:6;pointer-events:none;background:' + color + ';';
                lbl.textContent = '#' + (own.idx + 1);
                wrapper.appendChild(lbl);
            }

            if (own.isLast) {
                var botBar = document.createElement('div');
                botBar.className = 'opinion-bounds-overlay';
                botBar.style.cssText = 'position:absolute;bottom:0;left:0;width:100%;height:2px;background:' + color + ';z-index:5;pointer-events:none;';
                wrapper.appendChild(botBar);
            }
        });

        // Draw outside_rects for this page (needs canvas for coordinate scaling)
        var outsideRects = _boundsOutsideByPage[num];
        if (outsideRects && outsideRects.length) {
            var canvas = pageDiv.querySelector('.pdf-canvas');
            if (canvas && canvas.width > 10) {
                var dsx = canvas.offsetWidth / (canvas.width / pageScale(pageDiv, SCALE));
                var dsy = canvas.offsetHeight / (canvas.height / pageScale(pageDiv, SCALE));

                outsideRects.forEach(function(r) {
                    var color = _boundsColors[r.opIdx % _boundsColors.length];
                    var div = document.createElement('div');
                    div.className = 'opinion-bounds-overlay';
                    div.style.position = 'absolute';
                    div.style.left = (r.x0 * dsx) + 'px';
                    div.style.top = (r.y0 * dsy) + 'px';
                    div.style.width = ((r.x1 - r.x0) * dsx) + 'px';
                    div.style.height = ((r.y1 - r.y0) * dsy) + 'px';
                    div.style.background = _hexToRgba(color, 0.08);
                    div.style.border = '1px dashed ' + _hexToRgba(color, 0.5);
                    div.style.zIndex = '5';
                    div.style.pointerEvents = 'none';
                    div.style.boxSizing = 'border-box';
                    wrapper.appendChild(div);
                });
            }
        }
    }

    function drawOpinionBounds() {
        clearOverlaysByClass('opinion-bounds-overlay');
        _loadOpinionsData(function() {
            if (!_opinionsData || !_opinionsData.length) return;
            _buildBoundsCache();
            document.querySelectorAll('.lazy-page').forEach(_drawBoundsForPage);
        });
    }

    window.toggleOverlays = function() {
        if (overlayMode === 'off') {
            overlayMode = 'bounds';
            clearOverlaysByClass('redaction-overlay-box');
            clearOverlaysByClass('margin-overlay-box');
            redactionsVisible = false;
            marginsVisible = false;
            drawOpinionBounds();
        } else if (overlayMode === 'bounds') {
            overlayMode = 'transparent';
            clearOverlaysByClass('opinion-bounds-overlay');
            // Load the rows if needed; the margin strips are in the same list.
            redactionsVisible = true;
            marginsVisible = true;
            if (!redactionRects) {
                loadRedactionRows(drawRedactionOverlays);
            } else {
                drawRedactionOverlays();
            }
        } else if (overlayMode === 'transparent') {
            overlayMode = 'solid';
            document.querySelectorAll('.redaction-overlay-box').forEach(function(div) {
                var fill = div.dataset.fill || 'black';
                var lbl = div.querySelector('span');
                if (lbl) lbl.style.display = 'none';
                if (fill === 'black') {
                    div.style.background = 'rgba(0,0,0,1)';
                    div.style.border = 'none';
                } else {
                    div.style.background = 'rgba(255,255,255,1)';
                    div.style.border = 'none';
                }
            });
            document.querySelectorAll('.margin-overlay-box').forEach(function(div) {
                div.style.background = 'rgba(255,255,255,1)';
                div.style.border = 'none';
            });
        } else {
            // solid → off
            overlayMode = 'off';
            redactionsVisible = false;
            marginsVisible = false;
            clearOverlaysByClass('redaction-overlay-box');
            clearOverlaysByClass('margin-overlay-box');
            clearOverlaysByClass('opinion-bounds-overlay');
        }
        _showOverlayMode();
    };

    // Put the mode on the button and on the guide (#299).
    //
    // The label and the colour came from two maps here, and they wrote to
    // an element no template held: the cycle had no cue at all, and the
    // key "r" was the only control. The rows of _viewer_help.html are the
    // one table now. The row carries the label, and checker.css carries
    // the colour of the mode, keyed by data-mode.
    function _showOverlayMode() {
        var row = document.querySelector(
            '#viewer-help-modes [data-overlay-mode="' + overlayMode + '"]');
        document.querySelectorAll('#viewer-help-modes li').forEach(function(li) {
            li.classList.toggle('active', li === row);
        });
        var btn = document.getElementById('toggle-overlays-btn');
        if (!btn) return;
        btn.dataset.mode = overlayMode;
        if (row) btn.textContent = row.dataset.label;
    }

    // The guide of the viewer (#299). The "?" opens it and closes it, and
    // the mode button moves the same cycle the key "r" moves.
    var _helpPanel = document.getElementById('viewer-help-panel');
    var _helpBtn = document.getElementById('viewer-help-btn');
    var _overlayBtn = document.getElementById('toggle-overlays-btn');

    function _setHelpOpen(open) {
        if (!_helpPanel) return;
        _helpPanel.hidden = !open;
        if (!_helpBtn) return;
        _helpBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
        _helpBtn.classList.toggle('active', open);
    }

    // The actions on a detection box apply only while the boxes are on, so
    // that part of the guide waits for them (#299). The panel opens itself
    // the first time in a session: a reviewer meets the guide once, and
    // the "?" gives it back at any time.
    function _showDetectionHelp(on) {
        var part = document.getElementById('viewer-help-detections');
        if (!part) return;
        part.hidden = !on;
        if (!on) return;
        var key = 'viewer-help-seen-' + documentId;
        try {
            if (sessionStorage.getItem(key)) return;
            sessionStorage.setItem(key, '1');
        } catch (e) {
            return;
        }
        _setHelpOpen(true);
    }

    if (_overlayBtn) {
        _overlayBtn.addEventListener('click', function() { window.toggleOverlays(); });
    }
    if (_helpBtn && _helpPanel) {
        _helpBtn.addEventListener('click', function() { _setHelpOpen(_helpPanel.hidden); });
    }
    _showOverlayMode();

    // ── Margin overlay ──
    // The strips are rows of the same list as the rects (#240), drawn by
    // the redaction draw paths with their own class and colour so the
    // solid mode and the "r" cycle keep telling them apart.
    var marginsVisible = true;

    // Build one redaction / whiteout overlay box.
    //
    // Both draw paths go through here -- the whole document on toggle and a
    // single page on lazy render -- because they were near-duplicates that
    // had already drifted: the lazily drawn boxes carried no tooltip and no
    // data-fill, so hovering a box showed nothing on the path users
    // actually hit while scrolling.
    //
    // `seqs` accumulates a per-type counter for the page being drawn, which
    // becomes the box id's ordinal.
    function _makeRedactionBox(r, pdfIndex, dsx, dsy, seqs) {
        var isMargin = (r.rect_type === 'margin');
        var div = document.createElement('div');
        div.className = isMargin ? 'margin-overlay-box' : 'redaction-overlay-box';
        div.dataset.fill = r.fill || 'black';
        div.style.position = 'absolute';
        div.style.left = (r.x0 * dsx) + 'px';
        div.style.top = (r.y0 * dsy) + 'px';
        div.style.width = ((r.x1 - r.x0) * dsx) + 'px';
        div.style.height = ((r.y1 - r.y0) * dsy) + 'px';
        var solid = (overlayMode === 'solid');
        if (isMargin) {
            div.style.background = solid ? 'rgba(255, 255, 255, 1)' : 'rgba(200, 200, 255, 0.3)';
            div.style.border = solid ? 'none' : '1px dashed rgba(100, 100, 200, 0.5)';
        } else if (r.fill === 'black') {
            div.style.background = solid ? 'rgba(0, 0, 0, 1)' : 'rgba(0, 0, 0, 0.4)';
            div.style.border = solid ? 'none' : '1px solid rgba(0, 0, 0, 0.7)';
        } else {
            div.style.background = solid ? 'rgba(255, 255, 255, 1)' : 'rgba(255, 255, 255, 0.5)';
            div.style.border = solid ? 'none' : '1px solid rgba(200, 200, 200, 0.8)';
        }
        div.style.pointerEvents = 'auto';
        div.style.cursor = 'pointer';
        div.style.zIndex = isMargin ? '5' : '6';

        var kind = (r.fill === 'white') ? 'whiteout' : 'redaction';
        var typeName = r.rect_type || r.fill;
        seqs[typeName] = (seqs[typeName] || 0) + 1;
        var who = r.origin === 'human' ? ' (drawn by hand)' : '';
        div.title = typeName + who + '\n' + _tagOverlayBox(
            div, kind, typeName, pdfIndex, seqs[typeName], r, 'pt'
        );

        if (!isMargin) {
            // Type label, hidden in solid mode so the preview stays faithful.
            var label = document.createElement('span');
            label.style.cssText = 'position:absolute;top:0;left:0;font-size:9px;padding:1px 3px;color:black;background:rgba(255,255,255,0.7);pointer-events:none;';
            label.textContent = typeName;
            if (solid) label.style.display = 'none';
            div.appendChild(label);
        }

        div.addEventListener('dblclick', function(e) {
            e.stopPropagation();
            _selectRedactionBox(div, pdfIndex, r, dsx, dsy);
        });
        return div;
    }

    // The scale of a page's boxes: rows are in PDF points, so the pdf.js
    // viewport gives the factor, and no render size is needed (#240).
    function _pointScale(pageIndex, canvas) {
        var pdfPage = pdfPages[pageIndex];
        if (!pdfPage) return null;
        var vp = pdfPage.getViewport({scale: 1});
        return [canvas.offsetWidth / vp.width, canvas.offsetHeight / vp.height];
    }

    // The boxes of one page, drawn into its wrapper; clears that page first.
    function _drawPageBoxes(pageData) {
        var pageEl = _pageDivForIndex(pageData.page_index);
        if (!pageEl) return;
        var wrapper = pageEl.querySelector('.canvas-wrapper');
        var canvas = pageEl.querySelector('.pdf-canvas');
        if (!wrapper || !canvas || !canvas.width || canvas.width < 10) return;
        wrapper.querySelectorAll('.redaction-overlay-box, .margin-overlay-box').forEach(function (el) { el.remove(); });
        var scale = _pointScale(pageData.page_index, canvas);
        if (!scale) return;
        var seqs = {};
        pageData.rects.forEach(function(r) {
            var isMargin = (r.rect_type === 'margin');
            if (isMargin && !marginsVisible) return;
            wrapper.appendChild(_makeRedactionBox(r, pageData.page_index, scale[0], scale[1], seqs));
        });
    }

    // ── Redaction overlay ──
    var redactionRects = null;
    var redactionsVisible = true;

    window.toggleRedactions = function() {
        redactionsVisible = !redactionsVisible;
        var btn = document.getElementById('toggle-redactions-btn');
        if (btn) btn.style.background = redactionsVisible ? '#dc2626' : '#6b7280';
        if (redactionsVisible && !redactionRects) {
            loadRedactionRows(drawRedactionOverlays);
        } else if (redactionsVisible) {
            drawRedactionOverlays();
        } else {
            clearOverlaysByClass('redaction-overlay-box');
            clearOverlaysByClass('margin-overlay-box');
        }
    };

    function drawRedactionOverlays() {
        clearOverlaysByClass('redaction-overlay-box');
        clearOverlaysByClass('margin-overlay-box');
        if (!redactionRects || !redactionsVisible) return;
        redactionRects.forEach(_drawPageBoxes);
    }

    var _selectedRedactionBox = null;

    function _selectRedactionBox(div, pdfIndex, rectData, dsx, dsy) {
        // Deselect previous
        _deselectRedactionBox();

        _selectedRedactionBox = div;
        div.style.outline = '2px solid #f59e0b';
        div.style.zIndex = '20';

        // Add delete button
        var delBtn = document.createElement('button');
        delBtn.className = 'redaction-edit-btn redaction-del-btn';
        delBtn.textContent = 'Delete';
        delBtn.style.cssText = 'position:absolute;top:-28px;right:0;background:#ef4444;color:white;border:none;padding:4px 10px;font-size:12px;font-weight:600;border-radius:4px;cursor:pointer;z-index:21;white-space:nowrap;line-height:1;';
        delBtn.addEventListener('click', function(e) {
            e.stopPropagation();
            if (!confirm('Delete this ' + (rectData.rect_type || 'redaction') + '?')) return;

            // A dismiss of a computed box, a withdrawal of a drawn one
            // (#240). The row is addressed by its id, and the answer is
            // read: a refusal must not remove a box the server kept.
            var csrfToken = document.querySelector('[name=csrfmiddlewaretoken]').value;
            fetch('/scans/' + documentId + '/redactions/' + rectData.id + '/dismiss/', {
                method: 'POST',
                headers: {'X-CSRFToken': csrfToken, 'Content-Type': 'application/json'},
            }).then(function(r) { return r.json(); })
            .then(function(data) {
                if (!data || data.status !== 'ok') {
                    showToast((data && data.message) || 'Failed to delete the box');
                    return;
                }
                div.remove();
                _selectedRedactionBox = null;
                if (redactionRects) {
                    redactionRects.forEach(function(pd) {
                        if (pd.page_index === pdfIndex) {
                            pd.rects = pd.rects.filter(function(r) { return r.id !== rectData.id; });
                        }
                    });
                }
                if (window.refreshFindings) window.refreshFindings();
            }).catch(function() { showToast('Failed to delete the box'); });
        });
        div.appendChild(delBtn);

        // Add resize handles (4 corners + 4 edges)
        var handles = ['nw','n','ne','w','e','sw','s','se'];
        handles.forEach(function(pos) {
            var h = document.createElement('div');
            h.className = 'redaction-resize-handle';
            h.dataset.pos = pos;
            h.style.cssText = 'position:absolute;width:8px;height:8px;background:#f59e0b;border:1px solid #fff;z-index:22;cursor:' + pos + '-resize;';
            if (pos.indexOf('n') >= 0) h.style.top = '-4px';
            if (pos.indexOf('s') >= 0) h.style.bottom = '-4px';
            if (pos.indexOf('w') >= 0) h.style.left = '-4px';
            if (pos.indexOf('e') >= 0) h.style.right = '-4px';
            if (pos === 'n' || pos === 's') { h.style.left = 'calc(50% - 4px)'; }
            if (pos === 'w' || pos === 'e') { h.style.top = 'calc(50% - 4px)'; }
            if (pos === 'nw' || pos === 'sw') h.style.left = '-4px';
            if (pos === 'ne' || pos === 'se') h.style.right = '-4px';

            h.addEventListener('mousedown', function(e) {
                e.stopPropagation();
                e.preventDefault();
                _startRedactionResize(div, pos, e, dsx, dsy, rectData, pdfIndex);
            });
            div.appendChild(h);
        });

        div.style.cursor = 'move';

        div._redactionMoveHandler = function(e) {
            if (e.target !== div) return;
            e.stopPropagation();
            e.preventDefault();
            var startX = e.clientX, startY = e.clientY;
            var startLeft = parseFloat(div.style.left);
            var startTop  = parseFloat(div.style.top);
            var hasMoved = false;

            function onMove(ev) {
                hasMoved = true;
                var z = cssToVisualScale(div);
                div.style.left = (startLeft + (ev.clientX - startX) / z) + 'px';
                div.style.top  = (startTop  + (ev.clientY - startY) / z) + 'px';
            }

            function onUp() {
                document.removeEventListener('mousemove', onMove);
                document.removeEventListener('mouseup', onUp);
                if (!hasMoved) return;
                var nL = parseFloat(div.style.left);
                var nT = parseFloat(div.style.top);
                var nW = parseFloat(div.style.width);
                var nH = parseFloat(div.style.height);
                _saveRedactionBox(div, rectData, {
                    x0: Math.round(nL / dsx * 10) / 10,
                    y0: Math.round(nT / dsy * 10) / 10,
                    x1: Math.round((nL + nW) / dsx * 10) / 10,
                    y1: Math.round((nT + nH) / dsy * 10) / 10,
                }, dsx, dsy);
            }

            document.addEventListener('mousemove', onMove);
            document.addEventListener('mouseup', onUp);
        };
        div.addEventListener('mousedown', div._redactionMoveHandler);
    }

    function _deselectRedactionBox() {
        if (!_selectedRedactionBox) return;
        _selectedRedactionBox.style.outline = '';
        _selectedRedactionBox.style.zIndex = '6';
        _selectedRedactionBox.style.cursor = '';
        // Remove edit controls
        _selectedRedactionBox.querySelectorAll('.redaction-edit-btn, .redaction-resize-handle').forEach(function(el) { el.remove(); });
        if (_selectedRedactionBox._redactionMoveHandler) {
            _selectedRedactionBox.removeEventListener('mousedown', _selectedRedactionBox._redactionMoveHandler);
            _selectedRedactionBox._redactionMoveHandler = null;
        }
        _selectedRedactionBox = null;
    }

    // Click anywhere else to deselect
    document.addEventListener('click', function() {
        _deselectRedactionBox();
    });

    function _startRedactionResize(div, pos, startEvent, dsx, dsy, rectData, pdfIndex) {
        var startX = startEvent.clientX;
        var startY = startEvent.clientY;
        var startLeft = parseFloat(div.style.left);
        var startTop = parseFloat(div.style.top);
        var startW = parseFloat(div.style.width);
        var startH = parseFloat(div.style.height);

        var hasMoved = false;
        function onMove(e) {
            hasMoved = true;
            var z = cssToVisualScale(div);
            var dx = (e.clientX - startX) / z;
            var dy = (e.clientY - startY) / z;
            var newLeft = startLeft, newTop = startTop, newW = startW, newH = startH;

            if (pos.indexOf('e') >= 0) newW = startW + dx;
            if (pos.indexOf('w') >= 0) { newW = startW - dx; newLeft = startLeft + dx; }
            if (pos.indexOf('s') >= 0) newH = startH + dy;
            if (pos.indexOf('n') >= 0) { newH = startH - dy; newTop = startTop + dy; }

            if (newW > 10) { div.style.left = newLeft + 'px'; div.style.width = newW + 'px'; }
            if (newH > 10) { div.style.top = newTop + 'px'; div.style.height = newH + 'px'; }
        }

        function onUp() {
            document.removeEventListener('mousemove', onMove);
            document.removeEventListener('mouseup', onUp);
            if (!hasMoved) return;

            var newLeft = parseFloat(div.style.left);
            var newTop = parseFloat(div.style.top);
            var newW = parseFloat(div.style.width);
            var newH = parseFloat(div.style.height);
            _saveRedactionBox(div, rectData, {
                x0: Math.round(newLeft / dsx * 10) / 10,
                y0: Math.round(newTop / dsy * 10) / 10,
                x1: Math.round((newLeft + newW) / dsx * 10) / 10,
                y1: Math.round((newTop + newH) / dsy * 10) / 10,
            }, dsx, dsy);
        }

        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
    }

    // Save a moved or resized box by its id (#240). A computed box
    // becomes a hand-drawn row: the server answers the id that holds it
    // now, and the box follows it. A refusal puts the box back where the
    // server has it and shows the message.
    function _saveRedactionBox(div, rectData, bbox, dsx, dsy) {
        var csrfToken = document.querySelector('[name=csrfmiddlewaretoken]').value;
        fetch('/scans/' + documentId + '/redactions/' + rectData.id + '/move/', {
            method: 'POST',
            headers: {'X-CSRFToken': csrfToken, 'Content-Type': 'application/json'},
            body: JSON.stringify(bbox),
        }).then(function(r) { return r.json(); })
        .then(function(data) {
            if (!data || data.status !== 'ok') {
                showToast((data && data.message) || 'Failed to save the box');
                div.style.left = (rectData.x0 * dsx) + 'px';
                div.style.top = (rectData.y0 * dsy) + 'px';
                div.style.width = ((rectData.x1 - rectData.x0) * dsx) + 'px';
                div.style.height = ((rectData.y1 - rectData.y0) * dsy) + 'px';
                return;
            }
            rectData.x0 = bbox.x0; rectData.y0 = bbox.y0;
            rectData.x1 = bbox.x1; rectData.y1 = bbox.y1;
            if (data.id !== undefined && data.id !== rectData.id) {
                rectData.id = data.id;
                rectData.origin = 'human';
                div.dataset.id = data.id;
            }
            if (window.refreshFindings) window.refreshFindings();
        }).catch(function() { showToast('Failed to save the box'); });
    }

    function drawRedactionOverlaysForPage(pdfIndex) {
        if (!redactionRects || !redactionsVisible) return;
        for (var i = 0; i < redactionRects.length; i++) {
            if (redactionRects[i].page_index === pdfIndex) {
                _drawPageBoxes(redactionRects[i]);
                return;
            }
        }
        // No boxes on this page: clear any stale ones left on it.
        var pageEl = _pageDivForIndex(pdfIndex);
        if (pageEl) {
            var w = pageEl.querySelector('.canvas-wrapper');
            if (w) w.querySelectorAll('.redaction-overlay-box, .margin-overlay-box').forEach(function (el) { el.remove(); });
        }
    }

    // ── Detection box editing ──
    var _selectedDetBox = null;

    function _saveDetectionBbox(newBbox, det) {
        fetch('/scans/' + documentId + '/update-detection/', {
            method: 'POST',
            headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrfToken},
            body: JSON.stringify({detection_id: det.id, new_bbox: newBbox}),
        }).then(function(r) { return r.json(); }).then(function(data) {
            if (!data || data.status !== 'ok') {
                // The box goes back where it was, and the server's word
                // is shown: a box left where it was dropped would say the
                // move was kept (#240).
                showToast((data && data.message) || 'Failed to save detection bbox');
                refreshOverlays();
                return;
            }
            if (data.status === 'ok') {
                det.bbox[0] = newBbox[0];
                det.bbox[1] = newBbox[1];
                det.bbox[2] = newBbox[2];
                det.bbox[3] = newBbox[3];
                // A moved model box becomes a hand-drawn row (#240):
                // the server names the row that holds it now, and every
                // later edit of this box must address that one.
                if (data.detection_id !== undefined && data.detection_id !== det.id) {
                    det.id = data.detection_id;
                    det.manual = true;
                }
                if (window.refreshFindings) window.refreshFindings();
            }
        }).catch(function() {
            console.error('Failed to save detection bbox');
            showToast('Failed to save detection bbox');
        });
    }

    function _selectDetectionBox(div, det) {
        _deselectDetectionBox();
        _selectedDetBox = div;
        div.style.outline = '2px solid #f59e0b';
        div.style.zIndex = '20';
        div.style.cursor = 'move';
        var selLabel = div.querySelector('.detection-label');
        if (selLabel) selLabel.style.display = '';

        // Scale factors: div is in display px, det.bbox is in image px
        var sx = parseFloat(div.style.width) / (det.bbox[2] - det.bbox[0]);
        var sy = parseFloat(div.style.height) / (det.bbox[3] - det.bbox[1]);

        // Action buttons toolbar
        var toolbar = document.createElement('div');
        toolbar.className = 'det-resize-handle';
        toolbar.style.cssText = 'position:absolute;top:-30px;left:0;display:flex;gap:4px;z-index:23;white-space:nowrap;';

        // "Delete" was a lie (#299): the endpoint writes a decision on a
        // model row, or withdraws a hand-drawn row. Nothing is deleted.
        var deleteBtn = document.createElement('button');
        deleteBtn.textContent = 'Dismiss';
        deleteBtn.title = 'Take this box out of the volume. Nothing is ' +
            'deleted, and a new import keeps your choice.';
        deleteBtn.style.cssText = 'background:#ef4444;color:white;border:none;padding:4px 10px;font-size:12px;font-weight:600;border-radius:4px;cursor:pointer;white-space:nowrap;flex-shrink:0;line-height:1;';
        deleteBtn.addEventListener('click', function(e) {
            e.stopPropagation();
            fetch('/scans/' + documentId + '/delete-detection/', {
                method: 'POST',
                headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrfToken},
                body: JSON.stringify({detection_id: det.id}),
            }).then(function(r) { return r.json(); }).then(function(data) {
                if (!data || data.status !== 'ok') {
                    showToast((data && data.message) || 'Could not dismiss the detection');
                    return;
                }
                if (data.status === 'ok') {
                    div.remove();
                    _selectedDetBox = null;
                    refreshOverlays();
                    // The findings were rebuilt by the endpoint (#240 PR D).
                    if (window.refreshFindings) window.refreshFindings();
                }
            }).catch(function() {
                console.error('Failed to dismiss detection');
                showToast('Could not dismiss the detection');
            });
        });
        toolbar.appendChild(deleteBtn);
        div.appendChild(toolbar);

        // Resize handles
        ['nw','n','ne','w','e','sw','s','se'].forEach(function(pos) {
            var h = document.createElement('div');
            h.className = 'det-resize-handle';
            h.dataset.pos = pos;
            h.style.cssText = 'position:absolute;width:8px;height:8px;background:#f59e0b;border:1px solid #fff;z-index:22;cursor:' + pos + '-resize;';
            if (pos.indexOf('n') >= 0) h.style.top = '-4px';
            if (pos.indexOf('s') >= 0) h.style.bottom = '-4px';
            if (pos.indexOf('w') >= 0) h.style.left = '-4px';
            if (pos.indexOf('e') >= 0) h.style.right = '-4px';
            if (pos === 'n' || pos === 's') h.style.left = 'calc(50% - 4px)';
            if (pos === 'w' || pos === 'e') h.style.top = 'calc(50% - 4px)';

            h.addEventListener('mousedown', function(e) {
                e.stopPropagation();
                e.preventDefault();
                var startX = e.clientX, startY = e.clientY;
                var startLeft = parseFloat(div.style.left);
                var startTop = parseFloat(div.style.top);
                var startW = parseFloat(div.style.width);
                var startH = parseFloat(div.style.height);

                var hasMoved = false;
                function onMove(e) {
                    hasMoved = true;
                    var z = cssToVisualScale(div);
                    var dx = (e.clientX - startX) / z, dy = (e.clientY - startY) / z;
                    var newLeft = startLeft, newTop = startTop, newW = startW, newH = startH;
                    if (pos.indexOf('e') >= 0) newW = Math.max(10, startW + dx);
                    if (pos.indexOf('s') >= 0) newH = Math.max(10, startH + dy);
                    if (pos.indexOf('w') >= 0) { newLeft = startLeft + dx; newW = Math.max(10, startW - dx); }
                    if (pos.indexOf('n') >= 0) { newTop = startTop + dy; newH = Math.max(10, startH - dy); }
                    div.style.left = newLeft + 'px';
                    div.style.top = newTop + 'px';
                    div.style.width = newW + 'px';
                    div.style.height = newH + 'px';
                }

                function onUp() {
                    document.removeEventListener('mousemove', onMove);
                    document.removeEventListener('mouseup', onUp);
                    if (!hasMoved) return;
                    var newBbox = [
                        parseFloat(div.style.left) / sx,
                        parseFloat(div.style.top) / sy,
                        (parseFloat(div.style.left) + parseFloat(div.style.width)) / sx,
                        (parseFloat(div.style.top) + parseFloat(div.style.height)) / sy,
                    ];
                    _saveDetectionBbox(newBbox, det);
                }

                document.addEventListener('mousemove', onMove);
                document.addEventListener('mouseup', onUp);
            });
            div.appendChild(h);
        });

        // Drag-to-move (click on box body, not handles)
        div._moveHandler = function(e) {
            if (e.target !== div) return;
            e.preventDefault();
            var startX = e.clientX, startY = e.clientY;
            var startLeft = parseFloat(div.style.left);
            var startTop = parseFloat(div.style.top);
            var hasMoved = false;

            function onMove(e) {
                hasMoved = true;
                var z = cssToVisualScale(div);
                div.style.left = (startLeft + (e.clientX - startX) / z) + 'px';
                div.style.top = (startTop + (e.clientY - startY) / z) + 'px';
            }

            function onUp() {
                document.removeEventListener('mousemove', onMove);
                document.removeEventListener('mouseup', onUp);
                if (!hasMoved) return;
                var newBbox = [
                    parseFloat(div.style.left) / sx,
                    parseFloat(div.style.top) / sy,
                    (parseFloat(div.style.left) + parseFloat(div.style.width)) / sx,
                    (parseFloat(div.style.top) + parseFloat(div.style.height)) / sy,
                ];
                _saveDetectionBbox(newBbox, det);
            }

            document.addEventListener('mousemove', onMove);
            document.addEventListener('mouseup', onUp);
        };
        div.addEventListener('mousedown', div._moveHandler);
    }

    function _deselectDetectionBox() {
        if (!_selectedDetBox) return;
        _selectedDetBox.style.outline = '';
        _selectedDetBox.style.zIndex = '';
        _selectedDetBox.style.cursor = '';
        _selectedDetBox.querySelectorAll('.det-resize-handle').forEach(function(el) { el.remove(); });
        if (_selectedDetBox._moveHandler) {
            _selectedDetBox.removeEventListener('mousedown', _selectedDetBox._moveHandler);
            _selectedDetBox._moveHandler = null;
        }
        _selectedDetBox = null;
    }

    document.addEventListener('click', function(e) {
        if (_selectedDetBox && !_selectedDetBox.contains(e.target)) {
            _deselectDetectionBox();
        }
    });


    function clearOverlaysByClass(className) {
        document.querySelectorAll('.' + className).forEach(function(el) {
            el.remove();
        });
    }

    // ── Scroll to page and highlight opinion range ──
    var _highlightedOpinion = null;
    var _currentViewPage = null;

    // Cache opinions data for within-page highlighting
    var _opinionsData = null;

    function _loadOpinionsData(cb) {
        if (_opinionsData) { cb(); return; }
        // The page carries the same payload the endpoint answers
        // (#opinions-data, read by viewer_sidebar.js too), and the
        // endpoint may read S3 for the printed numbers of a measured
        // volume. Read the tag; the fetch stays for a page without it.
        var tag = document.getElementById('opinions-data');
        if (tag) {
            try {
                _opinionsData = JSON.parse(tag.textContent);
                cb();
                return;
            } catch (e) { /* fall through to the fetch */ }
        }
        fetch('/scans/' + documentId + '/opinions-json/')
            .then(function(r) { return r.json(); })
            .then(function(data) { _opinionsData = data; cb(); });
    }

    window.highlightOpinion = function(captionPage, keyPage, opIndex) {
        clearOverlaysByClass('opinion-dim-overlay');

        document.querySelectorAll('.opinion-card').forEach(function(c) { c.classList.remove('selected'); });
        if (event && event.currentTarget) event.currentTarget.classList.add('selected');

        _loadOpinionsData(function() {
            // off: no dimming, just scroll
            // transparent: semi-transparent dim on other pages/regions
            // solid: opaque whiteout on other pages/regions
            if (overlayMode !== 'off') {
                var solid = (overlayMode === 'solid');
                var thisOp = (typeof opIndex === 'number' && opIndex < _opinionsData.length)
                    ? _opinionsData[opIndex] : null;
                if (!thisOp) return;

                var outsideRects = thisOp.outside_rects || [];

                // Dim pages outside the opinion
                var pageBg = solid ? 'rgba(255,255,255,1)' : 'rgba(0,0,0,0.3)';
                var allPages = document.querySelectorAll('.lazy-page');
                allPages.forEach(function(pageDiv) {
                    var num = parseInt(pageDiv.dataset.pdfIndex);
                    var wrapper = pageDiv.querySelector('.canvas-wrapper');
                    if (!wrapper) return;

                    if (num < captionPage || num > keyPage) {
                        var dim = document.createElement('div');
                        dim.className = 'opinion-dim-overlay';
                        dim.style.cssText = 'position:absolute;top:0;left:0;width:100%;height:100%;background:' + pageBg + ';z-index:15;pointer-events:none;';
                        wrapper.appendChild(dim);
                    }
                });

                // Draw outside_rects as dim overlays (PDF coordinates)
                var rectBg = solid ? 'rgba(255,255,255,1)' : 'rgba(0,0,0,0.25)';
                outsideRects.forEach(function(r) {
                    var pageEl = _pageDivForIndex(r.page_index);
                    if (!pageEl) return;
                    var wrapper = pageEl.querySelector('.canvas-wrapper');
                    var canvas = pageEl.querySelector('.pdf-canvas');
                    if (!wrapper || !canvas) return;

                    var dsx = canvas.offsetWidth / (canvas.width / pageScale(pageEl, SCALE));
                    var dsy = canvas.offsetHeight / (canvas.height / pageScale(pageEl, SCALE));

                    var dim = document.createElement('div');
                    dim.className = 'opinion-dim-overlay';
                    dim.style.position = 'absolute';
                    dim.style.left = (r.x0 * dsx) + 'px';
                    dim.style.top = (r.y0 * dsy) + 'px';
                    dim.style.width = ((r.x1 - r.x0) * dsx) + 'px';
                    dim.style.height = ((r.y1 - r.y0) * dsy) + 'px';
                    dim.style.background = rectBg;
                    dim.style.zIndex = '15';
                    dim.style.pointerEvents = 'none';
                    wrapper.appendChild(dim);
                });
            }
        });

        _highlightedOpinion = {start: captionPage, end: keyPage};
        _currentViewPage = captionPage;
        // Redaction/margin overlays are already drawn per page at render time
        // and don't change when selecting an opinion. Redrawing the whole
        // document here forced a ~1.4s reflow (layout thrash) on every click.
        var startEl = _pageDivForIndex(captionPage);
        if (startEl) window.scrollPageIntoView(startEl);
    };

    // Click on viewer background to clear opinion highlight

    container.addEventListener('dblclick', function() {
        if (_highlightedOpinion) {
            clearOverlaysByClass('opinion-dim-overlay');
            _highlightedOpinion = null;
            _currentOpIndex = -1;
            document.querySelectorAll('.opinion-card').forEach(function(c) { c.classList.remove('selected'); });
        }
    });

    // Arrow key navigation between opinions
    var _currentOpIndex = -1;

    document.addEventListener('keydown', function(e) {
        if (e.key === 'r' || e.key === 'R') {
            if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT') return;
            e.preventDefault();
            toggleOverlays();
        }
    });

    // Keep _currentOpIndex in sync when clicking
    var _wrappedHighlight = window.highlightOpinion;
    window.highlightOpinion = function(cp, kp, idx) {
        _currentOpIndex = idx;
        _wrappedHighlight(cp, kp, idx);
    };

});
