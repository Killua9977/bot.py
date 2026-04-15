import yfinance as yf
import requests
import time
import csv
import os

TOKEN = "8571856335:AAFWGExs1m6ufjk4qCbIT7SvEBN6JBK4R04"
CHAT_ID = "7454699794"

pairs = {
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",
    "USDCHF": "USDCHF=X",
    "AUDUSD": "AUDUSD=X",
    "USDCAD": "USDCAD=X",
    "NZDUSD": "NZDUSD=X",
    "EURGBP": "EURGBP=X",
    "EURJPY": "EURJPY=X",
    "GBPJPY": "GBPJPY=X",
    "AUDJPY": "AUDJPY=X",
    "EURAUD": "EURAUD=X",
    "GBPAUD": "GBPAUD=X",
    "EURCAD": "EURCAD=X",
    "GBPCAD": "GBPCAD=X",
}

ACCOUNT_BALANCE = 100
RISK_PERCENT = 2

trades = []
wins = 0
losses = 0

FILE_NAME = "trades.csv"

# 📁 CREATE FILE IF NOT EXISTS
if not os.path.exists(FILE_NAME):
    with open(FILE_NAME, mode="w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["Pair", "Type", "Entry", "SL", "TP", "Result"])

def save_trade(trade, result):
    with open(FILE_NAME, mode="a", newline="") as file:
        writer = csv.writer(file)
        writer.writerow([
            trade["pair"],
            trade["type"],
            trade["entry"],
            trade["sl"],
            trade["tp"],
            result
        ])

def send(msg):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg}, timeout=10)
    except:
        print("Telegram send failed")

def has_open_trade(pair):
    for trade in trades:
        if trade["pair"] == pair and trade["status"] in ["PENDING", "OPEN"]:
            return True
    return False

def calculate_position_size(entry, sl):
    risk_amount = ACCOUNT_BALANCE * (RISK_PERCENT / 100)
    sl_distance = abs(entry - sl)
    if sl_distance == 0:
        return 0
    return round(risk_amount / sl_distance, 2)

# 🔥 TRACKER
def check_trades():
    global wins, losses

    for trade in trades:
        try:
            data = yf.Ticker(trade["symbol"]).history(period="1d", interval="1m")
            if data.empty:
                continue

            price = float(data["Close"].iloc[-1])

            # ENTRY HIT
            if trade["status"] == "PENDING":
                if trade["type"] == "BUY" and price <= trade["entry"]:
                    trade["status"] = "OPEN"
                    trade["entry_time"] = time.time()

                    send(
                        f"🚨 ENTRY HIT 🚨\n"
                        f"{trade['pair']} BUY\n"
                        f"Entry: {trade['entry']}\n"
                        f"SL: {trade['sl']}\n"
                        f"TP: {trade['tp']}"
                    )

                elif trade["type"] == "SELL" and price >= trade["entry"]:
                    trade["status"] = "OPEN"
                    trade["entry_time"] = time.time()

                    send(
                        f"🚨 ENTRY HIT 🚨\n"
                        f"{trade['pair']} SELL\n"
                        f"Entry: {trade['entry']}\n"
                        f"SL: {trade['sl']}\n"
                        f"TP: {trade['tp']}"
                    )

            elif trade["status"] == "OPEN":

                # ⏳ COOLDOWN
                if time.time() - trade["entry_time"] < 10:
                    continue

                if trade["type"] == "BUY":
                    if price >= trade["tp"]:
                        trade["status"] = "WIN"
                        wins += 1
                        save_trade(trade, "WIN")
                        send(f"✅ WIN {trade['pair']}")

                    elif price <= trade["sl"]:
                        trade["status"] = "LOSS"
                        losses += 1
                        save_trade(trade, "LOSS")
                        send(f"❌ LOSS {trade['pair']}")

                elif trade["type"] == "SELL":
                    if price <= trade["tp"]:
                        trade["status"] = "WIN"
                        wins += 1
                        save_trade(trade, "WIN")
                        send(f"✅ WIN {trade['pair']}")

                    elif price >= trade["sl"]:
                        trade["status"] = "LOSS"
                        losses += 1
                        save_trade(trade, "LOSS")
                        send(f"❌ LOSS {trade['pair']}")

        except:
            continue

# 🔥 MAIN STRATEGY
def scan_market():
    output = "📊 SNIPER SIGNALS\n\n"

    for name, symbol in pairs.items():
        try:
            data = yf.Ticker(symbol).history(period="5d", interval="15m")
            data_h1 = yf.Ticker(symbol).history(period="5d", interval="1h")

            if data.empty or data_h1.empty:
                continue

            close = data["Close"]
            price = float(close.iloc[-1])
            ma10 = close.rolling(10).mean().iloc[-1]
            ma50 = close.rolling(50).mean().iloc[-1]

            delta = close.diff()
            gain = delta.clip(lower=0).rolling(14).mean()
            loss = -delta.clip(upper=0).rolling(14).mean()
            rs = gain / loss
            rsi = 100 - (100 / (1 + rs))
            rsi_value = float(rsi.iloc[-1])

            close_h1 = data_h1["Close"]
            price_h1 = float(close_h1.iloc[-1])
            ma50_h1 = close_h1.rolling(50).mean().iloc[-1]

            trend_strength = abs(ma10 - ma50)

            if trend_strength < 0.0010:
                continue

            if (
                price > ma50 and ma10 > ma50 and
                price_h1 > ma50_h1 and
                55 < rsi_value < 65
            ):
                result = "BUY"

            elif (
                price < ma50 and ma10 < ma50 and
                price_h1 < ma50_h1 and
                35 < rsi_value < 45
            ):
                result = "SELL"

            else:
                continue

            if has_open_trade(name):
                continue

            entry = round(ma10, 5)

            if result == "BUY":
                sl = round(entry - 0.0080, 5)
                tp = round(entry + 0.0160, 5)
            else:
                sl = round(entry + 0.0080, 5)
                tp = round(entry - 0.0160, 5)

            lot = calculate_position_size(entry, sl)

            trades.append({
                "pair": name,
                "symbol": symbol,
                "type": result,
                "entry": entry,
                "sl": sl,
                "tp": tp,
                "status": "PENDING",
                "entry_time": None
            })

            output += (
                f"{name} {result}\n"
                f"Entry: {entry}\n"
                f"SL: {sl}\n"
                f"TP: {tp}\n"
                f"Lot Size: {lot}\n"
                f"----------------------\n"
            )

        except:
            continue

    if output != "📊 SNIPER SIGNALS\n\n":
        send(output)

def send_performance():
    total = wins + losses
    winrate = (wins / total * 100) if total > 0 else 0

    send(
        f"📊 PERFORMANCE\n\n"
        f"Wins: {wins}\n"
        f"Losses: {losses}\n"
        f"Win Rate: {round(winrate, 2)}%"
    )

def run_bot():
    print("Bot started... SNIPER V2 PRO MODE")

    last_scan = 0
    last_heartbeat = 0
    last_report = 0

    while True:
        try:
            now = time.time()

            if now - last_scan > 600:
                scan_market()
                last_scan = now

            check_trades()

            if now - last_heartbeat > 300:
                send("💓 Bot is alive")
                last_heartbeat = now

            if now - last_report > 1800:
                send_performance()
                last_report = now

            print("Bot running...")

        except Exception as e:
            print("Error:", e)

        time.sleep(10)

run_bot()
