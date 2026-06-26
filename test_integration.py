"""Teste de integração: indicators → strategy"""
from datetime import datetime, timezone

import pandas as pd

from src.indicators import atr, volume_sma
from src.strategy import (
    add_indicators,
    calculate_sl_tp,
    check_entry,
    check_exit,
    detect_squeeze,
)


def make_df(closes, highs=None, lows=None, volumes=None):
    n = len(closes)
    return pd.DataFrame({
        "timestamp": [datetime.now(timezone.utc)] * n,
        "open": [c - 10 for c in closes],
        "high": highs or [c + 20 for c in closes],
        "low": lows or [c - 20 for c in closes],
        "close": closes,
        "volume": volumes or [100.0] * n,
    })


def test_atr_known_values():
    """ATR converge para o range esperado."""
    df = make_df([50000.0] * 80, highs=[50100.0] * 80, lows=[49900.0] * 80)
    atr_series = atr(df, 14)
    assert atr_series.iloc[-1] == 200.0
    print(f"  ATR = {atr_series.iloc[-1]}")
    print("✓ test_atr_known_values")


def test_volume_sma():
    df = make_df([50000.0] * 30, volumes=[150.0] * 30)
    vs = volume_sma(df, 20)
    assert vs.iloc[-1] == 150.0
    print("✓ test_volume_sma")


def test_squeeze_detected():
    """70 barras de range normal + 1 barra de range estreito = squeeze."""
    closes = [50000.0] * 71
    highs = [50100.0] * 70 + [50020.0]
    lows = [49900.0] * 70 + [49980.0]
    df = make_df(closes, highs=highs, lows=lows)
    df = add_indicators(df)
    assert detect_squeeze(df["atr"]), "Squeeze deve ser detectado"
    print("✓ test_squeeze_detected")


def test_long_breakout():
    """Após squeeze, breakout acima da máxima com volume → LONG."""
    closes = [50000.0] * 70 + [50010.0, 50060.0]
    highs = [50100.0] * 70 + [50020.0, 50200.0]
    lows = [49900.0] * 70 + [49980.0, 50050.0]
    volumes = [100.0] * 70 + [100.0, 300.0]

    df = make_df(closes, highs=highs, lows=lows, volumes=volumes)
    df = add_indicators(df)

    assert detect_squeeze(df["atr"].iloc[:-1]), "Penúltima barra é squeeze"

    signal, price = check_entry(df, squeeze_high=50020, squeeze_low=49980)
    assert signal == "LONG", f"Esperado LONG, obtido {signal}"
    assert price == 50060.0
    print(f"  Signal={signal} Price={price}")
    print("✓ test_long_breakout")


def test_sl_tp_long():
    sl, tp = calculate_sl_tp(50000.0, 200.0, "LONG")
    assert sl == 50000.0 - 200.0 * 1.2
    assert tp == 50000.0 + 200.0 * 3.0
    print("✓ test_sl_tp_long")


def test_sl_tp_short():
    sl, tp = calculate_sl_tp(50000.0, 200.0, "SHORT")
    assert sl == 50000.0 + 200.0 * 1.2
    assert tp == 50000.0 - 200.0 * 3.0
    print("✓ test_sl_tp_short")


def test_exit_long():
    assert check_exit("LONG", 50000, 49760, 200) is True   # SL hit
    assert check_exit("LONG", 50000, 50601, 200) is True    # TP hit
    assert check_exit("LONG", 50000, 50200, 200) is False   # no exit
    print("✓ test_exit_long")


def test_exit_short():
    assert check_exit("SHORT", 50000, 50240, 200) is True   # SL
    assert check_exit("SHORT", 50000, 49399, 200) is True    # TP
    assert check_exit("SHORT", 50000, 49800, 200) is False   # no exit
    print("✓ test_exit_short")


if __name__ == "__main__":
    test_atr_known_values()
    test_volume_sma()
    test_squeeze_detected()
    test_long_breakout()
    test_sl_tp_long()
    test_sl_tp_short()
    test_exit_long()
    test_exit_short()
    print("\n=== Todos os testes de integração passaram ===")
