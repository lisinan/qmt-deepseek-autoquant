# -*- coding: utf-8 -*-
"""
OpenRouter AI 连接自检脚本

用法（在 conda env qmt 中）：
  python test_ai.py

检测：
  - OPENROUTER_API_KEY 是否配置
  - 主模型联通性（chat）
  - JSON 模式（chat_json）
  - 免费模型列表（list_free_models）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Windows 控制台 UTF-8
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai.analyst import ai_analyst
from ai.openrouter_client import openrouter


def main() -> int:
    print("=" * 60)
    print("qmtIDE-deepseek OpenRouter AI 自检")
    print("=" * 60)

    print(f"\n[配置] API key 配置: {'是' if openrouter.enabled else '否'}")
    print(f"[配置] 模型: {openrouter.model}")
    print(f"[配置] 端点: {openrouter.base_url}")

    if not openrouter.enabled:
        print("\n⚠ 未检测到 OPENROUTER_API_KEY 环境变量")
        print("  设置方式：")
        print('    PowerShell:  $env:OPENROUTER_API_KEY="sk-or-..."')
        print('    CMD:         set OPENROUTER_API_KEY=sk-or-...')
        print("  无 key 时 AI 层会自动跳过，主策略照常运行。")
        return 0

    # 1) 简单 chat
    print("\n[1/3] 基础 chat 调用...")
    resp = openrouter.chat(
        messages=[
            {"role": "system", "content": "你是一个简洁的助手。"},
            {"role": "user", "content": "用 20 字以内回答：A股 T+1 是什么意思？"},
        ],
        max_tokens=100,
    )
    if resp:
        print(f"  ✅ 模型 {resp['model']}: {resp['content']}")
        print(f"  用量: {resp.get('usage')}")
    else:
        print("  ❌ 失败（网络/超时/4xx）")
        return 1

    # 2) JSON 模式
    print("\n[2/3] JSON 模式...")
    parsed = openrouter.chat_json(
        messages=[
            {"role": "system",
             "content": "只输出 JSON：{\"ans\": \"你的简短回答\"}"},
            {"role": "user", "content": "1+1=?"},
        ],
        max_tokens=50,
    )
    if parsed:
        print(f"  ✅ 解析成功: {parsed}")
    else:
        print("  ❌ 失败")
        return 1

    # 3) 免费模型列表
    print("\n[3/3] 拉取免费模型列表...")
    models = openrouter.list_free_models()
    if models:
        print(f"  ✅ 找到 {len(models)} 个免费模型")
        for m in models[:8]:
            print(f"    - {m}")
        if len(models) > 8:
            print(f"    ... 还有 {len(models) - 8} 个")
    else:
        print("  ⚠ 列表为空或网络失败")

    # 4) AIAnalyst 集成测试
    print("\n[集成] AIAnalyst.analyze() ...")
    ind = {
        "ma5": 12.5, "ma10": 12.0, "ma20": 11.5,
        "ma20_slope": 0.05,
        "rsi": 55.0,
        "macd_dif": 0.2, "macd_dea": 0.1, "macd_hist": 0.1,
        "kdj_j": 60.0,
        "boll_upper": 14.0, "boll_lower": 11.0, "boll_mid": 12.5,
        "atr": 0.3, "vwap": 12.3,
        "close": 12.8, "change_pct": 1.5, "score": 5.5,
    }
    ctx = {"index_name": "创业板指", "index_change_pct": 0.5,
           "index_trend": "上行"}
    ai = ai_analyst.analyze("300308.SZ", "中际旭创", ind, ctx)
    if ai:
        print(f"  ✅ stance={ai.stance}  conf={ai.confidence:.2f}")
        print(f"     summary: {ai.summary}")
        print(f"     risks:   {ai.risks}")
        print(f"     cached:  {ai.cached}")
    else:
        print("  ❌ analyze 返回 None")

    print("\n" + "=" * 60)
    print("✅ AI 自检完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())