"""Abstract base for data sources that produce nautilus_trader data types."""

from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.instruments import Instrument


class DataSource(ABC):
    """Unified interface for loading market data into nautilus_trader.

    Subclasses implement ``_load_raw`` to return source-specific DataFrames and
    ``_build_instruments`` / ``_build_bars`` to convert those into nautilus
    objects.  Users interact only with ``instruments()`` and ``bars()``.

    The ``raw()`` method exposes the underlying data *before* any nautilus
    conversion, which is useful for inspection and debugging.
    """

    # -- public API -----------------------------------------------------------

    @abstractmethod
    def instruments(self) -> list[Instrument]:
        """Return all instruments available from this source."""

    @abstractmethod
    def bars(self, bar_type: BarType, instrument: Instrument) -> list[Bar]:
        """Return Bar objects for a single instrument.

        Parameters
        ----------
        bar_type : BarType
            The bar type specification (aggregation, price type, etc.).
        instrument : Instrument
            The instrument to load data for.
        """

    @abstractmethod
    def raw(self, symbol: str | None = None) -> pd.DataFrame:
        """Return the raw (pre-adjustment, pre-conversion) DataFrame.

        Parameters
        ----------
        symbol : str, optional
            If provided, filter to this symbol only.
        """
