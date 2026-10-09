const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
const usdCents = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2 });

export const money = (n: number | null | undefined) => (n == null ? "–" : usd.format(n));
export const moneyCents = (n: number | null | undefined) => (n == null ? "–" : usdCents.format(n));
export const pct = (n: number) => `${Math.round(n * 100)}%`;

export function date(d: string | null | undefined) {
  if (!d) return "–";
  const dt = new Date(d.length === 10 ? d + "T00:00:00" : d);
  return dt.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}

export function dateTime(d: string) {
  return new Date(d).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

export const PAYERS: Record<string, string> = {
  NS401: "Northstar",
  CSA77: "Coastal Senior",
  SMP12: "Sunshine Medicaid",
  MRD55: "Meridian PPO",
};

export const ACTION_LABEL: Record<string, string> = {
  APPEAL: "Appeal",
  CORRECTED_CLAIM: "Corrected claim",
  SEND_RECORDS: "Send records",
  REBILL_OTHER_PAYER: "Check eligibility, rebill",
  VERIFY_DUPLICATE: "Confirm duplicate",
  ESCALATE_CREDENTIALING: "Credentialing",
  WRITE_OFF: "Write off",
  MANUAL_REVIEW: "Review notes",
};

export const BUCKET_LABEL: Record<string, string> = {
  RECOVERABLE: "Recoverable",
  UNLIKELY: "Unlikely",
  LOST: "Lost",
  NO_LOSS: "No loss",
  RESOLVED: "Recovered",
};

export function dueText(days: number | null | undefined) {
  if (days == null) return "–";
  if (days < 0) return "Closed";
  if (days === 0) return "Today";
  return `${days} day${days === 1 ? "" : "s"}`;
}

export function dueTone(days: number | null | undefined): "urgent" | "soon" | "calm" | "none" {
  if (days == null || days < 0) return "none";
  if (days <= 14) return "urgent";
  if (days <= 30) return "soon";
  return "calm";
}
