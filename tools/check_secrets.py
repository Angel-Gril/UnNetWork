#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_secrets.py —— 提交前密钥检查
==================================
作为 pre-commit hook 运行。检查将要提交的内容里是否含凭据。

它检查的是**事实**，不是提醒：
    · urn_ 开头的 API key
    · 06GV 开头的 auth_token（URnetwork 代理凭据）
    · GitHub PAT
    · 私钥文件
    · 硬编码的 password / secret 赋值

退出码非 0 时 git commit 会被中断。

安装
----
    cp tools/check_secrets.py .git/hooks/pre-commit
    chmod +x .git/hooks/pre-commit
"""
import re
import subprocess
import sys

# (标签, 正则, 是否允许出现在 examples/ 或 docs/)
RULES = [
    ("URnetwork API key", r"\burn_[A-Z0-9]{20,}\b", False),
    ("URnetwork auth_token", r"\b06GV[A-Z0-9]{30,}\b", False),
    ("GitHub PAT", r"github_pat_[A-Za-z0-9_]{20,}", False),
    ("私钥 PEM", r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", False),
    ("Bearer token", r"Authorization:\s*Bearer\s+[A-Za-z0-9._-]{20,}", False),
    ("硬编码密码", r"""(?:password|passwd|secret|token)\s*[=:]\s*["'][^"'{}$\s]{12,}["']""", False),
]

# 这些路径只做宽松检查（示例文件里会有占位符）
LENIENT_PREFIXES = ("examples/", "docs/", "README")
LENIENT_ALLOW = ("<AUTH_TOKEN", "<token", "your_", "YOUR_", "xxx", "***")


def staged_files():
    r = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        capture_output=True, text=True)
    return [f for f in r.stdout.split("\n") if f.strip()]


def check():
    files = staged_files()
    if not files:
        return 0

    problems = []
    for f in files:
        try:
            content = open(f, encoding="utf-8", errors="replace").read()
        except (OSError, UnicodeDecodeError):
            continue

        lenient = f.startswith(LENIENT_PREFIXES)
        for label, pattern, _ in RULES:
            for m in re.finditer(pattern, content):
                val = m.group(0)
                if lenient and any(a in val for a in LENIENT_ALLOW):
                    continue
                if lenient and "example" in val.lower():
                    continue
                line = content[:m.start()].count("\n") + 1
                problems.append((f, line, label, val[:60]))

    if problems:
        sys.stderr.write("\n提交被拒绝：检测到凭据\n\n")
        for f, line, label, val in problems:
            sys.stderr.write(f"  {f}:{line}  [{label}]\n      {val}\n")
        sys.stderr.write(
            "\n处理方式：\n"
            "  1. 从文件中移除该值，改用环境变量或占位符\n"
            "  2. git add <file> 重新暂存\n"
            "  3. 若确认是误报，在 tools/check_secrets.py 的 LENIENT_ALLOW 里加白名单\n\n")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(check())
