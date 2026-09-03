"""Deploy SptBot to VPS via SSH. Reads host/user/password from env."""
from __future__ import annotations

import os
import posixpath
import sys
import time
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parent
HOST = os.environ["DEPLOY_HOST"]
USER = os.environ.get("DEPLOY_USER", "root")
PASSWORD = os.environ["DEPLOY_PASSWORD"]
REMOTE_DIR = os.environ.get("DEPLOY_DIR", "/opt/sptbot")

SKIP_DIRS = {".venv", "venv", "__pycache__", ".git", "data", ".idea", ".vscode"}
SKIP_FILES = {
    "_probe_net.py",
    "_probe_proxy_ports.py",
    "_check_token.py",
    "debug_parse.py",
    "parse_check.json",
    "deploy_remote.py",
}
SKIP_SUFFIX = {".pyc", ".pyo", ".db", ".db-journal", ".log"}


def connect() -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        HOST,
        username=USER,
        password=PASSWORD,
        timeout=60,
        banner_timeout=60,
        auth_timeout=60,
        allow_agent=False,
        look_for_keys=False,
    )
    return client


def run(client: paramiko.SSHClient, cmd: str, check: bool = True) -> tuple[int, str, str]:
    stdin, stdout, stderr = client.exec_command(cmd, timeout=600)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    code = stdout.channel.recv_exit_status()
    if check and code != 0:
        raise RuntimeError(f"cmd failed ({code}): {cmd}\n{err or out}")
    return code, out, err


def should_upload(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    if any(part in SKIP_DIRS for part in rel.parts):
        return False
    if path.name in SKIP_FILES:
        return False
    if path.suffix in SKIP_SUFFIX:
        return False
    return True


def upload_tree(sftp: paramiko.SFTPClient) -> int:
    count = 0
    try:
        sftp.stat(REMOTE_DIR)
    except FileNotFoundError:
        sftp.mkdir(REMOTE_DIR)

    for path in ROOT.rglob("*"):
        if not path.is_file() or not should_upload(path):
            continue
        rel = path.relative_to(ROOT).as_posix()
        remote = posixpath.join(REMOTE_DIR, rel)
        remote_parent = posixpath.dirname(remote)
        _mkdir_p(sftp, remote_parent)
        sftp.put(str(path), remote)
        count += 1
    return count


def _mkdir_p(sftp: paramiko.SFTPClient, remote: str) -> None:
    parts = remote.strip("/").split("/")
    cur = ""
    for part in parts:
        cur = f"{cur}/{part}" if cur else f"/{part}"
        try:
            sftp.stat(cur)
        except FileNotFoundError:
            sftp.mkdir(cur)


SERVICE = """[Unit]
Description=SPT Schedule Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={dir}
Environment=PYTHONUNBUFFERED=1
ExecStart={dir}/.venv/bin/python bot.py
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
"""


def main() -> None:
    print(f"connecting {USER}@{HOST} ...")
    client = connect()
    print("ssh ok")
    try:
        code, out, _ = run(client, "uname -a; python3 --version || true", check=False)
        print(out.strip())

        run(client, f"mkdir -p {REMOTE_DIR}")
        sftp = client.open_sftp()
        try:
            n = upload_tree(sftp)
            print(f"uploaded {n} files to {REMOTE_DIR}")
        finally:
            sftp.close()

        # Ensure .env exists on server with token from local .env if present
        local_env = ROOT / ".env"
        if local_env.exists():
            sftp = client.open_sftp()
            try:
                sftp.put(str(local_env), posixpath.join(REMOTE_DIR, ".env"))
                print("uploaded .env")
            finally:
                sftp.close()

        service_unit = SERVICE.format(dir=REMOTE_DIR)
        setup = f"""
set -e
cd {REMOTE_DIR}
export DEBIAN_FRONTEND=noninteractive
if command -v apt-get >/dev/null; then
  apt-get update -qq
  apt-get install -y -qq python3 python3-venv python3-pip >/dev/null
fi
python3 -m venv .venv
.venv/bin/pip install -q -U pip
.venv/bin/pip install -q -r requirements.txt
# quick schedule fetch check
.venv/bin/python - <<'PY'
import asyncio
from schedule import ScheduleHub
async def main():
    h = ScheduleHub()
    try:
        await h.refresh_all()
        for cid, s in h.services.items():
            print('corpus', cid, 'groups', len(s.by_kind.get('group', [])), s.updated_label)
    finally:
        await h.close()
asyncio.run(main())
PY
# telegram getMe without printing token (non-fatal if API slow)
.venv/bin/python - <<'PY'
import os
from pathlib import Path
from dotenv import load_dotenv
import httpx
load_dotenv(Path({REMOTE_DIR!r}) / '.env', override=True)
token = os.getenv('TELEGRAM_BOT_TOKEN','')
try:
    r = httpx.get(f'https://api.telegram.org/bot{{token}}/getMe', timeout=60)
    data = r.json()
    if data.get('ok'):
        print('telegram_ok', '@' + data['result'].get('username',''))
    else:
        print('telegram_warn', data)
except Exception as exc:
    print('telegram_skip', type(exc).__name__)
PY
# weather smoke (non-fatal)
.venv/bin/python - <<'PY'
import asyncio
import weather
try:
    line = asyncio.run(weather.kemerovo_weather_line())
    print('weather_ok', line.encode('ascii','replace').decode('ascii'))
except Exception as exc:
    print('weather_skip', type(exc).__name__)
PY
cat > /etc/systemd/system/sptbot.service <<'EOF'
{service_unit}
EOF
systemctl daemon-reload
systemctl enable sptbot
systemctl restart sptbot
sleep 3
systemctl is-active sptbot
systemctl --no-pager -l status sptbot | head -n 30
"""
        print("installing on server (venv, deps, systemd)...")
        code, out, err = run(client, setup, check=False)
        safe = (out or "").encode("ascii", "replace").decode("ascii")
        print(safe)
        if err.strip():
            print("stderr:", err[-2000:].encode("ascii", "replace").decode("ascii"))
        if code != 0:
            raise SystemExit(f"remote setup failed: {code}")
        print("deploy done")
    finally:
        client.close()


if __name__ == "__main__":
    main()
