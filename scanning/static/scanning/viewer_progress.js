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
                '<span><span class="font-medium text-gray-500">' +
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
        fetch(apiUrl)
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

                if (
                    data.status === "approved" ||
                    data.status === "pending_review"
                ) {
                    window.location.reload();
                } else if (data.status === "error") {
                    if (sstatus)
                        sstatus.textContent = "Error: " + data.message;
                    if (sbar) sbar.style.background = "#ef4444";
                } else if (data.status !== "cancelled") {
                    setTimeout(poll, 1000);
                }
            })
            .catch(function () {
                setTimeout(poll, 2000);
            });
    }
    poll();
})();
