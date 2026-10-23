"""OMEN: high-confidence Kalshi prediction-market scanner.

Scans Kalshi's public market data, prices every candidate play with the exchange's current fee schedule, keeps a
paper ledger of everything it alerts on, and posts to Discord. It never places a real order unless execution is
explicitly enabled (see docs/EXECUTION.md).
"""

__version__ = "0.1.0"
