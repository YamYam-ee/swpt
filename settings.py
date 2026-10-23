"""Inställningar: globalt kortkommando, autostart, systemfält, språk och index över hela datorn."""
import ctypes
import os
import sys
import time
import winreg
from ctypes import wintypes

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout,
                               QKeySequenceEdit, QLabel, QPushButton, QVBoxLayout)

import updater
from i18n import fmt_int, t

HERE = os.path.dirname(os.path.abspath(__file__))
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "swpt"

DEFAULTS = {
    "hotkey": "",           # t.ex. "Ctrl+Alt+S" (QKeySequence.PortableText)
    "tray": True,           # fortsätt köra i systemfältet när fönstret stängs
    "start_hidden": True,   # vid autostart: starta dolt i systemfältet
    "language": "auto",     # auto / sv / en
    "check_updates": True,  # fråga GitHub en gång per dygn om det finns en ny version
}


def get(cfg, key):
    return cfg.get("settings", {}).get(key, DEFAULTS[key])


# ---------------------------------------------------------------- autostart

def _command(hidden):
    if getattr(sys, "frozen", False):   # byggd som swpt.exe
        return f'"{sys.executable}"' + (" --background" if hidden else "")
    exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(exe):
        exe = sys.executable
    return f'"{exe}" "{os.path.join(HERE, "swpt.pyw")}"' + (" --background" if hidden else "")


def autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_NAME)
            return True
    except OSError:
        return False


def set_autostart(on, hidden):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, _command(hidden))
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except OSError:
                pass


# ---------------------------------------------------------------- globalt kortkommando

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY = 0x0312
HOTKEY_ID = 0x5357
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
_user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]

_SPECIAL = {
    Qt.Key_Space: 0x20, Qt.Key_Tab: 0x09, Qt.Key_Return: 0x0D, Qt.Key_Enter: 0x0D, Qt.Key_Escape: 0x1B,
    Qt.Key_Insert: 0x2D, Qt.Key_Delete: 0x2E, Qt.Key_Home: 0x24, Qt.Key_End: 0x23, Qt.Key_PageUp: 0x21,
    Qt.Key_PageDown: 0x22, Qt.Key_Left: 0x25, Qt.Key_Up: 0x26, Qt.Key_Right: 0x27, Qt.Key_Down: 0x28,
    Qt.Key_Period: 0xBE, Qt.Key_Comma: 0xBC, Qt.Key_Minus: 0xBD, Qt.Key_Plus: 0xBB, Qt.Key_Backspace: 0x08,
    Qt.Key_section: 0xDC, Qt.Key_Pause: 0x13,
}


def parse_hotkey(text):
    """"Ctrl+Alt+S" → (modifierare, virtuell tangent), eller None."""
    seq = QKeySequence.fromString(text, QKeySequence.PortableText)
    if seq.isEmpty():
        return None
    combo = seq[0]
    key, mods = combo.key(), combo.keyboardModifiers()
    if Qt.Key_A <= key <= Qt.Key_Z or Qt.Key_0 <= key <= Qt.Key_9:
        vk = int(key)
    elif Qt.Key_F1 <= key <= Qt.Key_F24:
        vk = 0x70 + int(key) - int(Qt.Key_F1)
    else:
        vk = _SPECIAL.get(key)
    if vk is None:
        return None
    m = MOD_NOREPEAT
    m |= MOD_CONTROL if mods & Qt.ControlModifier else 0
    m |= MOD_ALT if mods & Qt.AltModifier else 0
    m |= MOD_SHIFT if mods & Qt.ShiftModifier else 0
    m |= MOD_WIN if mods & Qt.MetaModifier else 0
    if m & (MOD_CONTROL | MOD_ALT | MOD_WIN) == 0 and not 0x70 <= vk <= 0x87:
        return None   # en vanlig tangent utan Ctrl/Alt/Win skulle stjäla den överallt
    return m, vk


def register_hotkey(hwnd, text):
    """Registrerar kortkommandot. Returnerar None om det gick, annars ett felmeddelande."""
    _user32.UnregisterHotKey(hwnd, HOTKEY_ID)
    if not text:
        return None
    parsed = parse_hotkey(text)
    if parsed is None:
        return t("Kortkommandot måste innehålla Ctrl, Alt eller Win (eller vara en F-tangent).")
    if not _user32.RegisterHotKey(hwnd, HOTKEY_ID, *parsed):
        return t("Kortkommandot {key} används redan av ett annat program.", key=display_hotkey(text))
    return None


def display_hotkey(text):
    return QKeySequence.fromString(text, QKeySequence.PortableText).toString(QKeySequence.NativeText)


# ---------------------------------------------------------------- dialogen

class SettingsDialog(QDialog):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.setWindowTitle(t("Inställningar"))
        self.setMinimumWidth(520)
        cfg = win.cfg
        lay = QVBoxLayout(self)
        lay.setSpacing(12)

        # kortkommando
        box = QGroupBox(t("Kortkommando"))
        bl = QVBoxLayout(box)
        row = QHBoxLayout()
        row.addWidget(QLabel(t("Öppna swpt med:")))
        self.key = QKeySequenceEdit(QKeySequence.fromString(get(cfg, "hotkey"), QKeySequence.PortableText))
        self.key.setMaximumSequenceLength(1)
        self.key.setClearButtonEnabled(True)
        row.addWidget(self.key, 1)
        bl.addLayout(row)
        hint = QLabel(t("Fungerar var du än är i Windows, t.ex. Ctrl+Alt+S. Klicka i rutan och tryck tangenterna. "
                        "Trycker du igen när swpt redan är framme döljs fönstret."))
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray;")
        bl.addWidget(hint)
        self.key_error = QLabel()
        self.key_error.setStyleSheet("color: #E5484D;")
        self.key_error.setWordWrap(True)
        self.key_error.hide()
        bl.addWidget(self.key_error)
        lay.addWidget(box)

        # start och bakgrund
        box = QGroupBox(t("Start och bakgrund"))
        bl = QVBoxLayout(box)
        self.autostart = QCheckBox(t("Starta swpt när Windows startar"))
        self.autostart.setChecked(autostart_enabled())
        self.start_hidden = QCheckBox(t("Starta dolt – bara som ikon i systemfältet"))
        self.start_hidden.setChecked(get(cfg, "start_hidden"))
        self.tray = QCheckBox(t("Fortsätt köra i systemfältet när fönstret stängs"))
        self.tray.setChecked(get(cfg, "tray"))
        tray_hint = QLabel(t("Behövs för att kortkommandot ska fungera hela tiden och för att indexet ska hållas "
                             "uppdaterat. Avsluta helt via ikonen i systemfältet."))
        tray_hint.setWordWrap(True)
        tray_hint.setStyleSheet("color: gray; margin-left: 22px;")
        bl.addWidget(self.autostart)
        sub = QHBoxLayout()
        sub.setContentsMargins(22, 0, 0, 0)
        sub.addWidget(self.start_hidden)
        bl.addLayout(sub)
        bl.addWidget(self.tray)
        bl.addWidget(tray_hint)
        self.autostart.toggled.connect(self.start_hidden.setEnabled)
        self.start_hidden.setEnabled(self.autostart.isChecked())
        lay.addWidget(box)

        # språk
        box = QGroupBox(t("Språk"))
        fl = QVBoxLayout(box)
        row = QHBoxLayout()
        row.addWidget(QLabel(t("Språk:")))
        self.lang = QComboBox()
        for code, label in (("auto", t("Automatiskt (samma som Windows)")), ("sv", "Svenska"), ("en", "English")):
            self.lang.addItem(label, code)
        self.lang.setCurrentIndex(max(0, self.lang.findData(get(cfg, "language"))))
        row.addWidget(self.lang, 1)
        fl.addLayout(row)
        self.lang_note = QLabel(t("Det nya språket används nästa gång swpt startar."))
        self.lang_note.setStyleSheet("color: gray;")
        self.lang_note.hide()
        fl.addWidget(self.lang_note)
        self.lang.currentIndexChanged.connect(lambda _: self.lang_note.show())
        lay.addWidget(box)

        # index
        box = QGroupBox(t("Index över hela datorn"))
        bl = QVBoxLayout(box)
        self.index_on = QCheckBox(t("Indexera hela datorn (för fliken ”Hela datorn”)"))
        self.index_on.setChecked(win.disk.enabled())
        self.index_status = QLabel()
        self.index_status.setStyleSheet("color: gray;")
        rebuild = QPushButton(t("Uppdatera indexet nu"))
        rebuild.clicked.connect(lambda: (win.disk.rebuild(quiet=True), self._show_index_status()))
        row = QHBoxLayout()
        row.addWidget(self.index_status, 1)
        row.addWidget(rebuild)
        bl.addWidget(self.index_on)
        bl.addLayout(row)
        self._show_index_status()
        lay.addWidget(box)

        # uppdateringar
        box = QGroupBox(t("Uppdateringar"))
        bl = QVBoxLayout(box)
        self.check_updates = QCheckBox(t("Sök efter nya versioner automatiskt (en gång per dygn)"))
        self.check_updates.setChecked(get(cfg, "check_updates"))
        version = QLabel(t("Version {v}", v=updater.current()))
        version.setStyleSheet("color: gray;")
        check_now = QPushButton(t("Sök efter uppdateringar nu"))
        check_now.clicked.connect(lambda: win.check_updates(manual=True))
        check_now.setEnabled(updater.supported())
        row = QHBoxLayout()
        row.addWidget(version, 1)
        row.addWidget(check_now)
        bl.addWidget(self.check_updates)
        bl.addLayout(row)
        lay.addWidget(box)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText(t("Spara"))
        buttons.button(QDialogButtonBox.Cancel).setText(t("Avbryt"))
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def _show_index_status(self):
        d = self.win.disk
        if d.building or (d.index is None and d.status):
            self.index_status.setText(d.status)
        elif d.index is not None:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(d.index.created))
            self.index_status.setText(t("{n} filer · senast genomsökt {when}", n=fmt_int(d.index.count()), when=when))
        else:
            self.index_status.setText(t("Inte indexerad"))

    def save(self):
        win = self.win
        s = win.cfg.setdefault("settings", {})
        hotkey = self.key.keySequence().toString(QKeySequence.PortableText)
        err = register_hotkey(int(win.winId()), hotkey)
        if err:
            self.key_error.setText(err)
            self.key_error.show()
            register_hotkey(int(win.winId()), get(win.cfg, "hotkey"))   # behåll det gamla
            return
        s["hotkey"] = hotkey
        s["tray"] = self.tray.isChecked()
        s["start_hidden"] = self.start_hidden.isChecked()
        s["language"] = self.lang.currentData()
        s["check_updates"] = self.check_updates.isChecked()
        try:
            set_autostart(self.autostart.isChecked(), self.start_hidden.isChecked())
        except OSError:
            pass
        if self.index_on.isChecked() != win.disk.enabled():
            if self.index_on.isChecked():
                win.enable_disk_index()
            else:
                win.disk.disable()
        win.apply_settings()
        win.save()
        self.accept()
