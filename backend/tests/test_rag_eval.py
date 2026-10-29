# 真实模型评测集的 pytest 入口（默认跳过）
# ---------------------------------------------------------------------
# 为什么不放进常规 CI：
#   这组用例要真调 DeepSeek + 真打向量接口，有费用、有网络依赖、耗时几十秒，
#   放进每次提交的回归会让人为了"跑绿"而去改断言，反而失去意义。
#   默认跳过，需要时显式开启：
#       RUN_RAG_EVAL=1 pytest tests/test_rag_eval.py -q
#   建议挂在夜间任务上——它守的是「回答形态」这条契约，
#   而这恰恰是纯单测（LLM 被 mock）覆盖不到的部分。

import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent
EVAL_SCRIPT = BACKEND_ROOT / "scripts" / "run_rag_eval.py"

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_RAG_EVAL") != "1",
    reason="真实模型评测：需 RUN_RAG_EVAL=1 且配置可用 API Key（会产生调用费用）",
)


def test_rag_answer_contract_eval():
    """跑完整评测集：检索模式、引用一致性、禁止项、必需项四类断言全部通过。"""
    # 用子进程而不是 import：评测脚本自带 sys.path 处理与命令行入口，复用同一份实现
    proc = subprocess.run(
        [sys.executable, str(EVAL_SCRIPT)],
        cwd=str(BACKEND_ROOT),
        capture_output=True,
        text=True,
        timeout=600,
    )

    # 失败时把完整 stdout/stderr 带进断言信息，否则只有一句 returncode 无法定位
    assert proc.returncode == 0, f"评测未通过：\n{proc.stdout}\n{proc.stderr}"
