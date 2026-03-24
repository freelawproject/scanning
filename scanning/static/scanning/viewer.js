document.addEventListener('DOMContentLoaded', function () {
    const container = document.getElementById('pdf-viewer');
    if (!container) return;

    const pdfUrl = container.dataset.pdfUrl;
    const pageMap = JSON.parse(container.dataset.pageMap || '[]');
    const flaggedPages = JSON.parse(container.dataset.flaggedPages || '[]');
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

    pdfjsLib.getDocument(pdfUrl).promise.then(function (pdf) {
        pdfDoc = pdf;
        container.innerHTML = '';
        createAllPlaceholders();
        setupLazyLoading();
    }).catch(function (err) {
        container.innerHTML = '<div class="viewer-loading">Error loading PDF: ' + err.message + '</div>';
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
        var isFlagged = flaggedPages.indexOf(entry.logical_number) !== -1;
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

    // --- Render a single PDF page into its placeholder ---
    function renderPdfPage(pageDiv, pdfIndex, logicalNumber) {
        pdfDoc.getPage(pdfIndex + 1).then(function (page) {
            var viewport = page.getViewport({ scale: SCALE });
            var origViewport = page.getViewport({ scale: 1 });

            var canvas = pageDiv.querySelector('.pdf-canvas');
            canvas.width = viewport.width;
            canvas.height = viewport.height;

            var wrapper = pageDiv.querySelector('.canvas-wrapper');
            wrapper.style.width = viewport.width + 'px';
            wrapper.style.height = viewport.height + 'px';
            wrapper.style.background = '';

            // Update default width on first render
            if (defaultPageWidth === 918 && viewport.width !== 918) {
                defaultPageWidth = viewport.width;
                updatePlaceholderWidths();
            }

            page.render({
                canvasContext: canvas.getContext('2d'),
                viewport: viewport,
            });

            // Setup redaction overlay
            var overlay = pageDiv.querySelector('.redaction-overlay');
            overlay.width = viewport.width;
            overlay.height = viewport.height;

            var pageRedactions = redactions[String(logicalNumber)] || [];
            drawExistingRedactions(overlay, pageRedactions, SCALE);
            rebuildRedactionDivs(pageDiv, logicalNumber);

            pageDiv.dataset.scale = SCALE;
            pageDiv.dataset.pdfWidth = origViewport.width;
            pageDiv.dataset.pdfHeight = origViewport.height;
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
                uploadPageInsert(entry.logical_number, fileInput.files[0], pageDiv);
            }
        });
    }

    function renderInsertedPage(entry) {
        var pageDiv = document.createElement('div');
        pageDiv.className = 'page-container inserted-page';
        pageDiv.id = 'page-' + entry.logical_number;
        pageDiv.innerHTML =
            '<div class="page-label">Page ' + entry.logical_number + ' &mdash; INSERTED</div>' +
            '<div class="canvas-wrapper">' +
            '  <img src="' + entry.insert_url + '" class="inserted-image">' +
            '</div>';
        container.appendChild(pageDiv);
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
            '    <button class="redact-btn" data-fill="black" title="Draw a black redaction">Redact</button>' +
            '    <button class="whiteout-btn" data-fill="white" title="Draw a white redaction">Whiteout</button>' +
            '    <button class="delete-btn" title="Delete this page">Delete</button>' +
            '  </span>' +
            '</div>' +
            '<div class="canvas-wrapper">' +
            '  <canvas class="pdf-canvas"></canvas>' +
            '  <canvas class="redaction-overlay"></canvas>' +
            '</div>';

        var redactBtn = div.querySelector('.redact-btn');
        redactBtn.addEventListener('click', function () {
            toggleRedactionMode(div, logicalNumber, 'black');
        });

        var whiteoutBtn = div.querySelector('.whiteout-btn');
        whiteoutBtn.addEventListener('click', function () {
            toggleRedactionMode(div, logicalNumber, 'white');
        });

        var editBtn = div.querySelector('.editable-page');
        if (editBtn) {
            editBtn.addEventListener('click', function () {
                var current = ocr && ocr.detected ? ocr.detected : '';
                var num = prompt('Enter the correct page number for PDF page ' + pdfPage + ':', current);
                if (num !== null && num.trim()) {
                    fetch('/document/' + documentId + '/assign-page/', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'X-CSRFToken': csrfToken,
                        },
                        body: JSON.stringify({ pdf_page: pdfPage, page_number: num.trim() }),
                    })
                    .then(function (r) { return r.json(); })
                    .then(function (data) {
                        if (data.status === 'ok') {
                            editBtn.className = 'ocr-tag editable-page';
                            editBtn.innerHTML = '#' + num.trim() + ' <small>(manual)</small>';
                        }
                    });
                }
            });
        }

        var deleteBtn = div.querySelector('.delete-btn');
        deleteBtn.addEventListener('click', function () {
            if (confirm('Delete PDF page ' + pdfPage + '?')) {
                deletePage(pdfPage, div);
            }
        });

        return div;
    }

    // --- Delete page ---
    function deletePage(pdfPage, pageDiv) {
        fetch('/document/' + documentId + '/delete-page/', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': csrfToken,
            },
            body: JSON.stringify({ pdf_page: pdfPage }),
        })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (data.status === 'ok') {
                pageDiv.style.opacity = '0.3';
                pageDiv.style.pointerEvents = 'none';
                var label = pageDiv.querySelector('.page-label');
                label.innerHTML = '<span>PDF p.' + pdfPage + ' &mdash; MARKED FOR DELETION</span>';
                resolveIssuesForPage(pdfPage);
            }
        });
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
        overlay.onmousemove = function (e) { onRedactMove(e, overlay, pageNumber); };
        overlay.onmouseup = function (e) { onRedactEnd(e, overlay, pageDiv, pageNumber); };
    }

    function onRedactStart(e, overlay, pageDiv, pageNumber) {
        isDrawing = true;
        var rect = overlay.getBoundingClientRect();
        startX = e.clientX - rect.left;
        startY = e.clientY - rect.top;
    }

    function onRedactMove(e, overlay, pageNumber) {
        if (!isDrawing) return;
        var rect = overlay.getBoundingClientRect();
        var curX = e.clientX - rect.left;
        var curY = e.clientY - rect.top;

        var ctx = overlay.getContext('2d');
        ctx.clearRect(0, 0, overlay.width, overlay.height);

        var pageRedactions = redactions[String(pageNumber)] || [];
        drawExistingRedactions(overlay, pageRedactions, SCALE);

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

        var rect = overlay.getBoundingClientRect();
        var endX = e.clientX - rect.left;
        var endY = e.clientY - rect.top;

        var scale = parseFloat(pageDiv.dataset.scale) || SCALE;

        var pdfX = Math.min(startX, endX) / scale;
        var pdfY = Math.min(startY, endY) / scale;
        var pdfW = Math.abs(endX - startX) / scale;
        var pdfH = Math.abs(endY - startY) / scale;

        if (pdfW < 5 || pdfH < 5) {
            var ctx = overlay.getContext('2d');
            ctx.clearRect(0, 0, overlay.width, overlay.height);
            drawExistingRedactions(overlay, redactions[String(pageNumber)] || [], SCALE);
            return;
        }

        var fill = activeRedactionFill;
        fetch('/document/' + documentId + '/redaction/add/', {
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
            drawExistingRedactions(overlay, redactions[String(pageNumber)], SCALE);
            rebuildRedactionDivs(pageDiv, pageNumber);
        });
    }

    function drawExistingRedactions(overlay, pageRedactions, scale) {
        var ctx = overlay.getContext('2d');
        pageRedactions.forEach(function (r) {
            if (r.fill === 'white') {
                ctx.fillStyle = 'rgba(255, 255, 255, 0.9)';
                ctx.fillRect(r.x * scale, r.y * scale, r.width * scale, r.height * scale);
                ctx.strokeStyle = '#3b82f6';
                ctx.lineWidth = 1;
                ctx.setLineDash([4, 3]);
                ctx.strokeRect(r.x * scale, r.y * scale, r.width * scale, r.height * scale);
                ctx.setLineDash([]);
            } else {
                ctx.fillStyle = 'rgba(0, 0, 0, 0.85)';
                ctx.fillRect(r.x * scale, r.y * scale, r.width * scale, r.height * scale);
                ctx.strokeStyle = '#ef4444';
                ctx.lineWidth = 1;
                ctx.strokeRect(r.x * scale, r.y * scale, r.width * scale, r.height * scale);
            }
        });
    }

    // Build clickable × buttons over each redaction so users can remove them
    function rebuildRedactionDivs(pageDiv, pageNumber) {
        var wrapper = pageDiv.querySelector('.canvas-wrapper');
        // Remove old buttons
        wrapper.querySelectorAll('.redaction-delete-btn').forEach(function (el) { el.remove(); });

        var pageRedactions = redactions[String(pageNumber)] || [];
        pageRedactions.forEach(function (r, idx) {
            var btn = document.createElement('button');
            btn.className = 'redaction-delete-btn';
            btn.title = 'Remove this ' + (r.fill === 'white' ? 'whiteout' : 'redaction');
            btn.textContent = '\u00d7';
            btn.style.left = ((r.x + r.width) * SCALE - 18) + 'px';
            btn.style.top = (r.y * SCALE + 2) + 'px';
            btn.addEventListener('click', function (e) {
                e.stopPropagation();
                fetch('/document/' + documentId + '/redaction/' + r.id + '/delete/', {
                    method: 'POST',
                    headers: { 'X-CSRFToken': csrfToken },
                }).then(function (resp) { return resp.json(); })
                .then(function (data) {
                    if (data.status === 'ok') {
                        redactions[String(pageNumber)].splice(idx, 1);
                        var overlay = pageDiv.querySelector('.redaction-overlay');
                        var ctx = overlay.getContext('2d');
                        ctx.clearRect(0, 0, overlay.width, overlay.height);
                        drawExistingRedactions(overlay, redactions[String(pageNumber)], SCALE);
                        rebuildRedactionDivs(pageDiv, pageNumber);
                    }
                });
            });
            wrapper.appendChild(btn);
        });
    }

    // --- Page insert upload ---
    function uploadPageInsert(pageNumber, file, pageDiv) {
        var formData = new FormData();
        formData.append('page_number', pageNumber);
        formData.append('image', file);

        var placeholder = pageDiv.querySelector('.missing-placeholder');
        if (placeholder) placeholder.innerHTML = '<p>Uploading...</p>';

        fetch('/document/' + documentId + '/insert/', {
            method: 'POST',
            headers: { 'X-CSRFToken': csrfToken },
            body: formData,
        })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            pageDiv.className = 'page-container inserted-page';
            pageDiv.style.width = '';
            pageDiv.innerHTML =
                '<div class="page-label">Page ' + pageNumber + ' &mdash; INSERTED</div>' +
                '<div class="canvas-wrapper">' +
                '  <img src="' + data.image_url + '" class="inserted-image">' +
                '</div>';
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
                fetch('/document/' + documentId + '/delete-page/', {
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
    window.goToPage = function (pageNumber) {
        // Issues pass logical page numbers; PDF containers store data-logical-number.
        // OCR panel passes pdf_page (= pdf_index+1) so the ID fallback covers that case.
        var el = container.querySelector('[data-logical-number="' + pageNumber + '"]')
                 || document.getElementById('page-' + pageNumber);
        if (el) {
            el.scrollIntoView({ behavior: 'smooth', block: 'start' });
            el.classList.add('highlight');
            setTimeout(function () { el.classList.remove('highlight'); }, 2000);
        }
    };

    // --- Dismiss an issue ---
    window.dismissIssue = function (btn, issueId) {
        fetch('/document/' + documentId + '/dismiss-issue/', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': csrfToken,
            },
            body: JSON.stringify({ issue_id: issueId }),
        })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (data.status === 'ok') {
                var card = btn.closest('.issue-card');
                card.style.opacity = '0.3';
                card.style.pointerEvents = 'none';
                // Check if all issues are dismissed
                var remaining = document.querySelectorAll('.issue-card:not([style*="opacity"])');
                if (remaining.length === 0) {
                    var issuesPanel = document.querySelector('.issues-panel');
                    var allClear = document.querySelector('.all-clear');
                    if (!allClear) {
                        var div = document.createElement('div');
                        div.className = 'all-clear';
                        div.textContent = 'All Clear — no issues found';
                        issuesPanel.appendChild(div);
                    }
                }
            }
        });
    };

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
            return fetch('/document/' + documentId + '/delete-page/', {
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
