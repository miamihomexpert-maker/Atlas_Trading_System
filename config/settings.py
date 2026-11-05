from dataclasses import dataclass
from math import isfinite
import os


@dataclass(frozen=True)
class Settings:
    instruments: tuple = ("SPY", "QQQ")
    scan_interval_seconds: int = 300
    max_market_data_age_seconds: int = 900
    paper_initial_cash: float = 100_000.0
    live_trading_enabled: bool = False

    def __post_init__(self):
        if self.scan_interval_seconds <= 0:
            raise ValueError("scan_interval_seconds must be positive.")
        if self.max_market_data_age_seconds <= 0:
            raise ValueError("max_market_data_age_seconds must be positive.")
        if not isfinite(self.paper_initial_cash) or self.paper_initial_cash <= 0:
            raise ValueError("paper_initial_cash must be positive and finite.")
        if self.live_trading_enabled:
            raise ValueError("Live trading is not implemented and must remain disabled.")
        normalized = tuple(
            str(instrument).strip().upper()
            for instrument in self.instruments
            if str(instrument).strip()
        )
        if not normalized:
            raise ValueError("At least one instrument must be configured.")
        if len(set(normalized)) != len(normalized):
            raise ValueError("Configured instruments must be unique.")
        object.__setattr__(self, "instruments", normalized)


def load_settings():
    symbols = tuple(
        symbol.strip().upper()
        for symbol in os.getenv("ATLAS_INSTRUMENTS", "SPY,QQQ").split(",")
        if symbol.strip()
    )
    live_setting = os.getenv("ATLAS_LIVE_TRADING", "false").strip().lower()
    if live_setting not in {"false", "0", "no", "true", "1", "yes"}:
        raise ValueError("ATLAS_LIVE_TRADING must be a boolean value.")
    if live_setting in {"true", "1", "yes"}:
        raise ValueError("Live trading is deliberately disabled in this release.")

    return Settings(
        instruments=symbols,
        scan_interval_seconds=int(os.getenv("ATLAS_SCAN_INTERVAL_SECONDS", "300")),
        max_market_data_age_seconds=int(
            os.getenv("ATLAS_MAX_MARKET_DATA_AGE_SECONDS", "900")
        ),
        paper_initial_cash=float(os.getenv("ATLAS_PAPER_INITIAL_CASH", "100000")),
    )