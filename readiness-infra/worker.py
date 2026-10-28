"""Outer ASGI observation, deliberately before OWUI's SPA catch-all."""
import json
import os
from pathlib import Path
import hashlib
import time
import uuid


from guard import install_egress_guard
install_egress_guard()
from open_webui.main import app as owui  # noqa: E402

identity = {"worker": os.environ.get("READINESS_WORKER", "migration"),
            "pid": os.getpid(), "incarnation": uuid.uuid4().hex}


def note(kind, **data):
    if os.environ.get("READINESS_EGRESS") == "live":
        with Path(f"/lab/artifacts/worker-{identity['worker']}.jsonl").open("a") as stream:
            stream.write(json.dumps({"kind": kind, "time": time.time(), **identity, **data}) + "\n")


original_handler = owui.state.CHAT_COMPLETION_HANDLER


async def observe_handler(request, form_data, *args, **kwargs):
    note("native_handler_entry", chat_id=form_data.get("chat_id"), message_id=form_data.get("id"),
         internal=getattr(request.state, "internal", False), model=form_data.get("model"),
         tool_ids=form_data.get("tool_ids"), system_hash=hashlib.sha256(json.dumps(
             [m for m in form_data.get("messages", []) if m.get("role") == "system"],
             sort_keys=True).encode()).hexdigest())
    return await original_handler(request, form_data, *args, **kwargs)


owui.state.CHAT_COMPLETION_HANDLER = observe_handler


async def app(scope, receive, send):
    if scope["type"] != "http":
        return await owui(scope, receive, send)

    async def observed(message):
        if message["type"] == "http.response.start":
            message.setdefault("headers", []).append((b"x-readiness-pid", str(os.getpid()).encode()))
            message["headers"].extend([(b"x-lathe-lab-pid", str(identity["pid"]).encode()),
                (b"x-lathe-lab-incarnation", identity["incarnation"].encode())])
            if scope["path"] == "/api/chat/completions":
                note("http_completion", status=message["status"])
        await send(message)

    path = scope["path"]
    if path not in {"/_readiness/identity", "/_readiness/emit", "/_readiness/loaded"}:
        return await owui(scope, receive, observed)
    status = 200
    data = {**identity, "migration": json.loads(Path("/run/readiness/migration.json").read_text()),
            "shared_marker": Path("/app/backend/data/readiness-marker").read_text()}
    if path in {"/_readiness/emit", "/_readiness/loaded"}:
        from open_webui.utils.auth import get_verified_user_by_token
        from open_webui.socket.main import emit_to_users
        headers = dict(scope["headers"])
        token = headers.get(b"authorization", b"").decode().removeprefix("Bearer ")
        user = await get_verified_user_by_token(token, owui.state.redis)
        expected_method = "POST" if path.endswith("emit") else "GET"
        if not user or user.role != "user" or scope["method"] != expected_method:
            status, data = 403, {"error": "ordinary-user authentication required"}
        elif path.endswith("emit"):
            await emit_to_users("readiness-proof", {"pid": os.getpid(), "user_id": user.id}, [user.id])
            data = {"sent": True, "pid": os.getpid()}
        else:
            from starlette.requests import Request
            from open_webui.utils.tools import get_tools
            from open_webui.utils.plugin import get_tools_cache
            from open_webui.models.tools import Tools
            request = Request({**scope, "app": owui}, receive=receive)
            tools = await get_tools(request, ["lathe_test"], user, {"__user__": user.model_dump()})
            module = get_tools_cache(request)["lathe_test"]
            tool = await Tools.get_tool_by_id("lathe_test")
            data = {**identity, "tools": sorted(tools),
                    "valves_sha256": hashlib.sha256(json.dumps(module.valves.model_dump(), sort_keys=True).encode()).hexdigest(),
                    "source_sha256": hashlib.sha256(tool.content.encode()).hexdigest()}
    body = json.dumps(data).encode()
    await observed({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": body})
