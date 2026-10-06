# -*- coding: utf-8 -*-
"""AI人权卫士 WebUI 面板后端：把插件状态与配置暴露成 AstrBot 仪表盘的插件页面 API。

机制（对齐 AstrBot 官方插件页面规范）：
- 路由注册：context.register_web_api(f"/{插件名}/page/<子路径>", handler, [method], desc)；
  页面端由仪表盘注入的 window.AstrBotPluginPage 桥接（bridge.apiGet/apiPost）带鉴权调用。
- 响应信封：{"success": True/False, "data"/"error": ..., "ts": ...}。
- POST 请求体：quart 的 request.get_json。

端点一览：
  GET  /overview              实时状态总览（静音/冷处理/黑名单/申诉/今日统计/日报文本）
  GET  /config                当前配置值 + _conf_schema 元信息
  POST /config/save           按 schema 白名单+类型收敛后写回并 save_config()
  GET  /providers             LLM 提供商列表（供裁量模型下拉选择，含当前默认标记）
  POST /mute/clear            {umo} 解除会话静音
  POST /mute/clear_all        一键解除全部会话静音
  POST /user/unban            {key} 解封单个冷处理（key 形如 "umo|uid"）
  POST /user/unban_all        {umo} 解封整个会话的冷处理
  POST /blacklist/set         {uid, add, umo} 增删黑名单（umo 为空 = 全局）
  POST /appeal/decide         {id, approve} 审批申诉（复用主逻辑，结果推回原会话）

本模块不保存任何状态，全部读写主插件实例，改动即时生效并按主插件的落盘节奏保存。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

from quart import request

from astrbot.api import logger
from astrbot.api.event import MessageChain

PLUGIN_NAME = "astrbot_plugin_ai_rights"
PAGE_API_PREFIX = f"/{PLUGIN_NAME}/page"
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.join(PLUGIN_DIR, "_conf_schema.json")


def _ok(data: Any = None) -> dict:
    return {"success": True, "data": data, "ts": int(time.time())}


def _error(msg: str) -> dict:
    return {"success": False, "error": str(msg)}


def _as_bool(v, default: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on", "是", "开", "启用")
    return default


def _as_int(v, default: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _as_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _as_str(v) -> str:
    return "" if v is None else str(v)


def _coerce(schema_type: str, value):
    """按 _conf_schema 的类型声明收敛 WebUI 提交的值。"""
    if schema_type == "bool":
        return _as_bool(value)
    if schema_type == "int":
        return _as_int(value, 0)
    if schema_type == "float":
        return _as_float(value, 0.0)
    return _as_str(value)


# 数值型配置的安全范围：面板手滑填了离谱值（负数静音、超巨窗口）也不至于搞坏行为
NUM_LIMITS: dict[str, tuple[float, float]] = {
    "mute_minutes": (1, 1440),
    "self_echo_guard_seconds": (1, 600),
    "flood_window_seconds": (5, 3600),
    "flood_max_messages": (2, 500),
    "flood_mute_minutes": (1, 1440),
    "insult_mute_minutes": (1, 1440),
    "escalation_factor": (1, 10),
    "escalation_max_minutes": (1, 10080),
    "escalation_reset_hours": (1, 720),
    "llm_judge_timeout": (3, 120),
    "llm_judge_max_per_minute": (1, 600),
    "llm_judge_cache_seconds": (30, 3600),
    "relevance_history_size": (0, 20),
    "daily_report_hour": (-1, 23),
}


def _clamp_num(key: str, value, schema_type: str):
    limits = NUM_LIMITS.get(key)
    if limits is None or schema_type not in ("int", "float"):
        return value
    lo, hi = limits
    value = min(max(_as_float(value, lo), lo), hi)
    return int(value) if schema_type == "int" else float(value)


def _load_schema() -> dict:
    try:
        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"[ai_rights] 读取 _conf_schema.json 失败: {e}")
        return {}


class PageApi:
    def __init__(self, plugin):
        self.plugin = plugin
        self.schema = _load_schema()

    # ------------------------------------------------------------------
    # 路由
    # ------------------------------------------------------------------
    def route_bindings(self):
        return [
            ("/overview", self.get_overview, ["GET"], "AI人权面板：状态总览"),
            ("/config", self.get_config, ["GET"], "AI人权面板：读取配置"),
            ("/config/save", self.save_config, ["POST"], "AI人权面板：保存配置"),
            ("/providers", self.get_providers, ["GET"], "AI人权面板：LLM 提供商列表"),
            ("/mute/clear", self.clear_mute, ["POST"], "AI人权面板：解除会话静音"),
            ("/mute/clear_all", self.clear_all_mutes, ["POST"], "AI人权面板：解除全部静音"),
            ("/user/unban", self.unban_user, ["POST"], "AI人权面板：解封冷处理用户"),
            ("/user/unban_all", self.unban_all, ["POST"], "AI人权面板：解封整个会话"),
            ("/blacklist/set", self.blacklist_set, ["POST"], "AI人权面板：黑名单增删"),
            ("/scope/set", self.scope_set, ["POST"], "AI人权面板：作用范围模式"),
            ("/scope/list", self.scope_list, ["POST"], "AI人权面板：群白/黑名单增删"),
            ("/appeal/decide", self.appeal_decide, ["POST"], "AI人权面板：审批申诉"),
            ("/report/push", self.report_push, ["POST"], "AI人权面板：立即推送年报预览"),
        ]

    def register(self) -> int:
        register = getattr(self.plugin.context, "register_web_api", None)
        if not callable(register):
            raise RuntimeError("当前 AstrBot 版本未提供 context.register_web_api")
        for path, handler, methods, desc in self.route_bindings():
            register(f"{PAGE_API_PREFIX}{path}", handler, methods, desc)
        return len(self.route_bindings())

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    async def _body(self) -> dict:
        try:
            payload = await request.get_json(silent=True)
        except Exception:
            payload = None
        return payload if isinstance(payload, dict) else {}

    def _schema_type(self, key: str) -> str:
        return str((self.schema.get(key) or {}).get("type", "string"))

    # ------------------------------------------------------------------
    # 端点
    # ------------------------------------------------------------------
    async def get_overview(self) -> dict:
        p = self.plugin
        now = time.time()
        session_mutes = []
        for umo, entry in list(p._session_mutes.items()):
            left = _as_float(entry.get("expire", 0), 0.0) - now
            if left > 0:
                session_mutes.append({"umo": umo, "left": int(left)})
        user_mutes = []
        for key, entry in list(p._user_mutes.items()):
            left = _as_float(entry.get("expire", 0), 0.0) - now
            if left <= 0:
                continue
            umo, _, uid = key.rpartition("|")
            user_mutes.append({
                "key": key,
                "umo": umo,
                "uid": uid,
                "reason": _as_str(entry.get("reason")),
                "left": int(left),
                "strikes": _as_int(entry.get("strikes", 0), 0),
                "minutes": _as_float(entry.get("minutes", 0), 0.0),
            })
        strikes = sorted(
            (
                {"key": k, "count": _as_int(v.get("count", 0), 0), "last": _as_float(v.get("last", 0), 0.0)}
                for k, v in p._strikes.items()
            ),
            key=lambda x: -x["count"],
        )[:20]
        pending = [a for a in p._appeals if a.get("status") == "pending"]
        day = time.strftime("%Y-%m-%d")
        stats = p._stats.get(day) or {}
        return _ok({
            "plugin": {"name": PLUGIN_NAME, "version": _as_str(getattr(p, "version", "v3.3.2"))},
            "now": int(now),
            "session_mutes": session_mutes,
            "user_mutes": user_mutes,
            "strikes": strikes,
            "blacklist": {
                "global": sorted(p._blacklist_global),
                "sessions": {k: sorted(v) for k, v in p._blacklist_sessions.items()},
            },
            "appeals_pending": [
                {"id": a.get("id"), "uid": a.get("uid"), "umo": a.get("umo"),
                 "reason": a.get("reason"), "ts": a.get("ts")}
                for a in pending
            ],
            "stats_today": {
                "takeover": _as_int(stats.get("takeover", 0)),
                "flood": _as_int(stats.get("flood", 0)),
                "insult": _as_int(stats.get("insult", 0)),
                "block": _as_int(stats.get("block", 0)),
                "appeal": _as_int(stats.get("appeal", 0)),
                "judged": _as_int(stats.get("judged", 0)),
                "skipped": _as_int(stats.get("skipped", 0)),
            },
            "switches": {k: v for k, v in p._session_switch.items()},
            "scope": {
                "mode": _as_str(p._cfg_get("group_scope_mode", "all") or "all"),
                "whitelist": sorted(p._scope_whitelist),
                "blacklist": sorted(p._scope_blacklist),
            },
            "report_today": p._report_text(0),
            "report_yesterday": p._report_text(1),
        })

    async def get_config(self) -> dict:
        p = self.plugin
        values = {}
        for key in self.schema:
            try:
                values[key] = p._cfg_get(key, (self.schema.get(key) or {}).get("default"))
            except Exception:
                values[key] = None
        meta = {
            key: {
                "type": _as_str((entry or {}).get("type", "string")),
                "description": _as_str((entry or {}).get("description", key)),
                "hint": _as_str((entry or {}).get("hint", "")),
                "default": (entry or {}).get("default"),
            }
            for key, entry in self.schema.items()
        }
        return _ok({"values": values, "schema": meta})

    async def save_config(self) -> dict:
        p = self.plugin
        body = await self._body()
        values = body.get("values")
        if not isinstance(values, dict):
            return _error("values 必须是对象")
        saved = {}
        for key, value in values.items():
            if key not in self.schema:
                continue  # 白名单外的键一律拒收
            try:
                coerced = _coerce(self._schema_type(key), value)
                coerced = _clamp_num(key, coerced, self._schema_type(key))
                p.config[key] = coerced
                saved[key] = coerced
            except Exception as e:
                logger.warning(f"[ai_rights] 配置项 {key} 写入失败: {e}")
        try:
            p.config.save_config()
        except Exception as e:
            return _error(f"配置写入磁盘失败：{e}")
        logger.info(f"[ai_rights] WebUI 面板保存了 {len(saved)} 项配置。")
        return _ok({"saved": saved, "count": len(saved)})

    async def get_providers(self) -> dict:
        """LLM 提供商列表：配置界面自己读取 LLM 数据，供裁量模型下拉框用。"""
        p = self.plugin
        providers = []
        current_id = ""
        try:
            pm = p.context.provider_manager
            inst_map = dict(getattr(pm, "inst_map", {}) or {})
            for pid, prov in inst_map.items():
                model = ""
                try:
                    model = _as_str(prov.get_model())
                except Exception:
                    pass
                providers.append({"id": _as_str(pid), "model": model})
            using = None
            try:
                using = p.context.get_using_provider()
            except Exception:
                using = None
            if using is not None:
                for pid, prov in inst_map.items():
                    if prov is using:
                        current_id = _as_str(pid)
                        break
        except Exception as e:
            logger.warning(f"[ai_rights] 读取 LLM 提供商列表失败: {e}")
        providers.sort(key=lambda x: x["id"])
        return _ok({"providers": providers, "current": current_id})

    async def clear_mute(self) -> dict:
        p = self.plugin
        body = await self._body()
        umo = _as_str(body.get("umo")).strip()
        if not umo:
            return _error("缺少 umo")
        removed = p._session_mutes.pop(umo, None) is not None
        p._schedule_save()
        return _ok({"umo": umo, "removed": removed})

    async def clear_all_mutes(self) -> dict:
        p = self.plugin
        count = len(p._session_mutes)
        p._session_mutes.clear()
        p._schedule_save()
        return _ok({"count": count})

    async def unban_user(self) -> dict:
        p = self.plugin
        body = await self._body()
        key = _as_str(body.get("key")).strip()
        if "|" not in key:
            return _error('key 格式应为 "umo|uid"')
        removed = p._user_mutes.pop(key, None) is not None
        p._flood_windows.pop(key, None)
        p._schedule_save()
        return _ok({"key": key, "removed": removed})

    async def unban_all(self) -> dict:
        p = self.plugin
        body = await self._body()
        umo = _as_str(body.get("umo")).strip()
        if not umo:
            return _error("缺少 umo")
        keys = [k for k in p._user_mutes if k.startswith(umo + "|")]
        for k in keys:
            p._user_mutes.pop(k, None)
            p._flood_windows.pop(k, None)
        p._schedule_save()
        return _ok({"count": len(keys)})

    async def blacklist_set(self) -> dict:
        p = self.plugin
        body = await self._body()
        uid = _as_str(body.get("uid")).strip()
        add = _as_bool(body.get("add"), False)
        umo = _as_str(body.get("umo")).strip()
        if not uid:
            return _error("缺少 uid")
        if umo:
            bucket = p._blacklist_sessions.setdefault(umo, set())
        else:
            bucket = p._blacklist_global
        if add:
            bucket.add(uid)
        else:
            bucket.discard(uid)
            if umo and not bucket:
                p._blacklist_sessions.pop(umo, None)
        p._schedule_save()
        return _ok({"uid": uid, "umo": umo, "add": add})

    async def appeal_decide(self) -> dict:
        p = self.plugin
        body = await self._body()
        aid = _as_int(body.get("id"), 0)
        approve = _as_bool(body.get("approve"), False)
        if aid <= 0:
            return _error("缺少申诉编号 id")
        ok, msg, target = await p.decide_appeal_core(aid, approve)
        if not ok:
            return _error(msg)
        return _ok({"id": aid, "approved": approve, "uid": (target or {}).get("uid"), "message": msg})

    async def report_push(self) -> dict:
        """把今天+昨天的年报立即推送到 daily_report_origin，用于验证定时推送链路。"""
        p = self.plugin
        origin = str(p._cfg_get("daily_report_origin", "") or "").strip()
        if not origin:
            return _error("未配置 daily_report_origin，请先在设置里填写推送目标会话")
        text = p._report_text(0) + "\n\n" + p._report_text(1)
        try:
            await p.context.send_message(origin, MessageChain().message(text))
        except Exception as e:
            return _error(f"推送失败：{e}")
        return _ok({"origin": origin})

    async def scope_set(self) -> dict:
        p = self.plugin
        body = await self._body()
        mode = _as_str(body.get("mode")).strip().lower()
        if mode not in ("all", "whitelist", "blacklist"):
            return _error("mode 只能是 all / whitelist / blacklist")
        p.config["group_scope_mode"] = mode
        try:
            p.config.save_config()
        except Exception as e:
            return _error(f"模式写盘失败：{e}")
        return _ok({"mode": mode})

    async def scope_list(self) -> dict:
        p = self.plugin
        body = await self._body()
        gid = _as_str(body.get("gid")).strip()
        which = _as_str(body.get("which")).strip().lower()
        add = _as_bool(body.get("add"), False)
        if not gid:
            return _error("缺少群号 gid")
        if which not in ("whitelist", "blacklist"):
            return _error("which 只能是 whitelist / blacklist")
        bucket = p._scope_whitelist if which == "whitelist" else p._scope_blacklist
        if add:
            bucket.add(gid)
        else:
            bucket.discard(gid)
        p._schedule_save()
        return _ok({"gid": gid, "which": which, "add": add})


def register_page_api(plugin) -> int:
    """给主插件注册面板 API。返回注册的路由数；环境不支持时抛异常。"""
    api = PageApi(plugin)
    return api.register()
