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
            sentry_sdk.flush(timeout=2.0)
        except Exception:
            # Never let reporting crash the abort/exit path, but don't swallow
            # silently: print to stderr (pod logs), which is signal-safe unlike
            # the logging module (its lock could be held by the interrupted frame).
            print(
                "workers: failed to report WORKER TIMEOUT to Sentry",
                file=sys.stderr,
            )
            traceback.print_exc(file=sys.stderr)
