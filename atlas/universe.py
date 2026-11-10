"""The default paper-scan universe.

The list is the S&P 500 constituent file in ``config/sp500.txt``, captured
from the public Wikipedia table. It is not a ranking and it is not a forecast.
``ATLAS_INSTRUMENTS`` still replaces it. A long list is walked in batches so
one yfinance cycle does not have to download every name.
"""

from pathlib import Path
import os


UNIVERSE_FILE = Path(__file__).resolve().parent.parent / "config" / "sp500.txt"
UNIVERSE_SOURCE = (
    "S&P 500 constituents captured 2026-11-10 from "
    "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
)


def load_symbol_file(path):
    symbols = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        text = line.split("#", 1)[0].strip().upper()
        if text:
            symbols.append(text)
    if not symbols:
        raise ValueError(f"No symbols in {path}.")
    if len(set(symbols)) != len(symbols):
        raise ValueError(f"Duplicate symbols in {path}.")
    return tuple(symbols)


def load_universe(path=None):
    return load_symbol_file(path or UNIVERSE_FILE)


def resolve_instruments():
    """Explicit ``ATLAS_INSTRUMENTS`` wins. An unset variable uses the S&P 500 file."""
    if "ATLAS_INSTRUMENTS" in os.environ:
        return tuple(
            symbol.strip().upper()
            for symbol in os.environ["ATLAS_INSTRUMENTS"].split(",")
            if symbol.strip()
        )
    return load_universe()


def scan_batch_size():
    raw = os.getenv("ATLAS_SCAN_BATCH", "20").strip()
    try:
        size = int(raw)
    except ValueError as error:
        raise ValueError("ATLAS_SCAN_BATCH must be a positive integer.") from error
    if size <= 0:
        raise ValueError("ATLAS_SCAN_BATCH must be a positive integer.")
    return size


def next_batch(symbols, start, size):
    """Return the next slice of ``symbols`` and the index after it.

    The walk wraps. A batch that covers the whole list restarts at 0.
    """
    ordered = [str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()]
    count = len(ordered)
    if count == 0:
        return [], 0
    size = min(int(size), count)
    start = int(start) % count
    batch = [ordered[(start + offset) % count] for offset in range(size)]
    nxt = (start + size) % count
    return batch, nxt
