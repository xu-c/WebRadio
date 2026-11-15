"""Start the Cardputer Zero radio or its desktop preview."""

import argparse
import os
import signal
import sys
from pathlib import Path

from radio_directory import Library, RadioBrowser
from radio_ui import BG, DIGIT_SHORTCUTS, RadioApp, Screen, SIZE


def font_path():
    candidates = (
        os.getenv("ZERO_RADIO_FONT"),
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "C:/Windows/Fonts/msyh.ttc",
        "/System/Library/Fonts/PingFang.ttc",
    )
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise OSError("找不到中文字体。请安装 Noto Sans CJK 或设置 ZERO_RADIO_FONT。")


def run_preview(app, screen, zoom):
    import tkinter as tk

    from PIL import Image, ImageTk

    root = tk.Tk()
    root.title("WebRadio · Cardputer Zero")
    root.configure(bg=BG)
    root.resizable(False, False)
    canvas = tk.Canvas(root, width=SIZE[0] * zoom, height=SIZE[1] * zoom,
                       bg=BG, highlightthickness=0)
    canvas.pack()
    frame = canvas.create_image(0, 0, anchor="nw")
    root.protocol("WM_DELETE_WINDOW", lambda: setattr(app, "running", False))
    escape_release = None

    def release_escape(_event=None):
        nonlocal escape_release
        if escape_release is not None and _event is not None:
            root.after_cancel(escape_release)
        escape_release = None
        app.escape_key(False)

    def schedule_escape_release(_event):
        nonlocal escape_release
        escape_release = root.after_idle(release_escape)

    def keypress(event):
        nonlocal escape_release
        key = {"Return": "enter", "KP_Enter": "enter", "BackSpace": "backspace"}.get(
            event.keysym, event.keysym.lower())
        if key == "escape":
            if escape_release is not None:
                root.after_cancel(escape_release)
                escape_release = None
            app.escape_key(True)
            return
        ctrl = bool(event.state & 4)
        if app.modal in ("search", "filter_text") and ctrl and key == "v":
            try:
                for char in root.clipboard_get():
                    if len(app.input) >= 80:
                        break
                    app.handle("", char)
            except tk.TclError:
                pass
            return
        char = "" if ctrl else event.char
        if char and char.isprintable() and char != " ":
            key = char.lower()
        if ctrl and key == "p":
            key = "ctrl+p"
        elif ctrl and key in tuple(DIGIT_SHORTCUTS):
            key = "f" + key
        app.handle(key, char, ctrl and key == "u")
        if key == "tab":
            return "break"

    def refresh():
        if not app.running:
            root.destroy()
            return
        app.tick()
        if app.dirty:
            image = screen.render(app).resize((SIZE[0] * zoom, SIZE[1] * zoom),
                                              Image.Resampling.NEAREST)
            canvas.photo = ImageTk.PhotoImage(image)
            canvas.itemconfigure(frame, image=canvas.photo)
            app.dirty = False
        root.after(35, refresh)

    root.bind("<KeyPress>", keypress)
    root.bind("<KeyRelease-Escape>", schedule_escape_release)
    root.bind("<FocusOut>", release_escape)
    refresh()
    root.mainloop()


def main():
    parser = argparse.ArgumentParser(description="WebRadio for Cardputer Zero")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--device", action="store_true", help="use Linux framebuffer and keyboard")
    modes.add_argument("--demo", action="store_true", help="desktop preview with sample stations")
    parser.add_argument("--framebuffer", default=os.getenv("LV_LINUX_FBDEV_DEVICE", "/dev/fb0"))
    parser.add_argument("--keyboard", default=os.getenv("LV_LINUX_KEYBOARD_DEVICE"))
    parser.add_argument("--rotate", action="store_true", help="reverse display orientation")
    parser.add_argument("--zoom", type=int, choices=range(1, 5), default=3)
    args = parser.parse_args()
    if args.device and not sys.platform.startswith("linux"):
        parser.error("--device requires Linux")

    name = "cardputer-zero-radio-demo" if args.demo else "cardputer-zero-radio"
    data = Path(os.getenv("XDG_DATA_HOME") or Path.home() / ".local" / "share") / name
    cache = Path(os.getenv("XDG_CACHE_HOME") or Path.home() / ".cache") / name
    try:
        screen = Screen(font_path())
        directory = None if args.demo else RadioBrowser(cache)
        app = RadioApp(directory, Library(data), args.demo)
        signal.signal(signal.SIGTERM, lambda *_: setattr(app, "running", False))
        try:
            if args.device:
                from radio_device import run

                run(app, screen, args.framebuffer, args.keyboard, args.rotate)
            else:
                run_preview(app, screen, args.zoom)
        finally:
            app.close()
    except (OSError, ValueError) as error:
        print(f"WebRadio: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
