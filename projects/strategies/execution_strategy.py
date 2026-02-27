"""ExecutionStrategy – receives signals from msgbus and executes orders.

Subscribes to:
- ``signal.rebalance`` (from SignalActor) — target portfolio weights
- ``signal.risk``      (from RiskActor)   — stop-loss / take-profit actions

This strategy owns the order lifecycle.  It never computes alpha signals.
"""

from __future__ import annotations

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Quantity
from nautilus_trader.trading.strategy import Strategy

from strategies.signals import (
    REBALANCE_TOPIC,
    RISK_TOPIC,
    RebalanceSignal,
    RiskSignal,
)


class ExecutionStrategyConfig(StrategyConfig, frozen=True):
    """Configuration for :class:`ExecutionStrategy`.

    Parameters
    ----------
    instrument_ids : list[str]
        InstrumentId strings for all tradeable instruments (used for
        ``close_all_positions`` on stop).
    max_position_pct : float
        Maximum fraction of equity allocated to a single stock.
    order_lot_size : int
        Board lot size for rounding.
    """

    instrument_ids: list[str]
    max_position_pct: float = 0.02
    order_lot_size: int = 100


class ExecutionStrategy(Strategy):
    """Translates RebalanceSignal and RiskSignal into orders."""

    def __init__(self, config: ExecutionStrategyConfig) -> None:
        super().__init__(config)
        self._instrument_id_strs = config.instrument_ids
        self._max_pos_pct = config.max_position_pct
        self._lot_size = config.order_lot_size

        # Track instruments under active risk guard (avoid re-entry same day)
        self._risk_blocked: set[InstrumentId] = set()

    # -- lifecycle ------------------------------------------------------------

    def on_start(self) -> None:
        self.msgbus.subscribe(REBALANCE_TOPIC, self._on_rebalance_signal)
        self.msgbus.subscribe(RISK_TOPIC, self._on_risk_signal)

    def on_stop(self) -> None:
        for iid_str in self._instrument_id_strs:
            self.close_all_positions(InstrumentId.from_str(iid_str))

    # -- signal handlers ------------------------------------------------------

    def _on_rebalance_signal(self, signal: RebalanceSignal) -> None:
        """Execute portfolio rebalance toward target weights."""
        # Clear risk block at the start of each rebalance cycle
        self._risk_blocked.clear()
        self._execute_rebalance(signal)

    def _on_risk_signal(self, signal: RiskSignal) -> None:
        """Execute a risk action (close or reduce position)."""
        iid = signal.instrument_id
        if iid in self._risk_blocked:
            return  # Already acted on this instrument this cycle

        self._risk_blocked.add(iid)

        if signal.action == "CLOSE":
            self._close_position(iid, signal.reason)
        elif signal.action == "REDUCE":
            self._reduce_position(iid, signal.reason)
        else:
            self.log.warning(f"Unknown risk action: {signal.action}")

    # -- order logic ----------------------------------------------------------

    def _execute_rebalance(self, signal: RebalanceSignal) -> None:
        available = list(signal.weights.keys())
        if not available:
            return

        first_bar = next(iter(signal.bar_snapshot.values()))
        account = self.portfolio.account(
            first_bar.bar_type.instrument_id.venue,
        )
        if account is None:
            self.log.warning("No account found — skipping rebalance")
            return
        equity = float(account.balance_total().as_double())
        if equity <= 0:
            return

        for iid, weight in signal.weights.items():
            if iid in self._risk_blocked:
                continue  # Skip instruments under risk guard

            bar = signal.bar_snapshot.get(iid)
            if bar is None:
                continue
            instrument = self.cache.instrument(iid)
            if instrument is None:
                continue

            price = float(bar.close.as_double())
            if price <= 0:
                continue

            capped = min(weight, self._max_pos_pct)
            target_qty = int((equity * capped / price) // self._lot_size) * self._lot_size
            if target_qty <= 0:
                continue

            current_qty = sum(
                float(p.signed_qty)
                for p in self.cache.positions_open(instrument_id=iid)
            )
            delta = target_qty - current_qty
            if abs(delta) < self._lot_size:
                continue

            side = OrderSide.BUY if delta > 0 else OrderSide.SELL
            order = self.order_factory.market(
                instrument_id=iid,
                order_side=side,
                quantity=Quantity.from_int(int(abs(delta))),
                time_in_force=TimeInForce.DAY,
            )
            self.submit_order(order)

    def _close_position(self, iid: InstrumentId, reason: str) -> None:
        """Close all open positions for *iid*."""
        positions = self.cache.positions_open(instrument_id=iid)
        if not positions:
            return
        self.log.info(f"Risk {reason}: closing {iid}")
        self.close_all_positions(iid)

    def _reduce_position(self, iid: InstrumentId, reason: str) -> None:
        """Reduce position in *iid* by half (rounded to lot size)."""
        positions = self.cache.positions_open(instrument_id=iid)
        if not positions:
            return

        for pos in positions:
            reduce_qty = int(abs(float(pos.signed_qty)) / 2)
            reduce_qty = (reduce_qty // self._lot_size) * self._lot_size
            if reduce_qty < self._lot_size:
                continue

            side = OrderSide.SELL if float(pos.signed_qty) > 0 else OrderSide.BUY
            order = self.order_factory.market(
                instrument_id=iid,
                order_side=side,
                quantity=Quantity.from_int(reduce_qty),
                time_in_force=TimeInForce.DAY,
            )
            self.log.info(f"Risk {reason}: reducing {iid} by {reduce_qty}")
            self.submit_order(order)
