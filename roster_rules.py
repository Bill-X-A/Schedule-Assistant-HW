"""华为零售门店 月度排班 —— 规则引擎

全项目只有这一份规则定义：
    报警机制  -> check_all()            判断一张表合不合规
    求解器    -> solve()                生成一张合规的表
两者读同一份 Config，规则顺序一一对应（见下方"规则清单"），改规则只改一处对应位置。

班次编码沿用门店现有表格，不做转换：
    SFT6 = 早班   HTDAYE = 中班   CBGDAYK = 晚班   / = 休息

规则清单（检查函数 / 求解器约束 一一对应）
  硬约束——违反即无解
    QUOTA 每人休息天数 = 额度
    R11   全员上班日不得排休
    R5    个人消费+智慧办公 每日 早>=min_early、晚>=min_late（不含管理组）
    R3b   个人消费+智慧办公 每日 晚-早 <= max_late_over_early（与软约束 R3 合成区间）
    R6    管理组、智慧办公 每日各至少一早一晚
    R4    周末在岗 >= weekend_min_on
    R1    连续上班 <= max_consec 天（含跨月）
    R2    晚班次日不得排早班（含跨月）
    R10   个人 |早-晚| <= balance_tol
    R9b   单次连休 <= max_off_run 天（含跨月）
    R9    整月连休次数 <= max_consec_off_runs
    LOCK  人事：必须排 / 不能排
  软约束——违反计罚，目标函数最小化
    R3        销售口径每日 早<=晚
    R4b       周中在岗不超过周末最低值
    R7        连休前一天排早、后一天排晚
    R14       管理组尽量六日不休
    R15       尽量不上满 soft_max_consec 天
    R16       全员日前一天宜集中放休（仅提示，不计罚）
    MID       每日中班超出 mid_soft_cap 的部分
    SANDWICH  休-上-休 夹心，休息被一天班割裂
    SHORTRUN  只连上 2~3 天的一段，班次须统一（全早或全晚）
    REQUEST   员工需求，满足则减分
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

# ================================================================ 常量

EARLY, MID, LATE, OFF = "SFT6", "HTDAYE", "CBGDAYK", "/"
SHIFTS = [EARLY, MID, LATE, OFF]
WORK = [EARLY, MID, LATE]
CN = {EARLY: "早", MID: "中", LATE: "晚", OFF: "休"}
WEEKEND = ("六", "日")

HARD, SOFT = "HARD", "SOFT"

GROUP_MGMT = "管理组"
GROUP_SMART = "智慧办公"
GROUP_SALES = "个人消费"
R6_GROUPS = [GROUP_MGMT, GROUP_SMART]      # 人少，必须每天各有一早一晚
R3_GROUPS = [GROUP_SALES, GROUP_SMART]     # 销售口径 = 个人消费 + 智慧办公
R5_GROUPS = [GROUP_SALES, GROUP_SMART]     # R5 人力下限只数销售人员，管理组有自己的 R6

DEFAULT_WEIGHTS = {
    "R3": 10, "R4b": 1, "R7": 8, "R14": 12, "R15": 10,
    "MID": 8, "SANDWICH": 12, "SHORTRUN": 15, "REQUEST": 25,
}


# ================================================================ 数据结构

@dataclass
class Config:
    """一次排班的全部参数。这里只有数据，没有逻辑——全部由 roster_io 从外部读入。"""
    year: int
    month: int
    days: int
    dow: List[str]                                   # 长度 = days，'一'..'日'
    off_quota: Dict[str, int]                        # 每人休息额度

    prev_tail: Dict[str, List[str]] = field(default_factory=dict)   # 上月末7天
    all_hands_days: List[int] = field(default_factory=list)         # 全员上班日
    locked: Dict[str, Dict[int, str]] = field(default_factory=dict)      # 人事：必须排
    forbidden: Dict[str, Dict[int, str]] = field(default_factory=dict)   # 人事：不能排
    annual_leave: Dict[str, List[int]] = field(default_factory=dict)     # 年假日期(1-based)

    min_early: int = 4                               # R5
    min_late: int = 4                                # R5
    max_late_over_early: int = 2                     # R3b 每日晚班最多比早班多几人
    weekend_min_on: int = 12                         # R4
    max_consec: int = 7                              # R1
    balance_tol: int = 2                             # R10 早晚班差值容差
    max_off_run: int = 2                             # R9b 单次连休上限
    max_consec_off_runs: int = 2                     # R9  整月连休次数上限

    soft_max_consec: int = 5                         # R15
    mid_soft_cap: int = 2                            # MID 每日免罚额度
    mgmt_avoid_weekend_off: bool = False             # R14 按月开关

    weights: Dict[str, int] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))

    def weight(self, key: str) -> int:
        return self.weights.get(key, DEFAULT_WEIGHTS.get(key, 0))

    def is_weekend(self, d: int) -> bool:
        return self.dow[d] in WEEKEND

    def on_leave(self, name: str, d: int) -> bool:
        """第 d 天(0-based)是不是这个人的年假。"""
        return (d + 1) in self.annual_leave.get(name, ())

    def leave_days(self, name: str) -> int:
        return len(self.annual_leave.get(name, ()))

    def carry_work(self, name: str) -> int:
        """上月末连续上班了几天（跨月 R1 用）。"""
        c = 0
        for v in reversed(self.prev_tail.get(name, [])):
            if v == OFF:
                break
            c += 1
        return c

    def carry_off(self, name: str) -> int:
        """上月末连续休息了几天（跨月 R9b 用）。"""
        c = 0
        for v in reversed(self.prev_tail.get(name, [])):
            if v != OFF:
                break
            c += 1
        return c


@dataclass
class Roster:
    """一张班表。grid[姓名] 是长度 = days 的班次列表，索引 0 对应 1 号。"""
    names: List[str]
    group_of: Dict[str, str]
    cfg: Config
    grid: Dict[str, List[str]]

    def members(self, *groups) -> List[str]:
        return [n for n in self.names if self.group_of[n] in groups]

    def count(self, day: int, shift: str, who: Optional[List[str]] = None) -> int:
        return sum(1 for n in (who or self.names) if self.grid[n][day] == shift)

    def on_duty(self, day: int, who: Optional[List[str]] = None) -> int:
        return sum(1 for n in (who or self.names) if self.grid[n][day] != OFF)

    def is_weekend(self, day: int) -> bool:
        return self.cfg.is_weekend(day)

    def seq(self, name: str) -> List[str]:
        """上月末7天 + 本月，跨月规则用这个序列。"""
        return self.cfg.prev_tail.get(name, []) + self.grid[name]

    def offset(self, name: str) -> int:
        """seq 里本月 1 号所在的位置，用于把 seq 下标换算回日期。"""
        return len(self.cfg.prev_tail.get(name, []))


@dataclass
class Violation:
    rule: str
    level: str
    msg: str
    day: Optional[int] = None                        # 1-based，直接显示给人看
    person: Optional[str] = None
    penalty: int = 0

    def __str__(self):
        loc = "/".join(x for x in (self.person, f"{self.day}日" if self.day else None) if x)
        return f"[{self.level}] {self.rule} {loc}: {self.msg}"


def _off_runs(seq: List[str]):
    """把一段班次序列里的休息段切出来，返回 [(起始下标, 长度)]。"""
    runs, i = [], 0
    while i < len(seq):
        if seq[i] == OFF:
            j = i
            while j < len(seq) and seq[j] == OFF:
                j += 1
            runs.append((i, j - i))
            i = j
        else:
            i += 1
    return runs


# ================================================================ 硬约束检查

def c_quota(r: Roster) -> List[Violation]:
    """每人休息天数必须等于额度。"""
    out = []
    for n in r.names:
        got = sum(1 for v in r.grid[n] if v == OFF)
        want = r.cfg.off_quota[n]
        if got != want:
            out.append(Violation("QUOTA", HARD, f"休息 {got} 天，额度 {want} 天", person=n))
    return out


def c_r11_all_hands(r: Roster) -> List[Violation]:
    """R11 全员上班日（三薪、首销、月末小周六）不得排休。"""
    return [Violation("R11", HARD, "全员上班日排休", day=d1, person=n)
            for d1 in r.cfg.all_hands_days for n in r.names
            if 1 <= d1 <= r.cfg.days and r.grid[n][d1 - 1] == OFF]


def c_r5_daily_min(r: Roster) -> List[Violation]:
    """R5 个人消费+智慧办公 每日早班、晚班人数下限，不含管理组。
    管理组的覆盖由 R6 单独保证，不能拿来顶销售人员的人力。"""
    out, who = [], r.members(*R5_GROUPS)
    for d in range(r.cfg.days):
        e, l = r.count(d, EARLY, who), r.count(d, LATE, who)
        if e < r.cfg.min_early:
            out.append(Violation("R5", HARD, f"销售早班 {e} 人（需>={r.cfg.min_early}）", day=d + 1))
        if l < r.cfg.min_late:
            out.append(Violation("R5", HARD, f"销售晚班 {l} 人（需>={r.cfg.min_late}）", day=d + 1))
    return out


def c_r3b_late_early_gap(r: Roster) -> List[Violation]:
    """R3b 销售口径每日 晚 - 早 <= max_late_over_early。
    与软约束 R3（早 <= 晚）合起来，就是"每天晚班比早班多 0~2 人"：
    上沿硬（真实班表 30 天里只破过 1 天），下沿软（真实班表常让步）。"""
    out, cap, who = [], r.cfg.max_late_over_early, r.members(*R5_GROUPS)
    for d in range(r.cfg.days):
        e, l = r.count(d, EARLY, who), r.count(d, LATE, who)
        if l - e > cap:
            out.append(Violation("R3b", HARD, f"销售晚班 {l} 比早班 {e} 多 {l - e} 人（上限 {cap}）",
                                 day=d + 1))
    return out


def c_r6_group_coverage(r: Roster) -> List[Violation]:
    """R6 管理组、智慧办公每天各至少一个早班、一个晚班（可增不可减）。"""
    out = []
    for g in R6_GROUPS:
        who = r.members(g)
        if not who:
            continue
        for d in range(r.cfg.days):
            if r.count(d, EARLY, who) < 1:
                out.append(Violation("R6", HARD, f"{g}无早班在岗", day=d + 1))
            if r.count(d, LATE, who) < 1:
                out.append(Violation("R6", HARD, f"{g}无晚班在岗", day=d + 1))
    return out


def c_r4_weekend_min(r: Roster) -> List[Violation]:
    """R4 周末在岗人数下限（含管理组）。"""
    return [Violation("R4", HARD,
                      f"周末在岗 {r.on_duty(d)} 人（需>={r.cfg.weekend_min_on}）", day=d + 1)
            for d in range(r.cfg.days)
            if r.is_weekend(d) and r.on_duty(d) < r.cfg.weekend_min_on]


def c_r1_max_consecutive(r: Roster) -> List[Violation]:
    """R1 连续上班不超过 max_consec 天。含跨月：上月末的连班一并计入。"""
    out, K = [], r.cfg.max_consec
    for n in r.names:
        run, off = 0, r.offset(n)
        for i, v in enumerate(r.seq(n)):
            if v == OFF:
                run = 0
                continue
            run += 1
            if run == K + 1:
                out.append(Violation("R1", HARD, f"连续上班 {run} 天",
                                     person=n, day=max(1, i - off + 1)))
    return out


def c_r2_shift_adjacency(r: Roster) -> List[Violation]:
    """R2 晚班次日不得排早班（晚下班接早上班，休息时长被压到最短）。
    早接晚不禁：早班下班早、晚班上班晚，中间休息反而充裕。含跨月衔接。"""
    out = []
    for n in r.names:
        s, off = r.seq(n), r.offset(n)
        for i in range(len(s) - 1):
            if (s[i], s[i + 1]) == (LATE, EARLY):
                out.append(Violation("R2", HARD, "晚→早 直接相接",
                                     person=n, day=max(1, i - off + 1)))
    return out


def c_r10_balance(r: Roster) -> List[Violation]:
    """R10 个人早晚班天数差 |早-晚| <= balance_tol。
    不写成"相等"：上班天数为奇数时会强制每人至少一个中班，
    且与 R3 联立会逼出"每日早=晚"的极刚性解。"""
    out, tol = [], r.cfg.balance_tol
    for n in r.names:
        e = sum(1 for v in r.grid[n] if v == EARLY)
        l = sum(1 for v in r.grid[n] if v == LATE)
        if abs(e - l) > tol:
            out.append(Violation("R10", HARD,
                                 f"早 {e} / 晚 {l}，偏差 {e - l:+d}（容差 ±{tol}）", person=n))
    return out


def c_r9b_off_run_length(r: Roster) -> List[Violation]:
    """R9b 单次连休不超过 max_off_run 天。含跨月：月初接上月末的休息一并算。"""
    out, cap = [], r.cfg.max_off_run
    for n in r.names:
        off = r.offset(n)
        # 年假整段豁免：把年假当成"不是休息"，这样它既不会被算成超长连休，
        # 也不会把它前后两段本来独立的休息粘成一段。
        seq = [("x" if (i >= off and r.cfg.on_leave(n, i - off)) else v)
               for i, v in enumerate(r.seq(n))]
        for st, ln in _off_runs(seq):
            if ln > cap:
                out.append(Violation("R9b", HARD, f"连休 {ln} 天（上限 {cap} 天）",
                                     person=n, day=max(1, st - off + 1)))
    return out


def c_r9_off_run_count(r: Roster) -> List[Violation]:
    """R9 整月连休（>=2天的休息段）次数不超过 max_consec_off_runs。"""
    out, cap = [], r.cfg.max_consec_off_runs
    for n in r.names:
        runs = [x for x in _off_runs(r.grid[n]) if x[1] >= 2]
        if len(runs) > cap:
            out.append(Violation("R9", HARD, f"连休 {len(runs)} 次（上限 {cap} 次）", person=n))
    return out


def c_annual_leave(r: Roster) -> List[Violation]:
    """LEAVE 年假当天必须是休息。年假由员工提前申请、日期固定，是硬性安排。"""
    return [Violation("LEAVE", HARD, f"年假当天实排{CN.get(r.grid[n][d1-1])}",
                      person=n, day=d1)
            for n, days in r.cfg.annual_leave.items() if n in r.grid
            for d1 in days if 1 <= d1 <= r.cfg.days and r.grid[n][d1 - 1] != OFF]


def c_lock(r: Roster) -> List[Violation]:
    """LOCK 人事专员的必须排 / 不能排。"""
    out = []
    for n, days in r.cfg.locked.items():
        for d1, sh in days.items():
            if n in r.grid and 1 <= d1 <= r.cfg.days and r.grid[n][d1 - 1] != sh:
                out.append(Violation("LOCK", HARD,
                                     f"应排{CN.get(sh, sh)}，实排{CN.get(r.grid[n][d1-1])}",
                                     person=n, day=d1))
    for n, days in r.cfg.forbidden.items():
        for d1, sh in days.items():
            if n in r.grid and 1 <= d1 <= r.cfg.days and r.grid[n][d1 - 1] == sh:
                out.append(Violation("LOCK", HARD, f"不应排{CN.get(sh, sh)}",
                                     person=n, day=d1))
    return out


# ================================================================ 软约束检查

def c_r3_early_le_late(r: Roster) -> List[Violation]:
    """R3 销售口径每日 早 <= 晚。
    注：两份真实班表中大量日子早晚相等，故为 '<=' 而非严格小于。"""
    out, w, who = [], r.cfg.weight("R3"), r.members(*R3_GROUPS)
    for d in range(r.cfg.days):
        e, l = r.count(d, EARLY, who), r.count(d, LATE, who)
        if e > l:
            out.append(Violation("R3", SOFT, f"销售早班 {e} > 晚班 {l}",
                                 day=d + 1, penalty=w * (e - l)))
    return out


def c_r4b_weekday_le_weekend(r: Roster) -> List[Violation]:
    """R4b 周中在岗不应超过周末最低值。真实班表长期违反，权重很低，只作观察。"""
    out, w = [], r.cfg.weight("R4b")
    wk = [r.on_duty(d) for d in range(r.cfg.days) if r.is_weekend(d)]
    if not wk:
        return out
    floor = min(wk)
    for d in range(r.cfg.days):
        if not r.is_weekend(d) and r.on_duty(d) > floor:
            out.append(Violation("R4b", SOFT, f"周中在岗 {r.on_duty(d)} > 周末最低 {floor}",
                                 day=d + 1, penalty=w))
    return out


def c_r7_off_flanking(r: Roster) -> List[Violation]:
    """R7 连休前一天尽量早班、后一天尽量晚班——本质是把休息间隔拉到最长。"""
    out, w = [], r.cfg.weight("R7")
    for n in r.names:
        g = r.grid[n]
        for st, ln in _off_runs(g):
            if ln < 2:
                continue
            en = st + ln - 1
            if st > 0 and g[st - 1] == LATE:
                out.append(Violation("R7", SOFT, "连休前一天为晚班（宜早班）",
                                     person=n, day=st, penalty=w))
            if en < r.cfg.days - 1 and g[en + 1] == EARLY:
                out.append(Violation("R7", SOFT, "连休后一天为早班（宜晚班）",
                                     person=n, day=en + 2, penalty=w))
    return out


def c_r14_mgmt_weekend(r: Roster) -> List[Violation]:
    """R14 管理组尽量六日不休。按月开关——管理组人少时容量极紧，多数月份应关闭。"""
    if not r.cfg.mgmt_avoid_weekend_off:
        return []
    w = r.cfg.weight("R14")
    return [Violation("R14", SOFT, "管理组周末排休", person=n, day=d + 1, penalty=w)
            for n in r.members(GROUP_MGMT) for d in range(r.cfg.days)
            if r.is_weekend(d) and r.grid[n][d] == OFF]


def c_r15_soft_consecutive(r: Roster) -> List[Violation]:
    """R15 尽量 4~5 天安排一天休，不要用满 R1 的上限。含跨月。"""
    out, w, S = [], r.cfg.weight("R15"), r.cfg.soft_max_consec
    for n in r.names:
        run, off = 0, r.offset(n)
        for i, v in enumerate(r.seq(n)):
            if v == OFF:
                run = 0
                continue
            run += 1
            if run == S + 1:
                out.append(Violation("R15", SOFT, f"连上超过 {S} 天",
                                     person=n, day=max(1, i - off + 1), penalty=w))
    return out


def c_r16_pre_all_hands(r: Roster) -> List[Violation]:
    """R16 全员日前一天宜集中放休（由真实班表 9月24->25 的做法推广而来）。
    只提示不计罚：它是建议，不是可以量化违反的规则。"""
    return [Violation("R16", SOFT, f"全员日前一天在岗 {r.on_duty(d1 - 2)} 人（宜集中放休）",
                      day=d1 - 1, penalty=0)
            for d1 in r.cfg.all_hands_days if 2 <= d1 <= r.cfg.days]


def c_mid_usage(r: Roster) -> List[Violation]:
    """MID 中班稀缺性。真实班表全月中班约 42 人日、日均 1.4 人。
    中班不受早/晚/衔接约束影响，是模型里的"免罚选项"，不加罚必被滥用。
    罚超出每日免罚额度的部分，而不是每一个中班——保留它作为衔接工具的作用。"""
    out, w, cap = [], r.cfg.weight("MID"), r.cfg.mid_soft_cap
    for d in range(r.cfg.days):
        c = r.count(d, MID)
        if c > cap:
            out.append(Violation("MID", SOFT, f"中班 {c} 人（免罚额度 {cap}）",
                                 day=d + 1, penalty=w * (c - cap)))
    return out


def c_sandwich(r: Roster) -> List[Violation]:
    """SANDWICH 休-上-休：休息被中间一天班割裂，员工实际没歇到。含跨月。"""
    out, w = [], r.cfg.weight("SANDWICH")
    for n in r.names:
        s, off = r.seq(n), r.offset(n)
        for i in range(len(s) - 2):
            if s[i] == OFF and s[i + 1] != OFF and s[i + 2] == OFF:
                out.append(Violation("SANDWICH", SOFT, "休-上-休，休息被一天班割裂",
                                     person=n, day=max(1, i - off + 2), penalty=w))
    return out


def _work_runs(seq: List[str]):
    """切出上班段，返回 [(起始下标, 长度)]。和 _off_runs 是一对。"""
    runs, i = [], 0
    while i < len(seq):
        if seq[i] != OFF:
            j = i
            while j < len(seq) and seq[j] != OFF:
                j += 1
            runs.append((i, j - i))
            i = j
        else:
            i += 1
    return runs


def c_shortrun(r: Roster) -> List[Violation]:
    """SHORTRUN 只连上 2~3 天的一段，班次必须统一：全早或全晚，中班也算混。
    4 天及以上的段不管（长段里换班正常，且 R15 本就要求 4~5 天一休）。
    一段混了罚一次。只看在本月内开始的段；月末视为段的边界。"""
    out, w = [], r.cfg.weight("SHORTRUN")
    for n in r.names:
        s, off = r.seq(n), r.offset(n)
        for st, ln in _work_runs(s):
            if st < off or ln not in (2, 3):
                continue                      # 上月开始的段不归本月管
            seg = s[st:st + ln]
            if not (all(v == EARLY for v in seg) or all(v == LATE for v in seg)):
                out.append(Violation(
                    "SHORTRUN", SOFT,
                    f"连上 {ln} 天班次不统一：{''.join(CN.get(v, v) for v in seg)}",
                    person=n, day=st - off + 1, penalty=w))
    return out


def c_request(r: Roster, requests=None) -> List[Violation]:
    """REQUEST 员工需求未满足。软，不阻断排班，但要能说清为什么没满足。"""
    out = []
    for n, days in (requests or {}).items():
        if n not in r.grid:
            continue
        for d1, want in days.items():
            if not 1 <= d1 <= r.cfg.days:
                continue
            got = r.grid[n][d1 - 1]
            if got != want:
                out.append(Violation("REQUEST", SOFT,
                                     f"想要「{CN.get(want, want)}」实排「{CN.get(got, got)}」",
                                     person=n, day=d1, penalty=r.cfg.weight("REQUEST")))
    return out


# ================================================================ 规则表

HARD_RULES = [c_quota, c_r11_all_hands, c_r5_daily_min, c_r3b_late_early_gap,
              c_r6_group_coverage,
              c_r4_weekend_min, c_r1_max_consecutive, c_r2_shift_adjacency,
              c_r10_balance, c_r9b_off_run_length, c_r9_off_run_count, c_lock]

SOFT_RULES = [c_r3_early_le_late, c_r4b_weekday_le_weekend, c_r7_off_flanking,
              c_r14_mgmt_weekend, c_r15_soft_consecutive, c_r16_pre_all_hands,
              c_mid_usage, c_sandwich, c_shortrun]


def check_all(r: Roster, include_soft: bool = True, requests=None) -> List[Violation]:
    out = []
    for f in HARD_RULES:
        out.extend(f(r))
    if include_soft:
        for f in SOFT_RULES:
            out.extend(f(r))
        if requests:
            out.extend(c_request(r, requests))
    return out


def score(vs: List[Violation]):
    """返回 (硬约束违规数, 软约束罚分)。求解目标 = 硬归零、软最小。"""
    return (sum(1 for v in vs if v.level == HARD),
            sum(v.penalty for v in vs if v.level == SOFT))


# ================================================================ 可行性预检

def feasibility_precheck(names, group_of, cfg: Config) -> List[str]:
    """求解前跑，用鸽巢原理估容量。目的是在"无解"发生之前就告诉使用者
    哪条规则这个月结构上做不到、差几天——而不是等求解器返回一句 INFEASIBLE。"""
    msgs = []
    avail = [d for d in range(cfg.days) if (d + 1) not in cfg.all_hands_days]

    # 1) R6 组：每天必须留一早一晚，能同时休的人数被压死
    for g in R6_GROUPS:
        who = [n for n in names if group_of[n] == g]
        if not who:
            continue
        need = sum(cfg.off_quota[n] for n in who)
        cap_day = len(who) - 2
        if cap_day < 0:
            msgs.append(f"【致命】{g} 仅 {len(who)} 人，无法满足每日一早一晚")
            continue
        room = len(avail) * cap_day
        if need > room:
            msgs.append(f"【不可行】{g} 需休 {need} 天，全月最多容纳 {room} 天，缺 {need - room} 天")
        elif room - need <= max(1, len(who)):
            msgs.append(f"【紧张】{g} 需休 {need} 天 / 容量 {room} 天，余量 {room - need} 天")

        if g == GROUP_MGMT and cfg.mgmt_avoid_weekend_off:
            wd = [d for d in avail if not cfg.is_weekend(d)]
            room_wd = len(wd) * cap_day
            if need > room_wd:
                msgs.append(f"【不可行】R14：{g} 需休 {need} 天，非周末仅容纳 {room_wd} 天，"
                            f"至少 {need - room_wd} 次休息必须落在周末")

    # 2) 销售人员：R5 要求每天至少 早+晚 个人在岗，能休的人数被压住
    r5 = [n for n in names if group_of[n] in R5_GROUPS]
    if r5:
        keep = cfg.min_early + cfg.min_late
        if len(r5) < keep:
            msgs.append(f"【致命】销售人员仅 {len(r5)} 人，凑不够每天 {keep} 人在岗（R5）")
        else:
            need = sum(cfg.off_quota[n] for n in r5)
            room = len(avail) * (len(r5) - keep)
            if need > room:
                msgs.append(f"【不可行】销售人员需休 {need} 天，R5 下最多容纳 {room} 天，缺 {need - room}")

    # 3) 全店周末：在岗 >= weekend_min_on
    total = sum(cfg.off_quota.values())
    wk_room = sum(len(names) - cfg.weekend_min_on for d in avail if cfg.is_weekend(d))
    wd_room = sum(len(names) for d in avail if not cfg.is_weekend(d))
    if total > wk_room + wd_room:
        msgs.append(f"【不可行】全店休息总额 {total} 人日超出容量 {wk_room + wd_room} 人日")

    # 4) 单人：R9b + R9 联立后，一个月最多能休几天
    #    2 段连休(各 max_off_run 天) + s 个单休，单休之间至少隔 1 天上班
    #    占用天数 >= max_off_run*cnt + s + (cnt + s - 1)，解出 s 的上限
    cnt, L = cfg.max_consec_off_runs, cfg.max_off_run
    s_max = (len(avail) - (L + 1) * cnt + 1) // 2
    per_max = L * cnt + max(0, s_max)
    over = [n for n in names if cfg.off_quota[n] > per_max]
    if over:
        msgs.append(f"【不可行】连休规则下每人最多休约 {per_max} 天，"
                    f"但 {'、'.join(over[:5])} 的额度更高")

    # 5) 跨月：上月末连班 + 本月开头的全员上班日，可能直接顶破 R1
    for n in names:
        carry = cfg.carry_work(n)
        if not carry:
            continue
        forced = 0
        for d1 in range(1, cfg.days + 1):
            if d1 in cfg.all_hands_days:
                forced += 1
            else:
                break
        if carry + forced > cfg.max_consec:
            msgs.append(f"【不可行】{n} 上月末已连上 {carry} 天，本月开头 {forced} 天为全员日，"
                        f"合计 {carry + forced} 天超过上限 {cfg.max_consec}")
        elif carry + forced == cfg.max_consec:
            msgs.append(f"【紧张】{n} 到 {forced} 号将连上 {cfg.max_consec} 天（上限），"
                        f"次日必须安排休息")
    return msgs


# ================================================================ 求解器
# 约束的书写顺序与上方"规则清单"一致，方便和检查函数对照。

def build_cpsat_model(names, group_of, cfg: Config, requests=None):
    """构造 CP-SAT 模型：硬约束进 m.Add()，软约束进目标函数。

    变量 x[人, 天, 班次] 是 0/1 布尔量——"这个人这天排这个班吗"。
    把"选择"拆成一组 0/1 开关，是为了能对它们求和：某天早班人数就是
    sum(x[n, d, EARLY] for n in names)。整数变量做不到这一点。
    """
    from ortools.sat.python import cp_model

    m = cp_model.CpModel()
    D, N, W = cfg.days, names, cfg.weights
    x = {(n, d, s): m.NewBoolVar(f"x_{n}_{d}_{s}")
         for n in N for d in range(D) for s in SHIFTS}
    pen = []                                          # [(权重, 变量)]，最后一起最小化

    # 每人每天恰好一种班次。不写这条，求解器会让人同一天既早班又晚班。
    for n in N:
        for d in range(D):
            m.AddExactlyOne(x[n, d, s] for s in SHIFTS)

    # ---------------------------------------------------------- 硬约束

    # QUOTA 休息额度
    for n in N:
        m.Add(sum(x[n, d, OFF] for d in range(D)) == cfg.off_quota[n])

    # R11 全员上班日不得排休
    for d1 in cfg.all_hands_days:
        if 1 <= d1 <= D:
            for n in N:
                m.Add(x[n, d1 - 1, OFF] == 0)

    # R5 销售人员（个人消费+智慧办公）每日早晚下限，不含管理组
    r5 = [n for n in N if group_of[n] in R5_GROUPS]
    for d in range(D):
        m.Add(sum(x[n, d, EARLY] for n in r5) >= cfg.min_early)
        m.Add(sum(x[n, d, LATE] for n in r5) >= cfg.min_late)

    # R3b 销售口径每日 晚 - 早 <= cap。"数量上限"句式，只不过数的是两个班次的差。
    cap = cfg.max_late_over_early
    for d in range(D):
        m.Add(sum(x[n, d, LATE] for n in r5) - sum(x[n, d, EARLY] for n in r5) <= cap)

    # R6 管理组/智办每日各一早一晚
    for g in R6_GROUPS:
        who = [n for n in N if group_of[n] == g]
        for d in range(D):
            if who:
                m.Add(sum(x[n, d, EARLY] for n in who) >= 1)
                m.Add(sum(x[n, d, LATE] for n in who) >= 1)

    # R4 周末在岗下限
    for d in range(D):
        if cfg.is_weekend(d):
            m.Add(sum(1 - x[n, d, OFF] for n in N) >= cfg.weekend_min_on)

    # R1 连上 <= K 天。反过来写成"任意 K+1 天窗口里至少 1 天休"——
    # 声明式：不数连了几天，只要求每个窗口都被休息打断。
    K = cfg.max_consec
    for n in N:
        for st in range(max(0, D - K)):
            m.Add(sum(x[n, st + i, OFF] for i in range(K + 1)) >= 1)
        carry = cfg.carry_work(n)                     # 跨月：上月末已连上的天数
        if carry:
            head = min(max(0, K - carry) + 1, D)
            m.Add(sum(x[n, d, OFF] for d in range(head)) >= 1)

    # R2 晚班次日不得排早班。"两件事不能同时发生" => 两个 0/1 之和 <= 1
    for n in N:
        for d in range(D - 1):
            m.Add(x[n, d, LATE] + x[n, d + 1, EARLY] <= 1)
        tail = cfg.prev_tail.get(n, [])
        if tail and tail[-1] == LATE:
            m.Add(x[n, 0, EARLY] == 0)

    # R10 |早 - 晚| <= tol。求解器不认识绝对值，拆成上下两条线性约束。
    tol = cfg.balance_tol
    for n in N:
        diff = (sum(x[n, d, EARLY] for d in range(D))
                - sum(x[n, d, LATE] for d in range(D)))
        m.Add(diff <= tol)
        m.Add(diff >= -tol)

    # R9b 单次连休 <= L 天。R1 的镜像：
    #   R1  = 任意 K+1 天窗口里【至少】1 天休   -> sum(休) >= 1
    #   R9b = 任意 L+1 天窗口里【最多】L 天休   -> sum(休) <= L
    L = cfg.max_off_run
    for n in N:
        for st in range(max(0, D - L)):
            # 窗口里只要有年假，这段休息就是年假造成的，整窗豁免
            if any(cfg.on_leave(n, st + i) for i in range(L + 1)):
                continue
            m.Add(sum(x[n, st + i, OFF] for i in range(L + 1)) <= L)
        carry = cfg.carry_off(n)                      # 跨月：上月末已连休的天数
        if carry:
            left = max(0, L - carry)                  # 本月开头还能连休几天
            m.Add(sum(x[n, d, OFF] for d in range(min(left + 1, D))) <= left)

    # R9 整月连休次数 <= cnt。
    # "次数"不是能直接求和的量，先给每天一个"连休段起点"指示变量：
    #   start_d 为真 <=> 第 d 天休、第 d+1 天也休、而第 d-1 天没休
    #   start_d >= x[d,休] + x[d+1,休] - x[d-1,休] - 1
    # 三个条件全满足时右边 = 1+1-0-1 = 1，逼 start_d 为 1；否则右边 <= 0，可取 0。
    # 只需写下界：sum(start) <= cnt 会把它们压住，没有力量把不该为真的顶上去。
    for n in N:
        tail = cfg.prev_tail.get(n, [])
        prev_off = 1 if (tail and tail[-1] == OFF) else 0
        starts = []
        for d in range(D - 1):
            v = m.NewBoolVar(f"r9start_{n}_{d}")
            if d == 0:
                m.Add(v >= x[n, 0, OFF] + x[n, 1, OFF] - prev_off - 1)
            else:
                m.Add(v >= x[n, d, OFF] + x[n, d + 1, OFF] - x[n, d - 1, OFF] - 1)
            starts.append(v)
        if starts:
            m.Add(sum(starts) <= cfg.max_consec_off_runs)

    # LEAVE 年假当天必须休息
    for n, days in cfg.annual_leave.items():
        if n in N:
            for d1 in days:
                if 1 <= d1 <= D:
                    m.Add(x[n, d1 - 1, OFF] == 1)

    # LOCK 人事三态：必须排 (== 1) / 不能排 (== 0)，一个数字的差别。
    # 挂在 assumption 上，无解时可回溯是哪几条人事安排互相冲突。
    assumptions = {}
    for store, val, label in ((cfg.locked, 1, "必须排"), (cfg.forbidden, 0, "不能排")):
        for n, days in store.items():
            for d1, sh in days.items():
                if n in N and 1 <= d1 <= D:
                    lit = m.NewBoolVar(f"lock_{n}_{d1}_{val}")
                    m.Add(x[n, d1 - 1, sh] == val).OnlyEnforceIf(lit)
                    assumptions[lit] = f"{n} {d1}日 {label} {CN.get(sh, sh)}"
    if assumptions:
        m.AddAssumptions(list(assumptions))

    # ---------------------------------------------------------- 软约束

    # R3 销售口径每日 早 <= 晚。超出多少罚多少，用一个非负松弛量接住差值。
    sales = [n for n in N if group_of[n] in R3_GROUPS]
    for d in range(D):
        gap = m.NewIntVar(0, len(sales), f"r3_{d}")
        m.Add(gap >= sum(x[n, d, EARLY] for n in sales)
                     - sum(x[n, d, LATE] for n in sales))
        pen.append((W.get("R3", 10), gap))

    # R14 管理组周末排休（按月开关）
    if cfg.mgmt_avoid_weekend_off:
        for n in [p for p in N if group_of[p] == GROUP_MGMT]:
            for d in range(D):
                if cfg.is_weekend(d):
                    pen.append((W.get("R14", 12), x[n, d, OFF]))

    # R15 尽量不上满 S 天。和 R1 同形，但违反时不禁止，只让指示变量亮起来计罚。
    S = cfg.soft_max_consec
    for n in N:
        for st in range(max(0, D - S)):
            v = m.NewBoolVar(f"r15_{n}_{st}")
            m.Add(sum(x[n, st + i, OFF] for i in range(S + 1)) + v >= 1)
            pen.append((W.get("R15", 10), v))

    # MID 只罚超出每日免罚额度的中班：excess >= 当日中班数 - cap，
    # 下界 0 + 目标最小化 => excess 自动取到 max(0, 中班数 - cap)。
    for d in range(D):
        excess = m.NewIntVar(0, len(N), f"mid_{d}")
        m.Add(excess >= sum(x[n, d, MID] for n in N) - cfg.mid_soft_cap)
        pen.append((W.get("MID", 8), excess))

    # SANDWICH 休-上-休。与 R9 同构，但中间那天要求【不休】，写成 (1 - x)：
    #   sand >= x[d,休] + (1 - x[d+1,休]) + x[d+2,休] - 2
    #        =  x[d,休] - x[d+1,休] + x[d+2,休] - 1
    for n in N:
        tail = cfg.prev_tail.get(n, [])
        if tail and D >= 2:                           # 跨月：上月最后一天 + 本月前两天
            p0 = 1 if tail[-1] == OFF else 0
            sand = m.NewBoolVar(f"sand_{n}_pre")
            m.Add(sand >= p0 - x[n, 0, OFF] + x[n, 1, OFF] - 1)
            pen.append((W.get("SANDWICH", 12), sand))
        for d in range(D - 2):
            sand = m.NewBoolVar(f"sand_{n}_{d}")
            m.Add(sand >= x[n, d, OFF] - x[n, d + 1, OFF] + x[n, d + 2, OFF] - 1)
            pen.append((W.get("SANDWICH", 12), sand))

    # SHORTRUN 只连上 2~3 天的一段，班次须统一（全早或全晚）。
    # 分三步：
    #   ① 认出"恰好 L 天的一段"：前一天休、后一天休、中间 L 天全上班
    #   ② 两个指示变量 ue（这 L 天全早）、ul（这 L 天全晚）
    #   ③ 段成立且既不全早也不全晚 => 罚
    #
    #   p >= 前休 + 后休 + Σ上班(d..d+L-1) - (L+1) - ue - ul
    #   段条件全成立时前三项之和 = L+2，右边 = 1 - ue - ul：
    #     全早(ue=1)或全晚(ul=1) -> 右边 <= 0，不罚；否则右边 = 1，罚。
    #   段条件有一条不成立 -> 右边 <= 0，不罚。
    #
    # 注意 ue/ul 必须写【上界】：ue <= x[早] 对段内每一天。
    # 因为目标函数在压 p，而 ue 越大 p 越小——不加上界，求解器会把 ue 直接
    # 设成 1 来逃罚。对比 R9 的 start 变量只要下界：那里没有力量把它往上推。
    # 写辅助变量时，先想清楚目标函数在把它往哪个方向推，就知道该写上界还是下界。
    for n in N:
        tail = cfg.prev_tail.get(n, [])
        for L in (2, 3):
            for d in range(D - L + 1):
                if d == 0:
                    if tail and tail[-1] != OFF:
                        continue                      # 段从上月开始，不归本月管
                    prev_off = 1                      # 月初或上月末是休息：视为边界
                else:
                    prev_off = x[n, d - 1, OFF]
                next_off = 1 if d + L == D else x[n, d + L, OFF]   # 月末视为边界

                ue = m.NewBoolVar(f"sr_e_{n}_{d}_{L}")
                ul = m.NewBoolVar(f"sr_l_{n}_{d}_{L}")
                for i in range(L):
                    m.Add(ue <= x[n, d + i, EARLY])
                    m.Add(ul <= x[n, d + i, LATE])

                p = m.NewBoolVar(f"sr_{n}_{d}_{L}")
                work = sum(1 - x[n, d + i, OFF] for i in range(L))
                m.Add(p >= prev_off + next_off + work - (L + 1) - ue - ul)
                pen.append((W.get("SHORTRUN", 15), p))

    # REQUEST 员工需求：满足则减分。这是唯一的"加分项"，其余都是罚分。
    bonus = []
    rw = W.get("REQUEST", 25)
    for n, days in (requests or {}).items():
        for d1, s in days.items():
            if n in N and 1 <= d1 <= D:
                bonus.append((rw, x[n, d1 - 1, s]))

    m.Minimize(sum(w * v for w, v in pen) - sum(w * v for w, v in bonus))
    return m, x, assumptions


def solve(names, group_of, cfg, requests=None, max_seconds=30):
    """返回 (Roster 或 None, 状态说明)。无解时状态里附带冲突的人事安排。"""
    from ortools.sat.python import cp_model

    m, x, assumptions = build_cpsat_model(names, group_of, cfg, requests)
    sv = cp_model.CpSolver()
    sv.parameters.max_time_in_seconds = max_seconds
    sv.parameters.num_search_workers = 8
    st = sv.Solve(m)

    if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        why = ""
        if st == cp_model.INFEASIBLE and assumptions:
            bad = set(sv.SufficientAssumptionsForInfeasibility())
            hit = [v for k, v in assumptions.items() if k.Index() in bad]
            if hit:
                why = "，冲突的人事安排：" + "；".join(hit)
        return None, sv.StatusName(st) + why

    grid = {n: [s for d in range(cfg.days) for s in SHIFTS if sv.Value(x[n, d, s])]
            for n in names}
    return Roster(names, group_of, cfg, grid), sv.StatusName(st)
