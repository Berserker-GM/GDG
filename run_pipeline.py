import requests, time, json

BASE = "http://localhost:8000"
TOKEN = "change-me-to-a-long-random-string"
HEADERS = {"X-Admin-Token": TOKEN}
XLSX = r"C:\Users\paras\OneDrive\Desktop\GDG_cloud_campaign.xlsx"

# 1. Check current status
status = requests.get(f"{BASE}/api/leaderboard/status").json()
print(f"Active members in DB: {status['active_members']}")

# 2. Import if DB is empty
if status["active_members"] == 0:
    print("\nImporting form...")
    with open(XLSX, "rb") as f:
        resp = requests.post(
            f"{BASE}/api/admin/import",
            headers=HEADERS,
            files={"form": ("GDG_cloud_campaign.xlsx", f,
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
        )
    rpt = resp.json()
    print(f"  Created: {rpt['created']}  Updated: {rpt['updated']}  Unique: {rpt['unique_students']}")
    if rpt.get("unsupported_links"):
        print(f"  Unsupported platform ({len(rpt['unsupported_links'])}): " +
              ", ".join(x["usn"] for x in rpt["unsupported_links"]))
    if rpt.get("usn_conflicts"):
        print(f"  USN conflicts resolved: {len(rpt['usn_conflicts'])}")
else:
    print("DB already populated — skipping import.")

# 3. Trigger refresh (scrape badges)
print("\nTriggering badge scrape...")
resp = requests.post(f"{BASE}/api/admin/refresh", headers=HEADERS)
if resp.status_code == 409:
    print("  A run is already in progress.")
else:
    run_id = resp.json()["run_id"]
    print(f"  Run #{run_id} started.")

# 4. Poll until done
print("\nWaiting for scrape to complete (this may take a few minutes)...")
for i in range(120):
    time.sleep(5)
    st = requests.get(f"{BASE}/api/leaderboard/status").json()
    run = st.get("last_run") or {}
    attempted = run.get("attempted", 0)
    succeeded = run.get("succeeded", 0)
    failed = run.get("failed", 0)
    run_status = run.get("status", "?")
    total = st["active_members"]
    print(f"  [{(i+1)*5}s] {attempted}/{total} fetched  ok={succeeded}  fail={failed}  status={run_status}")
    if not st["running"]:
        print(f"\nScrape finished with status: {run_status}")
        break

# 5. Print leaderboard
print("\n" + "="*70)
print("LEADERBOARD")
print("="*70)
lb = requests.get(f"{BASE}/api/leaderboard?limit=100").json()
entries = lb.get("entries", [])
if not entries:
    print("No published results yet.")
else:
    print(f"{'Rank':<5} {'Name':<30} {'Score':>7}  {'Badges':>6}  Dept")
    print("-"*70)
    for e in entries:
        dept = e.get("breakdown", {}).get("department") or e.get("department") or ""
        print(f"{e['rank']:<5} {e['name']:<30} {e['score']:>7.1f}  {e['counted_badges']:>6}  {dept}")

print(f"\nTotal entries: {len(entries)}")
print(f"Published at:  {lb.get('published_at')}")
print(f"Mode:          {lb.get('mode')}")
