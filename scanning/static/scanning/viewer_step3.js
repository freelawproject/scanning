/**
 * The text review of one opinion (issue #365).
 *
 * The page shows the redacted PDF of an opinion beside the text the
 * OCR ensemble wrote from it. Two columns, one block per page: the
 * left column draws the pages with pdf.js, the right column draws one
 * node per group of the ensemble document.
 *
 * Five rules, and where each one lives:
 *
 * - **The browser reads both objects from the bucket.** The container
 *   carries the address of the two routes that mint a presigned GET
 *   (``opinion_pdf_url``, ``opinion_ensemble_url``). This module
 *   spells no path of its own (#334), and the web pod reads no byte of
 *   either object.
 * - **Every overlay is drawn at every render.** A box holds the points
 *   of the volume page, and the scale follows the width of the column,
 *   so the render draws the boxes again. Nothing is positioned one
 *   time (#311). A page holds the scale it was drawn at, so it is
 *   drawn again only when that scale changes.
 * - **The document holds no markup.** A group holds a plain text,
 *   standoff marks over it and a kind (#404), a voted group holds
 *   tokens with a ``low_confidence`` flag, and this module builds the
 *   nodes (:func:`markedNodes`). Every string enters the DOM with
 *   ``textContent``. The words that differ are measured here too
 *   (#380), over the text as it is shown and never over the key of
 *   ``ensemble.compare_text``.
 * - **A box takes no pointer.** One hit test on the wrapper finds the
 *   box under the pointer, the rule of ``ocr_text.js``: a box that
 *   took the pointer would swallow the clicks of the page.
 * - **Each column scrolls alone.** The two columns are two scroll
 *   boxes, and a jump moves one column and never the window: a scroll
 *   of the window would take the text out from under the pointer that
 *   asked for the jump.
 *
 * The link between the columns is the group id. A text node carries
 * ``data-page`` and ``data-group``; so does its box.
 *
 * A group says its section (#399), and the section is read off the
 * group and never off its place in the list: a page shows its body
 * text, then its footnotes in a block of their own, and the page
 * draws its footnote zones under the boxes.
 */

(function () {
    'use strict';

    // The scale the pages are drawn at. The page fits the width of the
    // column to start with, and the zoom buttons move it between these
    // two: the reviewer reads the page against the text, so the page
    // must be readable, and a page wider than the column is scrolled
    // to the box that is marked.
    var MIN_SCALE = 0.3;
    var MAX_SCALE = 3.0;
    var ZOOM_STEP = 0.25;

    // What the page frame and the scroll bar take off the column.
    var COLUMN_PAD = 24;

    // How far outside the column a page is drawn, for the lazy render.
    var RENDER_MARGIN = '600px';

    // The longest reading the panel compares word by word (#380). The
    // table of the comparison is quadratic, and a group is one
    // paragraph, so this guard never fires on real work. It is here
    // because a whole page in one group would otherwise hold the
    // browser.
    var MAX_DIFF_WORDS = 1000;

    // The two sections of a page (#399), the values of
    // ``ensemble.BODY`` and ``ensemble.FOOTNOTES``.
    var BODY = 'text';
    var FOOTNOTES = 'footnotes';

    var root = null;
    var pagesColumn = null;
    var textColumn = null;

    // The ensemble document, once it is read. Null until then.
    var doc = null;
    var pdfDoc = null;
    var observer = null;

    // The group under the pointer, so a move clears the one before it.
    var selected = null;

    // The page a card of the findings asked for before the text was
    // read. The text column holds no block until then, so the jump is
    // done again when the blocks exist.
    var pending = null;

    // The size of page one, in points. Every placeholder takes it, so
    // the column has its true height before a page is drawn.
    var pageSize = null;

    // The scale the reviewer asked for, or null for the fit.
    var zoom = null;

    function endpoint(name) {
        return root ? root.dataset[name] : '';
    }

    /**
     * Return the CSRF token of the page.
     *
     * The hidden input of the header's sign-out form, which every page
     * of the portal has, and which the other viewers read the same
     * way.
     *
     * @returns {string} The token.
     */
    function csrfToken() {
        var field = document.querySelector('[name=csrfmiddlewaretoken]');
        return field ? field.value : '';
    }

    /**
     * Read one of the two routes and return the presigned URL.
     *
     * @param {string} name - The dataset key that holds the route.
     * @returns {Promise<string>} The URL, or a rejection that carries
     *     the message of the route.
     */
    function askForUrl(name) {
        var address = endpoint(name);
        if (!address) { return Promise.reject(new Error('no route')); }
        return fetch(address, { credentials: 'same-origin' })
            .then(function (response) {
                return response.json().then(function (data) {
                    if (!response.ok || !data.url) {
                        throw new Error(
                            data.error || 'the object is not in the bucket'
                        );
                    }
                    return data.url;
                });
            });
    }

    // -----------------------------------------------------------------
    // The left column: the pages of the redacted PDF
    // -----------------------------------------------------------------

    /**
     * Return the scale one page is drawn at.
     *
     * The width of the column decides it while the reviewer has asked
     * for nothing: a page fills the column and needs no sideways
     * scroll, and a resize draws the pages again at the new width.
     * After a press of a zoom button, that scale is the answer for
     * every page, and a page wider than the column scrolls.
     *
     * @param {number} width - The width of the page, in points.
     * @returns {number} The scale.
     */
    function scaleFor(width) {
        if (zoom !== null) { return zoom; }
        if (!pagesColumn || !width) { return MIN_SCALE; }
        var room = (pagesColumn.clientWidth - COLUMN_PAD) / width;
        return Math.min(MAX_SCALE, Math.max(MIN_SCALE, room));
    }

    /**
     * Show the scale the pages are drawn at.
     */
    function showZoom() {
        var label = document.getElementById('zoom-level');
        if (!label || !pageSize) { return; }
        label.textContent = Math.round(scaleFor(pageSize.width) * 100) + '%';
    }

    /**
     * Draw every page again at a scale the reviewer asked for.
     *
     * @param {string} action - ``in``, ``out`` or ``fit``.
     */
    function changeZoom(action) {
        if (!pageSize) { return; }
        var before = scaleFor(pageSize.width);
        if (action === 'fit') {
            zoom = null;
        } else {
            var step = action === 'in' ? ZOOM_STEP : -ZOOM_STEP;
            zoom = Math.min(MAX_SCALE, Math.max(MIN_SCALE, before + step));
        }
        var after = scaleFor(pageSize.width);
        sizePlaceholders();
        redrawRenderedPages();
        // The column keeps its place in the opinion: every page grew
        // or shrank by the same factor, and an offset in the old
        // pixels points at another page in the new ones.
        if (before) {
            pagesColumn.scrollTop *= after / before;
            pagesColumn.scrollLeft *= after / before;
        }
        showZoom();
    }

    function bindZoom() {
        document.querySelectorAll('[data-zoom]').forEach(function (button) {
            button.addEventListener('click', function () {
                changeZoom(button.dataset.zoom);
            });
        });
    }

    /**
     * Give every placeholder the size of a drawn page.
     *
     * A bare canvas lays out at 300 by 150 pixels, and a placeholder
     * of that size puts a jump to page nine several pages out. The
     * size of page one is the size of the others, so the whole column
     * takes it as soon as the PDF opens.
     */
    function sizePlaceholders() {
        if (!pageSize) { return; }
        var scale = scaleFor(pageSize.width);
        pagesColumn.querySelectorAll('.opinion-page').forEach(function (page) {
            if (page.dataset.rendered === '1') { return; }
            var wrapper = page.querySelector('.canvas-wrapper');
            wrapper.style.width = (pageSize.width * scale) + 'px';
            wrapper.style.height = (pageSize.height * scale) + 'px';
            page.style.width = (pageSize.width * scale) + 'px';
        });
    }

    /**
     * Build one empty container per page of the opinion.
     *
     * The containers exist before the PDF opens, so both columns hold
     * the same blocks from the first paint, and a card of the findings
     * reaches a page that is not drawn yet.
     *
     * @param {number} count - How many pages the opinion has.
     */
    function createPlaceholders(count) {
        for (var index = 0; index < count; index++) {
            var page = document.createElement('div');
            page.className = 'page-container opinion-page';
            page.id = 'op-page-' + index;
            page.dataset.pageIndex = String(index);

            var label = document.createElement('div');
            label.className = 'page-label';
            label.textContent = 'Page ' + (index + 1);
            page.appendChild(label);

            var wrapper = document.createElement('div');
            wrapper.className = 'canvas-wrapper';
            var canvas = document.createElement('canvas');
            canvas.className = 'pdf-canvas';
            wrapper.appendChild(canvas);
            page.appendChild(wrapper);
            pagesColumn.appendChild(page);
        }
    }

    /**
     * Draw a page when it comes near the column, and no page before.
     *
     * An opinion is a few pages, but a long one is fifty, and a render
     * of every page at once holds the tab. The rule is the step-2
     * viewer's: the observer draws what the reviewer is about to see.
     */
    function observePages() {
        observer = new IntersectionObserver(function (entries) {
            entries.forEach(function (entry) {
                var page = entry.target;
                if (!entry.isIntersecting) {
                    discardPage(page);
                    return;
                }
                renderPage(page, parseInt(page.dataset.pageIndex, 10));
            });
        }, { root: pagesColumn, rootMargin: RENDER_MARGIN });
        // A page that leaves the column and comes back is drawn
        // again at every crossing without the guard in
        // :func:`renderPage`, the rule of ``renderedPages`` in the
        // step-2 viewer.
        pagesColumn.querySelectorAll('.opinion-page').forEach(function (page) {
            observer.observe(page);
        });
    }

    /**
     * Take back the canvas of a page that left the column.
     *
     * A drawn page holds a bitmap of some megabytes, and a long
     * opinion has fifty pages. The rule is the step-2 viewer's
     * ``discardPage``: the container keeps its size, so the column
     * does not jump, and the page is drawn again when it comes back.
     * The boxes go with the canvas, because they hold the scale of
     * that render.
     *
     * @param {HTMLElement} pageDiv - The container of the page.
     */
    function discardPage(pageDiv) {
        if (pageDiv.dataset.rendered !== '1') { return; }
        if (pageDiv._renderTask) {
            try { pageDiv._renderTask.cancel(); } catch (e) { /* gone */ }
            pageDiv._renderTask = null;
        }
        var canvas = pageDiv.querySelector('.pdf-canvas');
        canvas.width = 0;
        canvas.height = 0;
        clearOverlays(pageDiv.querySelector('.canvas-wrapper'));
        pageDiv.dataset.rendered = '';
        pageDiv.dataset.renderedScale = '';
    }

    /**
     * Draw one page and its boxes.
     *
     * @param {HTMLElement} pageDiv - The container of the page.
     * @param {number} index - The 0-based page of the opinion.
     */
    function renderPage(pageDiv, index) {
        if (!pdfDoc || index >= pdfDoc.numPages) { return; }
        pdfDoc.getPage(index + 1).then(function (page) {
            var scale = scaleFor(page.getViewport({ scale: 1 }).width);
            // The page holds the scale it was drawn at, so a page that
            // crosses the margin again is not drawn again, and a
            // resize that changes the scale is.
            if (pageDiv.dataset.renderedScale === String(scale)) { return; }
            var viewport = page.getViewport({ scale: scale });
            var canvas = pageDiv.querySelector('.pdf-canvas');
            var wrapper = pageDiv.querySelector('.canvas-wrapper');
            canvas.width = viewport.width;
            canvas.height = viewport.height;
            wrapper.style.width = viewport.width + 'px';
            wrapper.style.height = viewport.height + 'px';
            pageDiv.style.width = viewport.width + 'px';
            pageDiv.dataset.rendered = '1';
            pageDiv.dataset.renderedScale = String(scale);

            if (pageDiv._renderTask) {
                try { pageDiv._renderTask.cancel(); } catch (e) { /* gone */ }
            }
            var task = page.render({
                canvasContext: canvas.getContext('2d'),
                viewport: viewport
            });
            pageDiv._renderTask = task;
            task.promise.then(function () {
                if (pageDiv._renderTask === task) {
                    pageDiv._renderTask = null;
                }
                // The scale is the render's own, so the boxes follow a
                // resize with no second rule.
                drawBoxes(pageDiv, index, viewport.scale);
            }, function () { /* swallow a cancel */ });
        }, function (error) {
            // A page that does not open leaves a blank canvas and no
            // word of why, and a presigned URL that died is the
            // likeliest reason, so the page says so.
            pageDiv.dataset.rendered = '';
            pageDiv.dataset.renderedScale = '';
            failed(
                pageDiv,
                'Page ' + (index + 1) + ' did not open: ' + error.message
            );
        });
    }

    /**
     * Put one line on a page that did not open.
     *
     * @param {HTMLElement} pageDiv - The container of the page.
     * @param {string} message - What failed.
     */
    function failed(pageDiv, message) {
        if (pageDiv.querySelector('.opinion-text-error')) { return; }
        pageDiv.appendChild(note('opinion-text-error', message));
    }

    /**
     * Draw the footnote zones and the box of every group of one page.
     *
     * A box is in the points of the volume page, and the opinion PDF
     * keeps the page size, so the scale of the render is the only
     * conversion. The zones go in first, so every box is drawn over
     * them. A zone is not a box: :func:`boxUnder` reads the boxes
     * alone, so a zone never takes the hit test (#399).
     *
     * @param {HTMLElement} pageDiv - The container of the page.
     * @param {number} index - The 0-based page of the opinion.
     * @param {number} scale - The scale of the render.
     */
    function drawBoxes(pageDiv, index, scale) {
        var wrapper = pageDiv.querySelector('.canvas-wrapper');
        clearOverlays(wrapper);
        var page = pageOf(index);
        if (!page) { return; }
        ((page.zones || {}).footnotes || []).forEach(function (box) {
            var zone = document.createElement('div');
            zone.className = 'ensemble-zone';
            placeOver(zone, box, scale);
            wrapper.appendChild(zone);
        });
        (page.groups || []).forEach(function (group) {
            var box = group.box_pt;
            if (!box) { return; }
            var el = document.createElement('div');
            el.className = 'ensemble-box';
            el.dataset.page = String(index);
            el.dataset.group = String(group.id);
            el.dataset.agreement = group.agreement;
            el.dataset.section = sectionOf(group);
            if (group.weak) { el.classList.add('weak'); }
            if (group.footnote_doubt) { el.classList.add('ensemble-doubt'); }
            placeOver(el, box, scale);
            wrapper.appendChild(el);
        });
        if (selected && selected.page === index) { paintSelection(); }
    }

    /**
     * Remove every overlay of one page: the boxes and the zones.
     *
     * @param {HTMLElement} wrapper - The canvas wrapper of the page.
     */
    function clearOverlays(wrapper) {
        wrapper.querySelectorAll('.ensemble-box, .ensemble-zone')
            .forEach(function (el) { el.remove(); });
    }

    /**
     * Put one element over a box of the page.
     *
     * @param {HTMLElement} el - The element.
     * @param {number[]} box - ``[x0, y0, x1, y1]`` in points.
     * @param {number} scale - The scale of the render.
     */
    function placeOver(el, box, scale) {
        el.style.left = (box[0] * scale) + 'px';
        el.style.top = (box[1] * scale) + 'px';
        el.style.width = ((box[2] - box[0]) * scale) + 'px';
        el.style.height = ((box[3] - box[1]) * scale) + 'px';
    }

    /**
     * Return the section of one group: ``text`` or ``footnotes``.
     *
     * The group says it (``ensemble.section``, #399). The order of the
     * groups is not the section, and a document written before the
     * sections (schema 2) holds no ``section``, so its groups are body
     * text, as they were.
     *
     * @param {Object} group - The group entry.
     * @returns {string} The section.
     */
    function sectionOf(group) {
        return group.section === FOOTNOTES ? FOOTNOTES : BODY;
    }

    /**
     * Return the page entry of the document, or null.
     *
     * @param {number} index - The 0-based page of the opinion.
     * @returns {Object|null} The entry.
     */
    function pageOf(index) {
        if (!doc || !doc.pages) { return null; }
        for (var i = 0; i < doc.pages.length; i++) {
            if (doc.pages[i].page_in_opinion === index) {
                return doc.pages[i];
            }
        }
        return null;
    }

    /**
     * Draw the boxes of every page that is already drawn.
     *
     * The document and the PDF arrive in either order. When the PDF is
     * first, its pages are drawn with no box on them, and this puts
     * the boxes on them once the document lands. It draws no page: the
     * canvas holds the render, and the scale is the one it was drawn
     * at.
     */
    function redrawBoxes() {
        if (!pagesColumn) { return; }
        pagesColumn.querySelectorAll('.opinion-page').forEach(function (page) {
            if (page.dataset.rendered !== '1') { return; }
            drawBoxes(
                page,
                parseInt(page.dataset.pageIndex, 10),
                parseFloat(page.dataset.renderedScale)
            );
        });
    }

    /**
     * Draw every page that is already drawn, at the scale it needs now.
     *
     * A resize changes the width of the column and therefore the
     * scale; a page whose scale did not change is left alone by
     * :func:`renderPage`.
     */
    function redrawRenderedPages() {
        if (!pagesColumn) { return; }
        pagesColumn.querySelectorAll('.opinion-page').forEach(function (page) {
            if (page.dataset.rendered !== '1') { return; }
            renderPage(page, parseInt(page.dataset.pageIndex, 10));
        });
    }

    // -----------------------------------------------------------------
    // The right column: the text
    // -----------------------------------------------------------------

    /**
     * Build the text column from the document.
     *
     * A page shows its body text, then its footnotes in a block of
     * their own (#399). Each group says its section, and the order of
     * the groups does not: the split reads :func:`sectionOf` and never
     * the page's ``footnotes`` string, which is the text and not the
     * groups. A page with no footnote group has no block.
     */
    function drawText() {
        textColumn.textContent = '';
        (doc.pages || []).forEach(function (page) {
            var block = document.createElement('div');
            block.className = 'opinion-text-page';
            block.id = 'op-text-' + page.page_in_opinion;
            block.dataset.pageIndex = String(page.page_in_opinion);

            var label = document.createElement('div');
            label.className = 'opinion-text-label';
            label.textContent = 'Page ' + (page.page_in_opinion + 1);
            block.appendChild(label);

            if (page.error) {
                block.appendChild(note('opinion-text-error', page.error));
            } else if (!(page.groups || []).length) {
                block.appendChild(note(
                    'opinion-text-error', 'No block of this page holds text.'
                ));
            } else {
                var footnotes = [];
                page.groups.forEach(function (group) {
                    if (sectionOf(group) === FOOTNOTES) {
                        footnotes.push(group);
                    } else {
                        block.appendChild(groupNode(page, group));
                    }
                });
                if (footnotes.length) {
                    block.appendChild(footnoteBlock(page, footnotes));
                }
            }
            textColumn.appendChild(block);
        });
    }

    /**
     * Build the footnote block of one page.
     *
     * @param {Object} page - The page entry.
     * @param {Object[]} groups - The footnote groups, in document order.
     * @returns {HTMLElement} The block.
     */
    function footnoteBlock(page, groups) {
        var block = document.createElement('div');
        block.className = 'ensemble-footnotes';
        var label = document.createElement('div');
        label.className = 'ensemble-footnotes-label';
        label.textContent = 'Footnotes';
        label.title = 'The text under the footnote zone of this page.';
        block.appendChild(label);
        groups.forEach(function (group) {
            block.appendChild(groupNode(page, group));
        });
        return block;
    }

    function note(className, text) {
        var el = document.createElement('p');
        el.className = className;
        el.textContent = text;
        return el;
    }

    /**
     * The element a group's kind draws (#404). A table is built from
     * its rows and holds no text of its own.
     */
    var KIND_ELEMENTS = {
        heading: 'h3',
        list_item: 'li',
        table: 'div',
        paragraph: 'p'
    };

    /**
     * Fill ``parent`` with ``text`` and its marks.
     *
     * The marks are standoff: ``{start, end, kind}`` over ``text``,
     * with ``kind`` one of ``em``, ``strong`` and ``sup``. The text is
     * cut at every mark edge, and each piece is a text node wrapped
     * in one element per mark that covers it, the innermost first.
     * Every string still enters the DOM with ``textContent``.
     *
     * @param {HTMLElement} parent - The node to fill.
     * @param {string} text - The plain text.
     * @param {Object[]} marks - Its marks, in its own offsets.
     */
    function markedNodes(parent, text, marks) {
        var edges = {0: true};
        edges[text.length] = true;
        (marks || []).forEach(function (mark) {
            edges[mark.start] = true;
            edges[mark.end] = true;
        });
        var cuts = Object.keys(edges).map(Number).sort(function (a, b) {
            return a - b;
        });
        for (var i = 0; i + 1 < cuts.length; i += 1) {
            var start = cuts[i];
            var end = cuts[i + 1];
            var piece = document.createTextNode(text.slice(start, end));
            var wrapped = piece;
            ['sup', 'em', 'strong'].forEach(function (kind) {
                var covers = (marks || []).some(function (mark) {
                    return mark.kind === kind
                        && mark.start <= start && end <= mark.end;
                });
                if (covers) {
                    var element = document.createElement(kind);
                    element.appendChild(wrapped);
                    wrapped = element;
                }
            });
            parent.appendChild(wrapped);
        }
    }

    /**
     * The marks of one token of a voted group, in the token's own
     * offsets: the group's marks cut to ``[at, at + length)``.
     *
     * @param {Object[]} marks - The group's marks.
     * @param {number} at - Where the token starts in the group text.
     * @param {number} length - The token's length.
     * @returns {Object[]} The clipped marks.
     */
    function clipMarks(marks, at, length) {
        var out = [];
        (marks || []).forEach(function (mark) {
            var start = Math.max(mark.start, at) - at;
            var end = Math.min(mark.end, at + length) - at;
            if (end > start) {
                out.push({start: start, end: end, kind: mark.kind});
            }
        });
        return out;
    }

    /**
     * Build the table of a group whose kind is a table.
     *
     * @param {Object} group - The group entry.
     * @returns {HTMLElement} The table.
     */
    function tableNode(group) {
        var table = document.createElement('table');
        table.className = 'ensemble-table';
        var body = document.createElement('tbody');
        (group.table || []).forEach(function (row) {
            var tr = document.createElement('tr');
            row.forEach(function (cell) {
                var td = document.createElement('td');
                td.textContent = cell;
                tr.appendChild(td);
            });
            body.appendChild(tr);
        });
        table.appendChild(body);
        return table;
    }

    /**
     * Build the node of one group.
     *
     * A voted group is built token by token, so a word with no
     * majority carries its own mark. Every other group is one string.
     * The document holds no markup, and this is the one place that
     * decides what a reading looks like: the element follows the
     * group's ``kind`` and the text its ``marks`` (#404).
     *
     * @param {Object} page - The page entry.
     * @param {Object} group - The group entry.
     * @returns {HTMLElement} The node.
     */
    function groupNode(page, group) {
        var kind = group.kind || 'paragraph';
        var node = document.createElement(KIND_ELEMENTS[kind] || 'p');
        node.className = 'ensemble-group';
        node.dataset.page = String(page.page_in_opinion);
        node.dataset.group = String(group.id);
        node.dataset.agreement = group.agreement;
        node.dataset.kind = kind;
        if (group.weak) { node.classList.add('weak'); }
        if (group.footnote_doubt) { node.classList.add('ensemble-doubt'); }

        var tokens = (group.tokens || []).filter(function (token) {
            return token.text;
        });
        var at = 0;
        if (kind === 'table') {
            node.appendChild(tableNode(group));
        } else if (group.agreement === 'voted' && tokens.length) {
            // The words join with one space, the rule the document's
            // own ``text`` follows, so ``at`` is where each token
            // starts in the group text and the marks cut to it.
            tokens.forEach(function (token, position) {
                var span = document.createElement('span');
                markedNodes(
                    span, token.text,
                    clipMarks(group.marks, at, token.text.length)
                );
                at += token.text.length + 1;
                if (token.low_confidence && token.inserted) {
                    span.className = 'ensemble-low';
                    span.title = 'The first engine that read here did'
                        + ' not read this word. The engines that read'
                        + ' it put it in.';
                } else if (token.low_confidence) {
                    span.className = 'ensemble-low';
                    span.title = 'No majority settled this word. It is'
                        + ' the reading of the first engine that read'
                        + ' here.';
                } else if (token.majority) {
                    span.className = 'ensemble-voted';
                    span.title = 'A majority chose this word, and not'
                        + ' every engine read it so.';
                }
                node.appendChild(span);
                if (position < tokens.length - 1) {
                    node.appendChild(document.createTextNode(' '));
                }
            });
        } else {
            markedNodes(node, group.text || '', group.marks || []);
        }

        var line = groupNote(page, group);
        if (line) {
            var tag = document.createElement('span');
            tag.className = 'ensemble-note';
            tag.textContent = line;
            tag.title = 'What the engines did here. Press the readings'
                + ' badge to see each one.';
            node.appendChild(document.createTextNode(' '));
            node.appendChild(tag);
        }
        if (differs(page, group)) {
            node.appendChild(document.createTextNode(' '));
            node.appendChild(compareButton(page, group, node));
            node.classList.add('differs');
        }
        return node;
    }

    /**
     * Return whether the engines did not read one group alike.
     *
     * The browser's copy of ``ensemble._differs``, which the card of
     * the findings and the ``OpinionText`` row both read: a majority,
     * a word vote, an engine that read nothing, or fewer engines in
     * the group than the page holds. A group this answers for carries
     * the badge that opens the readings.
     *
     * @param {Object} page - The page entry.
     * @param {Object} group - The group entry.
     * @returns {boolean} Whether they differ.
     */
    function differs(page, group) {
        if (group.agreement === 'majority' || group.agreement === 'voted') {
            return true;
        }
        if ((group.silent || []).length) { return true; }
        var engines = (page.engines || []).length;
        return engines > 0
            && Object.keys(group.engines || {}).length < engines;
    }

    /**
     * Build the badge that opens the readings of one group.
     *
     * @param {Object} page - The page entry.
     * @param {Object} group - The group entry.
     * @param {HTMLElement} node - The node of the group.
     * @returns {HTMLElement} The button.
     */
    function compareButton(page, group, node) {
        var button = document.createElement('button');
        button.type = 'button';
        button.className = 'ensemble-compare';
        button.textContent = (page.engines || []).length + ' readings';
        button.title = 'Show what each engine read here, and why this'
            + ' reading won';
        button.addEventListener('click', function (event) {
            event.stopPropagation();
            var standing = node.nextSibling;
            if (standing && standing.classList
                    && standing.classList.contains('ensemble-variants')) {
                standing.remove();
                button.classList.remove('open');
                return;
            }
            node.parentNode.insertBefore(
                variantsPanel(page, group), node.nextSibling
            );
            button.classList.add('open');
        });
        return button;
    }

    /**
     * Build the panel that holds one reading per engine.
     *
     * Every engine of the page has a line, in the order of the
     * document, which is the order of ``opinion_ocr.ENGINES`` and
     * therefore the same on every group of every opinion. The source
     * engine carries the mark, wherever it falls; an engine that read
     * nothing here, and an engine that drew no box at all, each say
     * which of the two they are. The reading enters the DOM with
     * ``textContent``.
     *
     * The text above shows the source engine's own reading, except in
     * a voted group, where the words are voted over it and a word of
     * another engine can win. So the panel opens with the reason that
     * engine is the source (#380), each line marks the words that
     * differ from the text above, and the button beside the reason
     * opens the rules of the vote.
     *
     * @param {Object} page - The page entry.
     * @param {Object} group - The group entry.
     * @returns {HTMLElement} The panel.
     */
    function variantsPanel(page, group) {
        var panel = document.createElement('div');
        panel.className = 'ensemble-variants';

        var reason = winnerReason(page, group);
        var why = document.createElement('p');
        why.className = 'ensemble-why';
        why.textContent = reason;
        why.appendChild(document.createTextNode(' '));
        why.appendChild(helpButton(why));
        panel.appendChild(why);

        var marks = panelMarks(page, group);
        (page.engines || []).forEach(function (name) {
            var unit = (group.engines || {})[name];
            var line = document.createElement('div');
            line.className = 'ensemble-variant';

            var who = document.createElement('span');
            who.className = 'ensemble-engine';
            who.textContent = name;
            if (name === group.source) {
                who.classList.add('winner');
                who.title = reason;
            }
            line.appendChild(who);

            var reading = document.createElement('span');
            reading.className = 'ensemble-reading';
            if (!unit) {
                reading.classList.add('absent');
                reading.textContent = 'no box here';
            } else if (!unit.text) {
                reading.classList.add('absent');
                reading.textContent = 'read nothing here';
            } else {
                readingNodes(reading, unit.text, marks[name]);
            }
            line.appendChild(reading);
            panel.appendChild(line);
        });
        return panel;
    }

    /**
     * Return the sentence that says why the text shows one reading.
     *
     * The green check on an engine name says which engine won and not
     * why (#380). The reason is the agreement of the group plus the
     * rank, and the rank is the order of the engines of the page,
     * which is the order of ``opinion_ocr.ENGINES``. So this viewer
     * spells no engine name of its own, and a fourth engine costs it
     * no change.
     *
     * @param {Object} page - The page entry.
     * @param {Object} group - The group entry.
     * @returns {string} The sentence.
     */
    function winnerReason(page, group) {
        var source = group.source;
        var agreeing = group.agreeing || [];
        var reason;
        if (group.agreement === 'single') {
            reason = source + ' alone read words here, so the text'
                + ' shows its reading.';
        } else if (group.agreement === 'voted') {
            reason = 'No reading held a majority, so the words were'
                + ' voted one by one over ' + source + "'s reading,"
                + ' which ranks first of the engines that read.';
        } else if (group.agreement === 'majority') {
            reason = agreeing.join(' and ') + ' read this the same, and'
                + ' the text shows ' + source + "'s own reading,"
                + ' which ranks first of them.';
        } else {
            reason = 'Every engine that read words here read them the'
                + ' same, and the text shows ' + source + "'s own"
                + ' reading, which ranks first.';
        }
        return reason + ' The rank is '
            + (page.engines || []).join(', ') + '.';
    }

    /**
     * Build the button that opens the rules of the vote.
     *
     * @param {HTMLElement} why - The reason line, which the block
     *     goes under.
     * @returns {HTMLElement} The button.
     */
    function helpButton(why) {
        var button = document.createElement('button');
        button.type = 'button';
        button.className = 'ensemble-ask';
        button.textContent = '?';
        button.title = 'How the ensemble chooses a reading';
        button.addEventListener('click', function (event) {
            event.stopPropagation();
            var standing = why.nextSibling;
            if (standing && standing.classList
                    && standing.classList.contains('ensemble-help')) {
                standing.remove();
                button.classList.remove('open');
                return;
            }
            why.parentNode.insertBefore(voteHelp(), why.nextSibling);
            button.classList.add('open');
        });
        return button;
    }

    //: The rules of the vote, as a reader of the panel needs them.
    //: One line per outcome, in the order the ensemble tries them.
    var VOTE_RULES = [
        ['Every engine alike', 'the text shows the reading of the'
            + ' first engine by rank.'],
        ['Most of them alike', 'the text shows the reading of the'
            + ' first of those, and the reason line names them.'],
        ['No majority', 'the words are voted one by one over the first'
            + ' engine that read. A word most engines read carries a'
            + ' light mark. A word no majority settled carries a'
            + ' strong one, and so does a word that engine did not'
            + ' read at all.'],
        ['One engine alone', 'the others read nothing here, so its'
            + ' reading stands.'],
        ['The vote', 'compares the readings with the quotes, the'
            + ' dashes and the markdown marks folded, so typography'
            + ' never decides it. The marks in this panel do show'
            + ' typography.']
    ];

    /**
     * Build the block that holds the rules of the vote.
     *
     * @returns {HTMLElement} The block.
     */
    function voteHelp() {
        var help = document.createElement('div');
        help.className = 'ensemble-help';
        VOTE_RULES.forEach(function (rule) {
            var row = document.createElement('p');
            var name = document.createElement('span');
            name.className = 'ensemble-help-name';
            name.textContent = rule[0];
            row.appendChild(name);
            row.appendChild(document.createTextNode(' \u2014 ' + rule[1]));
            help.appendChild(row);
        });
        return help;
    }

    // -----------------------------------------------------------------
    // The words that differ (#380)
    //
    // **The comparison is the text as it is shown.** ``compare_text``
    // is the rule of the vote, and it folds the quotes, the dashes and
    // the markdown marks, because those are not differences of
    // reading. This is another question: where must the eye go? A
    // curly quote against a straight one is what the reviewer came to
    // see. So this code holds no table and normalizes nothing, and a
    // reader must not copy ``ensemble.compare_word`` into it.
    //
    // The mark says nothing about the vote. ``differs`` alone decides
    // which group carries the badge that opens the panel.
    // -----------------------------------------------------------------

    /**
     * Return the words of one reading, each with its own range.
     *
     * The range is into the reading itself, so the panel draws the
     * spacing the engine wrote.
     *
     * @param {string} text - One engine's reading.
     * @returns {Array} ``[{text, start, end}]``.
     */
    function words(text) {
        var pattern = /\S+/g;
        var found = [];
        var match = pattern.exec(text);
        while (match !== null) {
            found.push({
                text: match[0],
                start: match.index,
                end: match.index + match[0].length
            });
            match = pattern.exec(text);
        }
        return found;
    }

    /**
     * Return the ranges of one reading that the text above lacks.
     *
     * The common start and the common end go first, which is most of
     * two readings of one paragraph. What is left is compared word by
     * word, and a middle longer than ``MAX_DIFF_WORDS`` is marked
     * whole: the reviewer still sees where the two readings part.
     *
     * @param {string} shown - The text above, which the ensemble wrote.
     * @param {string} reading - One engine's own reading.
     * @returns {Array} ``[[start, end]]``, into ``reading``.
     */
    function diffSpans(shown, reading) {
        var left = words(shown);
        var right = words(reading);
        var head = 0;
        var tail = 0;
        while (head < left.length && head < right.length
                && left[head].text === right[head].text) {
            head += 1;
        }
        while (tail < left.length - head && tail < right.length - head
                && left[left.length - 1 - tail].text
                    === right[right.length - 1 - tail].text) {
            tail += 1;
        }
        var a = left.slice(head, left.length - tail);
        var b = right.slice(head, right.length - tail);
        if (!a.length && !b.length) { return []; }
        if (a.length > MAX_DIFF_WORDS || b.length > MAX_DIFF_WORDS) {
            return ranges(b, null);
        }
        return ranges(b, uncommon(a, b).right);
    }

    /**
     * Mark the words of the second list that the first does not hold.
     *
     * The longest common subsequence of the two, and every word of
     * ``b`` outside it is marked. The table is built from the end, and
     * the walk from the start takes the same path the table was built
     * on.
     *
     * @param {Array} a - The words of the text above.
     * @param {Array} b - The words of one engine's reading.
     * @returns {Object} ``{right: [boolean]}``, one flag per word of
     *     ``b``.
     */
    function uncommon(a, b) {
        var width = b.length + 1;
        var table = new Uint16Array((a.length + 1) * width);
        var right = [];
        var i;
        var j;
        for (i = a.length - 1; i >= 0; i -= 1) {
            for (j = b.length - 1; j >= 0; j -= 1) {
                table[i * width + j] = a[i].text === b[j].text
                    ? table[(i + 1) * width + j + 1] + 1
                    : Math.max(
                        table[(i + 1) * width + j],
                        table[i * width + j + 1]
                    );
            }
        }
        for (j = 0; j < b.length; j += 1) { right.push(true); }
        i = 0;
        j = 0;
        while (i < a.length && j < b.length) {
            if (a[i].text === b[j].text) {
                right[j] = false;
                i += 1;
                j += 1;
            } else if (table[(i + 1) * width + j]
                    >= table[i * width + j + 1]) {
                i += 1;
            } else {
                j += 1;
            }
        }
        return { right: right };
    }

    /**
     * Return the ranges of the marked words, joined where they touch.
     *
     * Two words that follow each other in the list have only
     * whitespace between them, so one mark carries both.
     *
     * @param {Array} list - The words, each with its range.
     * @param {Array} marked - One flag per word, or null for every
     *     word.
     * @returns {Array} ``[[start, end]]``.
     */
    function ranges(list, marked) {
        var found = [];
        var open = null;
        var previous = -2;
        list.forEach(function (word, position) {
            if (marked && !marked[position]) { return; }
            if (open && position === previous + 1) {
                open[1] = word.end;
            } else {
                open = [word.start, word.end];
                found.push(open);
            }
            previous = position;
        });
        return found;
    }

    /**
     * Return the ranges each engine's line marks.
     *
     * **Every engine is compared with the text above**, and never with
     * the line that carries the check. The two are the same reading in
     * a single, a majority and a unanimous group, because the source
     * engine is always inside the agreeing set. They are not the same
     * in a voted group: the words are voted one by one, and the text
     * above holds another engine's word wherever the others outvoted
     * the source. So each line marks what it alone did, and the line
     * of the source marks the words the vote took from it.
     *
     * @param {Object} page - The page entry.
     * @param {Object} group - The group entry.
     * @returns {Object} ``{engine: [[start, end]]}``.
     */
    function panelMarks(page, group) {
        var shown = group.text || '';
        var engines = group.engines || {};
        var marks = {};
        if (!shown) { return marks; }
        (page.engines || []).forEach(function (name) {
            var unit = engines[name];
            if (!unit || !unit.text) { return; }
            marks[name] = diffSpans(shown, unit.text);
        });
        return marks;
    }

    /**
     * Draw one reading, with a span over each marked range.
     *
     * Every string enters the DOM with ``textContent``, the rule of
     * this module. A reading with no range is one text node, so a
     * group the engines read alike loses nothing.
     *
     * @param {HTMLElement} parent - The node of the reading.
     * @param {string} text - The reading.
     * @param {Array} spans - The ranges to mark, or nothing.
     * @returns {void}
     */
    function readingNodes(parent, text, spans) {
        var at = 0;
        (spans || []).forEach(function (span) {
            if (span[0] > at) {
                parent.appendChild(
                    document.createTextNode(text.slice(at, span[0]))
                );
            }
            var mark = document.createElement('span');
            mark.className = 'ensemble-diff';
            mark.textContent = text.slice(span[0], span[1]);
            parent.appendChild(mark);
            at = span[1];
        });
        if (at < text.length) {
            parent.appendChild(document.createTextNode(text.slice(at)));
        }
    }

    /**
     * Return the quiet note of one group, or an empty string.
     *
     * The note says why a reader must look at the group, in the order
     * the ensemble decides it: one engine alone, a silent engine, a
     * majority, then a weak alignment.
     *
     * @param {Object} page - The page entry.
     * @param {Object} group - The group entry.
     * @returns {string} The note.
     */
    function groupNote(page, group) {
        var names = Object.keys(group.engines || {});
        var silent = group.silent || [];
        var absent = (page.engines || []).filter(function (name) {
            return names.indexOf(name) < 0;
        });
        var parts = [];
        if (group.agreement === 'single') {
            // The engine that read, and not every engine with a box
            // here: a silent engine has a box and no word in it, and
            // it is named by the clause below as what it is.
            parts.push(group.source + ' alone');
        } else if (group.agreement === 'majority') {
            parts.push((group.agreeing || []).join(', ') + ' agree');
        }
        if (silent.length) {
            parts.push(silent.join(', ') + ' read nothing here');
        }
        if (absent.length) {
            parts.push('no box from ' + absent.join(', '));
        }
        if (group.weak) {
            parts.push('the boxes hardly meet');
        }
        if (group.footnote_doubt) {
            // The group is in the body, because the zone alone decides
            // (#399). The engines named here are the document's own.
            var labellers = group.footnote_by || [];
            parts.push(
                labellers.join(', ')
                + (labellers.length === 1 ? ' calls' : ' call')
                + ' this a footnote; no footnote zone here'
            );
        }
        return parts.length ? '[' + parts.join('; ') + ']' : '';
    }

    // -----------------------------------------------------------------
    // The link between the columns
    // -----------------------------------------------------------------

    /**
     * Mark one group in both columns, and bring the other one into view.
     *
     * @param {number} page - The 0-based page of the opinion.
     * @param {number} group - The id of the group inside that page.
     * @param {string} from - ``text`` or ``page``: the column the
     *     pointer is in, which is the column that does not move.
     */
    function select(page, group, from) {
        if (selected && selected.page === page && selected.group === group) {
            return;
        }
        clearSelection();
        selected = { page: page, group: group };
        paintSelection();
        if (from === 'text') {
            // A page the observer has not drawn has no box yet, and
            // the pages column is what must move before it draws one.
            // So the container of the page is the answer, and it is
            // there from the first paint.
            scrollWithin(
                pagesColumn,
                boxFor(page, group)
                    || document.getElementById('op-page-' + page)
            );
        } else {
            scrollWithin(textColumn, nodeFor(page, group));
        }
    }

    function clearSelection() {
        if (!selected) { return; }
        root.querySelectorAll('.ensemble-selected').forEach(function (el) {
            el.classList.remove('ensemble-selected');
        });
        selected = null;
    }

    function paintSelection() {
        if (!selected) { return; }
        [
            boxFor(selected.page, selected.group),
            nodeFor(selected.page, selected.group)
        ].forEach(function (el) {
            if (el) { el.classList.add('ensemble-selected'); }
        });
    }

    function boxFor(page, group) {
        return root.querySelector(
            '.ensemble-box[data-page="' + page + '"]' +
            '[data-group="' + group + '"]'
        );
    }

    function nodeFor(page, group) {
        return root.querySelector(
            '.ensemble-group[data-page="' + page + '"]' +
            '[data-group="' + group + '"]'
        );
    }

    /**
     * Bring one element into the middle of its own column.
     *
     * The column is the scroll box, so this writes ``scrollTop`` and
     * ``scrollLeft`` and never calls ``scrollIntoView``, which would
     * move the window and take the other column out from under the
     * pointer. A page zoomed past the width of the column scrolls
     * sideways too, so the box the reviewer marked is in front of them
     * and not off the edge.
     *
     * An element taller or wider than the column is put at its start,
     * because the middle of a whole page is not what the reviewer
     * asked for.
     *
     * @param {HTMLElement} column - The scroll box.
     * @param {HTMLElement} el - The element inside it.
     */
    function scrollWithin(column, el) {
        if (!column || !el) { return; }
        var frame = column.getBoundingClientRect();
        var box = el.getBoundingClientRect();
        if (box.top < frame.top || box.bottom > frame.bottom) {
            var top = box.top - frame.top + column.scrollTop;
            column.scrollTop = (
                box.height > frame.height
                    ? top
                    : top - (frame.height - box.height) / 2
            );
        }
        if (box.left < frame.left || box.right > frame.right) {
            var left = box.left - frame.left + column.scrollLeft;
            column.scrollLeft = (
                box.width > frame.width
                    ? left
                    : left - (frame.width - box.width) / 2
            );
        }
    }

    /**
     * Find the group under the pointer on one page.
     *
     * The boxes take no pointer, so this test is what opens one. The
     * smallest box under the pointer wins: a block inside a column box
     * is the one the reviewer means.
     *
     * @param {HTMLElement} wrapper - The wrapper of the page.
     * @param {number} x - The pointer, in the wrapper's own pixels.
     * @param {number} y - The pointer, in the wrapper's own pixels.
     * @returns {HTMLElement|null} The box.
     */
    function boxUnder(wrapper, x, y) {
        var found = null;
        var smallest = Infinity;
        wrapper.querySelectorAll('.ensemble-box').forEach(function (el) {
            var left = el.offsetLeft;
            var top = el.offsetTop;
            var width = el.offsetWidth;
            var height = el.offsetHeight;
            if (x < left || x > left + width) { return; }
            if (y < top || y > top + height) { return; }
            var size = width * height;
            if (size < smallest) { smallest = size; found = el; }
        });
        return found;
    }

    function bindPointers() {
        textColumn.addEventListener('mouseover', function (event) {
            var node = event.target.closest('.ensemble-group');
            if (!node) { return; }
            select(
                parseInt(node.dataset.page, 10),
                parseInt(node.dataset.group, 10),
                'text'
            );
        });
        pagesColumn.addEventListener('mousemove', function (event) {
            var wrapper = event.target.closest('.canvas-wrapper');
            if (!wrapper) { return; }
            var frame = wrapper.getBoundingClientRect();
            var box = boxUnder(
                wrapper,
                event.clientX - frame.left,
                event.clientY - frame.top
            );
            if (!box) { clearSelection(); return; }
            select(
                parseInt(box.dataset.page, 10),
                parseInt(box.dataset.group, 10),
                'page'
            );
        });
    }

    /**
     * Take both columns to the page a card of the findings names.
     */
    function bindFindings() {
        document.querySelectorAll('.finding-card[data-page]').forEach(
            function (card) {
                card.classList.add('finding-card-live');
                card.addEventListener('click', function () {
                    goToPage(parseInt(card.dataset.page, 10));
                });
            }
        );
    }

    /**
     * Take both columns to one page.
     *
     * @param {number} index - The 0-based page of the opinion.
     */
    function goToPage(index) {
        var text = document.getElementById('op-text-' + index);
        // Neither column can answer before its own load. The text
        // column holds no block until the document is read; the pages
        // column holds containers of 300 by 150 pixels until the PDF
        // opens and gives them the size of a page, and a jump measured
        // against those lands pages away. A card is live from the
        // first paint, so the jump is kept and done again by whichever
        // load is still out.
        pending = (text && pageSize) ? null : index;
        scrollWithin(textColumn, text);
        scrollWithin(pagesColumn, document.getElementById('op-page-' + index));
    }

    // -----------------------------------------------------------------
    // The button
    // -----------------------------------------------------------------

    function bindRerun() {
        var button = document.getElementById('rerun-ensemble');
        if (!button) { return; }
        button.addEventListener('click', function () {
            var label = button.innerHTML;
            button.disabled = true;
            button.textContent = 'Reading…';

            function giveBack() {
                button.disabled = false;
                button.innerHTML = label;
            }

            fetch(endpoint('rerunUrl'), {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'X-CSRFToken': csrfToken() }
            })
                .then(function (response) {
                    return response.json().then(function (data) {
                        return { ok: response.ok, data: data };
                    });
                })
                .then(function (result) {
                    if (!result.ok) {
                        showToast(result.data.message, 'error');
                        giveBack();
                        return;
                    }
                    // The text is another document, so the page reads
                    // it again from the start. The server's line
                    // survives the reload (#322).
                    showSavedAfterReload(result.data);
                    window.location.reload();
                })
                .catch(function () {
                    showToast('The request failed. Try again.', 'error');
                    giveBack();
                });
        });
    }

    // -----------------------------------------------------------------
    // The load
    // -----------------------------------------------------------------

    /**
     * Put the counts of the document beside the heading of the column.
     */
    function summarise() {
        var summary = document.getElementById('ensemble-summary');
        if (!summary || !doc) { return; }
        var counts = doc.counts || {};
        var footnotes = counts.footnote_groups
            ? ', ' + counts.footnote_groups + ' of them footnotes'
            : '';
        summary.textContent = (
            '— ' + (doc.engines || []).join(', ') + ', ' +
            (counts.groups || 0) + ' block(s)' + footnotes + ', ' +
            (counts.low_confidence || 0) + ' word(s) with no majority'
        );
    }

    function loadText() {
        if (!textColumn) { return; }
        askForUrl('ensembleUrlEndpoint')
            .then(function (url) { return fetch(url); })
            .then(function (response) { return response.json(); })
            .then(function (data) {
                doc = data;
                drawText();
                summarise();
                redrawBoxes();
                if (pending !== null) { goToPage(pending); }
            })
            .catch(function (error) {
                textColumn.appendChild(note(
                    'opinion-text-error',
                    'The text of this opinion did not load: ' + error.message
                ));
            });
    }

    function loadPdf() {
        if (!pagesColumn) { return; }
        askForUrl('pdfUrlEndpoint')
            .then(function (url) {
                return pdfjsLib.getDocument({ url: url }).promise;
            })
            .then(function (pdf) {
                pdfDoc = pdf;
                return pdf.getPage(1).then(function (page) {
                    var size = page.getViewport({ scale: 1 });
                    pageSize = { width: size.width, height: size.height };
                    sizePlaceholders();
                    observePages();
                    showZoom();
                    if (pending !== null) { goToPage(pending); }
                });
            })
            .catch(function (error) {
                pagesColumn.appendChild(note(
                    'opinion-text-error',
                    'The redacted PDF did not load: ' + error.message
                ));
            });
    }

    document.addEventListener('DOMContentLoaded', function () {
        root = document.getElementById('opinion-review');
        if (!root) { return; }
        pagesColumn = document.getElementById('opinion-pages');
        textColumn = document.getElementById('opinion-text');
        bindRerun();
        bindFindings();
        bindZoom();
        if (pagesColumn) {
            pdfjsLib.GlobalWorkerOptions.workerSrc =
                'https://cdn.jsdelivr.net/npm/pdfjs-dist@3.11.174/' +
                'build/pdf.worker.min.js';
            createPlaceholders(parseInt(root.dataset.pageCount, 10) || 0);
            loadPdf();
        }
        loadText();
        if (pagesColumn && textColumn) { bindPointers(); }
        // A resize changes the width of a column, and the scale
        // follows that width, so the pages are drawn again. A page
        // whose scale did not change is left alone by
        // :func:`renderPage`.
        var timer = null;
        window.addEventListener('resize', function () {
            clearTimeout(timer);
            timer = setTimeout(function () {
                sizePlaceholders();
                redrawRenderedPages();
                showZoom();
            }, 200);
        });
    });
}());
