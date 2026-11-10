// Shared by catalogue picks and manual ids; explain policy without implying support for every provider.
export const AUTO_THINKING_HELP = `Auto follows <button type="button" class="reset" data-thinking-settings>Automatic thinking tuning</button> for supported direct Claude/Gemini models; otherwise it leaves reasoning to the provider. Explicit levels override tuning.`;

export const THINKING_HELP = `<div id="thinking-policy-help" class="thinking-help-popup" popover="auto" role="tooltip">
  <b>Lightweight automatic reasoning</b>
  <p>With tuning On and effort Auto, Glide requests less reasoning for the Fast role to reduce latency and cost. This is model-specific, not a universal reasoning level.</p>
  <table><thead><tr><th>Direct provider / model</th><th>Fast role</th><th>Deep role</th></tr></thead><tbody>
    <tr><td>Claude Sonnet</td><td>Low effort; thinking between tool calls</td><td>Medium effort</td></tr>
    <tr><td>Claude Opus 4 / newer Opus</td><td>Low effort; thinking disabled / adaptive</td><td>Medium effort</td></tr>
    <tr><td>Claude Haiku 5</td><td>Low effort; thinking disabled</td><td>Medium effort</td></tr>
    <tr><td>Claude Haiku 4.5 and older</td><td colspan="2">Unchanged</td></tr>
    <tr><td>Gemini Pro</td><td>Low reasoning</td><td>Provider default</td></tr>
    <tr><td>Other Gemini 2.x / newer Gemini</td><td>None / minimal reasoning</td><td>Provider default</td></tr>
    <tr><td>OpenAI-compatible endpoints / OpenRouter</td><td colspan="2">Unaffected by this switch</td></tr>
  </tbody></table>
  <p>Off: Auto sends no automatic thinking override. Explicit effort always takes priority. Unsupported automatic options can fall back to provider defaults.</p>
</div>`;

/** Native popover for Escape/outside click; hover and keyboard focus expose the same table. */
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
