"""Network probes for Telegram without printing secrets."""
from __future__ import annotations

import socket

import httpx

TARGETS = [
    ("resolved_v4", "149.154.166.110"),
    ("dc_v4", "149.154.167.50"),
]

HAPP_PORTS = list(range(10800, 10820)) + [2080, 7890, 1080, 12334, 12335, 20170, 20171, 20172, 6152, 6153, 2334, 2335]


def tcp(host: str, port: int, timeout: float = 4.0) -> str:
    try:
        s = socket.create_connection((host, port), timeout)
        s.close()
        return "ok"
    except OSError as exc:
        return type(exc).__name__


def main() -> None:
    print("tcp443_resolved", tcp("149.154.166.110", 443))
    try:
        s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        s.settimeout(6)
        s.connect(("2001:67c:4e8:f004::9", 443, 0, 0))
        s.close()
        print("tcp443_v6 ok")
    except OSError as exc:
        print("tcp443_v6", type(exc).__name__)

    try:
        r = httpx.get("https://api.telegram.org", timeout=httpx.Timeout(8.0), transport=httpx.HTTPTransport(local_address="::"))
        print("http_v6", r.status_code)
    except Exception as exc:
        print("http_v6", type(exc).__name__)

    open_ports = []
    for p in HAPP_PORTS:
        s = socket.socket()
        s.settimeout(0.15)
        try:
            s.connect(("127.0.0.1", p))
            open_ports.append(p)
        except OSError:
            pass
        finally:
            s.close()
    print("happ_like_ports", open_ports or "none")


if __name__ == "__main__":
    main()
