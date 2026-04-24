import csv
import logging
import os
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ======================== CONFIGURATION ========================
API_KEY = os.getenv("CAPITAL_API_KEY", "").strip()
LOGIN = os.getenv("CAPITAL_LOGIN", "").strip()
PASSWORD = os.getenv("CAPITAL_PASSWORD", "").strip()

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

ENTRY_GAP_PIPS = float(os.getenv("ENTRY_GAP_PIPS", "2.0"))
STOP_LOSS_PIPS = float(os.getenv("STOP_LOSS_PIPS", "15.0"))
TAKE_PROFIT_PIPS = float(os.getenv("TAKE_PROFIT_PIPS", "50.0"))
POSITION_SIZE = float(os.getenv("POSITION_SIZE", "1.0"))

SETUP_HOUR_UTC = int(os.getenv("SETUP_HOUR_UTC", "7"))
SESSION_END_HOUR_UTC = int(os.getenv("SESSION_END_HOUR_UTC", "17"))

LOG_FILE = "fifty_pips_bot.log"
RESULTS_FILE = "fifty_pips_results.csv"
STATE_FILE = "fifty_pips_state.csv"

BASE_URL = "https://demo-api-capital.backend-capital.com"

# ======================== LOGGING ========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
logger = logging.getLogger("FiftyPips")

# ======================== RETRY SESSION ========================
def get_retry_session():
    session = requests.Session()
    retry = Retry(
        total=2,
        backoff_factor=1,
        status_forcelist=[500, 502, 503, 504],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    return session

# ======================== CAPITAL.COM CLIENT ========================
class CapitalClient:
    def __init__(self, api_key, login, password):
        self.api_key = api_key
        self.login = login
        self.password = password
        self.session = get_retry_session()
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
        data = self._req("GET", f"/api/v1/markets?searchTerm={search_term}")
        if data and data.get("markets"):
            return data["markets"][0]["epic"]
        logger.warning(f"No market found for '{search_term}'")
        return None

    def get_live_price(self, epic):
        data = self._req("GET", f"/api/v1/markets/{epic}")
        if data and "snapshot" in data:
            return float(data["snapshot"]["bid"]), float(data["snapshot"]["offer"])
        return None, None

    def get_most_recent_1h_candle(self, epic):
        """
        Fetch the most recently closed 1‑hour candle.
        Capital.com returns candles with the most recent first,
        so the first element in 'prices' is the latest closed candle.
        """
        data = self._req("GET", f"/api/v1/prices/{epic}?resolution=HOUR&max=3")
        if not data or "prices" not in data:
            return None
        # The API returns the latest candle first – use the first one.
        latest = data["prices"][0]
        return {
            "high": float(latest["highPrice"]["bid"]),
            "low": float(latest["lowPrice"]["bid"]),
            "time": latest["snapshotTime"],
        }

    def place_working_order(self, epic, direction, price, sl, tp, size):
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
        return self._req("DELETE", f"/api/v1/workingorders/{deal_id}")

    def get_working_orders(self):
        data = self._req("GET", "/api/v1/workingorders")
        return data.get("workingOrders", []) if data else []

    def get_open_positions(self):
        data = self._req("GET", "/api/v1/positions")
        return data.get("positions", []) if data else []

    def close_position(self, deal_id):
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
    multiplier = 0.01 if "JPY" in pair.upper() else 0.0001
    return round(price_diff / multiplier, 1)

def pips_to_price(pips, pair):
    multiplier = 0.01 if "JPY" in pair.upper() else 0.0001
    return pips * multiplier

# ======================== STATE PERSISTENCE ========================
def save_state(state):
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
    with open(RESULTS_FILE, "a", newline="") as f:
        csv.writer(f).writerow([
            now_utc().isoformat(), pair, direction, entry, exit_price, pnl_pips
        ])

# ======================== STRATEGY LOGIC ========================
PAIRS = ["EURUSD", "GBPUSD", "USDJPY"]

class FiftyPipsBot:
    def __init__(self, client):
        self.client = client
        self.state = load_state()
        self.setup_done_today = False
        self.last_setup_date = None

    def run_morning_setup(self):
        """Place pending orders based on the most recent 1h candle high/low."""
        if not is_weekday():
            logger.info("Weekend – skipping setup")
            return

        today_str = now_utc().strftime("%Y-%m-%d")

        # Ensure we don't run setup twice on the same day
        if self.state.get("setup_date") == today_str:
            logger.info("Setup already completed today")
            self.setup_done_today = True
            return

        # Wait a bit after 7:00 to guarantee the candle is available
        if now_utc().minute < 1:
            logger.info("Waiting for candle to settle...")
            time.sleep(60)

        chosen_pair = None
        chosen_epic = None
        chosen_candle = None

        for pair in PAIRS:
            epic = self.client.get_epic(pair)
            if not epic:
                continue
            candle = self.client.get_most_recent_1h_candle(epic)
            if not candle:
                logger.warning(f"No 1h candle returned for {pair}")
                continue
            if candle["high"] - candle["low"] <= 0:
                continue
            pip_range = price_to_pips(candle["high"] - candle["low"], pair)
            logger.info(f"{pair} most recent 1h candle range: {pip_range} pips")
            if pip_range >= 80:
                chosen_pair, chosen_epic, chosen_candle = pair, epic, candle
                break

        if not chosen_pair:
            logger.warning("No suitable pair found this morning")
            return

        # Cancel any leftover orders from yesterday
        for wo in self.client.get_working_orders():
            self.client.cancel_working_order(wo["dealId"])
            logger.info(f"Cancelled stale order {wo['dealId']}")

        gap = pips_to_price(ENTRY_GAP_PIPS, chosen_pair)
        sl_dist = pips_to_price(STOP_LOSS_PIPS, chosen_pair)
        tp_dist = pips_to_price(TAKE_PROFIT_PIPS, chosen_pair)

        buy_price = round(chosen_candle["high"] + gap, 5)
        sell_price = round(chosen_candle["low"] - gap, 5)

        buy_sl = round(buy_price - sl_dist, 5)
        buy_tp = round(buy_price + tp_dist, 5)
        buy_id = self.client.place_working_order(chosen_epic, "BUY", buy_price, buy_sl, buy_tp, POSITION_SIZE)

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

    def monitor(self):
        if not self.state or not self.state.get("buy_stop_id"):
            return

        working = self.client.get_working_orders()
        wo_ids = {wo["dealId"] for wo in working}

        buy_id = self.state["buy_stop_id"]
        sell_id = self.state["sell_stop_id"]

        if buy_id in wo_ids and sell_id in wo_ids:
            return  # Both still pending

        # One triggered, cancel the other
        if buy_id not in wo_ids and sell_id in wo_ids:
            logger.info("Buy Stop triggered – cancelling Sell Stop")
            self.client.cancel_working_order(sell_id)
            self.state["sell_stop_id"] = ""
            self.state["direction"] = "BUY"
            self.state["entry"] = 0
            save_state(self.state)

        elif sell_id not in wo_ids and buy_id in wo_ids:
            logger.info("Sell Stop triggered – cancelling Buy Stop")
            self.client.cancel_working_order(buy_id)
            self.state["buy_stop_id"] = ""
            self.state["direction"] = "SELL"
            self.state["entry"] = 0
            save_state(self.state)

        # Check open positions
        positions = self.client.get_open_positions()
        if not positions and self.state.get("direction"):
            direction = self.state["direction"]
            pair = self.state["pair"]
            logger.info(f"{pair} {direction} position closed (TP/SL hit)")
            send_telegram(f"🏁 {pair} {direction} trade completed")
            self.reset_state()
            return

        if positions and not self.state.get("position_id"):
            pos = positions[0]
            self.state["position_id"] = pos.get("dealId", "")
            self.state["entry"] = float(pos.get("openLevel", 0))
            save_state(self.state)
            logger.info(f"Position open – entry {self.state['entry']}")

    def session_close(self):
        if not self.state:
            return

        for wo in self.client.get_working_orders():
            self.client.cancel_working_order(wo["dealId"])
            logger.info(f"Session end – cancelled order {wo['dealId']}")

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
        now = now_utc()
        return now.hour == SETUP_HOUR_UTC and now.minute >= 1 and not self.setup_done_today

    def is_session_end(self):
        return now_utc().hour >= SESSION_END_HOUR_UTC

# ======================== MAIN LOOP ========================
def main():
    if not os.path.exists(RESULTS_FILE):
        with open(RESULTS_FILE, "w", newline="") as f:
            csv.writer(f).writerow(["timestamp", "pair", "direction", "entry", "exit", "pnl_pips"])

    if not all([API_KEY, LOGIN, PASSWORD]):
        logger.error("Missing Capital.com credentials")
        return

    client = CapitalClient(API_KEY, LOGIN, PASSWORD)
    bot = FiftyPipsBot(client)

    logger.info(f"🤖 50 Pips Bot started | Balance: ${client.get_balance():.2f}")
    send_telegram(f"🤖 50 Pips Bot Started\nBalance: ${client.get_balance():.2f}")

    while True:
        try:
            now = now_utc()
            if not is_weekday():
                time.sleep(60)
                continue

            if bot.is_setup_time():
                bot.run_morning_setup()
                time.sleep(60)
                continue

            if SETUP_HOUR_UTC < now.hour < SESSION_END_HOUR_UTC:
                bot.monitor()
                time.sleep(30)
                continue

            if bot.is_session_end() and bot.state:
                bot.session_close()

            time.sleep(30)

        except Exception as e:
            logger.exception(f"Loop error: {e}")
            send_telegram(f"⚠️ Bot error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()