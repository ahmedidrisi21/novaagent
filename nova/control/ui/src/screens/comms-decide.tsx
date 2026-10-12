import * as React from "react";
import { post } from "@/lib/api";

/** Approve / reject buttons that show the server's own refusal instead of swallowing it.
 *  A stale digest, a closed gate and an opted-out contact all arrive as a message here. */
export function DecideButtons({
  path, body, onDone, approveLabel = "Approve", rejectLabel = "Reject" as string | null,
}: {
  path: string; body: (action: "approve" | "reject") => unknown; onDone: () => void;
  approveLabel?: string; rejectLabel?: string | null;
}) {
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState("");
  const act = async (action: "approve" | "reject") => {
    setBusy(true);
    setError("");
    try {
      await post(path, body(action));
      onDone();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "the decision was not recorded");
    } finally {
      setBusy(false);
    }
  };
  const button = "glass-solid text-ink rounded-md px-3 py-1.5 text-[12.5px] font-medium disabled:opacity-50";
  return (
    <div className="space-y-1.5">
      <div className="flex gap-2">
        <button type="button" className={button} disabled={busy} onClick={() => act("approve")}>
          {approveLabel}
        </button>
        {rejectLabel ? (
          <button type="button" className={`${button} text-blocked`} disabled={busy}
                  onClick={() => act("reject")}>
            {rejectLabel}
          </button>
        ) : null}
      </div>
      {error ? <p role="alert" className="text-blocked text-[11.5px]">{error}</p> : null}
    </div>
  );
}
