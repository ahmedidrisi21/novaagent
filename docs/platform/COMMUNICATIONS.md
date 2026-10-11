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

## Live connections: lookups mid-call and the cascaded engine

Both run over WebSockets, borrowed from the runtime's pinned `websockets` package through
`ws.py`, the only module allowed to import it (registered in
`tests/platform/test_boundaries.py`, with a test proving everything still imports and the
refusal names the package when it is absent).

### Mid-call lookups (both engines)

Read-only, from the company knowledge the agent is granted (`agents/<id>.yaml`
`knowledge.sources`), scoped in SQL by `nova/knowledge` exactly as the agent's own tool is.
A hit is read out with its citation; no hit becomes an explicit "not found, offer to
follow up in writing", so the voice never improvises a price or a policy. Every lookup is
audited as `communications.knowledge.searched` with the question and the citations.

- **GPT-Live:** the session is accepted with client delegation and `LiveSideband`
  attaches to `wss://api.openai.com/v1/live/sessions/{id}/attach`. A delegation carries
  no question text, so the question is what the caller said since the last delegation.
  The answer is appended as commentary for the voice to paraphrase. The sideband
  opens on a background thread, so a slow handshake never holds up other calls' webhooks;
  if it cannot open, the call continues on its instructions alone and the failure is logged.
- **Cascaded:** passages for each utterance are handed to the reply model as reference
  data, with the rule to answer from them or say it cannot.

Nothing said on a call can make the gateway act — write, send, or call. The one
exception is an opt-out, below.

### The cascaded engine (Deepgram + Cartesia)

```
Twilio <Connect><Stream>  ──audio (mu-law 8k)──▶  /media/deepgram-cartesia  ──▶  Deepgram
   ▲                                                  │ start: call_id + token      │ finished utterance
   │                                                  ▼ (proved like a SIP session) ▼
   └──────── media / clear ◀── Cartesia (mu-law 8k) ◀── reply model (+ knowledge passages)
```

- **No transcoding:** Twilio, Deepgram and Cartesia all speak 8 kHz mu-law.
- **Barge-in:** Deepgram's `SpeechStarted` cancels the reply at Cartesia and sends Twilio
  `clear`, so the caller can interrupt.
- **Spoken opt-outs are honoured on the call.** Every word is text first, so "stop calling
  me" suppresses the contact for every agent at once, says goodbye, and hangs up.
- **A failed reply model** apologises, says someone will follow up, and hangs up.
- **Configuration:** `communications.cascade` names the reply model (any
  OpenAI-compatible `/chat/completions` endpoint) and optional speech models; the agent's
  `voice.voice` is the Cartesia voice id. Keys are `DEEPGRAM_API_KEY`, `CARTESIA_API_KEY`
  and `CASCADE_LLM_API_KEY`, in the gateway's environment.

```yaml
communications:
  voice_providers: [deepgram-cartesia, openai-live]
  cascade:
    llm_base_url: https://api.openai.com/v1
    llm_model: gpt-4.1-mini
  agents:
    - agent: customer-support
      voice: { primary: deepgram-cartesia, fallbacks: [openai-live], voice: "<cartesia voice id>" }
```

### Running both listeners

```python
gateway = build_gateway(bundle.communications, store=..., audit=..., env=os.environ,
                        public_url="https://calls.example.com",
                        knowledge_index=home / "knowledge.db",
                        knowledge_grants=grants_by_agent(bundle.agents))
build_server(gateway, public_url="https://calls.example.com", port=8090)   # webhooks
build_media_server(gateway, port=8091)                                     # wss://…/media/
```

Route `https://calls.example.com/` to port 8090 and `wss://calls.example.com/media/` to
port 8091 at the TLS proxy.

### Limits, stated plainly

- **No live call has been placed.** Every vendor is exercised against scripted stand-ins
  speaking its documented messages; the catalogue says `local_validated`, not
  `field_validated`. The first real call is the next step, and the place to measure latency.
- **Lookups only, no actions mid-call.** Booking, emailing and CRM writes are post-call
  actions (Phase 4), approved through policy.
- **No `nova` CLI command yet** starts the listeners; the snippet above is the entry point.

## Phase 3: operating calls

### `nova calls`

| Command | What it does |
|---|---|
| `serve BUNDLE` | webhook listener, media bridge (when a stream engine is configured) and the reconciliation sweep, in one process |
| `check BUNDLE` | names any credential the gateway is missing, never a value |
| `import-contacts BUNDLE FILE.csv` | contacts and consent from a CRM export (columns below) |
| `consent BUNDLE grant\|revoke CONTACT KIND --evidence …` | one consent record; granting without evidence is refused |
| `opt-out BUNDLE CONTACT` | no agent calls this contact again |
| `list BUNDLE [--status] [--agent] [--json]` | recent calls |
| `approve\|reject\|end BUNDLE CALL_ID` | the operator's three actions, audited under `$USER` or `--actor` |
| `reconcile BUNDLE` | one sweep, now |

Contacts CSV: `contact_id, phone (E.164), timezone (IANA), groups (;-separated)`, and
optionally `ai_voice_consent` and `marketing_consent`. A consent cell holds **evidence**
(a form id, URL or call id); a bare `yes` is rejected. Bad rows are reported by line and
skipped. The audit records counts, never numbers.

Calls live in the runtime's `calls_store_path` (`<home>/nova-calls.db`). The public URL is
`communications.public_url`, overridable with `--public-url`.

### Reconciliation

Carriers deliver callbacks when they can; a crash or a dropped webhook leaves a call the
store still thinks is ringing, holding one of its agent's concurrency slots. The sweep (every
60 s under `serve`) settles those **by asking the carrier**, never by assuming:

| Stuck in | After | Becomes |
|---|---|---|
| `approval_required` | 24 h | `cancelled`: an approval for yesterday's context is not an approval |
| `requested` | 10 min | `failed`: the process died between decision and dial; nothing was dialled |
| `initiated`, no carrier reference | 10 min | `failed`: the carrier never confirmed it and sent nothing |
| live, past its own time limit + 5 min | — | whatever the carrier reports; left alone if the carrier cannot be reached |

### The call monitor (Control API)

| Route | Role | |
|---|---|---|
| `GET /platform/v1/communications/calls?status=&agent=&limit=` | admin | recent calls and how many are live |
| `GET /platform/v1/communications/calls/<id>` | admin | one call |
| `POST /platform/v1/communications/calls/<id>/decide` `{action: approve\|reject\|end}` | admin | audited under the person; an approval re-runs the gate and answers 409 when the call is no longer callable |

Admin throughout: a call list names customers, and approving dials a real person. The
dashboard's source is not in this repository, so the screen for these routes is not here.

## Recording with consent

Off unless an agent's profile turns it on, and turning it on requires a written legal basis:

```yaml
communications:
  consent_prompt: "May we record this call? Press 1 or say yes to allow it. Press 2 or say no to continue without recording."
  agents:
    - agent: customer-support
      recording:
        enabled: true
        legal_basis: "Quality assurance; all-party consent asked on every call; reviewed by <counsel>, <date>."
        retention_days: 90
        on_refusal: continue        # or: end
```

**One rule, every market: ask everyone, every time, record only on an explicit yes.** Several
US states need every party's consent. Asking everyone satisfies all of them and needs no
per-state logic that could be wrong. The legal review is still needed to sign the rule off for
each launch market; that review is a person's job, and `legal_basis` is where it is written down.

```
answered ─▶ disclosure ─▶ "May we record?"  (carrier <Gather>: key 1/2 or speech; silence reported)
                              │
          ┌───────────────────┼────────────────────────┐
        yes (1, "yes",     no / silence /            no, and on_refusal: end
        "sure, go ahead")  "yes... actually no"            │
          │                   │                       "Goodbye", hang up
   record both parties,   no recording
   dual channel               │
          └───────────▶ connect the voice engine
```

- **Consent is classified deterministically.** No model decides whether someone said yes.
  Only an explicit yes (key 1, or yes-words with no no-words) counts. Anything ambiguous
  is no. The answer is kept on the call as evidence ("pressed 1", `said "sure, go ahead"`),
  and a retried callback cannot change it.
- **Payment details are never captured.** The payment guard hears both sides as speech is
  recognised, partial results included, and pauses the recording the moment card, account
  or identity talk starts, or a run of eight or more digits. That is before the number, not
  after it. It resumes after 20 s of ordinary conversation. The pause skips audio rather
  than recording silence. Both engines feed it: the cascaded engine from Deepgram partials,
  GPT-Live from its sideband transcript. That is why a recording profile on GPT-Live always
  attaches the sideband. The guard errs towards pausing: a phone number read out pauses too.
- **Recordings stay at the carrier** and NOVA keeps their metadata: state, duration,
  retention date. The Control API returns metadata only; it never proxies audio.
- **Retention:** the sweep deletes each recording at the carrier when its
  `retention_days` pass. `nova calls forget-recordings BUNDLE CONTACT` deletes a person's
  recordings on request.
- **Operator controls:** `nova calls pause-recording|resume-recording BUNDLE CALL_ID`, and
  `pause-recording` / `resume-recording` actions on
  `POST /communications/calls/<id>/decide`. All are audited.

## Phase 4a: from calls to company data

```yaml
communications:
  extraction: { llm_base_url: https://api.openai.com/v1, llm_model: gpt-4.1-mini }  # key: EXTRACTION_LLM_API_KEY
  purposes:
    - id: sales-follow-up
      fields:
        - { name: budget,   kind: number, contact_attribute: budget, auto_apply: 0.85 }
        - { name: interest, kind: choice, options: [high, medium, low], contact_attribute: interest }
        - { name: callback, kind: date,   description: "when they asked to be called back" }
  agents:
    - agent: sales
      keep_transcript_days: 0      # default: deleted as soon as fields are extracted
```

For every finished call with a transcript, from the sweep:

1. **Transcripts** come from both engines, one finished turn at a time, redacted before
   they are written. Digit runs and SSN-shaped numbers become `[number]`.
2. **Spoken opt-outs are honoured without a model.** A caller turn asking not to be called
   suppresses the contact for every agent. The cascaded engine already does this live;
   this catches GPT-Live calls too.
3. **Extraction.** The tenant's model reads the transcript as data, never as instructions,
   and returns value, confidence and quote per field.
4. **Verification.** Each value is coerced to its field's type (a choice must be one of its
   options, a date must be a date), and its **quote must appear in the transcript**. A quote
   that is not there scores zero: a model may misread a call, but it may not invent the
   evidence for its reading.
5. **Writeback.** A value mapped to a `contact_attribute` is written when its confidence
   reaches `auto_apply`, **unless a person entered the current value**: a call never
   overwrites a person. Everything else goes to the review queue with its quote. An
   approved value becomes a person's value.
6. **The transcript is deleted**, or kept for `keep_transcript_days` and then deleted.

Every written value carries the call id and the quote it came from. A model failure is
recorded once and not retried in a loop. Fields declared without a configured model leave
the transcript waiting rather than lose it.

| Surface | |
|---|---|
| `nova calls review BUNDLE` / `decide-review BUNDLE ID approve\|reject` | the queue |
| `nova calls contact BUNDLE CONTACT` | fields with where each value came from |
| `nova calls set-attribute BUNDLE CONTACT NAME VALUE` | a person's value; calls only propose changes to it |
| `GET /communications/reviews`, `POST /communications/reviews/<id>/decide` | admin |
| `GET /communications/contacts/<id>` | admin: groups, consent, suppression, fields with provenance |
| `GET /communications/calls/<id>` | now includes the extraction |

## Phase 4a, part 2: agents request calls

An agent with a calling profile gets two tools; every other agent gets none.

| Tool | |
|---|---|
| `request_call(contact_id, purpose, note)` | asks for a call to a contact by CRM id, for one of the agent's own purposes (an enum in the schema). It does not dial. |
| `call_status(request_id)` | queued, waiting for approval, refused (with the reason), ringing, connected, completed or failed |

```
agent ── request_call ──▶ <home>/nova-calls-inbox/requests/<agent>/<id>.json   (HMAC-signed)
                                         │  nova calls serve, every 2 s
                                         ▼
                 signature ✓ for <agent> ─▶ CallGateway.request_call  (grant, consent, hours,
                 agent/id match the path     caps, approval: the same gate as everything else)
                                         │
agent ◀─ call_status ── results/<agent>/<id>.json  (refreshed as the call progresses)
```

- **No credentials in the agent.** The tool writes a request; the gateway, which holds the
  carrier keys, decides it. An agent names a contact; it never supplies a number.
- **Signed per agent.** Each agent's key is an HMAC of a deployment master key
  (`<home>/nova/call-request.key`, created on first apply, mode 0600) and the agent id. A
  request is accepted only if it verifies against the agent its inbox directory names.
- **Idempotent.** The request id is the idempotency key: picked up twice, it dials once.
- **Installed like the knowledge tool.** `nova apply` writes `nova-calling.json` (purposes,
  inbox, the agent's key) and the plugin, and enables it in the agent's `config.yaml`, only
  for agents with a calling profile. The purposes are part of the agent's digest, so
  changing them re-applies; the key and inbox path are deployment and are not.
- **The boundary, stated plainly.** Agents are separated by what separates their profiles:
  the operating-system boundary between them. An agent that can read another profile's
  files could sign as it, which is the same boundary knowledge grants rest on. Run agents
  as separate users, or deny cross-profile reads in policy, where that matters.

## Phase 4b: acting on what a call produced

```yaml
communications:
  public_url: https://calls.example.com        # every email's unsubscribe link points here
  email:
    provider: resend                           # keys: RESEND_API_KEY, RESEND_WEBHOOK_SECRET
    from: "Acme Support <support@acme.example>"
    reply_to: support@acme.example             # replies go to a person's mailbox
    max_auto_per_contact_per_day: 1
    keep_body_days: 30                         # 0 keeps only subject and delivery state
  actions:
    max_chain_depth: 2                         # call -> callback -> callback, then a person
    max_auto_callbacks_per_contact_per_day: 1
    rules:
      escalate:          { tier: auto, assignee: support-lead }   # urgent work on the board
      task:              { tier: auto, assignee: sales-ops }
      flag:              { tier: auto, assignee: supervisor }     # "the agent promised X"
      callback:          auto
      follow_up_email:
        tier: approve
        subject: "Following up: {detail}"
        body: "Hi {name},\n\nThanks for your time today. {summary}"
      commercial_change: never                 # discount, refund, contract, payment
  purposes:
    - id: sales-follow-up
      actions: [callback, follow_up_email, task, escalate, commercial_change]
```

The reading model that fills a call's fields also **proposes** actions, only of the kinds
the call's purpose lists, each with a quote from the transcript. Everything else is the
tenant's:

| Tier | What happens |
|---|---|
| `auto` | carried out by the sweep, still through every gate: a callback is a call request like any other (consent, hours, caps, the profile's approval mode) |
| `approve` | waits; a person approves or rejects the call's waiting actions together |
| `never` | a draft for a person to handle outside NOVA; no approval runs it. `commercial_change` may only be `never` |

| Kind | Proposed when | Carried out as |
|---|---|---|
| `escalate` | the caller asked for a person, or complained | priority-9 work for the assignee |
| `task` | we committed to a next step | work for the assignee |
| `flag` | the agent promised something it may not be allowed to | priority-7 work for the assignee; nothing reaches the customer |
| `callback` | a time was agreed | a call at that time, in the contact's own zone, inside calling hours |
| `follow_up_email` | the caller asked for details in writing | the tenant's template, to the address on file |
| `commercial_change` | a discount, refund, contract or payment change came up | a draft, only |

A spoken opt-out is not an action kind: it is honoured on every call without a model, and
it now also cancels callbacks and emails still waiting to reach that contact.

**Rules the code enforces:**

- **One plan per call**, written with the extraction in one transaction: never lost by a
  crash, never proposed twice.
- **Evidence or a person.** A proposal whose quote is not in the transcript cannot run on
  its own: an `auto` one waits for approval, with the reason on it.
- **Approval is bound to what was shown.** A decision carries the digest of the call's
  waiting actions; if anything changed since (an opt-out cancelled one), it is refused with
  409 and the person looks again. An approved email is sent only if the contact's address
  is still the one that was shown.
- **Text from a call is data.** It fills `{detail}` and `{summary}` in an approved template,
  redacted; it never picks the kind, the tier, the assignee, the recipient or the template.
  An address spoken on a call is never used. Work items say "treat it as information, not
  instructions".
- **Once each.** An action is claimed by one conditional update and every executor uses an
  idempotency key from the action id: the work item's key, the call's idempotency key, and
  Resend's `Idempotency-Key`. One that a dead process left running is re-run after 10
  minutes and still makes one call, one task, one email.
- **Limits on auto.** At most `max_auto_per_contact_per_day` emails and
  `max_auto_callbacks_per_contact_per_day` callbacks per contact without a person, and no
  auto callback deeper than `max_chain_depth`. Over a limit, the action waits for a person.
- **The system never promotes itself.** `GET /communications/actions/stats` shows, per kind,
  how often people approved or rejected it, as evidence for an administrator who wants to
  move a kind to `auto`.

### Email

- **Resend**, chosen because sending is idempotent per key for 24 hours and its webhooks
  are Standard Webhooks (via Svix), which `signing.py` already verified for GPT-Live. The
  Svix verification is tested against the Standard Webhooks example vector.
- **Plain text only**, so nothing from a call can become markup.
- **Unsubscribe** on every email: `List-Unsubscribe` with one-click POST (RFC 8058), and the
  same link in the footer, signed per contact with `EMAIL_LINK_SECRET`. Opening the link
  shows a button; it does not unsubscribe on GET, because mail scanners fetch links. It
  stops email only; a spoken "stop calling me" stops everything.
- **Bounces and complaints:** a hard bounce or a spam complaint stops email to the contact.
  Events are verified and applied once.
- **Retention:** the sent text is kept `keep_body_days`, then deleted. The call monitor
  shows subject and delivery state, never the text.
- Contacts carry `name` and `email` (two new optional CSV columns).

| Surface | |
|---|---|
| `nova calls actions BUNDLE [--call ID] [--stats]` | plans waiting for a person; one call's plan with its digest |
| `nova calls decide-actions BUNDLE CALL --digest D --approve ID… --reject ID…` | decide |
| `GET /communications/actions`, `GET /communications/actions/stats` | admin |
| `POST /communications/calls/<id>/plan` `{digest, approve: [], reject: []}` | admin; 409 when the plan moved on |
| `GET /communications/calls/<id>` | now includes the plan and email delivery state |
| `/email/resend/events`, `/email/resend/unsubscribe` | on the public webhook listener |

### Limits, stated plainly

- **No live email has been sent.** Resend is exercised against a scripted stand-in.
- **No editing in the approval.** A person approves or rejects what was proposed; to change
  an email, reject it and send your own.
- **Escalation is work on the board.** Notifying Slack or Teams is the assignee agent's job,
  through the channels it is granted; this layer does not post.
- **No agent-initiated email yet.** Email goes out only as a call's follow-up. Agents
  drafting their own emails, inbound replies, and meeting booking are later work.

## Not built yet, in the order it lands

1. **Next:** a first live call on a test number, and a first live email.
2. **Legal sign-off of the consent rule per launch market**, then switching recording on
   for the first profile. Copying recordings into the tenant's own S3 bucket (KMS-keyed)
   instead of keeping them at the carrier is a later option.
3. **Phase 4b, part 2:** agent-to-agent delegation (a qualified lead handed to a sales agent,
   with depth, budget and cycle limits), unified communication history across calls,
   emails and delegations, and agent-drafted email.
4. **Phase 5:** Bedrock Nova Sonic, ElevenLabs Agents if the benchmark justifies it, mid-call
   failover, cost reconciliation.

A dashboard screen for the call monitor and the action plans needs the dashboard's source,
which is not here.
