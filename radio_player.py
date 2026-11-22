"""Manage a single mpv process and reconnect stalled live audio."""

import json
import math
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import wave
from array import array
from pathlib import Path


class OperationSound:
    """Play short original tones through the shared PulseAudio output."""

    def __init__(self):
        self.binary = shutil.which("paplay") if os.name == "posix" else None
        self.directory = tempfile.TemporaryDirectory(prefix="webradio-sounds-")
        self.process = None
        self.last_played = 0
        for name, frequency, duration in (("move", 880, 0.018), ("confirm", 1175, 0.035),
                                          ("back", 660, 0.025)):
            count = int(22050 * duration)
            samples = array("h", (
                int(1800 * math.sin(2 * math.pi * frequency * i / 22050)
                    * math.sin(math.pi * i / count) ** 2)
                for i in range(count)
            ))
            if sys.byteorder != "little":
                samples.byteswap()
            with wave.open(str(Path(self.directory.name) / f"{name}.wav"), "wb") as output:
                output.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
                output.writeframes(samples.tobytes())

    def play(self, name):
        now = time.monotonic()
        if (not self.binary or now - self.last_played < 0.07
                or self.process is not None and self.process.poll() is None):
            return
        self.last_played = now
        try:
            self.process = subprocess.Popen(
                [self.binary, str(Path(self.directory.name) / f"{name}.wav")],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError:
            self.binary = None

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=0.3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()

    def close(self):
        self.stop()
        self.directory.cleanup()


class PlaybackError(Exception):
    pass


def track_title(metadata):
    if not isinstance(metadata, dict):
        return ""
    tags = {key.casefold(): " ".join(value.split())[:160] for key, value in metadata.items()
            if isinstance(value, str)}
    title = tags.get("icy-title") or tags.get("streamtitle") or tags.get("title", "")
    artist = tags.get("artist", "")
    if artist and title and artist.casefold() not in title.casefold():
        return f"{artist} — {title}"
    return title


def play_stream(binary, url, canceled, volume, status, metadata):
    with tempfile.TemporaryDirectory(prefix="zero-radio-") as directory:
        endpoint = os.path.join(directory, "mpv.sock")
        process = subprocess.Popen([
            binary, "--no-config", "--no-terminal", "--load-scripts=no", "--ytdl=no",
            "--video=no", "--audio-display=no", "--network-timeout=8", "--tls-verify=yes",
            "--cache=yes", "--cache-secs=6", "--cache-pause-initial=yes", "--cache-pause-wait=1",
            f"--volume={volume()}", f"--input-ipc-server={endpoint}", "--", url,
        ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(0.2)
                started = time.monotonic()
                while not canceled.is_set():
                    try:
                        connection.connect(endpoint)
                        break
                    except OSError:
                        if process.poll() is not None or time.monotonic() - started > 4:
                            raise PlaybackError("播放器启动失败")
                        canceled.wait(0.08)
                if canceled.is_set():
                    return

                def command(*args):
                    connection.sendall(json.dumps({"command": args}).encode() + b"\n")

                command("observe_property", 1, "time-pos")
                command("observe_property", 2, "paused-for-cache")
                command("observe_property", 3, "metadata")
                last_progress = started
                last_position = None
                last_volume = None
                buffered = b""
                while not canceled.is_set():
                    now = time.monotonic()
                    if now - last_progress > (18 if last_position is not None else 22):
                        raise PlaybackError("音频流超时")
                    if process.poll() is not None:
                        raise PlaybackError("音频流已断开")
                    current_volume = volume()
                    if last_volume != current_volume:
                        command("set_property", "volume", current_volume)
                        last_volume = current_volume
                    try:
                        chunk = connection.recv(8192)
                    except socket.timeout:
                        continue
                    if not chunk:
                        raise PlaybackError("音频流已断开")
                    buffered += chunk
                    while b"\n" in buffered:
                        line, buffered = buffered.split(b"\n", 1)
                        event = json.loads(line)
                        if event.get("event") == "end-file":
                            raise PlaybackError("音频流已断开")
                        if event.get("event") != "property-change":
                            continue
                        data = event.get("data")
                        if event.get("id") == 1 and isinstance(data, (int, float)):
                            if last_position is not None and data != last_position:
                                last_progress = time.monotonic()
                                status("播放中")
                            last_position = data
                        elif event.get("id") == 2 and data is True:
                            status("缓冲中")
                        elif event.get("id") == 3:
                            metadata(track_title(data))
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=0.4)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


class Player:
    def __init__(self, volume, publish, count_play, demo=False, publish_title=None):
        self.volume = volume
        self.publish = publish
        self.count_play = count_play
        self.publish_title = publish_title
        self.demo = demo
        self.binary = shutil.which("mpv") if os.name != "nt" else None
        self.changed = threading.Condition()
        self.request = None
        self.active = None
        self.closed = False
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def select(self, generation, station):
        with self.changed:
            if self.active:
                self.active.set()
            self.request = (generation, station) if station else None
            self.changed.notify()

    def close(self):
        with self.changed:
            self.closed = True
            if self.active:
                self.active.set()
            self.changed.notify()
        self.thread.join(timeout=2)

    def run(self):
        while True:
            with self.changed:
                self.changed.wait_for(lambda: self.closed or self.request is not None)
                if self.closed:
                    return
                generation, station = self.request
                self.request = None
                canceled = self.active = threading.Event()
            self.listen(generation, station, canceled)

    def listen(self, generation, station, canceled):
        last_status = None
        counted = False
        playing_since = None
        last_title = ""

        def metadata(title):
            nonlocal last_title
            if title == station.name:
                title = ""
            if not canceled.is_set() and title != last_title:
                last_title = title
                if self.publish_title:
                    self.publish_title(generation, title)

        def status(message):
            nonlocal last_status, counted, playing_since
            if canceled.is_set() or message == last_status:
                return
            last_status = message
            self.publish(generation, message)
            if message == "播放中":
                playing_since = playing_since or time.monotonic()
                if not counted and not self.demo:
                    counted = True
                    self.count_play(station)

        if self.demo:
            if not canceled.wait(0.35):
                status("播放中 · 演示")
            return
        if not self.binary:
            status("当前环境需要 Linux 和 mpv")
            return

        urls = list(dict.fromkeys(url for url in (station.url, station.original) if url))
        delays = (1, 2, 4, 8)
        failures = 0
        url_index = 0
        while not canceled.is_set():
            playing_since = None
            try:
                play_stream(self.binary, urls[url_index], canceled, self.volume, status, metadata)
                return
            except (PlaybackError, OSError, ValueError):
                if canceled.is_set():
                    return
                metadata("")
                if playing_since is not None and time.monotonic() - playing_since >= 30:
                    failures = 0
                if failures == len(delays):
                    status("连接失败 · Enter 重试")
                    return
                delay = delays[failures]
                failures += 1
                url_index = (url_index + 1) % len(urls)
                status(f"重连 {failures}/{len(delays)} · {delay} 秒")
                if canceled.wait(delay):
                    return
                status("正在连接")
