"""SignalActor – subscribes to bars, computes alpha signals, publishes via msgbus.

This actor is responsible for *signal generation only*.  It never touches the
order API.  Signals are published on ``signal.rebalance`` for the
ExecutionStrategy to consume.

The default implementation uses random weights as a placeholder.  To plug in a
real alpha model, subclass and override :meth:`compute_weights`.
"""

from __future__ import annotations

import random

from nautilus_trader.common.actor import Actor
from nautilus_trader.config import ActorConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.identifiers import InstrumentId

from strategies.signals import REBALANCE_TOPIC, RebalanceSignal


class SignalActorConfig(ActorConfig, frozen=True):
    """Configuration for :class:`SignalActor`.

    Parameters
    ----------
    bar_types : list[str]
        BarType strings to subscribe to.
    random_seed : int
        RNG seed for the random weight generator.
    long_only : bool
        If ``True`` only positive weights are generated.
    """

    bar_types: list[str]
    random_seed: int = 42
    long_only: bool = True


class SignalActor(Actor):
    """Collects daily bars and publishes a RebalanceSignal on each day change."""

    def __init__(self, config: SignalActorConfig):
        super().__init__(config)
        self._bar_type_strs = config.bar_types
        self._rng = random.Random(config.random_seed)
        self._long_only = config.long_only

        # Runtime state
        self._current_date: int | None = None
        self._day_bars: dict[InstrumentId, Bar] = {}
        # Optional: bar extras lookup (vwap, adj_factor) — injected externally
        self._bar_extras: dict[tuple[InstrumentId, int], object] = {}

    # -- public helpers -------------------------------------------------------

    def set_bar_extras(
        self,
        extras: dict[tuple[InstrumentId, int], object],
    ) -> None:
        """Inject the bar extras lookup table (called by run_backtest before
        the engine starts)."""
        self._bar_extras = extras

    # -- lifecycle ------------------------------------------------------------

    def on_start(self) -> None:
        for bt_str in self._bar_type_strs:
            bt = BarType.from_str(bt_str)
            self.subscribe_bars(bt)

    def on_bar(self, bar: Bar) -> None:
        bar_date = bar.ts_event
        if self._current_date is not None and bar_date != self._current_date:
            self._emit_signal()
            self._day_bars.clear()
        self._current_date = bar_date
        self._day_bars[bar.bar_type.instrument_id] = bar

    def on_stop(self) -> None:
        self._day_bars.clear()

    # -- signal computation ---------------------------------------------------

    def compute_weights(
        self,
        day_bars: dict[InstrumentId, Bar],
    ) -> dict[InstrumentId, float]:
        """Compute target portfolio weights for the given day.

        Override this method to plug in a real alpha model.
        The default implementation generates normalised random weights.

        Parameters
        ----------
        day_bars : dict[InstrumentId, Bar]
            All bars received for the current trading day.

        Returns
        -------
        dict[InstrumentId, float]
            Target weights (non-negative, summing to 1).
        """
        available = list(day_bars.keys())
        if not available:
            return {}

        raw = [self._rng.random() for _ in available]
        if self._long_only:
            raw = [abs(w) for w in raw]
        total = sum(raw)
        if total == 0:
            return {}
        return {iid: w / total for iid, w in zip(available, raw)}

    def _emit_signal(self) -> None:
        weights = self.compute_weights(self._day_bars)
        if not weights:
            return

        signal = RebalanceSignal(
            weights=weights,
            bar_snapshot=dict(self._day_bars),
            ts_event=self._current_date,
            ts_init=self._current_date,
        )
        self.msgbus.publish(REBALANCE_TOPIC, signal)
