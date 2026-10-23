/**
 * The blocking review: every open blocking card of one volume, one
 * after the next.
 *
 * One section per opinion (opinion_blocking_review.html). The script
 * asks the cards endpoint for each opinion's cards, draws the crop of
 * each from the redacted PDF with pdf.js, and answers a card through
 * the review page's own endpoints: a chosen or typed word is a text
 * edit of the block (the one "Edit text" writes), a "looks right" is
 * the dismissal of the page's card, and the approval is the approval.
 * After every write the cards of the opinion are asked for again, so
 * what the page shows is always the stamped document and its two
 * revisions, which every write names.
 *
 * The crop is an estimate: no engine draws a box below the block, so
 * the server guesses the line from the word's place in the text
 * (blocking_review.crop_of), and the highlight is widened over it.
 *
 * Keys: ArrowDown and ArrowUp move down and up the rows of the card in
 * view, Return chooses the focused row; in the typed row, Return sends
 * what was typed.
 */
(function () {
    'use strict';

    /** The PDF page is drawn at this many pixels per point. */
    var SCALE = 2;

    var root = null;
    /** The level the page walks: 'blocking', or 'warning' for the
     *  warnings review, which gates nothing (``data-level``). */
    var level = 'blocking';
    var sections = [];

    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) { node.className = className; }
        if (text !== undefined && text !== null) { node.textContent = text; }
        return node;
    }

    function note(text, tone) {
        return el('p', 'bk-note' + (tone ? ' bk-' + tone : ''), text);
    }

    /** A rejected request answers like a refused one, so a chain of
     *  writes goes on past it and the page shows the reason. */
    function failed(error) {
        return { ok: false, data: { message: 'The request did not reach the server: ' + error.message } };
    }

    function getJson(url) {
        return fetch(url, { credentials: 'same-origin' }).then(function (response) {
            return response.json().then(function (data) {
                return { ok: response.ok, data: data };
            });
        }).catch(failed);
    }

    function postJson(url, body) {
        return fetch(url, {
            method: 'POST',
            credentials: 'same-origin',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': csrfToken()
            },
            body: JSON.stringify(body || {})
        }).then(function (response) {
            return response.json().catch(function () { return {}; }).then(function (data) {
                return { ok: response.ok, data: data };
            });
        }).catch(failed);
    }

    function fold(text) {
        return text.replace(/\s+/g, ' ').trim();
    }

    /** The CSRF token of the page, from the field the template renders. */
    function csrfToken() {
        var field = document.querySelector('[name=csrfmiddlewaretoken]');
        return field ? field.value : '';
    }

    /** The key a card keeps across reloads: its kind, page, block,
     *  offset and span. A gap card starts where the word after it
     *  starts, so the span tells the two apart; the finding stays
     *  last, the Undo of a dismissal reads it there. */
    function keyOf(card) {
        return [card.kind, card.page_in_opinion, card.group_id, card.start,
            card.gap ? 'gap' : (card.length || 0), card.finding_pk].join(':');
    }

    // ── one opinion ──────────────────────────────────────────────────
    function Section(node) {
        this.node = node;
        this.cardsUrl = node.dataset.cardsUrl;
        this.pdfUrlEndpoint = node.dataset.pdfUrlEndpoint;
        this.editTextUrl = node.dataset.editTextUrl;
        this.editDropUrl = node.dataset.editDropUrl;
        this.approveUrl = node.dataset.approveUrl;
        this.withdrawUrl = node.dataset.withdrawUrl;
        this.reviewUrl = node.dataset.reviewUrl;
        /** What was answered here, each with the card it answered and
         *  its undo, the review page's own: the edit withdrawn, the card
         *  reopened. The card stays on the page, greyed, in its place,
         *  with the Undo on it. */
        this.answers = [];
        this.cardsNode = node.querySelector('[data-role="cards"]');
        this.metaNode = node.querySelector('[data-role="meta"]');
        this.approveButton = node.querySelector('[data-role="approve"]');
        this.opinion = null;
        this.cards = [];
        this.kept = {};
        this.skipped = {};
        this.answered = 0;
        this.pdf = null;
        this.pages = {};
        this.approved = false;
        var self = this;
        this.approveButton.addEventListener('click', function () { self.approve(); });
    }

    Section.prototype.load = function () {
        var self = this;
        return getJson(this.cardsUrl).then(function (answer) {
            if (!answer.ok) {
                self.cardsNode.replaceChildren(note(answer.data.message || 'The cards did not load.', 'error'));
                self.cards = [];
                return;
            }
            self.opinion = answer.data.opinion;
            self.cards = answer.data.cards;
            // The open blocking cards, whatever this page walks: the
            // approval stays behind the gate.
            self.blockingOpen = answer.data.blocking_open || 0;
            self.render();
        }).catch(function (error) {
            self.cardsNode.replaceChildren(note('The cards did not load: ' + error.message, 'error'));
        });
    };

    /** The cards a person has not answered or kept. */
    Section.prototype.open = function () {
        var self = this;
        return this.cards.filter(function (card) { return !self.kept[keyOf(card)]; });
    };

    Section.prototype.render = function () {
        var self = this;
        var nodes = [];
        if (this.approved) {
            nodes.push(note('Approved.', 'ok'));
        } else if (!this.cards.length) {
            nodes.push(note('No blocking card is open. The text can be approved.', 'ok'));
        } else {
            // The open cards and the answered ones together, in the
            // order of the page: an answered card keeps its place.
            var entries = this.cards.map(function (card) { return { card: card, answer: null }; })
                .concat(this.answers.map(function (answer) { return { card: answer.card, answer: answer }; }));
            entries.sort(function (a, b) {
                return (a.card.page_in_opinion - b.card.page_in_opinion)
                    || ((a.card.start || 0) - (b.card.start || 0));
            });
            entries.forEach(function (entry, index) {
                nodes.push(cardNode(self, entry.card, index + 1, entries.length, entry.answer));
            });
        }
        this.cardsNode.replaceChildren.apply(this.cardsNode, nodes);
        this.metaNode.textContent = (this.opinion ? this.opinion.page_count + ' pages · ' : '')
            + this.open().length + ' card(s) open';
        this.approveButton.disabled = this.approved || this.cards.length > 0
            || (level === 'warning' && this.blockingOpen > 0);
        this.cards.concat(this.answers.map(function (a) { return a.card; })).forEach(function (card) {
            if (card.kind !== 'link' && card.crop) { drawCrop(self, card); }
        });
        countAll();
    };

    Section.prototype.reload = function () {
        return this.load().then(function () { focusNextCard(); });
    };

    /** Write the text of a block, the edit "Edit text" writes. */
    Section.prototype.write = function (card, text, node) {
        var self = this;
        var state = node.querySelector('.bk-state');
        state.textContent = 'writing…';
        return postJson(this.editTextUrl, {
            page_in_opinion: card.page_in_opinion,
            group_id: card.group_id,
            text: text,
            // The span judged: a word card answers its word, and the
            // block's other open words stay open; a block card
            // answers the block.
            span: card.kind === 'word'
                ? [card.start, card.start + card.length] : null,
            glue_revision: this.opinion.glue_revision,
            edit_revision: this.opinion.edit_revision
        }).then(function (answer) {
            if (!answer.ok) {
                state.textContent = answer.data.message || 'The edit was refused.';
                state.classList.add('bk-error');
                return;
            }
            self.answered += 1;
            if (answer.data.edit_id) {
                self.answers.push({
                    card: card,
                    label: 'written as "' + shorten(text) + '"',
                    undo: { url: self.withdrawUrl, body: { edit_id: answer.data.edit_id } }
                });
            }
            return self.reload();
        });
    };

    /** Take the whole block out of the text: it is not text of the
     *  opinion (the bleed-through of the page behind, a stray mark).
     *  The ``DROP`` edit of the review page, with the same Undo. */
    Section.prototype.drop = function (card, node) {
        var self = this;
        var state = node.querySelector('.bk-state');
        state.textContent = 'taking the block out…';
        return postJson(this.editDropUrl, {
            page_in_opinion: card.page_in_opinion,
            group_id: card.group_id,
            glue_revision: this.opinion.glue_revision,
            edit_revision: this.opinion.edit_revision
        }).then(function (answer) {
            if (!answer.ok) {
                state.textContent = answer.data.message || 'The edit was refused.';
                state.classList.add('bk-error');
                return;
            }
            self.answered += 1;
            if (answer.data.edit_id) {
                self.answers.push({
                    card: card,
                    label: 'taken out of the text as not text',
                    undo: { url: self.withdrawUrl, body: { edit_id: answer.data.edit_id } }
                });
            }
            return self.reload();
        });
    };

    /** Dismiss the page's card: what is shown is right. */
    Section.prototype.dismiss = function (card, node) {
        var self = this;
        var state = node.querySelector('.bk-state');
        if (!card.dismiss_url) {
            state.textContent = 'This card has no dismissal here; open the document.';
            return Promise.resolve();
        }
        state.textContent = 'dismissing the card…';
        return postJson(card.dismiss_url, {}).then(function (answer) {
            if (!answer.ok) {
                state.textContent = answer.data.message || 'The dismissal was refused.';
                state.classList.add('bk-error');
                return;
            }
            self.answered += 1;
            if (card.restore_url) {
                self.answers.push({
                    card: card,
                    label: 'kept as shown, the page\'s card closed',
                    undo: { url: card.restore_url, body: {} },
                    finding_pk: card.finding_pk
                });
            }
            return self.reload();
        });
    };

    /** Take one answer back: the review page's own undo. The cards are
     *  asked for again afterwards, the rule of every write here. */
    Section.prototype.undo = function (entry) {
        var self = this;
        var index = this.answers.indexOf(entry);
        if (index >= 0) { this.answers.splice(index, 1); }
        if (entry.finding_pk) {
            // The card reopens with every word of its page: none is kept.
            Object.keys(this.kept).forEach(function (key) {
                if (key.split(':').pop() === String(entry.finding_pk)) { delete self.kept[key]; }
            });
        }
        return postJson(entry.undo.url, entry.undo.body).then(function (answer) {
            if (!answer.ok) {
                self.answers.push(entry);
                self.cardsNode.insertBefore(note(answer.data.message || 'The undo was refused.', 'error'), self.cardsNode.firstChild);
                return;
            }
            self.answered = Math.max(0, self.answered - 1);
            return self.reload();
        });
    };

    /** Release a card kept or skipped here, before anything was
     *  written: it is open again. */
    Section.prototype.release = function (card) {
        delete this.kept[keyOf(card)];
        delete this.skipped[keyOf(card)];
        this.render();
        countAll();
        var node = this.cardsNode.querySelector('[data-key="' + keyOf(card) + '"]');
        if (node) { setActive(node); }
    };

    /** A card kept as shown: the word, the block or the single-engine
     *  block reads right. The page's finding counts every such card
     *  of the page (one ``NO_MAJORITY`` row for its word and block
     *  cards, one ``SINGLE_ENGINE`` row for its single cards), so it
     *  is dismissed once every card of that finding is kept, never
     *  before: a dismissal closes them all. */
    Section.prototype.keep = function (card, node) {
        var self = this;
        this.kept[keyOf(card)] = true;
        node.classList.add('bk-done');
        node.querySelector('.bk-state').textContent = 'kept as shown';
        var siblings = this.cards.filter(function (other) {
            return other.finding_pk === card.finding_pk;
        });
        var allKept = siblings.every(function (other) { return self.kept[keyOf(other)]; });
        if (allKept) { return this.dismiss(card, node); }
        node.querySelector('.bk-state').textContent = 'kept as shown; the page\'s card closes once its other cards are answered';
        this.render();
        countAll();
        focusNextCard();
        return Promise.resolve();
    };

    function shorten(text) {
        return text.length > 60 ? text.slice(0, 57) + '…' : text;
    }

    /** Whether the opinion can be approved from here: loaded, not
     *  approved yet, and no card left, link cards included. */
    Section.prototype.ready = function () {
        return !!this.opinion && !this.approved && this.cards.length === 0
            && (level !== 'warning' || this.blockingOpen === 0);
    };

    Section.prototype.approve = function () {
        var self = this;
        if (!this.opinion) { return Promise.resolve(); }
        this.approveButton.disabled = true;
        return postJson(this.approveUrl, {
            glue_revision: this.opinion.glue_revision,
            edit_revision: this.opinion.edit_revision
        }).then(function (answer) {
            if (!answer.ok) {
                self.cardsNode.replaceChildren(note(answer.data.message || 'The approval was refused.', 'error'));
                self.approveButton.disabled = false;
                return;
            }
            self.approved = true;
            self.node.classList.add('bk-approved');
            self.cardsNode.replaceChildren(note(answer.data.message || 'Approved.', 'ok'));
            self.metaNode.textContent = 'approved';
            countAll();
            focusNextCard();
        });
    };

    // ── the ready list ───────────────────────────────────────────────
    /** One opinion of the filter with nothing open, listed under the
     *  cards: the server rendered its approval address and the two
     *  revisions of its text. */
    function Ready(node) {
        this.node = node;
        this.approveUrl = node.dataset.approveUrl;
        this.glueRevision = parseInt(node.dataset.glueRevision, 10);
        this.editRevision = parseInt(node.dataset.editRevision, 10);
        this.state = node.querySelector('[data-role="state"]');
        this.button = node.querySelector('[data-role="approve-one"]');
        this.approved = false;
        var self = this;
        this.button.addEventListener('click', function () { self.approve().then(countAll); });
    }

    Ready.prototype.ready = function () { return !this.approved; };

    Ready.prototype.approve = function () {
        var self = this;
        this.button.disabled = true;
        this.state.textContent = 'approving…';
        return postJson(this.approveUrl, {
            glue_revision: this.glueRevision,
            edit_revision: this.editRevision
        }).then(function (answer) {
            if (!answer.ok) {
                self.state.textContent = answer.data.message || 'The approval was refused.';
                self.state.classList.add('bk-error');
                self.button.disabled = false;
                return;
            }
            self.approved = true;
            self.node.classList.add('bk-approved');
            self.state.textContent = 'approved';
            self.state.classList.add('bk-ok');
        });
    };

    var readyRows = [];

    /** Approve every section and every ready row that is ready, one
     *  after the next: one request at a time, each judged by the
     *  server's own gate. */
    function approveAllReady() {
        var button = root.querySelector('[data-role="approve-all"]');
        var ready = sections.filter(function (section) { return section.ready(); })
            .concat(readyRows.filter(function (row) { return row.ready(); }));
        if (button) { button.disabled = true; }
        ready.reduce(function (chain, item) {
            return chain.then(function () { return item.approve(); });
        }, Promise.resolve()).then(countAll);
    }

    // ── the PDF ──────────────────────────────────────────────────────
    Section.prototype.getPdf = function () {
        if (!this.pdf) {
            this.pdf = getJson(this.pdfUrlEndpoint).then(function (answer) {
                if (!answer.ok || !answer.data.url) {
                    throw new Error(answer.data.message || 'no PDF');
                }
                return pdfjsLib.getDocument({ url: answer.data.url }).promise;
            });
        }
        return this.pdf;
    };

    /** The whole page drawn once at SCALE, cached per section. */
    Section.prototype.getPage = function (number) {
        if (!this.pages[number]) {
            this.pages[number] = this.getPdf().then(function (pdf) {
                return pdf.getPage(number);
            }).then(function (page) {
                var viewport = page.getViewport({ scale: SCALE });
                var canvas = document.createElement('canvas');
                canvas.width = Math.ceil(viewport.width);
                canvas.height = Math.ceil(viewport.height);
                return page.render({ canvasContext: canvas.getContext('2d'), viewport: viewport }).promise
                    .then(function () { return canvas; });
            });
        }
        return this.pages[number];
    };

    function drawCrop(section, card) {
        var wrap = section.cardsNode.querySelector('[data-key="' + keyOf(card) + '"] .bk-cropwrap');
        if (!wrap) { return; }
        var canvas = wrap.querySelector('canvas');
        var box = card.crop.crop;
        section.getPage(card.page_in_opinion + 1).then(function (pageCanvas) {
            // The whole page when the card is opened (f): the crop is a
            // guess at the line, and the page says where the guess sits.
            if (card.full) { box = [0, 0, pageCanvas.width / SCALE, pageCanvas.height / SCALE]; }
            var x0 = Math.max(0, Math.floor(box[0] * SCALE));
            var y0 = Math.max(0, Math.floor(box[1] * SCALE));
            var x1 = Math.min(pageCanvas.width, Math.ceil(box[2] * SCALE));
            var y1 = Math.min(pageCanvas.height, Math.ceil(box[3] * SCALE));
            var width = Math.max(1, x1 - x0);
            var height = Math.max(1, y1 - y0);
            canvas.width = width;
            canvas.height = height;
            var context = canvas.getContext('2d');
            context.drawImage(pageCanvas, x0, y0, width, height, 0, 0, width, height);
            // The blocks of a cover card, outlined where the engines
            // read them: the box is a fact of the document, not a guess.
            (card.boxes || []).forEach(function (b) {
                context.strokeStyle = '#dc2626';
                context.lineWidth = 2;
                context.strokeRect(b[0] * SCALE - x0, b[1] * SCALE - y0,
                    (b[2] - b[0]) * SCALE, (b[3] - b[1]) * SCALE);
            });
            // On the whole page, the crop itself is outlined, so the eye
            // finds the line the card is about.
            if (card.full && !(card.boxes || []).length) {
                var c = card.crop.crop;
                context.strokeStyle = '#2f5d8a';
                context.lineWidth = 2;
                context.strokeRect(c[0] * SCALE - x0, c[1] * SCALE - y0,
                    (c[2] - c[0]) * SCALE, (c[3] - c[1]) * SCALE);
            }
        }).catch(function (error) {
            wrap.replaceChildren(note('The scan did not load: ' + error.message, 'error'));
        });
    }

    // ── the cards ────────────────────────────────────────────────────
    var ENGINE_ROWS = ['r0', 'r1', 'r2'];

    function cardNode(section, card, index, total, answer) {
        // An answered or kept card is never the open one: it is greyed
        // and the next card is the reader's.
        if (card.full && (answer || section.kept[keyOf(card)])) { card.full = false; }
        var node = el('article', 'bk-card bk-' + card.kind + (card.kind === 'block' ? ' bk-single' : '')
            + (card.full ? ' cr-expanded' : ''));
        node.dataset.key = keyOf(card);
        if (answer) { node.classList.add('bk-done', 'bk-answered'); }
        var header = el('header');
        header.appendChild(el('span', 'bk-step', 'Fix ' + index + ' of ' + total));
        header.appendChild(el('span', 'bk-where', card.where + ' · ' + describe(card)));
        if (card.kind !== 'link' && card.crop) {
            // The whole page and back, as the step-2 card review has it
            // (f, Esc back): a crop is a guess at the line, and the
            // page is where a reader checks the guess.
            var full = el('button', 'cr-full-btn', card.full ? 'Back to the crop (Esc)' : 'Full page');
            full.type = 'button';
            full.dataset.key = 'f';
            full.title = 'Key: f';
            full.addEventListener('click', function (event) {
                event.stopPropagation();
                // In place, on this node alone: a render would rebuild
                // every card of the opinion and wipe what is typed.
                card.full = !card.full;
                node.classList.toggle('cr-expanded', card.full);
                var cropWrap = node.querySelector('.bk-cropwrap');
                if (cropWrap) {
                    cropWrap.classList.toggle('cr-cropwrap', card.full);
                    cropWrap.classList.toggle('cr-fullpage', card.full);
                }
                full.textContent = card.full ? 'Back to the crop (Esc)' : 'Full page';
                drawCrop(section, card);
                setActive(node);
            });
            header.appendChild(full);
        }
        node.appendChild(header);
        if (card.kind === 'link') {
            node.appendChild(note(card.message));
        } else if (card.kind === 'cover') {
            // The redacted page around the blocks a box covers in part,
            // each outlined: a word that shows past the box is a
            // redaction drawn short.
            var coverWrap = el('div', 'bk-cropwrap' + (card.full ? ' cr-cropwrap cr-fullpage' : ''));
            coverWrap.appendChild(el('canvas'));
            node.appendChild(coverWrap);
            node.appendChild(note(card.message));
        } else {
            // The crop alone, no highlight: the line is a guess from the
            // word's place in the block's text, and a box drawn on a
            // guess points at the wrong words more often than not.
            var wrap = el('div', 'bk-cropwrap' + (card.full ? ' cr-cropwrap cr-fullpage' : ''));
            wrap.appendChild(el('canvas'));
            node.appendChild(wrap);
            if (card.kind === 'word') { node.appendChild(snippet(card)); }
            // An answered card shows no rows: its answer is written,
            // and the Undo below is the way to another one.
            if (!answer) { node.appendChild(stack(section, card, node)); }
        }
        var footer = el('footer');
        var kept = section.kept[keyOf(card)];
        var skipped = section.skipped[keyOf(card)];
        var state = el('span', 'bk-state',
            answer ? answer.label : (kept ? 'kept as shown' : (skipped ? 'skipped' : 'not yet decided')));
        footer.appendChild(state);
        footer.appendChild(el('span', 'bk-grow'));
        var open = el('a', 'btn-ghost text-xs', 'Open in the document');
        // The review page names its page containers ``op-page-{index}``,
        // 0-based, and jumps to the one in the hash (viewer_step3.js).
        open.href = section.reviewUrl + '#op-page-' + card.page_in_opinion;
        footer.appendChild(open);
        if (answer) {
            var back = el('button', 'btn-outline text-xs', 'Undo');
            back.type = 'button';
            back.title = answer.finding_pk
                ? 'Reopen the card of this page'
                : 'Withdraw the edit: the block reads as the engines left it';
            back.addEventListener('click', function () { back.disabled = true; section.undo(answer); });
            footer.appendChild(back);
        } else if (kept || skipped) {
            var release = el('button', 'btn-outline text-xs', 'Undo');
            release.type = 'button';
            release.title = 'Open this card again';
            release.addEventListener('click', function () { section.release(card); });
            footer.appendChild(release);
        } else if (card.kind === 'cover' && card.dismiss_url) {
            var covered = el('button', 'btn-outline text-xs', 'Looks right');
            covered.type = 'button';
            covered.title = 'Dismiss this card: no word shows past the'
                + ' redaction in the outlined block(s)';
            covered.addEventListener('click', function () {
                covered.disabled = true;
                section.dismiss(card, node);
            });
            footer.appendChild(covered);
        } else if (card.kind === 'link' && card.dismiss_url) {
            // A card that only points at the page: once the page was
            // looked at, the dismissal closes it, with the Undo of
            // every dismissal.
            var fine = el('button', 'btn-outline text-xs', 'Looks good');
            fine.type = 'button';
            fine.title = 'Dismiss this card: the page was looked at and'
                + ' reads right';
            fine.addEventListener('click', function () {
                fine.disabled = true;
                section.dismiss(card, node);
            });
            footer.appendChild(fine);
        } else if (card.kind !== 'link') {
            // The plain way to bless a card: the text stays as shown,
            // and the page's card closes once every open word of the
            // page is kept, the rule of ``keep``.
            var right = el('button', 'btn-outline text-xs', 'Looks good');
            right.type = 'button';
            right.title = 'Keep the text as shown; the page\'s card closes'
                + ' once every open word of the page is answered';
            right.addEventListener('click', function () {
                right.disabled = true;
                section.keep(card, node);
            });
            footer.appendChild(right);
            var junk = el('button', 'btn-outline text-xs', 'Not text');
            junk.type = 'button';
            junk.title = 'Take the whole block out of the text: it is not'
                + ' text of the opinion (the bleed-through of the page'
                + ' behind, a stray mark)';
            junk.addEventListener('click', function () {
                if (!window.confirm('Take this whole block out of the text?'
                        + ' Every card of the block closes with it; the'
                        + ' Undo puts it back.')) { return; }
                junk.disabled = true;
                section.drop(card, node);
            });
            footer.appendChild(junk);
            var skip = el('button', 'btn-outline text-xs', 'Skip');
            skip.type = 'button';
            skip.addEventListener('click', function () {
                section.skipped[keyOf(card)] = true;
                section.render();
                focusNextCard();
            });
            footer.appendChild(skip);
        }
        node.appendChild(footer);
        if (kept || skipped) { node.classList.add('bk-done'); }
        return node;
    }

    function describe(card) {
        if (card.level === 'warning') {
            if (card.kind === 'word' && card.gap) {
                return 'one engine read words here that the others did not: keep the gap or put them in';
            }
            if (card.kind === 'word') {
                return 'a majority settled this word over one engine: bless it or pick the other reading';
            }
            if (card.kind === 'block') {
                return (card.silent && card.silent.length
                    ? card.silent.join(', ') + ' read nothing here'
                    : 'the engines did not all read this block alike')
                    + ': keep it as shown or pick a reading';
            }
        }
        if (card.kind === 'word' && card.gap) { return 'no two engines agree on what is in this gap'; }
        if (card.kind === 'word') { return 'no two engines agree on a word'; }
        if (card.kind === 'block') {
            return (card.table ? 'a table' : 'a block') + ' with ' + card.open
                + ' word(s) no two engines agree on: pick a reading';
        }
        if (card.kind === 'single') { return 'one engine alone read this block (' + card.engine + ')'; }
        if (card.kind === 'cover') {
            return 'a box covers part of the outlined block(s): does any word show past it?';
        }
        return card.check_label || card.check;
    }

    function snippet(card) {
        var p = el('p', 'bk-snippet');
        p.appendChild(document.createTextNode('… ' + card.before + ' '));
        if (card.gap) {
            // A gap between two words: some engine read words here
            // that the shown engine did not.
            var gap = el('mark', 'bk-gap', '[ ]');
            gap.title = 'A gap: an engine read words here that the shown engine did not';
            p.appendChild(gap);
        } else {
            p.appendChild(el('mark', null, card.token));
        }
        p.appendChild(document.createTextNode(' ' + card.after + ' …'));
        return p;
    }

    function stack(section, card, node) {
        var list = el('div', 'bk-stack');
        if (card.kind === 'word') {
            card.readings.forEach(function (reading, index) {
                list.appendChild(option(section, card, node, reading.engine, reading.word, ENGINE_ROWS[index] || 'r2', card.before, card.after));
            });
        } else if (card.kind === 'single') {
            var row = option(section, card, node, card.engine, card.text, 'r0', '', '');
            row.title = 'The block reads right as shown: dismiss the card of this page';
            list.appendChild(row);
            if (card.table) {
                // A table takes no text edit (the review page's rule).
                list.appendChild(note('A table takes no text edit here; open the document to change it.'));
                return list;
            }
        } else if (card.kind === 'block') {
            card.readings.forEach(function (reading, index) {
                var whole = option(section, card, node, reading.engine, reading.text, ENGINE_ROWS[index] || 'r2', '', '');
                if (card.table && fold(reading.text) !== fold(card.text)) {
                    // A table takes no text edit (the review page's rule):
                    // the shown reading can be kept, another cannot be chosen.
                    whole.disabled = true;
                    whole.title = 'A table takes no text edit here; open the document.';
                } else {
                    whole.title = fold(reading.text) === fold(card.text)
                        ? 'The block reads right as shown: dismiss the card of this page'
                        : 'Write this reading as the text of the block';
                }
                list.appendChild(whole);
            });
            if (card.table) { return list; }
        }
        list.appendChild(typedRow(section, card, node));
        return list;
    }

    /** One engine's row. For a word card the value is the word; for a
     *  single block it is the whole text, and choosing it is "looks
     *  right". */
    function option(section, card, node, engine, value, rowClass, before, after) {
        var button = el('button', 'bk-opt ' + rowClass);
        button.type = 'button';
        button.appendChild(el('span', 'bk-who', engine));
        var val = el('span', 'bk-val');
        // The reading that is in the text now, marked with a tick in
        // the warnings review alone: there a majority chose it. On a
        // blocking card nothing chose it, it stands by the engines'
        // rank, so no row carries a tick and the choice is the
        // person's. Choosing the shown reading keeps the text as shown.
        var inText = card.gap ? value === '' : value !== '' && (card.kind === 'word'
            ? fold(value) === fold(card.token)
            : fold(value) === fold(card.text));
        var shown = inText && level !== 'blocking';
        if (value === '') {
            val.appendChild(el('span', 'bk-nothing', 'read nothing here'));
        } else {
            if (before) { val.appendChild(el('span', 'bk-ctx', lastWords(before, 1) + ' ')); }
            val.appendChild(document.createTextNode(value));
            if (after) { val.appendChild(el('span', 'bk-ctx', ' ' + firstWords(after, 1))); }
        }
        if (shown) {
            button.classList.add('bk-shown');
            button.title = 'This reading is in the text now; choosing it keeps the text as shown';
            val.appendChild(el('span', 'bk-shown-tag', '\u2713'));
        }
        button.appendChild(val);
        button.addEventListener('click', function () {
            markChosen(node, button);
            if (card.kind === 'single' || (card.kind === 'block' && fold(value) === fold(card.text))
                    || (card.gap && value === '')) {
                // Right as shown (a gap kept empty): the page's card
                // waits for its siblings.
                section.keep(card, node);
                return;
            }
            choose(section, card, node, value);
        });
        return button;
    }

    function typedRow(section, card, node) {
        var label = el('label', 'bk-opt bk-typed');
        label.appendChild(el('span', 'bk-who', 'you'));
        var input = el('input');
        input.type = 'text';
        input.placeholder = card.gap
            ? 'Type the words the page has in the gap and press Return'
            : card.kind === 'word'
                ? 'Type what the page says and press Return'
                : 'Type the whole block as the page has it and press Return';
        input.addEventListener('keydown', function (event) {
            if (event.key !== 'Enter') { return; }
            answeredByMouse = false;
            event.preventDefault();
            var value = fold(input.value);
            if (!value && (card.kind === 'single' || card.gap)) { return; }
            markChosen(node, label);
            choose(section, card, node, value);
        });
        label.appendChild(input);
        return label;
    }

    function markChosen(node, chosen) {
        node.querySelectorAll('.bk-opt').forEach(function (other) { other.classList.remove('bk-chosen'); });
        chosen.classList.add('bk-chosen');
    }

    /** Apply a value to a card: the block's text with the word replaced
     *  (a word card), or the typed text (a single block). A value that
     *  leaves the text as shown keeps the card. */
    function choose(section, card, node, value) {
        var text;
        if (card.kind === 'word' && card.gap) {
            // The words go into the gap; the text around it stays.
            text = fold(card.text.slice(0, card.start) + ' ' + value + ' ' + card.text.slice(card.start));
        } else if (card.kind === 'word') {
            text = fold(card.text.slice(0, card.start) + value + card.text.slice(card.start + card.length));
        } else {
            // A single block or a whole block: the value is the text.
            text = fold(value);
        }
        if (text === fold(card.text)) {
            section.keep(card, node);
            return;
        }
        section.write(card, text, node);
    }

    function lastWords(text, n) { return text.split(' ').slice(-n).join(' '); }
    function firstWords(text, n) { return text.split(' ').slice(0, n).join(' '); }

    // ── the page ─────────────────────────────────────────────────────
    function countAll() {
        var open = 0, answered = 0;
        sections.forEach(function (section) {
            open += section.open().length;
            answered += section.answered;
        });
        var total = root.querySelector('[data-role="total"]');
        var done = root.querySelector('[data-role="done"]');
        if (total) { total.textContent = String(open); }
        if (done) { done.textContent = String(answered); }
        var ready = sections.filter(function (section) { return section.ready(); }).length
            + readyRows.filter(function (row) { return row.ready(); }).length;
        var readyNode = root.querySelector('[data-role="ready"]');
        var approveAll = root.querySelector('[data-role="approve-all"]');
        if (readyNode) { readyNode.textContent = String(ready); }
        if (approveAll) { approveAll.disabled = ready === 0; }
    }

    function openCards() {
        return Array.prototype.slice.call(root.querySelectorAll('.bk-card:not(.bk-done)'));
    }

    function cardInView() {
        var middle = window.innerHeight / 2;
        var best = null, distance = Infinity;
        openCards().forEach(function (card) {
            var rect = card.getBoundingClientRect();
            var gap = Math.abs((rect.top + rect.bottom) / 2 - middle);
            if (gap < distance) { best = card; distance = gap; }
        });
        return best;
    }

    /** Where the curator is: the section and the place of the active
     *  card in it. It outlives the card, which a reload redraws and an
     *  answer takes out, so the next card is the one below it and the
     *  page never jumps back to its first open card. */
    var cursor = null;

    function focusNextCard() {
        var all = Array.prototype.slice.call(root.querySelectorAll('.bk-opinion'));
        var start = cursor ? all.indexOf(cursor.section) : 0;
        var next = null;
        for (var s = Math.max(0, start); s < all.length && !next; s++) {
            var cards = Array.prototype.slice.call(all[s].querySelectorAll('.bk-card'));
            var from = (cursor && s === start) ? cursor.index : 0;
            for (var i = from; i < cards.length; i++) {
                if (!cards[i].classList.contains('bk-done')) { next = cards[i]; break; }
            }
        }
        setActive(next);
        if (next && !answeredByMouse) {
            window.setTimeout(function () { next.scrollIntoView({ behavior: 'smooth', block: 'center' }); }, 120);
        }
        answeredByMouse = false;
    }

    function setActive(card) {
        root.querySelectorAll('.bk-card.bk-active').forEach(function (other) { other.classList.remove('bk-active'); });
        if (!card) { return; }
        card.classList.add('bk-active');
        var section = card.closest('.bk-opinion');
        cursor = {
            section: section,
            index: Array.prototype.indexOf.call(section.querySelectorAll('.bk-card'), card)
        };
    }

    function optionsOf(card) {
        return Array.prototype.slice.call(card.querySelectorAll('.bk-opt'));
    }

    function focusOption(card, index) {
        var list = optionsOf(card);
        if (!list.length) { return; }
        index = (index + list.length) % list.length;
        list.forEach(function (option, i) { option.classList.toggle('bk-focus', i === index); });
        card.dataset.focus = String(index);
        var input = list[index].querySelector('input');
        if (input) {
            input.focus();
        } else if (document.activeElement && document.activeElement.tagName === 'INPUT') {
            document.activeElement.blur();
        }
    }

    var TYPING_IN = { INPUT: true, TEXTAREA: true, SELECT: true };

    // Whether the last answer came by mouse. A click answers in place:
    // the reader is where the mouse is, and a scroll under it is a
    // jump. Return moves on to the next open card, as before. Read off
    // the click itself: a real click has a detail count, the click()
    // that Return sends through onKey has none.
    var answeredByMouse = false;

    function onKey(event) {
        // The browser's own shortcuts (Cmd+F, Ctrl+F, Alt+...) are not ours.
        if (event.metaKey || event.ctrlKey || event.altKey) { return; }
        var typing = Boolean(document.activeElement && TYPING_IN[document.activeElement.tagName]);
        var opened = root.querySelector('.bk-card.cr-expanded');
        if (opened && event.key === 'Escape') {
            event.preventDefault();
            opened.querySelector('.cr-full-btn').click();
            return;
        }
        if (!typing && (event.key === 'f' || event.key === 'F')) {
            // f closes the open card, else opens the card in view.
            var target = opened || root.querySelector('.bk-card.bk-active') || cardInView();
            var button = target && target.querySelector('.cr-full-btn');
            if (button) { event.preventDefault(); button.click(); }
            return;
        }
        if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp' && event.key !== 'Enter') { return; }
        if (typing && event.key === 'Enter') { return; }
        var card = root.querySelector('.bk-card.bk-active') || cardInView();
        if (!card) { return; }
        setActive(card);
        var at = card.dataset.focus === undefined ? -1 : Number(card.dataset.focus);
        if (event.key === 'ArrowDown') {
            event.preventDefault();
            focusOption(card, at + 1);
        } else if (event.key === 'ArrowUp') {
            event.preventDefault();
            focusOption(card, at <= 0 ? -1 : at - 1);
        } else if (at >= 0) {
            var option = optionsOf(card)[at];
            if (option && option.tagName === 'BUTTON') { event.preventDefault(); option.click(); }
        }
    }

    document.addEventListener('DOMContentLoaded', function () {
        root = document.getElementById('blocking-review');
        level = (root && root.dataset.level) || 'blocking';
        if (!root) { return; }
        if (window.pdfjsLib) {
            pdfjsLib.GlobalWorkerOptions.workerSrc =
                'https://cdn.jsdelivr.net/npm/pdfjs-dist@3.11.174/build/pdf.worker.min.js';
        }
        sections = Array.prototype.slice.call(root.querySelectorAll('.bk-opinion')).map(function (node) {
            return new Section(node);
        });
        document.addEventListener('keydown', onKey);
        // Before any button's own handler: the answer that follows
        // knows which hand it came from.
        root.addEventListener('click', function (event) { answeredByMouse = event.detail > 0; }, true);
        var approveAll = root.querySelector('[data-role="approve-all"]');
        if (approveAll) { approveAll.addEventListener('click', approveAllReady); }
        readyRows = Array.prototype.slice.call(root.querySelectorAll('.bk-ready')).map(function (node) {
            return new Ready(node);
        });
        countAll();
        // One opinion after the next, so the first cards show at once.
        sections.reduce(function (chain, section) {
            return chain.then(function () { return section.load(); });
        }, Promise.resolve()).then(function () {
            countAll();
            if (!root.querySelector('.bk-card.bk-active')) { setActive(openCards()[0] || null); }
        });
    });
})();
