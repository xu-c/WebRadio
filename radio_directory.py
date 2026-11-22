"""Radio Browser queries and local radio data."""

import hashlib
import json
import os
import re
import socket
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, replace
from functools import lru_cache
from http.client import HTTPException
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

PAGE_SIZE = 24
PINYIN_REGIONS = ("CN", "TW", "HK", "MO")
PINYIN_LANGUAGES = ("chinese", "mandarin", "cantonese")
PINYIN_LIMIT = 1000
ORDERS = ("clickcount", "clicktrend", "votes", "name")
MIRROR_POOL = "all.api.radio-browser.info"
DEFAULT_MIRRORS = ("de1.api.radio-browser.info", "nl1.api.radio-browser.info", MIRROR_POOL)
USER_AGENT = "CardputerZero-Radio/1.0"


def stream_url(value):
    if not isinstance(value, str) or not value or len(value) > 4096:
        return ""
    if any(char.isspace() or ord(char) < 32 for char in value):
        return ""
    try:
        parts = urlsplit(value)
        if parts.scheme in ("http", "https") and parts.hostname and not parts.username:
            return value
    except ValueError:
        pass
    return ""


@dataclass(frozen=True)
class Station:
    uuid: str
    name: str
    url: str
    original: str = ""
    country: str = ""
    codec: str = ""
    bitrate: str = ""
    tags: str = ""
    country_name: str = ""
    language: str = ""
    language_codes: str = ""

    @classmethod
    def parse(cls, row, saved=False):
        if not isinstance(row, dict):
            return None

        def label(key, limit=120):
            value = row.get(key)
            if isinstance(value, (str, int)):
                return " ".join(str(value).split())[:limit]
            return ""

        uuid = label("uuid" if saved else "stationuuid", 36).lower()
        if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", uuid):
            return None
        original = stream_url(row.get("original" if saved else "url"))
        resolved = stream_url(row.get("url" if saved else "url_resolved")) or original
        if not resolved:
            return None
        return cls(
            uuid, label("name") or "未命名电台", resolved, original,
            label("country" if saved else "countrycode", 8).upper(),
            label("codec", 12), label("bitrate", 8), label("tags", 512),
            label("country_name" if saved else "country"),
            label("language", 256), label("language_codes" if saved else "languagecodes", 120),
        )


@dataclass(frozen=True)
class Query:
    name: str = ""
    country: str = ""
    language: str = ""
    tag: str = ""
    order: str = "clickcount"
    pinyin: bool = False

    @classmethod
    def from_filters(cls, value):
        if not isinstance(value, dict):
            return cls(country="CN")
        country = value.get("country")
        if not isinstance(country, str) or (country and not re.fullmatch(r"[A-Z]{2}", country)):
            country = "CN"
        order = value.get("order")
        language, tag = value.get("language"), value.get("tag")
        return cls(country=country,
                   language=language[:80] if isinstance(language, str) else "",
                   tag=tag[:80] if isinstance(tag, str) else "",
                   order=order if order in ORDERS else "clickcount")

    def parameters(self, page, limit=PAGE_SIZE):
        values = dict(hidebroken="true", order=self.order,
                      reverse="false" if self.order == "name" else "true",
                      limit=limit, offset=page * PAGE_SIZE)
        for key, value in (("name", self.name), ("countrycode", self.country),
                           ("language", self.language), ("tag", self.tag)):
            if value:
                values[key] = value
        if self.tag:
            values["tagExact"] = "true"
        return urlencode(values)


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(dir=path.parent, prefix=".radio-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        Path(temp_path).unlink(missing_ok=True)


def pinyin_matches(name, keyword):
    needle = re.sub(r"[^a-z0-9]", "", keyword.lower().replace("ü", "v"))
    if not needle or not re.search(r"[\u3400-\u9fff]", name):
        return False
    return needle in pinyin_name(name)


@lru_cache(maxsize=4096)
def pinyin_name(name):
    from pypinyin import lazy_pinyin

    parts = [re.sub(r"[^a-z0-9]", "", part.casefold().replace("ü", "v"))
             for part in lazy_pinyin(name)]
    return "".join(parts)


class Library:
    def __init__(self, data_dir):
        self.path = data_dir / "library.json"
        stored = read_json(self.path, {})
        stored = stored if isinstance(stored, dict) else {}
        volume = stored.get("volume", 45)
        self.volume = max(0, min(100, volume)) if type(volume) is int else 45
        self.filters = Query.from_filters(stored.get("filters"))
        self.interface_language = "en" if stored.get("interface_language") == "en" else "zh"
        self.sound_enabled = stored.get("sound_enabled", True) is not False
        self.last_station = Station.parse(stored.get("last_station"), saved=True)
        rows = stored.get("favorites", [])
        self.favorites = {
            station.uuid: station
            for row in (rows if isinstance(rows, list) else [])
            if (station := Station.parse(row, saved=True))
        }

    def save(self, favorites, volume, filters=None, interface_language=None, last_station=None,
             sound_enabled=None):
        if filters is None:
            filters = self.filters
        if interface_language is None:
            interface_language = self.interface_language
        if last_station is None:
            last_station = self.last_station
        if sound_enabled is None:
            sound_enabled = self.sound_enabled
        filter_data = {key: getattr(filters, key) for key in ("country", "language", "tag", "order")}
        write_json(self.path, {
            "favorites": [asdict(station) for station in favorites.values()],
            "volume": volume,
            "filters": filter_data,
            "interface_language": interface_language,
            "sound_enabled": sound_enabled,
            "last_station": asdict(last_station) if last_station else None,
        })
        self.favorites = favorites
        self.volume = volume
        self.filters = Query.from_filters(filter_data)
        self.interface_language = interface_language
        self.sound_enabled = sound_enabled
        self.last_station = last_station

    def toggle(self, station):
        favorites = self.favorites.copy()
        if station.uuid in favorites:
            del favorites[station.uuid]
        else:
            favorites[station.uuid] = station
        self.save(favorites, self.volume)
        return station.uuid in favorites


class DirectoryError(Exception):
    pass


class Superseded(Exception):
    pass


class RadioBrowser:
    def __init__(self, cache_dir):
        self.cache_dir = cache_dir
        self.mirrors = DEFAULT_MIRRORS
        self.preferred = None
        threading.Thread(target=self.discover, daemon=True).start()

    def discover(self):
        try:
            addresses = {result[4][0] for result in socket.getaddrinfo(
                MIRROR_POOL, 443, type=socket.SOCK_STREAM)}
        except OSError:
            return
        mirrors = list(DEFAULT_MIRRORS[:-1])
        for address in sorted(addresses):
            try:
                host = socket.gethostbyaddr(address)[0].lower().rstrip(".")
            except OSError:
                continue
            if re.fullmatch(r"[a-z0-9-]+\.api\.radio-browser\.info", host):
                mirrors.append(host)
                self.mirrors = tuple(dict.fromkeys((*mirrors, MIRROR_POOL)))

    def request(self, path, response_type, canceled=lambda: False, click=False, max_bytes=512 * 1024):
        hosts = tuple(dict.fromkeys(host for host in
                                    (self.preferred, *self.mirrors) if host))
        for host in hosts[:2] if click else hosts:
            if canceled():
                raise Superseded()
            request = Request("https://" + host + path,
                              headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            try:
                with urlopen(request, timeout=3.5) as response:
                    raw = response.read(max_bytes + 1)
                if len(raw) > max_bytes:
                    continue
                data = json.loads(raw)
                if not isinstance(data, response_type):
                    continue
            except (OSError, ValueError, HTTPException):
                continue
            if canceled():
                raise Superseded()
            if not click:
                self.preferred = host
            return data
        raise DirectoryError("Radio Browser 暂时无法连接")

    def search(self, query, page, canceled, preview=None):
        if query.pinyin and query.name:
            return self.search_pinyin(query, page, canceled)
        params = query.parameters(page, PAGE_SIZE + 1)

        def cache_path(parameters):
            key = hashlib.sha256(parameters.encode()).hexdigest()[:24]
            return self.cache_dir / (key + ".json")

        def parse(rows, cached, legacy=False):
            stations = {station.uuid: station for row in rows[:PAGE_SIZE]
                        if (station := Station.parse(row))}
            more = len(rows) >= PAGE_SIZE if legacy else len(rows) > PAGE_SIZE
            return list(stations.values()), more, cached

        path = cache_path(params)
        cached_rows = read_json(path, None)
        legacy = not isinstance(cached_rows, list)
        if legacy:
            cached_rows = read_json(cache_path(query.parameters(page)), None)
        cached = parse(cached_rows, True, legacy) if isinstance(cached_rows, list) else None
        if preview and cached and cached[0] and not canceled():
            preview(cached)
        try:
            rows = self.request("/json/stations/search?" + params, list, canceled)
        except DirectoryError:
            if canceled():
                raise Superseded() from None
            if cached is not None:
                return cached
            raise
        if canceled():
            raise Superseded()
        try:
            write_json(path, rows)
            old_files = sorted((file for file in self.cache_dir.glob("*.json")
                                if not file.name.startswith("pinyin-")),
                               key=lambda file: file.stat().st_mtime, reverse=True)
            for old in old_files[64:]:
                old.unlink(missing_ok=True)
        except OSError:
            pass
        return parse(rows, False)

    def search_pinyin(self, query, page, canceled):
        base = replace(query, name="", pinyin=False)
        if base.country or base.language:
            scopes = (base,)
        else:
            scopes = (*(replace(base, country=country) for country in PINYIN_REGIONS),
                      *(replace(base, language=language) for language in PINYIN_LANGUAGES))
        matches = {}
        cached = False
        loaded = False
        for scope in scopes:
            if canceled():
                raise Superseded()
            params = scope.parameters(0, PINYIN_LIMIT)
            cache = self.cache_dir / ("pinyin-" + hashlib.sha256(params.encode()).hexdigest()[:24] + ".json")
            rows, fetched_at = None, 0
            try:
                record = read_json(cache, {})
                rows = record.get("rows") if isinstance(record, dict) else None
                fetched_at = cache.stat().st_mtime
            except OSError:
                pass
            if not isinstance(rows, list) or time.time() - fetched_at > 86400:
                try:
                    rows = self.request("/json/stations/search?" + params, list, canceled,
                                        max_bytes=4 * 1024 * 1024)
                    try:
                        write_json(cache, {"rows": rows})
                        old_files = sorted(self.cache_dir.glob("pinyin-*.json"),
                                           key=lambda file: file.stat().st_mtime, reverse=True)
                        for old in old_files[24:]:
                            old.unlink(missing_ok=True)
                    except OSError:
                        pass
                except DirectoryError:
                    cached = True
                    if not isinstance(rows, list):
                        continue
            loaded = True
            for row in rows:
                if canceled():
                    raise Superseded()
                station = Station.parse(row)
                if station and station.uuid not in matches and pinyin_matches(station.name, query.name):
                    score = row.get(query.order, 0)
                    matches[station.uuid] = (station, score if isinstance(score, int) else 0)
        if not loaded:
            raise DirectoryError("Radio Browser 暂时无法连接")
        if query.order == "name":
            ranked = sorted(matches.values(), key=lambda item: item[0].name.casefold())
        else:
            ranked = sorted(matches.values(), key=lambda item: item[1], reverse=True)
        results = [station for station, _ in ranked]
        start = page * PAGE_SIZE
        return results[start:start + PAGE_SIZE], len(results) > start + PAGE_SIZE, cached

    def count_play(self, station):
        try:
            self.request("/json/url/" + quote(station.uuid, safe=""), dict, click=True)
        except DirectoryError:
            pass
