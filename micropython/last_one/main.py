"""PyMakr upload entry point.

The production source lives in ``src/`` so it remains convenient to test on
a host.  PyMakr mirrors that directory onto the ESP32, therefore this tiny
launcher gives MicroPython the same import path and executes ``src/main.py``.
It deliberately contains no gateway logic: there is one authoritative entry
point in src/main.py.
"""

import sys

if "src" not in sys.path:
    sys.path.insert(0, "src")

exec(open("src/main.py").read(), globals())
