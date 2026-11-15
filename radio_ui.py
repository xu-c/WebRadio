"""Keyboard interaction and 320 × 170 interface shared by device and preview."""

import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace

from PIL import Image, ImageDraw, ImageFont

from radio_directory import PAGE_SIZE, DirectoryError, Query, Station, Superseded, pinyin_matches
from radio_player import Player

SIZE = (320, 170)
VISIBLE = 3
EXIT_HOLD = 3
SEARCH_CANCEL_HINT = 10
SEARCH_WORKERS = 2
DIGIT_SHORTCUTS = "45678"
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
ENGLISH_LABELS = {
    "全部": "All", "中国大陆": "Mainland China", "台湾": "Taiwan", "香港": "Hong Kong",
    "澳门": "Macao", "日本": "Japan", "美国": "United States", "英国": "United Kingdom",
    "德国": "Germany", "法国": "France", "中文": "Chinese", "普通话": "Mandarin",
    "粤语": "Cantonese", "英语": "English", "日语": "Japanese", "法语": "French",
    "德语": "German", "新闻": "News", "流行": "Pop", "爵士": "Jazz",
    "古典": "Classical", "谈话": "Talk", "氛围": "Ambient", "收听最多": "Most played",
    "近期热门": "Trending", "票数最多": "Most votes", "台名 A–Z": "Station name A–Z",
    "热门电台": "Popular", "按票数": "By votes", "按台名": "By name",
    "国家／地区": "Country / region", "语言": "Language", "类型": "Genre", "排序": "Sort",
    "离线缓存": "Offline cache", "Radio Browser 暂时无法连接": "Radio Browser unavailable",
    "无法保存音量": "Could not save volume", "无法保存收藏": "Could not save favorites",
    "无法保存语言": "Could not save language",
    "筛选已应用 · 无法保存": "Filters applied · Could not save",
    "已收藏": "Saved to favorites", "已取消收藏": "Removed from favorites",
    "请输入两位国家代码，例如 CN": "Enter a two-letter country code, e.g. CN.",
    "尚未播放": "Not playing", "已停止": "Stopped", "正在连接": "Connecting",
    "缓冲中": "Buffering", "播放中": "Playing", "播放中 · 演示": "Playing · Demo",
    "连接失败 · Enter 重试": "Failed · Enter retry", "当前环境需要 Linux 和 mpv": "Needs Linux + mpv",
    "未命名电台": "Unnamed station",
    "尚无上次收听": "No previous station",
    "无法保存上次收听": "Could not save last station",
    "拼音搜索需要 pypinyin": "Pinyin search requires pypinyin",
    "长按 Esc 3 秒退出": "Hold Esc for 3 seconds to quit",
    "搜索已取消": "Search canceled",
}

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


@dataclass
class SearchOrigin:
    query: Query
    tab: int
    page: int
    results: list
    more: bool
    index: int
    offset: int
    directory_status: str
    loading: bool


class RadioApp:
    def __init__(self, directory, library, demo=False, system_volume=None):
        self.directory = directory
        self.library = library
        self.demo = demo
        self.filters = self.query = library.filters
        self.page = 0
        self.more = False
        self.select_last = False
        self.results = []
        self.tab = 0
        self.indices = [0, 0]
        self.offsets = [0, 0]
        self.loading = False
        self.search_started = 0.0
        self.search_elapsed = 0
        self.search_canceled = False
        self.directory_status = ""
        self.modal = ""
        self.draft = self.query
        self.filter_row = 0
        self.input = ""
        self.input_error = ""
        self.search_global = False
        self.search_pinyin = False
        self.search_origin = None
        self.language_error = ""
        self.detail_station = None
        self.detail_scroll = 0
        self.toast = ""
        self.toast_until = 0
        self.volume = library.volume
        self.volume_row = 0
        self.system_volume = None
        self.system_volume_error = False
        self.system_volume_control = system_volume
        self.system_volume_worker = ThreadPoolExecutor(max_workers=1) if system_volume else None
        self.system_volume_request = None
        self.battery = None
        self.save_volume_at = None
        self.station = None
        self.now_playing = ""
        self.play_status = "尚未播放"
        self.escape_started = None
        self.search_id = 0
        self.play_id = 0
        self.events = queue.SimpleQueue()
        self.search_jobs = queue.Queue()
        self.click_jobs = queue.Queue(maxsize=8)
        self.running = True
        self.dirty = True
        self.player = Player(lambda: self.volume, self.play_event, self.count_play, demo,
                             publish_title=lambda generation, title: self.events.put(
                                 ("title", generation, title)))
        for _ in range(SEARCH_WORKERS):
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
        self.search_started = time.monotonic()
        self.search_elapsed = 0
        self.search_canceled = False
        self.directory_status = ""
        self.search_jobs.put((self.search_id, self.query, self.page))

    def cancel_search(self):
        self.search_id += 1
        self.loading = False
        self.search_canceled = True
        self.directory_status = ""
        self.dirty = True

    def leave_search(self):
        self.modal = ""
        origin = self.search_origin
        if origin is None:
            return
        canceled = self.loading
        self.cancel_search()
        self.search_origin = None
        self.select_last = False
        self.query, self.page, self.tab = origin.query, origin.page, origin.tab
        self.results, self.more = origin.results, origin.more
        self.indices[0], self.offsets[0] = origin.index, origin.offset
        self.directory_status = origin.directory_status
        self.search_canceled = False
        if origin.query != self.filters:
            self.show_query(self.filters)
            self.tab = origin.tab
        elif origin.loading:
            self.search()
        if canceled:
            self.notify("搜索已取消")

    def search_worker(self):
        while True:
            job = self.search_jobs.get()
            if job is None:
                return
            generation, query, page = job

            def canceled():
                return generation != self.search_id or not self.running

            if canceled():
                continue
            try:
                if self.demo:
                    def matches_name(station):
                        if query.pinyin and query.name:
                            return pinyin_matches(station.name, query.name)
                        return query.name.casefold() in station.name.casefold()

                    rows = [s for s in DEMO_STATIONS
                            if matches_name(s)
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
            except ImportError:
                self.events.put(("search", generation, "拼音搜索需要 pypinyin"))
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
        if self.escape_started is not None:
            self.dirty = True
            if now - self.escape_started >= EXIT_HOLD:
                self.running = False
                return
        while not self.events.empty():
            kind, generation, result = self.events.get()
            if kind == "play" and generation == self.play_id:
                self.play_status = result
                if (result.startswith("播放中") and self.station
                        and self.station != self.library.last_station):
                    try:
                        self.library.save(self.library.favorites, self.volume, last_station=self.station)
                    except OSError:
                        self.notify("无法保存上次收听")
                self.dirty = True
            if kind == "title" and generation == self.play_id:
                self.now_playing = result
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
        if self.loading:
            elapsed = int(now - self.search_started)
            if elapsed != self.search_elapsed:
                self.search_elapsed = elapsed
                self.dirty = True
        if self.toast and now >= self.toast_until:
            self.toast = ""
            self.dirty = True
        if self.save_volume_at is not None and now >= self.save_volume_at:
            self.save_volume()
        if self.system_volume_request is not None and self.system_volume_request.done():
            try:
                self.system_volume = self.system_volume_request.result()
            except (OSError, ValueError):
                self.system_volume = None
            self.system_volume_error = self.system_volume is None
            self.system_volume_request = None
            self.dirty = True

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
        self.now_playing = ""
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

    def request_system_volume(self, step=0):
        if self.system_volume_worker is not None:
            self.system_volume_error = False
            self.system_volume_request = self.system_volume_worker.submit(self.system_volume_control, step)

    def apply_filters(self):
        self.filters = replace(self.draft, name="", pinyin=False)
        try:
            self.library.save(self.library.favorites, self.volume, self.filters)
        except OSError:
            self.notify("筛选已应用 · 无法保存")
        self.show_query(self.draft)
        self.modal = ""

    def set_interface_language(self, language):
        try:
            self.library.save(self.library.favorites, self.volume, interface_language=language)
            self.language_error = ""
        except OSError:
            self.language_error = "无法保存语言"

    def edit(self, key, char, clear):
        searching = self.modal == "search"
        if key in ("escape", "8"):
            if searching:
                self.leave_search()
            else:
                self.modal = "filter"
        elif searching and key in ("4", "tab"):
            self.search_global = not self.search_global
        elif searching and key in ("5", "up", "ctrl+p"):
            self.search_pinyin = not self.search_pinyin
        elif key in ("7", "enter"):
            value = self.input.strip()
            if searching:
                if not value:
                    self.leave_search()
                    return
                if self.search_origin is None:
                    self.search_origin = SearchOrigin(
                        self.query, self.tab, self.page, self.results, self.more,
                        self.indices[0], self.offsets[0], self.directory_status, self.loading)
                filters = self.filters
                query = Query(order=filters.order) if self.search_global else filters
                self.show_query(replace(query, name=value, pinyin=self.search_pinyin))
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
        elif clear or key == "6":
            self.input = ""
        elif key == "4":
            self.input = self.input[:-1]
        elif key == "5":
            self.input = getattr(self.draft, FILTERS[self.filter_row][0])
        elif key == "backspace":
            self.input = self.input[:-1]
        elif char and char.isprintable():
            self.input = (self.input + char)[:80]
        self.input_error = ""

    def escape_key(self, pressed):
        if pressed:
            if self.escape_started is None:
                self.escape_started = time.monotonic()
        elif self.escape_started is not None:
            elapsed = time.monotonic() - self.escape_started
            self.escape_started = None
            if elapsed >= EXIT_HOLD:
                self.running = False
            else:
                self.handle("escape")
        self.dirty = True

    def handle(self, key, char="", clear=False):
        self.dirty = True
        if self.modal in ("search", "filter_text"):
            if len(key) == 2 and key[0] == "f" and key[1] in DIGIT_SHORTCUTS:
                key, char = "", key[-1]
            self.edit(key, char, clear)
            return
        key = DIRECTIONS.get(key, key)
        if self.modal == "help":
            if key == "7":
                self.modal = "about"
            elif key == "6":
                self.modal = "language"
                self.language_error = ""
            elif key in ("8", "escape", "enter", "h", "?"):
                self.modal = ""
            return
        if self.modal == "about":
            if key in ("8", "escape", "enter"):
                self.modal = "help"
            return
        if self.modal == "language":
            if key in ("4", "left"):
                self.set_interface_language("zh")
            elif key in ("5", "right"):
                self.set_interface_language("en")
            elif key in ("8", "escape", "enter"):
                self.modal = "help"
            return
        if self.modal == "volume":
            if key in ("up", "down"):
                self.volume_row = 0 if key == "up" else 1
            elif key in ("left", "right"):
                step = -1 if key == "left" else 1
                if self.volume_row == 0:
                    self.change_volume(step)
                else:
                    self.request_system_volume(step)
            elif key in ("4", "-"):
                self.change_volume(-1)
            elif key in ("5", "+", "="):
                self.change_volume(1)
            elif key in ("6", "7"):
                self.request_system_volume(-1 if key == "6" else 1)
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
                self.draft = Query(name=self.draft.name, pinyin=self.draft.pinyin)
            elif key == "5":
                self.draft = Query(name=self.draft.name, country="CN", pinyin=self.draft.pinyin)
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
            self.search_global = False
            self.search_pinyin = self.query.pinyin
        elif key == "5":
            self.modal, self.filter_row = "filter", 0
            self.draft = replace(self.filters, name=self.query.name, pinyin=self.query.pinyin)
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
        elif key == "q":
            if self.library.last_station:
                if self.station != self.library.last_station or not self.play_status.startswith("播放中"):
                    self.tune(self.library.last_station)
            else:
                self.notify("尚无上次收听")
        elif key in ("+", "=", "-"):
            self.change_volume(-1 if key == "-" else 1)
            self.notify(f"音量 {self.volume}%")
        elif key == "8" and self.search_origin is not None and not self.tab:
            self.leave_search()
        elif key in ("8", "v"):
            self.modal = "volume"
            self.volume_row = 0
            self.request_system_volume()
        elif key == "i" and self.selected():
            self.modal = "details"
            self.detail_station = self.selected()
            self.detail_scroll = 0
        elif key == "r" and not self.tab:
            self.search()
        elif key in ("h", "?"):
            self.modal = "help"
        elif key == "escape":
            if self.search_origin is not None:
                self.leave_search()
            elif self.loading and not self.tab:
                self.cancel_search()
            else:
                self.notify("长按 Esc 3 秒退出")

    def close(self):
        self.running = False
        for _ in range(SEARCH_WORKERS):
            self.search_jobs.put(None)
        try:
            self.click_jobs.put_nowait(None)
        except queue.Full:
            pass
        if self.save_volume_at is not None:
            self.save_volume()
        if self.system_volume_worker is not None:
            self.system_volume_worker.shutdown()
        self.player.close()


BG, PANEL, SELECT = "#101524", "#1b2338", "#26394c"
TEXT, SUB, TEAL, GOLD = "#f9f3e8", "#bac6d9", "#7fe4ce", "#ffc47c"
BLUE, VIOLET, ROSE = "#9fc9ff", "#c9b2ff", "#ff9d9c"
BORDER, SELECT_BORDER = "#303b56", "#527d89"
KEY_COLORS = (BLUE, VIOLET, GOLD, TEAL, SUB)
KEY_FILLS = ("#263752", "#352e50", "#493a34", "#24433f", "#30374b")


class Screen:
    def __init__(self, font):
        index = 2 if font.name == "NotoSansCJK-Regular.ttc" else 0
        self.fonts = {size: ImageFont.truetype(str(font), size=size, index=index)
                      for size in (10, 11, 12, 13, 14, 16)}
        self.background = Image.new("RGB", SIZE, BG)
        backdrop = ImageDraw.Draw(self.background)
        for y in range(SIZE[1]):
            backdrop.line((0, y, 319, y), fill=(16 + y // 34, 21 + y // 42, 36 + y // 24))

    def render(self, app):
        image = self.background.copy()
        draw = ImageDraw.Draw(image)
        english = app.library.interface_language == "en"
        separator = ", " if english else "、"

        def t(chinese, english_text=None):
            if not english:
                return chinese
            if english_text is not None:
                return english_text
            if chinese.startswith("音量 "):
                return "Volume " + chinese.removeprefix("音量 ")
            if chinese.startswith("重连 "):
                return "Retry " + chinese.removeprefix("重连 ").replace(" 秒", "s")
            return ENGLISH_LABELS.get(chinese, chinese)

        def text(x, y, value, size=12, color=TEXT, width=300, anchor="lt"):
            font = self.fonts[size]
            if draw.textlength(value, font=font) > width:
                while value and draw.textlength(value + "…", font=font) > width:
                    value = value[:-1]
                value += "…"
            draw.text((x, y), value, font=font, fill=color, anchor=anchor)

        def box(coords, color, radius=4, outline=None):
            draw.rounded_rectangle(coords, radius, fill=color, outline=outline)

        def header(height):
            for y in range(height):
                draw.line((0, y, 319, y), fill=(37 - y // 3, 46 - y // 3, 69 - y // 2))
            draw.line((0, height - 1, 319, height - 1), fill=BORDER)

        def modal_title(label, color=TEAL):
            draw.rounded_rectangle((8, 7, 10, 21), 1, fill=color)
            text(16, 6, label, 16, TEXT, 285)

        def softkeys(labels):
            # The centers follow the physical 4–8 keys under the Cardputer Zero display.
            draw.rectangle((0, 146, 319, 169), fill="#0e1321")
            draw.line((0, 146, 319, 146), fill=BORDER)
            keys = zip(DIGIT_SHORTCUTS, (48, 104, 160, 216, 272), labels, KEY_COLORS, KEY_FILLS)
            for key, x, label, color, fill in keys:
                if label:
                    box((x - 26, 149, x + 26, 167), fill, 4)
                    width = 40
                    size = 11 if draw.textlength(label, font=self.fonts[11]) <= width else 10
                    text(x - 23, 154, key, 10, color, 8)
                    text(x + 5, 153, label, size, TEXT, width, "mt")
            if app.escape_started is not None:
                progress = min(1, (time.monotonic() - app.escape_started) / EXIT_HOLD)
                draw.rectangle((0, 146, 319, 169), fill=PANEL)
                text(160, 150, t("长按 Esc 3 秒退出"), 11, TEAL, anchor="mt")
                draw.line((0, 168, round(319 * progress), 168), fill=TEAL, width=2)

        def wrap(value, width, size):
            line = ""
            for char in value:
                if line and draw.textlength(line + char, font=self.fonts[size]) > width:
                    split = line.rfind(" ") if english else -1
                    if split > 0:
                        yield line[:split].rstrip()
                        line = line[split + 1:]
                    else:
                        yield line.rstrip()
                        line = ""
                    if not line and char == " ":
                        continue
                line += char
            if line:
                yield line

        if app.modal:
            header(28)
            if app.modal == "help":
                modal_title(t("按键说明", "Controls"), BLUE)
                for i, line in enumerate((
                    t("F / X   上下选台，越过页尾或页首时翻页",
                      "F / X   Select station or cross page boundary"),
                    t("Z / C   上一页／下一页     I   电台详情",
                      "Z / C   Previous / next page     I   Details"),
                    t("Enter  播放／停止  Q  续听  R  刷新",
                      "Enter  Play / stop   Q  Resume   R  Refresh"),
                    t("4 搜索   5 筛选   6 收藏   7 切换   8 音量",
                      "4 Search   5 Filter   6 Save   7 View   8 Vol"),
                    t("Esc 返回 · 长按 3 秒退出", "Esc Back · Hold 3 seconds to quit"),
                )):
                    y = 32 + i * 21
                    box((8, y, 311, y + 18), PANEL, 4)
                    text(15, y + 4, line, 12, TEXT, 289)
                softkeys(("", "", t("语言", "Lang."), t("应用说明", "About"), t("返回", "Back")))
            elif app.modal == "about":
                modal_title(t("应用说明", "About WebRadio"), BLUE)
                text(9, 36, t("电台目录数据来自 Radio Browser。", "Station directory data comes from Radio Browser."))
                text(9, 54, "https://www.radio-browser.info/", 12, TEAL)
                for y, paragraph in (
                    (75, t("节目由各电台提供，作者不对电台内容负责。",
                           "Stations provide their own programming. The author is not responsible for station content.")),
                    (111, t("使用时请遵守 Radio Browser 的使用规则以及你所在地的法律法规。",
                            "Follow the Radio Browser rules and the laws that apply where you use the app.")),
                ):
                    for i, line in enumerate(wrap(paragraph, 299, 12)):
                        text(9, y + i * 15, line)
                softkeys(("", "", "", "", t("返回", "Back")))
            elif app.modal == "language":
                modal_title(t("界面语言", "Interface language"), VIOLET)
                for i, (code, label) in enumerate((("zh", "简体中文"), ("en", "English"))):
                    y = 39 + i * 33
                    selected = app.library.interface_language == code
                    box((8, y, 311, y + 27), SELECT if selected else PANEL,
                        5, SELECT_BORDER if selected else BORDER)
                    text(16, y + 7, label, 13)
                    if selected:
                        text(302, y + 8, t("当前", "Current"), 11, TEAL, anchor="rt")
                if app.language_error:
                    text(9, 115, t(app.language_error), 11, GOLD)
                softkeys(("中文", "English", "", "", t("返回", "Back")))
            elif app.modal == "filter":
                modal_title(t("筛选电台", "Station filters"), VIOLET)
                text(311, 9, t("F/X 选择 · Z/C 切换", "F/X Select · Z/C Change"),
                     11, SUB, anchor="rt")
                for i, (field, title, values) in enumerate(FILTERS):
                    y = 33 + i * 26
                    active = app.filter_row == i
                    tone = (BLUE, VIOLET, GOLD, TEAL)[i]
                    box((8, y, 311, y + 22), SELECT if active else PANEL,
                        5, SELECT_BORDER if active else BORDER)
                    if active:
                        draw.rounded_rectangle((10, y + 4, 12, y + 18), 1, fill=tone)
                    current = getattr(app.draft, field)
                    labels = dict(values)
                    value = t(labels[current]) if current in labels else current
                    text(19, y + 5, t(title), 12, tone if active else SUB)
                    text(303, y + 5, f"‹  {value}  ›" if active else value,
                         12, TEXT, 205, "rt")
                softkeys((t("清空", "Clear"), t("默认", "Default"),
                          t("自定义", "Custom") if app.filter_row < 3 else "",
                          t("应用", "Apply"), t("取消", "Cancel")))
            elif app.modal == "volume":
                modal_title(t("音量", "Volume"), GOLD)
                for index, (y, label, volume, color) in enumerate((
                    (32, t("应用音量", "App volume"), app.volume, TEAL),
                    (79, t("系统音量", "System volume"), app.system_volume, GOLD),
                )):
                    selected = index == app.volume_row
                    box((9, y, 310, y + 42), SELECT if selected else PANEL, 6,
                        color if selected else BORDER)
                    text(18, y + 5, label, 12, color)
                    text(301, y + 4, "—" if volume is None else f"{volume}%", 14, TEXT, anchor="rt")
                    for segment in range(20):
                        x = 18 + 14 * segment
                        fill = color if volume is not None and segment * 5 < volume else BORDER
                        box((x, y + 28, x + 11, y + 34), fill, 2)
                if app.system_volume_worker is None or app.system_volume_error:
                    hint = t("系统音量暂不可用", "System volume unavailable")
                elif app.system_volume_request is not None:
                    hint = t("正在获取系统音量…", "Checking system volume…")
                else:
                    hint = t("F / X 选择    Z / C 调节", "F / X Select    Z / C Adjust")
                text(160, 127, hint, 11, SUB, anchor="mt")
                softkeys((t("应用−", "App −"), t("应用+", "App +"),
                          t("系统−", "Sys −"), t("系统+", "Sys +"), t("返回", "Back")))
            elif app.modal == "details":
                modal_title(t("电台详情", "Station details"), BLUE)
                station = app.detail_station
                lines = list(wrap(station.name if station.name != "未命名电台" else t(station.name), 300, 12))
                for (_, label, _), values in zip(FILTERS[:3], station_attributes(station)):
                    lines.extend(wrap(f"{t(label)}   {separator.join(map(t, values))}", 300, 12))
                limit = 6
                app.detail_scroll = min(app.detail_scroll, max(len(lines) - limit, 0))
                for i, line in enumerate(lines[app.detail_scroll:app.detail_scroll + limit]):
                    text(9, 33 + i * 16, line, 12)
                if len(lines) > limit:
                    text(311, 132, t("F/X 滚动", "F/X Scroll"), 11, SUB, anchor="rt")
                softkeys(("", "", "", "", t("返回", "Back")))
            else:
                field = FILTERS[app.filter_row][0] if app.modal == "filter_text" else ""
                title = t(FILTERS[app.filter_row][1]) if field else t("搜索电台", "Search stations")
                modal_title(title, BLUE)
                hint = {"country": t("两位国家代码，例如 CN、JP、GB", "Two-letter country code, e.g. CN, JP or GB."),
                        "language": t("输入目录语言，例如 mandarin 或 english", "Language, e.g. mandarin or english."),
                        "tag": t("输入完整标签，例如 jazz 或 news", "Exact tag, e.g. jazz or news.")}.get(
                            field, t("输入电台名称，支持部分匹配", "Enter all or part of a station name."))
                if app.modal == "search":
                    scope = t("全球", "Worldwide")
                    if not app.search_global:
                        filters = app.filters
                        values = [t(labels.get(value, value)) for labels, value in (
                            (COUNTRIES, filters.country), (LANGUAGES, filters.language),
                            (TAGS, filters.tag)) if value]
                        scope = " · ".join(values) or t("全部电台", "All stations")
                    box((8, 33, 311, 51), PANEL, 4, BORDER)
                    text(15, 37, t(f"范围：{scope}", f"Scope: {scope}"), 11, BLUE, 284)
                    if app.search_pinyin:
                        hint = t("请输入全拼，例如 zhongguozhisheng",
                                 "Type full pinyin, e.g. zhongguozhisheng")
                    text(9, 99, hint, 11, SUB)
                    mode = t("全拼", "Pinyin") if app.search_pinyin else t("台名", "Name")
                    mode_width = round(draw.textlength(mode, font=self.fonts[11])) + 14
                    box((311 - mode_width, 7, 311, 23), KEY_FILLS[1], 4)
                    text(304, 10, mode, 11, VIOLET, anchor="rt")
                else:
                    text(9, 39, hint, 12, SUB)
                box((8, 59, 311, 91), "#131d30", 6, SELECT_BORDER)
                draw.line((15, 90, 304, 90), fill=BLUE)
                font = self.fonts[14]
                value = app.input
                while value and draw.textlength(value + "▏", font=font) > 283:
                    value = value[1:]
                text(16, 68, value + "▏", 14)
                if app.input_error:
                    text(9, 100, t(app.input_error), 12, GOLD)
                text(9, 127, t("输入数字 4–8：Fn + 对应数字键",
                               "To type digits 4–8, use Fn + number"), 11, SUB)
                if app.modal == "search":
                    scope_action = t("当前范围", "Filters") if app.search_global else t("全球", "Global")
                    mode_action = t("台名", "Name") if app.search_pinyin else t("全拼", "Pinyin")
                    softkeys((scope_action, mode_action, t("清空", "Clear"),
                              t("搜索", "Search"), t("返回", "Back")))
                else:
                    softkeys((t("退格", "Delete"), t("还原", "Restore"), t("清空", "Clear"),
                              t("确认", "Done"), t("返回", "Back")))
            return image

        header(25)
        draw.line((11, 10, 21, 5), fill=GOLD)
        draw.rounded_rectangle((8, 10, 23, 19), 2, fill=GOLD)
        for y in (12, 14, 16):
            draw.line((10, y, 15, y), fill=PANEL)
        draw.ellipse((17, 12, 21, 16), fill=PANEL)
        draw.point((20, 13), fill=TEXT)
        text(28, 6, "WebRadio", 13)
        for i, (x, label, color, fill) in enumerate((
            (97, t("发现", "Browse"), BLUE, KEY_FILLS[0]),
            (150, t("收藏", "Saved"), GOLD, KEY_FILLS[2]),
        )):
            if app.tab == i:
                box((x, 3, x + 48, 21), fill, 5, BORDER)
            text(x + 24, 6, label, 12, color if app.tab == i else SUB, 46, "mt")
        if app.demo:
            text(208, 7, t("演示", "Demo"), 11, GOLD)
        else:
            text(208, 7, t("H 帮助", "H Help"), 11, SUB)
        box((264, 4, 314, 20), "#192337", 4)
        battery = app.battery
        charge_color = SUB if battery is None else ROSE if battery <= 15 else TEAL
        draw.rectangle((269, 8, 280, 16), outline=charge_color)
        draw.line((281, 11, 281, 13), fill=charge_color)
        if battery:
            width = max(1, round(10 * battery / 100))
            draw.rectangle((270, 10, 269 + width, 14), fill=charge_color)
        text(311, 7, "—" if battery is None else f"{battery}%", 10, charge_color, anchor="rt")

        rows = app.rows()
        waiting = app.loading and not app.tab
        if app.tab:
            title = t("已收藏电台", "Saved stations")
            pagination = t(f"{len(rows)} 台", f"{len(rows)} {'station' if len(rows) == 1 else 'stations'}")
        else:
            order = t(ORDER_TITLES[app.query.order])
            area = t(COUNTRIES[app.query.country]) if app.query.country in COUNTRIES and app.query.country else app.query.country
            title = t(f"搜索：{app.query.name}", f"Search: {app.query.name}") if app.query.name else (
                f"{area} · {order}" if area else order)
            page = app.page + 1
            page_parts = ([t("Z 上一页", "Z Prev")] if app.page else []) + [t(f"第 {page} 页", f"Page {page}")]
            if app.more:
                page_parts.append(t("C 下一页", "C Next"))
            pagination = " · ".join(page_parts)
            if waiting and rows:
                if app.search_elapsed >= SEARCH_CANCEL_HINT:
                    seconds = app.search_elapsed
                    pagination = t(f"{seconds} 秒 · Esc 取消", f"{seconds}s · Esc Cancel")
                else:
                    dots = "." * (1 + app.search_elapsed % 3)
                    pagination = t("更新目录", "Refreshing") + dots
            elif app.search_canceled and rows:
                pagination = t("已取消 · R 重试", "Canceled · R Retry")
        text(9, 28, title, 12, BLUE if app.query.name and not app.tab else TEXT, 151)
        delayed = waiting and app.search_elapsed >= SEARCH_CANCEL_HINT
        text(311, 29, pagination, 11, GOLD if delayed else SUB, 151, "rt")

        offset = app.offsets[app.tab]
        for i, station in enumerate(rows[offset:offset + VISIBLE]):
            y = 41 + i * 24
            selected = offset + i == app.indices[app.tab]
            playing = (app.station and station.uuid == app.station.uuid
                       and not app.play_status.startswith(("连接失败", "当前环境")))
            favorite = station.uuid in app.library.favorites
            box((6, y, 313, y + 22), SELECT if selected else PANEL,
                4, SELECT_BORDER if selected else BORDER)
            if selected:
                draw.rounded_rectangle((7, y + 3, 9, y + 18), 1, fill=TEAL)
            else:
                draw.line((8, y + 4, 8, y + 17), fill=BORDER)
            if playing:
                draw.polygon(((13, y + 5), (13, y + 13), (19, y + 9)), fill=TEAL)
            if favorite:
                x = 24 if playing else 16
                draw.polygon(((x, y + 4), (x + 1, y + 7), (x + 5, y + 7),
                              (x + 2, y + 9), (x + 3, y + 13), (x, y + 11),
                              (x - 3, y + 13), (x - 2, y + 9), (x - 5, y + 7),
                              (x - 1, y + 7)), fill=GOLD)
            if not playing and not favorite:
                draw.ellipse((15, y + 7, 18, y + 10), outline=BLUE if selected else BORDER)
            text(33, y + 1, station.name if station.name != "未命名电台" else t(station.name), 11, TEXT, 274)
            country, languages, tags = station_attributes(station)
            text(33, y + 12, t(country[0]), 10, BLUE if selected else SUB, 79)
            text(115, y + 12, "·", 10, SUB)
            text(125, y + 12, separator.join(map(t, languages)), 10, VIOLET if selected else SUB, 80)
            text(209, y + 12, "·", 10, SUB)
            text(219, y + 12, separator.join(map(t, tags)), 10, GOLD if selected else SUB, 87)

        if not rows:
            box((8, 49, 311, 103), PANEL, 7, BORDER)
            if app.tab:
                empty = t("在发现页选中电台，按 6 收藏", "Select a station in Browse and press 6.")
            elif waiting:
                dots = "." * (1 + app.search_elapsed % 3)
                empty = t("正在查询 Radio Browser", "Searching Radio Browser") + dots
                if app.search_elapsed >= SEARCH_CANCEL_HINT:
                    seconds = app.search_elapsed
                    text(16, 83, t(f"已等待 {seconds} 秒 · Esc 取消",
                                  f"Waiting {seconds}s · Esc to cancel"), 11, GOLD)
            elif app.search_canceled:
                empty = t("搜索已取消 · R 重试 · 4 重新搜索",
                          "Search canceled · R Retry · 4 New search")
            elif app.directory_status and app.directory_status != "离线缓存":
                empty = t("连接失败，按 R 重试或切到收藏", "Offline. Press R to retry or 7 for saved stations.")
            else:
                empty = t("没有匹配的电台，试试调整搜索或筛选", "No matches. Change the search or filters.")
            text(16, 65 if delayed else 71, empty, 12, TEXT, 285)

        live = app.play_status.startswith("播放中")
        failed = app.play_status.startswith(("连接失败", "当前环境"))
        accent = (ROSE if failed else TEAL if live else GOLD) if app.station else BLUE
        box((6, 112, 313, 143), "#233447", 6, SELECT_BORDER if live else BORDER)
        box((11, 116, 34, 139), "#121d2d", 5)
        if live:
            for i, height in enumerate((7, 13, 10, 5)):
                x = 15 + i * 4
                draw.rounded_rectangle((x, 134 - height, x + 2, 134), 1, fill=accent)
        else:
            draw.polygon(((19, 122), (19, 134), (28, 128)), fill=accent)
        state = t(app.play_status)
        last = app.library.last_station
        if app.station:
            title = app.station.name
            if title == "未命名电台":
                title = t(title)
            context = app.now_playing or t("直播音频", "Live audio")
        elif last:
            title = last.name
            context = t("Q 继续收听", "Q Resume")
        else:
            title = t("网络电台", "Internet radio")
            context = t("F/X 选台 · Enter 播放", "F/X Select · Enter Play")
        text(41, 116, title, 12, TEXT, 153)
        state_width = min(110, round(draw.textlength(state, font=self.fonts[10])) + 12)
        box((308 - state_width, 115, 308, 128), "#172437", 4)
        text(303, 117, state, 10, accent, state_width - 10, "rt")
        notice = app.toast or (app.directory_status if not app.tab else "")
        if app.now_playing and not app.toast:
            notice = ""
        text(41, 130, t(notice) if notice else context, 11, GOLD if notice else TEAL if live else SUB, 215)
        selected = app.selected()
        text(305, 131, t("I 详情", "I Info") if selected else t("H 帮助", "H Help"),
             10, SUB, anchor="rt")
        saved = selected and selected.uuid in app.library.favorites
        last_action = (t("返回", "Back") if app.search_origin is not None and not app.tab
                       else t("音量", "Volume"))
        softkeys((t("搜索", "Search"), t("筛选", "Filter"),
                  t("取消收藏", "Unsave") if saved else t("收藏", "Save"),
                  t("回发现", "Browse") if app.tab else t("收藏夹", "Saved"), last_action))
        return image
