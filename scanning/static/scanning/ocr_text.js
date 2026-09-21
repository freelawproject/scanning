/**
 * The text overlay of the process viewer (issues #262, #381).
 *
 * A reviewer of review 1 finds a page with bleedthrough or a blurry
 * page, and cannot tell what it cost the reading. This module draws
 * the text one OCR engine read on the page itself: one box per unit of
 * that engine's document, at the unit's own position.
 *
 * **This file names no engine.** The select beside the button carries
 * the names, which the view writes from ``opinion_ocr.ENGINES``, and
 * the endpoint answers the three field names of the document it points
 * at (``fields``). So a fourth engine is one more entry of that table
 * and no line here. The rule is pinned by a test.
 *
 * Three rules the issue asks for, and where each one lives:
 *
 * - **Each engine loads one time.** The first draw of an engine asks
 *   ``scan_ocr_text_url`` for a presigned GET, reads the document
 *   straight from the bucket, and keeps one index of it. A later draw
 *   of the same engine paints from that index: only the DOM nodes go
 *   on a disable, so a reviewer can compare two engines on one page
 *   and pay for each read once.
 * - **The viewport only.** Each viewer calls ``ocrTextPaint`` at the
 *   end of a page render, and a page renders only when the lazy
 *   observer brings it near the viewport. So the zoom re-render and
 *   every jump to a page follow with no other hook.
 * - **Nothing draws by itself.** The reviewer presses the button.
 *
 * The units are in the render space of the page at 200 dpi, so a box
 * scales by ``canvas.width / page.width`` -- the rule the detection
 * boxes of step 2 follow. US Letter is 1700x2200 in all three reads.
 * The text is the model's, so it enters the DOM with ``textContent``.
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

    // One index per engine that was read, keyed by the engine name.
    // Each entry is ``{1-based page: page entry}``, kept for the life
    // of the page: a reviewer who compares two engines on one page
    // must not pay for the second read twice (#381).
    var indexes = {};
    // The engine the select holds. The view writes the options and
    // marks the first one that read, so this is never a name the
    // endpoint refuses.
    var engine = null;
    // What a person calls each engine, as the endpoint said it. The
    // chip on every box carries it, so a reviewer reads which engine
    // wrote the words in front of them.
    var labels = {};
    var loading = false;
    var enabled = false;

    // The box under the pointer, so a move closes the one before it.
    var openBox = null;

    // The border colour of a box, by the kind the engine gives the
    // unit. One table for the three engines (#381): the key is the
    // name with its case and its punctuation dropped, because the same
    // kind is "Page-header", "page_header" and "PageHeader" in the
    // three documents. Everything no engine names is grey, which is
    // what an engine this table has never seen draws.
    var CATEGORY_COLORS = {
        'pageheader': '#2563eb',
        'pagefooter': '#2563eb',
        'header': '#2563eb',
        'footer': '#2563eb',
        'title': '#7c3aed',
        'sectionheader': '#7c3aed',
        'text': '#059669',
        'listitem': '#059669',
        'list': '#059669',
        'caption': '#d97706',
        'footnote': '#d97706',
        'table': '#db2777',
        'formula': '#db2777',
        'equation': '#db2777',
        'picture': '#6b7280',
        'figure': '#6b7280',
        'image': '#6b7280',
    };

    /**
     * The colour of one unit's kind, or grey.
     *
     * @param {string} kind - The engine's own name for the unit.
     * @returns {string} A CSS colour.
     */
    function colorOf(kind) {
        var key = String(kind || '').toLowerCase().replace(/[^a-z]/g, '');
        return CATEGORY_COLORS[key] || '#6b7280';
    }

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
     * Bind the toolbar button and the engine select. Safe to call on a
     * page with neither: the view renders them only when one engine
     * read this volume.
     */
    window.ocrTextInit = function () {
        var button = document.getElementById('ocr-text-toggle');
        if (!button) return;
        var select = document.getElementById('ocr-text-engine');
        engine = select ? select.value : null;
        button.addEventListener('click', function () {
            if (loading) return;
            if (enabled) {
                setEnabled(button, false);
            } else {
                show(button);
            }
        });
        if (!select) return;
        select.addEventListener('change', function () {
            engine = select.value;
            if (!enabled) return;
            // The boxes on the page are the other engine's. Clear them
            // before the read, so the page never carries two reads.
            setEnabled(button, false);
            show(button, select);
        });
    };

    /**
     * Draw the engine the select holds, reading it first if need be.
     *
     * @param {HTMLElement} button - The toolbar button.
     * @param {HTMLElement} [select] - The engine select, to put back
     *     on a refusal.
     */
    function show(button, select) {
        if (engine && indexes[engine]) {
            setEnabled(button, true);
        } else {
            load(button, select);
        }
    }

    /**
     * Draw one page's overlay, if the overlay is on and the page was
     * read. Called at the end of a page render by both viewers.
     *
     * @param {HTMLElement} pageDiv - The .page-container element.
     * @param {number} pdfIndex - The 0-based page of the drawn space.
     */
    window.ocrTextPaint = function (pageDiv, pdfIndex) {
        var read = engine ? indexes[engine] : null;
        if (!enabled || !read || !pageDiv || !drawsTheVolume()) return;
        var page = read[pdfIndex + 1];
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

    function load(button, select) {
        var cfg = config();
        if (!cfg.ocrTextUrlApi || !engine) return;
        // The button and the select are both dead until this answers,
        // so the name cannot move under the read.
        loading = true;
        var wanted = engine;
        var label = button.textContent;
        startWaiting(button, 'Reading…');
        var api = cfg.ocrTextUrlApi + '?engine=' + encodeURIComponent(engine) +
            (cfg.finalSpace ? '&space=final' : '');
        var documentUrl = null;
        var fields = null;
        fetch(api)
            .then(function (r) {
                return r.json().then(function (d) {
                    if (!r.ok) throw new Error(d.error || 'no OCR text');
                    return d;
                });
            })
            .then(function (answer) {
                documentUrl = answer.url;
                fields = answer.fields;
                labels[wanted] = answer.label || wanted;
                if (answer.size) {
                    // The second read is the slow one, and its size is
                    // why. The number goes in a toast and not on the
                    // button: a label that grows moves every other
                    // button of the toolbar on each press.
                    var mb = Math.max(1, Math.round(answer.size / 1048576));
                    var said = 'Reading the text ' + answer.label +
                        ' read of this volume, ' + mb + ' MB.';
                    button.title = said;
                    if (typeof showToast === 'function') {
                        showToast(said + ' This takes a moment.', 'info');
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
                indexes[wanted] = buildIndex(doc, fields);
                stopWaiting(button, label);
                setEnabled(button, true);
            })
            .catch(function (err) {
                stopWaiting(button, label);
                // Put the select back on the engine that is drawn, so
                // the control never names a read the page does not
                // hold.
                if (select) {
                    engine = select.value = drawnEngine(select);
                }
                failed(err, documentUrl);
            });
    }

    /**
     * The engine whose read the page holds, for a select to fall back
     * to: the one that is drawn, else the first that is loaded, else
     * the first option that is not disabled.
     *
     * @param {HTMLElement} select - The engine select.
     * @returns {string} An engine name.
     */
    function drawnEngine(select) {
        var options = select.options;
        for (var i = 0; i < options.length; i++) {
            if (!options[i].disabled && indexes[options[i].value]) {
                return options[i].value;
            }
        }
        for (var j = 0; j < options.length; j++) {
            if (!options[j].disabled) return options[j].value;
        }
        return select.value;
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
        // The select goes with it. One read at a time, and a control
        // that answers a press by snapping back reads as broken.
        var select = document.getElementById('ocr-text-engine');
        if (select) select.disabled = true;
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
        var select = document.getElementById('ocr-text-engine');
        if (select) select.disabled = false;
    }

    /**
     * Say which engine a message is about.
     *
     * @returns {string} The engine's name for a reader.
     */
    function engineLabel() {
        return labels[engine] || 'The OCR';
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
        // The server's own refusal names the engine, so it is the
        // whole message. Only a fault of the network arrives without
        // one, and the sentence still reads.
        var message = 'The text did not load (' + reason +
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
     * The document holds the whole text of every page as well, which
     * is the same words again. It is kept for a page with no unit
     * only: that page is the one the reviewer hunts, and that text is
     * the only one left for it. dots.mocr and Mistral call the field
     * ``md`` and Surya calls it ``text``, so both are read.
     *
     * The page frame is the render the boxes were measured in:
     * ``origin_width``/``origin_height`` on the page for the engines
     * whose worker renders it, and the document's ``render`` for the
     * engine that is given a picture. That is the pair of rules
     * ``opinion_ocr._page_frame`` and ``_mistral_frame`` hold, and no
     * third rule exists.
     *
     * A unit with no box is dropped: the overlay has nowhere to draw
     * it. A Mistral document glued before #350 carries no box at all,
     * and that page then reads as a page with no unit, with a note
     * that says so.
     *
     * @param {Object} doc - The glued document.
     * @param {Object} fields - ``{units, text, type}`` of this engine.
     * @returns {Object} ``{1-based page: {width, height, cells, note,
     *     md}}``.
     */
    function buildIndex(doc, fields) {
        var out = {};
        var pages = (doc && doc.pages) || [];
        var render = (doc && doc.render) || {};
        pages.forEach(function (page) {
            var number = page.pdf_page;
            if (!number) return;
            var units = page[fields.units] || [];
            var boxed = 0;
            var cells = units.filter(function (unit) {
                // The box first, and counted before the text: a read
                // whose units carry no box at all is the one fault
                // the note below can name (#350), and a unit with a
                // box and no words is an ordinary empty unit.
                if (!unit || !unit.bbox) return false;
                boxed += 1;
                return (unit[fields.text] || '').trim() !== '';
            }).map(function (unit) {
                return {
                    bbox: unit.bbox,
                    category: unit[fields.type] || '',
                    text: String(unit[fields.text]).trim(),
                };
            });
            var entry = {
                width: page.origin_width || render.width || 0,
                height: page.origin_height || render.height || 0,
                cells: cells,
            };
            if (!cells.length) {
                entry.note = page.error || noteFor(page, units, boxed);
                entry.md = (page.md || page.text || '').trim();
            }
            out[number] = entry;
        });
        return out;
    }

    /**
     * Why a page draws no box, in words a reviewer can act on.
     *
     * @param {Object} page - The page of the document.
     * @param {Array} units - Its units, whatever the engine calls them.
     * @param {number} boxed - How many of them carry a box.
     * @returns {string} The note.
     */
    function noteFor(page, units, boxed) {
        if (units.length && !boxed) {
            return 'this read holds no boxes to draw. Ask a staff ' +
                'member to glue the run again';
        }
        if (page.filtered) return 'the layout of this page did not parse';
        return 'the OCR found no text on this page';
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
        box.style.borderColor = colorOf(cell.category);
        var panel = document.createElement('div');
        panel.className = 'ocr-cell-text';
        // The border colour says the kind and a colour alone names
        // nothing, so the panel says it in words. It used to ride on
        // the box's ``title``, which a box that takes no pointer never
        // shows. The engine goes in front of it (#381): three reads
        // draw the same page differently, and a reviewer comparing
        // them must never have to remember which one is on.
        var kind = document.createElement('span');
        kind.className = 'ocr-cell-cat';
        kind.textContent = cell.category
            ? engineLabel() + ' - ' + cell.category
            : engineLabel();
        panel.appendChild(kind);
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
