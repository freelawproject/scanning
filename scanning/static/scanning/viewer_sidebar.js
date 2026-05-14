/**
 * Sidebar interactions for the scan process viewer (steps 2-3).
 *
 * Handles opinion navigation, keyboard shortcuts, scroll tracking,
 * detection actions, issue dismissal, and opinion pairing.
 *
 * Expects SCAN_CONFIG global: { csrfToken, docId, step }
 */

var _currentOpinionIdx = -1;
var _opinions = [];

(function () {
    var cfg = window.SCAN_CONFIG;
    if (!cfg || cfg.step < 2) return;

    try {
        _opinions = JSON.parse(
            document.getElementById("opinions-data").textContent
        );
    } catch (e) {}

    // --- Opinion sidebar highlighting ---

    function _highlightSidebarOpinion(idx) {
        var cards = document.querySelectorAll(".opinion-nav");
        cards.forEach(function (c, i) {
            if (i === idx) {
                c.classList.add("selected");
                c.scrollIntoView({ behavior: "smooth", block: "nearest" });
            } else {
                c.classList.remove("selected");
            }
        });
    }

    window.selectOpinion = function (el, pageNum) {
        _currentOpinionIdx = parseInt(el.dataset.index || "0");
        _highlightSidebarOpinion(_currentOpinionIdx);
        if (_opinions.length && _currentOpinionIdx < _opinions.length) {
            var op = _opinions[_currentOpinionIdx];
            if (window.highlightOpinion) {
                window.highlightOpinion(
                    op.caption_page,
                    op.key_page,
                    _currentOpinionIdx
                );
            }
        } else {
            goToPage(pageNum);
        }
    };

    function _clearDim() {
        document
            .querySelectorAll(".opinion-dim-overlay, .opinion-dim")
            .forEach(function (d) {
                d.remove();
            });
    }

    // --- Step 3: view mode ---

    if (cfg.step === 3) {
        var _viewMode = "redacted";
        var _currentOpinionCard = null;

        window.setViewMode = function (mode) {
            _viewMode = mode;
            ["redacted", "masked", "unredacted"].forEach(function (m) {
                var btn = document.getElementById("mode-" + m);
                if (!btn) return;
                if (m === mode) {
                    btn.className =
                        btn.className.replace(
                            /bg-white.*border-gray-\d+[^'"]*/g,
                            ""
                        ) + " bg-blue-600 text-white border-blue-600";
                } else {
                    btn.className =
                        btn.className.replace(
                            /bg-blue-600 text-white border-blue-600/g,
                            ""
                        ) +
                        " bg-white dark:bg-gray-800 text-gray-600 dark:text-gray-300 border-gray-300 dark:border-gray-600";
                }
            });
            if (_currentOpinionCard)
                viewOpinionByMode(_currentOpinionIdx, _currentOpinionCard);
        };

        window.viewOpinionByMode = function (index, el) {
            var pk = el.dataset.opinionPk || "0";
            var variantMap = {
                redacted: "redacted",
                masked: "masked",
                unredacted: "original",
            };
            var variant = variantMap[_viewMode] || "redacted";
            _currentOpinionIdx = index;
            _currentOpinionCard = el;
            window._currentOpinionPk = parseInt(pk);
            window._currentViewMode = _viewMode;
            document
                .querySelectorAll("aside .issue-card")
                .forEach(function (c, i) {
                    c.style.outline = i === index ? "2px solid #2563eb" : "";
                });
            var url = "/opinions/" + pk + "/pdf/" + variant + "/";
            if (window.loadOpinionUrl) {
                window.loadOpinionUrl(url);
            } else if (window.loadOpinion) {
                window.loadOpinion(url);
            }
        };

        window.viewOpinion = function (index, filename) {
            var cards = document.querySelectorAll("aside .issue-card");
            if (cards[index]) viewOpinionByMode(index, cards[index]);
        };
    }

    // --- Visible page helper ---

    function _getVisiblePageNum() {
        var viewer = document.querySelector(".viewer-panel");
        if (!viewer) return -1;
        var vRect = viewer.getBoundingClientRect();
        var pages = viewer.querySelectorAll(".lazy-page");
        for (var i = 0; i < pages.length; i++) {
            var r = pages[i].getBoundingClientRect();
            if (r.bottom > vRect.top && r.top < vRect.bottom) {
                return parseInt(pages[i].dataset.pageNum || "0");
            }
        }
        return -1;
    }

    // --- Keyboard navigation ---

    var cardSelector =
        cfg.step === 3 ? "aside .issue-card" : ".opinion-nav";

    document.addEventListener("keydown", function (e) {
        if (
            e.target.tagName === "INPUT" ||
            e.target.tagName === "TEXTAREA" ||
            e.target.tagName === "SELECT"
        )
            return;
        var cards = document.querySelectorAll(cardSelector);
        if (e.key === "ArrowRight" && cards.length) {
            e.preventDefault();
            _currentOpinionIdx = Math.min(
                _currentOpinionIdx + 1,
                cards.length - 1
            );
            if (_currentOpinionIdx < 0) _currentOpinionIdx = 0;
            cards[_currentOpinionIdx].click();
        } else if (e.key === "ArrowLeft" && cards.length) {
            e.preventDefault();
            _currentOpinionIdx = Math.max(_currentOpinionIdx - 1, 0);
            cards[_currentOpinionIdx].click();
        } else if (e.key === "ArrowDown") {
            e.preventDefault();
            _clearDim();
            var pages = document.querySelectorAll(".lazy-page");
            var viewer = document.querySelector(".viewer-panel");
            if (viewer && pages.length) {
                var vTop = viewer.getBoundingClientRect().top;
                for (var i = 0; i < pages.length; i++) {
                    var diff = pages[i].getBoundingClientRect().top - vTop;
                    if (diff > 5) {
                        viewer.scrollBy({ top: diff, behavior: "smooth" });
                        break;
                    }
                }
            }
        } else if (e.key === "ArrowUp") {
            e.preventDefault();
            _clearDim();
            var pages = document.querySelectorAll(".lazy-page");
            var viewer = document.querySelector(".viewer-panel");
            if (viewer && pages.length) {
                var vTop = viewer.getBoundingClientRect().top;
                for (var i = pages.length - 1; i >= 0; i--) {
                    var diff = pages[i].getBoundingClientRect().top - vTop;
                    if (diff < -5) {
                        viewer.scrollBy({ top: diff, behavior: "smooth" });
                        break;
                    }
                }
            }
        } else if (e.key === "Escape") {
            _clearDim();
            document
                .querySelectorAll(".opinion-nav, .issue-card")
                .forEach(function (c) {
                    c.style.outline = "";
                });
            _currentOpinionIdx = -1;
        }
    });

    // --- Auto-highlight sidebar opinion on scroll ---

    document.addEventListener("DOMContentLoaded", function () {
        var viewer = document.querySelector(".viewer-panel");
        if (!viewer) return;
        var cards = document.querySelectorAll(".opinion-nav");
        if (!cards.length) return;

        function _opinionForPage(pageNum) {
            for (var i = 0; i < cards.length; i++) {
                var cap = parseInt(cards[i].dataset.captionPage || "0");
                var key = parseInt(cards[i].dataset.keyPage || "0");
                if (pageNum >= cap && pageNum <= key) return i;
            }
            return -1;
        }

        var _lastIdx = -1;
        var _timer;
        var _scrollPaused = false;
        window._pauseSidebarScroll = function () {
            _scrollPaused = true;
            clearTimeout(_timer);
            setTimeout(function () { _scrollPaused = false; }, 2000);
        };
        function _onScroll() {
            if (_scrollPaused) return;
            clearTimeout(_timer);
            _timer = setTimeout(function () {
                var viewerRect = viewer.getBoundingClientRect();
                var pages = viewer.querySelectorAll(".lazy-page");
                var bestPageNum = -1;
                for (var i = 0; i < pages.length; i++) {
                    var r = pages[i].getBoundingClientRect();
                    if (
                        r.bottom > viewerRect.top &&
                        r.top < viewerRect.bottom
                    ) {
                        bestPageNum = parseInt(
                            pages[i].dataset.pageNum || "0"
                        );
                        break;
                    }
                }
                if (bestPageNum < 1) return;
                var idx = _opinionForPage(bestPageNum);
                if (idx === _lastIdx) return;
                _lastIdx = idx;
                if (idx >= 0) {
                    _currentOpinionIdx = idx;
                    _highlightSidebarOpinion(idx);
                }
            }, 120);
        }

        viewer.addEventListener("scroll", _onScroll);
        setTimeout(_onScroll, 800);
    });
})();

// --- Global functions (called from template onclick handlers) ---

function goToPage(elOrNum) {
    var pageNum = (typeof elOrNum === "object") ? elOrNum.dataset.page : elOrNum;
    var el =
        document.getElementById("pv-page-" + pageNum) ||
        document.getElementById("page-" + pageNum);
    if (el) {
        el.scrollIntoView({ behavior: "smooth", block: "start" });
        el.style.outline = "3px solid #2563eb";
        setTimeout(function () {
            el.style.outline = "";
        }, 2000);
    }
}

function highlightDetection(el) {
    var d = _getDetectionData(el);
    if (!d) return;

    document.querySelectorAll(".unmatched-highlight").forEach(function (e) {
        e.remove();
    });

    if (window._pauseSidebarScroll) window._pauseSidebarScroll();
    goToPage(d.logicalPage);

    setTimeout(function () {
        var container = document.getElementById("pv-page-" + d.logicalPage);
        if (!container) return;
        var wrapper = container.querySelector(".canvas-wrapper");
        var canvas = container.querySelector(".pdf-canvas");
        if (!wrapper || !canvas) return;

        var sx = canvas.offsetWidth / (d.imgW || 1);
        var sy = canvas.offsetHeight / (d.imgH || 1);

        var div = document.createElement("div");
        div.className = "unmatched-highlight";
        div.style.position = "absolute";
        div.style.left = d.bbox[0] * sx + "px";
        div.style.top = d.bbox[1] * sy + "px";
        div.style.width = (d.bbox[2] - d.bbox[0]) * sx + "px";
        div.style.height = (d.bbox[3] - d.bbox[1]) * sy + "px";
        div.style.border = "3px solid #f59e0b";
        div.style.background = "rgba(245, 158, 11, 0.15)";
        div.style.zIndex = "20";
        div.style.pointerEvents = "none";
        div.style.borderRadius = "2px";
        div.style.animation = "unmatched-pulse 1s ease-in-out 3";
        wrapper.appendChild(div);

        setTimeout(function () {
            div.remove();
        }, 5000);
    }, 400);
}

function approveDetection(btn) {
    var d = _getDetectionData(btn);
    if (!d) return;
    var cfg = window.SCAN_CONFIG;
    btn.disabled = true;
    btn.textContent = "...";
    fetch("/scans/" + cfg.docId + "/approve-detection/", {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": cfg.csrfToken,
        },
        body: JSON.stringify({detection_id: d.detectionId}),
    })
        .then(function (r) {
            return r.json();
        })
        .then(function (data) {
            var row = btn.closest("[data-unmatched-page]");
            row.style.opacity = "0.3";
            row.style.pointerEvents = "none";
            btn.textContent = "\u2713";
            pairOpinions();
        })
        .catch(function () {
            console.error("Failed to approve detection");
            showToast("Failed to approve detection");
        });
}

function deleteUnmatchedDetection(btn) {
    var d = _getDetectionData(btn);
    if (!d) return;
    var cfg = window.SCAN_CONFIG;
    btn.disabled = true;
    btn.textContent = "...";
    fetch("/scans/" + cfg.docId + "/delete-detection/", {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": cfg.csrfToken,
        },
        body: JSON.stringify({detection_id: d.detectionId}),
    })
        .then(function (r) {
            return r.json();
        })
        .then(function (data) {
            var row = btn.closest("[data-unmatched-page]");
            row.style.opacity = "0.3";
            row.style.pointerEvents = "none";
            btn.textContent = "\u2717";
        })
        .catch(function () {
            console.error("Failed to delete detection");
            showToast("Failed to delete detection");
        });
}

function deleteDuplicates(btn) {
    var cfg = window.SCAN_CONFIG;
    var msg = btn.dataset.message;
    var match = msg.match(/\[([0-9, ]+)\]/);
    if (!match) return;
    var pdfPages = match[1].split(",").map(function (s) {
        return parseInt(s.trim());
    });
    var toDelete = pdfPages.slice(1);
    if (
        !confirm(
            "Delete duplicate PDF page(s) " +
                toDelete.join(", ") +
                "? (keeping page " +
                pdfPages[0] +
                ")"
        )
    )
        return;
    Promise.all(
        toDelete.map(function (p) {
            return fetch("/scans/" + cfg.docId + "/delete-page/", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRFToken": cfg.csrfToken,
                },
                body: JSON.stringify({ pdf_page: p }),
            }).then(function (r) {
                return r.json();
            });
        })
    ).then(function () {
        btn.textContent = "Deleted";
        btn.disabled = true;
        btn.closest(".issue-card").style.opacity = "0.3";
    });
}

function dismissIssue(btn, issueId) {
    var cfg = window.SCAN_CONFIG;
    fetch("/scans/" + cfg.docId + "/dismiss-issue/", {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": cfg.csrfToken,
        },
        body: JSON.stringify({ issue_id: issueId }),
    })
        .then(function (r) {
            return r.json();
        })
        .then(function (data) {
            if (data.status === "ok") {
                btn.closest(".issue-card").style.opacity = "0.3";
                btn.closest(".issue-card").style.pointerEvents = "none";
            } else {
                alert(data.message || "Cannot dismiss");
            }
        });
}

function pairOpinions() {
    var cfg = window.SCAN_CONFIG;
    if (cfg.step < 2) return;
    var btn = document.getElementById("pair-btn");
    if (!btn) return;
    btn.textContent = "Pairing...";
    btn.disabled = true;
    fetch("/scans/" + cfg.docId + "/pair-opinions/", {
        method: "POST",
        headers: { "X-CSRFToken": cfg.csrfToken },
    })
        .then(function (r) {
            return r.json();
        })
        .then(function (data) {
            btn.textContent = "Re-pair Opinions";
            btn.disabled = false;
            if (data.error) {
                alert("Error: " + data.error);
                return;
            }
            window.location.reload();
        })
        .catch(function (err) {
            btn.textContent = "Re-pair Opinions";
            btn.disabled = false;
            alert("Error: " + err);
        });
}

// --- Helper to read detection data from data-* attributes ---

function _getDetectionData(el) {
    var row = el.closest("[data-unmatched-page]");
    if (!row) return null;
    return {
        logicalPage: parseInt(row.dataset.logicalPage),
        pageIndex: parseInt(row.dataset.pageIndex),
        bbox: row.dataset.bbox.split(",").map(function (s) { return parseFloat(s.trim()); }),
        imgW: parseInt(row.dataset.imgW),
        imgH: parseInt(row.dataset.imgH),
        label: row.dataset.unmatchedLabel,
        labelId: parseInt(row.dataset.labelId),
        detectionId: parseInt(row.dataset.detectionId),
    };
}
