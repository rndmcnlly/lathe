"""Instrumentation for Python DNS/TCP calls, not a native-traffic firewall."""
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import traceback


def install_egress_guard(*, controller=False):
    names = {"localhost", os.environ["PGHOST"], os.environ["REDIS_HOST"]}
    if os.environ.get("READINESS_EGRESS") == "live":
        names.update({"app.daytona.io", "proxy.app.daytona.io"})
        if controller:
            names.add("openrouter.ai")
    addresses = {"127.0.0.1", "::1"}
    original_lookup = socket.getaddrinfo
    for name in names:
        addresses.update(row[4][0] for row in original_lookup(name, None))

    def reject(operation, host):
        safe_host = str(host)
        if not re.fullmatch(r"[A-Za-z0-9.:-]{1,253}", safe_host):
            safe_host = "non-hostname"
        origin = [{"file": Path(f.filename).name, "function": f.name, "line": f.lineno}
                  for f in traceback.extract_stack(limit=6)[:-1]]
        with Path("/run/readiness/egress-denied").open("a") as stream:
            stream.write(json.dumps({"operation": operation, "host": safe_host, "origin": origin}) + "\n")
        raise OSError("Readiness infrastructure forbids external network access")

    def lookup(host, *args, **kwargs):
        if host is not None and host not in names and host not in addresses:
            reject("lookup", host)
        rows = original_lookup(host, *args, **kwargs)
        if host in names:
            addresses.update(row[4][0] for row in rows)
        return rows

    socket.getaddrinfo = lookup
    for method in ("connect", "connect_ex"):
        original = getattr(socket.socket, method)

        def connect(sock, address, _original=original):
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                host = address[0]
                if host not in addresses:
                    try:
                        allowed = ipaddress.ip_address(host).is_loopback
                    except ValueError:
                        allowed = False
                    if not allowed:
                        reject("connect", host)
            return _original(sock, address)

        setattr(socket.socket, method, connect)
