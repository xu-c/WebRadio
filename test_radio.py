import io
import json
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from build_package import tar_gz
from radio_directory import Library, Query, RadioBrowser, Station, Superseded
from radio_player import PlaybackError, Player
from radio_ui import RadioApp

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
            self.assertTrue(library.toggle(STATION))
            library.save(library.favorites, 30)
            saved = Library(path)
            self.assertEqual(list(saved.favorites.values()), [STATION])
            self.assertEqual(saved.volume, 30)
            self.assertFalse(saved.toggle(STATION))
            self.assertFalse(Library(path).favorites)


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

    def test_search_filter_and_favorites_with_playback_active(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            app = RadioApp(None, Library(path), demo=True)
            try:
                self.wait_for_results(app)
                app.handle("/")
                for char in "Jazz":
                    app.handle(char, char)
                app.handle("enter")
                self.wait_for_results(app)
                self.assertEqual(len(app.rows()), 2)
                app.handle("f")
                app.handle("e")
                for char in "us":
                    app.handle(char, char)
                app.handle("enter")
                app.handle("enter")
                self.wait_for_results(app)
                self.assertEqual([s.name for s in app.rows()], ["Jazz24"])
                app.handle("enter")
                app.handle("s")
                app.handle("2")
                self.assertEqual(app.selected(), app.station)
                app.handle("s")
                self.assertIsNone(app.selected())
                self.assertEqual(app.station.name, "Jazz24")
                app.handle("space")
                self.assertIsNone(app.station)
            finally:
                app.close()
