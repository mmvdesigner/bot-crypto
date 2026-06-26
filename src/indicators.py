from __future__ import annotations

import pandas as pd


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """True Range = max(high-low, |high - prev_close|, |low - prev_close|)"""
    prev_close = close.shift(1)
    hl = high - low
    hc = (high - prev_close).abs()
    lc = (low - prev_close).abs()
    return pd.concat([hl, hc, lc], axis=1).max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    ATR de Wilder (RMA).
    Primeiro valor: média simples do TR dos primeiros `period` períodos.
    Em seguida: ATR = (ATR_prev * (period-1) + TR_current) / period
    """
    tr = true_range(df["high"], df["low"], df["close"])
    atr_series = tr.rolling(window=period, min_periods=period).mean()

    for i in range(period, len(tr)):
        atr_series.iloc[i] = (atr_series.iloc[i - 1] * (period - 1) + tr.iloc[i]) / period

    return atr_series


def volume_sma(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Média Simples do Volume."""
    return df["volume"].rolling(window=period, min_periods=period).mean()
