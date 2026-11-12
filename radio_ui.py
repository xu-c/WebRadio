"""Keyboard interaction and 320 × 170 interface shared by device and preview."""

import queue
import threading
import time
from dataclasses import replace

from PIL import Image, ImageDraw, ImageFont

from radio_directory import DirectoryError, Query, Station, Superseded
from radio_player import Player

SIZE = (320, 170)
VISIBLE = 4
FILTERS = (
    ("country", "国家／地区", (("", "全部"), ("CN", "中国"), ("TW", "台湾"), ("HK", "香港"),
                         ("JP", "日本"), ("US", "美国"), ("GB", "英国"), ("DE", "德国"))),
    ("language", "语言", (("", "全部"), ("chinese", "中文"), ("mandarin", "普通话"),
                      ("cantonese", "粤语"), ("english", "英语"), ("japanese", "日语"))),
    ("tag", "类型", (("", "全部"), ("news", "新闻"), ("pop", "流行"), ("jazz", "爵士"),
                  ("classical", "古典"), ("talk", "谈话"), ("ambient", "氛围"))),
    ("order", "排序", (("clickcount", "收听最多"), ("clicktrend", "近期热门"),
                    ("votes", "票数最多"), ("name", "台名 A–Z"))),
)

DEMO_STATIONS = [
    Station(f"0a000000-0000-4000-8000-{index:012}", name, "https://example.com/radio",
            country=country, codec=codec, bitrate=rate, tags=tag)
    for index, (name, country, codec, rate, tag) in enumerate((
        ("SomaFM · Groove Salad", "US", "AAC", "128", "ambient"),
        ("BBC World Service", "GB", "MP3", "96", "news"),
        ("上海动感 101", "CN", "MP3", "128", "pop"),
        ("Jazz24", "US", "AAC", "128", "jazz"),
        ("FIP Jazz", "FR", "MP3", "192", "jazz"),
        ("NHK Radio 1", "JP", "AAC", "128", "news"),
        ("香港电台第一台", "HK", "AAC", "128", "talk"),
        ("台北爱乐电台", "TW", "MP3", "128", "classical"),
    ))
]


class RadioApp:
    def __init__(self, directory, library, demo=False):
        self.directory = directory
        self.library = library
        self.demo = demo
        self.query = Query()
        self.page = 0
        self.more = False
        self.results = []
        self.tab = 0
        self.indices = [0, 0]
        self.offsets = [0, 0]
        self.loading = False
        self.directory_status = ""
        self.modal = ""
        self.draft = self.query
        self.filter_row = 0
        self.input = ""
        self.input_error = ""
        self.toast = ""
        self.toast_until = 0
        self.volume = library.volume
        self.save_volume_at = None
        self.station = None
        self.play_status = "尚未播放"
        self.search_id = 0
        self.play_id = 0
        self.events = queue.SimpleQueue()
        self.search_jobs = queue.Queue()
        self.click_jobs = queue.Queue(maxsize=8)
        self.running = True
        self.dirty = True
        self.player = Player(lambda: self.volume, self.play_event, self.count_play, demo)
        threading.Thread(target=self.search_worker, daemon=True).start()
        if not demo:
            threading.Thread(target=self.click_worker, daemon=True).start()
        self.search()

    def rows(self):
        return list(self.library.favorites.values())[::-1] if self.tab else self.results

    def selected(self):
        rows = self.rows()
        return rows[self.indices[self.tab]] if rows else None

    def move(self, index, tab=None):
        tab = self.tab if tab is None else tab
        count = len(self.library.favorites if tab else self.results)
        index = min(max(index, 0), max(count - 1, 0))
        self.indices[tab] = index
        self.offsets[tab] = max(0, min(max(self.offsets[tab], index - VISIBLE + 1),
                                       index, count - VISIBLE))

    def search(self):
        self.search_id += 1
        self.loading = True
        self.directory_status = ""
        self.search_jobs.put((self.search_id, self.query, self.page))

    def search_worker(self):
        while True:
            job = self.search_jobs.get()
            while not self.search_jobs.empty():
                job = self.search_jobs.get()
            if job is None:
                return
            generation, query, page = job

            def canceled():
                return generation != self.search_id or not self.running

            try:
                if self.demo:
                    languages = {"CN": "chinese mandarin", "HK": "chinese cantonese",
                                 "TW": "chinese mandarin", "JP": "japanese",
                                 "GB": "english", "US": "english", "FR": "french"}
                    rows = [s for s in DEMO_STATIONS
                            if query.name.casefold() in s.name.casefold()
                            and (not query.country or s.country == query.country)
                            and (not query.language or query.language in languages[s.country])
                            and (not query.tag or query.tag == s.tags)]
                    if query.order == "name":
                        rows.sort(key=lambda station: station.name.casefold())
                    result = rows, False, False
                else:
                    result = self.directory.search(
                        query, page, canceled,
                        lambda cached: self.events.put(("cache", generation, cached)))
                self.events.put(("search", generation, result))
            except DirectoryError as error:
                self.events.put(("search", generation, str(error)))
            except Superseded:
                continue

    def count_play(self, station):
        try:
            self.click_jobs.put_nowait(station)
        except queue.Full:
            pass

    def click_worker(self):
        recent = {}
        while True:
            station = self.click_jobs.get()
            if station is None:
                return
            now = time.monotonic()
            recent = {uuid: timestamp for uuid, timestamp in recent.items()
                      if now - timestamp < 60}
            if station.uuid not in recent:
                recent[station.uuid] = now
                self.directory.count_play(station)

    def play_event(self, generation, message):
        self.events.put(("play", generation, message))

    def notify(self, text):
        self.toast, self.toast_until = text, time.monotonic() + 2.4

    def tick(self):
        now = time.monotonic()
        while not self.events.empty():
            kind, generation, result = self.events.get()
            if kind == "play" and generation == self.play_id:
                self.play_status = result
                self.dirty = True
            if kind in ("search", "cache") and generation == self.search_id:
                self.loading = kind == "cache"
                if isinstance(result, str):
                    self.directory_status = result
                else:
                    old_index = self.indices[0]
                    uuid = self.results[old_index].uuid if self.results else None
                    self.results, self.more, cached = result
                    self.directory_status = "离线缓存 · R 刷新" if cached else ""
                    index = next((i for i, station in enumerate(self.results)
                                  if station.uuid == uuid), old_index)
                    self.move(index, tab=0)
                self.dirty = True
        if self.toast and now >= self.toast_until:
            self.toast = ""
            self.dirty = True
        if self.save_volume_at is not None and now >= self.save_volume_at:
            self.save_volume()

    def save_volume(self):
        self.save_volume_at = None
        try:
            self.library.save(self.library.favorites, self.volume)
        except OSError:
            self.notify("无法保存音量")
            self.dirty = True

    def tune(self, station):
        self.play_id += 1
        self.station = station
        self.play_status = "正在连接" if station else "已停止"
        self.player.select(self.play_id, station)

    def show_query(self, query, page=0):
        if query != self.query or page != self.page:
            self.results = []
            self.more = False
            self.indices[0] = self.offsets[0] = 0
        self.query, self.page, self.tab = query, page, 0
        self.search()

    def edit(self, key, char, clear):
        if key == "escape":
            self.modal = "filter" if self.modal == "filter_text" else ""
        elif key == "enter":
            value = self.input.strip()
            if self.modal == "search":
                self.show_query(replace(self.query, name=value))
                self.modal = ""
            else:
                field = FILTERS[self.filter_row][0]
                if field == "country" and value and (len(value) != 2 or
                                                      not value.isascii() or not value.isalpha()):
                    self.input_error = "请输入两位国家代码，例如 CN"
                    return
                value = value.upper() if field == "country" else value.casefold()
                self.draft = replace(self.draft, **{field: value})
                self.modal = "filter"
        elif clear:
            self.input = ""
        elif key == "backspace":
            self.input = self.input[:-1]
        elif char and char.isprintable():
            self.input = (self.input + char)[:80]
        self.input_error = ""

    def handle(self, key, char="", clear=False):
        self.dirty = True
        if self.modal in ("search", "filter_text"):
            self.edit(key, char, clear)
            return
        if self.modal == "help":
            if key in ("escape", "enter", "h", "?"):
                self.modal = ""
            return
        if self.modal == "filter":
            field, _, values = FILTERS[self.filter_row]
            if key in ("up", "k", "down", "j"):
                step = -1 if key in ("up", "k") else 1
                self.filter_row = (self.filter_row + step) % len(FILTERS)
            elif key in ("left", "right", ",", "."):
                current = getattr(self.draft, field)
                index = next((i for i, (value, _) in enumerate(values) if value == current), -1)
                step = 1 if key in ("right", ".") else -1
                index = (index + step) % len(values) if index >= 0 else 0
                self.draft = replace(self.draft, **{field: values[index][0]})
            elif key == "e" and field != "order":
                self.modal, self.input = "filter_text", getattr(self.draft, field)
            elif key == "x":
                self.draft = Query(name=self.draft.name)
            elif key == "enter":
                self.show_query(self.draft)
                self.modal = ""
            elif key == "escape":
                self.modal = ""
            return

        if key in ("up", "k", "down", "j"):
            step = -1 if key in ("up", "k") else 1
            self.move(self.indices[self.tab] + step)
        elif key in ("left", "right", ",", ".") and not self.tab:
            if key in ("left", ",") and self.page > 0:
                self.show_query(self.query, self.page - 1)
            elif key in ("right", ".") and self.more:
                self.show_query(self.query, self.page + 1)
        elif key in ("tab", "1", "2"):
            self.tab = 1 - self.tab if key == "tab" else int(key) - 1
        elif key == "/":
            self.modal, self.input = "search", self.query.name
        elif key == "f":
            self.modal, self.draft, self.filter_row = "filter", self.query, 0
        elif key == "s" and self.selected():
            try:
                added = self.library.toggle(self.selected())
                self.move(self.indices[self.tab])
                self.notify("已收藏" if added else "已取消收藏")
            except OSError:
                self.notify("无法保存收藏")
        elif key == "enter" and self.selected():
            selected = self.selected()
            active = self.station and selected.uuid == self.station.uuid
            failed = self.play_status.startswith(("连接失败", "当前环境"))
            self.tune(None if active and not failed else selected)
        elif key == "space":
            if self.station and self.play_status.startswith("连接失败"):
                self.tune(self.station)
            else:
                self.tune(None if self.station else self.selected())
        elif key in ("+", "=", "-"):
            self.volume = max(0, min(100, self.volume + (-5 if key == "-" else 5)))
            self.save_volume_at = time.monotonic() + 0.6
            self.notify(f"音量 {self.volume}%")
        elif key == "r" and not self.tab:
            self.search()
        elif key in ("h", "?"):
            self.modal = "help"
        elif key == "escape":
            self.running = False

    def close(self):
        self.running = False
        self.search_jobs.put(None)
        try:
            self.click_jobs.put_nowait(None)
        except queue.Full:
            pass
        if self.save_volume_at is not None:
            self.save_volume()
        self.player.close()


BG, PANEL, SELECT = "#10191e", "#18282f", "#263e43"
TEXT, SUB, TEAL, GOLD = "#f0f5f4", "#a9c1c4", "#83f0d0", "#ffcf78"


class Screen:
    def __init__(self, font):
        index = 2 if font.name == "NotoSansCJK-Regular.ttc" else 0
        self.fonts = {size: ImageFont.truetype(str(font), size=size, index=index)
                      for size in (10, 11, 12, 13, 14, 16)}

    def render(self, app):
        image = Image.new("RGB", SIZE, BG)
        draw = ImageDraw.Draw(image)

        def text(x, y, value, size=12, color=TEXT, width=300, anchor="lt"):
            font = self.fonts[size]
            if draw.textlength(value, font=font) > width:
                while value and draw.textlength(value + "…", font=font) > width:
                    value = value[:-1]
                value += "…"
            draw.text((x, y), value, font=font, fill=color, anchor=anchor)

        def box(coords, color, radius=4):
            draw.rounded_rectangle(coords, radius, fill=color)

        if app.modal:
            draw.rectangle((0, 0, 319, 27), fill=PANEL)
            if app.modal == "help":
                text(9, 6, "按键说明", 16, TEAL)
                for i, line in enumerate((
                    "↑ ↓ / J K   选择电台       Enter   播放／停止",
                    "← → / , .    翻页              Space   播放／停止",
                    "/   搜索     F   筛选     R   刷新     1 / 2   切换列表",
                    "S   收藏／取消收藏        − / +   音量",
                    "Esc   关闭面板／退出应用",
                )):
                    text(9, 36 + i * 22, line, 12)
                text(9, 153, "Enter / Esc 返回", 11, SUB)
            elif app.modal == "filter":
                text(9, 6, "筛选电台", 16, TEAL)
                for i, (field, title, values) in enumerate(FILTERS):
                    y = 33 + i * 26
                    active = app.filter_row == i
                    box((7, y, 312, y + 23), SELECT if active else PANEL)
                    current = getattr(app.draft, field)
                    value = dict(values).get(current, current)
                    text(14, y + 5, title, 12, TEAL if active else SUB)
                    text(303, y + 5, f"‹  {value}  ›" if active else value,
                         12, TEXT, 205, "rt")
                text(9, 144, "↑↓ 选择  ←→ 切换  E 自定义  X 重置", 11, SUB)
                text(9, 157, "Enter 应用   Esc 取消", 11, TEAL)
            else:
                field = FILTERS[app.filter_row][0] if app.modal == "filter_text" else ""
                title = FILTERS[app.filter_row][1] if field else "搜索电台"
                text(9, 6, title, 16, TEAL)
                hint = {"country": "两位国家代码，例如 CN、JP、GB",
                        "language": "输入目录语言，例如 mandarin 或 english",
                        "tag": "输入完整标签，例如 jazz 或 news"}.get(
                            field, "输入电台名称，支持部分匹配")
                text(9, 39, hint, 12, SUB)
                box((8, 61, 311, 89), SELECT)
                font = self.fonts[14]
                value = app.input
                while value and draw.textlength(value + "▏", font=font) > 283:
                    value = value[1:]
                text(16, 68, value + "▏", 14)
                if app.input_error:
                    text(9, 100, app.input_error, 12, GOLD)
                text(9, 151, "Enter 确认   Ctrl+U 清空   Esc 取消", 11, TEAL)
            return image

        draw.rectangle((0, 0, 319, 24), fill=PANEL)
        draw.arc((9, 5, 25, 21), 211, 329, fill=TEAL, width=1)
        draw.arc((12, 8, 22, 18), 213, 327, fill=TEAL, width=1)
        draw.ellipse((16, 13, 18, 15), fill=TEAL)
        text(28, 6, "WebRadio", 13)
        for i, (x, label) in enumerate(((97, "发现"), (150, "收藏"))):
            if app.tab == i:
                box((x, 3, x + 48, 21), SELECT)
            text(x + 11, 6, label, 12, TEAL if app.tab == i else SUB)
        if app.demo:
            text(208, 7, "演示", 11, GOLD)
        text(311, 7, f"音量 {app.volume}%", 11, SUB, anchor="rt")

        rows = app.rows()
        text(9, 29, "已收藏电台" if app.tab else (app.query.name or "热门电台"),
             12, TEXT, 130)
        if app.tab:
            summary = f"{len(rows)} 台"
        else:
            filters = [dict(values).get(getattr(app.query, field), getattr(app.query, field))
                       for field, _, values in FILTERS[:3] if getattr(app.query, field)]
            if app.query.order != "clickcount":
                filters.append(dict(FILTERS[-1][2])[app.query.order])
            filters.append(f"{'‹ ' if app.page else ''}{app.page + 1}{' ›' if app.more else ''}")
            summary = " · ".join(filters)
        text(311, 29, summary, 11, SUB, 160, "rt")

        offset = app.offsets[app.tab]
        for i, station in enumerate(rows[offset:offset + VISIBLE]):
            y = 44 + i * 20
            selected = offset + i == app.indices[app.tab]
            if selected:
                box((6, y, 313, y + 18), SELECT)
                draw.rounded_rectangle((6, y + 3, 8, y + 15), 1, fill=TEAL)
            playing = (app.station and station.uuid == app.station.uuid
                       and not app.play_status.startswith(("连接失败", "当前环境")))
            favorite = station.uuid in app.library.favorites
            if playing:
                draw.polygon(((13, y + 5), (13, y + 13), (19, y + 9)), fill=TEAL)
            if favorite:
                x = 24 if playing else 16
                draw.polygon(((x, y + 4), (x + 1, y + 7), (x + 5, y + 7),
                              (x + 2, y + 9), (x + 3, y + 13), (x, y + 11),
                              (x - 3, y + 13), (x - 2, y + 9), (x - 5, y + 7),
                              (x - 1, y + 7)), fill=GOLD)
            if not playing and not favorite:
                draw.point((16, y + 9), fill=SUB)
            text(33, y + 3, station.name, 12, TEXT, 192)
            info = " · ".join(part for part in (station.country, station.codec, station.bitrate)
                              if part and part != "0")
            text(306, y + 4, info, 10, SUB, 75, "rt")

        if not rows:
            if app.tab:
                empty = "在发现页选中电台，按 S 收藏"
            elif app.loading:
                empty = "正在查询 Radio Browser…"
            elif app.directory_status:
                empty = "连接失败，按 R 重试或切到收藏"
            else:
                empty = "没有匹配的电台，试试调整搜索或筛选"
            text(9, 69, empty, 13, SUB)

        box((6, 125, 313, 149), PANEL)
        draw.ellipse((13, 134, 18, 139),
                     fill=TEAL if app.play_status.startswith("播放中") else GOLD if app.station else SUB)
        text(25, 131, app.station.name if app.station else "播放器", 12, TEXT, 173)
        text(305, 132, app.play_status, 11, TEAL if app.station else SUB, 104, "rt")
        footer = app.toast or (
            "正在更新目录…" if app.loading and not app.tab and rows else
            app.directory_status if app.directory_status and not app.tab and rows else
            "/ 搜索   F 筛选   S 收藏   1/2 切换   H 帮助")
        text(9, 155, footer, 11, TEAL if app.toast else SUB)
        return image
