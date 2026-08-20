"""Metrics adapters: the I/O half of `app/core/metrics.py`.

Everything here fetches or parses. Nothing here decides -- verdicts belong to the
pure core, so they stay testable without credentials or a network.
"""
