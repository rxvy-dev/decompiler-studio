"""Look and feel for Decompiler Studio. The main theme is Evil: a crypt, black with a red tint, dried
blood, ember red, bone-coloured text, square corners, thin blood-red rules and one flickering sigil.
A few other themes are selectable from the Theme menu; apply(name) switches them at run time.

The palette lives in module globals (BG, RED, ...) that apply() rebinds, so everything that reads
`theme.RED` at paint time follows the active theme."""
import math
import random
from pathlib import Path

from pygments.formatters import HtmlFormatter
from pygments.style import Style
from pygments.token import (Comment, Error, Generic, Keyword, Name, Number, Operator,
                            Punctuation, String, Text, Token)

DEFAULT_THEME = "Evil"


def _t(name, light, **kw):
    kw["light"] = light
    kw["name"] = name
    return kw


# BG window | PANEL side panels | CODE_BG code area | LINE borders | FG text | DIM secondary text
# FAINT hints | RED accent | RED_HI bright accent | RED_DARK selection | RED_DEEP hover wash
# (the names say "RED" because Evil came first; in the other themes they hold that theme's accent)
THEMES = {
    "Evil": _t("Evil", False,
               BG="#060304", PANEL="#0e0708", CODE_BG="#0a0506", LINE="#2a1214", FG="#d9cfc2", DIM="#8a6f6a",
               FAINT="#4d3a38", RED="#c4151f", RED_HI="#ff3b2f", RED_DARK="#4a0a0f", RED_DEEP="#1d0709",
               SUB="#a58a84", LOGBG="#040203", HANDLE="#2e1517", GRAD_END="#7a0c12", DIS_BORDER="#1a0e0f",
               HEAD_END="#14080a", ONSEL="#fff", GLOW=(196, 21, 31), BIGGLOW=(220, 20, 30), HEAT=(150, 12, 18),
               HEAT2=(70, 6, 10), EMBER=(255, 90, 40), VIG=(0, 0, 0, 200),
               FOLDER=("#5a0b11", "#8f1019", "#b3121b"),
               SYN=dict(kw="#ff3b2f", type="#ff7a6a", func="#fff1e0", builtin="#ff9a6a", attr="#e0a080",
                        ns="#c9bcae", string="#e0b062", esc="#ff9a6a", number="#f08a5d", op="#cf4040",
                        punct="#9a8580", comment="#6e5551", preproc="#c0605a", err="#ffffff bg:#7a0a10")),
    "Void": _t("Void", False,
               BG="#050309", PANEL="#0b0713", CODE_BG="#08050f", LINE="#221637", FG="#d6d0e6", DIM="#7d7096",
               FAINT="#43395a", RED="#7c3aed", RED_HI="#b388ff", RED_DARK="#2c1360", RED_DEEP="#150a2e",
               SUB="#9a90b8", LOGBG="#030207", HANDLE="#2a1d45", GRAD_END="#3b1b86", DIS_BORDER="#120c20",
               HEAD_END="#0e0818", ONSEL="#fff", GLOW=(124, 58, 237), BIGGLOW=(150, 90, 250), HEAT=(80, 30, 170),
               HEAT2=(40, 14, 90), EMBER=(170, 110, 255), VIG=(0, 0, 0, 200),
               FOLDER=("#2c1360", "#4c2a9c", "#7c3aed"),
               SYN=dict(kw="#b388ff", type="#8ab4ff", func="#f3eaff", builtin="#7dd3fc", attr="#c4b5fd",
                        ns="#b9b3cc", string="#f0abfc", esc="#7dd3fc", number="#fbbf77", op="#a78bfa",
                        punct="#8478a0", comment="#5f5480", preproc="#c084fc", err="#ffffff bg:#5b1a7a")),
    "Toxic": _t("Toxic", False,
                BG="#030803", PANEL="#070f08", CODE_BG="#050b06", LINE="#12281a", FG="#cfe6d2", DIM="#6a8f70",
                FAINT="#35503a", RED="#1fb54a", RED_HI="#5dff7a", RED_DARK="#0a3d1a", RED_DEEP="#08200f",
                SUB="#86a98c", LOGBG="#020502", HANDLE="#153320", GRAD_END="#0d6a28", DIS_BORDER="#0c1a10",
                HEAD_END="#08140b", ONSEL="#fff", GLOW=(31, 181, 74), BIGGLOW=(60, 230, 100), HEAT=(14, 120, 40),
                HEAT2=(6, 60, 20), EMBER=(140, 255, 110), VIG=(0, 0, 0, 200),
                FOLDER=("#0a3d1a", "#177a36", "#1fb54a"),
                SYN=dict(kw="#5dff7a", type="#8affc1", func="#eaffee", builtin="#b6ff6a", attr="#9be0a8",
                         ns="#b5cdb8", string="#e6e36a", esc="#b6ff6a", number="#ffb86b", op="#3fc96b",
                         punct="#6f9477", comment="#4d6b53", preproc="#7fdc8f", err="#ffffff bg:#7a1a1a")),
    "Frost": _t("Frost", False,
                BG="#040810", PANEL="#08111c", CODE_BG="#060d17", LINE="#14283d", FG="#d3e2f0", DIM="#6d8aa6",
                FAINT="#3a5068", RED="#2a7fd4", RED_HI="#5cc8ff", RED_DARK="#0c2f55", RED_DEEP="#0a1b30",
                SUB="#8ba7c2", LOGBG="#03060c", HANDLE="#173049", GRAD_END="#164f8c", DIS_BORDER="#0d1826",
                HEAD_END="#091524", ONSEL="#fff", GLOW=(42, 127, 212), BIGGLOW=(70, 170, 240), HEAT=(20, 90, 170),
                HEAT2=(10, 45, 90), EMBER=(150, 220, 255), VIG=(0, 0, 0, 200),
                FOLDER=("#0c2f55", "#1d5c9e", "#2a7fd4"),
                SYN=dict(kw="#5cc8ff", type="#7aa8ff", func="#eef8ff", builtin="#4fd1c5", attr="#9cc7e8",
                         ns="#b4c6d6", string="#f2c97d", esc="#4fd1c5", number="#ff9f7a", op="#4aa3e0",
                         punct="#7a93ab", comment="#54708a", preproc="#6bb6ff", err="#ffffff bg:#7a1a2a")),
    "Bone": _t("Bone", True,
               BG="#f1ebe0", PANEL="#e8e0d1", CODE_BG="#f8f4ea", LINE="#cdbfa8", FG="#2b1d1b", DIM="#6e5a55",
               FAINT="#a89a8c", RED="#b01020", RED_HI="#d6221f", RED_DARK="#e6b8b0", RED_DEEP="#efd9d2",
               SUB="#6e5a55", LOGBG="#ece4d6", HANDLE="#c9b9a4", GRAD_END="#7a0c12", DIS_BORDER="#d8cdbb",
               HEAD_END="#e2d8c6", ONSEL="#1a0a08", GLOW=(176, 16, 32), BIGGLOW=(200, 60, 60), HEAT=(200, 60, 60),
               HEAT2=(230, 150, 130), EMBER=(190, 60, 30), VIG=(110, 80, 60, 70),
               FOLDER=("#b9503f", "#d9776a", "#b01020"),
               SYN=dict(kw="#b01020", type="#c2410c", func="#1a0a08", builtin="#9a3412", attr="#92400e",
                        ns="#5b4a44", string="#8a5a00", esc="#9a3412", number="#b45309", op="#a31622",
                        punct="#6e5a55", comment="#8a7a70", preproc="#a14b3f", err="#000000 bg:#f5b5b5")),
}
THEME_NAMES = list(THEMES)

# palette globals (rebound by apply())
CURRENT = DEFAULT_THEME
LIGHT = False
BG = PANEL = CODE_BG = LINE = FG = DIM = FAINT = RED = RED_HI = RED_DARK = RED_DEEP = ""
SUB = LOGBG = HANDLE = GRAD_END = DIS_BORDER = HEAD_END = ONSEL = ""
GLOW = BIGGLOW = HEAT = HEAT2 = EMBER = (0, 0, 0)
VIG = (0, 0, 0, 0)
FOLDER = ("", "", "")
SYN = {}
QSS = ""
BloodStyle = None


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
    """Switch the palette; returns the name actually applied (unknown names fall back to Evil)."""
    global CURRENT, LIGHT, QSS, BloodStyle, SYN, FOLDER, VIG, GLOW, BIGGLOW, HEAT, HEAT2, EMBER
    global BG, PANEL, CODE_BG, LINE, FG, DIM, FAINT, RED, RED_HI, RED_DARK, RED_DEEP
    global SUB, LOGBG, HANDLE, GRAD_END, DIS_BORDER, HEAD_END, ONSEL
    t = THEMES.get(name) or THEMES[DEFAULT_THEME]
    CURRENT = t["name"]
    LIGHT = t["light"]
    BG, PANEL, CODE_BG, LINE, FG, DIM, FAINT = (t[k] for k in ("BG", "PANEL", "CODE_BG", "LINE", "FG", "DIM", "FAINT"))
    RED, RED_HI, RED_DARK, RED_DEEP = (t[k] for k in ("RED", "RED_HI", "RED_DARK", "RED_DEEP"))
    SUB, LOGBG, HANDLE, GRAD_END, DIS_BORDER, HEAD_END, ONSEL = (
        t[k] for k in ("SUB", "LOGBG", "HANDLE", "GRAD_END", "DIS_BORDER", "HEAD_END", "ONSEL"))
    GLOW, BIGGLOW, HEAT, HEAT2, EMBER, VIG, FOLDER, SYN = (
        t[k] for k in ("GLOW", "BIGGLOW", "HEAT", "HEAT2", "EMBER", "VIG", "FOLDER", "SYN"))
    BloodStyle = _make_style()
    QSS = build_qss()
    return CURRENT


def formatter():
    return HtmlFormatter(style=BloodStyle, noclasses=True, nobackground=True)


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

#logo {{ color: {RED_HI}; font-size: 30px; }}
#biglogo {{ color: {RED_HI}; font-size: 84px; }}
#hint {{ color: {FAINT}; padding: 12px; }}
#subtitle {{ color: {SUB}; }}
#warning {{ color: {RED_HI}; font-weight: bold; letter-spacing: 1px; }}
#oracle {{ background: {PANEL}; border-top: 1px solid {RED_DARK}; }}
#pathlabel {{ color: {DIM}; padding: 4px 6px; border-bottom: 1px solid {LINE}; background: {PANEL}; }}
#panelTitle {{ color: {RED}; font-weight: bold; padding: 6px 4px 2px 4px; }}

QPushButton {{ background: {PANEL}; border: 1px solid {RED_DARK}; padding: 6px 16px; border-radius: 0; }}
QPushButton:hover {{ background: {RED_DEEP}; border-color: {RED_HI}; color: {ONSEL}; }}
QPushButton:focus {{ border-color: {RED_HI}; }}
QPushButton:pressed {{ background: {RED_DARK}; }}
QPushButton:checked {{ background: {RED_DARK}; border-color: {RED}; color: {ONSEL}; }}
QPushButton#primary {{ background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 {RED}, stop:1 {GRAD_END});
                       border: 1px solid {RED_HI}; color: #fff; font-weight: bold; }}
QPushButton#primary:hover {{ background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 {RED_HI}, stop:1 {RED}); }}
QPushButton:disabled {{ color: {FAINT}; border-color: {DIS_BORDER}; background: {BG}; }}

QLineEdit {{ background: {PANEL}; border: 1px solid {LINE}; padding: 6px; border-radius: 0;
             selection-background-color: {RED_DARK}; }}
QLineEdit:focus {{ border-color: {RED}; }}

QTreeView, QListWidget {{ background: {PANEL}; border: none; outline: 0; }}
QTreeView::item, QListWidget::item {{ padding: 3px 2px; }}
QTreeView::item:hover, QListWidget::item:hover {{ background: {RED_DEEP}; }}
QTreeView::item:selected, QListWidget::item:selected {{ background: {RED_DARK}; color: {ONSEL}; }}

QTextBrowser, QPlainTextEdit {{ background: {CODE_BG}; border: none;
                                selection-background-color: {RED_DARK}; selection-color: {ONSEL}; }}
QPlainTextEdit#log {{ background: {LOGBG}; color: {DIM}; border-top: 1px solid {RED_DARK}; }}

QSplitter::handle {{ background: {LINE}; }}
QSplitter::handle:hover {{ background: {RED}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}

QStatusBar {{ background: {BG}; color: {DIM}; border-top: 1px solid {LINE}; }}
QStatusBar::item {{ border: none; }}
QProgressBar {{ background: {PANEL}; border: 1px solid {RED_DARK}; border-radius: 0; max-height: 8px; }}
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
    ".c": ("C", "#c4151f"), ".h": ("H", "#8a1017"), ".cpp": ("C+", "#c4151f"),
    ".cs": ("C#", "#8a3f86"), ".py": ("PY", "#2f5f8f"), ".js": ("JS", "#b09500"),
    ".ts": ("TS", "#2a62a8"), ".html": ("<>", "#c03e1c"), ".css": ("#", "#4a3380"),
    ".json": ("{}", "#6e5f5c"), ".xml": ("<>", "#8a4a26"), ".asm": ("AS", "#5a4644"),
    ".txt": ("T", "#4d3a38"), ".md": ("MD", "#ff3b2f"), ".png": ("IMG", "#2a6e4c"),
    ".jpg": ("IMG", "#2a6e4c"), ".webp": ("IMG", "#2a6e4c"), ".so": ("SO", "#3a2a29"),
    ".dll": ("DL", "#3a2a29"),
}


def load_fonts(app_dir):
    """Register the bundled title font; returns its family name or None."""
    from PySide6.QtGui import QFontDatabase
    f = Path(app_dir) / "assets" / "MetalMania.woff"
    if f.exists():
        i = QFontDatabase.addApplicationFont(str(f))
        fams = QFontDatabase.applicationFontFamilies(i) if i >= 0 else []
        if fams:
            return fams[0]
    return None


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
    p.drawRoundedRect(QRectF(2, 6, 13, 6), 1.5, 1.5)
    p.setBrush(QColor(FOLDER[1]))
    p.drawRoundedRect(QRectF(2, 9, 28, 18), 2, 2)
    p.setBrush(QColor(FOLDER[2]))          # a drip down the front
    p.drawRoundedRect(QRectF(21, 9, 3, 13), 1.5, 1.5)
    p.drawEllipse(QRectF(20, 19, 5, 5))
    p.end()
    cache[CURRENT] = QIcon(pm)
    return cache[CURRENT]


# ---------------------------------------------------------------------------
# the sigil: an inverted pentagram in a circle
# ---------------------------------------------------------------------------
def star_points(cx, cy, r):
    """Five points of an upside-down star, in the order that draws it with one stroke."""
    pts = [(cx + r * math.cos(math.radians(90 + i * 72)),
            cy + r * math.sin(math.radians(90 + i * 72))) for i in range(5)]
    return [pts[i] for i in (0, 2, 4, 1, 3)]


def draw_sigil(p, cx, cy, r, color, width, alpha=255):
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QColor, QPen, QPolygonF
    c = QColor(color)
    c.setAlpha(alpha)
    p.setPen(QPen(c, width))
    p.setBrush(Qt.NoBrush)
    p.drawEllipse(QPointF(cx, cy), r, r)
    p.drawEllipse(QPointF(cx, cy), r * 1.08, r * 1.08)
    p.drawPolygon(QPolygonF([QPointF(x, y) for x, y in star_points(cx, cy, r)]))


def app_icon(size=128):
    from PySide6.QtCore import Qt, QRectF
    from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(BG))
    p.drawRoundedRect(QRectF(0, 0, size, size), size * 0.16, size * 0.16)
    draw_sigil(p, size / 2, size / 2 + size * 0.02, size * 0.34, RED_HI, max(2.0, size / 28))
    p.end()
    return QIcon(pm)


def make_widgets():
    """Custom widgets are built lazily so importing theme.py needs no Qt."""
    from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
    from PySide6.QtGui import QColor, QLinearGradient, QPainter, QRadialGradient
    from PySide6.QtWidgets import QWidget

    class DripBar(QWidget):
        """The header: a thin blood-red edge with a few drips hanging from it."""
        DRIP_ROOM = 24

        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            rnd = random.Random(666)
            # (x as fraction of width, length in px, thickness in px)
            self.drips = sorted((rnd.random(), rnd.choice((4, 6, 9, 12, 16, 20)), rnd.choice((3, 3, 4, 4, 5)))
                                for _ in range(15))

        def paintEvent(self, e):
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            w, h = self.width(), self.height()
            edge = h - self.DRIP_ROOM
            g = QLinearGradient(0, 0, 0, edge)
            g.setColorAt(0, QColor(BG))
            g.setColorAt(1, QColor(HEAD_END))
            p.fillRect(0, 0, w, edge, g)
            p.fillRect(0, edge, w, self.DRIP_ROOM, QColor(BG))
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(RED))
            p.drawRect(0, edge - 1, w, 2)
            for fx, ln, th in self.drips:
                x = fx * (w - 20) + 10
                p.drawRoundedRect(QRectF(x, edge - 1, th, ln + 1), th / 2, th / 2)
                p.drawEllipse(QPointF(x + th / 2, edge + ln), th * 0.8, th * 1.05)

    class SigilCanvas(QWidget):
        """Welcome page backdrop: red glow from below, a flickering inverted sigil, rising embers."""

        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.rnd = random.Random()
            self.embers = []
            self.t = 0.0
            self.glow_effect = None          # set by the app: drop shadow on the big title
            self.timer = QTimer(self, interval=50)
            self.timer.timeout.connect(self.tick)

        def showEvent(self, e):
            self.timer.start()
            super().showEvent(e)

        def hideEvent(self, e):
            self.timer.stop()
            super().hideEvent(e)

        def flicker(self):
            # two slow waves and a little noise: a candle, not a strobe
            return 0.75 + 0.15 * math.sin(self.t * 1.3) + 0.07 * math.sin(self.t * 4.1 + 1.0)

        def tick(self):
            self.t += 0.05
            w, h = max(1, self.width()), max(1, self.height())
            if len(self.embers) < 38 and self.rnd.random() < 0.5:
                self.embers.append({"x": self.rnd.uniform(0, w), "y": h + 4,
                                    "vy": self.rnd.uniform(0.6, 1.8), "dx": self.rnd.uniform(-0.3, 0.3),
                                    "r": self.rnd.uniform(0.8, 2.2), "life": 1.0,
                                    "ph": self.rnd.uniform(0, 6.28)})
            for em in self.embers:
                em["y"] -= em["vy"]
                em["x"] += em["dx"] + 0.4 * math.sin(self.t * 2 + em["ph"])
                em["life"] -= 0.0035 + 0.002 * em["vy"]
            self.embers = [e for e in self.embers if e["life"] > 0 and e["y"] > -10]
            if self.glow_effect is not None:
                self.glow_effect.setBlurRadius(26 + 22 * self.flicker())
            self.update()

        def paintEvent(self, e):
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            w, h = self.width(), self.height()
            p.fillRect(self.rect(), QColor(BG))
            fl = self.flicker()
            # heat from the bottom edge
            g = QRadialGradient(w / 2, h * 1.05, max(w, h) * 0.75)
            g.setColorAt(0, QColor(*HEAT, int(120 * fl)))
            g.setColorAt(0.45, QColor(*HEAT2, int(70 * fl)))
            g.setColorAt(1, QColor(*HEAT2, 0))
            p.fillRect(self.rect(), g)
            # the sigil, behind the title: wide soft glow, then a thin sharp line
            cx, cy, r = w / 2, h * 0.43, min(w, h) * 0.34
            for width, alpha in ((14, 10), (8, 18), (4, 30)):
                draw_sigil(p, cx, cy, r, RED, width, int(alpha * fl))
            draw_sigil(p, cx, cy, r, RED_HI, 1.4, int(95 * fl))
            # embers
            p.setPen(Qt.NoPen)
            for em in self.embers:
                a = max(0.0, min(1.0, em["life"]))
                p.setBrush(QColor(min(255, EMBER[0] + int(30 * (1 - a))), min(255, EMBER[1] + int(80 * a)), EMBER[2], int(210 * a)))
                p.drawEllipse(QPointF(em["x"], em["y"]), em["r"], em["r"])
            # vignette
            v = QRadialGradient(w / 2, h / 2, max(w, h) * 0.72)
            v.setColorAt(0.55, QColor(0, 0, 0, 0))
            v.setColorAt(1, QColor(*VIG))
            p.fillRect(self.rect(), v)

    return DripBar, SigilCanvas


apply(DEFAULT_THEME)
