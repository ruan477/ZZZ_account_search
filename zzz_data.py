# -*- coding: utf-8 -*-
"""Enka 数据拉取与 CSV 生成；可脱离聊天框架独立使用。"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from . import panel_calc as _panel_calc
except ImportError:
    import panel_calc as _panel_calc

import aiohttp
import pandas as pd
import csv

ROOT = Path(__file__).resolve().parents[3]
MAP_DIR = ROOT / "data" / "files" / "zzz_maps"
OUTPUT_DIR = ROOT / "data" / "files" / "zzz_panels"
HEADERS = {"User-Agent": "zzz-panel/1.0", "Accept": "application/json"}

NAME_ZH = {
    "HpMax": "生命值", "Atk": "攻击力", "Def": "防御力", "BreakStun": "冲击力",
    "Crit": "暴击率", "CritDmg": "暴击伤害", "PenDelta": "穿透值", "PenRate": "穿透率",
    "SpRecover": "能量自动回复", "ElementMystery": "异常掌控", "ElementAbnormalPower": "异常精通",
    "AddedDamageRatio_Physics": "物理伤害加成", "AddedDamageRatio_Fire": "火属性伤害加成",
    "AddedDamageRatio_Ice": "冰属性伤害加成", "AddedDamageRatio_Elec": "电属性伤害加成",
    "AddedDamageRatio_Ether": "以太伤害加成",
}
SKILL_NAMES = {0: "普通攻击", 1: "闪避", 2: "支援技", 3: "特殊技", 5: "连携技", 6: "核心技技能等级"}


def _load(name: str) -> dict:
    with open(MAP_DIR / name, "r", encoding="utf-8") as f:
        return json.load(f)


def load_maps() -> tuple[dict, dict, dict, dict]:
    partners = _load("PartnerId2Data_3.2.0.json")
    weapons = _load("WeaponId2Data_3.2.0.json")
    equips_raw = _load("EquipId2Data_3.2.0.json")
    properties = _load("property.json")
    equips = {}
    for row in equips_raw.values():
        for equip_id in row.get("equip_id_list", []):
            equips[str(equip_id)] = row.get("equip_name", "")
    return partners, weapons, equips, properties


async def fetch_enka(uid: int | str, timeout: int = 25) -> dict:
    uid_text = str(uid).strip()
    if not uid_text.isdigit():
        raise ValueError("UID 必须是纯数字")
    url = f"https://enka.network/api/zzz/uid/{uid_text}"
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(timeout=client_timeout, headers=HEADERS) as session:
        async with session.get(url) as response:
            body = await response.text()
            if response.status != 200:
                raise RuntimeError(f"Enka HTTP {response.status}: {body[:300]}")
            try:
                return json.loads(body)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"Enka 返回的不是 JSON: {body[:200]}") from exc


def _property_meta(property_id: Any, properties: dict) -> tuple[str, bool]:
    raw = str(property_id or "")
    # Enka 末三位是词条变体，例如 20103 -> property.json 的 201。
    base = raw if raw in properties else (raw[:-2] if len(raw) > 3 else raw)
    meta = properties.get(base, {})
    source_name = str(meta.get("Name", ""))
    name = NAME_ZH.get(source_name, f"属性{raw}")
    is_percent = "%" in str(meta.get("Format", ""))
    return name, is_percent


def _property_value(item: dict, properties: dict, main: bool = False) -> tuple[str, float, str]:
    """enka 词条 -> 显示值。

    主词条：raw*4（1号位 550 -> 2200）
    副词条：raw*PropertyLevel（乘档位，之前漏了这一步，导致明细比汇总小一截）
    百分比类再多除 100。
    """
    name, is_percent = _property_meta(item.get("PropertyId"), properties)
    try:
        raw = float(item.get("PropertyValue", 0) or 0)
        level = int(item.get("PropertyLevel", 1) or 1)
    except (TypeError, ValueError):
        raw, level = 0.0, 1
    value = raw * 4 if main else raw * level
    if is_percent:
        value /= 100.0
        shown = f"{value:g}%"
    else:
        shown = f"{value:g}"
    return name, value, shown


def _load_csv(name: str) -> list[dict[str, str]]:
    """读取只读映射表；映射表本身不在插件目录内，绝不写回。"""
    with open(MAP_DIR / name, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


_MUL_KEY = {"攻击力": "攻击%", "生命值": "生命%", "防御力": "防御%", "异常掌控": "异常掌控%"}


def _static_set_bonus(equipped_list: list[dict]) -> dict[str, float]:
    """按实际装备套装件数加入静态(非战斗条件)2/4件套效果。

    注意：攻击力/生命值/防御力的套装效果是「百分比」，必须落到 X% 乘区键上，
    否则会被 total_panel 当成固定值直接相加（曾导致星见雅攻击少 152）。
    """
    id_to_set = {r["disc_id"]: r["set_id"] for r in _load_csv("disc_id_to_set.csv")}
    counts: dict[str, int] = {}
    for disk in equipped_list:
        sid = id_to_set.get(str((disk.get("Equipment") or {}).get("Id", "")))
        if sid:
            counts[sid] = counts.get(sid, 0) + 1
    out: dict[str, float] = {}
    for r in _load_csv("disc_set_bonus.csv"):
        if r.get("effect_type") != "static":
            continue
        if counts.get(r.get("set_id", ""), 0) < int(r.get("piece_count") or 0):
            continue
        try: value = float(r.get("value") or 0)
        except ValueError: value = 0.0
        key = _MUL_KEY.get(r["prop"], r["prop"])
        out[key] = out.get(key, 0.0) + value
    return out


def _weapon_secondary(weapon_id: str) -> dict[str, float]:
    """音擎副属性只有满级单档 secondary_value_lv60；不做插值，存在表注明的偏差。"""
    for r in _load_csv("weapon_secondary.csv"):
        if str(r.get("weapon_id", "")) == weapon_id:
            try: value = float(r.get("secondary_value_lv60") or 0)
            except ValueError: value = 0.0
            return {r.get("secondary_prop", ""): value}
    return {}


def panel_rows(data: dict, character: str = "") -> tuple[list[dict], list[dict], list[dict]]:
    partners, weapons, equips, properties = load_maps()
    avatars = (((data.get("PlayerInfo") or {}).get("ShowcaseDetail") or {}).get("AvatarList") or [])
    overview, detail, agents = [], [], []
    for avatar in avatars:
        partner = partners.get(str(avatar.get("Id")), {})
        name = partner.get("full_name") or partner.get("name") or f"角色{avatar.get('Id', '')}"
        if character and character not in name:
            continue
        skills = {int(x.get("Index", -1)): x.get("Level", "") for x in avatar.get("SkillLevelList", [])}
        weapon = avatar.get("Equip") or avatar.get("Weapon") or {}
        weapon_id = str(weapon.get("Id", ""))
        weapon_name = (weapons.get(weapon_id) or {}).get("name") or (f"音擎{weapon_id}" if weapon_id else "")
        # 保留旧行为：原实现仅将驱动盘主/副词条累加到 prop_totals；该逻辑
        # 已移至 panel_calc.parse_disc，并由下方 total_panel 叠加真实本体/音擎/套装。
        disc, unknown = _panel_calc.parse_disc(avatar.get("EquippedList", []))
        disc.update({k: disc.get(k, 0) + v for k, v in _weapon_secondary(weapon_id).items()})
        for k, v in _static_set_bonus(avatar.get("EquippedList", [])).items():
            disc[k] = disc.get(k, 0) + v
        level = int(avatar.get("Level") or 60)
        core_level = int(avatar.get("CoreSkillEnhancement") or 0)
        base = _panel_calc.get_base_panel(name, level, core_level)
        weapon_base = {"攻击力": _panel_calc.get_weapon_base_atk(weapon_id, weapon_name, int(weapon.get("Level") or 60))}
        panel = _panel_calc.total_panel(base, weapon_base, disc)
        for disk in avatar.get("EquippedList", []):
            equipment = disk.get("Equipment") or {}
            main_parts, sub_parts = [], []
            for item in equipment.get("MainPropertyList", []):
                prop_name, value, shown = _property_value(item, properties, True); main_parts.append(f"{prop_name}+{shown}")
            for item in equipment.get("RandomPropertyList", []):
                prop_name, value, shown = _property_value(item, properties, False); sub_parts.append(f"{prop_name}+{shown}")
            equip_id = str(equipment.get("Id", ""))
            detail.append({"角色": name, "Slot": disk.get("Slot", ""), "驱动盘Id": equip_id,
                "驱动盘中文名": equips.get(equip_id, f"驱动盘{equip_id}"), "等级": equipment.get("Level", ""),
                "突破等级": equipment.get("BreakLevel", ""), "主词条中文名及数值": "；".join(main_parts), "副词条各条": "；".join(sub_parts)})
        row = {"角色名": name, "等级": level, "突破": avatar.get("PromotionLevel", ""), "影画等级": avatar.get("TalentLevel", ""),
            "核心技强化": core_level, **{SKILL_NAMES[i]: skills.get(i, "") for i in SKILL_NAMES}, "音擎名": weapon_name,
            "音擎等级": weapon.get("Level", ""), "音擎突破": weapon.get("BreakLevel", ""), **panel}
        overview.append(row); agents.append({"name": name, "properties": panel, "unknown_properties": unknown})
    return overview, detail, agents


def build_csv(data: dict, uid: int | str, character: str = "", output_dir: str | os.PathLike | None = None) -> tuple[list[str], list[dict]]:
    overview, detail, agents = panel_rows(data, character)
    if not overview:
        raise ValueError("Enka 展示柜没有匹配的角色数据")
    out = Path(output_dir) if output_dir else OUTPUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    tag = "".join(c for c in (character or str(uid)) if c.isalnum() or '\u4e00' <= c <= '\u9fff') or str(uid)
    overview_path = out / f"绝区零_{tag}_{stamp}_角色总览.csv"
    detail_path = out / f"绝区零_{tag}_{stamp}_驱动盘明细.csv"
    pd.DataFrame(overview).to_csv(overview_path, index=False, encoding="utf-8-sig")
    pd.DataFrame(detail, columns=["角色", "Slot", "驱动盘Id", "驱动盘中文名", "等级", "突破等级", "主词条中文名及数值", "副词条各条"]).to_csv(detail_path, index=False, encoding="utf-8-sig")
    # 固定验收样本对照：只打印实际计算值，绝不为贴合期望值修改数据。
    for item in overview:
        if item.get("角色名") == "星见雅":
            expected = {"攻击力": 2899.0, "生命值": 10669.0, "防御力": 1025.0,
                        "暴击率": 73.6, "暴击伤害": 123.6, "异常精通": 238.0}
            print("星见雅验收对照（期望 / 实算 / 差值）：")
            for key, want in expected.items():
                got = float(item.get(key, 0)); print(f"{key}: {want:g} / {got:g} / {got-want:+.2f}")
            print("异常掌控：期望 150 / 实算 %.2f / 差值 %+.2f（待查）" % (float(item.get("异常掌控", 0)), float(item.get("异常掌控", 0))-150))
    return [str(overview_path), str(detail_path)], agents
