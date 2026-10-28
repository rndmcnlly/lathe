"""Credential-free boundary checks executed inside the disposable app container."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys

import httpx
import socketio
import psycopg2

sys.path.insert(0, "/opt/staged")
from testing_support import EXPECTED_SCHEMA, normalize_schema


async def main():
    checks = []

    def require(condition, boundary):
        if not condition:
            raise RuntimeError(boundary)
        checks.append(boundary)
        Path("/run/readiness/checks.json").write_text(json.dumps(checks))

    async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
        async def request(port, method, path, token=None, **kwargs):
            response = await client.request(method, f"http://127.0.0.1:{port}{path}",
                headers={"Authorization": f"Bearer {token}"} if token else {}, **kwargs)
            if response.status_code >= 400:
                # Never include auth response bodies, passwords, or tokens in evidence.
                detail = ""
                if path == "/api/v1/tools/create":
                    value = response.json().get("detail")
                    if isinstance(value, str):
                        detail = ": " + value[:500]
                raise RuntimeError(f"{method} {path}: HTTP {response.status_code}" + detail)
            return response

        identities = []
        for port in (8081, 8082):
            for _ in range(120):
                try:
                    health = await request(port, "GET", "/health")
                    break
                except (httpx.TransportError, RuntimeError):
                    await asyncio.sleep(1)
            else:
                raise RuntimeError(f"worker {port} health timeout")
            identity_response = await request(port, "GET", "/_readiness/identity")
            identity = identity_response.json()
            identities.append(identity)
            pid = identity["pid"]
            require(health.headers["x-readiness-pid"] == str(pid)
                    == identity_response.headers["x-readiness-pid"], f"worker {port}: PID headers")
            require(b"uvicorn" in Path(f"/proc/{pid}/cmdline").read_bytes(),
                    f"worker {port}: actual uvicorn process")
            require((await request(port, "GET", "/api/version")).json()["version"] == "0.12.4",
                    f"worker {port}: version 0.12.4")
        require(identities[0]["pid"] != identities[1]["pid"], "two distinct OWUI processes")
        require(identities[0]["migration"] == identities[1]["migration"]
                and identities[0]["migration"]["count"] == 1
                and identities[0]["migration"]["pid"] not in {i["pid"] for i in identities},
                "one completed migration before both workers")
        require(all(i["shared_marker"] == os.environ["READINESS_RUN"] for i in identities),
                "shared ephemeral filesystem")
        worker_envs = []
        for identity in identities:
            raw = Path(f"/proc/{identity['pid']}/environ").read_bytes().split(b"\0")
            worker_envs.append(dict(item.split(b"=", 1) for item in raw if b"=" in item))
        shared_keys = ("DATABASE_URL", "PGVECTOR_DB_URL", "REDIS_URL", "WEBSOCKET_REDIS_URL",
                       "WEBUI_SECRET_KEY", "WEBUI_JWT_SECRET_KEY")
        require(all(worker_envs[0][key.encode()] == worker_envs[1][key.encode()]
                    == os.environ[key].encode() for key in shared_keys), "actual shared worker service/key environment")
        require(all(env[b"ENABLE_DB_MIGRATIONS"] == b"false" for env in worker_envs),
                "both actual workers have migrations disabled")
        with psycopg2.connect(os.environ["DATABASE_URL"]) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT '[1,2,3]'::vector <-> '[1,2,3]'::vector")
                require(cursor.fetchone() == (0.0,), "live PostgreSQL pgvector extension")
                cursor.execute("SELECT version_num FROM alembic_version")
                heads = {row[0] for row in cursor.fetchall()}
                from alembic.config import Config
                from alembic.script import ScriptDirectory
                config = Config("/app/backend/open_webui/alembic.ini")
                config.set_main_option("script_location", "/app/backend/open_webui/migrations")
                require(heads == set(ScriptDirectory.from_config(config).get_heads()),
                        "database migration head independently matches shipped Alembic scripts")

        admin = (await request(8081, "POST", "/api/v1/auths/signup", json={
            "name": "Infra Admin", "email": "admin@readiness.invalid",
            "password": secrets.token_urlsafe(32)})).json()
        require(admin["role"] == "admin", "separate synthetic administrator")
        user = (await request(8082, "POST", "/api/v1/auths/add", token=admin["token"], json={
            "name": "Infra Ordinary", "email": "ordinary@readiness.invalid",
            "password": secrets.token_urlsafe(32), "role": "user"})).json()
        require(user["role"] == "user" and user["id"] != admin["id"],
                "authenticated synthetic ordinary user across shared DB and signing key")
        source = Path("/opt/staged/lathe.py").read_text()
        await request(8081, "POST", "/api/v1/tools/create", token=admin["token"], json={
            "id": "lathe_infra", "name": "Lathe Infrastructure Preflight", "content": source,
            "meta": {"description": "No cloud execution"}, "access_grants": []})
        remote = (await request(8082, "GET", "/api/v1/tools/id/lathe_infra", token=admin["token"])).json()
        require(remote["content"] == source, "exact source loaded and independently read from other worker")
        specs = {item["name"]: normalize_schema(item["parameters"]) for item in remote["specs"]}
        require(specs == EXPECTED_SCHEMA, "complete independent public tool schema")
        valves = {"daytona_api_key": "", "deployment_label": "readiness-" + os.environ["READINESS_RUN"],
                  "auto_create_sandbox": False,
                  "sandbox_create_overrides": json.dumps({"snapshot": "daytona-small", "target": "us"}),
                  "handbook_url": "", "preview_wrapper_url": "", "preview_wrapper_key": ""}
        overrides = json.loads(valves["sandbox_create_overrides"])
        require(set(overrides) == {"snapshot", "target"}
                and not {"cpu", "memory", "disk"} & overrides.keys(), "snapshot-only staged overrides")
        await request(8081, "POST", "/api/v1/tools/id/lathe_infra/valves/update",
                      token=admin["token"], json=valves)
        readback = (await request(8082, "GET", "/api/v1/tools/id/lathe_infra/valves",
                                  token=admin["token"])).json()
        require(all(readback.get(k) == v for k, v in valves.items()), "independent staged valve readback")

        # Receive on B, emit via A's production Socket.IO manager to the real
        # authenticated user room. A local-only manager cannot pass this check.
        received = asyncio.Future()
        sio = socketio.AsyncClient(reconnection=False)

        @sio.on("readiness-proof")
        async def proof(data):
            if not received.done():
                received.set_result(data)

        try:
            await sio.connect("http://127.0.0.1:8082", socketio_path="ws/socket.io",
                              auth={"token": user["token"]}, transports=["websocket"], wait_timeout=10)
            joined = await sio.call("user-join", {"auth": {"token": user["token"]}}, timeout=10)
            require(joined["id"] == user["id"], "ordinary-user socket authentication")
            sent = (await request(8081, "POST", "/_readiness/emit", token=user["token"])).json()
            event = await asyncio.wait_for(received, 10)
            require(event == {"pid": identities[0]["pid"], "user_id": user["id"]} and sent["sent"],
                    "cross-process Redis socket delivery to ordinary-user room")
        finally:
            if sio.connected:
                await sio.disconnect()
        denied = Path("/run/readiness/egress-denied")
        attempts = denied.read_text() if denied.exists() else ""
        require(not attempts, "zero guard-observed Python external DNS/connect attempts: " + attempts[:2000])
        print(json.dumps({"status": "passed", "checks": checks, "identities": identities,
                          "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                          "testing_support_sha256": hashlib.sha256(Path("/opt/staged/testing_support.py").read_bytes()).hexdigest()}))


if __name__ == "__main__":
    asyncio.run(main())
