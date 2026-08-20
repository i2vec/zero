# 0 号机（`zero`）

一个**三 Agent 科学实验系统**：给它一句自然语言的科研任务，它会自动
「搭环境 → 写代码 → 在 sandbox 里跑实验 → 给出科学结论」，卡住时还能向老师提问。

- **Researcher**：科学主控，设计实验、写代码、执行、下结论。
- **Labwright**：环境工程师，把 Researcher 声明的依赖变成一个**已验证**的 sandbox。
- **Teacher**：题包策展 + 科学助教——开跑前 Preflight、解题中 HINT / 热更新题包、结题审阅冻结定稿；路径/镜像/装包等环境问题应 decline，交给 Labwright。

三者各跑独立的 Claude Code 会话，通过结构化 MCP 工具通信。全过程可追踪：每次
模型调用（含思维链）和每个编排事件都落盘。

> 想了解**内部设计与职责边界**，读 [`AGENT.md`](./AGENT.md)。本文只讲**怎么用**。

---

## 运行流程（`zero run`）

目标是**产出更好的题包 + 可复现环境**，不是抬高某次 Researcher 的分数。

```
题包目录（instruction.md + 可选 tests/ paper/ …）
        │
        ▼
Orchestrator 建 runs/<id>/，拷贝为 live task_package/
        │
        ├─（可选）ExternalTaskPreparer / Labwright 预准备
        │
        ▼
Teacher Preflight（默认开）── lint / 可先订正 live 包
        │
        ▼
Researcher 主循环
   ├─ Labwright：ensure_environment / 补资源 → Manifest READY
   ├─ sandbox 写代码、跑实验
   └─ ask_teacher → HINT
                  或 TASK / GRADER / BOTH_AMENDMENT（热更新 live 包，须过 lint）
        │
        ▼
Harbor 打分（对照当前 live 包）
        │
        ▼
Teacher 结题审阅 → 再订正（可选）→ 冻结 finalized_task/（+ optimized_task/）
        │
        ▼
导出 environment/、run.json、轨迹 …
```

| 阶段 | 谁 | 做什么 |
|------|----|--------|
| Preflight | Teacher | 开跑前检查题包；可改正题面/grader |
| 解题 | Researcher ↔ Labwright | 环境与实验 |
| 解题 | Researcher ↔ Teacher | HINT；或热更新 `task_package/`（Researcher 只见 `package_delta`） |
| 打分 | 编排层 | Harbor `tests/` |
| 结题 | Teacher | 按文献完善题包，冻结定稿 |

开关：`ZERO_TEACHER_PREFLIGHT`（默认开）、`ZERO_PACKAGE_MAX_REVISIONS`（默认 12）。

---

## 1. 环境准备

本仓库布局通常是**父级 monorepo + 嵌套包**：

```
/personal/zero/                 # ZERO_ROOT（推荐）
├── agent_skills/               # 各 Agent 的 Skills
├── experience/researcher/      # 跨 run 经验库
├── tasks/                      # 题包
├── runs/                       # 运行产物
├── run_target.sh               # 并发批跑示例
└── zero/                       # 本包（pip install -e .）
    ├── README.md / AGENT.md
    ├── .env / .env.example
    ├── capgw/
    └── zero/                   # Python 包
```

```bash
conda activate zero
cd /path/to/zero/zero           # 含 pyproject.toml 的包目录
pip install -e .                # 注册 `zero` 和 `capgw` 两个命令
```

若 `tasks/` / `runs/` / `agent_skills/` 在父目录，请在 `.env` 里设：

```bash
ZERO_ROOT=/path/to/zero-system-root   # 例如 /personal/zero
```

捕获模型调用的网关 **`capgw` 已内置**（`capgw/` 包）。`zero run` 默认会自动拉起它。

### 配置密钥（`.env`）

```bash
cp .env.example .env   # 填入真实密钥；`.env` 已被 gitignore
```

上游模型和 Bohrium key 都放在项目根的 `.env`。capgw 只读 `LLM_*` 三个键。
完整示例见 [`.env.example`](./.env.example)。

可用 `ZERO_LLM_ENV` / `ZERO_BOHRIUM_ENV` 把模型配置与 Bohrium key 拆到不同文件；
也可用 `ZERO_ENV_FILE` 指定要加载的 dotenv 路径。

---

## 2. 快速开始

```bash
# 只支持题包目录：必须含 instruction.md；有 tests/ 则打分 + Teacher 可阅
zero run ../tasks/chloroform-mc --run-name chloroform-01

# paper/ 存在时自动作为 Teacher hints（也可用 --hints 覆盖）
zero run ../tasks/chloroform-mc --run-name chloroform-mc

# tasks_v2 同理
zero run "../tasks_v2/Solving high-dimensional PDEs with the deep BSDE method" \
  --run-name deepbsde-v2

# 查看当前解析出的配置
zero info
```

positional `task`：**题包目录**（含 `instruction.md`）。不再接受内联自然语言或单独 `.md` 路径。

### `zero run` 选项

| 选项 | 作用 |
|------|------|
| `task` | 题包目录（必含 `instruction.md`；`tests/` 用于打分） |
| `--max-turns N` | Researcher 自治循环的最大轮数（默认 1000） |
| `--run-name NAME` | 自定义 `runs/<NAME>/`；中断可重跑同名；仅已完成时需删目录或换名 |
| `--no-capgw` | 不托管 capgw（假设你已经自己起好了） |
| `--trace-ui` | 顺带为这次运行开实时查看器（**默认关**；见 [§4](#4-轨迹查看器独立于运行)） |
| `--no-export` | 跳过拉取 `deliverables/`（轨迹等仍实时写入，见 [§5](#5-产物与目录)） |
| `--task-key KEY` | 标识**题目**本身（asks / 订正元数据；**不**用来找 hint；默认用 run 名） |
| `--hints PATH` | Teacher hint；默认自动用 `<package>/paper/`（若有） |
| `--no-teacher` | 关闭 Teacher（默认由 `ZERO_TEACHER_ENABLED` 决定） |

加了 `--trace-ui` 时，任务结束后进程会**保持查看器在线**，按 `Ctrl-C` 退出。

### 并发批跑

每个 `zero run` 默认共用本地 **一个** capgw（默认 `:8900`；捕获按
`<task_id>/trace/<agent>` 分文件）。并行跑多题时**不必**再手写端口：

```bash
zero run tasks/A --run-name A &
zero run tasks/B --run-name B &
wait
```

`CapgwRunner` 用文件锁 + 引用计数管理共享网关；最后一个退出的进程再停掉它。
若要恢复「每进程独占端口」：`ZERO_CAPGW_SHARED=0` 且为各进程设不同
`ZERO_CAPGW_PORT`。程序化并发也可直接用 `ZeroRuntime`（见 [`AGENT.md`](./AGENT.md)）。

### 冒烟脚本（`scripts/`）

```bash
python scripts/teacher_smoke.py       # Teacher 协议离线冒烟
python scripts/experience_smoke.py    # 经验库读写
python scripts/capgw_shared_smoke.py  # 共享 capgw 租约/引用计数
python scripts/grading_optimize_smoke.py  # Harbor 打分 + optimized_task
python scripts/dual_sandbox_smoke.py  # local 双 sandbox 冻结/spawn
python scripts/lbg_smoke.py           # 真实建一把最便宜的 lbg CPU sandbox 并销毁
ZERO_LBG_PROJECT_ID=<id> python scripts/lbg_snapshot_smoke.py  # 验证 image commit
```

---

## 3. Sandbox 后端

实验代码跑在 sandbox 里。后端由 `ZERO_SANDBOX_BACKEND` 决定，上层 Agent 完全无感知。

| 后端 | 说明 | 何时用 |
|------|------|--------|
| `local` | 每个 sandbox 一个本地 venv | 无 Docker 的轻量兜底 |
| `docker` | 每个 sandbox 一个长驻容器 | 本机有 Docker daemon |
| `lbg` | Bohrium 云 sandbox（通过 `lbg` CLI） | 需要云端算力/GPU |
| `auto`（默认） | 有 Docker 用 `docker`，否则 `local`（**不会**自动选 lbg） | 一般情况 |

```bash
ZERO_SANDBOX_BACKEND=docker zero run "..."
ZERO_SANDBOX_BACKEND=lbg    zero run "..."
```

路径约定：实验 cwd 是 **`/workspace`**；Harbor 风格题面常要求把结果写到 **`/app/outputs`**。
Researcher / Labwright 已加载 `sandbox-root` / `lbg-cli` Skills，默认以 root 进入并准备这些路径。

### 使用 Bohrium 云 sandbox（`lbg`）

前提：宿主机装好 `lbg`、`bohr` 与 `trisol` CLI，并提供 Bohrium access key。
Trisol 是 Playground 数据/模型资产传输的系统依赖，不属于 Python `pyproject.toml`：

```bash
curl -fsSL https://trisol.dp.tech/install.sh | bash
trisol version
TRISOL_TOKEN=... trisol whoami
```

Zero 也会在每个新 LBG sandbox 内安装并验证 Trisol；安装或版本检查失败时，
sandbox 创建失败并自动销毁，不会以“资产可用”的状态继续运行。key 的解析顺序：

1. 环境变量 `BOHRIUM_ACCESS_KEY` / `ACCESS_KEY` / `BOHRIUM_KEY`
2. dotenv 文件里的 `bohrium_key=...`（默认包目录 `.env`，用 `ZERO_BOHRIUM_ENV` 覆盖）

> key 只在宿主机侧注入子进程，**不会**写进 captures，也**不会**进 sandbox。

lbg 后端会自动：`bohr image list` 挑基础镜像 → 选满足 cpu/内存/GPU 的**最小** SKU →
幂等地建模板 → 启动 sandbox（超时自动销毁兜底）→ 尽力在 sandbox 内装 playground CLI。

可选环境变量：

| 变量 | 默认 | 说明 |
|------|------|------|
| `ZERO_LBG_TIMEOUT` | `43200`（12h） | sandbox 自动销毁寿命（秒）；过短会导致长训练中途 `Paused sandbox not found` |
| `ZERO_LBG_EXTRA_DISK_GB` | `0` | 在默认 30Gi 之上追加的 overlay 磁盘 |
| `ZERO_LBG_PROJECT_ID` | 空 | 按该 project 计费；**也是 `snapshot()` 真正调用 image commit 的前提** |
| `ZERO_LBG_IMAGE_WAIT_TIMEOUT` | `1800` | 任务结束等待 `imageUrl` 的最长秒数；`0` 只记 commit id |
| `ZERO_LBG_SPAWN_WAIT_TIMEOUT` | `300` | `publish_manifest` 等待镜像后再 spawn exp；超时则 freeze 重装 |
| `ZERO_PIP_INDEX_URL` | 清华源 | 创建时写入 sandbox 的 pip 镜像（置空则不改） |
| `ZERO_LBG_BIN` | `lbg` | lbg 可执行路径 |
| `ZERO_BOHR_BIN` | `/root/.bohrium/bohr` | bohr 可执行路径 |
| `ZERO_TRISOL_INSTALL_URL` | `https://trisol.dp.tech/install.sh` | LBG sandbox 内安装 Trisol 的 HTTPS 脚本 |
| `TRISOL_TOKEN` | 空 | Trisol 非交互认证 token；仅注入 sandbox 子进程，不写入任务产物 |
| `TRISOL_TEAM` | 空 | Trisol 资产团队 ID 或名称；等价于全局 `--team`，不是 Bohrium project id |
| `LITERATURE_SAGE_PROXY_URL` | 空 | Literature Sage 专用 HTTP CONNECT 代理；不影响 Trisol/S3 传输 |
| `DEPLOY_MASTER_BASE_URL` | 空 | Tool 未命中时使用的 Deploy Master API；为空会显式禁用构建 |

---

## 4. 轨迹查看器（独立于运行）

轨迹**播放**和**运行**是两套系统：默认 `zero run` **不**拉起查看器。
查看器默认打开 **run 列表**（`http://<host>:8901/`）：点某一行进入
**Segment Inspector**（左脊柱按阻塞交接切段，右栏展开模型/状态）。

```bash
zero viewer                 # 默认 host/port
zero viewer --port 9000
```

| 变量 | 默认 | 说明 |
|------|------|------|
| `ZERO_TRACE_UI_HOST` | `0.0.0.0` | 监听地址 |
| `ZERO_TRACE_UI_PORT` | `8901` | 端口 |

跑任务时顺带看实时轨迹：给 `zero run` 加 `--trace-ui`。

父级 `reports/` 下的静态 HTML 报告是人工/外部产物，**不是** `zero viewer` 生成的。

---

## 5. 产物与目录

**一次任务 = 一个大文件夹** `runs/<run名>/`（默认 `task-20260724-abc123`，或 `--run-name`）。

### 系统根目录（`ZERO_ROOT`）

| 路径 | 角色 |
|------|------|
| `<ZERO_ROOT>/` | `runs/` / `agent_skills/` / `tasks/` / `experience/` 所在根 |
| `agent_skills/{researcher,labwright,teacher}/` | 各 Agent Skills（人工发布） |
| `experience/researcher/` | Researcher 轻量经验库（跨 run） |
| `tasks/` | 题包（共享输入） |
| `runs/<run名>/` | **唯一**运行产物根 |

跨 run 共享：`agent_skills/`、`experience/researcher/`、`tasks/`、代码。无 `.zero_state/`。

### `runs/<run名>/` 布局

```
runs/<run名>/
├── run.json              # 结束元数据（状态/后端/sandbox/路径索引）
├── conclusion.md         # 最终科学结论（若有）
├── resolved_task.md      # 原题 + 本次权威科学订正
├── environment.json      # 已验证环境、snapshot/commit 与可用时的 imageUrl
├── task_package/         # Live 题包（唯一真相；Teacher 热更新写回）
├── package_revisions/    # 题包 revision 快照 + CHANGELOG
├── finalized_task/       # 结题冻结的定稿题包
├── grading/
│   ├── result.json       # Harbor 打分信封
│   ├── reward.txt / breakdown.json
│   └── outputs/          # 必要时从 /app/outputs 拉回
├── optimized_task/       # 与 finalized 同步的兼容输出（含 OPTIMIZATION 历史）
├── environment/
│   ├── environment.md    # 人类可读环境清单
│   ├── inventory.json
│   └── image.json
├── environment.json      # 索引（manifest + imageUrl + 指向上面）
├── workspace/            # 宿主侧工作区（三 Agent 的 cwd）
├── deliverables/
│   ├── repo/             # 从 sandbox export/repo 拉回（若有）
│   └── output/           # 从 sandbox export/output 拉回（若有）
├── trace/
│   ├── researcher.jsonl  # Layer 1：模型调用
│   ├── labwright.jsonl
│   ├── teacher.jsonl     # 没问过则不存在
│   └── events.jsonl      # Layer 2：编排事件
├── teacher/
│   ├── asks.jsonl
│   ├── task_addendum.md
│   ├── completion_review.json
│   └── hint_bank/        # 本次 Teacher 专用 hint；Researcher 看不到
├── resources/            # 本 run 资源下载/缓存
├── resources.lock.json   # 实际使用的跨题资源、不可变制品引用与验证证据
├── assets.json           # 统一交付索引：题目镜像、工具镜像、数据/模型 Trisol ID 与版本
├── sandboxes/            # local 后端 venv（若用）
├── logs/                 # 含 capgw.log
└── meta/
    ├── task.json         # 本 run 生命周期状态（JSON 文件，非 SQLite）
    ├── skill_candidates/
    └── experience_writes.jsonl
```

`--run-name`：若该文件夹已有且 `run.json` 为 **已完成**，需删目录或换名；中断/失败的同名 run **直接复用**同一文件夹。

### 交付物注意

- `deliverables/` 只拉取 Researcher 在 sandbox 里整理的 `export/repo` + `export/output`。
- 题面要求写到 `/app/outputs/...` 时，**不会**自动等于 `deliverables/output/`；需要 Researcher 再整理进 `export/`，或任务结束后用 `scripts/pull_outputs.py` 等手段从仍存活的 sandbox 补拉。
- `run.json` 里的 `has_repo` / `has_output` / `export_source` 标明实际拉到了什么；`task_completed` **不**保证 deliverables 非空。
- 过滤 `__pycache__` / `.git` 以及超过 `ZERO_EXPORT_MAX_FILE_MB`（默认 50MB）的单文件。
- `--no-export` 只跳过这次拉取，不影响 `trace/` / `resolved_task.md` / `environment.json` / `run.json`。

### 复用产物：最终题面与环境镜像

| 文件 | 内容 |
|------|------|
| `resolved_task.md` | 原始题面 + 本次全部 `TASK_AMENDMENT` |
| `environment.json` | 已验证环境的 manifest，以及镜像提交状态 / `imageUrl` |
| `assets.json` | 单点读取题目镜像 URI、工具 OCI URI/digest、数据/模型 Trisol ID/team/version/splits |

Snapshot 在 Labwright **`publish_manifest` 时**创建（交给 Researcher 使用该环境之前），不以实验输出为镜像内容。

### 跨题资源目录：Library First

Labwright 对每个 tool/model/dataset 先调用 Literature Sage 的 Search + Detail
查询。目录命中只代表“发现候选”，不代表资源可用：tool 仍须在本题环境验证；
model/dataset 优先把 `trisol_id` 解析为带 team/version/splits 的固定 URI，下载真实字节、
计算完整 SHA-256、只读挂载并
执行加载/读取检查。未命中的 tool 可交给 Deploy Master 构建 OCI 镜像；目录写入只保存
元数据和稳定制品引用。新数据/模型先上传 Trisol，再把返回 ID 写入 Literature Sage；
新工具由 Deploy Master 推送 OCI Registry，再把镜像 URI/digest 写入 Literature Sage。

通过验证后，Labwright 写 `resources.lock.json`；其中每个必需资源都包含来源、制品
URI/digest 和验证证据。`publish_manifest` 会拒绝缺 lock 或验证失败的必需资源，并把
lock digest 同时写入 Manifest 和 environment inventory。正式发布可设置
`ZERO_RESOURCE_RELEASE_STRICT=1`，要求制品引用带不可变 digest。

相关配置见 `.env.example`。Registry 不可用时会留下显式降级记录；写入超时会先按
unique key 查询确认结果，不会盲目重试或覆盖已有资源。

`environment.json` 的 `snapshot_scope`：

| 值 | 含义 |
|----|------|
| `environment_baseline` | Orchestrator 在 Researcher 启动前成功封存了 READY baseline |
| `latest_published_manifest_fallback` | 启动时还没有 READY（常见 CLI 路径）；结束时用最近发布的 READY manifest |

LBG 需要 `ZERO_LBG_PROJECT_ID` 才能生成可复用镜像；未设置时会标为不可发布的 reproducibility digest。

---

## 6. Teacher：Preflight、live 题包与结题

三条时间线：Preflight → 解题中 HINT/amend → 结题冻结 `finalized_task/`（+ `optimized_task/`）。
修包门槛完整写在 `agent_skills/teacher/skills/teaching/SKILL.md`，不依赖外部仓库。
结题是完善题包，不是抬本次分数。Researcher 只见 `package_delta`。

### 写 hint

```bash
# 1) 预放（先定 --run-name）
mkdir -p runs/myrun/teacher/hint_bank
echo '- check the RDF first peak' > runs/myrun/teacher/hint_bank/notes.md
zero run tasks/foo --run-name myrun

# 2) 启动时拷入 / 覆盖自动 paper/
zero run tasks/foo --run-name foo --hints tasks/foo/paper/paper.md
```

`zero run <package-dir>` 读 `instruction.md` 进 live `task_package/`；有 `tests/` 则打分 + Teacher 可阅；有 `paper/` 则默认进 hint bank。

### 回答种类

| 判断 | 回答 | 落地 |
|------|------|------|
| 科学题面有缺陷 | `TASK_AMENDMENT` | live 包 + addendum → `resolved_task.md` |
| grader / 阈值问题 | `GRADER_AMENDMENT` | live `tests/`（须过 lint） |
| 题面+可验证表面同改 | `BOTH_AMENDMENT` | 同 revision |
| 题包没问题 | `HINT` | 对话 |
| 环境类 | `NO_HELP` | Labwright |

结题：Harbor 打分 → Teacher `review_completion` → 冻结定稿。

| 变量 | 默认 | 说明 |
|------|------|------|
| `ZERO_TEACHER_ENABLED` | `1` | 或 `--no-teacher` |
| `ZERO_TEACHER_MAX_ASKS` | `8` | 解题提问预算 |
| `ZERO_TEACHER_PREFLIGHT` | `1` | 开跑前 Preflight |
| `ZERO_PACKAGE_MAX_REVISIONS` | `12` | live revision 上限 |

### 环境清单与镜像

Labwright 在 `publish_manifest` 时写入：

```
runs/<run>/environment/
  environment.md     # 人类可读（近似 Dockerfile 说明）
  inventory.json
  pip-freeze.txt
  tools.txt
  mounts.json
  image.json         # 结束时补齐 imageUrl
```

`environment.json` 为索引。LBG 需 `ZERO_LBG_PROJECT_ID` 才有可复用 URL。

**双 sandbox（P2）**：Labwright 在 **env** sandbox 装依赖并冻结，再自动 spawn **exp**
sandbox 给 Researcher；实验写入不会进发布镜像。中途加包需新开 env 再 publish。
发布等待镜像：`ZERO_LBG_SPAWN_WAIT_TIMEOUT`（默认 300）。
---

## 7. 题包结构（`tasks/`）

`zero run` **只接受题包目录**（必须有 `instruction.md`）：

```
tasks/<name>/
├── instruction.md      # Researcher 题面（必填）
├── paper/paper.md      # 默认 Teacher hints（可选）
├── task.toml           # 外部超时 / 镜像元数据等（CLI 不强制读）
├── task_spec.json / steps.json / resources.json / difficulty.json
├── environment/        # 参考 Dockerfile 等
├── tests/              # Harbor 打分（有 checker.py 则评分 + Teacher 可阅）
└── solution/           # 参考解
```

要把题包元数据、starter、校验接进运行链路，可实现 `ExternalTaskPreparer` 并经
`Orchestrator` / `ZeroRuntime` 注入（见 AGENT.md）。

---

## 8. 常用配置一览

| 变量 | 默认 | 说明 |
|------|------|------|
| `ZERO_ENV_FILE` | `<包>/.env` | 预加载进 `os.environ` 的 dotenv |
| `ZERO_ROOT` | 本包根 | 系统根（`runs/` / `agent_skills/` / `tasks/` …）；monorepo 请指到父级 |
| `ZERO_LLM_ENV` | `<包>/.env` | 上游模型配置（`LLM_*`） |
| `ZERO_BOHRIUM_ENV` | 同 `ZERO_LLM_ENV` 逻辑 | Bohrium key 文件（可与上者拆开） |
| `ZERO_MODEL` | `claude-sonnet-4` | 交给 Claude Code 的模型名 |
| `ZERO_CAPGW_URL` | `http://127.0.0.1:8900` | 已有 capgw 时的 URL |
| `ZERO_CAPGW_PORT` | `8900` | 共享（默认）或独占模式下的端口 |
| `ZERO_CAPGW_SHARED` | `1` | `1`：多 run 共用一端口（推荐）；`0`：独占，结束即停 |
| `ZERO_CAPGW_WATCHDOG_SEC` | `15` | 共享/独占模式下健康检查与自愈间隔（秒） |
| `ZERO_SANDBOX_BACKEND` | `auto` | `auto` / `local` / `docker` / `lbg` |
| `ZERO_DOCKER_BASE_IMAGE` | `python:3.11-slim` | docker 后端基础镜像 |
| `ZERO_RUNS_DIR` | `<root>/runs` | 运行产物根 |
| `ZERO_RESEARCHER_SKILLS` | `<root>/agent_skills/researcher` | Researcher 插件根 |
| `ZERO_LABWRIGHT_SKILLS` | `<root>/agent_skills/labwright` | Labwright 插件根 |
| `ZERO_TEACHER_SKILLS` | `<root>/agent_skills/teacher` | Teacher 插件根 |
| `ZERO_EXPERIENCE_DIR` | `<root>/experience/researcher` | 经验库 |
| `ZERO_EXPORT_MAX_FILE_MB` | `50` | 拉取 deliverables 单文件上限 |
| `ZERO_TRACE_UI_HOST` / `PORT` | `0.0.0.0` / `8901` | 查看器 |
| `ZERO_TEACHER_ENABLED` / `MAX_ASKS` | `1` / `8` | Teacher 开关 / 解题提问预算 |
| `ZERO_TEACHER_PREFLIGHT` | `1` | 开跑前 Teacher Preflight |
| `ZERO_PACKAGE_MAX_REVISIONS` | `12` | live 题包 revision 上限 |
| LBG 相关 | 见 [§3](#使用-bohrium-云-sandboxlbg) | `ZERO_LBG_*` / `ZERO_PIP_INDEX_URL` / `ZERO_BOHR_BIN` |

`zero info` 会打印当前解析结果，排错时先看它。

---

## 9. 排错

- **`capgw gateway did not come up`**：检查 `.env` 里的 `LLM_*`，或先手动
  `capgw serve --env-file .env --port 8900`，看 `runs/<run>/logs/capgw.log`。
- **模型 429 / 502**：上游限流或网关瞬时不可用；lbg 创建时的瞬时 502 已有重试。
- **lbg 报 `no Bohrium access key`**：见 [§3](#使用-bohrium-云-sandboxlbg) 的 key 解析顺序。
- **`Permission denied` on `/app` or `/workspace`**：用 root 进 sandbox（见 `sandbox-root` Skill）；环境类问题找 Labwright，不要问 Teacher。
- **`task_completed` 但 `deliverables/` 空**：检查 sandbox 是否只把文件写到了 `/app/outputs` 而未整理进 `export/`。
- **Researcher 想直接 `pip install`/`apt`/`docker`**：允许执行；为可复现，常规依赖仍建议经 `ensure_environment` 声明给 Labwright。

---

## 10. Researcher 经验库

跨 run 的**短教训**库，由 Researcher 调用 MCP **自己决定**是否入库；写入后立刻可被后续 run 检索。

```
experience/researcher/
├── index.jsonl
└── entries/<id>.md
```

| 工具 | 作用 |
|------|------|
| `mcp__experience__search_experience` | 按关键词 / tags 检索 |
| `mcp__experience__get_experience` | 按 id 读全文 |
| `mcp__experience__record_experience` | 写入共享库（轻校验：拒密钥、绝对路径、task/sandbox id） |

短经验 → 经验库（直写）；长流程 → `propose_reusable_skill`（人工审核）。
禁止入库：本题最终数值、一次性路径、装环境细节。

本次写入另记在 `runs/<run>/meta/experience_writes.jsonl`。

---

## 11. 可审核的 Agent Skills

Researcher 与 Labwright 都可以提出可复用的 Skill 候选，但不能直接改写正在加载的 Skill 目录。

当前已发布、默认会加载的 Skills：

| Role | Skills |
|------|--------|
| Researcher | `ask-teacher`、`lbg-cli`、`sandbox-root` |
| Labwright | `lbg-cli`、`sandbox-root` |
| Teacher | `teaching` |

候选保存在 `runs/<run>/meta/skill_candidates/`，校验通过后人工发布：

```bash
zero skills list
zero skills validate researcher <candidate-id>
zero skills publish researcher <candidate-id>
zero skills reject labwright <candidate-id> --reason "too task-specific"
```

发布到 `agent_skills/<role>/skills/<name>/`；已有 Skill 更新会自动备份。

---

## 12. 项目结构

包代码在 `zero/zero/`（安装后 import 名为 `zero`），同级还有内置网关 `capgw/`：

```
zero/                      # 本包（pip install -e .）
├── README.md / AGENT.md
├── scripts/               # teacher / experience / lbg 冒烟与辅助脚本
├── capgw/                 # 捕获网关 → runs/<id>/trace/*.jsonl
└── zero/                  # Python 包
    ├── cli.py             # zero run / viewer / skills / info
    ├── config.py
    ├── runtime.py         # ZeroRuntime：共享 capgw 的并发集成
    ├── preparation.py     # ExternalTaskPreparer 协议
    ├── export.py
    ├── experience/
    ├── orchestrator/
    ├── researcher/ labwright/ teacher/
    ├── sandbox/           # local / docker / lbg
    ├── protocol/
    ├── trace/             # Layer2 + 查看器 static/
    ├── skills/
    ├── resources/
    └── state/             # 每 run meta/task.json（非 SQLite）
```

设计细节见 [`AGENT.md`](./AGENT.md)。
