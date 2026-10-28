"""Synthetic live-policy HTTPX transport gate: loopback only, never platform acceptance."""
import asyncio
import json
import os
from pathlib import Path
import socket
import tempfile
from unittest.mock import patch

import httpx
from aiohttp import web

from guard import canonical_host, install_egress_guard


async def qualify():
    """Use real HTTPX DNS/TCP/HTTP, with only the DNS answers replaced locally.

    This runs in a separate controller subprocess before its real guard is
    installed. Patches are scoped even when invoked directly by offline tests.
    No MockTransport, credentials, platform TLS, provisioning or inference.
    """
    requests, inputs, resolutions = [], [], []
    payload = b"synthetic-independent-download\x00\xff"

    async def serve(request):
        requests.append({"host": request.host, "path": request.path})
        if request.path == "/toolbox/fixture/files/download":
            return web.Response(body=payload)
        return web.json_response({"synthetic_loopback_fixture": True, "path": request.path})

    app = web.Application()
    app.router.add_get("/{path:.*}", serve)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        original_lookup = socket.getaddrinfo
        fixture_names = {"localhost", "fixture-pg", "fixture-redis",
                         "app.daytona.io", "proxy.app.daytona.io", "openrouter.ai"}

        def mapped_lookup(host, *args, **kwargs):
            # Never fall back to internet DNS, including on a guard regression.
            name = canonical_host(host)
            if name not in fixture_names | {"127.0.0.1", "::1"}:
                raise AssertionError("fixture DNS received a forbidden host")
            resolutions.append(name)
            return original_lookup("127.0.0.1", *args, **kwargs)

        with tempfile.TemporaryDirectory(prefix="readiness-transport-") as temporary:
            denied_path = Path(temporary) / "denied.jsonl"
            def denials():
                return [json.loads(line) for line in denied_path.read_text().splitlines()] if denied_path.exists() else []

            # Preserve hooks before installation so no guard or DNS mapping leaks.
            with patch.dict(os.environ, {"PGHOST": "fixture-pg", "REDIS_HOST": "fixture-redis",
                                        "READINESS_EGRESS": "live"}), \
                 patch.object(socket, "getaddrinfo", mapped_lookup), \
                 patch.object(socket.socket, "connect", socket.socket.connect), \
                 patch.object(socket.socket, "connect_ex", socket.socket.connect_ex):
                install_egress_guard(controller=True, denied_path=denied_path)
                guarded_lookup = socket.getaddrinfo

                def observed_lookup(host, *args, **kwargs):
                    inputs.append(type(host).__name__)
                    return guarded_lookup(host, *args, **kwargs)

                with patch.object(socket, "getaddrinfo", observed_lookup):
                    async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
                        # These are the same default HTTPX adapter/response APIs
                        # used by provisioning and independent toolbox downloads.
                        for host in ("app.daytona.io", "APP.DAYTONA.IO."):
                            response = await client.get(f"http://{host}:{port}/api/snapshots/daytona-small")
                            response.raise_for_status()
                            if response.json() != {"synthetic_loopback_fixture": True,
                                                   "path": "/api/snapshots/daytona-small"}:
                                raise RuntimeError("synthetic snapshot transport mismatch")
                        response = await client.get(f"http://proxy.app.daytona.io:{port}/toolbox/fixture/files/download",
                                                    params={"path": "/synthetic-proof.bin"})
                        response.raise_for_status()
                        if response.content != payload:
                            raise RuntimeError("independent binary download transport mismatch")
                        response = await client.get(f"http://openrouter.ai:{port}/api/v1/models")
                        response.raise_for_status()
                        if not response.json().get("synthetic_loopback_fixture"):
                            raise RuntimeError("controller-only provider transport mismatch")

                        for host, reason in (("app.daytona.io.attacker.invalid", "not_allowed"),
                                             ("evilapp.daytona.io", "not_allowed"),
                                             ("bücher.invalid", "not_allowed"),
                                             ("203.0.113.1", "not_allowed"),
                                             ("app.daytona.io%00", "malformed_hostname")):
                            before, served = len(denials()), len(requests)
                            try:
                                await client.get(f"http://{host}:{port}/must-not-arrive")
                            except httpx.ConnectError:
                                pass
                            except httpx.TimeoutException as exc:
                                raise RuntimeError("guard rejection obscured by client timeout") from exc
                            else:
                                raise RuntimeError("forbidden fixture request was admitted")
                            rejected = denials()[before:]
                            if len(rejected) != 1 or rejected[0]["reason"] != reason or len(requests) != served:
                                raise RuntimeError("request failed without an observed guard rejection")

                    httpx_inputs = sorted(set(inputs))
                    if "bytes" not in httpx_inputs:
                        raise RuntimeError("actual HTTPX bytes-host lookup was not exercised")
                    # Socket APIs also genuinely accept string/case/root-dot
                    # representations. HTTPX itself lowercases URL hosts first.
                    for host in ("APP.DAYTONA.IO.", b"APP.DAYTONA.IO."):
                        socket.getaddrinfo(host, port)
                    before = len(denials())
                    try:
                        socket.getaddrinfo(b"app.daytona.io\xff", port)
                    except OSError as exc:
                        if "malformed hostname representation" not in str(exc):
                            raise
                    else:
                        raise RuntimeError("malformed ASCII bytes accepted")
                    if len(denials()) != before + 1 or denials()[-1]["reason"] != "malformed_hostname":
                        raise RuntimeError("malformed bytes rejection was not observed")
                if len(requests) != 4:
                    raise RuntimeError("actual HTTPX bytes-host transport was not exercised")
                return {"status": "passed", "synthetic_loopback_only": True,
                        "policy": "live-controller", "adapter": "httpx.AsyncClient/default HTTP transport",
                        "http_requests": requests, "lookup_input_types": sorted(set(inputs)),
                        "httpx_lookup_input_types": httpx_inputs,
                        "dns_answers": "127.0.0.1 only", "resolved_names": sorted(set(resolutions)),
                        "observed_denials": denials(), "binary_download_verified": True,
                        "platform_acceptance": False, "provider_requests": 0}
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(qualify())))
