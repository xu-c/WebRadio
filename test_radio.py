import io
import json
import tarfile
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from build_package import tar_gz
from radio_directory import PAGE_SIZE, Library, Query, RadioBrowser, Station, Superseded
from radio_player import PlaybackError, Player
from radio_ui import RadioApp, station_attributes

STATION = Station("01234567-89ab-cdef-0123-456789abcdef", "Test Radio",
                  "https://example.com/resolved", "https://example.com/original")
STATION_RESPONSE = [{
    "stationuuid": STATION.uuid, "name": STATION.name, "url_resolved": STATION.url,
    "url": STATION.original,
}]


class PackageTest(unittest.TestCase):
    def test_debian_payload_creates_parents_before_files(self):
        payload = tar_gz({
            "usr/share/radio/radio.py": (b"app", 0o644),
            "usr/share/radio/player.py": (b"player", 0o644),
            "usr/bin/radio": (b"launcher", 0o755),
        })
        directories = {"."}
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            for member in archive:
                self.assertIn(member.name.rsplit("/", 1)[0], directories)
                if member.isdir():
                    self.assertNotIn(member.name, directories)
                    directories.add(member.name)


class DirectoryTest(unittest.TestCase):
    def test_combined_query_and_pagination(self):
        params = parse_qs(Query("爵士", "CN", "mandarin", "jazz").parameters(2))
        self.assertEqual(params["name"], ["爵士"])
        self.assertEqual(params["countrycode"], ["CN"])
        self.assertEqual(params["language"], ["mandarin"])
        self.assertEqual(params["tag"], ["jazz"])
        self.assertEqual(params["offset"], ["48"])
        self.assertEqual(params["hidebroken"], ["true"])

    def test_failover_then_cached_page(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(RadioBrowser, "discover"):
            directory = RadioBrowser(Path(temp))
            with patch("radio_directory.urlopen", side_effect=[
                    TimeoutError(), io.BytesIO(json.dumps(STATION_RESPONSE).encode())]) as request:
                stations, more, cached = directory.search(Query(), 0, lambda: False)
                self.assertEqual((stations, more, cached), ([STATION], False, False))
                mirror = urlsplit(request.call_args.args[0].full_url).hostname
                self.assertEqual(directory.preferred, mirror)
            with patch("radio_directory.urlopen", side_effect=OSError):
                previews = []
                cached_result = ([STATION], False, True)
                self.assertEqual(directory.search(Query(), 0, lambda: False, previews.append),
                                 cached_result)
                self.assertEqual(previews, [cached_result])
                with self.assertRaises(Superseded):
                    directory.search(Query(), 0, lambda: True)

    def test_library_survives_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            library = Library(path)
            self.assertEqual(library.filters, Query(country="CN"))
            self.assertTrue(library.toggle(STATION))
            library.save(library.favorites, 30, Query("search text", "", "english", "news", "name"))
            saved = Library(path)
            self.assertEqual(list(saved.favorites.values()), [STATION])
            self.assertEqual(saved.volume, 30)
            self.assertEqual(saved.filters, Query(language="english", tag="news", order="name"))
            self.assertFalse(saved.toggle(STATION))
            self.assertFalse(Library(path).favorites)
            self.assertEqual(Library(path).filters, saved.filters)

    def test_lookahead_and_station_metadata(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(RadioBrowser, "discover"):
            directory = RadioBrowser(Path(temp))
            rows = [dict(STATION_RESPONSE[0], stationuuid=f"{index:08x}-0000-0000-0000-000000000000",
                         countrycode="FR", language="french", tags="jazz,news")
                    for index in range(PAGE_SIZE + 1)]
            with patch.object(directory, "request", side_effect=[rows, rows[:-1]]) as request:
                stations, more, _ = directory.search(Query(), 0, lambda: False)
                self.assertTrue(more)
                self.assertEqual(len(stations), PAGE_SIZE)
                self.assertEqual(station_attributes(stations[0]), (("法国",), ("法语",), ("爵士", "新闻")))
                self.assertFalse(directory.search(Query(), 1, lambda: False)[1])
                params = parse_qs(urlsplit(request.call_args.args[0]).query)
                self.assertEqual(params["limit"], [str(PAGE_SIZE + 1)])
                self.assertEqual(params["offset"], [str(PAGE_SIZE)])


class PlayerTest(unittest.TestCase):
    def test_uses_backup_and_counts_play_once(self):
        class Event:
            def is_set(self):
                return False

            def wait(self, seconds):
                delays.append(seconds)
                return False

        delays, statuses, clicks, urls = [], [], [], []
        player = Player(lambda: 45, lambda _, status: statuses.append(status), clicks.append)
        player.binary = "mpv"

        def stream(_, url, _canceled, _volume, status):
            urls.append(url)
            if len(urls) == 1:
                raise PlaybackError()
            status("播放中")
            status("缓冲中")
            status("播放中")

        try:
            with patch("radio_player.play_stream", side_effect=stream):
                player.listen(1, STATION, Event())
            self.assertEqual(urls, [STATION.url, STATION.original])
            self.assertEqual(delays, [1])
            self.assertEqual(clicks, [STATION])
            self.assertEqual(statuses[-1], "播放中")
        finally:
            player.close()


class InteractionTest(unittest.TestCase):
    def wait_for_results(self, app):
        deadline = time.monotonic() + 2
        while app.loading and time.monotonic() < deadline:
            app.tick()
            time.sleep(0.01)
        self.assertFalse(app.loading)

    def test_interface_language_switch_persists_without_changing_filters(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            library = Library(path)
            filters = Query(country="FR", language="french")
            library.save({STATION.uuid: STATION}, 35, filters)
            app = RadioApp(None, library, demo=True)
            try:
                self.assertEqual(library.interface_language, "zh")
                app.handle("h")
                app.handle("6")
                app.handle("5")
                stored = Library(path)
                self.assertEqual(stored.interface_language, "en")
                self.assertEqual(stored.filters, filters)
                self.assertEqual(stored.volume, 35)
                self.assertEqual(stored.favorites, {STATION.uuid: STATION})
                app.handle("8")
                app.handle("7")
                self.assertEqual(app.modal, "about")
                app.handle("8")
                app.handle("6")
                app.handle("4")
                self.assertEqual(Library(path).interface_language, "zh")
            finally:
                app.close()

    def test_search_filter_and_favorites_with_playback_active(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            app = RadioApp(None, Library(path), demo=True)
            try:
                self.wait_for_results(app)
                self.assertTrue(all(station.country == "CN" for station in app.rows()))
                app.handle("5")
                app.handle("4")
                app.handle("7")
                self.wait_for_results(app)
                app.handle("4")
                for char in "Jazz":
                    app.handle(char, char)
                app.handle("enter")
                self.wait_for_results(app)
                self.assertEqual(len(app.rows()), 2)
                app.handle("5")
                app.handle("6")
                for char in "us":
                    app.handle(char, char)
                app.handle("enter")
                app.handle("7")
                self.wait_for_results(app)
                self.assertEqual([s.name for s in app.rows()], ["Jazz24"])
                app.handle("enter")
                app.handle("6")
                app.handle("7")
                self.assertEqual(app.selected(), app.station)
                app.handle("6")
                self.assertIsNone(app.selected())
                self.assertEqual(app.station.name, "Jazz24")
                app.handle("space")
                self.assertIsNone(app.station)
            finally:
                app.close()

    def test_zero_keys_page_boundaries_details_and_volume(self):
        class Directory:
            def search(self, query, page, canceled, preview):
                rows = [replace(STATION, uuid=f"{page}-{i}", name=f"Station {page}-{i}")
                        for i in range(2)]
                return rows, page == 0, False

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            app = RadioApp(Directory(), Library(path))
            try:
                self.wait_for_results(app)
                app.handle("x")
                self.assertEqual(app.selected().name, "Station 0-1")
                app.handle("x")
                self.wait_for_results(app)
                self.assertEqual((app.page, app.selected().name), (1, "Station 1-0"))
                app.handle("f")
                self.wait_for_results(app)
                self.assertEqual((app.page, app.selected().name), (0, "Station 0-1"))
                app.handle("c")
                self.wait_for_results(app)
                app.handle("z")
                self.wait_for_results(app)
                self.assertEqual((app.page, app.selected().name), (0, "Station 0-0"))
                app.handle("i")
                self.assertEqual(app.detail_station, app.selected())
                app.handle("8")
                app.handle("8")
                app.handle("c")
                app.handle("5")
                app.handle("z")
                app.handle("8")
                self.assertEqual(app.volume, 50)
                self.assertEqual(app.modal, "")
                app.handle("5")
                app.handle("x")
                self.assertEqual(app.filter_row, 1)
                app.handle("c")
                app.handle("7")
                self.assertEqual(Library(path).filters.language, "chinese")
            finally:
                app.close()
            self.assertEqual(Library(path).volume, 50)
