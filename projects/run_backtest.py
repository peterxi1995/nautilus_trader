#!/usr/bin/env python3
"""Run a backtest from a TOML config file.

Usage:
    python run_backtest.py configs/backtest_daily_rebalance.toml
"""

from __future__ import annotations

import sys
import time
import tomllib

import pandas as pd

from nautilus_trader.backtest.config import BacktestEngineConfig
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import LoggingConfig, RiskEngineConfig
from nautilus_trader.model.currencies import CNY
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import TraderId, Venue
from nautilus_trader.model.objects import Money

from datasources.taobao_daily import TaobaoDailySource
from strategies.daily_rebalance import DailyRebalanceConfig, DailyRebalanceStrategy


def _load_config(path: str) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _build_datasource(cfg: dict) -> TaobaoDailySource:
    ds_cfg = cfg["datasource"]
    return TaobaoDailySource(
        data_dir=ds_cfg["data_dir"],
        start=ds_cfg.get("start"),
        end=ds_cfg.get("end"),
        exclude_halted=ds_cfg.get("exclude_halted", True),
        exclude_st=ds_cfg.get("exclude_st", True),
    )


_OMS_MAP = {"NETTING": OmsType.NETTING, "HEDGING": OmsType.HEDGING}
_ACCT_MAP = {"CASH": AccountType.CASH, "MARGIN": AccountType.MARGIN}


def run(config_path: str):
    cfg = _load_config(config_path)
    ds_cfg = cfg["datasource"]
    venue_cfg = cfg["venue"]
    strat_cfg = cfg["strategy"]
    engine_cfg = cfg["engine"]

    max_instruments = ds_cfg.get("max_instruments", 0)

    # -- data source ----------------------------------------------------------
    print("Loading data source...")
    t0 = time.time()
    source = _build_datasource(cfg)
    instruments = source.instruments()
    if max_instruments > 0:
        instruments = instruments[:max_instruments]
    print(f"  {len(instruments)} instruments loaded in {time.time() - t0:.1f}s")

    # -- engine ---------------------------------------------------------------
    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id=TraderId(engine_cfg.get("trader_id", "BACKTESTER-001")),
            logging=LoggingConfig(log_level=engine_cfg.get("log_level", "INFO")),
            risk_engine=RiskEngineConfig(bypass=engine_cfg.get("bypass_risk", True)),
        ),
    )

    # -- venues (auto-detected from instruments) ------------------------------
    currency = CNY
    oms_type = _OMS_MAP[venue_cfg.get("oms_type", "NETTING")]
    acct_type = _ACCT_MAP[venue_cfg.get("account_type", "CASH")]
    starting_balance = venue_cfg.get("starting_balance", 10_000_000)

    venues_added = set()
    for inst in instruments:
        v = inst.id.venue
        if v not in venues_added:
            engine.add_venue(
                venue=v,
                oms_type=oms_type,
                account_type=acct_type,
                base_currency=currency,
                starting_balances=[Money(starting_balance, currency)],
            )
            venues_added.add(v)

    # -- instruments & data ---------------------------------------------------
    print("Building bars...")
    t0 = time.time()
    bar_type_strs = []
    for inst in instruments:
        engine.add_instrument(inst)
        bt = source.default_bar_type(inst)
        bars = source.bars(bt, inst)
        if not bars:
            continue
        engine.add_data(bars)
        bar_type_strs.append(str(bt))

    print(f"  {len(bar_type_strs)} bar series added in {time.time() - t0:.1f}s")

    if not bar_type_strs:
        print("No bar data available. Aborting.")
        engine.dispose()
        return

    # -- strategy -------------------------------------------------------------
    strategy = DailyRebalanceStrategy(
        config=DailyRebalanceConfig(
            bar_types=bar_type_strs,
            random_seed=strat_cfg.get("random_seed", 42),
            max_position_pct=strat_cfg.get("max_position_pct", 0.02),
            order_lot_size=strat_cfg.get("order_lot_size", 100),
            long_only=strat_cfg.get("long_only", True),
        ),
    )
    engine.add_strategy(strategy)

    # -- run ------------------------------------------------------------------
    print("Running backtest...")
    t0 = time.time()
    engine.run()
    elapsed = time.time() - t0
    print(f"Backtest completed in {elapsed:.1f}s")

    # -- results --------------------------------------------------------------
    with pd.option_context("display.max_rows", 50, "display.max_columns", None, "display.width", 200):
        for v in sorted(venues_added, key=str):
            print(f"\n=== Account Report ({v}) ===")
            print(engine.trader.generate_account_report(v))
        print("\n=== Order Fills Report ===")
        print(engine.trader.generate_order_fills_report())
        print("\n=== Positions Report ===")
        print(engine.trader.generate_positions_report())

    engine.reset()
    engine.dispose()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <config.toml>")
        sys.exit(1)
    run(sys.argv[1])
