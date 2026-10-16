"""Bygger swpt.ico och logo.png från logo.svg.  Kör: python make_icon.py"""
import os
import struct
import sys

from PySide6.QtCore import QBuffer, QIODevice, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

HERE = os.path.dirname(os.path.abspath(__file__))


def render(renderer, size):
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    renderer.render(p)
    p.end()
    return img


def png_bytes(img):
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


def main():
    QGuiApplication(sys.argv)
    renderer = QSvgRenderer(os.path.join(HERE, "logo.svg"))
    sizes = (16, 20, 24, 32, 40, 48, 64, 128, 256)
    pngs = [png_bytes(render(renderer, s)) for s in sizes]
    # ICO med PNG-poster (stöds sedan Windows Vista).
    header = struct.pack("<3H", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    entries, data = b"", b""
    for s, png in zip(sizes, pngs):
        entries += struct.pack("<4B2H2I", s % 256, s % 256, 0, 0, 1, 32, len(png), offset + len(data))
        data += png
    with open(os.path.join(HERE, "swpt.ico"), "wb") as f:
        f.write(header + entries + data)
    render(renderer, 256).save(os.path.join(HERE, "logo.png"))


if __name__ == "__main__":
    main()
