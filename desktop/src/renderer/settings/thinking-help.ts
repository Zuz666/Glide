// Shared by catalogue picks and manual ids; explain policy without implying support for every provider.
export const AUTO_THINKING_HELP = `Auto follows <button type="button" class="reset" data-thinking-settings>Automatic thinking tuning</button> for supported direct Claude/Gemini models and supported OpenCode Claude models via native Messages; otherwise it leaves reasoning to the provider. Explicit levels bypass the entire tuning policy, not just its effort setting.`;

export const THINKING_HELP = `<div id="thinking-policy-help" class="thinking-help-popup" popover="auto" role="tooltip">
  <b>Lightweight automatic reasoning</b>
  <p>With tuning On and effort Auto, Glide requests less reasoning for the Fast role to reduce latency and cost. This is model-specific, not a universal reasoning level.</p>
  <p>Reviewed OpenCode Claude models use native Messages, with model-specific differences listed below. Gemini rows apply only to direct Gemini. Other OpenCode families and unknown models keep provider defaults.</p>
  <table><thead><tr><th>Provider / model</th><th>Fast role</th><th>Deep role</th></tr></thead><tbody>
    <tr><td>Direct Claude Sonnet / OpenCode Sonnet 5.5</td><td>Low effort; thinking between tool calls</td><td>Medium effort</td></tr>
    <tr><td>OpenCode Sonnet 5 / 4.6</td><td>Low effort; thinking disabled</td><td>Medium effort</td></tr>
    <tr><td>OpenCode Fable 5 / 5.1 / 5.2</td><td>Low effort; default thinking</td><td>Medium effort</td></tr>
    <tr><td>Claude Opus 4 / newer Opus</td><td>Low effort; thinking disabled / adaptive</td><td>Medium effort</td></tr>
    <tr><td>Direct Claude Haiku 5 / OpenCode Haiku 5.5</td><td>Low effort; thinking disabled</td><td>Medium effort</td></tr>
    <tr><td>Claude Haiku 4.5 and older / OpenCode Sonnet 4.5</td><td colspan="2">Unchanged</td></tr>
    <tr><td>Gemini Pro</td><td>Low reasoning</td><td>Provider default</td></tr>
    <tr><td>Other Gemini 2.x / newer Gemini</td><td>None / minimal reasoning</td><td>Provider default</td></tr>
    <tr><td>OpenAI-compatible endpoints / OpenRouter</td><td colspan="2">Unaffected by this switch</td></tr>
  </tbody></table>
  <p>Off: Auto sends no automatic thinking override. Explicit effort bypasses the entire automatic policy, including thinking shaping and remembered fallback, and does not fall back after refusal. It leaves native thinking defaults in place: effort is not a universal thinking on/off switch. Haiku 5.5 Fast Auto requests low with thinking disabled; manual low leaves native adaptive thinking. On default-off Claude 4.x, manual high does not enable thinking.</p>
  <p>OpenCode explicit effort keeps Output Budget unchanged; other providers retain their existing empty-answer retry policy. Higher effort can consume the budget before a visible answer; it is not a spending cap. Rejected automatic options can fall back to remembered provider defaults for that model and role; the selected-model preview reflects that separate automatic fallback.</p>
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
