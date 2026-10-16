"""Snabb förhandsvisning (mellanslag).

PDF och bilder visas i en egen vy: scroll bläddrar, Ctrl+scroll zoomar, dra med musen
markerar text (Ctrl+C kopierar) och sökfältet överst hittar text i dokumentet.
Övriga dokument (Word, Excel, PowerPoint, Outlook …) visas med Windows egna
förhandsvisare (IPreviewHandler), med miniatyr som reserv.
"""
import ctypes
import os
import threading
from ctypes import wintypes

from PySide6.QtCore import (QFileInfo, QMimeDatabase, QPoint, QPointF, QRect, QRectF, QSettings, QSizeF, Qt,
                            QTimer, Signal)
from PySide6.QtGui import (QColor, QFont, QGuiApplication, QImage, QImageReader, QKeySequence, QPainter,
                           QPainterPath, QPen, QShortcut, QTextCharFormat, QTextCursor, QTextDocument)
import ocr
import query as Q
import re
from i18n import t
from PySide6.QtPdf import QPdfDocument, QPdfDocumentRenderOptions
from PySide6.QtWidgets import (QApplication, QDialog, QFileIconProvider, QHBoxLayout, QLabel, QLineEdit, QMenu,
                               QPlainTextEdit, QScrollBar, QSizePolicy, QStyle, QTextEdit, QToolButton,
                               QVBoxLayout, QWidget)

MAX_TEXT_BYTES = 2 * 1024 * 1024
A4 = 210.0 / 297.0
DOC_SCROLLBAR = 24
SEARCH_BAR_H = 44
SETTINGS = ("swpt", "swpt")

HIT = QColor(255, 210, 0, 110)
HIT_CURRENT = QColor(255, 120, 0, 150)
SELECTION = QColor(0, 120, 215, 90)


def _fitz():
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz
    return fitz


# ---------------------------------------------------------------- dokumentvy (PDF och bilder)

class DocView(QWidget):
    """Ritar PDF-sidor (eller en bild). Scroll = bläddra, Ctrl+scroll = zoom vid pekaren,
    dra = markera text, mittenknapp-dra = panorera."""
    hits_changed = Signal(int, int)   # antal träffar, aktuell (0-baserad, -1 = ingen)
    _ocr_page = Signal(int, list)     # från OCR-tråden: sida, ord
    PAGE_GAP, MARGIN, MAX_ZOOM_OVER_FIT, DRAG_THRESHOLD, WHEEL_PX = 12.0, 10.0, 25.0, 4, 90

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.pdf = None
        self.fz = None
        self.image = QImage()
        self.page_sizes, self.page_rects = [], []
        self.content = QSizeF()
        self.scale, self.offset = 1.0, QPointF()
        self.fitted = False
        self.tiles = {}
        self.words = {}
        self.anchor = self.caret = None   # (sida, ordindex)
        self._press_word = None
        self.mode = None                  # "select" / "pan"
        self.press = self.last = QPointF()
        self.moved = False
        self.hits, self.hit_index = [], -1
        self._search_text, self._search_page = "", 0
        self._exact = False
        self._syncing = False
        self.ocr_words = {}               # sida -> ord från OCR (skannade sidor)
        self.ocr_pending = 0
        self._ocr_stop = False
        self._ocr_page.connect(self._on_ocr_page)

        self.render_timer = QTimer(self, singleShot=True, interval=60)
        self.render_timer.timeout.connect(self.render_visible)
        self.search_timer = QTimer(self, singleShot=True, interval=0)
        self.search_timer.timeout.connect(self._search_step)
        self.vbar = QScrollBar(Qt.Vertical, self)
        self.hbar = QScrollBar(Qt.Horizontal, self)
        for bar in (self.vbar, self.hbar):
            bar.valueChanged.connect(self._from_bars)
            bar.hide()
        QShortcut(QKeySequence.Copy, self, self.copy, context=Qt.WidgetWithChildrenShortcut)
        QShortcut(QKeySequence.SelectAll, self, self.select_all, context=Qt.WidgetWithChildrenShortcut)

    # --- innehåll
    def set_image(self, image, path=None):
        self.image = image
        self.page_sizes = [QSizeF(image.size())]
        if path and ocr.available():
            try:
                self.fz = _fitz().open(path)
                self._start_ocr(path, is_image=True)
            except Exception:
                self.fz = None
        self.setCursor(Qt.IBeamCursor if self.fz else Qt.OpenHandCursor)
        self._layout()

    def set_pdf(self, path):
        doc = QPdfDocument(self)
        if doc.load(path) != QPdfDocument.Error.None_ or doc.pageCount() < 1:
            doc.deleteLater()
            return False
        self.pdf = doc
        self.page_sizes = [doc.pagePointSize(i) for i in range(doc.pageCount())]
        try:
            self.fz = _fitz().open(path)
        except Exception:
            self.fz = None
        self.setCursor(Qt.IBeamCursor if self.fz else Qt.OpenHandCursor)
        self._layout()
        if self.fz is not None:
            self._start_ocr(path, is_image=False)
        return True

    # --- OCR av skannade sidor (i bakgrunden), så att man kan söka och markera text även där
    def _start_ocr(self, path, is_image):
        if not ocr.available():
            return
        sizes = [s.width() for s in self.page_sizes]

        def run():
            try:
                doc = _fitz().open(path)
                pages = [0] if is_image else [i for i, pg in enumerate(doc) if ocr.needs_ocr(pg)]
                self.ocr_pending = len(pages)
                for i in pages:
                    if self._ocr_stop:
                        return
                    scale = sizes[i] / max(doc[i].rect.width, 1)
                    words = [(x0 * scale, y0 * scale, x1 * scale, y1 * scale, w, ln)
                             for x0, y0, x1, y1, w, ln in ocr.page_words(doc[i], is_image)]
                    self._ocr_page.emit(i, words)
            except Exception:
                self.ocr_pending = 0
                try:
                    self._ocr_page.emit(-1, [])
                except RuntimeError:
                    pass
        threading.Thread(target=run, daemon=True).start()

    def _on_ocr_page(self, page, words):
        if page < 0:
            self.hits_changed.emit(len(self.hits), self.hit_index)
            return
        self.ocr_pending = max(0, self.ocr_pending - 1)
        conv = [(QRectF(x0, y0, x1 - x0, y1 - y0), w, 0, ln) for x0, y0, x1, y1, w, ln in words]
        self.ocr_words[page] = conv
        self.words[page] = conv
        if self._search_text and page < self._search_page:   # sökningen har redan passerat sidan
            current = self.hits[self.hit_index] if 0 <= self.hit_index < len(self.hits) else None
            self.hits = sorted(self.hits + self._word_hits(page), key=lambda h: (h[0], h[1].top(), h[1].left()))
            if current is not None:
                self.hit_index = self.hits.index(current)
            elif self.hits:
                self.hit_index = 0
                self._show_hit()
        self.hits_changed.emit(len(self.hits), self.hit_index)
        self.update()

    def _word_hits(self, page):
        """Träffar bland sidans ord (från PDF:en eller OCR): sökordet letas upp rad för rad och
        markeras över de ord det täcker. Med citattecken ("135") bara som hela ord."""
        pat = Q.word_re(self._search_text) if self._exact else re.compile(re.escape(self._search_text), re.IGNORECASE)
        lines = {}
        for r, w, block, ln in self._words(page):
            lines.setdefault((block, ln), []).append((r, w))
        hits = []
        for words in lines.values():
            spans, pos = [], 0
            for r, w in words:
                spans.append((pos, pos + len(w), r))
                pos += len(w) + 1
            for m in pat.finditer(" ".join(w for _, w in words)):
                rect = None
                for a, b, r in spans:
                    if a < m.end() and b > m.start():
                        rect = QRectF(r) if rect is None else rect.united(r)
                if rect is not None:
                    hits.append((page, rect))
        return hits

    def can_search(self):
        return self.fz is not None

    def first_page_aspect(self):
        s = self.page_sizes[0] if self.page_sizes else QSizeF()
        return s.width() / s.height() if s.height() > 0 else A4

    def _layout(self):
        max_w = max((s.width() for s in self.page_sizes), default=0)
        y = self.MARGIN
        self.page_rects = []
        for s in self.page_sizes:
            self.page_rects.append(QRectF(self.MARGIN + (max_w - s.width()) / 2, y, s.width(), s.height()))
            y += s.height() + self.PAGE_GAP
        self.content = QSizeF(max_w + 2 * self.MARGIN, y - self.PAGE_GAP + self.MARGIN)
        self.tiles, self.fitted = {}, False
        self.update()

    # --- geometri
    def _bar_extent(self):
        return self.style().pixelMetric(QStyle.PM_ScrollBarExtent)

    def view_w(self):
        return max(1, self.width() - (self._bar_extent() if self.pdf else 0))

    def fit_scale(self):
        if self.content.isEmpty() or self.width() <= 0:
            return 1.0
        s = self.view_w() / self.content.width()
        if not self.pdf:
            s = min(s, self.height() / self.content.height())
        return s

    def fit_width(self):
        self.scale, self.offset, self.fitted = self.fit_scale(), QPointF(), True
        self._clamp()
        self._schedule()
        self.update()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        ext = self._bar_extent()
        self.vbar.setGeometry(self.width() - ext, 0, ext, self.height())
        self.hbar.setGeometry(0, self.height() - ext, self.width() - ext, ext)
        if not self.fitted or self.scale <= self.fit_scale() * 1.0001:
            self.fit_width()
        else:
            self._clamp()
            self._schedule()

    def _clamp(self):
        vw, vh = self.view_w() / self.scale, self.height() / self.scale
        cw, ch = self.content.width(), self.content.height()
        x = (cw - vw) / 2 if cw <= vw else min(max(0.0, self.offset.x()), cw - vw)
        y = (ch - vh) / 2 if ch <= vh else min(max(0.0, self.offset.y()), ch - vh)
        self.offset = QPointF(x, y)
        self._sync_bars()

    def _sync_bars(self):
        self._syncing = True
        for bar, total, view, pos in ((self.vbar, self.content.height(), self.height(), self.offset.y()),
                                      (self.hbar, self.content.width(), self.view_w(), self.offset.x())):
            full = total * self.scale
            if full > view + 1:
                bar.setRange(0, int(full - view))
                bar.setPageStep(int(view))
                bar.setSingleStep(self.WHEEL_PX // 2)
                bar.setValue(int(pos * self.scale))
                bar.show()
            else:
                bar.hide()
        self._syncing = False

    def _from_bars(self):
        if self._syncing:
            return
        x = self.hbar.value() / self.scale if self.hbar.isVisible() else self.offset.x()
        y = self.vbar.value() / self.scale if self.vbar.isVisible() else self.offset.y()
        self.offset = QPointF(x, y)
        self._clamp()
        self._schedule()
        self.update()

    def scroll_by(self, dx, dy):
        self.offset += QPointF(dx, dy) / self.scale
        self._clamp()
        self._schedule()
        self.update()

    def zoom_at(self, pos, factor):
        fit = self.fit_scale()
        new = min(max(fit * (0.25 if self.pdf else 1.0), self.scale * factor), fit * self.MAX_ZOOM_OVER_FIT)
        if abs(new - self.scale) < 1e-9:
            return
        anchor = self.offset + pos / self.scale
        self.scale = new
        self.offset = anchor - pos / self.scale
        self._clamp()
        self._schedule()
        self.update()

    def _on_screen(self, page, r=None):
        base = self.page_rects[page]
        if r is not None:
            base = r.translated(base.topLeft())
        return QRectF((base.topLeft() - self.offset) * self.scale, base.size() * self.scale)

    # --- ritning
    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0x2b, 0x2b, 0x2b))
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        view = QRectF(self.rect())
        stale = False
        sel = self._selected_rects()
        for i, page in enumerate(self.page_rects):
            target = self._on_screen(i)
            if not target.intersects(view):
                continue
            if not self.pdf:
                p.drawImage(target, self.image)
                continue
            p.fillRect(target, Qt.white)
            tile = self.tiles.get(i)
            if tile:
                img, src, scale = tile
                p.drawImage(QRectF((page.topLeft() + src.topLeft() - self.offset) * self.scale,
                                   src.size() * self.scale), img)
                visible = QRectF(self.offset - page.topLeft(), QSizeF(self.view_w(), self.height()) / self.scale) \
                    .intersected(QRectF(QPointF(), page.size()))
                if abs(scale - self.scale) > 1e-9 or not src.contains(visible):
                    stale = True
            else:
                stale = True
            for k, (hp, r) in enumerate(self.hits):
                if hp == i:
                    p.fillRect(self._on_screen(i, r).adjusted(-1, -1, 1, 1),
                               HIT_CURRENT if k == self.hit_index else HIT)
            for r in sel.get(i, ()):
                p.fillRect(self._on_screen(i, r), SELECTION)
        p.end()
        if stale:
            self._schedule()

    def _schedule(self):
        if self.pdf and not self.render_timer.isActive():
            self.render_timer.start()

    def render_visible(self):
        if not self.pdf:
            return
        dpr = self.devicePixelRatioF()
        view = QRectF(self.offset, QSizeF(self.view_w(), self.height()) / self.scale)
        wanted = view.adjusted(-view.width() * .25, -view.height() * .25, view.width() * .25, view.height() * .25)
        tiles = {}
        for i, page in enumerate(self.page_rects):
            part = wanted.intersected(page)
            if part.isEmpty():
                continue
            source = part.translated(-page.topLeft())
            px = self.scale * dpr
            full = (page.size() * px).toSize()
            clip = QRectF(source.topLeft() * px, source.size() * px).toAlignedRect() \
                .intersected(QRect(QPoint(), full))
            if clip.isEmpty():
                continue
            opts = QPdfDocumentRenderOptions()
            opts.setScaledSize(full)
            opts.setScaledClipRect(clip)
            opts.setRenderFlags(QPdfDocumentRenderOptions.RenderFlag.Annotations)
            img = self.pdf.render(i, clip.size(), opts)
            if img.isNull():
                continue
            img.setDevicePixelRatio(1.0)
            tiles[i] = (img, QRectF(QPointF(clip.topLeft()) / px, QSizeF(clip.size()) / px), self.scale)
        self.tiles = tiles
        self.update()

    # --- text (ord från PyMuPDF, i samma sidkoordinater som QtPdf ritar i)
    def _words(self, page):
        if page in self.ocr_words:
            return self.ocr_words[page]
        if page not in self.words:
            out = []
            if self.fz:
                try:
                    pg = self.fz[page]
                    m = pg.rotation_matrix
                    for x0, y0, x1, y1, text, block, line, _ in pg.get_text("words"):
                        r = _fitz().Rect(x0, y0, x1, y1) * m
                        out.append((QRectF(r.x0, r.y0, r.width, r.height), text, block, line))
                except Exception:
                    pass
            self.words[page] = out
        return self.words[page]

    def _page_at(self, pos):
        c = self.offset + pos / self.scale
        best, dist = 0, float("inf")
        for i, r in enumerate(self.page_rects):
            d = 0 if r.top() <= c.y() <= r.bottom() else min(abs(c.y() - r.top()), abs(c.y() - r.bottom()))
            if d < dist:
                best, dist = i, d
            if d == 0:
                break
        return best, c - self.page_rects[best].topLeft()

    def _word_at(self, pos):
        page, pt = self._page_at(pos)
        words = self._words(page)
        if not words:
            return None

        def key(i):
            r = words[i][0]
            dx = max(r.left() - pt.x(), 0.0, pt.x() - r.right())
            dy = max(r.top() - pt.y(), 0.0, pt.y() - r.bottom())
            return (dy * 4) ** 2 + dx ** 2
        return page, min(range(len(words)), key=key)

    def _selection_range(self):
        if self.anchor is None or self.caret is None:
            return None
        return min(self.anchor, self.caret), max(self.anchor, self.caret)

    def _selected_words(self):
        rng = self._selection_range()
        if not rng:
            return
        (p0, i0), (p1, i1) = rng
        for p in range(p0, p1 + 1):
            words = self._words(p)
            a = i0 if p == p0 else 0
            b = i1 if p == p1 else len(words) - 1
            for i in range(a, b + 1):
                yield p, words[i]

    def _selected_rects(self):
        """Markeringen som en rektangel per rad och sida."""
        out, key = {}, None
        for p, (r, _, block, line) in self._selected_words():
            if (p, block, line) == key:
                out[p][-1] = out[p][-1].united(r)
            else:
                out.setdefault(p, []).append(QRectF(r))
                key = (p, block, line)
        return out

    def selected_text(self):
        parts, key = [], None
        for p, (_, text, block, line) in self._selected_words():
            if key is not None:
                parts.append(" " if (p, block, line) == key else "\n")
            parts.append(text)
            key = (p, block, line)
        return "".join(parts)

    def copy(self):
        text = self.selected_text()
        if text:
            QGuiApplication.clipboard().setText(text)

    def select_all(self):
        if not self.fz:
            return
        pages = [p for p in range(len(self.page_rects)) if self._words(p)]
        if pages:
            self.anchor, self.caret = (pages[0], 0), (pages[-1], len(self._words(pages[-1])) - 1)
            self.update()

    # --- sökning
    def search(self, text):
        self.search_timer.stop()
        self.hits, self.hit_index = [], -1
        (self._search_text, self._exact), self._search_page = Q.single(text), 0
        if self._search_text and self.fz:
            self.search_timer.start()
        self.hits_changed.emit(0, -1)
        self.update()

    def _search_step(self):
        fitz = _fitz()
        n = len(self.page_rects)
        end = min(self._search_page + 15, n)
        for p in range(self._search_page, end):
            if p in self.ocr_words or self._exact:
                self.hits += self._word_hits(p)
                continue
            try:
                pg = self.fz[p]
                m = pg.rotation_matrix
                for r in pg.search_for(self._search_text):
                    r = fitz.Rect(r) * m
                    self.hits.append((p, QRectF(r.x0, r.y0, r.width, r.height)))
            except Exception:
                pass
        self._search_page = end
        if self.hits and self.hit_index < 0:
            self.hit_index = 0
            self._show_hit()
        if end < n:
            self.search_timer.start()
        self.hits_changed.emit(len(self.hits), self.hit_index)
        self.update()

    def next_hit(self, delta):
        if self.hits:
            self.hit_index = (self.hit_index + delta) % len(self.hits)
            self._show_hit()
            self.hits_changed.emit(len(self.hits), self.hit_index)
            self.update()

    def _show_hit(self):
        page, r = self.hits[self.hit_index]
        c = r.translated(self.page_rects[page].topLeft())
        vw, vh = self.view_w() / self.scale, self.height() / self.scale
        view = QRectF(self.offset, QSizeF(vw, vh))
        if not view.adjusted(0, vh * .1, 0, -vh * .1).contains(c):
            self.offset = QPointF(c.center().x() - vw / 2, c.center().y() - vh / 2)
            self._clamp()
            self._schedule()

    # --- mus och tangentbord
    def wheelEvent(self, e):
        if e.modifiers() & Qt.ControlModifier:
            steps = e.angleDelta().y() / 120.0
            if steps:
                self.zoom_at(e.position(), 1.2 ** steps)
        else:
            pd = e.pixelDelta()
            if not pd.isNull():
                dx, dy = pd.x(), pd.y()
            else:
                dx, dy = e.angleDelta().x() / 120 * self.WHEEL_PX, e.angleDelta().y() / 120 * self.WHEEL_PX
            if e.modifiers() & Qt.ShiftModifier:
                dx, dy = dy, dx
            self.scroll_by(-dx, -dy)
        e.accept()

    def mousePressEvent(self, e):
        self.setFocus()
        self.press = self.last = e.position()
        self.moved = False
        if e.button() == Qt.MiddleButton or (e.button() == Qt.LeftButton and not self.fz):
            self.mode = "pan"
            self.setCursor(Qt.ClosedHandCursor)
        elif e.button() == Qt.LeftButton:
            self.mode = "select"
            hit = self._word_at(e.position())
            if e.modifiers() & Qt.ShiftModifier and self.anchor and hit:
                self.caret = hit
            else:
                self.anchor = self.caret = None
                self._press_word = hit
            self.update()

    def mouseMoveEvent(self, e):
        if not self.mode:
            return
        if not self.moved and (e.position() - self.press).manhattanLength() < self.DRAG_THRESHOLD:
            return
        self.moved = True
        if self.mode == "pan":
            self.scroll_by(-(e.position() - self.last).x(), -(e.position() - self.last).y())
        else:
            if self.anchor is None:
                self.anchor = self._press_word
            hit = self._word_at(e.position())
            if hit and self.anchor:
                self.caret = hit
            # rulla automatiskt när man drar utanför kanten
            if e.position().y() < 0 or e.position().y() > self.height():
                self.scroll_by(0, e.position().y() if e.position().y() < 0 else e.position().y() - self.height())
            self.update()
        self.last = e.position()

    def mouseReleaseEvent(self, e):
        self.mode = None
        self.setCursor(Qt.IBeamCursor if self.fz else Qt.OpenHandCursor)

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton and self.fz:
            hit = self._word_at(e.position())
            if hit:
                self.anchor = self.caret = hit
                self.update()

    def contextMenuEvent(self, e):
        if not self.fz:
            return
        menu = QMenu(self)
        copy = menu.addAction(t("Kopiera\tCtrl+C"), self.copy)
        copy.setEnabled(self._selection_range() is not None)
        menu.addAction(t("Markera allt\tCtrl+A"), self.select_all)
        menu.exec(e.globalPos())

    def keyPressEvent(self, e):
        step = self.WHEEL_PX
        page = self.height() * 0.9
        moves = {Qt.Key_Up: (0, -step), Qt.Key_Down: (0, step), Qt.Key_Left: (-step, 0), Qt.Key_Right: (step, 0),
                 Qt.Key_PageUp: (0, -page), Qt.Key_PageDown: (0, page)}
        k = e.key()
        if k in moves:
            self.scroll_by(*moves[k])
        elif k == Qt.Key_Home:
            self.fit_width()
        elif k == Qt.Key_End:
            self.scroll_by(0, self.content.height() * self.scale)
        elif k in (Qt.Key_Plus, Qt.Key_Equal, Qt.Key_Minus):
            self.zoom_at(QPointF(self.rect().center()), 1 / 1.25 if k == Qt.Key_Minus else 1.25)
        elif k == Qt.Key_0 and e.modifiers() & Qt.ControlModifier:
            self.fit_width()
        else:
            super().keyPressEvent(e)  # Esc/mellanslag går vidare till dialogen

    def closeEvent(self, e):
        self._ocr_stop = True
        if self.fz:
            self.fz.close()
        super().closeEvent(e)


# ---------------------------------------------------------------- textvy

class TextView(QPlainTextEdit):
    hits_changed = Signal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        self.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.cursors, self.hit_index = [], -1

    def can_search(self):
        return True

    def search(self, text):
        self.cursors, self.hit_index = [], -1
        text, exact = Q.single(text)
        flags = QTextDocument.FindWholeWords if exact else QTextDocument.FindFlag(0)
        doc = self.document()
        if text:
            c = doc.find(text, 0, flags)
            while not c.isNull() and len(self.cursors) < 10000:
                self.cursors.append(c)
                c = doc.find(text, c, flags)
        if self.cursors:
            self.hit_index = 0
        self._paint_hits()
        self.hits_changed.emit(len(self.cursors), self.hit_index)

    def next_hit(self, delta):
        if self.cursors:
            self.hit_index = (self.hit_index + delta) % len(self.cursors)
            self._paint_hits()
            self.hits_changed.emit(len(self.cursors), self.hit_index)

    def _paint_hits(self):
        sels = []
        for i, c in enumerate(self.cursors):
            s = QTextEdit.ExtraSelection()
            s.cursor = c
            fmt = QTextCharFormat()
            fmt.setBackground(HIT_CURRENT if i == self.hit_index else HIT)
            s.format = fmt
            sels.append(s)
        self.setExtraSelections(sels)
        if self.hit_index >= 0:
            c = QTextCursor(self.cursors[self.hit_index])
            c.clearSelection()
            self.setTextCursor(c)
            self.centerCursor()


# ---------------------------------------------------------------- Windows-skalets förhandsvisare

_shell = None


def _shell_api():
    """Laddar COM-gränssnitten först när de behövs (snabbare start)."""
    global _shell
    if _shell is not None:
        return _shell
    import comtypes
    from comtypes import GUID, HRESULT, COMMETHOD, IUnknown
    import comtypes.client  # noqa: F401  (initierar COM i huvudtråden)

    class IPreviewHandler(IUnknown):
        _iid_ = GUID("{8895b1c6-b41f-4c1c-a562-0d564250836f}")
        _methods_ = [
            COMMETHOD([], HRESULT, "SetWindow", (["in"], wintypes.HWND), (["in"], ctypes.POINTER(wintypes.RECT))),
            COMMETHOD([], HRESULT, "SetRect", (["in"], ctypes.POINTER(wintypes.RECT))),
            COMMETHOD([], HRESULT, "DoPreview"),
            COMMETHOD([], HRESULT, "Unload"),
            COMMETHOD([], HRESULT, "SetFocus"),
            COMMETHOD([], HRESULT, "QueryFocus", (["out"], ctypes.POINTER(wintypes.HWND))),
            COMMETHOD([], HRESULT, "TranslateAccelerator", (["in"], ctypes.POINTER(wintypes.MSG))),
        ]

    class IInitializeWithFile(IUnknown):
        _iid_ = GUID("{b7d14566-0509-4cce-a71f-0a554233bd9b}")
        _methods_ = [COMMETHOD([], HRESULT, "Initialize", (["in"], wintypes.LPCWSTR), (["in"], wintypes.DWORD))]

    class IInitializeWithStream(IUnknown):
        _iid_ = GUID("{b824b49d-22ac-4161-ac8a-9916e8fa3f7f}")
        _methods_ = [COMMETHOD([], HRESULT, "Initialize", (["in"], ctypes.c_void_p), (["in"], wintypes.DWORD))]

    class IShellItemImageFactory(IUnknown):
        _iid_ = GUID("{bcc18b79-ba16-442f-80c4-8a59c30c463b}")
        _methods_ = [COMMETHOD([], HRESULT, "GetImage", (["in"], wintypes.SIZE), (["in"], ctypes.c_int),
                               (["out"], ctypes.POINTER(wintypes.HBITMAP)))]

    shlwapi = ctypes.WinDLL("shlwapi")
    shlwapi.AssocQueryStringW.argtypes = [ctypes.c_uint, ctypes.c_uint, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                          wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    shlwapi.SHCreateStreamOnFileEx.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.BOOL,
                                               ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    shell32 = ctypes.WinDLL("shell32")
    shell32.SHCreateItemFromParsingName.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(GUID),
                                                    ctypes.POINTER(ctypes.c_void_p)]
    gdi32 = ctypes.WinDLL("gdi32")
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    _shell = dict(comtypes=comtypes, GUID=GUID, IPreviewHandler=IPreviewHandler,
                  IInitializeWithFile=IInitializeWithFile, IInitializeWithStream=IInitializeWithStream,
                  IShellItemImageFactory=IShellItemImageFactory, shlwapi=shlwapi, shell32=shell32, gdi32=gdi32)
    return _shell


def shell_thumbnail(path, size=1024):
    try:
        api = _shell_api()
        f = api["IShellItemImageFactory"]
        ptr = ctypes.c_void_p()
        if api["shell32"].SHCreateItemFromParsingName(path, None, ctypes.byref(f._iid_), ctypes.byref(ptr)) != 0:
            return QImage()
        factory = ctypes.cast(ptr, ctypes.POINTER(f))
        hbmp = factory.GetImage(wintypes.SIZE(size, size), 0x1)  # SIIGBF_THUMBNAILONLY
        img = QImage.fromHBITMAP(int(hbmp)) if hbmp else QImage()
        if hbmp:
            api["gdi32"].DeleteObject(hbmp)
        return img
    except Exception:
        return QImage()


class ShellPreviewHost(QWidget):
    """Värd för en IPreviewHandler i ett eget inbyggt barnfönster."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_NativeWindow)
        self.setAttribute(Qt.WA_DontCreateNativeAncestors)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.handler = None
        self._stream = None

    def load(self, path):
        try:
            api = _shell_api()
            ext = os.path.splitext(path)[1]
            buf = ctypes.create_unicode_buffer(64)
            n = wintypes.DWORD(64)
            # ASSOCF_INIT_DEFAULTTOSTAR = 4, ASSOCSTR_SHELLEXTENSION = 16
            if api["shlwapi"].AssocQueryStringW(4, 16, ext, "{8895b1c6-b41f-4c1c-a562-0d564250836f}", buf,
                                                ctypes.byref(n)) != 0:
                return False
            comtypes = api["comtypes"]
            handler = comtypes.client.CreateObject(api["GUID"](buf.value), clsctx=comtypes.CLSCTX_INPROC_SERVER
                                                   | comtypes.CLSCTX_LOCAL_SERVER, interface=api["IPreviewHandler"])
            ok = False
            try:
                handler.QueryInterface(api["IInitializeWithFile"]).Initialize(path, 0)
                ok = True
            except Exception:
                pass
            if not ok:
                stream = ctypes.c_void_p()
                # STGM_READ | STGM_SHARE_DENY_NONE
                if api["shlwapi"].SHCreateStreamOnFileEx(path, 0x40, 0, False, None, ctypes.byref(stream)) == 0:
                    try:
                        handler.QueryInterface(api["IInitializeWithStream"]).Initialize(stream, 0)
                        self._stream = stream
                        ok = True
                    except Exception:
                        pass
            if not ok:
                return False
            handler.SetWindow(int(self.winId()), ctypes.byref(self._rect()))
            handler.DoPreview()
            self.handler = handler
            return True
        except Exception:
            self.unload()
            return False

    def _rect(self):
        s = self.size() * self.devicePixelRatioF()
        return wintypes.RECT(0, 0, s.width(), s.height())

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.handler:
            try:
                self.handler.SetRect(ctypes.byref(self._rect()))
            except Exception:
                pass

    def unload(self):
        if self.handler:
            try:
                self.handler.Unload()
            except Exception:
                pass
            self.handler = None
        if self._stream:
            try:  # IUnknown::Release på strömmen
                release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(2, "Release")
                release(self._stream)
            except Exception:
                pass
            self._stream = None

    def closeEvent(self, e):
        self.unload()
        super().closeEvent(e)


# ---------------------------------------------------------------- flytande knappar

class ActionPill(QWidget):
    """Flytande list längst ned:  [ ✕ Stäng | ↗ Öppna ]."""

    def __init__(self, owner, on_close):
        super().__init__(owner, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowDoesNotAcceptFocus
                         | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(220, 46)
        self.path = ""
        self.on_close = on_close
        self.hover = False
        self.mouse = QPointF()
        self.press = QPointF(-1, -1)

    def left(self):
        return QRectF(0, 0, self.width() * 0.5, self.height())

    def right(self):
        return QRectF(self.width() * 0.5, 0, self.width() * 0.5, self.height())

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 190 if self.hover else 120))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        fg = QColor(255, 255, 255, 255 if self.hover else 200)
        f = QFont(self.font())
        f.setPointSizeF(10)
        f.setBold(True)
        p.setFont(f)
        for half in (self.left(), self.right()):
            if self.hover and half.contains(self.mouse):
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(255, 255, 255, 40))
                p.drawRoundedRect(half.adjusted(4, 4, -4, -4), 17, 17)

        pen = QPen(fg, 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        # vänster: kryss + "Stäng"
        l = self.left()
        x = QRectF(l.left() + 20, l.center().y() - 6, 12, 12)
        p.setPen(pen)
        p.drawLine(x.topLeft(), x.bottomRight())
        p.drawLine(x.topRight(), x.bottomLeft())
        p.drawText(QRectF(x.right() + 10, l.top(), l.right() - x.right() - 10, l.height()),
                   Qt.AlignVCenter | Qt.AlignLeft, t("Stäng"))

        p.setPen(QPen(QColor(255, 255, 255, 70), 1))
        p.drawLine(QPointF(l.right(), r.top() + 10), QPointF(l.right(), r.bottom() - 10))

        # höger: pil + "Öppna"
        o = self.right()
        box = QRectF(o.left() + 18, o.center().y() - 6, 12, 12)
        p.setPen(pen)
        arrow = QPainterPath()
        arrow.moveTo(box.left() + 4, box.top())
        arrow.lineTo(box.right(), box.top())
        arrow.lineTo(box.right(), box.top() + 8)
        arrow.moveTo(box.right(), box.top())
        arrow.lineTo(box.left(), box.bottom())
        p.drawPath(arrow)
        p.drawText(QRectF(box.right() + 10, o.top(), o.right() - box.right() - 10, o.height()),
                   Qt.AlignVCenter | Qt.AlignLeft, t("Öppna"))

    def enterEvent(self, _):
        self.hover = True
        self.update()

    def leaveEvent(self, _):
        self.hover = False
        self.update()

    def mouseMoveEvent(self, e):
        self.mouse = e.position()
        self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.press = e.position()

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        pos, press = e.position(), self.press
        self.press = QPointF(-1, -1)
        if self.left().contains(pos) and self.left().contains(press):
            self.on_close()
        elif self.right().contains(pos) and self.right().contains(press) and self.path:
            try:
                os.startfile(self.path)
            except OSError:
                pass


# ---------------------------------------------------------------- sökfält

class SearchBar(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.setFixedHeight(SEARCH_BAR_H)
        self.target = None
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(4)
        self.edit = QLineEdit(placeholderText=t("Sök i dokumentet…   (Ctrl+F, Enter = nästa)"))
        self.edit.setClearButtonEnabled(True)
        self.count = QLabel()
        self.count.setMinimumWidth(70)
        self.count.setAlignment(Qt.AlignCenter)
        self.count.setStyleSheet("color: gray;")
        self.prev = QToolButton(text="▲", toolTip=t("Föregående träff (Shift+Enter)"), autoRaise=True)
        self.next = QToolButton(text="▼", toolTip=t("Nästa träff (Enter)"), autoRaise=True)
        lay.addWidget(self.edit, 1)
        lay.addWidget(self.count)
        lay.addWidget(self.prev)
        lay.addWidget(self.next)
        self.timer = QTimer(self, singleShot=True, interval=150)
        self.timer.timeout.connect(self._run)
        self.edit.textChanged.connect(lambda _: self.timer.start())
        self.edit.returnPressed.connect(self._enter)
        self.prev.clicked.connect(lambda: self._step(-1))
        self.next.clicked.connect(lambda: self._step(+1))

    def attach(self, target, text=""):
        """Kopplar sökfältet till en vy (DocView/TextView) eller None om sökning inte går."""
        self.target = target if target is not None and target.can_search() else None
        if self.target:
            self.target.hits_changed.connect(self._show_count)
        self.edit.setEnabled(self.target is not None)
        self.prev.setEnabled(self.target is not None)
        self.next.setEnabled(self.target is not None)
        self.edit.blockSignals(True)
        self.edit.setText(text if self.target else "")
        self.edit.blockSignals(False)
        self.edit.setPlaceholderText(t("Sök i dokumentet…   (Ctrl+F, Enter = nästa)") if self.target
                                     else t("Sökning finns för PDF- och textfiler"))
        self.count.clear()
        if self.target and text:
            self._run()

    def _run(self):
        self.timer.stop()
        if self.target:
            self.target.search(self.edit.text())

    def _enter(self):
        if self.timer.isActive():
            self._run()
            return
        mods = QApplication.keyboardModifiers()
        self._step(-1 if mods & Qt.ShiftModifier else +1)

    def _step(self, d):
        if self.target:
            self.target.next_hit(d)

    def _show_count(self, n, i):
        if not self.edit.text().strip():
            self.count.clear()
        elif n == 0 and getattr(self.target, "ocr_pending", 0):
            self.count.setText(t("Läser skannad text…"))
        elif n == 0:
            self.count.setText(t("Inga träffar"))
        else:
            self.count.setText(f"{i + 1} / {n}")


# ---------------------------------------------------------------- dialogen

class PreviewDialog(QDialog):
    _user_center = None

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self.setWindowFlag(Qt.WindowMaximizeButtonHint, True)
        self.setAttribute(Qt.WA_DeleteOnClose)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.search_bar = SearchBar(self)
        lay.addWidget(self.search_bar)
        self.content = None
        self.placed_center = QPoint()
        self.pill = ActionPill(self, self.close)
        QShortcut(QKeySequence.Find, self, self.focus_search)

    def focus_search(self):
        if self.search_bar.edit.isEnabled():
            self.search_bar.edit.setFocus()
            self.search_bar.edit.selectAll()

    # --- placering
    def _place_floating(self):
        visible = self.isVisible() and not self.isMinimized()
        area = QRect(self.mapToGlobal(QPoint(0, 0)), self.size())
        self.pill.move(area.center().x() - self.pill.width() // 2, area.bottom() - 12 - self.pill.height() + 1)
        self.pill.setVisible(visible)
        if visible:
            self.pill.raise_()

    def moveEvent(self, e):
        super().moveEvent(e)
        self._place_floating()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._place_floating()

    def showEvent(self, e):
        super().showEvent(e)
        self._place_floating()

    def hideEvent(self, e):
        self.pill.hide()
        super().hideEvent(e)

    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() == e.Type.WindowStateChange:
            self._place_floating()

    def _size_for_aspect(self, aspect, extra_width):
        """Fönstret får innehållets form och blir så högt som ryms bekvämt."""
        if PreviewDialog._user_center is None:
            c = QSettings(*SETTINGS).value("preview/center")
            PreviewDialog._user_center = c if isinstance(c, QPoint) else QPoint()
        if self.isVisible():
            center = self.geometry().center()
        elif not PreviewDialog._user_center.isNull():
            center = PreviewDialog._user_center
        elif self.parentWidget():
            center = self.parentWidget().frameGeometry().center()
        else:
            center = self.screen().availableGeometry().center()
        scr = QGuiApplication.screenAt(center) or self.screen()
        avail = scr.availableGeometry()
        page_h = int(avail.height() * 0.88) - SEARCH_BAR_H
        w = int(page_h * aspect) + extra_width
        max_w = int(avail.width() * 0.95)
        if w > max_w:
            w = max_w
            page_h = int((w - extra_width) / aspect)
        r = QRect(0, 0, max(w, 420), max(page_h + SEARCH_BAR_H, 240))
        r.moveCenter(center)
        title = 32
        r.moveLeft(min(max(avail.left(), r.left()), avail.right() + 1 - r.width()))
        r.moveTop(min(max(avail.top() + title, r.top()), max(avail.top() + title, avail.bottom() + 1 - r.height())))
        self.setGeometry(r)
        self.placed_center = r.center()

    def _remember_if_moved(self):
        c = self.geometry().center()
        if (c - self.placed_center).manhattanLength() > 8:
            PreviewDialog._user_center = c
            QSettings(*SETTINGS).setValue("preview/center", c)

    # --- innehåll
    def _set_content(self, w):
        if self.content:
            if isinstance(self.content, ShellPreviewHost):
                self.content.unload()
            self.content.close()
            self.content.deleteLater()
        self.content = w
        self.layout().addWidget(w, 1)

    def _text_view(self, path):
        try:
            with open(path, "rb") as f:
                data = f.read(MAX_TEXT_BYTES + 1)
        except OSError:
            return None
        view = TextView(self)
        try:
            text = data[:MAX_TEXT_BYTES].decode("utf-8")
        except UnicodeDecodeError:
            text = data[:MAX_TEXT_BYTES].decode("cp1252", "replace")
        view.setPlainText(text + ("\n…" if len(data) > MAX_TEXT_BYTES else ""))
        return view

    def _message(self, path, text):
        w = QWidget(self)
        lay = QVBoxLayout(w)
        lay.addStretch()
        icon = QLabel(w)
        icon.setAlignment(Qt.AlignCenter)
        icon.setPixmap(QFileIconProvider().icon(QFileInfo(path)).pixmap(96, 96))
        lay.addWidget(icon)
        label = QLabel(text, w)
        label.setAlignment(Qt.AlignCenter)
        label.setWordWrap(True)
        lay.addWidget(label)
        lay.addStretch()
        return w

    def show_file(self, path, query=""):
        """Visar filen. query fylls i sökfältet och markeras direkt (t.ex. ordet man sökte innehåll på)."""
        fi = QFileInfo(path)
        self.pill.path = path
        self.setWindowTitle(fi.fileName())
        if self.isVisible():
            self._remember_if_moved()

        view, use_shell, thumb = None, False, QImage()
        mime = QMimeDatabase().mimeTypeForFile(fi)
        suffix = fi.suffix().lower()
        if fi.isDir():
            view = self._message(path, t("Mapp: {name}", name=fi.fileName()))
            self._size_for_aspect(1.0, 0)
        elif suffix == "pdf":
            d = DocView(self)
            if d.set_pdf(path):
                view = d
                self._size_for_aspect(d.first_page_aspect(), d._bar_extent())
            else:
                d.deleteLater()
        elif mime.name().startswith("image/"):
            reader = QImageReader(path)
            reader.setAutoTransform(True)
            image = reader.read()
            if not image.isNull():
                view = DocView(self)
                view.set_image(image, path)
                self._size_for_aspect(image.width() / max(1, image.height()), 0)
        elif mime.inherits("text/plain"):
            view = self._text_view(path)
            self._size_for_aspect(A4, DOC_SCROLLBAR)

        if view is None:
            thumb = shell_thumbnail(path)
            self._size_for_aspect(thumb.width() / max(1, thumb.height()) if not thumb.isNull() else A4, DOC_SCROLLBAR)
            use_shell = True

        self.show()
        self.raise_()
        self.activateWindow()

        if use_shell:
            host = ShellPreviewHost(self)
            self._set_content(host)
            host.show()
            QApplication.processEvents()
            if host.load(path):
                self.search_bar.attach(None)
                self.setFocus()
                return
            self.content = None
            host.deleteLater()
            if not thumb.isNull():
                view = DocView(self)
                view.set_image(thumb)

        if view is None:
            view = self._message(path, t("Ingen förhandsvisning för {name}", name=fi.fileName()))
            self._size_for_aspect(1.0, 0)
        self._set_content(view)
        self.search_bar.attach(view if isinstance(view, (DocView, TextView)) else None, query)
        view.setFocus()

    def keyPressEvent(self, e):
        k = e.key()
        if k == Qt.Key_Escape and self.search_bar.edit.hasFocus() and self.search_bar.edit.text():
            self.search_bar.edit.clear()
            return
        if k in (Qt.Key_Space, Qt.Key_Escape):
            self.close()
            return
        super().keyPressEvent(e)

    def closeEvent(self, e):
        if self.isVisible():
            self._remember_if_moved()
        if isinstance(self.content, ShellPreviewHost):
            self.content.unload()
        elif isinstance(self.content, DocView):
            self.content.close()
        super().closeEvent(e)
