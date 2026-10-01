# -*- coding: utf-8 -*-
"""绝区零公开面板：Enka 拉取、生成 CSV，并发送到当前会话。"""
from __future__ import annotations

import os
from typing import Dict, List, Optional

from core.plugin import BasePlugin, on, Priority
from core.provider import LLMRequest
from core.utils.tool_utils import BaseTool
from core.logging_manager import get_logger
from core.chat.message_utils import KiraMessageEvent
from core.chat import MessageChain
from core.chat.message_elements import Text, File

from .zzz_data import fetch_enka, build_csv

logger = get_logger("zzz_panel", "cyan")

try:
    from data.skills.zzz_score.character_data import CHARACTER_DATA
except Exception:
    CHARACTER_DATA: Dict[str, Dict[str, Dict[str, float]]] = {}


def calc_score(current: float, p25: float, p90: float, median: Optional[float] = None) -> float:
    if median is None:
        median = (p25 + p90) / 2.0
    if p25 <= 0 or p90 <= p25 or not (p25 < median < p90):
        return 0.0
    x = float(current or 0)
    if x <= 0: return 0.0
    if x < p25: score = 40.0 * x / p25
    elif x <= median: score = 40.0 + 40.0 * (x - p25) / (median - p25)
    elif x <= p90: score = 80.0 + 20.0 * (x - median) / (p90 - median)
    else: score = 100.0
    return max(0.0, min(100.0, score))


def grade_text(score: float) -> str:
    for floor, text in [(100,"SSS 毕业"),(90,"SS 准毕业"),(80,"S 优秀"),(70,"A 良好"),(60,"B 中上"),(50,"C 一般"),(40,"D 入门"),(20,"E 待提升")]:
        if score >= floor: return text
    return "F 急需提升"


def _norm(name: str) -> str:
    return (str(name) or "").replace(" ", "").replace("％", "%").strip()


class ZZZPanelPlugin(BasePlugin):
    def __init__(self, ctx, cfg: dict):
        super().__init__(ctx, cfg)
        self.default_uid = 20363573
        self.enable_command = True
        self.command_words = ["/面板", "/评分", "/zzz面板"]
        self.allowed_users: List[str] = []
        self.enable_tool = True

    async def initialize(self):
        source = self.plugin_cfg.get("section_source", {}) or {}
        raw_uid = str(source.get("default_uid", "20363573") or "20363573").strip()
        self.default_uid = int(raw_uid) if raw_uid.isdigit() else 20363573
        common = self.plugin_cfg.get("section_common", {}) or {}
        self.enable_command = bool(common.get("enable_command", True))
        self.command_words = [str(x).strip() for x in common.get("command_words", self.command_words) if str(x).strip()]
        self.allowed_users = [str(x).strip() for x in common.get("allowed_users", []) if str(x).strip()]
        self.enable_tool = bool(common.get("enable_tool", True))
        logger.info(f"[zzz_panel] Enka 数据源，默认 UID={self.default_uid}")

    async def terminate(self):
        pass

    def _is_allowed(self, user_id: str) -> bool:
        return not self.allowed_users or str(user_id) in self.allowed_users

    @staticmethod
    def _get_sid(event) -> str:
        if hasattr(event, "sid"):
            return event.sid
        if hasattr(event, "session") and hasattr(event.session, "sid"):
            return event.session.sid
        return "default"

    async def create_panel_files(self, uid: int | str | None = None, character: str = ""):
        target_uid = uid or self.default_uid
        data = await fetch_enka(target_uid)
        return build_csv(data, target_uid, character)

    async def send_panel_files(self, event, uid: int | str | None = None, character: str = "") -> str:
        try:
            paths, agents = await self.create_panel_files(uid, character)
        except Exception as exc:
            logger.error(f"[zzz_panel] 处理失败: {exc}")
            return f"面板生成失败：{str(exc)[:240]}"
        missing = [path for path in paths if not os.path.isfile(path)]
        if missing:
            return f"面板文件生成失败：文件不存在 {missing[0]}"
        sid = self._get_sid(event)
        labels = ["角色总览", "驱动盘明细"]
        for idx, path in enumerate(paths):
            label = labels[idx] if idx < len(labels) else f"面板{idx + 1}"
            await self.ctx.message_processor.send_message_chain(
                session=sid,
                chain=MessageChain([
                    File(path, name=os.path.basename(path)),
                    Text(f"{label}已生成：{os.path.basename(path)}"),
                ]),
            )
        report = self._format_report(agents, character)
        return f"已向当前会话分条发送角色总览与驱动盘明细两份 CSV。{report[:100] if report else ''}"

    def _format_report(self, agents: List[dict], character: str = "") -> str:
        blocks = []
        for agent in agents:
            name, props = agent["name"], agent["properties"]
            thresholds = CHARACTER_DATA.get(name)
            lines = [f"【{name}】面板评分", "-" * 20]
            if not thresholds:
                lines.append("暂无毕业阈值，仅导出当前属性。")
                blocks.append("\n".join(lines)); continue
            weighted_sum = weight_total = 0.0
            for attr, th in thresholds.items():
                cur = next((v for k, v in props.items() if _norm(attr) == _norm(k) or _norm(attr) in _norm(k)), None)
                if cur is None: continue
                score = calc_score(cur, th["p25"], th["p90"], th.get("median"))
                weight = float(th.get("weight", 1.0) or 1.0)
                weighted_sum += score * weight; weight_total += weight
                lines.append(f"{attr}：{cur:.1f}  {score:.1f}分 {grade_text(score)}")
            lines.append(f"综合：{weighted_sum / weight_total:.1f}分  {grade_text(weighted_sum / weight_total)}" if weight_total else "可评分属性不足")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    @on.im_message(priority=Priority.HIGH)
    async def handle_command(self, event: KiraMessageEvent):
        if not self.enable_command: return
        text = "".join(e.text for e in event.message.chain if isinstance(e, Text)).strip()
        cmd = next((x for x in self.command_words if text == x or text.startswith(x + " ")), None)
        if not cmd: return
        sender = getattr(event.message, "sender", None)
        user_id = str(getattr(sender, "user_id", "")) if sender else ""
        if not self._is_allowed(user_id):
            reply = "这个命令不对你开放"
        else:
            arg = text[len(cmd):].strip()
            pieces = arg.split(maxsplit=1)
            uid = pieces[0] if pieces and pieces[0].isdigit() else None
            character = pieces[1] if uid and len(pieces) > 1 else (arg if not uid else "")
            reply = await self.send_panel_files(event, uid, character)
        if not reply.startswith("已向当前会话发送"):
            await self.ctx.message_processor.send_message_chain(session=self._get_sid(event), chain=MessageChain([Text(reply)]))
        event.discard(force=True); event.stop()

    @on.llm_request(priority=Priority.HIGH)
    async def inject_tools(self, event, req: LLMRequest, *args, **kwargs):
        if self.enable_tool:
            req.tool_set.add(ZZZPanelTool(ctx=self.ctx, plugin=self))


class ZZZPanelTool(BaseTool):
    name = "get_zzz_panel"
    description = "通过 Enka 查询绝区零公开展示柜，生成角色总览和驱动盘明细 CSV，并直接发送到当前会话"
    parameters = {"type":"object","properties":{
        "uid":{"type":"string","description":"绝区零 UID；可选，不传时使用配置的默认 UID"},
        "character":{"type":"string","description":"可选角色名；留空导出展示柜全部角色"}},"required":[]}

    def __init__(self, ctx, plugin):
        self.ctx, self.plugin = ctx, plugin

    async def execute(self, event, uid: str = "", character: str = "", *args, **kwargs):
        sender = getattr(getattr(event, "message", None), "sender", None)
        user_id = str(getattr(sender, "user_id", "")) if sender else ""
        if not self.plugin._is_allowed(user_id):
            return "当前用户没有面板查询权限"
        return await self.plugin.send_panel_files(event, uid or None, character or "")
