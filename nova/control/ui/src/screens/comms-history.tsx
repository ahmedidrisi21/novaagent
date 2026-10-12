import { Chip, GlassCard, StatusPill } from "@/components/glass";
import { Hint } from "@/components/tooltip";
import { PanelBody } from "@/components/panel";
import { usePanel } from "@/lib/hooks";
import { absoluteIso, sinceIso } from "@/lib/state";
import { commsLabel, commsState, money, type Delegation, type HistoryEvent, type Insights, type Usage }
  from "./comms-types";

export function HistoryTab({ nonce }: { nonce: number }) {
  const history = usePanel<{ events: HistoryEvent[] }>("/communications/history?limit=100", 15000, nonce);
  return (
    <PanelBody state={history} empty={(d) => d.events.length ? null : {
      title: "No communication history",
      detail: "Calls, follow-up actions and emails appear here in one timeline, newest first.",
    }}>
      {(d) => (
        <div className="space-y-2">
          {d.events.map((e) => (
            <GlassCard key={`${e.channel}-${e.id}`} className="flex items-start gap-3 p-3" interactive={false}>
              <StatusPill state={commsState(e.status)} className="mt-0.5 w-[150px] shrink-0 justify-start">
                {commsLabel(e.status)}
              </StatusPill>
              <div className="min-w-0 flex-1">
                <p className="text-ink text-[13px]">{e.what}</p>
                <p className="text-ink-faint mt-0.5 text-[11.5px]">
                  <Chip className="mr-1.5">{e.channel}</Chip>
                  {e.agent || "—"} · {e.contact_id}{e.approved_by ? ` · approved by ${e.approved_by}` : ""}
                  {e.reason ? ` · ${e.reason}` : ""}
                </p>
              </div>
              {e.cost?.best_known != null ? (
                <span className="text-ink-faint shrink-0 text-[11.5px]">{money(e.cost.best_known)}</span>
              ) : null}
              <Hint text={absoluteIso(e.at)}>
                <span className="text-ink-faint shrink-0 text-[11.5px]">{sinceIso(e.at)}</span>
              </Hint>
            </GlassCard>
          ))}
        </div>
      )}
    </PanelBody>
  );
}

export function SpendTab({ nonce }: { nonce: number }) {
  const usage = usePanel<Usage>("/communications/usage", 30000, nonce);
  const insights = usePanel<Insights>("/communications/insights?days=7", 30000, nonce);
  return (
    <div className="space-y-6">
      <PanelBody state={usage}>
        {(u) => (
          <div className="space-y-3">
            <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
              <Stat label={`Spend (${u.month})`} value={money(u.spend, u.currency)} />
              <Stat label="Committed to live calls" value={money(u.committed, u.currency)} />
              <Stat label="Minutes" value={String(Math.round(u.minutes))} />
              <Stat label="Emails" value={String(u.emails)} />
            </div>
            <p className="text-ink-muted text-[12.5px]">
              {u.limit ? `${u.percent}% of ${money(u.limit, u.currency)} monthly limit used — ${money(u.remaining, u.currency)} remaining.`
                : "No monthly limit is set, so nothing stops spend."}
              {u.unpriced.length ? ` No rate for: ${u.unpriced.join(", ")} — their cost is not counted.` : ""}
            </p>
            {Object.entries(u.by_agent).map(([agent, row]) => (
              <p key={agent} className="text-ink-faint text-[12px]">
                {agent}: {row.calls} calls · {Math.round(row.minutes)} min · {money(row.spend, u.currency)}
              </p>
            ))}
          </div>
        )}
      </PanelBody>
      <PanelBody state={insights}>
        {(i) => (
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <Stat label="Calls (7 days)" value={String(i.calls.requested)} />
            <Stat label="Connect rate" value={i.calls.connect_rate == null ? "—" : `${Math.round(i.calls.connect_rate * 100)}%`} />
            <Stat label="Blocked by policy" value={String(i.calls.blocked)} />
            <Stat label="Waiting on people"
                  value={String(i.waiting_for_people.actions + i.waiting_for_people.reviews + i.waiting_for_people.calls)} />
          </div>
        )}
      </PanelBody>
    </div>
  );
}

export function DelegationsTab({ nonce }: { nonce: number }) {
  const rows = usePanel<{ delegations: Delegation[] }>("/communications/delegations", 15000, nonce);
  return (
    <PanelBody state={rows} empty={(d) => d.delegations.length ? null : {
      title: "No delegations",
      detail: "When one agent hands work to another, the request and its outcome are listed here.",
    }}>
      {(d) => (
        <div className="space-y-2">
          {d.delegations.map((x) => (
            <GlassCard key={x.delegation_id} className="flex items-start gap-3 p-3" interactive={false}>
              <StatusPill state={commsState(x.status)} className="mt-0.5 w-[150px] shrink-0 justify-start">
                {commsLabel(x.status)}
              </StatusPill>
              <div className="min-w-0 flex-1">
                <p className="text-ink text-[13px]">{x.objective}</p>
                <p className="text-ink-faint mt-0.5 text-[11.5px]">
                  {x.from_agent} → {x.to_agent}{x.reason ? ` · ${x.reason}` : ""}
                </p>
              </div>
              <span className="text-ink-faint shrink-0 text-[11.5px]">{sinceIso(x.created_at)}</span>
            </GlassCard>
          ))}
        </div>
      )}
    </PanelBody>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <GlassCard className="p-3" interactive={false}>
      <p className="text-ink-faint text-[11.5px]">{label}</p>
      <p className="text-ink mt-1 text-lg font-semibold">{value}</p>
    </GlassCard>
  );
}
