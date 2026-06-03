#!/usr/bin/env python3
"""Check miners on all running servers, install if missing."""
import json
import subprocess
import threading
import time

WALLET = "YOUR_WALLET_ADDRESS"

def get_instances():
    r = subprocess.run(['vastai', 'show', 'instances', '--raw'], capture_output=True, text=True)
    return json.loads(r.stdout)

def check_and_install(inst):
    iid = inst.get('id')
    host = inst.get('ssh_host', '')
    port = inst.get('ssh_port', '')
    gpu = inst.get('gpu_name', '?')
    dph = inst.get('dph_total', 0)
    
    print(f"\n=== {iid} ({gpu} @ ${dph:.3f}/hr) ===")
    
    if not host or not port:
        print("  [SKIP] No SSH info")
        return
    
    # Check miner log
    log_check = subprocess.run(
        ['ssh', '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=no',
         f'root@{host}', '-p', str(port), 
         'ls -la /var/log/alpha-miner.log 2>/dev/null || echo NO_LOG'],
        capture_output=True, text=True, timeout=10
    )
    
    if 'NO_LOG' not in log_check.stdout:
        print("  [LOG] EXISTS")
        # Check hashrate
        hashrate_check = subprocess.run(
            ['ssh', '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=no',
             f'root@{host}', '-p', str(port),
             'tail -5 /var/log/alpha-miner.log 2>/dev/null | grep hashrate_th_s | tail -1'],
            capture_output=True, text=True, timeout=10
        )
        if 'hashrate_th_s=' in hashrate_check.stdout:
            try:
                hr = hashrate_check.stdout.split('hashrate_th_s=')[1].split()[0]
                print(f"  [HASHRATE] {hr} TH/s - OK")
                return
            except:
                pass
        print("  [HASHRATE] Not found in log")
    else:
        print("  [LOG] NO LOG - NEEDS INSTALL")
    
    # Check binary
    bin_check = subprocess.run(
        ['ssh', '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=no',
         f'root@{host}', '-p', str(port),
         'ls -la /usr/bin/alpha-miner 2>/dev/null || echo NO_BINARY'],
        capture_output=True, text=True, timeout=10
    )
    
    if 'NO_BINARY' in bin_check.stdout:
        print("  [BINARY] NOT FOUND - INSTALLING...")
        # Install
        geo = inst.get('geolocation', '')
        pool = "eu1" if any(x in geo for x in ["EU","DE","FR","HU","BG","CZ","RO","MK"]) else "us2"
        host_pool = f"{pool}.alphapool.tech"
        worker = f"vast-{iid}"
        
        _DIFF = {"RTX 5090": 1048576, "RTX 5080": 524288, "RTX 4090": 524288,
                 "RTX 3090": 262144, "RTX 3080": 262144}
        diff = next((v for k, v in _DIFF.items() if k in gpu), 262144)
        
        # Download and start
        install_cmd = (
            f"mkdir -p /var/log && "
            f"curl -sL -o /usr/bin/alpha-miner https://pearl.alphapool.tech/downloads/alpha-miner-beta-174 && "
            f"chmod +x /usr/bin/alpha-miner && "
            f"nohup alpha-miner --pool stratum+tcp://{host_pool}:5566 --address {WALLET} --worker {worker} "
            f"--password 'x;d={diff}' --status-interval 30 >> /var/log/alpha-miner.log 2>&1 &"
        )
        
        install = subprocess.run(
            ['ssh', '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=no',
             f'root@{host}', '-p', str(port), install_cmd],
            capture_output=True, text=True, timeout=120
        )
        
        if install.returncode == 0:
            print(f"  [INSTALL] STARTED (Pool: {host_pool})")
        else:
            print(f"  [INSTALL] ERROR: {install.stderr[:100]}")
    else:
        print("  [BINARY] EXISTS - RESTARTING...")
        # Just restart
        geo = inst.get('geolocation', '')
        pool = "eu1" if any(x in geo for x in ["EU","DE","FR","HU","BG","CZ","RO","MK"]) else "us2"
        host_pool = f"{pool}.alphapool.tech"
        worker = f"vast-{iid}"
        
        _DIFF = {"RTX 5090": 1048576, "RTX 5080": 524288, "RTX 4090": 524288,
                 "RTX 3090": 262144, "RTX 3080": 262144}
        diff = next((v for k, v in _DIFF.items() if k in gpu), 262144)
        
        restart_cmd = (
            f"pkill -9 alpha-miner 2>/dev/null; "
            f"nohup alpha-miner --pool stratum+tcp://{host_pool}:5566 --address {WALLET} --worker {worker} "
            f"--password 'x;d={diff}' --status-interval 30 >> /var/log/alpha-miner.log 2>&1 &"
        )
        
        restart = subprocess.run(
            ['ssh', '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=no',
             f'root@{host}', '-p', str(port), restart_cmd],
            capture_output=True, text=True, timeout=30
        )
        print(f"  [RESTART] DONE (Pool: {host_pool})")

def main():
    print("=" * 70)
    print("CHECKING ALL RUNNING SERVERS")
    print("=" * 70)
    
    instances = get_instances()
    running = [i for i in instances if i.get('actual_status') == 'running']
    
    print(f"\nTotal running: {len(running)}")
    
    # Check all in parallel
    threads = []
    for inst in running:
        t = threading.Thread(target=check_and_install, args=(inst,))
        t.start()
        threads.append(t)
        time.sleep(0.2)  # Small delay to avoid overwhelming
    
    for t in threads:
        t.join()
    
    print("\n" + "=" * 70)
    print("CHECK COMPLETE - Wait 2-3 minutes for miners to start")
    print("=" * 70)

if __name__ == "__main__":
    main()
