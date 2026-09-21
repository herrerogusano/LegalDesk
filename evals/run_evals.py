#!/usr/bin/env python3
"""CLI entry point for the deterministic Phase 12 evaluation."""

try:
    from .runner import main
except ImportError:  # Direct script execution: ``python evals/run_evals.py``.
    from runner import main


if __name__ == "__main__":
    raise SystemExit(main())
