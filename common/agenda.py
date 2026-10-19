# -*- coding: utf-8 -*-
"""会议议程模型：解析、校验、按系统时钟定位当前项、顺延/提前调整。

设计要点
--------
* 时间一律用"从 00:00 起的分钟数"（0..1439）表示，不做字符串比较。
* ``end <= start`` 视为**跨午夜**（例如 23:30~01:00），此时时长是
  ``end - start + 1440``。等于号也算跨天（表示整 24 小时）。
* 议程**允许有空隙**（真实议程里常有"自由活动""自行安排"之类的空档），
  落到空隙里就是**空闲静默**，不做强制连续化。
* "当前项"由 ``index_at(now_min)`` 定位。主控**中途才启动**也能正确接上，
  不需要从第一项开始补跑。
* 三种运行期调整都只改数据，重算/下发交给上层：
    - ``shift_from(i, +N)``  顺延：从第 i 项起，之后所有项整体后移 N 分钟
    - ``finish_now(i, now)`` 提前/跳过：把第 i 项截断到现在，下一项立刻开始并
      **吸收**多出来的时间（不改动更后面的项）
* 所有方法都不碰 UI、不碰网络，纯数据进出 —— 这样才能拿自测脚本反复验证。
"""
import json
import os
from datetime import datetime, timedelta

from .const import AGENDA_FILENAME, app_base_dir, app_data_dir

MINUTES_PER_DAY = 1440

#: 议题名最大长度（横幅是单行大字，太长会被缩到看不清）
TITLE_MAX = 60


# ------------------------------------------------------------------ 时间工具
def parse_hhmm(text):
    """把 ``13:30`` / ``1330`` / ``13：30``（全角冒号）解析成分钟数。

    非法输入返回 ``None``（调用方负责提示用户）。
    """
    if text is None:
        return None
    raw = str(text).strip()
    if not raw:
        return None
    # 全角冒号 / 点号 / 连字符 都当成冒号
    for ch in ("：", "．", ".", "，", ","):
        raw = raw.replace(ch, ":")
    raw = raw.replace("-", ":")

    if ":" in raw:
        parts = raw.split(":")
        if len(parts) != 2:
            return None
        hh, mm = parts[0].strip(), parts[1].strip()
    elif raw.isdigit():
        if len(raw) == 4:              # 1330
            hh, mm = raw[:2], raw[2:]
        elif len(raw) <= 2:            # 13 -> 13:00
            hh, mm = raw, "0"
        else:
            return None
    else:
        return None

    if not (hh.isdigit() and (mm.isdigit() or mm == "")):
        return None
    hour = int(hh)
    minute = int(mm) if mm else 0
    if hour == 24 and minute == 0:
        return MINUTES_PER_DAY           # 允许写 24:00，归一到"次日 00:00"
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def format_hhmm(minutes):
    """分钟数 -> ``HH:MM``（超出一天自动回绕）。"""
    value = int(minutes) % MINUTES_PER_DAY
    return "%02d:%02d" % (value // 60, value % 60)


def normalize(minutes):
    return int(minutes) % MINUTES_PER_DAY


# ------------------------------------------------------------------ 单条议程
class AgendaItem(object):
    """一条议程：一段起止时间 + 一个议题名。"""

    __slots__ = ("start_min", "end_min", "title", "is_break")

    def __init__(self, start_min, end_min, title="", is_break=False):
        self.start_min = normalize(start_min)
        self.end_min = normalize(end_min)
        self.title = str(title or "").strip()[:TITLE_MAX]
        #: 是否是"休息类"议程项（中场休息 / 茶歇 / 午休 …）。
        #: 这类项**不下发倒计时、也不弹任何提醒**——开会的人不需要被反复提醒
        #: "休息还剩 10 分钟"。主控端自己仍然会显示剩余时间（给主持人看）。
        self.is_break = bool(is_break)

    # -------- 派生属性
    @property
    def duration_min(self):
        """时长（分钟）。结束早于等于开始 -> 跨午夜。"""
        span = (self.end_min - self.start_min) % MINUTES_PER_DAY
        return span or MINUTES_PER_DAY       # 起止相同 = 整 24 小时

    @property
    def crosses_midnight(self):
        return self.end_min <= self.start_min

    def label(self):
        return "%s~%s" % (format_hhmm(self.start_min), format_hhmm(self.end_min))

    def describe(self):
        tail = "（休息·不提醒）" if self.is_break else ""
        return "%s  %s%s" % (self.label(), self.title or "(未命名)", tail)

    # -------- 序列化
    def to_dict(self):
        data = {
            "start": format_hhmm(self.start_min),
            "end": format_hhmm(self.end_min),
            "title": self.title,
        }
        if self.is_break:
            data["is_break"] = True        # 普通项省略该字段，配置文件更干净
        return data

    @classmethod
    def from_dict(cls, data):
        start = parse_hhmm(data.get("start"))
        end = parse_hhmm(data.get("end"))
        if start is None or end is None:
            return None
        return cls(start, end, data.get("title") or "",
                   is_break=bool(data.get("is_break")))

    def copy(self):
        return AgendaItem(self.start_min, self.end_min, self.title,
                          is_break=self.is_break)


# -------------------------------------------------------------------- 议程表
class Agenda(object):
    """一张议程表。"""

    def __init__(self, items=None):
        self.items = list(items or [])
        self.sort()

    # ---------------------------------------------------------- 基本操作
    def sort(self):
        self.items.sort(key=lambda it: it.start_min)

    def __len__(self):
        return len(self.items)

    def __iter__(self):
        return iter(self.items)

    def add(self, item):
        self.items.append(item)
        self.sort()
        return self.items.index(item)

    def remove(self, index):
        if 0 <= index < len(self.items):
            return self.items.pop(index)
        return None

    def replace(self, index, item):
        if 0 <= index < len(self.items):
            self.items[index] = item
            self.sort()

    def clear(self):
        self.items = []

    def index_of(self, item):
        for i, it in enumerate(self.items):
            if it is item:
                return i
        return -1

    def total_span_min(self):
        if not self.items:
            return 0
        first = self.items[0].start_min
        last_end = self.items[-1].end_min
        span = (last_end - first) % MINUTES_PER_DAY
        return span or MINUTES_PER_DAY

    # ------------------------------------------------------ 时钟定位
    def index_at(self, now_min):
        """返回 ``now_min`` 时刻正处于第几项；落在空隙/议程之外返回 ``None``。

        同时考虑"昨日启动的跨午夜项还没结束"的情形：例如议程里有
        ``23:30~01:00``，现在是 00:30，应当仍算在这一项里。
        """
        now_min = normalize(now_min)
        for i, it in enumerate(self.items):
            span = it.duration_min
            for base in (0, -MINUTES_PER_DAY):
                lo = it.start_min + base
                if lo <= now_min < lo + span:
                    return i
        return None

    def next_index(self, index):
        if index is None or index + 1 >= len(self.items):
            return None
        return index + 1

    def deadline_ms(self, index, now_dt=None):
        """返回第 ``index`` 项**本次结束时刻**的绝对时间戳（毫秒）。

        规则很简单：取"今天(或次日)的那个 end 时刻、且严格晚于现在"。
        跨午夜项会自动落到次日。
        """
        now_dt = now_dt or datetime.now()
        it = self.items[index]
        end_today = now_dt.replace(hour=it.end_min // 60,
                                   minute=it.end_min % 60,
                                   second=0, microsecond=0)
        if end_today <= now_dt:
            end_today += timedelta(days=1)
        return int(end_today.timestamp() * 1000)

    def remaining_min(self, index, now_dt=None):
        now_dt = now_dt or datetime.now()
        delta = self.deadline_ms(index, now_dt) - int(now_dt.timestamp() * 1000)
        return max(0, int(delta // 60000))

    # ------------------------------------------------------ 运行期调整
    def shift_from(self, index, delta_min):
        """顺延：从第 ``index`` 项起（含），之后所有项整体平移。

        用途：讲师讲超时了，点 +10 分钟 —— 当前项的结束时刻和后续每一项的
        起止时间都往后推 10 分钟，相当于"整个日程平移"。
        """
        if index is None or delta_min == 0:
            return
        for it in self.items[index:]:
            it.start_min = normalize(it.start_min + delta_min)
            it.end_min = normalize(it.end_min + delta_min)

    def finish_now(self, index, now_min):
        """提前结束：把第 ``index`` 项截断到现在，下一项立刻开始。

        语义是"**吸收**"：下一项的结束时间**保持不变**（多出来的时间算给它），
        再后面的项完全不动。这样"讲师提前讲完"不会悄悄改动整张日程表。
        返回下一项的序号（没有下一项则返回 ``None``）。
        """
        if index is None or not (0 <= index < len(self.items)):
            return None
        now_min = normalize(now_min)
        self.items[index].end_min = now_min

        nxt_index = index + 1
        if nxt_index >= len(self.items):
            return None

        nxt = self.items[nxt_index]
        original_span = nxt.duration_min          # 先记下原本时长再改 start
        nxt.start_min = now_min

        # 让下一项"吸收"多出来的时间：它在原定时刻结束。
        # 但若现在时刻已经越过它原本的结束时刻（议程本身不连续时会这样），
        # 就按原时长往后顺推，避免出现"结束早于开始"的畸形数据。
        gap = normalize(nxt.end_min - now_min)
        if gap == 0 or gap > MINUTES_PER_DAY // 2:
            nxt.end_min = normalize(now_min + original_span)
        return nxt_index

    # ---------------------------------------------------------- 校验
    def validate(self):
        """返回一组给人看的提示（不是硬错误，允许有空隙）。"""
        notes = []
        prev = None
        for i, it in enumerate(self.items, 1):
            if it.duration_min <= 0:
                notes.append("第 %d 项时长非法" % i)
            if not it.title:
                notes.append("第 %d 项(%s)没填议题" % (i, it.label()))
            if it.crosses_midnight:
                notes.append("第 %d 项(%s)跨午夜，按「次日」处理"
                             % (i, it.label()))
            if prev is not None:
                gap = normalize(it.start_min - prev.end_min)
                if gap == 0:
                    pass
                elif gap < MINUTES_PER_DAY // 2:
                    notes.append("第 %d 项(%s)与前一项之间有 %d 分钟空隙"
                                 % (i, it.label(), gap))
                else:
                    overlap = normalize(prev.end_min - it.start_min)
                    notes.append("第 %d 项(%s)与前一项重叠 %d 分钟"
                                 % (i, it.label(), overlap))
            prev = it
        return notes

    # ---------------------------------------------------------- 序列化
    def to_dict(self):
        return [it.to_dict() for it in self.items]

    @classmethod
    def from_dict(cls, data):
        items = []
        if isinstance(data, list):
            for row in data:
                if isinstance(row, dict):
                    item = AgendaItem.from_dict(row)
                    if item is not None:
                        items.append(item)
        return cls(items)


# ---------------------------------------------------------------- 持久化
def agenda_path():
    """议程文件的读写位置：优先 exe/工程目录（便携），否则数据目录。"""
    portable = os.path.join(app_base_dir(), AGENDA_FILENAME)
    if os.path.isfile(portable):
        return portable
    return os.path.join(app_data_dir(), AGENDA_FILENAME)


def load_agenda():
    """读取议程 + 主控设置。任何异常都退回空议程（不能因为配置坏了启动不了）。"""
    payload = {"items": [], "node_minutes": None, "enabled": True}
    path = agenda_path()
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as fp:
                data = json.load(fp)
            if isinstance(data, dict):
                payload["items"] = Agenda.from_dict(data.get("items")).items
                payload["node_minutes"] = data.get("node_minutes")
                payload["enabled"] = bool(data.get("enabled", True))
        except (OSError, ValueError):
            pass
    return payload


def save_agenda(agenda, node_minutes=None, enabled=True, path=None):
    path = path or agenda_path()
    data = {
        "items": agenda.to_dict(),
        "node_minutes": list(node_minutes) if node_minutes else None,
        "enabled": bool(enabled),
    }
    try:
        with open(path, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
    except OSError:
        return None
    return path


# ------------------------------------------------------- 节点列表解析/校验
def parse_nodes(text, default=(10, 5, 3, 1), maximum=12):
    """把 ``"15,10,5,1"`` 解析成去重降序的分钟元组；解析不出就用默认值。"""
    if text is None:
        return tuple(default)
    raw = str(text).replace("，", ",").replace("、", ",").replace(" ", ",")
    values = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            value = int(float(chunk))
        except (TypeError, ValueError):
            continue
        if 1 <= value <= MINUTES_PER_DAY:
            values.append(value)
    if not values:
        return tuple(default)
    values = sorted(set(values), reverse=True)[:maximum]
    return tuple(values)


def format_nodes(nodes):
    """节点元组 -> ``"10,5,3,1"``（给输入框用）。"""
    return ",".join(str(int(n)) for n in (nodes or ()))
