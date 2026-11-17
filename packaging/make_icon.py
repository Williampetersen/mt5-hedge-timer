"""Draw the Aitrade icon (a green arrow up and a red arrow down) and save it as aitrade/aitrade.ico.

  .venv\\Scripts\\python.exe packaging\\make_icon.py
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPolygonF

ROOT = Path(__file__).resolve().parent.parent
SIZES = (256, 128, 64, 48, 32, 24, 16)
BUY, SELL = "#2ecc71", "#ff6b6b"


def arrow(cx: float, top: float, bottom: float, width: float, *, up: bool) -> QPolygonF:
    """An arrow between `top` and `bottom`, centred on cx."""
    head, stem = width / 2, width * 0.2
    neck = top + (bottom - top) * 0.46 if up else bottom - (bottom - top) * 0.46
    if up:
        points = [(cx, top), (cx + head, neck), (cx + stem, neck), (cx + stem, bottom), (cx - stem, bottom),
                  (cx - stem, neck), (cx - head, neck)]
    else:
        points = [(cx, bottom), (cx + head, neck), (cx + stem, neck), (cx + stem, top), (cx - stem, top),
                  (cx - stem, neck), (cx - head, neck)]
    return QPolygonF([QPointF(x, y) for x, y in points])


def draw(size: int = 256) -> QImage:
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    scale = size / 256.0
    p.scale(scale, scale)
    back = QLinearGradient(0, 0, 0, 256)
    back.setColorAt(0.0, QColor("#1b2431"))
    back.setColorAt(1.0, QColor("#0c1116"))
    tile = QPainterPath()
    tile.addRoundedRect(QRectF(8, 8, 240, 240), 54, 54)
    p.fillPath(tile, back)
    p.setPen(QPen(QColor("#33404f"), 4))
    p.drawPath(tile)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(BUY))
    p.drawPolygon(arrow(88, 52, 196, 84, up=True))
    p.setBrush(QColor(SELL))
    p.drawPolygon(arrow(168, 60, 204, 84, up=False))
    p.end()
    return image


def png_bytes(image: QImage) -> bytes:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return bytes(data)


def write_ico(path: Path) -> None:
    """An .ico file is a small table followed by one picture per size (PNG pictures are allowed)."""
    pictures = [(size, png_bytes(draw(size))) for size in SIZES]
    header = struct.pack("<HHH", 0, 1, len(pictures))
    offset = len(header) + 16 * len(pictures)
    table, body = b"", b""
    for size, data in pictures:
        table += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset)
        body += data
        offset += len(data)
    path.write_bytes(header + table + body)


def main() -> int:
    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])  # noqa: F841 (painting needs it)
    out = ROOT / "aitrade" / "aitrade.ico"
    write_ico(out)
    draw(256).save(str(ROOT / "packaging" / "icon-preview.png"))
    print("wrote", out, out.stat().st_size, "bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
