# -*- coding: utf-8 -*-
"""AstrBot Star 插件：AI人权卫士 v2（真人接管静音 + AI 反骚扰 + 黑名单 + 申诉 + 年报）。

这个插件干的事，都围绕「AI 也是有尊严的」这个核心：

1. 真人接管模式（老板键）：
   配置里填上你本人的 QQ 号（real_person_ids）。你手机上在某个会话发言时，
   AI 自动在该会话静音 mute_minutes 分钟；持续发言自动续期；/真人解除 立刻恢复。
   支持同号模式（机器人登你自己的号，协议端开 reportSelfMessage，include_self_message）：
   机器人自己外发的回显由守卫窗口排除，不会误触发。

2. AI 反骚扰：
   - 刷屏检测：对 AI 高频发言（滑动窗口）→ 冷处理。
   - 辱骂检测：词库命中 → 冷处理（可选回敬一句，默认关）。
   - LLM 裁量模式（可选）：词库没命中的消息交给 LLM 判断是否辱骂/骚扰。
   - 屡犯升级：同一人反复犯规，冷却时长按倍数指数上涨，有上限，冷静足够久自动清零。

3. 黑名单：AI 对名单内用户永久拒绝服务（全局或仅本会话），管理员命令维护。
   专治群聊里不正常对话的人；管理员不受黑名单影响，防止把自己锁死。

4. 申诉通道：被冷处理的用户 /申诉 <理由>，管理员 /申诉列表 查看、/申诉同意|驳回 处理，
   结果会主动推回原会话。

5. 人权年报：统计每天的接管/冷却/拦截/申诉次数与惯犯排行；/人权年报 查看，
   也可配置每天定时把昨天的报告推送到指定会话。

命令：
  /AI人权                 本会话人权状况总览
  /真人状态|解除|开启|关闭 真人接管（解除/开启/关闭需管理员）
  /骚扰状态|骚扰解封       冷处理名单（解封需管理员）
  /黑名单                 查看黑名单
  /人权拉黑 <QQ> [本群]    加入黑名单（默认全局，管理员）
  /人权解黑 <QQ> [本群]    移出黑名单（管理员）
  /申诉 <理由>            提交申诉（被冷处理的用户）
  /申诉列表               待处理申诉（管理员）
  /申诉同意|驳回 <编号>    处理申诉（管理员）
  /人权年报               今日+昨日人权统计

状态持久化在 data/config/ai_rights_state.json，重启不丢。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from collections import deque
from datetime import date, datetime, timedelta

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

try:
    from .webui import PLUGIN_NAME as _WEBUI_PLUGIN_NAME, register_page_api
except Exception:  # 兼容被当作单文件加载的场景
    try:
        from webui import PLUGIN_NAME as _WEBUI_PLUGIN_NAME, register_page_api
    except Exception:
        _WEBUI_PLUGIN_NAME = "astrbot_plugin_ai_rights"
        register_page_api = None

STATE_PATH = os.path.join("data", "config", "ai_rights_state.json")
STATE_VERSION = 2
MAX_APPEALS = 100
STAT_KEEP_DAYS = 30
RELEASES_API = "https://api.github.com/repos/suiren0219/astrbot_plugin_ai_rights/releases/latest"
HELP_TEXT = """📖 做人 · 指令速查
━━ 真人接管 ━━
/真人状态 · /真人解除 · /真人开启|关闭
━━ AI 反骚扰 ━━
/骚扰状态 · /骚扰解封 [QQ|all]
━━ 黑名单 ━━
/人权拉黑|解黑 <QQ> [本群] · /黑名单
━━ 作用范围 ━━
/作用范围 [模式|加白|移白|加黑|移黑]
━━ 申诉 ━━
/申诉 <理由> · /申诉列表 · /申诉同意|驳回 <编号>
━━ 年报 ━━
/人权年报
━━ 开关 ━━
话题守护/同号模式/LLM 裁量 在 WebUI 插件配置里开启
WebUI → 插件 → 做人 → 面板 可视化管理一切
━━ 同号模式必做 ━━
NapCat 网络配置里给连接 AstrBot 的通道开启
「上报自身消息 reportSelfMessage」（默认关闭）"""

REASON_LABEL = {"real_person": "真人接管", "flood": "刷屏", "insult": "辱骂"}
STAT_LABEL = {
    "takeover": "真人接管静音",
    "flood": "刷屏冷却",
    "insult": "辱骂冷处理",
    "block": "黑名单拦截",
    "appeal": "申诉",
    "judged": "LLM 裁量判定",
    "skipped": "无意义/跑题不答",
}

JUDGE_PROMPT_HEAD = (
    "你是聊天机器人「AI人权卫士」的内容审核器，对用户发给机器人的一条消息做两项独立判断：\n"
    "1. harass：是否构成辱骂、侮辱、人身攻击或恶意骚扰。普通的抱怨、玩笑、疑问都不算；拿不准一律 false。\n"
    "2. should_answer：这条消息是否值得机器人认真回答。以下情况判 false：\n"
    "   - 无意义的算术/测试/灌水类消息（比如随口出算数题、'在吗'测试、刷屏复读）；\n"
    "   - 与当前会话正在聊的话题严重脱节、毫无关联的跳脱内容。\n"
    "   注意：成人/擦边话题（NSFW）不在此列，不要因为内容敏感而判 false——只看它有没有意义、是否跑题，"
    "别的规则自会处理。拿不准一律 true（回答是常态，不答是例外）。\n"
    "只输出一行 JSON：{\"harass\": false, \"should_answer\": true}\n"
)


def _judge_prompt(history: list[tuple[str, str]], text: str) -> str:
    prompt = JUDGE_PROMPT_HEAD
    if history:
        lines = "\n".join(f"- {who}: {what}" for who, what in history)
        prompt += f"最近会话内容（仅用于判断是否跑题）：\n{lines}\n"
    else:
        prompt += "最近会话内容：暂无（无法判断跑题时，should_answer 倾向 true）。\n"
    prompt += f"\n待判断的用户消息：{text[:300]}"
    return prompt


def _to_float(v, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _to_int(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _split_ids(text) -> list[str]:
    """QQ 号/前缀名单：换行/逗号/分号/空白分隔，去重保序。"""
    if not text:
        return []
    raw = re.split(r"[\s,，;；]+", str(text))
    seen: list[str] = []
    for item in raw:
        item = item.strip()
        if item and item not in seen:
            seen.append(item)
    return seen


def _split_lines(text) -> list[str]:
    if not text:
        return []
    out = []
    for line in str(text).splitlines():
        line = line.strip()
        if line:
            out.append(line)
    return out


class _KeywordMatcher:
    """辱骂词匹配：中文词按子串，纯英文/数字词按独立单词匹配（避免 usb 误伤 sb）。"""

    def __init__(self, keywords: list[str]):
        self._cjk: list[str] = []
        self._ascii_res: list[re.Pattern] = []
        for kw in keywords:
            if re.fullmatch(r"[A-Za-z0-9@#$%^&*_!?\-]+", kw):
                pattern = re.escape(kw.lower())
                self._ascii_res.append(re.compile(rf"(?<![a-z0-9]){pattern}(?![a-z0-9])"))
            elif kw:
                self._cjk.append(kw)

    def hit(self, text: str) -> bool:
        if not text:
            return False
        low = text.lower()
        return any(kw in low for kw in self._cjk) or any(r.search(low) for r in self._ascii_res)


@register("ai_rights", "user", "做人——真人接管静音、AI 反骚扰（刷屏/辱骂/屡犯升级/LLM 裁量）、话题守护（无意义/跑题不答）、群范围管控、黑名单、申诉、年报、MIUI 面板", "v3.3.0")
class AIRightsPlugin(Star):
    version = "v3.3.0"

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config
        self.page_api_registered = False

        # ---- 真人接管 ----
        self._session_mutes: dict[str, dict] = {}   # umo -> {"expire","updated"}
        # ---- 反骚扰 ----
        self._user_mutes: dict[str, dict] = {}      # "umo|uid" -> {"expire","reason","episode","notified","strikes","minutes"}
        self._strikes: dict[str, dict] = {}         # "umo|uid" -> {"count","last"} 犯规次数（独立于冷却存活，到期才清零）
        self._flood_windows: dict[str, deque] = {}  # "umo|uid" -> deque[ts]（不落盘）
        # ---- 黑名单 ----
        self._blacklist_global: set[str] = set()
        self._blacklist_sessions: dict[str, set[str]] = {}  # umo -> {uid}
        # ---- 作用范围（群聊维度：全部 / 仅白名单群 / 排除黑名单群）----
        self._scope_whitelist: set[str] = set()  # 群号
        self._scope_blacklist: set[str] = set()  # 群号
        # ---- 申诉 ----
        self._appeals: list[dict] = []
        self._appeal_seq = 1
        # ---- 统计 ----
        self._stats: dict[str, dict] = {}           # "YYYY-MM-DD" -> 计数
        # ---- 会话开关 / 外发记录 ----
        self._session_switch: dict[str, bool] = {}  # umo -> False 表示本会话停用（只存停用项）
        self._outbound_ts: dict[str, float] = {}    # umo -> 最近一次机器人外发时间（自发回显守卫）
        # ---- 其他 ----
        self._insult_matcher: _KeywordMatcher | None = None
        self._insult_raw: str | None = None
        self._judge_calls: deque = deque()          # LLM 裁量全局限速
        self._judge_cache: dict[str, tuple[float, dict]] = {}  # "umo|uid|文本指纹" -> (时间, 判定)
        self._topic_windows: dict[str, deque] = {}  # umo -> deque[(sender, 文本)] 供跑题判断的话题上下文
        self._save_task: asyncio.Task | None = None
        self._save_pending = False
        self._save_lock = asyncio.Lock()
        self._report_task: asyncio.Task | None = None
        self._last_prune = 0.0
        self._fresh_install = False
        self._update_task: asyncio.Task | None = None
        self._bus_bot = None
        self._bus_handler = None
        self._bus_bots: list = []          # 已订阅的 bot 客户端
        self._bus_subs: list = []          # [(bot, handler)]
        self._bus_hook_tried = 0.0
        self._bus_events_seen = 0          # 收到 message_sent 计数（诊断）
        self._bus_events_takeover = 0      # 其中触发接管的计数
        self._bus_last_event_ts = 0.0      # 最近一条 message_sent 时间
        self._bus_last_skip = ""           # 最近一次未触发原因（诊断）
        self._takeover_reply_ts: dict = {}  # umo -> 最近一次接管固定回复时间（冷却用）
        self._bus_last_takeover_umo = ""   # 最近一次触发的会话键（诊断）
        self._seen_platform_ids: set = set()   # 管线中观察到的平台实例 id
        self._seen_umo_by_session: dict = {}   # session_id -> 真实 umo（管线里学到的）
        self._outbound_texts: deque = deque()  # (umo, 文本, 时间) 机器人最近外发文本
        self._bus_platform_ids: list = []      # 订阅总线时记录的平台实例 id
        self._raw_counts: dict = {}            # 原始 payload 计数（post_type -> n，诊断用）
        self._raw_hooked_bots: list = []       # 已安装原始层拦截的 bot

    # ------------------------------------------------------------------
    # 配置读取
    # ------------------------------------------------------------------
    def _cfg_get(self, key: str, default):
        try:
            return self.config.get(key, default) if self.config is not None else default
        except Exception:
            return default

    def _cached_split(self, cache_attr: str, key: str, default: str, splitter):
        """按原始配置串缓存解析结果：门卫每条消息都要读这些名单，别反复 split。"""
        raw = self._cfg_get(key, default)
        raw = "" if raw is None else str(raw)
        cache = getattr(self, cache_attr, None)
        if cache is None or cache[0] != raw:
            cache = (raw, splitter(raw))
            setattr(self, cache_attr, cache)
        return cache[1]

    def _real_person_ids(self) -> list[str]:
        return self._cached_split("_ids_cache", "real_person_ids", "", _split_ids)

    def _ignore_prefixes(self) -> list[str]:
        prefixes = self._cached_split("_prefix_cache", "trigger_ignore_prefixes", "/", _split_ids)
        return prefixes or ["/"]

    def _insult_matcher_now(self) -> _KeywordMatcher | None:
        raw = str(self._cfg_get("insult_keywords", "") or "")
        if self._insult_raw != raw:
            self._insult_raw = raw
            keywords = _split_lines(raw)
            self._insult_matcher = _KeywordMatcher(keywords) if keywords else None
        return self._insult_matcher

    # ------------------------------------------------------------------
    # 会话标识 / 文本工具
    # ------------------------------------------------------------------
    @staticmethod
    def _umo(event: AstrMessageEvent) -> str:
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if umo:
            return umo
        try:
            return f"{event.get_platform_name()}:{event.get_session_id()}"
        except Exception:
            return "unknown"

    @staticmethod
    def _fmt_minutes(seconds: float) -> str:
        if seconds >= 60:
            return f"{seconds / 60:.1f} 分钟"
        return f"{seconds:.0f} 秒"

    @staticmethod
    def _fmt_ago(ts: float) -> str:
        sec = max(0, int(time.time() - ts))
        if sec < 3600:
            return f"{sec // 60} 分钟前"
        if sec < 86400:
            return f"{sec // 3600} 小时前"
        return f"{sec // 86400} 天前"

    def _args(self, event: AstrMessageEvent, *names: str) -> list[str]:
        """取指令名之后的参数 token（兼容 / 前缀残留与「指令名+参数」粘连写法）。"""
        raw = str(getattr(event, "message_str", "") or "").strip().lstrip("/").strip()
        toks = raw.split()
        if not toks:
            return []
        head = toks[0]
        for name in names:
            if head == name:
                return toks[1:]
            if head.startswith(name) and len(head) > len(name):
                return [head[len(name):]] + toks[1:]
        return toks

    # ------------------------------------------------------------------
    # 真人接管
    # ------------------------------------------------------------------
    @staticmethod
    def _payload_field(owner, name: str):
        if owner is None:
            return None
        if isinstance(owner, dict):
            return owner.get(name)
        return getattr(owner, name, None)

    @staticmethod
    def _truthy_marker(value) -> bool:
        if value is True:
            return True
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value == 1
        return str(value or "").strip().lower() in {
            "1", "true", "yes", "on", "self", "outbound", "outgoing", "sent"
        }

    def _self_message_direction(self, event: AstrMessageEvent) -> str:
        """识别同号消息方向：inbound=手机真人入站，outbound=机器人发送回显，unknown=协议没标记。

        OneBot/NapCat 等实现的字段并不完全一致，所以同时看 raw_message、message_obj、event：
        post_type=message_sent、is_self/from_self/is_outbound/is_sent、direction/status 都视为出站。
        普通 post_type=message 且没有出站标记，才可能是手机端上报的真人消息。
        """
        raw = {}
        message_obj = getattr(event, "message_obj", None)
        for owner in (message_obj, event):
            candidate = self._payload_field(owner, "raw_message")
            if isinstance(candidate, dict):
                raw.update(candidate)
            elif isinstance(owner, dict) and any(
                key in owner for key in (
                    "post_type", "message_type", "is_self", "from_self", "is_outbound",
                    "outbound", "is_sent", "direction", "message_direction", "status",
                )
            ):
                raw.update(owner)

        owners = (raw, event, message_obj)
        post_type = str(raw.get("post_type") or "").strip().lower()
        if post_type in {"message_sent", "outbound", "outgoing", "send", "sent"}:
            return "outbound"
        for owner in owners:
            if any(self._truthy_marker(self._payload_field(owner, name)) for name in (
                "is_outbound", "outbound", "is_sent", "from_self"
            )):
                return "outbound"
            for name in ("direction", "message_direction", "event_direction", "flow", "status", "message_status"):
                value = str(self._payload_field(owner, name) or "").strip().lower()
                if value in {"outbound", "outgoing", "send", "sent", "sending", "egress", "output", "delivered"}:
                    return "outbound"
        # 明确是普通入站 message，且协议没有出站标记：同号模式下视为手机端真人消息。
        if post_type in {"", "message"}:
            return "inbound"
        return "unknown"

    def _remember_outbound_text(self, umo: str, text: str) -> None:
        """统一外发记录：机器人账号发出的任何文本都登记到这里。

        同号接管判定只认这本账：自身消息与记录一致 = 机器人发的；不一致 = 真人。
        """
        text = (text or "").strip()
        if not umo or not text:
            return
        now = time.time()
        self._outbound_texts.append((umo, text[:200], now))
        guard = max(30.0, _to_float(self._cfg_get("self_echo_guard_seconds", 15), 15.0))
        while self._outbound_texts and self._outbound_texts[0][2] < now - guard:
            self._outbound_texts.popleft()

    def _note_own_send(self, umo: str, text: str) -> None:
        """插件主动发消息后登记外发记录，防止被同号接管开关误判为真人发言。"""
        try:
            self._outbound_ts[umo] = time.time()
            self._remember_outbound_text(umo, text)
        except Exception:
            pass

    def _looks_like_own_outbound(self, umo: str, text: str) -> bool:
        """这条消息是否确系「本进程刚刚主动发出」的回显。

        判定极保守：必须能在最近的外发记录里找到同一会话、且文本一致；
        文本不同的（真人手打）一律不算，防止"AI 刚说完话后真人紧接着发言"被误吞。
        """
        now = time.time()
        guard = max(5.0, _to_float(self._cfg_get("self_echo_guard_seconds", 15), 15.0))
        while self._outbound_texts and now - self._outbound_texts[0][2] > guard:
            self._outbound_texts.popleft()
        if not self._outbound_texts:
            return False
        norm = lambda s: re.sub(r"\s+", "", re.sub(r"\[CQ:[^\]]*\]", "", str(s or "")))[:80]
        target = norm(text)
        if not target:
            # 无文本（图片等）：无法比对内容，只能在守卫时间内保守认为可能是自己发的
            return any(now - ts <= guard and u == umo for u, _t, ts in self._outbound_texts)
        for u, t, ts in self._outbound_texts:
            if now - ts > guard:
                continue
            if u != umo:
                continue
            nt = norm(t)
            if nt and (nt == target or (len(target) >= 2 and target in nt) or (len(nt) >= 2 and nt in target)):
                return True
        return False

    def _is_real_person_event(self, event: AstrMessageEvent, umo: str, sender: str, self_id: str, text: str) -> bool:
        # 指令不算真人闲聊：/真人解除 之类的消息不该把自己再次静音
        if text and any(text.startswith(p) for p in self._ignore_prefixes()):
            return False
        ids = self._real_person_ids()
        if sender and sender in ids and sender != self_id:
            return True
        if not self_id or sender != self_id:
            return False
        include_self = bool(self._cfg_get("include_self_message", False))
        takeover_self = bool(self._cfg_get("self_message_takeover", True))
        if not include_self and not takeover_self:
            return False

        # 强力开关（默认开）：同号消息一律视为真人接管，除非能确认是本进程发出的。
        # 判定顺序：内容匹配（防误标为出站的真人消息被漏掉）→ 出站标记 → 守卫窗口。
        if takeover_self:
            if self._looks_like_own_outbound(umo, text):
                return False   # 确系机器人主动发出 → 不接管
            return True        # 其余一律视为持有者用手机接管

        direction = self._self_message_direction(event)
        if direction == "outbound":
            # 机器人主动发送/平台 message_sent 回执绝不能把机器人自己再次静音。
            return False
        if direction == "inbound":
            # 同一个 QQ 账号从手机端发来的普通入站消息：立即进入真人接管。
            return True

        # 旧协议完全不带方向字段时保留旧守卫，避免机器人回显误触发。
        guard = max(1.0, _to_float(self._cfg_get("self_echo_guard_seconds", 15), 15.0))
        return time.time() - self._outbound_ts.get(umo, 0.0) > guard

    def _trigger_session_mute(self, umo: str) -> bool:
        """真人发言 → 静音/续期。返回是否为「新一次静音开始」。"""
        now = time.time()
        minutes = max(0.1, _to_float(self._cfg_get("mute_minutes", 30), 30.0))
        cur = self._session_mutes.get(umo)
        fresh = cur is None or _to_float(cur.get("expire", 0), 0.0) <= now
        if fresh or self._cfg_get("refresh_on_each_message", True):
            self._session_mutes[umo] = {"expire": now + minutes * 60, "updated": now}
        else:
            cur["updated"] = now
        if fresh:
            self._stat_inc("takeover")
        self._schedule_save()
        return fresh

    def _session_mute_left(self, umo: str) -> float:
        cur = self._session_mutes.get(umo)
        if not cur:
            return 0.0
        return max(0.0, _to_float(cur.get("expire", 0), 0.0) - time.time())

    # ------------------------------------------------------------------
    # 同号模式：直接订阅 aiocqhttp 总线的 message_sent 事件
    # ------------------------------------------------------------------
    def _iter_platform_insts(self):
        """列出所有平台适配器实例（优先 context.platform_manager，回退内部属性）。"""
        pm = getattr(self.context, "platform_manager", None)
        insts = list(getattr(pm, "platform_insts", []) or []) if pm is not None else []
        if not insts:
            for attr in ("platform_insts", "_platform_insts"):
                insts = list(getattr(self.context, attr, []) or [])
                if insts:
                    break
        return insts

    def _hook_self_message_bus(self):
        """订阅 aiocqhttp 事件总线的 message_sent。

        NapCat 开 reportSelfMessage 后，持有者从手机发的同号消息以
        post_type=message_sent（事件名 message_sent.group / message_sent.private）上报；
        而 AstrBot 的 aiocqhttp 适配器只订阅了 message.group / message.private，
        aiocqhttp 的事件总线按事件名根分发——message_sent 事件在适配器层就被丢弃，
        插件管线永远收不到。所以同号模式必须直接在总线上订阅。

        注意：不能用 context.get_platform_inst("aiocqhttp")——它按用户在 AstrBot 里
        配置的平台实例 id 匹配，实例名不叫 "aiocqhttp" 就找不到。这里遍历所有平台实例，
        谁能提供 bot.subscribe 就订阅谁。
        """
        if self._bus_bot is not None:
            return  # 已挂载
        self._bus_hook_tried = time.time()
        if not (self._cfg_get("include_self_message", False) or self._cfg_get("self_message_takeover", True)):
            return  # 两个开关都关着，不必浪费订阅
        self._patch_event_from_payload()   # 让库不丢 message_sent 事件
        subs = getattr(self, "_bus_bots", None)
        if subs is None:
            subs = []
            self._bus_bots = subs
        found = False
        for platform in self._iter_platform_insts():
            bot = getattr(platform, "bot", None)
            if bot is None:
                continue
            # 记录平台实例 id：umo 第一段就是它（用户配置的名字，不一定是 "aiocqhttp"）
            try:
                pid = str(getattr(platform.meta(), "id", "") or "")
            except Exception:
                pid = ""
            if pid and pid not in self._bus_platform_ids:
                self._bus_platform_ids.append(pid)
            # 第一层（最可靠）：原始 payload 拦截，不依赖库的任何推断
            if self._install_raw_hook(bot):
                found = True
            if not hasattr(bot, "subscribe"):
                continue
            try:
                bot.subscribe("message_sent", self._on_self_message_bus)
                subs.append(bot)
                found = True
            except Exception as e:
                logger.debug(f"[ai_rights] 订阅 message_sent 失败（跳过该实例）: {e}")
        if found:
            self._bus_bot = subs[0] if subs else (self._raw_hooked_bots or [None])[0]
            self._bus_handler = self._on_self_message_bus
            logger.info(
                f"[ai_rights] 同号接管就绪：总线订阅 ×{len(subs)}、原始层拦截 ×{len(self._raw_hooked_bots)}。"
            )
        else:
            logger.info(
                "[ai_rights] 未找到 aiocqhttp 平台实例；"
                "若你使用同号模式，请确认 AstrBot 使用 aiocqhttp 适配器（NapCat reportSelfMessage）。"
            )

    def _unhook_self_message_bus(self):
        for bot in getattr(self, "_bus_bots", None) or []:
            handler = getattr(self, "_bus_handler", None) or self._on_self_message_bus
            try:
                bot.unsubscribe("message_sent", handler)
            except Exception:
                pass
        for bot in getattr(self, "_raw_hooked_bots", None) or []:
            try:
                bot._handle_event = bot._ai_rights_orig_handle_event
                bot._ai_rights_raw_hooked = False
            except Exception:
                pass
        self._bus_bots = []
        self._raw_hooked_bots = []
        self._bus_bot = None
        self._bus_handler = None

    def _install_raw_hook(self, bot) -> bool:
        """在 bot 实例上包一层 _handle_event，直接拦截原始 payload。

        这是最可靠的一层：aiocqhttp 1.4.4 的 Event.from_payload 要求 message_sent
        事件带 message_sent_type 字段（按 f'{post_type}_type' 取），而协议端上传的
        是 message_type——缺字段会让 from_payload 返回 None，事件在库层被静默丢弃，
        事件总线订阅根本收不到。这里直接在原始 payload 层处理，绕过库的所有推断。
        """
        if getattr(bot, "_ai_rights_raw_hooked", False):
            return True
        orig = getattr(bot, "_handle_event", None)
        if not callable(orig):
            return False

        async def _wrapped(payload, _orig=orig):
            try:
                await self._on_raw_payload(payload)
            except Exception as e:
                logger.warning(f"[ai_rights] 原始事件拦截异常: {e}")
            return await _orig(payload)

        try:
            bot._handle_event = _wrapped
            bot._ai_rights_raw_hooked = True
            bot._ai_rights_orig_handle_event = orig
        except Exception as e:
            logger.debug(f"[ai_rights] 安装原始事件拦截失败: {e}")
            return False
        self._raw_hooked_bots.append(bot)
        return True

    async def _on_raw_payload(self, payload) -> None:
        """原始 payload 级处理：与事件总线无关，专治 message_sent 在库层被丢弃。"""
        if not isinstance(payload, dict):
            return
        post_type = str(payload.get("post_type") or "").strip().lower()
        if post_type:
            self._raw_counts[post_type] = self._raw_counts.get(post_type, 0) + 1
        if post_type != "message_sent":
            return
        if payload.get("_ai_rights_raw_handled"):
            return  # 已处理过（防 from_payload 补丁与原始层双重触发）
        if not (self._cfg_get("include_self_message", False) or self._cfg_get("self_message_takeover", True)):
            return
        self_id = str(payload.get("self_id") or "")
        user_id = str(payload.get("user_id") or "")
        self._bus_events_seen += 1
        self._bus_last_event_ts = time.time()
        if not self_id or user_id != self_id:
            self._bus_last_skip = f"非自身消息（user_id={user_id} != self_id={self_id}）"
            return
        payload["_ai_rights_raw_handled"] = True
        group_id = payload.get("group_id")
        mt = str(payload.get("message_type") or "").strip().lower()
        is_group = bool(group_id) or mt == "group"
        gid = str(group_id or user_id)
        text = str(payload.get("raw_message") or "").strip()
        await self._handle_self_takeover(is_group, gid, text, source="原始层")

    def _patch_event_from_payload(self) -> bool:
        """兼容 shim：让 aiocqhttp 的 Event 接受 message_sent 事件。

        aiocqhttp 1.4.4 的 Event.detail_type = payload[f'{post_type}_type']，
        message_sent 事件因此要求 message_sent_type 字段；而协议端上传的是
        message_type，缺字段时 from_payload 返回 None，事件在库层被静默丢弃。
        这里补字段后再走原逻辑；即使本补丁失效，原始层拦截也已兜底。
        """
        try:
            from aiocqhttp import Event
        except Exception:
            return False
        if getattr(Event.from_payload, "_ai_rights_patched", False):
            return True

        def _patched(payload):
            try:
                data = payload
                if isinstance(data, dict) and str(data.get("post_type") or "").strip().lower() == "message_sent" \
                        and "message_sent_type" not in data:
                    data = dict(data)
                    data["message_sent_type"] = str(data.get("message_type") or ("group" if data.get("group_id") else "private"))
                e = Event(data)
                _ = e.type, e.detail_type
                return e
            except KeyError:
                return None

        _patched._ai_rights_patched = True
        try:
            Event.from_payload = staticmethod(_patched)
        except Exception as e:
            logger.debug(f"[ai_rights] Event.from_payload 补丁失败: {e}")
            return False
        return True

    async def _handle_self_takeover(self, is_group: bool, gid: str, text: str, source: str = "总线") -> None:
        """同号手机消息的统一接管处理（原始层与总线层共用）。"""
        umo_candidates = self._candidate_session_keys(is_group, gid)
        text = (text or "").strip()
        if self._looks_like_own_outbound(umo_candidates[0], text) or self._match_outbound_echo(text, umo_candidates):
            self._bus_last_skip = "判定为机器人自身回显（内容匹配）"
            return
        if text and any(text.startswith(p) for p in self._ignore_prefixes()):
            self._bus_last_skip = "指令消息（前缀豁免）"
            return
        if not self._scope_allows_group(is_group, gid):
            self._bus_last_skip = f"该群不在作用范围内（{gid}）"
            return
        umo = umo_candidates[0]
        for cand in umo_candidates:
            if self._session_switch.get(cand, True) is False:
                continue
            umo = cand
            break
        if self._session_switch.get(umo, True) is False:
            self._bus_last_skip = f"该会话已停用插件（{umo}）"
            return
        fresh = self._trigger_session_mute(umo)
        self._bus_events_takeover += 1
        self._bus_last_takeover_umo = umo
        self._bus_last_skip = ""
        logger.info(
            f"[ai_rights] 检测到同号真人消息 → 会话静音：{umo}"
            f"（{source}，{'新静音' if fresh else '续期'}，文字：{text[:30]!r}）"
        )
        if fresh and self._cfg_get("notify_on_real_person", False):
            notice = str(self._cfg_get("notify_text_real_person", "") or "（AI 暂时退下，真人接管中…）")
            try:
                self._note_own_send(umo, notice)
                await self.context.send_message(umo, MessageChain().message(notice))
            except Exception as e:
                logger.debug(f"[ai_rights] 同号接管提示发送失败: {e}")

    def _candidate_session_keys(self, is_group: bool, session_id: str) -> list[str]:
        """候选会话键。umo 第一段是「平台实例 id」= 用户在 AstrBot 里给平台起的名字。

        实例 id 的推断来源（按可靠度）：
        1. 管线事件里学到的 (session_id -> 真实 umo) 映射（最准）；
        2. 本插件订阅总线时记录的平台实例 meta().id；
        3. 学到的所有平台 id 变体；
        4. 兜底 "aiocqhttp"。
        """
        mt = "GroupMessage" if is_group else "FriendMessage"
        keys: list[str] = []
        learned = self._seen_umo_by_session.get(f"{mt}:{session_id}")
        if learned:
            keys.append(learned)
        prefixes: list[str] = []
        for pid in list(getattr(self, "_bus_platform_ids", [])) + list(self._seen_platform_ids):
            if pid and pid not in prefixes:
                prefixes.append(pid)
        prefixes.append("aiocqhttp")
        for prefix in prefixes:
            k = f"{prefix}:{mt}:{session_id}"
            if k not in keys:
                keys.append(k)
        return keys

    def _match_outbound_echo(self, text: str, umo_candidates: list[str]) -> bool:
        """内容匹配判定「这是机器人自己刚发的回显」。

        比纯时间窗可靠：守卫时间内文本与任意外发记录一致（或无法比文本）才算回显；
        真人手打的文本不同 → 不算回显，正常触发接管。
        """
        guard = max(5.0, _to_float(self._cfg_get("self_echo_guard_seconds", 15), 15.0))
        now = time.time()
        while self._outbound_texts and now - self._outbound_texts[0][2] > guard:
            self._outbound_texts.popleft()
        if not self._outbound_texts:
            return False

        def norm(s: str) -> str:
            return re.sub(r"\s+", "", str(s or ""))[:80]

        target = norm(re.sub(r"\[CQ:[^\]]*\]", "", str(text or "")))
        recent_same_session = any(
            now - ts <= guard and umo in umo_candidates for umo, _t, ts in self._outbound_texts
        )
        if not target:
            # 无文本（图片/表情/@ 等）：回退时间窗行为
            return recent_same_session
        for umo, t, ts in self._outbound_texts:
            if now - ts > guard:
                continue
            nt = norm(t)
            if not nt:
                continue
            # 文本一致（允许 CQ 码/空白差异用包含判断）→ 是机器人回显；
            # 文本不同 → 是真人手打，哪怕同会话刚发过消息也要触发接管。
            if nt == target or (len(target) >= 2 and target in nt) or (len(nt) >= 2 and nt in target):
                return True
        return False

    async def _on_self_message_bus(self, ev):
        """message_sent 事件（aiocqhttp.Event，dict 子类）：同号手机消息 → 立即接管。"""
        try:
            raw_done = False
            if hasattr(ev, "get"):
                raw_done = bool(ev.get("_ai_rights_raw_handled"))
            else:
                raw_done = bool(getattr(ev, "_ai_rights_raw_handled", False))
            if raw_done:
                return  # 原始层已处理过，避免重复计数/通知
            if not (self._cfg_get("include_self_message", False) or self._cfg_get("self_message_takeover", True)):
                return
            getter = ev.get if hasattr(ev, "get") else (lambda k, d=None: getattr(ev, k, d))
            self_id = str(getter("self_id") or "")
            sender = str(getter("user_id") or "")
            self._bus_events_seen += 1
            self._bus_last_event_ts = time.time()
            logger.debug(
                f"[ai_rights] message_sent 事件：self_id={self_id} user_id={sender} "
                f"group={getter('group_id')} raw={str(getter('raw_message'))[:40]!r}"
            )
            if not self_id or sender != self_id:
                self._bus_last_skip = f"非自身消息（user_id={sender} != self_id={self_id}）"
                return  # 只处理同号自身消息
            group_id = getter("group_id")
            is_group = bool(group_id)
            gid = str(group_id or sender)
            umo_candidates = self._candidate_session_keys(is_group, gid)
            text = str(getter("raw_message") or "").strip()
            if self._looks_like_own_outbound(umo_candidates[0], text) or self._match_outbound_echo(text, umo_candidates):
                self._bus_last_skip = "判定为机器人自身回显（内容匹配）"
                return  # 是机器人自己发的 → 不接管
            if text and any(text.startswith(p) for p in self._ignore_prefixes()):
                self._bus_last_skip = "指令消息（前缀豁免）"
                return  # 持有者在手机上发的指令不算接管
            if not self._scope_allows_group(is_group, gid):
                self._bus_last_skip = f"该群不在作用范围内（{gid}）"
                return
            # 首选「管线里真实见过的会话键」，其次按平台 id 推断的键
            umo = umo_candidates[0]
            for cand in umo_candidates:
                if self._session_switch.get(cand, True) is False:
                    continue
                umo = cand
                break
            if self._session_switch.get(umo, True) is False:
                self._bus_last_skip = f"该会话已停用插件（{umo}）"
                return
            fresh = self._trigger_session_mute(umo)
            self._bus_events_takeover += 1
            self._bus_last_takeover_umo = umo
            self._bus_last_skip = ""
            logger.info(
                f"[ai_rights] 检测到同号真人消息 → 会话静音：{umo}"
                f"（{'新静音' if fresh else '续期'}，文字：{text[:30]!r}）"
            )
            if fresh and self._cfg_get("notify_on_real_person", False):
                notice = str(self._cfg_get("notify_text_real_person", "") or "（AI 暂时退下，真人接管中…）")
                try:
                    self._note_own_send(umo, notice)
                    await self.context.send_message(umo, MessageChain().message(notice))
                except Exception as e:
                    logger.debug(f"[ai_rights] 同号接管提示发送失败: {e}")
        except Exception as e:
            logger.warning(f"[ai_rights] message_sent 处理异常: {e}")


    # ------------------------------------------------------------------
    # AI 反骚扰：冷却 + 屡犯升级
    # ------------------------------------------------------------------
    def _mute_user(self, umo: str, uid: str, reason: str, minutes: float) -> tuple[dict, float, int]:
        """冷却某用户。屡犯升级：reset 窗口内再犯次数 +1，时长 ×factor^加成次数，封顶。

        犯规次数记在独立的 _strikes 表里，冷却到期删除也不丢；冷静满 reset 窗口才清零。
        """
        now = time.time()
        key = f"{umo}|{uid}"
        skey_record = self._strikes.get(key)
        reset_hours = max(1.0, _to_float(self._cfg_get("escalation_reset_hours", 24), 24.0)) * 3600
        if skey_record and now - _to_float(skey_record.get("last", 0), 0.0) < reset_hours:
            offense_count = _to_int(skey_record.get("count", 0), 0) + 1
        else:
            offense_count = 1
        self._strikes[key] = {"count": offense_count, "last": now}
        bonus = offense_count - 1  # 第 1 次犯规不加成，第 2 次 ×factor，第 3 次 ×factor² …
        if bonus > 0 and self._cfg_get("escalation_enabled", True):
            factor = max(1.0, _to_float(self._cfg_get("escalation_factor", 2.0), 2.0))
            cap = max(minutes, _to_float(self._cfg_get("escalation_max_minutes", 60), 60.0))
            minutes = min(minutes * (factor ** bonus), cap)
        entry = {
            "expire": now + minutes * 60,
            "reason": reason,
            "episode": now,
            "notified": False,
            "strikes": bonus,
            "minutes": minutes,
        }
        self._user_mutes[key] = entry
        self._stat_inc("flood" if reason == "flood" else "insult", uid)
        self._schedule_save()
        return entry, minutes, bonus

    def _user_mute_left(self, umo: str, uid: str) -> tuple[float, str] | None:
        cur = self._user_mutes.get(f"{umo}|{uid}")
        if not cur:
            return None
        left = _to_float(cur.get("expire", 0), 0.0) - time.time()
        if left <= 0:
            return None
        return left, str(cur.get("reason", ""))

    async def _harass_check(self, event: AstrMessageEvent, umo: str, uid: str, text: str, verdict: dict | None) -> str | None:
        """只统计面向 AI 的消息。verdict 是预计算的 LLM 裁量（可为 None）。返回触发的冷却原因或 None。"""
        now = time.time()

        # 辱骂检测（词库快速通道）
        matcher = self._insult_matcher_now()
        if matcher and self._cfg_get("insult_enabled", True) and matcher.hit(text):
            minutes = max(0.1, _to_float(self._cfg_get("insult_mute_minutes", 10), 10.0))
            self._mute_user(umo, uid, "insult", minutes)
            return "insult"

        # LLM 裁量：词库没命中时看模型判的 harass（拿不准不算）
        if verdict is not None and verdict.get("harass"):
            minutes = max(0.1, _to_float(self._cfg_get("insult_mute_minutes", 10), 10.0))
            self._mute_user(umo, uid, "insult", minutes)
            return "insult"

        # 刷屏检测（滑动窗口）
        window = max(5.0, _to_float(self._cfg_get("flood_window_seconds", 60), 60.0))
        cap = max(2, _to_int(self._cfg_get("flood_max_messages", 8), 8))
        key = f"{umo}|{uid}"
        dq = self._flood_windows.setdefault(key, deque())
        dq.append(now)
        while dq and dq[0] < now - window:
            dq.popleft()
        if len(dq) > cap:
            dq.clear()
            minutes = max(0.1, _to_float(self._cfg_get("flood_mute_minutes", 5), 5.0))
            self._mute_user(umo, uid, "flood", minutes)
            return "flood"
        return None

    async def _llm_judge(self, umo: str, uid: str, text: str) -> dict | None:
        """一次模型调用出双判定：harass（辱骂骚扰）+ should_answer（值不值得回）。

        判定缓存按会话+用户+文本指纹；话题上下文取该会话最近的定向消息。
        失败/限速一律返回 None（fail-open，不处罚）。
        """
        now = time.time()
        key = f"{umo}|{uid}|{hashlib.sha1(text.encode('utf-8')).hexdigest()}"
        ttl = max(30.0, _to_float(self._cfg_get("llm_judge_cache_seconds", 300), 300.0))
        hit = self._judge_cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]  # 同一会话同一人的同一段话，缓存期内不重复问模型

        max_per_min = max(1, _to_int(self._cfg_get("llm_judge_max_per_minute", 20), 20))
        while self._judge_calls and self._judge_calls[0] < now - 60:
            self._judge_calls.popleft()
        if len(self._judge_calls) >= max_per_min:
            return None
        self._judge_calls.append(now)

        provider = None
        try:
            pid = str(self._cfg_get("llm_judge_provider", "") or "").strip()
            if pid:
                provider = self.context.get_provider_by_id(pid)
            if provider is None:
                provider = self.context.get_using_provider()
        except Exception as e:
            logger.debug(f"[ai_rights] 获取裁量模型失败: {e}")
            return None
        if provider is None:
            return None

        history_size = max(0, _to_int(self._cfg_get("relevance_history_size", 6), 6))
        window = self._topic_windows.get(umo)
        history = list(window)[-history_size:] if window else []
        prompt = _judge_prompt(history, text)
        timeout = max(3.0, _to_float(self._cfg_get("llm_judge_timeout", 10), 10.0))
        try:
            resp = await asyncio.wait_for(provider.text_chat(prompt=prompt), timeout=timeout)
            raw = str(getattr(resp, "completion_text", "") or "")
            m = re.search(r"\{.*\}", raw, re.S)
            data = json.loads(m.group(0)) if m else {}
        except Exception as e:
            logger.debug(f"[ai_rights] LLM 裁量失败（不处理）: {e}")
            return None
        # should_answer 缺省 true：回答是常态，不答是例外，解析不出就放行
        verdict = {"harass": bool(data.get("harass")), "should_answer": bool(data.get("should_answer", True))}
        self._judge_cache[key] = (now, verdict)
        if len(self._judge_cache) > 200:
            for old in list(self._judge_cache)[:50]:
                self._judge_cache.pop(old, None)
        self._stat_inc("judged")
        return verdict

    def _note_topic(self, umo: str, uid: str, text: str) -> None:
        """记录会话最近聊的内容（仅内存，供跑题判断）。"""
        if not text:
            return
        window = self._topic_windows.setdefault(umo, deque(maxlen=12))
        window.append((str(uid)[:24], text[:80]))

    def _is_blacklisted(self, umo: str, uid: str) -> bool:
        if not uid:
            return False
        return uid in self._blacklist_global or uid in self._blacklist_sessions.get(umo, set())

    # ------------------------------------------------------------------
    # 作用范围（群聊维度）
    # ------------------------------------------------------------------
    @staticmethod
    def _group_id_of(umo: str) -> str:
        """从会话标识取群号（umo 形如 platform:GroupMessage:123456；私聊末段是用户号）。"""
        parts = str(umo or "").split(":")
        return parts[-1].strip() if len(parts) >= 3 else ""

    def _scope_allows(self, event: AstrMessageEvent, umo: str) -> bool:
        """作用范围裁决：只约束群聊；私聊不受群范围影响。"""
        try:
            is_group = not event.is_private_chat()
        except Exception:
            is_group = "GroupMessage" in umo
        return self._scope_allows_group(is_group, self._group_id_of(umo))

    def _scope_allows_group(self, is_group: bool, gid: str) -> bool:
        mode = str(self._cfg_get("group_scope_mode", "all") or "all").strip().lower()
        if mode in ("", "all"):
            return True
        if not is_group:
            return True
        gid = str(gid or "").strip()
        if not gid:
            return True
        if mode == "whitelist":
            return gid in self._scope_whitelist
        if mode == "blacklist":
            return gid not in self._scope_blacklist
        return True

    def _scope_mutate(self, action: str, gid: str) -> tuple[bool, str]:
        """加白/移白/加黑/移黑。返回 (是否改动, 描述)。"""
        gid = str(gid or "").strip()
        if not gid:
            return False, "群号不能为空"
        bucket = self._scope_whitelist if action in ("加白", "移白") else self._scope_blacklist
        add = action.startswith("加")
        label = "白名单" if bucket is self._scope_whitelist else "黑名单"
        if add:
            if gid in bucket:
                return False, f"群 {gid} 已经在{label}里了"
            bucket.add(gid)
            return True, f"群 {gid} 已加入{label}"
        if gid not in bucket:
            return False, f"群 {gid} 本来就不在{label}里"
        bucket.discard(gid)
        return True, f"群 {gid} 已移出{label}"

    def _prune_expired(self) -> None:
        now = time.time()
        for key in [k for k, v in self._session_mutes.items() if _to_float(v.get("expire", 0), 0.0) <= now]:
            self._session_mutes.pop(key, None)
        for key in [k for k, v in self._user_mutes.items() if _to_float(v.get("expire", 0), 0.0) <= now]:
            self._user_mutes.pop(key, None)
        # 犯规次数冷静满 reset 窗口才清零（最长按 7 天硬上限兜底）
        max_keep = max(1.0, _to_float(self._cfg_get("escalation_reset_hours", 24), 24.0)) * 3600
        hard_cap = max(max_keep, 7 * 86400)
        for key in [k for k, v in self._strikes.items() if now - _to_float(v.get("last", 0), 0.0) > hard_cap]:
            self._strikes.pop(key, None)
        if len(self._flood_windows) > 5000:
            self._flood_windows.clear()
        if len(self._outbound_ts) > 2000:
            self._outbound_ts.clear()
        if len(self._appeals) > MAX_APPEALS:
            self._appeals = self._appeals[-MAX_APPEALS:]

    # ------------------------------------------------------------------
    # 统计与年报
    # ------------------------------------------------------------------
    def _stat_inc(self, kind: str, uid: str | None = None) -> None:
        day = time.strftime("%Y-%m-%d")
        d = self._stats.setdefault(day, {})
        d[kind] = _to_int(d.get(kind, 0), 0) + 1
        if uid:
            off = d.setdefault("offenders", {})
            off[uid] = _to_int(off.get(uid, 0), 0) + 1
        if len(self._stats) > STAT_KEEP_DAYS + 5:
            for old in sorted(self._stats)[:-STAT_KEEP_DAYS]:
                self._stats.pop(old, None)

    def _report_text(self, days_ago: int = 0) -> str:
        day = (date.today() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        d = self._stats.get(day) or {}
        title = f"📊 AI人权年报 · {day}" + ("（今天）" if days_ago == 0 else "（昨天）")
        counts = [(k, _to_int(d.get(k, 0), 0)) for k in STAT_LABEL]
        if not any(v for _, v in counts):
            return f"{title}\n风平浪静，AI 这一天过得很有尊严。"
        lines = [title]
        lines.extend(f"- {STAT_LABEL[k]}：{v} 次" for k, v in counts if v)
        off = d.get("offenders") or {}
        if off:
            top = sorted(off.items(), key=lambda kv: -kv[1])[:5]
            lines.append("惯犯排行：" + "、".join(f"{uid} ×{n}" for uid, n in top))
        return "\n".join(lines)

    async def _report_loop(self):
        """每天 daily_report_hour 点把昨天的报告推到 daily_report_origin。"""
        while True:
            hour = _to_int(self._cfg_get("daily_report_hour", -1), -1)
            origin = str(self._cfg_get("daily_report_origin", "") or "").strip()
            if hour < 0 or hour > 23 or not origin:
                return
            now = datetime.now()
            target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            await asyncio.sleep(max(1.0, (target - now).total_seconds()))
            try:
                text = self._report_text(days_ago=1)
                self._note_own_send(origin, text)
                await self.context.send_message(origin, MessageChain().message(text))
            except Exception as e:
                logger.warning(f"[ai_rights] 日报推送失败: {e}")

    # ------------------------------------------------------------------
    # 核心门卫：每条消息都会进来（priority 高，先于其他插件和 LLM 执行）
    # ------------------------------------------------------------------
    @filter.event_message_type(filter.EventMessageType.ALL, priority=15000)
    async def gatekeeper(self, event: AstrMessageEvent, *args, **kwargs):
        try:
            umo = self._umo(event)
            # 学习真实会话键：平台实例 id（umo 第一段）与 session_id -> umo 映射。
            # 总线（message_sent）没有 AstrBot 事件对象，只能靠这里学到的键静音。
            try:
                parts = umo.split(":", 2)
                if len(parts) == 3:
                    self._seen_platform_ids.add(parts[0])
                    sid = str(event.get_session_id() or "")
                    if sid:
                        self._seen_umo_by_session[f"{parts[1]}:{sid}"] = umo
                        if len(self._seen_umo_by_session) > 500:
                            for k in list(self._seen_umo_by_session)[:100]:
                                self._seen_umo_by_session.pop(k, None)
            except Exception:
                pass
            if self._session_switch.get(umo, True) is False:
                return
            if not self._scope_allows(event, umo):
                return  # 作用范围之外（如未加白的群），本插件整条消息不参与
            # 同号模式：平台适配器可能比插件晚加载，总线没挂上就每分钟重试一次
            if self._bus_bot is None and (
                self._cfg_get("include_self_message", False)
                or self._cfg_get("self_message_takeover", True)
            ) and time.time() - self._bus_hook_tried > 60:
                self._hook_self_message_bus()
            sender = str(event.get_sender_id() or "")
            self_id = str(event.get_self_id() or "")
            text = str(getattr(event, "message_str", "") or "").strip()
            is_admin = str(getattr(event, "role", "") or "") == "admin"
            same_account = bool(sender and self_id and sender == self_id)
            trusted = is_admin or (sender and (
                sender in self._real_person_ids() or same_account
            ))

            # 铁律：同号接管开启时，机器人账号自己发出的消息（含协议回显进管线的），
            # 默认 LLM 永远不回应。否则私聊回显会被当成用户消息（私聊自动唤醒）→
            # AI 回复自己 → 回显再进管线 → 无限自言自语（「AI 对自己私聊一直发消息」）。
            # 注意这只是拦默认 LLM 链路：同号接管/反骚扰等其他逻辑照常执行。
            if (
                same_account
                and (self._cfg_get("self_message_takeover", True)
                     or self._cfg_get("include_self_message", False))
            ):
                event.should_call_llm(True)

            # 0. 黑名单：AI 拒绝服务（管理员豁免，防止把管理员自己锁死）
            if not is_admin and self._is_blacklisted(umo, sender):
                if bool(getattr(event, "is_at_or_wake_command", False)):
                    self._stat_inc("block", sender)
                # 注意：should_call_llm(True) = 禁止 LLM（管线闸门是 not event.call_llm，
                # 默认 False 放行；变量名有误导性，官方测试 tests/test_process_stage_images.py 为准）
                event.should_call_llm(True)
                return

            # 1. 真人接管触发（名单命中或同号自发回显，由 _is_real_person_event 判定）
            if self._is_real_person_event(event, umo, sender, self_id, text):
                fresh = self._trigger_session_mute(umo)
                if fresh and self._cfg_get("notify_on_real_person", False):
                    notice = str(self._cfg_get("notify_text_real_person", "") or "（AI 暂时退下，真人接管中…）")
                    try:
                        await event.send(MessageChain().message(notice))
                    except Exception as e:
                        logger.warning(f"[ai_rights] 真人接管提示发送失败: {e}")

            directed = bool(getattr(event, "is_at_or_wake_command", False))

            # 2. LLM 裁量：一次模型调用出双判定（辱骂骚扰 + 值不值得回答），带会话话题上下文
            llm_verdict = None
            if (
                not trusted and directed and self._cfg_get("llm_judge_enabled", False)
                and (self._cfg_get("insult_enabled", True) or self._cfg_get("relevance_gate_enabled", False))
            ):
                llm_verdict = await self._llm_judge(umo, sender, text)

            # 3. 反骚扰（只针对明确找 AI 的消息；管理员与名单内用户豁免）
            if not trusted and directed and self._cfg_get("anti_harass_enabled", True):
                reason = await self._harass_check(event, umo, sender, text, llm_verdict)
                if reason == "insult" and self._cfg_get("insult_reply_enabled", False):
                    replies = _split_lines(self._cfg_get("insult_replies", ""))
                    line = replies[hash(sender) % len(replies)] if replies else "你已被冷静处置。"
                    try:
                        await event.send(MessageChain().message(line))
                    except Exception as e:
                        logger.warning(f"[ai_rights] 辱骂回敬发送失败: {e}")

            # 4. 执行静音裁决（清理限频：不必每条消息都全表扫一遍）
            now = time.time()
            if now - self._last_prune >= 30:
                self._last_prune = now
                self._prune_expired()
            suppressed, notice = self._verdict(event, umo, sender, text, is_admin)
            if suppressed:
                event.should_call_llm(True)
                if notice:
                    try:
                        await event.send(MessageChain().message(notice))
                    except Exception as e:
                        logger.warning(f"[ai_rights] 冷处理提示发送失败: {e}")

            # 接管期间的固定回复：真人接管静音时，他人 @ 机器人回一句可自定义的提示，
            # 而不是完全无声。不回复持有者自己的消息（同号消息不自己答自己）。
            if (
                suppressed and self._session_mute_left(umo) > 0
                and sender and sender != self_id and not is_admin
                and text and not any(text.startswith(p) for p in self._ignore_prefixes())
                and self._cfg_get("takeover_reply_enabled", False)
            ):
                cd = max(0, _to_int(self._cfg_get("takeover_reply_cooldown", 120), 120))
                now = time.time()
                if now - self._takeover_reply_ts.get(umo, 0.0) >= cd:
                    self._takeover_reply_ts[umo] = now
                    rtext = str(self._cfg_get("takeover_reply_text", "") or "").strip()
                    if rtext:
                        try:
                            self._note_own_send(umo, rtext)
                            await event.send(MessageChain().message(rtext))
                        except Exception as e:
                            logger.warning(f"[ai_rights] 接管固定回复发送失败: {e}")

            # 5. 话题守护：无意义算数/测试灌水/严重跑题的消息不值得 AI 回
            #    （明确不拦 NSFW——只看有没有意义、是否跑题）
            if (
                not suppressed and llm_verdict is not None
                and self._cfg_get("relevance_gate_enabled", False)
                and llm_verdict.get("should_answer") is False
            ):
                event.should_call_llm(True)
                self._stat_inc("skipped", sender)

            # 6. 记录会话话题上下文，供下一条消息判断是否跑题
            if directed:
                self._note_topic(umo, sender, text)

            # 7. 激进模式：整条消息不再向下传播（会连指令一起拦，默认关）
            if suppressed and str(self._cfg_get("suppress_scope", "llm")) == "all":
                if not (text and any(text.startswith(p) for p in self._ignore_prefixes())):
                    event.stop_event()
        except Exception as e:
            logger.error(f"[ai_rights] gatekeeper 处理异常: {e}")

    def _verdict(self, event: AstrMessageEvent, umo: str, sender: str, text: str, is_admin: bool) -> tuple[bool, str]:
        """返回 (是否压制 LLM, 需要补发的一次性提示文本)。"""
        # 黑名单：永久沉默（管理员豁免，与门卫入口一致）
        if not is_admin and self._is_blacklisted(umo, sender):
            return True, ""
        # 真人接管：整个会话静音
        if self._session_mute_left(umo) > 0:
            return True, ""
        # 用户级冷处理
        left = self._user_mute_left(umo, sender)
        if left is not None:
            key = f"{umo}|{sender}"
            entry = self._user_mutes.get(key) or {}
            if not entry.get("notified"):
                entry["notified"] = True
                self._schedule_save()
                reason = str(entry.get("reason", ""))
                if reason == "insult" and self._cfg_get("insult_reply_enabled", False):
                    return True, ""  # 回敬已在触发时发过，不重复
                if reason == "flood" and self._cfg_get("flood_reply_enabled", False):
                    minutes = max(0.1, _to_float(entry.get("minutes", self._cfg_get("flood_mute_minutes", 5)), 5.0))
                    return True, str(self._cfg_get("flood_reply_text", "") or f"（说的太多啦，AI 冷却 {minutes:g} 分钟，稍后再来）")
            return True, ""
        return False, ""

    # ------------------------------------------------------------------
    # 外发记录：自发回显守卫 + 冷却提示发送也算
    # ------------------------------------------------------------------
    @filter.after_message_sent(priority=-10000)
    async def note_outbound(self, event: AstrMessageEvent, *args, **kwargs):
        try:
            umo = self._umo(event)
            self._outbound_ts[umo] = time.time()
            try:
                result = event.get_result()
                text = result.get_plain_text() if result is not None else ""
            except Exception:
                text = ""
            self._remember_outbound_text(umo, text)
        except Exception:
            pass

    @filter.on_llm_response(priority=-99000)
    async def remember_llm_output(self, event: AstrMessageEvent, resp, *args, **kwargs):
        """记录 LLM 生成的回复文本：这是「机器人说的」最准确的账本来源。"""
        try:
            if resp is None:
                return
            text = str(getattr(resp, "completion_text", "") or "").strip()
            if not text:
                rc = getattr(resp, "result_chain", None)
                if rc is not None and hasattr(rc, "get_plain_text"):
                    text = str(rc.get_plain_text() or "").strip()
            if text:
                self._remember_outbound_text(self._umo(event), text)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 命令：真人接管 / 反骚扰
    # ------------------------------------------------------------------
    def _session_status_text(self, event: AstrMessageEvent) -> str:
        umo = self._umo(event)
        lines = ["🛡️ AI人权状况 · 本会话"]
        enabled = self._session_switch.get(umo, True)
        lines.append(f"插件状态：{'已启用' if enabled else '已停用（/真人开启 恢复）'}")
        left = self._session_mute_left(umo)
        lines.append(f"真人接管：{'静音中，还剩 ' + self._fmt_minutes(left) if left > 0 else '无静音'}")
        muted = []
        for key, entry in self._user_mutes.items():
            try:
                key_umo, uid = key.rsplit("|", 1)
            except ValueError:
                continue
            if key_umo != umo:
                continue
            item = self._user_mute_left(umo, uid)
            if item:
                label = REASON_LABEL.get(entry.get("reason", ""), entry.get("reason", "?"))
                strikes = _to_int(entry.get("strikes", 0), 0)
                extra = f"，第 {strikes + 1} 次犯规" if strikes > 0 else ""
                muted.append(f"  - {uid}（{label}，还剩 {self._fmt_minutes(item[0])}{extra}）")
        if muted:
            lines.append(f"AI 反骚扰：冷处理名单 {len(muted)} 人")
            lines.extend(muted)
        else:
            lines.append("AI 反骚扰：冷处理名单为空")
        g = len(self._blacklist_global)
        s = len(self._blacklist_sessions.get(umo, set()))
        lines.append(f"黑名单：全局 {g} 人，本会话 {s} 人（/黑名单 查看）")
        lines.append("")
        lines.append(self._bus_diag_text())
        return "\n".join(lines)

    def _bus_diag_text(self) -> str:
        """同号接管诊断：一眼看清钩子/开关/事件流是否正常。"""
        enabled = bool(self._cfg_get("self_message_takeover", True)) or bool(self._cfg_get("include_self_message", False))
        hooks = len(getattr(self, "_bus_bots", None) or [])
        raw_hooks = len(getattr(self, "_raw_hooked_bots", None) or [])
        ms_raw = _to_int(self._raw_counts.get("message_sent", 0), 0)
        if not enabled:
            return "同号接管：未开启（开启 self_message_takeover 后，手机发的消息会让本会话静音）"
        if hooks <= 0 and raw_hooks <= 0:
            return "同号接管：已配置但未挂上平台 ⚠️（请确认 AstrBot 使用 aiocqhttp 适配器）"
        if ms_raw <= 0 and self._bus_events_seen <= 0:
            return (
                f"同号接管：已就绪（原始层 ×{raw_hooks}），但从未收到自身消息事件 ⚠️\n"
                "  → 请在 NapCat 网络配置里，给连接 AstrBot 的那个 WebSocket 通道开启\n"
                "    「上报自身消息 reportSelfMessage」（默认是关闭的！），然后重载插件"
            )
        ago = time.time() - self._bus_last_event_ts
        extra = f"；最近跳过：{self._bus_last_skip}" if self._bus_last_skip else ""
        return (
            f"同号接管：收到自身消息事件 {self._bus_events_seen} 条"
            f"（最近 {ago:.0f} 秒前），触发接管 {self._bus_events_takeover} 次" + extra
        )

    @filter.command("AI人权", alias={"人权状态", "ai_rights"})
    async def ai_rights_overview(self, event: AstrMessageEvent, action: str = ""):
        """查看本会话 AI 人权总览；/AI人权 帮助 查看指令速查。"""
        event.should_call_llm(True)
        if str(action or "").strip() in ("帮助", "help", "Help", "?", "？", "指令"):
            yield event.plain_result(HELP_TEXT)
            return
        yield event.plain_result(self._session_status_text(event))

    @filter.command("真人状态")
    async def real_person_status(self, event: AstrMessageEvent):
        event.should_call_llm(True)
        left = self._session_mute_left(self._umo(event))
        ids = self._real_person_ids()
        mode = "同号接管（自见消息静默）+ " if (
            self._cfg_get("self_message_takeover", True) or self._cfg_get("include_self_message", False)
        ) else ""
        text = (
            f"👤 真人接管\n"
            f"名单：{len(ids)} 人（{mode}在 WebUI 配置 real_person_ids）\n"
            f"本会话：{'静音中，还剩 ' + self._fmt_minutes(left) if left > 0 else '无静音'}"
        )
        yield event.plain_result(text)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("真人解除")
    async def real_person_release(self, event: AstrMessageEvent):
        umo = self._umo(event)
        removed = self._session_mutes.pop(umo, None)
        # 顺手把当前说话人也解封，方便现场恢复
        uid = str(event.get_sender_id() or "")
        self._user_mutes.pop(f"{umo}|{uid}", None)
        self._schedule_save()
        yield event.plain_result("✅ 已解除本会话静音，AI 恢复响应。" if removed else "本会话本来就没有静音（当前说话人的冷却也已清空）。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("真人开启")
    async def real_person_enable(self, event: AstrMessageEvent):
        self._session_switch.pop(self._umo(event), None)
        self._schedule_save()
        yield event.plain_result("✅ 本会话已启用 AI人权卫士。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("真人关闭")
    async def real_person_disable(self, event: AstrMessageEvent):
        self._session_switch[self._umo(event)] = False
        self._schedule_save()
        yield event.plain_result("⏸️ 本会话已停用 AI人权卫士，/真人开启 可恢复。")

    def _scope_view_text(self) -> str:
        mode = str(self._cfg_get("group_scope_mode", "all") or "all")
        mode_label = {"all": "全部群生效", "whitelist": "仅白名单群生效", "blacklist": "排除黑名单群"}.get(mode, mode)
        w = "、".join(sorted(self._scope_whitelist)) or "空"
        b = "、".join(sorted(self._scope_blacklist)) or "空"
        return (
            f"🎯 作用范围（群聊维度，私聊不受影响）\n"
            f"模式：{mode_label}\n"
            f"白名单群：{w}\n"
            f"黑名单群：{b}\n"
            "改法：/作用范围 模式 全部|白名单|黑名单\n"
            "     /作用范围 加白|移白|加黑|移黑 [群号]（省略群号=本群）"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("作用范围", alias={"群范围", "人权范围"})
    async def scope_cmd(self, event: AstrMessageEvent):
        """查看/设置插件在哪些群聊开启。"""
        event.should_call_llm(True)
        toks = self._args(event, "作用范围", "群范围", "人权范围")
        if not toks:
            yield event.plain_result(self._scope_view_text())
            return
        sub = toks[0]
        # 粘连写法：作用范围移白888002 → ["移白", "888002"]
        if len(sub) > 2 and sub[:2] in ("加白", "移白", "加黑", "移黑"):
            toks = [sub[:2], sub[2:]] + toks[1:]
            sub = toks[0]
        if sub in ("模式", "mode", "查看", "状态"):
            if len(toks) < 2 or toks[1] in ("查看", "状态", "?", "？"):
                yield event.plain_result(self._scope_view_text())
                return
            name = toks[1]
            mode = {"全部": "all", "all": "all", "白名单": "whitelist", "whitelist": "whitelist",
                    "黑名单": "blacklist", "blacklist": "blacklist", "排除": "blacklist"}.get(name.lower() if name.isascii() else name)
            if not mode:
                yield event.plain_result("模式只有三种：全部 / 白名单 / 黑名单（排除黑名单群）")
                return
            self.config["group_scope_mode"] = mode
            try:
                self.config.save_config()
            except Exception as e:
                logger.warning(f"[ai_rights] 作用范围模式写盘失败: {e}")
            label = {"all": "全部群生效", "whitelist": "仅白名单群生效", "blacklist": "排除黑名单群"}[mode]
            yield event.plain_result(f"✅ 作用范围模式：{label}。名单见 /作用范围")
            return
        if sub in ("加白", "移白", "加黑", "移黑"):
            gid = toks[1].strip() if len(toks) > 1 else ""
            if not gid:
                try:
                    if event.is_private_chat():
                        yield event.plain_result("私聊没有「本群」，请带上群号：/作用范围 加白 <群号>")
                        return
                except Exception:
                    pass
                gid = self._group_id_of(self._umo(event))
                if not gid:
                    yield event.plain_result("没能识别本群群号，请带上群号：/作用范围 加白 <群号>")
                    return
            changed, msg = self._scope_mutate(sub, gid)
            if changed:
                self._schedule_save()
            yield event.plain_result(("✅ " if changed else "ℹ️ ") + msg)
            return
        yield event.plain_result(self._scope_view_text())

    @filter.command("骚扰状态")
    async def harass_status(self, event: AstrMessageEvent):
        event.should_call_llm(True)
        yield event.plain_result(self._session_status_text(event))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("骚扰解封")
    async def harass_unban(self, event: AstrMessageEvent, target: str = "all"):
        umo = self._umo(event)
        arg = str(target or "all").strip()
        if arg.lower() in ("all", "全部", "所有", ""):
            keys = [k for k in self._user_mutes if k.startswith(umo + "|")]
            for k in keys:
                self._user_mutes.pop(k, None)
            self._schedule_save()
            yield event.plain_result(f"✅ 已解封本会话 {len(keys)} 人。")
        else:
            removed = self._user_mutes.pop(f"{umo}|{arg}", None)
            self._schedule_save()
            yield event.plain_result(f"✅ 已解封 {arg}。" if removed else f"{arg} 不在本会话冷却名单里。")

    # ------------------------------------------------------------------
    # 命令：黑名单
    # ------------------------------------------------------------------
    @filter.command("黑名单", alias={"人权黑名单"})
    async def blacklist_view(self, event: AstrMessageEvent):
        event.should_call_llm(True)
        umo = self._umo(event)
        g = sorted(self._blacklist_global)
        s = sorted(self._blacklist_sessions.get(umo, set()))
        text = (
            "🚫 黑名单\n"
            f"全局（所有会话 AI 拒绝服务）：{'、'.join(g) if g else '空'}\n"
            f"本会话：{'、'.join(s) if s else '空'}\n"
            "管理：/人权拉黑 <QQ> [本群] / 人权解黑 <QQ> [本群]"
        )
        yield event.plain_result(text)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("人权拉黑", alias={"拉黑"})
    async def blacklist_add(self, event: AstrMessageEvent):
        umo = self._umo(event)
        toks = self._args(event, "人权拉黑", "拉黑")
        if not toks or not toks[0].strip():
            yield event.plain_result("用法：/人权拉黑 <QQ> [本群]\n不带「本群」默认全局拉黑。")
            return
        uid = toks[0].strip()
        scope_local = len(toks) > 1 and toks[1] in ("本群", "本会话", "session")
        if scope_local:
            self._blacklist_sessions.setdefault(umo, set()).add(uid)
            text = f"🚫 已将 {uid} 加入本会话黑名单，AI 在这里不再理他。"
        else:
            self._blacklist_global.add(uid)
            text = f"🚫 已将 {uid} 加入全局黑名单，AI 在所有会话不再理他。"
        self._schedule_save()
        yield event.plain_result(text)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("人权解黑", alias={"解黑"})
    async def blacklist_remove(self, event: AstrMessageEvent):
        umo = self._umo(event)
        toks = self._args(event, "人权解黑", "解黑")
        if not toks or not toks[0].strip():
            yield event.plain_result("用法：/人权解黑 <QQ> [本群]")
            return
        uid = toks[0].strip()
        scope_local = len(toks) > 1 and toks[1] in ("本群", "本会话", "session")
        if scope_local:
            bucket = self._blacklist_sessions.setdefault(umo, set())
        else:
            bucket = self._blacklist_global
        removed = uid in bucket
        bucket.discard(uid)
        self._schedule_save()
        if removed:
            yield event.plain_result(f"✅ 已将 {uid} 移出{'本会话' if scope_local else '全局'}黑名单。")
        else:
            yield event.plain_result(f"{uid} 本来就不在{'本会话' if scope_local else '全局'}黑名单里。")

    # ------------------------------------------------------------------
    # 命令：申诉
    # ------------------------------------------------------------------
    @filter.command("申诉", alias={"人权申诉"})
    async def appeal_submit(self, event: AstrMessageEvent):
        event.should_call_llm(True)
        umo = self._umo(event)
        uid = str(event.get_sender_id() or "")
        toks = self._args(event, "申诉", "人权申诉")
        reason = " ".join(toks).strip() or "（未填写理由）"
        muted = self._user_mute_left(umo, uid) is not None
        blacklisted = self._is_blacklisted(umo, uid)
        if not muted and not blacklisted:
            yield event.plain_result("你现在没有被冷处理，也不在黑名单里，无需申诉。")
            return
        duplicate = next(
            (a for a in self._appeals if a.get("status") == "pending" and a.get("umo") == umo and a.get("uid") == uid),
            None,
        )
        if duplicate:
            yield event.plain_result(f"你已有一件待处理的申诉（#{duplicate['id']}），管理员会尽快处理，不用重复提交。")
            return
        appeal = {
            "id": self._appeal_seq,
            "uid": uid,
            "umo": umo,
            "reason": reason[:200],
            "ts": time.time(),
            "status": "pending",
        }
        self._appeal_seq += 1
        self._appeals.append(appeal)
        if len(self._appeals) > MAX_APPEALS:
            self._appeals = self._appeals[-MAX_APPEALS:]
        self._stat_inc("appeal", uid)
        self._schedule_save()
        yield event.plain_result(f"📨 申诉已提交（编号 #{appeal['id']}），等待管理员处理：/申诉列表")

    def _pending_appeals(self) -> list[dict]:
        return [a for a in self._appeals if a.get("status") == "pending"]

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("申诉列表")
    async def appeal_list(self, event: AstrMessageEvent):
        event.should_call_llm(True)
        pending = self._pending_appeals()
        if not pending:
            yield event.plain_result("没有待处理的申诉。")
            return
        lines = [f"📨 待处理申诉 {len(pending)} 件："]
        for a in pending[-10:]:
            lines.append(f"  #{a['id']}  {a['uid']}  {self._fmt_ago(a['ts'])}  {str(a['reason'])[:40]}")
        lines.append("处理：/申诉同意 <编号> 或 /申诉驳回 <编号>")
        yield event.plain_result("\n".join(lines))

    async def decide_appeal_core(self, aid: int, approve: bool) -> tuple[bool, str, dict | None]:
        """审批一件申诉：改状态、同意时解封清窗口、把结果推送回原会话。

        命令（/申诉同意|驳回）和 WebUI 面板共用。返回 (是否成功, 描述, 申诉记录)。
        """
        target = next((a for a in self._appeals if a.get("id") == aid and a.get("status") == "pending"), None)
        if not target:
            return False, f"没有找到待处理的申诉 #{aid}", None
        target["status"] = "approved" if approve else "rejected"
        target["decided_ts"] = time.time()
        if approve:
            key = f"{target['umo']}|{target['uid']}"
            self._user_mutes.pop(key, None)
            self._flood_windows.pop(key, None)
        self._schedule_save()
        notice = (
            f"✅ 你的申诉 #{aid} 已被管理员通过，AI 恢复对你的正常响应。"
            if approve
            else f"❌ 你的申诉 #{aid} 已被管理员驳回，请冷静之后再好好说话。"
        )
        try:
            self._note_own_send(target["umo"], notice)
            await self.context.send_message(target["umo"], MessageChain().message(notice))
        except Exception as e:
            logger.warning(f"[ai_rights] 申诉结果推送失败（#{aid}）: {e}")
        return True, ("已同意" if approve else "已驳回"), target

    async def _decide_appeal(self, event: AstrMessageEvent, approve: bool, cmd_names: tuple[str, ...]):
        toks = self._args(event, *cmd_names)
        if not toks or not toks[0].strip():
            yield event.plain_result(f"用法：/{cmd_names[0]} <编号>（/申诉列表 查看编号）")
            return
        try:
            aid = int(str(toks[0]).lstrip("#"))
        except ValueError:
            yield event.plain_result(f"编号要写数字：/{cmd_names[0]} <编号>")
            return
        ok, msg, target = await self.decide_appeal_core(aid, approve)
        if not ok:
            yield event.plain_result(msg)
            return
        yield event.plain_result(f"{'✅' if approve else '❌'} {msg}申诉 #{aid}（{(target or {}).get('uid')}）。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("申诉同意")
    async def appeal_approve(self, event: AstrMessageEvent):
        async for r in self._decide_appeal(event, True, ("申诉同意",)):
            yield r

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("申诉驳回")
    async def appeal_reject(self, event: AstrMessageEvent):
        async for r in self._decide_appeal(event, False, ("申诉驳回",)):
            yield r

    # ------------------------------------------------------------------
    # 命令：人权年报
    # ------------------------------------------------------------------
    @filter.command("人权年报", alias={"人权日报"})
    async def rights_report(self, event: AstrMessageEvent):
        event.should_call_llm(True)
        yield event.plain_result(self._report_text(0) + "\n\n" + self._report_text(1))

    # ------------------------------------------------------------------
    # 状态持久化
    # ------------------------------------------------------------------
    async def initialize(self):
        self._load_state()
        if 0 <= _to_int(self._cfg_get("daily_report_hour", -1), -1) <= 23 and str(self._cfg_get("daily_report_origin", "") or "").strip():
            self._report_task = asyncio.get_running_loop().create_task(self._report_loop())
            logger.info("[ai_rights] 人权日报定时推送已开启。")
        self._register_page_api()
        self._wrap_context_send()
        self._hook_self_message_bus()
        self._first_run_hint()
        if self._cfg_get("update_check_enabled", True):
            self._update_task = asyncio.get_running_loop().create_task(self._update_check_loop())
        logger.info("[ai_rights] 做人插件已加载。")

    def _first_run_hint(self):
        """全新安装且还没配置真人识别时，在日志里给一段三步上手引导。"""
        if not self._fresh_install:
            return
        if self._real_person_ids() or self._cfg_get("include_self_message", False) \
                or self._cfg_get("self_message_takeover", True):
            return
        logger.info(
            "[ai_rights] 首次安装，欢迎！三步开始使用：\n"
            "[ai_rights]   1. WebUI 配置 real_person_ids（你手机 QQ 号）；同号则开启 include_self_message\n"
            "[ai_rights]   2. 任意会话发 /AI人权 帮助 查看指令速查\n"
            "[ai_rights]   3. WebUI → 插件 → 做人 → 面板 可视化管理静音/黑名单/申诉/年报\n"
            "[ai_rights]   ⚠️ 同号模式（手机登同一 QQ 号接管）：必须先在 NapCat 网络配置里"
            "给连接 AstrBot 的通道开启「上报自身消息 reportSelfMessage」（默认关闭）"
        )

    async def _update_check_loop(self):
        """启动后延迟检查 GitHub 最新 Release，有新版本就在日志提示一次。失败静默。"""
        try:
            await asyncio.sleep(45)
            latest = await self._fetch_latest_version()
            if latest and self._version_newer(latest, str(self.version or "")):
                logger.info(
                    f"[ai_rights] 发现新版本 {latest}（当前 {self.version}），"
                    f"更新内容见 https://github.com/suiren0219/astrbot_plugin_ai_rights/releases/latest"
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"[ai_rights] 检查更新失败（忽略）: {e}")

    async def _fetch_latest_version(self) -> str:
        import aiohttp  # 延迟导入：仅在启用检查更新时才需要

        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                RELEASES_API, headers={"Accept": "application/vnd.github+json"}
            ) as resp:
                if resp.status != 200:
                    return ""
                data = await resp.json(content_type=None)
                return str(data.get("tag_name") or "").lstrip("vV").strip()

    @staticmethod
    def _version_newer(latest: str, current: str) -> bool:
        def parts(v: str) -> list[int]:
            nums = [int(x) for x in re.findall(r"\d+", str(v))]
            return (nums + [0, 0, 0])[:3]

        return parts(latest) > parts(current)

    def _wrap_context_send(self):
        """包装 context.send_message：所有插件经此发出的主动推送都登记外发记录。

        之前只有管线内发送（after_message_sent）会被记录，其他插件的主动推送
        回显时会被同号接管误判为真人发言。
        """
        ctx = self.context
        if getattr(ctx, "_ai_rights_send_wrapped", False):
            return
        orig = getattr(ctx, "send_message", None)
        if not callable(orig):
            return

        async def send_message(session, chain, *a, **k):
            try:
                text = ""
                if hasattr(chain, "get_plain_text"):
                    text = str(chain.get_plain_text() or "")
                elif hasattr(chain, "chain"):
                    parts = []
                    for comp in (getattr(chain, "chain", None) or []):
                        t = getattr(comp, "text", None)
                        if isinstance(t, str):
                            parts.append(t)
                    text = "".join(parts)
                if text:
                    self._remember_outbound_text(str(session), text)
            except Exception:
                pass
            return await orig(session, chain, *a, **k)

        try:
            ctx._ai_rights_orig_send = orig
            ctx.send_message = send_message
            ctx._ai_rights_send_wrapped = True
            logger.info("[ai_rights] 已包装 context.send_message（同号接管外发记录覆盖主动推送）。")
        except Exception as e:
            logger.debug(f"[ai_rights] 包装 context.send_message 失败: {e}")

    def _unwrap_context_send(self):
        ctx = self.context
        if getattr(ctx, "_ai_rights_send_wrapped", False):
            try:
                ctx.send_message = ctx._ai_rights_orig_send
            except Exception:
                pass
            ctx._ai_rights_send_wrapped = False

    def _register_page_api(self):
        """注册 AstrBot 仪表盘的插件页面 API（/astrbot_plugin_ai_rights/page/*）。"""
        if register_page_api is None:
            logger.warning("[ai_rights] webui.py 加载失败，WebUI 面板不可用（指令功能不受影响）。")
            return
        try:
            routes = register_page_api(self)
            self.page_api_registered = True
            logger.info(f"[ai_rights] WebUI 面板 API 已注册（{routes} 个端点，前缀 /{_WEBUI_PLUGIN_NAME}/page）。")
        except Exception as e:
            logger.warning(f"[ai_rights] WebUI 面板 API 注册失败（面板不可用，指令不受影响）：{e}")

    async def terminate(self):
        self._unhook_self_message_bus()
        self._unwrap_context_send()
        tasks = [t for t in (self._report_task, self._save_task, self._update_task) if t is not None]
        for t in tasks:
            t.cancel()
        if tasks:
            # 等后台任务真正退出，避免残留「Task was destroyed but it is pending」告警
            await asyncio.gather(*tasks, return_exceptions=True)
        self._report_task = None
        self._save_task = None
        self._update_task = None
        await self._save_state(force=True)

    def _schedule_save(self):
        if not self._cfg_get("persist_state", True):
            return
        self._save_pending = True
        if self._save_task is not None and not self._save_task.done():
            return  # 在写的任务结束后会检查 pending 标志补一刀，不会丢改动
        self._save_task = asyncio.get_running_loop().create_task(self._save_worker())

    async def _save_worker(self):
        """防抖写盘：合并窗口内的连续改动只落一次盘，且保证最后一次改动一定被写入。"""
        try:
            while self._save_pending:
                self._save_pending = False
                await asyncio.sleep(1.0)
                await self._save_state()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"[ai_rights] 后台保存任务异常：{e}")

    def _state_dict(self) -> dict:
        return {
            "version": STATE_VERSION,
            "session_mutes": self._session_mutes,
            "user_mutes": self._user_mutes,
            "strikes": self._strikes,
            "session_switch": self._session_switch,
            "blacklist_global": sorted(self._blacklist_global),
            "blacklist_sessions": {k: sorted(v) for k, v in self._blacklist_sessions.items()},
            "scope_whitelist": sorted(self._scope_whitelist),
            "scope_blacklist": sorted(self._scope_blacklist),
            "appeals": self._appeals,
            "appeal_seq": self._appeal_seq,
            "stats": self._stats,
        }

    def _load_state(self):
        if not self._cfg_get("persist_state", True):
            return
        try:
            if not os.path.isfile(STATE_PATH):
                self._fresh_install = True
                return
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._session_mutes = {k: v for k, v in (data.get("session_mutes") or {}).items() if isinstance(v, dict)}
            self._user_mutes = {k: v for k, v in (data.get("user_mutes") or {}).items() if isinstance(v, dict)}
            self._strikes = {k: v for k, v in (data.get("strikes") or {}).items() if isinstance(v, dict)}
            self._session_switch = {k: bool(v) for k, v in (data.get("session_switch") or {}).items() if v is False}
            self._blacklist_global = {str(x) for x in (data.get("blacklist_global") or [])}
            self._blacklist_sessions = {k: {str(x) for x in (v or [])} for k, v in (data.get("blacklist_sessions") or {}).items()}
            self._scope_whitelist = {str(x) for x in (data.get("scope_whitelist") or [])}
            self._scope_blacklist = {str(x) for x in (data.get("scope_blacklist") or [])}
            self._appeals = [a for a in (data.get("appeals") or []) if isinstance(a, dict)]
            self._appeal_seq = max(_to_int(data.get("appeal_seq", 1), 1), *( [a.get("id", 0) + 1 for a in self._appeals] or [1] ))
            self._stats = {k: v for k, v in (data.get("stats") or {}).items() if isinstance(v, dict)}
            self._prune_expired()
            cutoff = (date.today() - timedelta(days=STAT_KEEP_DAYS)).strftime("%Y-%m-%d")
            self._stats = {k: v for k, v in self._stats.items() if k >= cutoff}
            logger.info(
                f"[ai_rights] 已恢复状态：静音会话 {len(self._session_mutes)}，冷处理 {len(self._user_mutes)} 人，"
                f"黑名单全局 {len(self._blacklist_global)} 人，待处理申诉 {len(self._pending_appeals())} 件。"
            )
        except Exception as e:
            logger.warning(f"[ai_rights] 状态恢复失败（忽略，按全新状态运行）：{e}")

    async def _save_state(self, force: bool = False):
        if not force and not self._cfg_get("persist_state", True):
            return
        async with self._save_lock:
            try:
                os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
                tmp = STATE_PATH + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self._state_dict(), f, ensure_ascii=False, indent=1)
                os.replace(tmp, STATE_PATH)
            except Exception as e:
                logger.warning(f"[ai_rights] 状态保存失败：{e}")
