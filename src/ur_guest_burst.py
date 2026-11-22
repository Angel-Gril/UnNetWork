#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ur_guest_burst.py —— 多出口 IP 并行铸造
======================================
服务端的限流消息说得很明确：

    "Too many accounts have been created recently from your network address.
     This limit is scoped to the address you are connecting from...
     Please try again later or from a different connection."

即：**配额按出口 IP 计算，换 IP 就是换配额。**

所以做法是：每个代理一个线程，各自独立跑，直到该 IP 配额耗尽（429），
然后该线程退出。全部线程跑完即为一轮。

用法
----
    python ur_guest_burst.py --proxy-file proxies.txt --out burst.json

    # 每轮之后等一段时间再跑（配额按时间窗恢复）
    python ur_guest_burst.py --proxy-file proxies.txt --out burst.json \
        --rounds 6 --round-wait 1800

产出
----
    burst.json          完整记录（含 seedphrase）
    burst.txt           纯 key 列表
    burst-seeds.txt     网络名 / key / 助记词
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ur_register as R

lock = threading.Lock()


def mint_one(proxy, tag="b"):
    """单次创建；返回 dict 或 None（429 时返回 None 表示该 IP 配额用尽）"""
    name = "guest-%s" % uuid.uuid4()
    st, j = R.api_post("/auth/network-create", {
        "time_zone": "Asia/Shanghai", "locales": ["zh-CN", "zh"],
        "guest_mode": True, "network_name": name,
        "terms": True, "product_updates": True,
    }, proxy=proxy)

    if st == 429:
        return {"exhausted": True, "proxy": proxy}

    if st != 200:
        return {"error": "netcreate %d: %s" % (st, str(j)[:120]), "proxy": proxy}

    net = j.get("network") or {}
    jwt = net.get("by_jwt")
    if not jwt:
        return {"error": "no jwt", "proxy": proxy}

    keyname = "%s-%s" % (tag, time.strftime("%m%d%H%M%S"))
    st2, j2 = R.api_post("/account/api-key", {"name": keyname}, jwt=jwt, proxy=proxy)
    if st2 != 200:
        return {"error": "api-key %d" % st2, "proxy": proxy}

    api_key = (j2.get("api_key") or "").strip()
    if not api_key:
        return {"error": "empty key", "proxy": proxy}

    return {
        "api_key": api_key,
        "key_id": j2.get("id"),
        "network_id": net.get("network_id"),
        "network_name": net.get("network_name"),
        "seedphrase": j.get("seedphrase"),
        "by_jwt": jwt,
        "proxy": proxy,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def worker(proxy, results, tag, max_per_proxy=30, delay=2.0, verbose=True):
    """一个代理线程：连续创建直到 429"""
    n = 0
    errs = 0
    label = (proxy or "direct").split("@")[-1]
    while n < max_per_proxy:
        r = mint_one(proxy, tag)
        if r and r.get("exhausted"):
            if verbose:
                print("  [%s] 配额用尽，本 IP 停止" % label, flush=True)
            return n
        if r and r.get("api_key"):
            n += 1
            with lock:
                results.append(r)
            if verbose:
                print("  [%s] #%d %s" % (label, n, r["api_key"][:24] + "…"), flush=True)
        else:
            errs += 1
            if verbose:
                print("  [%s] 错误: %s" % (label, str((r or {}).get("error"))[:60]), flush=True)
            if errs >= 3:
                if verbose:
                    print("  [%s] 连续 %d 次错误，本 IP 停止" % (label, errs), flush=True)
                return n
            time.sleep(5)
            continue
        time.sleep(delay)
    return n


def save(results, out):
    try:
        json.dump(results, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        base = os.path.splitext(out)[0]
        with open(base + ".txt", "w", encoding="utf-8") as f:
            f.write("\n".join(x["api_key"] for x in results) + "\n")
        with open(base + "-seeds.txt", "w", encoding="utf-8") as f:
            for x in results:
                f.write("%s\t%s\t%s\n" % (x.get("network_name"), x["api_key"],
                                          x.get("seedphrase") or ""))
    except Exception as e:
        print("  保存失败:", e)


def main():
    ap = argparse.ArgumentParser(description="多出口 IP 并行铸造 guest 账号")
    ap.add_argument("--proxy-file", action="append", default=[])
    ap.add_argument("--include-direct", action="store_true", help="也包含直连（本机 IP）")
    ap.add_argument("--tag", default="b")
    ap.add_argument("--rounds", type=int, default=1, help="跑几轮（每轮重新尝试所有 IP）")
    ap.add_argument("--round-wait", type=int, default=1800, help="轮次之间等待秒数")
    ap.add_argument("--delay", type=float, default=2.0, help="同一 IP 连续请求间隔")
    ap.add_argument("--max-per-proxy", type=int, default=30)
    ap.add_argument("--out", default="burst.json")
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()

    proxies = R.load_proxies(args.proxy_file, None)
    if args.include_direct or not proxies:
        proxies = [None] + proxies
    print("出口数: %d（含直连: %s）" % (len(proxies), bool(args.include_direct)))

    results = []
    if args.resume and os.path.exists(args.out):
        try:
            results = [r for r in json.load(open(args.out, encoding="utf-8"))
                       if r.get("api_key")]
        except Exception:
            pass
    print("已有: %d" % len(results))

    t0 = time.time()
    for rd in range(1, args.rounds + 1):
        print("\n===== 第 %d 轮（%s）=====" % (rd, time.strftime("%H:%M:%S")))
        before = len(results)

        with ThreadPoolExecutor(max_workers=len(proxies)) as ex:
            futs = [ex.submit(worker, p, results, args.tag, args.max_per_proxy, args.delay)
                    for p in proxies]
            for f in as_completed(futs):
                try:
                    f.result()
                except Exception as e:
                    print("  线程异常:", e)

        got = len(results) - before
        print("\n第 %d 轮: +%d 个（累计 %d）" % (rd, got, len(results)))
        save(results, args.out)

        if rd < args.rounds:
            if got == 0:
                print("本轮 0 命中，等 %d 分钟" % (args.round_wait // 60))
            else:
                print("等 %d 分钟让配额恢复" % (args.round_wait // 60))
            time.sleep(args.round_wait)

    el = time.time() - t0
    print("\n" + "=" * 56)
    print("总计 %d 个，耗时 %.0f 分钟" % (len(results), el / 60))
    print("  产出: %s / %s / %s" % (
        args.out,
        os.path.splitext(args.out)[0] + ".txt",
        os.path.splitext(args.out)[0] + "-seeds.txt"))


if __name__ == "__main__":
    main()
