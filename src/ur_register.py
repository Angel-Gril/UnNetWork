#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ur_register.py —— 邮箱批量注册 + 铸造 API Key（带代理轮换）
============================================================
纯 API，无浏览器。解决 IP 级限流。

实测发现的完整注册链路
----------------------
    ① POST /auth/network-create {user_auth, terms:true, network_name, password}
         → 200 {network:{network_id,network_name}, verification_required}
         ⚠ 这是【注册】入口。只有它能让新邮箱变成账号。
    ② POST /auth/login {user_auth}          → 发验证码
         （verify-send 对新账号返回 "Invalid login."，login 不会）
    ③ 读邮箱，取验证码
         6 位数字（verify_otp_numeric=true）或 8 位十六进制（false）
    ④ POST /auth/verify {user_auth, verify_code} → {network:{by_jwt}}
    ⑤ POST /account/api-key {name}  (Bearer by_jwt) → {api_key}

限流实测
--------
    "Too many recent sign-in attempts for this account from your network address."
                                                              ^^^^^^^^^^^^^^^^^^^^^
    IP 级 + 账号级双重限流，触发后锁 5 分钟。
    ⇒ 必须靠代理轮换，否则 102 个要跑 8 小时以上。

代理格式（--proxy-file，每行一个）
--------------------------------
    http://<auth_token>:@<host>:<http_port>
    http://user:pass@host:port
    host:port

    这些可以直接从 ur_oneshot.py / ur_fleet.py 产出的 nodes.txt 里取 http:// 行。

用法
----
    # 单账号验证链路（不用代理）
    python ur_register.py --mail-file "C:\\AID\\邮箱账号导出_2026-11-20.txt" --limit 1 --no-proxy

    # 批量注册 + 代理轮换
    python ur_register.py --mail-file "..." --proxy-file proxies.txt --workers 2 --limit 50

    # 从已有节点文件自动提取代理
    python ur_register.py --mail-file "..." --proxy-nodes ur-fleet/nodes.txt --workers 2
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

API_BASE = "https://api.bringyour.com"
UA = "urnetwork-register/1.0"
MS_TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
GRAPH_MSG_URL = "https://graph.microsoft.com/v1.0/me/messages"

# 同时兼容 6 位数字与 8 位十六进制
CODE_PATTERNS = [
    re.compile(r"\b([0-9a-f]{8})\s+is your URnetwork verification code", re.I),
    re.compile(r"\b(\d{6})\s+is your URnetwork verification code", re.I),
    re.compile(r"verification code[^0-9a-f]{0,10}([0-9a-f]{6,10})", re.I),
]

lock = threading.Lock()
proxy_pool: list[str] = []
proxy_lock = threading.Lock()
proxy_dead: set[str] = set()


# ---------------------------------------------------------------- 代理
def load_proxies(paths, nodes_file=None):
    out = []
    for p in paths or []:
        if not os.path.exists(p):
            continue
        for line in open(p, encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            out.append(normalize_proxy(line))
    if nodes_file and os.path.exists(nodes_file):
        for line in open(nodes_file, encoding="utf-8", errors="replace"):
            line = line.strip()
            if line.startswith("http://"):
                out.append(normalize_proxy(line))
    # 去重
    seen, uniq = set(), []
    for x in out:
        if x and x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


def normalize_proxy(s):
    s = s.split("#")[0].strip()
    if s.startswith("socks"):
        return None          # urllib 不支持 socks，跳过
    if not s.startswith("http"):
        s = "http://" + s
    return s


def pick_proxy():
    with proxy_lock:
        alive = [p for p in proxy_pool if p not in proxy_dead]
    if not alive:
        return None
    return random.choice(alive)


def mark_proxy_dead(p):
    if not p:
        return
    with proxy_lock:
        proxy_dead.add(p)


def _opener(proxy=None):
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ---------------------------------------------------------------- HTTP
def _proxy_parts(p):
    """把 http://user:pass@host:port 拆成 (host:port, 'user:pass')"""
    if not p:
        return None, None
    u = urllib.parse.urlparse(p if "://" in p else "http://" + p)
    hp = f"{u.hostname}:{u.port or 80}"
    cred = None
    if u.username is not None:
        cred = f"{u.username}:{u.password or ''}"
    return hp, cred


def _http(url, data=None, headers=None, timeout=30, proxy=None, method=None):
    """
    用 curl 子进程发请求。
    原因：urllib 的 ProxyHandler 不会为 CONNECT 隧道自动带 Proxy-Authorization，
    遇到需要认证的代理会 407。curl 的 -U 处理得干净。
    """
    import shutil
    import subprocess
    import tempfile

    exe = shutil.which("curl") or shutil.which("curl.exe")
    if not exe:
        return 0, "curl not found"

    args = [exe, "-s", "--connect-timeout", "8", "-m", str(timeout), "-X", method or ("POST" if data else "GET"),
            "-w", "\n__STATUS__%{http_code}"]
    hp, cred = _proxy_parts(proxy)
    if hp:
        args += ["-x", "http://" + hp]
        if cred:
            args += ["-U", cred]

    for k, v in (headers or {}).items():
        args += ["-H", f"{k}: {v}"]

    tmp = None
    if data:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".bin")
        tmp.write(data)
        tmp.close()
        args += ["--data-binary", "@" + tmp.name]

    args.append(url)
    try:
        r = subprocess.run(args, capture_output=True, text=True,
                           timeout=max(8, timeout + 5), errors="replace")
        out = r.stdout or ""
        st = 0
        if "__STATUS__" in out:
            body, _, code = out.rpartition("__STATUS__")
            try:
                st = int(code.strip())
            except Exception:
                st = 0
            return st, body.strip()
        return 0, out.strip() or (r.stderr or "").strip()
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"
    finally:
        if tmp:
            try:
                os.unlink(tmp.name)
            except Exception:
                pass


def api_post(path, body, jwt=None, timeout=30, proxy=None):
    h = {"Content-Type": "application/json", "Accept": "application/json",
         "User-Agent": UA}
    if jwt:
        h["Authorization"] = "Bearer " + jwt
    st, txt = _http(API_BASE + path, json.dumps(body).encode(), h, timeout, proxy)
    try:
        return st, (json.loads(txt) if txt else {})
    except Exception:
        return st, {"_raw": txt}


# ------------------------------------------------------ Microsoft Graph
def ms_token(client_id, refresh_token, timeout=30, proxy=None):
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token", "client_id": client_id,
        "refresh_token": refresh_token,
        "scope": "https://graph.microsoft.com/.default offline_access",
    }).encode()
    st, txt = _http(MS_TOKEN_URL, body,
                    {"Content-Type": "application/x-www-form-urlencoded"},
                    timeout, proxy)
    if st != 200:
        return None, f"token {st}: {txt[:140]}"
    try:
        return json.loads(txt).get("access_token"), None
    except Exception as e:
        return None, f"token parse: {e}"


def graph_code(access_token, since_ts=None, timeout=25, proxy=None):
    # 只取 since_ts 之后到达的邮件，避免读到历史旧码
    url = (GRAPH_MSG_URL + "?$top=8&$select=subject,receivedDateTime"
           + "&$orderby=receivedDateTime%20desc")
    if since_ts:
        iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since_ts - 120))
        url += "&$filter=" + urllib.parse.quote(f"receivedDateTime ge {iso}")
    st, txt = _http(url, None,
                    {"Authorization": "Bearer " + access_token,
                     "Accept": "application/json"}, timeout, proxy)
    if st != 200:
        return None, f"graph {st}: {txt[:140]}"
    try:
        msgs = json.loads(txt).get("value") or []
    except Exception as e:
        return None, f"graph parse: {e}"

    for m in msgs:
        subj = m.get("subject") or ""
        if "ur" not in subj.lower():
            continue
        for pat in CODE_PATTERNS:
            hit = pat.search(subj)
            if hit:
                return hit.group(1), None
    return None, "no code in mail"


# --------------------------------------------------------------- 单账号
def reg_one(rec, tag="vps", wait_code=8, use_proxy=True, numeric=True, code_timeout=90, fixed_password=None):
    email = rec["email"]
    user_auth = email.lower()
    # network_name 必须全局唯一，加随机后缀避免 409
    _base = re.sub(r"[^a-z0-9]", "", user_auth.split("@")[0])[:16] or "net"
    net_name = f"{_base}{random.randint(1000, 9999)}"
    password = fixed_password or rec.get("password") or ("Pw" + base64.b32encode(os.urandom(9)).decode().rstrip("="))
    proxy = pick_proxy() if use_proxy else None
    log = []

    # ① 注册 network（已存在会返回 error，视为可继续）
    st, j = api_post("/auth/network-create", {
        "user_auth": user_auth, "terms": True,
        "network_name": net_name, "password": password,
        "time_zone": "Asia/Shanghai", "locales": ["zh-CN"],
        "verify_otp_numeric": numeric,
    }, proxy=proxy)
    log.append(f"netcreate {st}")
    if st == 429:
        return {"email": email, "error": "429 netcreate", "proxy": proxy, "retry": True}
    if st == 0:
        mark_proxy_dead(proxy)
        return {"email": email, "error": f"proxy fail: {str(j)[:80]}", "retry": True}
    if st != 200:
        msg = str(j)[:160]
        # 已存在也算可继续（走登录）
        if "already" not in msg.lower() and "exist" not in msg.lower():
            return {"email": email, "error": f"netcreate {st}: {msg}"}

    # ② 发码
    st, j = api_post("/auth/login", {
        "user_auth": user_auth, "time_zone": "Asia/Shanghai",
        "locales": ["zh-CN"],
    }, proxy=proxy)
    log.append(f"login {st}")
    if st == 429:
        return {"email": email, "error": "429 login", "proxy": proxy, "retry": True}
    if st != 200:
        return {"email": email, "error": f"login {st}: {str(j)[:140]}"}

    sent_at = time.time()      # 记住发码时刻，读信只认这之后到的
    time.sleep(wait_code)

    # ③ 读信
    at, err = ms_token(rec["client_id"], rec["refresh_token"], timeout=20, proxy=None)
    if not at:
        return {"email": email, "error": err}

    code = None
    deadline = time.time() + code_timeout
    while time.time() < deadline:
        code, err = graph_code(at, since_ts=sent_at, timeout=15, proxy=None)
        if code:
            break
        time.sleep(6)
    if not code:
        return {"email": email, "error": err or "code not found"}

    # ④ 校验换 JWT
    st, j = api_post("/auth/verify", {
        "user_auth": user_auth, "verify_code": code,
        "time_zone": "Asia/Shanghai", "locales": ["zh-CN"],
    }, proxy=proxy)
    log.append(f"verify {st}")
    jwt = ((j.get("network") or {}).get("by_jwt")) if st == 200 else None
    if not jwt:
        if st == 429:
            return {"email": email, "error": "429 verify", "proxy": proxy, "retry": True}
        return {"email": email, "error": f"verify {st}: {str(j)[:140]}"}

    # ⑤ 建 key
    keyname = f"{tag}-{time.strftime('%m%d%H%M')}"
    st, j = api_post("/account/api-key", {"name": keyname}, jwt=jwt, proxy=proxy)
    key = (j.get("api_key") or "").strip() if st == 200 else None
    if not key:
        return {"email": email, "error": f"api-key {st}: {str(j)[:140]}"}

    return {
        "email": email, "api_key": key, "key_id": j.get("id"),
        "name": j.get("name") or keyname, "network_name": net_name,
        "jwt_len": len(jwt), "proxy": proxy,
        "minted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


# ----------------------------------------------------------------- main
def parse_mail_file(path):
    out = []
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        p = line.split("----")
        if len(p) < 4:
            continue
        out.append({"email": p[0].strip(), "password": p[1].strip(),
                    "client_id": p[2].strip(), "refresh_token": p[3].strip()})
    return out


def main():
    ap = argparse.ArgumentParser(description="邮箱批量注册 URnetwork + 铸造 API Key")
    ap.add_argument("--mail-file", required=True)
    ap.add_argument("--proxy-file", action="append", default=[], help="代理列表文件（可多次）")
    ap.add_argument("--proxy-nodes", help="从 nodes.txt 自动提取 http:// 代理")
    ap.add_argument("--no-proxy", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--tag", default="vps")
    ap.add_argument("--password", help="统一的账号密码（不填则用邮箱文件里的密码）")
    ap.add_argument("--wait-code", type=int, default=9)
    ap.add_argument("--code-timeout", type=int, default=90, help="读信最多等多久（秒）")
    ap.add_argument("--retry-wait", type=int, default=300, help="遇 429 等多久重试")
    ap.add_argument("--interval", type=int, default=0, help="每个账号之间强制间隔秒数（慢速模式，绕邮件配额）")
    ap.add_argument("--max-retry", type=int, default=1)
    ap.add_argument("--out", default="registered.json")
    ap.add_argument("--skip-existing", action="store_true")
    args = ap.parse_args()

    global proxy_pool
    if not args.no_proxy:
        proxy_pool = load_proxies(args.proxy_file, args.proxy_nodes)
        print(f"代理池: {len(proxy_pool)} 个" + ("（无，将直连）" if not proxy_pool else ""))

    recs = parse_mail_file(args.mail_file)
    print(f"邮箱记录: {len(recs)} 条")

    existing = []
    done = set()
    if os.path.exists(args.out):
        try:
            existing = json.load(open(args.out, encoding="utf-8"))
            for r in existing:
                if r.get("api_key"):
                    done.add(r["email"].lower())
        except Exception:
            pass
    if args.skip_existing and done:
        recs = [r for r in recs if r["email"].lower() not in done]
        print(f"跳过已完成 {len(done)}，剩余 {len(recs)}")
    if args.limit:
        recs = recs[: args.limit]

    print(f"本次处理 {len(recs)}，并发 {args.workers}")
    print(f"注意：注册接口有 IP 级限流，无代理时每 5 分钟约 1-2 个\n")

    ok, failed = 0, []
    results = list(existing)
    t0 = time.time()

    _gate = {"t": 0.0}
    _glock = threading.Lock()

    def work(rec):
        if args.interval > 0:
            with _glock:
                gap = time.time() - _gate["t"]
                if gap < args.interval:
                    wait = args.interval - gap
                    print(f"    ⏳ 等待 {wait:.0f}s（间隔 {args.interval}s，避邮件配额）")
                    time.sleep(wait)
                _gate["t"] = time.time()
        r = reg_one(rec, args.tag, args.wait_code, not args.no_proxy,
                        code_timeout=args.code_timeout, fixed_password=args.password)
        retry = 0
        while r.get("retry") and retry < args.max_retry:
            retry += 1
            time.sleep(args.retry_wait)
            r = reg_one(rec, args.tag, args.wait_code, not args.no_proxy,
                        code_timeout=args.code_timeout, fixed_password=args.password)
        return r

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(work, r): r for r in recs}
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                res = fut.result()
            except Exception as e:
                res = {"email": futs[fut]["email"], "error": f"{type(e).__name__}: {e}"}
            with lock:
                if res.get("api_key"):
                    ok += 1
                    results.append(res)
                    px = "via proxy" if res.get("proxy") else "direct"
                    print(f"[{i}/{len(recs)}] ✅ {res['email']:<38} "
                          f"{res['api_key'][:22]}…  ({px})")
                else:
                    failed.append(res)
                    print(f"[{i}/{len(recs)}] ❌ {res['email']:<38} "
                          f"{res.get('error','?')[:64]}")
                good = [x for x in results if x.get("api_key")]
                json.dump(good, open(args.out, "w", encoding="utf-8"),
                          ensure_ascii=False, indent=2)
                tp = os.path.splitext(args.out)[0] + ".txt"
                open(tp, "w", encoding="utf-8").write(
                    "\n".join(x["api_key"] for x in good) + ("\n" if good else ""))

    el = time.time() - t0
    print(f"\n{'='*60}")
    print(f"成功 {ok} / 失败 {len(failed)} / 处理 {len(recs)}  耗时 {el:.0f}s")
    if failed:
        fp = os.path.splitext(args.out)[0] + "-failed.txt"
        open(fp, "w", encoding="utf-8").write(
            "\n".join(f"{x['email']}\t{x.get('error','?')}" for x in failed))
        print(f"失败明细: {fp}")
    print(f"\nkey 列表: {os.path.splitext(args.out)[0]}.txt")


if __name__ == "__main__":
    main()
