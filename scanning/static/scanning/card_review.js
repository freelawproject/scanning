/* The card review of review 2 (card_review.py): the missed headnote
 * brackets, the missed captions and the image boxes of one volume,
 * one after the next. Each card is a crop of the page, cut from the
 * PDF step 2 draws, with the box on it, movable at its edges, and two
 * or three buttons. The page writes nothing of its own: every answer
 * is one of step 2's own writes (the add, the approval, the resize,
 * the dismissal), and every answer has the Undo of the write it made.
 *
 * Keys: Return presses the first button of the card in view, x the
 * second ("No bracket", "Take out"), s skips it, u undoes, f opens
 * the card to the whole page and back, and the arrows move between
 * the cards. A crop is drawn when its card comes near the window and
 * its canvas is given back when it leaves, because a volume holds a
 * hundred cards and an outlier a thousand.
 *
 * Every card can draw: d (or "Draw") turns the crop into a drawing
 * surface, a dragged rectangle gets a label from the server's own
 * table and goes to ``add_single_detection`` like the viewer's drawer,
 * and the box stays on the card with an × that withdraws it. The
 * page's other live boxes are drawn as thin outlines, so what is
 * boxed already is in view before a person draws more.
 *
 * A finding card is keyed by its address and not by its row, because
 * an add deletes the finding (the box is cover now) and its Undo
 * writes it again under another pk: the page reads the cards again
 * after every Undo, and the answers and the skips survive by key.
 */
(function () {
    'use strict';

    var root = null;
    var cfg = null;
    var cards = [];
    /** What was answered here, by card key: the state label and the
     *  write that takes it back. */
    var answers = {};
    var skipped = {};
    /** The keys of the answers in the order they were made, so u
     *  takes back the last one wherever the cursor has moved on to. */
    var history = [];
    /** A box a person moved and has not saved, by card key, so a
     *  reload of the cards keeps it. */
    var drafts = {};
    var pdf = null;
    var observer = null;
    var DPR = Math.min(2, window.devicePixelRatio || 1);
    var MIN_BOX_PX = 8;
    var HANDLES = ['nw', 'n', 'ne', 'w', 'e', 'sw', 's', 'se'];
    var GROUPS = ['caption', 'bracket', 'text', 'image'];
    var KEY_NAMES = { enter: 'Return', x: 'x', s: 's', u: 'u', r: 'r', f: 'f', d: 'd', n: 'n' };
    var MIN_DRAW_PX = 8;
    var KINDS = {
        caption: 'caption the model missed',
        bracket: 'bracket the model missed',
        faint: 'bracket box under the gate',
        text: 'text in no box',
        image: 'image box'
    };
    /** What a card is, in words: a link card reads by its group, since
     *  a bracket finding with no guess is a link too. */
    function kindText(card) {
        if (card.kind === 'link') {
            return card.group === 'bracket' ? 'bracket the model missed' : 'caption the model missed';
        }
        return KINDS[card.kind] || card.kind;
    }
    /** The buttons of a card that holds a box to add: the first adds
     *  it, the second says there is nothing there. */
    function addVerbs(card) {
        if (card.group === 'bracket') {
            return { add: 'Add bracket', none: 'No bracket', added: 'bracket added' };
        }
        if (card.kind === 'text') {
            // The block's box is added as a column box, resized first
            // if need be; a box in a column that has one already joins
            // that column (``columns.column_groups``). Pulling the
            // page's own column box over the block (click its label on
            // the crop) answers the card too.
            return { add: 'Add column', none: 'Not text', added: 'column added' };
        }
        return { add: 'Add caption', none: 'No caption', added: 'caption added' };
    }
    /** The buttons of a card that holds a model box: the first keeps
     *  it as drawn or, once the box moved, saves the new box; the
     *  second takes it out. */
    var MODEL_VERBS = {
        image: { keep: 'Keep', kept: 'kept', move: 'Save size', out: 'Take out', gone: 'taken out' },
        faint: { keep: 'Approve', kept: 'approved', move: 'Save size', out: 'Not a bracket', gone: 'not a bracket' }
    };

    // ── helpers ──────────────────────────────────────────────────────
    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) { node.className = className; }
        if (text !== undefined) { node.textContent = text; }
        return node;
    }

    function note(text, tone) {
        return el('p', 'bk-note' + (tone ? ' bk-' + tone : ''), text);
    }

    function csrfToken() {
        return root.querySelector('[name=csrfmiddlewaretoken]').value;
    }

    function parse(response) {
        return response.json().catch(function () { return {}; }).then(function (data) {
            return { ok: response.ok, data: data };
        });
    }

    function getJson(url) {
        return fetch(url, { credentials: 'same-origin' }).then(parse);
    }

    function postJson(url, body) {
        return fetch(url, {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
            body: JSON.stringify(body || {})
        }).then(parse);
    }

    function failed(answer) {
        return (answer && answer.data && (answer.data.message || answer.data.error)) || 'The request failed.';
    }

    function toast(message, tone) {
        if (window.showToast) { showToast(message, tone); }
    }

    function moved(card) {
        if (!card.box || !card.original) { return false; }
        return card.box.some(function (v, i) { return Math.abs(v - card.original[i]) >= 0.5; });
    }

    /** One label of the server's table, by name. */
    function labelNamed(name) {
        return (cfg.labels || []).filter(function (l) { return l.name === name; })[0] || null;
    }

    function sameBox(a, b) {
        return !!a && !!b && a.every(function (v, i) { return Math.abs(v - b[i]) < 0.5; });
    }

    // ── the crop ─────────────────────────────────────────────────────
    function getPdf() {
        if (!pdf) { pdf = pdfjsLib.getDocument({ url: cfg.pdf_url }).promise; }
        return pdf;
    }

    /** The crop a card shows: the whole page when it is opened. */
    function cropOf(card) {
        return card.full ? [0, 0, card.img_width, card.img_height] : card.crop;
    }

    /** A blank canvas of the crop's own shape, so the page does not
     *  jump when the crop is drawn or given back. */
    function blankCanvas(card) {
        var crop = cropOf(card);
        var canvas = el('canvas');
        canvas.width = Math.max(1, Math.ceil(crop[2] - crop[0]));
        canvas.height = Math.max(1, Math.ceil(crop[3] - crop[1]));
        return canvas;
    }

    /** Give a drawn crop back: a blank canvas of the same shape holds
     *  the place, and the observer draws it again when it returns. */
    function releaseCrop(card, wrap) {
        var canvas = wrap.querySelector('canvas');
        if (!canvas || !canvas.dataset.drawn) { return; }
        canvas.replaceWith(blankCanvas(card));
    }

    /** Draw the crop of a card: the page rendered at the scale of the
     *  image space the box lives in, offset so the canvas holds the
     *  crop alone. */
    function drawCrop(card, wrap) {
        var canvas = wrap.querySelector('canvas');
        if (!canvas || canvas.dataset.drawn) { return; }
        canvas.dataset.drawn = '1';
        var crop = cropOf(card);
        getPdf().then(function (doc) {
            return doc.getPage(card.page_index + 1);
        }).then(function (page) {
            var base = page.getViewport({ scale: 1 });
            var scale = card.img_width / base.width * DPR;
            var viewport = page.getViewport({
                scale: scale, offsetX: -crop[0] * DPR, offsetY: -crop[1] * DPR
            });
            canvas.width = Math.ceil((crop[2] - crop[0]) * DPR);
            canvas.height = Math.ceil((crop[3] - crop[1]) * DPR);
            return page.render({ canvasContext: canvas.getContext('2d'), viewport: viewport }).promise;
        }).catch(function (error) {
            wrap.replaceChildren(note('The scan did not load: ' + (error && error.message), 'error'));
        });
    }

    // ── the box on the crop ──────────────────────────────────────────
    function placeBox(card, box, b) {
        var c = cropOf(card);
        var w = c[2] - c[0];
        var h = c[3] - c[1];
        b = b || card.box;
        box.style.left = ((b[0] - c[0]) / w * 100) + '%';
        box.style.top = ((b[1] - c[1]) / h * 100) + '%';
        box.style.width = ((b[2] - b[0]) / w * 100) + '%';
        box.style.height = ((b[3] - b[1]) / h * 100) + '%';
    }

    function clampBox(b, crop, moving) {
        var w = b[2] - b[0];
        var h = b[3] - b[1];
        if (moving) {
            if (b[0] < crop[0]) { b[0] = crop[0]; b[2] = crop[0] + w; }
            if (b[1] < crop[1]) { b[1] = crop[1]; b[3] = crop[1] + h; }
            if (b[2] > crop[2]) { b[2] = crop[2]; b[0] = crop[2] - w; }
            if (b[3] > crop[3]) { b[3] = crop[3]; b[1] = crop[3] - h; }
        } else {
            b[0] = Math.max(crop[0], Math.min(b[0], b[2] - MIN_BOX_PX));
            b[1] = Math.max(crop[1], Math.min(b[1], b[3] - MIN_BOX_PX));
            b[2] = Math.min(crop[2], Math.max(b[2], b[0] + MIN_BOX_PX));
            b[3] = Math.min(crop[3], Math.max(b[3], b[1] + MIN_BOX_PX));
        }
        return b.map(function (v) { return Math.round(v * 10) / 10; });
    }

    /** The primary button and the Reset of an image card follow the
     *  box: "Keep" as drawn, "Save size" once it moved. */
    function markMoved(card, node) {
        var primary = node.querySelector('[data-role="primary"]');
        var reset = node.querySelector('[data-role="reset"]');
        var verbs = MODEL_VERBS[card.kind];
        if (primary && verbs) {
            primary.textContent = moved(card) ? verbs.move : verbs.keep;
        }
        if (reset) { reset.hidden = !moved(card); }
        if (moved(card)) { drafts[card.key] = card.box.slice(); } else { delete drafts[card.key]; }
    }

    /** Drag ``subject.box`` (the card's own box, or another box of
     *  the page under edit) at its handles or inside it; ``changed``
     *  runs after every move. */
    function bindDrag(card, subject, box, wrap, node, changed) {
        var state = null;
        function move(event) {
            if (!state) { return; }
            var dx = (event.clientX - state.x) * state.factor;
            var dy = (event.clientY - state.y) * state.factor;
            var b = state.init.slice();
            var h = state.handle;
            if (h === 'move') {
                b[0] += dx; b[2] += dx; b[1] += dy; b[3] += dy;
            } else {
                if (h.indexOf('w') >= 0) { b[0] += dx; }
                if (h.indexOf('e') >= 0) { b[2] += dx; }
                if (h.indexOf('n') >= 0) { b[1] += dy; }
                if (h.indexOf('s') >= 0) { b[3] += dy; }
            }
            subject.box = clampBox(b, cropOf(card), h === 'move');
            placeBox(card, box, subject.box);
            changed();
        }
        function end() {
            state = null;
            document.removeEventListener('mousemove', move);
            document.removeEventListener('mouseup', end);
        }
        function start(event, handle) {
            event.preventDefault();
            event.stopPropagation();
            setActive(node);
            state = {
                handle: handle,
                x: event.clientX,
                y: event.clientY,
                init: subject.box.slice(),
                // Crop pixels per CSS pixel of the drawn crop.
                factor: (cropOf(card)[2] - cropOf(card)[0]) / wrap.getBoundingClientRect().width
            };
            document.addEventListener('mousemove', move);
            document.addEventListener('mouseup', end);
        }
        box.addEventListener('mousedown', function (event) {
            if (event.target === box) { start(event, 'move'); }
        });
        HANDLES.forEach(function (name) {
            var handle = el('div', 'cr-handle ' + name);
            handle.addEventListener('mousedown', function (event) { start(event, name); });
            box.appendChild(handle);
        });
    }

    /** The page's other boxes, as outlines, and the × of a box drawn
     *  from this card. A click on an outline's label opens that box
     *  for editing: the card is often answered by pulling a box of
     *  the page out to the text, not by drawing one more. */
    function otherBoxes(card, layer, node) {
        var crop = cropOf(card);
        (card.others || []).forEach(function (other) {
            if (card.edit && card.edit.id === other.id) { return; }
            var b = other.box;
            // A box with no part inside the crop has nothing to show.
            if (b[2] <= crop[0] || b[0] >= crop[2] || b[3] <= crop[1] || b[1] >= crop[3]) { return; }
            var box = el('div', 'cr-other' + (other.own ? ' cr-own' : '') + (other.mine ? ' cr-mine' : ''));
            var tag = el('span', 'cr-other-tag', other.label.toLowerCase().replace(/_/g, ' '));
            tag.title = 'Click to move or resize this box';
            // The label sits at the box's visible corner: a box that
            // reaches past the crop keeps its label on the crop.
            tag.style.left = (Math.max(0, crop[0] - b[0]) / (b[2] - b[0]) * 100) + '%';
            tag.style.top = (Math.max(0, crop[1] - b[1]) / (b[3] - b[1]) * 100) + '%';
            tag.addEventListener('mousedown', function (event) { event.stopPropagation(); });
            tag.addEventListener('click', function (event) {
                event.stopPropagation();
                if (answers[card.key] || skipped[card.key]) { return; }
                card.edit = { id: other.id, label: other.label, own: other.own, box: other.box.slice(), original: other.box.slice() };
                card.drawing = false;
                replace(card, node);
            });
            box.appendChild(tag);
            if (other.mine) {
                var x = el('button', 'cr-other-x', '×');
                x.type = 'button';
                x.title = 'Withdraw this box';
                x.addEventListener('mousedown', function (event) { event.stopPropagation(); });
                x.addEventListener('click', function (event) {
                    event.stopPropagation();
                    x.disabled = true;
                    withdrawDrawn(card, node, other);
                });
                box.appendChild(x);
            }
            layer.appendChild(box);
            placeBox(card, box, other.box);
        });
    }

    /** Convert a point of the layer, in CSS pixels, to image pixels. */
    function imagePoint(card, layer, clientX, clientY) {
        var rect = layer.getBoundingClientRect();
        var crop = cropOf(card);
        var fx = (crop[2] - crop[0]) / rect.width;
        var fy = (crop[3] - crop[1]) / rect.height;
        return [
            Math.max(crop[0], Math.min(crop[2], crop[0] + (clientX - rect.left) * fx)),
            Math.max(crop[1], Math.min(crop[3], crop[1] + (clientY - rect.top) * fy))
        ];
    }

    /** The drawing surface: a dragged rectangle, then the label menu. */
    function bindDraw(card, layer, node) {
        var start = null;
        var band = null;
        function rectOf(a, b) {
            return [Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.max(a[0], b[0]), Math.max(a[1], b[1])];
        }
        function move(event) {
            if (!start) { return; }
            placeBox(card, band, rectOf(start, imagePoint(card, layer, event.clientX, event.clientY)));
        }
        function end(event) {
            document.removeEventListener('mousemove', move);
            document.removeEventListener('mouseup', end);
            if (!start) { return; }
            var box = rectOf(start, imagePoint(card, layer, event.clientX, event.clientY));
            start = null;
            if (box[2] - box[0] < MIN_DRAW_PX || box[3] - box[1] < MIN_DRAW_PX) {
                band.remove();
                band = null;
                return;
            }
            box = box.map(function (v) { return Math.round(v * 10) / 10; });
            labelMenu(card, layer, node, band, box);
        }
        layer.addEventListener('mousedown', function (event) {
            if (event.target !== layer) { return; }
            event.preventDefault();
            setActive(node);
            if (band) { band.remove(); }
            layer.querySelectorAll('.cr-draw-popup').forEach(function (p) { p.remove(); });
            start = imagePoint(card, layer, event.clientX, event.clientY);
            band = el('div', 'cr-new');
            layer.appendChild(band);
            placeBox(card, band, [start[0], start[1], start[0], start[1]]);
            document.addEventListener('mousemove', move);
            document.addEventListener('mouseup', end);
        });
    }

    function labelMenu(card, layer, node, band, box) {
        var popup = el('div', 'cr-draw-popup');
        popup.addEventListener('mousedown', function (event) { event.stopPropagation(); });
        var select = el('select', 'cr-draw-select');
        cfg.labels.forEach(function (label) {
            var option = el('option', null, label.name.toLowerCase().replace(/_/g, ' '));
            option.value = String(label.id);
            option.dataset.name = label.name;
            if (label.name === card.label) { option.selected = true; }
            select.appendChild(option);
        });
        popup.appendChild(select);
        var add = el('button', 'btn-primary text-xs', 'Add');
        add.type = 'button';
        add.addEventListener('click', function () {
            add.disabled = true;
            var option = select.options[select.selectedIndex];
            drawnAdd(card, node, box, option.dataset.name, Number(option.value));
        });
        popup.appendChild(add);
        var cancel = el('button', 'btn-outline text-xs', 'Cancel');
        cancel.type = 'button';
        cancel.addEventListener('click', function () { band.remove(); popup.remove(); });
        popup.appendChild(cancel);
        var crop = cropOf(card);
        popup.style.left = ((box[0] - crop[0]) / (crop[2] - crop[0]) * 100) + '%';
        popup.style.top = ((box[3] - crop[1]) / (crop[3] - crop[1]) * 100) + '%';
        layer.appendChild(popup);
        select.focus();
    }

    function drawnAdd(card, node, box, label, labelId) {
        postJson(cfg.urls.add_single_detection, {
            page_index: card.page_index,
            label: label,
            label_id: labelId,
            confidence: 1.0,
            bbox: box,
            img_width: card.img_width,
            img_height: card.img_height
        }).then(function (answer) {
            if (!answer.ok || !answer.data || answer.data.status !== 'ok') {
                toast(failed(answer), 'error');
                return;
            }
            card.others = (card.others || []).filter(function (o) { return o.id !== answer.data.replaced_id; });
            card.others.push({ id: answer.data.detection_id, label: label, box: box, own: true, mine: true });
            card.drawing = false;
            if (window.showSaved) { showSaved(answer.data); }
            replace(card, node);
        }).catch(function (error) { toast('The request failed: ' + error.message, 'error'); });
    }

    function withdrawDrawn(card, node, other) {
        postJson(cfg.urls.delete_detection, { detection_id: other.id }).then(function (answer) {
            if (!answer.ok || !answer.data || answer.data.status !== 'ok') {
                toast(failed(answer), 'error');
                return;
            }
            card.others = card.others.filter(function (o) { return o.id !== other.id; });
            if (window.showSaved) { showSaved(answer.data); }
            replace(card, node);
        }).catch(function (error) { toast('The request failed: ' + error.message, 'error'); });
    }

    function boxLayer(card, wrap, node, frozen) {
        var layer = el('div', 'cr-layer' + (card.drawing ? ' cr-drawing' : ''));
        otherBoxes(card, layer, node);
        if (card.drawing) { bindDraw(card, layer, node); }
        if (card.anchor) {
            // The key icon a caption follows: drawn, never moved.
            var anchor = el('div', 'cr-anchor');
            layer.appendChild(anchor);
            placeBox(card, anchor, card.anchor);
        }
        if (card.edit) { layer.classList.add('cr-editing'); }
        if (card.box) {
            // A text card's own box is the block, and the column box
            // the add writes: it takes the handles like a guess.
            var still = false;
            var box = el('div', 'cr-box cr-' + card.kind + (frozen || card.edit || still ? ' cr-frozen' : ''));
            layer.appendChild(box);
            placeBox(card, box);
            if (!frozen && !card.edit && !still) {
                bindDrag(card, card, box, wrap, node, function () { markMoved(card, node); });
            }
            if (card.kind === 'text' && card.block && !sameBox(card.block, card.original || card.box)) {
                // A page with no column box: the box is the column the
                // block sits in, and the block itself is what the
                // footnote answer takes, so it is drawn too.
                var block = el('div', 'cr-block');
                layer.appendChild(block);
                placeBox(card, block, card.block);
            }
        }
        if (card.edit) {
            var editBox = el('div', 'cr-box cr-edit');
            editBox.appendChild(el('span', 'cr-other-tag', card.edit.label.toLowerCase().replace(/_/g, ' ')));
            layer.appendChild(editBox);
            placeBox(card, editBox, card.edit.box);
            bindDrag(card, card.edit, editBox, wrap, node, function () { markEdited(card, node); });
        }
        wrap.appendChild(layer);
    }

    function edited(card) {
        return Boolean(card.edit) && card.edit.box.some(function (v, i) { return Math.abs(v - card.edit.original[i]) > 0.5; });
    }

    /** The Save and the Reset of a box under edit follow the box. */
    function markEdited(card, node) {
        var save = node.querySelector('[data-role="edit-save"]');
        var reset = node.querySelector('[data-role="edit-reset"]');
        if (save) { save.disabled = !edited(card); }
        if (reset) { reset.hidden = !edited(card); }
    }

    // ── the cards ────────────────────────────────────────────────────
    function describe(card) {
        var text = kindText(card);
        if (card.confidence !== null && card.confidence !== undefined) {
            text += ' at ' + card.confidence.toFixed(2);
        }
        return text;
    }

    function button(label, key, className, onClick) {
        var btn = el('button', className, label);
        btn.type = 'button';
        btn.dataset.key = key;
        btn.title = 'Key: ' + KEY_NAMES[key];
        btn.addEventListener('click', function () { onClick(btn); });
        return btn;
    }

    /** The hash that sends the viewer to a card's page and box. */
    function hashTarget(card) {
        var params = new URLSearchParams();
        params.set('pdf', String(card.page_index));
        var box = card.box || card.crop;
        if (box) {
            params.set('box', box.map(function (v) { return Math.round(v); }).join(','));
            params.set('w', String(Math.round(card.img_width)));
            params.set('h', String(Math.round(card.img_height)));
        }
        return params.toString();
    }

    function footer(card, node) {
        var foot = el('footer');
        var answer = answers[card.key];
        var label = answer ? answer.label : (skipped[card.key] ? 'skipped' : 'not yet decided');
        foot.appendChild(el('span', 'bk-state', label));
        foot.appendChild(el('span', 'bk-grow'));
        // Step 2 at the card's page, with the box flashed: the viewer
        // reads the hash (``jumpToHashTarget`` in viewer_sidebar.js).
        // One named tab per volume, so every card's link lands in the
        // same tab and a hash change there jumps again.
        var open = el('a', 'btn-ghost text-xs', 'Open step 2');
        open.href = cfg.step2_url + '#' + hashTarget(card);
        open.target = 'step2-' + cfg.scan;
        open.rel = 'noopener';
        foot.appendChild(open);
        if (answer) {
            foot.appendChild(button('Undo', 'u', 'btn-outline text-xs', function (btn) {
                btn.disabled = true;
                undo(card, node);
            }));
            return foot;
        }
        if (skipped[card.key]) {
            foot.appendChild(button('Undo', 'u', 'btn-outline text-xs', function () {
                delete skipped[card.key];
                replace(card, node);
            }));
            return foot;
        }
        if (card.edit) {
            var save = button('Save box', 'enter', 'btn-primary text-xs', function (btn) {
                btn.disabled = true;
                saveEdit(card, node);
            });
            save.dataset.role = 'edit-save';
            save.disabled = !edited(card);
            foot.appendChild(save);
            var editReset = button('Reset', 'r', 'btn-ghost text-xs', function () {
                card.edit.box = card.edit.original.slice();
                placeBox(card, node.querySelector('.cr-box.cr-edit'), card.edit.box);
                markEdited(card, node);
            });
            editReset.dataset.role = 'edit-reset';
            editReset.hidden = !edited(card);
            foot.appendChild(editReset);
            var done = button('Done (Esc)', 'escape', 'btn-outline text-xs', function () {
                card.edit = null;
                replace(card, node);
            });
            done.dataset.role = 'edit-done';
            foot.appendChild(done);
            return foot;
        }
        var verbs = MODEL_VERBS[card.kind];
        if (verbs) {
            var primary = button(moved(card) ? verbs.move : verbs.keep, 'enter', 'btn-primary text-xs', function (btn) {
                btn.disabled = true;
                if (moved(card)) { resize(card, node); } else { keep(card, node); }
            });
            primary.dataset.role = 'primary';
            foot.appendChild(primary);
            var reset = button('Reset', 'r', 'btn-ghost text-xs', function () {
                card.box = card.original.slice();
                placeBox(card, node.querySelector('.cr-box'));
                markMoved(card, node);
            });
            reset.dataset.role = 'reset';
            reset.hidden = !moved(card);
            foot.appendChild(reset);
            foot.appendChild(button(verbs.out, 'x', 'btn-outline text-xs', function (btn) {
                btn.disabled = true;
                takeOut(card, node);
            }));
        } else {
            var verbsToAdd = addVerbs(card);
            if (card.kind !== 'link' && verbsToAdd.add) {
                foot.appendChild(button(verbsToAdd.add, 'enter', 'btn-primary text-xs', function (btn) {
                    btn.disabled = true;
                    add(card, node);
                }));
            }
            var footnoteLabel = labelNamed('FOOTNOTES');
            if (card.kind === 'text' && footnoteLabel) {
                // The text card's second answer: a footnote box, for the
                // footnote line the page's footnote box stopped short
                // of. It takes the box as the curator shaped it, else
                // the block itself (the dotted outline inside a wider
                // guess), never a column-sized box.
                foot.appendChild(button('Add footnote', 'n', 'btn-primary text-xs', function (btn) {
                    btn.disabled = true;
                    add(card, node, {
                        label: footnoteLabel.name,
                        labelId: footnoteLabel.id,
                        box: moved(card) ? card.box : (card.block || card.box),
                        added: 'footnote added'
                    });
                }));
            }
            foot.appendChild(button(verbsToAdd.none, 'x', 'btn-outline text-xs', function (btn) {
                btn.disabled = true;
                dismiss(card, node);
            }));
        }
        foot.appendChild(button('Skip', 's', 'btn-outline text-xs', function () {
            skipped[card.key] = true;
            replace(card, node);
            focusNextCard();
        }));
        return foot;
    }

    function cardNode(card) {
        var node = el('article', 'bk-card cr-card cr-' + card.kind + (card.full ? ' cr-expanded' : ''));
        node.dataset.key = card.key;
        var answer = answers[card.key];
        if (answer) { node.classList.add('bk-done'); }
        if (skipped[card.key]) { node.classList.add('cr-skipped'); }
        var header = el('header');
        header.appendChild(el('span', 'bk-step', card.step + ' of ' + card.total));
        var where = el('span', 'bk-where', card.where + ' · ' + describe(card) + ' · ');
        // The whole page, with the box still movable: for a guess in
        // the wrong column, or a picture the crop cut.
        var full = el('button', 'cr-full-btn', card.full ? 'Back to the crop (Esc)' : 'Full page');
        full.type = 'button';
        full.dataset.key = 'f';
        full.title = 'Key: f';
        full.addEventListener('click', function (event) {
            event.stopPropagation();
            card.full = !card.full;
            // A box moved on the whole page comes back inside the
            // crop: the crop grows to hold it, inside the page.
            if (!card.full && card.box) {
                var pad = 40;
                card.crop = [
                    Math.max(0, Math.min(card.crop[0], card.box[0] - pad)),
                    Math.max(0, Math.min(card.crop[1], card.box[1] - pad)),
                    Math.min(card.img_width, Math.max(card.crop[2], card.box[2] + pad)),
                    Math.min(card.img_height, Math.max(card.crop[3], card.box[3] + pad))
                ];
            }
            replace(card, node);
        });
        where.appendChild(full);
        // The drawing surface: a new box of any label, on the crop or
        // on the whole page.
        var draw = el('button', 'cr-full-btn' + (card.drawing ? ' cr-on' : ''), card.drawing ? 'Drawing… (Esc stops)' : 'Draw');
        draw.type = 'button';
        draw.dataset.key = 'd';
        draw.title = 'Key: d. Drag a box on the scan, then pick its label.';
        draw.addEventListener('click', function (event) {
            event.stopPropagation();
            card.drawing = !card.drawing;
            replace(card, node);
        });
        where.appendChild(document.createTextNode(' '));
        where.appendChild(draw);
        header.appendChild(where);
        node.appendChild(header);
        var wrap = el('div', 'bk-cropwrap cr-cropwrap' + (card.full ? ' cr-fullpage' : ''));
        wrap.appendChild(blankCanvas(card));
        node.appendChild(wrap);
        boxLayer(card, wrap, node, Boolean(answer) || Boolean(skipped[card.key]));
        node.appendChild(note(card.message));
        node.appendChild(footer(card, node));
        node.addEventListener('mousedown', function () { setActive(node); });
        wrap._card = card;
        observer.observe(wrap);
        return node;
    }

    /** Draw a card again in its place, after an answer, a skip or an
     *  Undo. */
    function replace(card, node) {
        var active = node.classList.contains('bk-active');
        if (card.full && (answers[card.key] || skipped[card.key])) { card.full = false; }
        var fresh = cardNode(card);
        node.replaceWith(fresh);
        if (active) { setActive(fresh); }
        countAll();
        return fresh;
    }

    // ── the answers ──────────────────────────────────────────────────
    function write(card, node, url, body, label, undoOf) {
        return postJson(url, body).then(function (answer) {
            if (!answer.ok || !answer.data || answer.data.status !== 'ok') {
                toast(failed(answer), 'error');
                replace(card, node);
                return;
            }
            answers[card.key] = { label: label, undo: undoOf(answer.data) };
            history.push(card.key);
            delete drafts[card.key];
            if (window.showSaved) { showSaved(answer.data); }
            replace(card, node);
            focusNextCard();
        }).catch(function (error) {
            toast('The request failed: ' + error.message, 'error');
            replace(card, node);
        });
    }

    function add(card, node, as) {
        as = as || {};
        write(card, node, cfg.urls.add_single_detection, {
            page_index: card.page_index,
            label: as.label || card.label,
            label_id: as.labelId || card.label_id,
            confidence: 1.0,
            bbox: as.box || card.box,
            img_width: card.img_width,
            img_height: card.img_height
        }, as.added || addVerbs(card).added, function (data) {
            // The hand-drawn row that holds the box (an add, or the
            // approval of a model box it landed on): withdrawn, it
            // gives back whatever it replaced.
            return { url: cfg.urls.delete_detection, body: { detection_id: data.detection_id } };
        });
    }

    function dismiss(card, node) {
        write(card, node, cfg.urls.dismiss_finding, { issue_id: card.issue_id }, 'nothing there', function (data) {
            // The finding's pk dies at the next rebuild, which any later
            // write runs; the dismissal row outlives every rebuild.
            return { url: cfg.urls.restore_finding, body: { dismissal_id: data.dismissal_id } };
        });
    }

    function keep(card, node) {
        write(card, node, cfg.urls.approve_detection, { detection_id: card.detection_id }, MODEL_VERBS[card.kind].kept, function (data) {
            return { url: cfg.urls.delete_detection, body: { detection_id: data.detection_id } };
        });
    }

    function resize(card, node) {
        write(card, node, cfg.urls.update_detection, {
            detection_id: card.detection_id, new_bbox: card.box
        }, 'resized', function (data) {
            return { url: cfg.urls.delete_detection, body: { detection_id: data.detection_id } };
        });
    }

    function takeOut(card, node) {
        write(card, node, cfg.urls.delete_detection, { detection_id: card.detection_id }, MODEL_VERBS[card.kind].gone, function () {
            return { url: cfg.urls.restore_detection, body: { detection_id: card.detection_id } };
        });
    }

    /** Write the box under edit through the resize endpoint. The
     *  response names the row that holds the box now (the row itself
     *  for a hand-drawn one, a new hand-drawn row over a model one),
     *  so the undo is the box put back, or that row withdrawn. The
     *  edit is no answer of the card: the cards are read again, and
     *  the card goes by itself when the moved box answers it. */
    function saveEdit(card, node) {
        var edit = card.edit;
        postJson(cfg.urls.update_detection, { detection_id: edit.id, new_bbox: edit.box }).then(function (answer) {
            if (!answer.ok || !answer.data || answer.data.status !== 'ok') {
                toast(failed(answer), 'error');
                replace(card, node);
                return;
            }
            var holder = answer.data.detection_id;
            history.push({
                edit: {
                    label: edit.label.toLowerCase().replace(/_/g, ' ') + ' box on p. ' + (card.page_index + 1),
                    undo: holder === edit.id
                        ? { url: cfg.urls.update_detection, body: { detection_id: edit.id, new_bbox: edit.original } }
                        : { url: cfg.urls.delete_detection, body: { detection_id: holder } }
                }
            });
            card.edit = null;
            if (window.showSaved) { showSaved(answer.data); }
            return reloadAt(card.key);
        }).catch(function (error) {
            toast('The request failed: ' + error.message, 'error');
            replace(card, node);
        });
    }

    /** The last thing done, when it was a box edit; else null. */
    function lastEdit() {
        var last = history[history.length - 1];
        return last && typeof last === 'object' ? last.edit : null;
    }

    function undoEdit() {
        var edit = lastEdit();
        if (!edit) { return Promise.resolve(); }
        var active = activeCard();
        var key = active ? active.dataset.key : null;
        return postJson(edit.undo.url, edit.undo.body).then(function (reply) {
            if (!reply.ok || !reply.data || reply.data.status !== 'ok') {
                toast(failed(reply), 'error');
                return;
            }
            history.pop();
            if (window.showSaved) { showSaved(reply.data); }
            return reloadAt(key);
        }).catch(function (error) { toast('The request failed: ' + error.message, 'error'); });
    }

    /** Read the cards again and come back to one, or to the next open
     *  one when it is gone. The page stays where it is. */
    function reloadAt(key) {
        return load().then(function () {
            var again = key ? root.querySelector('.bk-card[data-key="' + key + '"]') : null;
            if (again) {
                setActive(again);
            } else {
                focusNextCard();
            }
        });
    }

    function refreshUndoEdit() {
        var btn = root.querySelector('[data-role="undo-edit"]');
        if (!btn) { return; }
        var edit = lastEdit();
        // The button class sets its own display, which beats the
        // ``hidden`` attribute, so the display is set here.
        btn.style.display = edit ? '' : 'none';
        btn.textContent = edit ? 'Undo: ' + edit.label : '';
    }

    function undo(card, node) {
        var answer = answers[card.key];
        if (!answer) { return; }
        postJson(answer.undo.url, answer.undo.body).then(function (reply) {
            if (!reply.ok || !reply.data || reply.data.status !== 'ok') {
                toast(failed(reply), 'error');
                replace(card, node);
                return;
            }
            delete answers[card.key];
            history = history.filter(function (key) { return key !== card.key; });
            if (window.showSaved) { showSaved(reply.data); }
            // The finding an add deleted is written again under
            // another pk: read the cards again, and come back to this
            // one.
            return load().then(function () {
                setActive(root.querySelector('.bk-card[data-key="' + card.key + '"]'));
            });
        }).catch(function (error) {
            toast('The request failed: ' + error.message, 'error');
            replace(card, node);
        });
    }

    // ── the page ─────────────────────────────────────────────────────
    function countAll() {
        var keys = {};
        cards.forEach(function (card) { keys[card.key] = true; });
        var done = Object.keys(answers).filter(function (key) { return keys[key]; }).length;
        var skips = Object.keys(skipped).filter(function (key) { return keys[key]; }).length;
        root.querySelector('[data-role="total"]').textContent = String(cards.length);
        root.querySelector('[data-role="done"]').textContent = String(done);
        root.querySelector('[data-role="skipped"]').textContent = String(skips);
        refreshUndoEdit();
    }

    function render() {
        GROUPS.forEach(function (group) {
            var section = root.querySelector('[data-group="' + group + '"]');
            var list = cards.filter(function (card) { return card.group === group; });
            section.querySelector('[data-role="count"]').textContent = String(list.length);
            var holder = section.querySelector('[data-role="cards"]');
            holder.replaceChildren();
            if (!list.length) { holder.appendChild(note('Nothing to look at.', 'ok')); }
            list.forEach(function (card, i) {
                card.step = i + 1;
                card.total = list.length;
                card.original = card.box ? card.box.slice() : null;
                if (card.box && drafts[card.key] && !answers[card.key]) { card.box = drafts[card.key].slice(); }
                holder.appendChild(cardNode(card));
            });
        });
        countAll();
    }

    /** The server's cards, with every card this page answered kept in
     *  its place: an answer removes the card's finding or row from the
     *  server's list, and the card stays here, with its Undo, until the
     *  page is left. */
    function merge(fresh) {
        var keys = {};
        fresh.forEach(function (card) { keys[card.key] = true; });
        cards.forEach(function (card) {
            if (answers[card.key] && !keys[card.key]) { fresh.push(card); }
        });
        var order = {};
        GROUPS.forEach(function (group, i) { order[group] = i; });
        fresh.sort(function (a, b) {
            return (order[a.group] - order[b.group]) || (a.page_index - b.page_index) ||
                (a.key < b.key ? -1 : a.key > b.key ? 1 : 0);
        });
        return fresh;
    }

    function load() {
        var holder = root.querySelector('[data-role="cards-note"]');
        return getJson(cfg.cards_url).then(function (answer) {
            if (!answer.ok || !answer.data || !answer.data.cards) {
                holder.replaceChildren(note(failed(answer), 'error'));
                return;
            }
            holder.replaceChildren();
            cards = merge(answer.data.cards);
            render();
        }).catch(function (error) {
            holder.replaceChildren(note('The cards did not load: ' + error.message, 'error'));
        });
    }

    function recompute(btn) {
        var line = root.querySelector('[data-role="compute-note"]');
        btn.disabled = true;
        postJson(cfg.urls.compute_redactions_api, {}).then(function (answer) {
            var ok = answer.ok && answer.data && answer.data.status !== 'error';
            line.className = 'bk-note' + (ok ? ' bk-ok' : ' bk-error');
            line.textContent = failed(answer) + (ok ? ' Reload the cards when it is done.' : '');
            toast(failed(answer), ok ? undefined : 'error');
        }).catch(function (error) {
            line.className = 'bk-note bk-error';
            line.textContent = 'The request failed: ' + error.message;
        }).then(function () { btn.disabled = false; });
    }

    // ── the cursor and the keys ──────────────────────────────────────
    function allCards() {
        return Array.prototype.slice.call(root.querySelectorAll('.bk-card'));
    }

    function activeCard() {
        return root.querySelector('.bk-card.bk-active');
    }

    function setActive(node) {
        root.querySelectorAll('.bk-card.bk-active').forEach(function (other) {
            other.classList.remove('bk-active');
        });
        if (node) { node.classList.add('bk-active'); }
    }

    function isOpen(node) {
        return !node.classList.contains('bk-done') && !node.classList.contains('cr-skipped');
    }

    /** Whether the reader can see the card: at least half of its crop
     *  is inside the window. A card whose top edge has just come up at
     *  the foot of the window is not seen, whatever the cursor says,
     *  so Return never answers a card before its picture is shown; a
     *  crop taller than the window is seen when it fills half of it. */
    function seen(node) {
        var rect = (node.querySelector('.bk-cropwrap') || node).getBoundingClientRect();
        if (!(rect.height > 0)) { return false; }
        var shown = Math.min(rect.bottom, window.innerHeight) - Math.max(rect.top, 0);
        return shown >= Math.min(rect.height, window.innerHeight) / 2;
    }

    /** The card the keys act on: the active one while the reader can
     *  see it, else the one in view. The page never scrolls on its
     *  own after an answer, so the cursor moves on to a card that may
     *  sit below the fold, and a key then acts on what is in front of
     *  the reader, never on a card out of sight. */
    function keyCard() {
        var active = activeCard();
        return active && seen(active) ? active : cardInView();
    }

    /** The card whose middle is nearest the middle of the window. */
    function cardInView() {
        var mid = window.innerHeight / 2;
        var best = null;
        var bestDistance = Infinity;
        allCards().forEach(function (node) {
            var rect = node.getBoundingClientRect();
            var distance = Math.abs((rect.top + rect.bottom) / 2 - mid);
            if (distance < bestDistance) { best = node; bestDistance = distance; }
        });
        return best;
    }

    /** The next open card takes the cursor, and the page stays where
     *  it is: a scroll after every answer moved the cards under the
     *  pointer, and the reader's next click landed on the wrong one.
     *  The reader scrolls, by hand or with the arrow keys. */
    function focusNextCard() {
        var list = allCards();
        var at = list.indexOf(activeCard());
        var next = null;
        for (var i = at + 1; i < list.length && !next; i++) {
            if (isOpen(list[i])) { next = list[i]; }
        }
        for (i = 0; i <= at && i < list.length && !next; i++) {
            if (isOpen(list[i])) { next = list[i]; }
        }
        setActive(next);
    }

    function step(delta) {
        var list = allCards();
        if (!list.length) { return; }
        var at = list.indexOf(activeCard());
        var next = list[Math.max(0, Math.min(list.length - 1, at + delta))];
        setActive(next);
        next.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }

    function onKey(event) {
        if (event.target && /INPUT|TEXTAREA|SELECT/.test(event.target.tagName)) { return; }
        if (event.metaKey || event.ctrlKey || event.altKey) { return; }
        var drawing = root.querySelector('.cr-layer.cr-drawing');
        if (drawing && event.key === 'Escape') {
            event.preventDefault();
            drawing.closest('.bk-card').querySelector('[data-key="d"]').click();
            return;
        }
        var editing = root.querySelector('.cr-layer.cr-editing');
        if (editing && event.key === 'Escape') {
            event.preventDefault();
            editing.closest('.bk-card').querySelector('[data-role="edit-done"]').click();
            return;
        }
        var opened = root.querySelector('.bk-card.cr-expanded');
        if (opened && (event.key === 'Escape' || event.key === 'f' || event.key === 'F')) {
            event.preventDefault();
            opened.querySelector('.cr-full-btn').click();
            return;
        }
        if (opened && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) { return; }
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            event.preventDefault();
            setActive(keyCard());
            step(event.key === 'ArrowDown' ? 1 : -1);
            return;
        }
        var key = event.key === 'Enter' ? 'enter' : String(event.key).toLowerCase();
        if (!KEY_NAMES[key]) { return; }
        var card = keyCard();
        if (key === 'u' && lastEdit()) {
            // A box edit was the last thing done: u takes it back.
            event.preventDefault();
            undoEdit();
            return;
        }
        if (key === 'u' && !(card && card.querySelector('[data-key="u"]')) && history.length) {
            // The cursor moved on after the answer: u takes back the
            // last answer, wherever it is on the page.
            card = root.querySelector('.bk-card[data-key="' + history[history.length - 1] + '"]');
            if (card) { card.scrollIntoView({ behavior: 'smooth', block: 'center' }); }
        }
        if (!card) { return; }
        setActive(card);
        var btn = card.querySelector('[data-key="' + key + '"]');
        if (btn && !btn.disabled && !btn.hidden) {
            event.preventDefault();
            btn.click();
        }
    }

    document.addEventListener('DOMContentLoaded', function () {
        root = document.getElementById('card-review');
        if (!root) { return; }
        cfg = JSON.parse(document.getElementById('card-review-config').textContent);
        if (window.pdfjsLib) {
            pdfjsLib.GlobalWorkerOptions.workerSrc =
                'https://cdn.jsdelivr.net/npm/pdfjs-dist@3.11.174/build/pdf.worker.min.js';
        }
        observer = new IntersectionObserver(function (entries) {
            entries.forEach(function (entry) {
                if (!entry.target.isConnected) { observer.unobserve(entry.target); return; }
                if (entry.isIntersecting) {
                    drawCrop(entry.target._card, entry.target);
                } else {
                    releaseCrop(entry.target._card, entry.target);
                }
            });
        }, { rootMargin: '800px 0px' });
        document.addEventListener('keydown', onKey);
        root.querySelector('[data-role="reload"]').addEventListener('click', function () { load(); });
        root.querySelector('[data-role="undo-edit"]').addEventListener('click', function () { undoEdit(); });
        root.querySelector('[data-role="recompute"]').addEventListener('click', function (event) {
            recompute(event.currentTarget);
        });
        load().then(function () {
            if (!activeCard()) { focusNextCard(); }
        });
    });
})();
