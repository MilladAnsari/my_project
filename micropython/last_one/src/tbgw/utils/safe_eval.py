"""tbgw.utils.safe_eval
=========================

Upstream's CAN connector uses Python's `eval()` to compute
`dataExpression` (attribute-update transforms) and downlink byte-building
expressions, with a `strictEval` config flag: when true (the default),
`__builtins__` is not exposed to the expression, restricting it to the
variables explicitly passed in; when false, full builtins are available.

Per project decision, `eval()` is accepted as-is for this port (no
replacement mini-DSL) -- this module only reproduces the strictEval
on/off behavior so `can.json` configs can carry the same flag upstream's
`can.json` does, and so every call site doesn't have to hand-roll the
`{'__builtins__': {}}` trick itself.
"""


def evaluate(expression, variables, strict=True):
    """Evaluates `expression` with `variables` available as locals.

    strict=True (default, matches upstream's strictEval default of
    restricting builtins): the expression cannot reach `__builtins__`
    (no `open`, `import`, etc. reachable through builtin lookups),
    but the passed-in `variables` are still fully usable.

    strict=False: full builtins are available to the expression, matching
    upstream's documented escape hatch for expressions that need e.g.
    `abs()`, `min()`, `max()`.
    """
    globals_dict = {"__builtins__": {}} if strict else {}
    try:
        return eval(expression, globals_dict, dict(variables))
    except Exception as exc:
        raise ValueError("safe_eval: failed to evaluate %r: %s" % (expression, exc))
