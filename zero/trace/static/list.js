const runsEl = document.getElementById("runs");
const countEl = document.getElementById("count");
const refreshBtn = document.getElementById("refresh");

function fmtTime(ts) {
  if (!ts) return "—";
  try {
    return new Date(ts * 1000).toLocaleString();
  } catch {
    return "—";
  }
}

function statusClass(status) {
  const s = String(status || "").toLowerCase();
  if (s.includes("fail") || s.includes("error")) return "fail";
  if (s.includes("complete") || s.includes("success") || s === "task_completed") return "ok";
  if (s.includes("live") || s.includes("running") || s.includes("progress")) return "pending";
  return "neutral";
}

function agentChips(agents) {
  const a = agents || {};
  const bits = [];
  if (a.researcher) bits.push('<span class="agent-pill researcher">R</span>');
  if (a.labwright) bits.push('<span class="agent-pill labwright">L</span>');
  if (a.teacher) bits.push('<span class="agent-pill teacher">T</span>');
  if (a.events) bits.push('<span class="badge neutral">events</span>');
  return bits.join(" ") || '<span class="muted">无 trace</span>';
}

function render(runs) {
  countEl.textContent = runs.length ? `${runs.length} 个 run` : "暂无 run";
  if (!runs.length) {
    runsEl.innerHTML = '<div class="empty">runs/ 下还没有可查看的任务</div>';
    return;
  }
  runsEl.innerHTML = runs.map((r) => {
    const href = "trace?task_id=" + encodeURIComponent(r.task_id);
    const st = statusClass(r.status);
    const backend = r.backend ? `<span class="chip">${escapeHtml(String(r.backend))}</span>` : "";
    return `
      <a class="run-card" href="${href}">
        <div class="run-title">${escapeHtml(r.task_id)}</div>
        <div class="run-meta">
          <span class="badge ${st}">${escapeHtml(String(r.status || "unknown"))}</span>
          ${backend}
          <span class="chip">${fmtTime(r.mtime)}</span>
          <span class="ml-auto run-agents">${agentChips(r.agents)}</span>
        </div>
      </a>`;
  }).join("");
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

async function load() {
  try {
    const j = await (await fetch("api/runs")).json();
    render(j.runs || []);
  } catch (e) {
    countEl.textContent = "加载失败";
    runsEl.innerHTML = '<div class="empty">无法读取 /api/runs</div>';
  }
}

refreshBtn.addEventListener("click", load);
load();
setInterval(load, 5000);
