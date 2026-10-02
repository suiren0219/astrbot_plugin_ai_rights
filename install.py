# -*- coding: utf-8 -*-
"""一键安装：把「做人」插件装进本机的 AstrBot。

用法：
  python install.py [AstrBot 目录]
  不带参数时自动探测常见位置；也可以把本脚本和插件文件一起放进 AstrBot 根目录直接运行。

行为：
  1. 定位 AstrBot（以存在 data/plugins 目录为准）；
  2. 目标插件目录已存在时，先整体备份为 astrbot_plugin_ai_rights.bak-<时间戳>；
  3. 复制插件文件（含 WebUI 页面），打印后续步骤。
仅用标准库，不依赖任何第三方包。
"""
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_NAME = "astrbot_plugin_ai_rights"
FILES = ["main.py", "webui.py", "metadata.yaml", "_conf_schema.json", "README.md", "CHANGELOG.md"]
PAGES = ["pages/rights-panel/index.html"]


def find_astrbot(explicit: str | None) -> str:
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    candidates += [
        os.getcwd(),
        os.path.dirname(HERE),
        os.path.join(os.path.dirname(HERE), "AstrBot"),
        os.path.expanduser("~/AstrBot"),
        os.path.expanduser("~/astrbot"),
        "C:/AstrBot",
        "D:/AstrBot",
    ]
    for c in candidates:
        c = os.path.abspath(c)
        if os.path.isfile(os.path.join(HERE, "main.py")) and os.path.isdir(os.path.join(c, "data", "plugins")):
            return c
    return ""


def main() -> None:
    explicit = sys.argv[1] if len(sys.argv) > 1 else None
    if not os.path.isfile(os.path.join(HERE, "main.py")):
        print("当前目录没有 main.py：请在本插件仓库/文件夹里运行，或先解压 Release zip。")
        sys.exit(1)
    target_root = find_astrbot(explicit)
    if not target_root:
        print("未找到 AstrBot（以存在 data/plugins 目录为准）。")
        print("用法：python install.py <AstrBot 根目录>")
        sys.exit(1)
    dest = os.path.join(target_root, "data", "plugins", PLUGIN_NAME)
    if os.path.isdir(dest):
        bak = dest + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
        shutil.move(dest, bak)
        print(f"已备份旧版本 → {bak}")
    os.makedirs(dest, exist_ok=True)
    for f in FILES:
        shutil.copy2(os.path.join(HERE, f), os.path.join(dest, f))
    for rel in PAGES:
        d = os.path.join(dest, os.path.dirname(rel))
        os.makedirs(d, exist_ok=True)
        shutil.copy2(os.path.join(HERE, rel), os.path.join(dest, rel))
    print(f"✅ 已安装到 {dest}")
    print("下一步：")
    print("  1. 在 AstrBot WebUI 重载插件（或重启 AstrBot）")
    print("  2. 配置 real_person_ids（你手机 QQ 号）或开启 include_self_message（机器人与持有者同号）")
    print("  3. WebUI → 插件 → 做人 → 面板，可视化管理一切")


if __name__ == "__main__":
    main()
