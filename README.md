# 0 号机（`zero`）

一个**三 Agent 科学实验系统**：给它一句自然语言的科研任务，它会自动
「搭环境 → 写代码 → 在 sandbox 里跑实验 → 给出科学结论」，卡住时还能向老师提问。

- **Researcher**：科学主控，设计实验、写代码、执行、下结论。
- **Labwright**：环境工程师，把 Researcher 声明的依赖变成一个**已验证**的 sandbox。
- **Teacher**：持有本题额外 hint 的助教；科学卡壳给 HINT，**科学题面**有缺陷给订正；路径/镜像/装包等环境问题应 decline，交给 Labwright。

三者各跑独立的 Claude Code 会话，通过结构化 MCP 工具通信。全过程可追踪：每次
模型调用（含思维链）和每个编排事件都落盘。

> 想了解**内部设计与职责边界**，读 [`AGENT.md`](./AGENT.md)。本文只讲**怎么用**。

---

## 1. 环境准备

```bash
conda activate zero                       # 已装好 claude-agent-sdk 等依赖的环境
cd /path/to/zero                          # 本仓库根目录
pip install -e .                          # 一次安装：注册 `zero` 和 `capgw` 两个命令
```

捕获模型调用的网关 **`capgw` 已内置在本项目里**（`capgw/` 包），无需单独安装。
`zero run` 默认会自动拉起它（`python -m capgw.cli serve`），你一般不用手动启动。

### 配置密钥（`.env`）

```bash
cp .env.example .env   # 填入真实密钥；`.env` 已被 gitignore
```

上游模型和 Bohrium key 都放在项目根的 `.env`（一份 dotenv 搞定两类密钥）。
capgw 只读 `LLM_*` 三个键，其余行会被忽略。完整键名见 [`.env.example`](./.env.example)。

想拆开也行：用 `ZERO_LLM_ENV` 指定单独的模型配置文件，用 `ZERO_BOHRIUM_ENV`
指定单独的 key 文件。

---

## 2. 快速开始

```bash
# 推荐：选一个题面 md
zero run ../tasks/chloroform-mc/instruction.md --run-name chloroform-01
# 或仓库内示例：zero run sio2_task.md

# 也可以直接塞一句自然语言
zero run "用 scikit-learn 的 iris 数据集训练逻辑回归，报告测试集准确率并与随机基线比较"

# 查看当前解析出的配置
zero info
```

positional `task`：若是已有的 `.md` / `.markdown` / `.txt` 路径则读文件当题面；否则当内联 prompt。

或者直接跑内置的端到端 smoke（iris 逻辑回归全链路）：

```bash
python run_e2e.py
```

### `zero run` 选项

| 选项 | 作用 |
|------|------|
| `task` | 题面：`path/to/task.md`（推荐）或内联字符串 |
| `--max-turns N` | Researcher 自治循环的最大轮数（默认 60） |
| `--run-name NAME` | 自定义 `runs/<NAME>/`；中断可重跑同名；仅已完成时需删目录或换名 |
| `--no-capgw` | 不托管 capgw（假设你已经自己起好了） |
| `--trace-ui` | 顺带为这次运行开实时查看器（**默认关**；轨迹播放已独立成 `zero viewer`，见 [§4](#4-轨迹查看器独立于运行)） |
| `--no-export` | 跳过拉取 `deliverables/`（轨迹等仍实时写在 `runs/<run>/`，见 [§5](#5-产物与目录)） |
| `--task-key KEY` | 标识**题目**本身（asks / 订正元数据；**不**用来找 hint 文件；默认用 run 名） |
| `--hints PATH` | 把一个 `.md` 文件或含 `*.md` 的目录拷进本次 `runs/<run>/teacher/hint_bank/`（不覆盖已有） |
| `--no-teacher` | 关闭 Teacher（默认由 `ZERO_TEACHER_ENABLED` 决定，见 [§6](#6-teacher额外-hint-与题面订正)） |

加了 `--trace-ui` 时，任务结束后进程会**保持查看器在线**方便回看，按 `Ctrl-C` 退出。

---

## 3. Sandbox 后端

实验代码跑在 sandbox 里。后端由 `ZERO_SANDBOX_BACKEND` 决定，上层 Agent 完全无感知。

| 后端 | 说明 | 何时用 |
|------|------|--------|
| `local` | 每个 sandbox 一个本地 venv，命令直接在宿主机跑 | 无 Docker 的轻量兜底 |
| `docker` | 每个 sandbox 一个长驻容器 | 本机有 Docker daemon |
| `lbg` | Bohrium 云 sandbox（通过 `lbg` CLI） | 需要云端算力/GPU |
| `auto`（默认） | 有 Docker 用 `docker`，否则 `local`（**不会**自动选 lbg） | 一般情况 |

```bash
ZERO_SANDBOX_BACKEND=docker zero run "..."
ZERO_SANDBOX_BACKEND=lbg    zero run "..."
```

### 使用 Bohrium 云 sandbox（`lbg`）

前提：宿主机装好 `lbg` 与 `bohr` CLI，并提供 Bohrium access key。key 的解析顺序：

1. 环境变量 `BOHRIUM_ACCESS_KEY` / `ACCESS_KEY` / `BOHRIUM_KEY`
2. dotenv 文件里的 `bohrium_key=...`（默认 `zero/.env`，用 `ZERO_BOHRIUM_ENV` 覆盖）

> key 只在宿主机侧注入子进程，**不会**写进 captures，也**不会**进 sandbox。

lbg 后端会自动完成：`bohr image list` 挑基础镜像 → `machine list` 选满足
cpu/内存/GPU 的**最小** SKU → 幂等地建模板 → 启动 sandbox（超时自动销毁兜底）。

可选环境变量：

| 变量 | 默认 | 说明 |
|------|------|------|
| `ZERO_LBG_TIMEOUT` | `10800`（3h） | sandbox 自动销毁寿命（秒） |
| `ZERO_LBG_EXTRA_DISK_GB` | `0` | 在默认 30Gi 之上追加的 overlay 磁盘 |
| `ZERO_LBG_PROJECT_ID` | 空 | 设置后按该 project 预算计费（否则走个人钱包）；**同时是 `snapshot()` 真正调用 `lbg sdbx image commit` 的前提**——未设置时 `image_digest` 会退化为 `pip freeze` 摘要而不是真实镜像 |
| `ZERO_PIP_INDEX_URL` | 清华源 | 创建 sandbox 时写入 `~/.pip/pip.conf` 的 pip 镜像（置空则不改镜像） |
| `ZERO_LBG_BIN` | `lbg` | lbg 可执行路径 |
| `ZERO_BOHR_BIN` | `/root/.bohrium/bohr` | bohr 可执行路径 |

单独验证 lbg 后端是否可用（会真实创建一台最便宜的 CPU sandbox 并自动销毁）：

```bash
python scripts/lbg_smoke.py
```

单独验证 `snapshot()` / `image commit` 是否真的能产出可复用镜像（需要 `ZERO_LBG_PROJECT_ID`，会额外消耗一次镜像构建的时间和存储）：

```bash
ZERO_LBG_PROJECT_ID=<id> python scripts/lbg_snapshot_smoke.py
```

---

## 4. 轨迹查看器（独立于运行）

轨迹**播放**和**运行**是两套系统：默认 `zero run` **不**再拉起查看器（避免占端口、干扰批量跑）。
查看器是一个网页看板（默认 `http://<host>:8901`）：**Segment Inspector** 双栏布局——
左侧「交接脊柱」按阻塞交接切段（R↔L / R↔T 与终态），右侧展示选中段的模型调用与状态芯片；
顶栏统计三 Agent 调用次数。思维链默认折叠。随时读历史轨迹回放：

```bash
# 单独起查看器，读取 runs/<run>/trace/
zero viewer                 # 用默认 host/port
zero viewer --port 9000     # 临时换端口
```

| 变量 | 默认 | 说明 |
|------|------|------|
| `ZERO_TRACE_UI_HOST` | `0.0.0.0` | 监听地址（`zero viewer --host` 可临时覆盖） |
| `ZERO_TRACE_UI_PORT` | `8901` | 端口（`zero viewer --port` 可临时覆盖） |

如果想在**跑任务时**顺带看实时轨迹，给 `zero run` 加 `--trace-ui`。

---

## 5. 产物与目录

**一次任务 = 一个大文件夹** `runs/<run名>/`（默认 `task-20260724-abc123`，或 `--run-name`）。
运行中直接写入该树；查看器也只读这里的 `trace/`。

### 系统根目录（`ZERO_ROOT`，默认 = 本仓库根）

| 路径 | 角色 |
|------|------|
| `<repo>/`（本仓库） | 代码与 CLI（`zero` / `capgw`）；独立 clone 时也是默认 `ZERO_ROOT` |
| `agent_skills/{researcher,labwright,teacher}/` | 各 Agent 加载的 Skills（共享输入；需人工发布） |
| `experience/researcher/` | Researcher **轻量经验库**（跨 run；模型直接写入，见 [§9](#9-researcher-经验库)） |
| `tasks/` | 外部题面/包（可选，共享输入） |
| `runs/<run名>/` | **唯一**运行产物根（状态、资源、sandbox、日志、轨迹全在这里） |

若 `tasks/` / `runs/` / `agent_skills/` 放在仓库外（例如父级 monorepo），在 `.env` 里设
`ZERO_ROOT=/path/to/that/root`。跨 run 共享的只有：`agent_skills/`、`experience/researcher/`、
`tasks/`、代码。不再使用 `.zero_state/`。

### `runs/<run名>/` 布局

```
runs/<run名>/
├── run.json              # 结束元数据（状态/后端/sandbox/路径索引）
├── conclusion.md         # 最终科学结论（若有）
├── workspace/            # 宿主侧工作区（三 Agent 的 cwd）
├── deliverables/
│   ├── repo/             # 实验脚本与配置（从 sandbox 的 export/repo 拉回）
│   └── output/           # 指标/图表/结果（从 export/output 拉回）
├── trace/
│   ├── researcher.jsonl  # Layer 1：模型调用（含思维链）
│   ├── labwright.jsonl
│   ├── teacher.jsonl     # 没问过则不存在
│   └── events.jsonl      # Layer 2：编排事件
├── teacher/
│   ├── asks.jsonl        # 每次 ask_teacher（没问过则不存在）
│   ├── task_addendum.md  # 本次题面订正（没有则不存在）
│   └── hint_bank/        # 本次 Teacher 专用 hint（*.md）；Researcher 看不到
├── resources/            # 本 run 的资源下载/缓存 + manifest
├── sandboxes/            # local 后端的 venv sandbox（若用）
├── logs/                 # 含 capgw.log
└── meta/
    ├── task.json         # 本 run 生命周期状态
    ├── skill_candidates/ # 本 run 提出的 Skill 候选
    └── experience_writes.jsonl  # 本次写入经验库的审计（若有）
```

`--run-name`：若该文件夹已有且 `run.json` 为 **已完成**，需删目录或换名；中断/失败的同名 run **直接复用**同一大文件夹，不再被幽灵数据库挡住。

`deliverables/` 由 Researcher 在 sandbox 里 curate 的 `export/repo` + `export/output` 在结束时拉回
（本地/docker 读宿主 workspace，lbg 从云 sandbox 流式拉）。对 `__pycache__`/`.git` 和超过
`ZERO_EXPORT_MAX_FILE_MB`（默认 50MB）的单文件做防呆过滤。`--no-export` 只跳过这次拉取，
**不影响** `trace/` / `workspace/` / `teacher/` 的实时写入。

---

## 6. Teacher：额外 hint 与题面订正

Teacher 是第三个 agent，持有**本次运行**的人工 hint，Researcher 卡住时可以调
`mcp__teacher__ask_teacher` 问它；从不被问到的运行不受任何影响（懒启动，不产生
`runs/<run名>/trace/teacher.jsonl`）。

### 写 hint

Hint 只放在大文件夹里：`runs/<run>/teacher/hint_bank/*.md`（只有 Teacher 能读）。两种放法：

```bash
# 1) 预放（先定 --run-name）
mkdir -p runs/myrun/teacher/hint_bank
echo '- check the RDF first peak' > runs/myrun/teacher/hint_bank/notes.md
zero run "..." --run-name myrun

# 2) 启动时拷入
zero run "..." --hints /path/to/notes.md
# 或 --hints /path/to/dir_of_md_files
```

`--task-key` 只给 asks / 订正当题目标识，**不**用来定位 hint 文件。

### Teacher 的回答

| 判断 | 回答 | 落地 |
|------|------|------|
| **科学题面**有缺陷（缺量/缺单位/缺容差/缺输出契约/科学表述自相矛盾） | `TASK_AMENDMENT` | 订正写进 `runs/<run名>/teacher/task_addendum.md` |
| 解题遇到困难，题面没问题 | `HINT` | 只在这次对话里给 Researcher，方法级、分级释放 |
| 环境/路径/镜像/装包，或没有可给的东西 | `NO_HELP` | decline；环境类应让 Researcher 找 Labwright。提问预算用尽时也返回这个 |

`TASK_AMENDMENT` **不**订正部署说明、镜像标签、宿主机路径——那是 Labwright 的交付范围。

### 相关配置

| 变量 | 默认 | 说明 |
|------|------|------|
| `ZERO_TEACHER_ENABLED` | `1` | 关掉后 Researcher 拿不到 `ask_teacher` 工具（也可用 `--no-teacher`） |
| `ZERO_TEACHER_MAX_ASKS` | `8` | 一次运行的提问上限，用尽后一律 `NO_HELP` |

用过 hint 的运行要和没用过的分开看：`runs/<run>/teacher/asks.jsonl` 记录了每次
提问、用掉的 kind 以及提问预算消耗情况，避免污染 benchmark 分数对比。

---

## 7. 常用配置一览

| 变量 | 默认 | 说明 |
|------|------|------|
| `ZERO_ROOT` | 本仓库根 | 系统根目录（`runs/` / `agent_skills/` / `tasks/` 等） |
| `ZERO_LLM_ENV` | `<repo>/.env` | 上游模型配置文件（默认与 `bohrium_key` 同一份） |
| `ZERO_MODEL` | `claude-sonnet-4` | 交给 Claude Code 的模型名（capgw 上游会覆盖） |
| `ZERO_CAPGW_PORT` | `8900` | capgw 端口 |
| `ZERO_SANDBOX_BACKEND` | `auto` | `auto`/`local`/`docker`/`lbg` |
| `ZERO_DOCKER_BASE_IMAGE` | `python:3.11-slim` | docker 后端基础镜像 |
| `ZERO_RUNS_DIR` | `<root>/runs` | 一次任务一个大文件夹的根目录 |
| `ZERO_EXPERIENCE_DIR` | `<root>/experience/researcher` | Researcher 轻量经验库根目录 |
| `ZERO_EXPORT_MAX_FILE_MB` | `50` | 拉取 deliverables 时单文件大小上限（防呆，超过即跳过） |
| `ZERO_TEACHER_ENABLED` / `ZERO_TEACHER_MAX_ASKS` | `1` / `8` | 见 [§6](#6-teacher额外-hint-与题面订正) |

`zero info` 会打印当前解析结果，排错时先看它。

---

## 8. 排错

- **`capgw gateway did not come up`**：检查 `zero/.env` 里的 `LLM_*` 字段，或先手动
  `capgw serve --env-file zero/.env --port 8900` 看 `runs/<run>/logs/capgw.log`。
- **模型 429 / 502**：上游限流或网关瞬时不可用；lbg 创建时的瞬时 502 已内置
  重试 + 复用检查，其余请重试。
- **lbg 报 `no Bohrium access key`**：见 [§3](#使用-bohrium-云-sandboxlbg) 的 key 解析顺序。
- **Researcher 想直接 `pip install`/`apt`/`docker`**：这是被 hook 故意拦截的——
  依赖要通过 `ensure_environment` 声明给 Labwright。

---

## 9. Researcher 经验库

跨 run 的**短教训**库，与 Skills 并行：不从轨迹自动抽取，由 Researcher 调用 MCP **自己决定**是否入库；写入后立刻可被后续 run 检索。

```
experience/researcher/
├── index.jsonl           # 检索用元数据
└── entries/<id>.md       # 单条经验
```

| 工具 | 作用 |
|------|------|
| `mcp__experience__search_experience` | 按关键词 / tags 检索 |
| `mcp__experience__get_experience` | 按 id 读全文 |
| `mcp__experience__record_experience` | 写入共享库（轻校验：拒密钥、绝对路径、task/sandbox id） |

与 Skills 的分工：短经验 → 经验库（直写）；长流程 → `propose_reusable_skill`（人工审核后发布）。
禁止入库：本题最终数值、一次性路径、装环境细节。

路径可用 `ZERO_EXPERIENCE_DIR` 覆盖；本次写入另记在 `runs/<run>/meta/experience_writes.jsonl`。

```bash
python scripts/experience_smoke.py   # 无模型冒烟
```

---

## 10. 可审核的 Agent Skills

Researcher 与 Labwright 都可以在任务结束时提出可复用的 Skill 候选，但不能直接改写正在
加载的 Skill 目录：

- Researcher 候选：研究流程、实验自检、结果交付等研究方法；
- Labwright 候选：安装、工具、镜像、资源挂载与环境验证等工程方法。

候选保存在 `runs/<run>/meta/skill_candidates/`，先做 frontmatter、密钥、任务特定标识和作用域校验；
审核发布后才在**下一次**对应 Agent 会话中加载：

```bash
zero skills list
zero skills validate researcher <candidate-id>
zero skills publish researcher <candidate-id>
zero skills reject labwright <candidate-id> --reason "too task-specific"
```

发布到 `agent_skills/<role>/skills/<name>/` 时会写入 `skill-manifest.json`；已有 Skill 更新会自动备份。

---

## 11. 项目结构

包代码在 `zero/zero/`（安装后 import 名为 `zero`），同级还有内置网关 `capgw/`：

```
zero/                      # 本仓库（pip install -e .）
├── README.md / AGENT.md
├── run_e2e.py / scripts/  # 端到端与后端冒烟
├── capgw/                 # 捕获网关（模型 I/O → runs/<id>/trace/*.jsonl）
└── zero/                  # Python 包
    ├── cli.py             # `zero run` / `zero viewer` / `zero info`
    ├── config.py          # 环境变量驱动的路径与开关
    ├── export.py          # 结束时拉 deliverables + 写 run.json
    ├── experience/        # Researcher 轻量经验库（store + MCP）
    ├── orchestrator/      # 生命周期（不做科学决策）
    ├── researcher/ labwright/ teacher/
    ├── sandbox/           # local / docker / lbg
    ├── protocol/          # Spec / Manifest / TeacherAsk …
    ├── trace/             # Layer2 写入 + 查看器（static/）
    ├── skills/            # Skill 候选校验与发布
    └── state/             # SQLite
```

设计细节见 [`AGENT.md`](./AGENT.md)。
