#!/usr/bin/env python3
"""
DigitalOcean GPU Monitor Bot
Tracks H100 and H200 GPU availability across all regions
Sends Telegram notifications when GPUs become available
"""

import requests
import time
import json
from datetime import datetime

# Configuration
DO_API_TOKEN = "YOUR_DO_API_TOKEN"
TELEGRAM_BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID = "2313274238"

# Target GPU configurations to monitor
TARGET_GPUS = [
    {"slug": "h200_x8", "name": "H200 x8", "vram": "1.1 TB", "vcpu": 192, "ram": "1920 GB"},
    {"slug": "h100_x8", "name": "H100 x8", "vram": "640 GB", "vcpu": 160, "ram": "1920 GB"},
    {"slug": "h100", "name": "H100", "vram": "80 GB", "vcpu": 20, "ram": "240 GB"},
    {"slug": "h200", "name": "H200", "vram": "141 GB", "vcpu": 24, "ram": "240 GB"},
]

REGIONS = [
    "nyc1", "nyc2", "nyc3",  # New York
    "sfo1", "sfo2", "sfo3",  # San Francisco
    "ams2", "ams3",          # Amsterdam
    "lon1",                  # London
    "fra1",                  # Frankfurt
    "tor1",                  # Toronto
    "sgp1",                  # Singapore
    "blr1",                  # Bangalore
    "syd1",                  # Sydney
    "atl1",                  # Atlanta
    "ric1",                  # Richmond
]

headers = {
    "Authorization": f"Bearer {DO_API_TOKEN}",
    "Content-Type": "application/json"
}

def send_telegram(message):
    """Send message to Telegram"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        return response.json()
    except Exception as e:
        print(f"[ERROR] Telegram send failed: {e}")
        return None

def check_gpu_availability():
    """Check GPU availability across all regions"""
    available = []
    
    for region in REGIONS:
        try:
            # Check sizes available in this region
            url = f"https://api.digitalocean.com/v2/sizes?region={region}"
            response = requests.get(url, headers=headers, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                sizes = data.get("sizes", [])
                
                for size in sizes:
                    slug = size.get("slug", "").lower()
                    
                    # Check if this is a target GPU
                    for gpu in TARGET_GPUS:
                        if gpu["slug"].lower() in slug or gpu["name"].lower().replace(" ", "_") in slug:
                            if size.get("available", False):
                                available.append({
                                    "gpu": gpu["name"],
                                    "region": region,
                                    "slug": size.get("slug"),
                                    "price_monthly": size.get("price_monthly", 0),
                                    "price_hourly": size.get("price_hourly", 0),
                                    "description": size.get("description", "")
                                })
                                
        except Exception as e:
            print(f"[ERROR] Region {region}: {e}")
            continue
    
    return available

def format_notification(gpus):
    """Format Telegram message"""
    if not gpus:
        return None
    
    msg = "🚀 *GPU Available on DigitalOcean!*\n\n"
    
    for gpu in gpus:
        msg += f"📌 *{gpu['gpu']}*\n"
        msg += f"📍 Region: `{gpu['region']}`\n"
        msg += f"💰 ${gpu['price_hourly']}/hr (${gpu['price_monthly']}/mo)\n"
        msg += f"🔧 Slug: `{gpu['slug']}`\n\n"
    
    msg += f"\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} UTC"
    return msg

def main():
    """Main monitoring loop"""
    print("=" * 60)
    print("DigitalOcean GPU Monitor Started")
    print(f"Monitoring: {[g['name'] for g in TARGET_GPUS]}")
    print(f"Regions: {len(REGIONS)}")
    print(f"Telegram Chat: {TELEGRAM_CHAT_ID}")
    print("=" * 60)
    
    # Send startup notification
    send_telegram("🔍 *DO GPU Monitor Started*\nChecking for H100/H200 availability...")
    
    last_available = set()
    
    while True:
        try:
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Checking availability...")
            
            available = check_gpu_availability()
            current_set = set((g["gpu"], g["region"], g["slug"]) for g in available)
            
            # Check for new availability
            new_gpus = [g for g in available if (g["gpu"], g["region"], g["slug"]) not in last_available]
            
            if new_gpus:
                print(f"🎉 NEW GPU AVAILABLE: {len(new_gpus)} found!")
                message = format_notification(new_gpus)
                if message:
                    send_telegram(message)
                    print("📨 Telegram notification sent")
            else:
                print(f"✓ No new availability (checked {len(REGIONS)} regions)")
            
            last_available = current_set
            
            # Wait before next check
            time.sleep(30)  # Check every 30 seconds
            
        except KeyboardInterrupt:
            print("\n\n👋 Monitor stopped by user")
            send_telegram("⏹️ *DO GPU Monitor Stopped*")
            break
        except Exception as e:
            print(f"[ERROR] Main loop: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()
