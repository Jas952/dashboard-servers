import sys
import tty
import termios
import select
import os
import time
import json
import threading
import subprocess
import re
import shutil
from datetime import datetime

# ================= CONFIGURATION =================
WALLET = "YOUR_WALLET_ADDRESS"
IMAGE = "nvidia/cuda:12.4.1-base-ubuntu22.04"
SSH_KEY = os.path.expanduser("~/.ssh/vast_key")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)

BLACKLIST_FILE = os.path.join(ROOT_DIR, "data", "blacklist.json")
STATE_FILE = os.path.join(ROOT_DIR, "data", "autobuy_state.json")
GEO_SKIP = ["RU", "UA", "Ukraine", "BY", "CN", "JP", "BR", "HR", "TH", "IS", "AR", "TR", "MK", "HK", "CY"]
TG_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
TG_CHAT = "YOUR_TELEGRAM_CHAT_ID"

# ================= STATE =================
_system_logs = []
_error_logs = []
_rented_servers = {}
_selected_idx = 0
_prov_scroll = 0
_watchdog_scroll = 0  # Scroll for MINING servers table

_filters = {
    "BOT STATUS": {"val": False, "type": "toggle"},
    "RENT_LIMIT": {"val": 10, "type": "slider", "min": 0, "max": 100, "step": 1},
    # Synced with fleet-live.py (from screenshot)
    "MIN_REL": {"val": 67, "on": True, "type": "slider", "min": 50, "max": 100, "step": 1},  # 67% as shown
    "MAX_N": {"val": 4, "on": True, "type": "slider", "min": 1, "max": 16, "step": 1},
    "VERIFIED": {"on": True, "type": "toggle"},
    "MIN_HOST_MONTHS": {"val": 0, "on": False, "type": "slider", "min": 0, "max": 24, "step": 1},
    # Prices from fleet-live.py screenshot
    "RTX_3090": {"val": 0.35, "num": 4, "on": True, "type": "gpu", "min": 0.05, "max": 2.00, "step": 0.01},
    "RTX_4080": {"val": 0.30, "num": 4, "on": True, "type": "gpu", "min": 0.10, "max": 2.00, "step": 0.01},
    "RTX_4090": {"val": 0.50, "num": 4, "on": True, "type": "gpu", "min": 0.10, "max": 3.00, "step": 0.01},
    "RTX_5080": {"val": 0.40, "num": 4, "on": True, "type": "gpu", "min": 0.10, "max": 3.00, "step": 0.01},
    "RTX_5090": {"val": 0.90, "num": 4, "on": True, "type": "gpu", "min": 0.20, "max": 5.00, "step": 0.01},
}

_processed_offers = set()
_lock = threading.RLock()
_running = True
_rented_count = 0
_rented_mids = set()  # Track machine IDs already rented (sync with fleet-live.py)

# Hashrate expectations (from fleet-live.py)
EXPECTED = {"RTX 5090":310,"RTX 5080":160,"RTX 4090":145,"RTX 4080":120,
            "RTX 3090":100,"RTX 3080 Ti":75,"RTX 3080":65}
MIN_HR   = {"RTX 5090":180,"RTX 5080":90,"RTX 4090":80,"RTX 4080":65,
            "RTX 3090":50,"RTX 3080 Ti":40,"RTX 3080":35}

# ================= TERMINAL COLORS =================
C_GREEN = "\033[32m"  # Dimmer green
C_RED = "\033[31m"    # Dimmer red
C_BORDER = "\033[90m" # Dark grey
C_RESET = "\033[0m"
C_SEL = "\033[48;5;236m" # Dark grey background for selection

# ================= UTILS =================
_tg_last_sent = 0

def _tg(method, params):
    global _tg_last_sent
    now = time.time()
    if now - _tg_last_sent < 1:
        time.sleep(1)
    _tg_last_sent = now
    try:
        import urllib.request, urllib.parse, ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        url = f"https://api.telegram.org/bot{TG_TOKEN}/{method}"
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        # print(f"  [TG] {e}")
        return None

def tg_send(text, chat=None, kb=None):
    p = {"chat_id":chat or TG_CHAT,"text":text,"parse_mode":"HTML"}
    if kb: p["reply_markup"] = kb
    return _tg("sendMessage", p)

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    with _lock:
        line = f"[{ts}] {msg}"
        _system_logs.append(line)
        if len(_system_logs) > 50: _system_logs.pop(0)
    draw_ui()

def global_log(bot_name, tag, msg, iid, mid, gpu, geo, dph):
    try:
        entry = {
            "time": datetime.now().strftime("%m-%d %H:%M:%S"),
            "bot": bot_name,
            "tag": tag,
            "msg": msg,
            "iid": str(iid),
            "mid": str(mid),
            "gpu": str(gpu),
            "geo": str(geo),
            "dph": float(dph)
        }
        logs_path = os.path.join(ROOT_DIR, "data", "global_logs.jsonl")
        with open(logs_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except: pass

def log_err(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    with _lock:
        line = f"[{ts}] {msg}"
        _error_logs.append(line)
        if len(_error_logs) > 5: _error_logs.pop(0)
    draw_ui()

def save_state():
    with _lock:
        state = {
            "filters": _filters,
            "rented_servers": _rented_servers,
            "rented_count": _rented_count,
            "processed_offers": list(_processed_offers)
        }
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except: pass

def load_state():
    global _filters, _rented_servers, _rented_count, _processed_offers
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
            if "filters" in state:
                for k, v in state["filters"].items():
                    if k in _filters and isinstance(v, dict):
                        for key in ("val", "on", "num"):
                            if key in v:
                                _filters[k][key] = v[key]
            if "rented_servers" in state:
                _rented_servers = state["rented_servers"]
            if "rented_count" in state:
                _rented_count = state["rented_count"]
            if "processed_offers" in state:
                _processed_offers = set(state["processed_offers"])
    except: pass

def draw_ui():
    with _lock:
        cols, rows = shutil.get_terminal_size((120, 40))
        W = max(80, cols - 2)
        SEP = f"{C_BORDER}{'─' * W}{C_RESET}"
        now_str = datetime.now().strftime("%H:%M:%S")

        sys.stdout.write("\033[H")
        out = []

        # ── Header ────────────────────────────────────────────────
        bot_running = _filters["BOT STATUS"]["val"]
        bot_col = C_GREEN if bot_running else C_RED
        bot_st  = "RUNNING" if bot_running else "STOPPED"
        # Count only ACTIVE servers for display
        active_servers = sum(1 for d in _rented_servers.values() if d["status"] not in ("DEAD", "FAIL"))
        rented_str = f"  active: {active_servers}/{_filters['RENT_LIMIT']['val']}"
        title = f"PEARL AUTO-RENT BOT  [{bot_col}{bot_st}{C_RESET}]{rented_str}"
        out.append(f"  {title}  \033[90m{now_str}\033[0m\033[K")
        out.append(SEP + "\033[K")

        # ── Filters ───────────────────────────────────────────────
        keys = list(_filters.keys())
        for i, k in enumerate(keys):
            f = _filters[k]
            bg = C_SEL if i == _selected_idx else ""
            is_on = f.get("on", True)

            if k == "BOT STATUS":
                st = "RUNNING" if f["val"] else "STOPPED"
                c = C_GREEN if f["val"] else C_RED
                pad_l = (17 - len(st)) // 2
                pad_r = 17 - len(st) - pad_l
                mid = f"{' '*pad_l}{c}{st}{C_RESET}{bg}{' '*pad_r}"
                row = f"  {bg}[ENTER] {k:<15} ◀ {mid} ▶"
            elif k == "VERIFIED":
                st = "YES" if f["on"] else "NO "
                c = C_GREEN if f["on"] else C_RED
                pad_l = (17 - len(st)) // 2
                pad_r = 17 - len(st) - pad_l
                mid = f"{' '*pad_l}{c}{st}{C_RESET}{bg}{' '*pad_r}"
                row = f"  {bg}[SPACE] {k:<15} ◀ {mid} ▶"
            elif f["type"] == "slider":
                if not is_on:
                    mid = f"       {C_RED}OFF{C_RESET}{bg}       "
                else:
                    if "REL" in k:    val_str = f"{f['val']}%"
                    elif "LIMIT" in k: val_str = f"{f['val']} servers" if f['val'] > 0 else "OFF"
                    elif "MAX_N" in k:       val_str = f"Max {f['val']} GPUs"
                    elif "HOST" in k:        val_str = f">= {f['val']} months" if f['val'] > 0 else "Any host age"
                    else:                    val_str = str(f['val'])
                    mid = f"{val_str:^17}"
                row = f"  {bg}[◀▶]   {k:<15} ◀ {mid} ▶  [SPACE on/off]"
            elif f["type"] == "gpu":
                is_gpu_on = f.get("on", True)
                if not is_gpu_on:
                    mid = f"       {C_RED}OFF{C_RESET}{bg}       "
                    gpu_c = C_RED
                else:
                    val_str = f"${f['val']:.2f}/hr"
                    mid = f"{val_str:^17}"
                    gpu_c = "\033[36m"
                if f["num"] == 0:
                    num_val = "ANY"
                    num_pad = f"   {C_GREEN}{num_val}{C_RESET}{bg}   "
                else:
                    n_str = f"max {f['num']}x"
                    num_pad = f"{n_str:^9}"
                row = f"  {bg}{gpu_c}{k:<8}{C_RESET}{bg}[◀▶] ◀ {mid} ▶  [- {num_pad} +]  [SPACE on/off]"

            if i == _selected_idx:
                row += C_RESET
            out.append(row + "\033[K")

        out.append(SEP + "\033[K")

        # ── System logs (last 5, always shown) ────────────────────
        out.append(f"  \033[1mLOGS\033[0m\033[K")
        show_sys = _system_logs[-5:]
        while len(show_sys) < 5:
            show_sys.append("")
        for l in show_sys:
            out.append(f"  {l}\033[K")

        # ── Errors (collapsible — only shown when non-empty) ───────
        if _error_logs:
            out.append(SEP + "\033[K")
            out.append(f"  \033[1m\033[31mERRORS\033[0m\033[K")
            for e in _error_logs[-5:]:
                out.append(f"  {C_RED}{e}{C_RESET}\033[K")

        # ── Split servers by status (hide DEAD from display) ─────────────────────────────
        mining_items = [(cid, d) for cid, d in _rented_servers.items() if d["status"] == "MINING"]
        prov_items = [(cid, d) for cid, d in _rented_servers.items() if d["status"] not in ("MINING", "DEAD")]
        
        # ── MINING SERVERS table (WATCHDOG) ──────────────────────
        out.append(SEP + "\033[K")
        total_mining = len(mining_items)
        mining_title = f"  \033[1mMINING SERVERS (WATCHDOG)\033[0m  ({total_mining} active)"
        out.append(mining_title + "\033[K")
        
        max_vis_m = 0  # Initialize to avoid UnboundLocalError
        if not mining_items:
            out.append("  No active miners.\033[K")
        else:
            out.append(f"  {C_BORDER}{'ID':<12} {'GPU':<15} {'STATUS':<12} {'POOL/HASHRATE'}{C_RESET}\033[K")
            reserved = 25 + (len(_error_logs) + 2 if _error_logs else 0)
            max_vis_m = max(3, (rows - reserved) // 2)
            max_scroll_m = max(0, total_mining - max_vis_m)
            actual_scroll_m = min(max_scroll_m, max(0, _watchdog_scroll))
            start_m = max(0, total_mining - max_vis_m - actual_scroll_m)
            end_m = start_m + max_vis_m
            
            if start_m > 0:
                out.append(f"  \033[90m↑ {start_m} more above  [W]\033[0m\033[K")
            for cid, data in mining_items[start_m:end_m]:
                out.append(f"  {cid:<12} {data['gpu']:<15} {C_GREEN}{data['status']:<12}{C_RESET} {data['msg'][:40]}\033[K")
            remaining_m = total_mining - end_m
            if remaining_m > 0:
                out.append(f"  \033[90m↓ {remaining_m} more below  [S]\033[0m\033[K")

        # ── PROVISIONING & STARTUP table ───────────────────────────
        out.append(SEP + "\033[K")
        total_prov = len(prov_items)
        prov_title = f"  \033[1mPROVISIONING & STARTUP\033[0m  ({total_prov} starting)"
        out.append(prov_title + "\033[K")

        if not prov_items:
            out.append("  No servers in startup.\033[K")
        else:
            out.append(f"  {C_BORDER}{'ID':<12} {'GPU':<15} {'STATUS':<15} {'MESSAGE'}{C_RESET}\033[K")
            reserved = 25 + (len(_error_logs) + 2 if _error_logs else 0) + max_vis_m
            max_vis = max(3, rows - reserved)
            max_scroll = max(0, total_prov - max_vis)
            actual_scroll = min(max_scroll, max(0, _prov_scroll))
            start_idx = max(0, total_prov - max_vis - actual_scroll)
            end_idx = start_idx + max_vis

            if start_idx > 0:
                out.append(f"  \033[90m↑ {start_idx} more above  [w]\033[0m\033[K")
            for cid, data in prov_items[start_idx:end_idx]:
                st_color = C_RED if "FAIL" in data["status"] else ("\033[33m" if data["status"] in ("WAITING", "STARTING", "INSTALLING") else "\033[90m")
                out.append(f"  {cid:<12} {data['gpu']:<15} {st_color}{data['status']:<15}{C_RESET} {data['msg'][:40]}\033[K")
            remaining = total_prov - end_idx
            if remaining > 0:
                out.append(f"  \033[90m↓ {remaining} more below  [s]\033[0m\033[K")

        # ── Clear button ──────────────────────────────────────────
        out.append(SEP + "\033[K")
        bg_clear = "\033[48;5;236m" if _selected_idx == len(keys) else ""
        out.append(f"  {bg_clear}[ CLEAR TABLE  (ENTER) ]{C_RESET}\033[K")

        # ── Hotkey bar ────────────────────────────────────────────
        out.append(SEP + "\033[K")
        out.append(f"  \033[90m[↑↓] navigate  [◀▶] adjust  [ENTER] toggle/clear  [SPACE] on/off  [-/+] GPU count  [w/s] prov scroll  [W/S] mining scroll  [r] reload  [q] quit\033[0m\033[K")

        sys.stdout.write("\r\n".join(out) + "\033[J\r\n")
        sys.stdout.flush()

def load_blacklist():
    if os.path.exists(BLACKLIST_FILE):
        try:
            with open(BLACKLIST_FILE, "r") as f:
                return json.load(f)
        except: pass
    return {}

def vast_search(query, order=None):
    try:
        cmd = ["vastai", "search", "offers", query, "-n", "-d", "--limit", "1000", "--raw"]
        if order:
            cmd.extend(["-o", order])
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return json.loads(res.stdout) if res.stdout else []
    except subprocess.TimeoutExpired:
        log("⚠️ Vast API timeout (30s). Retrying...")
        return []
    except Exception as e:
        log_err(f"Search error: {e}")
        return []

def vast_create(oid):
    try:
        cmd = ["vastai", "create", "instance", str(oid), "--image", IMAGE, "--ssh", "--direct"]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        m = re.search(r"'new_contract':\s*(\d+)", res.stdout)
        return int(m.group(1)) if m else None
    except Exception as e:
        log_err(f"Create error for offer {oid}: {e}")
        return None

def vast_instances():
    try:
        cmd = ["vastai", "show", "instances-v1", "--raw"]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        data = json.loads(res.stdout) if res.stdout else []
        return data.get("instances", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
    except Exception:
        return []

# ================= MINER INSTALLER =================
def setup_miner_thread(cid, gpu, n, price_dph, geo="Unknown"):
    def set_st(status, msg):
        with _lock:
            if cid in _rented_servers:
                _rented_servers[cid]["status"] = status
                _rented_servers[cid]["msg"] = msg
        save_state()
        draw_ui()

    def fail_and_decrement(msg):
        set_st("FAIL", msg)
        # NOTE: We do NOT decrement _rented_count here
        # We count only SUCCESSFUL rents (when we actually got the server)
        # Failed servers after rent don't affect the limit
        save_state()

    set_st("WAITING", "Waiting for 'running' state...")
    log(f"⏳ [Server {cid}] Setup thread started. Waiting for 'running' state...")
    tg_send(
        f"⏳ <b>TRYING TO CATCH SERVER</b>\n\n"
        f"Server: <code>{cid}</code>\n"
        f"GPU: <b>{n}x {gpu}</b>\n"
        f"Region: <code>{geo}</code>\n"
        f"Status: Waiting for instance to become running..."
    )
    
    ssh_host = None
    ssh_port = None
    
    for attempt in range(90): # 30 minutes
        time.sleep(20)
        instances = vast_instances()
        if not instances: continue
        
        found = False
        for inst in instances:
            if inst["id"] == cid:
                found = True
                if inst.get("actual_status") == "running":
                    ssh_host = inst.get("ssh_host")
                    ssh_port = inst.get("ssh_port")
                    geo = inst.get("geolocation", "Unknown")
                break
                
        if ssh_host:
            break
        if not found:
            fail_and_decrement("Disappeared (auto-killed)")
            log(f"❌ [Server {cid}] Disappeared (probably auto-killed by watchdog).")
            tg_send(f"💀 <b>SERVER DISAPPEARED</b>\n\nServer: <code>{cid}</code>\nGPU: <b>{n}x {gpu}</b>\nReason: Instance removed from account\nAction: Removed from tracking")
            return

    if not ssh_host:
        fail_and_decrement("Stuck in loading")
        log(f"❌ [Server {cid}] Did not start in time. You may need to destroy it.")
        tg_send(f"🔴 <b>SERVER FAILED TO START</b>\n\nServer: <code>{cid}</code>\nGPU: <b>{n}x {gpu}</b>\nRegion: <code>{geo}</code>\nIssue: Stuck in loading for 30min\nAction: Check in Vast UI")
        return
        
    set_st("INSTALLING", "Connecting via SSH...")
    log(f"⏳ [Server {cid}] is RUNNING. Connecting via SSH to start alpha-miner...")
    
    pool = "eu1" if any(x in geo for x in ["EU", "FI", "ES", "DK", "DE", "FR", "NL", "PL", "IT", "CZ", "AT", "SE", "NO", "CH", "BE", "PT", "RO", "HU", "SK", "HR"]) else "us2"
    host = f"{pool}.alphapool.tech"
    worker = f"vast-{cid}"
    # Static difficulty per GPU class (from alphapool.tech docs)
    _DIFF = {"RTX 5090": 1048576, "RTX 5080": 524288, "RTX 4090": 524288, "RTX 4080": 262144,
             "RTX 3090": 262144, "RTX 3080 Ti": 262144, "RTX 3080": 262144, "RTX 3070": 131072}
    diff = next((v for k, v in _DIFF.items() if k in gpu), 262144)

    miner_cmd = (
        f"pkill -9 alpha-miner 2>/dev/null; fuser -k /dev/nvidia* 2>/dev/null; sleep 1; mkdir -p /var/log && "
        f"echo 'Miner starting (Downloading)...' > /var/log/alpha-miner.log && "
        f"curl -sL --max-time 60 -o /usr/bin/alpha-miner https://pearl.alphapool.tech/downloads/alpha-miner-beta-174 && "
        f"chmod +x /usr/bin/alpha-miner && echo 'Miner starting (Running)...' > /var/log/alpha-miner.log && "
        f"nohup alpha-miner --pool stratum+tcp://{host}:5566 --address {WALLET} --worker {worker} --password 'x;d={diff}' --status-interval 30 >> /var/log/alpha-miner.log 2>&1 &"
    )
    ssh_cmd = ["ssh", "-i", SSH_KEY, "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10", "-p", str(ssh_port), f"root@{ssh_host}", miner_cmd]
    
    success = False
    for ssh_attempt in range(15):
        try:
            res = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=120)
            if res.returncode == 0:
                success = True
                break
            elif "BAD_BINARY" in res.stdout or "BAD_BINARY" in res.stderr:
                log(f"⚠️ [Server {cid}] Bad binary downloaded, retrying ({ssh_attempt+1}/15)...")
        except subprocess.TimeoutExpired:
            log(f"⚠️ [Server {cid}] SSH timeout on attempt {ssh_attempt+1}, retrying...")
        time.sleep(10)
        
    if success:
        set_st("MINING", "Miner successfully started")
        log(f"✅ [Server {cid}] MINER STARTED! ({n}x {gpu} at ${price_dph:.3f}/hr)")
        global_log("AUTOBUY", "MINING", f"Miner successfully started", cid, "?", f"{n}x {gpu}", geo, price_dph)
        
        # Telegram notification - MINER STARTED
        tg_send(
            f"✅ <b>MINER STARTED SUCCESSFULLY</b>\n\n"
            f"Server: <code>{cid}</code>\n"
            f"GPU: <b>{n}x {gpu}</b>\n"
            f"Region: <code>{geo}</code>\n"
            f"Pool: <code>{host}</code>\n"
            f"Price: ${price_dph:.3f}/hr\n"
            f"Status: <b>MINING</b>"
        )
    else:
        fail_and_decrement("SSH connection failed")
        log(f"❌ [Server {cid}] Failed SSH. Watchdog will auto-kill it.")
        global_log("AUTOBUY", "FAILED", f"SSH connection failed", cid, "?", f"{n}x {gpu}", geo, price_dph)
        
        # Telegram notification - MINER FAILED
        tg_send(
            f"❌ <b>MINER SETUP FAILED</b>\n\n"
            f"Server: <code>{cid}</code>\n"
            f"GPU: <b>{n}x {gpu}</b>\n"
            f"Region: <code>{geo}</code>\n"
            f"Reason: SSH connection failed after 15 attempts\n"
            f"Action: Server marked for cleanup"
        )

# ================= AUTO-RENT BOT LOOP =================
def bot_thread_func():
    global _processed_offers, _rented_count
    _sync_counter = 0
    while _running:
        bot_status = _filters["BOT STATUS"]["val"]
        if not bot_status:
            time.sleep(15)
            save_state()
            continue

        # Sync server list every 5 cycles - only to clean up dead entries
        # NOTE: We do NOT adjust _rented_count here - it only counts successful rents
        _sync_counter += 1
        if _sync_counter % 5 == 1:
            try:
                log("🔄 Syncing instance list...")
                live_ids = {str(i.get("id")) for i in vast_instances()}
                with _lock:
                    # Clean up dead servers from tracking only, not from count
                    dead = [cid for cid in list(_rented_servers.keys()) if str(cid) not in live_ids]
                    for cid in dead:
                        if cid in _rented_servers:
                            _rented_servers[cid]["status"] = "DEAD"
                            log(f"🗑️ Server {cid} dead, but still counted in rent limit")
            except Exception: pass

        # _processed_offers только для успешно арендованных — сбрасываем каждый цикл
        # чтобы новые офферы не блокировались навсегда
        with _lock:
            _processed_offers.clear()
            # Count only ACTIVE servers (MINING or STARTING), not DEAD ones
            active_count = sum(1 for d in _rented_servers.values() if d["status"] not in ("DEAD", "FAIL"))
        
        # Check against limit using ACTIVE count, not historical rented count
        rent_limit = _filters["RENT_LIMIT"]["val"]
        if rent_limit > 0 and active_count >= rent_limit:
            log(f"🛑 RENT LIMIT REACHED ({active_count}/{rent_limit} active servers). Auto-stopping bot.")
            _filters["BOT STATUS"]["val"] = False
            draw_ui()
            save_state()
            continue
            
        bl_raw = load_blacklist()
        bl = set()
        for k, v in bl_raw.items():
            bl.add(str(k))
            if isinstance(v, dict) and "machine_id" in v:
                bl.add(str(v["machine_id"]))
        
        # Merge with _rented_mids to hide already rented servers (like fleet-live.py)
        bl.update(_rented_mids)
        
        min_rel = _filters["MIN_REL"]["val"] / 100.0
        
        # Get active target GPUs
        targets = []
        for k in _filters:
            if k.startswith("RTX_") and _filters[k].get("on", True):
                gpu_name = k.replace("_", " ")
                max_price = _filters[k]["val"]
                max_n = _filters[k]["num"]
                if max_n == 0: max_n = 100 # 0 means ANY (no limit)
                targets.append((gpu_name, max_price, max_n))

        log(f"🔍 Scanning market... (VERIFIED={'ON' if _filters['VERIFIED'].get('on',True) else 'OFF'}, targets={len(targets)})")

        # One combined query for all GPU types — avoids N round-trips and race conditions
        gpu_names_q = ",".join(k for k in _filters if k.startswith("RTX_") and _filters[k].get("on", True))
        q_parts = [
            f"gpu_name in [{gpu_names_q}]",
            "rentable=true"
        ]
        if _filters["MIN_REL"].get("on"): q_parts.append(f"reliability>={min_rel}")
        if _filters["VERIFIED"].get("on", True): q_parts.append("verified=true")
        if _filters["MAX_N"].get("on"): q_parts.append(f"num_gpus<={_filters['MAX_N']['val']}")
        min_host_months = _filters["MIN_HOST_MONTHS"]["val"] if _filters["MIN_HOST_MONTHS"].get("on") else 0
        q = " ".join(q_parts)
        all_offers = vast_search(q, order="dph")
            
        if not _filters["BOT STATUS"]["val"]: 
            continue
            
        found_count = 0
        rent_limit = _filters["RENT_LIMIT"]["val"]
        
        for o in all_offers:
            if not _filters["BOT STATUS"]["val"]: break
            if rent_limit > 0 and active_count >= rent_limit: break
            
            oid = o.get("id")
            if not oid or oid in _processed_offers:
                continue
                
            mid = str(o.get("machine_id"))
            if mid in bl:
                continue
                
            geo = o.get("geolocation") or "Unknown"

            # Skip hosts that haven't been running long enough
            if min_host_months > 0:
                host_secs = o.get("host_run_time", 0) or 0
                host_months = host_secs / (30 * 24 * 3600)
                if host_months < min_host_months:
                    continue

            # Skip blacklisted geographies (exact token match)
            geo_tokens = {p.strip().upper() for p in re.split(r'[\s,/\\\-]+', geo) if p.strip()}
            if any(code.upper() in geo_tokens for code in GEO_SKIP):
                continue

            gpu = o.get("gpu_name", "")
            n = o.get("num_gpus", 1)
            dph = o.get("dph_total", 999)
            price_per_gpu = dph / n
            
            # Find matching target
            max_p = None
            max_n_limit = 100
            for t_gpu, t_max_p, t_max_n in targets:
                if t_gpu == gpu:
                    max_p = t_max_p
                    max_n_limit = t_max_n
                    break
            
            if max_p is None: continue
            
            if n > max_n_limit:
                continue

            if price_per_gpu <= max_p:
                found_count += 1
                log(f"⚡ MATCH: Offer {oid} | {n}x {gpu} | ${price_per_gpu:.3f}/hr per GPU | {geo}")
                
                cid = vast_create(oid)
                if cid:
                    _processed_offers.add(oid)  # только успешно арендованные
                    _rented_count += 1
                    _rented_mids.add(str(oid))  # Track to hide from future scans
                    mid = str(o.get("machine_id", oid))
                    if mid != str(oid):
                        _rented_mids.add(mid)  # Also track machine_id
                    with _lock:
                        _rented_servers[cid] = {"gpu": f"{n}x {gpu}", "status": "STARTING", "msg": "Provisioning host..."}
                    save_state()
                    log(f"🎉 SUCCESS! Rented server {cid} (Offer {oid}). [{_rented_count}/{_filters['RENT_LIMIT']['val']}]")
                    global_log("AUTOBUY", "RENTED", f"Successfully matched and rented server", cid, oid, f"{n}x {gpu}", geo, dph)
                    
                    # Telegram notification - RENT SUCCESS
                    tg_send(
                        f"🎯 <b>SERVER RENTED SUCCESSFULLY</b>\n\n"
                        f"GPU: <b>{n}x {gpu}</b>\n"
                        f"Contract ID: <code>{cid}</code>\n"
                        f"Offer ID: <code>{oid}</code>\n"
                        f"Price: ${dph:.3f}/hr (${dph/n:.3f}/hr per GPU)\n"
                        f"Region: <code>{geo}</code>\n"
                        f"Total rented: {_rented_count}/{_filters['RENT_LIMIT']['val']}"
                    )
                    
                    threading.Thread(target=setup_miner_thread, args=(cid, gpu, n, dph, geo), daemon=True).start()
                else:
                    log(f"❌ Failed to rent offer {oid}. Taken by someone else.")
                    # Telegram notification - RENT FAILED
                    tg_send(
                        f"⚠️ <b>RENT FAILED</b>\n\n"
                        f"GPU: <b>{n}x {gpu}</b>\n"
                        f"Offer ID: <code>{oid}</code>\n"
                        f"Price: ${dph:.3f}/hr\n"
                        f"Region: <code>{geo}</code>\n"
                        f"Reason: Server was taken by another user"
                    )

        if found_count == 0 and _filters["BOT STATUS"]["val"]:
            log("💤 No matches. Next scan...")
        # No sleep — vast_search itself takes ~2s, that's enough

# ================= KEYBOARD INPUT =================
def read_keys():
    global _selected_idx, _running, _rented_count, _prov_scroll, _watchdog_scroll
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        while _running:
            dr, dw, de = select.select([sys.stdin], [], [], 0.1)
            if dr:
                ch = sys.stdin.read(1)
                if ch == 'q' or ch == '\x03':
                    _running = False
                    break
                    
                keys = list(_filters.keys())
                f = _filters[keys[_selected_idx]] if _selected_idx < len(keys) else None
                
                if ch in ('\r', '\n'): # ENTER
                    if _selected_idx == len(keys):
                        with _lock:
                            _rented_servers.clear()
                            _processed_offers.clear()
                            _rented_count = 0
                            _prov_scroll = 0
                            save_state()
                            log("🧹 Cleared rented servers list and reset limits.")
                        draw_ui()
                    elif f and f["type"] == "toggle":
                        f["val"] = not f.get("val", False)
                        if f["val"]:
                            log(f"▶️ Auto-renting STARTED.")
                            global_log("AUTOBUY", "STARTED", "Bot auto-renting enabled", "?", "?", "?", "?", 0.0)
                        else:
                            log(f"⏸️ Auto-renting PAUSED.")
                            global_log("AUTOBUY", "PAUSED", "Bot auto-renting disabled", "?", "?", "?", "?", 0.0)
                        _processed_offers.clear()
                        save_state()
                        draw_ui()
                elif ch == ' ':
                    if f:
                        if f["type"] == "slider":
                            f["on"] = not f.get("on", True)
                        elif f["type"] == "gpu":
                            f["on"] = not f.get("on", True)
                        elif f["type"] == "toggle" and keys[_selected_idx] == "VERIFIED":
                            f["on"] = not f.get("on", True)
                        _processed_offers.clear()
                        save_state()
                        draw_ui()
                elif ch.lower() in ('r', 'к'):
                    _running = False
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                    sys.stdout.write("\033[?1049l\033[?25h")
                    sys.stdout.flush()
                    os.execv(sys.executable, [sys.executable] + sys.argv)
                elif ch == 'w':  # lowercase - provisioning scroll up
                    _prov_scroll = max(0, _prov_scroll - 1)
                    draw_ui()
                elif ch == 's':  # lowercase - provisioning scroll down
                    _prov_scroll += 1
                    draw_ui()
                elif ch == 'W':  # uppercase - mining/WATCHDOG scroll up
                    _watchdog_scroll = max(0, _watchdog_scroll - 1)
                    draw_ui()
                elif ch == 'S':  # uppercase - mining/WATCHDOG scroll down
                    _watchdog_scroll += 1
                    draw_ui()
                elif ch.lower() == 'c':
                    with _lock:
                        _rented_servers.clear()
                        _processed_offers.clear()
                        _rented_count = 0
                        _prov_scroll = 0
                        _watchdog_scroll = 0
                        save_state()
                        log("🧹 Cleared rented servers list and reset limits.")
                    draw_ui()
                elif ch == '\x1b':
                    ch2 = sys.stdin.read(1)
                    if ch2 == '[':
                        ch3 = sys.stdin.read(1)
                        if ch3 == 'A': # UP
                            _selected_idx = max(0, _selected_idx - 1)
                            draw_ui()
                        elif ch3 == 'B': # DOWN
                            _selected_idx = min(len(keys), _selected_idx + 1)
                            draw_ui()
                        elif ch3 == 'C': # RIGHT
                            if f and f["type"] in ("slider", "gpu"):
                                f["val"] = min(f["max"], f["val"] + f["step"])
                                if isinstance(f['step'], int): f['val'] = int(f['val'])
                                else: f['val'] = round(f['val'], 2)
                                _processed_offers.clear()
                                save_state()
                                draw_ui()
                        elif ch3 == 'D': # LEFT
                            if f and f["type"] in ("slider", "gpu"):
                                f["val"] = max(f["min"], f["val"] - f["step"])
                                if isinstance(f['step'], int): f['val'] = int(f['val'])
                                else: f['val'] = round(f['val'], 2)
                                _processed_offers.clear()
                                save_state()
                                draw_ui()
                elif ch == '-':
                    if f and f["type"] == "gpu":
                        f["num"] = max(0, f["num"] - 1)
                        _processed_offers.clear()
                        save_state()
                        draw_ui()
                elif ch in ('+', '='):
                    if f and f["type"] == "gpu":
                        f["num"] = min(16, f["num"] + 1)
                        _processed_offers.clear()
                        save_state()
                        draw_ui()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

def main():
    global _running
    load_state()
    sys.stdout.write("\033[?1049h\033[2J\033[H")
    
    st_msg = "RUNNING" if _filters["BOT STATUS"].get("val") else "STOPPED"
    log(f"🤖 TUI Initialized. Bot is currently {st_msg}.")
    tg_send(
        f"🟢 <b>AUTOBUY BOT STARTED</b>\n\n"
        f"Status: <b>{st_msg}</b>\n"
        f"Rent limit: {_filters['RENT_LIMIT']['val']} servers\n"
        f"Started at: {datetime.now().strftime('%H:%M:%S')}"
    )
    log("Controls: UP/DOWN nav | LEFT/RIGHT adjust | SPACE toggle | -/+ max GPUs | r reload | q quit")
    
    t_bot = threading.Thread(target=bot_thread_func, daemon=True)
    t_bot.start()
    
    try:
        read_keys()
    finally:
        _running = False
        sys.stdout.write("\033[?1049l\r\n🛑 Auto-rent bot stopped.\r\n")

if __name__ == "__main__":
    main()
