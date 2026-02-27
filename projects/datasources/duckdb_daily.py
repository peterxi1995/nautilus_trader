"""Data source backed by a pre-built DuckDB file.

The database is expected to contain a table ``ohlcv`` built by
``projects/build_duckdb.py``.  All heavy lifting (CSV parsing, gzip
decompression, deduplication) is done once at ingest time.  Subsequent
queries use DuckDB's vectorised engine with push-down predicates on
``symbol`` and ``date``.
"""

from __future__ import annotations

import threading

import duckdb
import pandas as pd

from nautilus_trader.model.currencies import CNY
from nautilus_trader.model.data import Bar, BarSpecification, BarType
from nautilus_trader.model.enums import AggregationSource, BarAggregation, PriceType
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments.equity import Equity
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.persistence.wranglers import BarDataWrangler

from datasources.base import DataSource

# Chinese exchange suffix -> ISO 10383 MIC  (same as TaobaoDailySource)
_VENUE_MAP = {
    "SZ": "XSHE",
    "SH": "XSHG",
    "BJ": "XBSE",
}
_SUFFIX_MAP = {v: k for k, v in _VENUE_MAP.items()}


def _parse_venue(raw_symbol: str) -> str:
    return _VENUE_MAP.get(raw_symbol.rsplit(".", 1)[-1], raw_symbol.rsplit(".", 1)[-1])


def _parse_code(raw_symbol: str) -> str:
    return raw_symbol.rsplit(".", 1)[0]


class DuckDBDailySource(DataSource):
    """Loads China daily equity OHLCV data from a DuckDB file.

    Parameters
    ----------
    db_path : str
        Path to the DuckDB file built by ``build_duckdb.py``.
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
        db_path: str,
        start: str | None = None,
        end: str | None = None,
        exclude_halted: bool = True,
        exclude_st: bool = True,
        price_precision: int = 2,
    ):
        self._db_path = db_path
        self._start = start
        self._end = end
        self._exclude_halted = exclude_halted
        self._exclude_st = exclude_st
        self._price_precision = price_precision
        # DuckDB connections are not thread-safe; use one per thread.
        self._local = threading.local()

    # -- connection management ------------------------------------------------

    def _con(self) -> duckdb.DuckDBPyConnection:
        """Return a thread-local read-only connection."""
        if not hasattr(self._local, "con"):
            self._local.con = duckdb.connect(self._db_path, read_only=True)
        return self._local.con

    # -- shared WHERE clause helpers -----------------------------------------

    def _date_predicates(self) -> tuple[str, list]:
        """Build SQL predicates and params for date + flag filters."""
        clauses = []
        params: list = []
        if self._start:
            clauses.append("date >= ?")
            params.append(self._start)
        if self._end:
            clauses.append("date <= ?")
            params.append(self._end)
        if self._exclude_halted:
            clauses.append("is_halted = 0")
        if self._exclude_st:
            clauses.append("is_st = 0")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    # -- public API -----------------------------------------------------------

    def raw(self, symbol: str | None = None) -> pd.DataFrame:
        where, params = self._date_predicates()
        if symbol is not None:
            sep = "AND" if where else "WHERE"
            where += f" {sep} symbol = ?"
            params.append(symbol)
        return self._con().execute(f"SELECT * FROM ohlcv {where}", params).df()

    def instruments(self) -> list[Equity]:
        """Return one Equity per distinct symbol that has trading data in the
        requested date range."""
        where, params = self._date_predicates()
        rows = self._con().execute(
            f"SELECT DISTINCT symbol FROM ohlcv {where} ORDER BY symbol",
            params,
        ).fetchall()
        result = []
        for (sym,) in rows:
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
        suffix = _SUFFIX_MAP.get(venue_str, venue_str)
        source_symbol = f"{code}.{suffix}"

        # Adjusted OHLCV via SQL — DuckDB does this in one vectorised pass
        date_clauses = []
        params: list[str] = [source_symbol]
        if self._start:
            date_clauses.append("date >= ?")
            params.append(self._start)
        if self._end:
            date_clauses.append("date <= ?")
            params.append(self._end)

        extra = (" AND " + " AND ".join(date_clauses)) if date_clauses else ""
        sql = f"""
            SELECT
                date,
                open  * adj_factor AS open,
                high  * adj_factor AS high,
                low   * adj_factor AS low,
                close * adj_factor AS close,
                volume / adj_factor AS volume
            FROM ohlcv
            WHERE symbol = ?{extra}
              AND is_halted = 0
              AND is_st    = 0
            ORDER BY date
        """
        df = self._con().execute(sql, params).df()
        if df.empty:
            return []

        df["date"] = pd.to_datetime(df["date"])
        df = df.rename(columns={"date": "timestamp"}).set_index("timestamp")

        wrangler = BarDataWrangler(bar_type, instrument)
        return wrangler.process(df)

    # -- convenience ----------------------------------------------------------

    def default_bar_type(self, instrument: Equity) -> BarType:
        return BarType(
            instrument_id=instrument.id,
            bar_spec=BarSpecification(1, BarAggregation.DAY, PriceType.LAST),
            aggregation_source=AggregationSource.EXTERNAL,
        )

    def all_bars(self) -> dict[Equity, list[Bar]]:
        instruments = self.instruments()
        return {
            inst: bars
            for inst in instruments
            if (bars := self.bars(self.default_bar_type(inst), inst))
        }
