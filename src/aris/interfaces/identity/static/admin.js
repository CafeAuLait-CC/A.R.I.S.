const $ = (id) => document.getElementById(id);
const state = { token: "", epoch: 0, busy: false, source: null, page: null, next: null, stale: false, intent: null };
const requests = new Set();
function element(tag, text, className) {
  const item = document.createElement(tag);
  if (text !== undefined) item.textContent = text;
  if (className) item.className = className;
  return item;
}
function notice(message, error = false, target = "notice") {
  $(target).textContent = message;
  $(target).classList.toggle("error", error);
}
function buttons() {
  $("load-slack").disabled = $("load-registered").disabled = !state.token || state.busy;
  $("next-page").disabled = !state.token || state.busy || !state.next;
  $("resume-request").hidden = !state.intent;
  document.querySelectorAll(".member-review").forEach((button) => {
    button.disabled = state.busy || state.stale || !!state.intent || button.dataset.blocked === "true";
  });
}
function clearAccess(message) {
  state.epoch += 1;
  requests.forEach((request) => request.abort());
  Object.assign(state, { token: "", busy: false, page: null, source: null, next: null, stale: false, intent: null });
  $("credential").value = "";
  $("reason").value = ""; $("filter").value = "";
  $("members-head").replaceChildren(); $("members-body").replaceChildren();
  $("snapshot").textContent = ""; $("review-content").replaceChildren();
  $("review-dialog").close(); notice(message); buttons();
}
async function api(path, method = "POST", body = {}, key = null) {
  const epoch = state.epoch, token = state.token, controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 12000);
  requests.add(controller);
  try {
    const response = await fetch(path, {
      method, headers: { Authorization: "Bearer " + token, "Content-Type": "application/json", "X-Request-ID": crypto.randomUUID(), ...(key ? { "Idempotency-Key": key } : {}) },
      body: method === "GET" ? undefined : JSON.stringify(body),
      cache: "no-store", credentials: "omit", redirect: "error", referrerPolicy: "no-referrer", signal: controller.signal,
    });
    const data = await response.json();
    if (epoch !== state.epoch) { const error = new Error("Access changed"); error.stale = true; throw error; }
    if (!response.ok) {
      const error = new Error(data.error?.message || "The request could not be verified.");
      error.status = response.status; error.code = data.error?.code; throw error;
    }
    return data;
  } catch (error) {
    if (epoch !== state.epoch) error.stale = true;
    if (error.name === "AbortError") error.message = "Request interrupted. Retry to verify its status.";
    throw error;
  } finally { clearTimeout(timeout); requests.delete(controller); }
}
function stamp(value) {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "Unknown" : date.toLocaleString() + " · " + date.toISOString();
}
async function load(source, cursor = null) {
  if (state.busy) return;
  state.busy = true; state.stale = true; buttons(); notice("Loading the current directory…");
  try {
    const data = await api(source === "slack" ? "/internal/identity/slack-members/query" : "/internal/identity/directory/query",
      "POST", source === "slack" ? { cursor } : { after: cursor, limit: 20 });
    state.source = source; state.stale = false;
    state.page = { namespace: source === "slack" ? data.namespace : data.namespace.namespace,
      revision: data.revision, node_ids: data.node_ids,
      rows: source === "slack" ? data.members : data.members.map((member) => ({ slack: null, registered: member })) };
    state.next = data.next_cursor;
    $("snapshot").textContent = "Workspace " + state.page.namespace + " · ARIS revision " + data.revision +
      (source === "slack" ? " · Slack fetched " + stamp(data.slack_fetched_at) : " · Registered ARIS identities");
    renderRows(); notice(data.members.length ? "Review a member's mappings before confirming changes." : "No members on this page. A next page may still be available.");
  } catch (error) { if (!error.stale) notice(error.message, true); }
  finally { state.busy = false; buttons(); }
}
function input(value, placeholder, max = 128) {
  const field = element("input"); field.value = value; field.placeholder = placeholder;
  field.maxLength = max; field.autocomplete = "off"; field.spellcheck = false; return field;
}
function renderRows() {
  const page = state.page;
  const heading = element("tr");
  ["Member", "ARIS identity", "Profile", ...page.node_ids, ""].forEach((text) => heading.append(element("th", text)));
  $("members-head").replaceChildren(heading); $("members-body").replaceChildren();
  const filter = $("filter").value.trim().toLowerCase();
  for (const row of page.rows) {
    const registered = row.registered, slack = row.slack;
    const memberId = slack?.external_id || registered.external_id;
    const name = slack?.display_name || registered.profile.display_name;
    const tr = element("tr"), who = element("td"), identity = element("td"), profile = element("td");
    tr.dataset.search = (name + " " + memberId + " " + (registered?.profile.display_name || "")).toLowerCase();
    tr.hidden = !!filter && !tr.dataset.search.includes(filter);
    who.append(element("div", name, "member-name"), element("div", memberId, "member-id"));
    if (slack?.deleted === true) who.append(element("p", "Slack deactivated", "muted small"));
    if (slack?.is_bot === true || slack?.is_app_user === true) who.append(element("p", "Slack bot / app", "muted small"));
    if (slack?.home_team_id && slack.home_team_id !== page.namespace) who.append(element("p", "External workspace member", "muted small"));
    identity.append(element("span", registered ? registered.status : "Not registered", registered?.status === "active" ? "state" : "state muted-state"));
    const nameInput = input(name.length <= 128 ? name : "", "ARIS display name");
    const zoneInput = input(slack?.timezone || "", "IANA timezone, e.g. UTC");
    nameInput.setAttribute("aria-label", memberId + " ARIS display name");
    zoneInput.setAttribute("aria-label", memberId + " timezone");
    if (registered) {
      profile.append(element("div", registered.profile.display_name), element("p", registered.profile.timezone, "muted small"));
    } else { profile.append(nameInput, zoneInput); }
    tr.append(who, identity, profile);
    const fields = new Map(); let multiple = false;
    for (const node of page.node_ids) {
      const cell = element("td"), links = (registered?.mappings || []).filter((mapping) => mapping.node_id === node);
      const field = input(links.length === 1 ? links[0].os_username : "", "Linux username");
      field.setAttribute("aria-label", memberId + " " + node + " Linux username");
      if (links.length > 1) {
        multiple = true; field.disabled = true;
        cell.append(element("div", links.map((mapping) => mapping.os_username).join(", ")),
          element("p", "Multiple aliases: explicit batch edit required", "muted small"));
      } else {
        cell.append(field);
        if (links[0] && (links[0].confidence !== "verified" || links[0].source !== "manual")) cell.append(element("p", "Unverified mapping; review to verify", "muted small"));
      }
      fields.set(node, { field, previous: links[0] || null }); tr.append(cell);
    }
    const action = element("td"), review = element("button", "Review mappings", "member-review"); review.type = "button";
    const eligible = !!registered || (slack?.deleted === false && slack?.is_bot === false && slack?.is_app_user !== true);
    review.dataset.blocked = String(multiple || !eligible);
    if (!eligible) action.append(element("p", "An active human member must be verified before registration.", "muted small"));
    review.addEventListener("click", () => beginReview(row, fields, nameInput, zoneInput));
    action.append(review); tr.append(action); $("members-body").append(tr);
  }
  buttons();
}
function beginReview(row, fields, nameInput, zoneInput) {
  if (state.busy || state.intent || state.stale) return;
  try {
    const reason = $("reason").value.trim();
    if (!reason) throw new Error("Enter a reason for the mapping changes.");
    const values = [...fields].map(([node, entry]) => ({ node, value: entry.field.value.trim(), previous: entry.previous }));
    if (values.some((entry) => entry.value && /\s/.test(entry.value))) throw new Error("Linux usernames cannot contain whitespace.");
    const registered = row.registered, changes = [];
    const memberId = row.slack?.external_id || registered.external_id;
    if (!registered) {
      const displayName = nameInput.value.trim(), timezone = zoneInput.value.trim();
      if (!displayName || !timezone) throw new Error("Enter the new ARIS display name and a timezone.");
      try { new Intl.DateTimeFormat("en", { timeZone: timezone }).format(); } catch { throw new Error("Enter a known IANA timezone."); }
      changes.push({ type: "register_identity", subject: { provider: "slack", namespace: state.page.namespace, external_id: memberId },
        profile: { display_name: displayName, timezone }, mappings: values.filter((entry) => entry.value).map((entry) => ({ node_id: entry.node, os_username: entry.value })) });
    } else {
      for (const entry of values) {
        if (entry.previous && entry.previous.os_username !== entry.value) changes.push({ type: "remove_node_user", node_id: entry.node,
          os_username: entry.previous.os_username, expected_user_id: registered.user_id });
        if (entry.value && (!entry.previous || entry.previous.os_username !== entry.value || entry.previous.confidence !== "verified" || entry.previous.source !== "manual")) changes.push({
          type: "assign_node_user", node_id: entry.node, os_username: entry.value, user_id: registered.user_id,
          expected_user_id: entry.previous?.os_username === entry.value ? registered.user_id : null,
        });
      }
    }
    if (!changes.length) throw new Error("Mappings are unchanged.");
    state.intent = { key: crypto.randomUUID(), confirmKey: crypto.randomUUID(), withdrawKey: crypto.randomUUID(), commandId: null, proof: null,
      memberId, userId: registered?.user_id || null, namespace: state.page.namespace, nodeIds: [...state.page.node_ids], terminal: false, busy: false,
      input: { command: "identity.map_users", parameters: { expected_revision: state.page.revision, reason, changes } } };
    $("review-content").replaceChildren(); $("review-dialog").showModal(); buttons(); submitIntent();
  } catch (error) { notice(error.message, true); }
}
function commandRequest(path, body, key) {
  // The operation key is stable across retries, including an interrupted response.
  return api(path, "POST", body, key);
}
function reviewBusy(message) {
  $("confirm-review").disabled = $("withdraw-review").disabled = $("refresh-review").disabled = true;
  notice(message, false, "review-status");
}
async function submitIntent() {
  const intent = state.intent; if (!intent || intent.busy) return;
  intent.busy = true;
  reviewBusy("Preparing the original request. No mappings have been changed.");
  try {
    const result = await commandRequest("/internal/commands", intent.input, intent.key);
    if (!result.outcome?.command_id) throw new Error("The request was not accepted.");
    intent.commandId = result.outcome.command_id; intent.busy = false; await refreshReview();
  } catch (error) {
    if (error.stale) return;
    if ([400, 401, 403, 413, 415, 422].includes(error.status) && !intent.commandId) intent.terminal = true;
    notice(error.message + (intent.terminal ? " Close to edit or reload." : " Refresh retries the same intent."), true, "review-status");
    $("refresh-review").disabled = false;
  } finally { intent.busy = false; }
}
function validTarget(change, intent) {
  if (change.type === "register_identity") return change.subject.provider === "slack" && change.subject.namespace === intent.namespace && change.subject.external_id === intent.memberId &&
    change.mappings.every((mapping) => intent.nodeIds.includes(mapping.node_id));
  if (change.type === "assign_node_user") return change.user_id === intent.userId && intent.nodeIds.includes(change.node_id);
  if (change.type === "remove_node_user") return change.expected_user_id === intent.userId && intent.nodeIds.includes(change.node_id);
  return false;
}
async function refreshReview() {
  const intent = state.intent; if (!intent || intent.busy) return;
  if (!intent.commandId) { if (!intent.terminal) await submitIntent(); return; }
  intent.busy = true;
  reviewBusy("Verifying the original request and current directory…");
  try {
    const review = await api("/internal/commands/" + encodeURIComponent(intent.commandId) + "/review", "GET");
    intent.proof = null;
    const content = $("review-content"); content.replaceChildren();
    const parameters = review.request.parameters;
    if (review.request.command !== "identity.map_users" || review.request.scope.type !== "global" || review.request.scope.id !== "*" || !parameters.changes.every((change) => validTarget(change, intent))) throw new Error("The canonical request target differs. Withdraw and reload.");
    content.append(element("p", "Member ID: " + intent.memberId), element("p", "Workspace: " + intent.namespace),
      element("p", "Expected directory revision: " + parameters.expected_revision), element("h3", "Changes"));
    const list = element("ul");
    for (const change of parameters.changes) {
      if (change.type === "register_identity") {
        list.append(element("li", "Register “" + change.profile.display_name + "” · " + change.subject.external_id + " · " + change.profile.timezone));
        change.mappings.forEach((mapping) => list.append(element("li", "Link " + mapping.node_id + " / " + mapping.os_username)));
        if (!change.mappings.length) list.append(element("li", "Register without Linux username links."));
      } else if (change.type === "assign_node_user") list.append(element("li", "Link " + change.node_id + " / " + change.os_username + " → " + change.user_id));
      else list.append(element("li", "Remove " + change.node_id + " / " + change.os_username + " from " + change.expected_user_id));
    }
    content.append(list, element("h3", "Reason"), element("p", parameters.reason), element("p", "No reservation roles are granted by this request.", "muted small"));
    const outcome = review.outcome;
    $("withdraw-review").disabled = !["awaiting_confirmation", "queued"].includes(outcome.state);
    if (outcome.state === "awaiting_confirmation") {
      const current = await api("/internal/identity/directory/query", "POST", { limit: 1 });
      content.append(element("p", "Confirm by " + stamp(outcome.expires_at), "muted small"));
      if (outcome.execution_not_after) content.append(element("p", "Must run by " + stamp(outcome.execution_not_after), "muted small"));
      if (current.revision !== parameters.expected_revision) notice("The directory changed. Withdraw this request, reload and create a new review.", true, "review-status");
      else { intent.proof = outcome.proof; $("confirm-review").disabled = false; notice("Nothing has changed yet. Confirm queues this exact request for the worker.", false, "review-status"); }
    } else {
      const messages = { queued: "Confirmed and queued. Waiting for the worker; refresh for the result.", succeeded: "Mapping changes saved. Reload the directory before another change.",
        failed: "The mapping batch was not completed. Reload before starting a new request.", expired: "The request expired. Reload to start a new review.", cancelled: "The unexecuted request was withdrawn." };
      notice(messages[outcome.state] || "Current result is unavailable.", outcome.state === "failed", "review-status");
      intent.terminal = ["succeeded", "failed", "expired", "cancelled"].includes(outcome.state);
      if (intent.terminal) state.stale = true;
    }
  } catch (error) { if (!error.stale) notice(error.message, true, "review-status"); }
  finally { intent.busy = false; $("refresh-review").disabled = false; buttons(); }
}
async function operate(kind) {
  const intent = state.intent; if (!intent?.commandId || intent.busy) return;
  if (kind === "confirm" && !intent.proof) return;
  intent.busy = true;
  reviewBusy(kind === "confirm" ? "Submitting confirmation…" : "Withdrawing the unexecuted request…");
  try {
    await commandRequest("/internal/commands/" + encodeURIComponent(intent.commandId) + "/" + kind,
      kind === "confirm" ? { proof: intent.proof } : {}, kind === "confirm" ? intent.confirmKey : intent.withdrawKey);
    intent.busy = false; await refreshReview();
  } catch (error) { if (!error.stale) { notice(error.message + " Refresh to verify the result.", true, "review-status"); $("refresh-review").disabled = false; } }
  finally { intent.busy = false; }
}
$("use-credential").addEventListener("click", () => {
  const token = $("credential").value.trim(); if (!token) { notice("Enter a user credential.", true); return; }
  clearAccess("Credential set. Load a directory to verify access."); state.token = token; buttons();
});
$("sign-out").addEventListener("click", () => clearAccess("Access cleared."));
$("load-slack").addEventListener("click", () => load("slack"));
$("load-registered").addEventListener("click", () => load("registered"));
$("next-page").addEventListener("click", () => load(state.source, state.next));
$("filter").addEventListener("input", () => {
  const filter = $("filter").value.trim().toLowerCase();
  document.querySelectorAll("#members-body tr").forEach((row) => { row.hidden = !!filter && !row.dataset.search.includes(filter); });
});
$("resume-request").addEventListener("click", () => { $("review-dialog").showModal(); refreshReview(); });
$("close-review").addEventListener("click", () => $("review-dialog").close());
$("review-dialog").addEventListener("close", () => {
  if (state.intent?.terminal) { state.intent = null; $("review-content").replaceChildren(); }
  buttons();
});
$("refresh-review").addEventListener("click", refreshReview);
$("confirm-review").addEventListener("click", () => operate("confirm"));
$("withdraw-review").addEventListener("click", () => operate("withdraw"));
buttons();
