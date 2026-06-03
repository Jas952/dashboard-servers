import json, subprocess

# 1. Get vastai instances
res = subprocess.run(["vastai", "show", "instances", "--raw"], capture_output=True, text=True)
instances = json.loads(res.stdout)

import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)

# 2. Get history
try:
    history_path = os.path.join(ROOT_DIR, 'data', 'history.json')
    with open(history_path, 'r') as f:
        history = json.load(f)
except:
    history = {}

print(f"{'ID':<10} | {'GPU':<15} | {'Cost/hr':<8} | {'HR (TH/s)':<10} | {'Efficiency ($/100TH)':<20} | {'Status'}")
print("-" * 80)

results = []
for inst in instances:
    iid = str(inst['id'])
    cost = inst['dph_total']
    gpu = f"{inst['num_gpus']}x {inst['gpu_name']}"
    
    hr_list = history.get(iid, [])
    # Filter out nulls and 0.0 to get the latest valid hashrate
    valid_hr = [x for x in hr_list if x is not None and x > 0]
    
    if valid_hr:
        hr = valid_hr[-1]
        eff = (cost / hr) * 100
        status = "Active"
    else:
        hr = 0
        eff = 999.99
        status = "No HR Data (Warming up or Dead)"
        
    results.append({
        'id': iid, 'gpu': gpu, 'cost': cost, 'hr': hr, 'eff': eff, 'status': status
    })

results.sort(key=lambda x: x['eff'])

for r in results:
    eff_str = f"${r['eff']:.3f}" if r['hr'] > 0 else "N/A"
    print(f"{r['id']:<10} | {r['gpu']:<15} | ${r['cost']:<7.3f} | {r['hr']:<10.2f} | {eff_str:<20} | {r['status']}")

