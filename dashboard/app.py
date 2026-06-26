from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
from datetime import datetime, timezone

import hashlib
import hmac

import httpx
import pandas as pd
import streamlit as st

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from src.config import get_settings
from src.database import SupabaseDB

logger = logging.getLogger(__name__)

st.set_page_config(
    page_title="Bot Crypto — Expansão de Volume",
    page_icon="🤖",
    layout="wide",
)

# ---------------------------------------------------------------------------
#  Sessão
# ---------------------------------------------------------------------------

if "db" not in st.session_state:
    st.session_state.db = SupabaseDB()
if "page" not in st.session_state:
    st.session_state.page = "login"

# ---------------------------------------------------------------------------
#  Background services (bot + keepalive)
# ---------------------------------------------------------------------------

def _start_bot_in_background() -> None:
    from src.main import TradingBot

    async def _run() -> None:
        bot = TradingBot()
        await bot.run()

    def _target() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_run())
        except Exception:
            logger.exception("Bot thread morreu")

    t = threading.Thread(target=_target, daemon=True, name="bot-thread")
    t.start()
    logger.info("Bot thread iniciada")


def _start_keepalive() -> None:
    url = os.environ.get(
        "RENDER_EXTERNAL_URL",
        "https://bot-crypto-dashboard.onrender.com",
    )

    def _ping() -> None:
        while True:
            threading.Event().wait(300)  # 5 min
            try:
                httpx.get(url, timeout=30)
                logger.info("Keepalive ping OK")
            except Exception:
                logger.warning("Keepalive ping falhou")

    t = threading.Thread(target=_ping, daemon=True, name="keepalive")
    t.start()
    logger.info("Keepalive thread iniciada (URL=%s)", url)


@st.cache_resource
def _start_background_services() -> None:
    _start_bot_in_background()
    _start_keepalive()


_start_background_services()


# ---------------------------------------------------------------------------
#  Login
# ---------------------------------------------------------------------------

def login_page() -> None:
    st.markdown("## 🤖 Bot Crypto — Expansão de Volume")
    st.markdown("---")

    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        st.markdown("### Login")
        email = st.text_input("Email", placeholder="seu@email.com")
        password = st.text_input("Senha", type="password", placeholder="••••••••")

        if st.button("Entrar", type="primary", use_container_width=True):
            if not email or not password:
                st.error("Preencha email e senha.")
                return
            ok, err = st.session_state.db.sign_in(email, password)
            if ok:
                st.session_state.page = "dashboard"
                st.rerun()
            else:
                st.error(f"Falha no login: {err}")

        st.markdown("---")
        st.caption("Use o Supabase Auth para criar seu usuário.")


# ---------------------------------------------------------------------------
#  Preços públicos (sem auth)
# ---------------------------------------------------------------------------

_PRICE_CACHE: dict = {}
_PRICE_LOCK = threading.Lock()


def _fetch_current_prices(symbols: tuple[str, ...]) -> dict[str, float]:
    with _PRICE_LOCK:
        result: dict[str, float] = {}
        for s in symbols:
            cached = _PRICE_CACHE.get(s)
            if cached and (datetime.now(timezone.utc) - cached["ts"]).total_seconds() < 60:
                result[s] = cached["price"]
                continue
            try:
                r = httpx.get(
                    "https://fapi.binance.com/fapi/v1/ticker/price",
                    params={"symbol": s},
                    timeout=10,
                )
                if r.status_code == 200:
                    price = float(r.json()["price"])
                    _PRICE_CACHE[s] = {"price": price, "ts": datetime.now(timezone.utc)}
                    result[s] = price
            except Exception:
                pass
        return result


_BALANCE_CACHE: dict = {}
_BALANCE_LOCK = threading.Lock()


def _fetch_testnet_balance() -> float | None:
    with _BALANCE_LOCK:
        cached = _BALANCE_CACHE.get("balance")
        if cached and (datetime.now(timezone.utc) - cached["ts"]).total_seconds() < 120:
            return cached["value"]
        try:
            settings = get_settings()
            key = settings.binance_api_key
            secret = settings.binance_secret_key
            base = settings.binance_base_url
            if not key or not secret:
                return None
            params = {"recvWindow": 5000, "timestamp": int(time.time() * 1000)}
            q = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
            sig = hmac.new(secret.encode(), q.encode(), hashlib.sha256).hexdigest()
            params["signature"] = sig
            r = httpx.get(
                f"{base}/fapi/v2/account",
                params=params,
                headers={"X-MBX-APIKEY": key},
                timeout=10,
            )
            if r.status_code == 200:
                assets = r.json().get("assets", [])
                for a in assets:
                    if a["asset"] == "USDT":
                        val = float(a["walletBalance"])
                        _BALANCE_CACHE["balance"] = {"value": val, "ts": datetime.now(timezone.utc)}
                        return val
        except Exception:
            pass
        return None


# ---------------------------------------------------------------------------
#  Dashboard
# ---------------------------------------------------------------------------

def _pnl_color(val: float | None) -> str:
    if val is None:
        return "—"
    color = "#00cc66" if val >= 0 else "#ff4444"
    return f'<span style="color:{color};font-weight:bold">${val:+,.2f}</span>'


def _status_badge(status: str) -> str:
    colors = {"RUNNING": "#00cc66", "STOPPED": "#888", "ERROR": "#ff4444"}
    c = colors.get(status, "#888")
    return f'<span style="background:{c};color:white;padding:2px 10px;border-radius:10px;font-size:0.8em">{status}</span>'


def dashboard_page() -> None:
    db: SupabaseDB = st.session_state.db
    settings = get_settings()
    mode = "🔴 LIVE" if settings.is_live else "🟢 PAPER"

    # ── Header ──────────────────────────────────────────────────────────
    st.markdown(f"# 🤖 Bot Crypto  ·  `{mode}`")
    st.caption(f"{', '.join(settings.symbols)} · 15m · volume_expansion")

    state = db.get_bot_state() or {}
    status = state.get("status", "STOPPED")
    position = state.get("current_position", "FLAT")
    prices = _fetch_current_prices(settings.symbols)

    # Saldo: tenta da testnet primeiro, fallback pro bot_state
    bal = _fetch_testnet_balance()
    if bal is None:
        bal = state.get("current_balance")

    updated_at = state.get("updated_at") or state.get("last_update")
    if updated_at:
        try:
            dt = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
            label = dt.strftime("%H:%M:%S")
        except Exception:
            label = str(updated_at)[:19]
    else:
        label = "—"

    # ── Linha 1: Status + Preços + Saldo ───────────────────────────────
    cols = st.columns([1.2, 1.5, 1.2, 0.8, 0.8, 0.8])
    with cols[0]:
        st.markdown(f"**Status**  \n{_status_badge(status)}  \n🔄 `{label}`", unsafe_allow_html=True)
    with cols[1]:
        for s in settings.symbols:
            p = prices.get(s)
            st.markdown(f"**{s}**  \n`${p:,.2f}`" if p else f"**{s}**  \n—")
    with cols[2]:
        bal_str = f"${bal:,.2f}" if bal else "—"
        st.markdown(f"**Saldo**  \n`{bal_str}`  \n{'(simulado)' if not settings.is_live else ''}")
    with cols[3]:
        st.markdown(f"**Posição**  \n`{position}`")
    with cols[4]:
        sh = state.get("last_squeeze_high")
        sl = state.get("last_squeeze_low")
        st.markdown(f"**Squeeze H**  \n`{sh:,.2f}`" if sh else "**Squeeze H**  \n—")
    with cols[5]:
        st.markdown(f"**Squeeze L**  \n`{sl:,.2f}`" if sl else "**Squeeze L**  \n—")

    # ── Botão INICIAR / PARAR ──────────────────────────────────────────
    col_a, col_b = st.columns([1, 5])
    with col_a:
        label = "⏹ PARAR" if status == "RUNNING" else "▶ INICIAR"
        if st.button(label, type="primary", use_container_width=True):
            new = "STOPPED" if status == "RUNNING" else "RUNNING"
            ok = db.upsert_bot_state({"status": new})
            if ok:
                st.success(f"Comando {new} enviado.")
                st.rerun()
            else:
                st.error(f"Falha ao enviar {new}.")

    # ── Gráfico PnL ─────────────────────────────────────────────────────
    st.markdown("---")
    st.subheader("📈 PnL Acumulado")

    all_trades = db.get_recent_trades(limit=500)
    if all_trades:
        df_pnl = pd.DataFrame(all_trades)
        if "pnl" in df_pnl.columns and "timestamp" in df_pnl.columns:
            df_pnl["timestamp"] = pd.to_datetime(df_pnl["timestamp"])
            df_pnl = df_pnl.sort_values("timestamp")
            df_pnl["pnl_cum"] = df_pnl["pnl"].fillna(0).cumsum()
            st.line_chart(
                df_pnl.set_index("timestamp")["pnl_cum"],
                use_container_width=True,
                height=250,
            )
        else:
            st.info("Sem dados de PnL ainda.")
    else:
        st.info("Sem trades para exibir.")

    # ── Sumário ─────────────────────────────────────────────────────────
    st.markdown("---")
    st.subheader("📊 Resumo")

    summary = db.get_summary()
    cols = st.columns(6)
    with cols[0]: st.metric("Total", summary["total"])
    with cols[1]: st.metric("Wins", summary["wins"])
    with cols[2]: st.metric("Losses", summary["losses"])
    with cols[3]: st.metric("Win Rate", f"{summary['win_rate']:.1%}")
    with cols[4]:
        pnl = summary["pnl"]
        st.markdown(f"**PnL Total**  \n{_pnl_color(pnl)}", unsafe_allow_html=True)
    with cols[5]:
        avg = summary["pnl"] / summary["total"] if summary["total"] else 0
        st.markdown(f"**Média/Trade**  \n{_pnl_color(avg)}", unsafe_allow_html=True)

    # ── Trades abertos ─────────────────────────────────────────────────
    st.markdown("---")
    st.subheader("📌 Trades Abertos")

    open_trades = db.get_open_trades()
    if open_trades:
        df = pd.DataFrame(open_trades)
        cols = ["timestamp", "side", "entry_price", "amount", "strategy_name"]
        st.dataframe(df[[c for c in cols if c in df.columns]], use_container_width=True)
    else:
        st.info("Nenhum trade aberto.")

    # ── Histórico recente ───────────────────────────────────────────────
    st.markdown("---")
    st.subheader("📜 Histórico Recente")

    trades = db.get_recent_trades(limit=20)
    if trades:
        df = pd.DataFrame(trades)
        keys = ["timestamp", "side", "entry_price", "exit_price", "pnl", "status"]
        keys = [c for c in keys if c in df.columns]
        if "pnl" in df.columns:
            df["pnl_fmt"] = df["pnl"].apply(_pnl_color)
            keys[keys.index("pnl")] = "pnl_fmt"
        st.markdown(
            df[keys].to_html(escape=False, index=False),
            unsafe_allow_html=True,
        )
    else:
        st.info("Nenhum trade no histórico.")

    # ── Logout ──────────────────────────────────────────────────────────
    st.markdown("---")
    if st.button("🚪 Sair"):
        db.sign_out()
        st.session_state.page = "login"
        st.rerun()


# ---------------------------------------------------------------------------
#  Router
# ---------------------------------------------------------------------------

if not st.session_state.db.is_authenticated:
    login_page()
else:
    dashboard_page()
