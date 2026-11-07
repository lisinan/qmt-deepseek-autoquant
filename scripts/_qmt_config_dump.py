# -*- coding: utf-8 -*-
"""只读：把 indexUserConfig.xml 里 strategyTrade 各策略的关键开关列成表。"""
import re
import sys

PATH = r"C:\pazq_qmt\config\indexUserConfig.xml"

KEYS = ["name", "runMode", "accountType", "peroid", "eStrategyType",
        "startupAutorun", "specialType", "timeInterval", "account"]

with open(PATH, "r", encoding="utf-8", errors="replace") as f:
    text = f.read()

rows = []
for m in re.finditer(r"<item\s([^>]*?)/?>", text):
    attrs = m.group(1)
    d = {}
    for k in KEYS:
        mm = re.search(r'\b%s="([^"]*)"' % k, attrs)
        d[k] = mm.group(1) if mm else ""
    if d["name"]:
        rows.append(d)

print("strategyTrade 条目数: %d" % len(rows))
hdr = "%-32s %-8s %-6s %-9s %-6s %-6s" % (
    "name", "runMode", "acctType", "peroid", "eStrat", "autorun")
print(hdr)
print("-" * len(hdr))
for d in rows:
    print("%-32s %-8s %-6s %-9s %-6s %-6s" % (
        d["name"][:32], d["runMode"], d["accountType"], d["peroid"],
        d["eStrategyType"], d["startupAutorun"]))

print("")
print("=== catalog 里 simpleRun 与本策略同目录的项 ===")
for m in re.finditer(r"<catalog\s([^>]*?)/?>", text):
    attrs = m.group(1)
    mm = re.search(r'name="([^"]*)"', attrs)
    if not mm:
        continue
    nm = mm.group(1)
    sr = re.search(r'simpleRun="([^"]*)"', attrs)
    ft = re.search(r'formulaCatalogModelType="([^"]*)"', attrs)
    ty = re.search(r'type="([^"]*)"', attrs)
    if ty and ty.group(1) == "2" and ft and ft.group(1) == "4":
        print("  %-30s simpleRun=%s" % (nm[:30], sr.group(1) if sr else "?"))
