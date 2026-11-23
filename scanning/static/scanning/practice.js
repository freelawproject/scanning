/* The practice cards of the volunteers' FAQ (volunteer_practice.html):
 * a choice is checked against the card's data-answer and the card says
 * why. No answer is sent anywhere: once every card has been answered,
 * the page tells the server the practice is done (the first claim waits
 * on it). The keys are the card review's. */
(function () {
    'use strict';
    var root = document.getElementById('practice');
    if (!root) { return; }
    var cards = Array.prototype.slice.call(root.querySelectorAll('.bk-card'));
    var doneNode = root.querySelector('[data-role="done"]');

    function fold(text) {
        return (text || '').replace(/\s+/g, ' ').trim().toLowerCase();
    }

    function active() {
        var middle = window.innerHeight / 2;
        var best = null, bestDistance = Infinity;
        cards.forEach(function (card) {
            var box = card.getBoundingClientRect();
            var distance = Math.abs((box.top + box.bottom) / 2 - middle);
            if (distance < bestDistance) { best = card; bestDistance = distance; }
        });
        return best;
    }

    function setActive(card) {
        cards.forEach(function (other) { other.classList.toggle('bk-active', other === card); });
    }

    var reported = root.dataset.practiced === '1';
    function countDone() {
        var n = cards.filter(function (card) { return card.classList.contains('bk-done'); }).length;
        if (doneNode) { doneNode.textContent = String(n); }
        if (n === cards.length && cards.length) { finished(); }
    }

    // Every card answered, right or wrong: the server records the
    // practice once, and the page offers the person's own cards.
    function finished() {
        var box = root.querySelector('[data-role="finished"]');
        var hint = root.querySelector('[data-role="unfinished"]');
        if (box) { box.classList.remove('hidden'); }
        if (hint) { hint.classList.add('hidden'); }
        if (reported || !root.dataset.doneUrl) { return; }
        reported = true;
        var token = root.querySelector('[name=csrfmiddlewaretoken]');
        fetch(root.dataset.doneUrl, {
            method: 'POST',
            headers: { 'X-CSRFToken': token ? token.value : '' },
        }).then(function (r) { if (!r.ok) { reported = false; } })
          .catch(function () { reported = false; });
    }

    /* Judge a choice: the key of a row ('1', '2', '3', '0'), or typed
     * words. The answer is a row key, or typed words to match. */
    function readsNothing(card, key) {
        if (key === '0') { return true; }
        var row = card.querySelector('button.bk-opt[data-choice="' + key + '"]');
        return Boolean(row && row.querySelector('.bk-nothing'));
    }

    function judge(card, choice, typed) {
        var answer = card.dataset.answer;
        var expectTyped = card.dataset.typed;
        var right;
        if (expectTyped !== undefined) {
            right = typed !== undefined && fold(typed) === fold(expectTyped);
        } else if (typed !== undefined) {
            // Typing the right reading's words is right too.
            var row = card.querySelector('[data-choice="' + answer + '"]');
            var value = row ? row.dataset.value : '';
            right = fold(typed) === fold(value);
        } else {
            // "Leave blank" (0) and a reader who read nothing there say the
            // same thing: the place holds no text. Either is right when the
            // answer is one of them.
            right = choice === answer || (readsNothing(card, choice) && readsNothing(card, answer));
        }
        var state = card.querySelector('.bk-state');
        var why = card.querySelector('[data-role="why"]');
        var again = card.querySelector('[data-role="again"]');
        card.classList.add('bk-done');
        card.classList.toggle('bk-answered', right);
        state.textContent = right ? 'Right' : 'Not quite';
        state.style.color = right ? '' : '#b3261e';
        why.classList.remove('hidden');
        why.classList.toggle('bk-ok', right);
        why.classList.toggle('bk-error', !right);
        again.classList.toggle('hidden', right);
        card.querySelectorAll('.bk-opt').forEach(function (row) { row.classList.add('bk-frozen'); });
        countDone();
    }

    function reset(card) {
        card.classList.remove('bk-done', 'bk-answered');
        card.querySelector('.bk-state').textContent = 'not yet decided';
        card.querySelector('.bk-state').style.color = '';
        card.querySelector('[data-role="why"]').classList.add('hidden');
        card.querySelector('[data-role="again"]').classList.add('hidden');
        card.querySelectorAll('.bk-opt').forEach(function (row) { row.classList.remove('bk-frozen', 'bk-chosen'); });
        var input = card.querySelector('.bk-typed input');
        if (input) { input.value = ''; }
        countDone();
    }

    function choose(card, row) {
        if (card.classList.contains('bk-done')) { return; }
        card.querySelectorAll('.bk-opt').forEach(function (other) { other.classList.remove('bk-chosen'); });
        row.classList.add('bk-chosen');
        judge(card, row.dataset.choice);
    }

    cards.forEach(function (card) {
        card.querySelectorAll('button.bk-opt').forEach(function (row) {
            // The value the typed check compares with: the reading's own words.
            var val = row.querySelector('.bk-val');
            var ctx = val ? val.querySelectorAll('.bk-ctx') : [];
            var text = val ? val.textContent : '';
            Array.prototype.forEach.call(ctx, function (c) { text = text.replace(c.textContent, ''); });
            row.dataset.value = row.classList.contains('bk-blank') ? '' : text.trim();
            row.addEventListener('click', function () { setActive(card); choose(card, row); });
        });
        var input = card.querySelector('.bk-typed input');
        if (input) {
            input.addEventListener('focus', function () { setActive(card); });
            input.addEventListener('keydown', function (event) {
                if (event.key !== 'Enter') { return; }
                event.preventDefault();
                if (card.classList.contains('bk-done')) { return; }
                card.querySelectorAll('.bk-opt').forEach(function (other) { other.classList.remove('bk-chosen'); });
                input.parentNode.classList.add('bk-chosen');
                judge(card, 'e', input.value);
            });
        }
        card.querySelector('[data-role="again"]').addEventListener('click', function () { reset(card); });
        card.addEventListener('click', function () { setActive(card); });
    });

    document.addEventListener('keydown', function (event) {
        if (event.target.tagName === 'INPUT') { return; }
        var card = root.querySelector('.bk-card.bk-active') || active();
        if (!card) { return; }
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            var index = cards.indexOf(card) + (event.key === 'ArrowDown' ? 1 : -1);
            if (index < 0 || index >= cards.length) { return; }
            event.preventDefault();
            setActive(cards[index]);
            cards[index].scrollIntoView({ block: 'center', behavior: 'smooth' });
            return;
        }
        if (event.key === 'e') {
            var input = card.querySelector('.bk-typed input');
            if (input && !card.classList.contains('bk-done')) { event.preventDefault(); input.focus(); }
            return;
        }
        var row = card.querySelector('button.bk-opt[data-key="' + event.key + '"]');
        if (row) { event.preventDefault(); choose(card, row); }
    });

    setActive(cards[0]);
})();
