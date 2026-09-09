/**
 * The text overlay of the process viewer (issue #262).
 *
 * A reviewer of review 1 finds a page with bleedthrough or a blurry
 * page, and cannot tell what it cost the reading. This module draws
 * the text dots.mocr read on the page itself: one box per cell of the
 * layout JSON, at the cell's own position.
 *
 * Three rules the issue asks for, and where each one lives:
 *
 * - **The data loads one time.** The first enable asks
 *   ``scan_ocr_text_url`` for a presigned GET, reads the document
 *   straight from the bucket, and keeps one index of it. A later
 *   enable paints from that index: only the DOM nodes go on a disable.
 * - **The viewport only.** Each viewer calls ``ocrTextPaint`` at the
 *   end of a page render, and a page renders only when the lazy
 *   observer brings it near the viewport. So the zoom re-render and
 *   every jump to a page follow with no other hook.
 * - **Nothing draws by itself.** The reviewer presses the button.
 *
 * The cells are in the render space of the page at 200 dpi, so a box
 * scales by ``canvas.width / page.width`` -- the rule the detection
 * boxes of step 2 follow. The text is the model's, so it enters the
 * DOM with ``textContent``.
 *
 * **A box shows its text under the pointer, and not before.** The
 * reviewer came to judge a blurry page against the ink, so a panel
 * drawn over every cell hid the page they came to read. No box takes
 * the pointer either -- a CSS ``:hover`` would need one, and the box
 * would then swallow the clicks of the detection boxes and of the
 * redaction drag, which sit at the same layer -- so one hit test on
 * the wrapper opens the box under the pointer.
 *
 * Both viewers load this file, because one rule that drifted between
 * two copies would draw the text differently in the two steps.
 *
 * The read of a glued volume takes seconds, and a toolbar button gives
 * no signal of its own: a disabled one looks exactly like a live one.
 * So the wait says so three ways -- the label, a dim, and a pulse --
 * and a toast names the size, which is why the wait is long.
 */

(function () {
    'use strict';

    // One entry per page of the space the viewer draws, keyed by the
    // 1-based page. Null until the first load; kept for the life of
    // the page after it.
    var index = null;
    var loading = false;
    var enabled = false;

    // The box under the pointer, so a move closes the one before it.
    var openBox = null;

    // The border colour of a box, by the category dots.mocr gives the
    // cell. Everything the layout model does not name is grey.
    var CATEGORY_COLORS = {
        'Page-header': '#2563eb',
        'Page-footer': '#2563eb',
        'Title': '#7c3aed',
        'Section-header': '#7c3aed',
        'Text': '#059669',
        'List-item': '#059669',
        'Caption': '#d97706',
        'Footnote': '#d97706',
        'Table': '#db2777',
        'Formula': '#db2777',
        'Picture': '#6b7280',
    };

    function config() {
        return typeof SCAN_CONFIG !== 'undefined' ? SCAN_CONFIG : {};
    }

    /**
     * Whether the viewer draws the pages the document describes.
     *
     * Step 2 can load one opinion's PDF in place of the volume, and
     * its own overlays stand down there. It says so on the container.
     *
     * @returns {boolean} False while another document is loaded.
     */
    function drawsTheVolume() {
        var container = document.getElementById('pdf-viewer');
        return !container || container.dataset.ocrText !== 'off';
    }

    /**
     * Bind the toolbar button. Safe to call on a page with no button:
     * the view renders one only when a document exists.
     */
    window.ocrTextInit = function () {
        var button = document.getElementById('ocr-text-toggle');
        if (!button) return;
        button.addEventListener('click', function () {
            if (loading) return;
            if (enabled) {
                setEnabled(button, false);
            } else if (index) {
                setEnabled(button, true);
            } else {
                load(button);
            }
        });
    };

    /**
     * Draw one page's overlay, if the overlay is on and the page was
     * read. Called at the end of a page render by both viewers.
     *
     * @param {HTMLElement} pageDiv - The .page-container element.
     * @param {number} pdfIndex - The 0-based page of the drawn space.
     */
    window.ocrTextPaint = function (pageDiv, pdfIndex) {
        if (!enabled || !index || !pageDiv || !drawsTheVolume()) return;
        var page = index[pdfIndex + 1];
        if (!page) return;
        window.ocrTextClear(pageDiv);
        paintPage(pageDiv, page);
    };

    /**
     * Remove one page's overlay. A discarded page keeps no node: the
     * boxes hold the scale of the render they were drawn for.
     *
     * The hover listeners go with the boxes. They hold the rects of
     * that render, so a pair left behind would open a box of the old
     * scale, and a re-render would stack a second pair.
     *
     * @param {HTMLElement} pageDiv - The .page-container element.
     */
    window.ocrTextClear = function (pageDiv) {
        if (!pageDiv) return;
        var wrapper = pageDiv.querySelector('.canvas-wrapper');
        if (wrapper && wrapper._ocrTextHover) {
            wrapper._ocrTextHover();
            wrapper._ocrTextHover = null;
        }
        pageDiv.querySelectorAll('.ocr-cell, .ocr-page-note').forEach(
            function (el) { el.remove(); }
        );
    };

    // --- The load, in two fetches ---

    function load(button) {
        var cfg = config();
        if (!cfg.ocrTextUrlApi) return;
        loading = true;
        var label = button.textContent;
        startWaiting(button, 'Reading…');
        var api = cfg.ocrTextUrlApi + (cfg.finalSpace ? '?space=final' : '');
        var documentUrl = null;
        fetch(api)
            .then(function (r) {
                return r.json().then(function (d) {
                    if (!r.ok) throw new Error(d.error || 'no OCR text');
                    return d;
                });
            })
            .then(function (answer) {
                documentUrl = answer.url;
                if (answer.size) {
                    // The second read is the slow one, and its size is
                    // why. The number goes in a toast and not on the
                    // button: a label that grows moves every other
                    // button of the toolbar on each press.
                    var mb = Math.max(1, Math.round(answer.size / 1048576));
                    button.title = 'Reading the OCR text of this volume, ' +
                        mb + ' MB.';
                    if (typeof showToast === 'function') {
                        showToast('Reading the OCR text of this volume, ' +
                            mb + ' MB. This takes a moment.', 'info');
                    }
                }
                // Straight from the bucket: the web pod reads no byte
                // of a document that holds every cell of the volume.
                return fetch(documentUrl);
            })
            .then(function (r) {
                if (!r.ok) throw new Error('the bucket answered ' + r.status);
                return r.json();
            })
            .then(function (doc) {
                index = buildIndex(doc);
                stopWaiting(button, label);
                setEnabled(button, true);
            })
            .catch(function (err) {
                stopWaiting(button, label);
                failed(err, documentUrl);
            });
    }

    /**
     * Show that the button is at work.
     *
     * The read of a glued volume takes seconds, and the label alone
     * carried the whole signal: a ``disabled`` button of the zoom
     * toolbar looks exactly like a live one. So this writes words a
     * reviewer can read, dims the button and starts the pulse of
     * ``.loading``.
     *
     * @param {HTMLElement} button - The toolbar button.
     * @param {string} text - What the button says while it waits.
     */
    function startWaiting(button, text) {
        button.disabled = true;
        button.classList.add('loading');
        button.textContent = text;
        button.title = 'Reading the OCR text of this volume…';
    }

    /**
     * Give the button back, whatever the read did.
     *
     * @param {HTMLElement} button - The toolbar button.
     * @param {string} label - The label the button carried before.
     */
    function stopWaiting(button, label) {
        loading = false;
        button.disabled = false;
        button.classList.remove('loading');
        button.textContent = label;
        button.title = 'Show the text the OCR read on each page';
    }

    /**
     * Say that the read failed, and leave the button ready.
     *
     * A read the bucket refuses reaches the page as an opaque network
     * error, so the message names what the browser gave us and the
     * console carries the URL, which a signature makes too long for a
     * toast. Another press mints a new signature and tries again,
     * which is the way out of an expired one.
     *
     * @param {Error} err - What the fetch threw.
     * @param {string|null} documentUrl - The URL, once we had one.
     */
    function failed(err, documentUrl) {
        var reason = err && err.message ? err.message : 'unknown error';
        if (documentUrl) {
            console.error('The OCR text did not load from', documentUrl, err);
        }
        var message = 'The OCR text did not load (' + reason +
            '). Press the button again to try once more.';
        if (typeof showToast === 'function') {
            showToast(message, 'error');
        } else {
            alert(message);
        }
    }

    /**
     * Keep what the overlay draws, and drop the rest of the document.
     *
     * The document holds the ``md`` of every page as well, which is
     * the same text again. It is kept for a page with no cell only:
     * that page is the one the reviewer hunts, and its ``md`` is the
     * only text left for it.
     *
     * @param {Object} doc - The glued document.
     * @returns {Object} ``{1-based page: {width, height, cells, note, md}}``.
     */
    function buildIndex(doc) {
        var out = {};
        var pages = (doc && doc.pages) || [];
        pages.forEach(function (page) {
            var number = page.pdf_page;
            if (!number) return;
            var cells = (page.cells || []).filter(function (cell) {
                return cell && cell.bbox && (cell.text || '').trim();
            }).map(function (cell) {
                return {
                    bbox: cell.bbox,
                    category: cell.category || '',
                    text: String(cell.text).trim(),
                };
            });
            var entry = {
                width: page.origin_width || 0,
                height: page.origin_height || 0,
                cells: cells,
            };
            if (!cells.length) {
                entry.note = page.error ||
                    (page.filtered ? 'the layout of this page did not parse'
                                   : 'the OCR found no text on this page');
                entry.md = (page.md || '').trim();
            }
            out[number] = entry;
        });
        return out;
    }

    // --- The paint ---

    function setEnabled(button, on) {
        enabled = on;
        button.classList.toggle('active', on);
        document.querySelectorAll('#pdf-viewer .page-container').forEach(
            function (pageDiv) {
                if (!on) {
                    window.ocrTextClear(pageDiv);
                    return;
                }
                var pdfIndex = parseInt(pageDiv.dataset.pdfIndex, 10);
                if (isNaN(pdfIndex)) return;
                window.ocrTextPaint(pageDiv, pdfIndex);
            }
        );
    }

    function paintPage(pageDiv, page) {
        var wrapper = pageDiv.querySelector('.canvas-wrapper');
        var canvas = pageDiv.querySelector('.pdf-canvas');
        // A placeholder that is not rasterized yet has no size to
        // scale by. Its render calls us again.
        if (!wrapper || !canvas || !canvas.width) return;

        if (!page.cells.length) {
            wrapper.appendChild(pageNote(page));
            return;
        }
        var sx = canvas.width / (page.width || canvas.width);
        var sy = canvas.height / (page.height || canvas.height);
        // The rects of the hit test, in the wrapper's own pixels. They
        // come from the numbers the boxes were placed by, so the test
        // reads no layout: a ``getBoundingClientRect`` per cell per
        // move would lay the page out again on every mouse event.
        var mid = canvas.width / 2;
        var hits = page.cells.map(function (cell) {
            var box = buildBox(cell, sx, sy, mid);
            wrapper.appendChild(box);
            return {
                box: box,
                left: cell.bbox[0] * sx,
                top: cell.bbox[1] * sy,
                right: cell.bbox[2] * sx,
                bottom: cell.bbox[3] * sy,
            };
        });
        // The smallest box first, so a cell inside another cell wins
        // the pointer. One sort per page, never per move.
        hits.sort(function (a, b) {
            return area(a) - area(b);
        });
        bindHover(wrapper, hits);
    }

    function area(hit) {
        return (hit.right - hit.left) * (hit.bottom - hit.top);
    }

    function buildBox(cell, sx, sy, mid) {
        var box = document.createElement('div');
        box.className = 'ocr-cell';
        // A panel opens at the left edge of its box and is as wide as
        // its words, so a cell in the right half opens it leftwards
        // instead: the page is what the reviewer is looking at, and a
        // panel that leaves the page may leave the scroll box with it.
        if (cell.bbox[0] * sx > mid) box.classList.add('opens-left');
        box.style.left = (cell.bbox[0] * sx) + 'px';
        box.style.top = (cell.bbox[1] * sy) + 'px';
        box.style.width = ((cell.bbox[2] - cell.bbox[0]) * sx) + 'px';
        box.style.height = ((cell.bbox[3] - cell.bbox[1]) * sy) + 'px';
        box.style.borderColor = CATEGORY_COLORS[cell.category] || '#6b7280';
        var panel = document.createElement('div');
        panel.className = 'ocr-cell-text';
        if (cell.category) {
            // The border colour says the category and a colour alone
            // names nothing, so the panel says it in words. It used to
            // ride on the box's ``title``, which a box that takes no
            // pointer never shows.
            var kind = document.createElement('span');
            kind.className = 'ocr-cell-cat';
            kind.textContent = cell.category;
            panel.appendChild(kind);
        }
        var words = document.createElement('span');
        // The model wrote this text: textContent, never innerHTML.
        words.textContent = cell.text;
        panel.appendChild(words);
        box.appendChild(panel);
        return box;
    }

    // --- The hover, by hit test ---

    /**
     * Open the box under the pointer while the pointer is on the page.
     *
     * The listener is on the wrapper and the boxes take no pointer, so
     * a click still reaches the canvas, the detection boxes and the
     * redaction drag under it. One listener per page, removed with the
     * boxes by ``ocrTextClear``.
     *
     * The move only records the position; the read of it happens in
     * one animation frame, because a mouse move fires far more often
     * than a browser paints.
     *
     * The pointer is in screen pixels and the rects are in the page's
     * own, which the zoom scales apart with a CSS transform on this
     * wrapper. ``shared.eventToCanvasPixels`` is that conversion, and
     * the redaction drag reads a click through it, so the overlay must
     * not carry a second copy of the rule.
     *
     * @param {HTMLElement} wrapper - The .canvas-wrapper of the page.
     * @param {Array} hits - The cell rects, smallest first.
     */
    function bindHover(wrapper, hits) {
        var pending = null;
        var frame = 0;

        function read() {
            frame = 0;
            if (!pending) return;
            setOpen(hitAt(hits, pointIn(wrapper, pending)));
        }

        function onMove(event) {
            pending = event;
            if (!frame) frame = requestAnimationFrame(read);
        }

        function onLeave() {
            pending = null;
            setOpen(null);
        }

        wrapper.addEventListener('mousemove', onMove);
        wrapper.addEventListener('mouseleave', onLeave);
        // ``ocrTextClear`` removes the nodes; the listeners have to go
        // with them, or a re-render would stack a second pair on the
        // same wrapper.
        wrapper._ocrTextHover = function () {
            wrapper.removeEventListener('mousemove', onMove);
            wrapper.removeEventListener('mouseleave', onLeave);
            if (frame) cancelAnimationFrame(frame);
            setOpen(null);
        };
    }

    /**
     * Put a mouse event in the page's own pixels.
     *
     * The canvas is rendered at the wrapper's layout width (both
     * viewers write ``wrapper.style.width = viewport.width``), so the
     * page's pixels are the wrapper's, and only the zoom transform
     * separates them from the screen's.
     *
     * @param {HTMLElement} wrapper - The .canvas-wrapper of the page.
     * @param {MouseEvent} event - The move to place.
     * @returns {{x: number, y: number}} The point, in page pixels.
     */
    function pointIn(wrapper, event) {
        if (typeof eventToCanvasPixels === 'function') {
            return eventToCanvasPixels(event, wrapper);
        }
        var rect = wrapper.getBoundingClientRect();
        return { x: event.clientX - rect.left, y: event.clientY - rect.top };
    }

    /**
     * The smallest cell rect that holds the point, or nothing.
     *
     * @param {Array} hits - The cell rects, smallest first.
     * @param {{x: number, y: number}} point - In the page's pixels.
     * @returns {HTMLElement|null} The box to open.
     */
    function hitAt(hits, point) {
        for (var i = 0; i < hits.length; i++) {
            var hit = hits[i];
            if (point.x >= hit.left && point.x <= hit.right &&
                point.y >= hit.top && point.y <= hit.bottom) {
                return hit.box;
            }
        }
        return null;
    }

    /**
     * Show one box's text, and close the box that was open.
     *
     * @param {HTMLElement|null} box - The box to open, or nothing.
     */
    function setOpen(box) {
        if (openBox === box) return;
        if (openBox) openBox.classList.remove('open');
        openBox = box;
        if (box) box.classList.add('open');
    }

    function pageNote(page) {
        var note = document.createElement('div');
        note.className = 'ocr-page-note';
        var head = document.createElement('strong');
        head.textContent = 'No text: ' + page.note;
        note.appendChild(head);
        if (page.md) {
            var body = document.createElement('div');
            body.className = 'ocr-page-note-text';
            body.textContent = page.md;
            note.appendChild(body);
        }
        return note;
    }
})();
