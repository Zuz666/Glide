// Shared by catalogue picks and manual ids; capability decisions come only from the engine.
export const AUTO_THINKING_HELP = `Auto follows <button type="button" class="reset" data-thinking-settings>Automatic effort tuning</button>: Fast requests low and Deep requests medium only when the serving route verifies that exact level. Otherwise, the provider default applies. Explicit effort is unaffected by the switch.`;

export const THINKING_HELP = `<div id="thinking-policy-help" class="thinking-help-popup" popover="auto" role="tooltip">
  <b>Automatic effort tuning</b>
  <table><thead><tr><th>Role</th><th>Requested effort</th></tr></thead><tbody>
    <tr><td>Fast</td><td>low</td></tr>
    <tr><td>Deep</td><td>medium</td></tr>
  </tbody></table>
  <p>Applies only with tuning On, effort Auto, and verified support for the exact role target on the selected serving route. Off, an unavailable target, or unknown support leaves effort to the provider; no other level is substituted. Explicit effort is unaffected by the switch.</p>
  <p>These are requested controls, not measured reasoning. Provider defaults are not fixed effort levels. Rejected automatic requests report an error instead of trying another effort or provider defaults.</p>
  <p>Effort is not a latency, cost or spending guarantee. Output Budget remains separate: OpenCode explicit effort keeps it unchanged, while Auto retains the existing empty-answer retry behavior and can request a larger budget. Higher effort can consume the budget before a visible answer.</p>
</div>`;

/** Display the resolver's reason; never infer capabilities from model ids or manual choices. */
export function providerDefaultEffort(reason?: string): string {
  if (reason === "off") return "provider default (tuning Off)";
  if (reason === "unsupported") return "provider default (target unavailable)";
  return "provider default (support unknown)";
}

/** Native popover for Escape/outside click; hover and keyboard focus expose the same rule. */
export function bindThinkingHelp(host: HTMLElement): void {
  const trigger = host.querySelector<HTMLButtonElement>("[data-thinking-help]");
  const popup = host.querySelector<HTMLElement>("#thinking-policy-help");
  if (!trigger || !popup) return;
  const show = () => { if (!popup.matches(":popover-open")) popup.showPopover(); };
  const leave = () => window.setTimeout(() => {
    if (popup.isConnected && popup.matches(":popover-open") && !trigger.matches(":hover, :focus") && !popup.matches(":hover")) popup.hidePopover();
  }, 150);
  trigger.onmouseenter = trigger.onfocus = trigger.onclick = show;
  trigger.onmouseleave = trigger.onblur = popup.onmouseleave = leave;
}
