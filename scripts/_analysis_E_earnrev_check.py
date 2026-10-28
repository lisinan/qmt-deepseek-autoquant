# -*- coding: utf-8 -*-
"""E_data_governance 核查脚本 Part3: earnrev 缓存诊断 + 安全补齐
诊断: 2字节空文件是否为正常设计结果
修复: 仅对缺失文件 002415.SZ 调用 preload_earnrev(只追加创建, 不覆盖已有文件)
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
CACHE = ROOT / "data" / "cache"

POOL23 = ["300308.SZ", "300502.SZ", "300394.SZ", "300570.SZ", "688256.SH",
          "688041.SH", "300474.SZ", "688008.SH", "688981.SH", "603986.SH",
          "002371.SZ", "000977.SZ", "603019.SH", "002415.SZ", "300383.SZ",
          "002916.SZ", "300476.SZ", "603881.SH", "002230.SZ", "688111.SH",
          "300033.SZ", "300496.SZ", "688012.SH"]

print("=== earnrev 缓存现状(修复前) ===")
empty_files, content_files, missing = [], [], []
for c in POOL23:
    f = CACHE / f"earnrev_{c}.json"
    if not f.exists():
        missing.append(c)
        continue
    raw = f.read_text(encoding="utf-8")
    try:
        obj = json.loads(raw)
    except Exception as e:
        print(f"{c}: JSON解析失败 {e} (bytes={len(raw.encode())})")
        continue
    if obj:
        content_files.append((c, obj))
    else:
        empty_files.append((c, len(raw.encode())))
print("空(合法{})文件数:", len(empty_files), " 大小集合:", set(s for _, s in empty_files))
print("有内容文件:")
for c, o in content_files:
    print("  ", c, o)
print("缺失文件:", missing)

print("\n=== 诊断结论依据(读 data/earnrev_cache.py) ===")
print("- get_earnrev: 缓存存在且可解析 -> 直接返回(零网络)")
print("- 缓存不存在/解析失败 -> tushare forecast_df 拉取; df为空/None -> 返回{}且不写文件")
print("- df非空 -> _rev_from_forecast 计算修正; 写文件(json.dumps(rev)), rev=={}时写出2字节'{}'")
print("- _rev_from_forecast: 仅当同一报告期(end_date)存在>=2次指引且p_change_min/max均有效时才产出修订")
print("=> 2字节'{}' = 已联网取到预告但无可计算修订事件(无重复期指引或p_change无效), 属正常缓存, 非写入bug")
print("=> 文件缺失 = 从未预加载 或 拉取返回None(df=None)时早退未写文件")

# 安全修复: 只为缺失代码创建缓存(文件不存在才写, 不动已有文件)
if missing:
    print("\n=== 安全修复: 预加载缺失代码 ===", missing)
    from data.earnrev_cache import preload_earnrev
    try:
        out = preload_earnrev(missing, start="20200101")
        for c in missing:
            f = CACHE / f"earnrev_{c}.json"
            if f.exists():
                raw = f.read_text(encoding="utf-8")
                print(f"修复后 {c}: 存在, {len(raw.encode())}字节, 内容={raw[:120]}")
            else:
                print(f"修复后 {c}: 仍不存在(tushare返回空/None, 按设计不写文件, 记录原因)")
        print("preload返回非空:", {k: v for k, v in out.items()})
    except Exception as e:
        print("修复失败(网络/接口异常), 记录原因:", repr(e))
else:
    print("\n无需修复: 23只均有缓存文件")

print("\n=== 修复后复检 ===")
n_empty = n_content = n_missing = 0
for c in POOL23:
    f = CACHE / f"earnrev_{c}.json"
    if not f.exists():
        n_missing += 1
        continue
    obj = json.loads(f.read_text(encoding="utf-8"))
    if obj:
        n_content += 1
    else:
        n_empty += 1
print(f"空文件: {n_empty}, 有内容: {n_content}, 仍缺失: {n_missing}")
