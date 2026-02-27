#!/usr/bin/env python3
"""Quick test of the new Actor+Strategy+RiskActor architecture."""

import sys
import time

sys.path.insert(0, ".")

from nautilus_trader.backtest.config import BacktestEngineConfig
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import LoggingConfig, RiskEngineConfig
from nautilus_trader.model.currencies import CNY
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.objects import Money

from datasources.duckdb_daily import DuckDBDailySource
from strategies.execution_strategy import ExecutionStrategy, ExecutionStrategyConfig
from strategies.risk_actor import RiskActor, RiskActorConfig
from strategies.signal_actor import SignalActor, SignalActorConfig

DB_PATH = "/Users/rachelpeter/codebase/ocelot/data/china_daily.duckdb"
N_STOCKS = 50


def main():
    source = DuckDBDailySource(db_path=DB_PATH, start="2020-01-01", end="2020-06-30")
    instruments = source.instruments()[:N_STOCKS]
    print(f"{len(instruments)} instruments")

    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id=TraderId("TEST-001"),
            logging=LoggingConfig(log_level="ERROR"),
            risk_engine=RiskEngineConfig(bypass=True),
        )
    )

    venues_added = set()
    for inst in instruments:
        v = inst.id.venue
        if v not in venues_added:
            engine.add_venue(
                venue=v,
                oms_type=OmsType.NETTING,
                account_type=AccountType.CASH,
                base_currency=CNY,
                starting_balances=[Money(10_000_000, CNY)],
            )
            venues_added.add(v)

    bar_type_strs = []
    bar_extras_all = {}
    total_bars = 0

    for inst in instruments:
        engine.add_instrument(inst)
        bt = source.default_bar_type(inst)
        bars = source.bars(bt, inst)
        if bars:
            engine.add_data(bars)
            bar_type_strs.append(str(bt))
            total_bars += len(bars)
            extras = source.bar_extras(inst, bars)
            bar_extras_all.update(extras)

    print(f"{len(bar_type_strs)} bar series, {total_bars:,} bars, {len(bar_extras_all):,} extras")

    # Wire up Actor + Strategy architecture
    actor = SignalActor(SignalActorConfig(bar_types=bar_type_strs))
    actor.set_bar_extras(bar_extras_all)
    engine.add_actor(actor)

    risk = RiskActor(
        RiskActorConfig(
            bar_types=bar_type_strs,
            stop_loss_pct=0.10,
            take_profit_pct=0.20,
        )
    )
    engine.add_actor(risk)

    iid_strs = [str(inst.id) for inst in instruments]
    strat = ExecutionStrategy(
        ExecutionStrategyConfig(instrument_ids=iid_strs)
    )
    engine.add_strategy(strat)

    # Run
    t0 = time.perf_counter()
    engine.run()
    elapsed = time.perf_counter() - t0

    fills = engine.trader.generate_order_fills_report()
    print(f"Engine run: {elapsed:.2f}s ({total_bars / elapsed:.0f} bars/s)")
    print(f"Orders filled: {len(fills)}")
    print(f"us/bar: {elapsed / total_bars * 1e6:.1f}")

    engine.reset()
    engine.dispose()


if __name__ == "__main__":
    main()
