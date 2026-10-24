"""Look and feel for Decompiler Studio: a clean dark UI (Midnight is the default) with a few other
themes selectable from the Theme menu; apply(name) switches them at run time.

The palette lives in module globals (BG, RED, ...) that apply() rebinds, so everything that reads
`theme.RED` at paint time follows the active theme. (The "RED" names are historical: they hold the
theme's accent colour.)"""
import math
import random
from pathlib import Path

from pygments.formatters import HtmlFormatter
from pygments.style import Style
from pygments.token import (Comment, Error, Generic, Keyword, Name, Number, Operator,
                            Punctuation, String, Text, Token)

DEFAULT_THEME = "Midnight"


def _t(name, light, **kw):
    kw["light"] = light
    kw["name"] = name
    return kw


# BG window | PANEL side panels | CODE_BG code area | LINE borders | FG text | DIM secondary text
# FAINT hints | RED accent | RED_HI bright accent | RED_DARK selection | RED_DEEP hover wash
THEMES = {
    "Midnight": _t("Midnight", False,
                   BG="#0f1419", PANEL="#151b23", CODE_BG="#11161d", LINE="#263040", FG="#d7dde5", DIM="#8593a5",
                   FAINT="#4b5767", RED="#3b82f6", RED_HI="#60a5fa", RED_DARK="#1e3a5f", RED_DEEP="#18263a",
                   SUB="#93a1b3", LOGBG="#0b0f14", HANDLE="#2b3748", GRAD_END="#2158b8", DIS_BORDER="#1a222d",
                   HEAD_END="#131a22", ONSEL="#fff", WARN="#e3a008", GLOW=(59, 130, 246), BIGGLOW=(59, 130, 246),
                   HEAT=(30, 70, 150), HEAT2=(15, 35, 80), FOLDER=("#2a4a7a", "#4a78b8", "#60a5fa"),
                   SYN=dict(kw="#7ab0ff", type="#5fd3c8", func="#f0f4f8", builtin="#7dd3fc", attr="#b7c4d6",
                            ns="#a9b6c6", string="#e6c07b", esc="#7dd3fc", number="#f0a070", op="#8fb4e0",
                            punct="#8593a5", comment="#5c6b7d", preproc="#c792ea", err="#ffffff bg:#9a2a3a")),
    "Graphite": _t("Graphite", False,
                   BG="#121212", PANEL="#1a1a1a", CODE_BG="#151515", LINE="#2c2c2c", FG="#dcdcdc", DIM="#8c8c8c",
                   FAINT="#555555", RED="#14b8a6", RED_HI="#2dd4bf", RED_DARK="#134e4a", RED_DEEP="#1b2a29",
                   SUB="#9a9a9a", LOGBG="#0e0e0e", HANDLE="#333333", GRAD_END="#0d7d71", DIS_BORDER="#222222",
                   HEAD_END="#171717", ONSEL="#fff", WARN="#e3a008", GLOW=(20, 184, 166), BIGGLOW=(20, 184, 166),
                   HEAT=(15, 110, 100), HEAT2=(8, 55, 50), FOLDER=("#1f5c55", "#2a9d8f", "#2dd4bf"),
                   SYN=dict(kw="#2dd4bf", type="#7dd3fc", func="#ffffff", builtin="#a5f3fc", attr="#c9c9c9",
                            ns="#b0b0b0", string="#e6c07b", esc="#a5f3fc", number="#f0a070", op="#5eead4",
                            punct="#8c8c8c", comment="#666666", preproc="#c792ea", err="#ffffff bg:#9a2a3a")),
    "Forest": _t("Forest", False,
                 BG="#0b120d", PANEL="#101a13", CODE_BG="#0d150f", LINE="#1f3425", FG="#d3e3d6", DIM="#7f9a85",
                 FAINT="#43594a", RED="#2f9e55", RED_HI="#4ade80", RED_DARK="#14402a", RED_DEEP="#12251a",
                 SUB="#8fa896", LOGBG="#080d09", HANDLE="#24402d", GRAD_END="#1b6b3a", DIS_BORDER="#16241a",
                 HEAD_END="#0e1711", ONSEL="#fff", WARN="#e3a008", GLOW=(47, 158, 85), BIGGLOW=(47, 158, 85),
                 HEAT=(20, 100, 50), HEAT2=(10, 50, 25), FOLDER=("#1b4d2e", "#2f8a4d", "#4ade80"),
                 SYN=dict(kw="#4ade80", type="#7dd3c0", func="#f1fff4", builtin="#a3e635", attr="#bcd3c1",
                          ns="#b2c5b6", string="#e6d27b", esc="#a3e635", number="#f0a870", op="#5fc98a",
                          punct="#7f9a85", comment="#56705c", preproc="#8fd6a8", err="#ffffff bg:#9a2a3a")),
    "Violet": _t("Violet", False,
                 BG="#0e0b16", PANEL="#141020", CODE_BG="#110e1b", LINE="#2a2140", FG="#dad4ea", DIM="#8a7fa6",
                 FAINT="#4f4568", RED="#8b5cf6", RED_HI="#a78bfa", RED_DARK="#32246a", RED_DEEP="#1b1432",
                 SUB="#9a90b8", LOGBG="#0a0810", HANDLE="#33284f", GRAD_END="#5b34c4", DIS_BORDER="#1b1530",
                 HEAD_END="#120e1d", ONSEL="#fff", WARN="#e3a008", GLOW=(139, 92, 246), BIGGLOW=(139, 92, 246),
                 HEAT=(80, 40, 170), HEAT2=(40, 20, 90), FOLDER=("#3a2a7a", "#6a4fc4", "#a78bfa"),
                 SYN=dict(kw="#a78bfa", type="#7dd3fc", func="#f5f0ff", builtin="#67e8f9", attr="#c4b5fd",
                          ns="#b9b3cc", string="#f0abfc", esc="#67e8f9", number="#fbbf77", op="#b79cff",
                          punct="#8a7fa6", comment="#625878", preproc="#f0abfc", err="#ffffff bg:#9a2a3a")),
    "Daylight": _t("Daylight", True,
                   BG="#f4f6f8", PANEL="#e9edf1", CODE_BG="#ffffff", LINE="#cfd7e0", FG="#1f2933", DIM="#52606d",
                   FAINT="#9aa5b1", RED="#2563eb", RED_HI="#1d4ed8", RED_DARK="#cfe0fb", RED_DEEP="#e1ebfb",
                   SUB="#52606d", LOGBG="#eef1f4", HANDLE="#c3ccd6", GRAD_END="#1e40af", DIS_BORDER="#dde3ea",
                   HEAD_END="#e8ecf0", ONSEL="#0b1f3a", WARN="#9a6a00", GLOW=(37, 99, 235), BIGGLOW=(37, 99, 235),
                   HEAT=(150, 190, 250), HEAT2=(200, 220, 250), FOLDER=("#6d94d8", "#8fb0e8", "#2563eb"),
                   SYN=dict(kw="#1d4ed8", type="#0f766e", func="#111827", builtin="#7c3aed", attr="#6b4f2a",
                            ns="#475569", string="#a16207", esc="#7c3aed", number="#c2410c", op="#1d4ed8",
                            punct="#52606d", comment="#7b8794", preproc="#9333ea", err="#000000 bg:#fecaca")),
}
THEME_NAMES = list(THEMES)

# palette globals (rebound by apply())
CURRENT = DEFAULT_THEME
LIGHT = False
BG = PANEL = CODE_BG = LINE = FG = DIM = FAINT = RED = RED_HI = RED_DARK = RED_DEEP = ""
SUB = LOGBG = HANDLE = GRAD_END = DIS_BORDER = HEAD_END = ONSEL = WARN = ""
GLOW = BIGGLOW = HEAT = HEAT2 = (0, 0, 0)
FOLDER = ("", "", "")
SYN = {}
QSS = ""
CodeStyle = None


def _make_style():
    s = SYN

    class ThemeStyle(Style):
        background_color = CODE_BG
        default_style = ""
        styles = {
            Text: FG,
            Comment: "italic " + s["comment"],
            Comment.Preproc: s["preproc"],
            Keyword: "bold " + s["kw"],
            Keyword.Type: s["type"],
            Keyword.Constant: s["type"],
            Name: FG,
            Name.Function: "bold " + s["func"],
            Name.Class: "bold " + s["func"],
            Name.Builtin: s["builtin"],
            Name.Decorator: s["builtin"],
            Name.Tag: s["kw"],
            Name.Attribute: s["attr"],
            Name.Namespace: s["ns"],
            String: s["string"],
            String.Escape: s["esc"],
            Number: s["number"],
            Operator: s["op"],
            Operator.Word: "bold " + s["kw"],
            Punctuation: s["punct"],
            Generic.Heading: "bold " + s["kw"],
            Generic.Subheading: "bold " + s["type"],
            Generic.Strong: "bold",
            Generic.Emph: "italic",
            Error: s["err"],
            Token: FG,
        }
    return ThemeStyle


def apply(name=None):
    """Switch the palette; returns the name actually applied (unknown names fall back to the default)."""
    global CURRENT, LIGHT, QSS, CodeStyle, SYN, FOLDER, GLOW, BIGGLOW, HEAT, HEAT2
    global BG, PANEL, CODE_BG, LINE, FG, DIM, FAINT, RED, RED_HI, RED_DARK, RED_DEEP
    global SUB, LOGBG, HANDLE, GRAD_END, DIS_BORDER, HEAD_END, ONSEL, WARN
    t = THEMES.get(name) or THEMES[DEFAULT_THEME]
    CURRENT = t["name"]
    LIGHT = t["light"]
    BG, PANEL, CODE_BG, LINE, FG, DIM, FAINT = (t[k] for k in ("BG", "PANEL", "CODE_BG", "LINE", "FG", "DIM", "FAINT"))
    RED, RED_HI, RED_DARK, RED_DEEP = (t[k] for k in ("RED", "RED_HI", "RED_DARK", "RED_DEEP"))
    SUB, LOGBG, HANDLE, GRAD_END, DIS_BORDER, HEAD_END, ONSEL, WARN = (
        t[k] for k in ("SUB", "LOGBG", "HANDLE", "GRAD_END", "DIS_BORDER", "HEAD_END", "ONSEL", "WARN"))
    GLOW, BIGGLOW, HEAT, HEAT2, FOLDER, SYN = (
        t[k] for k in ("GLOW", "BIGGLOW", "HEAT", "HEAT2", "FOLDER", "SYN"))
    CodeStyle = _make_style()
    QSS = build_qss()
    return CURRENT


def formatter():
    return HtmlFormatter(style=CodeStyle, noclasses=True, nobackground=True)


def _rgba(c, a):
    return f"rgba({c[0]}, {c[1]}, {c[2]}, {int(a * 255)})"


def build_qss():
    return f"""
* {{ color: {FG}; font-size: 10pt; }}
QMainWindow, QWidget {{ background: {BG}; }}
QLabel {{ background: transparent; }}
QMenuBar {{ background: {BG}; border-bottom: 1px solid {LINE}; }}
QMenuBar::item {{ padding: 4px 10px; background: transparent; }}
QMenuBar::item:selected {{ background: {RED_DARK}; color: {ONSEL}; }}
QMenu {{ background: {PANEL}; border: 1px solid {RED_DARK}; }}
QMenu::item {{ padding: 5px 22px; }}
QMenu::item:selected {{ background: {RED_DARK}; color: {ONSEL}; }}

#logo {{ color: {FG}; font-size: 22px; font-weight: 800; }}
#biglogo {{ color: {FG}; font-size: 60px; font-weight: 800; }}
#hint {{ color: {FAINT}; padding: 12px; }}
#subtitle {{ color: {SUB}; }}
#warning {{ color: {WARN}; font-weight: bold; letter-spacing: 1px; }}
#dropzone {{ border: 2px dashed {RED}; border-radius: 16px; background: {_rgba(GLOW, 0.07)}; }}
#dropzone QLabel {{ background: transparent; }}
#dropicon {{ color: {RED_HI}; font-size: 40px; font-weight: 800; }}
#droptext {{ color: {FG}; font-size: 15pt; font-weight: 700; }}
#dropsub {{ color: {SUB}; }}
#pill {{ background: {_rgba(GLOW, 0.14)}; color: {RED_HI}; border: 1px solid {_rgba(GLOW, 0.45)}; border-radius: 12px;
         padding: 3px 12px; font-weight: 700; letter-spacing: 1px; }}
#featcard {{ background: {_rgba(GLOW, 0.06)}; border: 1px solid {LINE}; border-radius: 12px; }}
#featcard QLabel {{ background: transparent; }}
#feattitle {{ color: {FG}; font-weight: 800; font-size: 11pt; }}
#featbody {{ color: {SUB}; }}
#oracle {{ background: {PANEL}; border-top: 1px solid {RED_DARK}; }}
#pathlabel {{ color: {DIM}; padding: 4px 6px; border-bottom: 1px solid {LINE}; background: {PANEL}; }}
#panelTitle {{ color: {RED}; font-weight: bold; padding: 6px 4px 2px 4px; }}

QPushButton {{ background: {PANEL}; border: 1px solid {RED_DARK}; padding: 6px 16px; border-radius: 6px; }}
QPushButton:hover {{ background: {RED_DEEP}; border-color: {RED_HI}; color: {ONSEL}; }}
QPushButton:focus {{ border-color: {RED_HI}; }}
QPushButton:pressed {{ background: {RED_DARK}; }}
QPushButton:checked {{ background: {RED_DARK}; border-color: {RED}; color: {ONSEL}; }}
QPushButton#primary {{ background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 {RED}, stop:1 {GRAD_END});
                       border: 1px solid {RED_HI}; color: #fff; font-weight: bold; }}
QPushButton#primary:hover {{ background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 {RED_HI}, stop:1 {RED}); }}
QPushButton:disabled {{ color: {FAINT}; border-color: {DIS_BORDER}; background: {BG}; }}

QLineEdit {{ background: {PANEL}; border: 1px solid {LINE}; padding: 7px 9px; border-radius: 6px;
             selection-background-color: {RED_DARK}; }}
QLineEdit:focus {{ border-color: {RED}; }}

QTreeView, QListWidget {{ background: {PANEL}; border: none; outline: 0; }}
QTreeView::item, QListWidget::item {{ padding: 3px 2px; }}
QTreeView::item:hover, QListWidget::item:hover {{ background: {RED_DEEP}; }}
QTreeView::item:selected, QListWidget::item:selected {{ background: {RED_DARK}; color: {ONSEL}; border-radius: 4px; }}

QTextBrowser, QPlainTextEdit {{ background: {CODE_BG}; border: none;
                                selection-background-color: {RED_DARK}; selection-color: {ONSEL}; }}
QPlainTextEdit#log {{ background: {LOGBG}; color: {DIM}; border-top: 1px solid {RED_DARK}; }}

QSplitter::handle {{ background: {LINE}; }}
QSplitter::handle:hover {{ background: {RED}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}

QStatusBar {{ background: {BG}; color: {DIM}; border-top: 1px solid {LINE}; }}
QStatusBar::item {{ border: none; }}
QProgressBar {{ background: {PANEL}; border: 1px solid {RED_DARK}; border-radius: 4px; max-height: 8px; }}
QProgressBar::chunk {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {RED_DARK}, stop:1 {RED_HI}); }}

QScrollBar:vertical {{ background: {BG}; width: 10px; margin: 0; }}
QScrollBar:horizontal {{ background: {BG}; height: 10px; margin: 0; }}
QScrollBar::handle {{ background: {HANDLE}; border-radius: 0; min-height: 30px; min-width: 30px; }}
QScrollBar::handle:hover {{ background: {RED}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

QToolTip {{ background: {PANEL}; color: {FG}; border: 1px solid {RED}; }}
QMessageBox {{ background: {PANEL}; }}
QDialog {{ background: {PANEL}; }}
"""




# file badges shown in the tree: extension -> (label, color)
BADGES = {
    ".java": ("J", "#b8560a"), ".kt": ("K", "#7a55c4"), ".smali": ("S", "#6a5a58"),
    ".c": ("C", "#4f7fd6"), ".h": ("H", "#3d5fa8"), ".cpp": ("C+", "#4f7fd6"),
    ".cs": ("C#", "#8a3f86"), ".py": ("PY", "#2f5f8f"), ".js": ("JS", "#b09500"),
    ".ts": ("TS", "#2a62a8"), ".html": ("<>", "#d9622b"), ".css": ("#", "#4a3380"),
    ".json": ("{}", "#6e5f5c"), ".xml": ("<>", "#8a4a26"), ".asm": ("AS", "#5a4644"),
    ".txt": ("T", "#4d3a38"), ".md": ("MD", "#2f9e7a"), ".png": ("IMG", "#2a6e4c"),
    ".jpg": ("IMG", "#2a6e4c"), ".webp": ("IMG", "#2a6e4c"), ".so": ("SO", "#3a2a29"),
    ".dll": ("DL", "#3a2a29"),
}


def mono_font(size=10):
    from PySide6.QtGui import QFont, QFontDatabase
    for fam in ("JetBrains Mono", "Fira Code", "Cascadia Code", "Hack", "DejaVu Sans Mono",
                "Liberation Mono", "Consolas"):
        if fam in QFontDatabase.families():
            return QFont(fam, size)
    f = QFontDatabase.systemFont(QFontDatabase.FixedFont)
    f.setPointSize(size)
    return f


def badge_icon(label, color, cache={}):
    from PySide6.QtCore import Qt, QRectF
    from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
    key = (label, color)
    if key in cache:
        return cache[key]
    pm = QPixmap(32, 32)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(QColor("#00000066"))
    p.drawRoundedRect(QRectF(2, 4, 28, 24), 2, 2)
    p.setPen(QColor("#fff1e0"))
    f = QFont()
    f.setBold(True)
    f.setPixelSize(13 if len(label) <= 2 else 10)
    p.setFont(f)
    p.drawText(QRectF(2, 4, 28, 24), Qt.AlignCenter, label)
    p.end()
    cache[key] = QIcon(pm)
    return cache[key]


def folder_icon(cache={}):
    from PySide6.QtCore import Qt, QRectF
    from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
    if CURRENT in cache:
        return cache[CURRENT]
    pm = QPixmap(32, 32)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(FOLDER[0]))
    p.drawRoundedRect(QRectF(2, 6, 13, 6), 1.5, 1.5)          # the tab
    p.setBrush(QColor(FOLDER[1]))
    p.drawRoundedRect(QRectF(2, 9, 28, 18), 2, 2)             # the folder
    p.setBrush(QColor(FOLDER[2]))
    p.drawRoundedRect(QRectF(2, 9, 28, 4), 2, 2)              # lighter top edge
    p.end()
    cache[CURRENT] = QIcon(pm)
    return cache[CURRENT]


def app_icon(size=128):
    """A rounded square in the accent colour with { } on it."""
    from PySide6.QtCore import Qt, QRectF
    from PySide6.QtGui import QColor, QFont, QIcon, QLinearGradient, QPainter, QPixmap
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    g = QLinearGradient(0, 0, 0, size)
    g.setColorAt(0, QColor(RED_HI))
    g.setColorAt(1, QColor(GRAD_END))
    p.setPen(Qt.NoPen)
    p.setBrush(g)
    p.drawRoundedRect(QRectF(0, 0, size, size), size * 0.2, size * 0.2)
    f = QFont()
    f.setBold(True)
    f.setPixelSize(int(size * 0.5))
    p.setFont(f)
    p.setPen(QColor("#ffffff"))
    p.drawText(QRectF(0, 0, size, size * 0.96), Qt.AlignCenter, "{ }")
    p.end()
    return QIcon(pm)


def make_widgets():
    """Custom widgets are built lazily so importing theme.py needs no Qt."""
    from PySide6.QtCore import QRectF, Qt, QTimer
    from PySide6.QtGui import QColor, QLinearGradient, QPainter, QRadialGradient
    from PySide6.QtWidgets import QWidget

    class HeaderBar(QWidget):
        """The header: a flat bar with a thin accent line under it."""
        ROOM = 0                      # extra height below the content (none: the bar is flat)

        def paintEvent(self, e):
            p = QPainter(self)
            w, h = self.width(), self.height()
            g = QLinearGradient(0, 0, 0, h)
            g.setColorAt(0, QColor(BG))
            g.setColorAt(1, QColor(HEAD_END))
            p.fillRect(0, 0, w, h, g)
            p.fillRect(0, h - 2, w, 2, QColor(RED))

    class WelcomeCanvas(QWidget):
        """Welcome page backdrop: soft colour orbs, a fading dot grid and faint code drifting upwards."""
        LINES = [
            [("kw", "int"), ("fn", " main"), ("p", "(int argc, char **argv) {")],
            [("p", "    FILE *f = fopen(path, \"rb\");")],
            [("kw", "    if"), ("p", " (!f) "), ("kw", "return"), ("n", " -1"), ("p", ";")],
            [("p", "    uint32_t magic = read_u32(f);")],
            [("kw", "    switch"), ("p", " (magic) {")],
            [("kw", "    case"), ("n", " 0x464c457f"), ("p", ": "), ("fn", "load_elf"), ("p", "(f); "), ("kw", "break"), ("p", ";")],
            [("kw", "    case"), ("n", " 0x04034b50"), ("p", ": "), ("fn", "unzip_apk"), ("p", "(f); "), ("kw", "break"), ("p", ";")],
            [("p", "    }")],
            [("kw", "    for"), ("p", " (int i = 0; i < n; i++) {")],
            [("p", "        names[i] = read_string(pool, idx[i]);")],
            [("p", "    }")],
            [("kw", "    return"), ("n", " 0"), ("p", ";")],
            [("p", "}")],
            [("p", "")],
        ]

        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.t = 0.0
            self.glow_effect = None          # set by the app: drop shadow on the big title
            self.timer = QTimer(self, interval=60)
            self.timer.timeout.connect(self.tick)
            self.lines = self.LINES

        def showEvent(self, e):
            self.timer.start()
            super().showEvent(e)

        def hideEvent(self, e):
            self.timer.stop()
            super().hideEvent(e)

        def breathe(self):
            return 0.8 + 0.2 * math.sin(self.t * 0.9)

        def tick(self):
            self.t += 0.06
            if self.glow_effect is not None:
                self.glow_effect.setBlurRadius(18 + 10 * self.breathe())
            self.update()

        def paintEvent(self, e):
            from PySide6.QtGui import QFont
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            w, h = self.width(), self.height()
            g = QLinearGradient(0, 0, 0, h)
            g.setColorAt(0, QColor(BG))
            g.setColorAt(1, QColor(PANEL))
            p.fillRect(self.rect(), g)
            k = self.breathe()
            # two soft colour orbs that drift
            for cx, cy, r, col, a in (
                    (w * (0.18 + 0.04 * math.sin(self.t * 0.35)), h * (0.22 + 0.03 * math.cos(self.t * 0.3)), max(w, h) * 0.45, HEAT, 120),
                    (w * (0.85 + 0.04 * math.cos(self.t * 0.3)), h * (0.88 + 0.03 * math.sin(self.t * 0.4)), max(w, h) * 0.5, HEAT2 if LIGHT else HEAT, 110)):
                rg = QRadialGradient(cx, cy, r)
                rg.setColorAt(0, QColor(*col, int(a * k)))
                rg.setColorAt(1, QColor(*col, 0))
                p.fillRect(self.rect(), rg)
            # drifting code on the left and right, fading out toward the middle
            mono = mono_font(11)
            mono.setPixelSize(15)
            p.setFont(mono)
            lh = 24
            colors = {"kw": SYN["kw"], "fn": SYN["func"], "n": SYN["number"], "p": SYN["punct"]}
            total = len(self.lines) * lh
            off = (self.t * 14) % total
            for side_x, align_right in ((28, False), (None, True)):
                y = -off
                while y < h + lh:
                    for li, line in enumerate(self.lines):
                        yy = y + li * lh
                        if -lh < yy < h:
                            fade = 1.0 - abs((yy / h) - 0.5) * 1.1
                            x = side_x if side_x is not None else w - 28 - 330
                            for kind, txt in line:
                                c = QColor(colors.get(kind, SYN["punct"]))
                                hf = min(1.0, max(0.0, (abs(x - w / 2) - 300) / 220))     # nothing behind the centre content
                                c.setAlpha(int(60 * max(0.1, fade) * hf))
                                p.setPen(c)
                                p.drawText(int(x), int(yy), txt)
                                x += p.fontMetrics().horizontalAdvance(txt)
                    y += total
                    break
            # dot grid, strongest in the centre
            p.setPen(Qt.NoPen)
            step = 34
            for gx in range(step // 2, w, step):
                for gy in range(step // 2, h, step):
                    d = math.hypot(gx - w / 2, gy - h * 0.45) / (max(w, h) * 0.6)
                    a = int(46 * max(0.0, 1 - d))
                    if a > 3:
                        c = QColor(RED_HI)
                        c.setAlpha(a)
                        p.setBrush(c)
                        p.drawEllipse(gx - 1, gy - 1, 2, 2)

    return HeaderBar, WelcomeCanvas


apply(DEFAULT_THEME)
