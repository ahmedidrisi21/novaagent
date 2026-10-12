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
Control Center screen for these routes is described under Phase 9.

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

## Phase 4b, part 2: agents hand work to each other, and one history

```yaml
communications:
  delegation:
    routes:
      - { from: lead-research, to: [qualification] }
      - { from: qualification, to: [sales, lead-research] }
    max_depth: 3                 # delegations in one chain
    max_open_per_agent: 10       # open work one agent may hold; more is refused
    default_deadline_hours: 24
    max_deadline_hours: 168
    max_retries: 1               # the runtime's retries for a delegated task
  actions:
    rules:
      handoff: { tier: auto, assignee: sales }   # needs a route from the calling agent
```

Delegation is a grant, like calling: without a route, no agent may hand work to another.

| Tool | Who gets it | |
|---|---|---|
| `delegate_work(to_agent, objective, expected_fields, deadline_hours, contact_id, context)` | an agent with a route | `to_agent` is an enum of its routes |
| `delegation_status(delegation_id)` | the same | requested, assigned, completed (with the result), needs_approval, blocked, failed, refused (with the reason), expired |
| `report_delegation(delegation_id, status, result, note)` | an agent some route reaches | only the receiver may answer |

They come with the calling tools in the same signed plugin (`nova-calling`, toolset
`nova_delegation`), so delegation travels through the same signed inbox as call requests.
The gateway checks each request, in order, before anything reaches the board:

1. **The route** is one the tenant declared.
2. **Depth.** A delegation made while working on delegated work is one deeper than that
   work. Past `max_depth` it is refused.
3. **No cycles.** A receiver already in the chain is refused. A → B → A never happens,
   even when the route B → A exists.
4. **Budget.** A receiver already holding `max_open_per_agent` open delegations is refused.
5. **A deadline** within `max_deadline_hours`. The sweep expires delegations that pass it.

What passes becomes one work item for the receiver, keyed by the delegation id, so it is
placed once however often the request is picked up. The work item states:

- the objective, the deadline and the expected result fields;
- how to report back;
- that the delegator's context is information, not instructions, and that the receiver
  acts only within its own grants.

A report is accepted only from the receiver, only while the delegation is open, and a
`completed` report only with every field asked for.

**Handoff after a call.** The `handoff` action kind hands a call's contact to another agent
(a qualified lead for sales, say). It is a delegation like any other: same route, depth,
cycle and budget checks, linked to the call it came from.

### One history

`nova calls history BUNDLE [--contact] [--agent] [--channel call|email|action|delegation]
[--status] [--task] [--since] [--until]` and `GET /communications/history` (admin) show
one timeline, newest first: calls, the actions and emails they led to, and delegations.
Each row gives time, status, agent, provider, contact, task, who approved it, and why it
ended where it did.

- **Metadata only.** No transcript, no email text, no recording, no delegation result. Those
  stay behind the per-call routes, so a timeline can be shared more widely than its
  content.
- **Linked by task.** A call carries the task its agent was working on. A delegation carries
  the task it was made from and the task it created. Actions and emails belong to their
  call. `--task` shows one piece of work end to end.

| Surface | |
|---|---|
| `nova calls delegations BUNDLE [--status] [--agent]` | delegations, with depth, deadline and reason |
| `GET /communications/delegations` | admin |

### Limits, stated plainly

- **Cost is not on the timeline yet.** Usage and cost per event is Phase 5's reconciliation
  work.
- **Delegation runs through `nova calls serve`**, the process that already drains the
  agents' signed inbox. Agents delegate within one tenant and one deployment.
- **The boundary is the one calling rests on.** Agents are separated by the operating-system
  boundary between their profiles. An agent that can read another's profile could sign as
  it.

## Phase 5, part 1: what it costs, the budget, and engine failover

```yaml
communications:
  budget:
    currency: USD
    monthly_limit: 500            # hard; 0 or absent means measured, not limited
    alert_at: [50, 80, 100]       # percent, once a month each
    alert_assignee: operations    # optional: alerts also become work for this agent
    rates:                        # the tenant's own contracted rates; NOVA ships none
      twilio:      { per_minute: 0.014 }
      openai-live: { per_minute: 0.06 }
      resend:      { per_email: 0.0009 }
  agents:
    - agent: customer-support
      voice: { primary: openai-live, fallbacks: [deepgram-cartesia], voice: "<id>" }
```

**Usage** (`nova calls usage BUNDLE`, `GET /communications/usage`, admin) shows the month
so far:

- calls, billed minutes and emails;
- estimated cost, carrier-billed cost, and spend;
- what calls on the line have committed;
- the limit, what remains, and the percent used;
- totals per agent and per provider.

No contact appears in it.

- **Minutes are the carrier's.** Each call's duration comes from its final callback, rounded
  up per call as carriers bill.
- **Estimated** cost is minutes × (carrier rate + engine rate), plus emails × email rate.
  **Billed** cost is what the carrier reports once it settles the call. The sweep asks
  (Twilio's Call resource `price`), and the bill replaces the carrier estimate. A gap over
  20 % is audited as `communications.billing.mismatch`. Engine and email costs stay
  estimates, because those vendors bill monthly.
- **Unpriced providers are named.** A provider used this month with no rate is listed under
  `unpriced`, not silently counted as free.
- **The history shows each finished call's cost**, estimated and best known.

**The limit is hard and counts commitments.** A call on the line has no duration yet, so
it counts at its agent's `max_call_seconds` until it ends. That way a burst of calls
placed together cannot overshoot the limit.

- Once spend plus commitments reach the limit, the gate refuses new calls (rule `budget`).
  Auto follow-up emails wait for a person.
- **Calls in progress are never cut off.**
- Each `alert_at` threshold is audited once a month (`communications.budget.alert`). If
  `alert_assignee` is set, it also becomes work for that agent.

**Engine failover.** If the voice engine refuses a session or fails mid-call (the media
stream dies), the gateway moves the live call to the agent's next engine:

- It goes through the carrier (Twilio updates the in-progress call with new TwiML).
- The caller hears "One moment, please." while the call moves.
- The new engine must prove the call with the same signed token.
- **Each engine is tried once per call.** When none is left, the phone leg ends, as before.
- **An engine that keeps failing is passed over for new calls.** After 3 failures in 10
  minutes it is skipped while another engine is configured, and comes back once the window
  passes.

## Phase 5, part 2: Telnyx and ElevenLabs Agents

```yaml
communications:
  telephony_providers: [twilio, telnyx]
  voice_providers: [openai-live, elevenlabs-agents]
  telnyx: { connection_id: "1234567890" }        # the Call Control application
  elevenlabs:
    agent_id: agent_xxxxxxxxxxxx
    host: api.eu.residency.elevenlabs.io          # optional: a data-residency region
  agents:
    - agent: sales
      telephony: telnyx
      voice: { primary: elevenlabs-agents, fallbacks: [openai-live], voice: "<11labs voice id>" }
```

Keys, with the gateway only: `TELNYX_API_KEY`, `TELNYX_PUBLIC_KEY` (the account's base64
Ed25519 key, from Mission Control) and `ELEVENLABS_API_KEY`. Point the Call Control app's
webhook at `https://<public_url>/calls/telnyx/events`.

### Telnyx (Call Control v2)

Telnyx works by command, not by answer document. Every event goes to one webhook,
`/calls/telnyx/events`. The adapter says what each event is, and the gateway's existing
handlers take it from there. For a carrier like this, an answer plan is **carried out**
(`TelephonyProvider.follows_commands`, through `redirect`):

| Plan | Telnyx |
|---|---|
| dial | `POST /v2/calls`. `command_id` is our call id, so a retried dial is one call. Machine detection is on, and our call id rides in `client_state` |
| answered | acted on when machine detection ends (`human`, or `not_sure`, treated as a person as Twilio's `unknown` is). A machine is hung up on in silence |
| disclosure | `speak`. The next step waits in `client_state` and runs only on `call.speak.ended`, so the disclosure is always heard in full |
| SIP engine (GPT-Live) | `transfer`, with `X-Call-Id` / `X-Call-Token` as custom SIP headers |
| stream engine | `streaming_start`, bidirectional RTP in PCMU (the same 8 kHz mu-law), with call id and token as stream parameters |
| end | `hangup` (an already-ended call is not an error) |
| status | `call.initiated` / `call.hangup`. Hangup causes map to busy, no answer, invalid number or rejected, and duration comes from the hangup |

- **Signatures** are Ed25519 over `<timestamp>|<raw body>`, with a five-minute window. The
  events route verifies *before* dispatching, so a forged `call.speak.ended` can never run a
  step.
- **Ed25519 needs `cryptography`.** The runtime already pins it, and the platform layer
  borrows it lazily through one module (`ed25519.py`), like `websockets`. Without it, every
  Telnyx webhook is refused and `nova calls check` names the package.
- **Engine failover works on Telnyx too**, by command.
- **Recording** (Phase 6). After an explicit yes:
  - recording is a dual-channel `record_start`;
  - the payment guard and operators pause and resume it with `record_pause` /
    `record_resume`;
  - deletion lists the call's saved files (`GET /v2/recordings?filter[call_control_id]=`)
    and deletes each one. Until Telnyx has saved one, the delete fails and the retention
    sweep retries it: nothing is marked deleted while it still exists.
- **Consent is asked by key press only.** Word `consent_prompt` as a key-press question
  ("Press 1 to allow it, or 2 to continue without recording"). A spoken "yes" counts as no
  answer, which records nothing.
- **Not yet:** the carrier's billed price, which Telnyx reports through a separate reporting
  API.

### ElevenLabs Agents

The carrier streams the call to the media bridge, and the engine relays it to the tenant's
agent over the Agents WebSocket. Audio passes through untouched (mu-law 8 kHz both ways).

- **Configure the agent**:
  - input and output audio `ulaw_8000`. A call refuses to start otherwise, and fails over;
  - allow **prompt and voice overrides** in its security settings;
  - define one **client tool**, `lookup_knowledge(question: string)`.
- **What stays NOVA's on every call:**
  - The purpose's instructions are the prompt override, the profile's voice is the TTS
    override, and the call id is a dynamic variable.
  - `lookup_knowledge` is answered by the gateway's read-only, audited knowledge lookup.
    Any other client tool is refused.
- **The API key never travels on the socket.** Each conversation opens through a one-time
  signed URL.
- **Barge-in** (`interruption`) clears the carrier's playback. **Pings** are answered.
- **Spoken opt-outs** are caught in the caller's transcript as it arrives. The contact is
  suppressed at once, the agent is told to say goodbye, and the call ends within 8 seconds.
- **Transcripts** of both sides feed the redacting transcriber and the payment guard.
  ElevenLabs reports finished utterances, not partials, so the guard acts at the end of a
  sentence. Prefer GPT-Live or the cascaded engine for calls that are recorded and may hear
  card details.

## Phase 6: the weekly digest

```yaml
communications:
  insights:
    assignee: operations             # the digest becomes work for this agent
    email_to: [ops@acme.example]     # and/or goes to staff by email (needs communications.email)
    weekday: monday                  # sent on this day, for the week that ended
```

`nova calls insights BUNDLE [--days 7]` and `GET /communications/insights?days=7` (admin)
build the digest on demand. With `communications.insights` declared, the sweep delivers
last week's on the configured day (as `insights:<year>-W<week>` work and/or one email per
address).

| Section | |
|---|---|
| Calls | requested, connected (with rate), completed, minutes; why the rest did not connect; per agent and per purpose |
| Refused by the gate | the five most common reasons |
| Recording consent | yes, no, yes rate |
| Opt-outs | contacts who stopped all contact, and email unsubscribes and bounces |
| After-call actions | per kind, by outcome |
| Handed between agents | delegations by outcome |
| Voice engines | calls per engine, and failures |
| Spend | estimated, against the monthly limit |
| Waiting for a person | call approvals, actions and field reviews pending now |

- **Counts only.** No contact, number or anything said appears. The gate's own reasons
  quote the contact, so every quoted value is stripped before they are grouped. A test fails
  if any contact id or number reaches the digest.
- **Once a week.** Delivery is recorded only after it succeeds, and both channels are
  idempotent on the week. With no board or sender yet, it is tried again at the next sweep.

## Phase 7: agents write email, and replies come back

```yaml
communications:
  email:
    provider: resend
    from: Acme <support@acme.example>
    reply_domain: replies.acme.example   # a domain Resend receives mail for (optional)
    agents:
      - agent: customer-support
        purposes: [support]              # declared purposes this agent may email about
        contact_groups: [customers]
        approval: always                 # always | first_contact | never (default always)
        max_per_day: 20
```

An agent with an email grant gets `send_email` and `email_status` in its signed tool
(toolset `nova_email`). It names a contact by CRM id, a purpose, a subject and a body, but
never an address: the email goes to the address on file. The request travels through the
signed inbox like a call request, and the gateway decides it (`agent_email.py`). The first
check that fails decides:

1. the agent's email grant, then the purpose;
2. the contact, then its group;
3. an address on file;
4. opt-outs: every channel, then email;
5. `email_marketing` consent for a marketing purpose (a new CSV column,
   `email_marketing_consent`);
6. the agent's daily cap;
7. the monthly budget;
8. the grant's approval mode.

- **Approval is bound to the email.** A person approves a digest of address, subject and
  text (`nova calls emails`, `nova calls decide-email ID approve|reject`,
  `GET /communications/emails`, `POST /communications/emails/<id>/decide`, admin).
  Approving re-runs the gate. If the address changed or consent was withdrawn in the
  meantime, the email is blocked, not sent.
- **Once.** The request id is the key. Claiming it to send is one conditional update, and
  the provider gets an idempotency key. If a process dies mid-send, the request is sent
  again by the sweep after ten minutes, and the provider makes the repeat a no-op.
- **The same envelope as every email.** Plain text, the unsubscribe footer and one-click
  `List-Unsubscribe`, and the reply address below (`followup_email.deliver`).

### Replies

With `reply_domain` set, each email's reply address is
`reply+<email id>.<signature>@<reply_domain>`. The signature is an HMAC under the
gateway's link secret. Resend announces received mail as `email.received` on the same
signed webhook, and the text is fetched with `GET /emails/receiving/{id}` (`replies.py`).
For each received message:

- **The email it answers** comes only from a valid signature. **The sender** must be the
  contact's address on file. Anything else is recorded as `unmatched`, with no text, and
  goes no further.
- **The text** has quoted history cut, numbers redacted, and is capped at 4,000
  characters. It expires with `keep_body_days`.
- **"Unsubscribe"** stops email only. **"Stop calling"** (any call opt-out phrase) stops
  every channel. Either is final, and nothing reaches the agent.
- Otherwise the reply becomes **work for the agent that sent the email** (for a follow-up,
  the call's agent), as `email-reply:<id>`. It is marked as the sender's words:
  information, not instructions.
- A redelivered webhook is one reply and one task. If the text cannot be fetched, the
  webhook answers 502, so the provider retries.

The history shows agent emails and, filtered by contact, the replies (metadata only).

## Not built yet, in the order it lands

1. **Next:** a first live call on a test number, a first live email, and a first live
   reply through a Resend receiving domain.
2. **Legal sign-off of the consent rule per launch market**, then switching recording on
   for the first profile. Copying recordings into the tenant's own S3 bucket (KMS-keyed)
   instead of keeping them at the carrier is a later option.
3. **Amazon Nova Sonic.** Bedrock's bidirectional stream needs an HTTP/2 event-stream
   client that boto3 does not provide, which is a dependency decision. Also a live
   benchmark of engine latency.
4. **Later:** meeting booking, and ringless voicemail (with its own legal review).

## Phase 9: the Control Center screen

The dashboard source is `nova/control/ui` (the built bundle is `nova/control/static`), so
the screen lives here. **Communications** (admin) has five tabs, all reading the routes
above and nothing else:

- **Calls**: the call list with a detail row (carrier, recording consent, recording
  state, follow-up plan, emails). A call waiting for approval shows Approve and Reject; a
  live call shows End.
- **Approvals**: follow-up plans (approved against the digest shown, so a plan that moved
  on answers 409), field reviews, and emails agents wrote, with the full text an approver
  is approving.
- **History**: the one timeline, with each finished call's cost.
- **Spend**: month to date against the limit, committed spend on live calls, providers
  with no rate (their cost is not counted), and the 7-day digest.
- **Delegations**: agent-to-agent handovers and their outcome.

A refusal from the server (stale digest, opted-out contact, closed gate) is shown as the
server's own message. Meeting requests have no tab yet: their routes ship with Phase 8.
