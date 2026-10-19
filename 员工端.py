"""员工端：填写下个月的排班需求。手机上用。

运行（本地）:
    streamlit run 员工端.py
    没有 .streamlit/secrets.toml 时自动用本地文件 本地数据.json，
    先跑一次 python3 导入初始数据.py 生成测试数据。
"""
import calendar
import datetime
import random

import streamlit as st

from roster_store import (LocalStore, active_names, get_params, get_requests, month_key,
                          replace_requests, store_from_secrets)

SHIFT_OPTIONS = ["休", "早", "中", "晚"]     # 存进飞书时就是这几个字，引擎认得
MAX_ITEMS = 3                                # 每人每月最多 3 条
WEEKDAY = ["一", "二", "三", "四", "五", "六", "日"]


# ================================================================ 管道

st.set_page_config(page_title="排班需求填写", page_icon="📅", layout="centered")


@st.cache_resource
def get_store():
    """连数据层。@st.cache_resource 让它只连一次，不会每次点按钮都重连飞书。"""
    try:
        return store_from_secrets(st.secrets)
    except Exception:
        return LocalStore()                  # 本地没有 secrets 文件时走这里


def target_month():
    """员工填的是【下个月】的需求。返回 (年, 月, 当月天数)。"""
    today = datetime.date.today()
    if today.month == 12:
        y, m = today.year + 1, 1
    else:
        y, m = today.year, today.month + 1
    return y, m, calendar.monthrange(y, m)[1]


def fmt_day(y, m, d):
    """15 -> '15日(周四)'"""
    return f"{d}日(周{WEEKDAY[calendar.weekday(y, m, d)]})"


store = get_store()
year, month, days = target_month()
mk = month_key(year, month)

st.title(f"{month}月排班需求")
st.caption(f"每人最多填 {MAX_ITEMS} 条，重复提交以最后一次为准。")

try:
    names = active_names(store)
except Exception as e:
    st.error(f"读取人员名单失败，请稍后再试或联系人事专员。\n\n{e}")
    st.stop()


# ================================================================ 第 1 步：选两次名字
# 第二个下拉框的顺序是打乱的。如果两个框顺序一样，点错的人凭手感
# 第二次多半还点同一个位置，核对就形同虚设。打乱后必须真的看着名字找。
# 打乱结果存进 session_state，同一次打开页面里顺序不变，不会每点一下就重排。

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
        parts.append(f"{fmt_day(year, month, r['日'])} {r['期望班次']}")
    st.info("你已提交：" + "、".join(parts) + "\n\n再次提交会覆盖以上内容。")


# ================================================================ 第 3 步：三组输入

# 如果人事专员已经设好了本月的全员上班日，提前提醒，免得员工在那几天申请休息
try:
    prm = get_params(store, mk)
except Exception:
    prm = None
all_hands = prm["全员上班日"] if prm else []
if all_hands:
    labels = []
    for d in all_hands:
        labels.append(fmt_day(year, month, d))
    st.warning("本月全员上班日：" + "、".join(labels) + "，这几天不能申请休息。")

st.subheader(f"填写需求（最多 {MAX_ITEMS} 条，不需要的留空）")

first_day = datetime.date(year, month, 1)
last_day = datetime.date(year, month, days)

entries = []              # [(第几组, 选的日期 或 None, 选的班次 或 None)]
for i in range(MAX_ITEMS):
    st.markdown(f"**需求 {i + 1}**")
    col_date, col_shift = st.columns([1, 1])
    with col_date:
        picked = st.date_input("日期", value=None, min_value=first_day, max_value=last_day,
                               key=f"date_{i}", label_visibility="collapsed",
                               format="YYYY/MM/DD")
    with col_shift:
        shift = st.radio("班次", SHIFT_OPTIONS, index=None, horizontal=True,
                         key=f"shift_{i}", label_visibility="collapsed")
    entries.append((i + 1, picked, shift))


# ================================================================ 第 4 步：检查

problems = []
items = []
used_days = []
for no, picked, shift in entries:
    if picked is None and shift is None:
        continue                                        # 这一组没填，跳过
    if picked is None:
        problems.append(f"需求 {no} 选了班次但没选日期")
        continue
    if shift is None:
        problems.append(f"需求 {no} 选了日期但没选班次")
        continue
    if shift == "休" and picked.day in all_hands:
        problems.append(f"需求 {no}：{fmt_day(year, month, picked.day)} 是全员上班日，不能申请休息")
        continue
    used_days.append(picked.day)
    items.append({"日": picked.day, "期望班次": shift})

if len(used_days) != len(set(used_days)):
    problems.append("同一天填了不止一次")
if not items and not problems:
    problems.append("还没有填写任何需求")

for p in problems:
    st.warning(p)
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
        lines.append(f"- {fmt_day(year, month, it['日'])}　{it['期望班次']}")
    st.success(f"{name}，已提交 {len(items)} 条需求：\n\n" + "\n".join(lines))
    st.caption("如需修改，重新填写后再次提交即可。")

st.title("Designed by Bill ")
st.title("Assisted by Lizulinglong & Zhaoshuang ")
