#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ur_refresh.py —— 3 小时周期刷新调度器
=====================================
按国家权重分配槽位，定期回收旧节点、拉新节点，输出订阅文件。

设计要点
--------
1. **周期刷新**：每 3 小时一轮。旧的槽位回收后重拉（节点 8-12 小时后衰减到 50%，
   3 小时一轮能保持大部分节点新鲜）。
2. **按权重分配**：美国/加拿大 provider 多，给多些槽位；日本只有 33 个 provider，
   给 1-2 个槽位，抽不中就抽不中。
3. **分批轮换**：不是每轮都用全部 key，而是轮转，减少单账号压力。

国家权重（按 provider 池大小）
------------------------------
    US  24918  权重 30
    CA   3730  权重 20
    BR   1249  权重  8
    FR    783  权重  8
    GB    462  权重  6
    DE    171  权重  4
    JP     33  权重  1

用法
----
    # 跑一轮（手动）
    python ur_refresh.py --keys keys.txt --out /var/www/sub --once

    # 常驻：每 3 小时自动跑
    python ur_refresh.py --keys keys.txt --out /var/www/sub --interval 10800

    # 只刷新某几个国家
    python ur_refresh.py --keys keys.txt --out sub --once --only US,CA

产出（放在 --out 目录，直接被 nginx 托管）
----
    mihomo.yaml        主配置（推荐）
    singbox.json       sing-box
    nodes.txt          明文节点列表
    subscription.txt   base64 订阅
    sub.txt            通用 base64（给 v2rayN 等）
    inventory.json     节点明细
    report.md          统计
    README.txt         说明
    state.json         刷新状态（记上次时间/节点数）
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import random
import sys
import time
import urllib.parse
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ur_hunt import issue, list_slots, probe_exit, classify, _req
import ur_hunt as H

# ---- 国家权重（provider 池越大权重越高）
WEIGHTS = {
    "US": 30,
    "CA": 20,
    "BR": 8,
    "FR": 8,
    "GB": 6,
    "DE": 4,
    "JP": 1,
}

# ---- 每国最少/最多节点
MIN_PER_CC = {"JP": 1, "DE": 2, "GB": 3, "FR": 3, "BR": 3, "CA": 6, "US": 12}


def load_keys(path):
    out = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and (line.startswith("urn_") or line.startswith("utn_")):
            out.append(line)
    return out


def plan_countries(total_slots, only=None):
    """按权重把总槽位分配到国家（严格不超过 total_slots）"""
    w = {c: v for c, v in WEIGHTS.items() if not only or c in only}
    if not w:
        w = dict(WEIGHTS)
    if total_slots <= 0:
        return {}

    tot = sum(w.values())
    plan = {}
    # 先按比例取整
    for c, v in w.items():
        n = int(total_slots * v / tot)
        if n > 0:
            plan[c] = n
    if not plan:
        # 槽位太少，只给权重最高的国家
        top = max(w, key=lambda x: w[x])
        return {top: total_slots}

    # 余数按权重从高到低补
    rest = total_slots - sum(plan.values())
    order = sorted(w, key=lambda x: -w[x])
    i = 0
    while rest > 0 and i < len(order) * 3:
        c = order[i % len(order)]
        plan[c] = plan.get(c, 0) + 1
        rest -= 1
        i += 1

    # 小国（JP 这种）如果总量够，保底 1 个
    if total_slots >= 8:
        for c in ("JP", "DE", "GB"):
            if c in w and c not in plan:
                donor = max(plan, key=lambda x: plan[x])
                if plan[donor] > 2:
                    plan[donor] -= 1
                    plan[c] = 1

    return {k: v for k, v in plan.items() if v > 0}


def fetch_one(key, country, max_rounds=3, debug=False, residential_only=True,
              proto="http", max_probe_ms=15000,
              residential_tries=None):
    """
    回收一个槽位并拉一个新节点。

    residential_only=True 时：
      优先返回住宅 IP（proxy_flag=False 且 hosting_flag=False 且 AS 不在机房名单）
      连试 residential_tries 次（默认= max_rounds）都拿到机房 → 放弃该槽位，返回 None
      这样最终配置里只剩家宽，不会混进机房。
    """
    if residential_tries is None:
        residential_tries = max_rounds

    for attempt in range(max_rounds):
        # 只在真正满 5 个时腾一个位置，绝不批量删
        # （之前"删到只剩 3 个"的逻辑会把槽位清空，导致产物里全是失效节点）
        slots = list_slots(key)
        if len(slots) >= 5:
            victim = slots[-1].get("client_id")
            _req(key, "/network/remove-client", {"client_id": victim})
            time.sleep(0.6)

        res = issue(key, country)
        pcr = res.get("proxy_config_result") or {}
        if not pcr:
            err = res.get("error") or {}
            msg = err.get("message", "?")
            if err.get("client_limit_exceeded") or "limit" in str(msg).lower():
                # 边界情况：服务端认为满了但本地数不到 5，删一个再试
                sl = list_slots(key)
                if sl:
                    _req(key, "/network/remove-client",
                         {"client_id": sl[-1].get("client_id")})
                time.sleep(2)
                continue
            if "429" in msg or "minute" in str(msg).lower():
                time.sleep(30)
                continue
            return None, msg

        host = pcr.get("proxy_host")
        # 输出端口按用户要的协议选
        if proto == "http":
            sp = pcr.get("http_proxy_port")
        elif proto == "https":
            sp = pcr.get("https_proxy_port")
        else:
            sp = pcr.get("socks_proxy_port")
        # probe 端口固定用 socks：实测 socks 立即可用（1s），
        # http 需要 ~5s 预热，用 http probe 会大量误判 fail
        my_client = pcr.get("client_id")   # 本轮这个槽位的唯一标识，回收时只删它
        probe_port = pcr.get("socks_proxy_port") or sp
        tok = pcr.get("auth_token")
        if not (host and sp and tok):
            return None, "incomplete"

        time.sleep(2)
        info = probe_exit(host, probe_port, tok, proto="socks5")
        if not info or not info.get("query"):
            # 探测失败：不删槽位（可能是 provider 慢，槽位本身是好的）
            return None, "probe fail"

        # 慢节点直接踢：实测好的 3s，差的 >18s，超过阈值说明该 provider 质量差
        ms = info.get("_probe_ms") or 0
        if ms > max_probe_ms:
            # 慢节点也保留槽位，只是不收录到产物
            return None, f"too slow ({ms}ms > {max_probe_ms}ms)"

        # 国家校验 + 住宅校验
        ok, reason = classify(info, want_country=country,
                              residential_only=residential_only)
        if not ok:
            # 只有国家不符/非住宅才回收，且只删本轮签发的这一个
            _recycle_this(key, my_client)
            # 机房 IP：如果已经试够次数，就放弃这个槽位
            if residential_only and "非住宅" in reason and attempt + 1 >= residential_tries:
                return None, f"{reason}（已试 {attempt+1} 次，放弃）"
            continue

        return {
            "country": (info.get("countryCode") or "").upper(),
            "host": host,
            "proto": proto,
            "probe_ms": ms,
            "port": int(sp),
            "http": pcr.get("http_proxy_port"),
            "https_port": pcr.get("https_proxy_port"),
            "token": tok,
            "ip": info.get("query"),
            "isp": info.get("isp"),
            "asn": (info.get("as") or "").split()[0] if info.get("as") else "",
            "proxy_flag": info.get("proxy"),
            "hosting_flag": info.get("hosting"),
            "city": info.get("city"),
            "block": pcr.get("block"),
            "residential": (info.get("proxy") is False and info.get("hosting") is False),
            "ts": int(time.time()),
        }, None
    return None, "rounds exhausted"


def _recycle_last(key):
    """回收该 key 最后一个槽位（仅在不知道 client_id 时兜底用）"""
    try:
        slots = list_slots(key)
        if slots:
            _req(key, "/network/remove-client",
                 {"client_id": slots[-1].get("client_id")})
    except Exception:
        pass


def _recycle_this(key, client_id):
    """
    精确回收本轮刚签发的那个槽位。

    之前用 _recycle_last 删「列表最后一个」，那往往是别的任务
    已经收录进产物的节点 —— 导致刷新过程中自己把自己删光，
    产物里的节点全部失效（表现就是客户端全部 timeout）。
    """
    if not client_id:
        return
    try:
        _req(key, "/network/remove-client", {"client_id": client_id})
    except Exception:
        pass


def run_round(keys, outdir, only=None, debug=False, max_per_key=5,
              residential_only=True, max_rounds=3, parallel=1, proto="http",
              max_probe_ms=15000):
    """
    跑一轮完整刷新。

    parallel > 1 时：按 key 分片并行（每个 key 的限速是独立的，
    所以并行不会互相挤占配额，能直接把总时间压到 1/parallel）。
    """
    total_slots = len(keys) * max_per_key
    plan = plan_countries(total_slots, only)
    print(f"账号 {len(keys)} × {max_per_key} 槽位 = {total_slots}"
          f"（并行 {parallel}，协议 {proto}，延迟上限 {max_probe_ms}ms）")
    print(f"国家分配: {plan}\n")

    # 把国家展开成任务队列，然后按 key 轮流分配（保证每个 key 负载均衡）
    tasks = []
    for cc, n in plan.items():
        tasks.extend([cc] * n)
    random.shuffle(tasks)

    # 把任务切成 parallel 份，每份交给一个 worker
    by_key = defaultdict(list)
    for i, cc in enumerate(tasks):
        by_key[keys[i % len(keys)]].append(cc)

    chunks = list(by_key.items())
    nodes = []
    lock = threading.Lock()
    done_counter = {"n": 0}
    total = len(tasks)
    t0 = time.time()

    def worker(chunk):
        key, ccs = chunk
        local = []
        for cc in ccs:
            with lock:
                done_counter["n"] += 1
                idx = done_counter["n"]
            node, reason = fetch_one(key, cc, max_rounds=max_rounds,
                                     debug=debug,
                                     residential_only=residential_only,
                                     proto=proto, max_probe_ms=max_probe_ms)
            if node:
                local.append(node)
                kind = "住宅" if node.get("residential") else "机房"
                with lock:
                    print(f"[{idx}/{total}] {cc:<3} OK {node['ip']:<16} "
                          f"{kind} {str(node['isp'])[:26]}", flush=True)
            else:
                with lock:
                    print(f"[{idx}/{total}] {cc:<3} FAIL {str(reason)[:46]}",
                          flush=True)
            time.sleep(1)
        with lock:
            nodes.extend(local)
        return len(local)

    if parallel <= 1:
        for ch in chunks:
            worker(ch)
    else:
        with ThreadPoolExecutor(max_workers=parallel) as ex:
            list(ex.map(worker, chunks))

    el = time.time() - t0
    got = len(nodes)
    print(f"\n本轮 {got}/{total} 个节点，耗时 {el/60:.1f} 分钟"
          f"（{el/max(got,1):.0f}s/个，并行 {parallel}）")
    return nodes, el


# ---------------------------------------------------------------- 输出
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
            elif isinstance(v, list):
                out.append(f"{sp}-")
                out.append(dump_yaml(v, ind + 1))
            else:
                out.append(f"{sp}- {ys(v)}")
        return "\n".join(out)
    return f"{sp}{ys(obj)}"


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


def export(nodes, outdir, group="UR"):
    os.makedirs(outdir, exist_ok=True)
    by_cc = defaultdict(list)
    for n in nodes:
        by_cc[n["country"]].append(n)

    proxies, groups = [], []
    for cc in sorted(by_cc, key=lambda c: -len(by_cc[c])):
        lst = by_cc[cc]
        names = []
        for i, n in enumerate(lst, 1):
            nm = f"{cc}{i}-{n['ip']}"
            names.append(nm)
            proto = n.get("proto") or "socks5"
            item = {
                "name": nm,
                "type": "http" if proto in ("http", "https") else "socks5",
                "server": n["host"],
                "port": n["port"],
                "username": n["token"], "password": "",
                "udp": False,   # relay 返回私有地址 172.19.0.1，UDP 不可用
            }
            if proto == "https":
                item["tls"] = True          # 到代理的链路走 TLS
                item["skip-cert-verify"] = False
            proxies.append(item)
        groups.append({
            "name": cc, "type": "url-test",
            "url": "http://www.gstatic.com/generate_204",
            "interval": 180, "tolerance": 50, "proxies": names,
        })

    if not proxies:
        return []

    groups.append({"name": "AUTO", "type": "url-test",
                   "url": "http://www.gstatic.com/generate_204",
                   "interval": 180, "tolerance": 80,
                   "proxies": [p["name"] for p in proxies]})
    groups.insert(0, {"name": group, "type": "select",
                      "proxies": ["AUTO"] + [g["name"] for g in groups if g["name"] != "AUTO"] + ["DIRECT"]})

    cfg = {"mixed-port": 7890, "allow-lan": True, "mode": "rule",
           "log-level": "info", "ipv6": False,
           "proxies": proxies, "proxy-groups": groups,
           "rules": ["GEOIP,LAN,DIRECT,no-resolve", "GEOIP,CN,DIRECT", f"MATCH,{group}"]}

    files = []
    p = os.path.join(outdir, "mihomo.yaml")
    open(p, "w", encoding="utf-8").write(dump_yaml(cfg) + "\n")
    files.append(p)

    # 节点列表
    lines, uris = [], []
    for cc in sorted(by_cc, key=lambda c: -len(by_cc[c])):
        lines.append(f"# ===== {cc}（{len(by_cc[cc])} 个）=====")
        for i, n in enumerate(by_cc[cc], 1):
            q = urllib.parse.quote(f"{cc}{i}-{n['ip']}")
            scheme = "socks5h" if (n.get("proto") or "socks5") == "socks5" else n.get("proto")
            u = f"{scheme}://{n['token']}:@{n['host']}:{n['port']}#{q}"
            lines.append(u)
            uris.append(u)

    p = os.path.join(outdir, "nodes.txt")
    open(p, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    files.append(p)

    b64 = base64.b64encode("\n".join(uris).encode()).decode()
    p = os.path.join(outdir, "subscription.txt")
    open(p, "w", encoding="utf-8").write(b64)
    files.append(p)
    p = os.path.join(outdir, "sub.txt")
    open(p, "w", encoding="utf-8").write(b64)
    files.append(p)

    # sing-box
    outs = []
    for cc in sorted(by_cc):
        for i, n in enumerate(by_cc[cc], 1):
            proto = n.get("proto") or "socks5"
            tag = f"{cc}{i}-{n['ip']}"
            if proto == "socks5":
                outs.append({"type": "socks", "tag": tag,
                             "server": n["host"], "server_port": n["port"],
                             "version": "5", "username": n["token"], "password": ""})
            elif proto == "https":
                outs.append({"type": "http", "tag": tag,
                             "server": n["host"], "server_port": n["port"],
                             "username": n["token"], "password": "",
                             "tls": {"enabled": True, "insecure": False}})
            else:
                outs.append({"type": "http", "tag": tag,
                             "server": n["host"], "server_port": n["port"],
                             "username": n["token"], "password": ""})
    outs.append({"type": "direct", "tag": "direct"})
    sb = {"log": {"level": "info", "timestamp": True},
          "inbounds": [{"type": "mixed", "tag": "in", "listen": "::", "listen_port": 7890}],
          "outbounds": outs,
          "route": {"rules": [{"ip_is_private": True, "outbound": "direct"}],
                    "final": outs[0]["tag"], "auto_detect_interface": True}}
    p = os.path.join(outdir, "singbox.json")
    open(p, "w", encoding="utf-8").write(json.dumps(sb, ensure_ascii=False, indent=2))
    files.append(p)

    p = os.path.join(outdir, "inventory.json")
    open(p, "w", encoding="utf-8").write(json.dumps(nodes, ensure_ascii=False, indent=2))
    files.append(p)

    return files


def write_report(nodes, outdir, elapsed):
    by_cc = defaultdict(list)
    for n in nodes:
        by_cc[n["country"]].append(n)

    res_tot = dc_tot = 0
    lines = ["# 订阅状态", "",
             f"- 更新时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"- 节点总数：**{len(nodes)}**",
             f"- 覆盖国家：**{len(by_cc)}**",
             f"- 本轮耗时：{elapsed/60:.1f} 分钟", "",
             "| 国家 | 节点 | 住宅 | 机房 | ISP 示例 |",
             "|---|---|---|---|---|"]
    for cc in sorted(by_cc, key=lambda c: -len(by_cc[c])):
        lst = by_cc[cc]
        res = [n for n in lst if n["proxy_flag"] is False and n["hosting_flag"] is False]
        dc = len(lst) - len(res)
        res_tot += len(res); dc_tot += dc
        lines.append(f"| {cc} | {len(lst)} | {len(res)} | {dc} | {str(lst[0]['isp'])[:26]} |")
    lines += ["", f"**住宅 {res_tot} / 机房 {dc_tot}**"
              f"（住宅占比 {100*res_tot/max(len(nodes),1):.0f}%）"]

    p = os.path.join(outdir, "report.md")
    open(p, "w", encoding="utf-8").write("\n".join(lines) + "\n")

    readme = f"""# URnetwork 订阅

更新时间：{time.strftime('%Y-%m-%d %H:%M:%S')}
节点数：{len(nodes)}（住宅 {res_tot}）

## 订阅地址
- mihomo / Clash.Meta: /mihomo.yaml
- sing-box:            /singbox.json
- base64 订阅:         /subscription.txt
- 明文节点:            /nodes.txt

## 说明
- 每 3 小时自动刷新
- 协议：SOCKS5（udp 已关闭，服务端 relay 不可达）
- 端口即国家：US/CA/BR/FR/GB/DE/JP
"""
    p = os.path.join(outdir, "README.txt")
    open(p, "w", encoding="utf-8").write(readme)
    return p


def main():
    ap = argparse.ArgumentParser(description="URnetwork 订阅 3 小时刷新")
    ap.add_argument("--keys", required=True)
    ap.add_argument("--out", default="sub")
    ap.add_argument("--only", help="只刷这些国家，逗号分隔")
    ap.add_argument("--interval", type=int, default=0,
                    help="秒；0=只跑一次（配合 systemd timer 用）")
    ap.add_argument("--once", action="store_true", help="只跑一轮")
    ap.add_argument("--max-per-key", type=int, default=5)
    ap.add_argument("--residential-only", dest="residential_only",
                    action="store_true", default=True,
                    help="只要住宅 IP（默认开启）")
    ap.add_argument("--allow-datacenter", dest="residential_only",
                    action="store_false",
                    help="允许机房 IP 混入")
    ap.add_argument("--max-rounds", type=int, default=3,
                    help="每个槽位最多试几次（住宅筛选会重试）")
    ap.add_argument("--max-probe-ms", type=int, default=15000,
                    help="探测延迟上限，超过就丢弃（默认 15000）")
    ap.add_argument("--protocol", choices=["http", "https", "socks5"],
                    default="http", help="节点协议（默认 http，实测最快）")
    ap.add_argument("--parallel", type=int, default=1,
                    help="并行 worker 数（每个 key 限速独立，建议 6-10）")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    keys = load_keys(args.keys)
    if not keys:
        sys.exit(f"{args.keys} 里没有有效 key")
    print(f"载入 {len(keys)} 个 key")

    only = set(x.strip().upper() for x in args.only.split(",")) if args.only else None

    while True:
        print("\n" + "=" * 64)
        print(f"刷新开始 {time.strftime('%Y-%m-%d %H:%M:%S')}")
        nodes, el = run_round(keys, args.out, only, args.debug, args.max_per_key,
                              residential_only=args.residential_only,
                              max_rounds=args.max_rounds,
                              parallel=args.parallel,
                              proto=args.protocol,
                              max_probe_ms=args.max_probe_ms)

        if nodes:
            files = export(nodes, args.out)
            write_report(nodes, args.out, el)
            state = {"last_refresh": int(time.time()),
                     "last_refresh_str": time.strftime("%Y-%m-%d %H:%M:%S"),
                     "nodes": len(nodes), "elapsed_min": round(el / 60, 1)}
            json.dump(state, open(os.path.join(args.out, "state.json"), "w",
                                  encoding="utf-8"), ensure_ascii=False, indent=2)
            print(f"已写入 {len(files)+2} 个文件到 {os.path.abspath(args.out)}")
        else:
            print("[!] 本轮没有拿到节点，保留上次结果")

        if args.once or args.interval <= 0:
            break
        print(f"\n等待 {args.interval/3600:.1f} 小时后下一轮...")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
