#!/usr/bin/env python3
"""Check SSH for non-running instances."""
import json
import subprocess

result = subprocess.run(['vastai', 'show', 'instances', '--raw'], capture_output=True, text=True)
data = json.loads(result.stdout)

non_running = [i for i in data if i.get('actual_status') != 'running']

print('=' * 70)
print('SSH CHECK FOR NON-RUNNING INSTANCES')
print('=' * 70)

for i in non_running:
    iid = i.get('id')
    status = i.get('actual_status') or 'none'
    host = i.get('ssh_host', '')
    port = i.get('ssh_port', '')
    gpu = i.get('gpu_name', '?')
    dph = i.get('dph_total', 0)
    msg = (i.get('status_msg') or '')[:50]
    
    print(f'\n{iid}: {status} | {gpu} @ ${dph:.3f}/hr')
    if msg:
        print(f'  msg: {msg}')
    
    if not host or not port:
        print('  [SSH] NO SSH INFO')
        continue
    
    # Test SSH
    ssh = subprocess.run(
        ['ssh', '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=no',
         f'root@{host}', '-p', str(port), 'echo SSH_OK'],
        capture_output=True, text=True, timeout=10
    )
    
    if ssh.returncode == 0:
        print('  [SSH] REACHABLE')
        # Check if miner exists
        miner = subprocess.run(
            ['ssh', '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=no',
             f'root@{host}', '-p', str(port), 'ls -la /usr/bin/alpha-miner 2>/dev/null || echo NO_MINER'],
            capture_output=True, text=True, timeout=10
        )
        if 'NO_MINER' in miner.stdout:
            print('  [MINER] NOT INSTALLED')
        else:
            print('  [MINER] INSTALLED')
        # Check if log exists
        log = subprocess.run(
            ['ssh', '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=no',
             f'root@{host}', '-p', str(port), 'ls -la /var/log/alpha-miner.log 2>/dev/null || echo NO_LOG'],
            capture_output=True, text=True, timeout=10
        )
        if 'NO_LOG' in log.stdout:
            print('  [LOG] NO LOG')
        else:
            print('  [LOG] EXISTS')
    else:
        print('  [SSH] UNREACHABLE')

print('\n' + '=' * 70)
print('DONE')
print('=' * 70)
