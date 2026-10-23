import requests, time, json

for i in range(40):
    resp = requests.get("http://localhost:8000/api/leaderboard/status")
    d = resp.json()
    run = d.get("last_run") or {}
    attempted = run.get("attempted", 0)
    succeeded = run.get("succeeded", 0)
    failed = run.get("failed", 0)
    status = run.get("status", "?")
    running = d["running"]
    print(f"[{i*10}s] running={running} | attempted={attempted} succeeded={succeeded} failed={failed} status={status}")
    if not running:
        print("Done!")
        break
    time.sleep(10)
