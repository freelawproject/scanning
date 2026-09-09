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

    // The base size of the panel text, and the smallest the fit may
    // make it. Below the floor a panel clips and a hover shows the
    // rest.
    var BASE_FONT_PX = 11;
    var MIN_FONT_PX = 5;

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
     * @param {HTMLElement} pageDiv - The .page-container element.
     */
    window.ocrTextClear = function (pageDiv) {
        if (!pageDiv) return;
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
        var boxes = page.cells.map(function (cell) {
            var box = buildBox(cell, sx, sy);
            wrapper.appendChild(box);
            return box;
        });
        // One measure pass over the page, after every box is placed:
        // a measure inside the loop would lay the page out again for
        // each cell.
        boxes.forEach(fitBox);
    }

    function buildBox(cell, sx, sy) {
        var box = document.createElement('div');
        box.className = 'ocr-cell';
        box.style.left = (cell.bbox[0] * sx) + 'px';
        box.style.top = (cell.bbox[1] * sy) + 'px';
        box.style.width = ((cell.bbox[2] - cell.bbox[0]) * sx) + 'px';
        box.style.height = ((cell.bbox[3] - cell.bbox[1]) * sy) + 'px';
        box.style.borderColor = CATEGORY_COLORS[cell.category] || '#6b7280';
        box.title = cell.category;
        var panel = document.createElement('div');
        panel.className = 'ocr-cell-text';
        panel.style.fontSize = BASE_FONT_PX + 'px';
        // The model wrote this text: textContent, never innerHTML.
        panel.textContent = cell.text;
        box.appendChild(panel);
        return box;
    }

    /**
     * Make one panel's text fit its box.
     *
     * At a fixed width the height of a paragraph grows with the square
     * of the font size, so one square root gives the size that fits,
     * and one measure confirms it. A panel that still overflows keeps
     * the floor size and clips; it takes the pointer, so a hover shows
     * the rest of the text.
     *
     * @param {HTMLElement} box - One .ocr-cell element.
     */
    function fitBox(box) {
        var panel = box.firstChild;
        var room = box.clientHeight;
        if (!room || panel.scrollHeight <= room) return;
        var size = BASE_FONT_PX * Math.sqrt(room / panel.scrollHeight);
        size = Math.max(MIN_FONT_PX, Math.round(size * 10) / 10);
        panel.style.fontSize = size + 'px';
        if (panel.scrollHeight > room) {
            box.classList.add('clipped');
            box.title = box.title + ' (hover to read the whole text)';
        }
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
