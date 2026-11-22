#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ur_guest.py —— Guest 模式批量铸造（无需邮箱、无需验证码）
=========================================================
这是最快的一条路。实测一次完整铸造约 2 秒。

关键请求
--------
    POST /auth/network-create
    {
      "time_zone": "Asia/Shanghai",
      "locales": ["zh-CN", "zh"],
      "guest_mode": true,              ← 核心：不需要 user_auth / password
      "network_name": "guest-<uuid>",
      "terms": true,
      "product_updates": true
    }
    → 200 {
        "network": {"by_jwt": "...", "network_id": "...", "network_name": "..."},
        "seedphrase": "24 个单词"      ← 恢复凭据，务必备份
      }

    POST /account/api-key {"name": "..."}   (Bearer by_jwt)
    → 200 {"api_key": "urn_...", "id": "..."}

为什么这条路可行
----------------
邮箱注册路径（network-create 带 user_auth+password）会进入 verification_required，
必须收验证码；而服务端的邮件发送有严格的配额窗口，实测窗口很窄。
Guest 模式走的是 `guest_mode: true`，服务端直接建网络并返回 jwt + seedphrase，
**完全不触发邮件**，因此不受任何配额限制。

代价：guest 账号后续升级为正式账号才能绑定邮箱（`POST /auth/upgrade-guest`），
但不做升级也能拿 key、拉配置、出网。

用法
----
    # 生成 20 个
    python ur_guest.py --count 20 --out guests.json

    # 生成 100 个，并发 3
    python ur_guest.py --count 100 --workers 3 --out guests.json

    # 断点续跑
    python ur_guest.py --count 100 --out guests.json --resume
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

API_BASE = "https://api.bringyour.com"
UA = "urnetwork-guest/1.0"

# 复用 ur_register 的 HTTP 层（curl 子进程，超时可控）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ur_register as R

lock = threading.Lock()


def mint_guest(tag="g", proxy=None):
    """创建一个 guest 网络并铸造 key。返回 dict。"""
    name = "guest-%s" % uuid.uuid4()
    t0 = time.time()

    st, j = R.api_post("/auth/network-create", {
        "time_zone": "Asia/Shanghai",
        "locales": ["zh-CN", "zh"],
        "guest_mode": True,
        "network_name": name,
        "terms": True,
        "product_updates": True,
    }, proxy=proxy)

    if st == 429:
        return {"error": "429 network-create", "retry": True}
    if st != 200:
        return {"error": "network-create %d: %s" % (st, str(j)[:160])}

    net = j.get("network") or {}
    jwt = net.get("by_jwt")
    if not jwt:
        return {"error": "no by_jwt: %s" % str(j)[:160]}

    keyname = "%s-%s" % (tag, time.strftime("%m%d%H%M%S"))
    st2, j2 = R.api_post("/account/api-key", {"name": keyname}, jwt=jwt, proxy=proxy)
    if st2 != 200:
        return {"error": "api-key %d: %s" % (st2, str(j2)[:160])}

    api_key = (j2.get("api_key") or "").strip()
    if not api_key:
        return {"error": "empty api_key"}

    return {
        "api_key": api_key,
        "key_id": j2.get("id"),
        "key_name": j2.get("name") or keyname,
        "network_id": net.get("network_id"),
        "network_name": net.get("network_name"),
        "seedphrase": j.get("seedphrase"),
        "by_jwt": jwt,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "elapsed": round(time.time() - t0, 1),
    }


def main():
    ap = argparse.ArgumentParser(description="Guest 模式批量铸造 URnetwork API Key")
    ap.add_argument("--count", type=int, default=10, help="要生成多少个")
    ap.add_argument("--workers", type=int, default=1, help="并发（建议 1-3）")
    ap.add_argument("--interval", type=float, default=0, help="每个之间间隔秒数")
    ap.add_argument("--tag", default="g", help="key 名前缀")
    ap.add_argument("--out", default="guests.json")
    ap.add_argument("--proxy-file", action="append", default=[])
    ap.add_argument("--no-proxy", action="store_true")
    ap.add_argument("--resume", action="store_true", help="接着已有结果继续")
    ap.add_argument("--max-retry", type=int, default=2)
    ap.add_argument("--retry-wait", type=int, default=60)
    args = ap.parse_args()

    R.proxy_pool = [] if args.no_proxy else R.load_proxies(args.proxy_file, None)
    print("代理池: %d 个%s" % (len(R.proxy_pool), "（直连）" if not R.proxy_pool else ""))

    results = []
    if args.resume and os.path.exists(args.out):
        try:
            results = [r for r in json.load(open(args.out, encoding="utf-8"))
                       if r.get("api_key")]
        except Exception:
            pass
    print("已有: %d 个" % len(results))

    need = max(0, args.count - len(results))
    if need == 0:
        print("无需新增")
        return

    print("本次生成: %d 个，并发 %d\n" % (need, args.workers))
    t_start = time.time()
    ok = 0
    fail = 0

    def work(_i):
        # 轮换代理：每个 guest 用不同出口 IP，规避 IP 级 429
        proxy = None
        if R.proxy_pool:
            proxy = R.proxy_pool[_i % len(R.proxy_pool)]
        r = mint_guest(args.tag, proxy=proxy)
        retry = 0
        while r.get("retry") and retry < args.max_retry:
            retry += 1
            time.sleep(args.retry_wait)
            r = mint_guest(args.tag, proxy=proxy)
        return r

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(work, i) for i in range(need)]
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                r = fut.result()
            except Exception as e:
                r = {"error": "%s: %s" % (type(e).__name__, e)}

            with lock:
                if r.get("api_key"):
                    ok += 1
                    results.append(r)
                    print("[%d/%d] OK  %-22s %-14s %.1fs" % (
                        i, need, r["api_key"][:22] + "…",
                        r.get("network_name", ""), r.get("elapsed", 0)))
                else:
                    fail += 1
                    print("[%d/%d] ERR %s" % (i, need, str(r.get("error"))[:80]))

                # 实时落盘
                try:
                    json.dump(results, open(args.out, "w", encoding="utf-8"),
                              ensure_ascii=False, indent=2)
                    tp = os.path.splitext(args.out)[0] + ".txt"
                    with open(tp, "w", encoding="utf-8") as f:
                        f.write("\n".join(x["api_key"] for x in results) + "\n")
                    sp = os.path.splitext(args.out)[0] + "-seeds.txt"
                    with open(sp, "w", encoding="utf-8") as f:
                        for x in results:
                            f.write("%s\t%s\t%s\n" % (
                                x.get("network_name"), x["api_key"],
                                x.get("seedphrase") or ""))
                except Exception:
                    pass

            if args.interval > 0:
                time.sleep(args.interval)

    el = time.time() - t_start
    print("\n" + "=" * 56)
    print("成功 %d / 失败 %d / 目标 %d，耗时 %.0fs（%.1fs/个）" % (
        ok, fail, need, el, el / max(need, 1)))
    print("\n产出:")
    print("  %s            完整记录（含 seedphrase、jwt）" % args.out)
    print("  %s  纯 key 列表（喂 ur_hunt.py --jwt-file）" %
          (os.path.splitext(args.out)[0] + ".txt"))
    print("  %s  网络名/密钥/助记词对照" %
          (os.path.splitext(args.out)[0] + "-seeds.txt"))
    print("\n注意：seedphrase 是唯一恢复凭据，务必另行备份。")


if __name__ == "__main__":
    main()
