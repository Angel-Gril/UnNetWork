#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
diag_config.py —— 诊断配置里的悬空引用
=====================================
mihomo 启动时会校验：
  1. proxy-groups 里 proxies 列表引用的每个名字，必须在
     proxies 或 proxy-groups 里存在
  2. rules 里引用的策略，必须在 proxy-groups 或内置策略里存在
任何一条不满足就报错退出。

本脚本把这两类悬空引用全列出来。
"""
import re
import sys

BUILTIN = {"DIRECT", "REJECT", "REJECT-DROP", "PASS", "COMPATIBLE", "GLOBAL"}


def analyze(path):
    txt = open(path, encoding="utf-8", errors="replace").read()
    print(f"=== {path} ===")
    print(f"大小: {len(txt)} 字节\n")

    # 节点名
    nodes = re.findall(r'\{"name":"([^"]+)","type":"(socks5|http|ss|vmess|trojan|vless)"', txt)
    node_names = {n for n, _ in nodes}

    # 策略组：流式 { name: "X", type: "Y", proxies: [...] }
    groups = re.findall(
        r'\{ name: "([^"]+)", type: "([^"]+)", proxies: \[(.*?)\]',
        txt, re.S)
    group_names = {g[0] for g in groups}

    print(f"节点: {len(node_names)} 个")
    print(f"策略组: {len(group_names)} 个 -> {sorted(group_names)}\n")

    # --- 检查 1：组的成员引用 ---
    print("=== 检查 1: 策略组成员引用 ===")
    dangling_total = 0
    for name, typ, members in groups:
        member_list = re.findall(r'"([^"]+)"', members)
        bad = [m for m in member_list
               if m not in node_names and m not in group_names and m not in BUILTIN]
        if bad:
            print(f"  [!] {name} ({typ}) 有 {len(bad)} 个悬空引用:")
            for b in bad:
                print(f"        -> {b}")
            dangling_total += len(bad)
        if not member_list:
            print(f"  [!] {name} ({typ}) 成员为空")
    if not dangling_total:
        print("  OK")

    # --- 检查 2：rules 的策略引用 ---
    print("\n=== 检查 2: rules 的策略引用 ===")
    rules = re.findall(r'^\s+-\s+(\S+?),([^,\n]+?)(?:,(\S+))?$', txt, re.M)
    bad_rules = []
    for rule in rules:
        full = ",".join(x for x in rule if x)
        # 最后一跳是策略
        parts = full.split(",")
        if parts[0] in ("MATCH",):
            target = parts[1] if len(parts) > 1 else ""
        else:
            target = parts[-1]
        if target and target not in group_names and target not in BUILTIN:
            bad_rules.append((full[:70], target))
    if bad_rules:
        for full, t in bad_rules:
            print(f"  [!] {full}  -> 策略 {t} 不存在")
    else:
        print("  OK")

    # --- 检查 3：节点是否都在某个组里 ---
    print("\n=== 检查 3: 未被任何组引用的节点 ===")
    all_members = set()
    for _, _, members in groups:
        all_members.update(re.findall(r'"([^"]+)"', members))
    orphan = node_names - all_members
    if orphan:
        for o in sorted(orphan):
            print(f"  [!] {o}")
    else:
        print("  OK（所有节点都被至少一个组引用）")

    return dangling_total + len(bad_rules) + len(orphan)


if __name__ == "__main__":
    for p in sys.argv[1:]:
        analyze(p)
        print()
