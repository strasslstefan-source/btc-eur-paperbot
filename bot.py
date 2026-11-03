
import json
import os
import time
from datetime import datetime, timezone, timedelta

import requests
from database import initialize, connect

API = "https://api.kraken.com/0/public/OHLC"
CFG = json.load(open("config.json"))
PAIR = CFG["symbol"]
START_CAPITAL = float(CFG["initial_capital_eur"])
FEE = float(CFG["fee_rate"])
SLIP = float(CFG["slippage_rate"])
MAX_ENTRY = float(CFG["max_entry_eur"])
MAX_ENTRIES = int(CFG["max_entries"])
STOP = float(CFG["stop_loss_pct"])
TARGET = float(CFG["take_profit_pct"])
COOLDOWN = int(CFG["cooldown_minutes"])

def log(event, details):
    with connect() as db:
        db.execute(
            "INSERT INTO events(event,details) VALUES(%s,%s)",
            (event, json.dumps(details))
        )

def candles(interval):
    r = requests.get(
        API,
        params={"pair": PAIR, "interval": interval},
        timeout=20
    )
    r.raise_for_status()
    data = r.json()
    if data["error"]:
        raise RuntimeError(str(data["error"]))
    rows = next(v for k, v in data["result"].items() if k != "last")
    now = time.time()
    return [
        {
            "time": int(float(x[0])),
            "open": float(x[1]),
            "high": float(x[2]),
            "low": float(x[3]),
            "close": float(x[4])
        }
        for x in rows
        if float(x[0]) + interval * 60 <= now
    ]

def ema(values, period):
    result = [None] * (period - 1)
    value = sum(values[:period]) / period
    result.append(value)
    alpha = 2 / (period + 1)
    for x in values[period:]:
        value = alpha * x + (1 - alpha) * value
        result.append(value)
    return result

def rsi(values, period=14):
    changes = [values[i] - values[i-1] for i in range(1, len(values))]
    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period

    def calc(g, l):
        if l == 0:
            return 100.0 if g else 50.0
        return 100 - 100 / (1 + g / l)

    result = [None] * period + [calc(ag, al)]
    for i in range(period, len(changes)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
        result.append(calc(ag, al))
    return result

def signal(rows):
    if len(rows) < 100:
        raise RuntimeError("Zu wenige Stundenkerzen")
    closes = [x["close"] for x in rows]
    a = ema(closes, 12)
    b = ema(closes, 26)
    r = rsi(closes)
    if a[-1] > b[-1] and a[-1] > a[-2] and b[-1] > b[-2] and 50 <= r[-1] <= 68:
        return "LONG"
    if a[-1] < b[-1] and a[-1] < a[-2] and b[-1] < b[-2] and 32 <= r[-1] <= 50:
        return "SHORT"
    return "NEUTRAL"

def load_state():
    with connect() as db:
        state = db.execute(
            "SELECT * FROM bot_state WHERE id=1"
        ).fetchone()
        if state:
            return state
        now = datetime.now(timezone.utc)
        db.execute("""
            INSERT INTO bot_state
            (id,cash,entries,position,test_start,test_end,halted)
            VALUES(1,%s,0,NULL,%s,%s,FALSE)
        """, (START_CAPITAL, now, now + timedelta(hours=24)))
        return db.execute(
            "SELECT * FROM bot_state WHERE id=1"
        ).fetchone()

def save_state(cash, entries, position, last_exit, halted):
    with connect() as db:
        db.execute("""
            UPDATE bot_state
            SET cash=%s, entries=%s, position=%s,
                last_exit=%s, halted=%s
            WHERE id=1
        """, (
            cash, entries,
            json.dumps(position) if position else None,
            last_exit, halted
        ))

def main():
    initialize()
    state = load_state()
    cash = float(state["cash"])
    entries = int(state["entries"])
    position = state["position"]
    last_exit = state["last_exit"]
    halted = bool(state["halted"])
    test_end = state["test_end"]

    if datetime.now(timezone.utc) >= test_end:
        log("TEST_EXPIRED", {"message": "24h-Testzeit abgelaufen"})
        return

    log("WORKER_STARTED", {"cash": cash, "entries": entries})

    last5 = None
    lasth = None

    while datetime.now(timezone.utc) < test_end:
        try:
            five = candles(5)
            hours = candles(60)
            if len(five) < 2 or len(hours) < 100:
                raise RuntimeError("Nicht genügend Kerzendaten")

            if time.time() - (five[-1]["time"] + 300) > 900:
                raise RuntimeError("5m-Daten veraltet")
            if time.time() - (hours[-1]["time"] + 3600) > 7200:
                raise RuntimeError("Stundendaten veraltet")

            sig = signal(hours)

            if lasth is not None and hours[-1]["time"] - lasth != 3600:
                raise RuntimeError("Lücke in Stundenkerzen")
            lasth = hours[-1]["time"]

            new = [c for c in five if last5 is not None and c["time"] > last5]
            if last5 is None:
                last5 = five[-1]["time"]
            else:
                for c in new:
                    if c["time"] - last5 != 300:
                        raise RuntimeError("Lücke in 5m-Kerzen")
                    stamp = datetime.fromtimestamp(c["time"], timezone.utc)

                    if position:
                        side = position["side"]
                        entry = float(position["entry"])
                        if side == "LONG":
                            stop = entry * (1 - STOP)
                            target = entry * (1 + TARGET)
                            stop_hit = c["low"] <= stop
                            target_hit = c["high"] >= target
                        else:
                            stop = entry * (1 + STOP)
                            target = entry * (1 - TARGET)
                            stop_hit = c["high"] >= stop
                            target_hit = c["low"] <= target

                        exit_reason = None
                        exit_price = None
                        if stop_hit:
                            exit_reason, exit_price = "STOP_LOSS", stop
                        elif target_hit:
                            exit_reason, exit_price = "TAKE_PROFIT", target
                        elif sig != side:
                            exit_reason, exit_price = "SIGNAL_CHANGE", c["open"]

                        if exit_reason:
                            fill = exit_price * (
                                1 - SLIP if side == "LONG" else 1 + SLIP
                            )
                            qty = float(position["qty"])
                            gross = (
                                (fill - entry) * qty if side == "LONG"
                                else (entry - fill) * qty
                            )
                            exit_fee = fill * qty * FEE
                            entry_fee = float(position["entry_fee"])
                            pnl = gross - exit_fee - entry_fee
                            cash += gross - exit_fee

                            with connect() as db:
                                db.execute("""
                                    INSERT INTO trades
                                    (side,entry_time,exit_time,entry_price,
                                    exit_price,quantity,pnl,costs,reason)
                                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                                """, (
                                    side, position["time"], stamp, entry,
                                    fill, qty, pnl,
                                    entry_fee + exit_fee, exit_reason
                                ))
                            position = None
                            last_exit = stamp

                    if position is None and not halted:
                        if cash <= 0:
                            halted = True
                        elif cash <= START_CAPITAL - 3:
                            halted = True
                        elif last_exit is None or stamp - last_exit >= timedelta(minutes=COOLDOWN):
                            if sig in ("LONG", "SHORT") and entries < MAX_ENTRIES:
                                notional = min(MAX_ENTRY, cash)
                                fill = c["open"] * (
                                    1 + SLIP if sig == "LONG" else 1 - SLIP
                                )
                                qty = notional / fill
                                fee = notional * FEE
                                cash -= fee
                                position = {
                                    "side": sig, "entry": fill,
                                    "qty": qty, "entry_fee": fee,
                                    "time": stamp.isoformat()
                                }
                                entries += 1
                                log("POSITION_OPENED", {
                                    "side": sig, "price": fill,
                                    "quantity": qty
                                })

                    mark = c["close"]
                    eq = cash
                    if position:
                        side = position["side"]
                        entry = float(position["entry"])
                        qty = float(position["qty"])
                        hypothetical = mark * (
                            1 - SLIP if side == "LONG" else 1 + SLIP
                        )
                        gross = (
                            (hypothetical - entry) * qty if side == "LONG"
                            else (entry - hypothetical) * qty
                        )
                        eq += gross - hypothetical * qty * FEE

                    with connect() as db:
                        db.execute("""
                            INSERT INTO equity(cash,equity,pnl,signal)
                            VALUES(%s,%s,%s,%s)
                        """, (cash, eq, eq - START_CAPITAL, sig))

                    save_state(cash, entries, position, last_exit, halted)
                    last5 = c["time"]

            time.sleep(60)

        except Exception as exc:
            log("DATA_OR_RUNTIME_ERROR", {"error": str(exc)})
            time.sleep(60)

    log("TEST_FINISHED", {"cash": cash, "entries": entries})

if __name__ == "__main__":
    main()
