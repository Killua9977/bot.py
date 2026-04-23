import csv
import logging
import os
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests

# ======================== CONFIGURATION ========================
# Capital.com Credentials (set these as environment variables)
API_KEY = os.getenv("CAPITAL_API_KEY", "").strip()
LOGIN = os.getenv("CAPITAL_LOGIN", "").strip()
PASSWORD = os.getenv("CAPITAL_PASSWORD", "").strip()

# Telegram Alerts
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Strategy Settings (can be overridden with env vars)
ENTRY_GAP_PIPS = float(os.getenv("ENTRY_GAP_PIPS", "2.0"))
STOP_LOSS_PIPS = float(os.getenv("STOP_LOSS_PIPS", "15.0"))
TAKE_PROFIT_PIPS = float(os.getenv("TAKE_PROFIT_PIPS", "50.0"))
POSITION_SIZE = float(os.getenv("POSITION_SIZE", "1.0"))  # 1 = 1 unit

# Timing
SETUP_HOUR_UTC = int(os.getenv("SETUP_HOUR_UTC", "7"))   # 7:00 AM GMT
SESSION_END_HOUR_UTC = int(os.getenv("SESSION_END_HOUR_UTC", "17"))  # 5:00 PM GMT

# Files
LOG_FILE = "fifty_pips_bot.log"
RESULTS_FILE = "fifty_pips_results.csv"
STATE_FILE = "fifty_pips_state.csv"

# Demo API base URL
BASE_URL = "https://demo-api-capital.backend-capital.com"

# ======================== LOGGING ========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
logger = logging.getLogger("FiftyPips")

# ======================== CAPITAL.COM CLIENT ========================
class CapitalClient:
    def __init__(self, api_key, login, password):
        self.api_key = api_key
        self.login = login
        self.password = password
        self.session = requests.Session()
        self.cst = None
        self.x_sec_token = None
        self.authenticate()

    def authenticate(self):
        url = f"{BASE_URL}/api/v1/session"
        headers = {"X-CAP-API-KEY": self.api_key, "Content-Type": "application/json"}
        data = {"identifier": self.login, "password": self.password, "encryptedPassword": False}
        resp = self.session.post(url, headers=headers, json=data, timeout=30)
        if resp.status_code != 200:
            raise Exception(f"Auth failed: {resp.json().get('errorMessage', resp.text)}")
        self.cst = resp.headers["CST"]
        self.x_sec_token = resp.headers["X-SECURITY-TOKEN"]
        logger.info("✅ Connected to Capital.com Demo")

    def _req(self, method, endpoint, data=None):
        url = f"{BASE_URL}{endpoint}"
        headers = {
            "X-CAP-API-KEY": self.api_key,
            "CST": self.cst,
            "X-SECURITY-TOKEN": self.x_sec_token,
            "Content-Type": "application/json",
        }
        if method == "GET":
            resp = self.session.get(url, headers=headers, timeout=30)
        elif method == "POST":
            resp = self.session.post(url, headers=headers, json=data, timeout=30)
        elif method == "DELETE":
            resp = self.session.delete(url, headers=headers, timeout=30)
        else:
            return None

        if resp.status_code in (401, 403):
            logger.info("Session expired – re-authenticating")
            self.authenticate()
            return self._req(method, endpoint, data)

        if resp.status_code == 200:
            return resp.json()
        else:
            logger.error(f"API error {resp.status_code}: {resp.text}")
            return None

    def get_epic(self, search_term):
        """Find an instrument by search term (e.g. 'EURUSD')."""
        data = self._req("GET", f"/api/v1/markets?searchTerm={search_term}")
        if data and data.get("markets"):
            return data["markets"][0]["epic"]
        logger.warning(f"No market found for '{search_term}'")
        return None

    def get_live_price(self, epic):
        """Return (bid, ask) or (None, None)."""
        data = self._req("GET", f"/api/v1/markets/{epic}")
        if data and "snapshot" in data:
            return float(data["snapshot"]["bid"]), float(data["snapshot"]["offer"])
        return None, None

    def get_1h_candle(self, epic, hour_utc):
        """
        Fetch the 1‑hour candle that closed at `hour_utc` today.
        Capital.com timestamps are in UTC. This method downloads the last
        3 hours of 1‑hour candles and picks the one whose snapshotTime matches.
        """
        data = self._req(
            "GET",
            f"/api/v1/prices/{epic}?resolution=HOUR&max=3",
        )
        if not data or "prices" not in data:
            return None
        target_str = f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')} {hour_utc:02d}:00"
        for candle in reversed(data["prices"]):
            if candle["snapshotTime"].startswith(target_str):
                return {
                    "high": float(candle["highPrice"]["bid"]),
                    "low": float(candle["lowPrice"]["bid"]),
                }
        logger.warning(f"No 1‑hour candle found for {target_str}")
        return None

    def place_working_order(self, epic, direction, price, sl, tp, size):
        """Place a pending stop order (Buy Stop or Sell Stop)."""
        payload = {
            "epic": epic,
            "direction": direction,
            "size": size,
            "type": "STOP",
            "level": price,
            "stopDistance": abs(price - sl),
            "limitDistance": abs(tp - price),
            "guaranteedStop": False,
            "forceOpen": True,
        }
        data = self._req("POST", "/api/v1/workingorders", payload)
        if data and "dealReference" in data:
            return data["dealReference"]
        return None

    def cancel_working_order(self, deal_id):
        """Cancel a pending order."""
        return self._req("DELETE", f"/api/v1/workingorders/{deal_id}")

    def get_working_orders(self):
        """Return list of open working orders."""
        data = self._req("GET", "/api/v1/workingorders")
        return data.get("workingOrders", []) if data else []

    def get_open_positions(self):
        """Return list of open positions."""
        data = self._req("GET", "/api/v1/positions")
        return data.get("positions", []) if data else []

    def close_position(self, deal_id):
        """Close a position."""
        return self._req("DELETE", f"/api/v1/positions/{deal_id}")

    def get_balance(self):
        data = self._req("GET", "/api/v1/accounts")
        if data and data.get("accounts"):
            return float(data["accounts"][0]["balance"]["balance"])
        return 0.0

# ======================== HELPERS ========================
def send_telegram(msg):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT_ID, "text": msg},
            timeout=10,
        )
    except Exception as e:
        logger.error(f"Telegram error: {e}")

def now_utc():
    return datetime.now(timezone.utc)

def is_weekday():
    return now_utc().weekday() < 5

def price_to_pips(price_diff, pair):
    """Convert price difference to pips (JPY pairs use 0.01, others 0.0001)."""
    multiplier = 0.01 if "JPY" in pair.upper() else 0.0001
    return round(price_diff / multiplier, 1)

def pips_to_price(pips, pair):
    """Convert pips to price distance."""
    multiplier = 0.01 if "JPY" in pair.upper() else 0.0001
    return pips * multiplier

# ======================== STATE PERSISTENCE ========================
def save_state(state):
    """Save bot state to CSV (one row)."""
    with open(STATE_FILE, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["pair", "epic", "buy_stop_id", "sell_stop_id", "position_id", "direction", "entry", "sl", "tp", "setup_date"])
        w.writerow([
            state.get("pair", ""),
            state.get("epic", ""),
            state.get("buy_stop_id", ""),
            state.get("sell_stop_id", ""),
            state.get("position_id", ""),
            state.get("direction", ""),
            state.get("entry", ""),
            state.get("sl", ""),
            state.get("tp", ""),
            state.get("setup_date", ""),
        ])

def load_state():
    """Load bot state from CSV. Returns empty dict if no file or invalid."""
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            return {
                "pair": row.get("pair", ""),
                "epic": row.get("epic", ""),
                "buy_stop_id": row.get("buy_stop_id", ""),
                "sell_stop_id": row.get("sell_stop_id", ""),
                "position_id": row.get("position_id", ""),
                "direction": row.get("direction", ""),
                "entry": float(row["entry"]) if row.get("entry") else 0.0,
                "sl": float(row["sl"]) if row.get("sl") else 0.0,
                "tp": float(row["tp"]) if row.get("tp") else 0.0,
                "setup_date": row.get("setup_date", ""),
            }
    return {}

def log_result(pair, direction, entry, exit_price, pnl_pips):
    """Append closed trade to results CSV."""
    with open(RESULTS_FILE, "a", newline="") as f:
        csv.writer(f).writerow([
            now_utc().isoformat(), pair, direction, entry, exit_price, pnl_pips
        ])

# ======================== STRATEGY LOGIC ========================
PAIRS = ["EURUSD", "GBPUSD", "USDJPY"]  # Focus on 3 most liquid pairs

class FiftyPipsBot:
    def __init__(self, client: CapitalClient):
        self.client = client
        self.state = load_state()
        self.setup_done_today = False

    # ---------- Step 1: Place pending orders at 7:00 AM ----------
    def run_morning_setup(self):
        """Place buy‑stop and sell‑stop orders based on the 7:00 AM 1‑hour candle."""
        if not is_weekday():
            logger.info("Weekend – skipping setup")
            return

        today_str = now_utc().strftime("%Y-%m-%d")

        # Check if setup already done today
        if self.state.get("setup_date") == today_str:
            logger.info("Setup already completed today")
            self.setup_done_today = True
            return

        # Pick the first available pair with sufficient daily range
        chosen_pair = None
        chosen_epic = None
        chosen_candle = None

        for pair in PAIRS:
            epic = self.client.get_epic(pair)
            if not epic:
                continue
            candle = self.client.get_1h_candle(epic, SETUP_HOUR_UTC)
            if not candle:
                continue
            if candle["high"] - candle["low"] <= 0:
                continue
            # Prefer pairs with at least 100 pip daily range (classic rule)
            pip_range = price_to_pips(candle["high"] - candle["low"], pair)
            logger.info(f"{pair} 7:00 AM range: {pip_range} pips")
            if pip_range >= 80:  # Slightly relaxed from 100 to increase chances
                chosen_pair, chosen_epic, chosen_candle = pair, epic, candle
                break

        if not chosen_pair:
            logger.warning("No suitable pair found this morning")
            return

        # Check for existing working orders from yesterday and cancel them
        for wo in self.client.get_working_orders():
            self.client.cancel_working_order(wo["dealId"])
            logger.info(f"Cancelled stale order {wo['dealId']}")

        gap = pips_to_price(ENTRY_GAP_PIPS, chosen_pair)
        sl_dist = pips_to_price(STOP_LOSS_PIPS, chosen_pair)
        tp_dist = pips_to_price(TAKE_PROFIT_PIPS, chosen_pair)

        buy_price = round(chosen_candle["high"] + gap, 5)
        sell_price = round(chosen_candle["low"] - gap, 5)

        # Buy Stop
        buy_sl = round(buy_price - sl_dist, 5)
        buy_tp = round(buy_price + tp_dist, 5)
        buy_id = self.client.place_working_order(chosen_epic, "BUY", buy_price, buy_sl, buy_tp, POSITION_SIZE)

        # Sell Stop
        sell_sl = round(sell_price + sl_dist, 5)
        sell_tp = round(sell_price - tp_dist, 5)
        sell_id = self.client.place_working_order(chosen_epic, "SELL", sell_price, sell_sl, sell_tp, POSITION_SIZE)

        if buy_id and sell_id:
            logger.info(f"✅ {chosen_pair} BuyStop {buy_price} / SellStop {sell_price}")
            send_telegram(
                f"📊 50 Pips Setup – {chosen_pair}\n"
                f"Buy Stop: {buy_price}  SL: {buy_sl}  TP: {buy_tp}\n"
                f"Sell Stop: {sell_price}  SL: {sell_sl}  TP: {sell_tp}"
            )
            self.state = {
                "pair": chosen_pair,
                "epic": chosen_epic,
                "buy_stop_id": buy_id,
                "sell_stop_id": sell_id,
                "position_id": "",
                "direction": "",
                "entry": 0.0,
                "sl": 0.0,
                "tp": 0.0,
                "setup_date": today_str,
            }
            save_state(self.state)
            self.setup_done_today = True

    # ---------- Step 2: Monitor & manage ----------
    def monitor(self):
        """Check if a pending order triggered, manage open positions."""
        if not self.state or not self.state.get("buy_stop_id"):
            return

        # 1. Check working orders – if one disappeared, the other side triggered
        working = self.client.get_working_orders()
        wo_ids = {wo["dealId"] for wo in working}

        buy_id = self.state["buy_stop_id"]
        sell_id = self.state["sell_stop_id"]

        # If both still pending, nothing to do
        if buy_id in wo_ids and sell_id in wo_ids:
            return

        # One order triggered → cancel the other
        if buy_id not in wo_ids and sell_id in wo_ids:
            logger.info("Buy Stop triggered – cancelling Sell Stop")
            self.client.cancel_working_order(sell_id)
            self.state["sell_stop_id"] = ""
            self.state["direction"] = "BUY"
            self.state["entry"] = 0  # Unknown exact fill; we'll detect from positions
            save_state(self.state)

        elif sell_id not in wo_ids and buy_id in wo_ids:
            logger.info("Sell Stop triggered – cancelling Buy Stop")
            self.client.cancel_working_order(buy_id)
            self.state["buy_stop_id"] = ""
            self.state["direction"] = "SELL"
            self.state["entry"] = 0
            save_state(self.state)

        # 2. Check open positions
        positions = self.client.get_open_positions()
        if not positions and self.state.get("direction"):
            # Position already closed (TP or SL hit)
            direction = self.state["direction"]
            pair = self.state["pair"]
            logger.info(f"{pair} {direction} position closed (TP/SL hit)")
            send_telegram(f"🏁 {pair} {direction} trade completed")
            self.reset_state()
            return

        if positions and not self.state.get("position_id"):
            # Position detected, update state
            pos = positions[0]
            self.state["position_id"] = pos.get("dealId", "")
            self.state["entry"] = float(pos.get("openLevel", 0))
            save_state(self.state)
            logger.info(f"Position open – entry {self.state['entry']}")

    # ---------- Step 3: Session end cleanup ----------
    def session_close(self):
        """Cancel pending orders and close open positions."""
        if not self.state:
            return

        # Cancel remaining working orders
        for wo in self.client.get_working_orders():
            self.client.cancel_working_order(wo["dealId"])
            logger.info(f"Session end – cancelled order {wo['dealId']}")

        # Close open position
        for pos in self.client.get_open_positions():
            self.client.close_position(pos["dealId"])
            logger.info(f"Session end – closed position {pos['dealId']}")
            send_telegram(f"🔚 Session end – closed {self.state.get('pair', '')} position")

        self.reset_state()

    def reset_state(self):
        self.state = {}
        self.setup_done_today = False
        save_state({})
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)

    def is_setup_time(self):
        """Return True if current hour matches the setup hour and setup not yet done."""
        return now_utc().hour == SETUP_HOUR_UTC and not self.setup_done_today

    def is_session_end(self):
        """Return True if current hour >= session end hour."""
        return now_utc().hour >= SESSION_END_HOUR_UTC

# ======================== MAIN LOOP ========================
def main():
    # Ensure CSV files exist
    if not os.path.exists(RESULTS_FILE):
        with open(RESULTS_FILE, "w", newline="") as f:
            csv.writer(f).writerow(["timestamp", "pair", "direction", "entry", "exit", "pnl_pips"])

    if not all([API_KEY, LOGIN, PASSWORD]):
        logger.error("Missing Capital.com credentials – set CAPITAL_API_KEY, CAPITAL_LOGIN, CAPITAL_PASSWORD")
        return

    client = CapitalClient(API_KEY, LOGIN, PASSWORD)
    bot = FiftyPipsBot(client)

    logger.info(f"🤖 50 Pips Bot started | Balance: ${client.get_balance():.2f}")
    send_telegram(f"🤖 50 Pips Bot Started\nBalance: ${client.get_balance():.2f}")

    while True:
        try:
            now = now_utc()

            # Skip weekends
            if not is_weekday():
                time.sleep(60)
                continue

            # Morning setup (exactly at setup hour)
            if bot.is_setup_time():
                bot.run_morning_setup()
                time.sleep(60)
                continue

            # During the day: monitor trades
            if SETUP_HOUR_UTC < now.hour < SESSION_END_HOUR_UTC:
                bot.monitor()
                time.sleep(30)
                continue

            # Session end cleanup
            if bot.is_session_end() and bot.state:
                bot.session_close()

            time.sleep(30)

        except Exception as e:
            logger.exception(f"Loop error: {e}")
            send_telegram(f"⚠️ Bot error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()