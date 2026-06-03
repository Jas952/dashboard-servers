#!/usr/bin/env python3
import json, subprocess

data = json.loads(subprocess.run(['vastai', 'show', 'instances', '--raw'], capture_output=True, text=True).stdout)

print('='*70)
print('MINER LOG CHECK - ALL RUNNING SERVERS')
print('='*70)

running = [i for i in data if i.get('actual_status')=='running']
empty_logs = []

for i in running:
    iid = i.get('id')
    host = i.get('ssh_host', '')
    port = i.get('ssh_port', '')
    gpu = i.get('gpu_name', '?')
    
    if not host or not port:
        print(f'{iid} ({gpu}): NO SSH INFO')
        continue
    
    r = subprocess.run(
        ['ssh', '-o', 'ConnectTimeout=3', '-o', 'StrictHostKeyChecking=no',
         f'root@{host}', '-p', str(port), 
         'ls -la /var/log/alpha-miner.log 2>/dev/null && tail -5 /var/log/alpha-miner.log 2>/dev/null'],
        capture_output=True, text=True, timeout=10
    )
    
    stdout = r.stdout.strip()
    
    if r.returncode != 0 or not stdout:
        print(f'{iid} ({gpu}): ❌ NO LOG - NEEDS INSTALL')
        empty_logs.append(i)
    elif 'hashrate' not in stdout:
        print(f'{iid} ({gpu}): ⚠️  LOG EXISTS BUT NO HASHRATE')
        print(f'      Last: {stdout[:80]}')
    else:
        # Extract hashrate
        for line in stdout.split('\n'):
            if 'hashrate_th_s' in line:
                try:
                    hr = line.split('hashrate_th_s=')[1].split()[0]
                    print(f'{iid} ({gpu}): ✅ {hr} TH/s')
                except:
                    print(f'{iid} ({gpu}): ✅ hashrate found')
                break

print()
print('='*70)
if empty_logs:
    print(f'❌ {len(empty_logs)} servers need miner install:')
    for i in empty_logs:
        print(f'   {i.get("id")} ({i.get("gpu_name")})')
else:
    print('✅ All running servers have miner logs with hashrate')
print('='*70)
