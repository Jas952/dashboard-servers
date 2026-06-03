#!/usr/bin/env python3
import sys, os, time, json, select, subprocess, ssl, re
import urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

MSK = timezone(timedelta(hours=3))
def get_msk_date(ts):
    return datetime.fromtimestamp(ts, MSK).date()

def pad(s, w): return s + " " * max(0, w - len(re.sub(r'\033\[[0-9;]*m', '', s)))

WALLET = "YOUR_WALLET_ADDRESS"
POOL_API = f"https://api.example.com/worker/{WALLET}"

app_state = {
    "vast_balance": 0.0,
    "vast_instances": [],
    "pool_data": {},
    "payments": [],
    "server_history": {},
    "last_update": 0,
    "loading": True,
    "error": ""
}

tree_state = {}
tree_selected_idx = 0
tree_scroll = 0
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)
HISTORY_FILE = os.path.join(ROOT_DIR, "data", "payments_history.json")

def merge_payments(new_payments):
    try:
        history = []
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, "r") as f:
                history = json.load(f)
                
        merged = {p["ts"]: p for p in history}
        
        for p in new_payments:
            merged[p["ts"]] = p
            
        new_paid_ts = [p["ts"] for p in new_payments if p.get("status") == "paid"]
        if new_paid_ts:
            max_paid_ts = max(new_paid_ts)
            for ts, p in merged.items():
                if ts <= max_paid_ts and p.get("status") == "pending":
                    p["status"] = "paid"
                    
        final_list = sorted(list(merged.values()), key=lambda x: x.get("ts", 0), reverse=True)[:500]
        
        with open(HISTORY_FILE, "w") as f:
            json.dump(final_list, f)
            
        return final_list
    except Exception:
        return new_payments

def fetch_data():
    global app_state
    while True:
        try:
            # 1. Fetch Vast Balance
            res_user = subprocess.run(["vastai", "show", "user", "--raw"], capture_output=True, text=True)
            if res_user.returncode == 0:
                user_data = json.loads(res_user.stdout)
                app_state["vast_balance"] = user_data.get("credit", 0.0)
                app_state["vast_spend"] = abs(user_data.get("total_spend", 0.0))
                
            # 2. Fetch Vast Instances
            res_inst = subprocess.run(["vastai", "show", "instances", "--raw"], capture_output=True, text=True)
            if res_inst.returncode == 0:
                app_state["vast_instances"] = json.loads(res_inst.stdout)
                
            # 3. Fetch Pool Data
            req = urllib.request.Request(POOL_API)
            req.add_header('User-Agent', 'Mozilla/5.0')
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(req, timeout=10, context=ctx) as response:
                pool_raw = json.loads(response.read().decode())
                app_state["pool_data"] = pool_raw
                app_state["payments"] = merge_payments(pool_raw.get("payments", []))
                
            # 4. Fetch Server History
            history_path = os.path.join(ROOT_DIR, "data", "history.json")
            if os.path.exists(history_path):
                with open(history_path, "r") as f:
                    app_state["server_history"] = json.load(f)
                    
            app_state["error"] = ""
        except Exception as e:
            app_state["error"] = str(e)
            
        app_state["loading"] = False
        app_state["last_update"] = time.time()
        time.sleep(60)

def main():
    global tree_state, tree_selected_idx, tree_scroll
    import threading
    threading.Thread(target=fetch_data, daemon=True).start()
    
    sys.stdout.write("\033[?1049h\033[H\033[2J\033[?25l")
    sys.stdout.flush()
    
    try:
        fd = sys.stdin.fileno()
        import termios, tty
        old_settings = termios.tcgetattr(fd)
        tty.setcbreak(fd)
        
        while True:
            import shutil
            cols, rows = shutil.get_terminal_size((120, 40))
            buf = ["\033[H"]
            
            # --- HEADER ---
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            buf.append(f"\033[1;36m{' PEARL MINER DASHBOARD ':=^100}\033[0m\033[K\r\n")
            
            if app_state["loading"]:
                buf.append("  \033[93m⏳ Fetching initial data from Vast.ai & AlphaPool...\033[0m\033[K\r\n")
            else:
                vast_bal = app_state["vast_balance"]
                vast_spend = app_state.get("vast_spend", 0.0)
                bal_color = "\033[91m" if vast_bal < 10.0 else "\033[92m"
                pool_data = app_state["pool_data"]
                
                pool_bal = pool_data.get("balance_prl", 0.0)
                pool_paid = pool_data.get("total_paid_prl", 0.0)
                total_mined = pool_bal + pool_paid
                hash_24 = pool_data.get("estHash24h", "0 TH/s")
                hash_raw = pool_data.get("estHash24hRaw", 0)
                
                vast_warning = " \033[91m(⚠️ LOW BALANCE!)\033[0m" if vast_bal < 10.0 else ""
                
                # Metrics Calculation
                payments = app_state["payments"]
                now_ts = time.time()
                
                # Blocks in last hour
                hour_ago = now_ts - 3600
                blocks_1h = [p for p in payments if p.get("ts", 0) >= hour_ago]
                reward_1h = sum(p.get("amount_grain", 0) for p in blocks_1h) / 10**8
                
                # Today's Profit from payments_by_day
                today_date_str = datetime.now().strftime("%Y-%m-%d")
                payments_by_day = pool_data.get("payments_by_day", [])
                profit_today = next((p.get("amount_prl", 0.0) for p in payments_by_day if p.get("day") == today_date_str), 0.0)
                
                # Server Costs
                active_inst = [i for i in app_state["vast_instances"] if i.get("actual_status") == "running"]
                total_cost_hr = sum(i.get("dph_total", 0.0) for i in active_inst)
                
                # Yield Ratio (Tokens per PH/s based on last 24h payments)
                day_ago = now_ts - 86400
                blocks_24h = [p for p in payments if p.get("ts", 0) >= day_ago]
                reward_24h = sum(p.get("amount_grain", 0) for p in blocks_24h) / 10**8
                hash_ph = hash_raw / 1e15 if hash_raw > 0 else 0
                yield_ph = (reward_24h / 24) / hash_ph if hash_ph > 0 else 0
                
                # Estimate today's block count
                avg_block = (reward_24h / len(blocks_24h)) if blocks_24h else 1.2
                est_blocks = int(round(profit_today / avg_block)) if avg_block > 0 else 0
                
                l_hdr = pad(f"  \033[7m GLOBAL ACCOUNTS \033[0m", 63)
                l_vast = pad(f"  Vast.ai Balance     : {bal_color}${vast_bal:.2f}\033[0m (Spent: \033[91m${vast_spend:.2f}\033[0m){vast_warning}", 63)
                l_pool = pad(f"  Pool Balance        : \033[96m{pool_bal:.4f} PRL\033[0m (Paid: {pool_paid:.4f} PRL)", 63)
                l_mined = pad(f"  Total Mined         : \033[1;92m{total_mined:.4f} PRL\033[0m", 63)
                
                buf.append(l_hdr + f"\033[7m CURRENT PERFORMANCE \033[0m\033[K\r\n")
                buf.append(l_vast + f"| Current Hour Profit : \033[92m+{reward_1h:.2f} PRL\033[0m ({len(blocks_1h)} blk)\033[K\r\n")
                buf.append(l_pool + f"| Today's Profit      : \033[1;93m+{profit_today:.2f} PRL\033[0m (≈ {est_blocks} blk)\033[K\r\n")
                buf.append(l_mined + f"| Server Cost         : \033[91m-${total_cost_hr:.2f}/hr\033[0m\033[K\r\n")
                buf.append(f"\033[K\r\n")
                
                hash_1h = pool_data.get("estHash1h", "0 TH/s")
                
                buf.append(f"  \033[7m MINING METRICS \033[0m\033[K\r\n")
                buf.append(f"  Pool Hashrate (1h)  : \033[1m{hash_1h:<15}\033[0m | Yield Ratio : \033[92m{yield_ph:.2f} tokens / PH/s\033[0m (Avg Reward: {avg_block:.2f} PRL/block)\033[K\r\n")
                
            if app_state["error"]:
                buf.append(f"  \033[91m[Error] {app_state['error']}\033[0m\033[K\r\n")
                
            buf.append(f"\033[90m{'-'*100}\033[0m\033[K\r\n")
            
            # --- PLOTEXT CHARTS (Full Width) ---
            import plotext as plt
            from collections import defaultdict
            
            today_msk = datetime.now(MSK).date()
            all_blocks = app_state["payments"]
            
            daily_blocks = defaultdict(list)
            for p in all_blocks:
                d = get_msk_date(p.get("ts", 0))
                daily_blocks[d].append(p)
                
            last_7_days = [today_msk - timedelta(days=i) for i in range(6, -1, -1)]
            x_labels = []
            y_vals = []
            
            for d in last_7_days:
                blks = daily_blocks.get(d, [])
                total = len(blks)
                x_labels.append(d.strftime('%m-%d'))
                y_vals.append(total)
                
            try:
                plt.clear_data()
                plt.clear_figure()
                plt.theme("clear")
                plt.plotsize(100, 15)
                
                if x_labels:
                    plt.bar(x_labels, y_vals, color="yellow", marker="fhd")
                    plt.title("DAILY BLOCKS (MSK)")
                    plt.ylabel("Blocks")
                    
                    max_y = max(y_vals) if y_vals else 1
                    offset = max(1.0, max_y * 0.08)
                    
                    for i, (d, total) in enumerate(zip(last_7_days, y_vals)):
                        if total > 0:
                            if d == today_msk:
                                now_msk = datetime.now(MSK)
                                hrs = max(1.0, now_msk.hour + now_msk.minute/60.0)
                                bph = total / hrs
                            else:
                                bph = total / 24.0
                                
                            plt.text(str(total), i + 1, total + offset, color="white", alignment="center")
                            if total >= max_y * 0.15 and total >= 2:
                                plt.text(f"{bph:.1f}/h", i + 1, total / 2, color="white", background="yellow", alignment="center")
                
                chart_str = plt.build()
                for line in chart_str.split("\n"):
                    buf.append(f"  {line}\033[K\r\n")
            except Exception as e:
                buf.append(f"  [Chart Error]: {e}\033[K\r\n")
                
            buf.append(f"\033[90m{'-'*100}\033[0m\033[K\r\n")
            
            # --- HISTORY & INSTANCES TABLES ---
            l_hdr = pad(f"  \033[7m TODAY REWARDS (Last 25) \033[0m", 53)
            buf.append(f"{l_hdr}\033[7m ACTIVE INSTANCES \033[0m\033[K\r\n")
            
            history_lines = []
            today_blocks = [p for p in all_blocks if get_msk_date(p.get("ts", 0)) == today_msk]
            for p in today_blocks[:25]:
                dt = datetime.fromtimestamp(p.get("ts", 0), MSK).strftime("%H:%M")
                amt = p.get("amount_grain", 0) / 10**8
                st = p.get("status", "pending")
                color = "\033[92m" if st == "paid" else "\033[93m"
                history_lines.append(f"{dt} | {color}{amt:8.4f} PRL ({st:7})\033[0m")
                
            active_inst = [i for i in app_state.get("vast_instances", []) if i.get("actual_status") == "running"]
            active_inst.sort(key=lambda x: x.get("dph_total", 0), reverse=True)
            
            inst_lines = []
            for i in active_inst:
                iid = i.get("id", "?")
                dph = i.get("dph_total", 0.0)
                inst_lines.append(f"{iid:<10} | ${dph:.3f}/hr")
                
            max_lines = max(len(history_lines), len(inst_lines), 1)
            for i in range(max_lines):
                left = history_lines[i] if i < len(history_lines) else ""
                right = inst_lines[i] if i < len(inst_lines) else ""
                left_padded = pad(f"  {left}", 53)
                buf.append(f"{left_padded}  {right}\033[K\r\n")
                
            buf.append(f"\033[90m{'-'*100}\033[0m\033[K\r\n")
            buf.append(f"\033[K\r\n  [TUI] \033[1m'r'\033[0m: reload. \033[1m'q'\033[0m: quit.\033[K\r\n")
            buf.append("\033[J") # Clear remaining lines
            
            sys.stdout.write("".join(buf))
            sys.stdout.flush()
            
            # Wait for input
            r, _, _ = select.select([sys.stdin], [], [], 0.5)
            if r:
                ch = os.read(fd, 1).decode('utf-8', 'replace')
                if ch.lower() == 'r':
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                    sys.stdout.write("\033[?1049l\033[?25h")
                    sys.stdout.flush()
                    os.execv(sys.executable, [sys.executable] + sys.argv)
                elif ch.lower() == 'q' or ch == '\x03':
                    break
                    
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        sys.stdout.write("\033[?1049l\033[?25h")
        sys.stdout.flush()

if __name__ == "__main__":
    main()
