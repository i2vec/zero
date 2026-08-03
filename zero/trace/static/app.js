/* 0号机 Trace Viewer — Segment Inspector.
 *
 * SSE events:
 *   { t:"orch",  agent, event, detail, ts }
 *   { t:"model", agent, ts, index, input, output }
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
  const saved = localStorage.getItem("zeroView") || "rendered";
  document.body.dataset.view = saved;
  document.querySelectorAll("#seg button").forEach((b) => {
    if (b.dataset.v === saved) b.classList.add("active");
    b.onclick = () => {
      document.body.dataset.view = b.dataset.v;
      localStorage.setItem("zeroView", b.dataset.v);
      document.querySelectorAll("#seg button").forEach((x) => x.classList.toggle("active", x === b));
    };
  });
})();

// ---------- taxonomy ----------
const LIFECYCLE = new Set([
  "task_received", "researcher_started", "task_completed", "task_failed",
  "hook_intercept", "run_exported", "teacher_stats",
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
  task_amended: "订正题面", declined: "拒答 NO_HELP",
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
  if (ev.event === "hook_intercept") return "拦截: " + ((d.command || "").slice(0, 48));
  if (ev.event === "researcher_started") return "Researcher 开始";
  if (ev.event === "task_received") return "收到任务";
  if (ev.event === "run_exported") return "已导出";
  if (ev.event === "teacher_stats") return "Teacher 统计";
  return ev.event;
}

// ---------- segmenter ----------
let segments = [];
let openHandoff = null;
let openWork = null;
let nextId = 1;

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

function ensureWork(agent, ts) {
  // Callee models stay inside the open handoff; anyone else ends it.
  if (openHandoff) {
    if (openHandoff.agent === agent) return openHandoff;
    closeHandoff(false);
  }
  if (openWork && openWork.agent === agent) return openWork;
  closeWork();
  openWork = newSeg({
    kind: "work",
    agent,
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
/** Keys of <details> the user opened — survive live re-renders. */
const openKeys = new Set();

function bindOpen(det, key) {
  if (!key) return det;
  if (openKeys.has(key)) det.open = true;
  det.addEventListener("toggle", () => {
    if (det.open) openKeys.add(key);
    else openKeys.delete(key);
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
    sessionStorage.setItem("zeroTrace:" + curTask, JSON.stringify({
      stickLatest,
      segKey: (() => {
        const s = segments.find((x) => x.id === selectedId);
        return s ? segKey(s) : null;
      })(),
      openKeys: Array.from(openKeys),
    }));
  } catch (e) { /* quota / private mode */ }
}

function restoreUi() {
  if (!curTask) return;
  try {
    const raw = sessionStorage.getItem("zeroTrace:" + curTask);
    if (!raw) return;
    const st = JSON.parse(raw);
    if (typeof st.stickLatest === "boolean") stickLatest = st.stickLatest;
    if (Array.isArray(st.openKeys)) st.openKeys.forEach((k) => openKeys.add(k));
    if (st.segKey) {
      const hit = segments.find((s) => segKey(s) === st.segKey);
      if (hit) selectedId = hit.id;
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

function renderSpine() {
  const prevScroll = spineEl.scrollTop;
  spineEl.innerHTML = "";
  if (!segments.length) {
    spineEl.appendChild(el("div", "empty sm", "等待事件…"));
    return;
  }
  segments.forEach((seg) => {
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
  const data = out.error ? out.error : out.raw;
  raw.appendChild(pre(data != null ? jsonStr(data) : "(无原始数据)", out.error ? "err" : ""));
  body.appendChild(raw);

  const inpDet = document.createElement("details");
  inpDet.className = "input";
  const inpSum = document.createElement("summary");
  inpSum.textContent = "输入 · " + (inp.num_messages || 0) + " 消息 · " + (inp.num_tools || 0) + " 工具";
  inpDet.appendChild(inpSum);
  (inp.messages || []).forEach((m) => {
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

function renderDetail() {
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
    const extra = d.request_id || d.ask_id || d.sandbox_id || "";
    if (extra) {
      const line = el("div", "detail-meta");
      line.style.marginTop = "6px";
      line.appendChild(el("span", null, "id · "));
      line.appendChild(el("b", null, extra));
      head.appendChild(line);
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
      head.appendChild(det);
    }
  }
  detailEl.appendChild(head);

  if (seg.kind === "lifecycle" && !seg.models.length && !seg.statuses.length) {
    detailEl.appendChild(el("div", "muted", "生命周期事件，无模型调用。"));
    return;
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
      if (old) old.remove();
      if (shown < seg.statuses.length) {
        const btn = el("button", "more-btn", "还有 " + (seg.statuses.length - shown) + " 条…");
        btn.type = "button";
        btn.onclick = () => renderMore(shown);
        sec.appendChild(btn);
      }
    };
    sec.appendChild(wrap);
    renderMore(0);
    detailEl.appendChild(sec);
  }

  if (seg.models.length) {
    const sec = el("div", "section");
    sec.appendChild(el("div", "section-h", "模型调用 · " + seg.models.length));
    seg.models.forEach((m) => sec.appendChild(renderModelCard(m)));
    detailEl.appendChild(sec);
  } else if (seg.kind === "handoff" && seg.pending) {
    detailEl.appendChild(el("div", "muted", "交接进行中，等待被叫方模型调用与终态…"));
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
  if (stickLatest && segments.length) {
    selectedId = segments[segments.length - 1].id;
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
  stickLatest = true;
  followBtn.style.display = "none";
  if (segments.length) selectedId = segments[segments.length - 1].id;
  lastDetailSig = "";
  renderSpine();
  renderDetail();
  lastDetailSig = detailSig(segments.find((s) => s.id === selectedId));
  persistUi();
};

// ---------- stream ----------
let curTask = null, es = null, RES = 0, LAB = 0, TCH = 0;
let sawAny = false;

function resetState() {
  segments = [];
  openHandoff = null;
  openWork = null;
  nextId = 1;
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
  const taskChanged = curTask !== taskId;
  curTask = taskId;
  sawAny = false;
  resetState();
  if (taskChanged) openKeys.clear();
  else restoreUi(); // same task reconnect: preload prefs before events arrive
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
if (requestedTask) connect(requestedTask);
else { poll(); setInterval(poll, 4000); }
