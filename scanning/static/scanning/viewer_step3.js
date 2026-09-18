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
 * - **The document holds no markup.** A voted group holds tokens with
 *   a ``low_confidence`` flag, and this module builds the nodes. Every
 *   string enters the DOM with ``textContent``.
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
 */

(function () {
    'use strict';

    // The scale the pages are drawn at follows the column: the width
    // it has, and the height it shows. The ceiling is low on purpose,
    // the rule of the step-2 viewer, which fits one page in the panel
    // and never draws above 1.0. A wide screen would else draw a page
    // of 1160 by 1500 pixels, which is 7 MB of canvas that shows less
    // than half a page.
    var MIN_SCALE = 0.4;
    var MAX_SCALE = 1.2;

    // What the page frame and the scroll bar take off the column.
    var COLUMN_PAD = 24;

    // How far outside the column a page is drawn, for the lazy render.
    var RENDER_MARGIN = '600px';

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
     * Return how tall the column shows, in pixels.
     *
     * The stylesheet owns that number (``max-height`` on the two
     * columns), so this reads it and keeps no copy of it. The height
     * of the column itself is the height of what is in it before the
     * pages are drawn, which would make the scale depend on the size
     * it is about to set.
     *
     * @returns {number} The height, or 0 when the sheet sets none.
     */
    function columnHeight() {
        var limit = window.getComputedStyle(pagesColumn).maxHeight;
        var pixels = parseFloat(limit);
        return isNaN(pixels) ? 0 : pixels;
    }

    /**
     * Return the scale one page is drawn at.
     *
     * The column decides it: the width it has, so a narrow window gets
     * a smaller page and no sideways scroll, and the height it shows,
     * so one page of the opinion is one screen of the column. A resize
     * draws the pages again at the new size.
     *
     * @param {number} width - The width of the page, in points.
     * @param {number} height - The height of the page, in points.
     * @returns {number} The scale.
     */
    function scaleFor(width, height) {
        if (!pagesColumn || !width) { return MIN_SCALE; }
        var scale = (pagesColumn.clientWidth - COLUMN_PAD) / width;
        var tall = columnHeight();
        if (tall && height) {
            scale = Math.min(scale, (tall - COLUMN_PAD) / height);
        }
        return Math.min(MAX_SCALE, Math.max(MIN_SCALE, scale));
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
        var scale = scaleFor(pageSize.width, pageSize.height);
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
        pageDiv.querySelector('.canvas-wrapper')
            .querySelectorAll('.ensemble-box')
            .forEach(function (el) { el.remove(); });
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
            var size = page.getViewport({ scale: 1 });
            var scale = scaleFor(size.width, size.height);
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
     * Draw the box of every group of one page.
     *
     * A box is in the points of the volume page, and the opinion PDF
     * keeps the page size, so the scale of the render is the only
     * conversion.
     *
     * @param {HTMLElement} pageDiv - The container of the page.
     * @param {number} index - The 0-based page of the opinion.
     * @param {number} scale - The scale of the render.
     */
    function drawBoxes(pageDiv, index, scale) {
        var wrapper = pageDiv.querySelector('.canvas-wrapper');
        wrapper.querySelectorAll('.ensemble-box').forEach(function (el) {
            el.remove();
        });
        var page = pageOf(index);
        if (!page) { return; }
        (page.groups || []).forEach(function (group) {
            var box = group.box_pt;
            if (!box) { return; }
            var el = document.createElement('div');
            el.className = 'ensemble-box';
            el.dataset.page = String(index);
            el.dataset.group = String(group.id);
            el.dataset.agreement = group.agreement;
            if (group.weak) { el.classList.add('weak'); }
            el.style.left = (box[0] * scale) + 'px';
            el.style.top = (box[1] * scale) + 'px';
            el.style.width = ((box[2] - box[0]) * scale) + 'px';
            el.style.height = ((box[3] - box[1]) * scale) + 'px';
            wrapper.appendChild(el);
        });
        if (selected && selected.page === index) { paintSelection(); }
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
                page.groups.forEach(function (group) {
                    block.appendChild(groupNode(page, group));
                });
            }
            textColumn.appendChild(block);
        });
    }

    function note(className, text) {
        var el = document.createElement('p');
        el.className = className;
        el.textContent = text;
        return el;
    }

    /**
     * Build the node of one group.
     *
     * A voted group is built token by token, so a word with no
     * majority carries its own mark. Every other group is one string.
     * The document holds no markup, and this is the one place that
     * decides what a reading looks like.
     *
     * @param {Object} page - The page entry.
     * @param {Object} group - The group entry.
     * @returns {HTMLElement} The node.
     */
    function groupNode(page, group) {
        var node = document.createElement('p');
        node.className = 'ensemble-group';
        node.dataset.page = String(page.page_in_opinion);
        node.dataset.group = String(group.id);
        node.dataset.agreement = group.agreement;
        if (group.weak) { node.classList.add('weak'); }

        var tokens = (group.tokens || []).filter(function (token) {
            return token.text;
        });
        if (group.agreement === 'voted' && tokens.length) {
            // The words join with one space, the rule the document's
            // own ``text`` follows.
            tokens.forEach(function (token, position) {
                var span = document.createElement('span');
                span.textContent = token.text;
                if (token.low_confidence) {
                    span.className = 'ensemble-low';
                    span.title = 'The engines did not agree on this word.';
                }
                node.appendChild(span);
                if (position < tokens.length - 1) {
                    node.appendChild(document.createTextNode(' '));
                }
            });
        } else {
            node.textContent = group.text || '';
        }

        var line = groupNote(page, group);
        if (line) {
            var tag = document.createElement('span');
            tag.className = 'ensemble-note';
            tag.textContent = line;
            node.appendChild(document.createTextNode(' '));
            node.appendChild(tag);
        }
        return node;
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
     * Bring one element into view inside its own column.
     *
     * The column is the scroll box, so this writes ``scrollTop`` and
     * never calls ``scrollIntoView``, which would move the window and
     * take the other column out from under the pointer.
     *
     * @param {HTMLElement} column - The scroll box.
     * @param {HTMLElement} el - The element inside it.
     */
    function scrollWithin(column, el) {
        if (!column || !el) { return; }
        var frame = column.getBoundingClientRect();
        var box = el.getBoundingClientRect();
        if (box.top >= frame.top && box.bottom <= frame.bottom) { return; }
        column.scrollTop += (box.top - frame.top) - (frame.height / 3);
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
        summary.textContent = (
            '— ' + (doc.engines || []).join(', ') + ', ' +
            (counts.groups || 0) + ' block(s), ' +
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
            }, 200);
        });
    });
}());
