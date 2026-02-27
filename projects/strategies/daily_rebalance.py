"""Daily rebalance strategy driven by a random signal.

Subscribes to daily bars for a set of instruments. At the end of each
trading day (detected when the next day's bar arrives), it generates a
random target weight for every available stock, then submits orders to
move from the current portfolio toward those targets.
"""

from __future__ import annotations

import random
from decimal import Decimal

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Quantity
from nautilus_trader.trading.strategy import Strategy


class DailyRebalanceConfig(StrategyConfig, frozen=True):
    """Configuration for the daily rebalance strategy.

    Parameters
    ----------
    bar_types : list[str]
        BarType strings for every instrument to subscribe to,
        e.g. ``["000001.XSHE-1-DAY-LAST-EXTERNAL", ...]``.
    random_seed : int
        Seed for the random signal generator.
    max_position_pct : float
        Maximum fraction of equity allocated to a single stock (default 0.02).
    order_lot_size : int
        Minimum lot size for rounding orders (default 100, i.e. board lot).
    long_only : bool
        If True, only generate long weights (default True).
    """

    bar_types: list[str]
    random_seed: int = 42
    max_position_pct: float = 0.02
    order_lot_size: int = 100
    long_only: bool = True


class DailyRebalanceStrategy(Strategy):
    """Rebalance all subscribed instruments at end of each trading day."""

    def __init__(self, config: DailyRebalanceConfig):
        super().__init__(config)
        self._bar_type_strs = config.bar_types
        self._rng = random.Random(config.random_seed)
        self._max_pos_pct = config.max_position_pct
        self._lot_size = config.order_lot_size
        self._long_only = config.long_only

        # Runtime state
        self._bar_types: list[BarType] = []
        self._instrument_ids: list[InstrumentId] = []
        self._current_date = None
        self._day_bars: dict[InstrumentId, Bar] = {}

    # -- lifecycle ------------------------------------------------------------

    def on_start(self):
        for bt_str in self._bar_type_strs:
            bt = BarType.from_str(bt_str)
            self._bar_types.append(bt)
            self._instrument_ids.append(bt.instrument_id)
            self.subscribe_bars(bt)

    def on_bar(self, bar: Bar):
        bar_date = bar.ts_event  # nanosecond timestamp; same for all bars of a day
        if self._current_date is not None and bar_date != self._current_date:
            # New day detected -- rebalance on the previous day's bars
            self._rebalance()
            self._day_bars.clear()

        self._current_date = bar_date
        self._day_bars[bar.bar_type.instrument_id] = bar

    def on_stop(self):
        # Close all open positions at end of backtest
        for instrument_id in self._instrument_ids:
            self.close_all_positions(instrument_id)
        self._day_bars.clear()

    # -- core logic -----------------------------------------------------------

    def _rebalance(self):
        available = list(self._day_bars.keys())
        if not available:
            return

        # Generate random target weights that sum to 1
        raw = [self._rng.random() for _ in available]
        if self._long_only:
            raw = [abs(w) for w in raw]
        total = sum(raw)
        if total == 0:
            return
        weights = {iid: w / total for iid, w in zip(available, raw)}

        # Determine equity value
        account = self.portfolio.account(self._day_bars[available[0]].bar_type.instrument_id.venue)
        if account is None:
            self.log.warning("No account found, skipping rebalance")
            return
        equity = float(account.balance_total().as_double())
        if equity <= 0:
            return

        for instrument_id, weight in weights.items():
            bar = self._day_bars[instrument_id]
            instrument = self.cache.instrument(instrument_id)
            if instrument is None:
                continue

            price = float(bar.close.as_double())
            if price <= 0:
                continue

            # Cap weight at max_position_pct
            capped_weight = min(weight, self._max_pos_pct)
            target_notional = equity * capped_weight
            target_qty_raw = target_notional / price
            # Round down to lot size
            target_qty = int(target_qty_raw // self._lot_size) * self._lot_size
            if target_qty <= 0:
                continue

            # Current position
            open_positions = self.cache.positions_open(instrument_id=instrument_id)
            current_qty = 0.0
            for pos in open_positions:
                current_qty += float(pos.signed_qty)

            delta = target_qty - current_qty
            if abs(delta) < self._lot_size:
                continue

            side = OrderSide.BUY if delta > 0 else OrderSide.SELL
            qty = Quantity.from_int(int(abs(delta)))

            order = self.order_factory.market(
                instrument_id=instrument_id,
                order_side=side,
                quantity=qty,
                time_in_force=TimeInForce.DAY,
            )
            self.submit_order(order)
