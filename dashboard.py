
import streamlit as st
import pandas as pd
from database import initialize, connect

st.set_page_config(
    page_title="BTC/EUR Paper Trading",
    page_icon="₿",
    layout="wide"
)

st.title("₿ BTC/EUR Paper Trading")
st.caption("Paper Trading – keine echten Orders")

try:
    initialize()

    with connect() as conn:
        state = conn.execute(
            "SELECT * FROM bot_state WHERE id = 1"
        ).fetchone()

        equity = pd.read_sql_query(
            "SELECT time, cash, equity, pnl, signal "
            "FROM equity ORDER BY time DESC LIMIT 100",
            conn
        )

        trades = pd.read_sql_query(
            "SELECT side, entry_time, exit_time, entry_price, "
            "exit_price, quantity, pnl, costs, reason "
            "FROM trades ORDER BY id DESC LIMIT 50",
            conn
        )

        events = pd.read_sql_query(
            "SELECT time, event, details "
            "FROM events ORDER BY id DESC LIMIT 20",
            conn
        )

    if state is None:
        st.warning("Bot wurde noch nicht initialisiert.")
    else:
        st.metric("Virtuelles Bargeld", f"{state['cash']:.4f} €")
        st.metric("Anzahl Einstiege", state["entries"])
        st.write("Bot angehalten:", state["halted"])
        st.write("Offene Position:", state["position"])

    st.subheader("Eigenkapital-Verlauf")
    if not equity.empty:
        st.line_chart(equity.set_index("time")["equity"])
        st.dataframe(equity, use_container_width=True)

    st.subheader("Trade-Historie")
    st.dataframe(trades, use_container_width=True)

    st.subheader("Letzte Bot-Ereignisse")
    st.dataframe(events, use_container_width=True)

    if st.button("Ansicht aktualisieren"):
        st.rerun()

except Exception as exc:
    st.error(f"Datenbank oder Bot noch nicht bereit: {exc}")
