import faulthandler
import signal
import sys
import traceback
from typing import Any

from uvicorn.workers import UvicornWorker as BaseUvicornWorker


class UvicornWorker(BaseUvicornWorker):
    CONFIG_KWARGS: dict[str, Any] = {
        "loop": "auto",
        "http": "auto",
        "lifespan": "off",
    }

    def init_process(self) -> None:
        """Enable faulthandler so a native crash dumps a stack.

        Runs in the forked child (gunicorn calls this post-fork). faulthandler
        installs C-level handlers for the fatal signals (SIGSEGV, SIGFPE,
        SIGBUS, SIGILL); if a C extension such as fitz/MuPDF faults on a bad
        PDF, every thread's stack is dumped to stderr (pod logs) before the
        process dies, instead of a silent exit. It costs nothing until an
        actual crash. The 180s WORKER TIMEOUT is captured separately by the
        SIGABRT handler wired up in init_signals().
        """
        faulthandler.enable()
        # Launch the in-flight-request and concurrency monitor thread (issue
        # #115). Post-fork so it lives in the worker, not the preloaded master.
        from scanning.observability import start_monitor

        start_monitor()
        super().init_process()

    def init_signals(self) -> None:
        """Re-wire SIGABRT so a worker timeout is reported to Sentry.

        gunicorn's arbiter sends SIGABRT to a worker that misses its
        ``--timeout`` heartbeat, and gunicorn's base worker would route that to
        ``handle_abort``/the ``worker_abort`` hook. But uvicorn's worker resets
        every signal to ``SIG_DFL`` here and only re-installs SIGUSR1, so under
        this worker class SIGABRT would otherwise kill the process with no
        Python running and nothing sent to Sentry. Restore a handler that
        captures the current stacks first. Best-effort: it runs on the main
        thread and needs the GIL, so a worker fully wedged in GIL-holding C
        code may die before it runs — but in practice fitz/MuPDF releases the
        GIL during heavy work, so the handler usually gets a slice to report.

        We can't hand SIGABRT to faulthandler for a GIL-free fallback:
        ``faulthandler.register`` rejects the fatal signals (SIGABRT included)
        with a RuntimeError, and ``faulthandler.enable`` (see init_process)
        only dumps and re-raises to SIG_DFL with no chained Python callback.
        A fully GIL-wedged worker therefore loses the Sentry event, matching
        the existing SIGSEGV/SIGBUS story where only the stderr dump survives.
        """
        super().init_signals()
        signal.signal(signal.SIGABRT, self.handle_abort)

    def handle_abort(self, sig: int, frame: Any) -> None:
        self._report_timeout_to_sentry()
        super().handle_abort(sig, frame)

    @staticmethod
    def _report_timeout_to_sentry() -> None:
        """Capture every thread's stack and send it to Sentry as a fatal event."""
        try:
            import sentry_sdk

            dump = []
            for thread_id, thread_frame in sys._current_frames().items():
                dump.append(f"Thread {thread_id}:")
                dump.append("".join(traceback.format_stack(thread_frame)))
            stacks = "\n".join(dump)

            with sentry_sdk.new_scope() as scope:
                scope.set_extra("all_thread_stacks", stacks)
                sentry_sdk.capture_message(
                    "gunicorn WORKER TIMEOUT: worker aborted after --timeout",
                    level="fatal",
                )
            # gunicorn's arbiter escalates SIGABRT to SIGKILL on its next
            # murder-loop cycle (~1s later). Keep the flush sub-second so the
            # event has a chance to ship before SIGKILL and so it doesn't stall
            # the graceful exit (worker_abort + sys.exit(1)) in handle_abort.
            sentry_sdk.flush(timeout=0.7)
        except Exception:
            # Never let reporting crash the abort/exit path, but don't swallow
            # silently: print to stderr (pod logs), which is signal-safe unlike
            # the logging module (its lock could be held by the interrupted frame).
            print(
                "workers: failed to report WORKER TIMEOUT to Sentry",
                file=sys.stderr,
            )
            traceback.print_exc(file=sys.stderr)
