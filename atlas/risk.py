from dataclasses import dataclass
from math import floor, isfinite


@dataclass(frozen=True)
class RiskLimits:
    risk_per_trade_fraction: float = 0.01
    max_position_fraction: float = 0.10
    max_daily_loss_fraction: float = 0.03

    def __post_init__(self):
        for name, value in (
            ("risk_per_trade_fraction", self.risk_per_trade_fraction),
            ("max_position_fraction", self.max_position_fraction),
            ("max_daily_loss_fraction", self.max_daily_loss_fraction),
        ):
            if not isfinite(value) or not 0 < value <= 1:
                raise ValueError(f"{name} must be greater than 0 and at most 1.")


def calculate_position_size(
    equity,
    entry_price,
    stop_loss,
    limits=None,
    current_exposure=0.0,
):
    """Return whole units, bounded by both per-trade risk and exposure limits."""
    limits = limits or RiskLimits()
    values = (equity, entry_price, stop_loss, current_exposure)
    if not all(isfinite(value) for value in values):
        raise ValueError("Position sizing inputs must be finite numbers.")
    if equity <= 0 or entry_price <= 0 or stop_loss <= 0:
        raise ValueError("Equity, entry price, and stop loss must be positive.")
    if current_exposure < 0:
        raise ValueError("Current exposure cannot be negative.")

    risk_per_unit = abs(entry_price - stop_loss)
    if risk_per_unit == 0:
        raise ValueError("Entry price and stop loss must differ.")

    risk_units = floor(equity * limits.risk_per_trade_fraction / risk_per_unit)
    remaining_exposure = max(
        0.0,
        equity * limits.max_position_fraction - current_exposure,
    )
    exposure_units = floor(remaining_exposure / entry_price)
    return max(0, min(risk_units, exposure_units))


def validate_daily_loss(equity, daily_pnl, limits=None):
    limits = limits or RiskLimits()
    if not isfinite(equity) or not isfinite(daily_pnl) or equity <= 0:
        raise ValueError("Equity and daily P&L must be finite; equity must be positive.")
    return daily_pnl > -(equity * limits.max_daily_loss_fraction)


def get_risk_status():
    return "RISK MANAGEMENT MODULE ONLINE"
