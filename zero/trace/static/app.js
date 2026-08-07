/* 0号机 Trace Viewer — Segment Inspector.
 *
 * SSE events:
 *   { t:"orch",  agent, event, detail, ts }
 *   { t:"model", agent, ts, index, input, turn_input, output }  // compact deltas
 *   (legacy) also accepts full input.messages without turn_input
 *
 * Left spine = handoff/work/lifecycle segments; right = selected segment detail.
 */

// ---------- markdown / dom ----------
marked.setOptions({ gfm: true, breaks: true });
function mdEl(text) {
  const d = document.createElement("div");
  d.className = "md";
  d.innerHTML = DOMPurify.sanitize(marked.parse(text || ""));
  return d;
}
function el(tag, cls, txt) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (txt != null) e.textContent = txt;
  return e;
}
function pre(text, cls) {
  return el("pre", "io" + (cls ? " " + cls : ""), text);
}
function jsonStr(v) {
  return typeof v === "string" ? v : JSON.stringify(v, null, 2);
}
function fmtTime(ts) {
  if (ts == null) return "";
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString("zh-CN", { hour12: false }) + "." +
    String(d.getMilliseconds()).padStart(3, "0");
}

const spineEl = document.getElementById("spine");
const detailEl = document.getElementById("detail");
const statusEl = document.getElementById("status");
const taskEl = document.getElementById("task");
const followBtn = document.getElementById("follow");
const rc = document.getElementById("rc");
const lc = document.getElementById("lc");
const tc = document.getElementById("tc");
const roleFilterEl = document.getElementById("role-filter");
const expandAllBtn = document.getElementById("expand-all");
const collapseAllBtn = document.getElementById("collapse-all");
const jumpBottomBtn = document.getElementById("jump-bottom");
const detailBottomBtn = document.getElementById("detail-bottom");
let activeAgentFilter = localStorage.getItem("zeroTraceAgentFilter") || "all";

function setStatus(text, kind) {
  statusEl.className = "status" + (kind ? " " + kind : "");
  statusEl.innerHTML = (kind === "done" || kind === "failed") ? "" : '<span class="live-dot"></span>';
  statusEl.appendChild(document.createTextNode(text));
}

function updateIdleHeaders() {
  document.querySelectorAll(".colhead [data-agent]").forEach((node) => {
    const a = node.dataset.agent;
    const n = a === "labwright" ? LAB : a === "teacher" ? TCH : RES;
    node.classList.toggle("idle", n === 0);
  });
}

(function initViewToggle() {
  // New key intentionally resets the old rendered/raw/compact preference:
  // chain is the default information architecture for the redesigned viewer.
  const saved = localStorage.getItem("zeroTraceViewV2") || "chain";
  document.body.dataset.view = saved;
  document.querySelectorAll("#seg button").forEach((b) => {
    if (b.dataset.v === saved) b.classList.add("active");
    b.onclick = () => {
      document.body.dataset.view = b.dataset.v;
      localStorage.setItem("zeroTraceViewV2", b.dataset.v);
      document.querySelectorAll("#seg button").forEach((x) => x.classList.toggle("active", x === b));
      // Compact changes which parts of a card are visible and opens the
      // conversation body, so rebuild the selected detail immediately.
      lastDetailSig = "";
      renderDetail();
      lastDetailSig = detailSig(segments.find((s) => s.id === selectedId));
    };
  });
})();

function setAllDetails(open) {
  detailEl.querySelectorAll("details").forEach((det) => {
    det.open = open;
  });
  if (open) {
    detailEl.querySelectorAll("details").forEach((det) => {
      const key = det.dataset.openKey;
      if (key) {
        openKeys.add(key);
        closedKeys.delete(key);
      }
    });
  } else {
    detailEl.querySelectorAll("details").forEach((det) => {
      const key = det.dataset.openKey;
      if (key) {
        openKeys.delete(key);
        closedKeys.add(key);
      }
    });
  }
  persistUi();
}

expandAllBtn.onclick = () => setAllDetails(true);
collapseAllBtn.onclick = () => setAllDetails(false);

function scrollDetailBottom() {
  detailEl.scrollTop = detailEl.scrollHeight;
}

function jumpToBottom() {
  const visible = visibleSegments();
  stickLatest = true;
  followBtn.style.display = "none";
  if (visible.length) {
    selectedId = visible[visible.length - 1].id;
    lastDetailSig = "";
    renderSpine();
    renderDetail();
    lastDetailSig = detailSig(segments.find((s) => s.id === selectedId));
    persistUi();
  }
  spineEl.scrollTop = spineEl.scrollHeight;
  // Detail content may reflow after render; scroll again on next frames.
  scrollDetailBottom();
  requestAnimationFrame(() => {
    scrollDetailBottom();
    setTimeout(scrollDetailBottom, 50);
  });
}

jumpBottomBtn.onclick = jumpToBottom;
detailBottomBtn.onclick = () => {
  scrollDetailBottom();
  requestAnimationFrame(scrollDetailBottom);
};

(function initRoleFilter() {
  function update() {
    roleFilterEl.querySelectorAll("button").forEach((b) => {
      b.classList.toggle("active", b.dataset.agent === activeAgentFilter);
    });
  }
  update();
  roleFilterEl.querySelectorAll("button").forEach((b) => {
    b.onclick = () => {
      activeAgentFilter = b.dataset.agent;
      localStorage.setItem("zeroTraceAgentFilter", activeAgentFilter);
      stickLatest = false;
      const visible = visibleSegments();
      if (!visible.some((s) => s.id === selectedId)) selectedId = visible[0]?.id || null;
      followBtn.style.display = "block";
      lastDetailSig = "";
      update();
      renderSpine();
      renderDetail();
      persistUi();
    };
  });
})();

// ---------- taxonomy ----------
const LIFECYCLE = new Set([
  "task_received", "researcher_started", "task_completed", "task_failed",
  "hook_intercept", "run_exported", "teacher_stats",
  "teacher_preflight_started", "teacher_preflight_finished", "teacher_preflight_failed",
  "external_deliverable_validation_failed",
]);
const ENTER = new Set([
  "ensure_environment", "add_resources", "issue_reported", "decision_resolved", "ask_received",
]);
const RETURN = new Set([
  "manifest_published", "needs_decision", "failed", "agent_turn_end",
  "answered", "ask_budget_exhausted",
]);
const DROP = new Set(["agent_turn_start"]);

const AGENT_LABEL = {
  researcher: "Researcher", labwright: "Labwright", teacher: "Teacher", orchestrator: "Orchestrator",
};
const STATUS_LABEL = {
  sandbox_created: "Sandbox 已创建", sandbox_exec: "Sandbox 执行",
  collect_ambiguous: "资源候选歧义", resource_collected: "资源已搜集",
  resource_mounted: "资源已挂载", skill_candidate_proposed: "Skill 候选",
  hint_bank_read: "已读 hint bank", hint_given: "给出 HINT",
  hint_bank_seeded: "Hint bank 已种入",
  task_amended: "订正题面", declined: "拒答 NO_HELP",
  grader_amended: "订正 grader", both_amended: "题面+grader 同改",
  package_applied: "Live 题包已更新", package_apply_rejected: "题包订正被拒（lint）",
  preflight_started: "Preflight 开始", preflight_finished: "Preflight 结束",
  preflight_error: "Preflight 出错",
  completion_review_started: "结题审阅开始",
  completion_review_finished: "结题审阅结束",
  completion_review_error: "结题审阅出错",
  completion_apply: "结题订正已写入 live 包",
  review_no_change: "结题：题包无需改动",
  task_addendum_written: "题面订正已落盘", agent_error: "Agent 错误",
  external_task_preparation_started: "外部任务准备中",
  external_task_prepared: "外部任务已准备",
  external_task_preparation_failed: "外部任务准备失败",
  external_deliverable_validation_started: "交付物校验中",
  external_deliverables_validated: "交付物已校验",
  external_deliverable_validation_failed: "交付物校验失败",
};

function calleeOfEnter(event) {
  return event === "ask_received" ? "teacher" : "labwright";
}

function outcomeOfReturn(ev) {
  const d = ev.detail || {};
  const event = ev.event;
  if (event === "needs_decision") return { label: "NEEDS_DECISION", tone: "decision" };
  if (event === "failed") return { label: "FAILED", tone: "fail" };
  if (event === "manifest_published") return { label: "READY", tone: "ok" };
  if (event === "agent_turn_end") {
    const st = (d.status || "TURN_END").toUpperCase();
    if (/READY|ADDED|SUCCESS/i.test(st)) return { label: st, tone: "ok" };
    if (/FAIL|ERROR/i.test(st)) return { label: st, tone: "fail" };
    if (/DECISION|NEEDS/i.test(st)) return { label: st, tone: "decision" };
    return { label: st, tone: "neutral" };
  }
  if (event === "answered") {
    const k = (d.kind || "ANSWER").toUpperCase();
    if (k === "HINT" || k === "TASK_AMENDMENT") return { label: k, tone: "ok" };
    if (k === "GRADER_AMENDMENT" || k === "BOTH_AMENDMENT") return { label: k, tone: "ok" };
    if (k === "NO_HELP") return { label: k, tone: "decision" };
    return { label: k, tone: "neutral" };
  }
  if (event === "ask_budget_exhausted") return { label: "NO_HELP (budget)", tone: "fail" };
  return { label: event, tone: "neutral" };
}

function enterVerb(event) {
  if (event === "issue_reported") return "report_environment_issue";
  if (event === "decision_resolved") return "resolve_environment_decision";
  if (event === "ask_received") return "ask_teacher";
  return event;
}

function lifecycleLabel(ev) {
  const d = ev.detail || {};
  if (ev.event === "task_completed") return "任务完成";
  if (ev.event === "task_failed") return "任务失败";
  if (ev.event === "hook_intercept") return "拦截: " + (d.command || "");
  if (ev.event === "researcher_started") return "Researcher 开始";
  if (ev.event === "task_received") return "收到任务";
  if (ev.event === "run_exported") return "已导出";
  if (ev.event === "teacher_stats") return "Teacher 统计";
  if (ev.event === "teacher_preflight_started") return "Teacher Preflight 开始";
  if (ev.event === "teacher_preflight_finished") {
    const rev = d.revision != null ? ` · r${String(d.revision).padStart(3, "0")}` : "";
    const ok = d.ok === false ? "（未完全通过）" : "";
    return "Teacher Preflight 结束" + rev + ok;
  }
  if (ev.event === "teacher_preflight_failed") return "Teacher Preflight 失败";
  return ev.event;
}

// ---------- segmenter ----------
let segments = [];
let openHandoff = null;
let openWork = null;
let nextId = 1;
let previousMessagesByAgent = {};

function sameMessage(a, b) {
  return JSON.stringify(a) === JSON.stringify(b);
}

function withoutInjectedContext(message) {
  if (!message || message.role !== "user") return message;
  const blocks = (message.blocks || []).filter((block) => {
    const text = String(block.text || "").trim();
    // Claude Code injects these as ordinary user text in some SDK paths
    // (not always in a <system-reminder> wrapper). They describe the harness,
    // not the task input the agent is responding to.
    const injected = [
      "<system-reminder>",
      "Available agent types for the Agent tool:",
      "The following skills are available for use with the Skill tool:",
      "As you answer the user's questions, you can use the following context:",
      "IMPORTANT: this context may or may not be relevant to your task.",
    ];
    return !injected.some((prefix) => text.startsWith(prefix));
  });
  return Object.assign({}, message, { blocks });
}

function incrementalInput(agent, messages) {
  const previous = previousMessagesByAgent[agent] || [];
  let common = 0;
  while (common < previous.length && common < messages.length &&
         sameMessage(previous[common], messages[common])) {
    common += 1;
  }
  previousMessagesByAgent[agent] = messages;

  // A request repeats the whole conversation. Only appended user/tool-result
  // messages are the actual new input for this model turn; prior assistant
  // messages are rendered as the preceding turn's output.
  let delta = messages.slice(common).filter((message) => message.role !== "assistant");
  if (!previous.length) delta = delta.map(withoutInjectedContext)
    .filter((message) => (message.blocks || []).length);
  return delta;
}

function newSeg(partial) {
  const s = Object.assign({
    id: nextId++,
    kind: "work",
    agent: "researcher",
    title: "",
    tsStart: null,
    tsEnd: null,
    models: [],
    statuses: [],
    enter: null,
    returns: [],
    outcome: null,       // {label, tone}
    pending: false,
    lifecycle: null,
    participants: [],
    fromAgent: null,
    toAgent: null,
  }, partial);
  segments.push(s);
  return s;
}

function closeWork() { openWork = null; }

function closeHandoff(pending) {
  if (!openHandoff) return;
  if (pending && !openHandoff.outcome) {
    openHandoff.pending = true;
    openHandoff.outcome = { label: "进行中", tone: "pending" };
  }
  openHandoff = null;
}

function absorbIntoRecentHandoff(agent, ts) {
  // Live SSE can deliver agent_turn_end slightly ahead of the callee's last
  // capgw model line. Fold that trailing call back into the handoff instead of
  // inventing a dangling "Labwright · 1 次调用" work segment.
  const prev = segments.length ? segments[segments.length - 1] : null;
  if (!prev || prev.kind !== "handoff" || prev.agent !== agent) return null;
  // Model may have an earlier wall-clock ts than the already-applied turn_end.
  if (ts != null && prev.tsStart != null && ts + 1 < prev.tsStart) return null;
  if (ts != null && prev.tsEnd != null && ts - prev.tsEnd > 30) return null;
  return prev;
}

function ensureWork(agent, ts) {
  // Callee models stay inside the open handoff; anyone else ends it.
  if (openHandoff) {
    if (openHandoff.agent === agent) return openHandoff;
    closeHandoff(false);
  }
  const absorbed = absorbIntoRecentHandoff(agent, ts);
  if (absorbed) return absorbed;
  if (openWork && openWork.agent === agent) return openWork;
  closeWork();
  openWork = newSeg({
    kind: "work",
    agent,
    participants: [agent],
    fromAgent: agent,
    toAgent: agent,
    title: AGENT_LABEL[agent] || agent,
    tsStart: ts,
    tsEnd: ts,
  });
  return openWork;
}

function bumpSegTime(seg, ts) {
  if (ts == null) return;
  if (seg.tsStart == null || ts < seg.tsStart) seg.tsStart = ts;
  if (seg.tsEnd == null || ts > seg.tsEnd) seg.tsEnd = ts;
}

function pushOrch(ev) {
  const event = ev.event || "";
  if (DROP.has(event) || event.startsWith("turn:")) return;

  if (LIFECYCLE.has(event) || ev.agent === "orchestrator") {
    closeWork();
    closeHandoff(false);
    newSeg({
      kind: "lifecycle",
      agent: "orchestrator",
      title: lifecycleLabel(ev),
      tsStart: ev.ts, tsEnd: ev.ts,
      lifecycle: ev,
    });
    if (event === "task_completed") setStatus("已完成", "done");
    if (event === "task_failed") setStatus("失败", "failed");
    return;
  }

  if (ENTER.has(event)) {
    closeWork();
    closeHandoff(true);
    const agent = calleeOfEnter(event);
    const from = "Researcher";
    const to = AGENT_LABEL[agent];
    openHandoff = newSeg({
      kind: "handoff",
      agent,
      participants: ["researcher", agent],
      fromAgent: "researcher",
      toAgent: agent,
      title: from + " → " + to + " · " + enterVerb(event),
      tsStart: ev.ts, tsEnd: ev.ts,
      enter: ev,
      pending: true,
      outcome: { label: "进行中", tone: "pending" },
    });
    return;
  }

  if (RETURN.has(event)) {
    const out = outcomeOfReturn(ev);
    if (openHandoff) {
      openHandoff.returns.push(ev);
      bumpSegTime(openHandoff, ev.ts);
      if (openHandoff.pending || !openHandoff.outcome || openHandoff.outcome.tone === "pending") {
        openHandoff.outcome = out;
        openHandoff.pending = false;
      } else if (event === "agent_turn_end") {
        openHandoff.outcome = out;
      }
      // Labwright emits soft return (READY/NEEDS_DECISION/FAILED) then agent_turn_end.
      // Teacher closes on answered. Keep the segment open across the soft return so
      // trailing callee models still land inside the handoff.
      const hardClose = event === "agent_turn_end" || event === "answered" ||
        event === "ask_budget_exhausted";
      if (hardClose) closeHandoff(false);
    } else {
      // Merge stray agent_turn_end into the previous handoff when possible.
      const prev = segments.length ? segments[segments.length - 1] : null;
      if (event === "agent_turn_end" && prev && prev.kind === "handoff") {
        prev.returns.push(ev);
        bumpSegTime(prev, ev.ts);
        if (!prev.outcome || prev.outcome.tone === "pending") prev.outcome = out;
        prev.pending = false;
      } else {
        const agent = ev.agent === "teacher" ? "teacher" : "labwright";
        newSeg({
          kind: "handoff",
          agent,
          participants: [agent, "researcher"],
          fromAgent: agent,
          toAgent: "researcher",
          title: (AGENT_LABEL[agent] || agent) + " → Researcher · " + out.label,
          tsStart: ev.ts, tsEnd: ev.ts,
          returns: [ev],
          outcome: out,
          pending: false,
        });
      }
    }
    return;
  }

  // status chip
  if (ev.agent === "labwright" || ev.agent === "teacher" || ev.agent === "researcher") {
    const seg = ensureWork(ev.agent, ev.ts);
    seg.statuses.push(ev);
    bumpSegTime(seg, ev.ts);
  }
}

function pushModel(ev) {
  const agent = ev.agent || "researcher";
  if (agent === "labwright") lc.textContent = ++LAB;
  else if (agent === "teacher") tc.textContent = ++TCH;
  else rc.textContent = ++RES;
  updateIdleHeaders();

  // Prefer server-computed delta; fall back to client diff for legacy streams.
  if (Array.isArray(ev.turn_input)) {
    ev.turnInput = ev.turn_input;
    if (ev.input && Array.isArray(ev.input.messages)) {
      previousMessagesByAgent[agent] = ev.input.messages;
    }
  } else {
    ev.turnInput = incrementalInput(agent, (ev.input && ev.input.messages) || []);
  }
  // Full-view input panel shows this turn's delta (history is in earlier turns).
  if (ev.input && !Array.isArray(ev.input.messages)) {
    ev.input = Object.assign({}, ev.input, { messages: ev.turnInput || [] });
  } else if (!ev.input) {
    ev.input = { num_messages: 0, num_tools: 0, messages: ev.turnInput || [] };
  }
  const seg = ensureWork(agent, ev.ts);
  seg.models.push(ev);
  bumpSegTime(seg, ev.ts);
}

function pushEvent(ev) {
  if (ev.t === "orch") pushOrch(ev);
  else if (ev.t === "model") pushModel(ev);
}

// ---------- spine / detail render ----------
let selectedId = null;
let stickLatest = true;
let lastDetailSig = "";
/** Keys of <details> the user opened — survive live re-renders + refresh. */
const openKeys = new Set();
/** Explicitly closed keys — distinguish "default open" from "user collapsed". */
const closedKeys = new Set();
/** Per-segment model window {from,to}, keyed by segKey — survives refresh. */
const modelWindows = new Map();

function bindOpen(det, key, opts) {
  if (!key) return det;
  const defaultOpen = !!(opts && opts.defaultOpen);
  det.dataset.openKey = key;
  if (closedKeys.has(key)) det.open = false;
  else if (openKeys.has(key)) det.open = true;
  else det.open = defaultOpen;
  det.addEventListener("toggle", () => {
    if (det.open) {
      openKeys.add(key);
      closedKeys.delete(key);
    } else {
      openKeys.delete(key);
      closedKeys.add(key);
    }
    persistUi();
  });
  return det;
}

function segKey(seg) {
  return [seg.kind, seg.agent, seg.title, String(seg.tsStart || 0)].join("|");
}

function persistUi() {
  if (!curTask) return;
  try {
    // Snapshot model windows from live segments.
    segments.forEach((s) => {
      if (s._modelWindow) modelWindows.set(segKey(s), { ...s._modelWindow });
    });
    const payload = {
      stickLatest,
      segKey: (() => {
        const s = segments.find((x) => x.id === selectedId);
        return s ? segKey(s) : null;
      })(),
      openKeys: Array.from(openKeys),
      closedKeys: Array.from(closedKeys),
      modelWindows: Array.from(modelWindows.entries()),
      view: document.body.dataset.view || "chain",
    };
    sessionStorage.setItem("zeroTrace:" + curTask, JSON.stringify(payload));
    // Mirror to localStorage so a hard refresh / new tab still remembers.
    localStorage.setItem("zeroTrace:" + curTask, JSON.stringify(payload));
  } catch (e) { /* quota / private mode */ }
}

function restoreUi() {
  if (!curTask) return;
  try {
    const raw = sessionStorage.getItem("zeroTrace:" + curTask)
      || localStorage.getItem("zeroTrace:" + curTask);
    if (!raw) return;
    const st = JSON.parse(raw);
    if (typeof st.stickLatest === "boolean") stickLatest = st.stickLatest;
    if (Array.isArray(st.openKeys)) {
      openKeys.clear();
      st.openKeys.forEach((k) => openKeys.add(k));
    }
    if (Array.isArray(st.closedKeys)) {
      closedKeys.clear();
      st.closedKeys.forEach((k) => closedKeys.add(k));
    }
    if (Array.isArray(st.modelWindows)) {
      modelWindows.clear();
      st.modelWindows.forEach((pair) => {
        if (Array.isArray(pair) && pair.length === 2 && pair[1]) {
          modelWindows.set(pair[0], pair[1]);
        }
      });
    }
    if (st.view && !localStorage.getItem("zeroTraceViewV2")) {
      // Prefer explicit view toggle storage; only fall back here.
    }
    if (st.segKey) {
      const hit = segments.find((s) => segKey(s) === st.segKey);
      if (hit) {
        selectedId = hit.id;
        const win = modelWindows.get(st.segKey);
        if (win) hit._modelWindow = { ...win };
      }
    }
  } catch (e) { /* ignore */ }
  followBtn.style.display = stickLatest ? "none" : "block";
}

function spineTitle(seg) {
  if (seg.kind === "lifecycle") return seg.title;
  if (seg.kind === "handoff") return seg.title;
  const n = seg.models.length;
  return (AGENT_LABEL[seg.agent] || seg.agent) + " · " + n + " 次调用";
}

function detailSig(seg) {
  if (!seg) return "";
  return [
    seg.id, seg.models.length, seg.statuses.length,
    seg.outcome && seg.outcome.label, seg.pending ? 1 : 0,
  ].join(":");
}

function segmentMatchesFilter(seg) {
  if (activeAgentFilter === "all") return true;
  if (seg.kind === "lifecycle") return false;
  // This is intentionally strict: "Researcher" means only the Researcher's
  // own model/work trace, not every handoff it initiated to another role.
  return seg.agent === activeAgentFilter;
}

function visibleSegments() {
  return segments.filter(segmentMatchesFilter);
}

function renderSpine() {
  const prevScroll = spineEl.scrollTop;
  spineEl.innerHTML = "";
  const visible = visibleSegments();
  if (!visible.length) {
    const label = activeAgentFilter === "all"
      ? "等待事件…"
      : `暂无 ${AGENT_LABEL[activeAgentFilter]} 记录`;
    spineEl.appendChild(el("div", "empty sm", label));
    return;
  }
  if (!segments.length) {
    spineEl.appendChild(el("div", "empty sm", "等待事件…"));
    return;
  }
  visible.forEach((seg) => {
    const btn = el("button",
      "spine-item kind-" + seg.kind + " agent-" + seg.agent + (seg.id === selectedId ? " active" : ""));
    btn.type = "button";
    btn.appendChild(el("div", "spine-title", spineTitle(seg)));
    const sub = el("div", "spine-sub");
    if (seg.tsStart != null) sub.appendChild(el("span", null, fmtTime(seg.tsStart)));
    if (seg.kind === "handoff" && seg.outcome) {
      sub.appendChild(el("span", "badge " + seg.outcome.tone, seg.outcome.label));
    } else if (seg.kind === "work") {
      if (seg.statuses.length) sub.appendChild(el("span", null, seg.statuses.length + " 状态"));
    } else if (seg.kind === "lifecycle") {
      sub.appendChild(el("span", "badge neutral", "生命周期"));
    }
    btn.appendChild(sub);
    btn.onclick = () => {
      stickLatest = false;
      followBtn.style.display = "block";
      selectSegment(seg.id);
      persistUi();
    };
    spineEl.appendChild(btn);
  });
  if (stickLatest) spineEl.scrollTop = spineEl.scrollHeight;
  else spineEl.scrollTop = prevScroll;
}

function thinkFold(text, key) {
  const det = document.createElement("details");
  det.className = "think-fold";
  const sum = document.createElement("summary");
  const n = (text || "").length;
  sum.textContent = "思维链" + (n ? " · " + n + " 字" : "");
  det.appendChild(sum);
  det.appendChild(mdEl(text));
  return bindOpen(det, key);
}

function inputBlock(bk) {
  const t = bk.type;
  const b = el("div", "blk" + (t === "thinking" ? " think" : t === "tool_use" ? " tool" : t === "tool_result" ? " tr" : ""));
  b.appendChild(el("div", "blk-sub", t + (bk.is_error ? " · error" : "")));
  if (t === "text" || t === "thinking") b.appendChild(mdEl(bk.text));
  else b.appendChild(pre(bk.text || ""));
  return b;
}

function parseToolInput(call) {
  let input = call.input;
  if (typeof input === "string") {
    try { input = JSON.parse(input); } catch (e) { /* preserve plain input */ }
  }
  return input;
}

function toolTarget(input) {
  if (!input || typeof input !== "object") return "";
  return String(
    input.file_path || input.path || input.command || input.question ||
    input.sandbox_id || input.uri || input.url || input.pattern || ""
  );
}

function toolBody(input) {
  if (!input || typeof input !== "object") {
    return typeof input === "string" ? input : "";
  }
  if (typeof input.content === "string") return input.content;
  if (typeof input.new_string === "string") {
    const parts = [];
    if (input.old_string) parts.push("<<< old >>>\n" + input.old_string);
    parts.push("<<< new >>>\n" + input.new_string);
    return parts.join("\n\n");
  }
  if (typeof input.command === "string") return input.command;
  if (typeof input.prompt === "string") return input.prompt;
  return "";
}

function renderChainAction(call) {
  const wrap = el("div", "chain-action");
  const input = parseToolInput(call);
  const name = call.name || "tool";
  const target = toolTarget(input);
  const body = toolBody(input);

  const head = el("div", "chain-action-head", target ? `${name} · ${target}` : name);
  wrap.appendChild(head);

  if (body && body !== target) {
    wrap.appendChild(pre(body));
  } else if (input && typeof input === "object") {
    const rest = Object.assign({}, input);
    delete rest.file_path;
    delete rest.path;
    delete rest.content;
    delete rest.old_string;
    delete rest.new_string;
    delete rest.command;
    if (Object.keys(rest).length) wrap.appendChild(pre(jsonStr(rest)));
  } else if (typeof input === "string" && input && input !== target) {
    wrap.appendChild(pre(input));
  }
  return wrap;
}

function renderChainInput(message) {
  const wrap = el("div", "chain-input");
  const role = message.role === "user" ? "输入" : (message.role || "消息");
  wrap.appendChild(el("div", "chain-label", role));
  const blocks = message.blocks || [];
  if (!blocks.length) {
    wrap.appendChild(el("div", "muted", "（无可展示的新增输入）"));
    return wrap;
  }
  blocks.forEach((block) => {
    if (block.type === "thinking") return;
    if (block.type === "text") {
      wrap.appendChild(mdEl(block.text));
    } else if (block.type === "tool_result") {
      const result = el("div", "chain-tool-result");
      result.appendChild(el("div", "chain-label", block.is_error ? "工具结果 · error" : "工具结果"));
      result.appendChild(pre(block.text, block.is_error ? "err" : ""));
      wrap.appendChild(result);
    } else {
      wrap.appendChild(pre(block.text));
    }
  });
  return wrap;
}

function renderChainTurn(ev) {
  const out = ev.output || {};
  const agent = ev.agent || "researcher";
  const card = el("article", "chain-turn agent-" + agent);
  const head = el("div", "chain-head");
  head.appendChild(el("span", "agent-pill " + agent, AGENT_LABEL[agent] || agent));
  head.appendChild(el("span", "chip", fmtTime(ev.ts)));
  head.appendChild(el("span", "tag", "第 " + ((ev.index || 0) + 1) + " 轮"));
  const tools = out.tool_calls || [];
  if (out.error) head.appendChild(el("span", "badge err ml-auto", "错误"));
  else if (tools.length) {
    head.appendChild(el("span", "badge ok-soft ml-auto",
      tools.map((t) => t.name || "tool").slice(0, 2).join(", ")));
  }
  card.appendChild(head);

  const inputs = ev.turnInput || [];
  const inputSec = el("section", "chain-section");
  inputSec.appendChild(el("div", "chain-section-title", "输入"));
  if (inputs.length) inputs.forEach((message) => inputSec.appendChild(renderChainInput(message)));
  else inputSec.appendChild(el("div", "muted", "继续上一轮任务（没有新的用户/工具输入）"));
  card.appendChild(inputSec);

  const outputSec = el("section", "chain-section");
  outputSec.appendChild(el("div", "chain-section-title", "回答"));
  if (out.error) outputSec.appendChild(pre(jsonStr(out.error), "err"));
  else if (out.text) outputSec.appendChild(mdEl(out.text));
  else outputSec.appendChild(el("div", "muted", "（本轮没有回答正文）"));
  card.appendChild(outputSec);

  if (tools.length) {
    const actionSec = el("section", "chain-section");
    actionSec.appendChild(el("div", "chain-section-title", "动作"));
    tools.forEach((call) => actionSec.appendChild(renderChainAction(call)));
    card.appendChild(actionSec);
  }
  return card;
}

function renderModelCard(ev) {
  const out = ev.output || {}, inp = ev.input || {};
  const agent = ev.agent || "researcher";
  const base = "m:" + agent + ":" + (ev.index || 0);
  const det = document.createElement("details");
  det.className = "model agent-" + agent;

  const sum = document.createElement("summary");
  sum.appendChild(el("span", "agent-pill " + agent, AGENT_LABEL[agent] || agent));
  sum.appendChild(el("span", "chip", fmtTime(ev.ts)));
  sum.appendChild(el("span", "tag", "模型调用 #" + ((ev.index || 0) + 1)));
  const tools = out.tool_calls || [];
  if (out.error) sum.appendChild(el("span", "badge err ml-auto", "错误"));
  else if (tools.length) {
    const names = tools.map((t) => t.name || "tool").slice(0, 2).join(", ");
    sum.appendChild(el("span", "badge ok-soft ml-auto", names));
  } else sum.appendChild(el("span", "badge ok-soft ml-auto", "回复"));
  det.appendChild(sum);

  const body = el("div", "body");
  const pretty = el("div", "out-pretty");
  pretty.appendChild(el("div", "sec-label", "输出 · 渲染"));
  if (out.error) pretty.appendChild(pre(jsonStr(out.error), "err"));
  else {
    let any = false;
    if (out.reasoning) { any = true; pretty.appendChild(thinkFold(out.reasoning, base + ":think")); }
    if (out.text) {
      any = true;
      const b = el("div", "blk");
      b.appendChild(el("div", "blk-tag", "回复"));
      b.appendChild(mdEl(out.text));
      pretty.appendChild(b);
    }
    tools.forEach((tc) => {
      any = true;
      const b = el("div", "blk tool");
      b.appendChild(el("div", "blk-tag", "🔧 " + (tc.name || "tool")));
      b.appendChild(pre(jsonStr(tc.input)));
      pretty.appendChild(b);
    });
    if (!any) pretty.appendChild(el("div", "muted", "(空响应)"));
  }
  body.appendChild(pretty);

  const raw = el("div", "out-raw");
  raw.appendChild(el("div", "sec-label", "输出 · 原始 JSON"));
  // Lean SSE drops duplicate ``raw``; fall back to structured output fields.
  const data = out.error ? out.error : (out.raw != null ? out.raw : {
    reasoning: out.reasoning, text: out.text, tool_calls: out.tool_calls,
    stop_reason: out.stop_reason,
  });
  raw.appendChild(pre(jsonStr(data), out.error ? "err" : ""));
  body.appendChild(raw);

  const inpDet = document.createElement("details");
  inpDet.className = "input";
  const inpSum = document.createElement("summary");
  const deltaMsgs = inp.messages || ev.turnInput || [];
  inpSum.textContent = "本轮输入 · " + deltaMsgs.length + " 条"
    + (inp.num_messages ? "（请求共 " + inp.num_messages + " 消息）" : "")
    + (inp.num_tools ? " · " + inp.num_tools + " 工具" : "");
  inpDet.appendChild(inpSum);
  deltaMsgs.forEach((m) => {
    const mb = el("div", "msg");
    mb.appendChild(el("div", "blk-tag", "▸ " + (m.role || "")));
    (m.blocks || []).forEach((bk) => mb.appendChild(inputBlock(bk)));
    inpDet.appendChild(mb);
  });
  if (inp.system) {
    const sd = document.createElement("details");
    sd.className = "sys";
    const ss = document.createElement("summary");
    ss.textContent = "system 提示 (" + inp.system.length + " 字)";
    sd.appendChild(ss);
    sd.appendChild(el("div", "body-text", inp.system));
    inpDet.appendChild(bindOpen(sd, base + ":sys"));
  }
  body.appendChild(bindOpen(inpDet, base + ":in"));
  det.appendChild(body);
  return bindOpen(det, base);
}

function renderStatusChip(ev, idx) {
  const agent = ev.agent || "researcher";
  const card = el("div", "status-chip agent-" + agent);
  const meta = el("div", "meta");
  meta.appendChild(el("span", "chip", fmtTime(ev.ts)));
  meta.appendChild(el("span", "tag", STATUS_LABEL[ev.event] || ev.event));
  card.appendChild(meta);
  const d = ev.detail || {};
  if (Object.keys(d).length) {
    const det = document.createElement("details");
    det.className = "status-detail";
    const sum = document.createElement("summary");
    sum.textContent = "详情";
    det.appendChild(sum);
    det.appendChild(pre(jsonStr(d)));
    card.appendChild(bindOpen(det, "st:" + agent + ":" + (ev.event || "") + ":" + idx));
  }
  return card;
}

function compactEventDetail(ev) {
  const d = ev?.detail || {};
  for (const key of [
    "package_delta", "researcher_delta", "question", "message", "reason",
    "scientific_impact", "summary",
  ]) {
    if (typeof d[key] === "string" && d[key].trim()) return d[key].trim();
  }
  if (d.revision != null && (ev.event || "").includes("package")) {
    return `package r${String(d.revision).padStart(3, "0")}`;
  }
  return "";
}

function clearMoreObservers() {
  (_moreObservers || []).forEach((obs) => obs.disconnect());
  _moreObservers = [];
}

function moreButton(label, hint) {
  const btn = el("button", "more-btn");
  btn.type = "button";
  btn.appendChild(el("span", null, label));
  if (hint) btn.appendChild(el("span", "more-hint", hint));
  return btn;
}

function unwatchMore(el) {
  if (!el || !el._moreObs) return;
  el._moreObs.disconnect();
  el._moreObs = null;
}

/** Auto-expand when the sentinel approaches the detail viewport. */
function watchNear(el, onNear) {
  if (!el || typeof IntersectionObserver !== "function") return;
  unwatchMore(el);
  const obs = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      unwatchMore(el);
      onNear();
      break;
    }
  }, { root: detailEl, rootMargin: "320px 0px", threshold: 0 });
  el._moreObs = obs;
  obs.observe(el);
  _moreObservers.push(obs);
}

let _moreObservers = [];

function renderDetail() {
  clearMoreObservers();
  detailEl.innerHTML = "";
  const seg = segments.find((s) => s.id === selectedId);
  if (!seg) {
    detailEl.appendChild(el("div", "empty", segments.length ? "从左侧选择一段" : "等待事件…"));
    return;
  }

  const head = el("div", "detail-head");
  head.appendChild(el("h2", null, spineTitle(seg)));
  const meta = el("div", "detail-meta");
  meta.appendChild(el("span", null, "Agent · "));
  const ab = el("b", null, AGENT_LABEL[seg.agent] || seg.agent);
  meta.appendChild(ab);
  if (seg.tsStart != null) {
    meta.appendChild(el("span", null, "开始 · "));
    meta.appendChild(el("b", null, fmtTime(seg.tsStart)));
  }
  if (seg.tsEnd != null && seg.tsEnd !== seg.tsStart) {
    meta.appendChild(el("span", null, "结束 · "));
    meta.appendChild(el("b", null, fmtTime(seg.tsEnd)));
  }
  meta.appendChild(el("span", null, "模型 · "));
  meta.appendChild(el("b", null, String(seg.models.length)));
  meta.appendChild(el("span", null, "状态 · "));
  meta.appendChild(el("b", null, String(seg.statuses.length)));
  if (seg.outcome) {
    meta.appendChild(el("span", "badge " + seg.outcome.tone, seg.outcome.label));
  }
  head.appendChild(meta);

  if (seg.kind === "handoff") {
    const d = (seg.enter && seg.enter.detail) || {};
    const ret = (seg.returns && seg.returns[seg.returns.length - 1] && seg.returns[seg.returns.length - 1].detail) || {};
    const extra = d.request_id || d.ask_id || d.sandbox_id || "";
    if (extra) {
      const line = el("div", "detail-meta");
      line.style.marginTop = "6px";
      line.appendChild(el("span", null, "id · "));
      line.appendChild(el("b", null, extra));
      head.appendChild(line);
    }
    const rev = ret.package_revision != null ? ret.package_revision : d.package_revision;
    const delta = ret.package_delta || d.package_delta || "";
    if (rev != null || delta) {
      const pkgLine = el("div", "detail-meta");
      pkgLine.style.marginTop = "6px";
      if (rev != null) {
        pkgLine.appendChild(el("span", null, "package · "));
        pkgLine.appendChild(el("b", null, "r" + String(rev).padStart(3, "0")));
      }
      if (delta) {
        if (rev != null) pkgLine.appendChild(el("span", null, " · "));
        pkgLine.appendChild(el("span", "flow-copy", String(delta).slice(0, 240)));
      }
      head.appendChild(pkgLine);
    }
    const from = seg.fromAgent || (seg.participants || []).find((a) => a !== seg.agent) || "researcher";
    const to = seg.toAgent || seg.agent;
    const flow = el("div", "handoff-flow");
    flow.appendChild(el("span", "agent-pill " + from, AGENT_LABEL[from] || from));
    flow.appendChild(el("span", "flow-arrow", "→"));
    flow.appendChild(el("span", "agent-pill " + to, AGENT_LABEL[to] || to));
    flow.appendChild(el("span", "flow-copy",
      compactEventDetail(seg.enter) || "等待对方处理并返回结果"));
    head.appendChild(flow);
    if (seg.outcome) {
      const returned = el("div", "handoff-return");
      returned.appendChild(el("span", "flow-arrow", "↳ 返回"));
      returned.appendChild(el("span", "badge " + seg.outcome.tone, seg.outcome.label));
      const lastReturn = seg.returns[seg.returns.length - 1];
      const returnCopy = compactEventDetail(lastReturn);
      if (returnCopy) returned.appendChild(el("span", "flow-copy", returnCopy));
      head.appendChild(returned);
    }
  }
  if (seg.kind === "lifecycle" && seg.lifecycle) {
    const d = seg.lifecycle.detail || {};
    if (Object.keys(d).length) {
      const det = document.createElement("details");
      det.style.marginTop = "8px";
      const sum = document.createElement("summary");
      sum.textContent = "事件详情";
      sum.style.cursor = "pointer";
      sum.style.fontSize = "12px";
      sum.style.color = "var(--muted)";
      det.appendChild(sum);
      det.appendChild(pre(jsonStr(d)));
      head.appendChild(bindOpen(det, "life:" + segKey(seg)));
    }
  }
  detailEl.appendChild(head);

  if (seg.kind === "lifecycle" && !seg.models.length && !seg.statuses.length) {
    detailEl.appendChild(el("div", "muted", "生命周期事件，无模型调用。"));
    return;
  }

  let detailTarget = detailEl;
  let execution = null;
  if (seg.kind === "handoff" && (seg.statuses.length || seg.models.length)) {
    execution = document.createElement("details");
    execution.className = "execution-details";
    const summary = document.createElement("summary");
    summary.textContent = `执行细节 · ${seg.statuses.length} 个状态 · ${seg.models.length} 次模型调用`;
    execution.appendChild(summary);
    const execKey = "exec:" + segKey(seg);
    // Chain view defaults open; user's prior collapse/expand survives refresh.
    bindOpen(execution, execKey, {
      defaultOpen: document.body.dataset.view === "chain",
    });
    detailTarget = el("div", "execution-body");
    execution.appendChild(detailTarget);
  }

  // Restore model pagination window before painting cards.
  if (!seg._modelWindow) {
    const savedWin = modelWindows.get(segKey(seg));
    if (savedWin) seg._modelWindow = { ...savedWin };
  }

  if (seg.statuses.length) {
    const sec = el("div", "section");
    sec.appendChild(el("div", "section-h", "状态 · " + seg.statuses.length));
    const LIMIT = 6;
    let shown = 0;
    const wrap = el("div", null);
    const renderMore = (from) => {
      const end = Math.min(seg.statuses.length, from + LIMIT);
      for (let i = from; i < end; i++) wrap.appendChild(renderStatusChip(seg.statuses[i], i));
      shown = end;
      const old = sec.querySelector(".more-btn");
      if (old) {
        unwatchMore(old);
        old.remove();
      }
      if (shown < seg.statuses.length) {
        const btn = moreButton(
          "还有 " + (seg.statuses.length - shown) + " 条状态",
          "滚到此处自动加载 · 也可点击",
        );
        const loadMore = () => renderMore(shown);
        btn.onclick = loadMore;
        sec.appendChild(btn);
        watchNear(btn, loadMore);
      }
    };
    sec.appendChild(wrap);
    renderMore(0);
    detailTarget.appendChild(sec);
  }

  if (seg.models.length) {
    const sec = el("div", "section");
    const chain = document.body.dataset.view === "chain";
    sec.appendChild(el("div", "section-h",
      (chain ? "执行链路 · " : "模型调用 · ") + seg.models.length + (chain ? " 轮" : "")));
    if (chain) sec.classList.add("chain-list");
    const LIMIT = 8;
    let from;
    let to;
    if (seg._modelWindow && !stickLatest) {
      // Keep the user's page across live re-renders.
      from = Math.max(0, seg._modelWindow.from || 0);
      to = Math.min(seg.models.length, Math.max(from, seg._modelWindow.to || from));
      if (to <= from) to = Math.min(seg.models.length, from + LIMIT);
    } else if (stickLatest && seg.models.length > LIMIT) {
      // Live follow: paint the newest page first (data is already in memory).
      from = seg.models.length - LIMIT;
      to = seg.models.length;
    } else {
      from = 0;
      to = Math.min(seg.models.length, LIMIT);
    }
    const wrap = el("div", null);
    const paint = (anchor) => {
      wrap.querySelectorAll(".more-btn").forEach(unwatchMore);
      seg._modelWindow = { from, to };
      const prevScroll = detailEl.scrollTop;
      const prevHeight = detailEl.scrollHeight;
      wrap.innerHTML = "";
      if (from > 0) {
        const earlier = moreButton(
          "↑ 更早的 " + from + " 轮",
          "滚到此处自动加载 · 也可点击",
        );
        const loadEarlier = () => {
          stickLatest = false;
          followBtn.style.display = "block";
          from = Math.max(0, from - LIMIT);
          paint("earlier");
        };
        earlier.onclick = loadEarlier;
        wrap.appendChild(earlier);
        watchNear(earlier, loadEarlier);
      }
      seg.models.slice(from, to).forEach((m) => {
        wrap.appendChild(chain ? renderChainTurn(m) : renderModelCard(m));
      });
      if (to < seg.models.length) {
        const later = moreButton(
          "↓ 还有更新的 " + (seg.models.length - to) + " 轮",
          "滚到此处自动加载 · 也可点击",
        );
        const loadLater = () => {
          stickLatest = false;
          followBtn.style.display = "block";
          to = Math.min(seg.models.length, to + LIMIT);
          paint("later");
        };
        later.onclick = loadLater;
        wrap.appendChild(later);
        watchNear(later, loadLater);
      }
      if (anchor === "earlier") {
        // Keep viewport stable when prepending older turns.
        detailEl.scrollTop = prevScroll + (detailEl.scrollHeight - prevHeight);
      }
    };
    sec.appendChild(wrap);
    paint();
    detailTarget.appendChild(sec);
  } else if (seg.kind === "handoff" && seg.pending) {
    detailTarget.appendChild(el("div", "muted", "交接进行中，等待被叫方模型调用与终态…"));
  }
  if (execution) {
    detailEl.appendChild(execution);
  }
}

function selectSegment(id) {
  selectedId = id;
  lastDetailSig = "";
  renderSpine();
  renderDetail();
  lastDetailSig = detailSig(segments.find((s) => s.id === selectedId));
  persistUi();
}

let _raf = 0;
let _restoredOnce = false;
function afterPush() {
  const visible = visibleSegments();
  if (stickLatest && visible.length) {
    selectedId = visible[visible.length - 1].id;
    followBtn.style.display = "none";
  }
  // Debounce DOM work — SSE replay can deliver hundreds of events quickly.
  if (_raf) return;
  _raf = requestAnimationFrame(() => {
    _raf = 0;
    // After first replay chunk, restore selection / open tabs from sessionStorage.
    if (!_restoredOnce && segments.length) {
      _restoredOnce = true;
      restoreUi();
    }
    renderSpine();
    const seg = segments.find((s) => s.id === selectedId);
    const sig = detailSig(seg);
    // Skip wiping the detail pane when nothing about the selected segment changed
    // (keeps open model tabs intact while other agents keep streaming).
    if (sig !== lastDetailSig) {
      lastDetailSig = sig;
      const prevScroll = detailEl.scrollTop;
      renderDetail();
      if (!stickLatest) detailEl.scrollTop = prevScroll;
    }
    persistUi();
  });
}

followBtn.onclick = () => {
  jumpToBottom();
};

// ---------- stream ----------
let curTask = null, es = null, RES = 0, LAB = 0, TCH = 0;
let sawAny = false;

function resetState() {
  segments = [];
  openHandoff = null;
  openWork = null;
  nextId = 1;
  previousMessagesByAgent = {};
  selectedId = null;
  stickLatest = true;
  lastDetailSig = "";
  _restoredOnce = false;
  // Keep openKeys across soft reconnect of same task; clear on task switch in connect().
  RES = 0; LAB = 0; TCH = 0;
  rc.textContent = "0"; lc.textContent = "0"; tc.textContent = "0";
  updateIdleHeaders();
  followBtn.style.display = "none";
  spineEl.innerHTML = '<div class="empty sm">等待事件…</div>';
  detailEl.innerHTML = '<div class="empty">从左侧选择一段，或等待实时事件</div>';
}

function addCard(ev) {
  sawAny = true;
  pushEvent(ev);
  afterPush();
}

function connect(taskId) {
  if (es) es.close();
  const prevTask = curTask;
  const taskChanged = prevTask != null && prevTask !== taskId;
  curTask = taskId;
  sawAny = false;
  resetState();
  if (taskChanged) {
    openKeys.clear();
    closedKeys.clear();
    modelWindows.clear();
  }
  // Always preload prefs for this task before the SSE replay (full refresh
  // used to skip this because curTask started as null → "taskChanged").
  restoreUi();
  taskEl.textContent = taskId;
  setStatus("实时");
  es = new EventSource("stream/" + encodeURIComponent(taskId));
  es.onmessage = (e) => {
    try { addCard(JSON.parse(e.data)); } catch (err) { /* ignore */ }
  };
}

async function poll() {
  try {
    const j = await (await fetch("latest")).json();
    if (j.task_id && j.task_id !== curTask) connect(j.task_id);
    else if (!j.task_id) taskEl.textContent = "(暂无任务)";
  } catch (e) { /* server not ready */ }
}

const requestedTask = new URLSearchParams(window.location.search).get("task_id");
if (requestedTask) {
  connect(requestedTask);
} else {
  // Detail page requires an explicit task — list lives at /.
  window.location.replace("/");
}
