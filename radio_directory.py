"""Radio Browser queries and local radio data."""

import hashlib
import json
import os
import re
import socket
import tempfile
import threading
from dataclasses import asdict, dataclass
from http.client import HTTPException
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

PAGE_SIZE = 24
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
        return cls(uuid, label("name") or "未命名电台", resolved, original,
                   label("country" if saved else "countrycode", 8),
                   label("codec", 12), label("bitrate", 8), label("tags", 100))


@dataclass(frozen=True)
class Query:
    name: str = ""
    country: str = ""
    language: str = ""
    tag: str = ""
    order: str = "clickcount"

    def parameters(self, page):
        values = dict(hidebroken="true", order=self.order,
                      reverse="false" if self.order == "name" else "true",
                      limit=PAGE_SIZE, offset=page * PAGE_SIZE)
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


class Library:
    def __init__(self, data_dir):
        self.path = data_dir / "library.json"
        stored = read_json(self.path, {})
        stored = stored if isinstance(stored, dict) else {}
        volume = stored.get("volume", 45)
        self.volume = max(0, min(100, volume)) if type(volume) is int else 45
        rows = stored.get("favorites", [])
        self.favorites = {
            station.uuid: station
            for row in (rows if isinstance(rows, list) else [])
            if (station := Station.parse(row, saved=True))
        }

    def save(self, favorites, volume):
        write_json(self.path, {
            "favorites": [asdict(station) for station in favorites.values()],
            "volume": volume,
        })
        self.favorites = favorites
        self.volume = volume

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

    def request(self, path, response_type, canceled=lambda: False, click=False):
        hosts = tuple(dict.fromkeys(host for host in
                                    (self.preferred, *self.mirrors) if host))
        for host in hosts[:2] if click else hosts:
            if canceled():
                raise Superseded()
            request = Request("https://" + host + path,
                              headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            try:
                with urlopen(request, timeout=3.5) as response:
                    raw = response.read(512 * 1024 + 1)
                if len(raw) > 512 * 1024:
                    continue
                data = json.loads(raw)
                if not isinstance(data, response_type):
                    continue
            except (OSError, ValueError, HTTPException):
                continue
            if not click:
                self.preferred = host
            return data
        raise DirectoryError("Radio Browser 暂时无法连接")

    def search(self, query, page, canceled, preview=None):
        params = query.parameters(page)
        cache_key = hashlib.sha256(params.encode()).hexdigest()[:24]
        path = self.cache_dir / (cache_key + ".json")

        def parse(rows, cached):
            stations = {station.uuid: station for row in rows
                        if (station := Station.parse(row))}
            return list(stations.values()), len(rows) >= PAGE_SIZE, cached

        cached_rows = read_json(path, None)
        cached = parse(cached_rows, True) if isinstance(cached_rows, list) else None
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
            old_files = sorted(self.cache_dir.glob("*.json"),
                               key=lambda file: file.stat().st_mtime, reverse=True)
            for old in old_files[64:]:
                old.unlink(missing_ok=True)
        except OSError:
            pass
        return parse(rows, False)

    def count_play(self, station):
        try:
            self.request("/json/url/" + quote(station.uuid, safe=""), dict, click=True)
        except DirectoryError:
            pass
