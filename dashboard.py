
import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st

st.set_page_config(
    page_title="BTC/EUR Paper Trading",
    page_icon="₿",
    layout="wide"
)

DB = Path("paperbot.sqlite3")

st.title("₿ BTC/EUR Paper Trading")
st.caption("Virtuelles Trading – keine echten Orders")

if not DB.exists():
    st.warning("Noch keine Bot-Daten vorhanden. Der Bot wurde möglicherweise noch nicht gestartet.")
    st.stop()

con = sqlite3.connect(DB)

try:
    equity = pd.read_sql_query(
        "SELECT * FROM equity ORDER BY time DESC LIMIT 1",
        con
    )
    events = pd.read_sql_query(
        "SELECT * FROM events ORDER BY id DESC LIMIT 20",
        con
    )
    trades = pd.read_sql_query(
        "SELECT * FROM trades ORDER BY id DESC LIMIT 20",
        con
    )

    if not equity.empty:
        latest = equity.iloc[0]
        st.subheader("Kontostand")
        c1, c2, c3 = st.columns(3)
        c1.metric("Virtuelles Eigenkapital", f"{latest['equity']:.4f} €")
        c2.metric("Bargeld", f"{latest['cash']:.4f} €")
        c3.metric("Gewinn / Verlust", f"{latest['pnl']:+.4f} €")
        st.write("Letztes Signal:", latest["signal"])
    else:
        st.info("Noch keine abgeschlossenen Ausführungsintervalle.")

    st.subheader("Letzte Trades")
    if trades.empty:
        st.info("Noch keine abgeschlossenen Trades.")
    else:
        st.dataframe(trades, use_container_width=True)

    st.subheader("Bot-Ereignisse")
    if events.empty:
        st.info("Noch keine Ereignisse.")
    else:
        st.dataframe(events, use_container_width=True)

finally:
    con.close()

st.caption("Aktualisiere die Seite im Browser, um neue Daten zu laden.")
