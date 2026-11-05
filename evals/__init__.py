"""Scored, repeatable evaluation of the planner against the live model.

The offline suite proves the harness works; this measures the model inside it.
It sits outside `src/` so no wheel carries it, and every run that reaches a model
needs `--yes-spend`. See `run.py` for how to use it.
"""
