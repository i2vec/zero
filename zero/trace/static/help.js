/* Shared “使用说明” overlay for run list + trace detail. */

(function () {
  const HELP_HTML = `
<div class="help-panel" role="dialog" aria-modal="true" aria-labelledby="help-title">
  <div class="help-head">
    <h2 id="help-title">0号机 Viewer · 使用说明</h2>
    <button type="button" class="help-close" data-help-close aria-label="关闭">×</button>
  </div>
  <div class="help-body">
    <section>
      <h3>这是什么</h3>
      <p>本查看器回放 <code>zero run</code> 产生的轨迹：三个 Agent 如何交接、各自说了什么、调用了哪些工具。
      列表页选 run，详情页看执行段。默认读 <code>runs/&lt;run&gt;/trace/</code>，可与正在跑的任务实时同步。</p>
    </section>

    <section>
      <h3>三个 Agent</h3>
      <div class="help-agents">
        <article class="help-agent researcher">
          <header><span class="agent-pill researcher">Researcher</span><strong>科学主控</strong></header>
          <p>读题、设计实验、写代码、在 Sandbox 跑实验、分析结果并交付结论。环境问题找 Labwright，科学卡壳才问 Teacher。</p>
        </article>
        <article class="help-agent labwright">
          <header><span class="agent-pill labwright">Labwright</span><strong>环境工程师</strong></header>
          <p>按声明搭好可复现 Sandbox：装依赖、下数据、挂载资源、验证环境并发布 Manifest。不管科学题面，也不替 Researcher 做科学选择。</p>
        </article>
        <article class="help-agent teacher">
          <header><span class="agent-pill teacher">Teacher</span><strong>科学助教 / 题包策展</strong></header>
          <p>开跑前 Preflight 检查题包；解题中给 HINT，或热更新 live 题包（TASK / GRADER / BOTH_AMENDMENT）。
          订正写回 <code>task_package/</code>，须过 lint；Researcher 只看到 <code>package_delta</code>（不含标准答案）。
          路径/镜像/装包问题应拒答，那是 Labwright 的事。</p>
        </article>
      </div>
      <p class="help-note">三者都有完整宿主工具权限；职责边界靠 system prompt 与 MCP 合约，而不是工具白名单。</p>
    </section>

    <section>
      <h3>页面怎么读</h3>
      <ul>
        <li><strong>Runs 列表</strong>：每个卡片是一次任务；点进去看该 run 的 Segment Inspector。</li>
        <li><strong>交接脊柱（左）</strong>：按时间把任务切成段——生命周期、R↔L / R↔T 阻塞交接、以及各 Agent 的连续工作段。</li>
        <li><strong>段详情（右）</strong>：选中左侧一段后，展示这一段里的输入 / 回答 / 动作（或完整模型卡片）。长段按页展示（每页约 8 轮），避免一次撑爆 DOM。</li>
        <li><strong>● 实时</strong>：正在通过 SSE 收事件；结束后状态会变为「完成」或「失败」。模型事件只推本轮增量，不重复整段历史。</li>
      </ul>
    </section>

    <section>
      <h3>顶栏按钮</h3>
      <dl class="help-dl">
        <dt>← Runs</dt><dd>返回 run 列表。</dd>
        <dt>全部 / Researcher / Labwright / Teacher</dt>
        <dd>按<strong>主执行者</strong>过滤左侧脊柱。选某个 Agent 时，只看它自己的工作段与它发起的交接。</dd>
        <dt>链路</dt>
        <dd>精简回合视图：只显示本轮新增输入、回答正文、动作（含 Write 的完整代码等）。隐藏 system prompt 与冗长上下文。</dd>
        <dt>完整</dt>
        <dd>展开模型调用卡片：推理、正文、工具调用细节、输入上下文。</dd>
        <dt>原始 JSON</dt>
        <dd>显示上游原始消息 JSON，便于排查解析问题。</dd>
        <dt>展开 / 折叠</dt>
        <dd>一键打开或关闭详情里所有 <code>&lt;details&gt;</code>（完整视图里的模型卡、推理折叠等）。</dd>
        <dt>底部 / ↓ 底部</dt>
        <dd>顶栏「底部」选中最新段并滚到脊柱与详情最底部；详情右下角「↓ 底部」只滚当前段详情。</dd>
      </dl>
    </section>

    <section>
      <h3>颜色与徽章</h3>
      <ul>
        <li><strong>侧条颜色</strong>：紫 = 生命周期 / 编排；蓝 = Researcher；橙 = Labwright；绿 = Teacher。</li>
        <li><strong>绿色徽章</strong>：成功 / ENVIRONMENT_READY / HINT / TASK_AMENDMENT / GRADER_AMENDMENT / BOTH_AMENDMENT 等。</li>
        <li><strong>黄色徽章</strong>：待决策（Labwright 需要 Researcher 拍板）。</li>
        <li><strong>红色徽章</strong>：失败 / 错误。</li>
        <li><strong>灰色徽章</strong>：进行中、尚未收到返回。</li>
      </ul>
    </section>

    <section>
      <h3>常见协作路径</h3>
      <ol>
        <li>Orchestrator 收到任务 → Teacher Preflight（可改 live 题包）→ Researcher 开始。</li>
        <li>Researcher 调用 <code>ensure_environment</code> → Labwright 搭环境 → 返回 READY（或 NEEDS_DECISION）。</li>
        <li>Researcher 写代码、在 sandbox 执行；缺包再找 Labwright 补资源。</li>
        <li>科学/题包卡住 → <code>ask_teacher</code> → HINT，或订正题面/grader（看返回里的 <code>package_delta</code> / revision）。</li>
        <li>任务结束 → Harbor 打分 → Teacher 结题审阅 → 冻结 <code>finalized_task/</code>，导出 environment 等产物。</li>
      </ol>
    </section>

    <section>
      <h3>快捷操作</h3>
      <p>按 <kbd>?</kbd> 或点「使用说明」打开本面板；按 <kbd>Esc</kbd> 关闭。</p>
    </section>
  </div>
</div>`;

  let backdrop = null;

  function ensure() {
    if (backdrop) return backdrop;
    backdrop = document.createElement("div");
    backdrop.className = "help-backdrop";
    backdrop.hidden = true;
    backdrop.innerHTML = HELP_HTML;
    document.body.appendChild(backdrop);
    backdrop.addEventListener("click", (ev) => {
      if (ev.target === backdrop || ev.target.closest("[data-help-close]")) close();
    });
    return backdrop;
  }

  function open() {
    const node = ensure();
    node.hidden = false;
    document.body.classList.add("help-open");
    const closeBtn = node.querySelector("[data-help-close]");
    if (closeBtn) closeBtn.focus();
  }

  function close() {
    if (!backdrop) return;
    backdrop.hidden = true;
    document.body.classList.remove("help-open");
  }

  function toggle() {
    if (backdrop && !backdrop.hidden) close();
    else open();
  }

  function bindTriggers() {
    document.querySelectorAll("[data-help-open]").forEach((btn) => {
      btn.addEventListener("click", open);
    });
    document.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape" && backdrop && !backdrop.hidden) {
        close();
        return;
      }
      if (ev.key === "?" && !ev.ctrlKey && !ev.metaKey && !ev.altKey) {
        const tag = (ev.target && ev.target.tagName) || "";
        if (tag === "INPUT" || tag === "TEXTAREA" || (ev.target && ev.target.isContentEditable)) return;
        ev.preventDefault();
        toggle();
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bindTriggers);
  } else {
    bindTriggers();
  }

  window.ZeroHelp = { open, close, toggle };
})();
