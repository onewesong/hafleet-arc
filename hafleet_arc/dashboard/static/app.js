const esc = (value) => String(value ?? "").replace(/[&<>\"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[char]));
const state = { sessions: [] };
const stages = ["architecture", "design", "implement", "review", "postflight", "completed"];

function render(data) {
  state.sessions = data.sessions || [];
  const runner = data.runner || {};
  const runnerEl = document.querySelector("#runner");
  runnerEl.textContent = runner.state || "unknown";
  runnerEl.className = `pill ${runner.state || "unknown"}`;
  document.querySelector("#updated").textContent = data.generated_at ? new Date(data.generated_at).toLocaleTimeString() : "";
  const modules = data.modules || [];
  const activePhase = modules.find((item) => item.status === "running")?.phase || (runner.state === "completed" ? "completed" : "architecture");
  document.querySelector("#pipeline").innerHTML = stages.map((stage) => `<div class="stage ${stage === activePhase ? "active" : ""}"><span>${stage}</span></div>`).join("");
  document.querySelector("#modules").innerHTML = modules.length ? modules.map((item) => `<button class="row module-row" data-module="${esc(item.node_id)}"><span><strong>${esc(item.node_id)}</strong><small>${esc(item.message || "")}</small></span><span class="status ${esc(item.status)}">${esc(item.status)}</span></button>`).join("") : `<p class="muted">No module events yet.</p>`;
  document.querySelector("#session-count").textContent = `${state.sessions.length} sessions`;
  document.querySelector("#sessions").innerHTML = state.sessions.length ? state.sessions.map((item) => `<button class="row session-row" data-session="${esc(item.id)}"><span><strong>${esc(item.role)}</strong><small>${esc(item.cwd || item.id)}</small></span><span class="status">${esc(item.model || "default")}</span></button>`).join("") : `<p class="muted">No Codex sessions yet.</p>`;
  document.querySelectorAll("[data-session]").forEach((button) => button.addEventListener("click", () => openSession(button.dataset.session)));
}

async function refresh() { try { render(await (await fetch("/api/state", { cache: "no-store" })).json()); } catch (error) { document.querySelector("#runner").textContent = `dashboard error: ${error.message}`; } }
async function openSession(id) { const item = await (await fetch(`/api/sessions/${encodeURIComponent(id)}`)).json(); document.querySelector("#detail-title").textContent = `${item.role} · ${item.id}`; document.querySelector("#detail-body").innerHTML = (item.messages || []).map((message) => `<article class="message ${esc(message.role)}"><div class="message-meta"><strong>${esc(message.role)}</strong>${message.name ? ` · ${esc(message.name)}` : ""}<time>${esc(message.timestamp)}</time></div><pre>${esc(message.content)}</pre></article>`).join("") || `<p class="muted">No renderable messages in this session.</p>`; document.querySelector("#close").hidden = false; }
document.querySelector("#close").addEventListener("click", () => { document.querySelector("#detail-title").textContent = "Select a session"; document.querySelector("#detail-body").textContent = "Click a session to inspect its conversation."; document.querySelector("#close").hidden = true; });
refresh(); setInterval(refresh, 1500);
