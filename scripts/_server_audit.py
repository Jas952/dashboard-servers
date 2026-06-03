#!/usr/bin/env python3
import json, subprocess, time

result = subprocess.run(['vastai', 'show', 'instances', '--raw'], capture_output=True, text=True)
data = json.loads(result.stdout)

running = [i for i in data if i.get('actual_status')=='running']

print('='*80)
print('SERVER AUDIT - All Running Instances')
print('='*80)

issues = []
good = []

for i in running:
    iid = i.get('id')
    host = i.get('ssh_host', '')
    port = i.get('ssh_port', '')
    gpu = i.get('gpu_name', '?')
    dph = i.get('dph_total', 0)
    geo = i.get('geolocation') or '?'
    n = i.get('num_gpus', 1)
    
    print(f'\n{iid}: {n}x {gpu} @ ${dph:.3f}/hr - {geo}')
    
    if not host or not port:
        print('  ❌ NO SSH INFO')
        issues.append((iid, 'NO_SSH', 0))
        continue
    
    # Check miner log for hashrate
    r = subprocess.run(
        ['ssh', '-o', 'ConnectTimeout=3', '-o', 'StrictHostKeyChecking=no',
         f'root@{host}', '-p', str(port),
         'grep hashrate_th_s /var/log/alpha-miner.log 2>/dev/null | tail -1'],
        capture_output=True, text=True, timeout=10
    )
    
    if 'hashrate_th_s=' in r.stdout:
        try:
            hr = r.stdout.split('hashrate_th_s=')[1].split()[0]
            hr_val = float(hr)
            eff = (dph / hr_val) * 100 if hr_val > 0 else 999
            print(f'  ✅ Hashrate: {hr} TH/s | $/100T: ${eff:.2f}')
            good.append((iid, gpu, dph, hr_val, eff))
        except:
            print(f'  ⚠️  Hasrate parse error')
            issues.append((iid, 'PARSE_ERROR', 0))
    else:
        # Check if binary exists
        r2 = subprocess.run(
            ['ssh', '-o', 'ConnectTimeout=3', '-o', 'StrictHostKeyChecking=no',
             f'root@{host}', '-p', str(port),
             'ls /usr/bin/alpha-miner 2>/dev/null || echo MISSING'],
            capture_output=True, text=True, timeout=10
        )
        if 'MISSING' in r2.stdout:
            print(f'  ❌ NO MINER BINARY')
            issues.append((iid, 'NO_BINARY', 0))
        else:
            print(f'  ⚠️  Miner exists but NO HASHRATE')
            issues.append((iid, 'NO_HASHRATE', 0))

print('\n' + '='*80)
print('SUMMARY')
print('='*80)
print(f'Good: {len(good)} servers')
print(f'Issues: {len(issues)} servers')

if good:
    print('\n--- Good Servers ($/100T sorted) ---')
    good_sorted = sorted(good, key=lambda x: x[4])
    for iid, gpu, dph, hr, eff in good_sorted[:5]:
        print(f'  {iid}: {hr:.1f} TH/s | ${eff:.2f}/100T | ${dph:.2f}/hr')

if issues:
    print('\n--- Servers with Issues ---')
    for iid, issue, _ in issues:
        print(f'  {iid}: {issue}')

print('='*80)
