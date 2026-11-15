// Shared wording for known subtotals and completed-response coverage; legacy streams omit coverage.
import type { CostCoverage } from "../shared/engine.js";

export function sessionCostText(amount: number | undefined, coverage?: CostCoverage): string {
  const dollars = `$${(amount ?? 0).toFixed((amount ?? 0) < 10 ? 3 : 2)}`;
  if (!coverage) return dollars;
  const { reported, estimated, unknown, stale_estimates } = coverage;
  if (unknown > 0 && reported + estimated === 0) return "Unknown (incomplete)";
  const notes: string[] = [];
  if (estimated > 0) notes.push("includes estimates");
  if (unknown > 0) notes.push("incomplete");
  if (stale_estimates > 0) notes.push("stale rates");
  return notes.length ? `${dollars} (${notes.join("; ")})` : dollars;
}
