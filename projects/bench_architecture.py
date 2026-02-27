"""Architecture benchmark: compare three strategy structures.

Tests the NautilusTrader runtime overhead of different patterns for a daily
rebalancing strategy across N instruments:

  V1 — MONOLITH (current):
       One Strategy subscribes to all bars, does signal generation + order
       management inside on_bar / _rebalance.

  V2 — PRECOMPUTED SIGNALS:
       One Strategy, but signals are pre-computed as a {(date, instrument): weight}
       lookup table before the engine runs.  on_bar is minimal (dict lookup).

  V3 — ACTOR + STRATEGY SEPARATION:
       A SignalActor subscribes to all bars, computes signals, and publishes them
       via publish_data().  An ExecutionStrategy subscribes to Actor data and
       submits orders.  Only the Strategy touches the order API.

All three produce identical orders (same random seed → same weights).
The benchmark measures:
  - engine_run time (isolating the NT runtime cost from data loading)
  - per-bar wall time
  - Python callback overhead
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from dataclasses import dataclass, field
from decimal import Decimal

from nautilus_trader.backtest.config import BacktestEngineConfig
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.common.actor import Actor
from nautilus_trader.config import ActorConfig, LoggingConfig, RiskEngineConfig, StrategyConfig
from nautilus_trader.model.currencies import CNY
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import AccountType, OmsType, OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId, TraderId, Venue
from nautilus_trader.model.objects import Money, Quantity
from nautilus_trader.trading.strategy import Strategy

from datasources.duckdb_daily import DuckDBDailySource

DB_PATH = "/Users/rachelpeter/codebase/ocelot/data/china_daily.duckdb"
STARTING_BALANCE = 50_000_000


# ============================================================================
# Shared data loading (identical for all variants)
# ============================================================================

def load_data(n_stocks: int, start: str, end: str):
    """Return (instruments, bars_per_inst, bar_type_strs)."""
    source = DuckDBDailySource(db_path=DB_PATH, start=start, end=end)
    instruments = source.instruments()[:n_stocks]
    bars_per_inst = {}
    bar_type_strs = []
    total_bars = 0
    for inst in instruments:
        bt = source.default_bar_type(inst)
        bars = source.bars(bt, inst)
        if bars:
            bars_per_inst[inst] = bars
            bar_type_strs.append(str(bt))
            total_bars += len(bars)
    return instruments, bars_per_inst, bar_type_strs, total_bars


def build_engine(instruments, bars_per_inst):
    """Wire up a BacktestEngine with data loaded."""
    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id=TraderId("BENCHMARKER-001"),
            logging=LoggingConfig(log_level="ERROR"),
            risk_engine=RiskEngineConfig(bypass=True),
        )
    )
    venues_added = set()
    for inst in instruments:
        v = inst.id.venue
        if v not in venues_added:
            engine.add_venue(
                venue=v, oms_type=OmsType.NETTING,
                account_type=AccountType.CASH, base_currency=CNY,
                starting_balances=[Money(STARTING_BALANCE, CNY)],
            )
            venues_added.add(v)
        engine.add_instrument(inst)
        if inst in bars_per_inst:
            engine.add_data(bars_per_inst[inst])
    return engine


# ============================================================================
# V1 — MONOLITH (current design, copied from daily_rebalance.py)
# ============================================================================

class V1Config(StrategyConfig, frozen=True):
    bar_types: list[str]
    random_seed: int = 42
    max_position_pct: float = 0.02
    order_lot_size: int = 100
    long_only: bool = True


class V1Monolith(Strategy):
    """Monolithic: signal + execution in one Strategy."""

    def __init__(self, config: V1Config):
        super().__init__(config)
        self._bar_type_strs = config.bar_types
        self._rng = random.Random(config.random_seed)
        self._max_pos_pct = config.max_position_pct
        self._lot_size = config.order_lot_size
        self._long_only = config.long_only
        self._bar_types: list[BarType] = []
        self._instrument_ids: list[InstrumentId] = []
        self._current_date = None
        self._day_bars: dict[InstrumentId, Bar] = {}

    def on_start(self):
        for bt_str in self._bar_type_strs:
            bt = BarType.from_str(bt_str)
            self._bar_types.append(bt)
            self._instrument_ids.append(bt.instrument_id)
            self.subscribe_bars(bt)

    def on_bar(self, bar: Bar):
        bar_date = bar.ts_event
        if self._current_date is not None and bar_date != self._current_date:
            self._rebalance()
            self._day_bars.clear()
        self._current_date = bar_date
        self._day_bars[bar.bar_type.instrument_id] = bar

    def on_stop(self):
        for iid in self._instrument_ids:
            self.close_all_positions(iid)
        self._day_bars.clear()

    def _rebalance(self):
        available = list(self._day_bars.keys())
        if not available:
            return
        raw = [self._rng.random() for _ in available]
        if self._long_only:
            raw = [abs(w) for w in raw]
        total = sum(raw)
        if total == 0:
            return
        weights = {iid: w / total for iid, w in zip(available, raw)}

        account = self.portfolio.account(self._day_bars[available[0]].bar_type.instrument_id.venue)
        if account is None:
            return
        equity = float(account.balance_total().as_double())
        if equity <= 0:
            return

        for iid, weight in weights.items():
            bar = self._day_bars[iid]
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
            current_qty = sum(float(p.signed_qty) for p in self.cache.positions_open(instrument_id=iid))
            delta = target_qty - current_qty
            if abs(delta) < self._lot_size:
                continue
            side = OrderSide.BUY if delta > 0 else OrderSide.SELL
            order = self.order_factory.market(
                instrument_id=iid, order_side=side,
                quantity=Quantity.from_int(int(abs(delta))),
                time_in_force=TimeInForce.DAY,
            )
            self.submit_order(order)


# ============================================================================
# V2 — PRECOMPUTED SIGNALS
# ============================================================================

class V2Config(StrategyConfig, frozen=True):
    bar_types: list[str]
    random_seed: int = 42
    max_position_pct: float = 0.02
    order_lot_size: int = 100
    long_only: bool = True


class V2Precomputed(Strategy):
    """Signals pre-computed as a lookup table. on_bar is a cheap dict lookup."""

    def __init__(self, config: V2Config, signal_table: dict):
        super().__init__(config)
        self._bar_type_strs = config.bar_types
        self._signal_table = signal_table  # {(ts_event, instrument_id): weight}
        self._max_pos_pct = config.max_position_pct
        self._lot_size = config.order_lot_size
        self._bar_types: list[BarType] = []
        self._instrument_ids: list[InstrumentId] = []
        self._current_date = None
        self._day_bars: dict[InstrumentId, Bar] = {}

    def on_start(self):
        for bt_str in self._bar_type_strs:
            bt = BarType.from_str(bt_str)
            self._bar_types.append(bt)
            self._instrument_ids.append(bt.instrument_id)
            self.subscribe_bars(bt)

    def on_bar(self, bar: Bar):
        bar_date = bar.ts_event
        if self._current_date is not None and bar_date != self._current_date:
            self._execute()
            self._day_bars.clear()
        self._current_date = bar_date
        self._day_bars[bar.bar_type.instrument_id] = bar

    def on_stop(self):
        for iid in self._instrument_ids:
            self.close_all_positions(iid)
        self._day_bars.clear()

    def _execute(self):
        """Execute based on pre-computed weights — no signal computation."""
        available = list(self._day_bars.keys())
        if not available:
            return
        account = self.portfolio.account(self._day_bars[available[0]].bar_type.instrument_id.venue)
        if account is None:
            return
        equity = float(account.balance_total().as_double())
        if equity <= 0:
            return

        for iid in available:
            weight = self._signal_table.get((self._current_date, iid))
            if weight is None or weight <= 0:
                continue
            bar = self._day_bars[iid]
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
            current_qty = sum(float(p.signed_qty) for p in self.cache.positions_open(instrument_id=iid))
            delta = target_qty - current_qty
            if abs(delta) < self._lot_size:
                continue
            side = OrderSide.BUY if delta > 0 else OrderSide.SELL
            order = self.order_factory.market(
                instrument_id=iid, order_side=side,
                quantity=Quantity.from_int(int(abs(delta))),
                time_in_force=TimeInForce.DAY,
            )
            self.submit_order(order)


def precompute_signals(bar_type_strs: list[str], bars_per_inst, seed=42) -> dict:
    """Build {(ts_event, instrument_id): weight} table matching V1's random weights."""
    # Group bars by date
    from collections import defaultdict
    date_instruments: dict[int, list[InstrumentId]] = defaultdict(list)
    date_bars: dict[int, dict[InstrumentId, Bar]] = defaultdict(dict)
    for inst, bars in bars_per_inst.items():
        for bar in bars:
            ts = bar.ts_event
            iid = bar.bar_type.instrument_id
            date_instruments[ts].append(iid)
            date_bars[ts][iid] = bar

    rng = random.Random(seed)
    table = {}
    sorted_dates = sorted(date_instruments.keys())

    for i, ts in enumerate(sorted_dates):
        if i == 0:
            continue  # First day: V1 doesn't rebalance (no previous day)
        prev_ts = sorted_dates[i - 1]
        prev_iids = sorted(date_instruments[prev_ts], key=str)
        # Generate weights for the previous day's instruments
        raw = [rng.random() for _ in prev_iids]
        total_w = sum(raw)
        if total_w == 0:
            continue
        for iid, w in zip(prev_iids, raw):
            table[(prev_ts, iid)] = abs(w) / total_w

    return table


# ============================================================================
# V3 — ACTOR + STRATEGY SEPARATION
# ============================================================================

# Topic for Actor → Strategy communication
REBALANCE_TOPIC = "signal.rebalance"


class RebalanceSignal:
    """Lightweight signal published by SignalActor → consumed by ExecutionStrategy."""
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


class SignalActorConfig(ActorConfig, frozen=True):
    bar_types: list[str]
    random_seed: int = 42
    long_only: bool = True


class SignalActor(Actor):
    """Subscribes to bars, generates random weights, publishes RebalanceSignal."""

    def __init__(self, config: SignalActorConfig):
        super().__init__(config)
        self._bar_type_strs = config.bar_types
        self._rng = random.Random(config.random_seed)
        self._long_only = config.long_only
        self._current_date = None
        self._day_bars: dict[InstrumentId, Bar] = {}

    def on_start(self):
        for bt_str in self._bar_type_strs:
            bt = BarType.from_str(bt_str)
            self.subscribe_bars(bt)

    def on_bar(self, bar: Bar):
        bar_date = bar.ts_event
        if self._current_date is not None and bar_date != self._current_date:
            self._emit_signal()
            self._day_bars.clear()
        self._current_date = bar_date
        self._day_bars[bar.bar_type.instrument_id] = bar

    def on_stop(self):
        self._day_bars.clear()

    def _emit_signal(self):
        available = list(self._day_bars.keys())
        if not available:
            return
        raw = [self._rng.random() for _ in available]
        if self._long_only:
            raw = [abs(w) for w in raw]
        total = sum(raw)
        if total == 0:
            return
        weights = {iid: w / total for iid, w in zip(available, raw)}
        signal = RebalanceSignal(
            weights=weights,
            bar_snapshot=dict(self._day_bars),
            ts_event=self._current_date,
            ts_init=self._current_date,
        )
        self.msgbus.publish(REBALANCE_TOPIC, signal)


class ExecStrategyConfig(StrategyConfig, frozen=True):
    max_position_pct: float = 0.02
    order_lot_size: int = 100
    instrument_ids: list[str] = []


class ExecutionStrategy(Strategy):
    """Receives RebalanceSignal from the Actor and executes orders."""

    def __init__(self, config: ExecStrategyConfig):
        super().__init__(config)
        self._max_pos_pct = config.max_position_pct
        self._lot_size = config.order_lot_size
        self._instrument_id_strs = config.instrument_ids

    def on_start(self):
        self.msgbus.subscribe(REBALANCE_TOPIC, self._on_rebalance_signal)

    def _on_rebalance_signal(self, signal: RebalanceSignal):
        self._execute(signal)

    def on_stop(self):
        for iid_str in self._instrument_id_strs:
            self.close_all_positions(InstrumentId.from_str(iid_str))

    def _execute(self, signal: RebalanceSignal):
        available = list(signal.weights.keys())
        if not available:
            return
        first_bar = next(iter(signal.bar_snapshot.values()))
        account = self.portfolio.account(first_bar.bar_type.instrument_id.venue)
        if account is None:
            return
        equity = float(account.balance_total().as_double())
        if equity <= 0:
            return

        for iid, weight in signal.weights.items():
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
            current_qty = sum(float(p.signed_qty) for p in self.cache.positions_open(instrument_id=iid))
            delta = target_qty - current_qty
            if abs(delta) < self._lot_size:
                continue
            side = OrderSide.BUY if delta > 0 else OrderSide.SELL
            order = self.order_factory.market(
                instrument_id=iid, order_side=side,
                quantity=Quantity.from_int(int(abs(delta))),
                time_in_force=TimeInForce.DAY,
            )
            self.submit_order(order)


# ============================================================================
# Benchmark runner
# ============================================================================

@dataclass
class Result:
    variant: str
    n_stocks: int
    period: str
    total_bars: int
    engine_run_s: float
    us_per_bar: float  # microseconds per bar in engine_run


def run_variant(variant: str, n_stocks: int, start: str, end: str, label: str) -> Result:
    instruments, bars_per_inst, bar_type_strs, total_bars = load_data(n_stocks, start, end)
    engine = build_engine(instruments, bars_per_inst)

    if variant == "v1":
        strategy = V1Monolith(V1Config(bar_types=bar_type_strs))
        engine.add_strategy(strategy)

    elif variant == "v2":
        table = precompute_signals(bar_type_strs, bars_per_inst, seed=42)
        strategy = V2Precomputed(V2Config(bar_types=bar_type_strs), signal_table=table)
        engine.add_strategy(strategy)

    elif variant == "v3":
        actor = SignalActor(SignalActorConfig(bar_types=bar_type_strs))
        engine.add_actor(actor)
        iid_strs = [str(inst.id) for inst in instruments if inst in bars_per_inst]
        strategy = ExecutionStrategy(ExecStrategyConfig(instrument_ids=iid_strs))
        engine.add_strategy(strategy)

    t0 = time.perf_counter()
    engine.run()
    run_time = time.perf_counter() - t0

    engine.reset()
    engine.dispose()

    us_per_bar = (run_time / total_bars * 1_000_000) if total_bars else 0
    return Result(variant, n_stocks, label, total_bars, run_time, us_per_bar)


def main():
    parser = argparse.ArgumentParser(description="Architecture benchmark")
    parser.add_argument("--quick", action="store_true", help="Small grid for fast iteration")
    args = parser.parse_args()

    if args.quick:
        grid = [
            (50, "2020-01-01", "2020-06-30", "6mo"),
        ]
    else:
        grid = [
            (50,  "2020-01-01", "2020-06-30", "6mo"),
            (100, "2020-01-01", "2020-12-31", "1yr"),
            (200, "2020-01-01", "2020-12-31", "1yr"),
            (500, "2020-01-01", "2020-12-31", "1yr"),
        ]

    variants = ["v1", "v2", "v3"]
    results: list[Result] = []

    print("\n=== ARCHITECTURE BENCHMARK ===")
    print(f"{'variant':>10} {'stocks':>7} {'period':>6} {'bars':>9} {'run_time':>10} {'us/bar':>8}")
    print("-" * 60)

    for n, start, end, label in grid:
        for v in variants:
            sys.stdout.write(f"  running {v} {n} stocks {label} …")
            sys.stdout.flush()
            try:
                r = run_variant(v, n, start, end, label)
                results.append(r)
                print(f"\r{r.variant:>10} {r.n_stocks:>7} {r.period:>6} "
                      f"{r.total_bars:>9,} {r.engine_run_s:>9.2f}s {r.us_per_bar:>7.1f}")
            except Exception as e:
                print(f"\r  {v} {n} {label} ERROR: {e}")
        print("-" * 60)

    # Summary: relative speeds
    print("\n=== RELATIVE SPEED (vs V1 baseline) ===")
    print(f"{'stocks':>7} {'period':>6} {'V1 us/bar':>10} {'V2 us/bar':>10} {'V2 ratio':>9} "
          f"{'V3 us/bar':>10} {'V3 ratio':>9}")
    print("-" * 70)

    from itertools import groupby
    for (n, label), group in groupby(results, key=lambda r: (r.n_stocks, r.period)):
        row = {r.variant: r for r in group}
        if "v1" not in row:
            continue
        v1 = row["v1"].us_per_bar
        v2 = row.get("v2")
        v3 = row.get("v3")
        v2_str = f"{v2.us_per_bar:>9.1f}  {v2.us_per_bar/v1:>8.2f}x" if v2 else f"{'N/A':>9}  {'':>8}"
        v3_str = f"{v3.us_per_bar:>9.1f}  {v3.us_per_bar/v1:>8.2f}x" if v3 else f"{'N/A':>9}  {'':>8}"
        print(f"{n:>7} {label:>6} {v1:>9.1f}  {v2_str}  {v3_str}")

    # V3 bar subscription overhead
    print("\n=== V3 NOTE ===")
    print("In V3, the Actor subscribes to all N bar types (N handler calls per day).")
    print("The Strategy only receives 1 RebalanceSignal per day (sent via publish_data).")
    print("If the Actor's on_bar is cheap, the overhead should be minimal.")
    print("V3 advantage: clean separation for production (signal server vs execution server).")


if __name__ == "__main__":
    main()
