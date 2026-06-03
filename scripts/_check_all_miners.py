#!/usr/bin/env python3
import json, subprocess, concurrent.futures

WALLET = 'YOUR_WALLET_ADDRESS'
MINER_URL = 'https://pearl.alphapool.tech/downloads/alpha-miner-beta-174'
GPU_DIFF = {'RTX 5090':1048576,'RTX 5080':524288,'RTX 4090':524288,'RTX 3090':262144}
EU_POOLS = ['BG','RO','HU','DE','CZ','PL','SI','EE','NL']

def get_pool(geo):
    if not geo: return 'us2.alphapool.tech'
    country = geo.split(',')[-1].strip() if ',' in geo else geo
    return 'eu1.alphapool.tech' if country in EU_POOLS else 'us2.alphapool.tech'

def check_server(inst):
    iid = inst.get('id')
    host = inst.get('ssh_host', '')
    port = inst.get('ssh_port', '')
    gpu = inst.get('gpu_name', '?')
    n = inst.get('num_gpus', 1)
    geo = inst.get('geolocation', '')
    
    if not host or not port:
        return (iid, 'NO_SSH', None, n, gpu, geo)
    
    # Check hashrate
    try:
        r = subprocess.run(
            ['ssh', '-o', 'ConnectTimeout=3', '-o', 'StrictHostKeyChecking=no',
             f'root@{host}', '-p', str(port), 
             'grep hashrate_th_s /var/log/alpha-miner.log 2>/dev/null | tail -1'],
            capture_output=True, text=True, timeout=10
        )
        if 'hashrate_th_s=' in r.stdout:
            try:
                hr = r.stdout.split('hashrate_th_s=')[1].split()[0]
                return (iid, 'OK', float(hr), n, gpu, geo)
            except:
                return (iid, 'OK', 0, n, gpu, geo)
    except:
        pass
    
    # Check if binary exists
    try:
        r = subprocess.run(
            ['ssh', '-o', 'ConnectTimeout=3', '-o', 'StrictHostKeyChecking=no',
             f'root@{host}', '-p', str(port),
             'ls /usr/bin/alpha-miner 2>/dev/null || echo MISSING'],
            capture_output=True, text=True, timeout=10
        )
        if 'MISSING' in r.stdout:
            return (iid, 'NO_BINARY', None, n, gpu, geo)
    except:
        pass
    
    return (iid, 'NO_HASHRATE', None, n, gpu, geo)

def install_miner(inst):
    iid = inst.get('id')
    host = inst.get('ssh_host', '')
    port = inst.get('ssh_port', '')
    gpu = inst.get('gpu_name', 'RTX 3090')
    n = inst.get('num_gpus', 1)
    geo = inst.get('geolocation', '')
    
    if not host or not port:
        return (iid, 'NO_SSH')
    
    pool = get_pool(geo)
    diff = GPU_DIFF.get(gpu, 262144) * (n if n > 1 else 1)
    
    cmd = f"pkill -9 alpha-miner 2>/dev/null; rm -f /usr/bin/alpha-miner; mkdir -p /var/log && curl -sL -o /usr/bin/alpha-miner {MINER_URL} && chmod +x /usr/bin/alpha-miner && nohup alpha-miner --pool stratum+tcp://{pool}:5566 --address {WALLET} --worker vast-{iid} --password 'x;d={diff}' --status-interval 30 >> /var/log/alpha-miner.log 2>&1 &"
    
    try:
        r = subprocess.run(
            ['ssh', '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=no',
             f'root@{host}', '-p', str(port), cmd],
            capture_output=True, text=True, timeout=30
        )
        return (iid, 'INSTALLED' if r.returncode == 0 else 'FAILED')
    except Exception as e:
        return (iid, f'ERROR: {e}')

# Get instances
print('Fetching instances...')
result = subprocess.run(['vastai', 'show', 'instances', '--raw'], capture_output=True, text=True)
data = json.loads(result.stdout)

running = [i for i in data if i.get('actual_status') == 'running']
print(f'Total running: {len(running)}')

# Check all
print('\nChecking all servers...')
needs_install = []
with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
    futures = {executor.submit(check_server, i): i for i in running}
    for future in concurrent.futures.as_completed(futures):
        iid, status, hr, n, gpu, geo = future.result()
        if status == 'OK':
            print(f'{iid}: ✅ {hr} TH/s ({n}x {gpu})')
        elif status == 'NO_HASHRATE':
            print(f'{iid}: ⚠️  NO HASHRATE ({n}x {gpu} @ {geo})')
            inst = futures[future]
            needs_install.append(inst)
        elif status == 'NO_BINARY':
            print(f'{iid}: ❌ NO BINARY ({n}x {gpu})')
            inst = futures[future]
            needs_install.append(inst)
        else:
            print(f'{iid}: ❌ {status}')

print(f'\nFound {len(needs_install)} servers needing miner install')

if needs_install:
    print('\nInstalling miners...')
    for inst in needs_install:
        iid, status = install_miner(inst)
        print(f'{iid}: {status}')
    
    print('\nWaiting 30 seconds for miners to start...')
    import time
    time.sleep(30)
    
    print('\nVerifying...')
    for inst in needs_install:
        result = check_server(inst)
        iid, status, hr, n, gpu, geo = result
        if status == 'OK':
            print(f'{iid}: ✅ {hr} TH/s')
        else:
            print(f'{iid}: ⚠️  Still starting...')
