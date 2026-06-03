#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram Log Streamer for DO GPU Snipers
Pulls tmux logs from both remote servers via SSH and live-edits a Telegram message.
"""

import time
import subprocess
import requests

TG_BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
TG_CHAT_ID   = "-1002313274238"

SERVER_1_IP = "206.189.232.0"  # Old Sniper
SERVER_2_IP = "162.243.96.222" # New Sniper

LINES = 12
UPDATE_INTERVAL = 6.0

def get_remote_logs(ip):
    try:
        cmd = [
            "ssh", 
            "-o", "StrictHostKeyChecking=no", 
            "-o", "ConnectTimeout=3",
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath=/tmp/ssh-{ip}",
            "-o", "ControlPersist=600",
            f"root@{ip}",
            f"tmux capture-pane -t sniper -p | tail -n {LINES}"
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            return result.stdout.strip()
        else:
            return f"Error connecting/fetching from {ip}:\n{result.stderr.strip()}"
    except Exception as e:
        return f"Exception fetching from {ip}: {e}"

def clean_text(text):
    # Remove ANSI escape codes (colors) so it looks clean in Telegram
    import re
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi_escape.sub('', text)

def build_message_text():
    log1 = clean_text(get_remote_logs(SERVER_1_IP))
    log2 = clean_text(get_remote_logs(SERVER_2_IP))
    
    if not log1: log1 = "(no output or session is empty)"
    if not log2: log2 = "(no output or session is empty)"
    
    msg = f"📡 **LIVE: DO GPU SNIPERS**\n\n"
    msg += f"🖥 **Server 1 (Old Bot, 20 Keys) | {SERVER_1_IP}**\n"
    msg += f"```text\n{log1}\n```\n\n"
    msg += f"🖥 **Server 2 (New Bot, TUI) | {SERVER_2_IP}**\n"
    msg += f"```text\n{log2}\n```"
    return msg

def send_message(text):
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        return r.json().get("result", {}).get("message_id")
    except Exception as e:
        print(f"Error sending message: {e}")
        return None

def edit_message(message_id, text):
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/editMessageText"
    payload = {
        "chat_id": TG_CHAT_ID,
        "message_id": message_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        # Telegram returns 400 if message content is exactly the same, which is fine
        if r.status_code == 400 and "exactly the same" in r.text:
            return True
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"Error editing message: {e}")
        return False

def main():
    print("Fetching initial logs...")
    text = build_message_text()
    
    print("Sending initial Telegram message...")
    msg_id = send_message(text)
    if not msg_id:
        print("Failed to send initial message. Exiting.")
        return
    
    print(f"Message sent! ID={msg_id}. Starting live stream...")
    
    while True:
        time.sleep(UPDATE_INTERVAL)
        text = build_message_text()
        print(f"[{time.strftime('%H:%M:%S')}] Updating Telegram message...")
        edit_message(msg_id, text)

if __name__ == "__main__":
    main()
