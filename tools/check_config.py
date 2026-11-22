#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_config.py —— 生成的订阅配置检查
=====================================
订阅文件里含 auth_token（代理凭据）。这个脚本在刷新后运行，
确认没有把含凭据的文件复制到公开目录。

它检查的是**部署事实**：
    · 公开静态目录里是否存在含 auth_token 的文件
    · 凭据文件的权限是否为 600
    · 是否有备份残留

退出码非 0 表示存在暴露风险。

用法
----
    python tools/check_config.py --public-dir /var/www/ur --key-dir /opt/ur-sub
"""
import argparse
import os
import re
import stat
import sys

TOKEN_PATTERN = re.compile(r'06GV[A-Z0-9]{30,}')
CRED_FILES = {"keys.txt", "secrets.json", "emails.json"}


def check_public_dir(path):
    """公开目录不应含 auth_token"""
    problems = []
    if not os.path.isdir(path):
        return problems
    for root, _, files in os.walk(path):
        for fn in files:
            fp = os.path.join(root, fn)
            if not fn.endswith((".yaml", ".yml", ".txt", ".json", ".conf")):
                continue
            try:
                content = open(fp, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            hits = TOKEN_PATTERN.findall(content)
            if hits:
                problems.append(
                    f"{fp} 含 {len(hits)} 个 auth_token（应只经 /s/<token>/ 分发）")
    return problems


def check_permissions(key_dir):
    """凭据文件权限应为 600"""
    problems = []
    for root, _, files in os.walk(key_dir):
        for fn in files:
            if fn not in CRED_FILES:
                continue
            fp = os.path.join(root, fn)
            mode = stat.S_IMODE(os.stat(fp).st_mode)
            if mode & 0o077:
                problems.append(
                    f"{fp} 权限 {oct(mode)}，组/其他用户可读（应为 600）")
    return problems


def check_backup_residue(key_dir):
    """.bak-* 可能含旧凭据"""
    problems = []
    for root, _, files in os.walk(key_dir):
        for fn in files:
            if ".bak-" in fn or fn.endswith(".bak"):
                fp = os.path.join(root, fn)
                try:
                    content = open(fp, encoding="utf-8", errors="replace").read()
                except OSError:
                    continue
                if TOKEN_PATTERN.search(content) or "urn_" in content:
                    problems.append(f"{fp} 是备份文件且含凭据，应删除")
    return problems


def main():
    ap = argparse.ArgumentParser(description="订阅配置暴露面检查")
    ap.add_argument("--public-dir", default="/var/www/ur")
    ap.add_argument("--key-dir", default="/opt/ur-sub")
    ap.add_argument("--strict", action="store_true",
                    help="备份残留也视为失败")
    args = ap.parse_args()

    groups = [
        ("公开目录", check_public_dir(args.public_dir)),
        ("凭据权限", check_permissions(args.key_dir)),
        ("备份残留", check_backup_residue(args.key_dir)),
    ]

    total = 0
    for label, problems in groups:
        if problems:
            print(f"[{label}] {len(problems)} 项")
            for p in problems:
                print(f"  - {p}")
            total += len(problems)
        else:
            print(f"[{label}] 无问题")

    if total:
        print(f"\n共 {total} 项风险")
        return 1
    print("\n检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
