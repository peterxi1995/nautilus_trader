#!/usr/bin/env python3
"""Performance profiling for the daily rebalance backtest.

Tests a grid of (n_stocks x date_range) combinations and measures time
spent in each phase. Also runs cProfile on a representative medium case
to pinpoint hotspots.

Usage:
    python profile_backtest.py                       # grid + profile (duckdb)
    python profile_backtest.py --source csv          # use CSV source
    python profile_backtest.py --grid-only           # grid timing only
    python profile_backtest.py --profile-only        # cProfile only
    python profile_backtest.py --compare             # run grid for BOTH sources
"""

from __future__ import annotations

import argparse
import cProfile
import io
import pstats
import sys
import time
from dataclasses import dataclass, field

from nautilus_trader.backtest.config import BacktestEngineConfig
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import LoggingConfig, RiskEngineConfig
from nautilus_trader.model.currencies import CNY
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import TraderId, Venue
from nautilus_trader.model.objects import Money

from datasources.duckdb_daily import DuckDBDailySource
from datasources.taobao_daily import TaobaoDailySource
from strategies.daily_rebalance import DailyRebalanceConfig, DailyRebalanceStrategy

DATA_DIR = "/Users/rachelpeter/codebase/ocelot/data/raw_taobao"
DB_PATH = "/Users/rachelpeter/codebase/ocelot/data/china_daily.duckdb"
STARTING_BALANCE = 50_000_000

# ---------------------------------------------------------------------------
# Grid definition
# ---------------------------------------------------------------------------

STOCK_COUNTS = [10, 50, 100, 200, 500]

DATE_RANGES = [
    ("2020-01-01", "2020-01-31", "1mo"),
    ("2020-01-01", "2020-06-30", "6mo"),
    ("2020-01-01", "2020-12-31", "1yr"),
    ("2019-01-01", "2020-12-31", "2yr"),
]

# Case used for deep cProfile analysis
PROFILE_CASE = dict(n_stocks=100, start="2020-01-01", end="2020-12-31")


# ---------------------------------------------------------------------------
# Timing helpers
# ---------------------------------------------------------------------------

@dataclass
class PhaseTimer:
    phases: dict[str, float] = field(default_factory=dict)
    _t: float = 0.0

    def start(self):
        self._t = time.perf_counter()

    def stop(self, name: str):
        self.phases[name] = time.perf_counter() - self._t

    def total(self) -> float:
        return sum(self.phases.values())


def _fmt(value: float, width: int = 9) -> str:
    """Format seconds into a fixed-width human-readable column."""
    if value < 0.0005:
        return f"{'<1ms':>{width}}"
    if value < 1.0:
        return f"{value * 1000:>{width - 2}.0f}ms"
    return f"{value:>{width}.2f}s"


# ---------------------------------------------------------------------------
# Single backtest run with phase timing
# ---------------------------------------------------------------------------

def run_timed(n_stocks: int, start: str, end: str, source_type: str = "duckdb") -> PhaseTimer:
    timer = PhaseTimer()

    # ---- Phase 1: data source initialisation + instrument list -------------
    timer.start()
    if source_type == "duckdb":
        source = DuckDBDailySource(
            db_path=DB_PATH, start=start, end=end,
            exclude_halted=True, exclude_st=True,
        )
    else:
        source = TaobaoDailySource(
            data_dir=DATA_DIR, start=start, end=end,
            exclude_halted=True, exclude_st=True,
        )
    instruments = source.instruments()
    instruments = instruments[:n_stocks]
    n_actual = len(instruments)
    timer.stop("data_load")

    # ---- Phase 2: build Bar objects (wrangling) ----------------------------
    timer.start()
    bar_type_strs = []
    bars_per_inst: dict = {}
    total_bars = 0
    for inst in instruments:
        bt = source.default_bar_type(inst)
        bars = source.bars(bt, inst)
        if bars:
            bars_per_inst[inst] = bars
            bar_type_strs.append(str(bt))
            total_bars += len(bars)
    timer.stop("bar_build")

    if not bar_type_strs:
        return timer

    # ---- Phase 3: engine setup ---------------------------------------------
    timer.start()
    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id=TraderId("PROFILER-001"),
            logging=LoggingConfig(log_level="ERROR"),
            risk_engine=RiskEngineConfig(bypass=True),
        )
    )
    venues_added: set = set()
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
    timer.stop("engine_setup")

    # ---- Phase 4: strategy wiring ------------------------------------------
    timer.start()
    strategy = DailyRebalanceStrategy(
        config=DailyRebalanceConfig(
            bar_types=bar_type_strs,
            random_seed=42,
            max_position_pct=0.02,
            order_lot_size=100,
            long_only=True,
        )
    )
    engine.add_strategy(strategy)
    timer.stop("strategy_setup")

    # ---- Phase 5: backtest run ---------------------------------------------
    timer.start()
    engine.run()
    timer.stop("engine_run")

    engine.reset()
    engine.dispose()

    timer.phases["_n_actual"] = n_actual
    timer.phases["_total_bars"] = total_bars
    return timer


# ---------------------------------------------------------------------------
# Grid benchmark
# ---------------------------------------------------------------------------

def run_grid(source_type: str = "duckdb"):
    print(f"\n=== GRID BENCHMARK  [source={source_type}] ===")
    c = 9  # column width
    header = (
        f"{'stocks':>7}  {'period':>5} | "
        f"{'data_load':>{c}} {'bar_build':>{c}} {'eng_setup':>{c}} "
        f"{'strat':>{c}} {'run':>{c}} | "
        f"{'TOTAL':>{c}}  {'bars':>9}"
    )
    div = "-" * len(header)
    print(div)
    print(header)
    print(div)

    prev_label = None
    for start, end, label in DATE_RANGES:
        if prev_label and prev_label != label:
            print(div)
        prev_label = label
        for n in STOCK_COUNTS:
            sys.stdout.write(f"  running {n:>4} stocks  {label} ...")
            sys.stdout.flush()
            try:
                t = run_timed(n, start, end, source_type=source_type)
            except Exception as e:
                print(f"\r  {n:>4} stocks  {label}  ERROR: {e}")
                continue

            n_actual = int(t.phases.pop("_n_actual", n))
            total_bars = int(t.phases.pop("_total_bars", 0))
            print(
                f"\r{n_actual:>7}  {label:>5} | "
                f"{_fmt(t.phases.get('data_load', 0), c)} "
                f"{_fmt(t.phases.get('bar_build', 0), c)} "
                f"{_fmt(t.phases.get('engine_setup', 0), c)} "
                f"{_fmt(t.phases.get('strategy_setup', 0), c)} "
                f"{_fmt(t.phases.get('engine_run', 0), c)} | "
                f"{_fmt(t.total(), c)}  {total_bars:>9,}"
            )
    print(div)
    if source_type == "duckdb":
        print("\nNote: data_load = DuckDB SELECT DISTINCT symbol (sub-ms to mid-seconds at scale).")
        print("      bar_build = 1 targeted SQL query per instrument + BarDataWrangler.")
    else:
        print("\nNote: data_load re-reads all 44 CSV.gz files (flat ~17s per date range).")
        print("      bar_build runs BarDataWrangler once per instrument; df[symbol==x] is a bottleneck.")


# ---------------------------------------------------------------------------
# Deep cProfile
# ---------------------------------------------------------------------------

def run_profile(source_type: str = "duckdb"):
    n = PROFILE_CASE["n_stocks"]
    start = PROFILE_CASE["start"]
    end = PROFILE_CASE["end"]
    print(f"\n=== cPROFILE: {n} stocks, {start} to {end}  [source={source_type}] ===")

    pr = cProfile.Profile()
    pr.enable()
    run_timed(n, start, end, source_type=source_type)
    pr.disable()

    for sort_key, label in [("cumulative", "cumulative time"), ("tottime", "own time (hotspots)")]:
        buf = io.StringIO()
        ps = pstats.Stats(pr, stream=buf)
        ps.strip_dirs()
        ps.sort_stats(sort_key)
        ps.print_stats(25)
        print(f"--- Top 25 by {label} ---")
        skip_next = False
        for line in buf.getvalue().splitlines():
            stripped = line.strip()
            if (stripped.startswith("ncalls") or
                    stripped == "" or
                    stripped.startswith("Ordered by") or
                    stripped.startswith("List reduced") or
                    "function calls" in stripped or
                    (stripped.startswith("(") and "profile" in stripped)):
                continue
            print(line)
        print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--grid-only", action="store_true")
    mode_group.add_argument("--profile-only", action="store_true")
    mode_group.add_argument("--compare", action="store_true",
                            help="Run grid for both CSV and DuckDB sources side-by-side")
    parser.add_argument("--source", choices=["csv", "duckdb"], default="duckdb",
                        help="Data source backend (default: duckdb)")
    args = parser.parse_args()

    if args.compare:
        run_grid(source_type="csv")
        run_grid(source_type="duckdb")
    else:
        if not args.profile_only:
            run_grid(source_type=args.source)
        if not args.grid_only:
            run_profile(source_type=args.source)
