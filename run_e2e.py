"""End-to-end smoke: a real CPU-only research task through the full dual-agent loop."""

import asyncio
import json
import time

from zero.orchestrator.orchestrator import Orchestrator

TASK = (
    "科研任务：使用 scikit-learn 内置的 iris 数据集，训练一个逻辑回归分类器，"
    "在留出测试集上报告准确率，并验证该分类器是否显著优于随机猜测基线（三类，随机基线约 33%）。"
    "请先声明所需的环境依赖，再编写并运行实验代码，最后给出明确的科学结论。"
)


async def main():
    orch = Orchestrator(manage_capgw=True)
    try:
        result = await orch.run_task(TASK, max_turns=60)
    finally:
        orch.close()

    print("\n" + "=" * 70)
    print("TASK:", result.task_id, "| status:", result.status, "| backend:", result.backend)
    print("sandbox_ids:", result.sandbox_ids)
    print("hook interceptions:", result.interceptions)
    print("-" * 70)
    print("CONCLUSION (conclusion.md):\n", result.conclusion or "(none)")
    print("-" * 70)
    print("FINAL TEXT:\n", result.final_text[:2000])
    print("-" * 70)
    print("TRACE INDEX:\n", json.dumps(result.trace_index, ensure_ascii=False, indent=2))

    if orch.viewer_url:
        print("-" * 70)
        print(f"[trace] 查看器仍在运行: {orch.viewer_url}  （按 Ctrl-C 退出）")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            orch.stop_viewer()


if __name__ == "__main__":
    asyncio.run(main())
