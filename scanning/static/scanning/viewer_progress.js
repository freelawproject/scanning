/**
 * Polling script for scan processing progress.
 *
 * Shows progress bar, status messages, and live OCR results in the
 * sidebar while a scan is being processed by the daemon.
 *
 * Expects SCAN_CONFIG global: { docId, progressUrl }
 */

(function () {
    var cfg = window.SCAN_CONFIG;
    if (!cfg || !cfg.progressUrl) return;

    var apiUrl = cfg.progressUrl;
    var lastPageCount = 0;
    // How long to wait before the next poll (#612). Each poll reads the
    // live run of four engines, so a slow scan state and a tab nobody
    // looks at ask less often. A tab shown again polls at once.
    var POLL_MS = 1000; // queued, processing: the progress bar moves
    var AWAITING_POLL_MS = 5000; // awaiting: shard reads take minutes
    var HIDDEN_POLL_MS = 30000; // the tab is in the background
    var RETRY_MS = 2000; // the last poll failed
    var timer = null;
    var stopped = false;
    // The stored size of the OCR cache the last answer carried: the
    // server sends the cache again only when its size moved.
    var ocrSize = null;

    function renderPages(results) {
        var spages = document.getElementById("sidebar-pages");
        if (!spages || !results || results.length === 0) return;
        if (results.length === lastPageCount) return;
        lastPageCount = results.length;

        // Compute sequence issues
        var prevNum = null;
        for (var i = 0; i < results.length; i++) {
            var r = results[i];
            r.seq_issue = "";
            if (r.detected && prevNum !== null) {
                var num = parseInt(r.detected);
                if (!isNaN(num)) {
                    var diff = num - prevNum;
                    if (diff === 0) r.seq_issue = "duplicate";
                    else if (diff < 0) r.seq_issue = "backward";
                    else if (diff > 2) r.seq_issue = "gap";
                    prevNum = num;
                }
            } else if (r.detected) {
                var parsed = parseInt(r.detected);
                if (!isNaN(parsed)) prevNum = parsed;
            }
        }

        var html =
            '<h2 class="text-sm font-bold mt-3 mb-1">Pages (' +
            results.length +
            ")</h2>";
        for (var i = 0; i < results.length; i++) {
            var r = results[i];
            // Sequence dividers
            if (r.seq_issue === "gap") {
                html +=
                    '<div class="flex items-center gap-1 my-1"><div class="flex-1 border-t-2 border-dashed border-yellow-400"></div><span class="text-[9px] text-yellow-600 dark:text-yellow-400 font-bold">GAP</span><div class="flex-1 border-t-2 border-dashed border-yellow-400"></div></div>';
            } else if (r.seq_issue === "backward") {
                html +=
                    '<div class="flex items-center gap-1 my-1"><div class="flex-1 border-t-2 border-dashed border-red-400"></div><span class="text-[9px] text-red-600 dark:text-red-400 font-bold">ORDER</span><div class="flex-1 border-t-2 border-dashed border-red-400"></div></div>';
            } else if (r.seq_issue === "duplicate") {
                html +=
                    '<div class="flex items-center gap-1 my-1"><div class="flex-1 border-t-2 border-dashed border-orange-400"></div><span class="text-[9px] text-orange-600 dark:text-orange-400 font-bold">DUP</span><div class="flex-1 border-t-2 border-dashed border-orange-400"></div></div>';
            }

            var bg = "bg-green-50 dark:bg-green-900/20";
            var numClass = "text-green-700 dark:text-green-300";
            if (!r.detected) {
                bg =
                    "bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800";
            } else if (r.seq_issue === "backward") {
                bg =
                    "bg-red-100 dark:bg-red-900/30 border border-red-300 dark:border-red-700";
                numClass = "text-red-700 dark:text-red-300";
            } else if (r.seq_issue === "duplicate") {
                bg =
                    "bg-orange-100 dark:bg-orange-900/30 border border-orange-300 dark:border-orange-700";
                numClass = "text-orange-700 dark:text-orange-300";
            } else if (r.seq_issue === "gap") {
                bg =
                    "bg-yellow-100 dark:bg-yellow-900/30 border border-yellow-300 dark:border-yellow-700";
                numClass = "text-yellow-700 dark:text-yellow-300";
            }

            html +=
                '<div class="rounded px-2 py-0.5 mb-0.5 cursor-pointer text-xs flex items-center justify-between ' +
                bg +
                "\" data-pdf-index=\"" +
                (r.pdf_page - 1) +
                "\" onclick=\"goToPage(this)\">";
            html +=
                '<span><span class="font-medium text-gray-500 dark:text-gray-400">' +
                r.pdf_page +
                "</span>";
            if (r.detected) {
                html +=
                    ' <span class="' +
                    numClass +
                    ' font-semibold ml-1">#' +
                    r.detected +
                    "</span>";
            } else {
                html +=
                    ' <span class="text-red-600 dark:text-red-400 ml-1">\u2014</span>';
            }
            html += "</span>";
            if (r.type === "range")
                html +=
                    '<span class="text-[9px] text-blue-500">range</span>';
            if (r.zone === "manual")
                html +=
                    '<span class="text-[9px] text-purple-500">manual</span>';
            html += "</div>";
        }
        spages.innerHTML = html;
    }

    function poll() {
        // No timer is pending while a poll is in flight, so a tab shown
        // during one does not send a second.
        timer = null;
        var url =
            typeof ocrSize === "number"
                ? apiUrl + "?ocr_size=" + encodeURIComponent(ocrSize)
                : apiUrl;
        fetch(url)
            .then(function (r) {
                return r.json();
            })
            .then(function (data) {
                var sbar = document.getElementById("sidebar-bar");
                var sstatus = document.getElementById("sidebar-status");
                var pmsg = document.getElementById("processing-msg");
                if (data.total > 0 && sbar) {
                    sbar.style.width =
                        Math.round((data.current / data.total) * 100) + "%";
                }
                if (sstatus)
                    sstatus.textContent = data.message || "Processing...";
                if (pmsg) pmsg.textContent = data.message || "";

                if (data.ocr_results) renderPages(data.ocr_results);
                if (typeof data.ocr_size === "number") ocrSize = data.ocr_size;

                if (
                    data.status === "approved" ||
                    data.status === "pending_review" ||
                    data.status === "awaiting_validation" ||
                    data.status === "ready_for_page_completeness_review" ||
                    data.status === "page_completeness_review_done" ||
                    data.status === "ready_for_redaction_review" ||
                    data.status === "redaction_review_done"
                ) {
                    window.location.reload();
                } else if (
                    String(data.status).indexOf("error") === 0 ||
                    data.status === "cancelled"
                ) {
                    // Every error status and a cancel are terminal: the
                    // way back is the admin re-queue, so stop polling.
                    stopped = true;
                    if (sstatus)
                        sstatus.textContent = "Error: " + data.message;
                    if (sbar) sbar.style.background = "#ef4444";
                } else {
                    schedule(
                        data.status === "awaiting" ? AWAITING_POLL_MS : POLL_MS
                    );
                }
            })
            .catch(function () {
                schedule(RETRY_MS);
            });
    }

    function schedule(ms) {
        if (stopped) return;
        clearTimeout(timer);
        timer = setTimeout(poll, document.hidden ? HIDDEN_POLL_MS : ms);
    }

    document.addEventListener("visibilitychange", function () {
        if (!document.hidden && !stopped && timer !== null) {
            clearTimeout(timer);
            timer = null;
            poll();
        }
    });
    poll();
})();
