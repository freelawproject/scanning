"""Perpendiculars: paint them out before the OCR engines read.

``scanning/perpendiculars.py`` finds West's inverted-T page marks on
the rendered page and paints them over, so no engine reads the
parallel pagination as words. One switch, on by default: the daemon
paints its own Mistral render while it is on, and asks the dots.mocr
and Surya workers to paint theirs through the job input, so one flag
governs all three engines and a worker built before the flag ignores
it. The flag is in no run's identity, so flipping it re-pays no read:
a volume read before it keeps its reads until someone asks for a new
one from the engine's button.
"""

import environ

env = environ.FileAwareEnv()

PERPENDICULARS_WHITEOUT = env.bool("PERPENDICULARS_WHITEOUT", default=True)
