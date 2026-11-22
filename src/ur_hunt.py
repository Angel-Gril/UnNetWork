#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ur_hunt.py —— 定向筛选器（按国家 + ISP 属性筛出想要的出口）
=============================================================
反复「签发 → 测出口 → 不符就删」，直到凑够 N 个符合条件的节点。

核心机制
--------
1. `initial_device_state.country_code` 让服务端只调度该地区的 provider
2. 每次签发占用 1 个槽位（免费版 5 个）
3. 签发后实测出口 IP，查 ISP / proxy 标记
4. 不符合条件 → `remove-client` 回收槽位，再签
5. 符合条件 → 保留，进池

限额（免费版，实测）
--------------------
  5 个活跃代理上限   → 脚本自动回收最旧的
  5 个/分钟 新建     → 脚本内置 12s 间隔
  每日流量额度       → 签发本身几乎不耗，跑代理才耗

用法
----
# 拉 3 个美国住宅 IP（要求 proxy=false）
python ur_hunt.py --jwt "urn_..." --country US --want 3

# 指定多个账号（每账号 5 槽位，合起来更多采样）
python ur_hunt.py --jwt-file keys.txt --country DE --want 2

# 只要 AT&T 或 Verizon
python ur_hunt.py --jwt "urn_..." --country US --want 2 --isp "AT&T,Verizon"

# 不筛 ISP，只要不是机房（proxy=false）
python ur_hunt.py --jwt "urn_..." --country JP --want 1 --residential-only
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://api.bringyour.com"
UA = "urnetwork-hunt/1.0"

PROXY_CONFIG = {
    "enable_http": True,
    "http_require_auth": True,
    "https_require_auth": True,
    "enable_socks": True,
    "enable_wg": False,
    "lock_caller_ip": False,
}

# 已知机房 AS（命中即视为非住宅）
DATACENTER_AS = {
    "AS200373",   # 3xK Tech GmbH
    "AS8560",     # IONOS SE
    "AS14061",    # DigitalOcean
    "AS16509",    # Amazon
    "AS15169",    # Google
    "AS13335",    # Cloudflare
    "AS20940",    # Akamai
    "AS24940",    # Hetzner
    "AS16276",    # OVH
    "AS20473",    # Vultr
    "AS63949",    # Linode
    "AS9009",     # M247
    "AS51852",    # PQ Hosting
}


# ------------------------------------------------------------------ HTTP
def _req(jwt, path, body=None, timeout=30):
    url = API_BASE + path
    headers = {"Authorization": "Bearer " + jwt, "Accept": "application/json",
               "User-Agent": UA}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8", "replace") or "{}")
        except Exception:
            return {}
    except Exception:
        return {}


def issue(jwt, country):
    body = {
        "proxy_config": dict(PROXY_CONFIG, initial_device_state={"country_code": country}),
        "locale": "en-US", "time_zone": "UTC", "provide_intent": False,
    }
    return _req(jwt, "/network/auth-client", body)


def list_slots(jwt):
    r = _req(jwt, "/network/proxies")
    return r.get("clients") or []


def free_slot(jwt, debug=False):
    """槽位满 5 就回收最旧的一个"""
    slots = list_slots(jwt)
    if len(slots) < 5:
        return True
    victim = slots[-1].get("client_id")
    r = _req(jwt, "/network/remove-client", {"client_id": victim})
    if debug:
        print(f"      回收槽位 {str(victim)[:8]}… -> {r}")
    time.sleep(1.5)
    return not (r.get("error"))


# ------------------------------------------------------- 出口探测
def _curl(args, timeout=30):
    """调 curl 子进程（Python 标准库不支持 SOCKS5，curl 最可靠）"""
    import shutil
    import subprocess
    exe = shutil.which("curl") or shutil.which("curl.exe")
    if not exe:
        return None
    try:
        r = subprocess.run([exe] + args, capture_output=True, text=True,
                           timeout=timeout, errors="replace")
        return r.stdout.strip()
    except Exception:
        return None


def probe_ip(host, port, auth_token, timeout=18, proto="socks5"):
    """
    只通过代理取出口 IP（不查详情）。

    返回 (ip, elapsed_ms) 或 (None, elapsed_ms)。
    详情交给 geo_batch.batch_lookup 批量查 —— 逐条直连查 ip-api 会
    撞上 45 次/分钟的限额（那是本机出口共享的），导致大量字段为空。
    """
    cred = auth_token + ":"
    if proto == "http":
        proxy = f"http://{host}:{port}"
    elif proto == "https":
        proxy = f"https://{host}:{port}"
    else:
        proxy = f"socks5h://{host}:{port}"

    import time as _t
    t0 = _t.time()
    out = _curl(["-s", "-m", str(timeout), "--proxy", proxy,
                 "--proxy-user", cred,
                 "http://ip-api.com/line/?fields=query"], timeout=timeout + 8)
    elapsed = (_t.time() - t0) * 1000
    ip = (out or "").strip()
    if not ip or " " in ip or "fail" in ip.lower():
        return None, int(elapsed)
    return ip, int(elapsed)


def probe_exit(host, port, auth_token, timeout=25, retries=1, proto="socks5"):
    """
    通过代理取出口 IP 与画像。

    proto 三选一（实测延迟差异很大）：
      socks5  首连 13-14s，之后 1-2s
      http    CONNECT 明文，0.7-1.3s   ← 最快
      https   TLS 到代理，2-3.4s
    """
    cred = auth_token + ":"
    if proto == "http":
        proxy = f"http://{host}:{port}"
    elif proto == "https":
        proxy = f"https://{host}:{port}"
    else:
        proxy = f"socks5h://{host}:{port}"

    ip = None
    elapsed = 0.0
    for _ in range(retries):
        import time as _t
        t0 = _t.time()
        out = _curl(["-s", "-m", str(timeout), "--proxy", proxy,
                     "--proxy-user", cred,
                     "http://ip-api.com/line/?fields=query"], timeout=timeout + 8)
        elapsed = _t.time() - t0
        if out:
            ip = out.strip()
            if ip:
                break
        time.sleep(1.5)
    if not ip:
        return None

    fields = "countryCode,country,regionName,city,isp,org,as,asname,proxy,hosting,mobile,query"
    out = _curl(["-s", "-m", "15",
                 f"http://ip-api.com/json/{ip}?fields={fields}"], timeout=22)
    try:
        d = json.loads(out) if out else {"query": ip}
    except Exception:
        d = {"query": ip}
    d["_probe_ms"] = int(elapsed * 1000)
    return d


def classify(info, want_isp=None, residential_only=False, want_country=None):
    """返回 (accept: bool, reason: str)"""
    if not info or not info.get("query"):
        return False, "探测失败"

    # 国家严格校验：country_code 只是偏好，实测请求 US 会落到 CA
    if want_country:
        got_cc = (info.get("countryCode") or "").upper()
        if got_cc != want_country.upper():
            return False, f"国家不符（要 {want_country.upper()} 实得 {got_cc or '?'}）"

    asn = (info.get("as") or "").split()[0] if info.get("as") else ""
    isp = (info.get("isp") or "") + " " + (info.get("org") or "") + " " + (info.get("asname") or "")
    is_proxy = info.get("proxy") is True
    is_hosting = info.get("hosting") is True

    if want_isp:
        hit = any(w.strip().lower() in isp.lower() for w in want_isp.split(",") if w.strip())
        if not hit:
            return False, f"ISP 不符（{info.get('isp','?')}）"

    if residential_only:
        if is_proxy or is_hosting or asn in DATACENTER_AS:
            return False, f"非住宅（proxy={is_proxy} hosting={is_hosting} {asn}）"

    tag = "住宅" if (not is_proxy and not is_hosting) else "机房"
    return True, f"{tag} {info.get('isp','?')} {asn}"


# --------------------------------------------------------------- 主流程
def hunt(jwt, country, want, want_isp=None, residential_only=False,
         max_rounds=40, debug=False, name="UR", strict_country=True):
    got = []
    tries = 0
    t_last = 0.0

    while len(got) < want and tries < max_rounds:
        tries += 1
        # 速率控制：5 个/分钟 → 12s 间隔
        gap = time.time() - t_last
        if gap < 12:
            time.sleep(12 - gap)
        t_last = time.time()

        print(f"\n[{tries}] 签发 country={country} …")
        free_slot(jwt, debug=debug)

        res = issue(jwt, country)
        pcr = res.get("proxy_config_result") or {}
        if not pcr:
            msg = (res.get("error") or {}).get("message", "unknown")
            print(f"    签发失败: {msg}")
            if "minute" in msg.lower():
                print("    触发速率限制，等待 60s")
                time.sleep(61)
            continue

        host = pcr.get("proxy_host")
        sport = pcr.get("socks_proxy_port")
        token = pcr.get("auth_token")
        blk = pcr.get("block", "?")
        print(f"    host={host} socks={sport} block={blk}")

        time.sleep(3)   # 给 provider 建立时间
        info = probe_exit(host, sport, token)
        ok, reason = classify(info, want_isp, residential_only,
                            want_country=country if strict_country else None)
        ip = (info or {}).get("query", "?")
        cc = (info or {}).get("countryCode", "?")

        print(f"    出口 {ip} [{cc}] {reason}  → {'✅ 保留' if ok else '❌ 丢弃'}")

        if ok:
            got.append({
                "name": f"{name}{len(got)+1}",
                "host": host, "socks": sport, "http": pcr.get("http_proxy_port"),
                "https": pcr.get("https_proxy_port"), "token": token,
                "block": blk, "ip": ip, "info": info, "reason": reason,
            })
        else:
            # 不符合，回收刚才那个槽位
            slots = list_slots(jwt)
            if slots:
                _req(jwt, "/network/remove-client", {"client_id": slots[-1].get("client_id")})
                time.sleep(1)

    return got


# --------------------------------------------------------------- 输出
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


def write_out(got, outdir, group="UR"):
    os.makedirs(outdir, exist_ok=True)
    proxies, uris = [], []
    for n in got:
        proxies.append({
            "name": f"{n['name']}-{n['info'].get('countryCode','??')}-{n['ip']}",
            "type": "socks5", "server": n["host"], "port": int(n["socks"]),
            "username": n["token"], "password": "", "udp": False,
        })
        proxies.append({
            "name": f"{n['name']}-{n['info'].get('countryCode','??')}-{n['ip']}-http",
            "type": "http", "server": n["host"], "port": int(n["http"]),
            "username": n["token"], "password": "", "tls": False,
        })
        uris.append(f"socks5h://{n['token']}:@{n['host']}:{n['socks']}"
                    f"#{urllib.parse.quote(n['name']+'-'+str(n['info'].get('countryCode',''))+'-'+n['ip'])}")
        uris.append(f"http://{n['token']}:@{n['host']}:{n['http']}"
                    f"#{urllib.parse.quote(n['name']+'-http-'+n['ip'])}")

    if not proxies:
        print("[!] 没有符合条件的结果")
        return []

    names = [p["name"] for p in proxies]
    socks_names = [p["name"] for p in proxies if p["type"] == "socks5"]
    cfg = {
        "mixed-port": 7890, "allow-lan": True, "mode": "rule",
        "log-level": "info", "ipv6": False,
        "proxies": proxies,
        "proxy-groups": [
            {"name": group, "type": "select", "proxies": names + ["DIRECT"]},
            {"name": "AUTO", "type": "url-test",
             "url": "http://www.gstatic.com/generate_204",
             "interval": 180, "tolerance": 50, "proxies": socks_names},
            {"name": "FALLBACK", "type": "fallback",
             "url": "http://www.gstatic.com/generate_204",
             "interval": 120, "proxies": socks_names},
        ],
        "rules": ["GEOIP,LAN,DIRECT,no-resolve", "GEOIP,CN,DIRECT", "MATCH," + group],
    }
    files = []
    p = os.path.join(outdir, "mihomo.yaml")
    open(p, "w", encoding="utf-8").write(dump_yaml(cfg) + "\n")
    files.append(p)

    p = os.path.join(outdir, "nodes.txt")
    open(p, "w", encoding="utf-8").write("\n".join(uris) + "\n")
    files.append(p)

    p = os.path.join(outdir, "subscription.txt")
    open(p, "w", encoding="utf-8").write(base64.b64encode("\n".join(uris).encode()).decode())
    files.append(p)

    p = os.path.join(outdir, "picked.json")
    open(p, "w", encoding="utf-8").write(json.dumps(got, ensure_ascii=False, indent=2))
    files.append(p)
    return files


def main():
    ap = argparse.ArgumentParser(description="URnetwork 定向出口筛选器")
    ap.add_argument("--jwt", help="单个 API key")
    ap.add_argument("--jwt-file", help="每行一个 key 的文件")
    ap.add_argument("--country", default="US", help="目标国家代码（US/JP/DE/GB…）")
    ap.add_argument("--want", type=int, default=3, help="想要几个符合条件的")
    ap.add_argument("--isp", help="ISP 白名单，逗号分隔，如 'AT&T,Verizon'")
    ap.add_argument("--residential-only", action="store_true", help="只要非机房（proxy=false 且非已知云 AS）")
    ap.add_argument("--no-strict-country", action="store_true", help="不校验落地国家（默认严格）")
    ap.add_argument("--max-rounds", type=int, default=40)
    ap.add_argument("--out", default="ur-hunt")
    ap.add_argument("--group", default="UR")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    keys = []
    if args.jwt:
        keys.append(args.jwt)
    if args.jwt_file and os.path.exists(args.jwt_file):
        keys += [l.strip() for l in open(args.jwt_file) if l.strip()]
    if not keys:
        ap.error("需要 --jwt 或 --jwt-file")

    print(f"目标: country={args.country}  想要 {args.want} 个"
          f"{'  ISP=' + args.isp if args.isp else ''}"
          f"{'  仅住宅' if args.residential_only else ''}")
    print(f"账号数: {len(keys)}（每账号 5 槽位）")
    print(f"预计耗时: 每轮 ~12s 起，命中率取决于该地区 provider 数量")

    all_got = []
    for i, k in enumerate(keys):
        if len(all_got) >= args.want:
            break
        print(f"\n{'='*56}\n账号 {i+1}/{len(keys)}")
        got = hunt(k, args.country, args.want - len(all_got),
                   want_isp=args.isp, residential_only=args.residential_only,
                   max_rounds=args.max_rounds, debug=args.debug,
                   name="UR", strict_country=not args.no_strict_country)
        all_got.extend(got)

    print(f"\n{'='*56}\n命中 {len(all_got)}/{args.want}")
    if all_got:
        print(f"\n{'节点':<8}{'出口 IP':<18}{'地区':<6}ISP")
        for n in all_got:
            print(f"{n['name']:<8}{n['ip']:<18}{str(n['info'].get('countryCode','?')):<6}{n['info'].get('isp','?')}")

    files = write_out(all_got, args.out, args.group)
    if files:
        print(f"\n已写入 {len(files)} 个文件到 {os.path.abspath(args.out)}")
        for f in files:
            print("  ", os.path.basename(f))


if __name__ == "__main__":
    main()
