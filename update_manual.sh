#!/bin/bash
# Manual miner update script - run this in terminal
# Updates all remaining servers to v1.7.6

WALLET="YOUR_WALLET_ADDRESS"
POOL="us2.alphapool.tech:5566"
MINER_URL="https://github.com/AlphaMine-Tech/compute-agent/releases/download/v1.7.6-beta/alpha-V1.7.6.20260530.tar.gz"

# Already updated (4 test servers): 38453923 38450441 38585668 38628869

echo "=== Manual Miner Update v1.7.6 ==="
echo "Run each line manually in terminal:"
echo ""

# Get SSH info and print commands
vastai show instances --raw | python3 -c "
import json,sys
data=json.load(sys.stdin)
tested=[38453923,38450441,38585668,38628869]
for i in data:
    if i.get('actual_status')=='running' and i.get('id') not in tested:
        iid=i.get('id')
        host=i.get('ssh_host','')
        port=i.get('ssh_port','')
        gpu=i.get('gpu_name','?')
        n=i.get('num_gpus',1)
        if host and port:
            cmd=f\"ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no -n -T root@{host} -p {port} 'pkill -9 compute-agent 2>/dev/null; sleep 1; cd /tmp && curl -fsSL {MINER_URL} -o alpha-new.tar.gz && tar -xzf alpha-new.tar.gz && cp compute-agent /usr/bin/compute-agent && chmod +x /usr/bin/compute-agent && nohup /usr/bin/compute-agent --pool {POOL} --wallet {WALLET} --worker vast-{iid} --password x\\;d=262144 > /var/log/compute-agent.log 2>&1 &'\"
            print(f'echo \"=== {iid} ({n}x {gpu}) ===\"')
            print(cmd)
            print(f\"echo '\u2705 {iid} done'\")
            print()
"
