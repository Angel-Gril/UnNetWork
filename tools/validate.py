#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
validate.py —— 用 YAML 解析器验证配置 + 模拟 mihomo 的校验逻辑
"""
import sys

try:
    import yaml
except ImportError:
    sys.exit("需要 pyyaml: pip install pyyaml")

BUILTIN = {"DIRECT", "REJECT", "REJECT-DROP", "PASS", "COMPATIBLE", "GLOBAL"}


def validate(path):
    print(f"=== {path} ===")
    raw = open(path, encoding="utf-8", errors="replace").read()

    # 1) 语法
    try:
        d = yaml.safe_load(raw)
        print(f"  YAML 语法: OK")
    except Exception as e:
        print(f"  YAML 语法: 失败 -> {e}")
        return

    if not isinstance(d, dict):
        print(f"  顶层不是 dict: {type(d)}")
        return

    # 2) 节点
    proxies = d.get("proxies") or []
    names = set()
    dup = []
    for p in proxies:
        n = p.get("name")
        if n in names:
            dup.append(n)
        names.add(n)
    print(f"  节点: {len(proxies)} 个" + (f"  重复名: {dup}" if dup else ""))

    # 检查节点必填字段
    for p in proxies:
        t = p.get("type")
        miss = [k for k in ("name", "type", "server", "port") if not p.get(k)]
        if miss:
            print(f"    [!] 节点缺字段 {miss}: {p.get('name')}")
        if t == "socks5":
            if "username" not in p:
                print(f"    [!] socks5 缺 username: {p.get('name')}")

    # 3) 策略组
    groups = d.get("proxy-groups") or []
    gnames = {g.get("name") for g in groups}
    print(f"  策略组: {len(groups)} 个")

    problems = 0
    for g in groups:
        gn = g.get("name")
        gt = g.get("type")
        members = g.get("proxies") or []
        if not members:
            print(f"    [!] {gn} 成员为空（mihomo 会报错）")
            problems += 1
            continue
        bad = [m for m in members if m not in names and m not in gnames and m not in BUILTIN]
        if bad:
            print(f"    [!] {gn} 悬空引用: {bad}")
            problems += 1
        # url-test 必须的字段
        if gt == "url-test" and not g.get("url"):
            print(f"    [!] {gt} 组 {gn} 缺 url")
            problems += 1

    # 4) 规则
    rules = d.get("rules") or []
    print(f"  规则: {len(rules)} 条")
    for r in rules:
        if not isinstance(r, str):
            continue
        parts = r.split(",")
        target = parts[1] if parts[0] == "MATCH" else parts[-1]
        # GEOIP,x,y,no-resolve 的最后一个不是策略
        if parts[-1] == "no-resolve":
            target = parts[-2] if len(parts) >= 2 else ""
        if target and target not in gnames and target not in BUILTIN:
            print(f"    [!] 规则 {r} -> 策略 {target} 不存在")
            problems += 1

    # 5) 国家组语义检查
    print("\n  国家组语义检查:")
    for g in groups:
        gn = g.get("name") or ""
        if gn.endswith("节点") and gn != "节点选择":
            members = g.get("proxies") or []
            # 看成员名是否符合该国家
            cc_map = {"香港节点": ["HK", "Hong"], "台湾节点": ["TW", "Taiwan"],
                      "新加坡节点": ["SG", "Singapore"], "日本节点": ["JP", "Japan"],
                      "美国节点": ["US", "USA"]}
            exp = cc_map.get(gn, [])
            if exp and members:
                hit = [m for m in members if any(m.startswith(p) for p in exp)]
                if not hit:
                    print(f"    [!] {gn} 里没有 {exp} 的节点，"
                          f"实际是 {members[0][:20]}... 等（选了会走错国家）")

    print(f"\n  问题总数: {problems}")


if __name__ == "__main__":
    for p in sys.argv[1:]:
        validate(p)
        print()
