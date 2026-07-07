from __future__ import annotations

from typing import Optional, Tuple

import pandas as pd

from src.indicators import atr, volume_sma

# ---------------------------------------------------------------------------
#  Parâmetros (podem vir de config futuramente)
# ---------------------------------------------------------------------------
ATR_PERIOD = 14
VOLUME_SMA_PERIOD = 20
SQUEEZE_LOOKBACK = 20
SQUEEZE_TOLERANCE = 1.02
VOLUME_MULTIPLIER = 1.7
SL_ATR_MULT = 1.2
TP_ATR_MULT = 3.0


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Adiciona as colunas ATR, Volume SMA e is_squeeze ao DataFrame."""
    df = df.copy()
    df["atr"] = atr(df, ATR_PERIOD)
    df["volume_sma"] = volume_sma(df, VOLUME_SMA_PERIOD)
    df["is_squeeze"] = detect_squeeze(df["atr"], SQUEEZE_LOOKBACK)
    return df


def detect_squeeze(atr_series: pd.Series, lookback: int = SQUEEZE_LOOKBACK) -> pd.Series:
    """
    Retorna uma Series booleana indicando se o ATR atual está dentro de 2%
    do menor ATR dos últimos lookback períodos.
    Equivale a `atr <= ta.lowest(atr, 20) * 1.02` no Pine Script.
    """
    lowest_atr = atr_series.rolling(window=lookback, min_periods=lookback).min()
    return atr_series <= lowest_atr * SQUEEZE_TOLERANCE


def check_entry(
    df: pd.DataFrame,
    squeeze_high: float,
    squeeze_low: float,
) -> Tuple[Optional[str], Optional[float]]:
    """
    Verifica gatilho de entrada no candle seguinte ao squeeze.

    Retorna (signal, price) ou (None, None).
    signal = "LONG" | "SHORT"
    """
    last = df.iloc[-1]
    close = last["close"]
    volume = last["volume"]
    vol_sma = last["volume_sma"]

    volume_ok = volume > vol_sma * VOLUME_MULTIPLIER

    if close > squeeze_high and volume_ok:
        return "LONG", close

    if close < squeeze_low and volume_ok:
        return "SHORT", close

    return None, None


def calculate_sl_tp(
    entry_price: float,
    atr_value: float,
    side: str,
) -> Tuple[float, float]:
    """
    Calcula Stop Loss e Take Profit com base no ATR da entrada.

    LONG:  SL = entry - (ATR * 1.2)   TP = entry + (ATR * 3.0)
    SHORT: SL = entry + (ATR * 1.2)   TP = entry - (ATR * 3.0)
    """
    if side == "LONG":
        sl = entry_price - (atr_value * SL_ATR_MULT)
        tp = entry_price + (atr_value * TP_ATR_MULT)
    else:
        sl = entry_price + (atr_value * SL_ATR_MULT)
        tp = entry_price - (atr_value * TP_ATR_MULT)

    return sl, tp


def check_exit(
    side: str,
    entry_price: float,
    current_high: float,
    current_low: float,
    entry_atr: float,
) -> bool:
    """
    True se o preço atual atingiu SL ou TP.

    Usa high/low (não close) para detectar wicks que atingem SL/TP,
    igual ao PineScript strategy.exit().
    """
    sl, tp = calculate_sl_tp(entry_price, entry_atr, side)

    if side == "LONG":
        return current_low <= sl or current_high >= tp
    else:
        return current_high >= sl or current_low <= tp
