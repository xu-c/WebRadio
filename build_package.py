"""Build a Debian package without requiring a cross-compiler or dpkg-deb."""

import gzip
import io
import tarfile
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent
NAME = "cardputer-zero-radio"
VERSION = "1.0.11-1"
ARCHITECTURE = "arm64"
SHARE = f"usr/share/{NAME}"
LAUNCHER = "usr/share/APPLaunch"
ICON = f"{LAUNCHER}/share/images/{NAME}.png"


def radio_icon():
    image = Image.new("RGBA", (128, 128))
    draw = ImageDraw.Draw(image)
    ink, mint, amber = "#18282f", "#83f0d0", "#ffcf78"
    draw.rounded_rectangle((0, 0, 127, 127), 28, fill=ink)
    draw.line((33, 49, 83, 23), fill=mint, width=6)
    draw.ellipse((79, 19, 87, 27), fill=mint)
    draw.rounded_rectangle((16, 44, 111, 105), 12, fill=mint)
    for y in (59, 68, 77, 86):
        draw.rounded_rectangle((28, y, 58, y + 4), 2, fill=ink)
    draw.rounded_rectangle((67, 54, 102, 93), 7, fill=ink)
    draw.ellipse((73, 62, 96, 85), fill=amber)
    draw.line((84, 73, 90, 67), fill=ink, width=3)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def tar_gz(files):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        directories = set()
        for path, (content, mode) in files.items():
            parent = ""
            for component in path.split("/")[:-1]:
                parent += "/" + component
                if parent in directories:
                    continue
                directory = tarfile.TarInfo("." + parent)
                directory.type = tarfile.DIRTYPE
                directory.mode = 0o755
                directory.uname = directory.gname = "root"
                archive.addfile(directory)
                directories.add(parent)
            info = tarfile.TarInfo("./" + path)
            info.mode = mode
            info.size = len(content)
            info.uname = info.gname = "root"
            archive.addfile(info, io.BytesIO(content))
    return gzip.compress(buffer.getvalue(), mtime=0)


def ar_member(name, content):
    header = f"{name + '/':<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(content):<10}`\n"
    return header.encode("ascii") + content + (b"\n" if len(content) % 2 else b"")


def main():
    control = f"""Package: {NAME}
Version: {VERSION}
Section: sound
Priority: optional
Architecture: {ARCHITECTURE}
Maintainer: xu-c <3174585+xu-c@users.noreply.github.com>
Depends: python3 (>= 3.9), python3-pil (>= 9.2), python3-evdev, python3-pypinyin, fonts-noto-cjk, mpv, pulseaudio-utils, ca-certificates
Description: WebRadio for Cardputer Zero
 Find and play Radio Browser stations with keyboard search, filters and favorites.
"""
    desktop = f"""[Desktop Entry]
Type=Application
Name=WebRadio
Exec=/usr/bin/{NAME}
Icon=/{ICON}
Terminal=false
Categories=AudioVideo;Audio;
"""
    entry = f'#!/bin/sh\nexec /usr/bin/python3 -B /{SHARE}/radio.py --device "$@"\n'
    payload = {f"{SHARE}/{name}": ((ROOT / name).read_bytes(), 0o644) for name in (
        "radio.py", "radio_directory.py", "radio_player.py", "radio_ui.py", "radio_device.py")}
    payload.update({
        f"usr/bin/{NAME}": (entry.encode(), 0o755),
        f"{LAUNCHER}/applications/{NAME}.desktop": (desktop.encode(), 0o644),
        ICON: (radio_icon(), 0o644),
        f"usr/share/doc/{NAME}/README.md": ((ROOT / "README.md").read_bytes(), 0o644),
    })
    output = ROOT / "dist" / f"{NAME}_{VERSION}_{ARCHITECTURE}.deb"
    output.parent.mkdir(exist_ok=True)
    output.write_bytes(b"!<arch>\n" + b"".join((
        ar_member("debian-binary", b"2.0\n"),
        ar_member("control.tar.gz", tar_gz({"control": (control.encode(), 0o644)})),
        ar_member("data.tar.gz", tar_gz(payload)),
    )))
    print(output)


if __name__ == "__main__":
    main()
