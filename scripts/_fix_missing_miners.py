#!/usr/bin/env python3
import json, subprocess, sys, time

WALLET = 'YOUR_WALLET_ADDRESS'
MINER_URL = 'https://pearl.alphapool.tech/downloads/alpha-miner-beta-174'

GPU_DIFF = {'RTX 5090':1048576,'RTX 5080':524288,'RTX 4090':524288,'RTX 3090':262144}
EU_POOLS = ['BG','RO','HU','DE','CZ','PL','SI','EE','NL']

def get_pool(geo):
    if not geo: return 'us2.alphapool.tech'
    country = geo.split(',')[-1].strip() if ',' in geo else geo
    return 'eu1.alphapool.tech' if country in EU_POOLS else 'us2.alphapool.tech'

def get_difficulty(gpu, n):
    base = GPU_DIFF.get(gpu, 262144)
    return base * n if n > 1 else base

# Get instances
result = subprocess.run(['vastai', 'show', 'instances', '--raw'], capture_output=True, text=True)
data = json.loads(result.stdout)

needs_fix = []

print('='*70)
print('CHECKING SERVERS FOR MISSING MINERS')
print('='*70)

for i in data:
    if i.get('actual_status') != 'running':
        continue
    msg = i.get('status_msg', '').lower()
    if 'success' not in msg or 'cuda' not in msg:
        continue
    
    iid = i.get('id')
    host = i.get('ssh_host', '')
    port = i.get('ssh_port', '')
    
    if not host or not port:
        continue
    
    # Check miner log
    r = subprocess.run(
        ['ssh', '-o', 'ConnectTimeout=3', '-o', 'StrictHostKeyChecking=no',
         f'root@{host}', '-p', str(port), 'ls /var/log/alpha-miner.log 2>/dev/null || echo MISSING'],
        capture_output=True, text=True, timeout=10
    )
    
    if 'MISSING' in r.stdout or 'No such file' in r.stdout:
        print(f'{iid}: Miner log missing - needs install')
        needs_fix.append(i)
    else:
        # Check hashrate
        r2 = subprocess.run(
            ['ssh', '-o', 'ConnectTimeout=3', '-o', 'StrictHostKeyChecking=no',
             f'root@{host}', '-p', str(port), 'grep hashrate_th_s /var/log/alpha-miner.log 2>/dev/null | tail -1'],
            capture_output=True, text=True, timeout=10
        )
        if 'hashrate_th_s=' not in r2.stdout:
            print(f'{iid}: No hashrate - needs restart')
            needs_fix.append(i)
        else:
            print(f'{iid}: OK')

print(f'\nFound {len(needs_fix)} servers needing miner')

# Install on all
for inst in needs_fix:
    iid = inst.get('id')
    host = inst.get('ssh_host', '')
    port = inst.get('ssh_port', '')
    gpu = inst.get('gpu_name', 'RTX 3090')
    n = inst.get('num_gpus', 1)
    geo = inst.get('geolocation', '')
    
    pool = get_pool(geo)
    diff = get_difficulty(gpu, n)
    
    cmd = f"pkill -9 alpha-miner 2>/dev/null; rm -f /usr/bin/alpha-miner; mkdir -p /var/log && curl -sL -o /usr/bin/alpha-miner {MINER_URL} && chmod +x /usr/bin/alpha-miner && nohup alpha-miner --pool stratum+tcp://{pool}:5566 --address {WALLET} --worker vast-{iid} --password 'x;d={diff}' --status-interval 30 >> /var/log/alpha-miner.log 2>&1 &"
    
    print(f'\n{iid} ({gpu}): Installing miner...')
    r = subprocess.run(
        ['ssh', '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=no',
         f'root@{host}', '-p', str(port), cmd],
        capture_output=True, text=True, timeout=30
    )
    if r.returncode == 0:
        print(f'  OK - command sent')
    else:
        print(f'  Failed (code {r.returncode})')

print('\n' + '='*70)
print('Waiting 30 seconds for miners to start...')
print('='*70)
time.sleep(30)

# Verify
print('\n' + '='*70)
print('VERIFICATION')
print('='*70)
for inst in needs_fix:
    iid = inst.get('id')
    host = inst.get('ssh_host', '')
    port = inst.get('ssh_port', '')
    
    r = subprocess.run(
        ['ssh', '-o', 'ConnectTimeout=3', '-o', 'StrictHostKeyChecking=no',
         f'root@{host}', '-p', str(port), 'grep hashrate_th_s /var/log/alpha-miner.log 2>/dev/null | tail -1'],
        capture_output=True, text=True, timeout=10
    )
    if 'hashrate_th_s=' in r.stdout:
        try:
            hr = r.stdout.split('hashrate_th_s=')[1].split()[0]
            print(f'{iid}: Hashrate {hr} TH/s')
        except:
            print(f'{iid}: Starting...')
    else:
        print(f'{iid}: Still starting...')
