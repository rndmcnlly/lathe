"""Instrumentation for Python DNS/TCP calls, not a native-traffic firewall."""
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import traceback


def canonical_host(host):
    """One DNS/IP spelling for policy and resolution; bytes must be ASCII.

    DNS is case-insensitive, permits one terminal root dot, and accepts IDNA
    names. Do not use str(bytes), ignore decode errors, or strip arbitrary text.
    """
    if isinstance(host, bytes):
        host = host.decode("ascii")
    if not isinstance(host, str) or not host or "%" in host:
        raise ValueError("malformed hostname representation")
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    name = host.encode("idna").decode("ascii").lower().removesuffix(".")
    if len(name) > 253 or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                                  for label in name.split(".")):
        raise ValueError("malformed hostname representation")
    return name


def install_egress_guard(*, controller=False, denied_path="/run/readiness/egress-denied"):
    names = {canonical_host(name) for name in ("localhost", os.environ["PGHOST"], os.environ["REDIS_HOST"])}
    if os.environ.get("READINESS_EGRESS") == "live":
        names.update({"app.daytona.io", "proxy.app.daytona.io"})
        if controller:
            names.add("openrouter.ai")
    addresses = {"127.0.0.1", "::1"}
    original_lookup = socket.getaddrinfo
    for name in names:
        addresses.update(canonical_host(row[4][0]) for row in original_lookup(name, None))

    def reject(operation, host, reason="not_allowed"):
        safe_host = str(host)
        if not re.fullmatch(r"[A-Za-z0-9.:-]{1,253}", safe_host):
            safe_host = "non-hostname"
        origin = [{"file": Path(f.filename).name, "function": f.name, "line": f.lineno}
                  for f in traceback.extract_stack(limit=6)[:-1]]
        with Path(denied_path).open("a") as stream:
            stream.write(json.dumps({"operation": operation, "host": safe_host,
                                     "reason": reason, "origin": origin}) + "\n")
        if reason == "malformed_hostname":
            raise OSError("Readiness infrastructure rejected malformed hostname representation") from None
        raise OSError("Readiness infrastructure forbids external network access") from None

    def normalize(operation, host):
        try:
            return canonical_host(host)
        except (ValueError, UnicodeError):
            reject(operation, "non-hostname", "malformed_hostname")

    def lookup(host, *args, **kwargs):
        if host is not None:
            host = normalize("lookup", host)
        if host is not None and host not in names and host not in addresses:
            reject("lookup", host)
        rows = original_lookup(host, *args, **kwargs)
        if host in names:
            addresses.update(canonical_host(row[4][0]) for row in rows)
        return rows

    socket.getaddrinfo = lookup
    for method in ("connect", "connect_ex"):
        original = getattr(socket.socket, method)

        def connect(sock, address, _original=original):
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                host = normalize("connect", address[0])
                if host not in addresses:
                    try:
                        allowed = ipaddress.ip_address(host).is_loopback
                    except ValueError:
                        allowed = False
                    if not allowed:
                        reject("connect", host)
                address = (host, *address[1:])
            return _original(sock, address)

        setattr(socket.socket, method, connect)
