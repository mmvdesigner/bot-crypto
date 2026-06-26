from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from supabase import Client, create_client

from src.config import get_settings

logger = logging.getLogger(__name__)


class SupabaseDB:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._client: Optional[Client] = None

    def _connect(self, service_role: bool = False) -> Client:
        key = self._settings.supabase_service_key if service_role else self._settings.supabase_anon_key
        return create_client(self._settings.supabase_url, key)

    # ------------------------------------------------------------------
    #  Auth
    # ------------------------------------------------------------------

    def sign_in(self, email: str, password: str) -> Tuple[bool, Optional[str]]:
        try:
            c = self._connect(service_role=False)
            c.auth.sign_in_with_password({"email": email, "password": password})
            self._client = c
            logger.info("Autenticado: %s", email)
            return True, None
        except Exception as e:
            logger.warning("Falha login %s: %s", email, e)
            return False, str(e)

    def sign_out(self) -> None:
        if self._client:
            try:
                self._client.auth.sign_out()
            except Exception:
                pass
        self._client = None

    @property
    def is_authenticated(self) -> bool:
        if not self._client:
            return False
        try:
            return self._client.auth.get_session() is not None
        except Exception:
            return False

    # ------------------------------------------------------------------
    #  bot_state (singleton)
    # ------------------------------------------------------------------

    def _bot(self) -> Client:
        return self._connect(service_role=True)

    def get_bot_state(self) -> Optional[Dict[str, Any]]:
        try:
            r = self._bot().table("bot_state").select("*").limit(1).execute()
            return r.data[0] if r.data else None
        except Exception as e:
            logger.error("bot_state leitura: %s", e)
            return None

    def upsert_bot_state(self, data: Dict[str, Any]) -> bool:
        try:
            tbl = self._bot().table("bot_state")
            exist = tbl.select("id").limit(1).execute()
            if exist.data:
                tbl.update(data).eq("id", exist.data[0]["id"]).execute()
            else:
                tbl.insert(data).execute()
            return True
        except Exception as e:
            logger.error("bot_state upsert: %s", e)
            return False

    # ------------------------------------------------------------------
    #  trades_log
    # ------------------------------------------------------------------

    def _db(self) -> Client:
        if self._client:
            return self._client
        return self._connect(service_role=True)

    def open_trade(self, side: str, entry_price: float, amount: float) -> Optional[str]:
        try:
            r = (
                self._db()
                .table("trades_log")
                .insert({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "side": side,
                    "entry_price": entry_price,
                    "amount": amount,
                    "status": "OPEN",
                    "strategy_name": "volume_expansion",
                })
                .execute()
            )
            return r.data[0]["id"]
        except Exception as e:
            logger.error("open_trade: %s", e)
            return None

    def close_trade(self, trade_id: str, exit_price: float, pnl: float) -> bool:
        try:
            (
                self._db()
                .table("trades_log")
                .update({"exit_price": exit_price, "pnl": pnl, "status": "CLOSED"})
                .eq("id", trade_id)
                .execute()
            )
            return True
        except Exception as e:
            logger.error("close_trade: %s", e)
            return False

    def get_open_trades(self) -> List[Dict[str, Any]]:
        try:
            r = (
                self._db()
                .table("trades_log")
                .select("*")
                .eq("status", "OPEN")
                .order("timestamp", desc=True)
                .execute()
            )
            return r.data
        except Exception as e:
            logger.error("get_open_trades: %s", e)
            return []

    def get_recent_trades(self, limit: int = 50) -> List[Dict[str, Any]]:
        try:
            r = (
                self._db()
                .table("trades_log")
                .select("*")
                .order("timestamp", desc=True)
                .limit(limit)
                .execute()
            )
            return r.data
        except Exception as e:
            logger.error("get_recent_trades: %s", e)
            return []

    def get_summary(self) -> Dict[str, Any]:
        try:
            closed = (
                self._db()
                .table("trades_log")
                .select("*")
                .eq("status", "CLOSED")
                .execute()
            )
            total = len(closed.data)
            wins = sum(1 for t in closed.data if (t.get("pnl") or 0) > 0)
            pnl = sum(t.get("pnl") or 0 for t in closed.data)
            return {
                "total": total,
                "wins": wins,
                "losses": total - wins,
                "win_rate": wins / total if total else 0.0,
                "pnl": pnl,
            }
        except Exception as e:
            logger.error("get_summary: %s", e)
            return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "pnl": 0.0}
