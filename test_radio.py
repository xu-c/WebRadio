import io
import json
import tarfile
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from build_package import tar_gz
from radio_device import battery_percent, system_volume
from radio_directory import PAGE_SIZE, Library, Query, RadioBrowser, Station, Superseded, pinyin_matches
from radio_player import PlaybackError, Player, track_title
from radio_ui import RadioApp, station_attributes

STATION = Station("01234567-89ab-cdef-0123-456789abcdef", "Test Radio",
                  "https://example.com/resolved", "https://example.com/original")
STATION_RESPONSE = [{
    "stationuuid": STATION.uuid, "name": STATION.name, "url_resolved": STATION.url,
    "url": STATION.original,
}]


class DeviceStatusTest(unittest.TestCase):
    def test_adjusts_default_output_and_reads_back_actual_volume(self):
        with patch("radio_device.subprocess.run", side_effect=[
            CompletedProcess([], 0, "speaker\n"),
            CompletedProcess([], 0, "Volume: front-left: 64225 / 98% / -0.5 dB,"
                                    " front-right: 64225 / 98% / -0.5 dB\n"),
            CompletedProcess([], 0, ""),
            CompletedProcess([], 0, "Volume: front-left: 65536 / 100% / 0.0 dB\n"),
        ]) as run:
            self.assertEqual(system_volume(1), 100)
            self.assertEqual([call.args[0] for call in run.call_args_list], [
                ["pactl", "get-default-sink"],
                ["pactl", "get-sink-volume", "speaker"],
                ["pactl", "set-sink-volume", "speaker", "100%"],
                ["pactl", "get-sink-volume", "speaker"],
            ])

    def test_battery_prefers_fuel_gauge_and_handles_missing_data(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertIsNone(battery_percent(root))
            for name, level in (("battery", "100"), ("bq27220-0", "0")):
                supply = root / name
                supply.mkdir()
                (supply / "type").write_text("Battery\n")
                (supply / "capacity").write_text(level)
            self.assertEqual(battery_percent(root), 0)
            (root / "bq27220-0" / "present").write_text("0\n")
            self.assertEqual(battery_percent(root), 100)


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

        delays, statuses, clicks, urls, titles = [], [], [], [], []
        player = Player(lambda: 45, lambda _, status: statuses.append(status), clicks.append,
                        publish_title=lambda _, title: titles.append(title))
        player.binary = "mpv"

        def stream(_, url, _canceled, _volume, status, metadata):
            urls.append(url)
            if len(urls) == 1:
                raise PlaybackError()
            status("播放中")
            metadata(track_title({"icy-title": "Artist - Live Song", "artist": "Artist"}))
            metadata(track_title({"icy-title": "Artist - Live Song"}))
            status("缓冲中")
            status("播放中")

        try:
            with patch("radio_player.play_stream", side_effect=stream):
                player.listen(1, STATION, Event())
            self.assertEqual(urls, [STATION.url, STATION.original])
            self.assertEqual(delays, [1])
            self.assertEqual(clicks, [STATION])
            self.assertEqual(titles, ["Artist - Live Song"])
            self.assertEqual(statuses[-1], "播放中")
        finally:
            player.close()


class InteractionTest(unittest.TestCase):
    def test_sound_toggle_survives_restart_and_other_preference_saves(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            from unittest.mock import Mock

            sound = Mock()
            app = RadioApp(None, Library(path), demo=True, operation_sound=sound)
            try:
                self.assertTrue(app.library.sound_enabled)
                app.handle("h")
                sound.play.assert_called_once_with("confirm")
                app.handle("4")
                sound.stop.assert_called_once()
                self.assertFalse(Library(path).sound_enabled)
                sound.play.reset_mock()
                app.handle("8")
                app.handle("h")
                sound.play.assert_not_called()
                app.library.save(app.library.favorites, 35)
                self.assertFalse(Library(path).sound_enabled)
                app.handle("4")
                self.assertTrue(Library(path).sound_enabled)
                app.handle("8")
                sound.play.assert_called_once_with("back")
            finally:
                app.close()

    def wait_for_results(self, app):
        deadline = time.monotonic() + 2
        while app.loading and time.monotonic() < deadline:
            app.tick()
            time.sleep(0.01)
        self.assertFalse(app.loading)

    def test_cancel_during_slow_search_and_start_another(self):
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        replacement = replace(STATION, name="New result")

        class Directory:
            def search(self, query, page, canceled, preview):
                if query.name == "slow":
                    started.set()
                    release.wait(3)
                    finished.set()
                    return [STATION], False, False
                return [replacement], False, False

        with tempfile.TemporaryDirectory() as temp:
            app = RadioApp(Directory(), Library(Path(temp)))
            try:
                app.show_query(Query(name="slow"))
                self.assertTrue(started.wait(2))
                app.search_started = time.monotonic() - 9.5
                app.tick()
                self.assertEqual(app.search_elapsed, 9)
                app.search_started -= 1
                app.tick()
                self.assertEqual(app.search_elapsed, 10)
                app.escape_key(True)
                app.escape_key(False)
                self.assertTrue(app.running)
                self.assertTrue(app.search_canceled)
                self.assertFalse(app.loading)
                app.show_query(Query(name="new"))
                self.wait_for_results(app)
                self.assertFalse(release.is_set())
                self.assertFalse(app.search_canceled)
                self.assertEqual(app.results, [replacement])
                release.set()
                self.assertTrue(finished.wait(2))
                app.tick()
                self.assertEqual(app.results, [replacement])
            finally:
                release.set()
                app.close()

    def test_hold_escape_and_resume_only_successful_station(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            app = RadioApp(None, Library(path), demo=True)
            try:
                with patch("radio_ui.time.monotonic", return_value=10) as clock:
                    app.handle("h")
                    app.escape_key(True)
                    clock.return_value = 11
                    app.escape_key(False)
                    self.assertTrue(app.running)
                    self.assertEqual(app.modal, "")
                    app.escape_key(True)
                    app.escape_key(False)
                    self.assertTrue(app.running)
                    app.tune(STATION)
                    app.play_event(app.play_id, "正在连接")
                    app.tick()
                    self.assertIsNone(Library(path).last_station)
                    app.play_event(app.play_id, "播放中")
                    app.tick()
                    self.assertEqual(Library(path).last_station, STATION)
                    app.tune(None)
                    app.handle("q")
                    self.assertEqual(app.station, STATION)
                    app.escape_key(True)
                    clock.return_value = 12.99
                    app.tick()
                    self.assertTrue(app.running)
                    clock.return_value = 14
                    app.tick()
                    self.assertFalse(app.running)
            finally:
                app.close()

    def test_search_scope_and_pinyin(self):
        self.assertFalse(pinyin_matches("CNR-1 中国之声", "zgzs"))
        self.assertTrue(pinyin_matches("CNR-1 中国之声", "zhongguozhisheng"))
        self.assertTrue(pinyin_matches("中国之声", "zhong guo zhi sheng"))
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            app = RadioApp(None, Library(path), demo=True)
            try:
                app.handle("4")
                for char in "nhk":
                    app.handle(char, char)
                app.handle("enter")
                self.wait_for_results(app)
                self.assertEqual(app.query.country, "CN")
                self.assertFalse(app.results)
                app.handle("4")
                app.handle("tab")
                app.handle("enter")
                self.wait_for_results(app)
                self.assertEqual([s.name for s in app.results], ["NHK Radio 1"])
                self.assertEqual(app.library.filters.country, "CN")
                app.handle("4")
                app.handle("ctrl+p")
                app.handle("", clear=True)
                for char in "shanghaidonggan":
                    app.handle(char, char)
                app.handle("enter")
                self.wait_for_results(app)
                self.assertEqual([s.name for s in app.results], ["上海动感 101"])
            finally:
                app.close()

    def test_search_back_restores_list_and_selection(self):
        with tempfile.TemporaryDirectory() as temp:
            app = RadioApp(None, Library(Path(temp)), demo=True)
            try:
                self.wait_for_results(app)
                app.handle("down")
                original = app.selected()
                for reopen_input, back in ((False, "escape"), (True, "escape"),
                                            (False, "8"), (True, "8")):
                    app.handle("4")
                    app.handle("4")
                    app.handle("6")
                    for char in "jazz":
                        app.handle(char, char)
                    app.handle("f4")
                    app.handle("2", "2")
                    self.assertEqual(app.input, "jazz42")
                    app.handle("6")
                    for char in "nhk":
                        app.handle(char, char)
                    app.handle("7")
                    self.wait_for_results(app)
                    self.assertEqual(app.selected().name, "NHK Radio 1")
                    if reopen_input:
                        app.handle("4")
                    app.handle(back)
                    self.assertEqual(app.modal, "")
                    self.assertEqual(app.query, app.filters)
                    self.assertEqual(app.selected(), original)
                    self.assertIsNone(app.search_origin)
            finally:
                app.close()

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
            system_steps = []

            def adjust_system(step):
                system_steps.append(step)
                return 70 + 5 * sum(system_steps)

            app = RadioApp(Directory(), Library(path), system_volume=adjust_system)
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
                app.handle("6")
                app.handle("7")
                app.handle("7")
                self.assertEqual(app.system_volume_request.result(timeout=1), 75)
                app.tick()
                self.assertEqual(app.system_volume, 75)
                self.assertEqual(system_steps, [0, -1, 1, 1])
                app.handle("x")
                self.assertEqual(app.volume_row, 1)
                app.handle("right")
                app.handle("z")
                app.handle("4")
                self.assertEqual((app.volume, app.volume_row), (45, 1))
                app.handle("5")
                app.handle("f")
                self.assertEqual(app.volume_row, 0)
                app.handle("c")
                self.assertEqual(app.volume, 55)
                app.handle("left")
                app.handle("down")
                self.assertEqual(app.volume_row, 1)
                app.handle("up")
                self.assertEqual(app.volume_row, 0)
                self.assertEqual(app.system_volume_request.result(timeout=1), 75)
                self.assertEqual(system_steps, [0, -1, 1, 1, 1, -1])
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
