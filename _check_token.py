import os
from dotenv import load_dotenv
import httpx

load_dotenv(override=True)
token = os.getenv("TELEGRAM_BOT_TOKEN", "")
if not token:
    raise SystemExit("no token")
r = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=20)
data = r.json()
if not data.get("ok"):
    print("FAIL", data.get("description", r.status_code))
    raise SystemExit(1)
u = data["result"]
print("OK", "@" + str(u.get("username") or ""), u.get("first_name") or "", "id", u.get("id"))
