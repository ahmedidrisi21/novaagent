# Communications — the call gate, the carrier seam, and the first real adapters

An agent can now *ask* to phone a contact, and NOVA decides whether that call may happen,
dials it once, and tracks it to the end through duplicate and out-of-order carrier callbacks.
**No real carrier or voice engine is connected yet.** Phase 1 is the part every later phase
stands on: the grant, the gate, the state machine and the adapter contracts, proven against
a test carrier. Business requirements: *NOVA Voice & Communications Workforce BRD v1.1*.

**Core patches: none.** Everything is under `nova/communications/`.

---

## What exists

| Piece | File | What it does |
|---|---|---|
| Provider catalogue | `providers.py` | Four voice engines (OpenAI GPT-Live, ElevenLabs Agents, Amazon Nova Sonic on Bedrock, cascaded Deepgram + Cartesia) and two carriers (Twilio, Telnyx), each with a capability matrix and an evidence level per capability |
| Contracts | `contracts.py` | `TelephonyProvider`, `VoiceSessionProvider`, one call-status vocabulary, normalized failure reasons, and the forward-only transition rule |
| Tenant declaration | `spec.py` | `communications.yaml`: allowed providers, caller ids, calling hours, purposes, per-agent calling profiles |
| Call gate | `gate.py` | One pure function: allow, deny, or require approval, with a reason. Every gap denies |
| Store | `store.py` | SQLite per tenant: contacts, consent, suppressions, calls, carrier events |
| Gateway | `gateway.py` | Request → gate → idempotent insert → approval → dial → callbacks |
| Test doubles | `mock.py` | A signed test carrier and a silent voice engine, run through the same contract test any real adapter must pass |
| Control API | `GET /platform/v1/communications` | Admin only. Grants and catalogue; no credential, no contact number |

## The rules this phase makes structural

- **An agent never supplies a phone number.** It names a contact and a purpose. The
  destination comes from the contact record, the caller id from the tenant's declared numbers.
- **One request, one dial.** `(tenant, idempotency_key)` is unique, and a call enters
  `initiated` through one conditional UPDATE, so concurrent approvals dial once.
- **Approval is bound to the action.** The request stores a digest of the resolved number,
  caller id, carrier, purpose and duration. If any of it changes before approval, the
  approval cancels the call instead of dialling it. Approval also re-runs the gate, so a
  consent withdrawn while the call waited blocks it.
- **Order of refusal:** no grant → purpose → contact/group → suppression → consent → calling
  hours → concurrency → daily calls → approval. A suppressed contact never reaches an
  approval screen.
- **Consent kinds:** `ai_voice` for every agent-placed call (the FCC's 2024 ruling treats AI
  voices as artificial under the TCPA); `marketing_written` as well for a purpose marked
  `marketing: true`. Consent without evidence is refused at write time.
- **Callbacks:** verified before parsing, deduplicated per tenant by the carrier's event id,
  recorded before they are applied, and only ever move a call forward.

## Enforcement — what the caps really are

`max_concurrent_calls` and `max_calls_per_day` are refused **before** a carrier is asked to
dial. That is a hard, preemptive control **for calls placed through this gateway**, verified
at `nova/communications/gateway.py: CallGateway.request_call` and `approve`.

It is not hard against an agent that holds carrier credentials itself. The runtime's SMS
adapter reads `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` from an agent's environment, and an
agent with those can call Twilio's API directly. **Keep voice-capable carrier credentials out
of agent environments**; the gateway holds them. The Twilio catalogue entry says the same on
the connect screen.

`max_call_seconds` is passed to the carrier as the call's time limit. Whether a carrier
honours it is a property of that adapter, verified when the adapter is written.

## Declaring it

Absent `communications.yaml` means no agent may call. Credentials are refused in this file.

```yaml
communications:
  voice_providers: [openai-live, deepgram-cartesia]
  telephony_providers: [twilio]
  caller_ids: ["+15555550100"]
  calling_hours: { start: "08:00", end: "21:00" }   # the contact's local time
  purposes:
    - id: support-follow-up
    - id: renewal-offer
      marketing: true                                # needs written consent too
  agents:
    - agent: customer-support
      telephony: twilio
      caller_id: "+15555550100"
      voice: { primary: openai-live, fallbacks: [deepgram-cartesia], language: en-US }
      purposes: [support-follow-up]
      contact_groups: [customers]
      approval: first_contact        # always | first_contact | never
      max_call_seconds: 300
      max_concurrent_calls: 2
      max_calls_per_day: 50
```

`recording:` is refused until consent capture and pause/resume exist; accepting it now would
be a setting that does nothing.

## Phase 2: Twilio, GPT-Live, voicemail detection, disclosure

A call now runs end to end against real vendor APIs, exercised against scripted stand-ins
for each vendor (no live call has been placed yet).

```
agent request ─▶ gate ─▶ Twilio dials (MachineDetection, TimeLimit)
                               │ answered
                               ▼
            POST /calls/twilio/answer ── machine? ──▶ <Hangup/>, call FAILED machine_detected
                               │ person
                               ▼
            <Say>disclosure</Say><Dial><Sip>sip:proj_…@sip.api.openai.com
                                       ?X-Call-Id=…&X-Call-Token=…</Sip></Dial>
                               │
                               ▼
            POST /voice/openai-live/events (live.transport.incoming, signed)
                 token checks out, call ANSWERED, no session yet ──▶ accept(instructions, voice)
                 anything else ──▶ reject
```

| Piece | File |
|---|---|
| Twilio carrier | `twilio.py`: REST dial, status mapping, `AnsweredBy` → machine, TwiML |
| GPT-Live voice engine | `openai_live.py`: SIP target, Standard Webhooks verification, accept/reject/hang-up |
| Webhook listener | `webhooks.py`: `/calls/<carrier>/answer`, `/calls/<carrier>/status`, `/voice/<engine>/events` |
| Assembly | `assemble.py`: builds the gateway from the **gateway process's** environment |

- **The disclosure is the carrier's, not the model's.** `communications.disclosure` is
  required once any agent may call, and Twilio speaks it word for word before the bridge.
- **A voicemail greeting hears nothing.** Any `machine_*` or `fax` answer hangs up and
  records `machine_detected`. `unknown` is treated as a person: people who answer and stay
  silent are common.
- **A voice session must prove its call.** SIP headers are caller-controlled, so the bridge
  carries an HMAC token over the call id (`CALL_BRIDGE_SECRET`). The gateway accepts a
  session only when the token matches, the call is `answered`, the engine is the one chosen
  at answer time, and no session is bound yet. That last check is a conditional UPDATE, so
  simultaneous webhooks bind one session.
- **Signatures are checked against the vendors' own test vectors** (Twilio's validator
  tests; the Standard Webhooks example), so the adapters cannot pass by agreeing with
  themselves.
- **The listener is not the Control API.** It is internet-facing and authenticates by
  signature, so it runs as its own server behind a TLS proxy; its `public_url` is what
  signatures are checked against.

### Running it

Configure Twilio's number and OpenAI's webhook to point at `<public_url>/calls/twilio/...`
and `<public_url>/voice/openai-live/events`, set the variables listed in `assemble.py` in
the gateway's environment (never an agent's), then:

```python
gateway = build_gateway(bundle.communications, store=CommsStore(path, tenant_id=tenant),
                        audit=AuditLog.for_home(home, tenant_id=tenant),
                        env=os.environ, public_url="https://calls.example.com")
build_server(gateway, public_url="https://calls.example.com", port=8090).serve_forever()
```

### Phase 2 limits, stated plainly

- **No tools on a phone call.** Over SIP, GPT-Live delegates through a sideband WebSocket.
  This layer is standard library only and has no WebSocket client, so the voice model runs
  on the purpose's instructions alone. It cannot look anything up or act mid-call.
- **The cascaded engine (Deepgram + Cartesia) is not built.** It needs a media bridge that
  streams call audio over WebSockets, which needs either a WebSocket dependency or a
  hand-written client. That is a dependency decision for the platform owners, not something
  to slip in.
- **No `nova` CLI command yet** starts the listener; the snippet above is the entry point.
- **Opt-outs spoken on a call are not captured** until transcripts arrive (Phase 4). The
  purpose instructions should tell the voice to acknowledge and end the call.

## Not built yet, in the order it lands

1. **Phase 2 remainder:** the cascaded engine and the GPT-Live sideband (both need a
   WebSocket client: decision pending), and a CLI command for the listener.
2. **Phase 3:** reconciliation for a call left in `initiated` with no carrier reference
   after a crash; live call monitor; recording with consent.
3. **Phase 4:** the agent-facing `communications` toolset (request a call, read its status),
   added per session from the calling profile, never through an environment variable;
   transcripts, extraction and post-call actions.
4. **Phase 5:** Bedrock Nova Sonic, ElevenLabs Agents if the benchmark justifies it, mid-call
   failover.

Contacts and consent are written through `CommsStore` directly today. A CRM import and a
dashboard screen for them come with Phase 3.
