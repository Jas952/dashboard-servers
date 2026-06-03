#!/usr/bin/env python3
import json, subprocess, concurrent.futures

def check_server(inst):
    iid = inst.get('id')
    host = inst.get('ssh_host', '')
    port = inst.get('ssh_port', '')
    gpu = inst.get('gpu_name', '?')
    n = inst.get('num_gpus', 1)
    
    if not host or not port:
        return (iid, 'NO_SSH', n, gpu)
    
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
                return (iid, 'OK', float(hr), n, gpu)
            except:
                return (iid, 'OK', 0, n, gpu)
    except:
        pass
    
    return (iid, 'NO_HASHRATE', n, gpu)

print('Fetching instances...')
result = subprocess.run(['vastai', 'show', 'instances', '--raw'], capture_output=True, text=True)
data = json.loads(result.stdout)

running = [i for i in data if i.get('actual_status') == 'running']
print(f'Total running: {len(running)}')
print()

ok_list = []
no_hashrate = []
no_ssh = []

with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
    futures = {executor.submit(check_server, i): i for i in running}
    for future in concurrent.futures.as_completed(futures):
        result = future.result()
        iid, status, hr_or_n, n_or_gpu, gpu = result
        
        if status == 'OK':
            hr = hr_or_n
            n = n_or_gpu
            gpu_type = gpu
            print(f'{iid}: ✅ {hr} TH/s ({n}x {gpu_type})')
            ok_list.append((iid, hr, n, gpu))
        elif status == 'NO_HASHRATE':
            n = hr_or_n
            gpu_type = n_or_gpu
            print(f'{iid}: ❌ NO HASHRATE ({n}x {gpu_type})')
            no_hashrate.append((iid, n, gpu_type))
        else:
            print(f'{iid}: ❌ NO SSH')
            no_ssh.append((iid))

print()
print('='*60)
print('SUMMARY')
print('='*60)
print(f'✅ Working: {len(ok_list)}')
print(f'❌ No hashrate: {len(no_hashrate)}')
print(f'❌ No SSH: {len(no_ssh)}')

if no_hashrate:
    print()
    print('Servers needing miner install:')
    for iid, n, gpu in no_hashrate:
        print(f'  {iid}: {n}x {gpu}')
