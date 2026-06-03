import json, sys, time, subprocess, os
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)

BLACKLIST_FILE = os.path.join(ROOT_DIR, "data", "blacklist.json")

def load_bl():
    try:
        with open(BLACKLIST_FILE) as f: return json.load(f)
    except: return {}

def save_bl(bl):
    with open(BLACKLIST_FILE, "w") as f: json.dump(bl, f, indent=2)

r = subprocess.run(["vastai","show","instances-v1","--raw"], capture_output=True, text=True)
data = json.loads(r.stdout)
insts = data.get("instances", data) if isinstance(data, dict) else data

BAD_GEO = ["Iceland","Argentina","Türkiye","Macedonia","Turkey","Thailand","Japan","Brazil","Croatia"]

kill_ids = []
bl = load_bl()

for i in insts:
    st  = i.get("actual_status") or "unknown"
    iid = i["id"]
    mid = str(i.get("machine_id","?"))
    sd  = i.get("start_date", 0)
    up  = int((time.time()-sd)/60) if sd else 0
    geo = i.get("geolocation") or "?"
    gpu = i.get("gpu_name","?")

    bad    = False
    reason = ""

    if st in ("unknown","exited","error","offline"):
        bad = True; reason = f"Status={st}"
    elif st == "loading" and up > 10:
        bad = True; reason = f"Stuck loading {up}m"
    elif st == "created" and up > 15:
        bad = True; reason = f"Stuck created {up}m"
    elif any(x in geo for x in BAD_GEO):
        bad = True; reason = f"Bad geo: {geo}"

    if bad:
        kill_ids.append(iid)
        if mid != "?":
            bl[mid] = {
                "reason": reason,
                "gpu": gpu,
                "geo": geo,
                "added": datetime.utcnow().isoformat()
            }
        print(f"  KILL {iid}  {st:<10} up={up:>3}m  mid={mid}  {geo}  [{reason}]")

print(f"\nTotal to kill: {len(kill_ids)}")
save_bl(bl)
print(f"Blacklisted {len(bl)} machine_ids saved to {BLACKLIST_FILE}")

for iid in kill_ids:
    res = subprocess.run(["vastai","destroy","instance",str(iid)], input="y\n",
                         capture_output=True, text=True)
    ok = "destroying" in res.stdout.lower()
    print(f"  {'✅' if ok else '❌'} Destroyed {iid}")
