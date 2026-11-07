# -*- coding: utf-8 -*-
"""PyInstaller 输出过滤包装器。

PyInstaller 日志中路径以 repr 形式打印（如 C:\\Users\\...），
本包装器启动 PyInstaller 并逐行过滤输出，
把路径中的 \\\\ 还原为单个 \\（含盘符后的分隔符一并规范化）。

用法（与 python -m PyInstaller 完全一致）：
    python run_pyi.py --clean installer.spec
"""
import re
import subprocess
import sys

# 匹配两个及以上连续反斜杠：盘符后（如 C:\\Users）与普通分隔（a\\b）统一折叠为单个 \
BS_RUN = re.compile(r'\\\\+')


def _filter(line):
    return BS_RUN.sub(r'\\', line)


def main():
    # 控制台默认 GBK 可能编码不了某些字符（如个别警告中的符号），
    # 改为 utf-8 并对无法表示的字符做替换，避免输出时抛 UnicodeEncodeError
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

    cmd = [sys.executable, '-m', 'PyInstaller'] + sys.argv[1:]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding='utf-8',
        errors='replace',
        bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(_filter(line))
        sys.stdout.flush()
    sys.exit(proc.wait())


if __name__ == '__main__':
    main()
