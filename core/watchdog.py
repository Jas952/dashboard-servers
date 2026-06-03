#!/usr/bin/env python3
"""
Pearl Hashrate Watchdog — real-time hashrate monitor.
Runs continuously, waits for instances if none active.

  python3 watchdog.py          # refresh every 30s
  python3 watchdog.py 10       # refresh every 10s
"""

import subprocess, json, sys, os, re, time, select, threading, shutil
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

try:
    import plotext
except ImportError:
    pass

SSH_KEY = os.path.expanduser("~/.ssh/vast_key")
REFRESH = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 30
WALLET = "YOUR_WALLET_ADDRESS"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)

MIN_HR = {"RTX 5090":217,"RTX 5080":112,"RTX 4090":100,"RTX 4080":84,
          "RTX 3090":70,"RTX 3080 Ti":52,"RTX 3080":45}

_low_hr_timers = {}
_offline_timers = {}  # instance_id -> timestamp when it started underperforming
_ssh_fail_timers = {}
_reinstall_attempts = {}
_last_stats = {}     # instance_id -> {"hits": int, "ts": float}

_ak_filters = {
    "AK_INSTALL": {"val": 60, "min": 0, "max": 120, "step": 5, "desc": "INSTALL timeout (min)"},
    "AK_INSTANT": {"val": 0.0, "min": 0.0, "max": 1.0, "step": 0.05, "desc": "INSTANT Kill $/100T"},
    "AK_OFFLINE": {"val": 15, "min": 0, "max": 60, "step": 1, "desc": "OFFLINE State"},
}

def add_to_blacklist(iid, mid, reason="Manual Kill", details="Unknown"):
    if not mid or mid == "?": return
    bl = {}
    try:
        blacklist_path = os.path.join(ROOT_DIR, "data", "blacklist.json")
        if os.path.exists(blacklist_path):
            with open(blacklist_path, "r") as f:
                data = json.load(f)
                if isinstance(data, list):
                    bl = {str(k): {"reason": "Legacy", "details": "Unknown"} for k in data}
                else:
                    bl = data
    except: pass
    
    bl[str(mid)] = {
        "instance_id": str(iid),
        "reason": reason,
        "details": details,
        "time": datetime.now().strftime("%m-%d %H:%M")
    }
    try:
        blacklist_path = os.path.join(ROOT_DIR, "data", "blacklist.json")
        with open(blacklist_path, "w") as f:
            json.dump(bl, f, indent=2)
    except: pass
    
    try:
        parts = details.split(" | ")
        gpu = parts[0] if len(parts) > 0 else "?"
        dph = float(parts[1].replace("$", "").replace("/hr", "")) if len(parts) > 1 else 0.0
        geo = parts[2] if len(parts) > 2 else "?"
    except:
        gpu = "?"; dph = 0.0; geo = "?"
        
    tag = "KILLED" if "Kill" in reason else "BLACKLISTED"
    global_log("WATCHDOG", tag, f"{reason} (Blacklisted)", iid, mid, gpu, geo, dph)

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

def do_destroy(iid):
    try:
        import urllib.request
        req = urllib.request.Request(f"https://api.digitalocean.com/v2/droplets/{iid}", method="DELETE")
        req.add_header("Authorization", "Bearer YOUR_DO_API_TOKEN")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status in (204, 200, 202)
    except Exception:
        return False

def vast_destroy(iid):
    if str(iid).startswith("574") or len(str(iid)) >= 9:
        return do_destroy(iid)
    try:
        r = subprocess.run(["vastai","destroy","instance",str(iid)],
            input="y\n",capture_output=True,text=True,timeout=15)
        return "destroying" in r.stdout.lower()
    except Exception:
        return False

_do_cache = []
_last_do_fetch = 0.0

def get_do_instances():
    global _do_cache, _last_do_fetch
    if time.time() - _last_do_fetch < 60:
        return _do_cache
    
    try:
        import urllib.request, json, ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        
        do_keys = [
            "YOUR_DO_API_TOKEN", # Старый аккаунт
            "YOUR_DO_API_TOKEN"  # Новый аккаунт
        ]
        
        all_do_insts = []
        for key in do_keys:
            try:
                req = urllib.request.Request("https://api.digitalocean.com/v2/droplets?per_page=100")
                req.add_header("Authorization", f"Bearer {key}")
                with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
                    data = json.loads(resp.read().decode())
                
                for d in data.get("droplets", []):
                    if "gpu" not in d.get("tags", []): continue
                    ip = ""
                    for n in d.get("networks", {}).get("v4", []):
                        if n.get("type") == "public": ip = n.get("ip_address")
                    slug = d.get("size", {}).get("slug", "")
                    parts = slug.split("-")
                    if len(parts) >= 2 and "x" in parts[1]:
                        gpu_info = parts[1].split("x")
                        gpu_name = gpu_info[0].upper()
                        if gpu_name == "H200": gpu_name = "H200 NVIDIA"
                        elif gpu_name == "H100": gpu_name = "H100 NVIDIA"
                        num_gpus = int(gpu_info[1])
                    else:
                        gpu_name = "Unknown"
                        num_gpus = 1
                        
                    dph_raw = d.get("size", {}).get("price_hourly", 0.0)
                    # Применяем реальную стоимость с учетом кредитов ($58 вложено на $200 кредитов = 29% от цены)
                    dph = dph_raw * (58.0 / 200.0)
                    created_at = d.get("created_at")
                    start_date = 0.0
                    if created_at:
                        try: start_date = datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
                        except: pass
                        
                    st = "running" if d.get("status") == "active" else "loading"
                    
                    all_do_insts.append({
                        "id": d["id"],
                        "machine_id": f"do-{d['id']}",
                        "gpu_name": gpu_name,
                        "num_gpus": num_gpus,
                        "ssh_host": ip,
                        "ssh_port": 22,
                        "actual_status": st,
                        "dph_total": dph,
                        "start_date": start_date,
                        "geolocation": d.get("region", {}).get("slug", "?"),
                        "status_msg": "DigitalOcean",
                        "is_do": True
                    })
            except Exception:
                pass
                
        _last_do_fetch = time.time()
        _do_cache = all_do_insts
        return all_do_insts
    except Exception as e:
        return _do_cache

def get_instances():
    try:
        r = subprocess.run(["vastai","show","instances-v1","--raw"],
                           capture_output=True, text=True, timeout=15)
        data = json.loads(r.stdout)
        vast_insts = data.get("instances", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
    except Exception as e:
        vast_insts = []
    
    vast_insts.extend(get_do_instances())
    return vast_insts


def check_hr(inst):
    host = inst.get("ssh_host","")
    port = inst.get("ssh_port","")
    gpu  = inst.get("gpu_name","?")
    n    = inst.get("num_gpus",1)
    st   = inst.get("actual_status","?")
    iid  = inst["id"]
    mid  = inst.get("machine_id", "?")
    dph  = float(inst.get("dph_total", 0.0))
    start_date = float(inst.get("start_date", 0.0))

    r = {"id": iid, "mid": mid, "gpu": f"{n}x {gpu}", "status": st,
         "hr": 0.0, "share_hr": 0.0, "shares": 0, "attempts": 0, "drops": 0, "recent_drops": 0, "recent_subs": 0, "up": 0, "dph": dph,
         "start_date": start_date, "temps": [], "utils": [], "powers": [],
         "status_msg": inst.get("status_msg", "") or "", "last_log": "",
         "miner_dead": False, "is_do": inst.get("is_do", False)}

    if st != "running" or not host or not port:
        return r

    cmd = ["ssh","-i",SSH_KEY,"-o","StrictHostKeyChecking=no",
           "-o","ConnectTimeout=8","-o","BatchMode=yes",
           "-p",str(port),f"root@{host}",
           ("grep -a 'component=miner status' /var/log/alpha-miner.log 2>/dev/null|tail -50;"
            "echo '|||';"
            "grep -a -m 1 '^202' /var/log/alpha-miner.log 2>/dev/null|cut -d. -f1;"
            "echo '|||';"
            "grep -a -c 'share dropped' /var/log/alpha-miner.log 2>/dev/null;"
            "echo '|||';"
            "tail -200 /var/log/alpha-miner.log 2>/dev/null | grep -a -c 'share dropped';"
            "echo '|||';"
            "tail -200 /var/log/alpha-miner.log 2>/dev/null | grep -a -c 'submitted';"
            "echo '|||';"
            "tail -1 /var/log/alpha-miner.log 2>/dev/null;"
            "echo '|||';"
            "nvidia-smi --query-gpu=temperature.gpu,utilization.gpu,power.draw --format=csv,noheader,nounits 2>/dev/null;"
             "echo '|||';"
             "pgrep -x alpha-miner >/dev/null 2>&1 && echo MINER_ALIVE || echo MINER_DEAD; exit 0")]

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except Exception as e:
        r["last_log"] = f"SSH Timeout/Error"
        r["ssh_failed"] = True
        return r

    if res.returncode != 0 and "grep" not in res.stderr:
        err = res.stderr.strip().replace("\n", " ")
        r["last_log"] = err if err else "SSH Connection Failed"
        r["ssh_failed"] = True
        return r

    parts = res.stdout.split("|||")
    
    # Parse hashrate per GPU from the last 50 status lines
    gpu_stats = {} # gpu_id -> {"attempts": int, "shares": int, "hr": float, "share_hr": float}
    matches = re.finditer(r"gpu=(\d+):.*?attempts=(\d+)\s+hits=(\d+)\s+(?:hashrate_th_s|hashrate)=([\d.]+).*?(?:share_equiv_th_s|share_equiv)=([\d.]+)", parts[0])
    for m in matches:
        gpu_id = m.group(1)
        gpu_stats[gpu_id] = {
            "attempts": int(m.group(2)),
            "shares": int(m.group(3)),
            "hr": float(m.group(4)),
            "share_hr": float(m.group(5))
        }
        
    if gpu_stats:
        r["attempts"] = sum(s["attempts"] for s in gpu_stats.values())
        r["shares"]   = sum(s["shares"] for s in gpu_stats.values())
        r["hr"]       = sum(s["hr"] for s in gpu_stats.values())
        r["share_hr"] = sum(s["share_hr"] for s in gpu_stats.values())

    if len(parts) > 1:
        ts = parts[1].strip()
        if ts and "T" in ts:
            try:
                s = datetime.fromisoformat(ts + "+00:00")
                r["up"] = int((datetime.now(timezone.utc) - s).total_seconds() // 60)
            except: pass
            
    if len(parts) > 2:
        try: r["drops"] = int(parts[2].strip())
        except: pass
        
    if len(parts) > 3:
        try: r["recent_drops"] = int(parts[3].strip())
        except: pass
        
    if len(parts) > 4:
        try: r["recent_subs"] = int(parts[4].strip())
        except: pass
        
    if len(parts) > 5 and not r["last_log"]:
        ll = parts[5].strip()
        r["last_log"] = ll if ll else "Miner log empty or missing"

    if len(parts) > 6:
        for smi_line in parts[6].strip().splitlines():
            vals = [v.strip() for v in smi_line.split(',')]
            if len(vals) >= 3:
                try: r["temps"].append(int(float(vals[0])))
                except: pass
                try: r["utils"].append(int(float(vals[1])))
                except: pass
                try: r["powers"].append(float(vals[2]))
                except: pass

    # Check if miner process is alive (part 7 = pgrep result)
    if len(parts) > 7:
        proc_status = parts[7].strip()
        if "MINER_DEAD" in proc_status:
            r["miner_dead"] = True
            # If log has hashrate history but process is dead, the miner crashed
            if r["hr"] > 0:
                # We have stale hashrate from log but process is gone
                r["hr"] = 0.0
                r["share_hr"] = 0.0
            if not r["last_log"] or r["last_log"] == "Miner log empty or missing":
                r["last_log"] = "MINER PROCESS DEAD"

    return r


def get_expected_hr(gpu_name, num):
    m = {"RTX 5090":310,"RTX 5080":160,"RTX 4090":145,"RTX 4080":120,
         "RTX 3090":100,"RTX 3080 Ti":75,"RTX 3080":65,"RTX 3070":45}
    for k, v in m.items():
        if k in gpu_name: return v * num
    return 100 * num

def get_threshold(gpu_name, n):
    return MIN_HR.get(gpu_name, 0) * n

app_state = {
    "cycle": 0,
    "total": 0,
    "prev_prov_count": 0,  # Track prov count for auto-scroll
    "active_results": [],
    "prov_results": [],
    "underperf": [],
    "server_history": {},
    "instances": [],
    "now": ""
}
_state_lock = threading.Lock()

def load_history():
    try:
        history_path = os.path.join(ROOT_DIR, "data", "history.json")
        with open(history_path, "r") as f:
            return {str(k): v for k, v in json.load(f).items()}
    except:
        return {}

def save_history(h):
    try:
        history_path = os.path.join(ROOT_DIR, "data", "history.json")
        with open(history_path, "w") as f:
            json.dump(h, f)
    except: pass

def monitor_thread():
    global _low_hr_timers, _offline_timers, _ssh_fail_timers, app_state
    cycle = 0
    server_history = load_history()
    
    while True:
        cycle += 1
        now = datetime.now().strftime("%H:%M:%S")
        
        try:
            import urllib.request, ssl, json
            req = urllib.request.Request(f"https://pearl.alphapool.tech/api/miner/{WALLET}")
            req.add_header('User-Agent', 'Mozilla/5.0')
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(req, timeout=5, context=ctx) as response:
                pool_raw = json.loads(response.read().decode())
                payments = pool_raw.get("payments", [])
                hash_raw = pool_raw.get("estHash24hRaw", 0)
                
                day_ago = time.time() - 86400
                blocks_24h = [p for p in payments if p.get("ts", 0) >= day_ago]
                
                if blocks_24h:
                    avg_block_reward = sum(p.get("amount_grain", 0) for p in blocks_24h) / 10**8 / len(blocks_24h)
                    hash_ph = hash_raw / 1e15 if hash_raw > 0 else 0
                    if hash_ph > 0:
                        app_state["yield_per_block_per_ph"] = avg_block_reward / hash_ph
        except Exception: pass
        
        instances = get_instances()
        running = [i for i in instances if i.get("actual_status") in ("running", "offline")]
        not_running = [i for i in instances if i.get("actual_status") not in ("running", "offline")]

        if not instances:
            with _state_lock:
                app_state["now"] = now
                app_state["cycle"] = cycle
                app_state["active_results"] = []
                app_state["prov_results"] = []
                app_state["total"] = 0
                app_state["underperf"] = []
                app_state["instances"] = []
            time.sleep(REFRESH)
            continue

        with ThreadPoolExecutor(max_workers=8) as p:
            results = list(p.map(check_hr, running))

        # Restore previous stats for servers that had SSH timeout to prevent UI flickering
        old_results = {str(r["id"]): r for r in app_state.get("active_results", [])}
        for r in results:
            iid = str(r["id"])
            if r.get("ssh_failed") and iid in old_results:
                old = old_results[iid]
                r["hr"] = old.get("hr", 0.0)
                r["share_hr"] = old.get("share_hr", 0.0)
                r["shares"] = old.get("shares", 0)
                r["attempts"] = old.get("attempts", 0)
                r["drops"] = old.get("drops", 0)
                r["recent_drops"] = old.get("recent_drops", 0)
                r["recent_subs"] = old.get("recent_subs", 0)

        results.sort(key=lambda x: x["id"])
        total = sum(r["hr"] for r in results)
        underperf = []
        auto_kill_set = set()  # dedup auto-kill ids to avoid double-destroy calls
        
        # Check for global SSH proxy outage
        ssh_failed_count = sum(1 for r in results if r.get("ssh_failed"))
        global_ssh_outage = (len(results) >= 5 and ssh_failed_count >= max(4, len(results) * 0.6))
        
        # Check for global pool lag (alphapool issues)
        running_count = sum(1 for r in results if r["status"] == "running" and not r.get("ssh_failed"))
        bad_net_count = sum(1 for r in results if r["status"] == "running" and not r.get("ssh_failed") and r.get("recent_drops", 0) >= 3 and (r["recent_drops"] / max(1, r.get("recent_subs", 0))) > 0.15)
        global_pool_lag = (running_count >= 3 and bad_net_count >= running_count * 0.5)
        
        active_ids = {str(r["id"]) for r in results}
        for r in results:
            iid = str(r["id"])
            if iid not in server_history:
                # pad with Nones if this is a new server mid-run
                server_history[iid] = [None] * max(0, min(2880, cycle - 1))
            server_history[iid].append(r["hr"])
            
        # Track global $/100T
        total_dph = sum(r["dph"] for r in results)
        global_c100 = (total_dph / total * 100) if total > 0 else 0
        if "global_c100" not in server_history:
            server_history["global_c100"] = [None] * max(0, min(2880, cycle - 1))
        server_history["global_c100"].append(global_c100)
            
        for iid in list(server_history.keys()):
            if iid not in active_ids and iid != "global_c100":
                del server_history[iid]
        for iid in server_history:
            if len(server_history[iid]) > 2880:
                server_history[iid].pop(0)
                
        save_history(server_history)
        save_history(server_history)
        
        try:
            gh = {}
            gpu_history_path = os.path.join(ROOT_DIR, "data", "gpu_history.json")
            if os.path.exists(gpu_history_path):
                with open(gpu_history_path, "r") as f:
                    gh = json.load(f)
            gh.setdefault("timestamps", []).append(time.time())
            curr_gh = {}
            for r in results:
                if r["hr"] > 0 and r["status"] == "running":
                    g = r["gpu"].split("x ")[-1]
                    curr_gh[g] = curr_gh.get(g, 0.0) + r["hr"]
            gh.setdefault("data", {})
            for g, hr in curr_gh.items():
                gh["data"].setdefault(g, [])
                if len(gh["data"][g]) < len(gh["timestamps"]) - 1:
                    gh["data"][g] = [0.0] * (len(gh["timestamps"]) - 1)
                gh["data"][g].append(hr)
            for g in gh["data"]:
                if len(gh["data"][g]) < len(gh["timestamps"]):
                    gh["data"][g].append(0.0)
            if len(gh["timestamps"]) > 2880:
                gh["timestamps"] = gh["timestamps"][-2880:]
                for g in gh["data"]:
                    gh["data"][g] = gh["data"][g][-2880:]
            with open(gpu_history_path, "w") as f:
                json.dump(gh, f)
        except Exception:
            pass

        active_results = []
        prov_results = []
        
        # Separate servers with empty miner log (no miner installed) into awaiting_miner
        awaiting_miner_results = []
        true_active_results = []
        
        for r in results:
            # If running but no hashrate and log is empty/missing -> awaiting miner installation
            is_awaiting_miner = (
                r["status"] == "running" and 
                r["hr"] == 0 and 
                not r.get("ssh_failed") and
                ("empty" in r.get("last_log", "").lower() or "missing" in r.get("last_log", "").lower())
            )
            if is_awaiting_miner:
                awaiting_miner_results.append(r)
            else:
                true_active_results.append(r)
        
        # Use true_active_results for HASHRATE WATCHDOG
        results = true_active_results
        
        for r in results:
            iid = r["id"]
            n = int(r["gpu"].split("x")[0])
            gpu_name = r["gpu"].split("x ")[1]
            thr = get_threshold(gpu_name, n)
            exp_hr = get_expected_hr(gpu_name, n)
            
            start_date = float(r.get("start_date", 0.0))
            uptime_min = int((time.time() - start_date) / 60) if start_date > 0 else 0
            
            is_offline = (r["status"] == "offline")
            
            if r.get("ssh_failed"):
                is_low = False
                is_bad_net = False
            else:
                _ssh_fail_timers.pop(iid, None)
                is_low = (r["hr"] < thr) and (r["status"] == "running")
                is_bad_net = (r.get("recent_drops", 0) >= 3 and (r["recent_drops"] / max(1, r.get("recent_subs", 0))) > 0.15)
            
            if is_offline:
                if iid not in _offline_timers:
                    _offline_timers[iid] = time.time()
                elapsed_min = int((time.time() - _offline_timers[iid]) / 60)
                icon = "🔴"
                status_text = f"OFFLINE ({elapsed_min}m)"
                r["_is_low_or_bad"] = True
                
                if _ak_filters["AK_OFFLINE"]["val"] > 0 and elapsed_min >= _ak_filters["AK_OFFLINE"]["val"]:
                    status_text = f"AUTO-KILL ({elapsed_min}m)"
                    auto_kill_set.add(iid)
            elif r.get("ssh_failed"):
                _offline_timers.pop(iid, None)
                # Do NOT reset _low_hr_timers on SSH timeout — keep the running timer
                # so flaky SSH proxies cannot mask a chronically low-hashrate server.
                icon = "⚠️"
                r["_is_low_or_bad"] = True
                
                if not global_ssh_outage:
                    if iid not in _ssh_fail_timers:
                        _ssh_fail_timers[iid] = time.time()
                    elapsed_min = int((time.time() - _ssh_fail_timers[iid]) / 60)
                    status_text = f"SSH Failed ({elapsed_min}m)"
                    if _ak_filters["AK_OFFLINE"]["val"] > 0 and elapsed_min >= _ak_filters["AK_OFFLINE"]["val"]:
                        status_text = f"AUTO-KILL ({elapsed_min}m)"
                        auto_kill_set.add(iid)
                else:
                    _ssh_fail_timers.pop(iid, None)
                    status_text = "SSH Outage"
            elif is_low or is_bad_net:
                _offline_timers.pop(iid, None)
                if iid not in _low_hr_timers:
                    _low_hr_timers[iid] = time.time()
                
                # If global pool lag is happening, pause/reset the bad network timer
                if is_bad_net and global_pool_lag:
                    _low_hr_timers[iid] = time.time()
                
                elapsed_min = int((time.time() - _low_hr_timers[iid]) / 60)
                
                # Only Auto-Kill for hashrate during the Optimization phase if AK_INSTALL is used
                is_initial_phase = (uptime_min < 15)
                
                limit = _ak_filters["AK_INSTALL"]["val"] if is_initial_phase else 0
                limit_name = "INSTALL " if is_initial_phase else ""
                
                if is_low:
                    icon = "⚠️"
                    err_msg = "LOW"
                else:
                    icon = "📡" if global_pool_lag else "🌐"
                    err_msg = "POOL LAG" if global_pool_lag else "BAD NET!"
                
                # Visuals
                if is_initial_phase and is_low:
                    icon = "⏳"
                    status_text = f"Optimizing ({uptime_min}m)"
                    r["_is_low_or_bad"] = False
                    r["_is_optimizing"] = True
                else:
                    status_text = f"{err_msg} ({elapsed_min}m)"
                    r["_is_low_or_bad"] = not global_pool_lag
                    
                if not (is_bad_net and global_pool_lag):
                    underperf.append(iid)
                    # We ONLY kill for hashrate if it's the initial install phase
                    if limit > 0 and elapsed_min >= limit:
                        status_text = f"AUTO-KILL {limit_name}({elapsed_min}m)"
                        auto_kill_set.add(iid)
            else:
                _offline_timers.pop(iid, None)
                _low_hr_timers.pop(iid, None)
                if r.get("miner_dead"):
                    # Miner process crashed but server is running
                    if iid not in _low_hr_timers:
                        _low_hr_timers[iid] = time.time()
                    elapsed_min = int((time.time() - _low_hr_timers[iid]) / 60)
                    icon = "💀"
                    status_text = f"MINER DEAD ({elapsed_min}m)"
                    r["_is_low_or_bad"] = True
                    underperf.append(iid)
                else:
                    icon = "✅" if r["hr"] > 0 else "⏳"
                    status_text = "OK" if r["hr"] > 0 else "Starting"
                    r["_is_low_or_bad"] = False
                
            c100 = (r["dph"] / r["hr"]) * 100 if r["hr"] > 0 else 0
            
            # $/100T limit logic
            limit_instant = _ak_filters["AK_INSTANT"]["val"]
            
            if limit_instant > 0 and c100 >= limit_instant and not is_initial_phase:
                status_text = f"AUTO-KILL ($/100T > {limit_instant})"
                auto_kill_set.add(iid)
                r["_is_low_or_bad"] = True
                if iid not in underperf: underperf.append(iid)
                
            r["_icon"] = icon
            r["_status_text"] = status_text
            r["_exp_hr"] = exp_hr
            
            active_results.append(r)
            
        # Add awaiting_miner_results to prov_results (running servers without miner)
        for r in awaiting_miner_results:
            iid = r["id"]
            gpu = r["gpu"]
            n = int(gpu.split("x")[0])
            last_log = r.get("last_log", "Awaiting miner installation")
            
            # Use provisioning timer for tracking
            start_date = float(r.get("start_date", 0.0))
            elapsed_min = int((time.time() - start_date) / 60) if start_date > 0 else 0
            
            if elapsed_min >= 4:
                reinstall_info = _reinstall_attempts.get(iid, {"count": 0, "last_ts": 0})
                if reinstall_info["count"] < 2 and not r.get("is_do"):
                    if time.time() - reinstall_info["last_ts"] >= 180: # 3 minutes cooldown between retries
                        import threading, sys
                        tmp_install_path = os.path.join(ROOT_DIR, "tools", "tmp_install.py")
                        threading.Thread(target=lambda i: subprocess.run([sys.executable, tmp_install_path, str(i)]), args=(iid,), daemon=True).start()
                        reinstall_info["count"] += 1
                        reinstall_info["last_ts"] = time.time()
                        _reinstall_attempts[iid] = reinstall_info
                
                if reinstall_info["count"] >= 2:
                    last_log = f"[Auto-Reinstall Exhausted] " + last_log
                else:
                    last_log = f"[Auto-Reinstall Triggered ({reinstall_info['count']}/2)] " + last_log

            disp_status = f"awaiting miner ({elapsed_min}m)"
            icon = "⏳"
            
            prov_results.append({
                "id": iid, "gpu": gpu, "status": disp_status, "last_log": last_log,
                "_icon": icon,
                "_status_text": disp_status,
                "_is_low_or_bad": False,  # Not bad, just waiting
                "is_do": r.get("is_do", False)
            })
            
        for inst in not_running:
            iid = inst.get("id")
            gpu = inst.get("gpu_name", "?")
            n = inst.get("num_gpus", 1)
            st = str(inst.get("actual_status") or "unknown")
            raw_msg = inst.get("status_msg") or ""
            extra   = inst.get("extra_env") or inst.get("label") or ""
            msg = raw_msg if raw_msg and raw_msg != st else (extra if extra else st)
            is_offline = (st == "offline")
            
            # CRITICAL: Check for "failed to create task for container" error
            # This is a fatal error - immediately destroy and blacklist
            is_fatal_container_error = "failed to create task for container" in msg
            
            is_error = (
                st in ("exited", "error") or
                (st and "error" in st.lower() and st not in ("created", "loading", "provisioning")) or
                "Error response from daemon" in msg or
                "OCI runtime" in msg or
                "CDI devices" in msg or
                ("failed" in msg.lower() and "OCI" in msg)
            )

            # Immediately kill and blacklist servers with fatal container error
            # BUT still add to prov_results so user can see them before deletion
            if is_fatal_container_error:
                auto_kill_set.add(iid)
                mid = str(inst.get("machine_id", "?"))
                dph = float(inst.get("dph_total", 0.0))
                gpu_str = f"{inst.get('num_gpus', 1)}x {inst.get('gpu_name', '?')}"
                geo = inst.get("geolocation", "Unknown")
                add_to_blacklist(iid, mid, reason="Fatal: failed to create container task", 
                               details=f"{gpu_str} | ${dph:.3f}/hr | {geo} | Fatal container error")
                print(f"  ❌ [{iid}] FATAL CONTAINER ERROR - Destroyed and Blacklisted")
                # Add to prov_results with FATAL status before continuing
                prov_results.append({
                    "id": iid, "gpu": gpu_str, "status": "FATAL (will delete)", "last_log": msg,
                    "_icon": "💀",
                    "_status_text": "FATAL (will delete)",
                    "_is_low_or_bad": True
                })
                continue

            start_date = float(inst.get("start_date", 0.0))
            elapsed_min = int((time.time() - start_date) / 60) if start_date > 0 else 0

            if is_offline:
                if iid not in _offline_timers:
                    _offline_timers[iid] = time.time()
                elapsed_min = int((time.time() - _offline_timers[iid]) / 60)
                if _ak_filters["AK_OFFLINE"]["val"] > 0 and elapsed_min >= _ak_filters["AK_OFFLINE"]["val"]:
                    auto_kill_set.add(iid)
            else:
                limit_install = _ak_filters["AK_INSTALL"]["val"]
                if is_error and elapsed_min >= 5:  # give 5m grace even for errors
                    auto_kill_set.add(iid)
                elif limit_install > 0 and elapsed_min >= limit_install:
                    auto_kill_set.add(iid)

            if is_error:
                disp_status = f"ERROR ({st})"
                icon = "❌"
            elif is_offline:
                disp_status = f"OFFLINE ({elapsed_min}m)"
                icon = "❌"
            elif st == "loading":
                disp_status = f"loading ({elapsed_min}m)"
                icon = "🔄"
            else:
                disp_status = f"{st} ({elapsed_min}m)"
                icon = "⏳"

            if not msg and st in ("loading", "created", "unknown", "exited", "", "provisioning"):
                msg = "[Awaiting Server Initialization]"
            
            prov_results.append({
                "id": iid, "gpu": f"{n}x {gpu}", "status": disp_status, "last_log": msg,
                "_icon": icon,
                "_status_text": disp_status,
                "_is_low_or_bad": (is_offline or is_error)
            })
            
        not_running_ids = {str(i.get("id")) for i in not_running}
        awaiting_miner_ids = {str(r["id"]) for r in awaiting_miner_results}
                
        prov_results.sort(key=lambda x: x["id"])

        with _state_lock:
            app_state["now"] = now
            app_state["cycle"] = cycle
            app_state["active_results"] = active_results
            app_state["prov_results"] = prov_results
            app_state["total"] = total
            app_state["underperf"] = underperf
            app_state["instances"] = instances
            app_state["server_history"] = server_history

        try:
            state_dump = {
                "active_results": active_results,
                "prov_results": prov_results,
                "instances": instances,
                "now": now
            }
            state_path = os.path.join(ROOT_DIR, "checker-cron", "watchdog_state.json")
            with open(state_path, "w") as f:
                json.dump(state_dump, f)
        except Exception as e:
            import traceback
            error_path = os.path.join(ROOT_DIR, "checker-cron", "dump_error.log")
            with open(error_path, "w") as f:
                f.write(traceback.format_exc())

        # Process auto-kills (set dedupes any duplicate triggers)
        for iid in auto_kill_set:
            inst = next((i for i in instances if i["id"] == iid), None)
            if inst:
                mid = inst.get("machine_id", "?")
                if mid != "?":
                    geo = inst.get("geolocation", inst.get("country_code", "Unknown"))
                    dph = float(inst.get("dph_total", 0.0))
                    gpu_str = f"{inst.get('num_gpus', 1)}x {inst.get('gpu_name', '?')}"
                    st = inst.get("actual_status", "unknown")
                    # Only blacklist if truly bad host — not just slow to start
                    if st not in ("created", "loading", "provisioning"):
                        reason = "Auto-Kill (Underperforming)" if st == "running" else f"Auto-Kill (Stuck in {st})"
                        details = f"{gpu_str} | ${dph:.3f}/hr | {geo} | Status: {st}"
                        add_to_blacklist(iid, mid, reason=reason, details=details)
            vast_destroy(iid)
            _low_hr_timers.pop(iid, None)
            _offline_timers.pop(iid, None)
            
        time.sleep(REFRESH)



def do_copy(iid, all_res):
    found = next((r for r in all_res if r["id"] == iid), None)
    if found:
        hr   = found.get('hr', 0)
        gpu  = found.get('gpu', '?')
        dph  = found.get('dph', 0)
        c100 = (dph / hr) * 100 if hr > 0 else 0
        up   = f"{found.get('up', 0)}m"
        log  = found.get('last_log', '')
        hr_s = f"{hr:.1f} TH/s" if hr > 0 else "---"
        c100_s = f"${c100:.3f}" if hr > 0 else "---"
        msg  = (f"Посмотри этот сервер\n"
                f"проверь сервер {iid}. статус: {found.get('status','?')}."
                f" GPU: {gpu}. HR: {hr_s}. $/100T: {c100_s}. UP: {up}."
                f" лог: {log}")
        subprocess.run(["pbcopy"], universal_newlines=True, input=msg)
        return msg
    return ""

def do_reinstall(iid, all_res, instances):
    found = next((r for r in all_res if r["id"] == iid), None)
    inst  = next((i for i in instances if i["id"] == iid), None)
    if not found or not inst: return ""
    gpu  = found.get('gpu', '?')
    dph  = found.get('dph', 0)
    up   = found.get('up', 0)
    log  = str(found.get('last_log', '') or '').strip()
    geo  = inst.get('geolocation', '?')
    mid  = inst.get('machine_id', '?')
    ssh_host = inst.get('ssh_host', '?')
    ssh_port = inst.get('ssh_port', '?')
    log_s = log[:120] if log else "empty"
    msg = (f"Посмотри этот сервер\n"
           f"переустанови майнер на сервере {iid}. GPU: {gpu}. UP: {up}m. "
           f"$/hr: ${dph:.3f}. Гео: {geo}. mid: {mid}.\n"
           f"SSH: root@{ssh_host} -p {ssh_port}\n"
           f"HR: 0.0 TH/s. Лог: {log_s}")
    subprocess.run(["pbcopy"], universal_newlines=True, input=msg)
    return msg

def do_kill(iid, instances):
    inst = next((inst for inst in instances if inst["id"] == iid), None)
    if inst:
        if inst.get("is_do"): return # Do not allow killing DO instances
        mid = inst.get("machine_id", "?")
        gpu = f"{inst.get('num_gpus',1)}x {inst.get('gpu_name', '?')}"
        dph = float(inst.get("dph_total", 0.0))
        geo = inst.get("geolocation", inst.get("country_code", "Unknown"))
        details = f"{gpu} | ${dph:.3f}/hr | {geo}"
        if mid != "?": add_to_blacklist(iid, mid, reason="Manual Kill", details=details)
    
    import threading
    threading.Thread(target=vast_destroy, args=(iid,), daemon=True).start()
    
    _low_hr_timers.pop(iid, None)
    _offline_timers.pop(iid, None)
    try:
        with _state_lock:
            if "instances" in app_state: app_state["instances"] = [i for i in app_state["instances"] if i["id"] != iid]
            if "active_results" in app_state: app_state["active_results"] = [r for r in app_state["active_results"] if r["id"] != iid]
            if "prov_results" in app_state: app_state["prov_results"] = [r for r in app_state["prov_results"] if r["id"] != iid]
    except Exception: pass

def main():
    import threading
    t = threading.Thread(target=monitor_thread, daemon=True)
    t.start()
    
    selected_idx = 0
    selected_btn = 0 # 0: COPY, 1: KILL
    action_msg = ""
    action_msg_expire = 0
    _expanded = set()  # set of instance IDs with expanded log view
    show_ak_settings = False
    
    print("  Fetching initial data...")
    while not app_state["now"]:
        time.sleep(0.5)
        
    import tty, termios
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    
    try:
        sys.stdout.write("\033[?1049h\033[H\033[2J") # Enter alternate screen and clear
        sys.stdout.flush()
        tty.setcbreak(fd)
        while True:
            cols, rows = shutil.get_terminal_size((120, 40))
            
            # Sort active servers by most profitable (smallest $/100TH)
            active = sorted(app_state["active_results"], key=lambda r: (r.get("dph",0)/r.get("hr",0.1)*100) if r.get("hr",0) > 0 else float('inf'))
            prov = app_state["prov_results"]
            
            all_res = active + prov
            max_active = len(active)
            max_prov = len(prov)
            ak_keys = list(_ak_filters.keys())
            num_ak = (len(ak_keys) if show_ak_settings else 0) + 1
            
            active_gpus = []
            for r in active:
                if r["gpu"] not in active_gpus:
                    active_gpus.append(r["gpu"])
                    
            total_items = max_active + num_ak + 1 + max_prov + 1
            has_graph = len(active) > 0 and "plotext" in sys.modules and "global_c100" in app_state["server_history"]
            leg_idx = total_items
            if has_graph:
                total_items += 1
            # Fixed UI elements take ~15 lines (reduced from 25)
            FIXED_LINES = 15
            available = max(15, rows - FIXED_LINES)
            
            # Start table: DYNAMIC size, scrollable - uses most available space
            START_TABLE_MIN = 15  # Minimum visible rows (was 3)
            START_TABLE_MAX = max(START_TABLE_MIN, int(available * 0.8))  # Up to 80% of space (was 50%)
            vis_prov_count = min(START_TABLE_MAX, max_prov)
            
            # Give active table remaining space
            vis_active_count = min(max_active, available - vis_prov_count)
                
            if total_items > 0:
                selected_idx = max(0, min(selected_idx, total_items - 1))
                
            active_start = 0
            if selected_idx < max_active:
                active_start = max(0, selected_idx - vis_active_count // 2)
                if active_start + vis_active_count > max_active:
                    active_start = max(0, max_active - vis_active_count)
            elif max_active > 0:
                active_start = max(0, max_active - vis_active_count)
                
            visible_active = active[active_start : active_start + vis_active_count]
            
            # Auto-scroll Start table to show newest (last) entries when count increases
            prev_count = app_state.get("prev_prov_count", 0)
            if max_prov > prev_count and max_prov > vis_prov_count:
                # New servers added - scroll to show the last entries
                prov_start = max(0, max_prov - vis_prov_count)
            elif selected_idx >= max_active + num_ak + 1:
                # User is navigating in Start table - follow selection
                p_idx = selected_idx - max_active - num_ak - 1
                prov_start = max(0, min(p_idx, max_prov - vis_prov_count))
            else:
                # Default: show the most recent entries (bottom of list)
                prov_start = max(0, max_prov - vis_prov_count)
            
            visible_prov = prov[prov_start : prov_start + vis_prov_count]
            
            # Update previous count for next cycle
            app_state["prev_prov_count"] = max_prov
            
            W = max(80, cols - 2)
            SEP = f"\033[90m{'─' * W}\033[0m"

            buf = []
            buf.append("\033[H")

            # ── Header ────────────────────────────────────────────────
            total_th = app_state['total']
            total_str = f"{total_th / 1000.0:.2f} PH/s" if total_th >= 1000 else f"{total_th:.1f} TH/s"
            total_gpus = sum(int(str(r.get("gpu", "1x")).split("x")[0]) for r in active if "x" in str(r.get("gpu", "")))
            global_c100 = app_state["server_history"].get("global_c100", [None])
            last_c100 = next((v for v in reversed(global_c100) if v is not None), 0)
            c100_col = "\033[32m" if last_c100 < 0.20 else ("\033[33m" if last_c100 < 0.26 else "\033[31m")
            buf.append(f"  \033[1mWatch\033[0m  {app_state['now']}  cycle #{app_state['cycle']}  "
                       f"│  {total_str}  {len(active)} inst ({total_gpus} GPU)  "
                       f"│  fleet $/100T: {c100_col}${last_c100:.3f}\033[0m\n")
            buf.append(SEP + "\n")

            # ── Column header ─────────────────────────────────────────
            hdr_prefix = " " * 4
            hdr_line = (f"{hdr_prefix}{'ID':<11} {'GPU':<16} {'$/hr':>7} {'RAW HR':<11} {'AVG HR':<11}"
                        f" {'SHARES/h':<18} {'$/100T':<9} {'EFF%':<7} {'TEMP':>9} {'PWR':>9} {'WRM':<6} {'UP':<6} │ STATUS")
            buf.append(f"  \033[90m{hdr_line}\033[0m\n")
            buf.append(SEP + "\n")

            # ── Active servers ────────────────────────────────────────
            row_idx = 0
            if not active:
                buf.append(f"  No active hashing instances.\n")
            else:
                if active_start > 0:
                    buf.append(f"  \033[90m↑ {active_start} more above\033[0m\n")

                for i, r in enumerate(visible_active):
                    row_idx = active_start + i
                    iid = r["id"]
                    real_shares = max(0, r["shares"] - r["drops"])
                    real_shr = r["share_hr"]
                    if r["shares"] > 0: real_shr = r["share_hr"] * (real_shares / r["shares"])
                    hr_str  = f"{r['hr']:.1f} TH/s"
                    shr_str = f"{real_shr:.1f} TH/s" if real_shr > 0 else "---      "
                    up_str  = f"{r['up']}m" if r.get("up", 0) > 0 else "--"

                    wm = int((time.time() - r.get("start_date", 0.0)) / 60)
                    warm_str = f"{wm}m" if wm < 15 else "•"

                    c100 = (r["dph"] / r["hr"]) * 100 if r["hr"] > 0 else 0
                    c100_col2 = "\033[32m" if c100 < 0.20 else ("\033[33m" if c100 < 0.26 else "\033[31m")
                    c100_val  = f"${c100:.3f}"  # plain 7-char string for alignment
                    c100_str  = f"{c100_col2}{c100_val:<7}\033[0m"  # colour wraps padded value

                    if r.get("up", 0) > 0:
                        up_h   = r["up"] / 60
                        hits_h = int(real_shares / up_h)
                        att_h  = int(r["attempts"] / up_h)
                    else:
                        hits_h = att_h = 0
                    sh_str = f"{hits_h}/{att_h}"
                    if r["drops"] > 0: sh_str += f" -{r['drops']}DR"

                    # EFF% = hits/attempts ratio
                    eff = int(r["shares"] / r["attempts"] * 100) if r["attempts"] > 0 else 0
                    eff_col = "\033[32m" if eff >= 10 else ("\033[33m" if eff >= 5 else "\033[31m")
                    eff_val = f"{eff}%" if r["attempts"] > 0 else "---"
                    eff_str = f"{eff_col}{eff_val}\033[0m{'':<{max(0,5-len(eff_val))}}"

                    # GPU TDP reference (W) per card
                    _TDP = {"RTX 5090":575,"RTX 5080":360,"RTX 4090":450,"RTX 4080":320,
                            "RTX 3090":350,"RTX 3080 Ti":350,"RTX 3080":320,"RTX 3070":220}
                    gpu_model = r["gpu"].split("x ",1)[-1] if "x " in r["gpu"] else r["gpu"]
                    n_gpus = int(r["gpu"].split("x")[0]) if "x" in r["gpu"] else 1
                    tdp = _TDP.get(gpu_model, 300) * n_gpus

                    # TEMP/UTIL/PWR from nvidia-smi
                    temps  = r.get("temps", [])
                    utils  = r.get("utils", [])
                    powers = r.get("powers", [])
                    if temps:
                        avg_t = int(sum(temps)/len(temps))
                        avg_u = int(sum(utils)/len(utils)) if utils else 0
                        t_col = "\033[32m" if avg_t < 75 else ("\033[33m" if avg_t < 85 else "\033[31m")
                        temp_str = f"{t_col}{avg_t:>3}C {avg_u:>3}%\033[0m"
                    else:
                        temp_str = f"{'---':>3}  {'---':>3} "

                    if powers:
                        total_pwr = sum(powers)
                        pwr_pct   = total_pwr / tdp * 100 if tdp > 0 else 0
                        p_col = "\033[32m" if pwr_pct >= 85 else ("\033[33m" if pwr_pct >= 70 else "\033[31m")
                        pwr_str = f"{p_col}{int(total_pwr):>4}W {int(pwr_pct):>3}%\033[0m"
                    else:
                        pwr_str = f"{'---':>4}  {'---':>3} "

                    sel = (row_idx == selected_idx)
                    bg  = "\033[48;5;236m" if sel else ""
                    dead_log = str(r.get('last_log', '') or '').strip()
                    needs_reinstall = (r.get('miner_dead') or
                                       (r.get('hr', 0) == 0 and
                                        dead_log in ('', 'Miner log empty or missing', 'DEAD', 'TOUT', 'DENY', 'MINER PROCESS DEAD')) or
                                       r.get('flag') in ('DEAD', 'TOUT', 'DENY', 'NOSSH'))
                    if sel:
                        c_btn = "\033[7m[C]\033[27m" if selected_btn == 0 else "[C]"
                        k_btn = "\033[7m[K]\033[27m" if selected_btn == 1 else "[K]"
                        if r.get("is_do"): k_btn = "   "
                        l_btn = "\033[7m[L]\033[27m" if selected_btn == 2 or iid in _expanded else "[L]"
                        r_btn = ("\033[7m\033[93m[R]\033[0m\033[48;5;236m" if selected_btn == 3 else "\033[93m[R]\033[0m\033[48;5;236m") if needs_reinstall else ""
                        k_pfx = k_btn
                        side_btns = f" {c_btn}{l_btn}{r_btn}"
                    else:
                        r_hint = " \033[93m[R]\033[0m" if needs_reinstall else ""
                        k_pfx = "   " if r.get("is_do") else "[K]"
                        side_btns = f" [C][L]{r_hint}"
                    dph_str = f"${r.get('dph', 0):.2f}"
                    
                    id_col = "\033[96;1m" if r.get("is_do") else ""
                    id_end = "\033[0m" if r.get("is_do") else ""
                    
                    line = (f"  {bg}{k_pfx} {r['_icon']}  {id_col}{str(iid):<11} {r['gpu']:<16}{id_end} {dph_str:>7} "
                            f"{hr_str:<11} {shr_str:<11} {sh_str:<18} {c100_str} "
                            f"{eff_str} {temp_str} {pwr_str} {warm_str:<5} {up_str:<6} │ {r['_status_text']}{side_btns}")

                    if r['_is_low_or_bad']:
                        buf.append(f"\033[93m{line}\033[0m\n")
                    else:
                        buf.append(f"{line}\033[0m\n")
                    # Expanded log lines
                    if iid in _expanded:
                        raw_log = str(r.get('last_log', '') or '').strip()
                        log_lines = [l.strip() for l in raw_log.splitlines() if l.strip()][-3:]
                        if not log_lines:
                            log_lines = ["(no log available)"]
                        for ll in log_lines:
                            ll_disp = ll[:max(40, W - 10)]
                            buf.append(f"  \033[90m      {'':9} {'':14} {ll_disp}\033[0m\n")

                if active_start + vis_active_count < max_active:
                    hidden = max_active - (active_start + vis_active_count)
                    buf.append(f"  \033[90m↓ {hidden} more below\033[0m\n")

            buf.append(SEP + "\n")

            # ── Active footer buttons — index max_active ─────
            def _btn(label, active_flag): return f"\033[7m{label}\033[27m" if active_flag else label
            row_idx = max_active
            ce_btn = _btn("[ COPY ERR ]", row_idx == selected_idx and selected_btn == 0)
            ca_btn = _btn("[ COPY ALL ]", row_idx == selected_idx and selected_btn == 1)
            co_btn = _btn("[ COPY ]", row_idx == selected_idx and selected_btn == 2)
            ka_btn = _btn("[ KILL ALL ]", row_idx == selected_idx and selected_btn == 3)
            buf.append(f"  {ce_btn}  {ca_btn}  {co_btn}  {ka_btn}\n")
            
            # ── AUTO-KILL SETTINGS (dim cyan block) ───────────────────
            # Indices max_active+1 .. max_active+num_ak
            ak_keys = list(_ak_filters.keys())
            
            toggle_idx = max_active + 1
            sel_tog = (toggle_idx == selected_idx)
            bg_tog  = "\033[48;5;236m" if sel_tog else ""
            t_icon = "🔽 (Hide)" if show_ak_settings else "▶️ (Show)"
            buf.append(f"  {bg_tog}\033[36m\033[2m[ AUTO-KILL SETTINGS ] {t_icon}\033[0m\n")
            
            if show_ak_settings:
                for i, ak_k in enumerate(ak_keys):
                    f = _ak_filters[ak_k]
                    row_idx = max_active + 2 + i
                    sel = (row_idx == selected_idx)
                    bg  = "\033[48;5;236m" if sel else ""

                    if ak_k == "AK_INSTANT":
                        val_label = f"${f['val']:.2f}" if f['val'] > 0 else "\033[31mOFF\033[0m"
                    else:
                        val_label = f"{f['val']}m" if f['val'] > 0 else "\033[31mOFF\033[0m"
                    val_str = f"{val_label:^7}" if f['val'] != 0 else " " * 2 + val_label + " " * 2

                    desc_text = f["desc"]
                    if "limit" in f:
                        desc_text = desc_text.replace("limit", f"${f['limit']:.2f}")

                    line = f"  {bg}\033[36m\033[2m◆ {desc_text:<28}\033[0m{bg} ◀ {val_str} ▶"
                    if "limit" in f:
                        line += f"  [-${f['limit']:.2f}+]"
                    line += "\033[0m"
                    buf.append(f"{line}\n")

            buf.append("\n")
            
            if "plotext" in sys.modules and "global_c100" in app_state["server_history"]:
                try:
                    import plotext as plt
                    reserved_lines = 30 + vis_active_count + vis_prov_count
                    plot_h = max(6, (rows - reserved_lines) // 2)
                    plot_w = max(60, W - 10)
                    pad_left = (W - plot_w) // 2
                    pad_str = " " * max(2, pad_left)
                    
                    plt.clf()
                    plt.theme("clear")
                    plt.plotsize(plot_w, plot_h)
                    
                    c100_data = app_state["server_history"]["global_c100"]
                    clean_c100 = []
                    started = False
                    for val in c100_data:
                        if val is not None: started = True
                        if started: clean_c100.append(val or 0)
                    
                    if clean_c100:
                        x_vals = []
                        clean_c100_log = []
                        for i, v in enumerate(clean_c100):
                            if v > 0.05: # Ignore anomalous zeroes to keep graph visible area stable
                                x_vals.append(i)
                                clean_c100_log.append(v)
                        if x_vals:
                            plt.plot(x_vals, clean_c100_log, color="green", marker="braille")
                        plt.title("Fleet AVERAGE $/100T Trend (log scale)")
                        plt.yscale("log")
                        
                        # Set X-axis ticks every 30 minutes
                        cycles_per_30m = int((30 * 60) / REFRESH)
                        if cycles_per_30m < 1: cycles_per_30m = 1
                        
                        ticks = []
                        labels = []
                        curr_tick = len(clean_c100) - 1
                        mins_ago = 0
                        
                        while curr_tick >= 0:
                            ticks.append(curr_tick)
                            if mins_ago == 0:
                                labels.append("Now")
                            else:
                                if mins_ago >= 60:
                                    labels.append(f"-{mins_ago//60}h{f'{mins_ago%60}m' if mins_ago%60!=0 else ''}")
                                else:
                                    labels.append(f"-{mins_ago}m")
                            curr_tick -= cycles_per_30m
                            mins_ago += 30
                            
                        ticks.reverse()
                        labels.reverse()
                        plt.xticks(ticks, labels)
                        
                        plot_str = plt.build()
                        for line in plot_str.split("\n"):
                            buf.append(f"{pad_str}{line}\n")
                        buf.append("\n")
                except Exception:
                    pass
            
            buf.append(f"\n")
            buf.append(SEP + "\n")
            buf.append(f"  \033[1m⏳ Start\033[0m  ({max_prov} servers)\n")
            buf.append(f"  \033[90m{'ID':<9} {'GPU':<12} {'STATUS':<18} {'BTN':<8} LAST LOG\033[0m\n")
            buf.append(SEP + "\n")
            if not prov:
                buf.append(f"  No provisioning servers currently.\n")
            else:
                if prov_start > 0:
                    buf.append(f"  \033[90m↑ {prov_start} more above\033[0m\n")

                for i, r in enumerate(visible_prov):
                    row_idx = max_active + num_ak + 1 + prov_start + i
                    log_msg = str(r.get("last_log", "")).replace("\n", " ").strip()
                    st_text = str(r.get("_status_text", "")).strip()
                    max_log = max(20, W - 70)
                    if len(log_msg) > max_log: log_msg = log_msg[:max_log - 3] + "..."
                    # Don't repeat status in log col
                    if log_msg == st_text or log_msg in ("loading", "created", "unknown", "exited", ""):
                        log_disp = "\033[90m[Awaiting Server Initialization]\033[0m" if log_msg in ("loading", "created", "") else "\033[90m—\033[0m"
                    else:
                        log_col = "\033[91m" if r.get("_is_low_or_bad") else "\033[90m"
                        log_disp = f"{log_col}{log_msg}\033[0m"

                    sel = (row_idx == selected_idx)
                    bg  = "\033[48;5;236m" if sel else ""
                    if sel:
                        c_btn = "\033[7m[C]\033[27m" if selected_btn == 0 else "[C]"
                        k_btn = "\033[7m[K]\033[27m" if selected_btn == 1 else "[K]"
                        if r.get("is_do"): k_btn = "   "
                        btns  = f" {c_btn}{k_btn}"
                    else:
                        btns = " [C]   " if r.get("is_do") else " [C][K]"
                    st_col = "\033[91m" if r.get("_is_low_or_bad") else "\033[0m"
                    id_col = "\033[96;1m" if r.get("is_do") else ""
                    id_end = "\033[0m" if r.get("is_do") else ""
                    line = (f"  {bg}{r['_icon']}  {id_col}{str(r['id']):<9} {r['gpu']:<12}{id_end} "
                            f"{st_col}{st_text:<18}\033[0m{btns}  {log_disp}")
                    buf.append(f"{line}\033[0m\n")

                if prov_start + vis_prov_count < max_prov:
                    hidden = max_prov - (prov_start + vis_prov_count)
                    buf.append(f"  \033[90m↓ {hidden} more below\033[0m\n")

            buf.append(SEP + "\n")

            # ── Provisioning footer buttons ────────────────────────────
            row_idx = max_active + num_ak + 1 + max_prov
            ce_btn2 = _btn("[ COPY ERR ]", row_idx == selected_idx and selected_btn == 0)
            ca_btn2 = _btn("[ COPY ALL ]", row_idx == selected_idx and selected_btn == 1)
            co_btn2 = _btn("[ COPY     ]", row_idx == selected_idx and selected_btn == 2)
            ka_btn2 = _btn("[ KILL ALL ]", row_idx == selected_idx and selected_btn == 3)
            buf.append(f"  {ce_btn2}  {ca_btn2}  {co_btn2}  {ka_btn2}\n")


            if active and "plotext" in sys.modules:
                try:
                    import plotext as plt
                    reserved_lines = 30 + vis_active_count + vis_prov_count
                    plot_h = max(6, (rows - reserved_lines) // 2)
                    plot_w = max(60, W - 10)
                    pad_left = (W - plot_w) // 2
                    pad_str = " " * max(2, pad_left)
                    
                    plt.clf()
                    plt.theme("clear")
                    plt.plotsize(plot_w, plot_h)
                    plt.title("Hashrate")
                    plt.xlabel("Time (cycles)")
                    plt.ylabel("% Expected HR (log)")
                    plt.yscale("log")
                    
                    colors = ["blue", "yellow", "red", "green", "cyan", "magenta", "white"]
                    color_codes = {"blue": 34, "yellow": 33, "red": 31, "green": 32, "cyan": 36, "magenta": 35, "white": 37}
                    
                    gpu_groups = {}
                    for r in active:
                        gpu_groups.setdefault(r["gpu"], []).append(str(r["id"]))
                    
                    gpu_colors = {}
                    server_colors = {}
                    for i, gpu in enumerate(gpu_groups.keys()):
                        c_name = colors[i % len(colors)]
                        gpu_colors[gpu] = c_name
                    
                    for idx, r in enumerate(active):
                        iid = str(r["id"])
                        server_colors[iid] = colors[idx % len(colors)]
                    
                    for r in active:
                        iid = str(r["id"])
                        data = app_state["server_history"].get(iid, [])
                        if not data: continue
                        n = int(r["gpu"].split("x")[0])
                        gpu_name = r["gpu"].split("x ")[1] if "x " in r["gpu"] else r["gpu"]
                        exp = get_expected_hr(gpu_name, 1)
                        
                        clean_data = []
                        started = False
                        for val in data:
                            if val is not None:
                                started = True
                            if started:
                                clean_data.append(val)
                                
                        x_vals = []
                        y_vals = []
                        for i, val in enumerate(clean_data):
                            if val is not None and val > 0:
                                x_vals.append(i + 1)
                                y_vals.append((val / n) / exp * 100)
                        if x_vals:
                            plt.plot(x_vals, y_vals, marker="braille", color=server_colors[iid])
                    
                    actual_lengths = []
                    for h in app_state["server_history"].values():
                        first_valid = next((i for i, v in enumerate(h) if v is not None), -1)
                        if first_valid != -1:
                            actual_lengths.append(len(h) - first_valid)
                            
                    max_len = max(actual_lengths + [60])
                    right_bound = int(max_len * 1.3)
                    plt.xlim(1, right_bound)
                    graph_str = plt.build()
                    buf.append("\n")
                    
                    num_groups = len(active_gpus)
                    if num_groups > 0:
                        col_w = max(15, plot_w // num_groups)
                        
                        header_str = ""
                        for i, gpu in enumerate(active_gpus):
                            count = len(gpu_groups[gpu])
                            is_expanded = app_state.get(f"_leg_{gpu}", False)
                            icon = "▼" if is_expanded else "▶"
                            item_text = f"{icon} {gpu} ({count})"
                            
                            if selected_idx == leg_idx and selected_btn == i:
                                item_text = f"\033[7m{item_text}\033[27m"
                            
                            vis_len = len(re.sub(r'\033\[[0-9;]*m', '', item_text))
                            pad_len = max(1, col_w - vis_len)
                            header_str += item_text + " " * pad_len
                        
                        leg_pad = " " * max(2, pad_left + (plot_w - len(re.sub(r'\033\[[0-9;]*m', '', header_str))) // 2)
                        buf.append(f"{leg_pad}\033[1m{header_str}\033[0m\n")
                        
                        if any(app_state.get(f"_leg_{g}", False) for g in active_gpus):
                            max_items = max((len(gpu_groups[g]) if app_state.get(f"_leg_{g}", False) else 0) for g in active_gpus)
                            for row in range(max_items):
                                row_str = ""
                                for gpu in active_gpus:
                                    items = gpu_groups[gpu]
                                    if app_state.get(f"_leg_{gpu}", False) and row < len(items):
                                        iid = items[row]
                                        c_name = server_colors[iid]
                                        cc = color_codes[c_name]
                                        item_str = f"\033[{cc}m■ {iid}\033[0m"
                                        row_str += item_str + " " * (col_w - len(f"■ {iid}"))
                                    else:
                                        row_str += " " * col_w
                                buf.append(f"{leg_pad}{row_str}\n")
                            
                    for gline in graph_str.split("\n"):
                        buf.append(f"{pad_str}{gline}\n")
                except Exception as e:
                    buf.append(f"\n  [Graph Error]: {e}\n")
            
            buf.append(SEP + "\n")
            buf.append(f"  \033[90m[↑↓] navigate  [◀▶] AK adjust / btn select  [ENTER] action  [-/+] AK limit  [r] reload  [q] quit\033[0m\n")
            if action_msg and time.time() < action_msg_expire:
                buf.append(f"  \033[92m{action_msg}\033[0m\n")
            elif time.time() >= action_msg_expire:
                action_msg = ""
            
            buf.append("\033[J") # Clear any remaining lines from previous longer frames
            out_str = "".join(buf).rstrip("\n")
            out_str = out_str.replace("\n", "\033[K\n") + "\033[K\033[J"
            sys.stdout.write(out_str)
            sys.stdout.flush()
                
            # Drain all pending keys to make navigation ultra-snappy
            keys_processed = False
            import os
            while True:
                # Use 0.1s timeout for the first read (frame delay), 0.0s for subsequent reads
                timeout = 0.1 if not keys_processed else 0.0
                r, _, _ = select.select([sys.stdin], [], [], timeout)
                if not r:
                    break
                    
                ch = os.read(fd, 1).decode('utf-8', 'replace')
                key = ch
                if ch == '\x1b':
                    r2, _, _ = select.select([sys.stdin], [], [], 0.05)
                    if r2:
                        ch2 = os.read(fd, 1).decode('utf-8', 'replace')
                        if ch2 in ('[', 'O'):
                            r3, _, _ = select.select([sys.stdin], [], [], 0.05)
                            if r3:
                                ch3 = os.read(fd, 1).decode('utf-8', 'replace')
                                if ch3 == 'A': key = 'UP'
                                elif ch3 == 'B': key = 'DOWN'
                                elif ch3 == 'C': key = 'RIGHT'
                                elif ch3 == 'D': key = 'LEFT'
                
                keys_processed = True
                
                def needs_reinstall_for(idx):
                    if idx >= max_active: return False
                    r = active[idx] if idx < len(active) else None
                    if not r: return False
                    dl = str(r.get('last_log','') or '').strip()
                    return (r.get('hr',0) == 0 and dl in ('','Miner log empty or missing')) or r.get('flag') in ('DEAD','TOUT','DENY','NOSSH')

                def get_max_btn(idx):
                    if idx < max_active: return 3 if needs_reinstall_for(idx) else 2
                    if idx == max_active: return 3
                    if idx <= max_active + num_ak: return 0
                    if idx < max_active + num_ak + 1 + max_prov: return 1
                    if idx == max_active + num_ak + 1 + max_prov: return 3
                    if has_graph and idx == leg_idx: return max(0, len(active_gpus) - 1)
                    return 0
                
                if key == 'UP':
                    prev_idx = selected_idx
                    if selected_idx == 0 and total_items > 0:
                        selected_idx = total_items - 1
                    else:
                        selected_idx = max(0, selected_idx - 1)
                    selected_btn = min(selected_btn, get_max_btn(selected_idx))
                    # Clear any pending kill confirmations when navigating away
                    if prev_idx != selected_idx:
                        for k in list(app_state.keys()):
                            if k.startswith('_confirm_kill_'):
                                del app_state[k]
                elif key == 'DOWN':
                    prev_idx = selected_idx
                    if total_items > 0 and selected_idx == total_items - 1:
                        selected_idx = 0
                    else:
                        selected_idx = min(total_items - 1, selected_idx + 1)
                    selected_btn = min(selected_btn, get_max_btn(selected_idx))
                    # Clear any pending kill confirmations when navigating away
                    if prev_idx != selected_idx:
                        for k in list(app_state.keys()):
                            if k.startswith('_confirm_kill_'):
                                del app_state[k]
                elif key == 'LEFT':
                    if max_active < selected_idx <= max_active + num_ak:
                        if selected_idx > max_active + 1:
                            ak_keys = list(_ak_filters.keys())
                            ak_k = ak_keys[selected_idx - max_active - 2]
                            f = _ak_filters[ak_k]
                            f["val"] = max(f["min"], f["val"] - f["step"])
                    else:
                        selected_btn = max(0, selected_btn - 1)
                elif key == 'RIGHT':
                    if max_active < selected_idx <= max_active + num_ak:
                        if selected_idx > max_active + 1:
                            ak_keys = list(_ak_filters.keys())
                            ak_k = ak_keys[selected_idx - max_active - 2]
                            f = _ak_filters[ak_k]
                            f["val"] = min(f["max"], f["val"] + f["step"])
                    else:
                        selected_btn = min(get_max_btn(selected_idx), selected_btn + 1)
                elif key == '-':
                    if max_active < selected_idx <= max_active + num_ak:
                        if selected_idx > max_active + 1:
                            ak_keys = list(_ak_filters.keys())
                            ak_k = ak_keys[selected_idx - max_active - 2]
                            f = _ak_filters[ak_k]
                            if "limit" in f:
                                f["limit"] = max(0.10, f["limit"] - 0.05)
                elif key in ('+', '='):
                    if max_active < selected_idx <= max_active + num_ak:
                        if selected_idx > max_active + 1:
                            ak_keys = list(_ak_filters.keys())
                            ak_k = ak_keys[selected_idx - max_active - 2]
                            f = _ak_filters[ak_k]
                            if "limit" in f:
                                f["limit"] = min(1.00, f["limit"] + 0.05)
                elif key == 'l' or key == 'L':
                    if selected_idx < max_active:
                        iid = active[selected_idx]["id"] if selected_idx < len(active) else None
                        if iid:
                            if iid in _expanded: _expanded.discard(iid)
                            else: _expanded.add(iid)
                elif key == ' ':
                    pass
                elif key in ('\n', '\r'):
                    if total_items > 0:
                        if selected_idx == max_active or selected_idx == max_active + num_ak + 1 + max_prov: # Footer Buttons
                            is_active_btn = (selected_idx == max_active)
                            target_list = active if is_active_btn else prov
                            
                            if selected_btn in (0, 1, 2, 3):
                                if selected_btn == 3:
                                    k_cnt = 0
                                    for item in target_list:
                                        k_cnt += 1
                                        do_kill(item['id'], app_state["instances"])
                                    tbl_name = "active" if is_active_btn else "provisioning"
                                    action_msg = f"⚔️ Killed {k_cnt} {tbl_name} servers!"
                                    action_msg_expire = time.time() + 3.0
                                else:
                                    msgs = []
                                    for item in target_list:
                                        is_err = item.get('_is_low_or_bad', False) or item.get('status') != 'running'
                                        if selected_btn == 0 and not is_err: continue
                                        if selected_btn == 2 and is_err: continue
                                        m = f"проверь сервер {item['id']}. статус: {item.get('status', 'unknown')}."
                                        if item.get('last_log'): m += f" лог: {item['last_log']}"
                                        msgs.append(m)
                                    if msgs:
                                        import subprocess
                                        subprocess.run(["pbcopy"], universal_newlines=True, input="\n".join(msgs))
                                        action_msg = f"✅ Copied {len(msgs)} servers to clipboard!"
                                    else:
                                        action_msg = f"❌ No servers match this filter"
                                    action_msg_expire = time.time() + 3.0
                        elif has_graph and selected_idx == leg_idx:
                            if selected_btn < len(active_gpus):
                                gpu = active_gpus[selected_btn]
                                st_key = f"_leg_{gpu}"
                                app_state[st_key] = not app_state.get(st_key, False)
                        elif max_active < selected_idx <= max_active + num_ak:
                            if selected_idx == max_active + 1:
                                show_ak_settings = not show_ak_settings
                            else:
                                pass # Auto-Kill filters have no ENTER action now
                        else:
                            if selected_idx < max_active:
                                sel_id = active[selected_idx]["id"]
                            else:
                                sel_id = prov[selected_idx - max_active - num_ak - 1]["id"]
                                
                            if selected_btn == 0:
                                txt = do_copy(sel_id, all_res)
                                action_msg = f"✅ Copied to clipboard: {txt}"
                                action_msg_expire = time.time() + 3.0
                            elif selected_btn == 1:
                                # Confirmation required before kill
                                confirm_key = f"_confirm_kill_{sel_id}"
                                if app_state.get(confirm_key):
                                    # Second press - execute kill
                                    del app_state[confirm_key]
                                    action_msg = f"⚔️ Killing and blacklisting server {sel_id}..."
                                    action_msg_expire = time.time() + 3.0
                                    do_kill(sel_id, app_state["instances"])
                                else:
                                    # First press - ask confirmation
                                    app_state[confirm_key] = time.time()
                                    action_msg = f"⚠️ Press [ENTER] again to CONFIRM KILL of {sel_id}, or move to cancel"
                                    action_msg_expire = time.time() + 5.0
                            elif selected_btn == 2:
                                if selected_idx < max_active:
                                    if sel_id in _expanded: _expanded.discard(sel_id)
                                    else: _expanded.add(sel_id)
                                else:
                                    threading.Thread(target=lambda i=sel_id: subprocess.run(["vastai","start","instance",str(i)], capture_output=True)).start()
                            elif selected_btn == 3 and selected_idx < max_active:
                                txt = do_reinstall(sel_id, all_res, app_state["instances"])
                                action_msg = f"📋 Copied reinstall request for {sel_id}!"
                                action_msg_expire = time.time() + 3.0
                elif key.lower() == 'r':
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                    sys.stdout.write("\033[?1049l\033[?25h")
                    sys.stdout.flush()
                    os.execv(sys.executable, [sys.executable] + sys.argv)
                elif key.lower() == 'q' or key == '\x03': # Ctrl+C
                    print("\n  👋 Exiting...")
                    break
                
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        sys.stdout.write("\033[?1049l") # Exit alternate screen
        sys.stdout.flush()
        print("\n  👋 Exiting...")

if __name__ == "__main__":
    main()
