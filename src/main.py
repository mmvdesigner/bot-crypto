from __future__ import annotations

import asyncio
import logging
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


class SqueezeTracker:
    def __init__(self) -> None:
        self.high: float = 0.0
        self.low: float = 0.0
        self.active: bool = False
        self._prev_squeeze_high: Optional[float] = None
        self._prev_squeeze_low: Optional[float] = None

    def update(self, df: pd.DataFrame) -> Optional[Dict[str, Any]]:
        """Chamado a cada candle fechado. Detecta squeeze e prepara breakout."""
        if len(df) < SQUEEZE_LOOKBACK + 2:
            return

        squeeze_now = detect_squeeze(df["atr"])
        last = df.iloc[-1]

        # --- Se o candle ANTERIOR era squeeze, testar breakout agora ---
        if self.active:
            self.active = False
            signal, price = check_entry(df, self.high, self.low)
            if signal and price:
                atr_val = last["atr"]
                sl, tp = calculate_sl_tp(price, atr_val, signal)
                return {
                    "signal": signal,
                    "price": price,
                    "atr": atr_val,
                    "sl": sl,
                    "tp": tp,
                    "squeeze_high": self.high,
                    "squeeze_low": self.low,
                }

        # --- Se este candle é squeeze, guardar para o próximo ---
        if squeeze_now:
            self.active = True
            self.high = last["high"]
            self.low = last["low"]

        return None


class TradingBot:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._db = SupabaseDB()
        self._exchange: Optional[BinanceFutures] = None
        self._running = False
        self._ticker: Optional[str] = None
        self._df: Optional[pd.DataFrame] = None
        self._squeeze: dict[str, SqueezeTracker] = {}
        self._position: Optional[Dict[str, Any]] = None  # trade ativo

    # ------------------------------------------------------------------
    #  Loops
    # ------------------------------------------------------------------

    async def run(self) -> None:
        logger.info("Iniciando bot (mode=%s)", self._settings.trade_mode)

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
                    logger.info("Bot %s. Aguardando 10s...", status)
                    self._db.upsert_bot_state({
                        "current_position": "FLAT",
                        "last_squeeze_high": None,
                        "last_squeeze_low": None,
                    } if not self._position else {
                        "current_position": "FLAT",
                    })
                    await asyncio.sleep(10)
                    continue

                await self._tick()

            except Exception as e:
                logger.error("Erro no loop principal: %s", e, exc_info=True)
                self._db.upsert_bot_state({"status": "ERROR"})
                await asyncio.sleep(30)

    async def _tick(self) -> None:
        assert self._exchange is not None

        prices: dict[str, float] = {}
        for symbol in self._settings.symbols:
            try:
                klines = await self._exchange.get_klines(symbol, limit=100)
            except Exception as e:
                logger.warning("Falha ao buscar klines %s: %s", symbol, e)
                continue

            df = self._klines_to_df(klines)
            df = add_indicators(df)
            self._df = df
            self._ticker = symbol
            prices[symbol] = df.iloc[-1]["close"]

            # --- Verificar saída de posição atual ---
            if self._position:
                exit_hit = check_exit(
                    side=self._position["side"],
                    entry_price=self._position["entry_price"],
                    current_price=df.iloc[-1]["close"],
                    entry_atr=self._position["entry_atr"],
                )
                if exit_hit:
                    await self._close_position(df.iloc[-1]["close"])
                    continue

            # --- Verificar entrada (squeeze → breakout) ---
            if not self._position:
                sq = self._squeeze.setdefault(symbol, SqueezeTracker())
                entry = sq.update(df)
                if entry:
                    await self._open_position(entry, df)

        # --- Atualizar bot_state ---
        pos_label = "FLAT"
        if self._position:
            pos_label = self._position["side"]

        # Último squeeze ativo (qualquer símbolo)
        last_sh: float | None = None
        last_sl: float | None = None
        for sq in self._squeeze.values():
            if sq.active:
                last_sh = sq.high
                last_sl = sq.low

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

    async def _open_position(self, entry: Dict[str, Any], df: pd.DataFrame) -> None:
        assert self._exchange is not None

        side = entry["signal"]
        price = entry["price"]
        sl = entry["sl"]
        tp = entry["tp"]
        atr_val = entry["atr"]

        logger.info("=== ENTRADA %s %s price=%.2f sl=%.2f tp=%.2f atr=%.2f ===",
                     self._ticker, side, price, sl, tp, atr_val)

        # Calcular quantidade (1% do saldo / distância do SL)
        try:
            balance = await self._exchange.get_balance_usdt()
        except Exception:
            balance = 10000.0  # fallback paper

        risk_amount = balance * 0.01
        price_dist = abs(price - sl)
        amount = risk_amount / price_dist if price_dist > 0 else 0.001

        if self._settings.is_live:
            try:
                order = await self._exchange.place_order(
                    symbol=self._ticker,
                    side="BUY" if side == "LONG" else "SELL",
                    order_type="MARKET",
                    quantity=amount,
                )
                exec_price = float(order.get("avgPrice", price))
                exec_qty = float(order.get("executedQty", amount))
                logger.info("Ordem executada: %s", order)

                # Enviar SL/TP como stop loss / take profit market
                await self._exchange.place_order(
                    symbol=self._ticker,
                    side="SELL" if side == "LONG" else "BUY",
                    order_type="STOP_MARKET",
                    quantity=exec_qty,
                    stop_price=sl,
                    reduce_only=True,
                )
                await self._exchange.place_order(
                    symbol=self._ticker,
                    side="SELL" if side == "LONG" else "BUY",
                    order_type="TAKE_PROFIT_MARKET",
                    quantity=exec_qty,
                    stop_price=tp,
                    reduce_only=True,
                )
            except Exception as e:
                logger.error("Falha ordem %s: %s", side, e)
                return

            self._position = {
                "side": side,
                "entry_price": exec_price,
                "amount": exec_qty,
                "entry_atr": atr_val,
            }
        else:
            # Paper mode
            self._position = {
                "side": side,
                "entry_price": price,
                "amount": amount,
                "entry_atr": atr_val,
            }

        trade_id = self._db.open_trade(
            side=side,
            entry_price=self._position["entry_price"],
            amount=self._position["amount"],
        )
        self._position["trade_id"] = trade_id

    async def _close_position(self, exit_price: float) -> None:
        assert self._exchange is not None

        if not self._position:
            return

        side = self._position["side"]
        entry = self._position["entry_price"]
        amount = self._position["amount"]

        if side == "LONG":
            pnl = (exit_price - entry) * amount
        else:
            pnl = (entry - exit_price) * amount

        logger.info("=== SAÍDA %s pnl=%.2f exit=%.2f ===", self._ticker, pnl, exit_price)

        if self._settings.is_live:
            try:
                await self._exchange.cancel_all(self._ticker)
                await self._exchange.place_order(
                    symbol=self._ticker,
                    side="SELL" if side == "LONG" else "BUY",
                    order_type="MARKET",
                    quantity=amount,
                    reduce_only=True,
                )
            except Exception as e:
                logger.error("Falha ao fechar posição: %s", e)

        trade_id = self._position.get("trade_id")
        if trade_id:
            self._db.close_trade(trade_id, exit_price, pnl)

        self._position = None

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
        if self._position and self._settings.is_live:
            try:
                await self._exchange.cancel_all(self._ticker)  # type: ignore[arg-type]
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
