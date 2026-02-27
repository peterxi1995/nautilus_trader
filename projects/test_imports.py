#!/usr/bin/env python3
"""Test script to verify all imports work in VS Code debugger."""

import sys
print(f"Python executable: {sys.executable}")
print(f"Python version: {sys.version}")

# Test nautilus_trader imports
try:
    from nautilus_trader.core.data import Data
    print("✓ nautilus_trader.core.data.Data")
except ImportError as e:
    print(f"✗ nautilus_trader.core.data: {e}")
    sys.exit(1)

try:
    from nautilus_trader.backtest.engine import BacktestEngine
    print("✓ nautilus_trader.backtest.engine.BacktestEngine")
except ImportError as e:
    print(f"✗ nautilus_trader.backtest.engine: {e}")
    sys.exit(1)

try:
    from nautilus_trader.model.data import Bar
    print("✓ nautilus_trader.model.data.Bar")
except ImportError as e:
    print(f"✗ nautilus_trader.model.data: {e}")
    sys.exit(1)

# Test project imports
try:
    from datasources.taobao_daily import TaobaoDailySource
    print("✓ datasources.taobao_daily.TaobaoDailySource")
except ImportError as e:
    print(f"✗ datasources.taobao_daily: {e}")
    print(f"  sys.path: {sys.path[:2]}")
    sys.exit(1)

try:
    from strategies.daily_rebalance import DailyRebalanceStrategy
    print("✓ strategies.daily_rebalance.DailyRebalanceStrategy")
except ImportError as e:
    print(f"✗ strategies.daily_rebalance: {e}")
    sys.exit(1)

try:
    from run_backtest import _load_config
    print("✓ run_backtest._load_config")
except ImportError as e:
    print(f"✗ run_backtest: {e}")
    sys.exit(1)

print("\nAll imports successful! Debug configuration is working.")
