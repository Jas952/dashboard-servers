#!/bin/bash
# Update all miners to v1.7.6

NEW_MINER_URL="https://github.com/AlphaMine-Tech/compute-agent/releases/download/v1.7.6-beta/alpha-V1.7.6.20260530.tar.gz"
WALLET="YOUR_WALLET_ADDRESS"
POOL="us2.alphapool.tech:5566"

# Get all running instances
echo "=== Fetching servers from Vast.ai ==="
vastai show instances --raw > /tmp/instances.json

# Update each server
python3 << 'PYEOF'
import json
import subprocess

with open('/tmp/instances.json') as f:
    data = json.load(f)

tested = [38453923, 38450441, 38585668, 38628869]  # Already updated

for i in data:
    if i.get('actual_status') != 'running':
        continue
    iid = i.get('id')
    if iid in tested:
        print(f"⏭️  {iid}: Already updated (tested)")
        continue
    
    host = i.get('ssh_host', '')
    port = i.get('ssh_port', '')
    gpu = i.get('gpu_name', '?')
    
    if not host or not port:
        print(f"❌ {iid}: No SSH info")
        continue
    
    print(f"🔄 {iid}: Updating {gpu}...")
    
    cmd = f"""ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no -n -T root@{host} -p {port} 'pkill -9 compute-agent 2>/dev/null; cd /tmp && curl -fsSL "{NEW_MINER_URL}" -o alpha-new.tar.gz && tar -xzf alpha-new.tar.gz && cp compute-agent /usr/bin/compute-agent && chmod +x /usr/bin/compute-agent && nohup /usr/bin/compute-agent --pool {POOL} --wallet {WALLET} --worker vast-{iid} --password "x;d=262144" > /var/log/compute-agent.log 2>&1 &'"""
    
    result = subprocess.run(cmd, shell=True, capture_output=True, timeout=30)
    if result.returncode == 0:
        print(f"✅ {iid}: Updated successfully")
    else:
        print(f"⚠️  {iid}: SSH issue (may need manual check)")

print("\n=== Done! Wait 3-5 minutes for miners to start ===")
PYEOF
