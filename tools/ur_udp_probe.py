#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ur_udp_probe.py —— 测试 URnetwork 的 SOCKS5 是否支持 UDP ASSOCIATE
==================================================================
SOCKS5 协议本身支持 UDP 转发（RFC 1928 的 UDP ASSOCIATE，CMD=0x03），
流程是：TCP 建立控制连接 → 认证 → 发 UDP ASSOCIATE → 拿到 relay 地址
       → 之后 UDP 数据包发到 relay，由代理解析并转发。

如果支持，就能：
  · 让 mihomo / sing-box 的 socks5 outbound 带 udp: true
  · 转发 DNS 查询、QUIC、游戏流量
  · 而不是只能走 TCP

用法
----
    python ur_udp_probe.py --inventory inventory.json --sample 3
    python ur_udp_probe.py --host crisp.bringyour.com --port 7141 --token <auth_token>
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import sys

SOCKS5_VER = 0x05
AUTH_USERPASS = 0x02
CMD_UDP_ASSOCIATE = 0x03
ATYP_IPV4 = 0x01
ATYP_DOMAIN = 0x03


def socks5_udp_test(host, port, user, password="", timeout=10):
    """
    返回 dict:
      ok          TCP 控制连接是否建立
      auth        (bool, method)
      udp_supported  UDP ASSOCIATE 是否被接受
      relay       relay 地址 (ip, port)
      error       错误信息
    """
    res = {"ok": False, "auth": None, "udp_supported": False,
           "relay": None, "error": ""}
    s = None
    try:
        s = socket.create_connection((host, port), timeout=timeout)

        # 1) 方法协商
        s.sendall(bytes([SOCKS5_VER, 1, AUTH_USERPASS]))
        rep = s.recv(2)
        if len(rep) < 2:
            res["error"] = "协商响应为空"
            return res
        if rep[0] != SOCKS5_VER:
            res["error"] = f"非 SOCKS5 响应: {rep[0]}"
            return res
        method = rep[1]
        if method == 0xFF:
            res["error"] = "服务端拒绝所有认证方法"
            return res
        res["auth"] = ("no-auth" if method == 0x00 else
                       "user-pass" if method == AUTH_USERPASS else f"method={method}")

        # 2) 用户名密码认证
        if method == AUTH_USERPASS:
            u = user.encode()
            p = password.encode()
            s.sendall(bytes([0x01, len(u)]) + u + bytes([len(p)]) + p)
            ar = s.recv(2)
            if len(ar) < 2 or ar[1] != 0x00:
                res["error"] = "认证失败"
                return res
        res["ok"] = True

        # 3) UDP ASSOCIATE（地址填 0.0.0.0:0，让服务端指定 relay）
        s.sendall(bytes([SOCKS5_VER, CMD_UDP_ASSOCIATE, 0x00, ATYP_IPV4,
                         0, 0, 0, 0, 0, 0]))
        rep = s.recv(10)
        if len(rep) < 4:
            res["error"] = f"UDP ASSOCIATE 响应过短: {rep.hex()}"
            return res
        if rep[0] != SOCKS5_VER:
            res["error"] = f"版本错误: {rep[0]:02x}"
            return res
        rep_code = rep[1]
        if rep_code != 0x00:
            code_map = {1: "general failure", 2: "connection not allowed",
                        3: "network unreachable", 4: "host unreachable",
                        5: "connection refused", 6: "TTL expired",
                        7: "command not supported（不支持 UDP）",
                        8: "address type not supported"}
            res["error"] = f"REP={rep_code} {code_map.get(rep_code,'?')}"
            return res

        # 4) 解析 relay 地址
        atyp = rep[3]
        if atyp == ATYP_IPV4 and len(rep) >= 10:
            ip = ".".join(str(b) for b in rep[4:8])
            rport = struct.unpack(">H", rep[8:10])[0]
            res["relay"] = (ip, rport)
            res["udp_supported"] = True
        elif atyp == ATYP_DOMAIN:
            dlen = rep[4]
            dom = rep[5:5 + dlen].decode(errors="replace")
            rport = struct.unpack(">H", rep[5 + dlen:7 + dlen])[0] if len(rep) >= 7 + dlen else 0
            res["relay"] = (dom, rport)
            res["udp_supported"] = True
        else:
            res["udp_supported"] = True
            res["relay"] = ("?", 0)

    except socket.timeout:
        res["error"] = "超时"
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"
    finally:
        if s:
            try:
                s.close()
            except Exception:
                pass
    return res


def udp_dns_test(relay, timeout=6):
    """通过 SOCKS5 UDP relay 发一个 DNS 查询，看有没有响应"""
    ip, port = relay
    if ip in ("0.0.0.0", "?", "") or not port:
        ip = None
    # 构造 DNS 查询：1.1.1.1:53 查 example.com A
    q = (b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
         b"\x07example\x03com\x00\x00\x01\x00\x01")
    # SOCKS5 UDP 头：RSV(2) FRAG(1) ATYP(1) DST.ADDR DST.PORT
    dst_ip = socket.inet_aton("1.1.1.1")
    hdr = b"\x00\x00\x00" + bytes([ATYP_IPV4]) + dst_ip + struct.pack(">H", 53)
    pkt = hdr + q
    try:
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        u.settimeout(timeout)
        target = (ip or "0.0.0.0", port)
        u.sendto(pkt, target)
        data, _ = u.recvfrom(2048)
        u.close()
        return len(data) > 10, len(data)
    except Exception as e:
        return False, str(e)[:50]


def main():
    ap = argparse.ArgumentParser(description="测试 SOCKS5 UDP ASSOCIATE 支持")
    ap.add_argument("--inventory")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    ap.add_argument("--token")
    ap.add_argument("--sample", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=10)
    args = ap.parse_args()

    targets = []
    if args.host and args.port:
        targets = [{"host": args.host, "socks": args.port,
                    "token": args.token or "", "country": "?"}]
    elif args.inventory and os.path.exists(args.inventory):
        data = json.load(open(args.inventory, encoding="utf-8"))
        by = {}
        for n in data:
            cc = (n.get("country") or "?").upper()
            by.setdefault(cc, []).append(n)
        for cc, lst in by.items():
            targets.extend(lst[:1])
        targets = targets[: args.sample]
    else:
        sys.exit("需要 --inventory 或 --host/--port/--token")

    print(f"{'国家':<5}{'入口':<30}{'认证':<11}{'UDP':<12}{'relay':<22}备注")
    print("-" * 94)

    udp_ok = 0
    for t in targets:
        cc = (t.get("country") or "?")[:4]
        host = t["host"]
        port = t.get("socks") or t.get("socks_proxy_port")
        token = t.get("token") or ""
        r = socks5_udp_test(host, port, token, "", args.timeout)

        if r["udp_supported"]:
            udp_ok += 1
            udp_s = "✅ 支持"
        elif r["ok"]:
            udp_s = "❌ 拒绝"
        else:
            udp_s = "—"
        relay_s = f"{r['relay'][0]}:{r['relay'][1]}" if r["relay"] else "-"
        note = r["error"][:28] if r["error"] else ""
        print(f"{cc:<5}{host:<30}{r['auth'] or '-':<11}{udp_s:<12}{relay_s:<22}{note}")

    print("-" * 94)
    print(f"\nUDP ASSOCIATE 支持: {udp_ok}/{len(targets)}")

    # 若支持，实测一个 DNS 查询
    if udp_ok:
        first = next((t for t in targets), None)
        if first:
            r = socks5_udp_test(first["host"], first.get("socks") or first.get("socks_proxy_port"),
                                first.get("token", ""), "", args.timeout)
            if r["udp_supported"] and r["relay"]:
                ok, detail = udp_dns_test(r["relay"])
                print(f"\n通过 relay 发 DNS 查询: {'✅ 有响应' if ok else '❌ 无响应'}  ({detail})")
                print("  relay 为 0.0.0.0 时，UDP 应发往控制连接的主机地址。")

    print("\n=== 结论 ===")
    if udp_ok == len(targets) and udp_ok > 0:
        print(f"  [判定] UDP ASSOCIATE 被接受 {udp_ok}/{len(targets)}")
        print("  还需验证 relay 可达：私有地址（10./172./192.168.）外部无法直连")
    elif udp_ok > 0:
        print(f"  [判定] UDP 支持不一致 {udp_ok}/{len(targets)}")
        print("  配置：udp: false")
    else:
        print("  [判定] 不支持 UDP ASSOCIATE")
        print("  配置：udp: false，仅 TCP over SOCKS5")


if __name__ == "__main__":
    main()
