"""Agents requesting calls: the installed tool, the signed inbox, and the gateway's decision."""

from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from nova.apply import apply_bundle
from nova.audit import AuditLog
from nova.communications import CallGateway, CallStatus, CommsStore, Contact
from nova.communications.inbox import agent_key, master_key, process, refresh, sign
from nova.communications.mock import MockTelephony
from nova.runtime.hermes.paths import HermesPaths
from nova.spec import load_bundle

from tests.platform.conftest import EXAMPLE_BUNDLE

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "nova" / "runtime" / "hermes" / "calling_tool.py"
START = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)

COMMS = {"communications": {
    "allow_test_providers": True,
    "disclosure": "This is an AI assistant calling for Acme.",
    "voice_providers": ["mock-voice"], "telephony_providers": ["mock-telephony"],
    "caller_ids": ["+15555550100"],
    "purposes": [{"id": "support", "description": "Follow up on an open ticket"},
                 {"id": "renewal", "description": "Talk about a renewal"}],
    "agents": [{"agent": "customer-support", "telephony": "mock-telephony",
                "caller_id": "+15555550100", "voice": {"primary": "mock-voice"},
                "purposes": ["support"], "contact_groups": ["customers"],
                "approval": "never"}],
}}


def load_tool(path: Path):
    """The plugin as the runtime loads it: by file path, as its own module."""
    spec = importlib.util.spec_from_file_location(f"calling_{abs(hash(path))}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bundle_with(tmp_path, comms=COMMS):
    root = tmp_path / "bundle"
    if root.exists():
        shutil.rmtree(root)
    shutil.copytree(EXAMPLE_BUNDLE, root)
    (root / "communications.yaml").write_text(yaml.safe_dump(comms))
    return load_bundle(root)


@pytest.fixture
def applied(tmp_path, runtime, home):
    bundle = bundle_with(tmp_path)
    apply_bundle(bundle, runtime, audit=AuditLog(tmp_path / "a.jsonl", tenant_id=bundle.tenant_id))
    return bundle, HermesPaths(home)


def test_only_an_agent_its_tenant_lets_call_gets_the_tool(applied):
    _, paths = applied
    grant = json.loads(paths.calling_config_path("customer-support").read_text())
    assert [p["id"] for p in grant["purposes"]] == ["support"]
    assert (paths.calling_plugin_dir("customer-support") / "__init__.py").is_file()
    config = yaml.safe_load(paths.config_path("customer-support").read_text())
    assert "nova-calling" in config["plugins"]["enabled"]

    assert not paths.calling_config_path("operations").exists()
    other = yaml.safe_load(paths.config_path("operations").read_text())
    assert "nova-calling" not in (other.get("plugins") or {}).get("enabled", [])


def test_the_installed_tool_imports_nothing_from_nova(applied):
    _, paths = applied
    installed = paths.calling_plugin_dir("customer-support") / "__init__.py"
    probe = (f"import sys, importlib.util; sys.modules['nova'] = None; "
             f"s = importlib.util.spec_from_file_location('t', {str(installed)!r}); "
             f"m = importlib.util.module_from_spec(s); s.loader.exec_module(m); print('ok')")
    result = subprocess.run([sys.executable, "-I", "-c", probe], capture_output=True, text=True)
    assert result.stdout.strip() == "ok", result.stderr


def test_the_tool_and_the_gateway_sign_identically():
    tool = load_tool(TOOL)
    payload = {"agent": "a", "request_id": "r1234567", "contact_id": "c1", "purpose": "p",
               "note": "ünïcode", "task_id": ""}
    assert tool._sign("k" * 64, payload) == sign("k" * 64, payload)


def test_a_purpose_change_changes_the_agents_digest(tmp_path, runtime, home):
    bundle = bundle_with(tmp_path)
    spec = bundle.agent("customer-support")
    before = runtime.expected_digest(spec, knowledge=bundle.knowledge,
                                     communications=bundle.communications)
    changed = copy.deepcopy(COMMS)
    changed["communications"]["agents"][0]["purposes"] = ["support", "renewal"]
    after_bundle = bundle_with(tmp_path, changed)
    after = runtime.expected_digest(after_bundle.agent("customer-support"),
                                    knowledge=after_bundle.knowledge,
                                    communications=after_bundle.communications)
    assert before != after


# -- request, decision, status -------------------------------------------------------------


@pytest.fixture
def gateway(applied, runtime):
    bundle, paths = applied
    store = CommsStore(runtime.calls_store_path, tenant_id=bundle.tenant_id)
    store.put_contact(Contact("c1", "+13125550101", "America/Chicago", ("customers",)))
    store.grant_consent("c1", "ai_voice", evidence="form-1", at="2026-10-01T00:00:00Z")
    carrier = MockTelephony()
    gw = CallGateway(bundle.communications, store,
                     AuditLog(runtime.state_location / "gw.jsonl", tenant_id=bundle.tenant_id),
                     {"mock-telephony": carrier}, clock=lambda: START)
    tool = load_tool(paths.calling_plugin_dir("customer-support") / "__init__.py")
    yield gw, carrier, tool, runtime
    store.close()


def test_an_agents_request_is_decided_by_the_gateway_and_dials_once(gateway):
    gw, carrier, tool, runtime = gateway
    master = master_key(runtime.state_location)
    queued = json.loads(tool.handle_request_call({"contact_id": "c1", "purpose": "support",
                                                  "note": "ticket 42"}))
    assert queued["status"] == "queued"
    assert json.loads(tool.handle_call_status({"request_id": queued["request_id"]}))["status"] == "queued"

    (result,) = process(gw, runtime.calls_inbox_path, master)
    assert result["status"] == "initiated" and len(carrier.placed) == 1
    status = json.loads(tool.handle_call_status({"request_id": queued["request_id"]}))
    assert (status["status"], status["call_id"]) == ("initiated", result["call_id"])

    # Picked up twice — a second gateway, or a crash before the file moved — dials once.
    done = runtime.calls_inbox_path / "done" / "customer-support" / f"{queued['request_id']}.json"
    pending = runtime.calls_inbox_path / "requests" / "customer-support" / done.name
    shutil.copy(done, pending)
    process(gw, runtime.calls_inbox_path, master)
    assert len(carrier.placed) == 1

    ref = gw.store.call(result["call_id"]).provider_call_ref
    headers, body = carrier.callback(ref, CallStatus.COMPLETED, event_id="done")
    gw.ingest("mock-telephony", headers=headers, body=body)
    refresh(gw, runtime.calls_inbox_path)
    assert json.loads(tool.handle_call_status({"request_id": queued["request_id"]}))["status"] == "completed"


def test_a_request_for_a_purpose_outside_the_grant_never_leaves_the_tool(gateway):
    _, carrier, tool, _ = gateway
    refused = json.loads(tool.handle_request_call({"contact_id": "c1", "purpose": "renewal"}))
    assert refused["status"] == "refused" and not carrier.placed


def test_a_request_signed_as_one_agent_cannot_be_filed_as_another(gateway):
    gw, carrier, _, runtime = gateway
    master = master_key(runtime.state_location)
    forged = {"agent": "operations", "request_id": "forged001", "contact_id": "c1",
              "purpose": "support", "note": "", "task_id": ""}
    forged["signature"] = sign(agent_key(master, "customer-support"), forged)
    target = runtime.calls_inbox_path / "requests" / "operations"
    target.mkdir(parents=True)
    (target / "forged001.json").write_text(json.dumps(forged))
    (result,) = process(gw, runtime.calls_inbox_path, master)
    assert result["status"] == "rejected" and "signature" in result["reason"]
    assert not carrier.placed
