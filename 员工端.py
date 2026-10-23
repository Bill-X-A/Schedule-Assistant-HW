"""员工端：填写下个月的排班需求。手机上用。

需求结构：
    需求 1、2：可以多选日期（每个最多 3 天），同一个班次。适合"15、16 号想连休"这种
    需求 3、4：各选一天
    合计最多 8 天。数据层里仍是一天一行，引擎感知不到表单怎么填的。

运行（本地）:
    streamlit run 员工端.py
    没有 .streamlit/secrets.toml 时自动用本地文件 本地数据.json，
    先跑一次 python3 导入初始数据.py 生成测试数据。
"""
import calendar
import datetime
import random

import streamlit as st

from roster_io import DEFAULT_PARAMS, RosterDataError
from roster_store import (active_names, get_params, get_requests, month_key,
                          replace_requests, store_from_secrets)

SHIFT_OPTIONS = ["休", "早", "中", "晚"]     # 存进飞书时就是这几个字，引擎认得
MULTI_NEEDS = 2                              # 前几个需求可以多选
MULTI_MAX = 3                                # 多选需求每个最多几天
SINGLE_NEEDS = 2                             # 后几个需求只能选一天
MAX_OFF_RUN = DEFAULT_PARAMS["max_off_run"]  # 单次连休上限，和引擎保持一致
WEEKDAY = ["一", "二", "三", "四", "五", "六", "日"]


# ================================================================ 管道

st.set_page_config(page_title="排班需求填写", page_icon="📅", layout="centered")


@st.cache_resource
def get_store():
    """连数据层。@st.cache_resource 让它只连一次，不会每次点按钮都重连飞书。"""
    return store_from_secrets(st.secrets)


try:
    store = get_store()
except RosterDataError as e:
    st.error(f"系统未配置好，请联系人事专员。\n\n{e}")
    st.stop()


def target_month():
    """员工填的是【下个月】的需求。返回 (年, 月, 当月天数)。"""
    today = datetime.date.today()
    if today.month == 12:
        y, m = today.year + 1, 1
    else:
        y, m = today.year, today.month + 1
    return y, m, calendar.monthrange(y, m)[1]


def fmt_day(d):
    """15 -> '15日(周四)'"""
    return f"{d}日(周{WEEKDAY[calendar.weekday(year, month, d)]})"


def runs_over(days_list, limit):
    """找出连续超过 limit 天的段。[5,6,7,10] 且 limit=2 -> [[5,6,7]]"""
    out, cur = [], []
    for d in sorted(days_list):
        if cur and d == cur[-1] + 1:
            cur.append(d)
        else:
            if len(cur) > limit:
                out.append(cur)
            cur = [d]
    if len(cur) > limit:
        out.append(cur)
    return out


year, month, days = target_month()
mk = month_key(year, month)
ALL_DAYS = list(range(1, days + 1))

st.title(f"{month}月排班需求")
st.caption(f"需求 1、2 可以多选日期（每个最多 {MULTI_MAX} 天），需求 3、4 各选一天。"
           f"重复提交以最后一次为准。")

try:
    names = active_names(store)
except Exception as e:
    st.error(f"读取人员名单失败，请稍后再试或联系人事专员。\n\n{e}")
    st.stop()


# ================================================================ 第 1 步：选两次名字
# 第二个下拉框的顺序是打乱的：顺序一样的话，点错的人凭手感第二次多半
# 还点同一个位置，核对就形同虚设。打乱的结果在同一次打开页面里保持不变。

if "shuffled" not in st.session_state:
    shuffled = list(names)
    random.shuffle(shuffled)
    st.session_state["shuffled"] = shuffled

name1 = st.selectbox("选择你的名字", names, index=None, placeholder="请选择")
name2 = st.selectbox("再确认一次你的名字", st.session_state["shuffled"],
                     index=None, placeholder="请再选一次（顺序已打乱）")

if not name1 or not name2:
    st.stop()
if name1 != name2:
    st.error("两次选择的名字不一致，请重新确认。")
    st.stop()

name = name1


# ================================================================ 第 2 步：显示已提交的需求

try:
    all_req = get_requests(store, mk)
except Exception as e:
    st.error(f"读取需求失败：{e}")
    st.stop()

mine = []
for r in all_req:
    if r["姓名"] == name:
        mine.append(r)

if mine:
    mine.sort(key=lambda r: r["日"] or 0)
    parts = []
    for r in mine:
        parts.append(f"{fmt_day(r['日'])} {r['期望班次']}")
    st.info("你已提交：" + "、".join(parts) + "\n\n再次提交会覆盖以上内容。")


# ================================================================ 第 3 步：四组输入

# 人事专员如果已经设好本月参数，提前告诉员工全员上班日和休息天数
try:
    prm = get_params(store, mk)
except Exception:
    prm = None
all_hands = prm["全员上班日"] if prm else []
my_quota = None
if prm and prm["休息额度"]:
    adjust = prm["设置"].get("额度调整", {})
    my_quota = int(adjust.get(name, prm["休息额度"]))

notes = []
if all_hands:
    labels = []
    for d in all_hands:
        labels.append(fmt_day(d))
    notes.append("全员上班日：" + "、".join(labels) + "，这几天不能申请休息")
if my_quota is not None:
    notes.append(f"你本月休息 {my_quota} 天")
notes.append(f"连续休息最多 {MAX_OFF_RUN} 天")
st.warning("；".join(notes) + "。")

entries = []        # [(需求编号, [日...], 班次 或 None)]

for i in range(MULTI_NEEDS):
    st.markdown(f"**需求 {i + 1}**（可多选，最多 {MULTI_MAX} 天）")
    picked = st.multiselect("日期", ALL_DAYS, format_func=fmt_day, max_selections=MULTI_MAX,
                            key=f"days_{i}", placeholder="选择日期",
                            label_visibility="collapsed")
    shift = st.radio("班次", SHIFT_OPTIONS, index=None, horizontal=True,
                     key=f"shift_{i}", label_visibility="collapsed")
    entries.append((i + 1, list(picked), shift))

for j in range(SINGLE_NEEDS):
    i = MULTI_NEEDS + j
    st.markdown(f"**需求 {i + 1}**")
    day = st.selectbox("日期", ALL_DAYS, index=None, format_func=fmt_day,
                       key=f"day_{i}", placeholder="选择日期",
                       label_visibility="collapsed")
    shift = st.radio("班次", SHIFT_OPTIONS, index=None, horizontal=True,
                     key=f"shift_{i}", label_visibility="collapsed")
    entries.append((i + 1, [day] if day else [], shift))


# ================================================================ 第 4 步：检查

problems = []
items = []
seen = {}            # 日 -> 第几个需求，用来查重复
off_days = []        # 所有申请休息的日子，用来查连休

for no, picked, shift in entries:
    if not picked and shift is None:
        continue                                        # 这一组没填，跳过
    if not picked:
        problems.append(f"需求 {no} 选了班次但没选日期")
        continue
    if shift is None:
        problems.append(f"需求 {no} 选了日期但没选班次")
        continue
    for d in sorted(picked):
        if d in seen:
            problems.append(f"{fmt_day(d)} 在需求 {seen[d]} 和需求 {no} 里都填了")
            continue
        seen[d] = no
        if shift == "休" and d in all_hands:
            problems.append(f"{fmt_day(d)} 是全员上班日，不能申请休息")
            continue
        if shift == "休":
            off_days.append(d)
        items.append({"日": d, "期望班次": shift})

for run in runs_over(off_days, MAX_OFF_RUN):
    problems.append(f"{fmt_day(run[0])} 到 {fmt_day(run[-1])} 连续休息 {len(run)} 天，"
                    f"最多只能连休 {MAX_OFF_RUN} 天")
if my_quota is not None and len(off_days) > my_quota:
    problems.append(f"申请休息 {len(off_days)} 天，超过你本月的休息天数 {my_quota} 天")
if not items and not problems:
    problems.append("还没有填写任何需求")

for p in problems:
    if p == "还没有填写任何需求":
        st.caption(p)              # 刚打开页面时的正常状态，不用红字吓人
    else:
        st.error(p)
ok = len(problems) == 0


# ================================================================ 第 5 步：提交

if st.button("提交", type="primary", disabled=not ok, use_container_width=True):
    try:
        replace_requests(store, mk, name, items)
    except Exception as e:
        st.error(f"提交失败，请稍后再试：{e}")
        st.stop()
    lines = []
    items.sort(key=lambda it: it["日"])
    for it in items:
        lines.append(f"- {fmt_day(it['日'])}　{it['期望班次']}")
    st.success(f"{name}，已提交 {len(items)} 天的需求：\n\n" + "\n".join(lines))
    st.caption("如需修改，重新填写后再次提交即可。")
