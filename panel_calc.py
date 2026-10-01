# -*- coding: utf-8 -*-
"""
绝区零 角色总面板计算 + 属性评分 模块
纺希 / 先生(961855469)  2026-09-26

【一】基础属性公式（已用星见雅60级校准，与 B站wiki / prydwen 完全一致）
    属性 = BaseProps + GrowthProps*(等级-1)/10000
           + PromotionProps[突破等级] + CoreEnhancementProps[核心技等级]
    突破等级 = min(5, (等级-1)//10)
    校准样例（星见雅60级）：
        HP   = 617 + 837238*59/10000 + 2117 = 7673
        攻击 = 127 + 452.7 + 226 + 75      = 880
        防御 = 49 + 390.6 + 167            = 606

【二】总面板乘区（本次要修的重点）
    攻击 = (角色基础攻击 + 音擎基础攻击) * (1 + 攻击%) + 固定攻击
    生命 = 角色基础生命 * (1 + 生命%) + 固定生命
    防御 = 角色基础防御 * (1 + 防御%) + 固定防御
    暴击率 / 暴击伤害 / 异常掌控 / 异常精通 / 穿透率 / 冲击力 / 能量自动回复
        —— 直接相加，不进乘区
    驱动盘词条：主词条 = enka返回值 * 4（1号位550->2200、2号位79->316、
        4号位600->24%、5号位750->30%），副词条不缩放

【三】评分
    score = clamp((值 - p25) / (p90 - p25), 0, 1) * 100
    p25以下记0，p90以上记100，中间线性插值
    总分 = Σ(单项分 * 权重) / Σ权重

音擎基础攻击采用 weapon_base_panel.csv 中校准的60级数值；不适用攻击力的音擎不进入攻击计算。
"""

import os
import csv
import unicodedata
from typing import Dict, Optional, Tuple

# ---------------------------------------------------------------- 常量

# 数据表固定在 data/files/zzz_maps。本模块可能放在两处运行：
#   data/files/zzz_plugin_work/panel_calc.py          -> ../zzz_maps
#   data/plugins/kira-ai-plugin-zzz-panel/panel_calc.py -> ../../files/zzz_maps
_HERE = os.path.dirname(os.path.abspath(__file__))
_MAPS_CANDIDATES = [
    os.path.abspath(os.path.join(_HERE, "..", "..", "files", "zzz_maps")),
    os.path.abspath(os.path.join(_HERE, "..", "zzz_maps")),
    r"C:\Users\Administrator\Desktop\KiraAI-2.28.1\data\files\zzz_maps",
]
MAPS_DIR = next((d for d in _MAPS_CANDIDATES if os.path.isdir(d)), _MAPS_CANDIDATES[0])

# 这些属性走"乘区"，其余走"相加"
# 值 = 该字段在 disc 里对应的百分比键名
MUL_FIELDS = {"攻击力": "攻击%", "生命值": "生命%", "防御力": "防御%", "异常掌控": "异常掌控%"}

# 面板里以百分号显示的字段（用于排版）
PCT_FIELDS = {"暴击率", "暴击伤害", "穿透率", "能量自动回复"}

# 中文列名 <-> enka 英文键 的对照（enka 用全大写短名）
KEY_ALIAS = {
    "生命值": "HP",
    "攻击力": "ATK",
    "防御力": "DEF",
    "冲击力": "IMPACT",
    "暴击率": "CRIT",
    "暴击伤害": "CRIT_DMG",
    "异常掌控": "ANOMALY_MASTERY",
    "异常精通": "ANOMALY_PROFICIENCY",
    "穿透率": "PEN_RATE",
    "能量自动回复": "ENERGY_REGEN",
}

# enka PropertyId mapping. Fixed substats use raw*level; percentage substats use raw*level/100.
PROPERTY_ID_MAP = {
    11103: ("生命值", "flat"), 11102: ("生命%", "pct"),
    12103: ("攻击力", "flat"), 12102: ("攻击%", "pct"),
    13103: ("防御力", "flat"), 13102: ("防御%", "pct"),
    20103: ("暴击率", "pct"), 21103: ("暴击伤害", "pct"),
    23203: ("穿透值", "flat"), 31203: ("异常精通", "flat"),
    31402: ("异常掌控%", "main_pct"), 31903: ("以太伤害加成", "main_pct"),
    12201: ("冲击力", "flat"), 12202: ("冲击力", "main_pct"),
}

def parse_disc_property(prop, main=False):
    pid = int(prop.get("PropertyId", 0)); rec = PROPERTY_ID_MAP.get(pid)
    if not rec: return None, 0.0
    name, kind = rec; raw = float(prop.get("PropertyValue", 0)); level = int(prop.get("PropertyLevel", 1))
    if main:
        return name, raw * 4 / (100 if kind in ("pct", "main_pct") else 1)
    return name, raw * level / (100 if kind in ("pct", "main_pct") else 1)

def parse_disc(equipped_list):
    out, unknown = {}, []
    for item in equipped_list or []:
        e = item.get("Equipment", item)
        for prop in e.get("MainPropertyList", []):
            k, v = parse_disc_property(prop, True)
            if k: out[k] = out.get(k, 0) + v
            else: unknown.append(prop.get("PropertyId"))
        for prop in e.get("RandomPropertyList", []):
            k, v = parse_disc_property(prop, False)
            if k: out[k] = out.get(k, 0) + v
            else: unknown.append(prop.get("PropertyId"))
    return out, unknown

BASE_TABLE = os.path.join(MAPS_DIR, "agent_base_panel.csv")
CORE_TABLE = os.path.join(MAPS_DIR, "agent_core_bonus.csv")


# ---------------------------------------------------------------- 基础表

WPN_TABLE = os.path.join(MAPS_DIR, "weapon_base_panel.csv")

def _normalize_name(name: str) -> str:
    name = unicodedata.normalize("NFKC", str(name or ""))
    for mark in "「」『』":
        name = name.replace(mark, "")
    return "".join(name.split()).replace("霞落星殿", "霰落星殿")

def load_weapon_table(path: str = WPN_TABLE):
    by_id, by_name = {}, {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            try:
                lv1, lv60 = float(row["base_atk_lv1"]), float(row["base_atk_lv60"])
            except (ValueError, TypeError):
                continue
            rec = {"lv1": lv1, "lv60": lv60, "main_prop": row.get("main_prop", "")}
            by_id[str(row["weapon_id"]).strip()] = rec
            by_name[_normalize_name(row["name"])] = rec
    return by_id, by_name

def get_weapon_base(weapon_id=None, weapon_name=None, level=60):
    by_id, by_name = load_weapon_table()
    rec = by_id.get(str(weapon_id)) if weapon_id is not None else None
    if rec is None and weapon_name: rec = by_name.get(_normalize_name(weapon_name))
    if rec is None: raise KeyError("找不到音擎基础属性：id=%r name=%r" % (weapon_id, weapon_name))
    if rec["lv1"] is None: return {}
    # 数据表没有逐突破档位成长点；按 Lv.1/Lv.60 线性近似，真实分段成长会有偏差风险。
    lv = max(1, min(60, int(level or 60)))
    atk = rec["lv1"] + (rec["lv60"] - rec["lv1"]) * (lv - 1) / 59
    return {"攻击力": atk} if rec["main_prop"] != "基础防御力" else {}

def get_weapon_base_atk(weapon_id=None, weapon_name=None, level=60) -> float:
    return get_weapon_base(weapon_id, weapon_name, level).get("攻击力", 0.0)

def load_base_table(path: str = BASE_TABLE) -> Dict[Tuple[str, int], dict]:
    """读取 角色x等级 的纯本体面板表 -> {(角色名, 等级): row}"""
    table: Dict[Tuple[str, int], dict] = {}
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            table[(row["角色名"], int(row["等级"]))] = row
    return table


def load_core_table(path: str = CORE_TABLE) -> Dict[Tuple[str, int], dict]:
    """读取 角色x核心技等级 的加成表 -> {(角色名, 核心技等级): row}"""
    table: Dict[Tuple[str, int], dict] = {}
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            table[(row["角色名"], int(row["核心技等级"]))] = row
    return table


def _num(v) -> float:
    """把 '5.0%' / '1452' 之类统一转成 float"""
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().rstrip("%")
    try:
        return float(s)
    except ValueError:
        return 0.0


def get_base_panel(name: str, level: int = 60,
                   core_level: int = 6,
                   base_table: Optional[dict] = None) -> Dict[str, float]:
    """
    取某角色某等级的"纯本体"面板（不含音擎、不含驱动盘）
    注意：agent_base_panel.csv 里已含核心技加成以外的全部，核心技加成单独在
          agent_core_bonus.csv —— 这里把两张表叠起来，得到真正的本体面板
    """
    base_table = base_table or load_base_table()
    core_table = load_core_table()

    row = base_table.get((name, int(level)))
    if row is None:
        raise KeyError("找不到角色/等级：%s Lv.%s" % (name, level))

    panel = {k: _num(v) for k, v in row.items()
             if k not in ("角色名", "稀有度", "定位", "属性", "阵营", "等级", "突破等级")}

    core = core_table.get((name, int(core_level)))
    if core:
        for k in list(panel.keys()):
            if k in core:
                panel[k] += _num(core[k])
    return panel


# ---------------------------------------------------------------- 总面板

def total_panel(char_base: Dict[str, float],
                weapon_base: Optional[Dict[str, float]] = None,
                disc: Optional[Dict[str, float]] = None) -> Dict[str, float]:
    """
    合成总面板。

    char_base   角色本体面板，键同 agent_base_panel.csv 的中文列名
                如 {"生命值":7673, "攻击力":880, "防御力":606, "暴击率":5.0, ...}
    weapon_base 音擎满级基础属性，只需 {"攻击力":743}（有生命/防御也带上）
    disc        驱动盘 + 其它加成汇总，键为
                "攻击%"、"攻击力"（固定值）、"生命%"、"生命值"（固定值）、
                "防御%"、"防御力"（固定值）、"暴击率"、"暴击伤害"、
                "异常掌控"、"异常精通"、"穿透率"、"冲击力"、"能量自动回复"
                百分比类请传百分数（24 表示 24%），不要传 0.24
    """
    weapon_base = weapon_base or {}
    disc = disc or {}
    out = dict(char_base)

    # Only Anomaly Mastery has a percentage main stat and a nonzero base stat.
    # Crit, PEN (base zero), and proficiency remain additive; do not route them through MUL.
    for field, pct_key in MUL_FIELDS.items():
        base = _num(char_base.get(field, 0)) + _num(weapon_base.get(field, 0))
        pct = _num(disc.get(pct_key, 0)) / 100.0
        flat = _num(disc.get(field, 0))
        out[field] = round(base * (1 + pct) + flat, 2)

    for field in KEY_ALIAS:
        if field in MUL_FIELDS:
            continue
        out[field] = round(_num(char_base.get(field, 0)) + _num(disc.get(field, 0)), 2)
    return out


# ---------------------------------------------------------------- 评分

def calc_score(value: float, p25: float, p90: float) -> float:
    """单项评分：p25以下0分，p90以上100分，中间线性插值"""
    if p90 <= p25:
        return 0.0
    r = (value - p25) / (p90 - p25)
    return round(max(0.0, min(1.0, r)) * 100, 1)


def grade_text(score: float) -> str:
    if score >= 90:
        return "毕业"
    if score >= 75:
        return "优秀"
    if score >= 60:
        return "合格"
    if score >= 40:
        return "有待提升"
    return "不及格"


def score_panel(panel: Dict[str, float],
                standards: Dict[str, dict],
                weights: Optional[Dict[str, float]] = None) -> dict:
    """
    standards 形如 {"攻击力": {"p25": 3178.8, "p90": 3579.4}, ...}
    weights   形如 {"攻击力": 1.0, "暴击率": 1.0, "暴击伤害": 1.5}
    返回 {"items": {...}, "total": xx, "grade": "优秀"}
    """
    weights = weights or {}
    items, tw, ts = {}, 0.0, 0.0
    for field, std in standards.items():
        if field not in panel:
            continue
        s = calc_score(_num(panel[field]), _num(std.get("p25", 0)), _num(std.get("p90", 0)))
        w = _num(weights.get(field, 1.0))
        items[field] = {"value": panel[field], "p25": std.get("p25"),
                        "p90": std.get("p90"), "score": s,
                        "grade": grade_text(s), "weight": w}
        tw += w
        ts += s * w
    total = round(ts / tw, 1) if tw else 0.0
    return {"items": items, "total": total, "grade": grade_text(total)}


# ---------------------------------------------------------------- 自测

if __name__ == "__main__":
    import json

    # 1) 星见雅 60级 本体校准
    b = get_base_panel("星见雅", 60, 6)
    print("星见雅本体：", {k: b[k] for k in ("生命值", "攻击力", "防御力")})
    assert abs(b["生命值"] - 7673) < 2, b["生命值"]
    assert abs(b["攻击力"] - 880) < 2, b["攻击力"]
    assert abs(b["防御力"] - 606) < 2, b["防御力"]

    # 2) 总面板：假想配置 —— 攻击% 30、固定攻击 316
    tot = total_panel(
        b,
        weapon_base={"攻击力": 743},
        disc={"攻击%": 30, "攻击力": 316, "暴击率": 24, "暴击伤害": 48},
    )
    expect_atk = (880 + 743) * 1.3 + 316
    print("总攻击：", tot["攻击力"], "期望：", expect_atk)
    assert abs(tot["攻击力"] - expect_atk) < 0.01

    # 3) 评分：叶瞬光毕业线
    std = {
        "攻击力": {"p25": 3178.8, "p90": 3579.4},
        "暴击率": {"p25": 50.0, "p90": 50.6},
        "暴击伤害": {"p25": 209.6, "p90": 229.2},
    }
    demo = {"攻击力": 3400, "暴击率": 50.6, "暴击伤害": 220}
    print(json.dumps(score_panel(demo, std, {"暴击伤害": 1.5}),
                     ensure_ascii=False, indent=2))
    print("自测通过")
