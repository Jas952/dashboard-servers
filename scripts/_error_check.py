#!/usr/bin/env python3
import json, subprocess, sys

result = subprocess.run(['vastai', 'show', 'instances', '--raw'], capture_output=True, text=True)
data = json.loads(result.stdout)

FATAL = ['failed to create task','OCI runtime','out of memory','no space left','permission denied','image not found','unauthorized','failed to pull','error response from daemon']

print('='*70)
print('ERROR ANALYSIS - Non-Running Servers')
print('='*70)

for i in data:
    actual = i.get('actual_status')
    intended = i.get('intended_status')
    
    if actual != 'running':
        iid = i.get('id')
        gpu = i.get('gpu_name','?')
        dph = i.get('dph_total',0)
        status = actual or 'NULL'
        msg = (i.get('status_msg') or '').lower()
        
        print(f'\n{iid}: {gpu} @ ${dph:.3f}/hr')
        print(f'  Actual: {status} | Intended: {intended}')
        
        # Error state detection
        if actual is None and intended == 'running':
            print(f'  [ERROR STATE] actual_status=NULL, intended=running')
            print(f'  [ACTION] This is the "Error - not running" from UI')
        elif actual == 'loading' and intended == 'stopped':
            print(f'  [STOPPED DURING LOAD] User stopped during creation')
        
        # Check errors in msg
        fatal = [e for e in FATAL if e in msg]
        if fatal:
            print(f'  [FATAL] {fatal}')
        elif msg:
            print(f'  [MSG] {msg[:60]}')
        else:
            print(f'  [NO MSG]')

print('\n' + '='*70)
