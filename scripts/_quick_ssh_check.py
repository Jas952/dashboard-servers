#!/usr/bin/env python3
import json, subprocess

data = json.loads(subprocess.run(['vastai', 'show', 'instances', '--raw'], capture_output=True, text=True).stdout)

print('=' * 70)
print('SSH CHECK - NON-RUNNING SERVERS')
print('=' * 70)

for i in data:
    if i.get('actual_status') != 'running':
        iid = i.get('id')
        status = i.get('actual_status') or 'none'
        host = i.get('ssh_host', '')
        port = i.get('ssh_port', '')
        gpu = i.get('gpu_name', '?')
        
        print(f'\n{iid}: {status} | {gpu}')
        
        if not host or not port:
            print('  [SSH] NO INFO')
            continue
        
        r = subprocess.run(
            ['ssh', '-o', 'ConnectTimeout=3', '-o', 'StrictHostKeyChecking=no',
             f'root@{host}', '-p', str(port), 'echo OK'],
            capture_output=True, text=True, timeout=10
        )
        
        if r.returncode == 0:
            print('  [SSH] REACHABLE')
            # Check if CUDA image loaded
            docker = subprocess.run(
                ['ssh', '-o', 'ConnectTimeout=3', '-o', 'StrictHostKeyChecking=no',
                 f'root@{host}', '-p', str(port), 'docker ps 2>/dev/null | grep -i cuda || echo NO_DOCKER'],
                capture_output=True, text=True, timeout=10
            )
            if 'NO_DOCKER' not in docker.stdout and docker.stdout.strip():
                print('  [DOCKER] CUDA container running')
            else:
                print('  [DOCKER] No CUDA yet')
        else:
            print('  [SSH] UNREACHABLE (normal for loading)')

print('\n' + '=' * 70)
