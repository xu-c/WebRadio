"""Build a Debian package without requiring a cross-compiler or dpkg-deb."""

import gzip
import io
import tarfile
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent
NAME = "cardputer-zero-radio"
VERSION = "1.0.1"
SHARE = f"usr/share/{NAME}"
LAUNCHER = "usr/share/APPLaunch"
ICON = f"{LAUNCHER}/share/images/{NAME}.png"


def radio_icon():
    image = Image.new("RGBA", (128, 128))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, 127, 127), 29, fill="#18282f")
    draw.arc((22, 24, 106, 107), 211, 329, fill="#83f0d0", width=6)
    draw.arc((35, 39, 93, 95), 212, 328, fill="#83f0d0", width=6)
    draw.ellipse((58, 60, 70, 72), fill="#83f0d0")
    draw.line((42, 93, 86, 93), fill="#ffcf78", width=5)
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
Architecture: all
Maintainer: WebRadio
Depends: python3 (>= 3.9), python3-pil (>= 9.2), python3-evdev, fonts-noto-cjk, mpv, ca-certificates
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
    output = ROOT / "dist" / f"{NAME}_{VERSION}_all.deb"
    output.parent.mkdir(exist_ok=True)
    output.write_bytes(b"!<arch>\n" + b"".join((
        ar_member("debian-binary", b"2.0\n"),
        ar_member("control.tar.gz", tar_gz({"control": (control.encode(), 0o644)})),
        ar_member("data.tar.gz", tar_gz(payload)),
    )))
    print(output)


if __name__ == "__main__":
    main()
