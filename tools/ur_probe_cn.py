#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ur_probe_cn.py —— 国内环境可用性探针
====================================
在**中国大陆网络**下运行，判断 URnetwork 的代理端口到底能不能用。

关键要区分三种协议形态（很多人只测了 SOCKS5 就下结论）：

    socks5h://host:port   握手全明文（05 01 00），最容易被识别
    http://host:port      CONNECT 明文
    https://host:port     到代理是标准 TLS ← 这个最可能绕过去

用法
----
    # 用 ur_ops 产出的 inventory.json（信息最全，推荐）
    python ur_probe_cn.py --inventory inventory.json

    # 或用 nodes.txt
    python ur_probe_cn.py --nodes nodes.txt

    # 指定测试几个
    python ur_probe_cn.py --inventory inventory.json --sample 6

判断标准
--------
    204    通
    000    握手被断 / 连不上
    超时   被主动阻断

输出
----
    三种协议各测一遍，给出对比表。结论直接告诉你该走哪条路。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

TARGET = "http://www.gstatic.com/generate_204"
CURL = shutil.which("curl") or shutil.which("curl.exe")


def curl_test(proxy_args, cred, timeout=15):
    """返回 (code, elapsed, err)"""
    if not CURL:
        return None, 0, "curl 不可用"
    cmd = [CURL, "-s", "-m", str(timeout), "-o", os.devnull,
           "-w", "%{http_code}|%{time_total}"] + proxy_args + \
          (["-U", cred] if cred else []) + [TARGET]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout + 8, errors="replace")
        out = (p.stdout or "").strip()
        if "|" in out:
            code, el = out.split("|", 1)
            return code, float(el or 0), ""
        return out or "000", time.time() - t0, (p.stderr or "")[:60]
    except subprocess.TimeoutExpired:
        return "TIMEOUT", time.time() - t0, "超时"
    except Exception as e:
        return "ERR", 0, str(e)[:60]


def parse_nodes(path):
    """从 nodes.txt 解析出 socks5h 节点（含 token/host/port）"""
    out = []
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        m = re.match(r"^socks5h://([^:]+):@([^:]+):(\d+)#?(.*)$", line)
        if m:
            out.append({"token": m.group(1), "host": m.group(2),
                        "socks": int(m.group(3)), "label": m.group(4)})
        else:
            m = re.match(r"^http://([^:]+):@([^:]+):(\d+)#?(.*)$", line)
            if m:
                out.append({"token": m.group(1), "host": m.group(2),
                            "http": int(m.group(3)), "label": m.group(4)})
    return out


def main():
    ap = argparse.ArgumentParser(description="URnetwork 国内可用性探针")
    ap.add_argument("--inventory", help="ur_ops 的 inventory.json（推荐）")
    ap.add_argument("--nodes", help="nodes.txt")
    ap.add_argument("--sample", type=int, default=5, help="每个国家测几个")
    ap.add_argument("--timeout", type=int, default=15)
    args = ap.parse_args()

    if not CURL:
        sys.exit("需要 curl（Windows 10+ 自带，Linux: apt install curl）")

    nodes = []
    if args.inventory and os.path.exists(args.inventory):
        data = json.load(open(args.inventory, encoding="utf-8"))
        by_cc = {}
        for n in data:
            cc = (n.get("country") or "?").upper()
            by_cc.setdefault(cc, []).append(n)
        for cc, lst in by_cc.items():
            nodes.extend(lst[: args.sample])
        print(f"从 inventory 读取 {len(data)} 条，抽样 {len(nodes)} 个\n")
    elif args.nodes and os.path.exists(args.nodes):
        nodes = parse_nodes(args.nodes)[: args.sample * 3]
        print(f"从 nodes.txt 读取 {len(nodes)} 个\n")
    else:
        sys.exit("需要 --inventory 或 --nodes")

    if not nodes:
        sys.exit("没有可测节点")

    stats = {"socks5h": [0, 0], "http": [0, 0], "https": [0, 0]}

    print(f"{'国家':<5}{'入口':<32}{'SOCKS5':<12}{'HTTP':<12}{'HTTPS':<12}")
    print("-" * 78)

    for n in nodes:
        cc = (n.get("country") or "?").upper()[:4]
        host = n.get("host", "")
        socks = n.get("socks") or n.get("socks_proxy_port")
        http = n.get("http") or n.get("http_proxy_port")
        https = n.get("https_port") or n.get("https_proxy_port")
        token = n.get("token") or n.get("auth_token") or ""
        cred = token + ":" if token else None

        row = [cc, f"{host}"]
        for kind, args_, port in (
            ("socks5h", ["--socks5-hostname", f"{host}:{socks}"], socks),
            ("http", ["-x", f"http://{host}:{http}"], http),
            ("https", ["-x", f"https://{host}:{https}"], https),
        ):
            if not port:
                row.append("n/a")
                continue
            code, el, err = curl_test(args_, cred, args.timeout)
            stats[kind][1] += 1
            if code == "204":
                stats[kind][0] += 1
                row.append(f"204 {el:.1f}s")
            elif code in ("TIMEOUT", "000", "ERR", None):
                row.append("✗")
            else:
                row.append(str(code))
        print(f"{row[0]:<5}{row[1]:<32}{row[2]:<12}{row[3]:<12}{row[4]:<12}")

    print("-" * 78)
    print("\n=== 汇总 ===")
    for kind, (ok, total) in stats.items():
        if total:
            pct = ok * 100 // total
            bar = "█" * (pct // 10)
            print(f"  {kind:<9} {ok:>2}/{total:<3} {pct:>3}%  {bar}")
        else:
            print(f"  {kind:<9} 未测")

    print("\n=== 结论 ===")
    s_ok = stats["socks5h"][0]
    h_ok = stats["http"][0]
    t_ok = stats["https"][0]

    if t_ok > 0:
        print("  ✅ HTTPS 代理可用！")
        print("     → 不需要链式代理，客户端直接配 https 类型代理即可")
        print("     → 建议：优先用 https，SOCKS5 作为备份")
    elif h_ok > 0 or s_ok > 0:
        print("  ⚠️ 只有明文协议能用，HTTPS 不行")
        print("     → 说明是端口/ALPN 特征被识别，不是域名问题")
        print("     → 建议：上 REALITY 链式（port 443 伪装）")
    else:
        print("  ❌ 三种协议全部不通")
        print("     → 域名或 IP 段被阻断，或网络环境本身限制")
        print("     → 必须上链式代理，且 VPS 要在境外")


if __name__ == "__main__":
    main()
