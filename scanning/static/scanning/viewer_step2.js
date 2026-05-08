/**
 * PDF viewer for reviewing blackletter processing output.
 * Supports switching between PDFs, redacted/unredacted toggle,
 * and drawing black/white redaction rectangles.
 */
document.addEventListener('DOMContentLoaded', function () {
    var container = document.getElementById('pdf-viewer');
    if (!container) return;

    var initialPdfUrl = container.dataset.pdfUrl;
    var documentId = container.dataset.documentId;
    var csrfToken = document.querySelector('[name=csrfmiddlewaretoken]').value;
    var viewOnly = container.dataset.viewOnly === 'true';
    var opinionEditMode = container.dataset.opinionEdit === 'true';
    var pageMap = JSON.parse(container.dataset.pageMap || '[]');
    var flaggedPages = JSON.parse(container.dataset.flaggedPages || '[]');
    var ocrByPage = JSON.parse(container.dataset.ocrByPage || '{}');

    // Build page_index ↔ logical_number mappings from pageMap
    var _pageIndexToLogical = {};
    var _logicalToPageIndex = {};
    pageMap.forEach(function(entry) {
        if (entry.type === 'pdf_page' && entry.pdf_index !== undefined) {
            _pageIndexToLogical[entry.pdf_index] = entry.logical_number;
            _logicalToPageIndex[entry.logical_number] = entry.pdf_index;
        }
    });
    function _pageNumForIndex(pageIndex) {
        return _pageIndexToLogical[pageIndex] || (pageIndex + 1);
    }
    function _pageIndexForNum(pageNum) {
        return (_logicalToPageIndex[pageNum] !== undefined) ? _logicalToPageIndex[pageNum] : (pageNum - 1);
    }

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
    var redactions = {};

    // Detection overlay state
    var allDetections = null; // loaded once from API
    var detectionsVisible = {}; // { pageNum: true/false }
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
        // visual pixels, so divide by the active zoom to get natural deltas.
        var zoom = getPdfZoom();
        var dx = (e.clientX - detDrawDragStartX) / zoom;
        var dy = (e.clientY - detDrawDragStartY) / zoom;
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
            fetch('/scans/' + documentId + '/redaction-rects/')
                .then(function(r) { if (r.ok) return r.json(); return []; })
                .then(function(data) { redactionRects = data; if (redactionsVisible) drawRedactionOverlays(); })
                .catch(function() {});
            fetch('/scans/' + documentId + '/margin-rects/')
                .then(function(r) { if (r.ok) return r.json(); return []; })
                .then(function(data) { marginRects = data; if (marginsVisible) drawMarginOverlays(); })
                .catch(function() {});
        })();
    }

    function loadPdf(url) {
        if (url === currentUrl && url.indexOf('?t=') === -1) return;
        currentUrl = url;

        if (observer) { observer.disconnect(); observer = null; }
        renderedPages = {};
        pdfDoc = null;
        redactions = {};
        container.innerHTML = '<div class="viewer-loading">Loading PDF...</div>';

        pdfjsLib.getDocument(url).promise.then(function (pdf) {
            pdfDoc = pdf;
            container.innerHTML = '';
            createPlaceholders(pdf.numPages);
            setupLazyLoading();
        }).catch(function (err) {
            container.innerHTML = '<div class="viewer-loading">Error loading PDF: ' + err.message + '</div>';
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

        // Flagged?
        if (flaggedPages.indexOf(pageNum) !== -1) {
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
            if (ocr.detected) {
                var tag = ocr.type === 'range' ? 'Range ' : '#';
                ocrLabel = '<span class="ocr-tag editable-page" data-pdf-page="' + pdfPage + '" ' +
                    'title="Click to correct page number">' + tag + ocr.detected +
                    ' <small>(' + ocr.zone + ' ' + (ocr.score ? ocr.score.toFixed(2) : '') + ')</small></span>';
            } else {
                ocrLabel = '<span class="ocr-tag miss editable-page" data-pdf-page="' + pdfPage + '" ' +
                    'title="Click to assign a page number">[no page # found]</span>';
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
                        var num = prompt('Enter the correct page number for PDF page ' + pp + ':', current);
                        if (num !== null && num.trim()) {
                            fetch('/scans/' + documentId + '/assign-page/', {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
                                body: JSON.stringify({ pdf_page: pp, page_number: num.trim() }),
                            })
                            .then(function (r) { return r.json(); })
                            .then(function (data) {
                                if (data.status === 'ok') {
                                    btn.className = 'ocr-tag editable-page';
                                    btn.innerHTML = '#' + num.trim() + ' <small>(manual)</small>';
                                }
                            });
                        }
                    });
                })(editBtn, pdfPage);
            }

            var detectBtn = div.querySelector('.detect-btn');
            var drawDetBtn = div.querySelector('.draw-det-btn');
            var redactBtn = div.querySelector('.redact-btn');
            var whiteoutBtn = div.querySelector('.whiteout-btn');
            (function (pageDiv, pNum) {
                if (detectBtn) detectBtn.addEventListener('click', function () { toggleDetections(pageDiv, pNum); });
                if (drawDetBtn) drawDetBtn.addEventListener('click', function () { activateDrawMode(pageDiv, pNum); });
                redactBtn.addEventListener('click', function () { toggleRedactionMode(pageDiv, pNum, 'black'); });
                whiteoutBtn.addEventListener('click', function () { toggleRedactionMode(pageDiv, pNum, 'white'); });
            })(div, pageNum);
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

    function renderPage(pageDiv, pdfIndex) {
        pdfDoc.getPage(pdfIndex + 1).then(function (page) {
            var pageNum = parseInt(pageDiv.dataset.pageNum);
            pdfPages[pageNum] = page;  // cache for overlay coordinate conversion
            var viewport = page.getViewport({ scale: SCALE });
            var canvas = pageDiv.querySelector('.pdf-canvas');
            canvas.width = viewport.width;
            canvas.height = viewport.height;

            var wrapper = pageDiv.querySelector('.canvas-wrapper');
            wrapper.style.width = viewport.width + 'px';
            wrapper.style.height = viewport.height + 'px';
            wrapper.style.background = '';
            pageDiv.style.width = viewport.width + 'px';

            if (defaultPageWidth === 918 && viewport.width !== 918) {
                defaultPageWidth = viewport.width;
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

            page.render({ canvasContext: canvas.getContext('2d'), viewport: viewport });

            if (!viewOnly) {
                // Setup redaction overlay
                var overlay = pageDiv.querySelector('.redaction-overlay');
                if (overlay) {
                    overlay.width = viewport.width;
                    overlay.height = viewport.height;
                }

                var pageRedactions = redactions[String(pageNum)] || [];
                if (overlay) drawExistingRedactions(overlay, pageRedactions, SCALE);
                rebuildRedactionDivs(pageDiv, pageNum);

                // Redraw overlays (skip when viewing individual opinion PDFs)
                if (!_viewingOpinion) {
                    if (redactionsVisible && redactionRects) {
                        drawRedactionOverlaysForPage(pageNum);
                    }
                    if (marginsVisible && marginRects) {
                        drawMarginOverlays();
                    }
                    if (overlayMode === 'bounds' && _boundsPageOwners) {
                        _drawBoundsForPage(pageDiv);
                    }
                }
            }
            if (!viewOnly && _globalDetections && allDetections) {
                detectionsVisible[pageNum] = true;
                drawDetectionOverlay(pageDiv, pageNum);
            }

            // Overlay original PDF crops for IMAGE detections
            if (allDetections && !_viewingOpinion) {
                var pageIdx = parseInt(pageDiv.dataset.pdfIndex);
                var imgDets = allDetections.filter(function(d) {
                    return d.page_index === pageIdx && d.label === 'IMAGE';
                });
                if (imgDets.length > 0) {
                    var canvasW = viewport.width;
                    var canvasH = viewport.height;
                    var imgW = imgDets[0].img_width || 1;
                    var imgH = imgDets[0].img_height || 1;
                    var sx = canvasW / imgW;
                    var sy = canvasH / imgH;
                    var pdfPtW = viewport.width / SCALE;
                    var pdfPtH = viewport.height / SCALE;
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
                            '&dpi=' + dpi;
                        wrapper.appendChild(img);
                    });
                }
            }
            applyZoomToPage(pageDiv);
        });
    }

    // --- Redaction drawing ---

    function toggleRedactionMode(pageDiv, pageNum, fill) {
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
            drawExistingRedactions(overlay, redactions[String(pageNum)] || [], SCALE);
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
            var pdfX = Math.min(startX, endX) / SCALE;
            var pdfY = Math.min(startY, endY) / SCALE;
            var pdfW = Math.abs(endX - startX) / SCALE;
            var pdfH = Math.abs(endY - startY) / SCALE;

            if (pdfW < 5 || pdfH < 5) {
                var ctx = overlay.getContext('2d');
                ctx.clearRect(0, 0, overlay.width, overlay.height);
                drawExistingRedactions(overlay, redactions[String(pageNum)] || [], SCALE);
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
                // Steps 2-3: save to redaction_rects.json
                var fillVal = activeRedactionFill;
                var imgW = cachedImgW || 1, imgH = cachedImgH || 1;
                if (allDetections) {
                    for (var di = 0; di < allDetections.length; di++) {
                        if (allDetections[di].page_index === _pageIndexForNum(pageNum)) {
                            imgW = allDetections[di].img_width || imgW;
                            imgH = allDetections[di].img_height || imgH;
                            break;
                        }
                    }
                }
                var pxPerPtX = imgW / (overlay.width / SCALE);
                var pxPerPtY = imgH / (overlay.height / SCALE);

                fetch('/scans/' + documentId + '/save-redaction-rect/', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
                    body: JSON.stringify({
                        page_index: _pageIndexForNum(pageNum),
                        action: 'add',
                        adjusted: {
                            x0: Math.round(pdfX * pxPerPtX),
                            y0: Math.round(pdfY * pxPerPtY),
                            x1: Math.round((pdfX + pdfW) * pxPerPtX),
                            y1: Math.round((pdfY + pdfH) * pxPerPtY),
                        },
                        fill: fillVal,
                        type: fillVal === 'white' ? 'whiteout' : 'manual_redact',
                    }),
                })
                .then(function (r) { return r.json(); })
                .then(function (data) {
                    refreshOverlays();
                    if (redactionsVisible) {
                        clearOverlaysByClass('redaction-overlay-box');
                        fetch('/scans/' + documentId + '/redaction-rects/')
                            .then(function(r2) { return r2.json(); })
                            .then(function(d2) { redactionRects = d2; drawRedactionOverlays(); });
                    }
                });
            }
        };
    }

    // Defined in shared.js: drawExistingRedactions(overlay, pageRedactions, scale)

    function rebuildRedactionDivs(pageDiv, pageNum) {
        var wrapper = pageDiv.querySelector('.canvas-wrapper');
        wrapper.querySelectorAll('.redaction-delete-btn').forEach(function (el) { el.remove(); });
        var pageRedactions = redactions[String(pageNum)] || [];
        pageRedactions.forEach(function (r, idx) {
            var btn = document.createElement('button');
            btn.className = 'redaction-delete-btn';
            btn.title = 'Remove this ' + (r.fill === 'white' ? 'whiteout' : 'redaction');
            btn.textContent = '\u00d7';
            btn.style.left = ((r.x + r.width) * SCALE - 18) + 'px';
            btn.style.top = (r.y * SCALE + 2) + 'px';
            btn.addEventListener('click', function (e) {
                e.stopPropagation();
                fetch('/scans/' + documentId + '/redaction/' + r.id + '/delete/', {
                    method: 'POST',
                    headers: { 'X-CSRFToken': csrfToken },
                }).then(function (resp) { return resp.json(); })
                .then(function (data) {
                    if (data.status === 'ok') {
                        redactions[String(pageNum)].splice(idx, 1);
                        var overlay = pageDiv.querySelector('.redaction-overlay');
                        var ctx = overlay.getContext('2d');
                        ctx.clearRect(0, 0, overlay.width, overlay.height);
                        drawExistingRedactions(overlay, redactions[String(pageNum)], SCALE);
                        rebuildRedactionDivs(pageDiv, pageNum);
                    }
                });
            });
            wrapper.appendChild(btn);
        });
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

    function toggleDetections(pageDiv, pageNum) {
        _globalDetections = !_globalDetections;

        if (_globalDetections) {
            loadDetections(function () {
                // Show on all rendered pages
                document.querySelectorAll('.lazy-page').forEach(function(pd) {
                    var pn = parseInt(pd.id.replace('pv-page-', ''));
                    var canvas = pd.querySelector('.pdf-canvas');
                    if (!canvas || canvas.width < 10) return;
                    detectionsVisible[pn] = true;
                    var btn = pd.querySelector('.detect-btn');
                    if (btn) btn.classList.add('active');
                    drawDetectionOverlay(pd, pn);
                });
            });
        } else {
            document.querySelectorAll('.lazy-page').forEach(function(pd) {
                var pn = parseInt(pd.id.replace('pv-page-', ''));
                detectionsVisible[pn] = false;
                var btn = pd.querySelector('.detect-btn');
                if (btn) btn.classList.remove('active');
                clearDetectionOverlay(pd, pn);
            });
        }
    }

    function drawDetectionOverlay(pageDiv, pageNum) {
        var wrapper = pageDiv.querySelector('.canvas-wrapper');
        // Remove old detection divs
        wrapper.querySelectorAll('.detection-box').forEach(function (el) { el.remove(); });

        // pageNum is 1-based viewer page; detection page_index is 0-based from the full PDF
        // We need to match by page_index = _pageIndexForNum(pageNum) (for full redacted)
        // or by page_number if available
        // Only show labels that affect the pipeline (pairing, redaction, headnotes, layout)
        var USED_LABELS = {
            CASE_CAPTION: true, KEY_ICON: true,
            STATE_ABBREVIATION: true, PAGE_HEADER: true, PAGE_NUMBER: true,
            DIVIDER: true, HEADNOTE_BRACKET: true, EDITORIAL: true,
            CASE_SEQUENCE: true, HEADNOTE: true, CASE_METADATA: true,
            FOOTNOTES: true, IMAGE: true,
        };
        var pageDets = allDetections.filter(function (d) {
            return d.page_index === _pageIndexForNum(pageNum) && (d.manual || USED_LABELS[d.label]);
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
            box.title = d.label + ' (' + d.confidence + ')';

            if (d.manual) box.classList.add('manual');

            var label = document.createElement('span');
            label.className = 'detection-label';
            label.style.background = color;
            label.style.display = 'none';
            label.textContent = d.label + (d.manual ? ' (manual)' : ' ' + d.confidence);
            box.appendChild(label);

            // Double-click to select (shows Delete button)
            (function(det, detBox) {
                detBox.addEventListener('dblclick', function(e) {
                    e.stopPropagation();
                    _selectDetectionBox(detBox, det);
                });
            })(d, box);

            wrapper.appendChild(box);
        });
    }

    function clearDetectionOverlay(pageDiv, pageNum) {
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

    function activateDrawMode(pageDiv, pageNum) {
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
        activeDrawPageNum = pageNum;
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
            drawExistingRedactions(overlay, redactions[String(pageNum)] || [], SCALE);
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
            drawExistingRedactions(overlay, redactions[String(pageNum)] || [], SCALE);
            if (w < 10 || h < 10) return;
            _showDetDrawPreview(pageDiv, pageNum, x, y, w, h);
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

    function _showDetDrawPreview(pageDiv, pageNum, x, y, w, h) {
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
        _showDetDrawPopup(pageDiv, pageNum);
    }

    function _showDetDrawPopup(pageDiv, pageNum) {
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
            _confirmDetDraw(pageDiv, pageNum, labelName);
        });
    }

    function _confirmDetDraw(pageDiv, pageNum, labelName) {
        var canvas = pageDiv.querySelector('.pdf-canvas');
        var canvasW = canvas.width, canvasH = canvas.height;

        // Get img dimensions from existing detections, or use cached/defaults
        var pageIdx = _pageIndexForNum(pageNum);
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
            page_number: pageNum,
            label: labelName,
            label_id: labelId,
            confidence: 1.0,
            bbox: [bx1, by1, bx2, by2],
            img_width: imgW,
            img_height: imgH,
        };

        // Save directly to detections.json
        fetch('/scans/' + documentId + '/add-single-detection/', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
            body: JSON.stringify(detData),
        }).then(function (r) { return r.json(); })
        .then(function (data) {
            // Add to allDetections so it shows in overlay
            if (!allDetections) allDetections = [];
            detData.manual = true;
            allDetections.push(detData);
            _cancelDetDraw();
            detectionsVisible[pageNum] = true;
            pageDiv.querySelector('.detect-btn').classList.add('active');
            drawDetectionOverlay(pageDiv, pageNum);

            // Auto re-pair if pairing label
            var _pLabels = ['CASE_CAPTION', 'KEY_ICON'];
            if (_pLabels.indexOf(labelName) >= 0) {
                fetch('/scans/' + documentId + '/pair-opinions/', {
                    method: 'POST',
                    headers: {'X-CSRFToken': csrfToken, 'Content-Type': 'application/json'},
                }).then(function(r) { return r.json(); })
                .then(function(d2) {
                    if (d2.error) {
                        alert('Error: ' + d2.error);
                        return;
                    }
                    window.location.reload();
                });
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
        marginRects = null;
        // Reload detections first (needed for img dimensions), then overlays
        fetch('/scans/' + documentId + '/detections/')
            .then(function(r) { if (r.ok) return r.json(); return []; })
            .then(function(data) {
                allDetections = data;
                if (data && data.length > 0) {
                    cachedImgW = data[0].img_width || cachedImgW;
                    cachedImgH = data[0].img_height || cachedImgH;
                }
            }).catch(function() {});
        fetch('/scans/' + documentId + '/redaction-rects/')
            .then(function(r) { if (r.ok) return r.json(); return []; })
            .then(function(data) { redactionRects = data; if (redactionsVisible) drawRedactionOverlays(); });
        fetch('/scans/' + documentId + '/margin-rects/')
            .then(function(r) { if (r.ok) return r.json(); return []; })
            .then(function(data) { marginRects = data; if (marginsVisible) drawMarginOverlays(); });
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

    // Cached page ownership map built from _opinionsData
    var _boundsPageOwners = null;
    // Cached outside_rects grouped by page number: {pageNum: [{opIdx, x0, y0, x1, y1}, ...]}
    var _boundsOutsideByPage = null;

    function _buildBoundsCache() {
        _boundsPageOwners = {};
        _boundsOutsideByPage = {};
        _opinionsData.forEach(function(op, idx) {
            var startPage = _pageNumForIndex(op.caption_page);
            var kp = (op.key_page !== undefined) ? op.key_page : op.caption_page;
            var endPage = _pageNumForIndex(kp);
            for (var p = startPage; p <= endPage; p++) {
                if (!_boundsPageOwners[p]) _boundsPageOwners[p] = [];
                _boundsPageOwners[p].push({idx: idx, isFirst: p === startPage, isLast: p === endPage});
            }
            (op.outside_rects || []).forEach(function(r) {
                var pn = _pageNumForIndex(r.page_index);
                if (!_boundsOutsideByPage[pn]) _boundsOutsideByPage[pn] = [];
                _boundsOutsideByPage[pn].push({opIdx: idx, x0: r.x0, y0: r.y0, x1: r.x1, y1: r.y1});
            });
        });
    }

    function _drawBoundsForPage(pageDiv) {
        if (!_boundsPageOwners) return;
        var num = parseInt(pageDiv.id.replace('pv-page-', ''));
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
                var dsx = canvas.offsetWidth / (canvas.width / SCALE);
                var dsy = canvas.offsetHeight / (canvas.height / SCALE);

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
            // Load rects if needed
            if (!redactionRects) {
                fetch('/scans/' + documentId + '/redaction-rects/')
                    .then(function(r) { if (r.ok) return r.json(); return []; })
                    .then(function(data) {
                        redactionRects = data;
                        redactionsVisible = true;
                        drawRedactionOverlays();
                    });
            } else {
                redactionsVisible = true;
                drawRedactionOverlays();
            }
            if (!marginRects) {
                fetch('/scans/' + documentId + '/margin-rects/')
                    .then(function(r) { if (r.ok) return r.json(); return []; })
                    .then(function(data) { marginRects = data; marginsVisible = true; drawMarginOverlays(); });
            } else {
                marginsVisible = true;
                drawMarginOverlays();
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
        var btn = document.getElementById('toggle-overlays-btn');
        if (btn) {
            var labels = {'off': 'Overlays Off (r)', 'bounds': 'Bounds (r)', 'transparent': 'Overlays (r)', 'solid': 'Solid (r)'};
            var colors = {'off': '#6b7280', 'bounds': '#2563eb', 'transparent': '#dc2626', 'solid': '#059669'};
            btn.textContent = labels[overlayMode];
            btn.style.background = colors[overlayMode];
        }
    };

    // ── Margin overlay ──
    var marginRects = null;
    var marginsVisible = true;

    window.toggleMargins = function() {
        marginsVisible = !marginsVisible;
        var btn = document.getElementById('toggle-margins-btn');
        btn.style.background = marginsVisible ? '#2563eb' : '#6b7280';

        if (marginsVisible && !marginRects) {
            fetch('/scans/' + documentId + '/margin-rects/')
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    marginRects = data;
                    drawMarginOverlays();
                });
        } else if (marginsVisible) {
            drawMarginOverlays();
        } else {
            clearOverlaysByClass('margin-overlay-box');
        }
    };

    function drawMarginOverlays() {
        clearOverlaysByClass('margin-overlay-box');
        if (!marginRects || !marginsVisible) return;

        marginRects.forEach(function(pageData) {
            var pageNum = _pageNumForIndex(pageData.page_index);
            var container = document.getElementById('pv-page-' + pageNum);
            if (!container) return;
            var wrapper = container.querySelector('.canvas-wrapper');
            var canvas = container.querySelector('.pdf-canvas');
            if (!wrapper || !canvas) return;

            // Margin rects are in PDF points — scale via viewport
            var pdfPage = pdfPages[pageNum];
            if (!pdfPage) return;
            var vp = pdfPage.getViewport({scale: 1});
            var msx = canvas.offsetWidth / vp.width;
            var msy = canvas.offsetHeight / vp.height;

            pageData.rects.forEach(function(r) {
                var div = document.createElement('div');
                div.className = 'margin-overlay-box';
                div.style.position = 'absolute';
                div.style.left = (r.x0 * msx) + 'px';
                div.style.top = (r.y0 * msy) + 'px';
                div.style.width = ((r.x1 - r.x0) * msx) + 'px';
                div.style.height = ((r.y1 - r.y0) * msy) + 'px';
                var solid = (overlayMode === 'solid');
                div.style.background = solid ? 'rgba(255, 255, 255, 1)' : 'rgba(200, 200, 255, 0.3)';
                div.style.border = solid ? 'none' : '1px dashed rgba(100, 100, 200, 0.5)';
                div.style.pointerEvents = 'auto';
                div.style.cursor = 'pointer';
                div.style.zIndex = '5';

                // Double-click to resize margin
                (function(marginRect, marginDiv, pageIdx, scaleX, scaleY) {
                    marginDiv.addEventListener('dblclick', function(e) {
                        e.stopPropagation();
                        _selectMarginBox(marginDiv, pageIdx, marginRect, scaleX, scaleY);
                    });
                })(r, div, pageData.page_index, msx, msy);

                wrapper.appendChild(div);
            });
        });
    }

    // ── Redaction overlay ──
    var redactionRects = null;
    var redactionsVisible = true;

    window.toggleRedactions = function() {
        redactionsVisible = !redactionsVisible;
        var btn = document.getElementById('toggle-redactions-btn');
        btn.style.background = redactionsVisible ? '#dc2626' : '#6b7280';

        if (redactionsVisible && !redactionRects) {
            btn.textContent = 'Loading...';
            // Ensure detections are loaded for img dimensions
            loadDetections(function() {});
            fetch('/scans/' + documentId + '/redaction-rects/')
                .then(function(r) {
                    if (!r.ok) throw new Error('HTTP ' + r.status);
                    return r.json();
                })
                .then(function(data) {
                    redactionRects = data;
                    btn.textContent = 'Redactions';
                    drawRedactionOverlays();
                })
                .catch(function(err) {
                    btn.textContent = 'Redactions';
                    alert('Failed to load redaction rects: ' + err);
                    redactionsVisible = false;
                    btn.style.background = '#6b7280';
                });
        } else if (redactionsVisible) {
            drawRedactionOverlays();
        } else {
            clearOverlaysByClass('redaction-overlay-box');
        }
    };

    function drawRedactionOverlays() {
        clearOverlaysByClass('redaction-overlay-box');
        if (!redactionRects || !redactionsVisible) return;

        redactionRects.forEach(function(pageData) {
            var pageNum = _pageNumForIndex(pageData.page_index);
            var container = document.getElementById('pv-page-' + pageNum);
            if (!container) return;
            var wrapper = container.querySelector('.canvas-wrapper');
            var canvas = container.querySelector('.pdf-canvas');
            if (!wrapper || !canvas) return;
            // Skip pages that haven't rendered yet (canvas has no real size)
            if (!canvas.width || canvas.width < 10) return;

            // Rects are in image pixel coords — same scaling as detection overlay
            var imgW = cachedImgW || 1, imgH = cachedImgH || 1;
            if (allDetections) {
                for (var di = 0; di < allDetections.length; di++) {
                    if (allDetections[di].page_index === pageData.page_index) {
                        imgW = allDetections[di].img_width || imgW;
                        imgH = allDetections[di].img_height || imgH;
                        break;
                    }
                }
                if (imgW <= 1 && allDetections.length > 0) {
                    imgW = allDetections[0].img_width || 1;
                    imgH = allDetections[0].img_height || 1;
                }
                // Update cache
                if (imgW > 1) { cachedImgW = imgW; cachedImgH = imgH; }
            }
            var dsx = canvas.offsetWidth / imgW;
            var dsy = canvas.offsetHeight / imgH;

            pageData.rects.forEach(function(r) {
                var div = document.createElement('div');
                div.className = 'redaction-overlay-box';
                div.dataset.fill = r.fill || 'black';
                div.style.position = 'absolute';
                div.style.left = (r.x0 * dsx) + 'px';
                div.style.top = (r.y0 * dsy) + 'px';
                div.style.width = ((r.x1 - r.x0) * dsx) + 'px';
                div.style.height = ((r.y1 - r.y0) * dsy) + 'px';
                var solid = (overlayMode === 'solid');
                if (r.fill === 'black') {
                    div.style.background = solid ? 'rgba(0, 0, 0, 1)' : 'rgba(0, 0, 0, 0.4)';
                    div.style.border = solid ? 'none' : '1px solid rgba(0, 0, 0, 0.7)';
                } else {
                    div.style.background = solid ? 'rgba(255, 255, 255, 1)' : 'rgba(255, 255, 255, 0.5)';
                    div.style.border = solid ? 'none' : '1px solid rgba(200, 200, 200, 0.8)';
                }
                div.style.pointerEvents = 'auto';
                div.style.cursor = 'pointer';
                div.style.zIndex = '6';
                // Show label only on hover
                div.title = r.type || r.fill;

                // Double-click to select for editing
                div.addEventListener('dblclick', function(e) {
                    e.stopPropagation();
                    _selectRedactionBox(div, pageNum, r, dsx, dsy);
                });

                wrapper.appendChild(div);
            });
        });
    }

    var _selectedRedactionBox = null;

    function _selectRedactionBox(div, pageNum, rectData, dsx, dsy) {
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
            if (!confirm('Delete this ' + (rectData.type || 'redaction') + '?')) return;

            // Remove rect directly from redaction_rects.json
            var csrfToken = document.querySelector('[name=csrfmiddlewaretoken]').value;
            fetch('/scans/' + documentId + '/save-redaction-rect/', {
                method: 'POST',
                headers: {'X-CSRFToken': csrfToken, 'Content-Type': 'application/json'},
                body: JSON.stringify({
                    page_index: _pageIndexForNum(pageNum),
                    action: 'delete',
                    original: {x0: rectData.x0, y0: rectData.y0, x1: rectData.x1, y1: rectData.y1},
                    type: rectData.type,
                    fill: rectData.fill,
                }),
            }).then(function(r) { return r.json(); })
            .then(function(data) {
                div.remove();
                _selectedRedactionBox = null;
                // Update cached rects
                if (redactionRects) {
                    redactionRects.forEach(function(pd) {
                        if (pd.page_index === _pageIndexForNum(pageNum)) {
                            pd.rects = pd.rects.filter(function(r) {
                                return !(Math.abs(r.x0 - rectData.x0) < 2 && Math.abs(r.y0 - rectData.y0) < 2);
                            });
                        }
                    });
                }
            });
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
                _startRedactionResize(div, pos, e, dsx, dsy, rectData, pageNum);
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
                var z = getPdfZoom();
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
                var oldX0 = rectData.x0, oldY0 = rectData.y0;
                var oldX1 = rectData.x1, oldY1 = rectData.y1;
                rectData.x0 = Math.round(nL / dsx * 10) / 10;
                rectData.y0 = Math.round(nT / dsy * 10) / 10;
                rectData.x1 = Math.round((nL + nW) / dsx * 10) / 10;
                rectData.y1 = Math.round((nT + nH) / dsy * 10) / 10;
                var csrfToken = document.querySelector('[name=csrfmiddlewaretoken]').value;
                fetch('/scans/' + documentId + '/save-redaction-rect/', {
                    method: 'POST',
                    headers: {'X-CSRFToken': csrfToken, 'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        page_index: _pageIndexForNum(pageNum),
                        original: {x0: oldX0, y0: oldY0, x1: oldX1, y1: oldY1},
                        adjusted: {x0: rectData.x0, y0: rectData.y0, x1: rectData.x1, y1: rectData.y1},
                        type: rectData.type,
                        fill: rectData.fill,
                    }),
                });
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

    function _startRedactionResize(div, pos, startEvent, dsx, dsy, rectData, pageNum) {
        var startX = startEvent.clientX;
        var startY = startEvent.clientY;
        var startLeft = parseFloat(div.style.left);
        var startTop = parseFloat(div.style.top);
        var startW = parseFloat(div.style.width);
        var startH = parseFloat(div.style.height);

        var hasMoved = false;
        function onMove(e) {
            hasMoved = true;
            var z = getPdfZoom();
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

            // Update rectData with new position in PDF coordinates
            var newLeft = parseFloat(div.style.left);
            var newTop = parseFloat(div.style.top);
            var newW = parseFloat(div.style.width);
            var newH = parseFloat(div.style.height);
            rectData.x0 = Math.round(newLeft / dsx * 10) / 10;
            rectData.y0 = Math.round(newTop / dsy * 10) / 10;
            rectData.x1 = Math.round((newLeft + newW) / dsx * 10) / 10;
            rectData.y1 = Math.round((newTop + newH) / dsy * 10) / 10;

            // Save the adjusted rect directly to redaction_rects.json
            var csrfToken = document.querySelector('[name=csrfmiddlewaretoken]').value;
            fetch('/scans/' + documentId + '/save-redaction-rect/', {
                method: 'POST',
                headers: {'X-CSRFToken': csrfToken, 'Content-Type': 'application/json'},
                body: JSON.stringify({
                    page_index: _pageIndexForNum(pageNum),
                    original: {x0: startLeft / dsx, y0: startTop / dsy,
                               x1: (startLeft + startW) / dsx, y1: (startTop + startH) / dsy},
                    adjusted: {x0: rectData.x0, y0: rectData.y0, x1: rectData.x1, y1: rectData.y1},
                    type: rectData.type,
                    fill: rectData.fill,
                }),
            });
        }

        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
    }

    function drawRedactionOverlaysForPage(pageNum) {
        if (!redactionRects || !redactionsVisible) return;
        var pageData = null;
        for (var i = 0; i < redactionRects.length; i++) {
            if (redactionRects[i].page_index === _pageIndexForNum(pageNum)) {
                pageData = redactionRects[i];
                break;
            }
        }
        if (!pageData) return;

        var container = document.getElementById('pv-page-' + pageNum);
        if (!container) return;
        var wrapper = container.querySelector('.canvas-wrapper');
        var canvas = container.querySelector('.pdf-canvas');
        if (!wrapper || !canvas || canvas.width < 10) return;

        // Remove existing boxes for this page
        wrapper.querySelectorAll('.redaction-overlay-box').forEach(function(el) { el.remove(); });

        var imgW = cachedImgW || 1, imgH = cachedImgH || 1;
        if (allDetections) {
            for (var di = 0; di < allDetections.length; di++) {
                if (allDetections[di].page_index === _pageIndexForNum(pageNum)) {
                    imgW = allDetections[di].img_width || imgW;
                    imgH = allDetections[di].img_height || imgH;
                    break;
                }
            }
            if (imgW <= 1 && allDetections.length > 0) {
                imgW = allDetections[0].img_width || 1;
                imgH = allDetections[0].img_height || 1;
            }
            if (imgW > 1) { cachedImgW = imgW; cachedImgH = imgH; }
        }
        var dsx = canvas.offsetWidth / imgW;
        var dsy = canvas.offsetHeight / imgH;

        pageData.rects.forEach(function(r) {
            var div = document.createElement('div');
            div.className = 'redaction-overlay-box';
            div.style.position = 'absolute';
            div.style.left = (r.x0 * dsx) + 'px';
            div.style.top = (r.y0 * dsy) + 'px';
            div.style.width = ((r.x1 - r.x0) * dsx) + 'px';
            div.style.height = ((r.y1 - r.y0) * dsy) + 'px';
            var solid = (overlayMode === 'solid');
            if (r.fill === 'black') {
                div.style.background = solid ? 'rgba(0, 0, 0, 1)' : 'rgba(0, 0, 0, 0.4)';
                div.style.border = solid ? 'none' : '1px solid rgba(0, 0, 0, 0.7)';
            } else {
                div.style.background = solid ? 'rgba(255, 255, 255, 1)' : 'rgba(255, 255, 255, 0.5)';
                div.style.border = solid ? 'none' : '1px solid rgba(200, 200, 200, 0.8)';
            }
            div.style.pointerEvents = 'auto';
            div.style.cursor = 'pointer';
            div.style.zIndex = '6';
            var label = document.createElement('span');
            label.style.cssText = 'position:absolute;top:0;left:0;font-size:9px;padding:1px 3px;color:black;background:rgba(255,255,255,0.7);pointer-events:none;';
            label.textContent = r.type || r.fill;
            if (solid) label.style.display = 'none';
            div.appendChild(label);
            div.addEventListener('dblclick', function(e) {
                e.stopPropagation();
                _selectRedactionBox(div, pageNum, r, dsx, dsy);
            });
            wrapper.appendChild(div);
        });
    }

    // ── Detection box editing ──
    var _selectedDetBox = null;

    function _saveDetectionBbox(newBbox, det) {
        fetch('/scans/' + documentId + '/update-detection/', {
            method: 'POST',
            headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrfToken},
            body: JSON.stringify({detection_id: det.id, new_bbox: newBbox}),
        }).then(function(r) { return r.json(); }).then(function(data) {
            if (data.status === 'ok') {
                det.bbox[0] = newBbox[0];
                det.bbox[1] = newBbox[1];
                det.bbox[2] = newBbox[2];
                det.bbox[3] = newBbox[3];
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

        var deleteBtn = document.createElement('button');
        deleteBtn.textContent = 'Delete';
        deleteBtn.style.cssText = 'background:#ef4444;color:white;border:none;padding:4px 10px;font-size:12px;font-weight:600;border-radius:4px;cursor:pointer;white-space:nowrap;flex-shrink:0;line-height:1;';
        deleteBtn.addEventListener('click', function(e) {
            e.stopPropagation();
            fetch('/scans/' + documentId + '/delete-detection/', {
                method: 'POST',
                headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrfToken},
                body: JSON.stringify({detection_id: det.id}),
            }).then(function(r) { return r.json(); }).then(function(data) {
                if (data.status === 'ok') {
                    div.remove();
                    _selectedDetBox = null;
                    var sidebarItems = document.querySelectorAll('[data-unmatched-page="' + det.page_index + '"][data-unmatched-label="' + det.label + '"]');
                    sidebarItems.forEach(function(el) { el.remove(); });
                    refreshOverlays();
                }
            }).catch(function() {
                console.error('Failed to delete detection');
                showToast('Failed to delete detection');
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
                    var z = getPdfZoom();
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
                var z = getPdfZoom();
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

    // ── Margin box editing ──
    var _selectedMarginBox = null;

    function _selectMarginBox(div, pageIdx, rectData, scaleX, scaleY) {
        _deselectMarginBox();
        _selectedMarginBox = div;
        div.style.outline = '2px solid #2563eb';
        div.style.zIndex = '20';
        div.style.background = 'rgba(200, 200, 255, 0.5)';

        // Add delete button
        var delBtn = document.createElement('button');
        delBtn.className = 'margin-resize-handle';
        delBtn.textContent = 'Delete';
        delBtn.style.cssText = 'position:absolute;top:-24px;right:0;background:#ef4444;color:white;border:none;padding:10px 8px;font-size:11px;border-radius:3px;cursor:pointer;z-index:23;';
        delBtn.addEventListener('click', function(e) {
            e.stopPropagation();
            if (!confirm('Delete this margin?')) return;
            fetch('/scans/' + documentId + '/save-margin-rect/', {
                method: 'POST',
                headers: {'X-CSRFToken': csrfToken, 'Content-Type': 'application/json'},
                body: JSON.stringify({page_index: pageIdx, action: 'delete', original: rectData}),
            }).then(function() {
                div.remove();
                _selectedMarginBox = null;
                // Update cached margin rects
                if (marginRects) {
                    marginRects.forEach(function(pd) {
                        if (pd.page_index === pageIdx) {
                            pd.rects = pd.rects.filter(function(r) {
                                return !(Math.abs(r.x0 - rectData.x0) < 2 && Math.abs(r.y0 - rectData.y0) < 2);
                            });
                        }
                    });
                }
            });
        });
        div.appendChild(delBtn);

        // Add resize handles
        var handles = ['nw','n','ne','w','e','sw','s','se'];
        handles.forEach(function(pos) {
            var h = document.createElement('div');
            h.className = 'margin-resize-handle';
            h.dataset.pos = pos;
            h.style.cssText = 'position:absolute;width:8px;height:8px;background:#2563eb;border:1px solid #fff;z-index:22;cursor:' + pos + '-resize;';
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
                function onMove(ev) {
                    hasMoved = true;
                    var z = getPdfZoom();
                    var dx = (ev.clientX - startX) / z, dy = (ev.clientY - startY) / z;
                    var nL = startLeft, nT = startTop, nW = startW, nH = startH;
                    if (pos.indexOf('e') >= 0) nW = startW + dx;
                    if (pos.indexOf('w') >= 0) { nW = startW - dx; nL = startLeft + dx; }
                    if (pos.indexOf('s') >= 0) nH = startH + dy;
                    if (pos.indexOf('n') >= 0) { nH = startH - dy; nT = startTop + dy; }
                    if (nW > 5) { div.style.left = nL + 'px'; div.style.width = nW + 'px'; }
                    if (nH > 5) { div.style.top = nT + 'px'; div.style.height = nH + 'px'; }
                }
                function onUp() {
                    document.removeEventListener('mousemove', onMove);
                    document.removeEventListener('mouseup', onUp);
                    if (!hasMoved) return;
                    // Save updated margin rect (PDF points)
                    var nL = parseFloat(div.style.left);
                    var nT = parseFloat(div.style.top);
                    var nW = parseFloat(div.style.width);
                    var nH = parseFloat(div.style.height);
                    var updated = {
                        x0: Math.round(nL / scaleX * 10) / 10,
                        y0: Math.round(nT / scaleY * 10) / 10,
                        x1: Math.round((nL + nW) / scaleX * 10) / 10,
                        y1: Math.round((nT + nH) / scaleY * 10) / 10,
                    };
                    fetch('/scans/' + documentId + '/save-margin-rect/', {
                        method: 'POST',
                        headers: {'X-CSRFToken': csrfToken, 'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            page_index: pageIdx,
                            original: rectData,
                            adjusted: updated,
                        }),
                    });
                    rectData.x0 = updated.x0;
                    rectData.y0 = updated.y0;
                    rectData.x1 = updated.x1;
                    rectData.y1 = updated.y1;
                }
                document.addEventListener('mousemove', onMove);
                document.addEventListener('mouseup', onUp);
            });
            div.appendChild(h);
        });
    }

    function _deselectMarginBox() {
        if (!_selectedMarginBox) return;
        _selectedMarginBox.style.outline = '';
        _selectedMarginBox.style.zIndex = '5';
        _selectedMarginBox.querySelectorAll('.margin-resize-handle').forEach(function(el) { el.remove(); });
        _selectedMarginBox = null;
    }

    document.addEventListener('click', function(e) {
        if (_selectedMarginBox && !_selectedMarginBox.contains(e.target)) {
            _deselectMarginBox();
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

    window.scrollToPage = function(pageNum) {
        var el = document.getElementById('pv-page-' + pageNum);
        if (el) {
            el.scrollIntoView({behavior: 'smooth', block: 'start'});
        }
    };

    // Cache opinions data for within-page highlighting
    var _opinionsData = null;

    function _loadOpinionsData(cb) {
        if (_opinionsData) { cb(); return; }
        fetch('/scans/' + documentId + '/opinions-json/')
            .then(function(r) { return r.json(); })
            .then(function(data) { _opinionsData = data; cb(); });
    }

    window.highlightOpinion = function(captionPage, keyPage, opIndex) {
        clearOverlaysByClass('opinion-dim-overlay');

        document.querySelectorAll('.opinion-card').forEach(function(c) { c.classList.remove('selected'); });
        if (event && event.currentTarget) event.currentTarget.classList.add('selected');

        var startPage = _pageNumForIndex(captionPage);
        var endPage = _pageNumForIndex(keyPage);

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
                    var num = parseInt(pageDiv.id.replace('pv-page-', ''));
                    var wrapper = pageDiv.querySelector('.canvas-wrapper');
                    if (!wrapper) return;

                    if (num < startPage || num > endPage) {
                        var dim = document.createElement('div');
                        dim.className = 'opinion-dim-overlay';
                        dim.style.cssText = 'position:absolute;top:0;left:0;width:100%;height:100%;background:' + pageBg + ';z-index:15;pointer-events:none;';
                        wrapper.appendChild(dim);
                    }
                });

                // Draw outside_rects as dim overlays (PDF coordinates)
                var rectBg = solid ? 'rgba(255,255,255,1)' : 'rgba(0,0,0,0.25)';
                outsideRects.forEach(function(r) {
                    var pageNum = _pageNumForIndex(r.page_index);
                    var container = document.getElementById('pv-page-' + pageNum);
                    if (!container) return;
                    var wrapper = container.querySelector('.canvas-wrapper');
                    var canvas = container.querySelector('.pdf-canvas');
                    if (!wrapper || !canvas) return;

                    var dsx = canvas.offsetWidth / (canvas.width / SCALE);
                    var dsy = canvas.offsetHeight / (canvas.height / SCALE);

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

        _highlightedOpinion = {start: startPage, end: endPage};
        _currentViewPage = startPage;
        if (redactionsVisible) drawRedactionOverlays();
        if (marginsVisible) drawMarginOverlays();
        scrollToPage(startPage);
    };

    // Click on viewer background to clear opinion highlight

    container.addEventListener('dblclick', function() {
        if (_highlightedOpinion) {
            clearOverlaysByClass('opinion-dim-overlay');
            _highlightedOpinion = null;
            _currentOpIndex = -1;
            document.querySelectorAll('.opinion-card').forEach(function(c) { c.classList.remove('selected'); });
            if (redactionsVisible) drawRedactionOverlays();
            if (marginsVisible) drawMarginOverlays();
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
