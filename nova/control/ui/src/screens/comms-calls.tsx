import * as React from "react";
import { Phone } from "lucide-react";
import { Chip, GlassCard, StatusPill } from "@/components/glass";
import { PanelBody } from "@/components/panel";
import { usePanel } from "@/lib/hooks";
import { sinceIso, absoluteIso } from "@/lib/state";
import { Hint } from "@/components/tooltip";
import { DecideButtons } from "./comms-decide";
import { commsLabel, commsState, type CallDetail, type CallRow } from "./comms-types";

/** Live actions the API accepts, by call state. The server re-checks; this only decides
 *  which buttons are worth showing. */
const CAN_END = new Set(["dialing", "ringing", "answered", "in_progress"]);

export function CallsTab({ nonce, changed }: { nonce: number; changed: () => void }) {
  const calls = usePanel<{ calls: CallRow[]; active: number }>("/communications/calls", 8000, nonce);
  const [open, setOpen] = React.useState<string | null>(null);
  return (
    <PanelBody state={calls} empty={(d) => d.calls.length ? null : {
      title: "No calls yet",
      detail: "Calls appear when an agent with a calling grant asks to phone a contact.",
      hint: "nova calls list <bundle>",
    }}>
      {(data) => (
        <div className="space-y-2">
          {data.calls.map((call) => (
            <div key={call.call_id}>
              <GlassCard className="flex w-full items-start gap-3 p-3 text-left" role="button"
                tabIndex={0} onClick={() => setOpen(open === call.call_id ? null : call.call_id)}
                onKeyDown={(e: React.KeyboardEvent) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    setOpen(open === call.call_id ? null : call.call_id);
                  }
                }}>
                <StatusPill state={commsState(call.status)} className="mt-0.5 w-[150px] shrink-0 justify-start">
                  {commsLabel(call.status)}
                </StatusPill>
                <div className="min-w-0 flex-1">
                  <p className="text-ink text-[13px]">{call.purpose} call · {call.agent}</p>
                  <p className="text-ink-faint mt-0.5 text-[11.5px]">
                    {call.contact_id}{call.voice_engine ? ` · ${call.voice_engine}` : ""}
                    {call.reason ? ` · ${call.reason}` : ""}
                  </p>
                </div>
                <Hint text={absoluteIso(call.created_at)}>
                  <span className="text-ink-faint shrink-0 text-[11.5px]">{sinceIso(call.created_at)}</span>
                </Hint>
              </GlassCard>
              {open === call.call_id ? <CallDetailView call={call} changed={changed} /> : null}
            </div>
          ))}
        </div>
      )}
    </PanelBody>
  );
}

function CallDetailView({ call, changed }: { call: CallRow; changed: () => void }) {
  const detail = usePanel<CallDetail>(`/communications/calls/${call.call_id}`, 0);
  return (
    <div className="border-glass-border mt-1 ml-4 space-y-3 border-l pl-4 pt-2 pb-3">
      <PanelBody state={detail}>
        {(d) => (
          <>
            <div className="flex flex-wrap gap-1.5">
              <Chip><Phone className="mr-1 size-3" />{d.telephony || "no carrier yet"}</Chip>
              <Chip>recording consent: {d.recording_consent || "not asked"}</Chip>
              {d.recordings.map((r, i) => (
                <Chip key={i}>recording {r.state}{r.duration_seconds ? ` · ${r.duration_seconds}s` : ""}</Chip>
              ))}
              {d.emails.map((e) => <Chip key={e.email_id}>email “{e.subject}” · {e.status}</Chip>)}
            </div>
            {d.plan?.actions.length ? (
              <ul className="text-ink-muted space-y-1 text-[12px]">
                {d.plan.actions.map((a) => (
                  <li key={a.action_id}>{a.kind} ({a.tier}) — {commsLabel(a.status)}{a.quote ? ` · “${a.quote}”` : ""}</li>
                ))}
              </ul>
            ) : null}
            {d.status === "approval_required" ? (
              <DecideButtons path={`/communications/calls/${d.call_id}/decide`} onDone={changed}
                body={(action) => ({ action })} approveLabel="Approve call" rejectLabel="Reject call" />
            ) : CAN_END.has(d.status) ? (
              <DecideButtons path={`/communications/calls/${d.call_id}/decide`} onDone={changed}
                body={() => ({ action: "end" })} approveLabel="End call" rejectLabel={null} />
            ) : null}
          </>
        )}
      </PanelBody>
    </div>
  );
}
