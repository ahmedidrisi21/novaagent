import * as React from "react";
import { ApprovalsTab } from "./comms-approvals";
import { CallsTab } from "./comms-calls";
import { DelegationsTab, HistoryTab, SpendTab } from "./comms-history";

const TABS = [
  { id: "calls", label: "Calls" },
  { id: "approvals", label: "Approvals" },
  { id: "history", label: "History" },
  { id: "spend", label: "Spend" },
  { id: "delegations", label: "Delegations" },
] as const;

/** Voice and email, one screen. A decision bumps `nonce` so every tab re-reads at once
 *  instead of showing an approval that has already been settled. */
export function CommunicationsScreen() {
  const [tab, setTab] = React.useState<(typeof TABS)[number]["id"]>("calls");
  const [nonce, setNonce] = React.useState(0);
  const changed = () => setNonce((n) => n + 1);
  return (
    <div className="space-y-5">
      <div role="tablist" aria-label="Communications" className="flex flex-wrap gap-1.5">
        {TABS.map((t) => (
          <button key={t.id} type="button" role="tab" aria-selected={tab === t.id}
            onClick={() => setTab(t.id)}
            className={`rounded-md px-3 py-1.5 text-[12.5px] font-medium ${
              tab === t.id ? "glass-solid text-ink" : "text-ink-faint hover:text-ink"}`}>
            {t.label}
          </button>
        ))}
      </div>
      {tab === "calls" ? <CallsTab nonce={nonce} changed={changed} />
        : tab === "approvals" ? <ApprovalsTab nonce={nonce} changed={changed} />
        : tab === "history" ? <HistoryTab nonce={nonce} />
        : tab === "spend" ? <SpendTab nonce={nonce} />
        : <DelegationsTab nonce={nonce} />}
    </div>
  );
}
