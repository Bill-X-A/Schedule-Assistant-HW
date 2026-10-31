"""人事端：设置本月参数、查看需求、锁定安排、生成班表、上传定稿。电脑上用。

运行（本地）:
    streamlit run 人事端.py
    本地没配密码和飞书时，自动用 本地数据.json 且不要求密码（仅限本地开发）。

线上部署需要在 Secrets 里额外加一行：
    admin_password = "你设的密码"
"""
import calendar
import datetime
import io

import pandas as pd
import streamlit as st

from roster_io import (DEFAULT_PARAMS, SHIFT_ALIAS, STATUS_ACTIVE, RosterDataError,
                       parse_leave)
from roster_rules import (CN, EARLY, GROUP_MGMT, HARD, LATE, MID, OFF, Roster, check_all,
                          feasibility_precheck, score, solve)
from roster_store import (DRAFT, FINAL, LocalStore, get_habits, get_params, get_requests,
                          get_result, list_people, load_month, month_key, parse_roster_excel,
                          save_params, save_result, store_from_secrets)
from solve_roster import export_xlsx, report_unmet

WEEKDAY = ["一", "二", "三", "四", "五", "六", "日"]
SHIFT_CN = {"早": EARLY, "中": MID, "晚": LATE, "休": OFF}       # 中文 -> 代码
# 人事安排格子里的选项：空=不管；"早"=必须早班；"非早"=不能排早班
LOCK_OPTIONS = ["", "早", "中", "晚", "休", "非早", "非中", "非晚", "非休"]

st.set_page_config(page_title="排班系统 · 人事端", page_icon="🗓️", layout="wide")


# ================================================================ 连接与登录

@st.cache_resource
def get_store():
    return store_from_secrets(st.secrets)


try:
    store = get_store()
except RosterDataError as e:
    st.error(str(e))
    st.stop()


def require_password():
    """线上必须有密码；本地开发（用本地文件）且没配密码时放行。
    同样的原则：线上忘了配密码，宁可打不开，也不要对所有人敞开。"""
    try:
        pw = st.secrets["admin_password"]
    except Exception:
        pw = None
    if pw is None:
        if isinstance(store, LocalStore):
            st.sidebar.caption("本地开发模式：未设密码")
            return
        st.error("人事端未设置密码。请在 Streamlit Cloud 的 Secrets 里加一行 admin_password。")
        st.stop()
    if st.session_state.get("authed"):
        return
    st.title("排班系统 · 人事端")
    entered = st.text_input("密码", type="password")
    if entered == pw:
        st.session_state["authed"] = True
        st.rerun()
    elif entered:
        st.error("密码错误")
    st.stop()


require_password()


def safe(fn, *args, **kwargs):
    """读写飞书都过这一层。飞书报错时把我们写好的中文说明显示在页面上，
    而不是抛给 Streamlit——线上 Streamlit 会把报错原文隐藏掉，人事专员只能看到
    一句"This app has encountered an error"，什么都判断不了。"""
    try:
        return fn(*args, **kwargs)
    except RosterDataError as e:
        st.error(f"读写飞书出错：\n\n{e}")
        st.caption("常见原因：多维表格的字段名或字段类型和要求不一致；"
                   "飞书应用没有这个多维表格的编辑权限。")
        st.stop()


# ================================================================ 选月份

def fmt_day(y, m, d):
    return f"{d}日(周{WEEKDAY[calendar.weekday(y, m, d)]})"


today = datetime.date.today()
choices = []
for offset in (0, 1):
    y, m = today.year, today.month + offset
    if m > 12:
        y, m = y + 1, m - 12
    choices.append((y, m))

pick = st.sidebar.radio("排哪个月", choices, index=1,
                        format_func=lambda ym: f"{ym[0]}年{ym[1]}月")
year, month = pick
days = calendar.monthrange(year, month)[1]
mk = month_key(year, month)

st.title(f"{year}年{month}月排班")

people = safe(list_people, store)
names = []
group_of = {}
for p in people:
    if p["状态"] == STATUS_ACTIVE:
        names.append(p["姓名"])
        group_of[p["姓名"]] = p["组别"]

prm = safe(get_params, store, mk)
settings = prm["设置"] if prm else {}

st.sidebar.markdown("---")
st.sidebar.write(f"在编 **{len(names)}** 人")
st.sidebar.write("本月参数：" + ("✅ 已设置" if prm else "⚠️ 未设置"))
st.sidebar.write("本月定稿：" + ("✅ 已上传" if safe(get_result, store, mk, FINAL) else "— 暂无"))


def save_settings(**changes):
    """只改"设置"里的某几项，其余原样保留。几个页签各管一部分，互不覆盖。"""
    cur = safe(get_params, store, mk)
    if cur is None:
        st.error("请先在「本月设置」里保存全员上班日和休息额度。")
        return False
    new = dict(cur["设置"])
    new.update(changes)
    safe(save_params, store, mk, cur["全员上班日"], cur["休息额度"], new)
    return True


tab_set, tab_req, tab_lock, tab_gen, tab_final = st.tabs(
    ["① 本月设置", "② 需求一览", "③ 人事安排", "④ 生成排班", "⑤ 上传定稿"])


# ================================================================ ① 本月设置
# 全员上班日、本月休息额度、个别人额度调整、年假。

with tab_set:
    all_days = list(range(1, days + 1))
    ah = st.multiselect("全员上班日（三薪、首销、月末小周六）", all_days,
                        default=prm["全员上班日"] if prm else [],
                        format_func=lambda d: fmt_day(year, month, d))
    quota = st.number_input("本月休息额度（一般员工）", min_value=0, max_value=days,
                            value=int(prm["休息额度"]) if prm and prm["休息额度"] else 8)

    st.markdown("**个别人额度调整**（和一般额度不同的人，比如有调休）")
    adj_rows = []
    for n, v in settings.get("额度调整", {}).items():
        adj_rows.append({"姓名": n, "天数": int(v)})
    adj_df = st.data_editor(
        pd.DataFrame(adj_rows, columns=["姓名", "天数"]), num_rows="dynamic", key=f"adj_{mk}",
        column_config={"姓名": st.column_config.SelectboxColumn(options=names, required=True),
                       "天数": st.column_config.NumberColumn(min_value=0, max_value=days,
                                                           required=True)})

    st.markdown("**年假**（额外的休息，不占一般额度；管理组的年假请手工处理）")
    non_mgmt = []
    for n in names:
        if group_of[n] != GROUP_MGMT:
            non_mgmt.append(n)
    leave_rows = []
    for n, v in settings.get("年假", {}).items():
        leave_rows.append({"姓名": n, "日期": v})
    leave_df = st.data_editor(
        pd.DataFrame(leave_rows, columns=["姓名", "日期"]), num_rows="dynamic", key=f"leave_{mk}",
        column_config={"姓名": st.column_config.SelectboxColumn(options=non_mgmt, required=True),
                       "日期": st.column_config.TextColumn(help="例如 10-14，或 3,10-14",
                                                         required=True)})

    if st.button("保存本月设置", type="primary"):
        problems = []
        adjust = {}
        for _, r in adj_df.iterrows():
            if pd.notna(r["姓名"]) and pd.notna(r["天数"]):
                adjust[r["姓名"]] = int(r["天数"])
        leave = {}
        for _, r in leave_df.iterrows():
            if pd.isna(r["姓名"]) or pd.isna(r["日期"]):
                continue
            try:
                parse_leave(r["日期"], days)
                leave[r["姓名"]] = str(r["日期"]).strip()
            except ValueError as e:
                problems.append(f"{r['姓名']} 的年假日期有误：{e}")
        if problems:
            for p in problems:
                st.error(p)
        else:
            new = dict(settings)
            new["额度调整"] = adjust
            new["年假"] = leave
            safe(save_params, store, mk, ah, quota, new)
            st.success("已保存")
            st.rerun()


# ================================================================ ② 需求一览

with tab_req:
    reqs = safe(get_requests, store, mk)
    submitted = set()
    rows = []
    for r in sorted(reqs, key=lambda r: (r["姓名"], r["日"] or 0)):
        submitted.add(r["姓名"])
        day_txt = fmt_day(year, month, r["日"]) if r["日"] else "?"
        rows.append({"姓名": r["姓名"], "日期": day_txt, "期望班次": r["期望班次"]})
    c1, c2 = st.columns([3, 1])
    with c1:
        st.markdown(f"**已收到 {len(rows)} 条需求，来自 {len(submitted)} 人**")
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    with c2:
        missing = []
        for n in names:
            if n not in submitted:
                missing.append(n)
        st.markdown(f"**未提交（{len(missing)} 人）**")
        st.write("、".join(missing) if missing else "全部已提交")

    # 个人习惯：长期有效，员工在员工端自己填
    habits = safe(get_habits, store)
    h_rows = []
    for n in names:
        h = habits.get(n)
        if h:
            h_rows.append({"姓名": n, "最多连上": h["最多连上"],
                           "休息方式": h["休息方式"], "周末休息": h["周末休息"]})
    st.markdown(f"**个人习惯（{len(h_rows)}/{len(names)} 人填过，长期有效）**")
    if h_rows:
        st.dataframe(pd.DataFrame(h_rows), hide_index=True, use_container_width=True)
        n_wk = 0
        for r in h_rows:
            if r["周末休息"] != "无所谓":
                n_wk += 1
        if n_wk:
            st.caption(f"{n_wk} 人希望每周休一个周末日。周末在岗有下限，人多时不可能人人满足。")


# ================================================================ ③ 人事安排（三态锁格子）
# 每格：空=不管；早/中/晚/休=必须排；非早/非中/非晚/非休=不能排。
# 每改一格页面就会重跑，下面的冲突检查随之刷新。

def lock_grid_from_settings():
    """把设置里的锁定/禁止转成表格：行=人，列=日。"""
    grid = {}
    for n in names:
        grid[n] = [""] * days
    for n, dd in settings.get("锁定", {}).items():
        if n in grid:
            for k, code in dd.items():
                grid[n][int(k) - 1] = CN.get(SHIFT_ALIAS.get(code, code), "")
    for n, dd in settings.get("禁止", {}).items():
        if n in grid:
            for k, code in dd.items():
                cn = CN.get(SHIFT_ALIAS.get(code, code), "")
                if cn and not grid[n][int(k) - 1]:
                    grid[n][int(k) - 1] = "非" + cn
    cols = [str(d) for d in range(1, days + 1)]
    return pd.DataFrame([grid[n] for n in names], index=names, columns=cols)


def lock_dicts_from_grid(df):
    """表格 -> (锁定, 禁止)，格式 {姓名: {"15": "SFT6"}}，可直接存进 JSON。"""
    locked, forbidden = {}, {}
    for n in df.index:
        for col in df.columns:
            v = df.at[n, col]
            if not v or pd.isna(v):
                continue
            if v.startswith("非"):
                forbidden.setdefault(n, {})[col] = SHIFT_CN[v[1:]]
            else:
                locked.setdefault(n, {})[col] = SHIFT_CN[v]
    return locked, forbidden


with tab_lock:
    if prm is None:
        st.info("先在「① 本月设置」里保存全员上班日和休息额度。")
    else:
        st.caption("空=不管；早/中/晚/休=必须排这个班；非早/非中/非晚/非休=不能排这个班。")
        colcfg = {}
        for d in range(1, days + 1):
            colcfg[str(d)] = st.column_config.SelectboxColumn(
                label=f"{d}{WEEKDAY[calendar.weekday(year, month, d)]}",
                options=LOCK_OPTIONS, width="small")
        edited = st.data_editor(lock_grid_from_settings(), column_config=colcfg,
                                use_container_width=True, key=f"locks_{mk}")
        locked, forbidden = lock_dicts_from_grid(edited)

        # 用当前格子里的锁定跑一次预检（还没保存也能看到冲突）
        try:
            cfg, nm, gof, _, _, warns = load_month(store, year, month, verbose=False)
            cfg.locked = {}
            for n, dd in locked.items():
                cfg.locked[n] = {int(k): v for k, v in dd.items()}
            cfg.forbidden = {}
            for n, dd in forbidden.items():
                cfg.forbidden[n] = {int(k): v for k, v in dd.items()}
            msgs = feasibility_precheck(nm, gof, cfg)
        except RosterDataError as e:
            msgs, warns = [f"【数据错误】{e}"], []
        bad = [m for m in msgs if "冲突" in m or "不可行" in m or "致命" in m or "错误" in m]
        tight = [m for m in msgs if m not in bad]
        if bad:
            for m in bad:
                st.error(m)
        else:
            st.success("当前锁定没有发现冲突")
        for m in tight + warns:
            st.warning(m)

        n_lock = sum(len(v) for v in locked.values())
        n_forb = sum(len(v) for v in forbidden.values())
        if st.button(f"保存人事安排（必须 {n_lock} 格，禁止 {n_forb} 格）", type="primary"):
            if save_settings(锁定=locked, 禁止=forbidden):
                st.success("已保存")


# ================================================================ ④ 生成排班

def preview_df(grid, names_):
    rows = []
    for n in names_:
        row = []
        for v in grid[n]:
            row.append(CN.get(v, v))
        rows.append(row)
    cols = [f"{d}{WEEKDAY[calendar.weekday(year, month, d)]}" for d in range(1, days + 1)]
    return pd.DataFrame(rows, index=names_, columns=cols)


def color_cell(v):
    return {"早": "background-color:#FFF2CC", "中": "background-color:#C6E0B4",
            "晚": "background-color:#D9E1F2"}.get(v, "")


def styled(df):
    """给预览表上色。pandas 2.1 起叫 .map，更早的版本叫 .applymap。"""
    try:
        return df.style.map(color_cell)
    except AttributeError:
        return df.style.applymap(color_cell)


with tab_gen:
    if prm is None:
        st.info("先在「① 本月设置」里保存全员上班日和休息额度。")
    else:
        saved_w = dict(DEFAULT_PARAMS["weights"])
        saved_w.update(settings.get("权重", {}))
        with st.expander("权重（平时不用动）"):
            new_w = {}
            wcols = st.columns(4)
            i = 0
            for k in sorted(saved_w):
                with wcols[i % 4]:
                    new_w[k] = st.number_input(k, min_value=0, max_value=200,
                                               value=int(saved_w[k]), key=f"w_{k}")
                i += 1
            secs = st.slider("求解时间上限（秒）", 10, 120, 60)

        if st.button("生成排班", type="primary"):
            try:
                cfg, nm, gof, req, _, warns = load_month(store, year, month, verbose=False)
            except RosterDataError as e:
                st.error(str(e))
                st.stop()
            cfg.weights.update(new_w)
            pre = feasibility_precheck(nm, gof, cfg)
            blocking = [m for m in pre if "冲突" in m or "不可行" in m or "致命" in m]
            if blocking:
                st.error("有冲突，先到「③ 人事安排」或「① 本月设置」处理：")
                for m in blocking:
                    st.error(m)
                st.info("如果这是有意的安排（比如店长顶班、某天接受人手紧张）：\n\n"
                        "1. 在「③ 人事安排」里把这几格清空并保存\n"
                        "2. 回来正常生成、下载 Excel\n"
                        "3. 在 Excel 里手工改成你要的样子\n"
                        "4. 到「⑤ 上传定稿」上传，勾选「我已知晓」后存为定稿")
                st.stop()
            with st.spinner("排班中……"):
                sol, status = solve(nm, gof, cfg, requests=req, max_seconds=secs)
            if sol is None:
                st.error(f"排不出来：{status}")
                st.stop()
            vs = check_all(sol, requests=req)
            h, s = score(vs)
            unmet = report_unmet(sol, req) if req else []
            safe(save_result, store, mk, sol.grid, DRAFT)
            save_settings(权重=new_w)
            counts = {}
            for v in vs:
                counts[v.rule] = counts.get(v.rule, 0) + 1
            st.session_state[f"result_{mk}"] = {
                "grid": sol.grid, "names": nm, "status": status, "hard": h, "soft": s,
                "rules": counts, "req": req, "unmet": unmet, "warns": warns,
                "vs": [str(v) for v in vs if v.level == HARD]}

        res = st.session_state.get(f"result_{mk}")
        if res:
            total_req = sum(len(v) for v in res["req"].values())
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("求解状态", res["status"])
            c2.metric("硬约束违规", res["hard"])
            c3.metric("软约束罚分", res["soft"])
            c4.metric("需求满足", f"{total_req - len(res['unmet'])}/{total_req}")
            for w in res["warns"]:
                st.warning(w)
            for v in res["vs"]:
                st.error(v)

            st.dataframe(styled(preview_df(res["grid"], res["names"])),
                         use_container_width=True)

            if res["unmet"]:
                st.markdown("**未满足的需求**")
                rows = []
                for n, d1, want, got, why in res["unmet"]:
                    rows.append({"姓名": n, "日期": fmt_day(year, month, d1),
                                 "想要": CN[want], "实排": CN[got], "原因": "；".join(why)})
                st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

            # 导出：和命令行版同一个函数，格式、红字、批注完全一致
            cfg, nm, gof, req, _, _ = load_month(store, year, month, verbose=False)
            buf = io.BytesIO()
            export_xlsx(Roster(nm, gof, cfg, res["grid"]), None, buf,
                        {"soft": res["soft"], "rules": res["rules"]},
                        requests=res["req"], unmet=res["unmet"])
            st.download_button("下载 Excel", buf.getvalue(),
                               file_name=f"{year}年{month}月排班.xlsx", type="primary",
                               mime="application/vnd.openxmlformats-officedocument."
                                    "spreadsheetml.sheet")
            st.caption("已自动保存为草稿。下载后手工调整，改好的版本到「⑤ 上传定稿」。")


# ================================================================ ⑤ 上传定稿
# 人事手工改完的 Excel 才是真正执行的班表。传回来存为定稿，
# 下个月的跨月数据（连上天数、晚接早）就从这里取。

with tab_final:
    if safe(get_result, store, mk, FINAL):
        st.info("本月已有定稿。再次上传会替换它。")
    up = st.file_uploader("上传改好的排班 Excel", type=["xlsx"])
    if up is not None:
        try:
            cfg, nm, gof, req, _, _ = load_month(store, year, month, verbose=False)
        except RosterDataError as e:
            st.error(str(e))
            st.stop()
        grid, problems = parse_roster_excel(up, nm, days)
        if problems:
            for p in problems:
                st.error(p)
            st.stop()
        vs = check_all(Roster(nm, gof, cfg, grid))
        h, s = score(vs)
        hard = [v for v in vs if v.level == HARD]
        if hard:
            st.error(f"这份班表有 {len(hard)} 处违反硬性规则：")
            for v in hard:
                st.error(str(v))
        else:
            st.success(f"硬性规则全部满足。软约束罚分 {s}。")
        st.dataframe(styled(preview_df(grid, nm)), use_container_width=True)
        agree = True
        if hard:
            agree = st.checkbox("我已知晓以上问题，仍然存为定稿")
        if st.button("存为定稿", type="primary", disabled=not agree):
            safe(save_result, store, mk, grid, FINAL)
            st.success("已存为定稿。下个月排班会从这里读取跨月数据。")
