"""排班数据加载层 —— 把"数据"从"逻辑"里彻底抽出来。

代码只负责规则怎么算，数据从外面进来：

    2026年10月排班表.xlsx
      ├── 人员档案   姓名 / 组别 / 休息额度 / 状态
      ├── 本月需求   姓名 / 日期 / 期望班次
      └── 班表       30 天格子（可为空，生成前不需要）
    月度参数.json    年月 / 全员日 / 各项阈值 / 权重 / 锁定格 / 上月末7天

加载时的校验规则（与人事专员约定）：
  - 状态 = 不排班  -> 直接过滤，不进模型（实习生等编外人员）
  - 在编但班表缺行 -> 报错停下
  - 组别是未知值   -> 报错停下
  - 需求行人名/日期/班次非法 -> 跳过该行并警告，不影响整月
"""

import calendar
import json
import os
from typing import Dict, List, Optional, Tuple

import pandas as pd

from roster_rules import (Config, Roster, SHIFTS, EARLY, MID, LATE, OFF,
                          GROUP_MGMT, GROUP_SMART, GROUP_SALES)

SHEET_PEOPLE, SHEET_REQ, SHEET_GRID = "人员档案", "本月需求", "班表"

# 引擎认识的组只有这三个。Excel 里读到的字符串必须命中其中之一，
# 否则 R6（管理组/智办每日一早一晚）就不知道该不该管这个人。
KNOWN_GROUPS = {GROUP_MGMT, GROUP_SMART, GROUP_SALES}

STATUS_ACTIVE, STATUS_SKIP, STATUS_LEFT = "在编", "不排班", "离职"
# 白名单：只有「在编」参与排班；「不排班」（实习生等）和「离职」跳过；
# 其他任何没见过的状态直接报错。不能写成"不是不排班就当在编"——
# 那样有人填了「离职」「停职」，程序会照样给他排班，而且不报错。
INACTIVE_STATUS = {STATUS_SKIP, STATUS_LEFT}
KNOWN_STATUS = {STATUS_ACTIVE} | INACTIVE_STATUS
COL_LEAVE = "年假日期"      # 可选列。格式 "10-14" 或 "3,10-14"，空着表示本月无年假

# 需求表里允许人事/员工写中文，读进来转成引擎用的编码
SHIFT_ALIAS = {"早": EARLY, "中": MID, "晚": LATE, "休": OFF,
               EARLY: EARLY, MID: MID, LATE: LATE, OFF: OFF, "OFF": OFF}

WEEKDAY_CN = ["一", "二", "三", "四", "五", "六", "日"]


class RosterDataError(Exception):
    """数据有问题且无法继续时抛出。消息面向人事专员，不是面向程序员。"""


def month_calendar(year: int, month: int) -> Tuple[int, List[str]]:
    """天数和星期几直接由年月算出来，不从表格里读——
    表格里的星期列是人填的，会填错；日历不会。"""
    days = calendar.monthrange(year, month)[1]
    dow = [WEEKDAY_CN[calendar.weekday(year, month, d + 1)] for d in range(days)]
    return days, dow


# ---------------------------------------------------------------- 人员档案

def parse_leave(text, days: int) -> List[int]:
    """把 "3,10-14" 解析成 [3,10,11,12,13,14]。非法内容抛 ValueError。"""
    if text is None or str(text).strip().lower() in ("", "nan", "none", "无"):
        return []
    out = set()
    for part in str(text).replace("，", ",").replace("－", "-").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            a, b = int(a), int(b)
            if a > b:
                raise ValueError(f"区间 {part} 的起止写反了")
            out.update(range(a, b + 1))
        else:
            out.add(int(part))
    bad = [d for d in out if not 1 <= d <= days]
    if bad:
        raise ValueError(f"日期 {sorted(bad)} 超出本月 1~{days}")
    return sorted(out)


def load_people(path: str, days: int = 31):
    """返回 (在编姓名表, 组别映射, 休息额度映射, 年假映射, 被过滤掉的人)。
    休息额度填平时天数即可，年假是额外的，由程序自动加上去。"""
    try:
        df = pd.read_excel(path, sheet_name=SHEET_PEOPLE)
    except ValueError:
        raise RosterDataError(f"找不到工作表「{SHEET_PEOPLE}」，请检查 {os.path.basename(path)}")

    need = {"姓名", "组别", "休息额度", "状态"}
    missing = need - set(df.columns)
    if missing:
        raise RosterDataError(f"「{SHEET_PEOPLE}」缺少列：{'、'.join(sorted(missing))}")

    names, gof, quota, leave, skipped, bad = [], {}, {}, {}, [], []
    for _, row in df.iterrows():
        n = str(row["姓名"]).strip()
        if not n or n == "nan":
            continue
        status = str(row["状态"]).strip()
        if status in INACTIVE_STATUS:
            skipped.append(n)
            continue
        if status != STATUS_ACTIVE:
            bad.append(f"{n}（状态「{status}」，只能填 在编 / 不排班 / 离职）")
            continue

        g = str(row["组别"]).strip()
        if g not in KNOWN_GROUPS:
            bad.append(f"{n}（组别「{g}」）")
            continue

        if pd.isna(row["休息额度"]):
            bad.append(f"{n}（休息额度为空）")
            continue

        try:
            days_off = parse_leave(row.get(COL_LEAVE) if COL_LEAVE in df.columns else None, days)
        except ValueError as e:
            bad.append(f"{n}（年假日期：{e}）")
            continue
        if days_off and g == GROUP_MGMT:
            # 管理组人少、每天必须留一早一晚，年假会直接把容量吃穿。
            # 这类情况由人事专员手工处理（店长可补位），引擎不半自动地碰它——
            # 半生效比完全不生效更危险：排出来的表看着正常，实际某天没人在岗。
            bad.append(f"{n}（管理组不支持填年假，请由人事专员手工安排，并清空该格）")
            continue

        names.append(n)
        gof[n] = g
        quota[n] = int(row["休息额度"]) + len(days_off)   # 年假是额外的
        if days_off:
            leave[n] = days_off

    if bad:
        raise RosterDataError(
            "「人员档案」有无法识别的内容，已停止：\n  " + "\n  ".join(bad) +
            f"\n组别只能填：{'、'.join(sorted(KNOWN_GROUPS))}")
    if not names:
        raise RosterDataError("「人员档案」里没有任何在编人员")
    return names, gof, quota, leave, skipped


# ---------------------------------------------------------------- 本月需求

def load_requests(path: str, names: List[str], days: int):
    """返回 ({姓名: {日: 班次}}, 警告列表)。
    单行错误只跳过该行——一条填错不该拖垮整月排班。"""
    try:
        df = pd.read_excel(path, sheet_name=SHEET_REQ)
    except ValueError:
        return {}, [f"未找到工作表「{SHEET_REQ}」，本月按无需求处理"]

    need = {"姓名", "日期", "期望班次"}
    if need - set(df.columns):
        return {}, [f"「{SHEET_REQ}」列名不符（需要 姓名/日期/期望班次），本月按无需求处理"]

    req: Dict[str, Dict[int, str]] = {}
    warn: List[str] = []
    for i, row in df.iterrows():
        ln = i + 2                                   # Excel 行号（含标题行）
        n = str(row["姓名"]).strip()
        if not n or n == "nan":
            continue
        if n not in names:
            warn.append(f"第{ln}行：查无此人「{n}」，已跳过")
            continue
        try:
            d = int(row["日期"])
        except (TypeError, ValueError):
            warn.append(f"第{ln}行：{n} 的日期「{row['日期']}」不是数字，已跳过")
            continue
        if not 1 <= d <= days:
            warn.append(f"第{ln}行：{n} 的日期 {d} 超出本月 1~{days}，已跳过")
            continue

        s = SHIFT_ALIAS.get(str(row["期望班次"]).strip())
        if s is None:
            warn.append(f"第{ln}行：{n} 的班次「{row['期望班次']}」无法识别，已跳过")
            continue

        if d in req.get(n, {}) and req[n][d] != s:
            warn.append(f"第{ln}行：{n} 在 {d} 日有重复且冲突的需求，以先出现的为准")
            continue
        req.setdefault(n, {})[d] = s
    return req, warn


# ---------------------------------------------------------------- 班表

def load_grid(path: str, names: List[str], days: int):
    """读已有班表。生成前这张表可以是空的，返回 None 表示"没有基准版"。
    在编人员缺行 -> 报错停下（编外人员已在 load_people 过滤）。"""
    try:
        df = pd.read_excel(path, sheet_name=SHEET_GRID, header=None)
    except ValueError:
        return None

    grid, found = {}, set()
    for r in range(df.shape[0]):
        n = df.iloc[r, 0]
        if not isinstance(n, str) or not n.strip() or n.strip() not in names:
            continue
        n = n.strip()
        row = [str(df.iloc[r, 1 + c]).strip() if 1 + c < df.shape[1] else "nan"
               for c in range(days)]
        if all(v in ("nan", "") for v in row):
            continue
        grid[n] = [SHIFT_ALIAS.get(v, v) for v in row]
        found.add(n)

    if not found:
        return None                                  # 整张表空白：尚未排班

    missing = [n for n in names if n not in found]
    if missing:
        raise RosterDataError(
            f"「{SHEET_GRID}」缺少在编人员：{'、'.join(missing)}\n"
            f"请补上他们的班次；若本月不参与排班，请在「{SHEET_PEOPLE}」把状态改为「{STATUS_SKIP}」")
    return grid


# ---------------------------------------------------------------- 月度参数

DEFAULT_PARAMS = {
    "year": 2026, "month": 10,
    "all_hands_days": [],
    "min_early": 4, "min_late": 4, "max_late_over_early": 2, "weekend_min_on": 12,
    "max_consec": 7, "soft_max_consec": 5, "max_consec_off_runs": 2,
    "balance_tol": 2, "mid_soft_cap": 2, "max_off_run": 2, "max_mid_per_person": 4,
    "max_requested_off_run": 3,
    "mgmt_avoid_weekend_off": False,
    "weights": {"R3": 10, "R4b": 1, "R7": 8,
                "R14": 12, "R15": 10, "MID": 8, "SANDWICH": 12, "SHORTRUN": 15,
                "REQUEST": 25},
    "locked": {},          # {"姓名": {"15": "SFT6"}} 人事专员的硬性安排
    "forbidden": {},       # {"姓名": {"15": "CBGDAYK"}} 人事专员的"不能排"
    "prev_tail": {},       # {"姓名": ["SFT6", ...]} 上月末7天，手工填
}


def load_params(json_path: Optional[str]) -> dict:
    """读月度参数。找不到文件时直接报错——静默用默认值会让人以为参数生效了，
    实际全员上班日、跨月数据全是空的，排出来的表看着正常但是错的。"""
    p = dict(DEFAULT_PARAMS)
    if not json_path:
        return p
    if not os.path.exists(json_path):
        raise RosterDataError(
            f"找不到月度参数文件：{json_path}\n"
            f"  当前目录：{os.getcwd()}\n"
            f"  该目录下的 json：{[f for f in os.listdir('.') if f.endswith('.json')] or '（没有）'}")
    with open(json_path, encoding="utf-8") as f:
        user = json.load(f)
    uw = user.pop("weights", {})
    stale = [k for k in uw if k not in DEFAULT_PARAMS["weights"]]
    if stale:
        # 典型情况：R9 改成硬约束后不再吃权重，旧 JSON 里还留着它。
        # 不报错（不影响运行），但要说出来，免得以为调它有用。
        print(f"  [提示] 月度参数里这些权重已不起作用，可以删掉：{'、'.join(stale)}")
    p["weights"] = {**p["weights"], **{k: v for k, v in uw.items() if k not in stale}}
    p.update(user)
    return p


def write_params_template(json_path: str, year: int, month: int, **over):
    p = dict(DEFAULT_PARAMS)
    p["year"], p["month"] = year, month
    p.update(over)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False, indent=2)
    return json_path


# ---------------------------------------------------------------- 总入口

def load_all(xlsx_path: str, params_path: Optional[str] = None, verbose: bool = True):
    """返回 (cfg, names, group_of, requests, human_roster_or_None)。"""
    p = load_params(params_path)
    days, dow = month_calendar(p["year"], p["month"])

    names, gof, quota, leave, skipped = load_people(xlsx_path, days)
    req, warn = load_requests(xlsx_path, names, days)
    grid = load_grid(xlsx_path, names, days)

    # JSON 里的日期键是字符串，转成引擎用的 int
    def _norm(d):
        return {n: {int(k): SHIFT_ALIAS.get(v, v) for k, v in days_.items()}
                for n, days_ in d.items() if n in names}

    cfg = Config(
        year=p["year"], month=p["month"], days=days, dow=dow, off_quota=quota,
        prev_tail={n: v for n, v in p["prev_tail"].items() if n in names},
        all_hands_days=[d for d in p["all_hands_days"] if 1 <= d <= days],
        min_early=p["min_early"], min_late=p["min_late"],
        max_late_over_early=p["max_late_over_early"],
        weekend_min_on=p["weekend_min_on"], max_consec=p["max_consec"],
        soft_max_consec=p["soft_max_consec"],
        max_consec_off_runs=p["max_consec_off_runs"],
        balance_tol=p["balance_tol"], mid_soft_cap=p["mid_soft_cap"],
        max_off_run=p["max_off_run"], max_mid_per_person=p["max_mid_per_person"],
        max_requested_off_run=p["max_requested_off_run"],
        mgmt_avoid_weekend_off=p["mgmt_avoid_weekend_off"],
        weights=p["weights"],
    )
    cfg.annual_leave = leave
    cfg.requested_off = {}
    for n, dd in req.items():
        offs = [d for d, sh in dd.items() if sh == OFF]
        if offs:
            cfg.requested_off[n] = sorted(offs)
    cfg.locked = _norm(p["locked"])
    cfg.forbidden = _norm(p["forbidden"])

    if verbose:
        print(f"[加载] {p['year']}年{p['month']}月 共 {days} 天 | 在编 {len(names)} 人")
        for g in (GROUP_MGMT, GROUP_SMART, GROUP_SALES):
            who = [n for n in names if gof[n] == g]
            print(f"        {g}: {len(who)} 人  {'、'.join(who)}")
        if skipped:
            print(f"        不排班（已过滤）: {'、'.join(skipped)}")
        if leave:
            for n, ds in leave.items():
                print(f"        年假: {n} {ds[0]}~{ds[-1]}日 共 {len(ds)} 天"
                      f"（休息额度已自动加到 {quota[n]} 天）")
        ah = (", ".join(f"{d}日" for d in cfg.all_hands_days)
              if cfg.all_hands_days else "无  <<< 本月未设置任何全员上班日，确认是否遗漏")
        print(f"        全员上班日: {ah} | "
              f"需求 {sum(len(v) for v in req.values())} 条 | "
              f"锁定 {sum(len(v) for v in cfg.locked.values())} 格 | "
              f"禁止 {sum(len(v) for v in cfg.forbidden.values())} 格")
        for w in warn:
            print(f"  [警告] {w}")
        if grid is None:
            print("        班表为空，将从零生成")

    human = Roster(names, gof, cfg, grid) if grid else None
    return cfg, names, gof, req, human
