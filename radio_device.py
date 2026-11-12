"""Cardputer Zero framebuffer and keyboard adapters."""

import ctypes
import mmap
import os
import select
import struct
from array import array
from pathlib import Path

from PIL import Image

from radio_ui import SIZE


class FrameInfo(ctypes.Structure):
    _fields_ = [
        ("id", ctypes.c_char * 16), ("smem_start", ctypes.c_ulong),
        ("smem_len", ctypes.c_uint32), ("type", ctypes.c_uint32),
        ("type_aux", ctypes.c_uint32), ("visual", ctypes.c_uint32),
        ("xpanstep", ctypes.c_uint16), ("ypanstep", ctypes.c_uint16),
        ("ywrapstep", ctypes.c_uint16), ("line_length", ctypes.c_uint32),
        ("mmio_start", ctypes.c_ulong), ("mmio_len", ctypes.c_uint32),
        ("accel", ctypes.c_uint32), ("capabilities", ctypes.c_uint16),
        ("reserved", ctypes.c_uint16 * 2),
    ]


class Framebuffer:
    def __init__(self, path, rotate=False):
        import fcntl

        with open(path, "r+b", buffering=0) as handle:
            variable = struct.unpack("40I", fcntl.ioctl(handle, 0x4600, bytes(160)))
            fixed = FrameInfo.from_buffer_copy(
                fcntl.ioctl(handle, 0x4602, bytes(ctypes.sizeof(FrameInfo))))
            self.width, self.height, _, _, x_offset, y_offset, bits = variable[:7]
            if (self.width, self.height) not in (SIZE, SIZE[::-1]):
                raise ValueError(f"屏幕尺寸为 {self.width}×{self.height}，需要 320×170")
            self.rotation = (
                Image.Transpose.ROTATE_90 if rotate else Image.Transpose.ROTATE_270
            ) if (self.width, self.height) == SIZE[::-1] else (
                Image.Transpose.ROTATE_180 if rotate else None)
            self.channels = [variable[i:i + 3] for i in (8, 11, 14)]
            if (bits not in (16, 24, 32) or fixed.type != 0 or fixed.visual != 2
                    or any(reverse or length < 1 or offset + length > bits
                           for offset, length, reverse in self.channels)):
                raise ValueError("不支持此屏幕的颜色格式")
            self.depth = bits // 8
            self.stride = fixed.line_length
            self.row_bytes = self.width * self.depth
            self.start = y_offset * self.stride + x_offset * self.depth
            end = self.start + (self.height - 1) * self.stride + self.row_bytes
            if x_offset * self.depth + self.row_bytes > self.stride or end > fixed.smem_len:
                raise ValueError("屏幕缓冲区尺寸不正确")
            self.buffer = mmap.mmap(handle.fileno(), fixed.smem_len)
            self.luts = [[(value * ((1 << length) - 1) // 255) << offset
                          for value in range(256)] for offset, length, _ in self.channels]

    def show(self, image):
        if self.rotation is not None:
            image = image.transpose(self.rotation)
        offsets = [offset for offset, _, _ in self.channels]
        if self.depth in (3, 4) and self.channels == [(16, 8, 0), (8, 8, 0), (0, 8, 0)]:
            pixels = image.tobytes("raw", "BGRX" if self.depth == 4 else "BGR")
        elif self.depth == 2:
            red, green, blue = self.luts
            samples = iter(image.tobytes())
            pixels = array("H", (red[r] | green[g] | blue[b]
                                 for r, g, b in zip(samples, samples, samples))).tobytes()
        elif self.depth == 4 and offsets == [0, 8, 16] and all(
                length == 8 for _, length, _ in self.channels):
            pixels = image.tobytes("raw", "RGBX")
        else:
            red, green, blue = self.luts
            pixels = b"".join((red[r] | green[g] | blue[b]).to_bytes(self.depth, "little")
                              for r, g, b in image.getdata())
        if self.stride == self.row_bytes:
            self.buffer[self.start:self.start + len(pixels)] = pixels
        else:
            for row in range(self.height):
                start = self.start + row * self.stride
                src = row * self.row_bytes
                self.buffer[start:start + self.row_bytes] = pixels[src:src + self.row_bytes]

    def close(self):
        self.buffer.close()


class Keyboard:
    def __init__(self, path):
        import evdev

        self.codes = evdev.ecodes
        self.device = evdev.InputDevice(path) if path else None
        if not self.device:
            for candidate in sorted(Path("/dev/input").glob("event*")):
                try:
                    device = evdev.InputDevice(str(candidate))
                except OSError:
                    continue
                if "tca8418" in device.name.lower():
                    self.device = device
                    break
                device.close()
        if not self.device:
            path = "/dev/input/by-path/platform-3f804000.i2c-event"
            if os.path.exists(path):
                self.device = evdev.InputDevice(path)
        if not self.device:
            raise OSError("找不到内置键盘，可用 --keyboard 指定输入设备")
        self.modifiers = set()

    def poll(self):
        if not select.select([self.device.fd], [], [], 0.05)[0]:
            return
        try:
            events = list(self.device.read())
        except BlockingIOError:
            return
        for event in events:
            if event.type != self.codes.EV_KEY:
                continue
            code = self.codes.KEY.get(event.code, "")
            if isinstance(code, list):
                code = code[0]
            key = code.removeprefix("KEY_").lower()
            if key in ("leftshift", "rightshift", "leftctrl", "rightctrl"):
                if event.value:
                    self.modifiers.add(key)
                else:
                    self.modifiers.discard(key)
                continue
            if not event.value:
                continue
            if key == "esc":
                key = "escape"
            if event.value == 2 and key in ("escape", "enter", "tab", "s", "space",
                                            "4", "5", "6", "7", "8"):
                # Repeated letters and spaces are still valid in a text field.
                repeat = True
            else:
                repeat = False
            punctuation = {"space": " ", "minus": "-", "equal": "=", "slash": "/",
                           "comma": ",", "dot": ".", "apostrophe": "'", "semicolon": ";",
                           "leftbrace": "[", "rightbrace": "]", "grave": "`", "backslash": "\\"}
            char = key if len(key) == 1 else punctuation.get(key, "")
            shifted = any(modifier.endswith("shift") for modifier in self.modifiers)
            ctrl = any(modifier.endswith("ctrl") for modifier in self.modifiers)
            if shifted:
                char = char.upper().translate(str.maketrans(
                    "1234567890-=/,.';[]`\\", "!@#$%^&*()_+?<>\":{}~|"))
            if char and key != "space":
                key = char.lower()
            yield key, "" if ctrl else char, ctrl and key == "u", repeat

    def close(self):
        self.device.close()


def run(app, screen, framebuffer, keyboard, rotate):
    display = Framebuffer(framebuffer, rotate)
    try:
        keys = Keyboard(keyboard)
        try:
            while app.running:
                app.tick()
                if app.dirty:
                    display.show(screen.render(app))
                    app.dirty = False
                for key, char, clear, repeat in keys.poll():
                    if not repeat or app.modal in ("search", "filter_text") and char:
                        app.handle(key, char, clear)
        finally:
            keys.close()
    finally:
        display.close()
