from __future__ import annotations

import logging
import os
import sys

import pandas as pd
import streamlit as st

# Garantir que o diretório raiz do projeto esteja no sys.path
# para que "from src.config" funcione independente de como o
# Streamlit é invocado (streamlit run dashboard/app.py).
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


def dashboard_page() -> None:
    db: SupabaseDB = st.session_state.db

    settings = get_settings()
    mode = "🔴 LIVE" if settings.is_live else "🟢 PAPER"
    st.markdown(f"# 🤖 Bot Crypto  ·  `{mode}`")
    st.caption(f"{', '.join(settings.symbols)} · 15m · volume_expansion")

    # ── Bot state + controle ──────────────────────────────────────────
    state = db.get_bot_state() or {}
    status = state.get("status", "STOPPED")
    position = state.get("current_position", "FLAT")

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        status_emoji = {"RUNNING": "🟢", "STOPPED": "🔴", "ERROR": "🟡"}
        st.metric("Status", f"{status_emoji.get(status, '⚪')} {status}")
    with col2:
        st.metric("Posição", position)
    with col3:
        sh = state.get("last_squeeze_high")
        st.metric("Squeeze High", f"{sh:,.2f}" if sh else "—")
    with col4:
        sl = state.get("last_squeeze_low")
        st.metric("Squeeze Low", f"{sl:,.2f}" if sl else "—")

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

    # ── Sumário de PnL ────────────────────────────────────────────────
    st.markdown("---")
    st.subheader("📊 Resumo de Trades")

    summary = db.get_summary()
    cols = st.columns(5)
    with cols[0]: st.metric("Total", summary["total"])
    with cols[1]: st.metric("Wins", summary["wins"])
    with cols[2]: st.metric("Losses", summary["losses"])
    with cols[3]: st.metric("Win Rate", f"{summary['win_rate']:.1%}")
    with cols[4]:
        pnl = summary["pnl"]
        st.metric("PnL Total", f"${pnl:+,.2f}" if pnl else "$0.00")

    # ── Trades abertos ────────────────────────────────────────────────
    st.markdown("---")
    st.subheader("📌 Trades Abertos")

    open_trades = db.get_open_trades()
    if open_trades:
        df = pd.DataFrame(open_trades)
        cols = ["timestamp", "side", "entry_price", "amount", "strategy_name"]
        st.dataframe(df[[c for c in cols if c in df.columns]], use_container_width=True)
    else:
        st.info("Nenhum trade aberto.")

    # ── Histórico recente ─────────────────────────────────────────────
    st.markdown("---")
    st.subheader("📜 Histórico Recente")

    trades = db.get_recent_trades(limit=20)
    if trades:
        df = pd.DataFrame(trades)
        cols = ["timestamp", "side", "entry_price", "exit_price", "pnl", "status"]
        cols = [c for c in cols if c in df.columns]
        if "pnl" in df.columns:
            df["pnl"] = df["pnl"].apply(lambda v: f"${v:+,.2f}" if v is not None else "—")
        st.dataframe(df[cols], use_container_width=True)
    else:
        st.info("Nenhum trade no histórico.")

    # ── Logout ────────────────────────────────────────────────────────
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
