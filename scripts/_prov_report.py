#!/usr/bin/env python3
import json, subprocess

result = subprocess.run(['vastai', 'show', 'instances', '--raw'], capture_output=True, text=True)
data = json.loads(result.stdout)

non_running=[i for i in data if i.get('actual_status')!='running']

print('='*70)
print('NON-RUNNING SERVERS:', len(non_running))
print('='*70)

for i in non_running:
    iid=i.get('id')
    actual=i.get('actual_status') or 'NULL'
    intended=i.get('intended_status')
    gpu=i.get('gpu_name','?')
    dph=i.get('dph_total',0)
    geo=i.get('geolocation') or '?'
    msg=(i.get('status_msg') or '')[:50]
    print('%s: %8s | intended:%7s | %s | $%.2f/hr | %s' % (iid, actual, intended, gpu, dph, geo))
    if msg: print('      msg: %s' % msg)

print()
print('='*70)
print('ACCOUNT CHECK')
print('='*70)

result = subprocess.run(['vastai', 'show', 'user', '--raw'], capture_output=True, text=True)
user = json.loads(result.stdout)
print('Balance: $%.2f' % float(user.get('balance', 0) or 0))
print('Credit: $%.2f' % float(user.get('credit', 0) or 0))
print('Total: $%.2f' % (float(user.get('balance', 0) or 0) + float(user.get('credit', 0) or 0)))
