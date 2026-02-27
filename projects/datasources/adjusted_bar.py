"""Extended bar type with vwap and adj_factor.

NT's built-in ``Bar`` is a compiled Rust/Cython struct with a fixed memory
layout (OHLCV + timestamps).  Adding fields to it would require modifying the
Rust struct, Arrow/Parquet schema, PyO3 bindings, Cython wrappers and all
downstream serialisation — a very invasive change.

Instead we keep the standard ``Bar`` for the matching engine (which only needs
OHLCV) and carry the extra fields in a lightweight Python companion:

- **AdjustedBar** — wraps a ``Bar`` plus ``vwap`` and ``adj_factor``.
- **BarExtra**    — just the ``vwap`` + ``adj_factor`` for lookup tables.

Convention
----------
All prices stored in ``Bar`` are *forward-adjusted* prices::

    adjusted_price = raw_price × adj_factor
    adjusted_volume = raw_volume / adj_factor

To recover the *as-of-time* (unadjusted) values::

    raw_price  = adjusted_price  / adj_factor
    raw_volume = adjusted_volume × adj_factor

Volume
------
NT's ``Quantity`` type is fixed-point with configurable precision and fully
supports decimal values (e.g. ``Quantity.from_str("1.5")``).  Our data source
passes volume through the ``BarDataWrangler`` which preserves fractional
values when present.
"""

from __future__ import annotations

from nautilus_trader.model.data import Bar


class BarExtra:
    """Lightweight container for per-bar extra fields.

    Intended for use in lookup tables keyed by ``(InstrumentId, ts_event)``.

    Attributes
    ----------
    vwap : float
        Volume-weighted average price.  If not available from the data
        source, defaults to ``(high + low + close) / 3``.
    adj_factor : float
        Forward-adjustment factor.  Defaults to ``1.0`` (no adjustment).
    """

    __slots__ = ("vwap", "adj_factor")

    def __init__(self, vwap: float, adj_factor: float = 1.0) -> None:
        self.vwap = vwap
        self.adj_factor = adj_factor

    def __repr__(self) -> str:
        return f"BarExtra(vwap={self.vwap:.4f}, adj={self.adj_factor:.6f})"


class AdjustedBar:
    """Wraps an NT ``Bar`` with additional vwap and adj_factor fields.

    The wrapped ``bar`` is exactly what the matching engine uses.  The extra
    fields enable strategies to access richer data without modifying the
    framework.

    Attributes
    ----------
    bar : Bar
        The standard NautilusTrader bar (adjusted OHLCV).
    vwap : float
        Volume-weighted average price.  If missing at construction time,
        computed as ``(high + low + close) / 3``.
    adj_factor : float
        Forward-adjustment factor.  Defaults to 1.0.
    """

    __slots__ = ("bar", "vwap", "adj_factor")

    def __init__(
        self,
        bar: Bar,
        vwap: float | None = None,
        adj_factor: float = 1.0,
    ) -> None:
        self.bar = bar
        self.adj_factor = adj_factor

        if vwap is not None:
            self.vwap = vwap
        else:
            # Default: average of high, low, close
            self.vwap = (
                float(bar.high.as_double())
                + float(bar.low.as_double())
                + float(bar.close.as_double())
            ) / 3.0

    # -- convenience: recover unadjusted (as-of-time) values -----------------

    @property
    def unadjusted_open(self) -> float:
        """Raw open price before forward adjustment."""
        return float(self.bar.open.as_double()) / self.adj_factor

    @property
    def unadjusted_high(self) -> float:
        return float(self.bar.high.as_double()) / self.adj_factor

    @property
    def unadjusted_low(self) -> float:
        return float(self.bar.low.as_double()) / self.adj_factor

    @property
    def unadjusted_close(self) -> float:
        return float(self.bar.close.as_double()) / self.adj_factor

    @property
    def unadjusted_vwap(self) -> float:
        return self.vwap / self.adj_factor

    @property
    def unadjusted_volume(self) -> float:
        """Raw volume before forward adjustment."""
        return float(self.bar.volume.as_double()) * self.adj_factor

    def __repr__(self) -> str:
        return (
            f"AdjustedBar({self.bar.bar_type.instrument_id} "
            f"C={self.bar.close} vwap={self.vwap:.4f} adj={self.adj_factor:.6f})"
        )
