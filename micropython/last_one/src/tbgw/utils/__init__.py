"""Hardware-independent utility modules (logger, queue, event, tb_utility, safe_eval).

Everything in this sub-package is intentionally free of `machine`/`network`/TWAI
imports so it can be unit-tested on a desktop CPython interpreter as well as run
on MicroPython. See tests/host/ for the CPython-side tests.
"""
