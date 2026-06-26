from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    binance_api_key: str = field(default_factory=lambda: os.getenv("BINANCE_API_KEY", ""))
    binance_secret_key: str = field(default_factory=lambda: os.getenv("BINANCE_SECRET_KEY", ""))
    binance_testnet: bool = field(
        default_factory=lambda: os.getenv("BINANCE_TESTNET", "true").strip().lower() == "true"
    )

    supabase_url: str = field(default_factory=lambda: os.getenv("SUPABASE_URL", ""))
    supabase_anon_key: str = field(default_factory=lambda: os.getenv("SUPABASE_ANON_KEY", ""))
    supabase_service_key: str = field(default_factory=lambda: os.getenv("SUPABASE_SERVICE_KEY", ""))

    trade_mode: str = field(default_factory=lambda: os.getenv("TRADE_MODE", "paper").strip().lower())
    symbols: tuple[str, ...] = field(default_factory=lambda: tuple(
        s.strip().upper() for s in os.getenv("SYMBOLS", "BTCUSDT,BNBUSD").split(",") if s.strip()
    ))

    @property
    def is_live(self) -> bool:
        return self.trade_mode == "live"

    @property
    def is_testnet(self) -> bool:
        return self.binance_testnet

    @property
    def binance_base_url(self) -> str:
        return "https://testnet.binancefuture.com" if self.is_testnet else "https://fapi.binance.com"

    @property
    def binance_ws_url(self) -> str:
        return "wss://stream.binancefuture.com" if self.is_testnet else "wss://fstream.binance.com"

    def validate(self) -> None:
        missing: list[str] = []
        if not self.binance_api_key:
            missing.append("BINANCE_API_KEY")
        if not self.binance_secret_key:
            missing.append("BINANCE_SECRET_KEY")
        if not self.supabase_url:
            missing.append("SUPABASE_URL")
        if not self.supabase_anon_key:
            missing.append("SUPABASE_ANON_KEY")
        if missing:
            raise RuntimeError("Variáveis obrigatórias no .env: " + ", ".join(missing))


@lru_cache()
def get_settings() -> Settings:
    return Settings()
