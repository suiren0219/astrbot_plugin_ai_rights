# -*- coding: utf-8 -*-
"""打包脚本：生成插件安装包 astrbot_plugin_ai_rights.zip。

- 包内以 astrbot_plugin_ai_rights/ 文件夹为根（AstrBot 面板上传 zip 的要求：
  必须含 README.md，否则报错拒收）。
- 排除 __pycache__ / .git 等不可外发内容；文件按名字排序，保证同内容同包。
- 产物放在本目录，同时打印 sha256 前 16 位，方便核对版本。

运行：python pack.py
"""
import hashlib
import os
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.join(ROOT, "astrbot_plugin_ai_rights")
PLUGIN_ZIP = os.path.join(ROOT, "astrbot_plugin_ai_rights.zip")

EXCLUDE_DIRS = {"__pycache__", ".git", ".venv", "venv"}


def collect():
    """按相对路径收集插件目录里的全部文件，排序保证可复现。"""
    entries = []
    for base, dirs, files in os.walk(PLUGIN_DIR):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for name in files:
            if name.endswith((".pyc", ".pyo")):
                continue
            full = os.path.join(base, name)
            entries.append((os.path.relpath(full, ROOT).replace(os.sep, "/"), full))
    return sorted(entries)


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    entries = collect()
    if not any(n.endswith("README.md") for n, _ in entries):
        raise SystemExit("包内缺少 README.md，AstrBot 面板会拒收，先补上再打包")
    tmp = PLUGIN_ZIP + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for arcname, full in entries:
            z.write(full, arcname)
    os.replace(tmp, PLUGIN_ZIP)
    names = zipfile.ZipFile(PLUGIN_ZIP).namelist()
    print(f"已生成 {PLUGIN_ZIP}")
    for n in names:
        print(f"  - {n}")
    print(f"共 {len(names)} 个文件，{os.path.getsize(PLUGIN_ZIP)} 字节，sha256 {sha256(PLUGIN_ZIP)[:16]}…")


if __name__ == "__main__":
    main()
