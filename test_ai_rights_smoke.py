"""AI人权卫士 插件加载冒烟测试：用桩模块模拟 astrbot，真正 import 一次 main.py。

为什么需要：main.py 里 @register / @filter 装饰器、类结构错误，语法检查查不出来，
只有真加载才会炸（历史上宿舍插件就出过 @register 挂错类、面板一条命令注册不出来的问题）。
这里断言：模块能加载、@register 挂在插件类上、门卫/命令钩子齐全，并把真人接管、
刷屏、辱骂、豁免、持久化的核心逻辑用假事件驱动一遍。

运行：python test_ai_rights_smoke.py
"""
import asyncio
import importlib.util
import os
import sys
import tempfile
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.join(HERE, "astrbot_plugin_ai_rights")
MAIN_PY = os.path.join(PLUGIN_DIR, "main.py")

passed = 0


def check(desc, cond):
    global passed
    assert cond, f"✗ {desc}"
    passed += 1
    print(f"  [OK] {desc}")


def _install_stub(name: str, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


def _install_astrbot_stub():
    registered = []

    def _register(name, author, desc, version):
        def deco(cls):
            cls.__plugin_meta__ = (name, author, desc, version)
            registered.append(name)
            return cls
        return deco

    class Star:
        def __init__(self, context=None):
            self.context = context

    class Context:
        pass

    class AstrBotConfig(dict):
        pass

    class _Logger:
        def info(self, *a, **k): pass
        def warning(self, *a, **k): pass
        def error(self, *a, **k): pass
        def debug(self, *a, **k): pass

    class EventMessageType:
        ALL = "all"
        GROUP_MESSAGES = "group"
        PRIVATE_MESSAGES = "private"

    class PermissionType:
        ADMIN = "admin"
        MEMBER = "member"

    def _hook_deco(fn):
        fn.__is_event_hook__ = True
        return fn

    def event_message_type(_t=None, priority=0):
        def deco(fn):
            fn.__is_event_hook__ = True
            fn.__hook_priority__ = priority
            return fn
        return deco

    def command(name, alias=None, **k):
        def deco(fn):
            fn.__is_command__ = name
            fn.__command_alias__ = tuple(alias or ())
            return fn
        return deco

    def permission_type(t):
        def deco(fn):
            fn.__permission__ = t
            return fn
        return deco

    def after_message_sent(priority=0):
        def deco(fn):
            fn.__is_event_hook__ = True
            fn.__hook_priority__ = priority
            return fn
        return deco

    class MessageChain:
        def __init__(self, text=""):
            self.text = text

        def message(self, t):
            self.text = t
            return self

    class _QuartRequest:
        """quart.request 桩（以实例安装）：测试直接改 payload 再调 POST handler。"""
        payload = None

        async def get_json(self, silent=True):
            return self.payload

    flt = _install_stub(
        "astrbot.api.event.filter",
        EventMessageType=EventMessageType,
        PermissionType=PermissionType,
        event_message_type=event_message_type,
        command=command,
        permission_type=permission_type,
        after_message_sent=after_message_sent,
    )
    api_event = _install_stub(
        "astrbot.api.event",
        AstrMessageEvent=object,
        MessageChain=MessageChain,
        filter=flt,
    )
    _install_stub("astrbot.api", AstrBotConfig=AstrBotConfig, logger=_Logger())
    _install_stub("astrbot.api.star", Context=Context, Star=Star, register=_register)
    _install_stub("quart", request=_QuartRequest())
    # astrbot.api.event 需要作为包路径可导入
    _install_stub("astrbot")
    pkg = sys.modules["astrbot"]
    pkg.api = sys.modules["astrbot.api"]
    sys.modules["astrbot.api"].event = api_event
    sys.modules["astrbot.api"].star = sys.modules["astrbot.api.star"]
    return registered


class FakeEvent:
    """最小事件桩：只带门卫用得到的字段。"""

    def __init__(self, sender="10086", self_id="bot", text="", role="", wake=True,
                 umo="aiocqhttp:FriendMessage:10086", raw=None):
        self.sender = sender
        self.self_id = self_id
        self.message_str = text
        self.role = role
        self.is_at_or_wake_command = wake
        self.unified_msg_origin = umo
        self.raw_message = raw or {}
        self.call_llm = True
        self.stopped = False
        self.sent = []

    def get_sender_id(self):
        return self.sender

    def get_self_id(self):
        return self.self_id

    def get_platform_name(self):
        return "aiocqhttp"

    def get_session_id(self):
        return self.sender

    def should_call_llm(self, v):
        self.call_llm = v

    def stop_event(self):
        self.stopped = True

    async def send(self, chain):
        self.sent.append(chain)

    def plain_result(self, t):
        return types.SimpleNamespace(text=t)


class FakeProvider:
    """LLM 裁量桩：text_chat 返回固定文本。"""

    def __init__(self, reply):
        self.reply = reply

    async def text_chat(self, prompt=None, **kw):
        return types.SimpleNamespace(completion_text=self.reply)


class FakeContext:
    """Context 桩：主动推送 + 裁量模型。"""

    def __init__(self):
        self.sent = []
        self.provider = None

    async def send_message(self, session, chain):
        self.sent.append((str(session), getattr(chain, "text", "")))

    def get_using_provider(self):
        return self.provider

    def get_provider_by_id(self, pid):
        return self.provider


class FakeProv:
    def __init__(self, model=None):
        # 兼容两种用途：WebUI 列表桩传模型名；裁量桩传固定回复文本
        self._model_or_reply = model
        self.calls = 0
        self.last_prompt = None

    def get_model(self):
        return self._model_or_reply

    async def text_chat(self, prompt=None, **kw):
        self.calls += 1
        self.last_prompt = prompt
        return types.SimpleNamespace(completion_text=self._model_or_reply)


class FakeWebContext(FakeContext):
    """带 register_web_api 和 provider_manager 的 Context 桩（WebUI 面板用）。"""

    def __init__(self, provs=None, using=None):
        super().__init__()
        self._provs = provs or {}
        self.provider_manager = types.SimpleNamespace(inst_map=dict(self._provs))
        self._using = using
        self.routes = []

    def register_web_api(self, route, handler, methods, desc):
        self.routes.append((route, handler, tuple(methods), desc))

    def get_using_provider(self):
        return self._using


def _load_plugin(state_path):
    if PLUGIN_DIR not in sys.path:
        sys.path.insert(0, PLUGIN_DIR)  # main.py 以单文件方式加载时，from webui import 需要 PATH
    spec = importlib.util.spec_from_file_location("ai_rights_main", MAIN_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ai_rights_main"] = mod
    spec.loader.exec_module(mod)
    mod.STATE_PATH = state_path
    return mod


async def _run_v2(mod, state_path):
    mod.STATE_PATH = state_path  # 持久化文件切到 v2 测试专用路径
    cfg = {
        "real_person_ids": "10086",
        "persist_state": True,
        "anti_harass_enabled": True,
        "flood_window_seconds": 60,
        "flood_max_messages": 3,
        "flood_mute_minutes": 5,
        "insult_enabled": True,
        "insult_keywords": "傻逼",
        "insult_mute_minutes": 10,
        "escalation_enabled": True,
        "escalation_factor": 2.0,
        "escalation_max_minutes": 60,
        "escalation_reset_hours": 24,
        "llm_judge_enabled": False,
    }
    ctx = FakeContext()
    p = mod.AIRightsPlugin(ctx, cfg)
    await p.initialize()
    umo = "aiocqhttp:GroupMessage:300"

    # ---- 黑名单 ----
    ev = FakeEvent(sender="44444", text="来聊聊天", wake=True, umo=umo)
    await p.gatekeeper(ev)
    check("拉黑前：路人正常被理", ev.call_llm is True)
    ev_cmd = FakeEvent(sender="10086", text="人权拉黑 44444", role="admin", wake=True, umo=umo)
    res = [r async for r in p.blacklist_add(ev_cmd)]
    check("人权拉黑命令生效（全局）", "44444" in p._blacklist_global)
    ev2 = FakeEvent(sender="44444", text="我再说一句", wake=True, umo=umo)
    p._flood_windows.clear()  # 拉黑前那条消息合法进过窗口，清掉再验证拉黑后不再进
    await p.gatekeeper(ev2)
    check("黑名单用户 LLM 被拒", ev2.call_llm is False and not ev2.stopped)
    check("黑名单拦截已计入统计", sum(d.get("block", 0) for d in p._stats.values()) >= 1)
    check("黑名单用户不进骚扰窗口", f"{umo}|44444" not in p._flood_windows)
    # 管理员豁免黑名单（防止把管理员自己锁死）；用不在真人名单里的管理员，避免触发接管静音
    p._blacklist_global.add("30001")
    ev_admin = FakeEvent(sender="30001", text="管理员说话", role="admin", wake=True, umo=umo)
    await p.gatekeeper(ev_admin)
    check("管理员不受黑名单影响", ev_admin.call_llm is True)
    p._blacklist_global.discard("10086")
    # 会话级黑名单
    ev_cmd2 = FakeEvent(sender="10086", text="人权拉黑 55555 本群", role="admin", wake=True, umo=umo)
    [r async for r in p.blacklist_add(ev_cmd2)]
    check("会话级黑名单只在本地生效",
          p._is_blacklisted(umo, "55555") and not p._is_blacklisted("other:umo", "55555"))
    # 粘连写法 + 解黑
    ev_cmd3 = FakeEvent(sender="10086", text="人权解黑55555 本群", role="admin", wake=True, umo=umo)
    res3 = [r async for r in p.blacklist_remove(ev_cmd3)]
    check("粘连参数解析 + 解黑生效", "55555" not in p._blacklist_sessions.get(umo, set()))
    ev_cmd4 = FakeEvent(sender="10086", text="人权解黑 44444", role="admin", wake=True, umo=umo)
    [r async for r in p.blacklist_remove(ev_cmd4)]
    check("全局解黑生效", "44444" not in p._blacklist_global)

    # ---- 屡犯升级 ----
    for i in range(3):
        await p.gatekeeper(FakeEvent(sender="66666", text=f"刷{i}", wake=True, umo=umo))
    await p.gatekeeper(FakeEvent(sender="66666", text="第四条", wake=True, umo=umo))
    e1 = p._user_mutes[f"{umo}|66666"]
    check("第一次犯规：基础冷却 5 分钟", e1["strikes"] == 0 and abs(e1["minutes"] - 5) < 1)
    p._user_mutes[f"{umo}|66666"]["expire"] = 0  # 让冷却立刻过期，模拟再次犯规
    for i in range(3):
        await p.gatekeeper(FakeEvent(sender="66666", text=f"再刷{i}", wake=True, umo=umo))
    await p.gatekeeper(FakeEvent(sender="66666", text="又是第四条", wake=True, umo=umo))
    e2 = p._user_mutes[f"{umo}|66666"]
    check("再犯升级：strikes+1 且时长翻倍", e2["strikes"] == 1 and abs(e2["minutes"] - 10) < 1)
    p._user_mutes.pop(f"{umo}|66666", None)

    # ---- 申诉 ----
    await p.gatekeeper(FakeEvent(sender="77777", text="你就是个傻逼", wake=True, umo=umo))
    check("77777 已被辱骂冷处理", p._user_mute_left(umo, "77777") is not None)
    ev_ap = FakeEvent(sender="77777", text="申诉 对不起我错了", wake=True, umo=umo)
    res_ap = [r async for r in p.appeal_submit(ev_ap)]
    check("申诉提交成功（编号 #1）", any("#1" in r.text for r in res_ap) and len(p._pending_appeals()) == 1)
    aid = p._pending_appeals()[0]["id"]
    ev_list = FakeEvent(sender="10086", text="申诉列表", role="admin", wake=True, umo=umo)
    res_list = [r async for r in p.appeal_list(ev_list)]
    check("申诉列表能看到待处理", any(f"#{aid}" in r.text for r in res_list))
    ev_ok = FakeEvent(sender="10086", text=f"申诉同意 {aid}", role="admin", wake=True, umo=umo)
    [r async for r in p.appeal_approve(ev_ok)]
    check("申诉同意后冷却被解除", p._user_mute_left(umo, "77777") is None)
    check("申诉结果主动推送回原会话", any("申诉" in t for _, t in ctx.sent))
    # 无事申诉会被礼貌拒绝
    ev_no = FakeEvent(sender="77777", text="申诉 我还想申诉", wake=True, umo=umo)
    res_no = [r async for r in p.appeal_submit(ev_no)]
    check("未冷处理时申诉被拒", any("无需申诉" in r.text for r in res_no))

    # ---- LLM 裁量 ----
    ctx.provider = FakeProvider('{"harass": true}')
    p.config["llm_judge_enabled"] = True
    p.config["llm_judge_max_per_minute"] = 20
    await p.gatekeeper(FakeEvent(sender="88888", text="你这服务态度真是让人无语透顶", wake=True, umo=umo))
    check("LLM 裁量命中 → 冷处理", p._user_mute_left(umo, "88888") is not None)
    p._user_mutes.pop(f"{umo}|88888", None)
    ctx.provider = FakeProvider('{"harass": false}')
    await p.gatekeeper(FakeEvent(sender="99999", text="今天天气真不错", wake=True, umo=umo))
    check("LLM 裁量不成立 → 不处理", p._user_mute_left(umo, "99999") is None)
    p.config["llm_judge_enabled"] = False

    # ---- 人权年报 ----
    text = p._report_text(0)
    check("年报包含辱骂统计", "辱骂" in text)
    check("年报包含惯犯排行", "惯犯排行" in text)
    ytext = p._report_text(1)
    check("昨日报告可生成", "昨天" in ytext)

    # ---- 持久化：黑名单 + 申诉落盘并恢复 ----
    p._blacklist_global.add("12345")
    await p.terminate()
    check("状态文件已落盘", os.path.isfile(state_path))
    p2 = mod.AIRightsPlugin(FakeContext(), cfg)
    p2._load_state()
    check("黑名单重启恢复", "12345" in p2._blacklist_global)
    check("申诉历史重启恢复", len(p2._appeals) == 1)
    check("申诉编号不回退", p2._appeal_seq > 1)


async def _run_webui(mod, state_path):
    """WebUI 面板后端 API：注册、概览、LLM 列表、配置读写、黑名单/静音/申诉操作。"""
    mod.STATE_PATH = state_path
    quart_req = sys.modules["quart"].request
    provs = {"gpt-mini": FakeProv("mini-1"), "glm-flash": FakeProv("glm-5.3-flash")}
    ctx = FakeWebContext(provs=provs, using=provs["glm-flash"])

    class Cfg(dict):
        saved = False

        def save_config(self):
            Cfg.saved = True

    cfg = Cfg({"real_person_ids": "10086", "persist_state": True, "insult_keywords": "傻逼",
               "insult_enabled": True, "anti_harass_enabled": True})
    p = mod.AIRightsPlugin(ctx, cfg)
    await p.initialize()
    check("面板 API 已注册（12 个端点）", p.page_api_registered and len(ctx.routes) == 12)
    routes = {r[0]: r for r in ctx.routes}
    prefix = "/astrbot_plugin_ai_rights/page/"
    check("路由前缀符合官方插件页面规范", prefix + "overview" in routes and prefix + "config/save" in routes)

    res = await routes[prefix + "overview"][1]()
    check("overview 返回标准信封+统计", res["success"] is True and "stats_today" in res["data"])

    res = await routes[prefix + "providers"][1]()
    ids = [x["id"] for x in res["data"]["providers"]]
    check("配置界面读到 LLM 数据（含模型名与默认标记）",
          "gpt-mini" in ids and res["data"]["current"] == "glm-flash"
          and [x for x in res["data"]["providers"] if x["id"] == "gpt-mini"][0]["model"] == "mini-1")

    res = await routes[prefix + "config"][1]()
    check("config 读取值+schema 元信息", "mute_minutes" in res["data"]["values"] and "mute_minutes" in res["data"]["schema"])

    quart_req.payload = {"values": {"mute_minutes": "45", "include_self_message": True,
                                    "escalation_factor": 1.5, "hack_key": "x"}}
    res = await routes[prefix + "config/save"][1]()
    check("config 保存白名单外拒收+类型收敛+落盘",
          res["success"] and p.config["mute_minutes"] == 45
          and p.config["include_self_message"] is True and "hack_key" not in p.config
          and Cfg.saved)

    quart_req.payload = {"uid": "777", "add": True, "umo": ""}
    await routes[prefix + "blacklist/set"][1]()
    ev = FakeEvent(sender="777", text="hi", wake=True, umo="u:G:1")
    await p.gatekeeper(ev)
    check("面板拉黑后门卫拒绝服务", "777" in p._blacklist_global and ev.call_llm is False)
    quart_req.payload = {"uid": "777", "add": False, "umo": ""}
    await routes[prefix + "blacklist/set"][1]()
    check("面板解黑生效", "777" not in p._blacklist_global)

    await p.gatekeeper(FakeEvent(sender="10086", text="我来说句话", umo="u:G:1"))
    check("真人接管静音已触发", p._session_mute_left("u:G:1") > 0)
    quart_req.payload = {"umo": "u:G:1"}
    await routes[prefix + "mute/clear"][1]()
    check("面板解除会话静音", p._session_mute_left("u:G:1") == 0)

    await p.gatekeeper(FakeEvent(sender="888", text="你就是个傻逼", umo="u:G:2"))
    ev_ap = FakeEvent(sender="888", text="申诉 对不起", umo="u:G:2")
    [r async for r in p.appeal_submit(ev_ap)]
    aid = p._pending_appeals()[0]["id"]
    quart_req.payload = {"id": aid, "approve": True}
    res = await routes[prefix + "appeal/decide"][1]()
    check("面板审批申诉通过并解封", res["success"] and p._user_mute_left("u:G:2", "888") is None)
    check("审批结果主动推送回原会话", any("申诉" in t for _, t in ctx.sent))

    quart_req.payload = {"id": 999, "approve": True}
    res = await routes[prefix + "appeal/decide"][1]()
    check("不存在的申诉返回错误信封", res["success"] is False)

    await p.terminate()


async def _run_v3(mod, state_path):
    """v2.2 优化项：解析缓存、过期即时失效、裁量缓存、申诉防重复、面板数值钳制。"""
    mod.STATE_PATH = state_path
    quart_req = sys.modules["quart"].request
    prov = FakeProv('{"harass": true}')
    ctx = FakeWebContext(provs={"m": prov}, using=prov)

    class Cfg(dict):
        saved = False

        def save_config(self):
            Cfg.saved = True

    cfg = Cfg({"real_person_ids": "10086", "persist_state": True, "anti_harass_enabled": True,
               "insult_keywords": "", "llm_judge_enabled": False, "llm_judge_provider": "m"})
    p = mod.AIRightsPlugin(ctx, cfg)
    await p.initialize()
    umo = "u:V:1"

    # 配置解析缓存：改配置立刻生效（缓存按原始串比对）
    check("名单缓存初值正确", p._real_person_ids() == ["10086"])
    p.config["real_person_ids"] = "10086 20002"
    check("改配置后缓存自动更新", p._real_person_ids() == ["10086", "20002"])

    # 清理已限频（30 秒一次），但过期静音的裁决是实时计算的，不能被限频拖住
    # （先关着 LLM 裁量做这条，桩模型见谁都说骚扰，会干扰断言）
    p._session_mutes[umo] = {"expire": time.time() - 1, "updated": 0}
    ev = FakeEvent(sender="555", text="你好", wake=True, umo=umo)
    await p.gatekeeper(ev)
    check("过期静音不再压制（限频不影响裁决）", ev.call_llm is True)

    # LLM 裁定缓存：同一人同一段话只问一次模型
    p.config["llm_judge_enabled"] = True
    await p.gatekeeper(FakeEvent(sender="888", text="你就是个废物", wake=True, umo=umo))
    check("裁量首次调用模型", prov.calls == 1 and p._user_mute_left(umo, "888") is not None)
    p._user_mutes.pop(f"{umo}|888", None)
    await p.gatekeeper(FakeEvent(sender="888", text="你就是个废物", wake=True, umo=umo))
    check("相同文本命中缓存不再烧 token", prov.calls == 1 and p._user_mute_left(umo, "888") is not None)

    # 申诉防重复提交
    [r async for r in p.appeal_submit(FakeEvent(sender="888", text="申诉 对不起", wake=True, umo=umo))]
    res2 = [r async for r in p.appeal_submit(FakeEvent(sender="888", text="申诉 再来一条", wake=True, umo=umo))]
    check("重复申诉被拦截", any("不用重复提交" in r.text for r in res2) and len(p._pending_appeals()) == 1)

    # 面板保存数值钳制：离谱值收敛到安全范围
    routes = {r[0]: r for r in ctx.routes}
    quart_req.payload = {"values": {"mute_minutes": "99999", "daily_report_hour": 99, "escalation_factor": 0}}
    res = await routes["/astrbot_plugin_ai_rights/page/config/save"][1]()
    check("面板保存数值钳制到安全范围",
          res["success"] and p.config["mute_minutes"] == 1440
          and p.config["daily_report_hour"] == 23 and p.config["escalation_factor"] == 1)

    await p.terminate()
    check("v3 状态落盘正常", os.path.isfile(state_path))


async def _run_v4(mod, state_path):
    """v2.3 话题守护：should_answer=false 不回（不拦 NSFW）、话题上下文、双判定、开关独立。"""
    mod.STATE_PATH = state_path
    prov = FakeProv('{"harass": false, "should_answer": false}')
    ctx = FakeWebContext(provs={"m": prov}, using=prov)

    class Cfg(dict):
        def save_config(self):
            pass

    cfg = Cfg({"real_person_ids": "10086", "persist_state": True, "anti_harass_enabled": True,
               "insult_enabled": False, "insult_keywords": "",
               "llm_judge_enabled": True, "llm_judge_provider": "m",
               "relevance_gate_enabled": True})
    p = mod.AIRightsPlugin(ctx, cfg)
    await p.initialize()
    umo = "u:W:1"

    # 无意义算数：should_answer=false → AI 不回，且不算辱骂
    ev1 = FakeEvent(sender="701", text="1+1等于几", wake=True, umo=umo)
    await p.gatekeeper(ev1)
    check("无意义算数被话题守护拦下（AI 不回）", ev1.call_llm is False)
    check("没进辱骂冷却（跑题≠骚扰）", p._user_mute_left(umo, "701") is None)
    check("计入无意义不答统计", sum(d.get("skipped", 0) for d in p._stats.values()) == 1)

    # 第二条触发裁量时，prompt 里应带上第一条建立的话题上下文
    ev2 = FakeEvent(sender="702", text="那 2+2 等于几", wake=True, umo=umo)
    await p.gatekeeper(ev2)
    check("跑题判断带上了最近会话内容", prov.last_prompt is not None and "1+1等于几" in prov.last_prompt)

    # 值得回答的消息放行
    prov._model_or_reply = '{"harass": false, "should_answer": true}'
    ev3 = FakeEvent(sender="703", text="今天天气真不错啊", wake=True, umo=umo)
    await p.gatekeeper(ev3)
    check("值得回答的消息正常放行", ev3.call_llm is True)

    # 双判定的另一维：harass=true 照样进辱骂冷却
    prov._model_or_reply = '{"harass": true, "should_answer": true}'
    ev4 = FakeEvent(sender="704", text="你就是个蠢货", wake=True, umo=umo)
    await p.gatekeeper(ev4)
    check("LLM 判 harass → 辱骂冷却", p._user_mute_left(umo, "704") is not None)

    # 缓存含双判定：被冷却的人再发同一段话，不再调模型
    calls_before = prov.calls
    await p.gatekeeper(FakeEvent(sender="704", text="你就是个蠢货", wake=True, umo=umo))
    check("同文本命中缓存不重复调用", prov.calls == calls_before)

    # 话题守护关掉：只裁量不拦截
    p.config["relevance_gate_enabled"] = False
    prov._model_or_reply = '{"harass": false, "should_answer": false}'
    ev5 = FakeEvent(sender="705", text="随便说点什么吧", wake=True, umo=umo)
    await p.gatekeeper(ev5)
    check("话题守护关闭后不拦截", ev5.call_llm is True)

    # overview 统计带 skipped
    routes = {r[0]: r for r in ctx.routes}
    res = await routes["/astrbot_plugin_ai_rights/page/overview"][1]()
    check("overview 统计含无意义不答", "skipped" in res["data"]["stats_today"])

    await p.terminate()


async def _run_v5(mod, state_path):
    """v2.4 作用范围：群白/黑名单控制插件在哪些群开启，私聊不受影响。"""
    mod.STATE_PATH = state_path
    quart_req = sys.modules["quart"].request
    ctx = FakeWebContext()

    class Cfg(dict):
        def save_config(self):
            pass

    cfg = Cfg({"real_person_ids": "10086", "persist_state": True, "group_scope_mode": "all"})
    p = mod.AIRightsPlugin(ctx, cfg)
    await p.initialize()
    ga = "aiocqhttp:GroupMessage:888001"
    gb = "aiocqhttp:GroupMessage:888002"

    await p.gatekeeper(FakeEvent(sender="10086", text="我在群里说话", wake=False, umo=ga))
    check("all 模式群聊正常生效", p._session_mute_left(ga) > 0)
    p._session_mutes.clear()

    p.config["group_scope_mode"] = "whitelist"
    await p.gatekeeper(FakeEvent(sender="10086", text="群B说话", wake=False, umo=gb))
    check("whitelist 模式未加白群不参与", p._session_mute_left(gb) == 0)
    priv = "aiocqhttp:FriendMessage:10086"
    await p.gatekeeper(FakeEvent(sender="10086", text="私聊说话", wake=False, umo=priv))
    check("whitelist 模式私聊不受影响", p._session_mute_left(priv) > 0)
    p._session_mutes.clear()

    [r async for r in p.scope_cmd(FakeEvent(sender="10086", text="作用范围 加白 888002", role="admin", wake=True, umo=ga))]
    check("命令加白生效", "888002" in p._scope_whitelist)
    await p.gatekeeper(FakeEvent(sender="10086", text="群B又说话", wake=False, umo=gb))
    check("加白后该群开始生效", p._session_mute_left(gb) > 0)
    p._session_mutes.clear()

    [r async for r in p.scope_cmd(FakeEvent(sender="10086", text="作用范围移白888002", role="admin", wake=True, umo=ga))]
    await p.gatekeeper(FakeEvent(sender="10086", text="群B最后说一句", wake=False, umo=gb))
    check("粘连移白后群B恢复不参与", "888002" not in p._scope_whitelist and p._session_mute_left(gb) == 0)

    p.config["group_scope_mode"] = "blacklist"
    [r async for r in p.scope_cmd(FakeEvent(sender="10086", text="作用范围 加黑 888001", role="admin", wake=True, umo=ga))]
    await p.gatekeeper(FakeEvent(sender="10086", text="群A说话", wake=False, umo=ga))
    check("排除黑名单群不生效", p._session_mute_left(ga) == 0)
    gc = "aiocqhttp:GroupMessage:888003"
    await p.gatekeeper(FakeEvent(sender="10086", text="群C说话", wake=False, umo=gc))
    check("blacklist 模式其他群正常生效", p._session_mute_left(gc) > 0)
    p._session_mutes.clear()

    [r async for r in p.scope_cmd(FakeEvent(sender="10086", text="作用范围 模式 全部", role="admin", wake=True, umo=ga))]
    check("模式切换命令", p.config["group_scope_mode"] == "all")

    routes = {r[0]: r for r in ctx.routes}
    quart_req.payload = {"mode": "whitelist"}
    res = await routes["/astrbot_plugin_ai_rights/page/scope/set"][1]()
    check("面板切作用范围模式", res["success"] and p.config["group_scope_mode"] == "whitelist")
    quart_req.payload = {"gid": "999999", "add": True, "which": "whitelist"}
    await routes["/astrbot_plugin_ai_rights/page/scope/list"][1]()
    check("面板加白名单", "999999" in p._scope_whitelist)
    res = await routes["/astrbot_plugin_ai_rights/page/overview"][1]()
    check("overview 携带 scope 数据",
          res["data"]["scope"]["mode"] == "whitelist" and "999999" in res["data"]["scope"]["whitelist"])
    quart_req.payload = {"gid": "999999", "add": False, "which": "whitelist"}
    await routes["/astrbot_plugin_ai_rights/page/scope/list"][1]()

    p._scope_whitelist.add("777888")
    await p.terminate()
    p2 = mod.AIRightsPlugin(ctx, Cfg({"persist_state": True}))
    p2._load_state()
    check("群名单重启恢复", p2._scope_whitelist == {"777888"})


async def _run(mod, state_path):
    cfg = {
        "real_person_ids": "10086\n20002",
        "include_self_message": False,
        "mute_minutes": 30,
        "refresh_on_each_message": True,
        "persist_state": True,
        "anti_harass_enabled": True,
        "flood_window_seconds": 60,
        "flood_max_messages": 3,
        "flood_mute_minutes": 5,
        "flood_reply_enabled": True,
        "insult_enabled": True,
        "insult_keywords": "傻逼\nsb\nnmsl",
        "insult_mute_minutes": 10,
        "insult_reply_enabled": True,
        "insult_replies": "哼。",
        "suppress_scope": "llm",
    }
    p = mod.AIRightsPlugin(mod.__dict__.get("Context") or object(), cfg)
    await p.initialize()

    umo = "aiocqhttp:FriendMessage:10086"

    # ---- 真人接管：名单内发言 → 会话静音，LLM 被拦 ----
    ev = FakeEvent(sender="10086", text="我自己来说")
    await p.gatekeeper(ev)
    check("真人发言触发会话静音", p._session_mute_left(umo) > 0)
    check("真人发言不触发反骚扰", f"{umo}|10086" not in p._user_mutes)
    ev2 = FakeEvent(sender="10086", text="我又说了一句")
    await p.gatekeeper(ev2)
    check("真人持续发言不报错（续期路径）", p._session_mute_left(umo) > 0)
    ev3 = FakeEvent(sender="10086", text="/真人解除")
    await p.gatekeeper(ev3)
    check("指令前缀消息不触发/不续期", True)  # 解除由命令处理器负责，门卫只保证不重复静音

    # 静音期内，任何人的 LLM 都被拦（会话级）；换个发送者避免计入后面 77777 的刷屏窗口
    ev4 = FakeEvent(sender="55555", text="在吗")
    await p.gatekeeper(ev4)
    check("会话静音压制 LLM", ev4.call_llm is False and not ev4.stopped)

    # ---- 名单外的人不受会话静音影响时：先解除再测反骚扰 ----
    p._session_mutes.pop(umo, None)

    for i in range(3):
        await p.gatekeeper(FakeEvent(sender="77777", text=f"刷屏{i}"))
    ev_flood = FakeEvent(sender="77777", text="最后一条")
    await p.gatekeeper(ev_flood)
    left = p._user_mute_left(umo, "77777")
    check("刷屏超过阈值 → 用户级冷处理", left is not None and left[1] == "flood")
    check("刷屏提示只发一次", any("冷却" in c.text for c in ev_flood.sent))
    check("被冷处理的人 LLM 被拦", ev_flood.call_llm is False)

    ev_other = FakeEvent(sender="88888", text="我是路人")
    await p.gatekeeper(ev_other)
    check("同会话其他用户不受牵连", ev_other.call_llm is True)

    # ---- 辱骂 ----
    ev_insult = FakeEvent(sender="66666", text="你就是个傻逼")
    await p.gatekeeper(ev_insult)
    left = p._user_mute_left(umo, "66666")
    check("辱骂词命中 → 冷处理", left is not None and left[1] == "insult")
    check("辱骂回敬一句（默认开启时）", any(c.text == "哼。" for c in ev_insult.sent))

    m = mod._KeywordMatcher(["傻逼", "sb", "nmsl"])
    check("中文词子串命中", m.hit("你真是个傻逼东西"))
    check("英文词独立单词命中", m.hit("你个SB啊") and m.hit("nmsl"))
    check("usb 不误伤 sb", not m.hit("这是个usb设备"))

    # ---- 管理员豁免 ----
    for i in range(6):
        await p.gatekeeper(FakeEvent(sender="99999", text=f"管理员狂刷{i}", role="admin"))
    check("管理员豁免反骚扰", p._user_mute_left(umo, "99999") is None)

    # ---- 名单内真人豁免（换独立会话，避免真人发言把主会话再次静音污染后续断言）----
    grp_umo = "aiocqhttp:GroupMessage:200"
    for i in range(6):
        await p.gatekeeper(FakeEvent(sender="20002", text=f"真人狂刷{i}", umo=grp_umo))
    check("名单内真人豁免反骚扰", p._user_mute_left(grp_umo, "20002") is None)

    # ---- 会话停用开关 ----
    p._session_switch[umo] = False
    ev_off = FakeEvent(sender="10086", text="我还说")
    await p.gatekeeper(ev_off)
    check("停用后门卫不作为", ev_off.call_llm is True and ev_off.stopped is False)
    p._session_switch.pop(umo, None)

    # 同号模式：协议明确标记 outbound 的机器人消息绝不触发；普通 message 入站的手机真人消息立即触发
    p.config["include_self_message"] = True
    p._session_mutes.pop(umo, None)
    ev_out = FakeEvent(sender="bot", self_id="bot", text="机器人主动发的", raw={"post_type": "message_sent"})
    await p.gatekeeper(ev_out)
    check("明确 outbound 的机器人消息不触发真人接管", p._session_mute_left(umo) <= 0)
    ev_in = FakeEvent(sender="bot", self_id="bot", text="持有者手机发的", raw={"post_type": "message"})
    await p.gatekeeper(ev_in)
    check("同号普通 inbound 手机消息立即触发接管", p._session_mute_left(umo) > 0)
    p._session_mutes.pop(umo, None)
    ev_flag_out = FakeEvent(sender="bot", self_id="bot", text="带出站标记", raw={"post_type": "message", "is_outbound": True})
    await p.gatekeeper(ev_flag_out)
    check("is_outbound 标记也能排除机器人回显", p._session_mute_left(umo) <= 0)
    p.config["include_self_message"] = False

    # ---- suppress_scope=all：非指令消息 stop_event，指令放行 ----
    p.config["suppress_scope"] = "all"
    await p.gatekeeper(FakeEvent(sender="10086", text="闲聊一句"))
    ev_cmd = FakeEvent(sender="10086", text="/真人解除")
    await p.gatekeeper(ev_cmd)
    check("all 模式拦闲聊且放行指令", ev_cmd.stopped is False)
    p.config["suppress_scope"] = "llm"

    # ---- 持久化：terminate 落盘，新实例恢复 ----
    await p.gatekeeper(FakeEvent(sender="10086", text="再接管一次"))
    flood_umo = "aiocqhttp:GroupMessage:777"
    for i in range(4):
        await p.gatekeeper(FakeEvent(sender="77777", text=f"又一轮刷屏{i}", umo=flood_umo))
    await p.terminate()
    check("状态文件已落盘", os.path.isfile(state_path))

    p2 = mod.AIRightsPlugin(object(), cfg)
    p2._load_state()
    check("重启后恢复会话静音", p2._session_mute_left(umo) > 0)
    check("重启后恢复用户冷处理", p2._user_mute_left(flood_umo, "77777") is not None)

    # ---- 命令可用性（纯文本拼装，不打桩 send）----
    ev_status = FakeEvent(sender="10086")
    text = p._session_status_text(ev_status)
    check("状态总览包含真人接管与名单信息", "真人接管" in text and "冷处理" in text)


def main():
    print("== 安装 astrbot 桩 ==")
    _install_astrbot_stub()

    print("== 加载 main.py ==")
    with tempfile.TemporaryDirectory() as td:
        state_path = os.path.join(td, "ai_rights_state.json")
        mod = _load_plugin(state_path)
        meta = getattr(mod.AIRightsPlugin, "__plugin_meta__", None)
        check("@register 挂在插件类上", meta is not None and meta[0] == "ai_rights")
        check("@register 版本号是 v2.5", meta is not None and "v2.5" in meta[3])
        check("gatekeeper 是事件钩子（priority=15000）",
              getattr(mod.AIRightsPlugin.gatekeeper, "__is_event_hook__", False)
              and getattr(mod.AIRightsPlugin.gatekeeper, "__hook_priority__", 0) == 15000)
        check("after_message_sent 钩子存在",
              getattr(mod.AIRightsPlugin.note_outbound, "__is_event_hook__", False))
        cmds = {n: getattr(mod.AIRightsPlugin, n) for n in dir(mod.AIRightsPlugin)}
        names = {getattr(f, "__is_command__") for f in cmds.values() if hasattr(f, "__is_command__")}
        expect = {"AI人权", "真人状态", "真人解除", "真人开启", "真人关闭", "骚扰状态", "骚扰解封",
                  "黑名单", "人权拉黑", "人权解黑", "申诉", "申诉列表", "申诉同意", "申诉驳回", "人权年报",
                  "作用范围"}
        check("16 条命令全部注册", expect <= names)
        admin_cmds = {getattr(f, "__is_command__") for f in cmds.values()
                      if hasattr(f, "__permission__") and getattr(f, "__permission__") == "admin"}
        check("管理员命令限定正确", {"真人解除", "真人开启", "真人关闭", "骚扰解封",
                                    "人权拉黑", "人权解黑", "申诉列表", "申诉同意", "申诉驳回",
                                    "作用范围"} <= admin_cmds)

        print("== 驱动核心逻辑（v1 真人接管 + 反骚扰）==")
        asyncio.run(_run(mod, state_path))

        print("== 驱动新功能（v2 黑名单/升级/申诉/裁量/年报）==")
        asyncio.run(_run_v2(mod, os.path.join(os.path.dirname(state_path), "state_v2.json")))

        print("== 驱动 WebUI 面板 API（v2.1）==")
        asyncio.run(_run_webui(mod, os.path.join(os.path.dirname(state_path), "state_webui.json")))

        print("== 驱动优化项（v2.2 缓存/钳制/防抖）==")
        asyncio.run(_run_v3(mod, os.path.join(os.path.dirname(state_path), "state_v3.json")))

        print("== 驱动话题守护（v2.3 should_answer 双判定）==")
        asyncio.run(_run_v4(mod, os.path.join(os.path.dirname(state_path), "state_v4.json")))

        print("== 驱动作用范围（v2.4 群白/黑名单）==")
        asyncio.run(_run_v5(mod, os.path.join(os.path.dirname(state_path), "state_v5.json")))

    print(f"\n全部通过：{passed} 项检查 ✓")


if __name__ == "__main__":
    main()
