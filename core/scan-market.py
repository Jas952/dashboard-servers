#!/usr/bin/env python3
"""
Сканер рынка Vast.ai для Pearl AlphaPool.
Ищет verified-серверы с адекватной экономикой ($/100TH).
Выводит отчёт в консоль и сохраняет в market-report.txt.
"""

import subprocess
import json
import sys
import re
from datetime import datetime

# --- Конфиг ---

# Ориентировочный хешрейт по GPU (TH/s на 1 карту) из GUIDE-RENT.md
HASHRATE = {
    'RTX 5090': 310,
    'RTX 5080': 160,
    'RTX 4090': 145,
    'RTX 4080': 120,
    'RTX 3090': 100,
    'RTX 3080 Ti': 75,
    'RTX 3080': 65,
}

# Целевые GPU для поиска
TARGET_GPUS = ['RTX_3090', 'RTX_5080', 'RTX_5090', 'RTX_4090', 'RTX_4080']

# Гео-фильтры из GUIDE-RENT.md
SKIP_GEO = ['RU', 'CN', 'HK', 'NL', 'IS', 'MY', 'BR']
WARN_GEO = ['UA', 'JP', 'QA', 'MX', 'GB']

# Экономика
MAX_COST_100TH = 0.25  # показываем всё до $0.25, дороже — скрываем
GOOD_THRESHOLD = 0.20
OK_THRESHOLD = 0.22

MIN_RELIABILITY = 0.90
MAX_GPUS = 4


def fetch_offers():
    """Получить офферы через vastai CLI."""
    gpu_filter = ','.join(TARGET_GPUS)
    query = f'rentable=true verified=true reliability>={MIN_RELIABILITY} gpu_name in [{gpu_filter}] num_gpus<={MAX_GPUS}'
    cmd = ['vastai', 'search', 'offers', query, '-n', '-d', '-o', 'dph', '--limit', '1000', '--raw']
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Ошибка vastai: {result.stderr}")
        sys.exit(1)
    
    return json.loads(result.stdout)


def analyze(offers):
    """Фильтруем и считаем экономику."""
    results = []
    
    for o in offers:
        geo = o.get('geolocation', '')
        # Tokenise geolocation into upper-case parts (e.g. "US, Texas" -> {"US", "TEXAS"})
        # to avoid substring false-positives like 'IS' matching 'Wisconsin'.
        geo_tokens = {p.strip().upper() for p in re.split(r'[\s,/\\\-]+', geo) if p.strip()}
        
        # Пропускаем плохую географию (точное совпадение кода страны)
        if any(code.upper() in geo_tokens for code in SKIP_GEO):
            continue
        
        gpu_name = o.get('gpu_name', '')
        num_gpus = o.get('num_gpus', 1)
        dph = o.get('dph_total', 999)
        rel = o.get('reliability2', o.get('reliability', 0))
        disk = o.get('disk_space', 0)
        direct = o.get('direct_port_count', 0)
        
        th_per_gpu = HASHRATE.get(gpu_name, 0)
        if th_per_gpu == 0:
            continue
        
        total_th = th_per_gpu * num_gpus
        cost_100th = (dph / total_th) * 100 if total_th > 0 else 999
        
        if cost_100th > MAX_COST_100TH:
            continue
        
        # Метки
        if cost_100th <= GOOD_THRESHOLD:
            tag = '✅'
        elif cost_100th <= OK_THRESHOLD:
            tag = '🟡'
        else:
            tag = '🟠'
        
        geo_warn = ' ⚠️' if any(code.upper() in geo_tokens for code in WARN_GEO) else ''
        
        # Рекомендация пула по гео
        if any(x in geo for x in ['US', 'CA', 'MX']):
            pool = 'us2'
        else:
            pool = 'eu1'
        
        results.append({
            'id': o['id'],
            'gpu': f"{num_gpus}x {gpu_name}",
            'num_gpus': num_gpus,
            'gpu_name': gpu_name,
            'geo': geo,
            'geo_warn': geo_warn,
            'dph': dph,
            'total_th': total_th,
            'cost_100th': cost_100th,
            'rel': rel,
            'tag': tag,
            'ssh': '✓' if direct > 0 else '✗',
            'pool': pool,
            'machine_id': o.get('machine_id'),
        })
    
    results.sort(key=lambda x: x['cost_100th'])
    return results


def format_report(results):
    """Формируем текстовый отчёт."""
    lines = []
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    
    lines.append(f"══════════════════════════════════════════════════════════════════")
    lines.append(f"  VAST.AI MARKET SCAN — Pearl AlphaPool")
    lines.append(f"  {now} | Verified only | Rel ≥ {MIN_RELIABILITY} | ≤ {MAX_GPUS} GPU")
    lines.append(f"══════════════════════════════════════════════════════════════════")
    lines.append("")
    
    good = [r for r in results if r['cost_100th'] <= GOOD_THRESHOLD]
    ok = [r for r in results if GOOD_THRESHOLD < r['cost_100th'] <= OK_THRESHOLD]
    edge = [r for r in results if OK_THRESHOLD < r['cost_100th'] <= MAX_COST_100TH]
    
    # --- Хорошие ---
    lines.append(f"✅ ОТЛИЧНЫЕ (≤ ${GOOD_THRESHOLD}/100TH) — {len(good)} шт.")
    lines.append(f"─────────────────────────────────────────────────────────────")
    if good:
        for r in good:
            lines.append(
                f"  ID {r['id']:<10}  {r['gpu']:<14}  {r['geo']:<22}{r['geo_warn']}"
            )
            lines.append(
                f"    $/час: ${r['dph']:.3f}   ~{r['total_th']} TH/s   "
                f"$/100TH: ${r['cost_100th']:.3f}   Rel: {r['rel']:.0%}   "
                f"SSH: {r['ssh']}   Пул: {r['pool']}"
            )
            lines.append("")
    else:
        lines.append("  (нет)")
        lines.append("")
    
    # --- Нормальные ---
    lines.append(f"🟡 НОРМАЛЬНЫЕ (${GOOD_THRESHOLD}–${OK_THRESHOLD}/100TH) — {len(ok)} шт.")
    lines.append(f"─────────────────────────────────────────────────────────────")
    if ok:
        for r in ok:
            lines.append(
                f"  ID {r['id']:<10}  {r['gpu']:<14}  {r['geo']:<22}{r['geo_warn']}"
            )
            lines.append(
                f"    $/час: ${r['dph']:.3f}   ~{r['total_th']} TH/s   "
                f"$/100TH: ${r['cost_100th']:.3f}   Rel: {r['rel']:.0%}   "
                f"SSH: {r['ssh']}   Пул: {r['pool']}"
            )
            lines.append("")
    else:
        lines.append("  (нет)")
        lines.append("")
    
    # --- На грани ---
    lines.append(f"🟠 НА ГРАНИ (${OK_THRESHOLD}–${MAX_COST_100TH}/100TH) — {len(edge)} шт.")
    lines.append(f"─────────────────────────────────────────────────────────────")
    if edge:
        for r in edge:
            lines.append(
                f"  ID {r['id']:<10}  {r['gpu']:<14}  {r['geo']:<22}{r['geo_warn']}"
            )
            lines.append(
                f"    $/час: ${r['dph']:.3f}   ~{r['total_th']} TH/s   "
                f"$/100TH: ${r['cost_100th']:.3f}   Rel: {r['rel']:.0%}   "
                f"SSH: {r['ssh']}   Пул: {r['pool']}"
            )
            lines.append("")
    else:
        lines.append("  (нет)")
        lines.append("")
    
    # --- Итого ---
    lines.append(f"══════════════════════════════════════════════════════════════════")
    lines.append(f"  ИТОГО: {len(results)} офферов | "
                 f"✅ {len(good)} отличных | 🟡 {len(ok)} нормальных | 🟠 {len(edge)} на грани")
    lines.append(f"══════════════════════════════════════════════════════════════════")
    
    return '\n'.join(lines)


if __name__ == '__main__':
    print("Сканирую Vast.ai...\n")
    offers = fetch_offers()
    print(f"Получено {len(offers)} офферов от API, фильтрую...\n")
    
    results = analyze(offers)
    report = format_report(results)
    
    print(report)
    
    # Сохраняем в файл
    with open('market-report.txt', 'w') as f:
        f.write(report)
    
    print(f"\nОтчёт сохранён в market-report.txt")
