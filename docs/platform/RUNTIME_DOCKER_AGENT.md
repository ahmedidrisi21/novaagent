# A second runtime: Docker Agent

Status: **plan, nothing built.** Source read: `docker/docker-agent` (Go, Apache-2.0).

NOVA's adapter contract (`nova/runtime/base.py`) is written in NOVA's vocabulary so that
Hermes is not the only runtime. This note maps that contract onto Docker Agent and says what
would be built, in what order, and what must be true before a customer runs on it.

## Why it fits

Docker Agent defines agents and teams in YAML, runs them as separate OS processes, and has
the enforcement points NOVA needs.

| NOVA needs | Docker Agent has | Notes |
|---|---|---|
| Fail-closed tool policy | `pre_tool_use` and `tool_guard` hooks (shell commands); failure default for both is **block** | The hook runs `nova/policy/decide.py` the same way the Hermes plugin does. Every other hook event defaults to *warn*, so only the two blocking events are used for policy |
| Per-agent tool scoping | Per-agent `toolsets`, `permissions` | Compiled from `AgentSpec`, not hand-written |
| Spend limits | `budget` config | Mapped to `max_tool_calls_per_run` and cost caps |
| Agents working together | Team config with `transfer_task`, background agents; `a2a` | NOVA's own delegation routes still govern cross-team handoffs |
| Run with no human attached | `serve api` (REST + SSE), `serve mcp`, `serve a2a` | The adapter drives agents over the API |
| Knowledge, calling, email tools | MCP toolsets | The signed calling/knowledge tools become a small MCP server, one per agent |
| Models | Bedrock, Anthropic, OpenAI, Gemini and more | Bedrock model resolution stays in NOVA's `provider.py` equivalent |

## What it does not give us (honest gaps)

- **A durable task queue.** Hermes has a kanban store that survives restarts and that
  `list_tasks` reads. Docker Agent's "board" is a terminal UI for coding agents; sessions
  live in a SQLite file. Reporting NOVA's `TaskView` needs an adapter-owned task ledger.
  Until it exists the capability must read `durable_tasks = False`.
- **Scheduling.** No per-agent cron store found. `scheduling = False` until the adapter
  owns one (reuse NOVA's own scheduler, not a second implementation).
- **Credential isolation.** A spawned process inherits the parent's environment, as with
  Hermes. Each agent must be started with only its own secrets in a clean environment, and
  readiness must report where each credential resolved from.
- **Telemetry.** Anonymous usage data goes to Docker by default. The launcher must set
  `TELEMETRY_ENABLED=false`, and a test must prove an agent process starts with it off.
- **Scale claims.** One process per agent is the same isolation model as Hermes. It is not
  faster or cheaper by itself; measure before promising a number.

## Phases

1. **Spike (no customer code).** Run one generated YAML agent behind `serve api`. Prove:
   a `tool_guard` hook blocks a call and the block survives a hook crash; budget stops a
   run; the process starts with telemetry off and a clean environment.
2. **Materialize.** `DockerAgentRuntime` registered next to Hermes: `AgentSpec` to YAML,
   digest and drift, branding, `list_agents`/`remove_agent` that refuse what NOVA did not
   create. Contract tests shared with Hermes (`test_runtime_contract.py`).
3. **Enforce.** Policy hook, tool scoping, budget. Deliberate-break checks on each, as for
   every NOVA safety rule.
4. **Tools.** Calling, delegation, email and knowledge as per-agent MCP servers, with the
   same signed inbox and gateway decisions.
5. **Work and observe.** Task ledger, health, usage per agent, audit of every run.
6. **Teams.** A tenant team compiles to one Docker Agent team; handoffs across teams still
   go through NOVA delegation (depth, cycle, budget and deadline checks).

## Rules this plan keeps

- Nothing outside `nova/runtime/docker_agent/` imports it; the registry loads it lazily.
- The Go binary is a deployment dependency, never a Python one. NOVA still imports only the
  standard library and PyYAML.
- A capability the runtime lacks is reported as lacking, never faked.
- A customer does not run on this runtime until phase 3's break checks pass.
