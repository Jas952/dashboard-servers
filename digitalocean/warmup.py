import requests
import time

API_KEY = "YOUR_DO_API_TOKEN"
HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json"
}
BASE_URL = "https://api.digitalocean.com/v2"

def api_call(method, endpoint, json_data=None):
    url = f"{BASE_URL}{endpoint}"
    while True:
        try:
            if method == "POST":
                r = requests.post(url, headers=HEADERS, json=json_data, timeout=10)
            elif method == "GET":
                r = requests.get(url, headers=HEADERS, timeout=10)
                
            if r.status_code == 429:
                sleep_time = int(r.headers.get("Retry-After", 5))
                print(f"[!] Rate limited. Sleeping for {sleep_time}s...")
                time.sleep(sleep_time)
                continue
            r.raise_for_status()
            return r.json() if r.text else {}
        except Exception as e:
            print(f"[!] Error on {endpoint}: {e}")
            if "r" in locals() and hasattr(r, "text"):
                print("Response:", r.text)
            return None

print("Fetching droplets...")
droplets_data = api_call("GET", "/droplets?per_page=100")
if not droplets_data:
    print("Failed to fetch droplets.")
    exit(1)

droplet_ids = [d["id"] for d in droplets_data.get("droplets", [])]
droplet_urns = [f"do:droplet:{d_id}" for d_id in droplet_ids]
print(f"Found {len(droplet_ids)} droplets: {droplet_ids}")

print("\nCreating Project 'Neural Ingest Engine'...")
proj_data = api_call("POST", "/projects", {
    "name": "Neural Ingest Engine",
    "purpose": "Machine learning",
    "environment": "Development",
    "description": "Data processing pipeline for ML inference"
})
if proj_data and "project" in proj_data:
    proj_id = proj_data["project"]["id"]
    print(f"Project created with ID: {proj_id}")
    
    if droplet_urns:
        print("Moving droplets to project...")
        api_call("POST", f"/projects/{proj_id}/resources", {"resources": droplet_urns})
        print("Droplets moved.")
else:
    print("Failed to create project (maybe it already exists?).")

print("\nCreating Firewall 'ml-workers-firewall'...")
fw_data = api_call("POST", "/firewalls", {
    "name": "ml-workers-firewall",
    "inbound_rules": [
        {"protocol": "tcp", "ports": "22", "sources": {"addresses": ["0.0.0.0/0", "::/0"]}}
    ],
    "outbound_rules": [
        {"protocol": "tcp", "ports": "all", "destinations": {"addresses": ["0.0.0.0/0", "::/0"]}},
        {"protocol": "udp", "ports": "all", "destinations": {"addresses": ["0.0.0.0/0", "::/0"]}},
        {"protocol": "icmp", "destinations": {"addresses": ["0.0.0.0/0", "::/0"]}}
    ],
    "droplet_ids": droplet_ids
})
if fw_data:
    print("Firewall created successfully.")
else:
    print("Failed to create firewall.")

print("\nDone.")
