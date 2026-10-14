import { CircleCheck } from "lucide-react";
import { EmptyState, GlassCard } from "@/components/glass";
import { PanelBody } from "@/components/panel";
import { usePanel } from "@/lib/hooks";
import { sinceIso } from "@/lib/state";
import { DecideButtons } from "./comms-decide";
import type { EmailRequest, Plan, Review } from "./comms-types";

/** Everything a person must settle before a communication reaches a customer: call
 *  action plans (bound to a digest), extracted fields, and emails agents wrote. */
export function ApprovalsTab({ nonce, changed }: { nonce: number; changed: () => void }) {
  const plans = usePanel<{ plans: Plan[] }>("/communications/actions", 10000, nonce);
  const reviews = usePanel<{ reviews: Review[] }>("/communications/reviews", 10000, nonce);
  const emails = usePanel<{ emails: EmailRequest[] }>("/communications/emails", 10000, nonce);
  const none = plans.state === "ok" && reviews.state === "ok" && emails.state === "ok"
    && !plans.data.plans.length && !reviews.data.reviews.length && !emails.data.emails.length;
  if (none) {
    return <EmptyState icon={CircleCheck} title="Nothing is waiting on a person"
      detail="Plans, field reviews and agent emails that need a decision appear here, each bound to what you were shown." />;
  }
  return (
    <div className="space-y-6">
      <Section title="Follow-up plans">
        <PanelBody state={plans}>
          {(d) => d.plans.map((plan) => (
            <GlassCard key={plan.call_id} className="space-y-2 p-4" interactive={false}>
              <p className="text-ink-faint font-mono text-[11.5px]">call {plan.call_id}</p>
              <ul className="text-ink space-y-1 text-[13px]">
                {plan.actions.filter((a) => a.status === "awaiting_approval").map((a) => (
                  <li key={a.action_id}>{a.kind}{a.quote ? ` — “${a.quote}”` : ""}</li>
                ))}
              </ul>
              <DecideButtons path={`/communications/calls/${plan.call_id}/plan`} onDone={changed}
                body={(action) => {
                  const ids = plan.actions.filter((a) => a.status === "awaiting_approval")
                    .map((a) => a.action_id);
                  return { digest: plan.digest, [action]: ids };
                }} approveLabel="Approve all" rejectLabel="Reject all" />
            </GlassCard>
          ))}
        </PanelBody>
      </Section>
      <Section title="Field reviews">
        <PanelBody state={reviews}>
          {(d) => d.reviews.map((r) => (
            <GlassCard key={r.review_id} className="space-y-2 p-4" interactive={false}>
              <p className="text-ink text-[13px]">
                {r.name}: <b>{r.value}</b>{r.current_value ? ` (now ${r.current_value})` : ""}
              </p>
              <p className="text-ink-faint text-[11.5px]">
                {r.contact_id}{r.quote ? ` · “${r.quote}”` : ""}{r.reason ? ` · ${r.reason}` : ""}
              </p>
              <DecideButtons path={`/communications/reviews/${r.review_id}/decide`} onDone={changed}
                body={(action) => ({ action })} />
            </GlassCard>
          ))}
        </PanelBody>
      </Section>
      <Section title="Agent emails">
        <PanelBody state={emails}>
          {(d) => d.emails.map((e) => (
            <GlassCard key={e.request_id} className="space-y-2 p-4" interactive={false}>
              <p className="text-ink text-[13px] font-medium">{e.subject}</p>
              <p className="text-ink-faint text-[11.5px]">
                {e.agent} → {e.to_addr} · {e.purpose} · {sinceIso(e.created_at)}
              </p>
              <p className="text-ink-muted text-[12.5px] whitespace-pre-wrap">{e.body}</p>
              <DecideButtons path={`/communications/emails/${e.request_id}/decide`} onDone={changed}
                body={(action) => ({ action })} />
            </GlassCard>
          ))}
        </PanelBody>
      </Section>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="space-y-2">
      <h3 className="text-ink-muted text-[12px] font-semibold tracking-wide uppercase">{title}</h3>
      {children}
    </div>
  );
}
