"""Configuration for the memecoin on-chain feature research engine.

The engine is independent from ``vectorbt_research``: it reads the validated
per-day event archives and writes feature tables.  It does not import, call or
modify the execution simulator or the VectorBT module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

#: The five validated research days.
DAYS: tuple[str, ...] = (
    "2026-08-09",
    "2026-10-03",
    "2026-10-04",
    "2026-10-05",
    "2026-11-08",
)

#: Predefined split, identical to the rest of the project.
TRAIN_DAYS: tuple[str, ...] = ("2026-10-03", "2026-10-04", "2026-10-05")
HOLDOUT_DAYS: tuple[str, ...] = ("2026-08-09", "2026-11-08")

DATASET_ROOT = Path("output/backtest/days")
FEATURE_ROOT = Path("output/backtest/features")

TIMEFRAMES: dict[str, int] = {"1m": 60_000, "5m": 300_000}
MINUTE_MS = 60_000

#: Default trailing windows, in bars, for rolling features.
DEFAULT_WINDOWS: tuple[int, ...] = (5, 15, 60)

#: Default order size used for the constant-product slippage estimate.
DEFAULT_ORDER_SIZE_SOL = 0.1

#: Gate Learn's 25 % concentration observation.  It is recorded as a research
#: annotation only; it is never used to reject a token or a signal.
DEFAULT_CONCENTRATION_THRESHOLD = 0.25


def split_of(day: str) -> str:
    if day in TRAIN_DAYS:
        return "train"
    if day in HOLDOUT_DAYS:
        return "holdout"
    return "unspecified"


@dataclass(frozen=True, slots=True)
class FeatureConfig:
    """Parameters for one feature build."""

    timeframe: str = "1m"
    windows: tuple[int, ...] = DEFAULT_WINDOWS
    order_size_sol: float = DEFAULT_ORDER_SIZE_SOL
    concentration_threshold: float = DEFAULT_CONCENTRATION_THRESHOLD
    #: Optional lower bound for a bucket to be marked ``bucket_usable``.  This
    #: mirrors the VectorBT 5-minute coverage rule so both modules can agree on
    #: which buckets are supported by enough real minute data.
    min_coverage_bars: int = 1
    #: Extra metadata written next to the outputs.
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.timeframe not in TIMEFRAMES:
            raise ValueError(
                f"Unsupported timeframe {self.timeframe!r}: {TIMEFRAMES}"
            )
        if not self.windows:
            raise ValueError("windows must not be empty")
        if any(window < 1 for window in self.windows):
            raise ValueError("windows must be positive bar counts")
        if self.order_size_sol <= 0.0:
            raise ValueError("order_size_sol must be positive")
        if not 0.0 < self.concentration_threshold <= 1.0:
            raise ValueError("concentration_threshold must be within (0, 1]")
        ratio = self.bar_ms // MINUTE_MS
        if not 1 <= self.min_coverage_bars <= ratio:
            raise ValueError(
                f"min_coverage_bars must be within [1, {ratio}] for "
                f"timeframe {self.timeframe!r}"
            )

    @property
    def bar_ms(self) -> int:
        return TIMEFRAMES[self.timeframe]

    @property
    def coverage_ratio(self) -> int:
        return self.bar_ms // MINUTE_MS

    def as_dict(self) -> dict[str, object]:
        return {
            "timeframe": self.timeframe,
            "bar_ms": self.bar_ms,
            "windows": list(self.windows),
            "order_size_sol": self.order_size_sol,
            "concentration_threshold": self.concentration_threshold,
            "min_coverage_bars": self.min_coverage_bars,
        }
