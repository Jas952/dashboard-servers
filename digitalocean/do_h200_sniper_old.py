#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DO GPU Sniper — H200 (NVIDIA) + MI300X (AMD)
Fast poll + parallel region create on availability.

Usage:
  python do_h200_sniper.py           -- all targets, rent first available
  python do_h200_sniper.py --nvidia  -- only NVIDIA H200
  python do_h200_sniper.py --amd     -- only AMD MI300X
  python do_h200_sniper.py --8x      -- prefer 8-card configs
"""

import sys
import time
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
RATE_LIMITS = {k: 0 for k in API_KEYS}
WALLET    = "prl1p78gg659wed6wd7ruhn8sx7u96eu7avp844nmdqw7jgnxqmxtv3cqvr0f3l"
POOL_HOST = "ru1.alphapool.tech"
POOL_PORT = 5566
POLL_INTERVAL = 0.8   # ~4500 req/hr (DO cap 5000/hr)

REGIONS_PRIORITY = [
    "nyc2", "nyc3", "nyc1", "sfo3", "fra1", "ams3",
    "sgp1", "lon1", "tor1", "syd1", "blr1", "atl1", "sfo2", "ric1",
]
PARALLEL_REGIONS = 6   # simultaneous create attempts when slot detected

TARGETS = {
    "gpu-h100x1-80gb":    ("1x H100 NVIDIA",   2.49, "nvidia", "h100x1",  1048576),
    "gpu-h100x8-640gb":   ("8x H100 NVIDIA",  19.92, "nvidia", "h100x8",  1048576),
    "gpu-h200x1-141gb":   ("1x H200 NVIDIA",  3.44,  "nvidia", "h200x1",  1048576),
    "gpu-h200x8-1128gb":  ("8x H200 NVIDIA", 27.52,  "nvidia", "h200x8",  1048576),
    "gpu-mi300x1-192gb":  ("1x MI300X AMD",   1.99,  "amd",    "mi300x1", 524288),
    "gpu-mi300x8-1536gb": ("8x MI300X AMD",  15.92,  "amd",    "mi300x8", 524288),
}

MINER_NVIDIA_URL = "https://pearl.alphapool.tech/downloads/alpha-miner-1.7.5-beta"
MINER_AMD_URL    = "https://github.com/AlphaMine-Tech/alpha-miner/releases/download/amd-v1.0.0/alpha-miner-amd"

BASE_URL = "https://api.digitalocean.com/v2"
SESSION  = requests.Session()

def get_best_token():
    now = time.time()
    for k in API_KEYS:
        if RATE_LIMITS[k] < now:
            return k
    return min(API_KEYS, key=lambda k: RATE_LIMITS[k])

SSH_PUBKEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICIK9iXNLHep9CbmD3u62h4AlbUOWBmiLQQIB/30Eh5U "
    "alexm@LAPTOP-MLG6LFR2"
)

# ─────────────────────────────────────────────────────
def make_cloud_init(vendor, worker_suffix, difficulty):
    worker = f"do-{worker_suffix}"
    if vendor == "nvidia":
        miner_url, miner_bin, extra_deps = MINER_NVIDIA_URL, "alpha-miner", ""
        gpu_wait = """
for i in $(seq 1 90); do nvidia-smi &>/dev/null && break; sleep 5; done
"""
    else:
        miner_url, miner_bin, extra_deps = MINER_AMD_URL, "alpha-miner-amd", ""
        gpu_wait = ""

    return f"""#!/bin/bash
set -e
export DEBIAN_FRONTEND=noninteractive
mkdir -p /root/.ssh && chmod 700 /root/.ssh
grep -qF '{SSH_PUBKEY.split()[1]}' /root/.ssh/authorized_keys 2>/dev/null || echo '{SSH_PUBKEY}' >> /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys
apt-get update -qq && apt-get install -y -qq curl {extra_deps}
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
StandardOutput=journal
StandardError=journal
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload && systemctl enable alpha-miner && systemctl start alpha-miner
"""


def build_cloud_init_cache(slugs):
    cache = {}
    for slug in slugs:
        label, _, vendor, worker_sfx, diff = TARGETS[slug]
        cache[slug] = make_cloud_init(vendor, worker_sfx, diff)
    return cache


def api_get(path, params=None):
    while True:
        token = get_best_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        try:
            r = SESSION.get(f"{BASE_URL}{path}", headers=headers, params=params, timeout=20)
            if r.status_code == 429:
                sleep_time = int(r.headers.get("Retry-After", 10))
                RATE_LIMITS[token] = time.time() + sleep_time
                continue
            r.raise_for_status()
            return r.json()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            time.sleep(1)


def ensure_ssh_key_on_account():
    keys = api_get("/account/keys").get("ssh_keys", [])
    fp = SSH_PUBKEY.split()[1]
    if any(fp in k.get("public_key", "") for k in keys):
        return
    token = get_best_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = SESSION.post(
        f"{BASE_URL}/account/keys",
        headers=headers,
        json={"name": "sniper-deploy", "public_key": SSH_PUBKEY},
        timeout=20,
    )
    if r.status_code in (200, 201):
        print("  [OK] SSH key on DO account")


def get_ssh_key_ids():
    return [k["id"] for k in api_get("/account/keys").get("ssh_keys", [])]


def try_create_once(slug, region, ssh_key_ids, user_data):
    body = {
        "name": f"m-{slug[:10]}-{region}",
        "region": region,
        "size": slug,
        "image": "ubuntu-22-04-x64",
        "ssh_keys": ssh_key_ids,
        "user_data": user_data,
        "backups": False,
        "ipv6": False,
        "tags": ["gpu"],
    }
    token = get_best_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        r = SESSION.post(f"{BASE_URL}/droplets", headers=headers, json=body, timeout=25)
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
        return None, str(e)
    if r.status_code == 429:
        RATE_LIMITS[token] = time.time() + int(r.headers.get("Retry-After", 10))
        return None, "HTTP 429: Too many requests"
    if r.status_code in (200, 201, 202):
        return r.json()["droplet"]["id"], None
    try:
        err = r.json().get("message", r.text)
    except Exception:
        err = r.text
    return None, f"HTTP {r.status_code}: {err}"


def race_create(slug, regions, ssh_key_ids, user_data):
    """Fire create in parallel; first success wins. Drop accidental duplicates."""
    regions = list(dict.fromkeys(regions))[:PARALLEL_REGIONS]
    if not regions:
        return None, None, "no regions"

    winners = []

    def attempt(region):
        did, err = try_create_once(slug, region, ssh_key_ids, user_data)
        return region, did, err

    with ThreadPoolExecutor(max_workers=len(regions)) as pool:
        futures = [pool.submit(attempt, r) for r in regions]
        for fut in as_completed(futures):
            region, did, err = fut.result()
            if did:
                winners.append((did, region))
                break

    if not winners:
        return None, None, err

    did, region = winners[0]
    for extra_id, extra_reg in winners[1:]:
        print(f"  [!] Extra droplet {extra_id} in {extra_reg} — destroying")
        SESSION.delete(f"{BASE_URL}/droplets/{extra_id}", headers={"Authorization": f"Bearer {get_best_token()}"}, timeout=15)
    return did, region, None


def wait_for_active(droplet_id, timeout=300):
    print(f"  [...] Waiting active (max {timeout}s)...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = SESSION.get(f"{BASE_URL}/droplets/{droplet_id}", headers={"Authorization": f"Bearer {get_best_token()}"}, timeout=15)
        if r.status_code == 404:
            print(f"\n  [!] Droplet {droplet_id} gone (404) — slot lost during provisioning")
            return None
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", 10)))
            continue
        r.raise_for_status()
        d = r.json()["droplet"]
        if d["status"] == "active":
            ip = next((n["ip_address"] for n in d["networks"]["v4"] if n["type"] == "public"), None)
            print(f"\n  [OK] Active, IP={ip}")
            return ip
        time.sleep(3)
    print("\n  [!] Timeout waiting for active")
    return None


def handle_rent_success(slug, region, droplet_id):
    ip = wait_for_active(droplet_id)
    if not ip:
        return False
    print_result(slug, region, droplet_id, ip)
    return True


def build_target_list():
    args = sys.argv[1:]
    slugs = list(TARGETS.keys())
    
    # ФИЛЬТР: Убираем все 8x серверы, оставляем только 1x
    slugs = [s for s in slugs if "x8" not in s]

    if "--nvidia" in args:
        slugs = [s for s in slugs if TARGETS[s][2] == "nvidia"]
    elif "--amd" in args:
        slugs = [s for s in slugs if TARGETS[s][2] == "amd"]
    
    slugs = sorted(slugs, key=lambda s: TARGETS[s][1])
    return slugs


def order_regions(avail, all_ordered):
    if avail:
        out = [r for r in all_ordered if r in avail]
        for r in avail:
            if r not in out:
                out.append(r)
        return out
    return list(all_ordered)


def print_result(slug, region, droplet_id, ip):
    label, _, _, _, _ = TARGETS[slug]
    print()
    print("=" * 62)
    print(f"  [SUCCESS] {label}")
    print(f"  Region:  {region}")
    print(f"  IP:      {ip}")
    print(f"  ID:      {droplet_id}")
    print(f"  SSH:     ssh -i ~/.ssh/rpow_deploy root@{ip}")
    print(f"  Logs:    journalctl -u alpha-miner -f")
    print(f"  Destroy: python do_h200_destroy.py {droplet_id}")
    print("=" * 62)


def main():
    targets = build_target_list()
    ensure_ssh_key_on_account()
    ssh_key_ids = get_ssh_key_ids()
    cloud_cache = build_cloud_init_cache(targets)

    print("=" * 62)
    print("  DO GPU Sniper — TURBO (parallel regions, 0.8s poll)")
    print("=" * 62)
    print(f"  Poll: {POLL_INTERVAL}s | Parallel regions: {PARALLEL_REGIONS}")
    for slug in targets:
        label, price, vendor, _, _ = TARGETS[slug]
        print(f"    [{vendor.upper():6s}] {slug}  ${price:.2f}/hr")
    print()

    all_regions = {r["slug"] for r in api_get("/regions", {"per_page": 100}).get("regions", []) if r.get("available")}
    ordered_regions = [r for r in REGIONS_PRIORITY if r in all_regions]
    for r in all_regions:
        if r not in ordered_regions:
            ordered_regions.append(r)

    attempt = 0
    last_sleep_time = time.time()
    while True:
        attempt += 1
        ts = time.strftime("%H:%M:%S")
        
        # 1-minute sleep every 1 hour (3600 seconds)
        if time.time() - last_sleep_time >= 3600:
            print(f"\n  [{ts}] Hourly pause (60s)...")
            time.sleep(60)
            last_sleep_time = time.time()
            ts = time.strftime("%H:%M:%S") # update timestamp after sleep
        try:
            sizes = api_get("/sizes", {"per_page": 200})
            sizes = {s["slug"]: s for s in sizes.get("sizes", [])}
        except KeyboardInterrupt:
            print("\n[!] Stopped.")
            return
        except Exception as e:
            print(f"\n  [{ts}] API error: {e}")
            time.sleep(2)
            continue

        rented = False
        for slug in targets:
            s = sizes.get(slug, {})
            if not s.get("available"):
                continue

            label, _, _, _, _ = TARGETS[slug]
            user_data = cloud_cache[slug]
            regions = order_regions(s.get("regions") or [], ordered_regions)

            print(f"\n  [{ts}] SLOT: {label} — racing {regions[:PARALLEL_REGIONS]}...")
            did, region, err = race_create(slug, regions, ssh_key_ids, user_data)
            if did:
                print(f"  [{ts}] RENTED {label} in {region} (ID={did})")
                if handle_rent_success(slug, region, did):
                    return
                rented = True
                break
            if err and "not available" not in str(err).lower():
                print(f"  [{ts}] miss: {err}")

        if not rented:
            print(f"  [{ts}] #{attempt:5d} — scanning...    ", end="\r", flush=True)
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
