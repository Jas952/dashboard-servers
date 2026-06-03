#!/usr/bin/env python3
"""
DigitalOcean GPU Availability Checker
Мониторит наличие GPU Droplet в nyc2.
Уведомляет в Telegram при любом изменении статуса.

  python3 do_gpu_checker.py           # каждые 30с
  python3 do_gpu_checker.py 60        # каждые 60с
"""

import json
import os
import subprocess
import sys
import time
import threading
import urllib.request
import urllib.parse
import urllib.error
import ssl
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── Config ────────────────────────────────────────────────────
DO_TOKEN = "YOUR_DO_API_TOKEN"
# All DO regions to monitor
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

TG_BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
TG_CHAT_ID = "-1002313274238"

POLL_INTERVAL = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 5

# Target GPU configurations for alphapool
GPU_CONFIGS = {
    "gpu-h200x1-141gb": {"name": "H200 x1", "vram": "141 GB", "vcpu": 24, "ram": "240 GB", "image": "ubuntu-22-04-x64"},
    "gpu-h200x8-1128gb": {"name": "H200 x8", "vram": "1.1 TB", "vcpu": 192, "ram": "1920 GB", "image": "ubuntu-22-04-x64"},
    "gpu-h100x1-80gb": {"name": "H100 x1", "vram": "80 GB", "vcpu": 20, "ram": "240 GB", "image": "ubuntu-22-04-x64"},
    "gpu-h100x8-640gb": {"name": "H100 x8", "vram": "640 GB", "vcpu": 160, "ram": "1920 GB", "image": "ubuntu-22-04-x64"},
}

GPU_SLUGS = list(GPU_CONFIGS.keys())

# AlphaPool configuration
ALPHAPOOL_CONFIG = {
    "pool": "stratum+tcp://eu1.alphapool.tech:5566",
    "address": "YOUR_WALLET_ADDRESS",
    "worker_prefix": "do-gpu",
}

# VPC / Fleet UUID extracted from user's URL
VPC_UUID = "b2744055-0668-45ad-8d66-489456f902c7"

# ── State ─────────────────────────────────────────────────────
_prev_probe = None          # {region: {slug: (http_code, error_msg)}}
_created = {}               # {(region, slug): droplet_id} — уже созданные, не дублить
_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


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
        with urllib.request.urlopen(req, timeout=10, context=_ctx) as resp:
            result = json.loads(resp.read().decode())
            if not result.get("ok"):
                log(f"TG error: {result}")
            return result.get("ok", False)
    except Exception as e:
        log(f"TG send failed: {e}")
        return False


def check_available_gpus():
    """Возвращает список (region, slug) реально доступных GPU по всем регионам."""
    url = "https://api.digitalocean.com/v2/sizes?per_page=200"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {DO_TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=15, context=_ctx) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            log("❌ API Rate Limit (429) при проверке размеров! Бэкофф 60 сек...")
            time.sleep(60)
        else:
            log(f"Sizes API HTTP error: {e.code}")
        return []
    except Exception as e:
        log(f"Sizes API error: {e}")
        return []

    available = []
    for size in data.get("sizes", []):
        slug = size.get("slug", "")
        if slug in GPU_SLUGS and size.get("available", False):
            # DO API (баг/фича) возвращает пустой список regions для H100/H200
            regions_for_size = size.get("regions", [])
            if not regions_for_size:
                regions_for_size = REGIONS # Пробуем во всех наших регионах
                
            for r in regions_for_size:
                if r in REGIONS:
                    available.append((r, slug))
    return available


SSH_KEY = os.path.expanduser("~/.ssh/vast_key")


_failed_cache = {}  # {(region, slug): expiration_time}

def probe_gpu_slug(slug, region):
    """
    Пробует создать дроплет БЕЗ user_data.
    Если GPU доступна — дроплет создаётся, затем ждём active и ставим майнер.
    Возвращает (http_code, error_msg_or_droplet_id).
    """
    if len(_created) >= 1:
        return (0, "limit reached, skipped")
        
    if (region, slug) in _created:
        return (0, f"already created (droplet {_created[(region, slug)]})")
        
    # Защита от спама: если этот регион недавно ответил ошибкой 422 (нет ресурса),
    # ждем 60 секунд перед следующей попыткой, чтобы не словить бан Rate Limit.
    if _failed_cache.get((region, slug), 0) > time.time():
        return (0, "cooldown after failure")

    config = GPU_CONFIGS.get(slug, {})
    image = config.get("image", "ubuntu-22-04-x64")
    
    payload = json.dumps({
        "name": f"miner-{region}-{slug.split('-')[1]}",
        "region": region,
        "size": slug,
        "image": image,
        "ssh_keys": [56688722],
        "tags": ["gpu-miner", "auto-created"],
        "vpc_uuid": VPC_UUID
    }).encode()
    req = urllib.request.Request(
        "https://api.digitalocean.com/v2/droplets",
        data=payload, method="POST"
    )
    req.add_header("Authorization", f"Bearer {DO_TOKEN}")
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=10, context=_ctx) as resp:
            data = json.loads(resp.read().decode())
            droplet = data.get("droplet", {})
            droplet_id = droplet.get("id", "?")
            _created[(region, slug)] = droplet_id
            log(f"🚀 СОЗДАН дроплет {droplet_id} ({slug} в {region}) — жду active...")
            tg_send(
                f"🚀 <b>Дроплет создан!</b>\n"
                f"📍 {region} | {config.get('name', slug)}\n"
                f"🆔 <code>{droplet_id}</code>\n"
                f"⏳ Жду запуска, потом установлю майнер..."
            )
            t = threading.Thread(
                target=wait_and_install_miner,
                args=(droplet_id, region, slug),
                daemon=True,
            )
            t.start()
            return (201, f"CREATED droplet_id={droplet_id}")
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            msg = json.loads(body).get("message", body)
        except Exception:
            msg = body
        if e.code == 429:
            log(f"[droplet] ❌ API Rate Limit 429 при создании {slug} в {region}!")
        else:
            _failed_cache[(region, slug)] = time.time() + 60
        return (e.code, msg)
    except Exception as e:
        _failed_cache[(region, slug)] = time.time() + 60
        return (0, f"network error: {e}")


def get_droplet_info(droplet_id):
    """Получает инфу о дроплете. Возвращает dict или None."""
    url = f"https://api.digitalocean.com/v2/droplets/{droplet_id}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {DO_TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=10, context=_ctx) as resp:
            data = json.loads(resp.read().decode())
            return data.get("droplet", {})
    except Exception as e:
        log(f"Ошибка получения инфо дроплета {droplet_id}: {e}")
        return None


def wait_and_install_miner(droplet_id, region, slug):
    """
    Фоновый поток: ждёт пока дроплет станет active,
    получает IP, подключается по SSH и ставит майнер.
    """
    config = GPU_CONFIGS.get(slug, {})
    log(f"[droplet {droplet_id}] Ожидаю статус active...")

    ip_addr = None
    for attempt in range(60):  # 60 × 10с = 10 мин
        time.sleep(10)
        info = get_droplet_info(droplet_id)
        if info is None:
            continue
        
        status = info.get("status", "")
        networks = info.get("networks", {}).get("v4", [])
        public_ips = [n["ip_address"] for n in networks if n.get("type") == "public"]
        
        if status == "active" and public_ips:
            ip_addr = public_ips[0]
            log(f"[droplet {droplet_id}] ✅ Active! IP: {ip_addr}")
            tg_send(
                f"✅ <b>Дроплет запущен!</b>\n"
                f"🆔 <code>{droplet_id}</code> | 📍 {region}\n"
                f"🌐 IP: <code>{ip_addr}</code>\n"
                f"⏳ Устанавливаю майнер..."
            )
            break
        
        if attempt % 6 == 5:
            log(f"[droplet {droplet_id}] Статус: {status}, IP: {public_ips}, попытка {attempt+1}/60")
    
    if not ip_addr:
        log(f"[droplet {droplet_id}] ❌ Таймаут — дроплет не стал active за 10 мин")
        tg_send(f"❌ <b>Таймаут!</b> Дроплет <code>{droplet_id}</code> не запустился за 10 мин")
        return

    log(f"[droplet {droplet_id}] Жду SSH на {ip_addr}...")
    time.sleep(30)

    ssh_ok = False
    for attempt in range(8):  # 8 × 15с = 2 мин
        try:
            res = subprocess.run(
                ["ssh", "-i", SSH_KEY, "-o", "StrictHostKeyChecking=no",
                 "-o", "ConnectTimeout=10", "-o", "BatchMode=yes",
                 f"root@{ip_addr}", "echo ok"],
                capture_output=True, text=True, timeout=15,
            )
            if res.returncode == 0:
                ssh_ok = True
                log(f"[droplet {droplet_id}] SSH подключён!")
                break
        except Exception:
            pass
        time.sleep(15)

    if not ssh_ok:
        log(f"[droplet {droplet_id}] ❌ SSH не доступен на {ip_addr}")
        tg_send(f"❌ SSH недоступен на <code>{ip_addr}</code> (дроплет {droplet_id})")
        return

    worker = f"{ALPHAPOOL_CONFIG['worker_prefix']}-{region}-{slug.split('-')[1]}"
    pool = ALPHAPOOL_CONFIG['pool']
    addr = ALPHAPOOL_CONFIG['address']

    install_cmd = (
        f"pkill -9 alpha-miner 2>/dev/null; "
        f"mkdir -p /var/log /opt/alphapool; "
        f"curl -sL -o /usr/bin/alpha-miner https://pearl.alphapool.tech/downloads/alpha-miner-beta-174 && "
        f"chmod +x /usr/bin/alpha-miner && "
        f"nohup alpha-miner --pool {pool} --address {addr} --worker {worker} "
        f"--status-interval 30 >> /var/log/alpha-miner.log 2>&1 &"
    )

    log(f"[droplet {droplet_id}] Устанавливаю майнер (worker: {worker})...")
    try:
        res = subprocess.run(
            ["ssh", "-i", SSH_KEY, "-o", "StrictHostKeyChecking=no",
             "-o", "ConnectTimeout=10", f"root@{ip_addr}", install_cmd],
            capture_output=True, text=True, timeout=120,
        )
        if res.returncode == 0:
            log(f"[droplet {droplet_id}] ✅ Майнер установлен! Worker: {worker}")
            tg_send(
                f"⛏ <b>Майнер запущен!</b>\n"
                f"🆔 <code>{droplet_id}</code> | 📍 {region}\n"
                f"🎮 {config.get('name', slug)} | {config.get('vram', '?')}\n"
                f"🌐 <code>{ip_addr}</code>\n"
                f"👷 Worker: <code>{worker}</code>"
            )
        else:
            log(f"[droplet {droplet_id}] ❌ Ошибка установки: {res.stderr[:200]}")
            tg_send(f"❌ Ошибка установки майнера на {droplet_id}: {res.stderr[:200]}")
    except Exception as e:
        log(f"[droplet {droplet_id}] ❌ SSH ошибка: {e}")
        tg_send(f"❌ SSH ошибка при установке на {droplet_id}: {e}")


def probe_available_only(available_gpus):
    """Возвращает {region: {slug: (http_code, error_msg)}} только для доступных GPU."""
    results = {r: {} for r in REGIONS}
    
    # Поскольку DO скрывает регион, мы вынуждены послать POST во все регионы
    # Но мы делаем это только когда "available": true
    tasks = available_gpus
    
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(probe_gpu_slug, slug, region): (slug, region) for region, slug in tasks}
        for future in as_completed(futures):
            slug, region = futures[future]
            try:
                results[region][slug] = future.result()
            except Exception as e:
                results[region][slug] = (0, f"thread error: {e}")
    return results


def short_error(msg):
    """Сокращает длинное сообщение об ошибке для читаемости."""
    msg = msg.strip()
    if len(msg) > 80:
        return msg[:77] + "..."
    return msg


def main():
    global _prev_probe

    log(f"DO GPU Multi-Region Checker — {len(REGIONS)} регионов, интервал: {POLL_INTERVAL}с")

    tg_send(
        f"🟢 <b>DO GPU Checker запущен (Safe Rate Limit v2)</b>\n"
        f"Регионов: {len(REGIONS)} | GPU типов: {len(GPU_SLUGS)}\n"
        f"Интервал: {POLL_INTERVAL}с | Fleet: <code>{VPC_UUID}</code>\n"
        f"АльфаПул: worker-{ALPHAPOOL_CONFIG['worker_prefix']}-..."
    )

    cycle = 0
    while True:
        cycle += 1
        changes = []

        if len(_created) >= 1:
            if cycle % 20 == 0:
                log(f"[#{cycle}] Лимит достигнут (арендован 1 инстанс). Ожидаю завершения фоновых потоков установки майнера...")
            time.sleep(POLL_INTERVAL)
            continue

        # Шаг 1: Получаем список доступных GPU
        available_gpus = check_available_gpus()
        
        if not available_gpus:
            if cycle % 20 == 0:
                log(f"[#{cycle}] Нет свободных GPU, ожидаю...")
        else:
            log(f"[#{cycle}] НАЙДЕНО {len(available_gpus)} GPU: {available_gpus}")

        # Шаг 2: Пробуем создать только те, что доступны
        if available_gpus:
            probe = probe_available_only(available_gpus)

            if _prev_probe is not None:
                for region in REGIONS:
                    for slug in GPU_SLUGS:
                        new_code, new_msg = probe.get(region, {}).get(slug, (0, ""))
                        if new_code == 201:
                            config = GPU_CONFIGS.get(slug, {})
                            changes.append(
                                f"🚀🚀🚀 <b>GPU АРЕНДОВАНА!</b>\n"
                                f"📍 Регион: <code>{region}</code>\n"
                                f"🎮 {config.get('name', slug)} | {config.get('vram', '?')}\n"
                                f"💻 {config.get('vcpu', '?')} vCPU | {config.get('ram', '?')} RAM\n"
                                f"🎫 {new_msg}\n"
                                f"⛓️ Miner стартует автоматически"
                            )
                            log(f"[#{cycle}] 🎯 RENTED {region}/{slug}: [{new_code}] {new_msg}")
                        elif "limit reached" not in new_msg and "already created" not in new_msg and "cooldown" not in new_msg:
                            # Логируем любые ошибки, включая сетевые (new_code=0)
                            log(f"[#{cycle}] ❌ FAIL {region}/{slug}: [{new_code}] {new_msg}")
            
            _prev_probe = probe
        else:
            # Сбрасываем _prev_probe, чтобы не слать старые ошибки
            _prev_probe = {r: {} for r in REGIONS}

        # Отправка изменений
        if changes:
            header = f"🔄 <b>GPU Updates</b> (цикл #{cycle})\n\n"
            body = "\n\n".join(changes[:5])
            if len(changes) > 5:
                body += f"\n\n... и ещё {len(changes) - 5} изменений"
            tg_send(header + body)
            log(f"[#{cycle}] Sent {len(changes)} notifications")

        if cycle % 20 == 0:
            created_count = len(_created)
            log(f"[#{cycle}] Alive — создано: {created_count} дроплетов")

        if cycle % 120 == 0:
            tg_send(f"💓 DO GPU Checker жив | Проверено {len(REGIONS)} регионов | Цикл #{cycle}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
