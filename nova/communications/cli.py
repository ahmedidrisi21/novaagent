"""``nova calls`` — operate phone calls from a terminal.

Lives beside the code it drives rather than in ``nova/cli.py``: that file registers the
group and hands over, so calling grows here without growing the facade.

Every command loads the bundle (so the grant being enforced is the one declared), opens
the runtime's call store, and writes to the tenant's audit log under the operator's name.
Carrier and voice credentials come from this process's environment — the gateway's, never
an agent's — and ``nova calls check`` names any that are missing, without printing values.
"""

from __future__ import annotations

import getpass
import json
import os
import threading
import time
from datetime import timezone
from pathlib import Path

from nova.audit import AuditLog, new_correlation_id
from nova.communications.assemble import build_gateway, missing_credentials
from nova.communications.contacts import import_contacts
from nova.communications.contracts import ProviderError, StreamVoiceEngine
from nova.communications.spec import CONSENT_KINDS
from nova.communications.store import CommsStore

#: Placeholder base for commands that never reach a carrier callback. Carriers are only
#: given callback URLs when dialling, and dialling commands require a real one.
_NO_PUBLIC_URL = "https://public-url-not-configured.invalid"


def register(sub) -> None:
    calls = sub.add_parser("calls", help="operate agents' phone calls")
    calls_sub = calls.add_subparsers(dest="calls_command", required=True)

    def command(name: str, helptext: str, *, call: bool = False):
        parser = calls_sub.add_parser(name, help=helptext)
        parser.add_argument("bundle", type=Path)
        if call:
            parser.add_argument("call_id")
        return parser

    serve = command("serve", "run the webhook listener, media bridge and reconciliation")
    serve.add_argument("--public-url", default="", help="overrides communications.public_url")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8090, help="webhooks (HTTPS behind a proxy)")
    serve.add_argument("--media-port", type=int, default=8091, help="media bridge (wss://)")
    serve.add_argument("--sweep-every", type=int, default=60, metavar="SECONDS")

    command("check", "which credentials the gateway is missing (names only)")

    listing = command("list", "recent calls")
    listing.add_argument("--status", default="")
    listing.add_argument("--agent", default="")
    listing.add_argument("--limit", type=int, default=25)
    listing.add_argument("--json", action="store_true")

    for name, helptext in (("approve", "approve a call awaiting approval, and dial it"),
                           ("reject", "reject a call awaiting approval"),
                           ("end", "cancel a pending call or hang up a live one")):
        parser = command(name, helptext, call=True)
        parser.add_argument("--actor", default="", help="who is deciding (default: $USER)")
        parser.add_argument("--reason", default="")
        parser.add_argument("--public-url", default="")

    command("reconcile", "settle calls whose callbacks never arrived; expire old approvals "
                         "and recordings past retention")

    for name, helptext in (("pause-recording", "stop capturing audio on a live call"),
                           ("resume-recording", "capture audio again on a live call")):
        parser = command(name, helptext, call=True)
        parser.add_argument("--actor", default="")

    review = command("review", "values from calls waiting for a person to decide")
    review.add_argument("--json", action="store_true")

    decide = command("decide-review", "approve or reject one queued value")
    decide.add_argument("review_id")
    decide.add_argument("decision", choices=["approve", "reject"])
    decide.add_argument("--actor", default="")

    contact = command("contact", "a contact's fields, and where each value came from")
    contact.add_argument("contact_id")
    contact.add_argument("--json", action="store_true")

    attribute = command("set-attribute", "set a contact field as a person; calls never overwrite it")
    attribute.add_argument("contact_id")
    attribute.add_argument("name")
    attribute.add_argument("value")
    attribute.add_argument("--actor", default="")

    forget = command("forget-recordings", "delete every recording of one contact, now")
    forget.add_argument("contact_id")
    forget.add_argument("--actor", default="")

    actions = command("actions", "post-call action plans waiting for a person, or one call's plan")
    actions.add_argument("--call", default="", help="one call's whole plan, with its digest")
    actions.add_argument("--stats", action="store_true",
                         help="per action kind: proposed, approved and rejected by people")
    actions.add_argument("--json", action="store_true")

    decide_actions = command("decide-actions", "approve or reject a call's waiting actions",
                             call=True)
    decide_actions.add_argument("--digest", required=True,
                                help="from `nova calls actions --call`: what you looked at")
    decide_actions.add_argument("--approve", nargs="*", default=[], metavar="ACTION_ID")
    decide_actions.add_argument("--reject", nargs="*", default=[], metavar="ACTION_ID")
    decide_actions.add_argument("--actor", default="")

    contacts = command("import-contacts", "import contacts and consent from a CRM CSV export")
    contacts.add_argument("csv", type=Path)

    consent = command("consent", "grant or revoke one contact's consent")
    consent.add_argument("action", choices=["grant", "revoke"])
    consent.add_argument("contact_id")
    consent.add_argument("kind", choices=list(CONSENT_KINDS))
    consent.add_argument("--evidence", default="", help="required to grant: form id, URL, call id")

    opt_out = command("opt-out", "stop every agent calling a contact")
    opt_out.add_argument("contact_id")
    opt_out.add_argument("--reason", default="asked not to be called")


def _actor(args) -> str:
    return getattr(args, "actor", "") or getpass.getuser()


def _open(args, runtime_factory):
    from nova.spec import load_bundle

    bundle = load_bundle(args.bundle)
    runtime = runtime_factory(bundle.tenant_id)
    args.calls_runtime = runtime
    store = CommsStore(runtime.calls_store_path, tenant_id=bundle.tenant_id)
    audit = AuditLog.for_home(runtime.state_location, tenant_id=bundle.tenant_id,
                              actor=f"nova-calls:{_actor(args)}")
    public_url = getattr(args, "public_url", "") or bundle.communications.public_url
    gateway = build_gateway(
        bundle.communications, store=store, audit=audit, env=os.environ,
        public_url=public_url or _NO_PUBLIC_URL,
        knowledge_index=runtime.knowledge_index_path,
        knowledge_grants={spec.id: spec.knowledge.sources for spec in bundle.agents},
        tasks=task_submitter(runtime, audit),
    )
    return bundle, gateway, public_url


def task_submitter(runtime, audit):
    """Post-call work onto the runtime's board, idempotent on the action's key. None when
    the runtime takes no submitted work: work actions then wait rather than fail."""
    if not runtime.capabilities.work_submission:
        return None

    def submit(item) -> str:
        result = runtime.submit_work([item], audit=audit, correlation_id=new_correlation_id())
        return result.task_id(item.key)

    return submit


def _print_call(record) -> None:
    print(f"  {record.call_id}  {record.status.value:18} {record.agent:20} "
          f"{record.contact_id:16} {record.purpose}")
    if record.reason:
        print(f"      {record.reason}")


def _stamp(gateway) -> str:
    return gateway.now().astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z")


def _check(args, bundle, gateway, public_url) -> int:
    missing = missing_credentials(bundle.communications, os.environ)
    if not missing:
        print("every allowed provider has its credentials")
        return 0
    for provider, names in missing.items():
        print(f"  {provider:20} missing {', '.join(names)}")
    return 1


def _list(args, bundle, gateway, public_url) -> int:
    records = gateway.store.recent_calls(limit=args.limit, agent=args.agent, status=args.status)
    if args.json:
        print(json.dumps([r.to_dict() for r in records], indent=2))
        return 0
    for record in records:
        _print_call(record)
    if not records:
        print("no calls")
    return 0


def _needs_public_url(handler):
    def guarded(args, bundle, gateway, public_url) -> int:
        if not public_url:
            print("set communications.public_url or pass --public-url: the carrier needs it "
                  "to report back on the call")
            return 2
        return handler(args, bundle, gateway, public_url)

    return guarded


@_needs_public_url
def _approve(args, bundle, gateway, public_url) -> int:
    result = gateway.approve(args.call_id, approver=_actor(args))
    _print_call(result.record)
    return 0 if result.record.status.value in ("initiated", "ringing") else 1


def _reject(args, bundle, gateway, public_url) -> int:
    _print_call(gateway.reject(args.call_id, approver=_actor(args), reason=args.reason))
    return 0


@_needs_public_url
def _end(args, bundle, gateway, public_url) -> int:
    _print_call(gateway.end_call(args.call_id, actor=_actor(args), reason=args.reason))
    return 0


def _reconcile(args, bundle, gateway, public_url) -> int:
    from nova.communications.sweep import sweep

    changes = sweep(gateway)
    for change in changes:
        print(f"  {change.call_id}  {change.before} -> {change.after}: {change.note}")
    print(f"{len(changes)} call(s) reconciled")
    return 0


def _import_contacts(args, bundle, gateway, public_url) -> int:
    report = import_contacts(args.csv.read_text(encoding="utf-8"), store=gateway.store,
                             audit=gateway.audit, at=_stamp(gateway), source=str(args.csv))
    print(f"imported {report.imported} contact(s), {report.consents} consent record(s)")
    for error in report.errors:
        print(f"  {error}")
    return 1 if report.errors else 0


def _consent(args, bundle, gateway, public_url) -> int:
    if args.action == "grant":
        if not args.evidence:
            print("--evidence is required: consent nobody can show is consent nobody has")
            return 2
        gateway.store.grant_consent(args.contact_id, args.kind, evidence=args.evidence,
                                    at=_stamp(gateway))
    else:
        gateway.store.revoke_consent(args.contact_id, args.kind)
    gateway.audit.record(f"communications.consent.{args.action}ed",
                         correlation_id=new_correlation_id(), subject=args.contact_id,
                         detail={"kind": args.kind, "evidence": args.evidence})
    print(f"{args.action}ed {args.kind} for {args.contact_id}")
    return 0


def _opt_out(args, bundle, gateway, public_url) -> int:
    gateway.opt_out(args.contact_id, reason=args.reason, source=f"cli:{_actor(args)}")
    print(f"{args.contact_id} will not be called by any agent")
    return 0


def _pause(args, bundle, gateway, public_url) -> int:
    from nova.communications.recording import set_paused

    changed = set_paused(gateway, args.call_id, args.calls_command == "pause-recording",
                         actor=_actor(args))
    print(f"{changed} recording(s) {'paused' if args.calls_command == 'pause-recording' else 'resumed'}")
    return 0 if changed else 1


def _forget(args, bundle, gateway, public_url) -> int:
    from nova.communications.recording import forget_contact

    print(f"deleted {forget_contact(gateway, args.contact_id, actor=_actor(args))} recording(s)")
    return 0


def _review(args, bundle, gateway, public_url) -> int:
    pending = gateway.store.reviews()
    if args.json:
        print(json.dumps(pending, indent=2))
        return 0
    for item in pending:
        current = f" (now {item['current_value']!r})" if item["current_value"] else ""
        print(f"  {item['review_id']}  {item['contact_id']}.{item['name']} = {item['value']!r}"
              f"{current}  [{item['confidence']:.2f}] {item['reason']}")
        if item["quote"]:
            print(f'      "{item["quote"]}"  (call {item["call_id"]})')
    if not pending:
        print("nothing waiting for review")
    return 0


def _decide_review(args, bundle, gateway, public_url) -> int:
    from nova.communications.extraction import decide_review

    result = decide_review(gateway, args.review_id, approve=args.decision == "approve",
                           actor=_actor(args))
    print(f"{args.review_id}: {result['status']}")
    return 0


def _print_action(action) -> None:
    when = f" at {action['run_at']}" if action["run_at"] else ""
    print(f"  {action['action_id']}  {action['kind']:18} {action['status']:18} "
          f"[{action['tier']}]{when}")
    for key, value in action["params"].items():
        text = str(value).replace("\n", " ")
        print(f"      {key}: {text[:100]}{'…' if len(text) > 100 else ''}")
    if action["quote"]:
        print(f'      "{action["quote"]}"')
    for key in ("note", "error", "result"):
        if action[key]:
            print(f"      {key}: {action[key]}")


def _actions(args, bundle, gateway, public_url) -> int:
    from nova.communications.actions import plan_view

    if args.stats:
        print(json.dumps(gateway.store.action_stats(), indent=2))
        return 0
    if args.call:
        view = plan_view(gateway.store, args.call)
        if args.json:
            print(json.dumps(view, indent=2))
            return 0
        print(f"call {args.call}  digest {view['digest']}")
        for action in view["actions"]:
            _print_action(action)
        return 0
    waiting = gateway.store.actions(status="awaiting_approval")
    if args.json:
        print(json.dumps(waiting, indent=2))
        return 0
    for call_id in dict.fromkeys(a["call_id"] for a in waiting):
        print(f"call {call_id}  (decide with: nova calls actions BUNDLE --call {call_id})")
        for action in (a for a in waiting if a["call_id"] == call_id):
            _print_action(action)
    if not waiting:
        print("no actions waiting for a person")
    return 0


def _decide_actions(args, bundle, gateway, public_url) -> int:
    from nova.communications.actions import decide_plan

    view = decide_plan(gateway, args.call_id, digest=args.digest, approve=args.approve,
                       reject=args.reject, actor=_actor(args))
    for action in view["actions"]:
        _print_action(action)
    return 0


def _contact(args, bundle, gateway, public_url) -> int:
    values = gateway.store.attributes(args.contact_id)
    if args.json:
        print(json.dumps(values, indent=2))
        return 0
    for name, item in values.items():
        origin = f"call {item['call_id']}" if item["source"] == "call" else item["source"]
        print(f"  {name:24} {item['value']!r}  ({origin})")
    if not values:
        print("no fields recorded")
    return 0


def _set_attribute(args, bundle, gateway, public_url) -> int:
    gateway.store.set_attribute(args.contact_id, args.name, args.value, source="person",
                                at=_stamp(gateway))
    gateway.audit.record("communications.contact.attribute_set", correlation_id=new_correlation_id(),
                         subject=args.contact_id, detail={"name": args.name, "actor": _actor(args)})
    print(f"{args.contact_id}.{args.name} set; calls will propose changes to it, not make them")
    return 0


def run(args, runtime_factory) -> int:
    """Dispatch ``nova calls``. ``runtime_factory(tenant_id)`` returns the runtime."""
    bundle, gateway, public_url = _open(args, runtime_factory)
    handler = _COMMANDS[args.calls_command]
    try:
        return handler(args, bundle, gateway, public_url)
    except (KeyError, ValueError, ProviderError) as exc:
        print(f"error: {exc}")
        return 1
    finally:
        gateway.store.close()


def _serve(args, bundle, gateway, public_url) -> int:
    from nova.communications.media import build_media_server
    from nova.communications.sweep import sweep
    from nova.communications.webhooks import build_server

    if not public_url:
        print("set communications.public_url or pass --public-url")
        return 2
    webhooks = build_server(gateway, public_url=public_url, host=args.host, port=args.port)
    threading.Thread(target=webhooks.serve_forever, name="call-webhooks", daemon=True).start()
    servers = [webhooks]
    if any(isinstance(v, StreamVoiceEngine) for v in gateway.voices.values()):
        media = build_media_server(gateway, host=args.host, port=args.media_port)
        threading.Thread(target=media.serve_forever, name="call-media", daemon=True).start()
        servers.append(media)
    print(f"webhooks on {args.host}:{args.port} for {public_url}; "
          f"carriers: {', '.join(gateway.carriers) or 'none'}; "
          f"voices: {', '.join(gateway.voices) or 'none'}")
    from nova.communications.inbox import master_key, process, refresh

    runtime = args.calls_runtime
    inbox, master = runtime.calls_inbox_path, master_key(runtime.state_location)
    next_sweep = 0.0
    try:
        while True:
            # Agents' requests every couple of seconds; reconciliation on its own, slower beat.
            for result in process(gateway, inbox, master):
                print(f"agent request {result['request_id']}: {result['status']}")
            refresh(gateway, inbox)
            if time.monotonic() >= next_sweep:
                for change in sweep(gateway):
                    print(f"reconciled {change.call_id}: {change.before} -> {change.after} "
                          f"({change.note})")
                next_sweep = time.monotonic() + max(5, args.sweep_every)
            time.sleep(2)
    except KeyboardInterrupt:
        return 0
    finally:
        for server in servers:
            server.shutdown()


_COMMANDS = {
    "serve": _serve,
    "check": _check,
    "list": _list,
    "approve": _approve,
    "reject": _reject,
    "end": _end,
    "reconcile": _reconcile,
    "import-contacts": _import_contacts,
    "consent": _consent,
    "opt-out": _opt_out,
    "pause-recording": _pause,
    "resume-recording": _pause,
    "forget-recordings": _forget,
    "review": _review,
    "decide-review": _decide_review,
    "contact": _contact,
    "set-attribute": _set_attribute,
    "actions": _actions,
    "decide-actions": _decide_actions,
}
