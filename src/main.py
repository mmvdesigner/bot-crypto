from __future__ import annotations

import asyncio
import logging
import math
import signal
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pandas as pd

from src.config import get_settings
from src.database import SupabaseDB
from src.exchange import BinanceFutures
from src.strategy import (
    add_indicators,
    calculate_sl_tp,
    check_entry,
    check_exit,
    detect_squeeze,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
#  Bot principal
# ---------------------------------------------------------------------------

SQUEEZE_LOOKBACK = 20
TICK_INTERVAL = 10  # segundos entre cada polling
ERROR_COOLDOWN = 60  # segundos após erro antes de tentar novamente

# Step sizes por símbolo (Binance Futures)
# TODO: buscar dinamicamente via /fapi/v1/exchangeInfo
STEP_SIZES: dict[str, float] = {
    "BTCUSDT": 0.001,
    "ETHUSDT": 0.001,
}
DEFAULT_STEP_SIZE = 0.001


class TradingBot:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._db = SupabaseDB()
        self._exchange: Optional[BinanceFutures] = None
        self._running = False
        self._ticker: Optional[str] = None
        self._df: Optional[pd.DataFrame] = None
        self._active_squeeze: dict[str, dict[str, float]] = {}
        # Posições isoladas por símbolo
        self._positions: dict[str, Dict[str, Any]] = {}
        # Tracking de candles processados (evitar reprocessar mesmo candle)
        self._last_candle_ts: dict[str, int] = {}

    # ------------------------------------------------------------------
    #  Loops
    # ------------------------------------------------------------------

    async def run(self) -> None:
        logger.info("Iniciando bot (mode=%s, symbols=%s)",
                     self._settings.trade_mode, self._settings.symbols)

        self._exchange = BinanceFutures()
        self._running = True

        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda: asyncio.create_task(self.stop()))
            except NotImplementedError:
                pass

        try:
            await self._main_loop()
        except asyncio.CancelledError:
            pass
        finally:
            await self._shutdown()

    async def stop(self) -> None:
        logger.info("Parando bot...")
        self._running = False

    async def _main_loop(self) -> None:
        while self._running:
            try:
                state = self._db.get_bot_state()
                status = (state or {}).get("status", "STOPPED")

                if status != "RUNNING":
                    logger.info("Bot %s. Aguardando %ds...", status, TICK_INTERVAL)
                    self._db.upsert_bot_state({
                        "current_position": "FLAT",
                        "last_squeeze_high": None,
                        "last_squeeze_low": None,
                    })
                    await asyncio.sleep(TICK_INTERVAL)
                    continue

                await self._tick()

            except Exception as e:
                logger.error("Erro no loop principal: %s", e, exc_info=True)
                # NÃO travar em ERROR — auto-recover após cooldown
                logger.info("Auto-recovery em %ds...", ERROR_COOLDOWN)
                try:
                    self._db.upsert_bot_state({"status": "RUNNING"})
                except Exception:
                    pass
                await asyncio.sleep(ERROR_COOLDOWN)
                continue

            # CRÍTICO: sleep entre ticks para não bombardear a API
            await asyncio.sleep(TICK_INTERVAL)

    async def _tick(self) -> None:
        assert self._exchange is not None

        prices: dict[str, float] = {}
        for symbol in self._settings.symbols:
            try:
                klines = await self._exchange.get_klines(symbol, limit=100)
            except Exception as e:
                logger.warning("Falha ao buscar klines %s: %s", symbol, e)
                continue

            df_full = self._klines_to_df(klines)
            df_full = add_indicators(df_full)

            # Separar candles completos vs candle em formação
            # PineScript opera no close da barra — usar apenas candles fechados
            # para decisões de entrada. O candle em formação é usado para exit.
            if len(df_full) < 2:
                continue

            df_closed = df_full.iloc[:-1]  # Apenas candles completos
            forming = df_full.iloc[-1]     # Candle atual (em formação)
            prices[symbol] = float(forming["close"])

            self._df = df_full
            self._ticker = symbol

            # Verificar se temos um NOVO candle fechado (evitar reprocessar)
            last_closed_ts = int(df_closed.iloc[-1]["timestamp"].timestamp())
            prev_ts = self._last_candle_ts.get(symbol, 0)

            # --- Verificar saída de posição (usa candle em formação) ---
            pos = self._positions.get(symbol)
            if pos:
                exit_hit = check_exit(
                    side=pos["side"],
                    entry_price=pos["entry_price"],
                    current_high=float(forming["high"]),
                    current_low=float(forming["low"]),
                    entry_atr=pos["entry_atr"],
                )
                if exit_hit:
                    await self._close_position(symbol, float(forming["close"]))
                    continue

            # --- Verificar entrada apenas em NOVOS candles fechados ---
            if last_closed_ts == prev_ts:
                # Mesmo candle, já processado para entrada
                continue

            self._last_candle_ts[symbol] = last_closed_ts

            # --- Atualizar níveis de squeeze ativo (persistente como ta.valuewhen) ---
            if len(df_closed) >= 1 and bool(df_closed.iloc[-1]["is_squeeze"]):
                self._active_squeeze[symbol] = {
                    "high": float(df_closed.iloc[-1]["high"]),
                    "low": float(df_closed.iloc[-1]["low"]),
                }

            # --- Verificar entrada nos níveis do squeeze ativo ---
            sq = self._active_squeeze.get(symbol)
            if sq and symbol not in self._positions and len(df_closed) >= 1:
                curr_bar = df_closed.iloc[-1]
                signal, price = check_entry(df_closed, sq["high"], sq["low"])
                if signal and price:
                    logger.info(
                        "BREAKOUT detectado! symbol=%s signal=%s price=%.2f "
                        "squeeze_high=%.2f squeeze_low=%.2f",
                        symbol, signal, price, sq["high"], sq["low"],
                    )
                    atr_val = float(curr_bar["atr"])
                    sl, tp = calculate_sl_tp(price, atr_val, signal)
                    entry = {
                        "signal": signal,
                        "price": price,
                        "atr": atr_val,
                        "sl": sl,
                        "tp": tp,
                        "squeeze_high": sq["high"],
                        "squeeze_low": sq["low"],
                    }
                    await self._open_position(symbol, entry, df_closed)

        # --- Atualizar bot_state ---
        # Manter compatibilidade com CHECK constraint (FLAT/LONG/SHORT)
        if not self._positions:
            pos_label = "FLAT"
        else:
            # Usar o side da primeira posição (constraint do schema)
            first_pos = next(iter(self._positions.values()))
            pos_label = first_pos["side"]

        # Último squeeze ativo (qualquer símbolo)
        last_sh: float | None = None
        last_sl: float | None = None
        for sq_info in self._active_squeeze.values():
            last_sh = sq_info["high"]
            last_sl = sq_info["low"]
            break

        current_balance = None
        try:
            current_balance = await self._exchange.get_balance_usdt()
        except Exception:
            pass

        self._db.upsert_bot_state({
            "current_position": pos_label,
            "last_squeeze_high": last_sh if last_sh else None,
            "last_squeeze_low": last_sl if last_sl else None,
            "current_prices": prices,
            "current_balance": current_balance,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })

    # ------------------------------------------------------------------
    #  Posições
    # ------------------------------------------------------------------

    async def _open_position(
        self, symbol: str, entry: Dict[str, Any], df: pd.DataFrame
    ) -> None:
        assert self._exchange is not None

        side = entry["signal"]
        price = entry["price"]
        sl = entry["sl"]
        tp = entry["tp"]
        atr_val = entry["atr"]

        logger.info(
            "=== ENTRADA %s %s price=%.2f sl=%.2f tp=%.2f atr=%.2f ===",
            symbol, side, price, sl, tp, atr_val,
        )

        # Calcular quantidade (1% do saldo / distância do SL)
        try:
            balance = await self._exchange.get_balance_usdt()
        except Exception:
            balance = 10000.0  # fallback paper

        risk_amount = balance * 0.01
        price_dist = abs(price - sl)
        if price_dist <= 0:
            logger.warning("Distância SL é zero, abortando entrada")
            return

        amount = risk_amount / price_dist

        # Arredondar para step_size do símbolo
        step = STEP_SIZES.get(symbol.upper(), DEFAULT_STEP_SIZE)
        amount = math.floor(amount / step) * step
        if amount <= 0:
            logger.warning("Quantidade calculada é zero após arredondamento")
            return

        if self._settings.is_live:
            try:
                order = await self._exchange.place_order(
                    symbol=symbol,
                    side="BUY" if side == "LONG" else "SELL",
                    order_type="MARKET",
                    quantity=amount,
                )
                exec_price = float(order.get("avgPrice", price))
                exec_qty = float(order.get("executedQty", amount))
                logger.info("Ordem executada: %s", order)

                # Recalcular SL/TP com preço real de execução
                sl, tp = calculate_sl_tp(exec_price, atr_val, side)

                # Enviar SL/TP como ordens na exchange
                await self._exchange.place_order(
                    symbol=symbol,
                    side="SELL" if side == "LONG" else "BUY",
                    order_type="STOP_MARKET",
                    quantity=exec_qty,
                    stop_price=sl,
                    reduce_only=True,
                )
                await self._exchange.place_order(
                    symbol=symbol,
                    side="SELL" if side == "LONG" else "BUY",
                    order_type="TAKE_PROFIT_MARKET",
                    quantity=exec_qty,
                    stop_price=tp,
                    reduce_only=True,
                )
            except Exception as e:
                logger.error("Falha ordem %s: %s", side, e)
                return

            self._positions[symbol] = {
                "side": side,
                "entry_price": exec_price,
                "amount": exec_qty,
                "entry_atr": atr_val,
            }
        else:
            # Paper mode
            self._positions[symbol] = {
                "side": side,
                "entry_price": price,
                "amount": amount,
                "entry_atr": atr_val,
            }

        trade_id = self._db.open_trade(
            side=side,
            entry_price=self._positions[symbol]["entry_price"],
            amount=self._positions[symbol]["amount"],
        )
        self._positions[symbol]["trade_id"] = trade_id

    async def _close_position(self, symbol: str, exit_price: float) -> None:
        assert self._exchange is not None

        pos = self._positions.get(symbol)
        if not pos:
            return

        side = pos["side"]
        entry = pos["entry_price"]
        amount = pos["amount"]

        if side == "LONG":
            pnl = (exit_price - entry) * amount
        else:
            pnl = (entry - exit_price) * amount

        logger.info("=== SAÍDA %s %s pnl=%.2f exit=%.2f ===", symbol, side, pnl, exit_price)

        if self._settings.is_live:
            try:
                # Cancelar ordens pendentes (SL/TP espelho)
                await self._exchange.cancel_all(symbol)
                await self._exchange.place_order(
                    symbol=symbol,
                    side="SELL" if side == "LONG" else "BUY",
                    order_type="MARKET",
                    quantity=amount,
                    reduce_only=True,
                )
            except Exception as e:
                logger.error("Falha ao fechar posição: %s", e)

        trade_id = pos.get("trade_id")
        if trade_id:
            self._db.close_trade(trade_id, exit_price, pnl)

        del self._positions[symbol]

    # ------------------------------------------------------------------
    #  Utilitários
    # ------------------------------------------------------------------

    @staticmethod
    def _klines_to_df(klines: List[List]) -> pd.DataFrame:
        records = []
        for k in klines:
            records.append({
                "timestamp": datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc),
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
            })
        return pd.DataFrame(records)

    async def _shutdown(self) -> None:
        logger.info("Shutdown...")
        if self._settings.is_live:
            for symbol in list(self._positions.keys()):
                try:
                    await self._exchange.cancel_all(symbol)
                except Exception:
                    pass
        if self._exchange:
            await self._exchange.close()
        logger.info("Bot parado.")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    bot = TradingBot()
    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
