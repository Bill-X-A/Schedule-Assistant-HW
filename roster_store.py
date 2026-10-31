"""排班数据层 —— 员工端、人事端共用。

四张多维表格：
    人员档案   姓名 | 组别(单选) | 状态(单选)                        长期，人员变动才改
    需求       月份 | 姓名 | 日(数字) | 期望班次(单选) | 提交时间    员工端写，一条需求一行
    排班结果   月份 | 姓名 | 班次 | 状态(单选: 草稿/定稿)            一人一行，31个代码逗号连接
    月度参数   月份 | 全员上班日 | 休息额度(数字) | 设置              人事端写，一月一行

"设置"字段是一段 JSON，装程序才读写的东西：
    {"额度调整": {"徐博奇": 10}, "年假": {"刘冰": "10-14"},
     "锁定": {...}, "禁止": {...}, "权重": {...}, "阈值": {...}}

两套实现、同一组方法：
    FeishuStore  连真实飞书多维表格（部署用）
    LocalStore   存在本地 JSON 文件（开发、测试用，不需要网络）
应用代码只认这组方法，不关心背后是哪一套——和 roster_io 当初把 Excel 隔离出去是同一个思路。
"""

import json
import os
from typing import Dict, List, Optional

from roster_io import (DEFAULT_PARAMS, KNOWN_GROUPS, SHIFT_ALIAS, STATUS_ACTIVE,
                       INACTIVE_STATUS, KNOWN_STATUS,
                       RosterDataError, month_calendar, parse_leave)
from roster_rules import Config, Roster, GROUP_MGMT, OFF

T_PEOPLE, T_REQ, T_RESULT, T_PARAMS = "人员档案", "需求", "排班结果", "月度参数"
T_HABIT = "个人习惯"
ALL_TABLES = (T_PEOPLE, T_REQ, T_RESULT, T_PARAMS, T_HABIT)

# 个人习惯的选项。飞书里这三个字段是单选，选项文字必须和这里一致。
HABIT_CONSEC_OPTIONS = ["不在意", "4", "5", "6"]
HABIT_REST_OPTIONS = ["无所谓", "喜欢连休", "喜欢分散"]
HABIT_WEEKEND_OPTIONS = ["无所谓", "每周至少休一个周末日"]
_REST_CODE = {"喜欢连休": "block", "喜欢分散": "spread"}      # 表里的文字 -> 引擎认的代码
LOCAL_FILE = "本地数据.json"
DRAFT, FINAL = "草稿", "定稿"


def month_key(year: int, month: int) -> str:
    return f"{year}-{month:02d}"


def prev_month(year: int, month: int):
    if month == 1:
        return year - 1, 12
    return year, month - 1


# ================================================================ 本地实现

class LocalStore:
    """把四张表存成一个 JSON 文件。结构和飞书一一对应：每张表是一个记录列表，
    每条记录是 {"record_id": ..., "fields": {...}}。"""

    def __init__(self, path: str = LOCAL_FILE):
        self.path = path
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.db = json.load(f)
        else:
            self.db = {}
        for t in ALL_TABLES:                 # 老的本地文件没有「个人习惯」，补一张空表
            self.db.setdefault(t, [])
        self._next = 1 + sum(len(v) for v in self.db.values())

    def _save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.db, f, ensure_ascii=False, indent=1)

    # 下面四个是"原语"：读全表、批量新增、批量删除、更新一条。
    # FeishuStore 实现同样四个，上层所有逻辑都只用这四个。
    def _list(self, table):
        return list(self.db[table])

    def _create(self, table, fields_list):
        for fields in fields_list:
            self.db[table].append({"record_id": f"rec{self._next}", "fields": fields})
            self._next += 1
        self._save()

    def _delete(self, table, record_ids):
        drop = set(record_ids)
        self.db[table] = [r for r in self.db[table] if r["record_id"] not in drop]
        self._save()

    def _update(self, table, record_id, fields):
        for r in self.db[table]:
            if r["record_id"] == record_id:
                r["fields"].update(fields)
        self._save()


# ================================================================ 飞书实现

class FeishuStore:
    """连飞书多维表格。需要一个飞书自建应用（app_id / app_secret），
    并把它加为多维表格的协作者（可编辑）。

    tables 是 {表名: table_id}，table_id 在多维表格网址里 table= 后面那一串。
    """

    def __init__(self, app_id: str, app_secret: str, app_token: str, tables: Dict[str, str]):
        import lark_oapi as lark
        self.lark = lark
        self.client = (lark.Client.builder()
                       .app_id(app_id).app_secret(app_secret)
                       .log_level(lark.LogLevel.WARNING).build())
        self.app_token = app_token
        self.tables = tables
        for need in ALL_TABLES:
            if need not in tables:
                raise RosterDataError(f"飞书配置里缺少表「{need}」的 table_id")

    def _check(self, resp, what):
        if not resp.success():
            raise RosterDataError(
                f"飞书{what}失败：code={resp.code} msg={resp.msg}\n"
                f"请对照字段要求检查这张表：字段名要一字不差；"
                f"「休息额度」「日」是数字，「组别」「状态」「期望班次」是单选，其余都是文本。")

    def _list(self, table):
        """读整张表。表都很小（每月几十行），一次读完在本地筛，
        比在飞书端写筛选公式简单可靠。"""
        from lark_oapi.api.bitable.v1 import ListAppTableRecordRequest
        out, token = [], None
        while True:
            b = (ListAppTableRecordRequest.builder()
                 .app_token(self.app_token).table_id(self.tables[table]).page_size(500))
            if token:
                b = b.page_token(token)
            resp = self.client.bitable.v1.app_table_record.list(b.build())
            self._check(resp, f"读取「{table}」")
            for item in (resp.data.items or []):
                out.append({"record_id": item.record_id, "fields": item.fields or {}})
            if not resp.data.has_more:
                break
            token = resp.data.page_token
        return out

    def _create(self, table, fields_list):
        from lark_oapi.api.bitable.v1 import (AppTableRecord, BatchCreateAppTableRecordRequest,
                                              BatchCreateAppTableRecordRequestBody)
        for i in range(0, len(fields_list), 500):              # 单次最多 500 条
            chunk = fields_list[i:i + 500]
            recs = [AppTableRecord.builder().fields(f).build() for f in chunk]
            req = (BatchCreateAppTableRecordRequest.builder()
                   .app_token(self.app_token).table_id(self.tables[table])
                   .request_body(BatchCreateAppTableRecordRequestBody.builder()
                                 .records(recs).build())
                   .build())
            self._check(self.client.bitable.v1.app_table_record.batch_create(req),
                        f"写入「{table}」")

    def _delete(self, table, record_ids):
        from lark_oapi.api.bitable.v1 import (BatchDeleteAppTableRecordRequest,
                                              BatchDeleteAppTableRecordRequestBody)
        ids = list(record_ids)
        for i in range(0, len(ids), 500):
            req = (BatchDeleteAppTableRecordRequest.builder()
                   .app_token(self.app_token).table_id(self.tables[table])
                   .request_body(BatchDeleteAppTableRecordRequestBody.builder()
                                 .records(ids[i:i + 500]).build())
                   .build())
            self._check(self.client.bitable.v1.app_table_record.batch_delete(req),
                        f"删除「{table}」记录")

    def _update(self, table, record_id, fields):
        from lark_oapi.api.bitable.v1 import AppTableRecord, UpdateAppTableRecordRequest
        req = (UpdateAppTableRecordRequest.builder()
               .app_token(self.app_token).table_id(self.tables[table]).record_id(record_id)
               .request_body(AppTableRecord.builder().fields(fields).build())
               .build())
        self._check(self.client.bitable.v1.app_table_record.update(req), f"更新「{table}」")


# ================================================================ 字段取值

def _text(v) -> str:
    """飞书的文本字段读回来有时是字符串，有时是 [{"type":"text","text":"..."}] 这种分段格式。
    统一转成普通字符串。"""
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, list):
        parts = []
        for seg in v:
            if isinstance(seg, dict):
                parts.append(str(seg.get("text", "")))
            else:
                parts.append(str(seg))
        return "".join(parts).strip()
    if isinstance(v, dict):
        return str(v.get("text", "")).strip()
    return str(v).strip()


def _num(v) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(float(_text(v) if not isinstance(v, (int, float)) else v))
    except ValueError:
        return None


# ================================================================ 业务方法
# 下面这些函数只调用 store._list / _create / _delete / _update 四个原语，
# 所以 LocalStore 和 FeishuStore 都能用。

def list_people(store) -> List[dict]:
    """返回 [{"姓名","组别","状态"}]。员工端下拉框、人事端排班都用它。"""
    out = []
    for r in store._list(T_PEOPLE):
        f = r["fields"]
        name = _text(f.get("姓名"))
        if not name:
            continue
        out.append({"姓名": name,
                    "组别": _text(f.get("组别")),
                    "状态": _text(f.get("状态")) or "在编"})
    return out


def get_habits(store) -> Dict[str, dict]:
    """全部人的习惯，{姓名: {"最多连上": "5", "休息方式": "喜欢分散", "周末休息": "无所谓"}}。
    没填过的人不在里面。"""
    out = {}
    for r in store._list(T_HABIT):
        f = r["fields"]
        name = _text(f.get("姓名"))
        if not name:
            continue
        out[name] = {"最多连上": _text(f.get("最多连上")) or "不在意",
                     "休息方式": _text(f.get("休息方式")) or "无所谓",
                     "周末休息": _text(f.get("周末休息")) or "无所谓",
                     "record_id": r["record_id"]}
    return out


def save_habit(store, name: str, consec: str, rest: str, weekend: str):
    """保存某人的习惯：有就更新，没有就新建。一人一行。"""
    for value, options, label in ((consec, HABIT_CONSEC_OPTIONS, "最多连上"),
                                  (rest, HABIT_REST_OPTIONS, "休息方式"),
                                  (weekend, HABIT_WEEKEND_OPTIONS, "周末休息")):
        if value not in options:
            raise RosterDataError(f"「{label}」只能选：{'、'.join(options)}")
    fields = {"姓名": name, "最多连上": consec, "休息方式": rest, "周末休息": weekend}
    old = get_habits(store).get(name)
    if old:
        store._update(T_HABIT, old["record_id"], fields)
    else:
        store._create(T_HABIT, [fields])


def active_names(store) -> List[str]:
    """员工端下拉框用：只列状态为「在编」的人。离职、不排班的都不出现。"""
    names = []
    for p in list_people(store):
        if p["状态"] == STATUS_ACTIVE:
            names.append(p["姓名"])
    return names


def get_requests(store, mk: str) -> List[dict]:
    """某月全部需求，[{"姓名","日","期望班次","record_id"}]。"""
    out = []
    for r in store._list(T_REQ):
        f = r["fields"]
        if _text(f.get("月份")) != mk:
            continue
        out.append({"姓名": _text(f.get("姓名")),
                    "日": _num(f.get("日")),
                    "期望班次": _text(f.get("期望班次")),
                    "record_id": r["record_id"]})
    return out


MAX_REQUEST_DAYS = 8      # 员工端：前两个需求各最多 3 天 + 后两个各 1 天


def replace_requests(store, mk: str, name: str, items: List[dict]):
    """员工提交需求：先删掉他本月的旧需求，再写入新的——以最后一次提交为准。
    items = [{"日": 15, "期望班次": "休"}, ...]，一天一条，最多 MAX_REQUEST_DAYS 条。"""
    if len(items) > MAX_REQUEST_DAYS:
        raise RosterDataError(f"每人每月最多申请 {MAX_REQUEST_DAYS} 天")
    old_ids = []
    for r in get_requests(store, mk):
        if r["姓名"] == name:
            old_ids.append(r["record_id"])
    if old_ids:
        store._delete(T_REQ, old_ids)
    rows = []
    for it in items:
        rows.append({"月份": mk, "姓名": name,
                     "日": int(it["日"]), "期望班次": it["期望班次"]})
    if rows:
        store._create(T_REQ, rows)


def _find_params(store, mk: str):
    for r in store._list(T_PARAMS):
        if _text(r["fields"].get("月份")) == mk:
            return r
    return None


def get_params(store, mk: str) -> Optional[dict]:
    """返回 {"全员上班日": [..], "休息额度": 9, "设置": {...}}，本月还没设置则返回 None。"""
    r = _find_params(store, mk)
    if r is None:
        return None
    f = r["fields"]
    days = []
    for part in _text(f.get("全员上班日")).replace("，", ",").split(","):
        part = part.strip()
        if part:
            days.append(int(part))
    raw = _text(f.get("设置"))
    try:
        settings = json.loads(raw) if raw else {}
    except ValueError:
        raise RosterDataError(f"{mk} 的月度参数里「设置」不是合法的 JSON")
    return {"全员上班日": days, "休息额度": _num(f.get("休息额度")), "设置": settings}


def save_params(store, mk: str, all_hands: List[int], quota: int, settings: dict):
    fields = {"月份": mk,
              "全员上班日": ",".join(str(d) for d in sorted(all_hands)),
              "休息额度": int(quota),
              "设置": json.dumps(settings, ensure_ascii=False)}
    r = _find_params(store, mk)
    if r is None:
        store._create(T_PARAMS, [fields])
    else:
        store._update(T_PARAMS, r["record_id"], fields)


def get_result(store, mk: str, status: str = FINAL) -> Optional[Dict[str, List[str]]]:
    """读某月某状态的班表，{姓名: [31个代码]}；没有则 None。"""
    grid = {}
    for r in store._list(T_RESULT):
        f = r["fields"]
        if _text(f.get("月份")) == mk and _text(f.get("状态")) == status:
            codes = []
            for c in _text(f.get("班次")).split(","):
                codes.append(SHIFT_ALIAS.get(c.strip(), c.strip()))
            grid[_text(f.get("姓名"))] = codes
    return grid or None


def save_result(store, mk: str, grid: Dict[str, List[str]], status: str):
    """保存班表。同月同状态的旧记录先删掉——草稿只留最新一版，定稿只留一份。"""
    old_ids = []
    for r in store._list(T_RESULT):
        f = r["fields"]
        if _text(f.get("月份")) == mk and _text(f.get("状态")) == status:
            old_ids.append(r["record_id"])
    if old_ids:
        store._delete(T_RESULT, old_ids)
    rows = []
    for name, codes in grid.items():
        rows.append({"月份": mk, "姓名": name, "班次": ",".join(codes), "状态": status})
    store._create(T_RESULT, rows)


# ================================================================ 读上传的班表 Excel

def parse_roster_excel(file, names: List[str], days: int):
    """读一张班表 Excel，返回 ({姓名: [班次代码...]}, 问题列表)。

    兼容两种格式：
      人事端导出的：A 列姓名，B 列起是 1 号
      公司原来的表：A 列姓名，B 列是"当月已排OFF个数"，C 列起才是 1 号
    办法是在表头里找连着写 1、2、3 的那一行，从那一列开始读。
    写死"B 列开始"的话，用公司原表上传时每个人会整体错位一天，而且不报错。"""
    import pandas as pd
    df = pd.read_excel(file, sheet_name=0, header=None)

    start = None
    for r in range(min(6, df.shape[0])):
        for c in range(df.shape[1] - 2):
            try:
                trio = [int(float(df.iloc[r, c + k])) for k in range(3)]
            except (TypeError, ValueError):
                continue
            if trio == [1, 2, 3]:
                start = c
                break
        if start is not None:
            break
    if start is None:
        return {}, ["表头里找不到日期行（应有一行依次写着 1、2、3……）"]

    grid, problems = {}, []
    for r in range(df.shape[0]):
        n = df.iloc[r, 0]
        if not isinstance(n, str) or n.strip() not in names:
            continue
        n = n.strip()
        row = []
        for c in range(days):
            col = start + c
            raw = df.iloc[r, col] if col < df.shape[1] else None
            v = None
            if raw is not None and not (isinstance(raw, float) and raw != raw):   # 不是空格
                v = SHIFT_ALIAS.get(str(raw).strip())
            if v is None:
                problems.append(f"{n} {c + 1}日的内容「{raw}」无法识别")
                v = OFF
            row.append(v)
        grid[n] = row
    for n in names:
        if n not in grid:
            problems.append(f"文件里找不到「{n}」")
    return grid, problems


# ================================================================ 组装成引擎要的对象

def load_month(store, year: int, month: int, verbose: bool = True):
    """从数据层组装出引擎要的东西，返回值和 roster_io.load_all 完全一样：
        (cfg, names, group_of, requests, human_roster_or_None, warnings)
    多出来的 warnings 是给页面显示用的提示列表。"""
    mk = month_key(year, month)
    days, dow = month_calendar(year, month)
    warnings = []

    # ---- 人员 ----
    names, gof, bad = [], {}, []
    for p in list_people(store):
        if p["状态"] in INACTIVE_STATUS:
            continue
        if p["状态"] not in KNOWN_STATUS:
            bad.append(f"{p['姓名']}（状态「{p['状态']}」，只能是 在编 / 不排班 / 离职）")
            continue
        if p["组别"] not in KNOWN_GROUPS:
            bad.append(f"{p['姓名']}（组别「{p['组别']}」）")
            continue
        names.append(p["姓名"])
        gof[p["姓名"]] = p["组别"]
    if bad:
        raise RosterDataError("人员档案里有无法识别的内容：" + "、".join(bad))
    if not names:
        raise RosterDataError("人员档案里没有在编人员")

    # ---- 月度参数 ----
    prm = get_params(store, mk)
    if prm is None:
        raise RosterDataError(f"{mk} 还没有设置月度参数（全员上班日、休息额度）")
    if not prm["休息额度"]:
        raise RosterDataError(f"{mk} 的月度参数里没有填休息额度")
    st = prm["设置"]

    # 休息额度：本月统一值 + 个别调整 + 年假（年假是额外的）
    quota, leave = {}, {}
    adjust = st.get("额度调整", {})
    for n in names:
        quota[n] = int(adjust.get(n, prm["休息额度"]))
    for n, text in st.get("年假", {}).items():
        if n not in names:
            warnings.append(f"年假里的「{n}」不在在编名单中，已忽略")
            continue
        if gof[n] == GROUP_MGMT:
            raise RosterDataError(f"{n} 是管理组，管理组年假请手工安排，不要在系统里填")
        try:
            ds = parse_leave(text, days)
        except ValueError as e:
            raise RosterDataError(f"{n} 的年假日期有误：{e}")
        if ds:
            leave[n] = ds
            quota[n] += len(ds)

    # ---- 跨月：上月定稿的最后 7 天 ----
    py, pm = prev_month(year, month)
    last = get_result(store, month_key(py, pm), FINAL)
    prev_tail = {}
    if last is None:
        warnings.append(f"{py}年{pm}月没有定稿班表，本月的跨月约束（连上天数、晚接早）不生效")
    else:
        for n in names:
            if n in last:
                prev_tail[n] = last[n][-7:]
            else:
                warnings.append(f"上月定稿里没有「{n}」，此人跨月约束不生效")

    # ---- 需求 ----
    req = {}
    for r in get_requests(store, mk):
        n, d, s = r["姓名"], r["日"], SHIFT_ALIAS.get(r["期望班次"])
        if n not in names:
            warnings.append(f"需求里的「{n}」不在在编名单中，已跳过")
            continue
        if d is None or not 1 <= d <= days or s is None:
            warnings.append(f"{n} 有一条需求日期或班次无法识别，已跳过")
            continue
        req.setdefault(n, {})[d] = s

    # ---- 组装 Config ----
    p = dict(DEFAULT_PARAMS)
    # 和 roster_io.load_params 同样的规则：不认识的权重键提示并丢弃，
    # 免得两条加载路径对同一份数据给出不同的权重。
    known = DEFAULT_PARAMS["weights"]
    user_w = st.get("权重", {})
    stale = [k for k in user_w if k not in known]
    if stale:
        warnings.append(f"权重里这些键已不起作用，已忽略：{'、'.join(stale)}")
    p["weights"] = dict(known)
    for k, v in user_w.items():
        if k in known:
            p["weights"][k] = v
    for k, v in st.get("阈值", {}).items():
        if k in p:
            p[k] = v

    def _cells(d):
        out = {}
        for n, dd in d.items():
            if n in names:
                out[n] = {int(k): SHIFT_ALIAS.get(v, v) for k, v in dd.items()}
        return out

    cfg = Config(
        year=year, month=month, days=days, dow=dow, off_quota=quota,
        prev_tail=prev_tail,
        all_hands_days=[d for d in prm["全员上班日"] if 1 <= d <= days],
        min_early=p["min_early"], min_late=p["min_late"],
        max_late_over_early=p["max_late_over_early"],
        weekend_min_on=p["weekend_min_on"], max_consec=p["max_consec"],
        soft_max_consec=p["soft_max_consec"],
        max_consec_off_runs=p["max_consec_off_runs"],
        balance_tol=p["balance_tol"], mid_soft_cap=p["mid_soft_cap"],
        max_off_run=p["max_off_run"], max_mid_per_person=p["max_mid_per_person"],
        max_requested_off_run=p["max_requested_off_run"],
        max_smart_same_shift=p["max_smart_same_shift"],
        mgmt_avoid_weekend_off=p["mgmt_avoid_weekend_off"],
        weights=p["weights"],
    )
    cfg.annual_leave = leave
    cfg.requested_off = {}
    for n, dd in req.items():
        offs = [d for d, sh in dd.items() if sh == OFF]
        if offs:
            cfg.requested_off[n] = sorted(offs)
    # ---- 个人习惯：表里的文字 -> 引擎的参数 ----
    for n, h in get_habits(store).items():
        if n not in names:
            continue                                  # 离职、不排班的人的习惯不用管
        c = h["最多连上"]
        if c in HABIT_CONSEC_OPTIONS[1:]:
            cfg.habit_max_consec[n] = int(c)
        elif c != "不在意":
            warnings.append(f"{n} 的习惯「最多连上」是「{c}」，不认识，已按不在意处理")
        rest = h["休息方式"]
        if rest in _REST_CODE:
            cfg.habit_rest[n] = _REST_CODE[rest]
        elif rest != "无所谓":
            warnings.append(f"{n} 的习惯「休息方式」是「{rest}」，不认识，已按无所谓处理")
        if h["周末休息"] == HABIT_WEEKEND_OPTIONS[1]:
            cfg.habit_weekend.append(n)
        elif h["周末休息"] != "无所谓":
            warnings.append(f"{n} 的习惯「周末休息」是「{h['周末休息']}」，不认识，已按无所谓处理")

    cfg.locked = _cells(st.get("锁定", {}))
    cfg.forbidden = _cells(st.get("禁止", {}))
    if not cfg.all_hands_days:
        warnings.append("本月没有设置任何全员上班日，确认是否遗漏")

    draft = get_result(store, mk, DRAFT)
    human = Roster(names, gof, cfg, draft) if draft and all(n in draft for n in names) else None

    if verbose:
        print(f"[加载] {year}年{month}月 {days}天 | 在编 {len(names)} 人 | "
              f"需求 {sum(len(v) for v in req.values())} 条 | 全员日 {cfg.all_hands_days}")
        for w in warnings:
            print(f"  [提示] {w}")
    return cfg, names, gof, req, human, warnings


def store_from_secrets(secrets) -> object:
    """Streamlit 里用：st.secrets 有 [feishu] 段就连飞书；
    没有的话，只有本地确实存在测试数据文件才用本地文件，否则报错。

    为什么不直接回落到本地文件：部署到 Streamlit Cloud 时如果忘了配 secrets，
    员工照样能提交、页面照样显示成功，数据却写进云端一个临时文件，
    应用一休眠就丢了，谁都不知道。宁可报错停下。"""
    try:
        has_feishu = "feishu" in secrets
    except Exception:                      # 本地没有 secrets.toml 时，读 st.secrets 会抛异常
        has_feishu = False
    if has_feishu:
        fs = secrets["feishu"]
        return FeishuStore(fs["app_id"], fs["app_secret"], fs["app_token"], dict(fs["tables"]))
    if os.path.exists(LOCAL_FILE):
        return LocalStore()
    raise RosterDataError(
        "没有配置飞书连接（secrets 里缺少 [feishu]），本地也没有测试数据文件。\n"
        "线上部署：在 Streamlit Cloud 的 Settings → Secrets 里填飞书配置。\n"
        "本地开发：先运行 python3 导入初始数据.py 生成测试数据。")
