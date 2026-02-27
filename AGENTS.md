# NautilusTrader Reference

Quick reference for working with the NautilusTrader framework.

## Architecture Overview

```
nautilus_trader/
  model/           Core types: Bar, Price, Quantity, Order, Instrument
  trading/         Strategy base class, trader
  backtest/        BacktestEngine, fill/fee/latency models
  execution/       MatchingCore, execution algorithms
  data/            Data engine, aggregation, subscriptions
  common/          Actor base class, MessageBus, components
  persistence/     Data loaders, ParquetDataCatalog, wranglers
  cache/           Cache facade for instruments, orders, positions
  examples/        Built-in example strategies
  test_kit/        Testing utilities, providers, stubs
crates/            Rust implementation (core, model, indicators)
```

Event flow: `DataEngine -> MessageBus -> Strategy.on_bar() -> submit_order() -> RiskEngine -> ExecutionEngine -> MatchingCore -> OrderFilled -> Strategy.on_order_filled()`

## Data Subscription and Request

All subscription/request methods are on the `Actor` base class (parent of `Strategy`).

```python
# Subscribe (streaming, calls on_bar/on_quote_tick/on_trade_tick handlers)
self.subscribe_bars(bar_type: BarType)
self.subscribe_quote_ticks(instrument_id: InstrumentId)
self.subscribe_trade_ticks(instrument_id: InstrumentId)

# Request (one-shot historical, fires callback or on_historical_data)
self.request_bars(bar_type, start, end=None, limit=0, callback=None)
self.request_aggregated_bars(bar_types, start, end=None)
```

Messages are published to MessageBus topics like `bars.{bar_type}` and routed to the strategy's handler methods.

## Bar Data Types

```python
from nautilus_trader.model.data import Bar, BarType, BarSpecification

spec = BarSpecification(1, BarAggregation.DAY, PriceType.LAST)
bar_type = BarType(
    instrument_id=InstrumentId.from_str("000001.XSHE"),
    bar_spec=spec,
    aggregation_source=AggregationSource.EXTERNAL,
)
# String form: "000001.XSHE-1-DAY-LAST-EXTERNAL"

bar = Bar(
    bar_type=bar_type,
    open=Price.from_str("16.65"),
    high=Price.from_str("16.95"),
    low=Price.from_str("16.55"),
    close=Price.from_str("16.87"),
    volume=Quantity.from_int(153023187),
    ts_event=ts_ns,   # uint64 nanoseconds
    ts_init=ts_ns,
)
```

Time-based aggregations: MILLISECOND, SECOND, MINUTE, HOUR, DAY, WEEK, MONTH.
Threshold-based: TICK, VOLUME, VALUE.

## Loading Bar Data from CSV

```python
from nautilus_trader.persistence.wranglers import BarDataWrangler

# DataFrame must have columns: open, high, low, close, volume (volume optional)
# Index must be named "timestamp" and be datetime
df = pd.read_csv("data.csv")
df["timestamp"] = pd.to_datetime(df["date"])
df = df.set_index("timestamp")
df = df[["open", "high", "low", "close", "volume"]]

wrangler = BarDataWrangler(bar_type, instrument)
bars = wrangler.process(df, default_volume=1_000_000.0)
```

## Instrument Definition (Equity)

```python
from nautilus_trader.model.instruments.equity import Equity

equity = Equity(
    instrument_id=InstrumentId(Symbol("000001"), Venue("XSHE")),
    raw_symbol=Symbol("000001"),
    currency=CNY,          # or USD, etc.
    price_precision=2,
    price_increment=Price.from_str("0.01"),
    lot_size=Quantity.from_int(100),  # board lot
    ts_event=0,
    ts_init=0,
    # Optional: margin_init, margin_maint, maker_fee, taker_fee
)
```

## Strategy Structure

```python
from nautilus_trader.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy

class MyConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType
    trade_size: Decimal

class MyStrategy(Strategy):
    def __init__(self, config: MyConfig):
        super().__init__(config)
        self.instrument_id = config.instrument_id
        self.bar_type = config.bar_type

    def on_start(self):
        self.instrument = self.cache.instrument(self.instrument_id)
        self.subscribe_bars(self.bar_type)

    def on_bar(self, bar: Bar):
        # Trading logic here
        order = self.order_factory.market(
            instrument_id=self.instrument_id,
            order_side=OrderSide.BUY,
            quantity=Quantity.from_int(100),
        )
        self.submit_order(order)

    def on_order_filled(self, event: OrderFilled):
        pass

    def on_stop(self):
        self.close_all_positions(self.instrument_id)
```

Key order methods: `submit_order()`, `modify_order()`, `cancel_order()`, `close_position()`, `close_all_positions()`.
Order types via factory: `order_factory.market()`, `.limit()`, `.stop_market()`, `.stop_limit()`.

## Backtest Setup

```python
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.backtest.config import BacktestEngineConfig

config = BacktestEngineConfig(
    trader_id=TraderId("BACKTESTER-001"),
    logging=LoggingConfig(log_level="INFO"),
    risk_engine=RiskEngineConfig(bypass=True),
)
engine = BacktestEngine(config=config)

# 1. Add venue
engine.add_venue(
    venue=Venue("XSHE"),
    oms_type=OmsType.NETTING,
    account_type=AccountType.CASH,
    base_currency=CNY,
    starting_balances=[Money(1_000_000, CNY)],
)

# 2. Add instruments
engine.add_instrument(equity)

# 3. Add data (list of Bar objects)
engine.add_data(bars)

# 4. Add strategy
engine.add_strategy(strategy)

# 5. Run
engine.run()

# 6. Results
print(engine.trader.generate_order_fills_report())
print(engine.trader.generate_positions_report())

engine.reset()
engine.dispose()
```

## Order Matching Simulation

### Fill Timing
Orders are matched on the **same bar** when fillability conditions are met. The matching engine calls `iterate(timestamp_ns)` during bar processing and evaluates all pending orders against the current bar's prices.

### Limit Order Fills
```
is_limit_fillable(side, price):
    BUY:  ask_raw <= price_raw   (crosses spread)
    SELL: bid_raw >= price_raw   (crosses spread)
    # If fill_limit_at_touch=True:
    BUY:  price_raw >= bid_raw   (at or inside bid)
    SELL: price_raw <= ask_raw   (at or inside ask)
```
Default: a limit order fills only when price crosses through it (strictly marketable). With `fill_limit_at_touch=True` in the FillModel, it fills when the price touches the limit price.

### Fill Price
- **Market orders**: filled at current bid (sell) or ask (buy).
- **Limit orders**: filled at the limit price (or better if price gapped through).
- When only bar data is available (no order book), the engine derives bid/ask from the bar's OHLC prices.

### Partial Fills and Volume
- If an OrderBook is provided (via `FillModel.get_orderbook_for_fill_simulation()`), fills respect the available volume at each price level. Orders larger than available volume receive partial fills.
- If no OrderBook is provided (default), orders fill in full.
- `BestPriceFillModel` creates an OrderBook with unlimited liquidity, filling everything at best prices.

### FillModel Configuration
```python
from nautilus_trader.backtest.models import FillModel

fill_model = FillModel(
    prob_fill_on_limit=0.0,  # probability of filling at touch
    prob_slippage=0.0,       # probability of 1-tick slippage
)

engine.add_venue(
    venue=...,
    fill_model=fill_model,
    ...
)
```

### OMS Types
- `OmsType.NETTING`: single position per instrument per strategy (net long/short).
- `OmsType.HEDGING`: each fill creates a separate position with unique PositionId.

## Data Catalog (Parquet)

```python
from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog

catalog = ParquetDataCatalog("./catalog_path")
catalog.write_data(instruments)
catalog.write_data(bars)

instruments = catalog.instruments()
bars = catalog.bars(bar_types=[...], start="2024-01-10", end="2024-01-15")
```

## Project Layout (our additions)

```
projects/
  datasources/
    base.py              Abstract DataSource interface
    duckdb_daily.py      DuckDB-backed source (primary, with bar_extras)
    taobao_daily.py      CSV-backed source (legacy)
    adjusted_bar.py      AdjustedBar + BarExtra types
  strategies/
    signals.py           RebalanceSignal, RiskSignal data classes
    signal_actor.py      SignalActor (bar subscriber → alpha → msgbus)
    execution_strategy.py ExecutionStrategy (signal consumer → orders)
    risk_actor.py        RiskActor (position monitor → risk signals)
    daily_rebalance.py   Legacy monolithic strategy (kept for reference)
  configs/               TOML configuration files
  run_backtest.py        Main entry point (Actor+Strategy wiring)
  bench_architecture.py  Architecture benchmark (V1/V2/V3)
```

## Actor + Strategy Architecture

Event flow:
```
DataEngine → bars → SignalActor.on_bar()
                   → RiskActor.on_bar()

SignalActor._emit_signal()
  → msgbus.publish("signal.rebalance", RebalanceSignal)
    → ExecutionStrategy._on_rebalance_signal() → submit_order()

RiskActor._check_position()
  → msgbus.publish("signal.risk", RiskSignal)
    → ExecutionStrategy._on_risk_signal() → close/reduce position
```

### Signal Publishing (msgbus.publish / msgbus.subscribe)

Actors cannot submit orders — only Strategies can.  Communication uses
`self.msgbus.publish(topic, payload)` and `self.msgbus.subscribe(topic, handler)`.

```python
# In Actor (signal producer):
self.msgbus.publish("signal.rebalance", signal)

# In Strategy (signal consumer):
def on_start(self):
    self.msgbus.subscribe("signal.rebalance", self._on_signal)
```

### AdjustedBar / BarExtra

NT's built-in `Bar` is a compiled Rust struct (OHLCV + timestamps).  We extend
it at the Python level with `AdjustedBar` and `BarExtra` for vwap + adj_factor:

```python
from datasources.adjusted_bar import AdjustedBar, BarExtra

# BarExtra is used in lookup tables: {(InstrumentId, ts_event_ns) → BarExtra}
extra = BarExtra(vwap=80.5, adj_factor=0.95)

# AdjustedBar wraps a Bar with extra fields
adj_bar = AdjustedBar(bar, vwap=80.5, adj_factor=0.95)
adj_bar.unadjusted_close  # = close / adj_factor
adj_bar.unadjusted_volume # = volume * adj_factor
```

Convention: `adjusted_price = raw_price × adj_factor`, `adjusted_volume = raw_volume / adj_factor`.

### Volume as Decimal

NT's `Quantity` type is fixed-point with configurable precision and supports
decimal values: `Quantity.from_str("1.5")` works.  The precision is inferred
from the string representation.
