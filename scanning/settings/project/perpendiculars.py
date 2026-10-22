"""Perpendiculars: paint them out before the OCR engines read.

``scanning/perpendiculars.py`` finds West's inverted-T page marks on the
rendered page and paints them over, so no engine reads the parallel
pagination as words. One switch, off by default: the daemon paints its
own Mistral render when it is on, and asks the dots.mocr and Surya
workers to paint theirs through the job input, so one flag governs all
three engines and a worker built before the flag ignores it. It goes
in the result, never in a run's identity, so turning it on re-pays no
read: a volume read before it keeps its reads until someone asks for a
new one.
"""

import environ

env = environ.FileAwareEnv()

PERPENDICULARS_WHITEOUT = env.bool("PERPENDICULARS_WHITEOUT", default=False)
