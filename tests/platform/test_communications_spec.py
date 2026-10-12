"""communications.yaml, the call gate's rules, and the honesty of the provider catalogue."""

from __future__ import annotations

import copy
import shutil
from datetime import datetime, timezone, UTC
from pathlib import Path

import pytest
import yaml

from nova.channels.providers import Verification
from nova.communications import Contact, catalogue, decide_call, parse_communications
from nova.communications.gate import ALLOW, DENY, REQUIRE_APPROVAL, CallFacts
from nova.communications.providers import CAPABILITY_NAMES
from nova.control import ControlAPI
from nova.control.auth import Principal
from nova.errors import SpecError
from nova.spec import load_bundle

from tests.platform.conftest import EXAMPLE_BUNDLE

ROOT = Path(__file__).resolve().parents[2]

BASE = {
    "communications": {
        "allow_test_providers": True,
        "disclosure": "Hi, this is an AI assistant calling for Acme.",
        "voice_providers": ["mock-voice"],
        "telephony_providers": ["mock-telephony"],
        "caller_ids": ["+15555550100"],
        "purposes": [{"id": "support"}, {"id": "offer", "marketing": True}],
        "agents": [
            {
                "agent": "customer-support",
                "telephony": "mock-telephony",
                "caller_id": "+15555550100",
                "voice": {"primary": "mock-voice"},
                "purposes": ["support", "offer"],
                "contact_groups": ["customers"],
                "approval": "first_contact",
            }
        ],
    }
}


#: Profile keys BASE leaves at their defaults, so a change to them lands on the profile.
PROFILE_ONLY = {"recording", "approval", "max_call_seconds"}


def doc(**changes):
    data = copy.deepcopy(BASE)
    profile = data["communications"]["agents"][0]
    for key, value in changes.items():
        if key in profile or key in PROFILE_ONLY:
            profile[key] = value
        else:
            data["communications"][key] = value
    return data


# -- the declaration ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "changes, field",
    [
        ({"caller_id": "+15555550199"}, "caller_id"),          # not a tenant number
        ({"voice": {"primary": "openai-live"}}, "voice"),      # not in the tenant allowlist
        ({"purposes": ["unknown"]}, "purposes"),
        ({"contact_groups": []}, "contact_groups"),
        ({"recording": {"enabled": True}}, "legal_basis"),     # recording needs a stated basis
        ({"api_key": "sk-123"}, "api_key"),
    ],
)
def test_a_grant_cannot_reach_beyond_what_the_tenant_declared(changes, field):
    with pytest.raises(SpecError) as refused:
        parse_communications(doc(**changes))
    assert field in str(refused.value)


def test_a_test_carrier_needs_an_explicit_opt_in():
    with pytest.raises(SpecError, match="places no real calls"):
        parse_communications(doc(allow_test_providers=False))


def test_a_profile_for_an_agent_the_bundle_lacks_fails_the_load(tmp_path):
    root = tmp_path / "bundle"
    shutil.copytree(EXAMPLE_BUNDLE, root)
    data = doc(agent="no-such-agent")
    (root / "communications.yaml").write_text(yaml.safe_dump(data))
    with pytest.raises(SpecError, match="no-such-agent"):
        load_bundle(root)


def test_declaring_calls_changes_the_bundle_digest_and_absence_does_not(tmp_path):
    root = tmp_path / "bundle"
    shutil.copytree(EXAMPLE_BUNDLE, root)
    before = load_bundle(root).digest()
    (root / "communications.yaml").write_text(yaml.safe_dump({"communications": {}}))
    assert load_bundle(root).digest() == before
    (root / "communications.yaml").write_text(yaml.safe_dump(doc()))
    assert load_bundle(root).digest() != before


# -- the gate -----------------------------------------------------------------------------

SPEC = parse_communications(BASE)
NOW = datetime(2026, 10, 12, 15, 0, tzinfo=UTC)  # 10:00 in Chicago
CONTACT = Contact("c1", "+13125550101", "America/Chicago", ("customers",))


def facts(**changes):
    values = dict(
        agent="customer-support", purpose="support", contact=CONTACT,
        consents=frozenset({"ai_voice"}), suppressed=False, active_calls=0, calls_today=0,
        prior_contact=True, now=NOW,
    )
    values.update(changes)
    return CallFacts(**values)


@pytest.mark.parametrize(
    "changes, rule",
    [
        ({"agent": "operations"}, "no_grant"),
        ({"purpose": "other"}, "purpose"),
        ({"contact": None}, "contact"),
        ({"contact": Contact("c1", "+1312", "America/Chicago", ("vendors",))}, "contact_group"),
        ({"suppressed": True}, "suppressed"),
        ({"consents": frozenset()}, "consent"),
        ({"purpose": "offer"}, "consent"),  # marketing needs written consent too
        ({"contact": Contact("c1", "+1312", "", ("customers",))}, "calling_hours"),
        ({"contact": Contact("c1", "+1312", "Mars/Olympus", ("customers",))}, "calling_hours"),
        ({"now": datetime(2026, 10, 12, 3, 0, tzinfo=UTC)}, "calling_hours"),
        ({"active_calls": 1}, "concurrency"),
        ({"calls_today": 20}, "daily_calls"),
    ],
)
def test_each_rule_denies(changes, rule):
    decision = decide_call(SPEC, facts(**changes))
    assert (decision.effect, decision.rule) == (DENY, rule)


def test_a_suppressed_contact_is_refused_before_anyone_is_asked_to_approve():
    decision = decide_call(SPEC, facts(suppressed=True, prior_contact=False))
    assert decision.effect == DENY


def test_first_contact_needs_approval_and_later_ones_do_not():
    assert decide_call(SPEC, facts(prior_contact=False)).effect == REQUIRE_APPROVAL
    assert decide_call(SPEC, facts(prior_contact=True)).effect == ALLOW


# -- the catalogue ------------------------------------------------------------------------


def test_every_capability_answers_a_question_the_catalogue_asks():
    for provider in catalogue():
        assert set(provider.capabilities) <= set(CAPABILITY_NAMES[provider.kind]), provider.id


def test_nothing_claims_more_than_vendor_documentation_without_code_to_show_for_it():
    for provider in catalogue():
        stronger = [
            name for name, cap in provider.capabilities.items()
            if cap.verification not in (Verification.DECLARED,)
        ]
        if stronger or provider.verification is not Verification.DECLARED:
            path = provider.implementation.split(" ")[0]
            assert path and (ROOT / path).exists(), provider.id


# -- the control API ----------------------------------------------------------------------


def test_calling_grants_are_for_admins_only():
    assert not Principal(name="r", role="viewer").may("/communications")
    assert Principal(name="a", role="admin").may("/communications")


def test_the_api_reports_the_grant_and_the_catalogue(tmp_path, runtime):
    root = tmp_path / "bundle"
    shutil.copytree(EXAMPLE_BUNDLE, root)
    (root / "communications.yaml").write_text(yaml.safe_dump(doc()))
    body = ControlAPI(load_bundle(root), runtime).handle("/platform/v1/communications").body
    assert body["declared"] is True
    assert body["settings"]["agents"][0]["agent"] == "customer-support"
    assert {p["id"] for p in body["catalogue"]} >= {"openai-live", "twilio"}
