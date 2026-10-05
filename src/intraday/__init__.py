"""src.intraday -- the intra-check session and the intraday opportunity scan as constructed parts.

Each module holds one piece lifted from `IntradayMixin`; the mixin in src/pipeline_intraday.py
builds each part per call from the host's collaborators. The 399-line scan body itself
(`IntradayScanBody`) stays in src/pipeline_intraday.py: a single function cannot be carved
to fit the 400-line floor for a new file without changing it.
"""
