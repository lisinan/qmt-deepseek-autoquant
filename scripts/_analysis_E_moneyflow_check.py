# -*- coding: utf-8 -*-
"""E_data_governance 核查脚本 Part2: 资金流缓存核查(只读)"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "cache"

POOL23 = ["300308.SZ", "300502.SZ", "300394.SZ", "300570.SZ", "688256.SH",
          "688041.SH", "300474.SZ", "688008.SH", "688981.SH", "603986.SH",
          "002371.SZ", "000977.SZ", "603019.SH", "002415.SZ", "300383.SZ",
          "002916.SZ", "300476.SZ", "603881.SH", "002230.SZ", "688111.SH",
          "300033.SZ", "300496.SZ", "688012.SH"]
FIELDS = ["buy_sm_vol", "buy_md_vol", "buy_lg_vol", "buy_elg_vol",
          "sell_sm_vol", "sell_md_vol", "sell_lg_vol", "sell_elg_vol",
          "net_mf_vol", "net_mf_amount"]

files = sorted(CACHE.glob("moneyflow_*.json"))
files = [f for f in files if not f.name.endswith(".bak-20261028")]
print("资金流文件数(不含.bak):", len(files))

rows = []
problems = []
for f in files:
    code = f.name[len("moneyflow_"):-len(".json")]
    series = json.loads(f.read_text(encoding="utf-8"))
    keys = sorted(series.keys())
    n = len(keys)
    first, last = (keys[0], keys[-1]) if keys else ("", "")
    # 字段完整性: 每个日期都应含全部10字段, 特别 net_mf_amount
    miss_field_days = 0
    miss_nma_days = 0
    nonfloat = 0
    for d, v in series.items():
        if not isinstance(v, dict) or any(k not in v for k in FIELDS):
            miss_field_days += 1
        if not isinstance(v, dict) or "net_mf_amount" not in v:
            miss_nma_days += 1
        elif not isinstance(v.get("net_mf_amount"), (int, float)):
            nonfloat += 1
    dup = n != len(set(keys))
    ok = (last == "20261027" and miss_field_days == 0 and miss_nma_days == 0
          and nonfloat == 0 and not dup)
    rows.append((code, n, first, last, miss_field_days, miss_nma_days, nonfloat, dup, ok))
    if not ok:
        problems.append((code, n, first, last, miss_field_days, miss_nma_days, nonfloat, dup))

print(f"{'code':<12}{'n':>6} {'first':>9} {'last':>9} {'缺字段日':>8} {'缺nmf日':>8} {'nmf非数':>7} {'重复':>4} OK")
for r in rows:
    print(f"{r[0]:<12}{r[1]:>6} {r[2]:>9} {r[3]:>9} {r[4]:>8} {r[5]:>8} {r[6]:>7} {str(r[7]):>5} {r[8]}")

have = {r[0] for r in rows}
miss_codes = [c for c in POOL23 if c not in have]
print("\n缺失文件代码:", miss_codes)
print("不达标文件:", problems if problems else "无")

# 备份文件对比(旧->新): 确认.bak确实更旧
print("\n=== .bak 备份对比(确认补齐动作) ===")
for c in ["688012.SH", "300308.SZ"]:
    cur = CACHE / f"moneyflow_{c}.json"
    bak = CACHE / f"moneyflow_{c}.json.bak-20261028"
    if bak.exists():
        s_new = json.loads(cur.read_text(encoding="utf-8"))
        s_old = json.loads(bak.read_text(encoding="utf-8"))
        nk = sorted(s_new.keys()); ok_ = sorted(s_old.keys())
        print(f"{c}: 现{nk[-1] if nk else '-'}(n={len(nk)}) vs 备份{ok_[-1] if ok_ else '-'}(n={len(ok_)}); "
              f"新增{len(set(nk)-set(ok_))}日; 重叠期一致={all(s_new[d]==s_old[d] for d in set(nk)&set(ok_))}")
# 全量备份一致性
allok = True
for f in files:
    bak = Path(str(f) + ".bak-20261028")
    if not bak.exists():
        continue
    s_new = json.loads(f.read_text(encoding="utf-8"))
    s_old = json.loads(bak.read_text(encoding="utf-8"))
    inter = set(s_new) & set(s_old)
    if not all(s_new[d] == s_old[d] for d in inter):
        allok = False
        print("重叠期不一致:", f.name)
print("全部23个备份重叠期一致性:", allok)

# 全局末端日期分布
from collections import Counter
print("\n末端日期分布:", Counter(r[3] for r in rows))
print("最早首日:", min(r[2] for r in rows))
