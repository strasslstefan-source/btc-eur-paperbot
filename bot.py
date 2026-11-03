
import json
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

BASE = "https://api.kraken.com/0/public/OHLC"
DB = Path("paperbot.sqlite3")
CONFIG = json.loads(Path("config.json").read_text())
START_CAPITAL = float(CONFIG["initial_capital_eur"])
FEE = float(CONFIG["fee_rate"])
SLIPPAGE = float(CONFIG["slippage_rate"])
MAX_ENTRY = float(CONFIG["max_entry_eur"])
MAX_ENTRIES = int(CONFIG["max_entries"])
STOP = float(CONFIG["stop_loss_pct"])
TARGET = float(CONFIG["take_profit_pct"])
COOLDOWN = int(CONFIG["cooldown_minutes"])
PAIR = CONFIG.get("symbol", "XBTEUR")

db = sqlite3.connect(DB, check_same_thread=False)
db.execute("""
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 time TEXT, event TEXT, details TEXT
)""")
db.execute("""
CREATE TABLE IF NOT EXISTS equity (
 time TEXT, cash REAL, equity REAL, pnl REAL, signal TEXT
)""")
db.execute("""
CREATE TABLE IF NOT EXISTS trades (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 side TEXT, entry_time TEXT, exit_time TEXT,
 entry_price REAL, exit_price REAL, quantity REAL,
 pnl REAL, costs REAL, reason TEXT
)""")
db.commit()

cash = START_CAPITAL
position = None
entries = 0
last_exit = None
last_5m = None
last_hour = None
start_time = datetime.now(timezone.utc)
end_time = start_time + timedelta(hours=24)
halted = False


def log(event, details):
    now = datetime.now(timezone.utc).isoformat()
    db.execute("INSERT INTO events(time,event,details) VALUES(?,?,?)",
               (now, event, str(details)))
    db.commit()
    print(now, event, details, flush=True)


def candles(interval):
    response = requests.get(
        BASE,
        params={"pair": PAIR, "interval": interval},
        timeout=20
    )
    response.raise_for_status()
    data = response.json()
    if data.get("error"):
        raise RuntimeError(str(data["error"]))
    result = data["result"]
    key = next(k for k in result if k != "last")
    rows = result[key]
    now = time.time()
    duration = interval * 60
    complete = [
        {
            "time": int(float(r[0])),
            "open": float(r[1]),
            "high": float(r[2]),
            "low": float(r[3]),
            "close": float(r[4])
        }
        for r in rows
        if float(r[0]) + duration <= now
    ]
    return complete


def ema(values, period):
    if len(values) < period:
        raise ValueError("Zu wenige EMA-Daten")
    value = sum(values[:period]) / period
    result = [None] * (period - 1) + [value]
    alpha = 2 / (period + 1)
    for price in values[period:]:
        value = price * alpha + value * (1 - alpha)
        result.append(value)
    return result


def rsi_wilder(values, period=14):
    if len(values) < period + 1:
        raise ValueError("Zu wenige RSI-Daten")
    changes = [values[i] - values[i - 1]
               for i in range(1, len(values))]
    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    result = [None] * period

    def value(g, l):
        if l == 0:
            return 100.0 if g > 0 else 50.0
        return 100 - 100 / (1 + g / l)

    result.append(value(avg_gain, avg_loss))
    for i in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        result.append(value(avg_gain, avg_loss))
    return result


def get_signal(rows):
    if len(rows) < 100:
        raise RuntimeError("Weniger als 100 abgeschlossene Stundenkerzen")
    closes = [r["close"] for r in rows]
    e12 = ema(closes, 12)
    e26 = ema(closes, 26)
    rsi = rsi_wilder(closes, 14)

    if any(x is None for x in (e12[-1], e12[-2], e26[-1],
                               e26[-2], rsi[-1])):
        return "NEUTRAL"

    if (e12[-1] > e26[-1] and e12[-1] > e12[-2]
            and e26[-1] > e26[-2] and 50 <= rsi[-1] <= 68):
        return "LONG"

    if (e12[-1] < e26[-1] and e12[-1] < e12[-2]
            and e26[-1] < e26[-2] and 32 <= rsi[-1] <= 50):
        return "SHORT"

    return "NEUTRAL"


def equity_value(mark):
    if position is None:
        return cash
    p = position
    if p["side"] == "LONG":
        exit_price = mark * (1 - SLIPPAGE)
        gross = (exit_price - p["entry"]) * p["qty"]
    else:
        exit_price = mark * (1 + SLIPPAGE)
        gross = (p["entry"] - exit_price) * p["qty"]
    exit_fee = exit_price * p["qty"] * FEE
    return cash + gross - exit_fee


def close_position(price, reason, stamp):
    global position, cash, last_exit
    p = position
    if p is None:
        return
    if p["side"] == "LONG":
        fill = price * (1 - SLIPPAGE)
        gross = (fill - p["entry"]) * p["qty"]
    else:
        fill = price * (1 + SLIPPAGE)
        gross = (p["entry"] - fill) * p["qty"]

    exit_fee = fill * p["qty"] * FEE
    cash += gross - exit_fee
    total_costs = p["entry_fee"] + exit_fee
    net = gross - total_costs

    db.execute("""
    INSERT INTO trades(side,entry_time,exit_time,entry_price,
    exit_price,quantity,pnl,costs,reason)
    VALUES(?,?,?,?,?,?,?,?,?)
    """, (p["side"], p["time"], stamp, p["entry"], fill,
          p["qty"], net, total_costs, reason))
    db.commit()
    log("POSITION_CLOSED", {
        "side": p["side"], "reason": reason,
        "net_pnl_eur": round(net, 4), "cash": round(cash, 4)
    })
    position = None
    last_exit = datetime.now(timezone.utc)


def open_position(side, price, stamp):
    global position, cash, entries
    if entries >= MAX_ENTRIES or position is not None:
        return
    if cash <= 0:
        return
    notional = min(MAX_ENTRY, cash)
    fill = price * (1 + SLIPPAGE if side == "LONG" else 1 - SLIPPAGE)
    qty = notional / fill
    fee = notional * FEE
    cash -= fee
    position = {
        "side": side, "entry": fill, "qty": qty,
        "entry_fee": fee, "time": stamp
    }
    entries += 1
    log("POSITION_OPENED", {
        "side": side, "entry": fill, "quantity": qty,
        "notional": notional, "fee": fee, "entry_count": entries
    })


def process_candle(candle, signal):
    global halted
    stamp = datetime.fromtimestamp(
        candle["time"], timezone.utc
    ).isoformat()
    op = candle["open"]

    if position:
        p = position
        if p["side"] == "LONG":
            stop_price = p["entry"] * (1 - STOP)
            target_price = p["entry"] * (1 + TARGET)
            hit_stop = candle["low"] <= stop_price
            hit_target = candle["high"] >= target_price
        else:
            stop_price = p["entry"] * (1 + STOP)
            target_price = p["entry"] * (1 - TARGET)
            hit_stop = candle["high"] >= stop_price
            hit_target = candle["low"] <= target_price

        # Bei Berührung beider Grenzen in derselben Kerze
        # wird vorsichtshalber der Stop-Loss angenommen.
        if hit_stop:
            close_position(stop_price, "STOP_LOSS", stamp)
        elif hit_target:
            close_position(target_price, "TAKE_PROFIT", stamp)

    if position and signal in ("NEUTRAL", "LONG", "SHORT"):
        if signal != position["side"]:
            close_position(op, "SIGNAL_CHANGE", stamp)
            return

    if position is None and not halted:
        if cash <= 0:
            return
        if equity_value(candle["close"]) <= START_CAPITAL - 3:
            halted = True
            log("RISK_STOP", "Keine neuen Trades; Position wird geschlossen")
            return
        if last_exit is not None:
            if datetime.now(timezone.utc) - last_exit < timedelta(minutes=COOLDOWN):
                return
        if signal in ("LONG", "SHORT") and entries < MAX_ENTRIES:
            open_position(signal, op, stamp)


def main():
    global last_5m, last_hour, halted

    log("BOT_STARTED", {
        "capital_eur": START_CAPITAL,
        "mode": "PAPER ONLY",
        "test_end_utc": end_time.isoformat()
    })

    # Beim Start keine rückwirkenden Trades ausführen.
    initial_5m = candles(5)
    initial_hour = candles(60)
    if len(initial_5m) < 2 or len(initial_hour) < 100:
        raise RuntimeError("Nicht genügend abgeschlossene Kursdaten")
    last_5m = initial_5m[-1]["time"]
    last_hour = initial_hour[-1]["time"]

    while datetime.now(timezone.utc) < end_time:
        try:
            five = candles(5)
            hours = candles(60)

            if len(five) < 2 or len(hours) < 100:
                raise RuntimeError("Zu wenige abgeschlossene Kerzen")

            now = time.time()
            if now - five[-1]["time"] - 300 > 900:
                raise RuntimeError("5-Minuten-Daten veraltet")
            if now - hours[-1]["time"] - 3600 > 7200:
                raise RuntimeError("Stundendaten veraltet")

            signal = get_signal(hours)

            if hours[-1]["time"] != last_hour:
                if hours[-1]["time"] - last_hour != 3600:
                    raise RuntimeError("Lücke in den Stundenkerzen")
                last_hour = hours[-1]["time"]
                log("NEW_HOURLY_SIGNAL", signal)

            new_candles = [c for c in five if c["time"] > last_5m]
            for candle in new_candles:
                if candle["time"] - last_5m != 300:
                    raise RuntimeError("Lücke in den 5-Minuten-Kerzen")
                process_candle(candle, signal)
                last_5m = candle["time"]

                eq = equity_value(candle["close"])
                db.execute(
                    "INSERT INTO equity(time,cash,equity,pnl,signal) VALUES(?,?,?,?,?)",
                    (datetime.now(timezone.utc).isoformat(),
                     cash, eq, eq - START_CAPITAL, signal)
                )
                db.commit()

                if eq <= START_CAPITAL - 3 and position:
                    halted = True
                    close_position(candle["open"], "RISK_STOP", 
                                   datetime.fromtimestamp(candle["time"], timezone.utc).isoformat())

            time.sleep(60)

        except Exception as exc:
            log("DATA_OR_RUNTIME_ERROR", repr(exc))
            time.sleep(60)

    # Ende des 24-Stunden-Tests: offene Position schließen.
    final_candles = candles(5)
    if position and final_candles:
        close_position(final_candles[-1]["close"], "TEST_END",
                       datetime.now(timezone.utc).isoformat())

    log("TEST_FINISHED", {
        "final_cash_eur": round(cash, 4),
        "total_pnl_eur": round(cash - START_CAPITAL, 4),
        "entries": entries
    })


if __name__ == "__main__":
    main()
