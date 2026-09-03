"""Check/restart sptbot on VPS. Credentials from env."""
from __future__ import annotations

import os
import sys

import paramiko

HOST = os.environ["DEPLOY_HOST"]
USER = os.environ.get("DEPLOY_USER", "root")
PASSWORD = os.environ["DEPLOY_PASSWORD"]


def main() -> None:
    cmd = " ".join(sys.argv[1:]) or "systemctl is-active sptbot; systemctl show sptbot -p ActiveState,SubState,MainPID --no-pager; journalctl -u sptbot -n 40 --no-pager -o cat"
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, username=USER, password=PASSWORD, timeout=30, allow_agent=False, look_for_keys=False)
    try:
        _, stdout, stderr = client.exec_command(cmd, timeout=120)
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        code = stdout.channel.recv_exit_status()
        sys.stdout.buffer.write(out.encode("utf-8", "replace"))
        if err.strip():
            sys.stdout.buffer.write(("\nSTDERR:\n" + err).encode("utf-8", "replace"))
        raise SystemExit(code)
    finally:
        client.close()


if __name__ == "__main__":
    main()
