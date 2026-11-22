#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ur_scan.py —— 扫描式刷新（不签发、不删除槽位）
================================================
核心思路
--------
`list_slots()` 返回的 proxy_client 里已经含完整的
host / 三个端口 / auth_token，不需要重新 issue()。

之前 ur_refresh.py 每轮都签发新节点 + 删旧槽位腾位置，
165 个任务下来把槽位删空了，产物里的节点全部失效
（客户端表现为全部 timeout）。

本脚本只做三件事：
    1. 扫出所有槽位的连接信息
    2. 通过代理取出口 IP（并行）
    3. batch 批量查 IP 详情 + 判定住宅/国家
槽位永久保留。

用法
----
    python ur_scan.py --keys keys.txt --out sub
    python ur_scan.py --keys keys.txt --out sub --protocol https --parallel 8
    python ur_scan.py --keys keys.txt --out sub --topup   # 顺便补满空槽位
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import random
import sys
import threading
import time
import urllib.parse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ur_hunt import issue, list_slots, probe_ip, classify, _req
from geo_batch import batch_lookup


def load_keys(path):
    out = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and (line.startswith("urn_") or line.startswith("utn_")):
            out.append(line)
    return out


def extract_node(slot, proto="https"):
    pc = slot.get("proxy_client") or {}
    host, tok = pc.get("proxy_host"), pc.get("auth_token")
    if not (host and tok):
        return None
    ports = {
        "http": pc.get("http_proxy_port"),
        "https": pc.get("https_proxy_port"),
        "socks5": pc.get("socks_proxy_port"),
    }
    port = ports.get(proto) or ports.get("socks5")
    if not port:
        return None
    return {
        "client_id": slot.get("client_id"),
        "host": host,
        "port": int(port),
        "token": tok,
        "ports": ports,
        "create_time": slot.get("create_time"),
        "proto": proto,
    }


def scan(key, proto="https"):
    try:
        slots = list_slots(key)
    except Exception:
        return []
    out = []
    for s in slots:
        n = extract_node(s, proto)
        if n:
            out.append(n)
    return out


def probe_stage(nodes, parallel, max_probe_ms, proto):
    """阶段一：并行取出口 IP"""
    lock = threading.Lock()
    done = {"n": 0}
    alive = []

    def work(n):
        pp = n["ports"].get("socks5") or n["port"]
        ip, ms = probe_ip(n["host"], pp, n["token"], timeout=25, proto="socks5")
        with lock:
            done["n"] += 1
            i = done["n"]
        if not ip:
            with lock:
                print(f"[{i}/{len(nodes)}] --- probe fail", flush=True)
            return
        if ms > max_probe_ms:
            with lock:
                print(f"[{i}/{len(nodes)}] --- too slow ({ms}ms)", flush=True)
            return
        n["ip"] = ip
        n["probe_ms"] = ms
        alive.append(n)
        with lock:
            print(f"[{i}/{len(nodes)}] {ip:<16} {ms:>5}ms", flush=True)

    with ThreadPoolExecutor(max_workers=parallel) as ex:
        list(ex.map(work, nodes))
    return alive


def detail_stage(alive, want_country, residential_only):
    """阶段二：batch 查详情并判定"""
    if not alive:
        return []
    print(f"\n=== 批量查询 {len(alive)} 个 IP 的详情 ===")
    details = batch_lookup([n["ip"] for n in alive])
    print(f"  查到 {len(details)} 条")

    good = []
    reasons = defaultdict(int)
    for n in alive:
        d = details.get(n["ip"])
        if not d:
            reasons["无详情"] += 1
            continue
        ok, reason = classify(d, want_country=want_country,
                              residential_only=residential_only)
        if not ok:
            reasons[reason.split("（")[0]] += 1
            continue
        n["isp"] = d.get("isp") or d.get("org") or d.get("asname") or "unknown"
        n["asn"] = (d.get("as") or "").split()[0] if d.get("as") else ""
        n["country"] = (d.get("countryCode") or "??")[:2].upper()
        n["city"] = d.get("city")
        n["proxy_flag"] = d.get("proxy")
        n["hosting_flag"] = d.get("hosting")
        n["residential"] = (not d.get("proxy") and not d.get("hosting"))
        good.append(n)

    print(f"  通过 {len(good)}/{len(alive)}")
    if reasons:
        print(f"  淘汰原因: {dict(reasons)}")
    return good


def topup(keys, proto, parallel, max_probe_ms, residential_only, want_country):
    """给槽位不足 5 个的 key 补签发"""
    need_keys = []
    with ThreadPoolExecutor(max_workers=parallel) as ex:
        for k, slots in zip(keys, ex.map(lambda x: len(list_slots(x)), keys)):
            if slots < 5:
                need_keys.append((k, 5 - slots))
    if not need_keys:
        print("\n  所有 key 槽位已满，无需补签发")
        return []

    print(f"\n=== 补签发（{len(need_keys)} 个 key 有空位）===")
    added = []
    for key, need in need_keys[:12]:
        for j in range(need):
            cc = want_country or random.choice(["US", "CA", "US", "CA", "US", "CA", "FR", "GB"])
            res = issue(key, cc)
            pcr = res.get("proxy_config_result") or {}
            if not pcr:
                break
            n = {
                "client_id": pcr.get("client_id"),
                "host": pcr.get("proxy_host"),
                "port": int(pcr.get("https_proxy_port") if proto == "https"
                            else pcr.get("socks_proxy_port")),
                "token": pcr.get("auth_token"),
                "ports": {"http": pcr.get("http_proxy_port"),
                          "https": pcr.get("https_proxy_port"),
                          "socks5": pcr.get("socks_proxy_port")},
                "proto": proto,
            }
            time.sleep(2.5)
            ip, ms = probe_ip(n["host"], n["ports"]["socks5"], n["token"])
            if not ip:
                _req(key, "/network/remove-client", {"client_id": n["client_id"]})
                print(f"  {n['host']}:{n['port']} -- probe fail")
                continue
            n["ip"], n["probe_ms"] = ip, ms
            d = batch_lookup([ip]).get(ip, {})
            ok, reason = classify(d, want_country=want_country,
                                  residential_only=residential_only) if d else (False, "无详情")
            if ok:
                n.update({
                    "isp": d.get("isp") or "unknown",
                    "asn": (d.get("as") or "").split()[0] if d.get("as") else "",
                    "country": (d.get("countryCode") or "??")[:2].upper(),
                    "city": d.get("city"),
                    "proxy_flag": d.get("proxy"),
                    "hosting_flag": d.get("hosting"),
                    "residential": (not d.get("proxy") and not d.get("hosting")),
                })
                added.append(n)
                print(f"  + {n['country']} {ip} {str(n['isp'])[:26]}")
            else:
                _req(key, "/network/remove-client", {"client_id": n["client_id"]})
                print(f"  - {ip} {reason[:36]}")
            time.sleep(1)
    return added


# ---------------------------------------------------------------- 输出
def ys(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "null"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if s == "" or any(c in s for c in ":#{}[],&*?|-<>=!%@`'\"") or s.strip() != s:
        return json.dumps(s, ensure_ascii=False)
    return s


def dump_yaml(obj, ind=0):
    sp = "  " * ind
    if isinstance(obj, dict):
        out = []
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                out.append(f"{sp}{k}:")
                out.append(dump_yaml(v, ind + 1))
            else:
                out.append(f"{sp}{k}: {ys(v)}")
        return "\n".join(out)
    if isinstance(obj, list):
        out = []
        for v in obj:
            if isinstance(v, dict):
                b = dump_yaml(v, ind + 1).split("\n")
                out.append(f"{sp}- {b[0].strip()}")
                out.extend(b[1:])
            else:
                out.append(f"{sp}- {ys(v)}")
        return "\n".join(out)
    return f"{sp}{ys(obj)}"


def export(nodes, outdir, group="UR", dedup_per_ip=2):
    """
    输出配置。

    dedup_per_ip：同一个出口 IP 最多保留几个条目。
    URnetwork 住宅池里 GoCodeIT 一家占八成，不去重的话
    61 个条目里 50 个是同一个出口，切换节点等于没换。
    """
    os.makedirs(outdir, exist_ok=True)

    by_ip = defaultdict(list)
    for n in nodes:
        by_ip[n["ip"]].append(n)
    picked = []
    for ip, lst in by_ip.items():
        lst.sort(key=lambda x: x.get("probe_ms") or 999999)
        picked.extend(lst[:max(1, dedup_per_ip)])
    picked.sort(key=lambda x: x.get("probe_ms") or 999999)
    orig = sum(len(v) for v in by_ip.values())
    nodes = picked
    if dedup_per_ip < 900:
        print(f"  去重: {orig} -> {len(nodes)} 条"
              f"（{len(by_ip)} 个唯一 IP，每个最多 {dedup_per_ip} 条）")

    by_cc = defaultdict(list)
    for n in nodes:
        by_cc[n["country"]].append(n)

    proxies, groups = [], []
    for cc in sorted(by_cc, key=lambda c: -len(by_cc[c])):
        lst = by_cc[cc]
        names = []
        for i, n in enumerate(lst, 1):
            ms = n.get('probe_ms') or 0
            nm = f"{cc}{i}-{ms}ms-{n['ip']}"
            names.append(nm)
            item = {
                "name": nm,
                "type": "http" if n["proto"] in ("http", "https") else "socks5",
                "server": n["host"], "port": n["port"],
                "username": n["token"], "password": "", "udp": False,
            }
            if n["proto"] == "https":
                item["tls"] = True
                item["skip-cert-verify"] = False
            proxies.append(item)
        groups.append({
            "name": cc, "type": "url-test",
            "url": "http://www.gstatic.com/generate_204",
            "interval": 300, "tolerance": 100, "timeout": 10000, "lazy": True,
            "proxies": names,
        })

    if not proxies:
        return []
    groups.append({"name": "AUTO", "type": "url-test",
                   "url": "http://www.gstatic.com/generate_204",
                   "interval": 300, "tolerance": 100, "timeout": 10000, "lazy": True,
                   "proxies": [p["name"] for p in proxies]})
    groups.insert(0, {"name": group, "type": "select",
                      "proxies": ["AUTO"] + [g["name"] for g in groups if g["name"] != "AUTO"] + ["DIRECT"]})

    cfg = {"mixed-port": 7890, "allow-lan": True, "mode": "rule",
           "log-level": "info", "ipv6": False,
           "proxies": proxies, "proxy-groups": groups,
           "rules": ["GEOIP,LAN,DIRECT,no-resolve", "GEOIP,CN,DIRECT", f"MATCH,{group}"]}
    open(os.path.join(outdir, "mihomo.yaml"), "w", encoding="utf-8").write(dump_yaml(cfg) + "\n")

    lines, uris = [], []
    for cc in sorted(by_cc, key=lambda c: -len(by_cc[c])):
        lines.append(f"# ===== {cc}（{len(by_cc[cc])} 个）=====")
        for i, n in enumerate(by_cc[cc], 1):
            q = urllib.parse.quote(f"{cc}{i}-{n['ip']}")
            scheme = "socks5h" if n["proto"] == "socks5" else n["proto"]
            u = f"{scheme}://{n['token']}:@{n['host']}:{n['port']}#{q}"
            lines.append(u)
            uris.append(u)
    open(os.path.join(outdir, "nodes.txt"), "w", encoding="utf-8").write("\n".join(lines) + "\n")

    b64 = base64.b64encode("\n".join(uris).encode()).decode()
    for fn in ("subscription.txt", "sub.txt"):
        open(os.path.join(outdir, fn), "w", encoding="utf-8").write(b64)

    open(os.path.join(outdir, "inventory.json"), "w", encoding="utf-8").write(
        json.dumps(nodes, ensure_ascii=False, indent=2))
    return nodes


def write_report(nodes, outdir, elapsed):
    by_cc = defaultdict(list)
    for n in nodes:
        by_cc[n["country"]].append(n)
    res_tot = sum(1 for n in nodes if n.get("residential"))
    lines = ["# 订阅状态", "",
             f"- 更新时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"- 节点总数：**{len(nodes)}**",
             f"- 覆盖国家：**{len(by_cc)}**",
             f"- 本轮耗时：{elapsed/60:.1f} 分钟",
             f"- 模式：扫描现有槽位（不签发不删除）", "",
             "| 国家 | 节点 | 住宅 | 机房 | ISP 示例 |", "|---|---|---|---|---|"]
    for cc in sorted(by_cc, key=lambda c: -len(by_cc[c])):
        lst = by_cc[cc]
        r = sum(1 for n in lst if n.get("residential"))
        lines.append(f"| {cc} | {len(lst)} | {r} | {len(lst)-r} | {str(lst[0].get('isp'))[:26]} |")
    lines += ["", f"**住宅 {res_tot} / 机房 {len(nodes)-res_tot}**"
              f"（住宅占比 {100*res_tot/max(len(nodes),1):.0f}%）"]
    open(os.path.join(outdir, "report.md"), "w", encoding="utf-8").write("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser(description="URnetwork 扫描式刷新")
    ap.add_argument("--keys", required=True)
    ap.add_argument("--out", default="sub")
    ap.add_argument("--protocol", choices=["http", "https", "socks5"], default="https")
    ap.add_argument("--parallel", type=int, default=8)
    ap.add_argument("--max-probe-ms", type=int, default=25000)
    ap.add_argument("--residential-only", dest="residential_only", action="store_true", default=True)
    ap.add_argument("--allow-datacenter", dest="residential_only", action="store_false")
    ap.add_argument("--only", help="只要这些国家")
    ap.add_argument("--topup", action="store_true", help="给空槽位补签发")
    ap.add_argument("--dedup-per-ip", type=int, default=2,
                    help="同一出口 IP 最多保留几条（默认2，0=不去重）")
    args = ap.parse_args()

    keys = load_keys(args.keys)
    if not keys:
        sys.exit(f"{args.keys} 里没有有效 key")
    only = set(x.strip().upper() for x in args.only.split(",")) if args.only else None

    t0 = time.time()
    print("=" * 64)
    print(f"扫描刷新 {time.strftime('%Y-%m-%d %H:%M:%S')}  "
          f"协议={args.protocol} 并行={args.parallel}")

    print("\n=== 1. 扫槽位 ===")
    all_nodes = []
    key_slots = {}
    with ThreadPoolExecutor(max_workers=args.parallel) as ex:
        for k, ns in zip(keys, ex.map(lambda x: scan(x, args.protocol), keys)):
            key_slots[k] = len(ns)
            all_nodes.extend(ns)
    dist = defaultdict(int)
    for v in key_slots.values():
        dist[v] += 1
    print(f"  {len(keys)} 个 key，{len(all_nodes)} 个槽位")
    print(f"  槽位分布: {dict(sorted(dist.items()))}")

    if not all_nodes:
        sys.exit("没有可扫描的槽位")

    print(f"\n=== 2. 取出口 IP（并行 {args.parallel}）===")
    alive = probe_stage(all_nodes, args.parallel, args.max_probe_ms, args.protocol)
    print(f"\n  存活 {len(alive)}/{len(all_nodes)}")

    good = detail_stage(alive, only, args.residential_only)

    if args.topup:
        added = topup(keys, args.protocol, args.parallel, args.max_probe_ms,
                      args.residential_only, list(only)[0] if only and len(only) == 1 else None)
        good.extend(added)

    el = time.time() - t0
    if good:
        export(good, args.out, dedup_per_ip=(args.dedup_per_ip if args.dedup_per_ip > 0 else 999))
        write_report(good, args.out, el)
        json.dump({"last_scan": int(time.time()),
                   "last_scan_str": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "slots": len(all_nodes), "alive": len(alive),
                   "nodes": len(good), "elapsed_min": round(el / 60, 1)},
                  open(os.path.join(args.out, "state.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print(f"\n=== 完成: {len(good)} 个节点，耗时 {el/60:.1f} 分钟 ===")
        print(f"写入 {os.path.abspath(args.out)}")
    else:
        print("\n[!] 没有合格节点")


if __name__ == "__main__":
    main()
