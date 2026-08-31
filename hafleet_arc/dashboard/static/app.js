const esc = (value) => String(value ?? "").replace(/[&<>\"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[char]));
const state = { data: null, chatMessages: new Map(), stream: null, reconnectTimer: null, chatFilters: { module: "", role: "", round: "", kind: "" } };
const roles = ["architect", "planner", "implementer", "tester", "reviewer", "postflight"];
const labels = { architect: "Architect", planner: "Planner", implementer: "Implementer", tester: "Tester", reviewer: "Reviewer", postflight: "Postflight" };
const icons = { architect: "⌂", planner: "✎", implementer: "⚙", tester: "▣", reviewer: "✓", postflight: "◈" };
const stages = ["architecture", "design", "implement", "test", "review", "postflight", "completed"];

function statusText(status) { return ({ working: "Working", success: "Done", failed: "Failed", paused: "Paused", idle: "Waiting" })[status] || status || "Waiting"; }
function characterSvg(role, status) { return `<img class="character-art ${status === "working" ? "working" : ""}" src="/assets/${role}.svg" alt="${labels[role]} character" loading="lazy" />`; }

function renderRoom(data) {
  const characters = data.characters || [];
  const planner = characters.find((character) => character.id === "planner");
  const tester = characters.find((character) => character.id === "tester");
  const hasLegacyPlanner = Boolean(planner && (planner.session_id || (planner.tasks || []).length));
  const hasConfiguredTester = Boolean(tester && (tester.session_id || (tester.tasks || []).length));
  const visibleRoles = roles.filter((role) => (role !== "planner" || hasLegacyPlanner) && (role !== "tester" || hasConfiguredTester));
  document.querySelector("#room").innerHTML = visibleRoles.map((role) => {
    const item = characters.find((character) => character.id === role) || { id: role, label: labels[role], status: "idle", message: "Waiting for work" };
    return `<button class="workstation ${esc(item.status)}" data-character="${role}" aria-label="Open ${labels[role]} details"><div class="bubble">${esc(item.message || statusText(item.status))}</div><div class="monitor"><span>${icons[role]}</span><small>${esc(item.phase || "idle")}</small></div><div class="worker">${characterSvg(role, item.status)}<div class="worker-tag"><strong>${labels[role]}</strong><span>${esc(statusText(item.status))}</span></div></div><div class="task-cards">${(item.tasks || []).map((task) => `<span class="task-card" data-task-module="${esc(task.node_id)}">${esc(task.node_id)}</span>`).join("") || (item.module_id ? `<span class="task-card">${esc(item.module_id)}</span>` : "")}</div></button>`;
  }).join("");
  document.querySelectorAll("[data-character]").forEach((button) => button.addEventListener("click", (event) => {
    const task = event.target.closest("[data-task-module]");
    if (task) { event.stopPropagation(); openCharacter(button.dataset.character, task.dataset.taskModule); return; }
    openCharacter(button.dataset.character);
  }));
}

function renderPipeline(data) {
  const active = (data.modules || []).find((item) => item.status === "running")?.phase || (data.runner?.state === "completed" ? "completed" : "architecture");
  const tester = (data.characters || []).find((character) => character.id === "tester");
  const hasConfiguredTester = Boolean(tester && (tester.session_id || (tester.tasks || []).length));
  const visibleStages = stages.filter((stage) => stage !== "test" || hasConfiguredTester);
  document.querySelector("#pipeline").innerHTML = visibleStages.map((stage) => `<div class="pipeline-stage ${stage === active ? "active" : ""}"><span>${stage}</span></div>`).join("");
}

function renderModules(data) {
  const modules = data.modules || [];
  document.querySelector("#module-count").textContent = `${modules.length} modules`;
  document.querySelector("#modules").innerHTML = modules.length ? modules.map((item) => {
    const conversation = (data.conversations || []).find((entry) => entry.module_id === item.node_id);
    const findings = Number(conversation?.major_findings || item.major_findings || 0);
    const round = Number(conversation?.round || item.round || 0);
    return `<button class="module-row" data-module="${esc(item.node_id)}"><span class="module-mark ${esc(item.status)}"></span><span><strong>${esc(item.node_id)}</strong><small>${esc(item.phase || "")} ${round ? `· review round ${round}` : ""}</small></span>${findings ? `<span class="module-findings">${findings} major</span>` : ""}<span class="status ${esc(item.status)}">${esc(statusText(item.status))}</span></button>`;
  }).join("") : `<p class="subtle">No modules yet.</p>`;
  document.querySelectorAll("#modules [data-module]").forEach((button) => button.addEventListener("click", () => {
    const module = (state.data?.modules || []).find((item) => item.node_id === button.dataset.module);
    const role = (state.data?.characters || []).find((item) => item.phase === module?.phase)?.id || "reviewer";
    state.chatFilters.module = button.dataset.module;
    updateChatFilters();
    renderExecution();
    openCharacter(role, button.dataset.module);
  }));
}

function chatRoleLabel(role) { return labels[role] || String(role || "System"); }
function chatMessageText(message) {
  const payload = message.payload || {};
  if (message.kind === "review.feedback") return payload.summary || "Reviewer requested changes";
  if (message.kind === "review.verdict") return payload.passed ? "Review approved" : `Changes requested (${(payload.blocking_findings || []).length} major/blocker)`;
  if (message.kind === "test.completed") return payload.summary || "Tests passed";
  if (message.kind === "test.failed" || message.kind === "test.feedback") return payload.summary || "Tests failed";
  if (message.kind === "pipeline.state") return `Pipeline: ${payload.status || "updated"}`;
  return payload.response || payload.message || `${message.kind || "message"}`;
}
function activityFromMessage(message) {
  const payload = message.payload || {};
  const kind = String(message.kind || "message");
  const system = kind.startsWith("operation.") || kind === "pipeline.state" || kind === "checkpoint.created" || String(message.from || "").toLowerCase() === "orchestrator";
  let summary = chatMessageText(message);
  let title = kind.replace(/[._]/g, " ");
  let severity = payload.severity || (payload.blocking_findings?.length ? "major" : "");
  if (kind === "review.feedback") { title = "Review feedback"; summary = payload.summary || "Reviewer requested changes"; }
  if (kind === "review.verdict") { title = payload.passed ? "Review approved" : "Changes requested"; summary = payload.passed ? "No blocking findings" : `Changes requested (${(payload.blocking_findings || []).length} major/blocker)`; }
  if (kind === "pipeline.state") { title = "Pipeline state"; }
  if (kind === "test.completed") { title = "Tests passed"; }
  if (kind === "test.failed" || kind === "test.feedback") { title = "Test failures"; severity = severity || "major"; }
  return { id: `message:${message.id || message.sequence || `${message.created_at}:${kind}`}`, timestamp: message.created_at || message.timestamp || "", category: system ? "system" : "agent", kind, role: message.from || "system", module_id: message.module_id || "", phase: message.phase || payload.phase || "", round: Number(message.round || payload.round || 0), severity, title, summary, payload, raw: message };
}

function activityFromEvent(event, index) {
  const type = String(event.type || event.kind || "event");
  const moduleId = event.module_id || event.node_id || "";
  const role = event.role || event.agent || "system";
  const summary = event.message || event.reason || event.state || event.status || type;
  return { id: `event:${event.id || event.sequence || `${event.timestamp || index}:${type}:${moduleId}`}`, timestamp: event.timestamp || event.created_at || "", category: "system", kind: type, role, module_id: moduleId, phase: event.phase || "", round: Number(event.round || 0), severity: event.severity || "", title: type.replace(/[._]/g, " "), summary, payload: event, raw: event };
}

function activityFromModule(module, index) {
  return { id: `module:${module.node_id || index}`, timestamp: module.updated_at || module.timestamp || "", category: "module", kind: "module.state", role: "system", module_id: module.node_id || "", phase: module.phase || "", round: Number(module.round || 0), severity: module.status === "failed" ? "blocker" : "", title: "Module state", summary: module.message || statusText(module.status), payload: module, raw: module };
}

function allActivities(data) {
  const seen = new Set();
  const activities = [];
  (data?.events || []).forEach((event, index) => { const item = activityFromEvent(event, index); if (!seen.has(item.id)) { seen.add(item.id); activities.push(item); } });
  (data?.modules || []).forEach((module, index) => { const item = activityFromModule(module, index); if (!seen.has(item.id)) { seen.add(item.id); activities.push(item); } });
  Array.from(state.chatMessages.values()).forEach((message) => { const item = activityFromMessage(message); if (!seen.has(item.id)) { seen.add(item.id); activities.push(item); } });
  return activities.sort((a, b) => String(b.timestamp).localeCompare(String(a.timestamp)) || String(b.id).localeCompare(String(a.id))).slice(0, 240);
}

function activityIcon(activity) { return activity.category === "agent" ? (icons[activity.role] || "•") : activity.category === "module" ? "▣" : "⚡"; }
function renderExecution() {
  const data = state.data || {};
  const filters = state.chatFilters;
  const activities = allActivities(data).filter((activity) => {
    if (filters.module && String(activity.module_id || "") !== filters.module) return false;
    if (filters.role && String(activity.role || "") !== filters.role) return false;
    if (filters.round && String(activity.round || 0) !== filters.round) return false;
    if (filters.kind && String(activity.kind || "") !== filters.kind) return false;
    return true;
  });
  const target = document.querySelector("#chat");
  const count = document.querySelector("#activity-count");
  if (count) count.textContent = `${activities.length} activities`;
  if (!target) return;
  target.innerHTML = activities.length ? activities.map((activity) => {
    const findings = ["review.feedback", "test.failed", "test.feedback"].includes(activity.kind) ? (activity.payload.findings || []).map((finding) => { const reqs = Array.isArray(finding.requirement_ids) && finding.requirement_ids.length ? ` · ${finding.requirement_ids.map(esc).join(", ")}` : ""; return `<li class="chat-finding ${esc(finding.severity || "major")}"><strong>${esc(finding.severity || "major")}</strong> ${finding.id ? `<code>${esc(finding.id)}</code> ` : ""}${esc(finding.title || finding.description || "Finding")}${reqs}</li>`; }).join("") : "";
    const clickable = activity.module_id ? "true" : "false";
    return `<article class="activity-row ${esc(activity.category)} ${esc(activity.role || "system")} ${esc(activity.kind)}" data-activity-module="${esc(activity.module_id)}" data-activity-role="${esc(activity.role)}" tabindex="${clickable === "true" ? "0" : "-1"}" role="${clickable === "true" ? "button" : "article"}><div class="activity-icon" aria-hidden="true">${activityIcon(activity)}</div><div class="activity-content"><div class="activity-head"><strong>${esc(activity.title)}</strong><span class="activity-source">${esc(chatRoleLabel(activity.role))}</span>${activity.module_id ? `<span class="activity-module">${esc(activity.module_id)}</span>` : ""}${activity.phase ? `<span class="activity-phase">${esc(activity.phase)}</span>` : ""}${activity.round ? `<span class="activity-round">round ${activity.round}</span>` : ""}${activity.severity ? `<span class="activity-severity ${esc(activity.severity)}">${esc(activity.severity)}</span>` : ""}<time>${esc(activity.timestamp)}</time></div><p>${esc(activity.summary)}</p>${findings ? `<ul class="chat-findings">${findings}</ul>` : ""}</div></article>`;
  }).join("") : `<p class="subtle">No activity matches the current filters.</p>`;
  target.querySelectorAll("[data-activity-module]").forEach((row) => {
    if (!row.dataset.activityModule) return;
    const open = () => openCharacter(row.dataset.activityRole || "reviewer", row.dataset.activityModule);
    row.addEventListener("click", open);
    row.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); } });
  });
}

function updateChatFilters() {
  const activities = allActivities(state.data || {});
  const choices = [["chat-module-filter", "module_id", "All modules"], ["chat-role-filter", "role", "All roles"], ["chat-round-filter", "round", "All rounds"], ["chat-kind-filter", "kind", "All activity types"]];
  choices.forEach(([id, key, empty]) => {
    const select = document.querySelector(`#${id}`); if (!select) return;
    const filterKey = { module_id: "module", role: "role", round: "round", kind: "kind" }[key];
    const current = state.chatFilters[filterKey];
    const unique = [...new Set(activities.map((activity) => String(activity[key] || "")).filter(Boolean))].sort((a, b) => a.localeCompare(b));
    select.innerHTML = `<option value="">${empty}</option>${unique.map((value) => `<option value="${esc(value)}">${esc(value)}</option>`).join("")}`;
    select.value = current;
  });
}

function renderChat() { renderExecution(); }
function addChatMessage(message) {
  if (!message || message.kind === "heartbeat") return;
  const key = message.id || `sequence-${message.sequence}`;
  if (state.chatMessages.has(key)) return;
  state.chatMessages.set(key, message);
  updateChatFilters();
  renderExecution();
}
function connectChatStream() {
  if (!window.EventSource) { document.querySelector("#chat-connection").textContent = "Polling fallback"; return; }
  if (state.stream) state.stream.close();
  const stream = new EventSource("/api/stream"); state.stream = stream;
  stream.onopen = () => { document.querySelector("#chat-connection").textContent = "Live"; };
  stream.onerror = () => { document.querySelector("#chat-connection").textContent = "Reconnecting…"; stream.close(); window.clearTimeout(state.reconnectTimer); state.reconnectTimer = window.setTimeout(connectChatStream, 2000); };
  ["turn.request", "turn.started", "turn.completed", "agent.message", "test.request", "test.started", "test.case.generated", "test.completed", "test.failed", "test.feedback", "test.artifact.created", "review.feedback", "review.verdict", "operation.started", "operation.completed", "operation.failed", "pipeline.state", "checkpoint.created", "heartbeat"].forEach((kind) => stream.addEventListener(kind, (event) => {
    try { addChatMessage(JSON.parse(event.data)); } catch {}
  }));
}

function render(data) {
  state.data = data;
  const runner = data.runner || {};
  const runnerEl = document.querySelector("#runner");
  runnerEl.textContent = statusText(runner.state);
  runnerEl.className = `pill ${runner.state || "unknown"}`;
  const executionRunner = document.querySelector("#execution-runner");
  if (executionRunner) { executionRunner.textContent = statusText(runner.state); executionRunner.className = `pill ${runner.state || "unknown"}`; }
  document.querySelector("#run-message").textContent = runner.message || "Factory is ready";
  document.querySelector("#updated").textContent = data.generated_at ? `Updated ${new Date(data.generated_at).toLocaleTimeString()}` : "";
  document.querySelector("#session-count").textContent = `${(data.sessions || []).length} Codex sessions`;
  (data.messages || []).forEach(addChatMessage);
  updateChatFilters();
  renderExecution();
  renderRoom(data); renderPipeline(data); renderModules(data);
}

function characterById(id) { return (state.data?.characters || []).find((item) => item.id === id) || { id, label: labels[id], status: "idle", phase: "idle", message: "Waiting for work" }; }
function sessionById(id) { return (state.data?.sessions || []).find((item) => item.id === id); }
function detailRows(item) { return `<div class="detail-grid"><span>Status</span><strong class="status ${esc(item.status)}">${esc(statusText(item.status))}</strong><span>Phase</span><strong>${esc(item.phase)}</strong><span>Module</span><strong>${esc(item.module_id || "—")}</strong><span>Workspace</span><strong class="path">${esc(item.workspace || item.worktree?.path || "—")}</strong><span>Branch</span><strong>${esc(item.worktree?.branch || "—")}</strong><span>Changed files</span><strong>${(item.files_changed || []).length}</strong></div>`; }
function changeStatusLabel(status) { return ({ A: "Added", M: "Modified", D: "Deleted", R: "Renamed", "??": "Untracked" })[status] || status || "Changed"; }
function changeSourceLabel(source) {
  return ({
    planner_output: "Planner output",
    architect_commit: "Architect commit",
    implementer_worktree: "Implementer worktree",
    reviewer_worktree: "Reviewer worktree",
    module_checkpoint: "Module checkpoint",
    postflight_worktree: "Postflight worktree",
    postflight_commit: "Postflight commit",
  })[source] || ({ latest_commit: "Latest commit", working_tree: "Working tree" })[source] || "Role/stage output";
}
function diffHtml(diff) {
  return String(diff || "").split(/\r?\n/).map((line) => {
    let kind = "context";
    if (line.startsWith("@@")) kind = "hunk";
    else if (line.startsWith("diff ") || line.startsWith("index ") || line.startsWith("--- ") || line.startsWith("+++ ")) kind = "meta";
    else if (line.startsWith("+")) kind = "added";
    else if (line.startsWith("-")) kind = "removed";
    return `<span class="diff-line ${kind}"><span class="diff-gutter" aria-hidden="true"></span><span class="diff-content">${esc(line) || " "}</span></span>`;
  }).join("");
}
function fileChangesMarkup(changes) {
  if (!changes.length) return `<p class="subtle">No file-level changes available.</p>`;
  const grouped = changes.reduce((result, change) => { const label = changeSourceLabel(change.source); result[label] = (result[label] || 0) + 1; return result; }, {});
  const summary = Object.entries(grouped).map(([label, count]) => `<span>${esc(label)} <b>${count}</b></span>`).join("");
  return `<div class="change-summary">${summary}</div><p class="change-summary-note">Files are scoped to this role or stage; a module checkpoint is the reviewed module commit.</p><div class="file-change-list">${changes.map((change, index) => `<div class="file-change" data-change-index="${index}"><button class="file-change-trigger" type="button" aria-expanded="false"><span class="change-status status-${esc(change.status)}" data-status="${esc(change.status)}">${esc(changeStatusLabel(change.status))}</span><span class="file-change-main"><strong title="${esc(change.path)}">${esc(change.path)}</strong>${change.old_path ? `<small>from ${esc(change.old_path)}</small>` : ""}</span><span class="change-source">${esc(changeSourceLabel(change.source))}</span><span class="change-stat"><b>+${Number(change.additions || 0)}</b> <em>-${Number(change.deletions || 0)}</em></span></button><div class="file-change-diff" hidden>${change.diff ? `<pre class="syntax-diff">${diffHtml(change.diff)}</pre>` : `<p class="subtle">No textual diff available for this file.</p>`}</div></div>`).join("")}</div>`;
}
function messageKind(message) {
  const value = String(message?.kind || message?.role || "system").toLowerCase();
  if (value.includes("developer")) return "developer";
  if (value.includes("tool") || value.includes("function")) return "tool";
  if (value.includes("assistant") || value.includes("agent")) return "assistant";
  if (value.includes("user")) return "user";
  return "system";
}
function messageSummary(message) {
  return String(message?.content || "").split(/\r?\n/)[0].trim().replace(/\s+/g, " ").slice(0, 120) || "(empty message)";
}
function conversationOverview(messages) {
  const counts = messages.reduce((result, message) => {
    const kind = messageKind(message); result[kind] = (result[kind] || 0) + 1; return result;
  }, {});
  const legend = ["user", "assistant", "tool", "system", "developer"].filter((kind) => counts[kind]).map((kind) => `<span class="conversation-legend-item"><i class="conversation-node-dot ${kind}"></i>${kind} <b>${counts[kind]}</b></span>`).join("");
  const nodes = messages.map((message, index) => {
    const kind = messageKind(message); const target = `conversation-message-${index}`;
    const label = `${kind} message ${index + 1}${message.timestamp ? ` at ${message.timestamp}` : ""}: ${messageSummary(message)}`;
    return `<button class="conversation-node ${kind}" data-message-target="${target}" aria-label="Jump to ${esc(label)}" title="${esc(label)}"><span class="sr-only">${esc(index + 1)}</span></button>`;
  }).join("");
  return `<div class="conversation-overview"><div class="conversation-overview-head"><h4>Message overview</h4><span>${messages.length} message${messages.length === 1 ? "" : "s"}</span></div><div class="conversation-legend">${legend || `<span class="subtle">No messages</span>`}</div><div class="conversation-timeline" aria-label="Conversation message timeline">${nodes || `<span class="subtle">No messages yet.</span>`}</div></div>`;
}
function conversationMessages(messages) {
  return messages.map((message, index) => {
    const kind = messageKind(message); const id = `conversation-message-${index}`;
    return `<article id="${id}" class="message ${kind}" tabindex="0" role="button" aria-expanded="false"><div class="message-meta"><strong>${esc(message.role || kind)}</strong>${message.name ? ` · ${esc(message.name)}` : ""}<time>${esc(message.timestamp)}</time></div><pre>${esc(message.content)}</pre><span class="message-hint">Click to expand</span></article>`;
  }).join("");
}

async function openCharacter(role, moduleId = "") {
  const item = characterById(role); const session = item.session_id ? sessionById(item.session_id) : null;
  let detail = item;
  if (item.session_id) { try { const query = moduleId ? `?module_id=${encodeURIComponent(moduleId)}` : ""; const response = await fetch(`/api/sessions/${encodeURIComponent(item.session_id)}${query}`); if (response.ok) detail = moduleId ? { ...item, ...(await response.json()), module_id: moduleId } : { ...(await response.json()), ...item }; } catch {} }
  document.querySelector("#drawer-kicker").textContent = `${labels[role]} · ${item.phase || "idle"}`;
  document.querySelector("#drawer-title").textContent = `${labels[role]} detail`;
  const workingDiff = detail.diff || "";
  const commitDiff = detail.commit_diff || "";
  const diffBlock = (title, content, empty) => content ? `<details class="diff-block"><summary>${title}</summary><pre class="syntax-diff">${diffHtml(content)}</pre></details>` : `<p class="subtle">${empty}</p>`;
  const messages = detail.messages || [];
  const fileChanges = detail.file_changes || [];
  const testResults = detail.test_results || (state.data?.test_results || []).filter((result) => !moduleId || result.module_id === moduleId);
  const testSummary = testResults.length ? `<section class="drawer-section"><h3>Test results</h3><div class="test-result-list">${testResults.slice(-6).reverse().map((result) => { const tests = result.tests || []; const passed = tests.filter((test) => ["passed", "pass", "success", "ok"].includes(String(test.status || "").toLowerCase())).length; const failed = tests.filter((test) => ["failed", "fail", "error", "timeout"].includes(String(test.status || "").toLowerCase())).length; const artifacts = Object.entries(result.artifacts || {}).filter(([, value]) => value).map(([name, value]) => `${esc(name)}: ${esc(value)}`).join(" · "); return `<div class="test-result-row"><strong>${esc(result.module_id || moduleId || "ROOT")} · round ${Number(result.round || 0)}</strong><span class="activity-severity ${result.verdict === "pass" ? "passed" : "major"}">${esc(result.verdict || "unknown")}</span><span class="subtle">${passed} passed · ${failed} failed</span><small>${esc(result.result_path || result.artifacts?.report || "")}${artifacts ? ` · ${artifacts}` : ""}</small></div>`; }).join("")}</div></section>` : "";
  document.querySelector("#drawer-body").innerHTML = `${detailRows(detail)}<section class="drawer-section"><h3>Latest event</h3><p>${esc(item.message || "Waiting for work")}</p></section>${testSummary}<section class="drawer-section"><h3>Files changed</h3>${fileChangesMarkup(fileChanges)}${detail.diff_stat ? `<p class="subtle change-stat-summary">${esc(detail.diff_stat)}</p>` : ""}${diffBlock("Working tree diff", workingDiff, "No uncommitted diff")}${diffBlock("Latest commit diff", commitDiff, "No recent commit diff")}</section><section class="drawer-section conversation-section"><h3>Conversation</h3>${messages.length ? `${conversationOverview(messages)}<div class="conversation-list">${conversationMessages(messages)}</div>` : `<p class="subtle">${session ? "Session has no renderable messages yet." : "No session is associated with this worker yet."}</p>`}</section>`;
  document.querySelectorAll("#drawer-body .file-change-trigger").forEach((button) => {
    const toggleChange = () => {
      const row = button.closest(".file-change"); const expanded = !row.classList.contains("expanded");
      document.querySelectorAll("#drawer-body .file-change").forEach((item) => { item.classList.remove("expanded"); item.querySelector(".file-change-diff").hidden = true; item.querySelector(".file-change-trigger").setAttribute("aria-expanded", "false"); });
      if (expanded) { row.classList.add("expanded"); row.querySelector(".file-change-diff").hidden = false; button.setAttribute("aria-expanded", "true"); }
    };
    button.addEventListener("click", toggleChange);
    button.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); toggleChange(); } });
  });
  document.querySelectorAll("#drawer-body .message").forEach((message) => {
    const toggle = () => {
      const expanded = message.classList.toggle("expanded");
      message.setAttribute("aria-expanded", String(expanded));
    };
    message.addEventListener("click", toggle);
    message.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); toggle(); }
    });
  });
  const nodes = document.querySelectorAll("#drawer-body .conversation-node");
  const activateNode = (target) => {
    nodes.forEach((node) => node.classList.toggle("active", node.dataset.messageTarget === target.id));
  };
  nodes.forEach((node) => {
    const jump = () => {
      const target = document.getElementById(node.dataset.messageTarget); if (!target) return;
      target.classList.add("expanded", "conversation-target"); target.setAttribute("aria-expanded", "true"); activateNode(target);
      target.scrollIntoView({ behavior: "smooth", block: "center" });
      window.setTimeout(() => target.classList.remove("conversation-target"), 1500);
    };
    node.addEventListener("click", jump);
    node.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); jump(); } });
  });
  if (window.IntersectionObserver) {
    const observer = new IntersectionObserver((entries) => entries.forEach((entry) => { if (entry.isIntersecting) activateNode(entry.target); }), { root: document.querySelector("#drawer"), threshold: 0.35 });
    document.querySelectorAll("#drawer-body .message").forEach((message) => observer.observe(message));
  }
  document.querySelector("#drawer").classList.add("open"); document.querySelector("#drawer").setAttribute("aria-hidden", "false"); document.querySelector("#backdrop").hidden = false;
}
function closeDrawer() { document.querySelector("#drawer").classList.remove("open"); document.querySelector("#drawer").setAttribute("aria-hidden", "true"); document.querySelector("#backdrop").hidden = true; }
async function refresh() { try { render(await (await fetch("/api/state", { cache: "no-store" })).json()); } catch (error) { document.querySelector("#runner").textContent = `Dashboard error: ${error.message}`; } }
document.querySelector("#close").addEventListener("click", closeDrawer); document.querySelector("#backdrop").addEventListener("click", closeDrawer); refresh(); setInterval(refresh, 1500);
[["chat-module-filter", "module"], ["chat-role-filter", "role"], ["chat-round-filter", "round"], ["chat-kind-filter", "kind"]].forEach(([id, key]) => document.querySelector(`#${id}`)?.addEventListener("change", (event) => { state.chatFilters[key] = event.target.value; renderExecution(); }));
connectChatStream();
