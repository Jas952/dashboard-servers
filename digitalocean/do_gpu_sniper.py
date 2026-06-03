#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DO GPU Sniper — H100/H200 (NVIDIA) + MI300X (AMD)
Fast poll + parallel region create on availability.
Telegram notifications on every event.

Usage:
  python do_gpu_sniper.py              -- all targets, rent first available
  python do_gpu_sniper.py --nvidia     -- only NVIDIA (H100/H200)
  python do_gpu_sniper.py --amd        -- only AMD MI300X
  python do_gpu_sniper.py --8x         -- prefer 8-card configs
  python do_gpu_sniper.py --h100       -- only H100
  python do_gpu_sniper.py --h200       -- only H200
"""

import sys
import time
import json
import re
import urllib.request
import urllib.parse
import ssl
import threading
import shutil
import os
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

# ─────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────
API_KEYS = [
    "YOUR_DO_API_TOKEN",
    "YOUR_DO_API_TOKEN",
    "YOUR_DO_API_TOKEN",
    "YOUR_DO_API_TOKEN",
    "YOUR_DO_API_TOKEN",
    "YOUR_DO_API_TOKEN",
    "YOUR_DO_API_TOKEN",
    "YOUR_DO_API_TOKEN",
    "YOUR_DO_API_TOKEN",
    "YOUR_DO_API_TOKEN",
    "YOUR_DO_API_TOKEN",
    "YOUR_DO_API_TOKEN",
    "YOUR_DO_API_TOKEN",
    "YOUR_DO_API_TOKEN",
    "YOUR_DO_API_TOKEN",
    "YOUR_DO_API_TOKEN",
    "YOUR_DO_API_TOKEN",
    "YOUR_DO_API_TOKEN",
    "YOUR_DO_API_TOKEN",
    "YOUR_DO_API_TOKEN"
]
WALLET  = "YOUR_WALLET_ADDRESS"
POOL_HOST = "eu1.alphapool.tech"
POOL_PORT = 5566
POLL_INTERVAL = 1.5   # 1.5s to save GET limits (2400/hr)
POLL_OFFSET   = 0.0   # phase offset on the wall-clock grid; #2 uses 0.6 to interleave
ACCOUNT_NAME = "Аккаунт #1"

# Account GPU-card limit. 8x configs need 8 cards; if > GPU_LIMIT they always
# fail with HTTP 422 "exceed your droplet limit". Filter them out.
GPU_LIMIT = 1

# Anti-ban rest: pause SLEEP_DURATION every SLEEP_EVERY, aligned to wall-clock.
# #1 rests at :00, #2 at :15 (SLEEP_PHASE=900) so one bot is always awake.
SLEEP_EVERY    = 1800  # rest cycle length (s) — every 30 min
SLEEP_DURATION = 60    # rest duration (s) — 1 min
SLEEP_PHASE    = 0     # offset of the rest window within the cycle

# Retry: DO often reclaims a GPU droplet during provisioning (create errored).
# On reclaim, re-create immediately in a tight burst to grab the flickering slot.
PROVISION_RETRIES = 6    # rapid re-create attempts on reclaim or network error
ACTIVE_TIMEOUT    = 150  # max seconds to wait for 'active' per attempt
ACTIVE_POLL       = 2.0  # poll interval while waiting for active (faster loss detect)

# Telegram
TG_BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
TG_CHAT_ID   = "-1002313274238"

REGIONS_PRIORITY = [
    "nyc2", "nyc3", "nyc1", "sfo3", "fra1", "ams3",
    "sgp1", "lon1", "tor1", "syd1", "blr1", "atl1", "sfo2", "ric1",
]
PARALLEL_REGIONS = 6   # restored to 6 parallel regions

# ── GPU Targets ──────────────────────────────────────
# slug: (label, $/hr, vendor, worker_suffix, difficulty)
TARGETS = {
    "gpu-h100x1-80gb":    ("1x H100 NVIDIA",   2.49, "nvidia", "h100x1",  1048576),
    "gpu-h100x8-640gb":   ("8x H100 NVIDIA",  19.92, "nvidia", "h100x8",  1048576),
    "gpu-h200x1-141gb":   ("1x H200 NVIDIA",   3.44, "nvidia", "h200x1",  1048576),
    "gpu-h200x8-1128gb":  ("8x H200 NVIDIA",  27.52, "nvidia", "h200x8",  1048576),
    "gpu-mi300x1-192gb":  ("1x MI300X AMD",    1.99, "amd",    "mi300x1", 524288),
    "gpu-mi300x8-1536gb": ("8x MI300X AMD",   15.92, "amd",    "mi300x8", 524288),
}

MINER_NVIDIA_URL = "https://pearl.alphapool.tech/downloads/alpha-miner-1.7.5-beta"
MINER_AMD_URL    = "https://github.com/AlphaMine-Tech/alpha-miner/releases/download/amd-v1.0.0/alpha-miner-amd"

BASE_URL = "https://api.digitalocean.com/v2"
HEADERS  = {"Content-Type": "application/json"}
SESSION  = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20)
SESSION.mount('https://', adapter)
SESSION.mount('http://', adapter)
SESSION.headers.update(HEADERS)

TOKEN_LOCK = threading.Lock()
RATE_LIMITS = {k: 0 for k in API_KEYS}
_token_idx = 0  # round-robin pointer


C_GREEN = "\033[32m"
C_RED = "\033[31m"
C_BORDER = "\033[90m"
C_RESET = "\033[0m"

_sys_logs = []
_success_logs = []
_error_logs = []  # POST errors table
_log_lock = threading.RLock()
_scan_status = "Initializing..."
_ui_running = True
GHOST_COOLDOWNS = {}

# ── Missed-opportunity log (real misses only) ────────
MISSED_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"do_missed_{os.path.splitext(os.path.basename(__file__))[0]}.json")
MISSED_THROTTLE = 300  # default seconds; avoid spamming same key
# Per-reason throttle (s). limit_full/rate_limited are account-global & chronic → rare alerts.
REASON_THROTTLE = {"lost_after_create": 300, "limit_full": 3600, "rate_limited": 1800,
                   "account_limit": 1800, "api_error": 600, "reclaimed": 120}
# Reasons deduped per-account (ignore slug) so all 6 GPU types share one alert.
GLOBAL_REASONS = {"limit_full", "rate_limited"}
_missed_lock = threading.Lock()
_missed_last = {}

def log_sys(msg):
    with _log_lock:
        _sys_logs.append(msg)
        if len(_sys_logs) > 20:
            _sys_logs.pop(0)

def log_success(msg):
    with _log_lock:
        _success_logs.append(msg)
        if len(_success_logs) > 10:
            _success_logs.pop(0)

def log_post_error(tryno, slug, region, err):
    """Record a POST-level error for the UI error table."""
    ts = time.strftime("%H:%M:%S")
    err_short = str(err)[:80].replace('\n', ' ')
    label = TARGETS.get(slug, (slug,))[0]
    entry = f"[{ts}] try {tryno} | {label[:14]} | {region[:6]} | {err_short}"
    with _log_lock:
        _error_logs.append(entry)
        if len(_error_logs) > 15:
            _error_logs.pop(0)

def log_missed(slug, region, reason, detail="", telegram=False):
    """Record a missed opportunity. Always logs to file (throttled); sends
    Telegram only when telegram=True. Chronic reasons dedupe per-account."""
    now = time.time()
    key = (reason,) if reason in GLOBAL_REASONS else (slug, reason)
    throttle = REASON_THROTTLE.get(reason, MISSED_THROTTLE)
    with _missed_lock:
        if now - _missed_last.get(key, 0) < throttle:
            return
        _missed_last[key] = now
        label = TARGETS.get(slug, (slug,))[0]
        entry = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "account": ACCOUNT_NAME,
            "slug": slug,
            "label": label,
            "region": region,
            "reason": reason,
            "detail": str(detail)[:300],
        }
        try:
            data = []
            if os.path.exists(MISSED_LOG_FILE):
                with open(MISSED_LOG_FILE, "r") as f:
                    data = json.load(f)
            data.append(entry)
            with open(MISSED_LOG_FILE, "w") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log_sys(f"[!] missed-log write failed: {e}")
    log_sys(f"[MISS] {label} {region} — {reason}: {str(detail)[:60]}")
    if not telegram:
        return
    if reason == "limit_full":
        tg_send(
            f"⛔ <b>GPU НЕДОСТУПЕН [{ACCOUNT_NAME}]</b>\n"
            f"Аккаунт не может создавать GPU-дроплеты — квота исчерпана/равна 0.\n"
            f"Нужно запросить <b>GPU access</b> в панели DigitalOcean.\n"
            f"📝 {str(detail)[:200]}"
        )
    elif reason == "reclaimed":
        tg_send(
            f"🟡 <b>СЛОТ ОТОЗВАН [{ACCOUNT_NAME}]</b>\n"
            f"🎮 {label}\n"
            f"📍 {region}\n"
            f"DO создал дроплет, но отозвал на провижене (errored).\n"
            f"📝 {str(detail)[:200]}"
        )
    else:
        tg_send(
            f"🟠 <b>УПУЩЕН СЛОТ [{ACCOUNT_NAME}]</b>\n"
            f"🎮 {label}\n"
            f"📍 Регион: <code>{region}</code>\n"
            f"❗ Причина: {reason}\n"
            f"📝 {str(detail)[:200]}"
        )

def draw_ui():
    cols, rows = shutil.get_terminal_size((120, 40))
    W = max(80, cols - 2)
    SEP = f"{C_BORDER}{'─' * W}{C_RESET}"
    now_str = datetime.now().strftime("%H:%M:%S")

    sys.stdout.write("\033[H")
    out = []
    
    out.append(f"  DO GPU SNIPER  [{C_GREEN}RUNNING{C_RESET}]  \033[90m{now_str}\033[0m\033[K")
    out.append(SEP + "\033[K")
    
    out.append(f"  \033[1mAPI KEYS STATUS\033[0m\033[K")
    with TOKEN_LOCK:
        now = time.time()
        for i, k in enumerate(API_KEYS):
            short_k = f"{k[:10]}...{k[-6:]}"
            wait = max(0, int(RATE_LIMITS[k] - now))
            if wait > 0:
                st = f"{C_RED}COOLDOWN ({wait}s){C_RESET}"
            else:
                st = f"{C_GREEN}READY{C_RESET}"
            out.append(f"  Key {i+1:2d} [{short_k}]: {st}\033[K")
    
    if _success_logs:
        out.append(SEP + "\033[K")
        out.append(f"  \033[1m\033[32mSUCCESS RENTS\033[0m\033[K")
        with _log_lock:
            for l in _success_logs:
                out.append(f"  {C_GREEN}{l}{C_RESET}\033[K")

    out.append(SEP + "\033[K")
    out.append(f"  \033[1m\033[31mPOST ERRORS\033[0m  \033[90m(последние 15 ошибок при попытке создания дроплета)\033[0m\033[K")
    with _log_lock:
        show_err = _error_logs[-15:]
        if not show_err:
            out.append(f"  \033[90m  — нет ошибок\033[0m\033[K")
        else:
            for l in show_err:
                out.append(f"  {C_RED}{l}{C_RESET}\033[K")

    out.append(SEP + "\033[K")
    out.append(f"  \033[1mSYSTEM LOGS\033[0m\033[K")
    with _log_lock:
        show_sys = _sys_logs[-15:]
        while len(show_sys) < 15:
            show_sys.append("")
        for l in show_sys:
            out.append(f"  {l}\033[K")

    out.append(SEP + "\033[K")
    with _log_lock:
        out.append(f"  {_scan_status}\033[K")

    sys.stdout.write("\r\n".join(out) + "\033[J\r\n")
    sys.stdout.flush()

def ui_thread_func():
    while _ui_running:
        draw_ui()
        time.sleep(0.3)

def get_best_token():
    global _token_idx
    with TOKEN_LOCK:
        now = time.time()
        n = len(API_KEYS)
        # Round-robin: try each token starting from current pointer
        for i in range(n):
            idx = (_token_idx + i) % n
            k = API_KEYS[idx]
            if RATE_LIMITS[k] < now:
                _token_idx = (idx + 1) % n  # advance pointer
                return k, 0
        # All tokens rate-limited — return the one expiring soonest
        best_token = min(API_KEYS, key=lambda k: RATE_LIMITS[k])
        return best_token, max(0, RATE_LIMITS[best_token] - now)

def set_token_rate_limit(token, wait_time):
    with TOKEN_LOCK:
        RATE_LIMITS[token] = time.time() + wait_time

# Our SSH key ID on DigitalOcean
SSH_KEY_ID = 56821200

# SSL context for Telegram (no verify)
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


# ─────────────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────────────
def tg_send(text):
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=10, context=_ssl_ctx) as resp:
            return json.loads(resp.read().decode()).get("ok", False)
    except Exception:
        return False


# ─────────────────────────────────────────────────────
# CLOUD-INIT (auto-install miner via DO, no SSH needed)
# ─────────────────────────────────────────────────────
def make_cloud_init(vendor, worker_suffix, difficulty):
    worker = f"do-{worker_suffix}"
    if vendor == "nvidia":
        miner_url, miner_bin = MINER_NVIDIA_URL, "alpha-miner"
        gpu_deps = "nvidia-headless-550-server nvidia-utils-550-server"
        gpu_wait = """
for i in $(seq 1 90); do nvidia-smi &>/dev/null && break; sleep 5; done
"""
    else:
        miner_url, miner_bin = MINER_AMD_URL, "alpha-miner-amd"
        gpu_deps = ""
        gpu_wait = ""

    return f"""#!/bin/bash
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq && apt-get install -y -qq curl {gpu_deps}
{gpu_wait}
curl -fsSL -L -o /usr/local/bin/{miner_bin} "{miner_url}" && chmod +x /usr/local/bin/{miner_bin}
cat > /etc/systemd/system/alpha-miner.service << 'EOF'
[Unit]
Description=AlphaPool Pearl Miner ({vendor.upper()})
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=root
ExecStart=/usr/local/bin/{miner_bin} --pool stratum+tcp://{POOL_HOST}:{POOL_PORT} --address {WALLET} --worker {worker} --password "x;d={difficulty}" --status-interval 30
Restart=always
RestartSec=15s
StandardOutput=append:/var/log/alpha-miner.log
StandardError=append:/var/log/alpha-miner.log
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload && systemctl enable alpha-miner && systemctl start alpha-miner
"""


def build_cloud_init_cache(slugs):
    cache = {}
    for slug in slugs:
        _, _, vendor, worker_sfx, diff = TARGETS[slug]
        cache[slug] = make_cloud_init(vendor, worker_sfx, diff)
    return cache


# ─────────────────────────────────────────────────────
# API HELPERS
# ─────────────────────────────────────────────────────
def api_get(path, params=None):
    while True:
        token, wait = get_best_token()
        if wait > 0:
            time.sleep(min(1, wait))
            continue
            
        headers = {"Authorization": f"Bearer {token}"}
        try:
            r = SESSION.get(f"{BASE_URL}{path}", params=params, headers=headers, timeout=20)
            if r.status_code == 429:
                wait_time = int(r.headers.get("Retry-After", 10))
                log_sys(f"[429] Token rate limited, switching...")
                set_token_rate_limit(token, wait_time)
                continue
            r.raise_for_status()
            return r.json()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            time.sleep(1)


def try_create_once(slug, region, user_data):
    token, wait = get_best_token()
    if wait > 0:
        return None, "All tokens rate limited globally"
    body = {
        "name": f"m-{slug[:10]}-{region}",
        "region": region,
        "size": slug,
        "image": "ubuntu-22-04-x64",
        "ssh_keys": [SSH_KEY_ID],
        "user_data": user_data,
        "backups": False,
        "ipv6": False,
        "tags": ["gpu"],
    }
    headers = {"Authorization": f"Bearer {token}"}
    try:
        r = SESSION.post(f"{BASE_URL}/droplets", json=body, headers=headers, timeout=25)
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
        return None, str(e)
        
    if r.status_code == 429:
        wait_time = int(r.headers.get("Retry-After", 10))
        set_token_rate_limit(token, wait_time)
        return None, f"HTTP 429: Token rate limited, wait {wait_time}s"
        
    if r.status_code in (200, 201, 202):
        return r.json()["droplet"]["id"], None
    try:
        err = r.json().get("message", r.text)
    except Exception:
        err = r.text
    return None, f"HTTP {r.status_code}: {err}"


def race_create(slug, regions, user_data):
    """Fire create in parallel across regions; first success wins."""
    regions = list(dict.fromkeys(regions))[:PARALLEL_REGIONS]
    if not regions:
        return None, None, "no regions"

    winners = []
    errors = []

    def attempt(region):
        now = time.time()
        if GHOST_COOLDOWNS.get((slug, region), 0) > now:
            return region, None, "micro-cooldown"
        did, err = try_create_once(slug, region, user_data)
        if err:
            err_str = str(err).lower()
            if "not available" in err_str or "capacity" in err_str or "unprocessable" in err_str:
                GHOST_COOLDOWNS[(slug, region)] = now + 30.0
        return region, did, err

    with ThreadPoolExecutor(max_workers=len(regions)) as pool:
        futures = [pool.submit(attempt, r) for r in regions]
        for fut in as_completed(futures):
            region, did, err = fut.result()
            if did:
                winners.append((did, region))
            elif err:
                errors.append(err)

    if not winners:
        err = errors[0] if errors else "unknown error"
        return None, None, err

    did, region = winners[0]
    # Destroy accidental duplicates
    for extra_id, extra_reg in winners[1:]:
        log_sys(f"[!] Extra droplet {extra_id} in {extra_reg} — destroying")
        token, _ = get_best_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        SESSION.delete(f"{BASE_URL}/droplets/{extra_id}", headers=headers, timeout=15)
    return did, region, None


def wait_for_active(droplet_id, timeout=ACTIVE_TIMEOUT, poll=ACTIVE_POLL):
    log_sys(f"[...] Waiting active (max {timeout}s)...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        token, wait = get_best_token()
        if wait > 0:
            time.sleep(min(poll, wait))
            continue
        headers = {"Authorization": f"Bearer {token}"}
        try:
            r = SESSION.get(f"{BASE_URL}/droplets/{droplet_id}", headers=headers, timeout=15)
        except Exception:
            time.sleep(poll)
            continue
        if r.status_code == 404:
            log_sys(f"[!] Droplet {droplet_id} gone (404)")
            return None
        if r.status_code == 429:
            wait_time = int(r.headers.get("Retry-After", 10))
            set_token_rate_limit(token, wait_time)
            continue
        if r.status_code >= 400:
            time.sleep(poll)
            continue
        r.raise_for_status()
        d = r.json()["droplet"]
        if d["status"] == "active":
            ip = next((n["ip_address"] for n in d["networks"]["v4"] if n["type"] == "public"), None)
            log_sys(f"[OK] Active, IP={ip}")
            return ip
        time.sleep(poll)
    log_sys("[!] Timeout waiting for active")
    return None


def cleanup_droplet(did):
    """Best-effort destroy of a leftover droplet that failed to go active."""
    try:
        token, _ = get_best_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        SESSION.delete(f"{BASE_URL}/droplets/{did}", headers=headers, timeout=15)
    except Exception:
        pass


def rent_with_retry(slug, label, regions, user_data, ts):
    """Create + wait for active. If DO reclaims the droplet during provisioning
    (create errored), re-create immediately in a tight burst. Returns
    ((did, region, ip), None) on success, or (None, last_err) on failure."""
    last_err = None
    notified = False
    for tryno in range(1, PROVISION_RETRIES + 1):
        did, region, err = race_create(slug, regions, user_data)
        if not did:
            last_err = err
            # Network errors (SSL/connection abort/timeout) → retry; capacity gone → stop
            err_low = str(err).lower()
            is_network_err = any(x in err_low for x in (
                "ssleoferror", "eof occurred", "connection aborted",
                "remotedisconnected", "remote end closed", "read timed out",
                "connectionerror", "max retries exceeded"
            ))
            log_post_error(tryno, slug, ",".join(regions[:PARALLEL_REGIONS]), err)
            if is_network_err:
                log_sys(f"[{ts}] [~] Network error on try {tryno}, retrying… ({err_low[:80]})")
                continue
            break  # POST-level failure (capacity gone) — a burst won't help
        if not notified:
            notified = True
            tg_send(
                f"🎯 <b>Поймал окно! [{ACCOUNT_NAME}]</b>\n"
                f"🎮 {label} | 📍 {region}\n"
                f"🆔 <code>{did}</code>\n"
                f"⏳ Закрепляю (до {PROVISION_RETRIES} попыток)…"
            )
        log_success(f"[{ts}] 🎯 Created {label} {region} (ID={did}) — try {tryno}/{PROVISION_RETRIES}, waiting active…")
        ip = wait_for_active(did)
        if ip:
            return (did, region, ip), None
        cleanup_droplet(did)
        last_err = f"reclaimed during provisioning (try {tryno}/{PROVISION_RETRIES})"
        log_sys(f"[{ts}] [!] {label} {did} reclaimed by DO — retry {tryno}/{PROVISION_RETRIES}")
    return None, last_err


def order_regions(avail, all_ordered):
    if avail:
        out = [r for r in all_ordered if r in avail]
        for r in avail:
            if r not in out:
                out.append(r)
        return out
    return list(all_ordered)


# ─────────────────────────────────────────────────────
# TARGET LIST BUILDER
# ─────────────────────────────────────────────────────
def gpu_count(slug):
    """Number of GPU cards in a config, parsed from its worker suffix (e.g. h100x8 -> 8)."""
    m = re.search(r"x(\d+)$", TARGETS[slug][3])
    return int(m.group(1)) if m else 1


def build_target_list():
    args = sys.argv[1:]
    # Drop configs that need more GPUs than the account is allowed to create.
    slugs = [s for s in TARGETS if gpu_count(s) <= GPU_LIMIT]
    if "--nvidia" in args:
        slugs = [s for s in slugs if TARGETS[s][2] == "nvidia"]
    elif "--amd" in args:
        slugs = [s for s in slugs if TARGETS[s][2] == "amd"]
    if "--h100" in args:
        slugs = [s for s in slugs if "h100" in s]
    elif "--h200" in args:
        slugs = [s for s in slugs if "h200" in s]
    if "--8x" in args:
        slugs = sorted(slugs, key=lambda s: (0 if "x8" in s else 1))
    else:
        # Default: NVIDIA (H100/H200) first, then sort by price
        slugs = sorted(slugs, key=lambda s: (0 if TARGETS[s][2] == "nvidia" else 1, TARGETS[s][1]))
    return slugs


# ─────────────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────────────
def _main_loop():
    global _scan_status
    targets = build_target_list()
    cloud_cache = build_cloud_init_cache(targets)

    log_sys("=" * 62)
    log_sys("  DO GPU Sniper — OPTIMIZED FOR 1 ACCOUNT")
    log_sys("=" * 62)
    log_sys(f"  Poll: {POLL_INTERVAL}s | Parallel regions: {PARALLEL_REGIONS}")
    for slug in targets:
        label, price, vendor, _, _ = TARGETS[slug]
        log_sys(f"    [{vendor.upper():6s}] {slug}  ${price:.2f}/hr")

    tg_send(
        f"🟢 <b>DO GPU Sniper запущен [{ACCOUNT_NAME}]</b>\n"
        f"⚡ Турбо-режим: {POLL_INTERVAL}с опрос | {PARALLEL_REGIONS} параллельных регионов\n"
        f"🎯 Целей: {len(targets)} типов GPU\n"
        f"{''.join(f'  • {TARGETS[s][0]}' + chr(10) for s in targets)}"
    )

    # Get all available regions from DO
    all_regions = {r["slug"] for r in api_get("/regions", {"per_page": 100}).get("regions", []) if r.get("available")}
    ordered_regions = [r for r in REGIONS_PRIORITY if r in all_regions]
    for r in all_regions:
        if r not in ordered_regions:
            ordered_regions.append(r)

    attempt = 0
    while True:
        attempt += 1
        ts = time.strftime("%H:%M:%S")

        # Anti-ban rest window, aligned to the wall-clock cycle.
        if SLEEP_DURATION > 0:
            pos = (time.time() - SLEEP_PHASE) % SLEEP_EVERY
            if pos < SLEEP_DURATION:
                nap = SLEEP_DURATION - pos
                _scan_status = f"[{ts}] 😴 Отдых {int(nap)}с (анти-бан)"
                log_sys(f"[{ts}] 😴 Rest {int(nap)}s (anti-ban cooldown)")
                time.sleep(nap)
                continue

        try:
            sizes = api_get("/sizes", {"per_page": 200})
            sizes = {s["slug"]: s for s in sizes.get("sizes", [])}
        except KeyboardInterrupt:
            log_sys("[!] Stopped.")
            return
        except Exception as e:
            log_sys(f"[{ts}] API error: {e}")
            time.sleep(2)
            continue

        rented = False
        for slug in targets:
            s = sizes.get(slug, {})
            if not s.get("available"):
                continue

            label, price, vendor, _, _ = TARGETS[slug]
            user_data = cloud_cache[slug]
            # DO API bug: GPU sizes return empty regions list, try all
            regions = order_regions(s.get("regions") or [], ordered_regions)

            log_sys(f"[{ts}] 🎯 SLOT: {label} (${price}/hr) — racing {regions[:PARALLEL_REGIONS]}...")

            result, err = rent_with_retry(slug, label, regions, user_data, ts)
            if result:
                did, region, ip = result
                tg_send(
                    f"✅✅✅ <b>GPU АРЕНДОВАНА И ЗАПУЩЕНА! [{ACCOUNT_NAME}]</b>\n"
                    f"🎮 {label} | ${price}/hr\n"
                    f"📍 Регион: <code>{region}</code>\n"
                    f" IP: <code>{ip}</code>\n"
                    f"🆔 <code>{did}</code>\n"
                    f"🔑 SSH: <code>ssh root@{ip}</code>\n"
                    f"📋 Логи: <code>journalctl -u alpha-miner -f</code>\n"
                    f"⛏ Майнер ставится автоматически (cloud-init)"
                )
                log_success("=" * 62)
                log_success(f"[SUCCESS] {label}")
                log_success(f"Region:  {region}")
                log_success(f"IP:      {ip}")
                log_success(f"ID:      {did}")
                log_success(f"SSH:     ssh root@{ip}")
                log_success("=" * 62)
                return
            if err:
                el = str(err).lower()
                if "reclaim" in el:
                    # Caught a window but DO reclaimed it after all retries.
                    log_success(f"[{ts}] ⚠️ {label}: пойман, но DO отозвал после {PROVISION_RETRIES} попыток")
                    log_missed(slug, ",".join(regions[:PARALLEL_REGIONS]), "reclaimed", err, telegram=True)
                    break  # re-poll fresh /sizes instead of chaining bursts
                is_phantom = ("not available" in el or "capacity" in el
                              or "unprocessable" in el or "micro-cooldown" in el)
                if not is_phantom:
                    log_sys(f"[{ts}] miss: {err}")
                    if "droplet limit" in el:
                        reason = "limit_full"      # chronic: GPU quota 0 / account full
                    elif "429" in el or "rate limited" in el:
                        reason = "rate_limited"
                    elif "limit" in el or "quota" in el or "exceed" in el:
                        reason = "account_limit"
                    else:
                        reason = "api_error"
                    # Telegram only for the actionable chronic blocker; rest → file only
                    log_missed(slug, ",".join(regions[:PARALLEL_REGIONS]), reason, err,
                               telegram=(reason == "limit_full"))

        if not rented:
            _scan_status = f"[{ts}] #{attempt:5d} — scanning..."
            # Wall-clock-aligned poll: keeps a stable phase offset between bots
            now = time.time()
            next_tick = (int(now / POLL_INTERVAL) + 1) * POLL_INTERVAL + POLL_OFFSET
            delay = next_tick - now
            if delay <= 0:
                delay += POLL_INTERVAL
            time.sleep(delay)

        # Heartbeat every ~5 min (~375 cycles at 0.8s)
        if attempt % 375 == 0:
            tg_send(f"💓 [{ACCOUNT_NAME}] Sniper жив | Цикл #{attempt} | {len(targets)} целей | Пока ничего не найдено")


def main():
    global _ui_running
    sys.stdout.write("\033[?1049h\033[2J\033[H")
    threading.Thread(target=ui_thread_func, daemon=True).start()
    try:
        _main_loop()
    except KeyboardInterrupt:
        pass
    finally:
        _ui_running = False
        sys.stdout.write("\033[?1049l\033[?25h")
        sys.stdout.flush()

if __name__ == "__main__":
    main()
