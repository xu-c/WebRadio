"""Keyboard interaction and 320 × 170 interface shared by device and preview."""

import queue
import threading
import time
from dataclasses import replace

from PIL import Image, ImageDraw, ImageFont

from radio_directory import PAGE_SIZE, DirectoryError, Query, Station, Superseded
from radio_player import Player

SIZE = (320, 170)
VISIBLE = 3
DIRECTIONS = {"f": "up", "z": "left", "x": "down", "c": "right",
              "k": "up", "j": "down", ",": "left", ".": "right"}
FILTERS = (
    ("country", "国家／地区", (("", "全部"), ("CN", "中国大陆"), ("TW", "台湾"), ("HK", "香港"),
                         ("MO", "澳门"), ("JP", "日本"), ("US", "美国"), ("GB", "英国"),
                         ("DE", "德国"), ("FR", "法国"))),
    ("language", "语言", (("", "全部"), ("chinese", "中文"), ("mandarin", "普通话"),
                      ("cantonese", "粤语"), ("english", "英语"), ("japanese", "日语"),
                      ("french", "法语"), ("german", "德语"))),
    ("tag", "类型", (("", "全部"), ("news", "新闻"), ("pop", "流行"), ("jazz", "爵士"),
                  ("classical", "古典"), ("talk", "谈话"), ("ambient", "氛围"))),
    ("order", "排序", (("clickcount", "收听最多"), ("clicktrend", "近期热门"),
                    ("votes", "票数最多"), ("name", "台名 A–Z"))),
)
ORDER_TITLES = {"clickcount": "热门电台", "clicktrend": "近期热门",
                "votes": "按票数", "name": "按台名"}
LANGUAGE_CODES = {"zh": "中文", "cmn": "普通话", "yue": "粤语", "en": "英语",
                  "ja": "日语", "fr": "法语", "de": "德语"}
COUNTRIES, LANGUAGES, TAGS = (dict(values) for _, _, values in FILTERS[:3])

DEMO_STATIONS = [
    Station(f"0a000000-0000-4000-8000-{index:012}", name, "https://example.com/radio",
            country=country, language=language, tags=tags)
    for index, (name, country, language, tags) in enumerate((
        ("上海动感 101", "CN", "chinese,mandarin", "pop,music"),
        ("北京音乐广播", "CN", "mandarin", "pop,music"),
        ("广州新闻电台", "CN", "cantonese", "news,talk"),
        ("SomaFM · Groove Salad", "US", "english", "ambient"),
        ("BBC World Service", "GB", "english", "news"),
        ("Jazz24", "US", "english", "jazz"),
        ("FIP Jazz", "FR", "french", "jazz,music"),
        ("NHK Radio 1", "JP", "japanese", "news"),
        ("香港电台第一台", "HK", "cantonese", "talk"),
        ("台北爱乐电台", "TW", "mandarin", "classical"),
    ))
]


def station_attributes(station):
    country = COUNTRIES.get(station.country) if station.country else None
    language = station.language or station.language_codes
    languages = LANGUAGES if station.language else LANGUAGE_CODES

    def names(value, labels):
        return tuple(dict.fromkeys(labels.get(part.casefold(), part)
                                   for item in value.split(",") if (part := item.strip())))

    return ((country or station.country_name or station.country or "—",),
            names(language, languages) or ("—",),
            names(station.tags, TAGS) or ("—",))


class RadioApp:
    def __init__(self, directory, library, demo=False):
        self.directory = directory
        self.library = library
        self.demo = demo
        self.query = library.filters
        self.page = 0
        self.more = False
        self.select_last = False
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
        self.detail_station = None
        self.detail_scroll = 0
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
                    rows = [s for s in DEMO_STATIONS
                            if query.name.casefold() in s.name.casefold()
                            and (not query.country or s.country == query.country)
                            and (not query.language or query.language in s.language)
                            and (not query.tag or query.tag in s.tags.split(","))]
                    if query.order == "name":
                        rows.sort(key=lambda station: station.name.casefold())
                    start = page * PAGE_SIZE
                    result = rows[start:start + PAGE_SIZE], len(rows) > start + PAGE_SIZE, False
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
                    self.directory_status = "离线缓存" if cached else ""
                    index = next((i for i, station in enumerate(self.results)
                                  if station.uuid == uuid), old_index)
                    if self.select_last and self.results:
                        index = len(self.results) - 1
                        self.select_last = False
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

    def show_query(self, query, page=0, select_last=False):
        if query != self.query or page != self.page:
            self.results = []
            self.more = False
            self.indices[0] = self.offsets[0] = 0
        self.query, self.page, self.tab = query, page, 0
        self.select_last = select_last
        self.search()

    def change_page(self, step, select_last=False):
        if not self.tab and ((step < 0 and self.page > 0) or (step > 0 and self.more)):
            self.show_query(self.query, self.page + step, select_last)

    def move_selection(self, step):
        current = self.indices[self.tab]
        count = len(self.rows())
        if count and (0 <= current + step < count or self.tab):
            self.move(current + step)
        elif not self.tab and (step < 0 or count):
            self.change_page(step, select_last=step < 0)

    def change_volume(self, step):
        volume = max(0, min(100, self.volume + step * 5))
        if volume != self.volume:
            self.volume = volume
            self.save_volume_at = time.monotonic() + 0.6

    def apply_filters(self):
        try:
            self.library.save(self.library.favorites, self.volume, self.draft)
        except OSError:
            self.notify("筛选已应用 · 无法保存")
        self.show_query(self.draft)
        self.modal = ""

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
        key = DIRECTIONS.get(key, key)
        if self.modal == "help":
            if key in ("8", "escape", "enter", "h", "?"):
                self.modal = ""
            return
        if self.modal == "volume":
            if key in ("left", "down", "4", "-"):
                self.change_volume(-1)
            elif key in ("right", "up", "5", "+", "="):
                self.change_volume(1)
            elif key in ("8", "enter", "escape"):
                self.modal = ""
            return
        if self.modal == "details":
            if key in ("up", "down"):
                self.detail_scroll = max(0, self.detail_scroll + (-1 if key == "up" else 1))
            elif key in ("8", "enter", "escape", "i"):
                self.modal = ""
            return
        if self.modal == "filter":
            field, _, values = FILTERS[self.filter_row]
            if key in ("up", "down"):
                step = -1 if key == "up" else 1
                self.filter_row = (self.filter_row + step) % len(FILTERS)
            elif key in ("left", "right"):
                current = getattr(self.draft, field)
                index = next((i for i, (value, _) in enumerate(values) if value == current), -1)
                step = 1 if key == "right" else -1
                index = (index + step) % len(values) if index >= 0 else 0
                self.draft = replace(self.draft, **{field: values[index][0]})
            elif key in ("6", "e") and field != "order":
                self.modal, self.input = "filter_text", getattr(self.draft, field)
            elif key == "4":
                self.draft = Query(name=self.draft.name)
            elif key == "5":
                self.draft = Query(name=self.draft.name, country="CN")
            elif key in ("7", "enter"):
                self.apply_filters()
            elif key in ("8", "escape"):
                self.modal = ""
            return

        if key in ("up", "down"):
            self.move_selection(-1 if key == "up" else 1)
        elif key in ("left", "right"):
            self.change_page(-1 if key == "left" else 1)
        elif key in ("tab", "7", "1", "2"):
            self.tab = 1 - self.tab if key in ("tab", "7") else int(key) - 1
        elif key in ("4", "/"):
            self.modal, self.input = "search", self.query.name
        elif key == "5":
            self.modal, self.draft, self.filter_row = "filter", self.query, 0
        elif key in ("6", "s") and self.selected():
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
            self.change_volume(-1 if key == "-" else 1)
            self.notify(f"音量 {self.volume}%")
        elif key == "8":
            self.modal = "volume"
        elif key == "i" and self.selected():
            self.modal = "details"
            self.detail_station = self.selected()
            self.detail_scroll = 0
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
                      for size in (10, 11, 12, 13, 14, 16, 28)}

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

        def softkeys(labels):
            # The centers follow the physical 4–8 keys under the Cardputer Zero display.
            draw.rectangle((0, 146, 319, 169), fill=PANEL)
            draw.line((0, 146, 319, 146), fill=SELECT)
            for key, x, label in zip("45678", (48, 104, 160, 216, 272), labels):
                if label:
                    text(x, 149, label, 12, TEAL, 54, "mt")
                    text(x, 162, key, 10, SUB, 54, "mt")

        def wrap(value, width, size):
            line = ""
            for char in value:
                if line and draw.textlength(line + char, font=self.fonts[size]) > width:
                    yield line.rstrip()
                    line = ""
                line += char
            if line:
                yield line

        if app.modal:
            draw.rectangle((0, 0, 319, 27), fill=PANEL)
            if app.modal == "help":
                text(9, 6, "按键说明", 16, TEAL)
                for i, line in enumerate((
                    "F / X   上下选台，越过页尾或页首时翻页",
                    "Z / C   上一页／下一页     I   电台详情",
                    "Enter   播放／停止     R   刷新目录",
                    "4 搜索   5 筛选   6 收藏   7 切换   8 音量",
                    "Esc   关闭面板／退出应用",
                )):
                    text(9, 33 + i * 20, line, 12)
                softkeys(("", "", "", "", "返回"))
            elif app.modal == "filter":
                text(9, 6, "筛选电台", 16, TEAL)
                text(311, 9, "F/X 选择 · Z/C 切换", 11, SUB, anchor="rt")
                for i, (field, title, values) in enumerate(FILTERS):
                    y = 33 + i * 26
                    active = app.filter_row == i
                    box((7, y, 312, y + 23), SELECT if active else PANEL)
                    current = getattr(app.draft, field)
                    value = dict(values).get(current, current)
                    text(14, y + 5, title, 12, TEAL if active else SUB)
                    text(303, y + 5, f"‹  {value}  ›" if active else value,
                         12, TEXT, 205, "rt")
                softkeys(("清空", "默认", "自定义" if app.filter_row < 3 else "", "应用", "取消"))
            elif app.modal == "volume":
                text(9, 6, "音量", 16, TEAL)
                text(160, 46, f"{app.volume}%", 28, TEXT, anchor="mt")
                box((30, 89, 289, 101), SELECT, 6)
                if app.volume:
                    box((30, 89, 30 + round(259 * app.volume / 100), 101), TEAL, 6)
                text(160, 115, "Z / C 或 4 / 5 调节", 12, SUB, anchor="mt")
                softkeys(("音量−", "音量+", "", "", "返回"))
            elif app.modal == "details":
                text(9, 6, "电台详情", 16, TEAL)
                station = app.detail_station
                lines = list(wrap(station.name, 300, 12))
                for (_, label, _), values in zip(FILTERS[:3], station_attributes(station)):
                    lines.extend(wrap(f"{label}　{'、'.join(values)}", 300, 12))
                limit = 6
                app.detail_scroll = min(app.detail_scroll, max(len(lines) - limit, 0))
                for i, line in enumerate(lines[app.detail_scroll:app.detail_scroll + limit]):
                    text(9, 33 + i * 16, line, 12)
                if len(lines) > limit:
                    text(311, 132, "F/X 滚动", 11, SUB, anchor="rt")
                softkeys(("", "", "", "", "返回"))
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
        draw.line((11, 10, 21, 5), fill=TEAL)
        draw.rounded_rectangle((8, 10, 23, 19), 2, fill=TEAL)
        for y in (12, 14, 16):
            draw.line((10, y, 15, y), fill=PANEL)
        draw.ellipse((17, 12, 21, 16), fill=PANEL)
        draw.point((20, 13), fill=GOLD)
        text(28, 6, "WebRadio", 13)
        for i, (x, label) in enumerate(((97, "发现"), (150, "收藏"))):
            if app.tab == i:
                box((x, 3, x + 48, 21), SELECT)
            text(x + 11, 6, label, 12, TEAL if app.tab == i else SUB)
        if app.demo:
            text(208, 7, "演示", 11, GOLD)
        text(311, 7, f"音量 {app.volume}%", 11, SUB, anchor="rt")

        rows = app.rows()
        if app.tab:
            title, pagination = "已收藏电台", f"{len(rows)} 台"
        else:
            order = ORDER_TITLES[app.query.order]
            area = COUNTRIES.get(app.query.country, app.query.country)
            title = f"搜索：{app.query.name}" if app.query.name else (
                f"{area} · {order}" if area else order)
            page_parts = (["Z 上一页"] if app.page else []) + [f"第 {app.page + 1} 页"]
            if app.more:
                page_parts.append("C 下一页")
            pagination = " · ".join(page_parts)
        text(9, 28, title, 12, TEXT, 151)
        text(311, 29, pagination, 11, SUB, 151, "rt")

        offset = app.offsets[app.tab]
        for i, station in enumerate(rows[offset:offset + VISIBLE]):
            y = 43 + i * 27
            selected = offset + i == app.indices[app.tab]
            if selected:
                box((6, y, 313, y + 25), SELECT)
                draw.rounded_rectangle((6, y + 4, 8, y + 21), 1, fill=TEAL)
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
            text(33, y + 2, station.name, 12, TEXT, 274)
            country, languages, tags = station_attributes(station)
            text(33, y + 15, country[0], 10, SUB, 66)
            text(104, y + 15, "·", 10, SUB)
            text(115, y + 15, "、".join(languages), 10, SUB, 78)
            text(199, y + 15, "·", 10, SUB)
            text(210, y + 15, "、".join(tags), 10, SUB, 96)

        if not rows:
            if app.tab:
                empty = "在发现页选中电台，按 6 收藏"
            elif app.loading:
                empty = "正在查询 Radio Browser…"
            elif app.directory_status and app.directory_status != "离线缓存":
                empty = "连接失败，按 R 重试或切到收藏"
            else:
                empty = "没有匹配的电台，试试调整搜索或筛选"
            text(9, 69, empty, 13, SUB)

        status = app.toast or (
            "正在更新目录…" if app.loading and not app.tab and rows else
            app.directory_status if not app.tab and rows else "")
        if status:
            text(9, 130, status, 11, TEAL if app.toast else SUB, 246)
        elif app.station:
            draw.ellipse((9, 132, 13, 136),
                         fill=TEAL if app.play_status.startswith("播放中") else GOLD)
            text(19, 129, app.station.name, 11, TEXT, 131)
            text(258, 129, app.play_status, 11, SUB, 103, "rt")
        else:
            text(9, 130, "F/X 选台 · Enter 播放", 11, SUB)
        selected = app.selected()
        text(311, 130, "I 详情" if selected else "H 帮助", 11, SUB, anchor="rt")
        saved = selected and selected.uuid in app.library.favorites
        softkeys(("搜索", "筛选", "取消收藏" if saved else "收藏",
                  "回发现" if app.tab else "收藏夹", "音量"))
        return image
