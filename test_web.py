import urllib.request
import json

base = "http://127.0.0.1:8080"

# Dashboard
r = urllib.request.urlopen(f"{base}/")
print(f"GET / → {r.status} ({len(r.read())} bytes)")

# API stats
r = urllib.request.urlopen(f"{base}/api/stats")
data = json.loads(r.read())
print(f"GET /api/stats → {data}")

# Videos page
r = urllib.request.urlopen(f"{base}/videos")
print(f"GET /videos → {r.status}")

# Trends API
r = urllib.request.urlopen(f"{base}/api/trends/1")
data = json.loads(r.read())
print(f"GET /api/trends/1 → {len(data.get('datasets',[]))} datasets, {len(data.get('labels',[]))} labels")

print("\n✓ Web 服务正常")
