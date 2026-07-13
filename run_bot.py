#!/usr/bin/env python3
"""Standalone bot runner — executa o TradingBot sem Streamlit.

Uso:
  python run_bot.py

Requer .env com BINANCE_API_KEY, BINANCE_SECRET_KEY, SUPABASE_URL etc.
"""

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

from src.main import TradingBot

async def main() -> None:
    bot = TradingBot()
    await bot.run()

if __name__ == "__main__":
    asyncio.run(main())
