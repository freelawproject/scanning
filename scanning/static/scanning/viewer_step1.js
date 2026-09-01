// Step 1 persists its zoom level separately from steps 2/3 so that
// reviewing page numbers at high zoom doesn't carry over to opinion
// review (and vice versa).
window.__pdfZoomKey = 'pdfZoom_step1';

document.addEventListener('DOMContentLoaded', function () {
    const container = document.getElementById('pdf-viewer');
    if (!container) return;

    const pdfUrl = container.dataset.pdfUrl;
    const pageMap = JSON.parse(container.dataset.pageMap || '[]');
    const flaggedIndices = JSON.parse(container.dataset.flaggedIndices || '[]');
    const redactions = JSON.parse(container.dataset.redactions || '{}');
    const ocrByPage = JSON.parse(container.dataset.ocrByPage || '{}');
    const documentId = container.dataset.documentId;
    const csrfToken = document.querySelector('[name=csrfmiddlewaretoken]').value;

    const SCALE = 1.5;
    const PLACEHOLDER_HEIGHT = 1056; // 792 * 1.5 (letter height at scale)
    let pdfDoc = null;
    let defaultPageWidth = 918; // 612 * 1.5

    // Track which pages have been rendered
    var renderedPages = {};

    // Initialize pdf.js
    pdfjsLib.GlobalWorkerOptions.workerSrc =
        'https://cdn.jsdelivr.net/npm/pdfjs-dist@3.11.174/build/pdf.worker.min.js';

    var _previewHandle = null;

    function showPdf(pdf) {
        pdfDoc = pdf;
        container.innerHTML = '';
        createAllPlaceholders();
        setupLazyLoading();
    }

    // Swap the viewer to the original scan (issue #185). Stops the
    // preview polling first: the user chose the original, so a bitonal
    // that finishes later must not render over it.
    function startOriginalLoad() {
        if (_previewHandle) { _previewHandle.cancel(); _previewHandle = null; }
        renderPreviewBanner(null, startOriginalLoad);
        // Reset the lazy-render bookkeeping: showPdf() rebuilds the
        // placeholders with the same pdf-index values, and a stale
        // renderedPages entry makes the observer skip exactly the pages
        // the user already viewed in the preview.
        renderedPages = {};
        pdfDoc = null;
        showViewerMessage(container, 'Loading the original PDF...');
        loadOriginalPdf(documentId, {
            onReady: showPdf,
            onFail: function (err, url) {
                showOriginalLoadFailure(container, url);
            },
        });
    }

    _previewHandle = loadPreviewPdf(pdfUrl, {
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

    // --- Step 1: Create lightweight placeholder divs for every page ---
    function createAllPlaceholders() {
        var entries = pageMap;
        if (entries.length === 0) {
            for (var i = 0; i < pdfDoc.numPages; i++) {
                entries.push({ type: 'pdf_page', pdf_index: i, logical_number: i + 1 });
            }
        }

        entries.forEach(function (entry) {
            if (entry.type === 'pdf_page') {
                createPdfPlaceholder(entry);
            } else if (entry.type === 'missing') {
                renderMissingPage(entry);
            } else if (entry.type === 'inserted') {
                renderInsertedPage(entry);
            }
        });
    }

    function createPdfPlaceholder(entry) {
        var pageDiv = createPageContainer(entry.logical_number, entry.pdf_index);
        var isFlagged = flaggedIndices.indexOf(entry.pdf_index) !== -1;
        if (isFlagged) {
            pageDiv.classList.add('flagged');
        }
        if (entry.duplicate) {
            pageDiv.classList.add('duplicate-page');
            var label = pageDiv.querySelector('.page-label > span');
            if (label) {
                var badge = document.createElement('span');
                badge.className = 'dupe-badge';
                badge.textContent = 'DUPLICATE';
                label.appendChild(document.createTextNode(' '));
                label.appendChild(badge);
            }
        }
        // Set a placeholder size so the scroll area is correct
        var wrapper = pageDiv.querySelector('.canvas-wrapper');
        wrapper.style.width = defaultPageWidth + 'px';
        wrapper.style.height = PLACEHOLDER_HEIGHT + 'px';
        wrapper.style.background = '#f0f0f0';

        // Store entry data for lazy rendering
        // Use pdf_index for unique ID (logical_number can repeat for dupes)
        pageDiv.id = 'page-' + (entry.pdf_index + 1);
        pageDiv.dataset.pdfIndex = entry.pdf_index;
        pageDiv.dataset.logicalNumber = entry.logical_number;
        pageDiv.classList.add('lazy-page');

        container.appendChild(pageDiv);
    }

    // --- Step 2: IntersectionObserver for lazy rendering ---
    function setupLazyLoading() {
        var observer = new IntersectionObserver(function (entries) {
            entries.forEach(function (obsEntry) {
                if (!obsEntry.isIntersecting) return;
                var pageDiv = obsEntry.target;
                var pdfIndex = parseInt(pageDiv.dataset.pdfIndex);
                if (renderedPages[pdfIndex]) return;
                renderedPages[pdfIndex] = true;
                renderPdfPage(pageDiv, pdfIndex, parseInt(pageDiv.dataset.logicalNumber));
            });
        }, {
            root: document.querySelector('.viewer-panel'),
            rootMargin: '800px 0px', // start rendering 800px before visible
        });

        var lazyPages = container.querySelectorAll('.lazy-page');
        lazyPages.forEach(function (el) {
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
            wrapper.querySelectorAll('.redaction-delete-btn').forEach(function (el) { el.remove(); });
        }
        pageDiv.style.width = '';
        pageDiv.style.height = '';
    }

    function rerenderForCurrentZoom() {
        var currentZoom = getPdfZoom();
        container.querySelectorAll('.lazy-page').forEach(function (pageDiv) {
            var pdfIndex = parseInt(pageDiv.dataset.pdfIndex);
            if (!renderedPages[pdfIndex]) return;
            if (Math.abs(pageRenderedZoom(pageDiv) - currentZoom) < 0.001) return;
            if (isPageNearViewport(pageDiv)) {
                renderPdfPage(pageDiv, pdfIndex, parseInt(pageDiv.dataset.logicalNumber));
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

    // --- Render a single PDF page into its placeholder ---
    function renderPdfPage(pageDiv, pdfIndex, logicalNumber) {
        var zoom = getPdfZoom();
        var effScale = SCALE * zoom;
        pdfDoc.getPage(pdfIndex + 1).then(function (page) {
            var viewport = page.getViewport({ scale: effScale });
            var origViewport = page.getViewport({ scale: 1 });

            var canvas = pageDiv.querySelector('.pdf-canvas');
            canvas.width = viewport.width;
            canvas.height = viewport.height;

            var wrapper = pageDiv.querySelector('.canvas-wrapper');
            wrapper.style.width = viewport.width + 'px';
            wrapper.style.height = viewport.height + 'px';
            wrapper.style.background = '';

            // Update default width on first render (compare at base scale to
            // stay consistent regardless of the current zoom).
            var widthAtBaseScale = viewport.width / zoom;
            if (defaultPageWidth === 918 && widthAtBaseScale !== 918) {
                defaultPageWidth = widthAtBaseScale;
                updatePlaceholderWidths();
            }

            // Cancel any in-flight render on this page before kicking off a new one.
            if (pageDiv._renderTask) {
                try { pageDiv._renderTask.cancel(); } catch (_e) {}
                pageDiv._renderTask = null;
            }
            var task = page.render({
                canvasContext: canvas.getContext('2d'),
                viewport: viewport,
            });
            pageDiv._renderTask = task;
            task.promise.then(function () {
                if (pageDiv._renderTask === task) pageDiv._renderTask = null;
            }, function () { /* swallow cancel */ });

            // Setup redaction overlay
            var overlay = pageDiv.querySelector('.redaction-overlay');
            overlay.width = viewport.width;
            overlay.height = viewport.height;

            var pageRedactions = redactions[String(logicalNumber)] || [];
            drawExistingRedactions(overlay, pageRedactions, effScale);
            rebuildRedactionDivs(pageDiv, logicalNumber);

            pageDiv.dataset.scale = effScale;
            pageDiv.dataset.renderedZoom = zoom;
            pageDiv.dataset.pdfWidth = origViewport.width;
            pageDiv.dataset.pdfHeight = origViewport.height;
            applyZoomToPage(pageDiv);
        });
    }

    function updatePlaceholderWidths() {
        container.querySelectorAll('.lazy-page').forEach(function (el) {
            if (!renderedPages[parseInt(el.dataset.pdfIndex)]) {
                var wrapper = el.querySelector('.canvas-wrapper');
                if (wrapper) wrapper.style.width = defaultPageWidth + 'px';
            }
        });
        container.querySelectorAll('.missing-page').forEach(function (el) {
            el.style.width = defaultPageWidth + 'px';
        });
    }

    // --- Missing / Inserted pages (rendered immediately, they're lightweight) ---
    function renderMissingPage(entry) {
        var pageDiv = document.createElement('div');
        pageDiv.className = 'page-container missing-page';
        pageDiv.id = 'page-' + entry.logical_number;
        pageDiv.style.width = defaultPageWidth + 'px';
        pageDiv.innerHTML =
            '<div class="page-label">Page ' + entry.logical_number + ' &mdash; MISSING</div>' +
            '<div class="missing-placeholder">' +
            '  <p>This page was not found in the document.</p>' +
            '  <p>Upload a scan or image to fill this gap:</p>' +
            '  <form class="insert-form" enctype="multipart/form-data">' +
            '    <input type="hidden" name="page_number" value="' + entry.logical_number + '">' +
            '    <label class="upload-btn">' +
            '      Choose Image' +
            '      <input type="file" name="image" accept="image/*" style="display:none">' +
            '    </label>' +
            '  </form>' +
            '</div>';
        container.appendChild(pageDiv);

        var fileInput = pageDiv.querySelector('input[type="file"]');
        fileInput.addEventListener('change', function () {
            if (fileInput.files.length > 0) {
                // The anchor is the physical position the server
                // stamped on this placeholder (issue #214). The printed
                // number cannot place an image: front matter has none,
                // and two pages can print the same one.
                uploadPageInsert(entry, fileInput.files[0], pageDiv);
            }
        });
    }

    function renderInsertedPage(entry) {
        var pageDiv = document.createElement('div');
        pageDiv.className = 'page-container inserted-page';
        pageDiv.id = 'page-' + entry.logical_number;
        pageDiv.innerHTML = insertedPageHtml(entry.logical_number, entry.insert_url, entry.insert_edit_id, entry.unplaced);
        bindRemoveInsert(pageDiv, entry.insert_edit_id);
        container.appendChild(pageDiv);
    }

    // The label of an inserted page, with the button that takes it
    // back. A deletion has always had its undo and an insert had none,
    // so a wrong image could only be covered by another one (#214).
    function insertedPageHtml(logicalNumber, imageUrl, editId, unplaced) {
        // An image this volume has no position for is shown all the
        // same: Remove is the only way to take an insert back (#214).
        var label = unplaced
            ? 'Page ' + logicalNumber + ' &mdash; UPLOADED, BUT THIS VOLUME HAS NO PLACE FOR IT'
            : 'Page ' + logicalNumber + ' &mdash; INSERTED';
        var button = editId
            ? ' <button class="remove-insert-btn" style="cursor:pointer;' +
              'background:#dc2626;color:white;border:none;border-radius:3px;' +
              'padding:1px 6px;font-size:10px;margin-left:4px">Remove</button>'
            : '';
        return '<div class="page-label">' + label + button + '</div>' +
            '<div class="canvas-wrapper">' +
            '  <img src="' + imageUrl + '" class="inserted-image">' +
            '</div>';
    }

    function bindRemoveInsert(pageDiv, editId) {
        var button = pageDiv.querySelector('.remove-insert-btn');
        if (!button) { return; }
        button.addEventListener('click', function (e) {
            e.stopPropagation();
            if (!confirm('Remove this uploaded page?')) { return; }
            fetch('/scans/' + documentId + '/insert/remove/', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
                body: JSON.stringify({ edit_id: editId }),
            })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data.status !== 'ok') { return; }
                if (typeof window.refreshProcessActionBar === 'function') {
                    window.refreshProcessActionBar();
                }
                if (typeof window.onPageEditSaved === 'function') {
                    window.onPageEditSaved();
                }
                window.location.reload();
            });
        });
    }

    // --- Page container with OCR label, redact & delete buttons ---
    function createPageContainer(logicalNumber, pdfIndex) {
        var div = document.createElement('div');
        div.className = 'page-container';
        div.id = 'page-' + logicalNumber;

        var pdfPage = (pdfIndex !== undefined) ? pdfIndex + 1 : logicalNumber;
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
                    'title="Click to assign a page number">[no page # found — click to assign]</span>';
            }
        }

        div.innerHTML =
            '<div class="page-label">' +
            '  <span>PDF p.' + pdfPage + (ocrLabel ? ' &rarr; ' + ocrLabel : '') + '</span>' +
            '  <span class="page-tools">' +
            // Redact/whiteout buttons are only functional in step 2 (process_viewer.js)
            // '    <button class="redact-btn" data-fill="black" title="Draw a black redaction">Redact</button>' +
            // '    <button class="whiteout-btn" data-fill="white" title="Draw a white redaction">Whiteout</button>' +
            '    <button class="delete-btn" title="Delete this page">Delete</button>' +
            '  </span>' +
            '</div>' +
            '<div class="canvas-wrapper">' +
            '  <canvas class="pdf-canvas"></canvas>' +
            '  <canvas class="redaction-overlay"></canvas>' +
            '</div>';

        var redactBtn = div.querySelector('.redact-btn');
        if (redactBtn) {
            redactBtn.addEventListener('click', function () {
                toggleRedactionMode(div, logicalNumber, 'black');
            });
        }

        var whiteoutBtn = div.querySelector('.whiteout-btn');
        if (whiteoutBtn) {
            whiteoutBtn.addEventListener('click', function () {
                toggleRedactionMode(div, logicalNumber, 'white');
            });
        }

        var editBtn = div.querySelector('.editable-page');
        if (editBtn) {
            editBtn.addEventListener('click', function () {
                var current = ocr && ocr.detected ? ocr.detected : '';
                var num = prompt(
                    'Page number for PDF page ' + pdfPage +
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
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRFToken': csrfToken,
                    },
                    body: JSON.stringify({
                        pdf_page: pdfPage,
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
                        editBtn.className = 'ocr-tag editable-page';
                        editBtn.innerHTML = '#' + res.data.detected + ' <small>(manual)</small>';
                    } else {
                        editBtn.className = 'ocr-tag miss editable-page';
                        editBtn.innerHTML = '[no page # found — click to assign]';
                    }
                    window.onPageEditSaved();
                    // The server already rebuilt page_map, but the rendered
                    // duplicate/missing badges and the issue cards are now
                    // stale. Surface the pending banner so the user can
                    // recompute them (no RunPod, no rebuild). Only set the
                    // edit-specific wording when nothing was pending yet
                    // (banner hidden): a pending insert or deletion keeps its
                    // own text, which says the same thing about its own edit.
                    var banner = document.getElementById('pending-banner');
                    if (banner && banner.hidden) {
                        banner.textContent =
                            'Page numbers changed. Click "Recompute page ' +
                            'number issues" to refresh the duplicate flags ' +
                            'and the issues.';
                        banner.hidden = false;
                    }
                    var badge = document.getElementById('pending-badge');
                    if (badge) badge.hidden = false;
                });
            });
        }

        var deleteBtn = div.querySelector('.delete-btn');
        deleteBtn.addEventListener('click', function () {
            deletePageAndResolve(pdfPage, div);
        });

        // If page is already marked for deletion, show the deleted state
        if (typeof deletedPages !== 'undefined' && deletedPages.indexOf(pdfPage) !== -1) {
            markPageAsDeleted(div, pdfPage, 'PDF p.', csrfToken, documentId, deletePageAndResolve);
        }

        return div;
    }

    // Every page edit of review 1 is recorded, but nothing applies it to
    // the volume yet (issue #151), so say both. shared.js calls this
    // after a page deletion and after an undo; the page-number edit and
    // the insert upload call it directly.
    var SAVED_MESSAGE = 'Saved. We do not apply this change to the volume yet.';
    window.onPageEditSaved = function () {
        showToast(SAVED_MESSAGE, 'success');
    };

    // --- Delete page ---
    // deletePage is defined in shared.js; call resolveIssuesForPage after
    function deletePageAndResolve(pdfPage, pageDiv) {
        deletePage(csrfToken, documentId, pdfPage, pageDiv, 'PDF p.', deletePageAndResolve);
        setTimeout(function () { resolveIssuesForPage(pdfPage); }, 500);
    }

    function resolveIssuesForPage(pdfPage) {
        var issueCards = document.querySelectorAll('.issue-card[data-message]');
        issueCards.forEach(function (card) {
            var msg = card.dataset.message || '';
            var match = msg.match(/\[([0-9, ]+)\]/);
            if (match) {
                var pages = match[1].split(',').map(function (s) { return parseInt(s.trim()); });
                if (pages.indexOf(pdfPage) !== -1) {
                    var dupePages = pages.slice(1);
                    var allResolved = dupePages.every(function (p) {
                        var containers = document.querySelectorAll('.page-container');
                        for (var i = 0; i < containers.length; i++) {
                            var lbl = containers[i].querySelector('.page-label');
                            if (lbl && lbl.textContent.indexOf('PDF p.' + p) !== -1 &&
                                lbl.textContent.indexOf('DELETION') !== -1) {
                                return true;
                            }
                        }
                        return false;
                    });
                    if (allResolved) {
                        card.style.opacity = '0.3';
                        var btn = card.querySelector('.delete-dupe-btn');
                        if (btn) { btn.textContent = 'Resolved'; btn.disabled = true; }
                    }
                }
            }
            if (msg.indexOf('PDF page ' + pdfPage) !== -1 && card.dataset.check === 'duplicate_page') {
                card.style.opacity = '0.5';
            }
        });
    }

    // --- Redaction drawing ---
    var activeRedactionDiv = null;
    var activeRedactionFill = 'black';
    var isDrawing = false;
    var startX = 0, startY = 0;

    function toggleRedactionMode(pageDiv, pageNumber, fill) {
        var overlay = pageDiv.querySelector('.redaction-overlay');
        var blackBtn = pageDiv.querySelector('.redact-btn');
        var whiteBtn = pageDiv.querySelector('.whiteout-btn');

        // Clicking the same button again → deactivate
        if (activeRedactionDiv === pageDiv && activeRedactionFill === fill) {
            overlay.style.cursor = 'default';
            overlay.style.pointerEvents = 'none';
            blackBtn.classList.remove('active');
            whiteBtn.classList.remove('active');
            overlay.onmousedown = null;
            overlay.onmousemove = null;
            overlay.onmouseup = null;
            activeRedactionDiv = null;
            return;
        }

        // Deactivate previous
        if (activeRedactionDiv) {
            var prevOverlay = activeRedactionDiv.querySelector('.redaction-overlay');
            prevOverlay.style.cursor = 'default';
            prevOverlay.style.pointerEvents = 'none';
            activeRedactionDiv.querySelector('.redact-btn').classList.remove('active');
            activeRedactionDiv.querySelector('.whiteout-btn').classList.remove('active');
            prevOverlay.onmousedown = null;
            prevOverlay.onmousemove = null;
            prevOverlay.onmouseup = null;
        }

        overlay.style.cursor = 'crosshair';
        overlay.style.pointerEvents = 'auto';
        overlay.oncontextmenu = function (e) { e.preventDefault(); };
        activeRedactionDiv = pageDiv;
        activeRedactionFill = fill;
        if (fill === 'white') {
            whiteBtn.classList.add('active');
        } else {
            blackBtn.classList.add('active');
        }

        overlay.onmousedown = function (e) { onRedactStart(e, overlay, pageDiv, pageNumber); };
        overlay.onmousemove = function (e) { onRedactMove(e, overlay, pageDiv, pageNumber); };
        overlay.onmouseup = function (e) { onRedactEnd(e, overlay, pageDiv, pageNumber); };
    }

    function onRedactStart(e, overlay, pageDiv, pageNumber) {
        isDrawing = true;
        var pt = eventToCanvasPixels(e, overlay);
        startX = pt.x;
        startY = pt.y;
    }

    function onRedactMove(e, overlay, pageDiv, pageNumber) {
        if (!isDrawing) return;
        var pt = eventToCanvasPixels(e, overlay);
        var curX = pt.x;
        var curY = pt.y;

        var ctx = overlay.getContext('2d');
        ctx.clearRect(0, 0, overlay.width, overlay.height);

        var pageRedactions = redactions[String(pageNumber)] || [];
        drawExistingRedactions(overlay, pageRedactions, pageScale(pageDiv, SCALE));

        ctx.fillStyle = activeRedactionFill === 'white' ? 'rgba(255, 255, 255, 0.5)' : 'rgba(255, 0, 0, 0.3)';
        ctx.strokeStyle = activeRedactionFill === 'white' ? '#3b82f6' : 'red';
        ctx.lineWidth = 2;
        var x = Math.min(startX, curX);
        var y = Math.min(startY, curY);
        var w = Math.abs(curX - startX);
        var h = Math.abs(curY - startY);
        ctx.fillRect(x, y, w, h);
        ctx.strokeRect(x, y, w, h);
    }

    function onRedactEnd(e, overlay, pageDiv, pageNumber) {
        if (!isDrawing) return;
        isDrawing = false;

        var pt = eventToCanvasPixels(e, overlay);
        var endX = pt.x;
        var endY = pt.y;

        var scale = parseFloat(pageDiv.dataset.scale) || SCALE;

        var pdfX = Math.min(startX, endX) / scale;
        var pdfY = Math.min(startY, endY) / scale;
        var pdfW = Math.abs(endX - startX) / scale;
        var pdfH = Math.abs(endY - startY) / scale;

        if (pdfW < 5 || pdfH < 5) {
            var ctx = overlay.getContext('2d');
            ctx.clearRect(0, 0, overlay.width, overlay.height);
            drawExistingRedactions(overlay, redactions[String(pageNumber)] || [], scale);
            return;
        }

        var fill = activeRedactionFill;
        fetch('/scans/' + documentId + '/save-redaction-rect/', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': csrfToken,
            },
            body: JSON.stringify({
                page_number: pageNumber,
                x: pdfX,
                y: pdfY,
                width: pdfW,
                height: pdfH,
                fill: fill,
            }),
        })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (!redactions[String(pageNumber)]) {
                redactions[String(pageNumber)] = [];
            }
            redactions[String(pageNumber)].push({
                id: data.id,
                x: pdfX,
                y: pdfY,
                width: pdfW,
                height: pdfH,
                fill: fill,
            });

            var ctx = overlay.getContext('2d');
            ctx.clearRect(0, 0, overlay.width, overlay.height);
            drawExistingRedactions(overlay, redactions[String(pageNumber)], scale);
            rebuildRedactionDivs(pageDiv, pageNumber);
        });
    }

    // drawExistingRedactions is defined in shared.js

    // Build clickable × buttons over each redaction so users can remove them
    function rebuildRedactionDivs(pageDiv, pageNumber) {
        var wrapper = pageDiv.querySelector('.canvas-wrapper');
        // Remove old buttons
        wrapper.querySelectorAll('.redaction-delete-btn').forEach(function (el) { el.remove(); });

        var scale = pageScale(pageDiv, SCALE);
        var pageRedactions = redactions[String(pageNumber)] || [];
        pageRedactions.forEach(function (r, idx) {
            var btn = document.createElement('button');
            btn.className = 'redaction-delete-btn';
            btn.title = 'Remove this ' + (r.fill === 'white' ? 'whiteout' : 'redaction');
            btn.textContent = '\u00d7';
            btn.style.left = ((r.x + r.width) * scale - 18) + 'px';
            btn.style.top = (r.y * scale + 2) + 'px';
            btn.addEventListener('click', function (e) {
                e.stopPropagation();
                fetch('/scans/' + documentId + '/save-redaction-rect/', {
                    method: 'POST',
                    headers: { 'X-CSRFToken': csrfToken },
                }).then(function (resp) { return resp.json(); })
                .then(function (data) {
                    if (data.status === 'ok') {
                        redactions[String(pageNumber)].splice(idx, 1);
                        var overlay = pageDiv.querySelector('.redaction-overlay');
                        var ctx = overlay.getContext('2d');
                        ctx.clearRect(0, 0, overlay.width, overlay.height);
                        drawExistingRedactions(overlay, redactions[String(pageNumber)], scale);
                        rebuildRedactionDivs(pageDiv, pageNumber);
                    }
                });
            });
            wrapper.appendChild(btn);
        });
    }

    // --- Page insert upload ---
    function uploadPageInsert(entry, file, pageDiv) {
        var pageNumber = entry.logical_number;
        var formData = new FormData();
        formData.append('page_number', pageNumber);
        formData.append('anchor_pdf_page', entry.anchor_pdf_page);
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
            if (data.status !== 'ok') {
                if (placeholder) {
                    placeholder.innerHTML = '<p>' + (data.error || 'Upload failed. Try again.') + '</p>';
                }
                return;
            }
            pageDiv.className = 'page-container inserted-page';
            pageDiv.style.width = '';
            pageDiv.innerHTML = insertedPageHtml(pageNumber, data.image_url, data.edit_id);
            bindRemoveInsert(pageDiv, data.edit_id);
            if (typeof window.refreshProcessActionBar === 'function') {
                window.refreshProcessActionBar();
            }
            window.onPageEditSaved();
        })
        .catch(function (err) {
            if (placeholder) placeholder.innerHTML = '<p>Upload failed. Try again.</p>';
        });
    }

    // --- Show duplicate pages side by side in a modal ---
    window.showDuplicates = function (logicalNumber) {
        // Cast to number to guard against string/number type mismatch
        var logNum = Number(logicalNumber);
        var dupeEntries = pageMap.filter(function (e) {
            return e.type === 'pdf_page' && Number(e.logical_number) === logNum;
        });
        // Fallback: scan rendered DOM elements for matching logical-number
        if (dupeEntries.length < 2) {
            var doms = container.querySelectorAll('[data-logical-number="' + logNum + '"]');
            if (doms.length >= 2) {
                dupeEntries = Array.prototype.slice.call(doms).map(function (el) {
                    return { type: 'pdf_page', pdf_index: parseInt(el.dataset.pdfIndex), logical_number: logNum };
                });
            }
        }
        if (dupeEntries.length < 2) { goToPage(logicalNumber); return; }

        var overlay = document.createElement('div');
        overlay.className = 'dupe-modal-overlay';
        overlay.addEventListener('click', function (e) {
            if (e.target === overlay) document.body.removeChild(overlay);
        });

        var modal = document.createElement('div');
        modal.className = 'dupe-modal';

        var titleBar = document.createElement('div');
        titleBar.className = 'dupe-modal-title';
        titleBar.textContent = 'Duplicate — Page ' + logicalNumber + ' (' + dupeEntries.length + ' copies)';
        var closeBtn = document.createElement('button');
        closeBtn.className = 'dupe-modal-close';
        closeBtn.textContent = '\u2715';
        closeBtn.addEventListener('click', function () { document.body.removeChild(overlay); });
        titleBar.appendChild(closeBtn);
        modal.appendChild(titleBar);

        var pagesRow = document.createElement('div');
        pagesRow.className = 'dupe-modal-pages';

        dupeEntries.forEach(function (entry) {
            var col = document.createElement('div');
            col.className = 'dupe-modal-col';

            var lbl = document.createElement('div');
            lbl.className = 'dupe-modal-label';
            lbl.textContent = 'PDF page ' + (entry.pdf_index + 1);
            col.appendChild(lbl);

            var canvas = document.createElement('canvas');
            canvas.className = 'dupe-modal-canvas';
            col.appendChild(canvas);

            var delBtn = document.createElement('button');
            delBtn.className = 'btn dupe-modal-delete';
            delBtn.textContent = 'Delete this page';
            delBtn.addEventListener('click', function () {
                fetch('/scans/' + documentId + '/delete-page/', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
                    body: JSON.stringify({ pdf_page: entry.pdf_index + 1 }),
                }).then(function () {
                    document.body.removeChild(overlay);
                    location.reload();
                });
            });
            col.appendChild(delBtn);
            pagesRow.appendChild(col);

            if (pdfDoc) {
                pdfDoc.getPage(entry.pdf_index + 1).then(function (page) {
                    var vp = page.getViewport({ scale: 0.65 });
                    canvas.width = vp.width;
                    canvas.height = vp.height;
                    page.render({ canvasContext: canvas.getContext('2d'), viewport: vp });
                });
            }
        });

        modal.appendChild(pagesRow);
        overlay.appendChild(modal);
        document.body.appendChild(overlay);
    };

    // --- Scroll to page ---
    window.goToPage = function (elOrNum) {
        // Accepts an element or a number.
        //  - OCR "Pages" list items carry data-pdf-index: jump to that exact
        //    PDF page. Unambiguous even when logical page numbers repeat (e.g.
        //    unnumbered front matter borrowing the real pages' numbers, #90).
        //  - Issue cards / image badges carry data-page (a logical page
        //    number), resolved via data-logical-number with a page-<n> fallback.
        var el;
        if (typeof elOrNum === 'object' && elOrNum.dataset.pdfIndex !== undefined) {
            el = container.querySelector(
                '[data-pdf-index="' + elOrNum.dataset.pdfIndex + '"]'
            );
        } else {
            var pageNumber = (typeof elOrNum === 'object') ? elOrNum.dataset.page : elOrNum;
            el = container.querySelector('[data-logical-number="' + pageNumber + '"]')
                 || document.getElementById('page-' + pageNumber);
        }
        if (el) {
            window.scrollPageIntoView(el);
            el.classList.add('highlight');
            setTimeout(function () { el.classList.remove('highlight'); }, 2000);
        }
    };

    // --- Dismiss an issue ---
    window.dismissIssue = function (btn, issueId) {
        fetch('/scans/' + documentId + '/dismiss-issue/', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': csrfToken,
            },
            body: JSON.stringify({ issue_id: issueId }),
        })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (data.status !== 'ok') {
                alert(data.message || data.error || 'Could not dismiss issue.');
                return;
            }
            var card = btn.closest('.issue-card');
            if (card) card.remove();
            refreshIssuesCount();
            if (typeof window.refreshProcessActionBar === 'function') {
                window.refreshProcessActionBar();
            }
        })
        .catch(function () {
            alert('Could not dismiss issue. Please try again.');
        });
    };

    // Update the "Issues (N)" heading after a dismissal and swap in the
    // "all clear" state once the last issue is gone.
    function refreshIssuesCount() {
        var section = document.getElementById('issues-section');
        if (!section) return;
        var remaining = section.querySelectorAll('.issue-card').length;
        var countEl = document.getElementById('issues-count');
        if (countEl) countEl.textContent = remaining;
        if (remaining === 0) {
            section.hidden = true;
            var allClear = document.getElementById('issues-all-clear');
            if (allClear) allClear.hidden = false;
        }
    }

    // --- Delete duplicate pages ---
    window.deleteDuplicates = function (btn) {
        var msg = btn.dataset.message;
        var match = msg.match(/\[([0-9, ]+)\]/);
        if (!match) return;

        var pdfPages = match[1].split(',').map(function (s) { return parseInt(s.trim()); });
        var toDelete = pdfPages.slice(1);

        if (!confirm('Delete duplicate PDF page(s) ' + toDelete.join(', ') + '? (keeping page ' + pdfPages[0] + ')')) {
            return;
        }

        var promises = toDelete.map(function (pdfPage) {
            return fetch('/scans/' + documentId + '/delete-page/', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken,
                },
                body: JSON.stringify({ pdf_page: pdfPage }),
            }).then(function (r) { return r.json(); });
        });

        Promise.all(promises).then(function () {
            toDelete.forEach(function (pdfPage) {
                var containers = document.querySelectorAll('.page-container');
                containers.forEach(function (c) {
                    var label = c.querySelector('.page-label');
                    if (label && label.textContent.indexOf('PDF p.' + pdfPage) !== -1) {
                        c.style.opacity = '0.3';
                        c.style.pointerEvents = 'none';
                        label.innerHTML = '<span>PDF p.' + pdfPage + ' &mdash; MARKED FOR DELETION</span>';
                    }
                });
            });

            btn.textContent = 'Deleted';
            btn.disabled = true;
            btn.closest('.issue-card').style.opacity = '0.5';
        });
    };
});
