# -*- coding: utf-8 -*-
"""审核要求核查工具：把市场审核里点名的每一项都自动验证一遍。

用法：python check_compliance.py
"""
import os
import re
import sys

BASE = os.path.dirname(os.path.abspath(__file__))  # 仓库布局：插件文件在根目录
MAIN = os.path.join(BASE, "main.py")
WEBUI = os.path.join(BASE, "webui.py")
README = os.path.join(BASE, "README.md")
META = os.path.join(BASE, "metadata.yaml")

ok = 0
fail = 0


def check(desc, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [OK] {desc}")
    else:
        fail += 1
        print(f"  [!!] {desc} {detail}")


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


main = read(MAIN)
webui = read(WEBUI)
readme = read(README)
meta = read(META)
all_src = main + webui

print("== 1. 持久化位置（审核唯一硬性违规项）==")
check("STATE_PATH 指向 data/plugin_data/<plugin_name>/",
      'STATE_PATH = os.path.join("data", "plugin_data", "astrbot_plugin_ai_rights", "state.json")' in main)
# 旧路径只允许出现在迁移常量/迁移逻辑里
legacy_lines = [l for l in main.splitlines() if "data\", \"config\", \"ai_rights_state" in l
                or "config/ai_rights_state" in l]
write_sites = [l for l in legacy_lines if "LEGACY_STATE_PATH =" not in l and "旧" not in l and "#" not in l.split("data", 1)[0]]
check("旧路径仅用于一次性迁移，无写入", len(write_sites) == 0, f"→ {write_sites[:2]}")
check("迁移逻辑存在（copy2 + remove）", "shutil.copy2(LEGACY_STATE_PATH, STATE_PATH)" in main
      and "os.remove(LEGACY_STATE_PATH)" in main)
check("README 已更新为新路径", "data/plugin_data/astrbot_plugin_ai_rights/state.json" in readme)
check("README 不含旧路径作为现行说明",
      "状态落盘在 `data/config/ai_rights_state.json`" not in readme)

print("== 2. 恶意行为扫描 ==")
check("无 eval(", not re.search(r"\beval\s*\(", all_src))
check("无 exec(", not re.search(r"\bexec\s*\(", all_src))
check("无 os.system / popen", "os.system(" not in all_src and "os.popen(" not in all_src)
check("无 subprocess 调用", "subprocess" not in all_src.replace("# subprocess", ""))
check("无 socket/反弹 shell", "socket.socket" not in all_src and "/bin/sh" not in all_src)
check("无 base64 解码执行", "b64decode" not in all_src and "base64.b64decode" not in all_src)
check("无 SQL 直连（sqlite3 等）", "sqlite3" not in all_src and "pymysql" not in all_src)

print("== 3. 网络外传面 ==")
# 只允许：GitHub releases 版本检查、aiohttp 由 AstrBot 生态常规使用
urls = set(re.findall(r"https?://[\w./-]+", all_src))
external = {u for u in urls if "github.com/suiren0219/astrbot_plugin_ai_rights" not in u
            and "astrbot" not in u.lower()}
check("无凭据外传（无 requests.post 到第三方）",
      "requests.post" not in all_src and "urllib.request" not in all_src)
check("外部 URL 仅 GitHub Releases API", not external, f"→ {sorted(external)[:3]}")
check("版本检查走 aiohttp + 超时", "aiohttp.ClientTimeout" in main)

print("== 4. 日志与规范 ==")
check("日志统一 from astrbot.api import logger", "from astrbot.api import AstrBotConfig, logger" in main)
check("无 print( 调试输出", not re.search(r"(?<![\w.])print\(", all_src))
check("无阻塞网络 I/O（time.sleep 在异步路径）",
      not re.search(r"(?m)^\s+time\.sleep\(", main))
check("未篡改会话上下文（无直接改 event.message_str 等）",
      "event.message_str =" not in main and "event.unified_msg_origin =" not in main)
check("metadata 有 name/display_name/version/author/repo",
      all(k in meta for k in ("name:", "display_name:", "version:", "author:", "repo:")))
check("pages 声明与目录一致",
      "pages:" in meta and os.path.isfile(os.path.join(BASE, "pages", "rights-panel", "index.html")))

print(f"\n结果：{ok} 项通过，{fail} 项未通过")
sys.exit(1 if fail else 0)
