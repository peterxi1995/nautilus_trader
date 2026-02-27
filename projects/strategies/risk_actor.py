"""RiskActor – monitors open positions and publishes risk signals.

Subscribes to bars for all traded instruments.  On each bar, checks every
open position against configurable stop-loss and take-profit thresholds.
When a threshold is breached, publishes a :class:`RiskSignal` on the
``signal.risk`` topic for :class:`ExecutionStrategy` to act on.

Because this is an *Actor* (not a *Strategy*), it cannot submit orders
directly — enforcing a clean separation of concerns.
"""

from __future__ import annotations

from nautilus_trader.common.actor import Actor
from nautilus_trader.config import ActorConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price

from strategies.signals import RISK_TOPIC, RiskSignal


class RiskActorConfig(ActorConfig, frozen=True):
    """Configuration for :class:`RiskActor`.

    Parameters
    ----------
    bar_types : list[str]
        BarType strings to subscribe to (must match SignalActor's).
    stop_loss_pct : float
        Maximum tolerated loss as a fraction (e.g. 0.10 = −10%).
    take_profit_pct : float
        Profit target as a fraction (e.g. 0.20 = +20%).
    action_on_stop_loss : str
        ``"CLOSE"`` or ``"REDUCE"`` when stop-loss triggers.
    action_on_take_profit : str
        ``"CLOSE"`` or ``"REDUCE"`` when take-profit triggers.
    """

    bar_types: list[str]
    stop_loss_pct: float = 0.10
    take_profit_pct: float = 0.20
    action_on_stop_loss: str = "CLOSE"
    action_on_take_profit: str = "CLOSE"


class RiskActor(Actor):
    """Monitors positions and publishes RiskSignal on threshold breach."""

    def __init__(self, config: RiskActorConfig) -> None:
        super().__init__(config)
        self._bar_type_strs = config.bar_types
        self._stop_loss_pct = config.stop_loss_pct
        self._take_profit_pct = config.take_profit_pct
        self._action_sl = config.action_on_stop_loss
        self._action_tp = config.action_on_take_profit

        # Track instruments already signalled this bar-day to avoid duplicates
        self._signalled_today: set[InstrumentId] = set()
        self._current_date: int | None = None

    # -- lifecycle ------------------------------------------------------------

    def on_start(self) -> None:
        for bt_str in self._bar_type_strs:
            bt = BarType.from_str(bt_str)
            self.subscribe_bars(bt)

    def on_bar(self, bar: Bar) -> None:
        # Reset signalled set on day change
        bar_date = bar.ts_event
        if self._current_date is not None and bar_date != self._current_date:
            self._signalled_today.clear()
        self._current_date = bar_date

        iid = bar.bar_type.instrument_id
        if iid in self._signalled_today:
            return

        self._check_position(iid, bar)

    def on_stop(self) -> None:
        self._signalled_today.clear()

    # -- risk checking --------------------------------------------------------

    def _check_position(self, iid: InstrumentId, bar: Bar) -> None:
        """Check all open positions for *iid* against risk thresholds."""
        positions = self.cache.positions_open(instrument_id=iid)
        if not positions:
            return

        close_price = float(bar.close.as_double())
        if close_price <= 0:
            return

        for pos in positions:
            entry_price = pos.avg_px_open
            if entry_price <= 0:
                continue

            # PnL percentage (positive = profit, negative = loss)
            if float(pos.signed_qty) > 0:
                # Long position
                pnl_pct = (close_price - entry_price) / entry_price
            else:
                # Short position
                pnl_pct = (entry_price - close_price) / entry_price

            # Stop-loss check
            if pnl_pct <= -self._stop_loss_pct:
                self._emit_risk_signal(
                    iid=iid,
                    action=self._action_sl,
                    reason="STOP_LOSS",
                    pnl_pct=pnl_pct,
                    ts_event=bar.ts_event,
                )
                return  # One signal per instrument per day

            # Take-profit check
            if pnl_pct >= self._take_profit_pct:
                self._emit_risk_signal(
                    iid=iid,
                    action=self._action_tp,
                    reason="TAKE_PROFIT",
                    pnl_pct=pnl_pct,
                    ts_event=bar.ts_event,
                )
                return

    def _emit_risk_signal(
        self,
        iid: InstrumentId,
        action: str,
        reason: str,
        pnl_pct: float,
        ts_event: int,
    ) -> None:
        self._signalled_today.add(iid)
        signal = RiskSignal(
            instrument_id=iid,
            action=action,
            reason=reason,
            current_pnl_pct=pnl_pct,
            ts_event=ts_event,
            ts_init=ts_event,
        )
        self.log.info(repr(signal))
        self.msgbus.publish(RISK_TOPIC, signal)
