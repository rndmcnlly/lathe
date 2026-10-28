"""Offline safeguards for the opt-in disposable lab; no provider/Docker calls."""
import copy
import json
import sys
from decimal import Decimal
from types import SimpleNamespace

import pytest

import readiness_lab as lab


def payload():
    return {"model": lab.MODEL, "provider": copy.deepcopy(lab.PROVIDER), "max_tokens": 4096,
            "messages": [{"role": "user", "content": "hello"}]}


@pytest.mark.parametrize("change", [
    lambda p: p.update(model="another/model"),
    lambda p: p["provider"].pop("zdr"),
    lambda p: p["provider"]["max_price"].update(prompt=1),
    lambda p: p.update(max_tokens=True),
    lambda p: p.update(max_tokens=4097),
    lambda p: p.update(plugins=[{"id": "web"}]),
    lambda p: p.update(n=2),
    lambda p: p.update(modalities=["image"]),
    lambda p: p["messages"][0].update(content=[{"type": "image_url"}]),
])
def test_unbudgeted_requests_refused_without_reservation(change):
    request = payload()
    change(request)
    budget = lab.Reservation()
    with pytest.raises(RuntimeError):
        budget.reserve(request, 1000)
    assert budget.calls == 0 and budget.reserved == 0


def test_budget_is_reserved_before_dispatch_and_never_overdrawn(monkeypatch):
    monkeypatch.setattr(lab, "BUDGET", Decimal("0.09"))
    budget = lab.Reservation()
    request = payload()
    cost = budget.reserve(request, lab.MAX_PROMPT_BYTES)
    assert cost > 0 and budget.calls == 1
    budget.reserve(request, lab.MAX_PROMPT_BYTES)
    with pytest.raises(RuntimeError, match="budget exhausted"):
        budget.reserve(request, lab.MAX_PROMPT_BYTES)
    assert budget.reserved == cost * 2 <= lab.BUDGET


def test_request_cap(monkeypatch):
    monkeypatch.setattr(lab, "MAX_REQUESTS", 1)
    budget = lab.Reservation()
    budget.reserve(payload(), 100)
    with pytest.raises(RuntimeError):
        budget.reserve(payload(), 100)


def test_default_and_plan_do_not_import_live_helpers(monkeypatch, capsys):
    monkeypatch.setattr(lab, "setup_imports", lambda: pytest.fail("Live helper imported"))
    for args in (["readiness_lab.py"], ["readiness_lab.py", "--plan"]):
        monkeypatch.setattr(sys, "argv", args)
        assert lab.main() == 0
        assert json.loads(capsys.readouterr().out)["network_or_cloud_work"] is False


@pytest.mark.parametrize("cloud_fails,removal_sticks", [(False, False), (True, False), (False, True)])
def test_cleanup_orders_and_independently_verifies(monkeypatch, tmp_path, cloud_fails, removal_sticks):
    label = "lathe-ci-next-0123456789abcdef"
    names = {label + suffix for suffix in ("-app", "-pg", "-redis")}
    networks = {label + "-net"}
    calls = []

    def command(*args, **kwargs):
        calls.append(args)
        if args[1:3] == ("ps", "-a"):
            return "\n".join(names)
        if args[1:3] == ("network", "ls"):
            return "\n".join(networks)
        if args[1] == "inspect":
            return "false"
        if args[1] == "rm" and not removal_sticks:
            names.discard(args[-1])
        if args[1:3] == ("network", "rm"):
            networks.discard(args[-1])
        return ""

    async def cloud_cleanup():
        assert any(c[1] == "stop" for c in calls)
        assert not any(c[1] == "rm" for c in calls)
        if cloud_fails:
            raise KeyboardInterrupt("interrupted cloud cleanup")

    async def no_sandboxes(*args):
        return []

    monkeypatch.setattr(lab, "run_command", command)
    monkeypatch.setattr(lab, "owned_sandboxes", no_sandboxes)
    monkeypatch.setattr(lab, "setup_imports", lambda: SimpleNamespace(sanitize=lambda x: x))
    monkeypatch.setitem(sys.modules, "test_deployment", SimpleNamespace(cleanup_test_sandboxes=cloud_cleanup))
    monkeypatch.setenv("DAYTONA_API_KEY", "offline-fake-key")
    assert lab.cleanup(label, tmp_path) is (not cloud_fails and not removal_sticks)
    record = json.loads(next(tmp_path.glob("cleanup-*.json")).read_text())
    assert record["owui_stopped_before_cloud_cleanup"]
    assert any(c[1] == "rm" for c in calls)
    assert not networks


def test_cleanup_refuses_non_lab_namespace(monkeypatch, tmp_path):
    monkeypatch.setattr(lab, "setup_imports", lambda: SimpleNamespace())
    monkeypatch.setattr(lab, "run_command", lambda *a, **k: pytest.fail("Unsafe Docker call"))
    with pytest.raises(RuntimeError, match="Invalid owned"):
        lab.cleanup("production", tmp_path)
