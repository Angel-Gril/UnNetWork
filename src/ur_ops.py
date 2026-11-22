#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ur_ops.py —— 全流程编排：多账号 × 5 槽位 × 按国家分类 → 订阅
==============================================================
一条命令把 N 个 key 的每个 5 个槽位全部拉满，按目标国家分配，
产出可直接当代理订阅用的配置。

核心逻辑
--------
1. 读 keys.txt（每行一个 API key）
2. 按 COUNTRY_PLAN 的比例，给每个 key 分配 5 个目标国家
3. 每个 (key, country) 调一次 POST /network/auth-client
4. 实测出口 IP，校验落地国家（`country_code` 只是偏好，会 fallback）
5. 全部汇总，按国家分组输出 mihomo / sing-box / URI / base64

限制与对策
----------
  每账号 5 个并发槽位  → 每 key 拉满 5 次即止
  每账号 5 次/分钟      → 每 key 内部间隔 12s
  IP 级 429             → 遇 429 退避 60s 重试
  出口国家会 fallback   → 严格校验，不符就删了重拉

用法
----
    # 全量：keys.txt 里所有 key，每个 5 槽位
    python ur_ops.py --keys keys.txt --out fleet

    # 先跑 3 个 key 验证
    python ur_ops.py --keys keys.txt --limit 3 --out fleet-test

    # 指定每国目标数（覆盖默认比例）
    python ur_ops.py --keys keys.txt --countries "US:3,CA:1,JP:1"

    # 只重新拉某个国家
    python ur_ops.py --keys keys.txt --only US --out fleet

产出
----
    fleet/
    ├── mihomo.yaml          每国一个 url-test 组 + GLOBAL 选择器
    ├── singbox.json         全部 outbound
    ├── nodes.txt            按国家分块的 URI
    ├── subscription.txt     base64 订阅
    ├── inventory.json       每节点的 key/country/ip/isp/as
    └── report.md            统计报告
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.parse
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ur_hunt import (issue, list_slots, free_slot, probe_exit, classify,
                     dump_yaml, _req)
import ur_hunt as H


# 每账号 5 槽位的国家分配比例（按 provider 池大小加权）
DEFAULT_PLAN = {
    "US": 2,   # 24918 个 provider，最稳
    "CA": 1,   # 3730
    "BR": 1,   # 1249
    "FR": 1,   # 783
}
# 备选池：当主池某个国家一直失败时轮换到这里
FALLBACK_PLAN = [
    {"US": 2, "CA": 1, "GB": 1, "DE": 1},
    {"US": 2, "FR": 1, "SE": 1, "NL": 1},
    {"US": 3, "CA": 1, "GB": 1},
    {"US": 2, "BR": 1, "SG": 1, "HK": 1},
]


class Node:
    __slots__ = ("key_idx", "key", "country", "want_country", "host",
                 "socks", "http", "https_port", "token", "block",
                 "ip", "isp", "asn", "proxy_flag", "hosting_flag", "city")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    def to_dict(self):
        return {k: getattr(self, k) for k in self.__slots__ if k != "key"}


def parse_plan(s):
    """'US:3,CA:1,JP:1' -> {'US':3,'CA':1,'JP':1}"""
    out = {}
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            c, n = part.split(":", 1)
            out[c.strip().upper()] = int(n)
        else:
            out[part.upper()] = 1
    return out


def provision_slot(key, country, debug=False, no_recycle=False):
    """给某个 key 拉一个指定国家的槽位，返回 Node 或 (None, reason)

    no_recycle=True 时：槽位已满就直接跳过，不回收已有节点
    （回收会让之前拉的节点失效——实测第一轮节点就是被这样挤掉的）
    """
    if no_recycle:
        slots = list_slots(key)
        if len(slots) >= 5:
            return None, "slots full (no-recycle)"
    else:
        free_slot(key, debug=debug)
    res = issue(key, country)
    pcr = res.get("proxy_config_result") or {}
    if not pcr:
        msg = (res.get("error") or {}).get("message", "unknown")
        return None, msg

    host = pcr.get("proxy_host")
    sport = pcr.get("socks_proxy_port")
    token = pcr.get("auth_token")
    if not (host and sport and token):
        return None, "incomplete config"

    time.sleep(3)
    info = probe_exit(host, sport, token)
    if not info or not info.get("query"):
        return None, "probe failed"

    ok, reason = classify(info, want_country=country)
    if not ok:
        # 不符就回收这个槽位
        slots = list_slots(key)
        if slots:
            _req(key, "/network/remove-client",
                 {"client_id": slots[-1].get("client_id")})
        return None, reason

    return Node(
        key_idx=None, key=key, country=(info.get("countryCode") or "").upper(),
        want_country=country, host=host, socks=sport,
        http=pcr.get("http_proxy_port"), https_port=pcr.get("https_proxy_port"),
        token=token, block=pcr.get("block"), ip=info.get("query"),
        isp=info.get("isp"), asn=(info.get("as") or "").split()[0] if info.get("as") else "",
        proxy_flag=info.get("proxy"), hosting_flag=info.get("hosting"),
        city=info.get("city"),
    ), None


def run(keys, plan, per_key, max_rounds, debug, only=None, no_recycle=False):
    nodes: list[Node] = []
    t0 = time.time()
    total_slots = len(keys) * per_key
    done = 0

    for ki, key in enumerate(keys):
        # 给这个 key 展开成国家列表
        targets = []
        for c, n in plan.items():
            targets.extend([c] * n)
        if only:
            targets = [c for c in targets if c in only]
        targets = targets[:per_key]

        print(f"\n{'='*60}")
        print(f"[key {ki+1}/{len(keys)}] {key[:20]}…  目标: {','.join(targets)}")

        for c in targets:
            done += 1
            tag = f"  [{done}/{total_slots}] {c:<3}"
            got = None
            reason = ""
            for attempt in range(1, max_rounds + 1):
                node, reason = provision_slot(key, c, debug=debug, no_recycle=no_recycle)
                if node:
                    got = node
                    break
                if "429" in str(reason):
                    print(f"{tag} 429 退避 60s")
                    time.sleep(60)
                    continue
                if attempt < max_rounds:
                    time.sleep(2)

            if got:
                got.key_idx = ki
                nodes.append(got)
                kind = "住宅" if (got.proxy_flag is False and got.hosting_flag is False) else "机房"
                print(f"{tag} OK {got.ip:<16} {kind} {str(got.isp)[:26]}")
            else:
                print(f"{tag} FAIL {str(reason)[:50]}")

            time.sleep(12 if not got else 3)   # 成功快一点，失败按限速

    return nodes, time.time() - t0


# ---------------------------------------------------------------- 输出
def export(nodes, outdir, group="UR"):
    os.makedirs(outdir, exist_ok=True)
    by_cc = defaultdict(list)
    for n in nodes:
        by_cc[n.country].append(n)

    proxies, groups = [], []
    for cc in sorted(by_cc, key=lambda c: -len(by_cc[c])):
        lst = by_cc[cc]
        names = []
        for i, n in enumerate(lst, 1):
            nm = f"{cc}{i}-{n.ip}"
            names.append(nm)
            proxies.append({
                "name": nm, "type": "socks5",
                "server": n.host, "port": int(n.socks),
                "username": n.token, "password": "", "udp": False,
            })
        groups.append({
            "name": cc, "type": "url-test",
            "url": "http://www.gstatic.com/generate_204",
            "interval": 180, "tolerance": 50, "proxies": names,
        })

    if not proxies:
        return []

    groups.append({
        "name": "AUTO-ALL", "type": "url-test",
        "url": "http://www.gstatic.com/generate_204",
        "interval": 180, "tolerance": 80,
        "proxies": [p["name"] for p in proxies],
    })
    groups.insert(0, {
        "name": group, "type": "select",
        "proxies": ["AUTO-ALL"] + [g["name"] for g in groups if g["name"] != "AUTO-ALL"] + ["DIRECT"],
    })

    cfg = {
        "mixed-port": 7890, "allow-lan": True, "mode": "rule",
        "log-level": "info", "ipv6": False,
        "proxies": proxies,
        "proxy-groups": groups,
        "rules": ["GEOIP,LAN,DIRECT,no-resolve", "GEOIP,CN,DIRECT", f"MATCH,{group}"],
    }

    files = []
    p = os.path.join(outdir, "mihomo.yaml")
    open(p, "w", encoding="utf-8").write(dump_yaml(cfg) + "\n")
    files.append(p)

    # nodes.txt / subscription
    lines, uris = [], []
    for cc in sorted(by_cc, key=lambda c: -len(by_cc[c])):
        lst = by_cc[cc]
        lines.append(f"# ===== {cc}（{len(lst)} 个）=====")
        for i, n in enumerate(lst, 1):
            q = urllib.parse.quote(f"{cc}{i}-{n.ip}")
            u1 = f"socks5h://{n.token}:@{n.host}:{n.socks}#{q}"
            lines.append(u1)
            uris.append(u1)
    p = os.path.join(outdir, "nodes.txt")
    open(p, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    files.append(p)

    p = os.path.join(outdir, "subscription.txt")
    open(p, "w", encoding="utf-8").write(
        base64.b64encode("\n".join(uris).encode()).decode())
    files.append(p)

    # singbox
    outs = []
    for cc in sorted(by_cc):
        for i, n in enumerate(by_cc[cc], 1):
            outs.append({
                "type": "socks", "tag": f"{cc}{i}-{n.ip}",
                "server": n.host, "server_port": int(n.socks),
                "version": "5", "username": n.token, "password": "",
            })
    outs.append({"type": "direct", "tag": "direct"})
    sb = {
        "log": {"level": "info", "timestamp": True},
        "inbounds": [{"type": "mixed", "tag": "in", "listen": "::", "listen_port": 7890}],
        "outbounds": outs,
        "route": {"rules": [{"ip_is_private": True, "outbound": "direct"}],
                  "final": outs[0]["tag"], "auto_detect_interface": True},
    }
    p = os.path.join(outdir, "singbox.json")
    open(p, "w", encoding="utf-8").write(json.dumps(sb, ensure_ascii=False, indent=2))
    files.append(p)

    # inventory
    p = os.path.join(outdir, "inventory.json")
    open(p, "w", encoding="utf-8").write(json.dumps(
        [dict(n.to_dict(), key_prefix=(n.key or "")[:16]) for n in nodes],
        ensure_ascii=False, indent=2))
    files.append(p)

    return files


def report(nodes, outdir, elapsed):
    by_cc = defaultdict(list)
    for n in nodes:
        by_cc[n.country].append(n)

    lines = ["# Fleet 统计报告", "",
             f"- 节点总数：**{len(nodes)}**",
             f"- 覆盖国家：**{len(by_cc)}**",
             f"- 耗时：{elapsed/60:.1f} 分钟", "",
             "## 按国家", "",
             "| 国家 | 节点数 | 住宅 | 机房 | ISP 示例 |",
             "|---|---|---|---|---|"]

    tot_res = tot_dc = 0
    for cc in sorted(by_cc, key=lambda c: -len(by_cc[c])):
        lst = by_cc[cc]
        res = [n for n in lst if n.proxy_flag is False and n.hosting_flag is False]
        dc = [n for n in lst if n not in res]
        tot_res += len(res)
        tot_dc += len(dc)
        isp = lst[0].isp or "?"
        lines.append(f"| {cc} | {len(lst)} | {len(res)} | {len(dc)} | {isp} |")

    lines += ["", f"**住宅 {tot_res} / 机房 {tot_dc}**"
              f"（住宅占比 {100*tot_res/max(len(nodes),1):.0f}%）", "",
              "## 全部节点", "",
              "| 国家 | 出口 IP | 类型 | ISP | AS | block |",
              "|---|---|---|---|---|---|"]
    for n in sorted(nodes, key=lambda x: (x.country, x.ip)):
        kind = "住宅" if (n.proxy_flag is False and n.hosting_flag is False) else "机房"
        lines.append(f"| {n.country} | `{n.ip}` | {kind} | {str(n.isp)[:28]} | {n.asn} | {n.block} |")

    p = os.path.join(outdir, "report.md")
    open(p, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    return p


def main():
    ap = argparse.ArgumentParser(description="多账号×5槽位 按国家分类 → 订阅")
    ap.add_argument("--keys", required=True, help="keys.txt（每行一个 key）")
    ap.add_argument("--out", default="fleet")
    ap.add_argument("--limit", type=int, default=0, help="只用前 N 个 key")
    ap.add_argument("--per-key", type=int, default=5, help="每 key 拉几个槽位")
    ap.add_argument("--countries", help="覆盖默认分配，如 'US:3,CA:1,JP:1'")
    ap.add_argument("--only", help="只拉这些国家，逗号分隔")
    ap.add_argument("--max-rounds", type=int, default=4, help="每槽位最多重试几次")
    ap.add_argument("--group", default="UR")
    ap.add_argument("--no-recycle", action="store_true",
                    help="槽位满时跳过，不回收已有节点（保护旧节点）")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.keys):
        sys.exit(f"找不到 {args.keys}")

    keys = [l.strip() for l in open(args.keys, encoding="utf-8")
            if l.strip().startswith("urn_") or l.strip().startswith("utn_")]
    if not keys:
        sys.exit("keys 文件里没有有效 key")
    if args.limit:
        keys = keys[: args.limit]

    plan = parse_plan(args.countries) if args.countries else dict(DEFAULT_PLAN)
    only = set(x.strip().upper() for x in args.only.split(",")) if args.only else None

    print(f"账号: {len(keys)}  每账号槽位: {args.per_key}")
    print(f"国家分配: {plan}")
    print(f"预计节点: {len(keys) * args.per_key}\n")

    nodes, elapsed = run(keys, plan, args.per_key, args.max_rounds,
                         args.debug, only, args.no_recycle)

    print(f"\n{'='*60}")
    print(f"完成: {len(nodes)} 个节点，耗时 {elapsed/60:.1f} 分钟")

    files = export(nodes, args.out, args.group)
    rp = report(nodes, args.out, elapsed)
    files.append(rp)

    by_cc = defaultdict(int)
    for n in nodes:
        by_cc[n.country] += 1
    print("\n按国家:")
    for cc, c in sorted(by_cc.items(), key=lambda x: -x[1]):
        print(f"  {cc}: {c}")
    print(f"\n产出 {len(files)} 个文件到 {os.path.abspath(args.out)}")
    for f in files:
        print("  ", os.path.basename(f))


if __name__ == "__main__":
    main()
