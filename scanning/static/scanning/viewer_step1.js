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

    // What a curator may type as a page number: one printed number, or
    // the range one physical page carries when the book prints several
    // pages on it (issue #233). The server takes the same two shapes
    // and stores a range with a hyphen, whatever dash was typed, so the
    // dashes here are the ones a reporter prints and a person pastes.
    const PAGE_ENTRY_RE = /^(\d{1,4})(?:\s*[-–—]\s*(\d{1,4}))?$/;

    // The label the page render gives a range, so a saved entry reads
    // the same before and after a reload.
    const RANGE_TAG = 'Range ';

    function isPageNumberEntry(text) {
        var parts = PAGE_ENTRY_RE.exec(text);
        if (!parts) return false;
        var first = parseInt(parts[1], 10);
        if (first < 1) return false;
        if (parts[2] === undefined) return true;
        return first < parseInt(parts[2], 10);
    }

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
        goToRequestedPage();
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
    // One placeholder stands for one gap. A range missing at the end of
    // the volume is one gap too (#256), so that placeholder carries the
    // range as its label and says the plural: the upload takes a PDF of
    // the whole range, and the button asks a scanner for all of it.
    function renderMissingPage(entry) {
        var pageDiv = document.createElement('div');
        pageDiv.className = 'page-container missing-page';
        pageDiv.id = 'page-' + entry.logical_number;
        pageDiv.style.width = defaultPageWidth + 'px';
        var missingLabel = escapeHtml(entry.logical_number);
        var range = entry.missing_range;
        // The stored label carries the hyphen every reader of a range
        // parses (#233); the heading shows the dash a book prints.
        var heading = range
            ? 'Pages ' + escapeHtml(range[0] + '\u2013' + range[1])
            : 'Page ' + missingLabel;
        pageDiv.innerHTML =
            '<div class="page-label">' + heading + ' &mdash; MISSING</div>' +
            '<div class="missing-placeholder">' +
            '  <p>' + (range
                ? 'These pages were not found in the document.'
                : 'This page was not found in the document.') + '</p>' +
            '  <p>' + (range
                ? 'Upload a PDF of the missing pages, or an image of one:'
                : 'Upload an image or a PDF to fill this gap:') + '</p>' +
            '  <form class="insert-form" enctype="multipart/form-data">' +
            '    <input type="hidden" name="page_number" value="' + missingLabel + '">' +
            '    <label class="upload-btn">' +
            '      Choose a file' +
            '      <input type="file" name="image" accept="image/*,application/pdf" style="display:none">' +
            '    </label>' +
            '  </form>' +
            '  <button class="repair-btn" data-action="insert" ' +
            'data-anchor-pdf-page="' + entry.anchor_pdf_page + '" ' +
            'data-logical-page="' + missingLabel + '" ' +
            (range ? 'data-repair-what="scan the missing pages" ' : '') +
            'title="Ask a scanner with the book to scan ' +
            (range ? 'these pages">Ask for these pages' : 'this page">Ask for this page') +
            '</button>' +
            '</div>';
        container.appendChild(pageDiv);
        pageDiv.dataset.anchorPdfPage = entry.anchor_pdf_page;
        if (range) { pageDiv.dataset.missingRange = '1'; }
        drawRepairNote(pageDiv, findRepair('insert', entry.anchor_pdf_page));

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
        pageDiv.innerHTML = insertedPageHtml(entry.logical_number, entry.insert_url, entry.insert_edit_id, entry.unplaced, entry.insert_kind, entry.insert_file_url);
        bindRemoveInsert(pageDiv, entry.insert_edit_id);
        container.appendChild(pageDiv);
        drawInsertedPdf(pageDiv);
    }

    // A curator may upload a PDF of the page as well as an image
    // (#232), and an <img> draws nothing for one. Render its first page
    // into the canvas, and leave the link that opens the whole file.
    // The link stands whatever happens: the file is on the default
    // storage, and a cross-origin read of it needs a bucket CORS rule
    // that a deployment may not carry yet.
    function drawInsertedPdf(pageDiv) {
        var canvas = pageDiv.querySelector('.inserted-pdf');
        if (!canvas || !canvas.dataset.pdfUrl) { return; }
        pdfjsLib.getDocument(canvas.dataset.pdfUrl).promise.then(function (pdf) {
            var note = pageDiv.querySelector('.inserted-pdf-note');
            if (note && pdf.numPages > 1) {
                note.textContent = 'PDF, ' + pdf.numPages + ' pages. ' +
                    'The first one is shown.';
            }
            return pdf.getPage(1).then(function (page) {
                var viewport = page.getViewport({ scale: SCALE * getPdfZoom() });
                canvas.width = viewport.width;
                canvas.height = viewport.height;
                canvas.style.width = '100%';
                return page.render({
                    canvasContext: canvas.getContext('2d'),
                    viewport: viewport,
                }).promise;
            });
        }).catch(function () {
            var note = pageDiv.querySelector('.inserted-pdf-note');
            if (note) {
                note.textContent = 'This PDF cannot be shown here. ' +
                    'Open it with the link above.';
            }
        });
    }

    // The label of an inserted page, with the button that takes it
    // back. A deletion has always had its undo and an insert had none,
    // so a wrong image could only be covered by another one (#214).
    function insertedPageHtml(logicalNumber, imageUrl, editId, unplaced, kind, fileUrl) {
        // The printed number is a person's typing (a curator files the
        // page under what is printed on it, and that is not always
        // digits), and every viewer of this scan sees it. So it is
        // escaped here, at the sink, not narrowed at the source.
        var printed = escapeHtml(logicalNumber);
        // An image this volume has no position for is shown all the
        // same: Remove is the only way to take an insert back (#214).
        var label = unplaced
            ? 'Page ' + printed + ' &mdash; UPLOADED, BUT THIS VOLUME HAS NO PLACE FOR IT'
            : 'Page ' + printed + ' &mdash; INSERTED';
        var button = editId
            ? ' <button class="remove-insert-btn" style="cursor:pointer;' +
              'background:#dc2626;color:white;border:none;border-radius:3px;' +
              'padding:1px 6px;font-size:10px;margin-left:4px">Remove</button>'
            : '';
        var pdfUrl = escapeHtml(fileUrl || imageUrl);
        var body = kind === 'pdf'
            ? '  <p class="inserted-pdf-note">' +
              '<a href="' + pdfUrl + '" target="_blank" rel="noopener">Open the PDF</a>' +
              '</p>' +
              '  <canvas class="inserted-pdf" data-pdf-url="' + pdfUrl + '"></canvas>'
            : '  <img src="' + escapeHtml(imageUrl) + '" class="inserted-image">';
        return '<div class="page-label">' + label + button + '</div>' +
            '<div class="canvas-wrapper">' +
            body +
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
                var tag = ocr.type === 'range' ? RANGE_TAG : '#';
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
            '    <button class="replace-btn" data-pdf-page="' + pdfPage + '" ' +
            'title="Upload an image or a PDF that stands in for this page">Replace</button>' +
            '    <button class="delete-btn" title="Delete this page">Delete</button>' +
            '    <button class="repair-btn" data-action="replace" data-pdf-page="' + pdfPage + '" ' +
            'title="Ask a scanner with the book to scan this page again">Ask for a rescan</button>' +
            '    <input type="file" class="replace-input" accept="image/*,application/pdf" hidden>' +
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
                    ' (a range like 913-925 if this page carries several ' +
                    'book pages; leave blank if it has no number):',
                    current
                );
                if (num === null) return; // cancelled
                var trimmed = num.trim();
                if (trimmed && !isPageNumberEntry(trimmed)) {
                    alert(
                        'Page number must be a positive whole number, a ' +
                        'range like 913-925, or blank for none.'
                    );
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
                        // The server normalizes a range to one hyphen.
                        ocr.type = res.data.detected.indexOf('-') === -1
                            ? 'single' : 'range';
                        var tag = ocr.type === 'range' ? RANGE_TAG : '#';
                        editBtn.className = 'ocr-tag editable-page';
                        editBtn.innerHTML = tag + escapeHtml(res.data.detected) + ' <small>(manual)</small>';
                    } else {
                        ocr.type = null;
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

        // A page a curator already replaced carries its note before the
        // deletion mark is drawn: markPageAsDeleted saves the label it
        // finds, and the undo puts that saved label back.
        var replaced = (SCAN_CONFIG.replacedPages || {})[String(pdfPage)];
        if (replaced) {
            markPageAsReplaced(div, pdfPage, replaced);
        }
        drawRepairNote(div, findRepair('replace', pdfPage));

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

    // --- Replace a page (issue #232) ---
    // A blurry or unreadable page gets an image or a PDF in its place.
    // The row is a saved decision: nothing builds it into the volume
    // until the apply of #206, which is why the note says so.
    //
    // Both handlers are bound on the container and not on the buttons.
    // markPageAsDeleted (shared.js) writes over the whole page-label,
    // and its undo writes the saved label back and re-binds the delete
    // button alone -- a listener bound to the Replace button would not
    // survive that round trip.
    container.addEventListener('click', function (e) {
        var replaceBtn = e.target.closest('.replace-btn');
        if (replaceBtn) {
            var pageDiv = replaceBtn.closest('.page-container');
            var input = pageDiv && pageDiv.querySelector('.replace-input');
            if (input) { input.click(); }
            return;
        }
        var undoBtn = e.target.closest('.undo-replace-btn');
        if (undoBtn) {
            e.stopPropagation();
            undoPageReplacement(parseInt(undoBtn.dataset.pdfPage, 10),
                                undoBtn.closest('.page-container'));
        }
    });

    container.addEventListener('change', function (e) {
        var input = e.target.closest('.replace-input');
        if (!input || !input.files || !input.files.length) { return; }
        var pageDiv = input.closest('.page-container');
        var btn = pageDiv.querySelector('.replace-btn');
        var pdfPage = parseInt(btn.dataset.pdfPage, 10);
        uploadPageReplacement(pdfPage, input.files[0], pageDiv);
        input.value = '';
    });

    function uploadPageReplacement(pdfPage, file, pageDiv) {
        var formData = new FormData();
        formData.append('pdf_page', pdfPage);
        formData.append('image', file);
        fetch('/scans/' + documentId + '/replace-page/', {
            method: 'POST',
            headers: { 'X-CSRFToken': csrfToken },
            body: formData,
        })
        .then(function (r) {
            return r.json().then(function (d) { return { ok: r.ok, data: d }; });
        })
        .then(function (res) {
            if (!res.ok || res.data.status !== 'ok') {
                showToast((res.data && res.data.error) ||
                          'Could not replace this page.');
                return;
            }
            markPageAsReplaced(pageDiv, pdfPage, {
                url: res.data.file_url,
                kind: res.data.kind,
            });
            markSidebarReplaced(pdfPage, true);
            if (typeof window.refreshProcessActionBar === 'function') {
                window.refreshProcessActionBar();
            }
            window.onPageEditSaved();
        })
        .catch(function () {
            showToast('Could not replace this page. Try again.');
        });
    }

    // The note that says this page stands in for the scanned one. It
    // goes on the label itself and not inside the label's first span,
    // which carries the OCR tag the page-number editor rewrites.
    function markPageAsReplaced(pageDiv, pdfPage, replaced) {
        var label = pageDiv.querySelector('.page-label');
        if (!label) { return; }
        var old = label.querySelector('.replaced-note');
        if (old) { old.remove(); }
        var note = document.createElement('span');
        note.className = 'replaced-note';
        note.innerHTML =
            'This page has been replaced. ' +
            '<a href="' + escapeHtml(replaced.url) + '" target="_blank" rel="noopener">View</a>' +
            ' <button class="undo-replace-btn" data-pdf-page="' + pdfPage + '" ' +
            'title="Take this replacement back">Undo</button>';
        label.appendChild(note);
        refreshSavedLabel(label);
    }

    // markPageAsDeleted (shared.js) saves the label it finds in
    // originalHtml, and its undo writes that copy back. A note added
    // or removed on a live page must reach the copy too, or the undo
    // of a later deletion drops it. While the page is marked for
    // deletion the label *is* the deletion mark, and saving that would
    // make the undo restore the mark itself: so the copy is refreshed
    // on a live page only.
    function refreshSavedLabel(label) {
        if (!label || !label.dataset.originalHtml) { return; }
        if (label.querySelector('.undo-delete-btn')) { return; }
        label.dataset.originalHtml = label.innerHTML;
    }

    function undoPageReplacement(pdfPage, pageDiv) {
        if (!confirm('Take this replacement back?')) { return; }
        fetch('/scans/' + documentId + '/replace-page/undo/', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
            body: JSON.stringify({ pdf_page: pdfPage }),
        })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (data.status !== 'ok') {
                showToast(data.error || 'Could not take the replacement back.');
                return;
            }
            var note = pageDiv.querySelector('.replaced-note');
            if (note) { note.remove(); }
            refreshSavedLabel(pageDiv.querySelector('.page-label'));
            markSidebarReplaced(pdfPage, false);
            if (typeof window.refreshProcessActionBar === 'function') {
                window.refreshProcessActionBar();
            }
            window.onPageEditSaved();
        })
        .catch(function () {
            showToast('Could not take the replacement back. Try again.');
        });
    }

    // The sidebar row of one page carries the same warning as a badge,
    // beside the DUP one the server renders.
    function markSidebarReplaced(pdfPage, on) {
        var row = document.querySelector(
            '#pages-list [data-pdf-index="' + (pdfPage - 1) + '"]'
        );
        if (!row) { return; }
        var badge = row.querySelector('.page-repl-badge');
        if (on && !badge) {
            badge = document.createElement('span');
            badge.className =
                'page-repl-badge text-[9px] font-bold text-purple-600 ' +
                'dark:text-purple-400 ml-1';
            badge.title = 'A curator replaced this page';
            badge.textContent = 'REPL';
            var holder = row.querySelector('span') || row;
            holder.appendChild(badge);
        } else if (!on && badge) {
            badge.remove();
        }
    }

    // --- Ask a scanner for a page (issue #249) ---
    // A reviewer with no book cannot replace a blurry page or fill a
    // gap. The request is a row a scanner finds on the Repairs page and
    // on this page. It is fulfilled by the upload at the same address
    // (the server derives that), and dismissed by any user, never
    // deleted. The rows live in repairRequests, and every drawing reads
    // that one list: the note on the page, the sidebar badge, the
    // sidebar section and the header badge.
    var repairRequests = (SCAN_CONFIG.repairRequests || []).slice();

    function findRepair(action, address) {
        for (var i = 0; i < repairRequests.length; i++) {
            var r = repairRequests[i];
            if (r.action !== action) { continue; }
            var at = action === 'insert' ? r.anchor_pdf_page : r.pdf_page;
            if (String(at) === String(address)) { return r; }
        }
        return null;
    }

    function replaceRepair(row) {
        repairRequests = repairRequests.filter(function (r) { return r.id !== row.id; });
        repairRequests.push(row);
    }

    function dropRepair(id) {
        repairRequests = repairRequests.filter(function (r) { return r.id !== id; });
    }

    // The buttons and the Dismiss are bound on the container, like the
    // Replace button: markPageAsDeleted writes over the whole label.
    container.addEventListener('click', function (e) {
        var askBtn = e.target.closest('.repair-btn');
        if (askBtn) {
            e.preventDefault();
            askForRepair(askBtn, askBtn.closest('.page-container'));
            return;
        }
        var dismissBtn = e.target.closest('.dismiss-repair-btn');
        if (dismissBtn) {
            e.stopPropagation();
            dismissRepair(parseInt(dismissBtn.dataset.requestId, 10),
                          dismissBtn.closest('.page-container'));
        }
    });

    function askForRepair(btn, pageDiv) {
        var action = btn.dataset.action;
        var what = btn.dataset.repairWhat ||
            (action === 'insert' ? 'scan this missing page' : 'scan this page again');
        var note = prompt('Ask a scanner to ' + what + '.\nWhat did you see? (optional)', '');
        if (note === null) { return; }
        var body = { action: action, note: note.trim() };
        if (action === 'insert') {
            body.anchor_pdf_page = parseInt(btn.dataset.anchorPdfPage, 10);
            body.logical_page = btn.dataset.logicalPage || '';
        } else {
            // The server reads the printed number off its own copy of
            // the OCR results; a label sent from here would be ignored.
            body.pdf_page = parseInt(btn.dataset.pdfPage, 10);
        }
        fetch('/scans/' + documentId + '/repair/request/', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
            body: JSON.stringify(body),
        })
        .then(function (r) {
            return r.json().then(function (d) { return { ok: r.ok, data: d }; });
        })
        .then(function (res) {
            if (!res.ok || res.data.status !== 'ok') {
                showToast((res.data && res.data.error) || 'Could not save the request.');
                return;
            }
            replaceRepair(res.data.request);
            drawRepairNote(pageDiv, res.data.request);
            renderRepairsSection();
            if (res.data.already_fulfilled) {
                // The open row is answered, and the key matched it. Say
                // the way out; a "saved" toast here would lose the ask.
                showToast(res.data.message);
                return;
            }
            showToast(res.data.created
                ? 'Saved. A scanner sees this on the Repairs page.'
                : 'This page was already requested.', 'success');
        })
        .catch(function () {
            showToast('Could not save the request. Try again.');
        });
    }

    function dismissRepair(requestId, pageDiv) {
        if (!confirm('Dismiss this request? The row is kept, and the page is not scanned again.')) { return; }
        fetch('/scans/' + documentId + '/repair/dismiss/', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
            body: JSON.stringify({ request_id: requestId }),
        })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (data.status !== 'ok') {
                showToast(data.error || 'Could not dismiss the request.');
                return;
            }
            dropRepair(requestId);
            drawRepairNote(pageDiv, null);
            renderRepairsSection();
            showToast('Dismissed.', 'success');
        })
        .catch(function () {
            showToast('Could not dismiss the request. Try again.');
        });
    }

    // The note on the page. On a PDF page it goes on the label, like
    // the replaced note; on a missing placeholder it goes above the
    // upload form. A null row removes the note.
    function drawRepairNote(pageDiv, row) {
        if (!pageDiv) { return; }
        var label = pageDiv.querySelector('.page-label');
        var host = pageDiv.classList.contains('missing-page')
            ? pageDiv.querySelector('.missing-placeholder') : label;
        if (!host) { return; }
        var old = host.querySelector('.repair-note');
        if (old) { old.remove(); }
        var askBtn = pageDiv.querySelector('.repair-btn');
        if (askBtn) { askBtn.hidden = !!row; }
        if (row) {
            var note = document.createElement('span');
            note.className = 'repair-note' + (row.fulfilled ? ' fulfilled' : '');
            var pages = pageDiv.dataset.missingRange ? 'these pages' : 'this page';
            var text = row.fulfilled
                ? 'A scanner was asked for ' + pages + ', and a new scan is saved. '
                : (row.action === 'insert' ? 'A scanner was asked for ' + pages + '. '
                                           : 'A scanner was asked to scan this page again. ');
            // A fulfilled request keeps its Dismiss: the row is still
            // open and holds the address, so a reviewer who finds the
            // new scan bad too dismisses it and asks again.
            var dismissTitle = row.fulfilled
                ? 'Close this answered request. Then you can ask again.'
                : 'The page is fine, or the request no longer applies';
            note.innerHTML =
                escapeHtml(text) +
                '<span class="repair-note-text">' +
                (row.note ? escapeHtml(row.note) + ' ' : '') +
                '(' + escapeHtml(row.requested_by) + ', ' + escapeHtml(row.date_created) + ')' +
                (row.stale ? ', made on an earlier upload of this scan' : '') +
                '</span>' +
                (row.fulfilled ? ' Still bad? Dismiss this request and ask again.' : '') +
                ' <button class="dismiss-repair-btn" data-request-id="' + row.id + '" ' +
                'title="' + dismissTitle + '">Dismiss</button>';
            if (host === label) { label.appendChild(note); }
            else { host.insertBefore(note, host.firstChild); }
        }
        if (host === label) { refreshSavedLabel(label); }
        if (row && row.action === 'replace') { markSidebarNeed(row.pdf_page, !row.fulfilled); }
        if (!row && pageDiv.dataset.pdfIndex !== undefined) {
            markSidebarNeed(parseInt(pageDiv.dataset.pdfIndex, 10) + 1, false);
        }
    }

    function markSidebarNeed(pdfPage, on) {
        var sidebarRow = document.querySelector(
            '#pages-list [data-pdf-index="' + (pdfPage - 1) + '"]'
        );
        if (!sidebarRow) { return; }
        var badge = sidebarRow.querySelector('.page-need-badge');
        if (on && !badge) {
            badge = document.createElement('span');
            badge.className =
                'page-need-badge text-[9px] font-bold text-orange-600 ' +
                'dark:text-orange-400 ml-1';
            badge.title = 'A reviewer asked a scanner to scan this page again';
            badge.textContent = 'NEED';
            var holder = sidebarRow.querySelector('span') || sidebarRow;
            holder.appendChild(badge);
        } else if (!on && badge) {
            badge.remove();
        }
    }

    // The sidebar section and the header badge, redrawn whole from the
    // list, so they cannot disagree with the notes on the pages.
    function renderRepairsSection() {
        var waiting = repairRequests.filter(function (r) { return !r.fulfilled; });
        var section = document.getElementById('repairs-section');
        var list = document.getElementById('repairs-list');
        var badge = document.getElementById('repairs-badge');
        document.querySelectorAll('.repairs-count').forEach(function (el) {
            el.textContent = waiting.length;
        });
        if (badge) { badge.hidden = waiting.length === 0; }
        if (!section || !list) { return; }
        section.hidden = waiting.length === 0;
        list.innerHTML = waiting.map(function (r) {
            var where = r.action === 'insert'
                ? 'after PDF p.' + r.anchor_pdf_page : 'PDF p.' + r.pdf_page;
            return '<div class="repair-card rounded border border-orange-300 bg-orange-50 ' +
                'hover:bg-orange-100 dark:border-orange-700 dark:bg-orange-900/20 ' +
                'dark:hover:bg-orange-900/40 px-2 py-1.5 mb-1 cursor-pointer text-xs" ' +
                'data-request-id="' + r.id + '" data-pdf-index="' + r.nav_pdf_index + '" ' +
                'onclick="goToPage(this)">' +
                '<div class="flex items-center gap-1.5">' +
                '<span class="font-bold uppercase text-[10px]">' + escapeHtml(r.action_label) + '</span>' +
                '<span class="text-gray-500 dark:text-gray-400">' + escapeHtml(where) +
                (r.logical_page ? ' (#' + escapeHtml(r.logical_page) + ')' : '') + '</span>' +
                (r.stale ? '<span class="text-[9px] font-bold text-gray-500">EARLIER UPLOAD</span>' : '') +
                '</div>' +
                (r.note ? '<p class="mt-0.5 text-gray-700 dark:text-gray-300">' + escapeHtml(r.note) + '</p>' : '') +
                '<p class="mt-0.5 text-[10px] text-gray-500 dark:text-gray-400">' +
                escapeHtml(r.requested_by) + ', ' + escapeHtml(r.date_created) + '</p>' +
                '</div>';
        }).join('');
    }

    // The Repairs page links to one page of step 1 (?goto=<pdf_index>).
    // The placeholders exist as soon as the document is shown, so the
    // scroll works before the page itself is rendered.
    function goToRequestedPage() {
        var params = new URLSearchParams(window.location.search);
        var index = params.get('goto');
        if (index === null || !/^\d+$/.test(index)) { return; }
        var el = container.querySelector('.lazy-page[data-pdf-index="' + index + '"]');
        if (el && typeof goToPage === 'function') {
            setTimeout(function () { goToPage(el); }, 150);
        }
    }

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
                    placeholder.innerHTML = '<p>' + escapeHtml(data.error || 'Upload failed. Try again.') + '</p>';
                }
                return;
            }
            pageDiv.className = 'page-container inserted-page';
            pageDiv.style.width = '';
            pageDiv.innerHTML = insertedPageHtml(pageNumber, data.image_url, data.edit_id, false, data.kind, data.file_url);
            bindRemoveInsert(pageDiv, data.edit_id);
            drawInsertedPdf(pageDiv);
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
