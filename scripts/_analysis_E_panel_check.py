# -*- coding: utf-8 -*-
"""E_data_governance 核查脚本 Part1: 日线面板核查(新旧对比)
新面板结构: dict[ts_code] -> {'date': [str], 'open/high/low/close/volume': [float]}
只读, 不修改任何数据文件。
"""
import pickle
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

NEW = ROOT / "storage" / "hist_cache" / "ts_panel_25_901_20211201_20261027.pkl"
OLD = ROOT / "storage" / "hist_cache" / "ts_panel_23_901_20211201_20261016.pkl"

POOL23 = ["300308.SZ", "300502.SZ", "300394.SZ", "300570.SZ", "688256.SH",
          "688041.SH", "300474.SZ", "688008.SH", "688981.SH", "603986.SH",
          "002371.SZ", "000977.SZ", "603019.SH", "002415.SZ", "300383.SZ",
          "002916.SZ", "300476.SZ", "603881.SH", "002230.SZ", "688111.SH",
          "300033.SZ", "300496.SZ", "688012.SH"]
IDX3 = ["000001.SH", "399006.SZ", "000300.SH"]
EXPECT25 = POOL23 + IDX3
FIELDS = ("open", "high", "low", "close", "volume")

def load(p):
    return pickle.loads(p.read_bytes())

new = load(NEW)
old = load(OLD)

print("=== 新面板基本信息 ===")
codes_new = sorted(new.keys())
print("n_codes:", len(codes_new))
lens = {c: len(new[c]["date"]) for c in codes_new}
print("各代码日期数 min/max:", min(lens.values()), max(lens.values()))
all_dates = sorted(set(d for c in codes_new for d in new[c]["date"]))
print("union first/last:", all_dates[0], all_dates[-1], " union_n:", len(all_dates))
missing = [c for c in EXPECT25 if c not in codes_new]
extra = [c for c in codes_new if c not in EXPECT25]
print("missing_codes:", missing)
print("extra_codes:", extra)

print("\n=== 旧面板基本信息 ===")
codes_old = sorted(old.keys())
print("n_codes:", len(codes_old))
old_dates = sorted(set(d for c in codes_old for d in old[c]["date"]))
print("old first/last:", old_dates[0], old_dates[-1])
print("old_codes:", codes_old)

# 以 union 日期轴检查
d = all_dates
print("\n=== 日期轴检查(union) ===")
print("is_ascending:", d == sorted(d), " is_unique:", len(d) == len(set(d)))
print("last_date == 20261027:", d[-1] == "20261027")

print("\n=== 连续性检查(union, 自然日缺口) ===")
def to_dt(s):
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
gaps = []
for i in range(1, len(d)):
    gap = (to_dt(d[i]) - to_dt(d[i-1])).days
    if gap > 1:
        gaps.append((d[i-1], d[i], gap))
print("gap>1 次数:", len(gaps), " 最大自然日缺口:", max((g[2] for g in gaps), default=0))
big = [g for g in gaps if g[2] > 10]
print("单次缺口>10自然日:", big)
print("最后12个union日期:", d[-12:])
# 最近一段小缺口明细
print("最近5个gap:", [(g[0], g[1], g[2]) for g in gaps[-5:]])

print("\n=== OHLC 合理性(新面板) ===")
bad_hl, bad_price, dup_d = [], [], []
for c in codes_new:
    dd, o, h, l, cl = (new[c]["date"], new[c]["open"], new[c]["high"],
                       new[c]["low"], new[c]["close"])
    if len(dd) != len(set(dd)):
        dup_d.append(c)
    for i in range(len(dd)):
        oi, hi, li, ci = o[i], h[i], l[i], cl[i]
        if any(x is None for x in (oi, hi, li, ci)):
            continue
        try:
            oi, hi, li, ci = float(oi), float(hi), float(li), float(ci)
        except Exception:
            continue
        if not (hi >= max(oi, ci) and li <= min(oi, ci)):
            bad_hl.append((c, dd[i], oi, hi, li, ci))
        if not (oi > 0 and hi > 0 and li > 0 and ci > 0):
            bad_price.append((c, dd[i], oi, hi, li, ci))
print("重复日期代码:", dup_d)
print("OHLC关系违规条数:", len(bad_hl), bad_hl[:5])
print("非正价格条数:", len(bad_price), bad_price[:5])
for c in codes_new:
    dd = new[c]["date"]
    print(f"  {c}: n={len(dd)}, first={dd[0]}, last={dd[-1]}, close_last={new[c]['close'][-1]}")

print("\n=== 新旧面板逐位一致性(<=20261016) ===")
overlap = [x for x in d if x <= "20261016"]
print("union重叠日期数:", len(overlap))
diffs = []
for c in codes_old:
    if c not in new:
        diffs.append((c, "MISSING_IN_NEW"))
        continue
    o_idx = {dd: i for i, dd in enumerate(old[c]["date"])}
    n_idx = {dd: i for i, dd in enumerate(new[c]["date"])}
    for fld in FIELDS:
        ov, nv = old[c][fld], new[c][fld]
        for dd in overlap:
            if dd not in o_idx or dd not in n_idx:
                continue
            a, b = ov[o_idx[dd]], nv[n_idx[dd]]
            if a is None and b is None:
                continue
            if a is None or b is None:
                diffs.append((c, dd, fld, a, b)); continue
            if float(a) != float(b):
                diffs.append((c, dd, fld, a, b))
print("重叠期差异条数:", len(diffs))
for x in diffs[:20]:
    print("  DIFF:", x)
# 旧面板独有日期(应无, 因新面板起点更早或相同)
old_only = [x for x in old_dates if x not in set(d)]
print("旧面板有而新面板无的日期:", old_only)

print("\n=== 新面板新增日期段(>20261016) ===")
new_only = [x for x in d if x > "20261016"]
print("新增日期数:", len(new_only), " 区间:", new_only[0], "->", new_only[-1])
print("列表:", new_only)
print("新面板多出代码:", [c for c in codes_new if c not in codes_old])
# 每个代码在新增段的实际数据覆盖
for c in codes_new:
    dd = set(new[c]["date"])
    cov = sum(1 for x in new_only if x in dd)
    print(f"  {c}: 新增段覆盖 {cov}/{len(new_only)}")
