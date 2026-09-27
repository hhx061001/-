"""Render the app's vector mark to Windows icon and Qt PNG assets."""

from pathlib import Path
import struct

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer


ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "static"
SIZES = (16, 24, 32, 48, 64, 128, 256)


def render_png(renderer: QSvgRenderer, size: int) -> bytes:
    image = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)
    renderer.render(painter)
    painter.end()
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


def main() -> None:
    app = QGuiApplication([])
    renderer = QSvgRenderer(str(ASSETS / "logo.svg"))
    if not renderer.isValid():
        raise RuntimeError("logo.svg could not be rendered")
    images = [(size, render_png(renderer, size)) for size in SIZES]
    (ASSETS / "logo.png").write_bytes(images[-1][1])
    offset = 6 + 16 * len(images)
    entries = []
    for size, png in images:
        entries.append(struct.pack("<BBBBHHII", size if size < 256 else 0,
                                   size if size < 256 else 0, 0, 0, 1, 32,
                                   len(png), offset))
        offset += len(png)
    (ASSETS / "logo.ico").write_bytes(
        struct.pack("<HHH", 0, 1, len(images)) + b"".join(entries)
        + b"".join(png for _, png in images)
    )
    app.quit()


if __name__ == "__main__":
    main()
