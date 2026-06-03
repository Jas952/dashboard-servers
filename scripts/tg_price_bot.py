import time
import json
import os
import requests
from datetime import datetime

BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
CHAT_ID = "YOUR_CHAT_ID"
STATE_FILE = "tg_price_msg_id.txt"

def get_safetrade_price():
    url = "https://safe.trade/api/v2/peatio/public/markets/prlusdt/tickers"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    }
    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
            return float(data.get("ticker", {}).get("last", 0))
    except Exception as e:
        print(f"Error fetching SafeTrade: {e}")
    return None

def get_pearl_otc_price():
    url = "https://app.pearl-otc.com/api/otc/offers"
    headers = {
        "accept": "*/*",
        "accept-language": "en,en-US;q=0.9,ru-RU;q=0.8,ru;q=0.7",
        "authorization": "Bearer YOUR_JWT_TOKEN",
        "cache-control": "no-cache",
        "cookie": "YOUR_COOKIE",
        "pragma": "no-cache",
        "priority": "u=1, i",
        "referer": "https://app.pearl-otc.com/",
        "sec-ch-ua": "\"Google Chrome\";v=\"147\", \"Not.A/Brand\";v=\"8\", \"Chromium\";v=\"147\"",
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": "\"macOS\"",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
    }
    
    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
            offers = data.get("offers", [])
            sells = [float(o["price_per_prl"]) for o in offers if o.get("side") == "SELL_PRL" and o.get("status") == "ACTIVE"]
            buys = [float(o["price_per_prl"]) for o in offers if o.get("side") == "BUY_PRL" and o.get("status") == "ACTIVE"]
            
            best_ask = min(sells) if sells else None
            best_bid = max(buys) if buys else None
            return best_ask, best_bid
    except Exception as e:
        print(f"Error fetching Pearl OTC: {e}")
    return None, None

def get_pearl_last_sale():
    url = "https://app.pearl-otc.com/api/stats/settlements"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
            settlements = data.get("settlements", [])
            if settlements:
                last_sale = settlements[0]
                prl = last_sale.get("prl", 0)
                usdc = last_sale.get("usdc", 0)
                time_str = last_sale.get("time", "")
                
                try:
                    dt = datetime.strptime(time_str.split(".")[0], "%Y-%m-%dT%H:%M:%S")
                    formatted_time = dt.strftime("%Y-%m-%d %H:%M:%S")
                except:
                    formatted_time = time_str
                    
                return f"{prl:g} PRL  {usdc:g} USD [{formatted_time}]"
    except Exception as e:
        print(f"Error fetching Pearl OTC last sale: {e}")
    return "N/A"

def get_saved_msg_id():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            content = f.read().strip()
            if content.isdigit():
                return int(content)
    return None

def save_msg_id(msg_id):
    with open(STATE_FILE, "w") as f:
        f.write(str(msg_id))

def send_or_update_telegram(text):
    msg_id = get_saved_msg_id()
    url_base = f"https://api.telegram.org/bot{BOT_TOKEN}/"
    
    if msg_id:
        # Edit existing message
        url = url_base + "editMessageText"
        payload = {
            "chat_id": CHAT_ID,
            "message_id": msg_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        r = requests.post(url, json=payload)
        if r.status_code == 200:
            print(f"Successfully updated message {msg_id}")
            return
        else:
            print(f"Failed to edit message {msg_id}, response: {r.text}")
            # If failed (e.g. message deleted), we will send a new one
            print("Sending a new message instead...")
            
    # Send new message
    url = url_base + "sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    r = requests.post(url, json=payload)
    if r.status_code == 200:
        new_id = r.json().get("result", {}).get("message_id")
        if new_id:
            save_msg_id(new_id)
            print(f"Successfully sent new message with ID: {new_id}")
    else:
        print(f"Failed to send message: {r.text}")

def main():
    print("Starting Telegram Price Tracker Bot...")
    while True:
        try:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching prices...")
            safetrade_price = get_safetrade_price()
            best_ask, best_bid = get_pearl_otc_price()
            last_sale = get_pearl_last_sale()
            
            # Format message
            st_text = f"${safetrade_price:.4f}" if safetrade_price else "N/A"
            ask_text = f"${best_ask:.4f}" if best_ask else "N/A"
            bid_text = f"${best_bid:.4f}" if best_bid else "N/A"
            
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC+3")
            
            message = (
                "🟢 SafeTrade (PRL/USDT):\n"
                f"└ Last Price: {st_text}\n\n"
                "⚪️ Pearl OTC:\n"
                f"├ Best Ask (Sell): {ask_text}\n"
                f"├ Best Bid (Buy): {bid_text}\n"
                f"└ Last Sale: {last_sale}\n\n"
                "(SafeTrade) [https://safetrade.com/exchange/PRL-USDT?type=basic]\n"
                "(Pearl OTC) [https://app.pearl-otc.com/]\n\n"
                f"[{now_str}]"
            )
            
            send_or_update_telegram(message)
            
        except Exception as e:
            print(f"Unhandled exception in main loop: {e}")
            
        # Sleep for 5 minutes (300 seconds)
        print("Sleeping for 5 minutes...")
        time.sleep(300)

if __name__ == "__main__":
    main()
