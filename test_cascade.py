import requests
resp = requests.post(
    "http://localhost:8319/v1/chat/completions",
    headers={"Content-Type": "application/json", "Authorization": "Bearer sk-cascade-1"},
    json={"model": "cascade", "messages": [{"role":"user","content":"say hi"}], "max_tokens": 5}
)
print(f"Status: {resp.status_code}")
print(f"Provider: {resp.json().get('model')}")
