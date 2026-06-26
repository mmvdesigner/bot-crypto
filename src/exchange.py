from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from typing import Any, Dict, Optional

import httpx

from src.config import get_settings

logger = logging.getLogger(__name__)


class ExchangeError(Exception):
    pass


class InsufficientBalanceError(ExchangeError):
    pass


class RateLimitError(ExchangeError):
    pass


class BannedError(ExchangeError):
    pass


class NetworkError(ExchangeError):
    pass


class BinanceFutures:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._base = self._settings.binance_base_url
        self._http = httpx.AsyncClient(
            base_url=self._base,
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0),
        )
        self._recv_window = 5000

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "BinanceFutures":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    # ------------------------------------------------------------------
    #  Assinatura
    # ------------------------------------------------------------------

    def _sign(self, params: Dict[str, Any]) -> str:
        query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        return hmac.new(
            self._settings.binance_secret_key.encode(),
            query.encode(),
            hashlib.sha256,
        ).hexdigest()

    # ------------------------------------------------------------------
    #  Chamada HTTP com retry e tratamento de erros
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        signed: bool = False,
        params: Optional[Dict[str, Any]] = None,
        retries: int = 3,
    ) -> Any:
        params = params or {}

        if signed:
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = self._recv_window
            params["signature"] = self._sign(params)

        headers: Dict[str, str] = {}
        if signed or self._settings.binance_api_key:
            headers["X-MBX-APIKEY"] = self._settings.binance_api_key

        last: Optional[Exception] = None

        for attempt in range(1, retries + 1):
            try:
                resp = await self._http.request(
                    method=method,
                    url=path,
                    headers=headers,
                    params=params if method == "GET" else None,
                    data=params if method in ("POST", "PUT", "DELETE") else None,
                )

                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", "30"))
                    logger.warning("429 — dormindo %ds (attempt %d)", wait, attempt)
                    await asyncio.sleep(wait)
                    last = RateLimitError(f"429 retry-after={wait}s")
                    continue

                if resp.status_code == 418:
                    wait = 300
                    logger.critical("418 BANIDO — dormindo %ds", wait)
                    await asyncio.sleep(wait)
                    last = BannedError("418 banned")
                    continue

                if 400 <= resp.status_code < 500:
                    body = resp.json()
                    msg = body.get("msg", resp.text)
                    if "insufficient balance" in msg.lower() or "-2010" in msg or "-2011" in msg:
                        raise InsufficientBalanceError(msg)
                    raise ExchangeError(f"HTTP {resp.status_code}: {msg}")

                if resp.status_code >= 500:
                    logger.warning("Servidor %d (attempt %d/%d)", resp.status_code, attempt, retries)
                    last = ExchangeError(f"HTTP {resp.status_code}")
                    if attempt < retries:
                        await asyncio.sleep(2 ** attempt)
                    continue

                resp.raise_for_status()
                return resp.json()

            except httpx.TimeoutException as e:
                logger.warning("Timeout %s %s (attempt %d)", method, path, attempt)
                last = NetworkError(f"Timeout: {e}")
                if attempt < retries:
                    await asyncio.sleep(2 ** attempt)
                continue

            except httpx.RequestError as e:
                logger.warning("Rede %s %s (attempt %d): %s", method, path, attempt, e)
                last = NetworkError(f"Rede: {e}")
                if attempt < retries:
                    await asyncio.sleep(2 ** attempt)
                continue

            except (InsufficientBalanceError, ExchangeError):
                raise

        raise last or ExchangeError("Retries excedidos")

    # ------------------------------------------------------------------
    #  API pública
    # ------------------------------------------------------------------

    async def ping(self) -> bool:
        try:
            await self._request("GET", "/fapi/v1/ping")
            return True
        except Exception:
            return False

    async def server_time(self) -> int:
        return (await self._request("GET", "/fapi/v1/time"))["serverTime"]

    async def get_klines(self, symbol: str, interval: str = "15m", limit: int = 100) -> list:
        return await self._request("GET", "/fapi/v1/klines", params={
            "symbol": symbol.upper(),
            "interval": interval,
            "limit": min(limit, 1500),
        })

    async def get_balance_usdt(self) -> float:
        data = await self._request("GET", "/fapi/v2/account", signed=True)
        for asset in data.get("assets", []):
            if asset["asset"] == "USDT":
                return float(asset.get("walletBalance", 0))
        return 0.0

    async def get_positions(self) -> list:
        return await self._request("GET", "/fapi/v2/positionRisk", signed=True)

    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        return await self._request(
            "POST", "/fapi/v1/leverage",
            signed=True,
            params={"symbol": symbol.upper(), "leverage": leverage},
        )

    async def set_margin_type(self, symbol: str, margin: str = "ISOLATED") -> dict:
        return await self._request(
            "POST", "/fapi/v1/marginType",
            signed=True,
            params={"symbol": symbol.upper(), "marginType": margin},
        )

    async def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        stop_price: Optional[float] = None,
        reduce_only: bool = False,
    ) -> dict:
        params: Dict[str, Any] = {
            "symbol": symbol.upper(),
            "side": side,
            "type": order_type,
            "quantity": quantity,
        }
        if stop_price:
            params["stopPrice"] = stop_price
        if reduce_only:
            params["reduceOnly"] = "true"
        if order_type == "MARKET":
            params["newOrderRespType"] = "RESULT"

        return await self._request("POST", "/fapi/v1/order", signed=True, params=params)

    async def cancel_all(self, symbol: str) -> Any:
        return await self._request(
            "DELETE", "/fapi/v1/allOpenOrders",
            signed=True,
            params={"symbol": symbol.upper()},
        )

    async def get_listen_key(self) -> str:
        return (await self._request("POST", "/fapi/v1/listenKey", signed=True))["listenKey"]

    async def keepalive_key(self, key: str) -> None:
        await self._request("PUT", "/fapi/v1/listenKey", signed=True, params={"listenKey": key})

    async def get_mark_price(self, symbol: str) -> dict:
        return await self._request("GET", "/fapi/v1/premiumIndex", params={"symbol": symbol.upper()})
