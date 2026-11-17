const $ = (id) => document.getElementById(id);
const state = { token: "", epoch: 0, busy: false, next: null, selected: null, intent: null };
const requests = new Set();
function el(tag, text, className) { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (className) node.className = className; return node; }
function notice(message, error = false, target = "notice") { $(target).textContent = message; $(target).classList.toggle("error", error); }
function stamp(value) { return value ? new Date(value).toLocaleString() + " · " + value : "Not recorded"; }
function buttons() {
  $("load").disabled = $("first").disabled = !state.token || state.busy;
  $("next").disabled = !state.token || state.busy || !state.next;
  $("resume").hidden = !state.intent;
  $("retry-form").hidden = !state.selected?.retry.allowed || !!state.intent;
  $("new-review").hidden = !["succeeded", "failed", "expired", "cancelled"].includes(state.intent?.outcome?.state);
  const working = state.busy || !!state.intent?.busy;
  $("reason").disabled = $("acknowledge").disabled = !!state.intent || working;
  $("review-retry").disabled = working || !!state.intent || !state.selected?.retry.allowed || !$("reason").value.trim() || !$("acknowledge").checked;
  $("confirm").disabled = working || !state.intent?.confirmable;
  $("withdraw").disabled = working || !["awaiting_confirmation", "queued"].includes(state.intent?.outcome?.state);
  $("refresh").disabled = working || !state.selected;
}
function clearAccess(message) {
  state.epoch += 1; requests.forEach((request) => request.abort());
  Object.assign(state, { token: "", busy: false, next: null, selected: null, intent: null });
  $("credential").value = $("reason").value = ""; $("acknowledge").checked = false;
  ["records", "details", "intent"].forEach((id) => $(id).replaceChildren()); $("snapshot").textContent = "";
  $("review-dialog").close(); notice("", false, "review-status"); notice(message); buttons();
}
async function api(path, method = "GET", body = null, key = null) {
  const epoch = state.epoch, token = state.token, controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 12000); requests.add(controller);
  try {
    const response = await fetch(path, { method, headers: { Authorization: "Bearer " + token, "Content-Type": "application/json", "X-Request-ID": crypto.randomUUID(), ...(key ? { "Idempotency-Key": key } : {}) },
      body: method === "GET" ? undefined : JSON.stringify(body), credentials: "omit", cache: "no-store", redirect: "error", referrerPolicy: "no-referrer", signal: controller.signal });
    const data = await response.json();
    if (epoch !== state.epoch) { const error = new Error("Access changed"); error.stale = true; throw error; }
    if (!response.ok) {
      const error = new Error(data.error?.message || "The request could not be verified.");
      if ([401, 403].includes(response.status)) { clearAccess("Access is unavailable. Sign in again."); error.stale = true; }
      throw error;
    }
    return data;
  } catch (error) { if (epoch !== state.epoch) error.stale = true; if (error.name === "AbortError") error.message = "Request interrupted. Resume with the same request to verify its status."; throw error; }
  finally { clearTimeout(timeout); requests.delete(controller); }
}
async function load(after = null) {
  if (state.busy) return; state.busy = true; buttons(); notice("Loading current delivery records…");
  try {
    const page = await api("/internal/notifications/uncertain/query", "POST", { limit: 10, after });
    state.next = page.next_cursor; $("snapshot").textContent = "Workspace " + page.namespace.namespace + " · Updated " + stamp(page.generated_at);
    $("records").replaceChildren();
    for (const item of page.items) {
      const row = el("tr"); row.append(el("td", new Date(item.created_at).toLocaleString()));
      const recipient = el("td", item.target.external_id); recipient.append(el("small", item.target.provider)); row.append(recipient);
      row.append(el("td", item.template_id), el("td", item.total_attempts + " / " + item.maximum_attempts));
      const status = el("td"); status.append(el("span", "Uncertain", "state"));
      if (item.known_successful_attempts) status.append(el("small", "Earlier success verified")); row.append(status);
      const action = el("td"), button = el("button", "Review delivery", "quiet"); button.type = "button"; button.addEventListener("click", () => open(item.notification_id)); action.append(button); row.append(action); $("records").append(row);
    }
    notice(page.items.length ? "Review the full attempt history before allowing another attempt." : "No uncertain notifications on this page.");
  } catch (error) { if (!error.stale) notice(error.message, true); }
  finally { state.busy = false; buttons(); }
}
function renderDetails(item) {
  $("details").replaceChildren(); const meta = el("div", undefined, "meta");
  for (const [label, value] of [["Recipient", item.target.external_id], ["Status", item.state], ["Template", item.template_id], ["Attempts", item.total_attempts + " / " + item.maximum_attempts]]) { const cell = el("p"); cell.append(el("strong", label + ": "), document.createTextNode(value)); meta.append(cell); }
  $("details").append(meta, el("h3", "Original message"), el("pre", item.content ? [item.content.title, item.content.summary, ...item.content.fields.map((f) => f.label + ": " + f.value)].join("\n") : "Original message could not be verified.", "message"), el("h3", "Attempt history"));
  for (const attempt of item.attempts) {
    const success = ["delivered", "late_delivered"].includes(attempt.state), box = el("div", undefined, "attempt" + (success ? " success" : ""));
    box.append(el("strong", "Attempt " + attempt.number + " · " + attempt.state.replaceAll("_", " ")), el("p", "Started " + stamp(attempt.started_at), "muted small"));
    if (attempt.finished_at) box.append(el("p", "Recorded " + stamp(attempt.finished_at), "muted small"));
    if (attempt.error_code) box.append(el("p", attempt.error_code, "muted small"));
    if (attempt.message_ref) box.append(el("p", "Receipt " + attempt.channel_ref + " / " + attempt.message_ref, "muted small")); $("details").append(box);
  }
  if (!item.retry.allowed) $("details").append(el("p", "Another attempt is blocked: " + item.retry.code.replaceAll("_", " ").toLowerCase() + ".", "warning"));
}
async function open(id) {
  if (state.busy) return;
  if (state.intent) { $("review-dialog").showModal(); return; }
  state.busy = true; buttons();
  try { state.selected = await api("/internal/notifications/" + id); renderDetails(state.selected); $("reason").value = ""; $("acknowledge").checked = false; $("intent").replaceChildren(); notice("", false, "review-status"); $("review-dialog").showModal(); }
  catch (error) { if (!error.stale) notice(error.message, true); }
  finally { state.busy = false; buttons(); }
}
function canonicalMatches(review, intent) {
  const request = review.request, p = request.parameters, expected = intent.input.parameters;
  return request.command === "notification.retry_uncertain" && request.scope.type === "global" && request.scope.id === "*" &&
    Object.keys(p).sort().join() === Object.keys(expected).sort().join() && Object.keys(expected).every((key) => p[key] === expected[key]);
}
async function refreshIntent() {
  const intent = state.intent;
  if (!intent) { state.selected = await api("/internal/notifications/" + state.selected.notification_id); renderDetails(state.selected); return; }
  if (!intent.commandId) { await submitIntent(); return; }
  const review = await api("/internal/commands/" + intent.commandId + "/review");
  if (!canonicalMatches(review, intent)) throw new Error("The reviewed intent does not match this request.");
  const current = await api("/internal/notifications/" + intent.input.parameters.notification_id);
  state.selected = current; renderDetails(current); intent.outcome = review.outcome;
  intent.proof = review.outcome.state === "awaiting_confirmation" ? review.outcome.proof : null;
  intent.confirmable = !!intent.proof && current.retry.allowed && current.last_attempt_id === intent.input.parameters.expected_attempt_id;
  $("intent").replaceChildren(el("h3", "Canonical approval request"), el("p", "Reason: " + review.request.parameters.reason), el("p", "This may send a duplicate message.", "warning"));
  if (review.outcome.expires_at) $("intent").append(el("p", "Confirm by " + stamp(review.outcome.expires_at), "muted small"));
  const labels = { awaiting_confirmation: "Awaiting your confirmation.", queued: "Approval is queued for processing.", succeeded: "Approval recorded. Delivery status is shown above.", failed: "Approval failed: " + (review.outcome.code || "Unknown"), expired: "Approval expired. No new confirmation is available.", cancelled: "Approval withdrawn." };
  notice(labels[review.outcome.state] || "Approval status could not be verified.", false, "review-status"); buttons();
}
async function submitIntent() {
  const intent = state.intent;
  const result = await api("/internal/commands", "POST", intent.input, intent.key);
  if (!result.outcome) throw new Error("Request was not accepted: " + (result.error_code || "Unknown"));
  intent.commandId = result.outcome.command_id; await refreshIntent();
}
async function work(action) {
  if (state.busy || state.intent?.busy) return;
  state.busy = true; if (state.intent) { state.intent.busy = true; state.intent.confirmable = false; } buttons();
  try { await action(); }
  catch (error) { if (!error.stale) notice(error.message, true, "review-status"); }
  finally { state.busy = false; if (state.intent) state.intent.busy = false; buttons(); }
}
$("use-credential").addEventListener("click", () => { const token = $("credential").value.trim(); clearAccess("Credential set. Load records to verify your current permission."); state.token = token; buttons(); });
$("clear-access").addEventListener("click", () => clearAccess("Access cleared. Accepted approvals are unchanged."));
$("load").addEventListener("click", () => load()); $("first").addEventListener("click", () => load()); $("next").addEventListener("click", () => load(state.next));
$("close").addEventListener("click", () => $("review-dialog").close()); $("resume").addEventListener("click", () => { $("review-dialog").showModal(); work(refreshIntent); });
$("reason").addEventListener("input", buttons); $("acknowledge").addEventListener("change", buttons);
$("new-review").addEventListener("click", () => { state.intent = null; $("intent").replaceChildren(); $("reason").value = ""; $("acknowledge").checked = false; notice("Review the current evidence before starting a new request.", false, "review-status"); buttons(); });
$("review-retry").addEventListener("click", () => {
  if ($("review-retry").disabled) return;
  state.intent = { key: crypto.randomUUID(), confirmKey: crypto.randomUUID(), withdrawKey: crypto.randomUUID(), commandId: null, proof: null, outcome: null, busy: false, confirmable: false,
    input: { command: "notification.retry_uncertain", parameters: { notification_id: state.selected.notification_id, expected_attempt_id: state.selected.last_attempt_id, reason: $("reason").value.trim(), acknowledge_possible_duplicate: true } } };
  work(submitIntent);
});
$("refresh").addEventListener("click", () => work(refreshIntent));
$("confirm").addEventListener("click", () => {
  if ($("confirm").disabled) return; const intent = state.intent, proof = intent.proof;
  work(async () => { await api("/internal/commands/" + intent.commandId + "/confirm", "POST", { proof }, intent.confirmKey); await refreshIntent(); });
});
$("withdraw").addEventListener("click", () => { if ($("withdraw").disabled) return; const intent = state.intent; work(async () => { await api("/internal/commands/" + intent.commandId + "/withdraw", "POST", {}, intent.withdrawKey); await refreshIntent(); }); });
buttons();
