"""Signal data classes for inter-component communication via MessageBus.

Topics
------
- ``signal.rebalance`` : SignalActor → ExecutionStrategy
- ``signal.risk``      : RiskActor   → ExecutionStrategy
"""

from __future__ import annotations

from nautilus_trader.model.data import Bar
from nautilus_trader.model.identifiers import InstrumentId

# ── MessageBus topics ────────────────────────────────────────────────────────

REBALANCE_TOPIC = "signal.rebalance"
RISK_TOPIC = "signal.risk"

# ── Signal classes ───────────────────────────────────────────────────────────


class RebalanceSignal:
    """Published by SignalActor when a rebalance is triggered.

    Attributes
    ----------
    weights : dict[InstrumentId, float]
        Target portfolio weights (non-negative, sum to 1).
    bar_snapshot : dict[InstrumentId, Bar]
        End-of-day bars that produced the signal (for price reference).
    ts_event : int
        Nanosecond timestamp of the bar day that triggered rebalance.
    ts_init : int
        Nanosecond timestamp when the signal was created.
    """

    __slots__ = ("weights", "bar_snapshot", "ts_event", "ts_init")

    def __init__(
        self,
        weights: dict[InstrumentId, float],
        bar_snapshot: dict[InstrumentId, Bar],
        ts_event: int = 0,
        ts_init: int = 0,
    ):
        self.weights = weights
        self.bar_snapshot = bar_snapshot
        self.ts_event = ts_event
        self.ts_init = ts_init

    def __repr__(self) -> str:
        return (
            f"RebalanceSignal(n_instruments={len(self.weights)}, "
            f"ts_event={self.ts_event})"
        )


class RiskSignal:
    """Published by RiskActor when a position breaches a risk threshold.

    Attributes
    ----------
    instrument_id : InstrumentId
        The instrument to act on.
    action : str
        ``"CLOSE"`` to fully close, ``"REDUCE"`` to cut by half.
    reason : str
        ``"STOP_LOSS"`` or ``"TAKE_PROFIT"``.
    current_pnl_pct : float
        PnL percentage that triggered the signal (positive = profit).
    ts_event : int
        Nanosecond timestamp of the bar that triggered the signal.
    ts_init : int
        Nanosecond timestamp when the signal was created.
    """

    __slots__ = (
        "instrument_id",
        "action",
        "reason",
        "current_pnl_pct",
        "ts_event",
        "ts_init",
    )

    def __init__(
        self,
        instrument_id: InstrumentId,
        action: str,
        reason: str,
        current_pnl_pct: float = 0.0,
        ts_event: int = 0,
        ts_init: int = 0,
    ):
        self.instrument_id = instrument_id
        self.action = action
        self.reason = reason
        self.current_pnl_pct = current_pnl_pct
        self.ts_event = ts_event
        self.ts_init = ts_init

    def __repr__(self) -> str:
        return (
            f"RiskSignal({self.action} {self.instrument_id} "
            f"reason={self.reason} pnl={self.current_pnl_pct:+.2%})"
        )
