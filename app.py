"""swpt – snabb sökning i hela datorn (eget index) och i sparade mappflikar, med förhandsvisning."""
import ctypes
import ctypes.wintypes
import gc
import multiprocessing
import os
import queue
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from datetime import datetime

from PySide6.QtCore import (QAbstractTableModel, QByteArray, QEvent, QFileInfo, QItemSelectionModel, QMimeData,
                            QModelIndex, QPointF, QRectF, QSize,
                            QObject, Qt, QTimer, QUrl, Signal)
from PySide6.QtGui import (QBrush, QColor, QFont, QGuiApplication, QIcon, QKeySequence, QPainter, QPen, QPixmap,
                           QShortcut)
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QFileDialog, QFileIconProvider,
                               QDialog, QDialogButtonBox, QStackedWidget, QSystemTrayIcon, QTabBar,
                               QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMainWindow, QMenu,
                               QMessageBox, QPushButton, QStyle, QTableView, QTabWidget, QToolButton,
                               QVBoxLayout, QWidget)

import diskindex
import extract
import ocr
import query as Q
import store
import settings
import updater
from i18n import fmt_dec, fmt_int, t
from preview import PreviewDialog

HERE = os.path.dirname(os.path.abspath(__file__))
HIDDEN_SYSTEM = 0x2 | 0x4
# Filer som OneDrive/SharePoint bara har i molnet (inte nedladdade).
CLOUD_ONLY = 0x1000 | 0x40000 | 0x400000

# Fält i en filpost (tuple för snabb filtrering).
ID, PATH, NAME, IS_DIR, SIZE, MTIME, CLOUD, REL, NAME_L, REL_L = range(10)

RESCAN_AFTER = 15  # sekunder innan en flik söks igenom igen vid byte/fokus
COMPUTER_ID = "__computer__"   # innehållsindex för dokumenten i hela datorn (användarmappen)
SKIP_DOC_DIRS = {"node_modules", "__pycache__", "site-packages", "$recycle.bin", "appdata"}


# ---------------------------------------------------------------- bakgrundsjobb

class Worker(QObject):
    """Läser in mappar och bygger innehållsindex i en bakgrundstråd."""
    status = Signal(str, str)   # flik-id, meddelande
    scanned = Signal(str)       # fillistan har ändrats
    indexed = Signal(str)       # innehållsindexet har ändrats

    def __init__(self):
        super().__init__()
        self._q = queue.Queue()
        self._pending = set()
        self._lock = threading.Lock()
        self._stop = False
        self._pool = None
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, kind, tab_id, path=None):
        with self._lock:
            if (kind, tab_id) in self._pending:
                return
            self._pending.add((kind, tab_id))
        self._q.put((kind, tab_id, path))

    def stop(self):
        self._stop = True
        self._q.put(None)
        if self._pool:
            self._pool.shutdown(wait=False, cancel_futures=True)

    def _run(self):
        diskindex.background_thread()
        self.con = store.connect()
        while True:
            job = self._q.get()
            if job is None or self._stop:
                return
            kind, tab_id, path = job
            with self._lock:
                self._pending.discard((kind, tab_id))
            try:
                getattr(self, "_" + kind)(tab_id, path)
            except Exception as e:  # visa felet i fliken i stället för att tråden dör
                self.con.rollback()
                self.status.emit(tab_id, t("Fel: {e}", e=e))
            if self._pool is not None and self._q.empty():
                self._pool.shutdown(wait=False)   # hjälpprocesserna tar minne – starta nya vid behov
                self._pool = None

    def _scan_docs(self, tab_id, root):
        """Som _scan, men sparar bara dokument som går att söka i (för innehållssök i hela datorn)."""
        self._scan(tab_id, root, docs_only=True)

    def _purge_orphans(self, _, keep):
        """Tar bort fillistor och innehållsindex för flikar som inte finns längre (t.ex. ofästa)."""
        tabs = [r[0] for r in self.con.execute("SELECT DISTINCT tab FROM files")]
        for tab in tabs:
            if tab not in keep:
                self._purge(tab, None)

    def _scan(self, tab_id, root, docs_only=False):
        if not os.path.isdir(root):
            self.status.emit(tab_id, t("Mappen hittades inte – är den synkad/ansluten?"))
            return
        self.status.emit(tab_id, t("Läser in mappen…"))
        con = self.con
        old = {r[1]: r for r in con.execute("SELECT id,path,size,mtime,cloud FROM files WHERE tab=?", (tab_id,))}
        new, changed, seen = [], [], set()
        stack = [root]
        while stack and not self._stop:
            try:
                it = os.scandir(stack.pop())
            except OSError:
                continue
            with it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                        is_dir = e.is_dir(follow_symlinks=False)
                    except OSError:
                        continue
                    attrs = getattr(st, "st_file_attributes", 0)
                    name = e.name
                    if attrs & HIDDEN_SYSTEM or name.startswith("~$"):
                        continue
                    if is_dir:
                        if docs_only and (name.startswith(".") or name.lower() in SKIP_DOC_DIRS):
                            continue
                        if not e.is_junction():
                            stack.append(e.path)
                        if docs_only:
                            continue
                        size, cloud = 0, 0
                    else:
                        if docs_only and not extract.supported(name):
                            continue
                        size, cloud = st.st_size, 1 if attrs & CLOUD_ONLY else 0
                    p, mtime = e.path, st.st_mtime
                    seen.add(p)
                    o = old.get(p)
                    if o is None:
                        new.append((tab_id, p, name, int(is_dir), size, mtime, cloud))
                    elif o[2] != size or o[3] != mtime or o[4] != cloud:
                        changed.append((size, mtime, cloud, o[0]))
        if self._stop:
            return
        gone = [(o[0],) for p, o in old.items() if p not in seen]
        if new or changed or gone:
            with con:
                con.executemany("INSERT INTO files(tab,path,name,is_dir,size,mtime,cloud) VALUES(?,?,?,?,?,?,?)", new)
                con.executemany("UPDATE files SET size=?,mtime=?,cloud=? WHERE id=?", changed)
                con.executemany("DELETE FROM content WHERE rowid=?", gone)
                con.executemany("DELETE FROM files WHERE id=?", gone)
            self.scanned.emit(tab_id)
        self.status.emit(tab_id, "")

    def _index(self, tab_id, _):
        con = self.con
        todo = [(i, p, m) for i, p, m in con.execute(
            "SELECT id,path,mtime FROM files WHERE tab=? AND is_dir=0 AND cloud=0 AND size<=?"
            " AND (cmtime IS NULL OR cmtime<>mtime)", (tab_id, extract.MAX_FILE)) if extract.supported(p)]
        if not todo:
            return
        futs = {self._get_pool().submit(extract.extract, p): (i, m) for i, p, m in todo}
        n = len(todo)
        self.status.emit(tab_id, t("Indexerar innehåll {k}/{n}…", k=0, n=n))
        try:
            for k, f in enumerate(as_completed(futs), 1):
                if self._stop:
                    return
                i, m = futs[f]
                text = f.result()
                con.execute("DELETE FROM content WHERE rowid=?", (i,))
                if text:
                    con.execute("INSERT INTO content(rowid,text) VALUES(?,?)", (i, text))
                con.execute("UPDATE files SET cmtime=? WHERE id=?", (m, i))
                if k % 20 == 0:
                    con.commit()
                    self.status.emit(tab_id, t("Indexerar innehåll {k}/{n}…", k=k, n=n))
                if k % 100 == 0:
                    self.indexed.emit(tab_id)
        except BrokenProcessPool:
            self._pool = None
            con.commit()
            self.status.emit(tab_id, t("Indexeringen avbröts – försöker igen vid nästa uppdatering"))
            return
        con.commit()
        self.status.emit(tab_id, "")
        self.indexed.emit(tab_id)

    def _get_pool(self):
        if self._pool is None:
            self._pool = ProcessPoolExecutor(max_workers=max(1, min(4, (os.cpu_count() or 2) - 1)))
        return self._pool

    def _ocr(self, tab_id, _):
        """Läser skannade PDF:er och bilder med Windows OCR och lägger texten i innehållsindexet."""
        con = self.con
        todo = [(i, p, m) for i, p, m in con.execute(
            "SELECT id,path,mtime FROM files WHERE tab=? AND is_dir=0 AND cloud=0 AND size<=?"
            " AND (omtime IS NULL OR omtime<>mtime)", (tab_id, extract.MAX_FILE)) if ocr.candidate(p)]
        if not todo:
            return
        futs = {self._get_pool().submit(ocr.ocr_file, p): (i, m) for i, p, m in todo}
        n = len(todo)
        self.status.emit(tab_id, t("Läser skannade dokument {k}/{n}…", k=0, n=n))
        try:
            for k, f in enumerate(as_completed(futs), 1):
                if self._stop:
                    return
                i, m = futs[f]
                text = f.result()
                if text:
                    con.execute("DELETE FROM content WHERE rowid=?", (i,))
                    con.execute("INSERT INTO content(rowid,text) VALUES(?,?)", (i, text))
                con.execute("UPDATE files SET omtime=? WHERE id=?", (m, i))
                if k % 5 == 0 or k == n:
                    con.commit()
                    self.status.emit(tab_id, t("Läser skannade dokument {k}/{n}…", k=k, n=n))
                if k % 25 == 0:
                    self.indexed.emit(tab_id)
        except BrokenProcessPool:
            self._pool = None
            con.commit()
            self.status.emit(tab_id, t("Indexeringen avbröts – försöker igen vid nästa uppdatering"))
            return
        con.commit()
        self.status.emit(tab_id, "")
        self.indexed.emit(tab_id)

    def _unindex(self, tab_id, _):
        with self.con as con:
            con.execute("DELETE FROM content WHERE rowid IN (SELECT id FROM files WHERE tab=?)", (tab_id,))
            con.execute("UPDATE files SET cmtime=NULL, omtime=NULL WHERE tab=?", (tab_id,))

    def _purge(self, tab_id, _):
        with self.con as con:
            con.execute("DELETE FROM content WHERE rowid IN (SELECT id FROM files WHERE tab=?)", (tab_id,))
            con.execute("DELETE FROM files WHERE tab=?", (tab_id,))


# ---------------------------------------------------------------- resultatlista

def human_size(n):
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n} B" if unit == "B" else f"{fmt_dec(n)} {unit}"
        n /= 1024


# Qt-konstanter som vanliga tal: data() anropas tusentals gånger per ommålning, och att slå upp
# Qt.DisplayRole osv. i PySide6 vid varje anrop kostar mer än resten av funktionen.
DISPLAY, DECORATION, TOOLTIP, ALIGNMENT, FOREGROUND = (int(r) for r in (
    Qt.DisplayRole, Qt.DecorationRole, Qt.ToolTipRole, Qt.TextAlignmentRole, Qt.ForegroundRole))
USED_ROLES = frozenset((DISPLAY, DECORATION, TOOLTIP, ALIGNMENT, FOREGROUND))
ALIGN_RIGHT = int((Qt.AlignRight | Qt.AlignVCenter).value)
ITEM_FLAGS = Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemNeverHasChildren | Qt.ItemIsDragEnabled


class ResultModel(QAbstractTableModel):
    HEADERS = [t("Namn"), t("Mapp"), t("Träff i innehåll"), t("Ändrad"), t("Storlek")]

    def __init__(self, page):
        super().__init__(page)
        self.page = page
        self.rows = []
        self.hits = {}          # gemen sökväg -> fil-id i innehållsindexet, för filer med träff
        self.snips = {}         # id -> utdrag, hämtas först när raden visas
        self.fts_query = None
        self.sort_col = -1      # -1 = filnamnsträffar först, sedan innehållsträffar efter relevans
        self.sort_order = Qt.AscendingOrder
        self.icons = QFileIconProvider()
        self.icon_cache = {}
        self.folder_icon = self.icons.icon(QFileIconProvider.Folder)
        self.grey = QBrush(QColor(128, 128, 128))

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.HEADERS[section]

    def _snippet(self, fid):
        s = self.snips.get(fid)
        if s is None:
            s = self.page.win.snippet(self.fts_query, fid)
            self.snips[fid] = s
        return s

    def data(self, index, role=DISPLAY):
        if role not in USED_ROLES:
            return None
        r = self.rows[index.row()]
        c = index.column()
        if role == DISPLAY:
            if c == 0:
                return r[NAME]
            if c == 1:
                return r[REL]
            if c == 2:
                fid = self.hits.get(r[PATH].lower()) if self.hits else None
                return self._snippet(fid) if fid is not None else ""
            if c == 3:
                return datetime.fromtimestamp(r[MTIME]).strftime("%Y-%m-%d %H:%M") if r[MTIME] > 0 else ""
            if c == 4:
                return "" if r[IS_DIR] or r[SIZE] < 0 else human_size(r[SIZE])
        elif role == DECORATION and c == 0:
            return self._icon(r)
        elif role == TOOLTIP:
            if c == 2 and self.hits and r[PATH].lower() in self.hits:
                return self._snippet(self.hits[r[PATH].lower()])
            return r[PATH] + ("\n" + t("Finns bara i molnet – söks inte i innehållet") if r[CLOUD] else "")
        elif role == FOREGROUND and r[CLOUD]:
            return self.grey
        elif role == ALIGNMENT and c == 4:
            return ALIGN_RIGHT

    def _icon(self, r):
        if r[IS_DIR]:
            return self.folder_icon
        ext = os.path.splitext(r[NAME])[1].lower()
        icon = self.icon_cache.get(ext)
        if icon is None:
            icon = self.icon_cache[ext] = self.icons.icon(QFileInfo(r[PATH]))
        return icon

    def flags(self, index):
        return ITEM_FLAGS

    def mimeTypes(self):
        return ["text/uri-list"]

    def mimeData(self, indexes):
        rows = sorted({i.row() for i in indexes})
        md = QMimeData()
        md.setUrls([QUrl.fromLocalFile(self.rows[r][PATH]) for r in rows])
        return md

    def canFetchMore(self, parent):
        return not parent.isValid() and self.page.can_fetch_more()

    def fetchMore(self, parent):
        self.page.fetch_more()

    def set_results(self, rows, hits=frozenset(), fts_query=None, sort=True):
        self.beginResetModel()
        self.rows, self.hits, self.fts_query = rows, hits or {}, fts_query
        self.snips = {}
        if sort:
            self._sort()
        self.endResetModel()

    def grow(self, n):
        """Visar fler rader ur en LazyRows som har fyllts på."""
        old = len(self.rows)
        if n > old:
            self.beginInsertRows(QModelIndex(), old, n - 1)
            self.rows.n = n
            self.endInsertRows()

    def append_rows(self, rows):
        if rows:
            self.beginInsertRows(QModelIndex(), len(self.rows), len(self.rows) + len(rows) - 1)
            self.rows.extend(rows)
            self.endInsertRows()

    def sort(self, column, order=Qt.AscendingOrder):
        self.sort_col, self.sort_order = column, order
        self.layoutAboutToBeChanged.emit()
        self._sort()
        self.layoutChanged.emit()

    def _sort(self):
        keys = {
            0: lambda r: (not r[IS_DIR], r[NAME_L]),
            1: lambda r: (r[REL_L], r[NAME_L]),
            2: lambda r: r[PATH].lower() not in self.hits,
            3: lambda r: r[MTIME],
            4: lambda r: r[SIZE],
        }
        if self.sort_col in keys:
            self.rows.sort(key=keys[self.sort_col], reverse=self.sort_order == Qt.DescendingOrder)


class BasePage(QWidget):
    """Det som är gemensamt för alla flikar: sökruta, resultatlista och förhandsvisning."""
    debounce_ms = 60

    def __init__(self, win, placeholder):
        super().__init__()
        self.win = win
        self.shown = 0
        self.preview = None

        self.edit = QLineEdit(placeholderText=placeholder)
        self.edit.setClearButtonEnabled(True)
        self.edit.setMinimumHeight(32)
        f = self.edit.font()
        f.setPointSizeF(f.pointSizeF() + 1.5)
        self.edit.setFont(f)
        self.cb = QCheckBox(t("Sök i filinnehåll"))
        self.top = QHBoxLayout()
        self.top.addWidget(self.edit, 1)
        self.top.addWidget(self.cb)

        self.model = ResultModel(self)
        v = self.view = QTableView()
        v.setModel(self.model)
        v.setSelectionBehavior(QAbstractItemView.SelectRows)
        v.setShowGrid(False)
        v.setWordWrap(False)
        v.setAlternatingRowColors(True)
        v.setDragEnabled(True)
        v.setDragDropMode(QAbstractItemView.DragOnly)
        v.verticalHeader().hide()
        v.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)
        v.verticalHeader().setDefaultSectionSize(24)
        h = v.horizontalHeader()
        h.setSectionsClickable(True)
        h.setSortIndicatorShown(True)
        h.setHighlightSections(False)
        h.sectionClicked.connect(self.on_header)
        # Alla kolumner går att dra i. Mapp (eller Träff i innehåll) fyller ut fönstret
        # tills man själv ändrar en bredd; därefter sparas bredderna.
        h.setStretchLastSection(False)
        h.setSectionResizeMode(QHeaderView.Interactive)
        h.setMinimumSectionSize(40)
        for col, width in enumerate((340, 260, 320, 125, 80)):
            v.setColumnWidth(col, width)
        self._fitting = False
        self._user_sized = False
        h.sectionResized.connect(self._on_section_resized)
        v.setContextMenuPolicy(Qt.CustomContextMenu)
        v.customContextMenuRequested.connect(self.result_menu)
        v.doubleClicked.connect(lambda _: self.open_selected())

        self.info = QLabel()
        self.info.setStyleSheet("color: gray; padding: 2px 4px;")
        self.bottom = QHBoxLayout()
        self.bottom.addWidget(self.info, 1)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 2)
        lay.addLayout(self.top)
        lay.addWidget(v, 1)
        lay.addLayout(self.bottom)

        self.timer = QTimer(self, singleShot=True, interval=self.debounce_ms)
        self.timer.timeout.connect(self.search)
        self.edit.textChanged.connect(lambda _: self.timer.start())
        self.edit.installEventFilter(self)
        v.installEventFilter(self)
        v.viewport().installEventFilter(self)

    # --- att implementera i flikerna
    def search(self):
        raise NotImplementedError

    def sort_by(self, col, order):
        self.model.sort(col, order)

    def can_fetch_more(self):
        return False

    def fetch_more(self):
        pass

    # --- gemensamt
    columns_key = None  # nyckel för sparade kolumnbredder

    def _layout_columns(self):
        """Visar innehållskolumnen bara när innehållssökning är på."""
        self._fitting = True
        self.view.setColumnHidden(2, not (self.cb.isChecked() and not self.cb.isHidden()))
        self._fitting = False
        self._fit_columns()

    def _fit_columns(self):
        """Låter Mapp-kolumnen (eller innehållskolumnen) fylla ut bredden – tills man själv har ändrat bredder."""
        if self._user_sized:
            return
        v = self.view
        filler = 1 if v.isColumnHidden(2) else 2
        others = sum(v.columnWidth(c) for c in range(self.model.columnCount())
                     if c != filler and not v.isColumnHidden(c))
        self._fitting = True
        v.setColumnWidth(filler, max(160, v.viewport().width() - others))
        self._fitting = False

    def _on_section_resized(self, *_):
        if self._fitting:
            return
        self._user_sized = True
        if self.columns_key:
            cols = self.win.cfg.setdefault("columns", {})
            cols[self.columns_key] = [self.view.columnWidth(c) for c in range(self.model.columnCount())]

    def _restore_columns(self):
        widths = self.win.cfg.get("columns", {}).get(self.columns_key)
        if widths and len(widths) == self.model.columnCount():
            self._fitting = True
            for c, w in enumerate(widths):
                self.view.setColumnWidth(c, int(w) if w >= 40 else (320, 260, 320, 125, 80)[c])
            self._fitting = False
            self._user_sized = True


    def on_header(self, col):
        m = self.model
        order = Qt.DescendingOrder if m.sort_col == col and m.sort_order == Qt.AscendingOrder else Qt.AscendingOrder
        self.sort_by(col, order)
        self.view.horizontalHeader().setSortIndicator(col, order)

    def selected_rows(self):
        return sorted({i.row() for i in self.view.selectionModel().selectedRows()})

    def selected_paths(self):
        return [self.model.rows[r][PATH] for r in self.selected_rows()]

    def open_selected(self):
        for p in self.selected_paths()[:10]:
            try:
                os.startfile(p)
            except OSError as e:
                QMessageBox.warning(self, t("Kunde inte öppna"), f"{p}\n\n{e}")

    def reveal_selected(self):
        paths = self.selected_paths()
        if paths:
            subprocess.Popen(f'explorer /select,"{paths[0]}"')

    def result_menu(self, pos):
        paths = self.selected_paths()
        if not paths:
            return
        menu = QMenu(self)
        menu.addAction(t("Förhandsvisa\tMellanslag"), self.preview_current)
        menu.addAction(t("Öppna\tEnter"), self.open_selected)
        menu.addAction(t("Visa i Utforskaren"), self.reveal_selected)
        menu.addSeparator()
        menu.addAction(t("Kopiera sökväg"), lambda: QGuiApplication.clipboard().setText("\r\n".join(paths)))
        menu.addAction(t("Kopiera filnamn"),
                       lambda: QGuiApplication.clipboard().setText("\r\n".join(os.path.basename(p) for p in paths)))
        menu.exec(self.view.viewport().mapToGlobal(pos))

    def keeping_selection(self, fn):
        """Kör fn (en uppdatering av listan i bakgrunden) utan att markering och scroll försvinner."""
        sm = self.view.selectionModel()
        rows = self.model.rows
        paths = {rows[i.row()][PATH] for i in sm.selectedRows() if i.row() < len(rows)}
        cur = self.view.currentIndex()
        cur_path = rows[cur.row()][PATH] if cur.isValid() and cur.row() < len(rows) else None
        scroll = self.view.verticalScrollBar().value()
        fn()
        if paths or cur_path:
            rows = self.model.rows
            for r in range(min(len(rows), 5000)):
                p = rows[r][PATH]
                if p in paths:
                    sm.select(self.model.index(r, 0), QItemSelectionModel.Select | QItemSelectionModel.Rows)
                if p == cur_path:
                    sm.setCurrentIndex(self.model.index(r, 0), QItemSelectionModel.NoUpdate)
        self.view.verticalScrollBar().setValue(scroll)

    # --- förhandsvisning (mellanslag)
    def preview_current(self):
        row = self.view.currentIndex().row()
        if row < 0 and self.model.rows:
            row = 0
        self.preview_row(row)

    def preview_query(self, rec):
        """Ordet som ska sökas fram i förhandsvisningen: det längsta sökordet, om filen träffades på innehåll."""
        if rec[PATH].lower() not in self.model.hits:
            return ""
        terms = [(t, ex) for t, ex in Q.parse(self.edit.text()) if len(t) >= 3 and "\\" not in t and "/" not in t]
        if not terms:
            return ""
        term, exact = max(terms, key=lambda x: len(x[0]))
        return f'"{term}"' if exact else term

    def preview_row(self, row):
        rows = self.model.rows
        if not 0 <= row < len(rows):
            return
        if self.preview is None:
            self.preview = PreviewDialog(self.win)
            self.preview.destroyed.connect(self._preview_closed)
        self.view.selectRow(row)
        self.view.scrollTo(self.model.index(row, 0))
        self.preview.show_file(rows[row][PATH], self.preview_query(rows[row]))

    def _preview_closed(self):
        self.preview = None
        self.win.activateWindow()
        self.view.setFocus()

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Resize and obj is self.view.viewport():
            self._fit_columns()
            return False
        if ev.type() == QEvent.KeyPress:
            key = ev.key()
            if obj is self.edit and key in (Qt.Key_Down, Qt.Key_Return, Qt.Key_Enter) and self.model.rows:
                self.view.setFocus()
                self.view.selectRow(0)
                return True
            if obj is self.view:
                if key == Qt.Key_Space:
                    self.preview_current()
                    return True
                if key in (Qt.Key_Return, Qt.Key_Enter):
                    self.open_selected()
                    return True
                if key == Qt.Key_Up and self.view.currentIndex().row() <= 0:
                    self.edit.setFocus()
                    return True
                if ev.text() and ev.text().isprintable() and not ev.modifiers() & (Qt.ControlModifier | Qt.AltModifier):
                    self.edit.setFocus()
                    self.edit.insert(ev.text())
                    return True
            if key == Qt.Key_Escape and self.edit.text():
                self.edit.clear()
                self.edit.setFocus()
                return True
        return super().eventFilter(obj, ev)


class LazyRows:
    """Resultatrader som skapas först när listan visar dem (sökningar kan ge miljontals träffar)."""

    def __init__(self, query):
        self.q = query
        self.n = len(query.matches)
        self.cache = {}

    def __len__(self):
        return self.n

    def __getitem__(self, k):
        r = self.cache.get(k)
        if r is None:
            if len(self.cache) > 5000:
                self.cache.clear()
            r = self.cache[k] = self.q.row(self.q.matches[k])
        return r


class CombinedRows:
    """Innehållsträffar (först) följda av namnträffarna i en LazyRows som kan växa."""

    def __init__(self, extra, lazy):
        self.extra, self.lazy = extra, lazy

    @property
    def n(self):
        return len(self.extra) + (self.lazy.n if self.lazy else 0)

    @n.setter
    def n(self, value):
        if self.lazy:
            self.lazy.n = value - len(self.extra)

    def __len__(self):
        return self.n

    def __getitem__(self, k):
        e = len(self.extra)
        return self.extra[k] if k < e else self.lazy[k - e]


class DiskManager(QObject):
    """Sköter indexet över hela datorn: bygger det (i egen process), laddar det och håller det
    uppdaterat med Windows ändringsnotiser. Bygger om i bakgrunden när det blivit gammalt."""
    changed = Signal(bool)          # True = ett nytt index har laddats
    _loaded = Signal(object, str)
    REBUILD_AFTER = 6 * 3600        # sekunder: indexet byggs om i bakgrunden vid start om det är äldre
    REBUILD_EVERY = 24 * 3600 * 1000  # ändringar fångas live, så en full genomgång per dygn räcker
    MAX_EXTRA = 50_000              # så många tillagda filer i minnet – sedan byggs indexet om

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.index = None
        self.status = ""
        self.watcher = None
        self.proc = self.progress = None
        self.building = self.quiet = False
        self.capturing = False      # samla ändringar tills nästa index är laddat
        self.captured = []
        self.dir = str(store.APP_DIR)
        self._loaded.connect(self._on_loaded)
        self.poll = QTimer(self, interval=250)
        self.poll.timeout.connect(self._poll_build)
        self.drain = QTimer(self, interval=1000)
        self.drain.timeout.connect(self._drain)
        self.periodic = QTimer(self, interval=self.REBUILD_EVERY)
        self.periodic.timeout.connect(lambda: self.rebuild(quiet=True))

    def enabled(self):
        return self.win.cfg.get("disk_index") == "yes"

    def start(self):
        if not self.enabled():
            self.changed.emit(False)
            return
        self._start_watcher()
        self.periodic.start()
        diskindex.cleanup(self.dir, keep=diskindex.latest(self.dir) or os.path.join(self.dir, "diskindex.bin"))
        if diskindex.latest(self.dir) or os.path.exists(os.path.join(self.dir, "diskindex.bin")):
            self.status = t("Laddar index…")
            self._load()
        else:
            self.rebuild()
        self.changed.emit(False)

    def _start_watcher(self):
        if self.watcher is None:
            self.watcher = diskindex.Watcher(diskindex.fixed_drives(), [str(store.APP_DIR)])
            self.drain.start()

    def _load(self):
        self.capturing = True

        def run():
            try:
                path = diskindex.latest(self.dir) or diskindex.convert_legacy(self.dir)
                if path is None:
                    raise FileNotFoundError("inget index")
                self._loaded.emit(diskindex.DiskIndex.load(path), "")
            except Exception as e:
                self._loaded.emit(None, str(e))
        threading.Thread(target=run, daemon=True).start()

    def _on_loaded(self, ix, err):
        if ix is None:
            self.capturing = False
            self.status = ""
            self.rebuild()
            return
        for d in self._apply(ix, self.captured):
            ix.add_tree(d)
        self.captured, self.capturing = [], self.building
        self.index = ix
        self.status = t("Uppdaterar indexet i bakgrunden…") if self.building else ""
        self.changed.emit(True)
        # det gamla indexet släpps när sökningarna har bytts ut – sedan kan dess fil tas bort
        QTimer.singleShot(10_000, lambda: self.index is not None and diskindex.cleanup(self.dir, keep=self.index.file))
        if not self.building and time.time() - ix.created > self.REBUILD_AFTER:
            self.rebuild(quiet=True)

    def rebuild(self, quiet=False):
        if self.building or not self.enabled():
            return
        ctx = multiprocessing.get_context("spawn")
        self.progress = ctx.Queue()
        self.proc = ctx.Process(target=diskindex.build_index,
                                args=(diskindex.new_path(self.dir), self.progress, [str(store.APP_DIR)]),
                                daemon=True)
        self.proc.start()
        self.building, self.quiet = True, quiet and self.index is not None
        self.capturing, self.captured = True, []
        self.status = t("Uppdaterar indexet i bakgrunden…") if self.quiet else t("Indexerar datorn…")
        self._start_watcher()
        self.poll.start()
        self.changed.emit(False)

    def _poll_build(self):
        try:
            while True:
                kind, n = self.progress.get_nowait()
                if kind == "progress" and not self.quiet:
                    self.status = t("Indexerar datorn… {n} filer hittills", n=fmt_int(n))
                elif kind == "sorting" and not self.quiet:
                    self.status = t("Sorterar {n} filer…", n=fmt_int(n))
                elif kind == "done":
                    self.building = False
                    self.poll.stop()
                    if not self.quiet:
                        self.status = t("Laddar index…")
                    self._load()
        except queue.Empty:
            pass
        if self.building and not self.proc.is_alive():
            self.building = self.capturing = False
            self.poll.stop()
            self.status = t("Indexeringen misslyckades – högerklicka på fliken för att försöka igen")
        self.changed.emit(False)

    def _apply(self, ix, events):
        rescans, overflow = [], False
        for ev in events:   # (åtgärd, sökväg, info) eller ("overflow", enhet)
            if ev[0] == "overflow":
                overflow = True
            else:
                rescans += ix.apply(*ev)
        if overflow:
            QTimer.singleShot(0, lambda: self.rebuild(quiet=True))
        return rescans[:50]

    def _drain(self):
        events = []
        try:
            while len(events) < 20000:
                events.append(self.watcher.events.get_nowait())
        except queue.Empty:
            pass
        if not events:
            return
        if self.capturing:
            self.captured.extend(events)
        if self.index is not None:
            for d in self._apply(self.index, events):
                self.index.add_tree(d)
            if len(self.index.extra) > self.MAX_EXTRA:
                self.rebuild(quiet=True)
            self.changed.emit(False)

    def disable(self):
        self.stop()
        self.watcher = None
        self.index = None
        self.status = ""
        self.win.cfg["disk_index"] = "no"
        self.changed.emit(True)
        QTimer.singleShot(2000, lambda: diskindex.cleanup(self.dir))

    def stop(self):
        self.drain.stop()
        self.poll.stop()
        self.periodic.stop()
        if self.watcher:
            self.watcher.stop()
        if self.proc is not None and self.proc.is_alive():
            self.proc.terminate()
        self.building = False


class ComputerPage(BasePage):
    """Standardfliken: hela datorn, med swpts eget index."""
    debounce_ms = 30
    columns_key = "computer"
    SORTS = {0: diskindex.SORT_NAME, 1: diskindex.SORT_PATH, 3: diskindex.SORT_MODIFIED, 4: diskindex.SORT_SIZE}

    def __init__(self, win):
        super().__init__(win, t("Sök i hela datorn…   (t.ex.  offert *.pdf   ·   ext:docx standard   ·   projekt\\ ritning)"))
        self.cb.setChecked(win.cfg.get("computer_content", {}).get("checked", False))
        self.cb.setToolTip(t("Söker i texten i dina dokument (PDF, Word, Excel, PowerPoint, text) i användarmappen"))
        self.cb.toggled.connect(self.on_content_toggled)
        self.extra, self.worker_msg = [], ""
        self.disk = win.disk
        self.q = None
        self.sort = (diskindex.SORT_NAME, False)
        self.seen_version = -1
        self.step_timer = QTimer(self, singleShot=True, interval=0)
        self.step_timer.timeout.connect(self._step)
        self.live_timer = QTimer(self, singleShot=True, interval=1500)
        self.live_timer.timeout.connect(self._live_refresh)
        self.start_btn = QPushButton(t("Indexera datorn"))
        self.start_btn.clicked.connect(win.enable_disk_index)
        self.start_btn.hide()
        self.bottom.addWidget(self.start_btn)
        self.disk.changed.connect(self.on_disk_changed)
        self._restore_columns()
        self._layout_columns()
        self.view.horizontalHeader().setSortIndicator(0, Qt.AscendingOrder)
        self.model.sort_col = 0

    def _incremental(self):
        return self.sort == (diskindex.SORT_NAME, False)

    def search(self):
        self.step_timer.stop()
        ix = self.disk.index
        if ix is None:
            self.q = None
            self.model.set_results([], sort=False)
            self.update_info()
            return
        self.q = ix.query(self.edit.text())
        self.q_started = time.perf_counter()
        self.seen_version = ix.version
        self._content_search()
        self.q.step(0.006)
        if self.q.done:
            self._finish(reset=True)
        else:
            lazy = LazyRows(self.q) if self._incremental() else None
            self.model.set_results(CombinedRows(self.extra, lazy), self.hits, self.fts, sort=False)
            self.step_timer.start()
        self.view.scrollToTop()
        self.update_info()

    def _step(self):
        q = self.q
        if q is None:
            return
        q.step(0.006)
        if q.done:
            self._finish(reset=bool(q._extras) or not self._incremental())
        else:
            if self._incremental():
                self.model.grow(len(self.extra) + len(q.matches))
            self.step_timer.start()
        self.update_info()

    def _finish(self, reset):
        # Liveuppdateringar högst var 10:e sökningstid, så att de tar högst ~10 % av en kärna.
        self.live_timer.setInterval(min(30_000, max(1500, int((time.perf_counter() - self.q_started) * 10_000))))
        field, desc = self.sort
        if not self._incremental():
            self.q.sort(field, desc)
        if reset:
            rows = CombinedRows(self.extra, LazyRows(self.q))
            self.keeping_selection(lambda: self.model.set_results(rows, self.hits, self.fts, sort=False))
        else:
            self.model.grow(len(self.extra) + len(self.q.matches))

    def _content_search(self):
        """Innehållsträffar bland dokumenten i användarmappen (om kryssrutan är på)."""
        q = self.q
        self.extra, self.hits, self.fts = [], {}, None
        terms = [x for x in q.name_terms if len(x) >= 3]
        if not (self.cb.isChecked() and terms):
            return
        self.fts = " ".join('"' + x.replace('"', '""') + '"' for x in terms)
        pats = [Q.word_pattern(x) for x in terms if x in q.exact_terms]
        for fid, path, name, size, mtime in self.win.content_rows(COMPUTER_ID, self.fts, pats):
            self.hits[path.lower()] = fid
            folder = path[:len(path) - len(name) - 1]
            name_l, folder_l = name.lower(), folder.lower()
            if q.ok_str(name_l, folder_l):
                continue   # finns redan bland namnträffarna
            if q.exts and not name_l.endswith(tuple(q.exts)):
                continue
            if q.path_terms and any(x not in folder_l + "\\" + name_l for x in q.path_terms):
                continue
            self.extra.append((-(fid + 1), path, name, False, size, mtime, 0, folder, "", ""))

    def on_content_toggled(self, checked):
        cc = self.win.cfg.setdefault("computer_content", {})
        if checked and not cc.get("indexed"):
            if not self.win.confirm_computer_content():
                self.cb.blockSignals(True)
                self.cb.setChecked(False)
                self.cb.blockSignals(False)
                return
            cc["indexed"] = True
            self.win.refresh_computer_content()
        cc["checked"] = checked
        self._layout_columns()
        self.win.save()
        self.search()

    def set_worker_msg(self, msg):
        self.worker_msg = msg
        self.update_info()

    def sort_by(self, col, order):
        field = self.SORTS.get(col)
        if field is None:
            return
        self.model.sort_col, self.model.sort_order = col, order
        self.sort = (field, order == Qt.DescendingOrder)
        self.search()

    def update_info(self):
        d = self.disk
        if d.index is None:
            if d.status:
                self.info.setText(d.status)
            elif not d.enabled():
                self.info.setText(t("Hela datorn är inte indexerad – klicka på ”Indexera datorn” för att kunna söka bland alla filer."))
            else:
                self.info.setText("")
            self.start_btn.setVisible(not d.enabled())
            return
        self.start_btn.hide()
        n = len(self.extra) + (len(self.q.matches) if self.q else 0)
        parts = [t("{n} objekt", n=fmt_int(n)) + ("" if self.q is None or self.q.done else t(" … söker"))]
        if self.cb.isChecked() and self.edit.text().strip() and not any(len(x) >= 3 for x in self.edit.text().split()):
            parts.append(t("innehållssökning kräver minst 3 tecken"))
        if d.status:
            parts.append(d.status)
        if self.worker_msg:
            parts.append(self.worker_msg)
        self.info.setText("   ·   ".join(parts))

    def _can_live_refresh(self):
        """Listan uppdateras bara när den syns och man inte håller på med den."""
        win = self.win
        return win.isVisible() and not win.isMinimized() and win.current() is self and self.preview is None \
            and self.view.verticalScrollBar().value() == 0 and not self.view.selectionModel().hasSelection()

    def _live_refresh(self):
        """Visar ändringar på datorn i listan – men aldrig mitt i att man har markerat något."""
        ix = self.disk.index
        if ix is None or ix.version == self.seen_version or not self._can_live_refresh():
            self.update_info()
            return
        self.keeping_selection(self.search)

    def on_disk_changed(self, reloaded):
        ix = self.disk.index
        if ix is None or self.q is None:
            self.search()
            return
        if reloaded:   # nytt index (sällan): sök om direkt, så att det gamla kan släppas ur minnet
            self.keeping_selection(self.search)
            return
        # Hela sökningen görs om, så högst en gång per intervall (timern startas inte om) och
        # aldrig när fönstret är dolt – då görs den i stället när fönstret visas igen.
        if ix.version != self.seen_version and self._can_live_refresh():
            if not self.live_timer.isActive():
                self.live_timer.start()
        else:
            self.update_info()


class FolderPage(BasePage):
    """En sparad flik för en mapp, med egen fillista och innehållsindex."""
    columns_key = "folder"

    def __init__(self, win, cfg):
        super().__init__(win, t("Sök i {name}…   (flera ord = alla måste finnas)", name=cfg["name"]))
        self.cfg = cfg
        self.recs, self.by_id = [], {}
        self.worker_msg = t("Läser in…")
        self.last_scan = 0.0
        self.view.horizontalHeader().setSortIndicator(-1, Qt.AscendingOrder)
        self.cb.setChecked(cfg.get("content", False))
        self.cb.toggled.connect(self.on_content_toggled)
        self._restore_columns()
        self._layout_columns()

    @property
    def tab_id(self):
        return self.cfg["id"]

    def reload(self):
        """Läser flikens fillista från databasen till minnet."""
        root = os.path.normpath(self.cfg["path"])
        cut = len(root.rstrip("\\/")) + 1
        recs = []
        for fid, path, name, is_dir, size, mtime, cloud in self.win.con.execute(
                "SELECT id,path,name,is_dir,size,mtime,cloud FROM files WHERE tab=?", (self.tab_id,)):
            rel = path[cut:len(path) - len(name)].rstrip("\\")
            recs.append((fid, path, name, is_dir, size, mtime, cloud, rel, name.lower(), (rel + "\\" + name).lower()))
        recs.sort(key=lambda r: r[NAME_L])
        self.recs = recs
        self.by_id = {r[ID]: r for r in recs}
        self.keeping_selection(self.search)

    def search(self):
        parsed = Q.parse(self.edit.text())
        path_terms = [t.replace("/", "\\") for t, _ in parsed if "\\" in t or "/" in t]
        name_terms = [t for t, _ in parsed if "\\" not in t and "/" not in t]
        exact = [t for t, ex in parsed if ex and t in name_terms]
        rows = self.recs
        if name_terms:
            if len(name_terms) == 1:
                term = name_terms[0]
                rows = [r for r in rows if term in r[NAME_L]]
            else:
                rows = [r for r in rows if all(t in r[NAME_L] for t in name_terms)]
        if exact:
            res = [Q.word_re(t) for t in exact]
            rows = [r for r in rows if all(rx.search(r[NAME_L]) for rx in res)]
        if path_terms:
            rows = [r for r in rows if all(t in r[REL_L] for t in path_terms)]

        hits, fts_query = {}, None
        content_terms = [t for t in name_terms if len(t) >= 3]
        if self.cb.isChecked() and content_terms:
            fts_query = " ".join('"' + t.replace('"', '""') + '"' for t in content_terms)
            ids = self.win.content_hits(self.tab_id, fts_query, [Q.word_pattern(t) for t in exact if len(t) >= 3])
            hits = {self.by_id[i][PATH].lower(): i for i in ids if i in self.by_id}
            have = {r[ID] for r in rows}
            extra = [self.by_id[i] for i in ids if i not in have and i in self.by_id]
            if path_terms:
                extra = [r for r in extra if all(t in r[REL_L] for t in path_terms)]
            rows = rows + extra
        elif rows is self.recs:
            rows = list(rows)
        self.shown = len(rows)
        self.model.set_results(rows, hits, fts_query)
        self.update_info()

    def update_info(self):
        parts = [t("{n} av {total} objekt", n=fmt_int(self.shown), total=fmt_int(len(self.recs)))]
        if self.cb.isChecked():
            cloud = sum(1 for r in self.recs if r[CLOUD] and extract.supported(r[NAME]))
            if cloud:
                parts.append(t("{n} filer finns bara i molnet och söks inte i innehållet", n=cloud))
            if self.edit.text().strip() and not any(len(t) >= 3 for t in self.edit.text().split()):
                parts.append(t("innehållssökning kräver minst 3 tecken"))
        if self.cfg.get("ocr"):
            parts.append(t("OCR på"))
        if self.worker_msg:
            parts.append(self.worker_msg)
        self.info.setText("   ·   ".join(parts))

    def set_worker_msg(self, msg):
        self.worker_msg = msg
        self.update_info()

    def on_content_toggled(self, checked):
        self.cfg["content"] = checked
        if checked and not self.cfg.get("indexed"):
            self.cfg["indexed"] = True
            self.win.worker.submit("index", self.tab_id)
        self._layout_columns()
        self.win.save()
        self.search()


# ---------------------------------------------------------------- uppdateringar

class Updater(QObject):
    """Söker efter nya versioner och laddar ner dem i bakgrunden (se updater.py)."""
    found = Signal(object)          # info om en nyare version, eller None
    progress = Signal(float)        # 0–1
    downloaded = Signal(str)        # sökväg till MSI:n
    failed = Signal(str)

    def check(self):
        def run():
            try:
                self.found.emit(updater.check())
            except Exception as e:
                self.failed.emit(str(e))
        threading.Thread(target=run, daemon=True).start()

    def download(self, info):
        def run():
            try:
                self.downloaded.emit(updater.download(info, self.progress.emit))
            except Exception as e:
                self.failed.emit(str(e))
        threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------- huvudfönster

def _wordmatch(text, pattern):
    """SQL-funktion: finns mönstret (ett exakt ord) i texten?"""
    return 1 if re.search(pattern, text or "", re.IGNORECASE) else 0


class TabHost(QWidget):
    """Flikar med en egen rubrikrad:  [logga][flikar] ……… [⚙][+ Ny flik].
    Ersätter QTabWidget så att knapparna får rätt höjd och linjerar med innehållet."""
    currentChanged = Signal(int)

    def __init__(self, left, right):
        super().__init__()
        self.bar = QTabBar()
        self.bar.setDocumentMode(True)
        self.bar.setDrawBase(False)
        self.bar.setExpanding(False)
        self.bar.setMovable(True)
        self.stack = QStackedWidget()
        head = QHBoxLayout()
        head.setContentsMargins(0, 4, 0, 2)
        head.setSpacing(0)
        head.addWidget(left)
        head.addWidget(self.bar, 0, Qt.AlignVCenter)
        head.addStretch(1)
        head.addWidget(right)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addLayout(head)
        lay.addWidget(self.stack, 1)
        self.bar.currentChanged.connect(self._on_current)
        self.bar.tabMoved.connect(self._on_moved)

    def _on_current(self, i):
        if 0 <= i < self.stack.count():
            self.stack.setCurrentIndex(i)
        self.currentChanged.emit(i)

    def _on_moved(self, frm, to):
        w = self.stack.widget(frm)
        self.stack.removeWidget(w)
        self.stack.insertWidget(to, w)
        self.stack.setCurrentIndex(self.bar.currentIndex())

    def addTab(self, w, *args):
        self.stack.addWidget(w)
        return self.bar.addTab(*args)

    def removeTab(self, i):
        self.stack.removeWidget(self.stack.widget(i))
        self.bar.removeTab(i)

    def tabBar(self):
        return self.bar

    def widget(self, i):
        return self.stack.widget(i)

    def count(self):
        return self.bar.count()

    def currentWidget(self):
        return self.stack.currentWidget()

    def currentIndex(self):
        return self.bar.currentIndex()

    def setCurrentIndex(self, i):
        self.bar.setCurrentIndex(i)

    def setCurrentWidget(self, w):
        self.bar.setCurrentIndex(self.stack.indexOf(w))

    def indexOf(self, w):
        return self.stack.indexOf(w)

    def setTabText(self, i, text):
        self.bar.setTabText(i, text)

    def setTabToolTip(self, i, text):
        self.bar.setTabToolTip(i, text)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("swpt")
        self.setWindowIcon(app_icon())
        self.cfg = store.load_config()
        self.con = store.connect()
        self.con.create_function("wordmatch", 2, _wordmatch, deterministic=True)
        self.pages = {}

        self.worker = Worker()
        self.worker.status.connect(self.on_status)
        self.worker.scanned.connect(lambda tid: self.pages.get(tid) and self.pages[tid].reload())
        for tab in self.cfg["tabs"]:
            tab.setdefault("pinned", True)   # flikar från tidigare versioner var alltid sparade
        self.worker.indexed.connect(self.on_indexed)
        self.disk = DiskManager(self)

        corner = corner_buttons(self.add_tab, self.open_settings)
        self.update_btn = QToolButton(objectName="update")
        self.update_btn.setCursor(Qt.PointingHandCursor)
        self.update_btn.clicked.connect(self.start_update)
        self.update_btn.hide()
        corner.layout().insertWidget(0, self.update_btn)
        self.tabs = TabHost(brand_label(), corner)
        bar = self.tabs.tabBar()
        bar.setContextMenuPolicy(Qt.CustomContextMenu)
        bar.customContextMenuRequested.connect(self.tab_menu)
        bar.tabMoved.connect(lambda *_: self.save())
        self.setCentralWidget(self.tabs)

        self.computer = ComputerPage(self)
        self.tabs.addTab(self.computer, QIcon(self.style().standardIcon(QStyle.SP_ComputerIcon)), t("Hela datorn"))
        self.tabs.setTabToolTip(0, t("Söker bland alla filer på datorns lokala diskar"))
        for tab in self.cfg["tabs"]:
            self._add_page(tab)
        if g := self.cfg.get("geometry"):
            self.restoreGeometry(QByteArray.fromHex(g.encode()))
        else:
            self.resize(1150, 720)
        self.tabs.setCurrentIndex(0)
        self.tabs.currentChanged.connect(self.on_current)

        self.computer.search()
        self.disk.start()
        QTimer.singleShot(400, self.first_run)
        for p in self.pages.values():
            p.reload()
            self.refresh(p)
        keep = list(self.pages) + ([COMPUTER_ID] if self.cfg.get("computer_content", {}).get("indexed") else [])
        self.worker.submit("purge_orphans", "*", keep)
        self.refresh_computer_content()
        self.content_poll = QTimer(self, interval=15 * 60_000)
        self.content_poll.timeout.connect(self.refresh_computer_content)
        self.content_poll.start()

        QShortcut(QKeySequence("Ctrl+T"), self, self.add_tab)
        QShortcut(QKeySequence("Ctrl+W"), self,
                  lambda: isinstance(self.current(), FolderPage) and not self.current().cfg.get("pinned")
                  and self.close_tab(self.current()))
        QShortcut(QKeySequence("Ctrl+,"), self, self.open_settings)
        QShortcut(QKeySequence("F5"), self, lambda: self.refresh(self.current()))
        for seq in ("Ctrl+F", "Ctrl+L"):
            QShortcut(QKeySequence(seq), self, self.focus_search)
        QShortcut(QKeySequence("Ctrl+Tab"), self,
                  lambda: self.tabs.setCurrentIndex((self.tabs.currentIndex() + 1) % self.tabs.count()))
        QShortcut(QKeySequence("Ctrl+Shift+Tab"), self,
                  lambda: self.tabs.setCurrentIndex((self.tabs.currentIndex() - 1) % self.tabs.count()))
        for i in range(1, 10):
            QShortcut(QKeySequence(f"Ctrl+{i}"), self, lambda i=i: self.tabs.setCurrentIndex(i - 1))

        self.poll = QTimer(self, interval=60_000)
        self.poll.timeout.connect(lambda: isinstance(self.current(), FolderPage) and self.refresh(self.current()))
        self.poll.start()
        QGuiApplication.instance().applicationStateChanged.connect(self.on_app_state)
        self.hidden_ticks = 0
        self.idle_check = QTimer(self, interval=30_000)
        self.idle_check.timeout.connect(self._idle_check)
        self.idle_check.start()

        self.tray = None
        self._quitting = False
        self.apply_settings()

        self.update_info, self._updating, self._manual_check = None, False, False
        self.updates = Updater(self)
        self.updates.found.connect(self.on_update_found)
        self.updates.progress.connect(
            lambda f: self.update_btn.setText(t("Laddar ner… {p} %", p=int(f * 100))))
        self.updates.downloaded.connect(self.on_update_downloaded)
        self.updates.failed.connect(self.on_update_failed)
        self.update_timer = QTimer(self, interval=updater.CHECK_EVERY)
        self.update_timer.timeout.connect(self.check_updates)
        if updater.supported():
            QTimer.singleShot(20_000, self.check_updates)
            self.update_timer.start()

    # --- hjälpare
    def current(self):
        return self.tabs.currentWidget()

    def folder_pages(self):
        return [w for w in (self.tabs.widget(i) for i in range(self.tabs.count())) if isinstance(w, FolderPage)]

    def focus_search(self):
        if p := self.current():
            p.edit.setFocus()
            p.edit.selectAll()

    def refresh(self, page):
        if isinstance(page, ComputerPage):
            page.search()
            return
        if not isinstance(page, FolderPage):
            return
        page.last_scan = time.time()
        self.worker.submit("scan", page.tab_id, page.cfg["path"])
        if page.cfg.get("indexed"):
            self.worker.submit("index", page.tab_id)
        if page.cfg.get("ocr"):
            self.worker.submit("ocr", page.tab_id)

    def save(self):
        self.cfg["tabs"] = [p.cfg for p in self.folder_pages() if p.cfg.get("pinned")]   # bara fästa sparas
        store.save_config(self.cfg)

    def _add_page(self, cfg):
        page = FolderPage(self, cfg)
        self.pages[cfg["id"]] = page
        i = self.tabs.addTab(page, cfg["name"])
        self.tabs.setTabToolTip(i, cfg["path"])
        self._decorate_tab(page)
        return page

    def _decorate_tab(self, page):
        """Nål till vänster (fäst/lossa) och, för ofästa flikar, ett kryss till höger."""
        bar, i = self.tabs.tabBar(), self.tabs.indexOf(page)
        pinned = page.cfg.get("pinned", False)
        for side in (QTabBar.LeftSide, QTabBar.RightSide):
            if old := bar.tabButton(i, side):
                old.deleteLater()
        pin = QToolButton(autoRaise=True)
        pin.setIcon(_pin_icon(pinned))
        pin.setIconSize(QSize(14, 14))
        pin.setFixedSize(20, 20)
        pin.setCursor(Qt.PointingHandCursor)
        pin.setToolTip(t("Lossa fliken – den stängs när swpt stängs") if pinned
                       else t("Fäst fliken – den finns kvar nästa gång swpt startar"))
        pin.clicked.connect(lambda: self.toggle_pin(page))
        bar.setTabButton(i, QTabBar.LeftSide, pin)
        if pinned:
            bar.setTabButton(i, QTabBar.RightSide, None)
        else:
            close = QToolButton(autoRaise=True)
            close.setIcon(_close_icon())
            close.setIconSize(QSize(10, 10))
            close.setFixedSize(18, 18)
            close.setCursor(Qt.PointingHandCursor)
            close.setToolTip(t("Stäng fliken (Ctrl+W)"))
            close.clicked.connect(lambda: self.close_tab(page))
            bar.setTabButton(i, QTabBar.RightSide, close)

    def toggle_pin(self, page):
        page.cfg["pinned"] = not page.cfg.get("pinned", False)
        if page.cfg["pinned"]:   # fästa flikar samlas först, direkt efter Hela datorn
            target = 1 + sum(1 for p in self.folder_pages() if p is not page and p.cfg.get("pinned"))
            frm = self.tabs.indexOf(page)
            if frm != target:
                self.tabs.tabBar().moveTab(frm, target)
        self._decorate_tab(page)
        self.save()

    def close_tab(self, page):
        self.tabs.removeTab(self.tabs.indexOf(page))
        del self.pages[page.tab_id]
        self.worker.submit("purge", page.tab_id)
        page.deleteLater()
        self.save()

    def _close_unpinned(self):
        for p in [p for p in self.folder_pages() if not p.cfg.get("pinned")]:
            self.close_tab(p)

    # --- innehållssök i hela datorn
    def confirm_computer_content(self):
        box = QMessageBox(self)
        box.setWindowTitle(t("Sök i filinnehåll"))
        box.setIconPixmap(QIcon(os.path.join(HERE, "logo.svg")).pixmap(64, 64))
        box.setText(t("<b>Indexera innehållet i dina dokument?</b>"))
        box.setInformativeText(t(
            "För att kunna söka i filinnehåll läser swpt texten i dina dokument – PDF, Word, Excel, "
            "PowerPoint och textfiler – i din användarmapp ({path}), inklusive OneDrive och SharePoint.\n\n"
            "Första gången kan det ta en stund beroende på hur många dokument du har. Det sker i bakgrunden, "
            "och indexet hålls sedan uppdaterat. Filer som bara finns i molnet läses inte.",
            path=os.path.expanduser("~")))
        yes = box.addButton(t("Indexera innehåll"), QMessageBox.AcceptRole)
        box.addButton(t("Avbryt"), QMessageBox.RejectRole)
        box.setDefaultButton(yes)
        box.exec()
        return box.clickedButton() is yes

    def refresh_computer_content(self):
        if self.cfg.get("computer_content", {}).get("indexed"):
            self.worker.submit("scan_docs", COMPUTER_ID, os.path.expanduser("~"))
            self.worker.submit("index", COMPUTER_ID)

    def unindex_computer_content(self):
        cc = self.cfg.setdefault("computer_content", {})
        cc["indexed"] = cc["checked"] = False
        self.computer.cb.setChecked(False)
        self.worker.submit("purge", COMPUTER_ID)
        self.save()

    def content_rows(self, tab_id, fts_query, exact=()):
        try:
            return self.con.execute(
                "SELECT f.id, f.path, f.name, f.size, f.mtime FROM content JOIN files f ON f.id=content.rowid"
                " WHERE content MATCH ? AND f.tab=?" + " AND wordmatch(content.text, ?)" * len(exact)
                + " ORDER BY rank LIMIT 5000", (fts_query, tab_id, *exact)).fetchall()
        except sqlite3.Error:
            return []

    # --- databas (huvudtråden)
    def content_hits(self, tab_id, fts_query, exact=()):
        try:
            return [r[0] for r in self.con.execute(
                "SELECT f.id FROM content JOIN files f ON f.id=content.rowid"
                " WHERE content MATCH ? AND f.tab=?" + " AND wordmatch(content.text, ?)" * len(exact)
                + " ORDER BY rank LIMIT 5000", (fts_query, tab_id, *exact))]
        except sqlite3.Error:
            return []

    def snippet(self, fts_query, fid):
        try:
            row = self.con.execute("SELECT snippet(content,0,'«','»','…',64) FROM content"
                                   " WHERE content MATCH ? AND rowid=?", (fts_query, fid)).fetchone()
        except sqlite3.Error:
            row = None
        return " ".join(row[0].split()) if row else ""

    # --- signaler
    def on_status(self, tab_id, msg):
        if p := (self.computer if tab_id == COMPUTER_ID else self.pages.get(tab_id)):
            p.set_worker_msg(msg)

    def on_indexed(self, tab_id):
        p = self.computer if tab_id == COMPUTER_ID else self.pages.get(tab_id)
        if p and p.cb.isChecked() and p.edit.text().strip():
            p.keeping_selection(p.search)

    def on_current(self, _):
        p = self.current()
        if isinstance(p, FolderPage) and time.time() - p.last_scan > RESCAN_AFTER:
            self.refresh(p)
        elif p is self.computer:
            p.on_disk_changed(False)
        self.focus_search()

    def _idle_check(self):
        """Dold i systemfältet eller minimerad: släpp sökresultaten efter en minut och lämna tillbaka
        minnet till Windows (igen var 30:e minut). Allt läses in igen när fönstret visas."""
        if self.isVisible() and not self.isMinimized():
            self.hidden_ticks = 0
            return
        self.hidden_ticks += 1
        if self.hidden_ticks % 60 != 2:
            return
        c = self.computer
        if c.q is not None:
            c.step_timer.stop()
            c.q, c.extra = None, []
            c.model.set_results([], sort=False)
        gc.collect()
        try:
            k32 = ctypes.windll.kernel32
            k32.GetCurrentProcess.restype = ctypes.wintypes.HANDLE
            k32.SetProcessWorkingSetSize.argtypes = [ctypes.wintypes.HANDLE, ctypes.c_size_t, ctypes.c_size_t]
            k32.SetProcessWorkingSetSize(k32.GetCurrentProcess(), ctypes.c_size_t(-1), ctypes.c_size_t(-1))
        except Exception:
            pass

    def on_app_state(self, state):
        p = self.current()
        if state != Qt.ApplicationActive or p is None:
            return
        if p is self.computer:   # ändringar som kommit medan fönstret var dolt
            p.on_disk_changed(False)
        if isinstance(p, FolderPage) and time.time() - p.last_scan > RESCAN_AFTER:
            self.refresh(p)

    # --- flikhantering
    def add_tab(self):
        path = QFileDialog.getExistingDirectory(self, t("Välj mapp för den nya fliken"), str(os.path.expanduser("~")))
        if not path:
            return
        path = os.path.normpath(path)
        dlg = QDialog(self)
        dlg.setWindowTitle(t("Ny flik"))
        dlg.setMinimumWidth(380)
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel(t("Namn på fliken:")))
        name_edit = QLineEdit(os.path.basename(path) or path)
        name_edit.selectAll()
        lay.addWidget(name_edit)
        pin = QCheckBox(t("Fäst fliken – den finns kvar nästa gång swpt startar"))
        lay.addWidget(pin)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText(t("Skapa flik"))
        bb.button(QDialogButtonBox.Cancel).setText(t("Avbryt"))
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        if dlg.exec() != QDialog.Accepted or not name_edit.text().strip():
            return
        page = self._add_page({"id": uuid.uuid4().hex, "name": name_edit.text().strip(), "path": path,
                               "content": False, "indexed": False, "pinned": pin.isChecked()})
        self.tabs.setCurrentWidget(page)
        page.reload()
        self.refresh(page)
        self.save()

    def tab_menu(self, pos):
        i = self.tabs.tabBar().tabAt(pos)
        page = self.tabs.widget(i) if i >= 0 else None
        menu = QMenu(self)
        if not isinstance(page, FolderPage):
            menu.addAction(t("Ny flik…"), self.add_tab)
            menu.addSeparator()
            if self.disk.enabled():
                menu.addAction(t("Uppdatera indexet nu"), lambda: self.disk.rebuild(quiet=True))
                menu.addAction(t("Stäng av indexering av datorn…"), self.disable_disk_index)
            else:
                menu.addAction(t("Indexera datorn"), self.enable_disk_index)
            if self.cfg.get("computer_content", {}).get("indexed"):
                menu.addAction(t("Sluta indexera innehåll i dokument (frigör utrymme)"), self.unindex_computer_content)
        else:
            pinned = page.cfg.get("pinned")
            menu.addAction(t("Lossa flik") if pinned else t("Fäst flik"), lambda: self.toggle_pin(page))
            menu.addSeparator()
            menu.addAction(t("Byt namn…"), lambda: self.rename_tab(page))
            menu.addAction(t("Byt mapp…"), lambda: self.change_folder(page))
            menu.addAction(t("Öppna mappen i Utforskaren"), lambda: os.startfile(page.cfg["path"]))
            menu.addAction(t("Uppdatera nu (F5)"), lambda: self.refresh(page))
            if page.cfg.get("ocr"):
                menu.addAction(t("Sluta läsa skannade dokument (OCR)"), lambda: self.set_ocr(page, False))
            else:
                menu.addAction(t("Läs in skannade dokument (OCR)…"), lambda: self.set_ocr(page, True))
            if page.cfg.get("indexed"):
                menu.addAction(t("Sluta indexera innehåll (frigör utrymme)"), lambda: self.unindex(page))
            menu.addSeparator()
            if pinned:
                menu.addAction(t("Ta bort flik…"), lambda: self.remove_tab(page))
            else:
                menu.addAction(t("Stäng flik\tCtrl+W"), lambda: self.close_tab(page))
        menu.exec(self.tabs.tabBar().mapToGlobal(pos))

    # --- inställningar, kortkommando och systemfält
    def open_settings(self):
        self.show_window()
        settings.SettingsDialog(self).exec()

    def apply_settings(self):
        err = settings.register_hotkey(int(self.winId()), settings.get(self.cfg, "hotkey"))
        if err:
            QTimer.singleShot(800, lambda: QMessageBox.warning(self, "swpt", err))
        use_tray = settings.get(self.cfg, "tray")
        QApplication.instance().setQuitOnLastWindowClosed(not use_tray)
        if use_tray and self.tray is None:
            self.tray = QSystemTrayIcon(app_icon(), self)
            menu = QMenu()
            menu.addAction(t("Öppna swpt"), self.show_window)
            menu.addAction(t("Inställningar…"), self.open_settings)
            menu.addSeparator()
            menu.addAction(t("Avsluta"), self.quit_app)
            self.tray.setContextMenu(menu)
            self.tray.activated.connect(
                lambda r: self.show_window() if r in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick) else None)
            self.tray.show()
        elif not use_tray and self.tray is not None:
            self.tray.hide()
            self.tray.deleteLater()
            self.tray = None
        if self.tray:
            key = settings.get(self.cfg, "hotkey")
            self.tray.setToolTip("swpt" + (f"  ({settings.display_hotkey(key)})" if key else ""))
        if settings.autostart_enabled():   # håll sökvägen i autostarten aktuell om projektet flyttats
            try:
                settings.set_autostart(True, settings.get(self.cfg, "start_hidden"))
            except OSError:
                pass

    def show_window(self):
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()
        try:
            ctypes.windll.user32.SetForegroundWindow(int(self.winId()))
        except Exception:
            pass
        self.focus_search()

    def toggle_window(self):
        if self.isVisible() and self.isActiveWindow() and not self.isMinimized():
            if self.tray:
                self.hide()
            else:
                self.showMinimized()
        else:
            self.show_window()

    def nativeEvent(self, event_type, message):
        msg = ctypes.wintypes.MSG.from_address(int(message))
        if msg.message == settings.WM_HOTKEY and msg.wParam == settings.HOTKEY_ID:
            self.toggle_window()
            return True, 0
        return False, 0

    def quit_app(self):
        self._quitting = True
        self.close()
        QApplication.instance().quit()

    # --- uppdateringar
    def check_updates(self, manual=False):
        if self._updating or not (manual or settings.get(self.cfg, "check_updates")):
            return
        self._manual_check = manual
        self.updates.check()

    def on_update_found(self, info):
        manual, self._manual_check = self._manual_check, False
        self.update_info = info
        if info is None:
            self.update_btn.hide()
            if manual:
                QMessageBox.information(self, t("Uppdateringar"),
                                        t("Du har den senaste versionen av swpt ({v}).", v=updater.current()))
            return
        self.update_btn.setText(t("Uppdatera till {v}", v=info["version"]))
        self.update_btn.setToolTip(t("Det finns en ny version av swpt. Klicka för att ladda ner och installera "
                                     "den – swpt startar om av sig själv."))
        self.update_btn.setEnabled(True)
        self.update_btn.show()
        if manual:
            if QMessageBox.question(self, t("Uppdateringar"),
                                    t("swpt {v} finns. Vill du uppdatera nu? swpt startar om av sig själv.",
                                      v=info["version"])) == QMessageBox.Yes:
                self.start_update()
        elif self.tray and self.cfg.get("update_notified") != info["version"]:
            self.cfg["update_notified"] = info["version"]
            self.save()
            self.tray.showMessage("swpt", t("swpt {v} finns. Öppna swpt och klicka på ”Uppdatera”.",
                                            v=info["version"]), app_icon(), 6000)

    def start_update(self):
        if not self.update_info or self._updating:
            return
        self._updating = True
        self.update_btn.setEnabled(False)
        self.update_btn.setText(t("Laddar ner… {p} %", p=0))
        self.updates.download(self.update_info)

    def on_update_downloaded(self, msi):
        self.update_btn.setText(t("Installerar…"))
        updater.install(msi)      # väntar tills swpt har avslutats
        self.quit_app()

    def on_update_failed(self, err):
        if self._updating:
            self._updating = False
            self.update_btn.setEnabled(True)
            self.update_btn.setText(t("Uppdatera till {v}", v=self.update_info["version"]))
            QMessageBox.warning(self, t("Uppdateringar"), t("Uppdateringen kunde inte laddas ner.") + f"\n\n{err}")
        elif self._manual_check:
            self._manual_check = False
            QMessageBox.warning(self, t("Uppdateringar"),
                                t("Kunde inte söka efter uppdateringar. Är datorn ansluten till internet?")
                                + f"\n\n{err}")

    # --- index över hela datorn
    def first_run(self):
        """Första start: fråga om datorn ska indexeras."""
        if "disk_index" in self.cfg:
            return
        box = QMessageBox(self)
        box.setWindowTitle(t("Välkommen till swpt"))
        box.setIconPixmap(QIcon(os.path.join(HERE, "logo.svg")).pixmap(64, 64))
        box.setText(t("<b>Vill du att swpt indexerar datorn?</b>"))
        box.setInformativeText(t(
            "För att kunna söka blixtsnabbt bland alla filer på datorn går swpt först igenom filnamnen "
            "på dina lokala diskar. Det tar ungefär en minut och sker i bakgrunden – du kan använda "
            "swpt under tiden.\n\n"
            "Därefter håller swpt indexet uppdaterat automatiskt. Bara filnamn, mappar, storlek och "
            "datum sparas, och bara på den här datorn."))
        yes = box.addButton(t("Indexera nu"), QMessageBox.AcceptRole)
        box.addButton(t("Inte nu"), QMessageBox.RejectRole)
        box.setDefaultButton(yes)
        box.exec()
        if box.clickedButton() is yes:
            self.enable_disk_index()
        else:
            self.cfg["disk_index"] = "no"
            self.save()
            self.computer.update_info()

    def enable_disk_index(self):
        self.cfg["disk_index"] = "yes"
        self.save()
        self.disk.start()

    def disable_disk_index(self):
        if QMessageBox.question(self, t("Stäng av indexering"),
                                t("Stänga av indexeringen av datorn?\n\nIndexet raderas och fliken ”Hela datorn” "
                                "kan inte söka förrän du indexerar igen. Mappflikarna påverkas inte.")) \
                == QMessageBox.Yes:
            self.disk.disable()
            self.save()

    def rename_tab(self, page):
        name, ok = QInputDialog.getText(self, t("Byt namn"), t("Nytt namn på fliken:"), text=page.cfg["name"])
        if ok and name.strip():
            page.cfg["name"] = name.strip()
            self.tabs.setTabText(self.tabs.indexOf(page), page.cfg["name"])
            page.edit.setPlaceholderText(t("Sök i {name}…   (flera ord = alla måste finnas)", name=page.cfg["name"]))
            self.save()

    def change_folder(self, page):
        path = QFileDialog.getExistingDirectory(self, t("Välj ny mapp"), page.cfg["path"])
        if not path:
            return
        page.cfg["path"] = os.path.normpath(path)
        self.tabs.setTabToolTip(self.tabs.indexOf(page), page.cfg["path"])
        self.worker.submit("purge", page.tab_id)
        page.recs, page.by_id = [], {}
        page.search()
        self.refresh(page)
        self.save()

    def set_ocr(self, page, on):
        """Slår på/av textigenkänning av skannade dokument för en flik."""
        if not on:
            page.cfg["ocr"] = False
            page.update_info()
            self.save()
            return
        if not ocr.available():
            QMessageBox.warning(self, t("Skannade dokument (OCR)"), t(
                "Windows textigenkänning (OCR) är inte installerad. Lägg till den under Inställningar → "
                "Tid och språk → Språk → Svenska → Språkalternativ → Optisk teckenigenkänning."))
            return
        box = QMessageBox(self)
        box.setWindowTitle(t("Skannade dokument (OCR)"))
        box.setIconPixmap(QIcon(os.path.join(HERE, "logo.svg")).pixmap(64, 64))
        box.setText(t("<b>Läsa in skannade dokument i ”{name}”?</b>", name=page.cfg["name"]))
        box.setInformativeText(t(
            "swpt tolkar texten i inskannade PDF:er och bilder (JPG, PNG, TIFF) i den här mappen med "
            "Windows inbyggda textigenkänning, så att du kan söka i dem. Allt sker på datorn och i "
            "bakgrunden – ungefär en halv sekund per sida.\n\n"
            "PDF:er som redan har text påverkas inte. Nya skannade filer i mappen läses in automatiskt."))
        yes = box.addButton(t("Starta OCR"), QMessageBox.AcceptRole)
        box.addButton(t("Avbryt"), QMessageBox.RejectRole)
        box.setDefaultButton(yes)
        box.exec()
        if box.clickedButton() is not yes:
            return
        page.cfg["ocr"] = True
        if not page.cb.isChecked():
            page.cb.setChecked(True)      # innehållssökning behövs för att hitta texten
        self.worker.submit("ocr", page.tab_id)
        page.update_info()
        self.save()

    def unindex(self, page):
        page.cfg["indexed"] = False
        page.cb.setChecked(False)
        self.worker.submit("unindex", page.tab_id)
        self.save()

    def remove_tab(self, page):
        if QMessageBox.question(self, t("Ta bort flik"),
                                t("Ta bort fliken ”{name}”?\n\n"
                                  "Filerna i mappen påverkas inte – bara fliken och dess sökindex tas bort.",
                                  name=page.cfg["name"])) \
                != QMessageBox.Yes:
            return
        self.close_tab(page)

    def closeEvent(self, ev):
        self.cfg["geometry"] = bytes(self.saveGeometry().toHex()).decode()
        self._close_unpinned()   # ofästa flikar är bara till för stunden
        self.save()
        if self.tray is not None and not self._quitting:
            # stäng till systemfältet: fortsätt köra (kortkommando och index lever vidare)
            ev.ignore()
            self.hide()
            if not self.cfg.get("tray_hint_shown"):
                self.cfg["tray_hint_shown"] = True
                self.save()
                key = settings.get(self.cfg, "hotkey")
                self.tray.showMessage("swpt", t("swpt körs vidare i systemfältet.") + (
                    " " + t("Öppna med {key}.", key=settings.display_hotkey(key)) if key else ""),
                    app_icon(), 4000)
            return
        settings.register_hotkey(int(self.winId()), "")
        self.worker.stop()
        self.disk.stop()
        QApplication.closeAllWindows()
        super().closeEvent(ev)


# ---------------------------------------------------------------- logga

def app_icon():
    return QIcon(os.path.join(HERE, "swpt.ico"))


def _plus_icon(color, size=16):
    pm = QPixmap(size * 2, size * 2)
    pm.setDevicePixelRatio(2)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(QColor(color), 2.2, Qt.SolidLine, Qt.RoundCap))
    m = size / 2
    p.drawLine(QPointF(m, 3), QPointF(m, size - 3))
    p.drawLine(QPointF(3, m), QPointF(size - 3, m))
    p.end()
    return QIcon(pm)


CORNER_STYLE = """
    QToolButton#newTab, QToolButton#settings {
        border: 1px solid rgba(128, 128, 128, 0.35);
        border-radius: 8px;
        background: transparent;
        font-weight: 600;
        min-height: 26px;
    }
    QToolButton#newTab { padding: 4px 14px 4px 10px; }
    QToolButton#settings { padding: 4px 7px; }
    QToolButton#newTab:hover, QToolButton#settings:hover { background: rgba(0, 184, 212, 0.14); border-color: #00B8D4; }
    QToolButton#newTab:pressed, QToolButton#settings:pressed { background: rgba(0, 184, 212, 0.28); }
    QToolButton#update {
        border: 1px solid #00B8D4;
        border-radius: 8px;
        background: #00B8D4;
        color: white;
        font-weight: 600;
        min-height: 26px;
        padding: 4px 12px;
    }
    QToolButton#update:hover { background: #00A3BD; }
    QToolButton#update:disabled { background: rgba(0, 184, 212, 0.55); color: white; }
"""


def _gear_icon(color, size=18):
    pm = QPixmap(size * 2, size * 2)
    pm.setDevicePixelRatio(2)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    c = QPointF(size / 2, size / 2)
    p.setPen(QPen(QColor(color), 2.4, Qt.SolidLine, Qt.RoundCap))
    for k in range(8):      # kuggar
        p.save()
        p.translate(c)
        p.rotate(k * 45)
        p.drawLine(QPointF(0, -size * 0.30), QPointF(0, -size * 0.42))
        p.restore()
    p.setPen(QPen(QColor(color), 2.0))
    p.drawEllipse(c, size * 0.27, size * 0.27)
    p.drawEllipse(c, size * 0.09, size * 0.09)
    p.end()
    return QIcon(pm)


def _pin_icon(pinned, size=14):
    """Häftstift: ifyllt och färgat när fliken är fäst, tunn kontur annars."""
    pm = QPixmap(size * 2, size * 2)
    pm.setDevicePixelRatio(2)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    color = QColor("#00B8D4") if pinned else QColor(150, 150, 150, 170)
    p.translate(size / 2, size / 2)
    p.rotate(45)
    pen = QPen(color, 1.4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(color if pinned else Qt.NoBrush)
    s = size
    p.drawRoundedRect(QRectF(-s * 0.18, -s * 0.46, s * 0.36, s * 0.38), 1.5, 1.5)    # huvud
    p.drawRect(QRectF(-s * 0.30, -s * 0.10, s * 0.60, s * 0.10))                       # krage
    p.setBrush(Qt.NoBrush)
    p.drawLine(QPointF(0, 0.02 * s), QPointF(0, s * 0.46))                              # nål
    p.end()
    return QIcon(pm)


def _close_icon(size=10):
    pm = QPixmap(size * 2, size * 2)
    pm.setDevicePixelRatio(2)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(QColor(150, 150, 150), 1.6, Qt.SolidLine, Qt.RoundCap))
    p.drawLine(QPointF(1.5, 1.5), QPointF(size - 1.5, size - 1.5))
    p.drawLine(QPointF(size - 1.5, 1.5), QPointF(1.5, size - 1.5))
    p.end()
    return QIcon(pm)


def corner_buttons(on_new_tab, on_settings):
    """Knapparna uppe till höger, med samma högermarginal som innehållet."""
    w = QWidget()
    w.setStyleSheet(CORNER_STYLE)
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 6, 0)
    lay.setSpacing(6)
    gear = QToolButton(objectName="settings")
    gear.setIcon(_gear_icon("#9AA0A6"))
    gear.setIconSize(QSize(18, 18))
    gear.setCursor(Qt.PointingHandCursor)
    gear.setToolTip(t("Inställningar (Ctrl+,)"))
    gear.clicked.connect(on_settings)
    b = QToolButton(objectName="newTab")
    b.setText(t("Ny flik"))
    b.setIcon(_plus_icon("#00B8D4"))
    b.setIconSize(QSize(16, 16))
    b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
    b.setCursor(Qt.PointingHandCursor)
    b.setToolTip(t("Lägg till en mapp som flik (Ctrl+T)"))
    b.clicked.connect(on_new_tab)
    lay.addWidget(gear)
    lay.addWidget(b)
    return w


def brand_label():
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(6, 0, 14, 0)
    lay.setSpacing(7)
    logo = QLabel()
    logo.setPixmap(QIcon(os.path.join(HERE, "logo.svg")).pixmap(22, 22))
    name = QLabel("swpt")
    f = name.font()
    f.setPointSizeF(f.pointSizeF() + 3)
    f.setBold(True)
    f.setLetterSpacing(QFont.PercentageSpacing, 104)
    name.setFont(f)
    lay.addWidget(logo)
    lay.addWidget(name)
    return w


def main():
    # Bakgrundstrådarna lämnar över till gränssnittet var 1 ms i stället för var 5:e, så att
    # fönstret inte hackar när det flyttas eller ritas om medan något görs i bakgrunden.
    sys.setswitchinterval(0.001)
    try:  # egen ikon i aktivitetsfältet i stället för Pythons
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("swpt.search")
    except Exception:
        pass
    app = QApplication(sys.argv)
    app.setApplicationName("swpt")
    app.setWindowIcon(app_icon())

    # Bara en swpt åt gången: startas den igen visas fönstret som redan körs.
    name = "swpt-" + os.environ.get("USERNAME", "user")
    sock = QLocalSocket()
    sock.connectToServer(name)
    if sock.waitForConnected(300):
        sock.write(b"show")
        sock.flush()
        sock.waitForBytesWritten(300)
        return
    QLocalServer.removeServer(name)
    server = QLocalServer()
    server.listen(name)

    win = MainWindow()
    server.newConnection.connect(lambda: (server.nextPendingConnection(), win.show_window()))
    if "--background" in sys.argv and win.tray is not None:
        pass   # autostart: ligger kvar i systemfältet tills man öppnar den
    else:
        win.show()
        win.focus_search()
    sys.exit(app.exec())
