#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ur_oneshot.py —— 一次性导出 URnetwork 节点配置（单文件，零第三方依赖）
=====================================================================

不需要 VPS、不需要常驻服务、不需要 root、不需要 SDK。
只要拿到一次配置就能在手机上长期用。

两种输入
--------
1) 抓包得到的响应 JSON（推荐，最省事）
       python ur_oneshot.py --from-json auth-client.json

2) 直接用 by_client_jwt 现场请求
       python ur_oneshot.py --jwt "eyJhbGciOi..."

输出（全部写到 --out 目录，默认 ./ur-out）
------------------------------------------
    mihomo.yaml         mihomo / Clash.Meta / OpenClash / FlClash
    singbox.json        sing-box / SFA / SFI / SFM
    nodes.txt           通用 URI，一行一个（v2rayN / NekoBox / Shadowrocket 可粘贴）
    subscription.txt    base64 订阅内容
    ur.conf             WireGuard（仅 Pro 号有）
    summary.json        实际拿到了什么（判断免费/Pro）

手机上怎么用
------------
· Shadowrocket / NekoBox：直接粘 nodes.txt 里的行
· Clash.Meta / FlClash / OpenClash：导入 mihomo.yaml
· sing-box for Android：导入 singbox.json

为什么不用解码/解密
------------------
`POST /network/auth-client` 的响应就是明文 JSON，配置字段直接可读。
URnetwork 自有传输栈（QUIC/WebRTC/后量子 TLS）只作用于「客户端↔Provider」，
代理出口走的是**标准 HTTP / SOCKS5（Proxy-Authorization）**，
所以传统内核可以直连，无需实现它的任何私有协议。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://api.bringyour.com"
UA = "urnetwork-oneshot/1.0"

# 免费号只请求 HTTP/HTTPS（SOCKS/WG 属 Pro，请求了也不会下发）
PROXY_CONFIG_FREE = {
    "lock_caller_ip": False,      # 关键：false，否则换网络/WiFi 就断
    "enable_socks": False,
    "enable_http": True,
    "http_require_auth": True,
    "https_require_auth": True,
    "enable_wg": False,
    # 实测必需：不传 initial_device_state 服务端返回 "Invalid location"
    "initial_device_state": {"country_code": "US"},
}

# 全量请求（Pro 号用；免费号会自动降级为空）
PROXY_CONFIG_FULL = dict(PROXY_CONFIG_FREE, enable_socks=True, enable_wg=True)


# ------------------------------------------------------------------ 工具
def pick(d, *names, default=None):
    if not isinstance(d, dict):
        return default
    for n in names:
        if n in d and d[n] is not None:
            return d[n]
    return default


def b2s(n):
    try:
        n = float(n)
    except Exception:
        return str(n)
    for u in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024:
            return f"{n:.2f} {u}"
        n /= 1024.0
    return f"{n:.2f} PiB"


# ------------------------------------------------------------- 抓取 API
def fetch(jwt: str, free: bool, debug: bool) -> dict:
    body = {
        "proxy_config": PROXY_CONFIG_FREE if free else PROXY_CONFIG_FULL,
        "locale": "zh-CN",
        "time_zone": "Asia/Shanghai",
        "provide_intent": False,
    }
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        API_BASE + "/network/auth-client",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": "Bearer " + jwt,
            "User-Agent": UA,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        print(f"[!] HTTP {e.code}: {raw[:400]}", file=sys.stderr)
        if e.code in (401, 403):
            print("    → JWT 失效或非本账号；重新抓一次 /network/auth-client 响应即可", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"[!] 请求失败: {e}", file=sys.stderr)
        sys.exit(2)

    if debug:
        print("--- raw response ---")
        print(raw[:2000])
    try:
        return json.loads(raw)
    except ValueError:
        print(f"[!] 响应不是 JSON: {raw[:300]}", file=sys.stderr)
        sys.exit(2)


# --------------------------------------------------------------- 提取
class Ep:
    def __init__(self, result: dict, name: str = "UR"):
        self.name = name
        pcr = pick(result, "proxy_config_result", "proxyConfigResult", default={}) or {}
        self.pcr = pcr

        self.host = pick(pcr, "proxy_host", "proxyHost", default="") or ""
        self.keepalive = pick(pcr, "keepalive_seconds", "keepaliveSeconds", default=25) or 25
        self.api_base = pick(pcr, "api_base_url", "apiBaseUrl", default="") or ""
        self.auth_token = pick(pcr, "auth_token", "authToken", default="") or ""

        wg = pick(pcr, "wg_config", "wgConfig", default={}) or {}
        self.wg_priv = pick(wg, "client_private_key", "clientPrivateKey", default="") or ""
        self.wg_peer = pick(wg, "proxy_public_key", "proxyPublicKey", default="") or ""
        self.wg_ip = pick(wg, "client_ipv4", "clientIpv4", default="") or ""
        self.wg_port = pick(wg, "wg_proxy_port", "wgProxyPort", default=0) or 0
        self.wg_conf = pick(wg, "config", default="") or ""

        self.socks_port = pick(pcr, "socks_proxy_port", "socksProxyPort", default=0) or 0
        sa = pick(pcr, "socks_proxy_auth", "socksProxyAuth", default={}) or {}
        self.socks_user = pick(sa, "username", default="") or ""
        self.socks_pass = pick(sa, "password", default="") or ""

        self.http_port = pick(pcr, "http_proxy_port", "httpProxyPort", default=0) or 0
        ha = pick(pcr, "http_proxy_auth", "httpProxyAuth", default={}) or {}
        self.http_user = pick(ha, "username", default="") or ""
        self.http_pass = pick(ha, "password", default="") or ""

        # ---- 实测校正（2026-11-20）：
        # _auth 对象实际不存在，服务端只下发 auth_token。
        # 代理认证 = Basic base64(<auth_token>:)  —— token 作用户名，密码为空。
        # 已验证：HTTP 7148 / SOCKS5 7147 无认证返回 407，用 token 作用户名即通过。
        if not self.http_user and self.auth_token:
            self.http_user = self.auth_token
            self.http_pass = ""
        if not self.socks_user and self.auth_token:
            self.socks_user = self.auth_token
            self.socks_pass = ""

        self.https_port = pick(pcr, "https_proxy_port", "httpsProxyPort", default=0) or 0

        ex = pick(pcr, "exit", "Exit", default={}) or {}
        self.cc = pick(ex, "country_code", "countryCode", default="") or ""
        self.city = pick(ex, "city", "City", default="") or ""
        self.region = pick(ex, "region", "Region", default="") or ""
        self.exit_id = pick(ex, "exit_id", "exitId", default="") or ""

    has_wg = property(lambda s: bool(s.wg_priv and s.wg_peer and s.wg_port and s.host))
    has_socks = property(lambda s: bool(s.socks_port and s.host))
    has_http = property(lambda s: bool(s.http_port and s.host))

    def tag(self, kind):
        parts = [self.name] + [x for x in (self.cc, self.city) if x] + [kind]
        return "-".join(p.replace(" ", "") for p in parts if p)


# ----------------------------------------------------- 各格式生成
def gen_wg(ep):
    if ep.wg_conf.strip():
        return ep.wg_conf if ep.wg_conf.endswith("\n") else ep.wg_conf + "\n"
    if not ep.has_wg:
        return None
    addr = ep.wg_ip if "/" in ep.wg_ip else ep.wg_ip + "/32"
    return (
        "[Interface]\n"
        f"PrivateKey = {ep.wg_priv}\n"
        f"Address = {addr}\n"
        "DNS = 1.1.1.1\n"
        "MTU = 1420\n\n"
        "[Peer]\n"
        f"PublicKey = {ep.wg_peer}\n"
        "AllowedIPs = 0.0.0.0/0, ::/0\n"
        f"Endpoint = {ep.host}:{ep.wg_port}\n"
        f"PersistentKeepalive = {ep.keepalive}\n"
    )


def gen_mihomo(ep):
    return _mihomo_from([ep], "UR")


def _mihomo_from(eps, group):
    """把多套 Ep 合并成一个 mihomo 配置"""
    proxies = []
    for ep in eps:
        if ep.has_wg:
            ip = ep.wg_ip.split("/")[0] if ep.wg_ip else "10.0.0.2"
            proxies.append({
                "name": ep.tag("wg"), "type": "wireguard",
                "server": ep.host, "port": int(ep.wg_port), "ip": ip,
                "private-key": ep.wg_priv, "public-key": ep.wg_peer,
                "allowed-ips": ["0.0.0.0/0", "::/0"], "mtu": 1420,
                "udp": False, "remote-dns-resolve": True, "dns": ["1.1.1.1"],
                "persistent-keepalive": int(ep.keepalive),
            })
        if ep.has_socks:
            it = {"name": ep.tag("socks"), "type": "socks5",
                  "server": ep.host, "port": int(ep.socks_port), "udp": False}
            if ep.socks_user:
                it["username"] = ep.socks_user
                it["password"] = ep.socks_pass
            proxies.append(it)
        if ep.has_http:
            it = {"name": ep.tag("http"), "type": "http",
                  "server": ep.host, "port": int(ep.http_port), "tls": False}
            if ep.http_user:
                it["username"] = ep.http_user
                it["password"] = ep.http_pass
            proxies.append(it)
    if not proxies:
        return None

    names = [p["name"] for p in proxies]
    # 优先 socks5（实测比 http 稳、支持 UDP）
    primary = next((p["name"] for p in proxies if p["type"] == "socks5"), names[0])
    return {
        "mixed-port": 7890, "allow-lan": True, "mode": "rule",
        "log-level": "info", "ipv6": False,
        "proxies": proxies,
        "proxy-groups": [
            {"name": group, "type": "select",
             "proxies": [primary] + [n for n in names if n != primary] + ["DIRECT"]},
            {"name": "AUTO", "type": "url-test",
             "url": "http://www.gstatic.com/generate_204",
             "interval": 180, "tolerance": 50, "proxies": names},
        ],
        "rules": ["GEOIP,LAN,DIRECT,no-resolve", "GEOIP,CN,DIRECT", "MATCH," + group],
    }


def gen_singbox(ep):
    outs = []
    if ep.has_wg:
        ip = ep.wg_ip or "10.0.0.2"
        if "/" not in ip:
            ip += "/32"
        outs.append({
            "type": "wireguard", "tag": ep.tag("wg"),
            "server": ep.host, "server_port": int(ep.wg_port),
            "local_address": [ip], "private_key": ep.wg_priv,
            "peer_public_key": ep.wg_peer, "mtu": 1420,
        })
    if ep.has_socks:
        it = {"type": "socks", "tag": ep.tag("socks"), "server": ep.host,
              "server_port": int(ep.socks_port), "version": "5"}
        if ep.socks_user:
            it["username"] = ep.socks_user
            it["password"] = ep.socks_pass
        outs.append(it)
    if ep.has_http:
        it = {"type": "http", "tag": ep.tag("http"), "server": ep.host,
              "server_port": int(ep.http_port)}
        if ep.http_user:
            it["username"] = ep.http_user
            it["password"] = ep.http_pass
        outs.append(it)
    if not outs:
        return None
    outs.append({"type": "direct", "tag": "direct"})
    return {
        "log": {"level": "info", "timestamp": True},
        "inbounds": [{"type": "mixed", "tag": "mixed-in",
                      "listen": "::", "listen_port": 7890}],
        "outbounds": outs,
        "route": {"rules": [{"ip_is_private": True, "outbound": "direct"}],
                  "final": outs[0]["tag"], "auto_detect_interface": True},
    }


def gen_uris(ep):
    out = []
    tag = urllib.parse.quote(ep.tag("node"))
    if ep.has_socks:
        a = (f"{urllib.parse.quote(ep.socks_user, safe='')}:"
             f"{urllib.parse.quote(ep.socks_pass, safe='')}@" if ep.socks_user else "")
        out.append(f"socks5h://{a}{ep.host}:{ep.socks_port}#{tag}")
    if ep.has_http:
        a = (f"{urllib.parse.quote(ep.http_user, safe='')}:"
             f"{urllib.parse.quote(ep.http_pass, safe='')}@" if ep.http_user else "")
        out.append(f"http://{a}{ep.host}:{ep.http_port}#{tag}")
    return out


def gen_summary(ep):
    return {
        "proxy_host": ep.host,
        "wireguard": {"available": ep.has_wg,
                      "endpoint": f"{ep.host}:{ep.wg_port}" if ep.has_wg else None},
        "socks5": {"available": ep.has_socks,
                   "endpoint": f"{ep.host}:{ep.socks_port}" if ep.has_socks else None,
                   "auth": bool(ep.socks_user)},
        "http": {"available": ep.has_http,
                 "endpoint": f"{ep.host}:{ep.http_port}" if ep.has_http else None,
                 "auth": bool(ep.http_user)},
        "https_port": ep.https_port or None,
        "exit": {"country": ep.cc, "region": ep.region, "city": ep.city,
                 "exit_id": ep.exit_id},
        "verdict": ("PRO（含 WireGuard）" if ep.has_wg
                    else "FREE（HTTP+SOCKS 可用，无 WireGuard）"),
    }


def dump_yaml(obj, ind=0):
    sp = "  " * ind
    if isinstance(obj, dict):
        lines = []
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                lines.append(f"{sp}{k}:")
                lines.append(dump_yaml(v, ind + 1))
            else:
                lines.append(f"{sp}{k}: {yaml_scalar(v)}")
        return "\n".join(lines)
    if isinstance(obj, list):
        lines = []
        for v in obj:
            if isinstance(v, dict):
                body = dump_yaml(v, ind + 1).split("\n")
                lines.append(f"{sp}- {body[0].strip()}")
                lines.extend(body[1:])
            elif isinstance(v, list):
                lines.append(f"{sp}-")
                lines.append(dump_yaml(v, ind + 1))
            else:
                lines.append(f"{sp}- {yaml_scalar(v)}")
        return "\n".join(lines)
    return f"{sp}{yaml_scalar(obj)}"


def yaml_scalar(v):
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


# ----------------------------------------------------------------- main
def api_get(jwt, path, debug=False):
    req = urllib.request.Request(API_BASE + path, headers={
        "Authorization": "Bearer " + jwt, "Accept": "application/json",
        "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        if debug:
            print(f"    GET {path} -> {e}")
        return {}


def api_post(jwt, path, body, debug=False):
    req = urllib.request.Request(
        API_BASE + path, data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + jwt,
                 "Content-Type": "application/json",
                 "Accept": "application/json", "User-Agent": UA},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode("utf-8", "replace") or "{}")
    except Exception as e:
        if debug:
            print(f"    POST {path} -> {e}")
        return {}


def rotate_eps(jwt, count, free, name="UR", debug=False):
    """
    循环收集 count 套配置。
    免费版限制：5 个活跃代理 + 5 个/分钟 新建。
    满 5 个时自动回收最旧的一个。
    """
    import time
    eps = []
    for i in range(count):
        if i > 0:
            # 速率控制：每分钟最多 5 个
            time.sleep(1.5 if i % 5 else 61)

        # 槽位管理
        pl = api_get(jwt, "/network/proxies", debug=debug)
        clients = pl.get("clients") or []
        if len(clients) >= 5:
            victim = clients[-1].get("client_id")
            r = api_post(jwt, "/network/remove-client", {"client_id": victim}, debug=debug)
            if debug:
                print(f"    回收槽位 {str(victim)[:8]}... -> {r}")
            time.sleep(1.5)

        res = api_post(jwt, "/network/auth-client",
                       {"proxy_config": PROXY_CONFIG_FREE if free else PROXY_CONFIG_FULL,
                        "locale": "en-US", "time_zone": "UTC", "provide_intent": False},
                       debug=debug)
        pcr = res.get("proxy_config_result") or {}
        if not pcr:
            err = (res.get("error") or {}).get("message", "unknown")
            print(f"  [{i+1}/{count}] 签发失败: {err}")
            continue

        ep = Ep(res, name=f"{name}{i+1}")
        blk = pcr.get("block", "?")
        print(f"  [{i+1}/{count}] {ep.host}:{ep.http_port} (socks {ep.socks_port}) block={blk}")
        eps.append(ep)
    return eps


def main():
    ap = argparse.ArgumentParser(description="URnetwork 一次性配置导出")
    ap.add_argument("--jwt", help="by_client_jwt（现场请求）")
    ap.add_argument("--from-json", help="抓包得到的 auth-client 响应 JSON 文件")
    ap.add_argument("--pro", action="store_true", help="请求 WG/SOCKS（Pro 号）")
    ap.add_argument("--out", default="ur-out")
    ap.add_argument("--name", default="UR", help="节点名前缀")
    ap.add_argument("--rotate", type=int, default=0, help="轮换签发 N 套配置（免费版上限 5 并发）")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    if not args.jwt and not args.from_json:
        ap.error("需要 --jwt 或 --from-json 之一")

    if args.from_json:
        print(f"[*] 读取抓包文件 {args.from_json}")
        with open(args.from_json, "r", encoding="utf-8") as f:
            result = json.load(f)
        # 兼容：抓包文件可能是完整响应，也可能是 {proxy_config_result:{...}}
        if "proxy_config_result" not in result and "proxyConfigResult" not in result:
            for k in ("result", "data", "body"):
                if isinstance(result.get(k), dict):
                    result = result[k]
                    break
    else:
        print(f"[*] 请求 {API_BASE}/network/auth-client")
        result = fetch(args.jwt, free=not args.pro, debug=args.debug)

    # ---- 收集 Ep 列表（单套 或 轮换多套）----
    if args.rotate and args.rotate > 1:
        if not args.jwt:
            print("[!] --rotate 需要 --jwt", file=sys.stderr)
            sys.exit(2)
        print(f"[*] 轮换签发 {args.rotate} 套（免费版：5 并发上限 / 5 每分鐘）")
        eps = rotate_eps(args.jwt, args.rotate, not args.pro,
                         name=args.name, debug=args.debug)
        if not eps:
            print("[!] 一套都没拿到", file=sys.stderr)
            sys.exit(3)
    else:
        ep0 = Ep(result, name=args.name)
        if not ep0.host:
            print("[!] 没找到 proxy_host —— 响应里没有 proxy_config_result", file=sys.stderr)
            print("    顶层键:", list(result.keys()), file=sys.stderr)
            sys.exit(3)
        eps = [ep0]

    ep = eps[0]
    os.makedirs(args.out, exist_ok=True)
    written = []

    def save(name, text):
        if not text:
            return
        p = os.path.join(args.out, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text if text.endswith("\n") else text + "\n")
        written.append(p)

    sm = gen_summary(ep)
    if len(eps) > 1:
        sm["nodes"] = [gen_summary(e) for e in eps]
        sm["count"] = len(eps)
    save("summary.json", json.dumps(sm, ensure_ascii=False, indent=2))

    wg = gen_wg(ep)
    save("ur.conf", wg)

    mh = _mihomo_from(eps, "UR")
    save("mihomo.yaml", dump_yaml(mh) if mh else None)

    sb = gen_singbox(ep)
    save("singbox.json", json.dumps(sb, ensure_ascii=False, indent=2) if sb else None)

    uris = [u for e in eps for u in gen_uris(e)]
    save("nodes.txt", "\n".join(uris) if uris else None)
    if uris:
        save("subscription.txt", base64.b64encode("\n".join(uris).encode()).decode())

    # --------- 报告 ---------
    print("\n" + "=" * 60)
    if len(eps) > 1:
        print(f"  已获取 {len(eps)} 套配置：")
        for e in eps:
            print(f"    {e.name:<8} {e.host}:{e.http_port}  socks {e.socks_port}")
    else:
        print(f"  出口主机 : {ep.host}")
        print(f"  出口位置 : {ep.cc or '?'} / {ep.region or '-'} / {ep.city or '-'}")
    print(f"  账号类型 : {sm['verdict']}")
    print(f"  WireGuard: {'可用 ' + sm['wireguard']['endpoint'] if ep.has_wg else '不可用（Pro 功能）'}")
    print(f"  SOCKS5   : {'可用 ' + sm['socks5']['endpoint'] if ep.has_socks else '不可用（Pro 功能）'}")
    print(f"  HTTP     : {'可用 ' + sm['http']['endpoint'] if ep.has_http else '不可用'}")
    print(f"  HTTPS    : 端口 {ep.https_port or '-'}")
    print("=" * 60)
    print(f"  已写入 {len(written)} 个文件到 {os.path.abspath(args.out)}")
    for p in written:
        print("   ", os.path.basename(p))

    if uris:
        print("\n手机可直接粘贴的节点行：")
        for u in uris:
            print("  " + u)
    else:
        print("\n[!] 没有任何可用端点。检查账号额度或稍后重试。")

    if not ep.has_wg and not ep.has_socks:
        print("\n提示：这是免费号。SOCKS5 与 WireGuard 属 Pro 功能，")
        print("      免费可用的是 HTTP/HTTPS 代理 —— 上面 nodes.txt 里的 http:// 行就是。")


if __name__ == "__main__":
    main()
