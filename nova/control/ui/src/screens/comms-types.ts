/* Shapes of /platform/v1/communications/*. Loose where the store passes rows through. */
import type { State } from "@/components/glass";

export type CallRow = {
  call_id: string; agent: string; contact_id: string; purpose: string; task_id?: string;
  telephony?: string; status: string; failure?: string; reason?: string; approved_by?: string;
  voice_engine?: string; recording_consent?: string; created_at: string; updated_at: string;
};
export type PlanAction = {
  action_id: string; call_id: string; kind: string; tier: string; status: string;
  quote?: string; note?: string; error?: string; params_json?: string; created_at: string;
};
export type Plan = { call_id: string; digest: string; actions: PlanAction[] };
export type CallDetail = CallRow & {
  recordings: Array<{ state: string; duration_seconds?: number; started_at?: string; retention_until?: string }>;
  extraction: unknown; plan: Plan | null;
  emails: Array<{ email_id: string; subject: string; status: string; detail?: string }>;
};
export type Review = {
  review_id: string; contact_id: string; name: string; value: string; current_value?: string;
  call_id?: string; quote?: string; confidence?: number; reason?: string; created_at: string;
};
export type EmailRequest = {
  request_id: string; agent: string; contact_id: string; purpose: string; to_addr: string;
  subject: string; body: string; digest: string; status: string; reason?: string; created_at: string;
};
export type HistoryEvent = {
  at: string; channel: string; id: string; status: string; agent: string; contact_id: string;
  what: string; reason?: string; approved_by?: string;
  cost?: { estimated?: number; best_known?: number } | null;
};
export type Delegation = {
  delegation_id: string; from_agent: string; to_agent: string; objective: string; status: string;
  reason?: string; deadline?: string; created_at: string;
};
export type Usage = {
  month: string; currency: string; limit: number; calls: number; minutes: number; emails: number;
  spend: number; estimated: number; committed: number; remaining: number | null;
  percent: number | null; unpriced: string[];
  by_agent: Record<string, { calls: number; minutes: number; spend: number }>;
  by_provider: Record<string, Record<string, number>>;
};
export type Insights = {
  calls: { requested: number; connected: number; completed: number; minutes: number;
    blocked: number; connect_rate: number | null; failed: Record<string, number> };
  blocked_reasons: Array<Record<string, unknown>> | Record<string, number>;
  waiting_for_people: { actions: number; reviews: number; calls: number };
  recording_consent: { granted: number; refused: number; unclear: number; yes_rate: number | null };
};

const STATE: Record<string, State> = {
  completed: "neutral", done: "neutral", sent: "neutral", delivered: "neutral", approved: "neutral",
  in_progress: "running", answered: "running", dialing: "running", ringing: "running",
  running: "running", queued: "waiting", requested: "waiting", sending: "waiting",
  awaiting_approval: "waiting", approval_required: "waiting", pending: "waiting",
  needs_approval: "waiting", assigned: "waiting",
  failed: "blocked", policy_blocked: "blocked", rejected: "blocked", cancelled: "neutral",
  expired: "blocked", blocked: "blocked", busy: "blocked", no_answer: "waiting",
};

export const commsState = (status: string): State => STATE[status.replace(/^reply /, "")] ?? "info";
export const commsLabel = (status: string) => {
  const text = status.replace(/_/g, " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
};
export const money = (value: number | null | undefined, currency = "USD") =>
  value == null ? "—" : new Intl.NumberFormat(undefined, { style: "currency", currency,
    maximumFractionDigits: 2 }).format(value);
