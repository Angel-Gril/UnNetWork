#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ur_reach.py —— 循环注册直到达到账号目标数
========================================
邮件配额按窗口发放：实测每次窗口开启能连续成功约 5 个，然后关闭一段。

本脚本反复轮转邮箱列表，直到账号数达标或轮次用尽。
未成功的邮箱会自动进入下一轮。

用法
----
    python ur_reach.py --target 80 --mail-file mails_all.txt --out emails.json

    # 自定义轮次与间隔
    python ur_reach.py --target 80 --mail-file mails_all.txt --out emails.json \
        --max-rounds 30 --round-wait 120
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ur_register as R
from ur_register import parse_mail_file, reg_one


def load_done(out):
    if not os.path.exists(out):
        return []
    try:
        return [r for r in json.load(open(out, encoding="utf-8")) if r.get("api_key")]
    except Exception:
        return []


def save(results, out):
    base = os.path.splitext(out)[0]
    try:
        json.dump(results, open(out, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        with open(base + ".txt", "w", encoding="utf-8") as f:
            f.write("\n".join(x["api_key"] for x in results) + "\n")
        with open(base + "-seeds.txt", "w", encoding="utf-8") as f:
            for x in results:
                f.write("%s\t%s\t%s\n" % (x.get("email", ""), x["api_key"],
                                          x.get("seedphrase") or ""))
    except Exception as e:
        print("  保存失败:", e, flush=True)


def main():
    ap = argparse.ArgumentParser(description="循环注册直到达标")
    ap.add_argument("--target", type=int, default=80, help="目标账号数")
    ap.add_argument("--mail-file", required=True)
    ap.add_argument("--out", default="emails.json")
    ap.add_argument("--password", default="Qwert123456789")
    ap.add_argument("--wait-code", type=int, default=8)
    ap.add_argument("--code-timeout", type=int, default=15)
    ap.add_argument("--max-rounds", type=int, default=30)
    ap.add_argument("--round-wait", type=int, default=120,
                    help="两轮之间等待秒数（给邮件配额恢复）")
    ap.add_argument("--tag", default="em")
    args = ap.parse_args()

    recs = parse_mail_file(args.mail_file)
    print("邮箱池: %d 个" % len(recs), flush=True)
    print("目标: %d 个账号" % args.target, flush=True)

    results = load_done(args.out)
    print("已有: %d 个" % len(results), flush=True)

    t0 = time.time()
    for rd in range(1, args.max_rounds + 1):
        if len(results) >= args.target:
            print("\n✅ 已达目标 %d 个" % len(results), flush=True)
            break

        done_emails = {r["email"].lower() for r in results}
        queue = [r for r in recs if r["email"].lower() not in done_emails]
        if not queue:
            print("\n邮箱池已耗尽", flush=True)
            break

        print("\n" + "=" * 60, flush=True)
        print("第 %d 轮  %s   待试 %d 个 / 已有 %d 个"
              % (rd, time.strftime("%H:%M:%S"), len(queue), len(results)), flush=True)

        before = len(results)
        for i, rec in enumerate(queue, 1):
            if len(results) >= args.target:
                break
            try:
                r = reg_one(rec, args.tag, args.wait_code, True,
                            code_timeout=args.code_timeout,
                            fixed_password=args.password)
            except Exception as e:
                r = {"email": rec["email"], "error": "%s: %s" % (type(e).__name__, e)}

            if r.get("api_key"):
                results.append(r)
                print("[%d/%d] ✅ %-38s %s"
                      % (i, len(queue), rec["email"], r["api_key"][:22] + "…"), flush=True)
                save(results, args.out)
            else:
                print("[%d/%d] ❌ %-38s %s"
                      % (i, len(queue), rec["email"], str(r.get("error"))[:46]), flush=True)

            if len(results) >= args.target:
                print("\n✅ 达标", flush=True)
                break

        got = len(results) - before
        el = time.time() - t0
        print("\n第 %d 轮: +%d（累计 %d/%d，耗时 %.0f 分钟）"
              % (rd, got, len(results), args.target, el / 60), flush=True)
        save(results, args.out)

        if len(results) >= args.target:
            break

        if rd < args.max_rounds:
            w = args.round_wait if got > 0 else max(args.round_wait, 300)
            print("等 %d 分钟再进下一轮" % (w // 60), flush=True)
            time.sleep(w)

    el = time.time() - t0
    print("\n" + "=" * 60, flush=True)
    print("结束：%d / %d 个账号，总耗时 %.1f 小时"
          % (len(results), args.target, el / 3600), flush=True)
    print("产出: %s / %s / %s"
          % (args.out, os.path.splitext(args.out)[0] + ".txt",
             os.path.splitext(args.out)[0] + "-seeds.txt"), flush=True)


if __name__ == "__main__":
    main()
