const esc = (value) => String(value ?? "").replace(/[&<>\"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[char]));
const state = { data: null };
const roles = ["architect", "planner", "implementer", "reviewer", "postflight"];
const labels = { architect: "Architect", planner: "Planner", implementer: "Implementer", reviewer: "Reviewer", postflight: "Postflight" };
const icons = { architect: "⌂", planner: "✎", implementer: "⚙", reviewer: "✓", postflight: "◈" };
const stages = ["architecture", "design", "implement", "review", "postflight", "completed"];

function statusText(status) { return ({ working: "Working", success: "Done", failed: "Failed", paused: "Paused", idle: "Waiting" })[status] || status || "Waiting"; }
function characterSvg(role, status) { return `<img class="character-art ${status === "working" ? "working" : ""}" src="/assets/${role}.svg" alt="${labels[role]} character" loading="lazy" />`; }

function renderRoom(data) {
  const characters = data.characters || [];
  document.querySelector("#room").innerHTML = roles.map((role) => {
    const item = characters.find((character) => character.id === role) || { id: role, label: labels[role], status: "idle", message: "Waiting for work" };
    return `<button class="workstation ${esc(item.status)}" data-character="${role}" aria-label="Open ${labels[role]} details"><div class="bubble">${esc(item.message || statusText(item.status))}</div><div class="monitor"><span>${icons[role]}</span><small>${esc(item.phase || "idle")}</small></div><div class="worker">${characterSvg(role, item.status)}<div class="worker-tag"><strong>${labels[role]}</strong><span>${esc(statusText(item.status))}</span></div></div><div class="task-cards">${(item.tasks || []).map((task) => `<span class="task-card" data-module="${esc(task.node_id)}">${esc(task.node_id)}</span>`).join("") || (item.module_id ? `<span class="task-card">${esc(item.module_id)}</span>` : "")}</div></button>`;
  }).join("");
  document.querySelectorAll("[data-character]").forEach((button) => button.addEventListener("click", () => openCharacter(button.dataset.character)));
}

function renderPipeline(data) {
  const active = (data.modules || []).find((item) => item.status === "running")?.phase || (data.runner?.state === "completed" ? "completed" : "architecture");
  document.querySelector("#pipeline").innerHTML = stages.map((stage) => `<div class="pipeline-stage ${stage === active ? "active" : ""}"><span>${stage}</span></div>`).join("");
}

function renderModules(data) {
  const modules = data.modules || [];
  document.querySelector("#module-count").textContent = `${modules.length} modules`;
  document.querySelector("#modules").innerHTML = modules.length ? modules.map((item) => `<button class="module-row" data-module="${esc(item.node_id)}"><span class="module-mark ${esc(item.status)}"></span><span><strong>${esc(item.node_id)}</strong><small>${esc(item.message || item.phase || "")}</small></span><span class="status ${esc(item.status)}">${esc(statusText(item.status))}</span></button>`).join("") : `<p class="subtle">No module events yet.</p>`;
  document.querySelectorAll("[data-module]").forEach((button) => button.addEventListener("click", () => {
    const module = (state.data?.modules || []).find((item) => item.node_id === button.dataset.module);
    const role = (state.data?.characters || []).find((item) => item.phase === module?.phase)?.id || "reviewer";
    openCharacter(role);
  }));
}

function renderEvents(data) {
  const events = (data.events || []).slice(-12).reverse();
  document.querySelector("#events").innerHTML = events.length ? events.map((event) => `<div class="event-row"><time>${esc(event.timestamp || "")}</time><span><strong>${esc(event.type || "event")}</strong> ${esc(event.message || event.reason || event.state || "")}</span></div>`).join("") : `<p class="subtle">No events yet.</p>`;
}

function render(data) {
  state.data = data;
  const runner = data.runner || {};
  const runnerEl = document.querySelector("#runner");
  runnerEl.textContent = statusText(runner.state);
  runnerEl.className = `pill ${runner.state || "unknown"}`;
  document.querySelector("#run-message").textContent = runner.message || "Factory is ready";
  document.querySelector("#updated").textContent = data.generated_at ? `Updated ${new Date(data.generated_at).toLocaleTimeString()}` : "";
  document.querySelector("#session-count").textContent = `${(data.sessions || []).length} Codex sessions`;
  renderRoom(data); renderPipeline(data); renderModules(data); renderEvents(data);
}

function characterById(id) { return (state.data?.characters || []).find((item) => item.id === id) || { id, label: labels[id], status: "idle", phase: "idle", message: "Waiting for work" }; }
function sessionById(id) { return (state.data?.sessions || []).find((item) => item.id === id); }
function detailRows(item) { return `<div class="detail-grid"><span>Status</span><strong class="status ${esc(item.status)}">${esc(statusText(item.status))}</strong><span>Phase</span><strong>${esc(item.phase)}</strong><span>Module</span><strong>${esc(item.module_id || "—")}</strong><span>Workspace</span><strong class="path">${esc(item.workspace || item.worktree?.path || "—")}</strong><span>Branch</span><strong>${esc(item.worktree?.branch || "—")}</strong><span>Changed files</span><strong>${(item.files_changed || []).length}</strong></div>`; }
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

async function openCharacter(role) {
  const item = characterById(role); const session = item.session_id ? sessionById(item.session_id) : null;
  let detail = item;
  if (item.session_id) { try { const response = await fetch(`/api/sessions/${encodeURIComponent(item.session_id)}`); if (response.ok) detail = { ...item, ...(await response.json()) }; } catch {} }
  document.querySelector("#drawer-kicker").textContent = `${labels[role]} · ${item.phase || "idle"}`;
  document.querySelector("#drawer-title").textContent = `${labels[role]} detail`;
  const workingDiff = detail.diff || "";
  const commitDiff = detail.commit_diff || "";
  const diffBlock = (title, content, empty) => content ? `<details class="diff-block"><summary>${title}</summary><pre>${esc(content)}</pre></details>` : `<p class="subtle">${empty}</p>`;
  const messages = detail.messages || [];
  document.querySelector("#drawer-body").innerHTML = `${detailRows(detail)}<section class="drawer-section"><h3>Latest event</h3><p>${esc(item.message || "Waiting for work")}</p></section><section class="drawer-section"><h3>Files changed</h3><pre>${esc((detail.files_changed || []).join("\n") || detail.diff_stat || "No uncommitted file changes")}</pre>${detail.diff_stat ? `<p class="subtle">${esc(detail.diff_stat)}</p>` : ""}${diffBlock("Working tree diff", workingDiff, "No uncommitted diff")}${diffBlock("Latest commit diff", commitDiff, "No recent commit diff")}</section><section class="drawer-section conversation-section"><h3>Conversation</h3>${messages.length ? `${conversationOverview(messages)}<div class="conversation-list">${conversationMessages(messages)}</div>` : `<p class="subtle">${session ? "Session has no renderable messages yet." : "No session is associated with this worker yet."}</p>`}</section>`;
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
