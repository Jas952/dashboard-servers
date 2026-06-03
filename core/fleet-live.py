#!/usr/bin/env python3
"""
Pearl Fleet Commander — live monitor + Telegram bot.

  python3 fleet-live.py              # refresh 60s
  python3 fleet-live.py 30           # refresh 30s
  python3 fleet-live.py --no-telegram
"""

import subprocess, json, sys, os, re, ssl, time, threading, shutil
import urllib.request, urllib.parse
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

_ssl = ssl.create_default_context()
_ssl.check_hostname = False
_ssl.verify_mode = ssl.CERT_NONE

# ── CONFIG ────────────────────────────────────────────────────
SSH_KEY    = os.path.expanduser("~/.ssh/vast_key")
TG_TOKEN   = "YOUR_TELEGRAM_BOT_TOKEN"
TG_CHAT    = "-1003645573999"
WALLET     = "YOUR_WALLET_ADDRESS"
IMAGE      = "nvidia/cuda:12.4.1-base-ubuntu22.04"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)
REFRESH    = 60
SCAN_INT   = 2            # new-offer check every 2s
AVAIL_INT  = 2            # edit "Available" msg every 2s
WARMUP     = 12
TG_RPT_INT = 300
TG_RATE_COOLDOWN = 120

TARGET_GPUS    = ["RTX_3090","RTX_5080","RTX_5090","RTX_4090","RTX_4080"]
GEO_SKIP       = ["RU", "UA", "Ukraine", "BY", "CN"]

DEFAULT_PRICES = {
    "RTX_5090": 0.70,
    "RTX_5080": 0.35,
    "RTX_4090": 0.35,
    "RTX_4080": 0.20,
    "RTX_3090": 0.25
}

_filters = {
    "MIN_REL": {"val": 90.0, "on": True, "str": "{val}%", "step": 1.0, "min": 50.0, "max": 100.0},
    "MAX_N": {"val": 4, "on": True, "str": "{val} GPUs per rig", "step": 1, "min": 1, "max": 16},
    "VERIFIED": {"val": True, "on": True, "str": "Only Verified", "step": 0, "min": True, "max": True},
    "INET_UP": {"val": 50, "on": False, "str": ">= {val} Mbps up", "step": 10, "min": 0, "max": 1000},
    "INET_DOWN": {"val": 100, "on": False, "str": ">= {val} Mbps down", "step": 10, "min": 0, "max": 1000},
    "MIN_HOST_MONTHS": {"val": 0, "on": False, "str": ">= {val} months old", "step": 1, "min": 0, "max": 24}
}
for gpu in TARGET_GPUS:
    def_p = DEFAULT_PRICES.get(gpu, 0.25)
    _filters[f"MAX_{gpu}"] = {"val": def_p, "on": True, "str": "${val}/hr per GPU", "step": 0.01, "min": 0.01, "max": 5.0}

EXPECTED = {"RTX 5090":310,"RTX 5080":160,"RTX 4090":145,"RTX 4080":120,
            "RTX 3090":100,"RTX 3080 Ti":75,"RTX 3080":65}
MIN_HR   = {"RTX 5090":180,"RTX 5080":90,"RTX 4090":80,"RTX 4080":65,
            "RTX 3090":50,"RTX 3080 Ti":40,"RTX 3080":35}

PAGE_SIZE = 5

_tg_on = True
_known_machines = set()   # track by machine_id (stable), NOT offer_id (rotates)
_killed = set()
_offer_cache = {}
_last_rpt = 0
_avail_msg_id = None      # persistent "Available Servers" message
_avail_page = 0
_last_avail_update = 0
_recent_books = []
_rented_mids = set()

def global_log(bot_name, tag, msg, iid, mid, gpu, geo, dph):
    import json
    from datetime import datetime
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


def update_book_status(mid, label, msg):
    book = next((b for b in _recent_books if b["id"] == mid), None)
    if not book:
        book = {"id": mid, "label": label, "status": "", "time": time.time()}
        _recent_books.append(book)
    book["status"] = msg

def clear_book_status_later(mid):
    time.sleep(10)
    for b in list(_recent_books):
        if b["id"] == mid:
            try: _recent_books.remove(b)
            except: pass
# ── TELEGRAM ──────────────────────────────────────────────────
_tg_rate_limited_until = 0

def _tg(method, p):
    global _tg_rate_limited_until
    if not _tg_on: return None
    # Rate limit cooldown
    if time.time() < _tg_rate_limited_until and method != "getUpdates":
        return None
    try:
        data = json.dumps(p).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/{method}",
            data=data, headers={"Content-Type":"application/json"})
        raw = urllib.request.urlopen(req, timeout=15, context=_ssl).read()
        resp = json.loads(raw)
        return resp
    except urllib.error.HTTPError as e:
        body = e.read().decode() if hasattr(e, 'read') else ''
        if e.code == 429:
            # Rate limited — extract retry_after or use default
            try:
                ra = json.loads(body).get('parameters',{}).get('retry_after', TG_RATE_COOLDOWN)
            except: ra = TG_RATE_COOLDOWN
            _tg_rate_limited_until = time.time() + ra
            # print(f"  [TG] Rate limited, cooling down {ra}s")
        else:
            # print(f"  [TG] HTTP {e.code}: {body[:200]}")
            pass
        return None
    except Exception as e:
        # print(f"  [TG] {e}")
        return None

def tg_send(text, chat=None, kb=None):
    p = {"chat_id":chat or TG_CHAT,"text":text,"parse_mode":"HTML"}
    if kb: p["reply_markup"] = kb
    return _tg("sendMessage", p)

def tg_edit(chat, mid, text, kb=None):
    p = {"chat_id":chat,"message_id":mid,"text":text,"parse_mode":"HTML"}
    if kb: p["reply_markup"] = kb
    return _tg("editMessageText", p)

def tg_answer(cid, text=""): _tg("answerCallbackQuery",{"callback_query_id":cid,"text":text})

def tg_updates(off=0):
    r = _tg("getUpdates", {
        "offset": off, "timeout": 3,
        "allowed_updates": ["message","channel_post","callback_query"]
    })
    return r.get("result",[]) if r and r.get("ok") else []

# ── VAST API ──────────────────────────────────────────────────
def vast_instances():
    try:
        r = subprocess.run(["vastai","show","instances","--raw"],
                           capture_output=True,text=True,timeout=30)
        return json.loads(r.stdout)
    except: return []

def vast_search():
    gf = ",".join(TARGET_GPUS)
    q_parts = [f"rentable=true gpu_name in [{gf}]"]
    if _filters.get("VERIFIED", {}).get("on"): q_parts.append("verified=true")
    if _filters.get("INET_UP", {}).get("on"): q_parts.append(f"inet_up>={_filters['INET_UP']['val']}")
    if _filters.get("INET_DOWN", {}).get("on"): q_parts.append(f"inet_down>={_filters['INET_DOWN']['val']}")
    if _filters["MIN_REL"]["on"]: q_parts.append(f"reliability>={_filters['MIN_REL']['val']/100.0}")
    if _filters["MAX_N"]["on"]: q_parts.append(f"num_gpus<={_filters['MAX_N']['val']}")
    q = " ".join(q_parts)
    try:
        r = subprocess.run(["vastai","search","offers",q,"-n","-d","-o","dph","--limit","1000","--raw"],
                           capture_output=True,text=True,timeout=30)
        return json.loads(r.stdout)
    except: return []

def vast_create(oid):
    r = subprocess.run(["vastai","create","instance",str(oid),
        "--image",IMAGE,"--ssh","--direct"],
        capture_output=True,text=True,timeout=30)
    m = re.search(r"'new_contract':\s*(\d+)", r.stdout)
    return int(m.group(1)) if m else None

def vast_destroy(iid):
    r = subprocess.run(["vastai","destroy","instance",str(iid)],
        input="y\n",capture_output=True,text=True,timeout=15)
    return "destroying" in r.stdout.lower()


# ── MARKET SCANNER ────────────────────────────────────────────
# AWS IP ranges that block SSH (updated as discovered)
_AWS_PREFIXES = (
    "3.", "18.", "34.", "35.", "52.", "54.",
    "13.32.", "13.33.", "13.34.", "13.35.",
    "23.20.", "32.197.", "3.91.", "3.236.",
)

def _is_aws_ip(ip: str) -> bool:
    if not ip: return False
    return any(ip.startswith(p) for p in _AWS_PREFIXES)

def scan_market():
    raw = vast_search()
    bl = load_blacklist()
    banned_mids = set()
    for k, v in (bl.items() if isinstance(bl, dict) else []):
        # Key is the machine_id, value contains instance details
        banned_mids.add(str(k))
    if isinstance(bl, list):
        for b in bl: banned_mids.add(str(b))

    res = []
    for o in raw:
        mid = str(o.get("machine_id", o["id"]))
        if mid in banned_mids or mid in _rented_mids or str(o["id"]) in _rented_mids: continue
        # Skip AWS-hosted machines — they block SSH
        pub_ip = o.get("public_ipaddr") or o.get("host_ip") or ""
        if _is_aws_ip(str(pub_ip)): continue
        geo = o.get("geolocation","")
        geo_tokens = {p.strip().upper() for p in re.split(r'[\s,/\\\-]+', geo) if p.strip()}
        if any(code.upper() in geo_tokens for code in GEO_SKIP): continue
        gpu = o.get("gpu_name",""); n = o.get("num_gpus",1)
        dph = o.get("dph_total",999)
        th = EXPECTED.get(gpu,0)*n
        if th==0: continue
        # host age filter
        mhm = _filters.get("MIN_HOST_MONTHS", {})
        if mhm.get("on") and mhm.get("val", 0) > 0:
            host_months = (o.get("host_run_time", 0) or 0) / (30 * 24 * 3600)
            if host_months < mhm["val"]: continue
        c = (dph/th)*100
        
        gpu_key = f"MAX_{gpu.replace(' ', '_')}"
        if gpu_key in _filters:
            f = _filters[gpu_key]
            if f["on"]:
                max_allowed = f["val"] * n
                if dph > max_allowed: continue
        rel = o.get("reliability2",o.get("reliability",0))
        drv = o.get("driver_version","?")
        pool = "us2" if any(x in geo for x in ["US","CA"]) else "eu1"
        mid = o.get("machine_id", o["id"])  # stable identifier
        res.append({"id":o["id"],"mid":mid,"gpu":gpu,"n":n,"label":f"{n}x {gpu}",
                     "geo":geo,"dph":dph,"th":th,"c100":c,
                     "rel":rel,"drv":drv,"pool":pool})
    # Sort by GPU model (version), then by number of GPUs (size), then by profitability (price)
    res.sort(key=lambda x: (x["gpu"], x["n"], x["c100"]))
    return res

def fmt_offers(offers, page=0, total=None):
    """Format a page of offers for Telegram (max 4096 chars)."""
    if not offers: return "No profitable servers found."
    total = total or len(offers)
    start = page * PAGE_SIZE
    chunk = offers[start:start+PAGE_SIZE]
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    lines = [f"🔍 <b>Available Servers</b> ({total} found)"]
    if pages > 1:
        lines[0] += f"  —  page {page+1}/{pages}"
    lines.append("")
    for i,o in enumerate(chunk, start+1):
        tag = "🟢" if o["c100"]<=0.20 else ("🟡" if o["c100"]<=0.22 else "🟠")
        c100_str = f"${o['c100']:.3f}/100T"
        lines.append(
            f"{tag} <b>{o['label']}</b>  ·  <b>${o['dph']:.3f}/hr</b>  ·  <code>{c100_str}</code>\n"
            f"📍 {o['geo']}\n"
            f"⚡ ~{o['th']} TH/s  |  Rel: {o['rel']:.0%}  |  Pool: {o['pool']}\n")
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3990] + "\n..."
    return text

def offer_kb(offers, page=0):
    """Inline keyboard: Book buttons for current page + nav."""
    start = page * PAGE_SIZE
    chunk = offers[start:start+PAGE_SIZE]
    pages = (len(offers) + PAGE_SIZE - 1) // PAGE_SIZE
    kb = []
    for i,o in enumerate(chunk, start+1):
        txt = f"📦 Book #{i} — {o['label']} ${o['dph']:.3f}/hr"
        cb = f"book:{o['id']}:{o['pool']}"
        kb.append([{"text":txt,"callback_data":cb}])
    # Navigation row
    if pages > 1:
        nav = []
        if page > 0:
            nav.append({"text":"◀️ Prev","callback_data":f"page:{page-1}"})
        nav.append({"text":f"{page+1}/{pages}","callback_data":"noop"})
        if page < pages - 1:
            nav.append({"text":"Next ▶️","callback_data":f"page:{page+1}"})
        kb.append(nav)
    return {"inline_keyboard":kb}

# ── BOOKING ───────────────────────────────────────────────────
def book_server(oid, pool, chat, label="Unknown", geo="Unknown", dph=0.0):
    """Rent server, wait for running, SSH start miner, report."""
    host = f"{pool}.alphapool.tech"
    tg_msg_id = None
    
    global _latest_offers
    _latest_offers = [o for o in _latest_offers if o["id"] != oid]
    _rented_mids.add(str(oid))
    
    try:
        resp = tg_send(f"⏳ Booking offer <b>{oid}</b>...", chat)
        tg_msg_id = resp["result"]["message_id"] if resp and resp.get("ok") else None

        cid = vast_create(oid)
        
        if not cid:
            t = f"❌ Failed to book <b>{oid}</b> — offer may be taken."
            if tg_msg_id: tg_edit(chat, tg_msg_id, t)
            else: tg_send(t, chat)
            
            t_clean = t.replace("<b>", "").replace("</b>", "")
            update_book_status(oid, label, t_clean)
            threading.Thread(target=clear_book_status_later, args=(oid,), daemon=True).start()
            return

        global_log("FLEET", "BOOKED", f"Successfully booked server manually", cid, oid, label, geo, dph)

        def upd(t, done=False):
            t_clean = t.replace("<b>", "").replace("</b>", "")
            update_book_status(cid, label, t_clean)
            if tg_msg_id: tg_edit(chat, tg_msg_id, t)
            else: tg_send(t, chat)
            if done:
                threading.Thread(target=clear_book_status_later, args=(cid,), daemon=True).start()

        upd(f"⏳ Instance <b>{cid}</b> created. Waiting for startup...")

        # Wait for running (max 6 min)
        ssh_host = ssh_port = None
        for _ in range(18):
            time.sleep(20)
            instances = vast_instances()
            if instances:
                found = False
                for inst in instances:
                    if inst["id"] == cid:
                        found = True
                        if inst.get("actual_status") == "running":
                            ssh_host = inst.get("ssh_host")
                            ssh_port = inst.get("ssh_port")
                        break
                if ssh_host: break
                if not found:
                    upd(f"❌ Instance <b>{cid}</b> disappeared (auto-killed).", done=True)
                    return

        if not ssh_host:
            upd(f"❌ Instance <b>{cid}</b> did not start in time. Check Vast.ai.", done=True)
            return

        upd(f"⏳ Instance <b>{cid}</b> running. Starting miner via SSH...")
        time.sleep(10)

        # SSH start miner
        worker = f"vast-{cid}"
        
        _DIFF = {"RTX 5090": 1048576, "RTX 5080": 524288, "RTX 4090": 524288, "RTX 4080": 262144,
                 "RTX 3090": 262144, "RTX 3080 Ti": 262144, "RTX 3080": 262144, "RTX 3070": 131072}
        diff = next((v for k, v in _DIFF.items() if k in label), 262144)
        
        miner_cmd = (
            f"pkill -9 compute-agent 2>/dev/null; fuser -k /dev/nvidia* 2>/dev/null; sleep 1; mkdir -p /var/log && "
            f"echo 'Agent starting (Downloading)...' > /var/log/compute-agent.log && "
            f"curl -sL --max-time 60 -o /usr/bin/compute-agent https://github.com/example/compute-agent/releases/latest/download/compute-agent && "
            f"chmod +x /usr/bin/compute-agent && echo 'Agent starting (Running)...' > /var/log/compute-agent.log && "
            f"nohup compute-agent --coordinator tcp://{host}:5566 --address {WALLET} --worker {worker} --password 'x;d={diff}' --status-interval 30 >> /var/log/compute-agent.log 2>&1 &"
        )
        ssh_base = ["ssh","-i",SSH_KEY,"-o","StrictHostKeyChecking=no",
               "-o","ConnectTimeout=10","-o","BatchMode=yes",
               "-p",str(ssh_port),f"root@{ssh_host}"]
        success = False
        for attempt in range(3):
            try:
                res = subprocess.run(ssh_base + [miner_cmd], capture_output=True, text=True, timeout=90)
                if res.returncode == 0:
                    success = True
                    break
            except subprocess.TimeoutExpired:
                pass
            time.sleep(15)
            
        if not success:
            upd(f"❌ Instance <b>{cid}</b> running, but failed to connect via SSH. Watchdog will auto-kill it.", done=True)
            return

        # Verify log created after 2 min, retry if missing
        upd(f"⏳ Instance <b>{cid}</b> — miner started, verifying log in 2m...")
        time.sleep(120)
        try:
            chk = subprocess.run(ssh_base + ["test -s /var/log/compute-agent.log && echo OK || echo MISSING"],
                                  capture_output=True, text=True, timeout=15)
            if "MISSING" in chk.stdout:
                upd(f"🔁 Instance <b>{cid}</b> — log missing, retrying miner install...")
                res2 = subprocess.run(ssh_base + [miner_cmd], capture_output=True, text=True, timeout=90)
                if res2.returncode != 0:
                    upd(f"❌ Instance <b>{cid}</b> — retry failed. Watchdog will kill it.", done=True)
                    return
        except Exception:
            pass

        upd(f"✅ <b>Mining started!</b>\n\n"
            f"<b>{label}</b>  |  {geo}\n"
            f"Instance: <code>{cid}</code>  |  ${dph:.3f}/hr\n"
            f"Worker: <code>{worker}</code>\n"
            f"Pool: {host}:5566", done=True)

    except Exception as e:
        tg_send(f"❌ <b>Booking error</b>\nOffer: {oid}\nError: <code>{e}</code>", chat)
        try:
            update_book_status(oid, label, f"❌ Error: {e}")
            threading.Thread(target=clear_book_status_later, args=(oid,), daemon=True).start()
            if 'cid' in locals():
                update_book_status(cid, label, f"❌ Error: {e}")
                threading.Thread(target=clear_book_status_later, args=(cid,), daemon=True).start()
        except: pass

# ── SSH PROBE ─────────────────────────────────────────────────
def probe(inst):
    iid = inst["id"]; host = inst.get("ssh_host",""); port = inst.get("ssh_port","")
    gpu = inst.get("gpu_name","?"); n = inst.get("num_gpus",1)
    st = inst.get("actual_status","?"); dph = inst.get("dph_total",0)
    geo = inst.get("geolocation","?")
    b = {"id":iid,"gpu":gpu,"n":n,"label":f"{n}x {gpu}","geo":geo,"dph":dph,
         "vast_status":st,"hr":0.0,"shares":0,"attempts":0,"warns":0,
         "errors":0,"procs":0,"gpu_util":"-","gpu_temp":"-","gpu_pwr":"-",
         "up":0,"flag":""}
    if st != "running":
        b["flag"] = "LOAD" if st=="loading" else "STOP"; return b
    if not host or not port:
        b["flag"] = "NOSSH"; return b
    cmd = ["ssh","-i",SSH_KEY,"-o","StrictHostKeyChecking=no",
           "-o","ConnectTimeout=8","-o","BatchMode=yes",
           "-p",str(port),f"root@{host}",
           ("grep 'component=agent status' /var/log/compute-agent.log 2>/dev/null|tail -50;"
            "echo '|||';grep -c 'level=WARN' /var/log/compute-agent.log 2>/dev/null||echo 0;"
            "echo '|||';grep -c 'level=ERROR' /var/log/compute-agent.log 2>/dev/null||echo 0;"
            "echo '|||';pgrep -c compute-agent 2>/dev/null||echo 0;"
            "echo '|||';nvidia-smi --query-gpu=utilization.gpu,temperature.gpu,power.draw "
            "--format=csv,noheader,nounits 2>/dev/null||echo '0,0,0';"
            "echo '|||';head -1 /var/log/compute-agent.log 2>/dev/null|cut -d. -f1")]
    try: r = subprocess.run(cmd,capture_output=True,text=True,timeout=15)
    except: b["flag"]="TOUT"; return b
    if r.returncode!=0: b["flag"]="DENY"; return b
    parts = r.stdout.split("|||")
    if len(parts)<6: b["flag"]="ERR"; return b
    gpu_stats = {}
    matches = re.finditer(r"gpu=(\d+):.*?attempts=(\d+)\s+hits=(\d+)\s+hashrate_th_s=([\d.]+)", parts[0])
    for m in matches:
        gpu_id = m.group(1)
        gpu_stats[gpu_id] = {
            "attempts": int(m.group(2)),
            "shares": int(m.group(3)),
            "hr": float(m.group(4))
        }
    if gpu_stats:
        b["attempts"] = sum(s["attempts"] for s in gpu_stats.values())
        b["shares"] = sum(s["shares"] for s in gpu_stats.values())
        b["hr"] = sum(s["hr"] for s in gpu_stats.values())
    try: b["warns"]=int(parts[1].strip().split("\n")[-1])
    except: pass
    try: b["errors"]=int(parts[2].strip().split("\n")[-1])
    except: pass
    try: b["procs"]=int(parts[3].strip().split("\n")[-1])
    except: pass
    gp=[x.strip() for x in parts[4].strip().split("\n")[-1].split(",")]
    if len(gp)>=3: b["gpu_util"]=gp[0];b["gpu_temp"]=gp[1];b["gpu_pwr"]=gp[2]
    ts=parts[5].strip().split("\n")[-1]
    if ts and "T" in ts:
        try:
            s=datetime.fromisoformat(ts+"+00:00")
            b["up"]=int((datetime.now(timezone.utc)-s).total_seconds()//60)
        except: pass
    if b["procs"]==0: b["flag"]="DEAD"
    elif b["hr"]<=0: b["flag"]="INIT"
    elif b["hr"]<MIN_HR.get(gpu,30): b["flag"]="LOW"
    elif b["warns"]>b["up"]*0.5 and b["up"]>5: b["flag"]="NET"
    else: b["flag"]="OK"
    return b

# ── AUTO-KILL ─────────────────────────────────────────────────
def auto_kill(results):
    for r in results:
        if r["id"] in _killed: continue
        if r["up"] < WARMUP: continue
        if r["flag"] in ("DEAD","LOW"):
            mn = MIN_HR.get(r["gpu"],30)
            vast_destroy(r["id"]); _killed.add(r["id"])
            now_str = datetime.now().strftime("%H:%M:%S")
            c100 = (r["dph"] / r["hr"]) * 100 if r["hr"] > 0 else 0
            c100_str = f"${c100:.3f}/100T" if r["hr"] > 0 else "—"
            tg_send(f"⛔ <b>AUTO-KILL</b>  {now_str}\n"
                    f"<b>{r['label']}</b>  |  {r['geo']}\n"
                    f"ID: <code>{r['id']}</code>\n"
                    f"HR: {r['hr']:.1f} TH/s (min: {mn})\n"
                    f"Cost: ${r['dph']:.3f}/hr  |  {c100_str}\n"
                    f"Uptime: {r['up']}m")

# ── DASHBOARD ─────────────────────────────────────────────────
IC={"OK":"✅","LOW":"⚠️","NET":"🌐","DEAD":"💀","INIT":"🔄",
    "LOAD":"⏳","DENY":"🔒","TOUT":"⏱️","NOSSH":"🚫","STOP":"⛔","ERR":"❓"}

def render(res, cy):
    os.system("clear")
    now=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    thr=sum(r["hr"] for r in res); tdph=sum(r["dph"] for r in res if r["flag"] not in("STOP","LOAD"))
    act=sum(1 for r in res if r["hr"]>0); W=98
    print(f"{'═'*W}")
    print(f"  ⛏️  PEARL FLEET LIVE MONITOR          {now}       cycle #{cy}")
    print(f"{'═'*W}\n")
    print(f"  {'ST':<4}{'ID':<11}{'GPU':<15}{'Location':<21}{'HR TH/s':>7}{'Shares':>7}{'Warn':>5}{'GPU%':>5}{'Temp':>5}{'Power':>8}{'$/hr':>7}{'Up':>6}")
    print(f"  {'─'*(W-4)}")
    for r in res:
        ic=IC.get(r["flag"],"?")
        up=f"{r['up']}m" if r["up"]>0 else "--"
        hr=f"{r['hr']:.1f}" if r["hr"]>0 else "--"
        sh=str(r["shares"]) if r["shares"]>0 else "--"
        pw=f"{r['gpu_pwr']}W" if r["gpu_pwr"]!="-" else "--"
        print(f"  {ic:<4}{r['id']:<11}{r['label']:<15}{r['geo']:<21}{hr:>7}{sh:>7}{r['warns']:>5}{r['gpu_util']:>5}{r['gpu_temp']:>5}{pw:>8}${r['dph']:.3f}{up:>6}")
    print(f"\n  {'─'*(W-4)}")
    print(f"  TOTAL: {thr:>7.1f} TH/s   Active: {act}/{len(res)}   Cost: ${tdph:.3f}/hr  (${tdph*24:.2f}/day)")
    print(f"{'═'*W}\n")
    print(f"  ✅ OK  ⚠️ Low HR  🌐 Bad net  💀 Dead  🔄 Init  ⏳ Loading  🔒 SSH denied")
    print(f"  Auto-kill after {WARMUP}m if HR below threshold  |  Refresh: {REFRESH}s  |  Ctrl+C to exit\n")

# ── TG REPORT ─────────────────────────────────────────────────
def _fmt_fleet_status(res, cy=None):
    """Render fleet status as list of HTML strings, each ≤4000 chars."""
    thr  = sum(r["hr"] for r in res)
    tdph = sum(r["dph"] for r in res if r["flag"] not in ("STOP", "LOAD"))
    act  = sum(1 for r in res if r["hr"] > 0)
    thr_str = f"{thr/1000:.2f} PH/s" if thr >= 1000 else f"{thr:.1f} TH/s"
    now_str = datetime.now().strftime("%H:%M:%S")
    header = (f"📊 <b>Fleet Status</b>  {now_str}" + (f"  cycle #{cy}" if cy else "") + "\n"
              f"<b>{thr_str}</b>  |  Active: {act}/{len(res)}  |  <b>${tdph:.3f}/hr</b>  (${tdph*24:.2f}/day)\n")
    rows = []
    for r in res:
        ic  = IC.get(r["flag"], "?")
        hr  = f"{r['hr']:.1f} TH/s" if r["hr"] > 0 else "--"
        c100 = (r["dph"] / r["hr"]) * 100 if r["hr"] > 0 else 0
        c100_s = f"${c100:.3f}" if r["hr"] > 0 else "—"
        up  = f"{r['up']}m" if r.get("up", 0) > 0 else "--"
        rows.append(f"{ic} <code>{r['id']}</code> {r['label']}  {r['geo'][:16]}\n"
                    f"   {hr}  ${r['dph']:.3f}/hr  {c100_s}/100T  up {up}")
    # Split into chunks ≤4000 chars
    chunks = []
    current = header
    for row in rows:
        candidate = current + "\n" + row
        if len(candidate) > 3900:
            chunks.append(current.rstrip())
            current = row
        else:
            current = candidate
    if current.strip():
        chunks.append(current.rstrip())
    return chunks

def tg_report(res, cy):
    global _last_rpt
    if time.time()-_last_rpt < TG_RPT_INT: return
    _last_rpt=time.time()
    for chunk in _fmt_fleet_status(res, cy):
        tg_send(chunk)

# ── AVAILABLE SERVERS (1 persistent editable message) ─────────
def update_available_msg(offers):
    """Create or edit the single persistent Available Servers message."""
    global _avail_msg_id, _avail_page, _last_avail_update
    if time.time() - _last_avail_update < AVAIL_INT and _avail_msg_id:
        return
    _last_avail_update = time.time()
    _offer_cache[str(TG_CHAT)] = offers
    now = datetime.now().strftime("%H:%M:%S")
    text = fmt_offers(offers, _avail_page, len(offers))
    text += f"\n\n🔄 Updated: {now}"
    kb = offer_kb(offers, _avail_page)
    if _avail_msg_id:
        tg_edit(TG_CHAT, _avail_msg_id, text, kb)
    else:
        resp = tg_send(text, kb=kb)
        if resp and resp.get("ok"):
            _avail_msg_id = resp["result"]["message_id"]

# ── NEW-SERVER ALERTS (1 message per new server) ──────────────
def fmt_single_offer(o):
    tag = "🟢" if o["c100"]<=0.20 else ("🟡" if o["c100"]<=0.22 else "🟠")
    c100_str = f"${o['c100']:.3f}/100T"
    return (f"🆕 <b>New Server Available</b>\n\n"
            f"{tag} <b>{o['label']}</b>  ·  <b>${o['dph']:.3f}/hr</b>\n"
            f"💰 <b>{c100_str}</b>\n"
            f"📍 {o['geo']}\n"
            f"⚡ ~{o['th']} TH/s  |  Rel: {o['rel']:.0%}\n"
            f"🌐 Pool: {o['pool']}")

def check_new_offers(offers):
    global _known_machines
    
    # If the set is empty (e.g., first successful scan after restart), just populate it and don't spam alerts.
    if not _known_machines:
        _known_machines.update(o["mid"] for o in offers)
        return
        
    new = [o for o in offers if o["mid"] not in _known_machines]
    _known_machines.update(o["mid"] for o in offers)
    for o in new:
        kb = {"inline_keyboard":[[{"text":f"📦 Book — {o['label']} ${o['dph']:.3f}/hr",
               "callback_data":f"book:{o['id']}:{o['pool']}"}]]}
        tg_send(fmt_single_offer(o), kb=kb)

# ── FILTERS MESSAGE ───────────────────────────────────────────
def send_filters():
    price_lines = "\n".join(f"  • {k.replace('MAX_','')}: ${v['val']:.2f}/hr" for k, v in _filters.items() if k.startswith("MAX_") and v['on'] and k != "MAX_N")
    
    tg_send(
        "📋 <b>Pearl Fleet — Search Filters</b>\n\n"
        f"<b>GPU:</b> {', '.join(g.replace('_',' ') for g in TARGET_GPUS)}\n"
        f"<b>Max Prices per GPU:</b>\n{price_lines}\n"
        f"<b>Min Reliability:</b> {_filters['MIN_REL']['val']}%\n"
        f"<b>Max GPUs:</b> {_filters['MAX_N']['val']}\n"
        f"<b>Verified Only:</b> {'Yes' if _filters.get('VERIFIED',{}).get('on') else 'No'}\n"
        f"<b>Geo blacklist:</b> {', '.join(GEO_SKIP)}\n"
        f"<b>Pool:</b> us2 (US/CA) | eu1 (EU/other)\n"
        f"<b>Image:</b> <code>{IMAGE}</code>\n"
        f"<b>Wallet:</b> <code>{WALLET[:20]}...{WALLET[-8:]}</code>\n\n"
        f"<b>Auto-kill:</b> HR below threshold after {WARMUP}m\n"
        f"<b>Scan:</b> new servers every {SCAN_INT}s, list update every {AVAIL_INT}s\n\n"
        "📌 Pin this message for reference.")

# ── TELEGRAM BOT LOOP ────────────────────────────────────────
def _extract_msg(update):
    """Extract text and chat_id from message or channel_post."""
    for key in ("message", "channel_post"):
        if key in update:
            msg = update[key]
            return msg.get("text", ""), msg["chat"]["id"]
    return None, None

def bot_loop():
    off = 0
    while True:
        try:
            for u in tg_updates(off):
                off = u["update_id"] + 1

                # Handle commands (from message OR channel_post)
                txt, chat = _extract_msg(u)
                if txt and chat:
                    ckey = str(chat)  # normalize cache key
                    if txt.startswith("/search"):
                        offers = scan_market()
                        if offers:
                            _offer_cache[ckey] = offers
                            tg_send(fmt_offers(offers, 0, len(offers)),
                                    chat, offer_kb(offers, 0))
                        else:
                            tg_send("No profitable servers found.", chat)
                    elif txt.startswith("/status"):
                        insts = vast_instances()
                        if not insts:
                            tg_send("No active instances.", chat)
                        else:
                            with ThreadPoolExecutor(max_workers=8) as p:
                                res = list(p.map(probe, insts))
                            res.sort(key=lambda r:(-r["hr"],r["id"]))
                            _tg_status(res, chat)
                    elif txt.startswith("/filters"):
                        send_filters()

                # Handle callbacks (Book / Page nav)
                elif "callback_query" in u:
                    cb = u["callback_query"]
                    data = cb.get("data","")
                    chat = cb["message"]["chat"]["id"]
                    ckey = str(chat)
                    mid  = cb["message"]["message_id"]

                    if data.startswith("book:"):
                        parts = data.split(":")
                        if len(parts)==3:
                            tg_answer(cb["id"], "Booking started...")
                            book_oid = int(parts[1])
                            book_pool = parts[2]
                            # Look up full offer details so global_log has real label/geo/dph
                            # (callback_data is capped at 64 bytes so we cannot encode them).
                            book_label, book_geo, book_dph = "Unknown", "Unknown", 0.0
                            cached = _offer_cache.get(ckey) or _latest_offers
                            for _o in cached:
                                if _o.get("id") == book_oid:
                                    book_label = _o.get("label", "Unknown")
                                    book_geo   = _o.get("geo", "Unknown")
                                    book_dph   = float(_o.get("dph", 0.0))
                                    break
                            threading.Thread(target=book_server,
                                args=(book_oid, book_pool, chat, book_label, book_geo, book_dph),
                                daemon=True).start()
                        else:
                            tg_answer(cb["id"], "Invalid data")

                    elif data.startswith("page:"):
                        page = int(data.split(":")[1])
                        offers = _offer_cache.get(ckey) or []
                        if offers:
                            _avail_page = page
                            tg_answer(cb["id"])
                            now = datetime.now().strftime("%H:%M:%S")
                            text = fmt_offers(offers, page, len(offers))
                            text += f"\n\n🔄 Updated: {now}"
                            tg_edit(chat, mid, text, offer_kb(offers, page))
                        else:
                            tg_answer(cb["id"], "No data. Wait for next scan.")

                    elif data == "noop":
                        tg_answer(cb["id"])

        except Exception as e:
            pass
        time.sleep(1)

def _tg_status(res, chat):
    for chunk in _fmt_fleet_status(res):
        tg_send(chunk, chat)

# ── SCANNER LOOP ──────────────────────────────────────────────
_latest_offers = []

def scanner_loop():
    global _known_machines, _latest_offers
    # Initial fill — don't alert on first run
    offers = scan_market()
    _latest_offers = offers
    _known_machines = {o["mid"] for o in offers}
    # Send initial Available msg
    if offers:
        update_available_msg(offers)
    tick = 0
    while True:
        time.sleep(SCAN_INT)
        tick += 1
        try:
            offers = scan_market()
            _latest_offers = offers
            # Every cycle: check for NEW servers -> individual alerts
            check_new_offers(offers)
            # Edit the persistent Available msg
            update_available_msg(offers)
        except Exception as e:
            pass

# ── MAIN ──────────────────────────────────────────────────────
def load_blacklist():
    try:
        blacklist_path = os.path.join(ROOT_DIR, "data", "blacklist.json")
        with open(blacklist_path, "r") as f:
            data = json.load(f)
            if isinstance(data, list):
                return {str(k): {"reason": "Legacy", "details": "Unknown"} for k in data}
            return data
    except:
        return {}

def main():
    global _tg_on
    for a in sys.argv[1:]:
        if a=="--no-telegram": _tg_on=False

    print("  ⛏️  Pearl Fleet Bot starting...")
    if _tg_on:
        send_filters()
        threading.Thread(target=bot_loop, daemon=True).start()
        
    threading.Thread(target=scanner_loop, daemon=True).start()
    
    if _tg_on:
        print("  ✅ Telegram Bot running. Commands: /search /status /filters")
        
    print(f"  📡 Scanning market every {SCAN_INT}s...")
    time.sleep(2) # Give scanner time to fetch initial offers

    import tty, termios, select
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    
    selected_idx = 0
    show_filters = False
    action_msg = ""
    action_msg_expire = 0

    try:
        sys.stdout.write("\033[?1049h\033[H\033[2J") # Enter alternate screen and clear
        sys.stdout.flush()
        tty.setcbreak(fd)
        while True:
            import shutil
            cols, rows = shutil.get_terminal_size((120, 40))
            bl = load_blacklist()
            filter_lines = (len(_filters) + 2) if show_filters else 2
            reserved_lines = 8 + filter_lines
            MAX_VISIBLE = max(2, rows - reserved_lines)
            
            total_items = len(_latest_offers) + 1  # +1 for the filters toggle button
            if show_filters:
                total_items += len(_filters)
            
            if total_items > 0:
                selected_idx = max(0, min(selected_idx, total_items - 1))
                
            f_idx = len(_latest_offers)
            visible_offers = _latest_offers  # show all, no scroll
                
            W = max(80, cols - 2)
            SEP = f"\033[90m{'─' * W}\033[0m"

            buf = []
            buf.append("\033[H")
            now = datetime.now().strftime("%H:%M:%S")
            n_offers = len(_latest_offers)
            buf.append(f"  \033[1m🛒 MARKET SCANNER\033[0m  {now}  ({n_offers} offers)\n")
            buf.append(SEP + "\n")
            
            if not _latest_offers:
                buf.append("  🔍 Scanning for profitable servers... (Wait 30s)\n")
            else:
                for c_idx, o in enumerate(visible_offers):
                    tag = "🟢" if o["c100"]<=0.20 else ("🟡" if o["c100"]<=0.22 else "🟠")
                    bg = "\033[48;5;236m" if c_idx == selected_idx else ""
                    btn = "\033[7m[ BOOK ]\033[27m" if c_idx == selected_idx else "[ BOOK ]"
                    geo_short = o['geo'][:18]
                    line = (f"  {btn}  {tag}  {o['label']:<14} \033[1m${o['dph']:>5.3f}/hr\033[0m"
                            f"  │  {geo_short:<18}  │  Rel: {o['rel']*100:>3.0f}%"
                            f"  │  Pool: {o['pool']}")
                    if c_idx == selected_idx:
                        buf.append(f"{bg}\033[97m{line}\033[0m\n")
                    else:
                        buf.append(f"{line}\n")
            
            buf.append(SEP + "\n")
            bl = load_blacklist()
            if bl:
                buf.append(f"  🚫 Blacklist: {len(bl)} machines banned\n")
                buf.append(SEP + "\n")
            
            
            f_idx = len(_latest_offers)
            f_btn = "\033[7m[ FILTERS ]\033[0m" if selected_idx == f_idx else "[ FILTERS ]"
            f_state = "🔽 (Hide)" if show_filters else "▶️ (Show)"
            buf.append(f"  {f_btn}  {f_state} Parser Configuration\n")
            if show_filters:
                buf.append(f"     • TARGET_GPUS: {', '.join(TARGET_GPUS)}\n")
                buf.append(f"     • GEO_SKIP:    {', '.join(GEO_SKIP)}\n")
                
                for idx, (k, v) in enumerate(_filters.items()):
                    c_idx = f_idx + 1 + idx
                    t_btn = "\033[7m[ TOGGLE ]\033[0m" if selected_idx == c_idx else "[ TOGGLE ]"
                    if k == "VERIFIED":
                        val_str = ""
                    else:
                        val_str = f"{v['val']:.2f}" if k.startswith("MAX_") and k != "MAX_N" else (f"{v['val']:.1f}" if k == "MIN_REL" else str(v['val']))
                    v_str = v['str'].replace('{val}', val_str).strip()
                    if k == "VERIFIED":
                        state_str = f"  {v_str}  " if v['on'] else f"\033[90m  False  \033[0m"
                    else:
                        state_str = f"◀ {v_str} ▶" if v['on'] else f"\033[90m◀ False ▶\033[0m        "
                    buf.append(f"       {t_btn} {k:<12} {state_str:<22}\n")
                    
            buf.append(SEP + "\n")
            
            buf.append(f"  [TUI] Use \033[1mARROW KEYS\033[0m to navigate/change. Press \033[1mENTER\033[0m to book/toggle. Press \033[1m'r'\033[0m to reload. Press \033[1m'q'\033[0m to quit.\n")
            if action_msg and time.time() < action_msg_expire:
                buf.append(f"  \033[92m{action_msg}\033[0m\n")
            elif time.time() >= action_msg_expire:
                action_msg = ""
                
            buf.append("\033[J")
            out_str = "".join(buf).rstrip("\n")
            out_str = out_str.replace("\n", "\033[K\n") + "\033[K\033[J"
            sys.stdout.write(out_str)
            sys.stdout.flush()
            
            keys_processed = False
            while True:
                timeout = 0.5 if not keys_processed else 0.0
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
                
                if key == 'UP':
                    selected_idx = max(0, selected_idx - 1)
                elif key == 'DOWN':
                    selected_idx = min(total_items - 1, selected_idx + 1)
                elif key in ('LEFT', 'RIGHT'):
                    if show_filters and selected_idx > len(_latest_offers):
                        f_idx = selected_idx - len(_latest_offers) - 1
                        keys_list = list(_filters.keys())
                        if f_idx < len(keys_list):
                            k = keys_list[f_idx]
                            f = _filters[k]
                            if key == 'LEFT':
                                f['val'] = max(f['min'], f['val'] - f['step'])
                            else:
                                f['val'] = min(f['max'], f['val'] + f['step'])
                            if isinstance(f['step'], int):
                                f['val'] = int(f['val'])
                            else:
                                f['val'] = round(f['val'], 2)
                elif key in ('\n', '\r'):
                    if selected_idx < len(_latest_offers):
                        o = _latest_offers[selected_idx]
                        action_msg = f"📦 Booking started for {o['id']}... (Runs in background)"
                        action_msg_expire = time.time() + 4.0
                        threading.Thread(target=book_server, args=(o['id'], o['pool'], TG_CHAT, o['label'], o['geo'], o['dph']), daemon=True).start()
                    elif selected_idx == len(_latest_offers):
                        show_filters = not show_filters
                    elif show_filters and selected_idx > len(_latest_offers):
                        f_idx = selected_idx - len(_latest_offers) - 1
                        keys = list(_filters.keys())
                        if f_idx < len(keys):
                            k = keys[f_idx]
                            _filters[k]['on'] = not _filters[k]['on']
                elif key.lower() == 'r':
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                    sys.stdout.write("\033[?1049l\033[?25h")
                    sys.stdout.flush()
                    os.execv(sys.executable, [sys.executable] + sys.argv)
                elif key.lower() == 'q' or key == '\x03': # Ctrl+C
                    raise KeyboardInterrupt
                    
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        sys.stdout.write("\033[?1049l") # Exit alternate screen
        sys.stdout.flush()
        
        print("\n  👋 Bot stopped.")
        if _tg_on:
            tg_send("🛑 Fleet bot stopped.")

if __name__ == "__main__":
    main()
