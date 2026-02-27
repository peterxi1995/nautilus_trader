"""Data source for Taobao China daily stock data (CSV/gzip files)."""

from __future__ import annotations

import glob
import os
from functools import lru_cache

import pandas as pd

from nautilus_trader.model.currencies import CNY
from nautilus_trader.model.data import Bar, BarSpecification, BarType
from nautilus_trader.model.enums import AggregationSource, BarAggregation, PriceType
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments.equity import Equity
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.persistence.wranglers import BarDataWrangler

from datasources.base import DataSource

# Chinese exchange suffix -> ISO 10383 MIC
_VENUE_MAP = {
    "SZ": "XSHE",  # Shenzhen
    "SH": "XSHG",  # Shanghai
    "BJ": "XBSE",  # Beijing
}

# Column name mapping (Chinese -> internal)
_COL_MAP = {
    "股票代码": "symbol",
    "日期": "date",
    "开盘价": "open",
    "最高价": "high",
    "最低价": "low",
    "收盘价": "close",
    "成交量(股)": "volume",
    "成交额(元)": "turnover",
    "换手率(%)": "turnover_pct",
    "涨停价": "limit_up",
    "跌停价": "limit_down",
    "均价": "avg_price",
    "前交易日收盘价": "prev_close",
    "是否停牌": "is_halted",
    "是否ST": "is_st",
    "复权因子": "adj_factor",
}


def _parse_venue(raw_symbol: str) -> str:
    """Convert '000001.SZ' suffix to ISO venue code."""
    suffix = raw_symbol.rsplit(".", 1)[-1]
    return _VENUE_MAP.get(suffix, suffix)


def _parse_code(raw_symbol: str) -> str:
    """Extract numeric code from '000001.SZ'."""
    return raw_symbol.rsplit(".", 1)[0]


class TaobaoDailySource(DataSource):
    """Loads Taobao-format China daily equity CSVs.

    Parameters
    ----------
    data_dir : str
        Path to the directory containing ``daily_*.csv`` or ``daily_*.csv.gz``
        files.
    start : str, optional
        Start date filter (inclusive), e.g. ``"2020-01-01"``.
    end : str, optional
        End date filter (inclusive), e.g. ``"2020-06-30"``.
    exclude_halted : bool
        Drop rows where the stock is halted (default True).
    exclude_st : bool
        Drop rows where the stock has ST status (default True).
    price_precision : int
        Decimal precision for prices (default 2).
    """

    def __init__(
        self,
        data_dir: str,
        start: str | None = None,
        end: str | None = None,
        exclude_halted: bool = True,
        exclude_st: bool = True,
        price_precision: int = 2,
    ):
        self._data_dir = data_dir
        self._start = start
        self._end = end
        self._exclude_halted = exclude_halted
        self._exclude_st = exclude_st
        self._price_precision = price_precision

    # -- internal loading -----------------------------------------------------

    @lru_cache(maxsize=1)  # noqa: B019
    def _load_all(self) -> pd.DataFrame:
        """Read and concatenate all daily CSVs in the data directory."""
        # Collect files, preferring .csv.gz over .csv when both exist
        gz_files = sorted(glob.glob(os.path.join(self._data_dir, "daily_*.csv.gz")))
        csv_files = sorted(glob.glob(os.path.join(self._data_dir, "daily_*.csv")))
        # Exclude plain CSVs that have a .gz counterpart
        gz_stems = {f.removesuffix(".gz") for f in gz_files}
        csv_files = [f for f in csv_files if f not in gz_stems]
        files = sorted(gz_files + csv_files)
        if not files:
            raise FileNotFoundError(f"No daily_* files found in {self._data_dir}")

        frames = []
        for f in files:
            df = pd.read_csv(f, dtype={"股票代码": str})
            frames.append(df)
        combined = pd.concat(frames, ignore_index=True)

        # Rename columns to English
        combined.rename(columns=_COL_MAP, inplace=True)

        # Parse dates and deduplicate (guard against overlapping files)
        combined["date"] = pd.to_datetime(combined["date"])
        combined.drop_duplicates(subset=["symbol", "date"], keep="last", inplace=True)

        # Apply date filters
        if self._start:
            combined = combined[combined["date"] >= self._start]
        if self._end:
            combined = combined[combined["date"] <= self._end]

        # Filter halted / ST
        if self._exclude_halted:
            combined = combined[combined["is_halted"] == 0]
        if self._exclude_st:
            combined = combined[combined["is_st"] == 0]

        combined.sort_values(["symbol", "date"], inplace=True)
        combined.reset_index(drop=True, inplace=True)
        return combined

    def _adjusted(self, df: pd.DataFrame) -> pd.DataFrame:
        """Apply forward adjustment: price*adj, volume/adj."""
        out = df.copy()
        adj = out["adj_factor"]
        for col in ("open", "high", "low", "close"):
            out[col] = out[col] * adj
        out["volume"] = out["volume"] / adj
        return out

    # -- public API -----------------------------------------------------------

    def raw(self, symbol: str | None = None) -> pd.DataFrame:
        df = self._load_all()
        if symbol is not None:
            df = df[df["symbol"] == symbol]
        return df.copy()

    def instruments(self) -> list[Equity]:
        df = self._load_all()
        symbols = df["symbol"].unique()
        result = []
        for sym in sorted(symbols):
            code = _parse_code(sym)
            venue = _parse_venue(sym)
            result.append(
                Equity(
                    instrument_id=InstrumentId(Symbol(code), Venue(venue)),
                    raw_symbol=Symbol(code),
                    currency=CNY,
                    price_precision=self._price_precision,
                    price_increment=Price(10 ** (-self._price_precision), self._price_precision),
                    lot_size=Quantity.from_int(100),
                    ts_event=0,
                    ts_init=0,
                )
            )
        return result

    def bars(self, bar_type: BarType, instrument: Equity) -> list[Bar]:
        code = str(instrument.raw_symbol)
        venue_str = str(instrument.venue)
        # Reverse-map ISO venue to source suffix
        suffix_map = {v: k for k, v in _VENUE_MAP.items()}
        suffix = suffix_map.get(venue_str, venue_str)
        source_symbol = f"{code}.{suffix}"

        df = self._load_all()
        df = df[df["symbol"] == source_symbol]
        if df.empty:
            return []

        # Apply adjustment
        df = self._adjusted(df)

        # Prepare for BarDataWrangler: needs open, high, low, close, volume with timestamp index
        wrangler_df = df[["date", "open", "high", "low", "close", "volume"]].copy()
        wrangler_df.rename(columns={"date": "timestamp"}, inplace=True)
        wrangler_df.set_index("timestamp", inplace=True)

        wrangler = BarDataWrangler(bar_type, instrument)
        return wrangler.process(wrangler_df)

    # -- convenience ----------------------------------------------------------

    def default_bar_type(self, instrument: Equity) -> BarType:
        """Return the standard daily bar type for an instrument."""
        return BarType(
            instrument_id=instrument.id,
            bar_spec=BarSpecification(1, BarAggregation.DAY, PriceType.LAST),
            aggregation_source=AggregationSource.EXTERNAL,
        )

    def all_bars(self) -> dict[Equity, list[Bar]]:
        """Load bars for every instrument. Returns {instrument: bars_list}."""
        instruments = self.instruments()
        result = {}
        for inst in instruments:
            bt = self.default_bar_type(inst)
            bars = self.bars(bt, inst)
            if bars:
                result[inst] = bars
        return result
