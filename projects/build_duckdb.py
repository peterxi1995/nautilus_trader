"""One-time ingestion: all daily_*.csv.gz/csv → a single DuckDB file.

The resulting table ``ohlcv`` stores pre-renamed columns (English names) for
every trading day across all symbols.  A composite primary key on
``(symbol, date)`` deduplicates overlapping source files and supports fast
range queries.

Usage
-----
    python projects/build_duckdb.py \\
        --data-dir /Users/rachelpeter/codebase/ocelot/data/raw_taobao \\
        --db-path  /Users/rachelpeter/codebase/ocelot/data/china_daily.duckdb

The script is idempotent: re-running it drops and recreates the table.
Typical runtime: ~30–60 s depending on disk speed.
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
import time

import duckdb
import pandas as pd

# ── column mapping (matches TaobaoDailySource) ────────────────────────────────
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

_SCHEMA = """
CREATE TABLE ohlcv (
    symbol       VARCHAR    NOT NULL,
    date         DATE       NOT NULL,
    open         DOUBLE     NOT NULL,
    high         DOUBLE     NOT NULL,
    low          DOUBLE     NOT NULL,
    close        DOUBLE     NOT NULL,
    volume       DOUBLE,
    turnover     DOUBLE,
    turnover_pct DOUBLE,
    limit_up     DOUBLE,
    limit_down   DOUBLE,
    avg_price    DOUBLE,
    prev_close   DOUBLE,
    is_halted    TINYINT    DEFAULT 0,
    is_st        TINYINT    DEFAULT 0,
    adj_factor   DOUBLE     DEFAULT 1.0,
    PRIMARY KEY (symbol, date)
);
"""

_INSERT_COLS = [
    "symbol", "date", "open", "high", "low", "close",
    "volume", "turnover", "turnover_pct", "limit_up", "limit_down",
    "avg_price", "prev_close", "is_halted", "is_st", "adj_factor",
]


def _collect_files(data_dir: str) -> list[str]:
    gz = sorted(glob.glob(os.path.join(data_dir, "daily_*.csv.gz")))
    csv = sorted(glob.glob(os.path.join(data_dir, "daily_*.csv")))
    gz_stems = {f.removesuffix(".gz") for f in gz}
    csv = [f for f in csv if f not in gz_stems]
    return sorted(gz + csv)


def _read_file(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, dtype={"股票代码": str})
    df.rename(columns=_COL_MAP, inplace=True)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    # Ensure all expected columns exist; fill missing with None
    for col in _INSERT_COLS:
        if col not in df.columns:
            df[col] = None
    return df[_INSERT_COLS]


def build(data_dir: str, db_path: str) -> None:
    files = _collect_files(data_dir)
    if not files:
        print(f"ERROR: no daily_* files found in {data_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(files)} source files → {db_path}")
    t0 = time.perf_counter()

    con = duckdb.connect(db_path)
    con.execute("DROP TABLE IF EXISTS ohlcv")
    con.execute(_SCHEMA)

    total_rows = 0
    for i, path in enumerate(files, 1):
        fname = os.path.basename(path)
        sys.stdout.write(f"\r  [{i:>2}/{len(files)}] {fname:<50}")
        sys.stdout.flush()
        df = _read_file(path)
        # Use DuckDB's fast Arrow/pandas→table insert
        con.execute(
            "INSERT OR REPLACE INTO ohlcv SELECT * FROM df"
        )
        total_rows += len(df)

    print(f"\r  Done. {total_rows:,} rows ingested (before dedup).")

    # Build an index to speed up (symbol, date) range scans
    print("  Creating index on (symbol, date) …", end="", flush=True)
    con.execute("CREATE INDEX IF NOT EXISTS idx_symbol_date ON ohlcv (symbol, date)")
    print(" done.")

    final_rows = con.execute("SELECT COUNT(*) FROM ohlcv").fetchone()[0]
    elapsed = time.perf_counter() - t0
    print(f"  Final row count (after dedup): {final_rows:,}  [{elapsed:.1f}s]")
    con.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Build china_daily.duckdb from CSV files.")
    parser.add_argument(
        "--data-dir",
        default="/Users/rachelpeter/codebase/ocelot/data/raw_taobao",
        help="Directory containing daily_*.csv.gz files",
    )
    parser.add_argument(
        "--db-path",
        default="/Users/rachelpeter/codebase/ocelot/data/china_daily.duckdb",
        help="Output DuckDB file path",
    )
    args = parser.parse_args()
    build(args.data_dir, args.db_path)


if __name__ == "__main__":
    main()
