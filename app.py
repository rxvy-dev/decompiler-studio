#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Decompiler Studio - Copyright (C) 2026 rxvy-dev. Free software under the GNU GPL v3 or later; see LICENSE.
"""Decompiler Studio - open an app/binary, browse the recovered source as a folder tree."""
import html
import os
import sys
import tempfile
from pathlib import Path

import engines
import llm
import theme
import viewers

HELP = """Decompiler Studio
Usage:
  decompiler-studio                     open the GUI
  decompiler-studio FILE                open the GUI with FILE loaded
  decompiler-studio --cli FILE OUTDIR   decompile without the GUI
"""


def cli(argv):
    if sys.stdout is None:  # windowed Windows build has no console
        sys.stdout = sys.stderr = open(os.devnull, "w")
    if len(argv) != 2:
        print(HELP)
        return 2
    try:
        engines.decompile(argv[0], argv[1], print)
    except Exception as e:
        print("Error:", e)
        return 1
    return 0


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print(HELP)
        return 0
    if len(sys.argv) > 2 and sys.argv[1] == "--py-batch":
        engines.python_batch_worker(sys.argv[2])
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        return cli(sys.argv[2:])
    if len(sys.argv) >= 2 and sys.argv[1] == "--install-ai":
        # app --install-ai [--model=qwen2.5-coder:7b]: Ollama + model for Explain / Malware check
        m = next((a.split("=", 1)[1] for a in sys.argv[2:] if a.startswith("--model=")), None)
        try:
            llm.install_ai(print, m)
            return 0
        except Exception as e:
            print(f"AI model not installed: {e}")
            return 1
        finally:
            llm.stop_server()
    if len(sys.argv) == 4 and sys.argv[1] in ("--extract-assets", "--decompile-binary"):
        # one file from an already recovered tree: app --extract-assets <file> <tree folder>
        if sys.argv[1] == "--extract-assets":
            import unity_assets
            print(unity_assets.extract(sys.argv[2], sys.argv[3], print))
        else:
            print(engines.decompile_embedded(sys.argv[2], sys.argv[3], print))
        return 0

    from PySide6.QtCore import Qt, QThread, Signal, QDir, QSortFilterProxyModel
    from PySide6.QtGui import QAction, QActionGroup, QColor, QFont, QKeySequence
    from PySide6.QtWidgets import (QFileSystemModel, QApplication, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
                                   QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar,
                                   QSplitter, QTextBrowser, QTreeView, QVBoxLayout, QWidget, QStackedWidget,
                                   QListWidget, QListWidgetItem, QTabWidget, QPushButton,
                                   QFileIconProvider, QGraphicsDropShadowEffect, QFrame)
    from pygments import highlight
    from pygments.formatters import HtmlFormatter
    from pygments.lexers import get_lexer_for_filename, TextLexer, guess_lexer_for_filename
    from pygments.util import ClassNotFound

    class Worker(QThread):
        log = Signal(str)
        finished_ok = Signal(str, str)
        failed = Signal(str)

        def __init__(self, path, out):
            super().__init__()
            self.path, self.out = path, out

        def run(self):
            try:
                kind = engines.decompile(self.path, self.out, self.log.emit)
                self.finished_ok.emit(kind, self.out)
            except engines.Cancelled:
                self.failed.emit("Cancelled.")
            except Exception as e:
                self.failed.emit(str(e))

    def _looks_binary(head):
        """NUL bytes, or lots of bytes that are neither text nor UTF-8 (a .split piece of an asset file)."""
        if b"\x00" in head:
            return True
        if not head:
            return False
        txt = head.decode("utf-8", errors="replace")
        bad = txt.count("\ufffd") + sum(1 for ch in txt if ord(ch) < 32 and ch not in "\t\n\r\f\x1b")
        return bad > len(txt) * 0.05

    def _hexdump(data):
        rows = []
        for i in range(0, len(data), 16):
            row = data[i:i + 16]
            rows.append(f"{i:08x}  {' '.join(f'{b:02x}' for b in row):<47}  {''.join(chr(b) if 32 <= b < 127 else '.' for b in row)}")
        return "\n".join(rows)

    class NativeWorker(QThread):
        log = Signal(str)
        finished_ok = Signal(str)
        failed = Signal(str)

        def __init__(self, path, root, job="native"):
            super().__init__()
            self.path, self.root, self.job = path, root, job

        def run(self):
            try:
                if self.job == "unity":
                    import unity_assets
                    engines.CANCEL.clear()
                    out = unity_assets.extract(self.path, self.root, self.log.emit, engines.check_cancel)
                else:
                    out = engines.decompile_embedded(self.path, self.root, self.log.emit)
                self.finished_ok.emit(str(out))
            except engines.Cancelled:
                self.failed.emit("Cancelled.")
            except Exception as e:
                self.failed.emit(str(e))

    class AiInstallWorker(QThread):
        log = Signal(str)
        finished_ok = Signal(str)
        failed = Signal(str)

        def __init__(self, model):
            super().__init__()
            self.model = model

        def run(self):
            try:
                self.finished_ok.emit(llm.install_ai(self.log.emit, self.model, engines.check_cancel))
            except engines.Cancelled:
                self.failed.emit("Cancelled.")
            except Exception as e:
                self.failed.emit(str(e))

    HeaderBar, WelcomeCanvas, WorkCanvas = theme.make_widgets()
    UI = {"title_font": None}

    class IconProvider(QFileIconProvider):
        def icon(self, info):
            if not isinstance(info, QFileIconProvider.IconType) and hasattr(info, "isDir"):
                if info.isDir():
                    return theme.folder_icon()
                ext = "." + info.suffix().lower() if info.suffix() else ""
                label, color = theme.BADGES.get(ext, (ext[1:3].upper() or "?", "#3a3a3a"))
                return theme.badge_icon(label, color)
            return super().icon(info)

    def title_font(px):
        f = QFont(UI["title_font"]) if UI["title_font"] else QFont()
        if not UI["title_font"]:
            f.setBold(True)
        return f
    class ExplainWorker(QThread):
        model = Signal(str)
        piece = Signal(str)
        finished_ok = Signal()
        failed = Signal(str)

        def __init__(self, code, filename, label, url, model, mode="explain"):
            super().__init__()
            self.args = (code, filename, label, url, model)
            self.mode = mode
            self._stop = False

        def stop(self):
            self._stop = True

        def run(self):
            try:
                for kind, text in llm.stream_explain(*self.args, should_stop=lambda: self._stop, mode=self.mode):
                    if kind == "model":
                        self.model.emit(text)
                    else:
                        self.piece.emit(text)
                self.finished_ok.emit()
            except llm.LlmError as e:
                self.failed.emit(str(e))
            except Exception as e:
                self.failed.emit(f"Explain failed: {e}")

    HIGHLIGHT_LIMIT = 1_500_000      # above this, show plain text (colors would freeze the UI)
    SEARCH_LIMIT = 500_000_000

    class Win(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("Decompiler Studio")
            self.resize(1300, 800)
            self.setAcceptDrops(True)
            self.out_dir = None
            self.worker = None
            self.work_root = tempfile.mkdtemp(prefix="decompiler-studio-")

            bar = self.menuBar()
            m = bar.addMenu("&File")
            a = QAction("&Open file...", self, shortcut=QKeySequence.Open, triggered=self.open_dialog)
            m.addAction(a)
            a = QAction("&Extract source to folder...", self, triggered=self.export)
            m.addAction(a)
            m.addAction(QAction("&Decompile selected binary", self, shortcut="Ctrl+D", triggered=self.decompile_selected))
            a = QAction("&Quit", self, shortcut=QKeySequence.Quit, triggered=self.close)
            m.addAction(a)

            nm = bar.addMenu("&Go")
            nm.addAction(QAction("Go to &function / offset...", self, shortcut="Ctrl+G", triggered=self.goto_dialog))
            nm.addAction(QAction("Go to &definition of the word under the cursor", self, shortcut="F12", triggered=self.goto_definition))
            nm.addAction(QAction("&Who calls this?", self, shortcut="Shift+F12", triggered=self.who_calls))
            nm.addAction(QAction("&Back", self, shortcut="Alt+Left", triggered=self.go_back))
            nm.addSeparator()
            nm.addAction(QAction("File &info (hashes, entropy)", self, shortcut="Ctrl+I", triggered=self.file_info))
            em = bar.addMenu("&Explain")
            em.addAction(QAction("&Explain selection / function", self, shortcut="Ctrl+E", triggered=self.explain))
            em.addAction(QAction("&Check selection / function for malware", self, shortcut="Ctrl+Shift+M", triggered=self.check_malware))
            em.addAction(QAction("&Install AI model...", self, triggered=self.install_ai))
            em.addAction(QAction("Choose &model...", self, triggered=self.pick_model))
            em.addAction(QAction("Server &address...", self, triggered=self.pick_server))
            em.addAction(QAction("&How to set it up", self, triggered=self.explain_help))

            tm = bar.addMenu("&Theme")
            self.theme_group = QActionGroup(self)
            self.theme_group.setExclusive(True)
            for tname in theme.THEME_NAMES:
                label = tname + ("  (default)" if tname == theme.DEFAULT_THEME else "")
                ta = QAction(label, self, checkable=True, checked=(tname == theme.CURRENT))
                ta.triggered.connect(lambda _=False, n=tname: self.set_theme(n))
                self.theme_group.addAction(ta)
                tm.addAction(ta)

            # ---- header bar
            header = HeaderBar(objectName="header")
            hl = QHBoxLayout(header)
            hl.setContentsMargins(14, 8, 10, 10)
            logo = QLabel("Decompiler Studio", objectName="logo")
            logo.setFont(title_font(30))
            glow = QGraphicsDropShadowEffect(logo)
            glow.setColor(QColor(*theme.GLOW, 120))
            self.logo_glow = glow
            glow.setBlurRadius(22)
            glow.setOffset(0, 0)
            logo.setGraphicsEffect(glow)
            self.filelabel = QLabel("", objectName="subtitle")
            hl.addWidget(logo)
            hl.addSpacing(18)
            hl.addWidget(self.filelabel, 1)
            self.btn_open = QPushButton("Open", objectName="primary", clicked=self.open_dialog,
                                        toolTip="Open a file to decompile (Ctrl+O)")
            self.btn_export = QPushButton("Extract", clicked=self.export, enabled=False,
                                          toolTip="Export the recovered source to a folder")
            self.btn_report = QPushButton("Report", clicked=self.show_report, enabled=False,
                                          toolTip="What was recovered, and what to trust")
            self.btn_log = QPushButton("Log", checkable=True, checked=False,
                                       toolTip="Show or hide the progress log")
            self.btn_check = QPushButton("Malware check", clicked=self.check_malware,
                                         toolTip="Ask the local AI model whether the selected code (or the function you are "
                                                 "looking at) looks malicious (Ctrl+Shift+M)")
            self.btn_explain = QPushButton("Explain", clicked=self.explain,
                                           toolTip="Explain the selected code (or the function you are looking at) in plain "
                                                   "English with a small local AI model (Ctrl+E)")
            self.btn_cancel = QPushButton("Cancel", clicked=self.cancel,
                                          toolTip="Cancel: stop the decompile and every helper it started")
            self.btn_cancel.hide()
            for b in (self.btn_cancel, self.btn_report, self.btn_export, self.btn_check, self.btn_explain, self.btn_log, self.btn_open):
                b.setCursor(Qt.PointingHandCursor)
                hl.addWidget(b)

            self.model = QFileSystemModel()
            self.model.setIconProvider(IconProvider())
            self.model.setFilter(QDir.AllEntries | QDir.NoDotAndDotDot | QDir.Hidden)
            self.tree = QTreeView()
            self.tree.setModel(self.model)
            for c in (1, 2, 3):
                self.tree.hideColumn(c)
            self.tree.setHeaderHidden(True)
            self.tree.setAnimated(True)
            self.tree.setIndentation(16)
            from PySide6.QtCore import QSize
            self.tree.setIconSize(QSize(18, 18))
            self.tree.hide()
            self.treehint = QLabel("Nothing opened yet.\nRecovered folders and files\nwill appear here.",
                                   objectName="hint", alignment=Qt.AlignTop | Qt.AlignLeft)
            self.tree.clicked.connect(self.on_pick)
            self.tree.doubleClicked.connect(self.on_double)
            self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
            self.tree.customContextMenuRequested.connect(self.tree_menu)
            self.native_worker = None

            self.search = QLineEdit(placeholderText="Search every recovered file (Enter)...")
            self.search.returnPressed.connect(self.do_search)
            self.results = QListWidget()
            self.results.itemActivated.connect(self.on_result)
            self.results.itemClicked.connect(self.on_result)
            self.results.hide()

            left = QWidget()
            lv = QVBoxLayout(left)
            lv.setContentsMargins(8, 4, 4, 8)
            lv.addWidget(QLabel("Files", objectName="panelTitle"))
            lv.addWidget(self.search)
            lv.addWidget(self.tree, 3)
            lv.addWidget(self.treehint, 3)
            lv.addWidget(self.results, 2)

            self.viewer = QTextBrowser()
            self.viewer.setOpenLinks(False)
            self.viewer.setFont(theme.mono_font(10))
            self.viewer.document().setDocumentMargin(12)
            # fast viewer for big files: QPlainTextEdit handles millions of lines
            self.bigviewer = QPlainTextEdit(readOnly=True)
            self.bigviewer.setFont(theme.mono_font(10))
            self.bigviewer.setLineWrapMode(QPlainTextEdit.NoWrap)
            # welcome screen shown before anything is opened
            self.welcome = WelcomeCanvas()
            wl = QVBoxLayout(self.welcome)
            wl.addStretch(2)
            big = QLabel("Decompiler Studio", objectName="biglogo", alignment=Qt.AlignCenter)
            big.setFont(title_font(72))
            bigglow = QGraphicsDropShadowEffect(big)
            bigglow.setColor(QColor(*theme.BIGGLOW, 150))
            self.big_glow = bigglow
            bigglow.setOffset(0, 0)
            bigglow.setBlurRadius(28)
            big.setGraphicsEffect(bigglow)
            self.welcome.glow_effect = bigglow
            wl.addWidget(big)
            sub = QLabel("Open an APK, JAR, EXE, ELF, DLL, .pyc or .asar, or drop it anywhere on this window.",
                         objectName="subtitle", alignment=Qt.AlignCenter)
            wl.addWidget(sub)
            wl.addSpacing(22)
            # drop zone: dashed box with the open button
            zone = QFrame(objectName="dropzone")
            zone.setFixedWidth(560)
            zl = QVBoxLayout(zone)
            zl.setContentsMargins(24, 22, 24, 22)
            zl.setSpacing(6)
            zi = QLabel("\u2193", objectName="dropicon", alignment=Qt.AlignCenter)
            zt = QLabel("Drop a file anywhere in this window", objectName="droptext", alignment=Qt.AlignCenter)
            zs = QLabel("or", objectName="dropsub", alignment=Qt.AlignCenter)
            ob = QPushButton("Open a file", objectName="primary", clicked=self.open_dialog)
            ob.setCursor(Qt.PointingHandCursor)
            ob.setMinimumWidth(190)
            ob.setMinimumHeight(38)
            for wdg in (zi, zt, zs):
                zl.addWidget(wdg)
            orow = QHBoxLayout()
            orow.addStretch(1)
            orow.addWidget(ob)
            orow.addStretch(1)
            zl.addLayout(orow)
            zrow = QHBoxLayout()
            zrow.addStretch(1)
            zrow.addWidget(zone)
            zrow.addStretch(1)
            wl.addLayout(zrow)
            wl.addSpacing(16)
            prow = QHBoxLayout()
            prow.addStretch(1)
            for ext in ("APK", "JAR", "EXE", "ELF", "DLL", "PYC", "ASAR"):
                prow.addWidget(QLabel(ext, objectName="pill"))
            prow.addStretch(1)
            wl.addLayout(prow)
            wl.addSpacing(26)
            frow = QHBoxLayout()
            frow.addStretch(1)
            for title, body in (("Native code \u2192 C", "Ghidra, tidied to read\nlike hand-written C"),
                                ("Android \u2192 Java", "jadx, plus a maincode\nfolder for the app's own code"),
                                ("Explain", "a small local AI model\nexplains code in plain English")):
                card = QFrame(objectName="featcard")
                cl2 = QVBoxLayout(card)
                cl2.setContentsMargins(16, 12, 16, 12)
                cl2.setSpacing(2)
                cl2.addWidget(QLabel(title, objectName="feattitle"))
                cl2.addWidget(QLabel(body, objectName="featbody"))
                card.setFixedWidth(238)
                frow.addWidget(card)
            frow.addStretch(1)
            frow.insertStretch(0, 1)
            wl.addLayout(frow)
            wl.addSpacing(22)
            warn = QLabel("WARNING: " + engines.NOTICE, objectName="warning", alignment=Qt.AlignCenter)
            wl.addWidget(warn)
            wl.addStretch(3)

            self.stack = QStackedWidget()
            self.stack.addWidget(self.welcome)
            self.stack.addWidget(self.viewer)
            self.stack.addWidget(self.bigviewer)
            self.working = WorkCanvas()
            self.stack.addWidget(self.working)
            ImageView, AudioView, ModelView, FontView, HexView = viewers.make_viewers(theme)
            self.imgview, self.audview, self.modview = ImageView(), AudioView(), ModelView()
            self.fontview, self.hexview = FontView(), HexView()
            self.media = {"image": self.imgview, "audio": self.audview, "model": self.modview, "font": self.fontview}
            for v in (self.imgview, self.audview, self.modview, self.fontview, self.hexview):
                self.stack.addWidget(v)
                v.status.connect(lambda m: self.statusBar().showMessage(m))
            self.history = []                  # (file, line) for Alt+Left
            self.viewer.viewport().installEventFilter(self)
            self.bigviewer.viewport().installEventFilter(self)
            for v in (self.viewer, self.bigviewer):
                v.setContextMenuPolicy(Qt.CustomContextMenu)
                v.customContextMenuRequested.connect(self.viewer_menu)
            self.pathlabel = QLabel("", objectName="pathlabel")
            right = QWidget()
            rv = QVBoxLayout(right)
            rv.setContentsMargins(0, 0, 0, 0)
            rv.setSpacing(0)
            rv.addWidget(self.pathlabel)
            self.pathlabel.hide()
            self.binbar = QWidget()
            bb = QHBoxLayout(self.binbar)
            bb.setContentsMargins(10, 8, 10, 8)
            self.binlabel = QLabel("This is a native binary. Turn it into C code:")
            self.btn_decomp = QPushButton("Decompile this binary", objectName="primary", clicked=self.decompile_current,
                                          toolTip="Run Ghidra on this file (Ctrl+D)")
            self.btn_alt = QPushButton("Hex view", clicked=self.toggle_view)
            self.btn_info = QPushButton("File info", clicked=self.file_info, toolTip="Type, size, MD5/SHA-1/SHA-256, entropy (Ctrl+I)")
            bb.addWidget(self.binlabel, 1)
            bb.addWidget(self.btn_alt)
            bb.addWidget(self.btn_info)
            bb.addWidget(self.btn_decomp)
            rv.addWidget(self.binbar)
            self.binbar.hide()
            self.oracle = QWidget(objectName="oracle")
            ov = QVBoxLayout(self.oracle)
            ov.setContentsMargins(8, 4, 8, 6)
            oh = QHBoxLayout()
            self.oracle_title = QLabel("Explain", objectName="panelTitle")
            oh.addWidget(self.oracle_title, 1)
            self.btn_oracle_stop = QPushButton("Stop", clicked=self.explain_stop)
            self.btn_oracle_close = QPushButton("Close", clicked=self.oracle.hide)
            oh.addWidget(self.btn_oracle_stop)
            oh.addWidget(self.btn_oracle_close)
            ov.addLayout(oh)
            self.oracle_text = QTextBrowser()
            self.oracle_text.setOpenLinks(False)
            self.oracle_text.document().setDocumentMargin(8)
            ov.addWidget(self.oracle_text)
            self.oracle.hide()
            self.explainer = None
            self.rsplit = QSplitter(Qt.Vertical)
            self.rsplit.addWidget(self.stack)
            self.rsplit.addWidget(self.oracle)
            self.rsplit.setSizes([600, 220])
            self.rsplit.setCollapsible(0, False)
            rv.addWidget(self.rsplit, 1)

            self.logbox = QPlainTextEdit(readOnly=True, objectName="log")
            self.logbox.setMaximumBlockCount(5000)
            self.logbox.setFont(theme.mono_font(9))
            self.btn_log.toggled.connect(self.logbox.setVisible)
            self.logbox.hide()

            top = QSplitter(Qt.Horizontal)
            top.addWidget(left)
            top.addWidget(right)
            top.setSizes([380, 920])
            vs = QSplitter(Qt.Vertical)
            vs.addWidget(top)
            vs.addWidget(self.logbox)
            vs.setSizes([640, 120])
            central = QWidget()
            cl = QVBoxLayout(central)
            cl.setContentsMargins(0, 0, 0, 0)
            cl.setSpacing(0)
            cl.addWidget(header)
            cl.addWidget(vs, 1)
            self.setCentralWidget(central)
            self.stack.setCurrentWidget(self.welcome)

            self.progress = QProgressBar()
            self.progress.setMaximumWidth(160)
            self.progress.setRange(0, 0)
            self.progress.hide()
            self.statusBar().addPermanentWidget(self.progress)
            self.statusBar().addPermanentWidget(QLabel(engines.NOTICE + "  ", objectName="warning"))
            self.statusBar().showMessage("Open a file to begin")

        # -------- drag and drop
        def dragEnterEvent(self, e):
            if e.mimeData().hasUrls():
                e.acceptProposedAction()

        def dropEvent(self, e):
            urls = e.mimeData().urls()
            if urls:
                self.load(urls[0].toLocalFile())

        # -------- actions
        def open_dialog(self):
            p, _ = QFileDialog.getOpenFileName(self, "Open a file to decompile")
            if p:
                self.load(p)

        def load(self, path):
            if self.worker and self.worker.isRunning():
                return
            out = os.path.join(self.work_root, Path(path).name + ".src")
            self.logbox.clear()
            self.logbox.appendPlainText(f"Opening {path}")
            self.statusBar().showMessage("Working...")
            self.progress.show()
            self.worker = Worker(path, out)
            self.worker.log.connect(self.logbox.appendPlainText)
            self.worker.log.connect(self.working.feed)
            self._prev_widget = self.stack.currentWidget()
            self.pathlabel.hide()
            self.binbar.hide()
            self.working.start(Path(path).name)
            self.stack.setCurrentWidget(self.working)
            self.worker.finished_ok.connect(self.done)
            self.worker.failed.connect(self.fail)
            self.worker.start()
            self.setWindowTitle(f"Decompiler Studio - {Path(path).name}")
            self.filelabel.setText(f"opening  {Path(path).name} ...")
            self.btn_log.setChecked(True)
            self.btn_open.setEnabled(False)
            self.btn_cancel.show()
            self.btn_cancel.setEnabled(True)

        def cancel(self):
            self.btn_cancel.setEnabled(False)
            self.statusBar().showMessage("Cancelling...")
            engines.cancel_all()

        def done(self, kind, out):
            self.progress.hide()
            self.btn_cancel.hide()
            self.btn_open.setEnabled(True)
            self.btn_export.setEnabled(True)
            self.btn_report.setEnabled((Path(out) / "REPORT.md").exists())
            self.filelabel.setText(f"{Path(self.worker.path).name}   ·   {engines.KIND_LABEL.get(kind, kind)}")
            self.out_dir = out
            self.model.setRootPath(out)
            self.treehint.hide()
            self.tree.show()
            self.tree.setRootIndex(self.model.index(out))
            self.statusBar().showMessage(f"{engines.KIND_LABEL.get(kind, kind)} done - source tree loaded (Report explains what was recovered)")
            self.working.finish(lambda: self._after_job(lambda: self.expand_first(out)))

        def _after_job(self, show):
            show()
            if self.stack.currentWidget() is self.working:      # nothing to open: go back
                self._leave_working()
            else:
                self._fade_in()

        def _fade_in(self):
            """Fade the result in after the work animation."""
            from PySide6.QtCore import QPropertyAnimation, QEasingCurve
            from PySide6.QtWidgets import QGraphicsOpacityEffect
            w = self.stack.currentWidget()
            if w is self.working:
                return
            eff = QGraphicsOpacityEffect(w)
            w.setGraphicsEffect(eff)
            an = QPropertyAnimation(eff, b"opacity", self)
            an.setDuration(320)
            an.setStartValue(0.0)
            an.setEndValue(1.0)
            an.setEasingCurve(QEasingCurve.OutCubic)
            an.finished.connect(lambda: w.setGraphicsEffect(None))
            self._fade_anim = an
            an.start()

        def _leave_working(self):
            """Back to what was shown before the job (a cancelled or failed run)."""
            self.working.stop()
            if self.stack.currentWidget() is self.working:
                prev = getattr(self, "_prev_widget", None)
                self.stack.setCurrentWidget(prev if prev is not None and prev is not self.working else self.welcome)

        def expand_first(self, out):
            # open the first source file automatically
            rep = Path(out) / "REPORT.md"
            for ext in ("*.java", "*.cs", "*.py", "decompiled.c", "*.c", "*.asm", "*.xml", "*.txt"):
                for f in sorted(Path(out).rglob(ext)):
                    self.show_file(str(f))
                    idx = self.model.index(str(f))
                    self.tree.scrollTo(idx)
                    self.tree.setCurrentIndex(idx)
                    return

        def fail(self, msg):
            self.progress.hide()
            self.btn_cancel.hide()
            self._leave_working()
            if msg == "Cancelled.":
                self.btn_open.setEnabled(True)
                self.filelabel.setText("cancelled")
                self.statusBar().showMessage("Cancelled")
                self.logbox.appendPlainText("Cancelled by user.")
                return
            self.btn_open.setEnabled(True)
            self.filelabel.setText("failed - see log")
            self.statusBar().showMessage("Failed")
            self.logbox.appendPlainText("ERROR: " + msg)
            QMessageBox.warning(self, "Decompiling failed", msg)

        def on_pick(self, idx):
            p = self.model.filePath(idx)
            if os.path.isfile(p):
                self.show_file(p)

        def _selected_binary(self, idx=None):
            idx = idx if idx is not None and idx.isValid() else self.tree.currentIndex()
            if not idx.isValid():
                return None
            p = self.model.filePath(idx)
            return p if os.path.isfile(p) and self._binary_kind(p) in ("native", "unity") else None

        def on_double(self, idx):
            p = self._selected_binary(idx)
            if p:
                self.decompile_binary(p, job=self._binary_kind(p))

        def decompile_current(self):
            cur = getattr(self, "cur_file", None)
            if not (cur and os.path.isfile(cur[0])):
                return
            kind = self._binary_kind(cur[0])
            if kind == "native":
                self.decompile_binary(cur[0])
            elif kind == "unity":
                self.decompile_binary(cur[0], job="unity")

        @staticmethod
        def _binary_kind(p):
            if engines.native_magic(p):
                return "native"
            try:
                import unity_assets
                if unity_assets.is_unity_file(p):
                    return "unity"
            except Exception:
                pass
            return "data"

        def _show_bar(self, p):
            kind = self._binary_kind(p)
            if kind == "native":
                self.binlabel.setText("Native code (.so / .exe / .dll). Turn it into C with Ghidra:")
                self.btn_decomp.setText("Decompile this binary")
                self.btn_decomp.show()
            elif kind == "unity":
                self.binlabel.setText("Unity asset file: the game's textures, models, sounds and level data (not code).")
                self.btn_decomp.setText("Extract assets")
                self.btn_decomp.show()
            else:
                self.binlabel.setText("Data file, not code: nothing to decompile. Click a byte to inspect it, "
                                      "click the entropy strip to jump.")
                self.btn_decomp.hide()
            mk = viewers.kind_of(p)
            self.btn_alt.setVisible(bool(mk))
            self.btn_alt.setText("Preview")
            self.btn_info.show()
            self.binbar.show()
            return kind

        def _media_bar(self, p, mk):
            self.binlabel.setText({
                "image": "Image: wheel to zoom, drag to pan, double-click for 1:1 / fit. Pixel colours show below.",
                "audio": "Sound: Play, or click the waveform to jump.",
                "model": "3D model: drag to rotate, wheel to zoom, double-click for wireframe.",
                "font": "Font preview."}[mk])
            self.btn_decomp.hide()
            self.btn_alt.setText("View as text" if mk == "model" else "Hex view")
            self.btn_alt.show()
            self.btn_info.show()
            self.binbar.show()

        def toggle_view(self):
            cur = getattr(self, "cur_file", None)
            if not cur:
                return
            w = self.stack.currentWidget()
            if w in self.media.values():
                self.show_file(cur[0], as_="text" if w is self.modview else "hex")
            else:
                self.show_file(cur[0])

        def file_info(self):
            cur = getattr(self, "cur_file", None)
            if not cur or not os.path.isfile(cur[0]):
                self.statusBar().showMessage("Open a file first")
                return
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                rows = viewers.facts(cur[0])
            finally:
                QApplication.restoreOverrideCursor()
            text = Path(cur[0]).name + "\n\n" + "\n".join(f"{k + ':':<9} {v}" for k, v in rows)
            box = QMessageBox(self)
            box.setWindowTitle("File info")
            box.setText(f"<pre style='font-family:monospace'>{html.escape(text)}</pre>")
            box.setTextInteractionFlags(Qt.TextSelectableByMouse)
            copy = box.addButton("Copy", QMessageBox.ActionRole)
            box.addButton(QMessageBox.Close)
            box.exec()
            if box.clickedButton() is copy:
                QApplication.clipboard().setText(text)
                self.statusBar().showMessage("File info copied")

        # -------- navigation: function list, go to definition, back
        def _func_index(self, p):
            """[(name, addr, file, line)] for the current file, or for every part of a split decompile."""
            import re as _re
            p = Path(p)
            files = sorted(p.parent.glob("part_*.c")) if p.parent.name == "decompiled" else [p]
            if p.name == "decompiled.c" and (p.parent / "decompiled").is_dir():
                files = sorted((p.parent / "decompiled").glob("part_*.c"))
            key = tuple((str(f), f.stat().st_mtime) for f in files if f.exists())
            cache = getattr(self, "_fcache", {})
            if key in cache:
                return cache[key]
            c_re = _re.compile(r"^// ---- (.+?) @ ([0-9a-fA-F]+) ----$")
            asm_re = _re.compile(r"^; (\S+)\s+@ (0x[0-9a-fA-F]+)")
            j_re = _re.compile(r"^\s*(?:(?:public|private|protected|static|final|synchronized|abstract|native|override|"
                               r"internal|virtual|async|unsafe)\s+)*(?:[\w<>\[\],.?]+\s+)+(\w+)\s*\([^;]*\)\s*(?:throws [\w., ]+)?\{?\s*$")
            cls_re = _re.compile(r"^\s*(?:public |private |protected |static |final |abstract |sealed |internal )*"
                                 r"(?:class|interface|enum|struct|record)\s+(\w+)")
            out = []
            for f in files:
                try:
                    with open(f, "r", encoding="utf-8", errors="replace") as fh:
                        for i, line in enumerate(fh, 1):
                            if not line or line[0] not in "/; \tpubliprosfaintevcd":
                                continue
                            m = c_re.match(line) or asm_re.match(line)
                            if m:
                                out.append((m.group(1), m.group(2), str(f), i))
                                continue
                            if f.suffix in (".java", ".cs", ".kt"):
                                m = cls_re.match(line)
                                if m:
                                    out.append((m.group(1), "class", str(f), i))
                                    continue
                                m = j_re.match(line)
                                if m and m.group(1) not in ("if", "for", "while", "switch", "catch", "return", "new", "synchronized"):
                                    out.append((m.group(1), "", str(f), i))
                except OSError:
                    pass
            cache[key] = out
            self._fcache = cache
            return out

        def _jump(self, f, line):
            cur = getattr(self, "cur_file", None)
            if cur:
                w = self.stack.currentWidget()
                at = w.textCursor().blockNumber() + 1 if hasattr(w, "textCursor") else cur[1]
                self.history.append((cur[0], at))
            if not cur or cur[0] != f or self.stack.currentWidget() not in (self.viewer, self.bigviewer):
                self.show_file(f)
                idx = self.model.index(f)
                self.tree.scrollTo(idx)
                self.tree.setCurrentIndex(idx)
            self.goto_line(line)

        def go_back(self):
            if not self.history:
                self.statusBar().showMessage("Nothing to go back to")
                return
            f, line = self.history.pop()
            self.show_file(f)
            if line:
                self.goto_line(line)

        def eventFilter(self, obj, ev):
            # double-click (or Ctrl+click) a function name in the code: jump to where it is defined
            from PySide6.QtCore import QEvent, QTimer
            if obj in (self.viewer.viewport(), self.bigviewer.viewport()):
                t = ev.type()
                if t == QEvent.MouseButtonDblClick or (t == QEvent.MouseButtonRelease and ev.modifiers() & Qt.ControlModifier):
                    QTimer.singleShot(0, lambda: self.goto_definition(quiet=True))
            return super().eventFilter(obj, ev)

        def goto_definition(self, word=None, quiet=False):
            from PySide6.QtGui import QTextCursor
            cur = getattr(self, "cur_file", None)
            w = self.stack.currentWidget()
            if not cur or not hasattr(w, "textCursor"):
                return
            if not word:
                c = w.textCursor()
                c.select(QTextCursor.WordUnderCursor)
                word = c.selectedText().strip()
            if not word:
                return
            hits = [e for e in self._func_index(cur[0]) if e[0] == word]
            if not hits and word.startswith(("FUN_", "sub_")):
                addr = word.split("_", 1)[1].lower().lstrip("0")
                hits = [e for e in self._func_index(cur[0]) if e[1].lower().lstrip("0") == addr]
            if not hits:
                if not quiet:
                    self.statusBar().showMessage(f"No definition of {word} found here")
                return
            name, addr, f, line = hits[0]
            self._jump(f, line)
            self.statusBar().showMessage(f"{name}" + (f" @ {addr}" if addr else "") + "   (Alt+Left to go back)")

        def _word_at_cursor(self):
            from PySide6.QtGui import QTextCursor
            w = self.stack.currentWidget()
            if not hasattr(w, "textCursor"):
                return ""
            c = w.textCursor()
            if not c.hasSelection():
                c.select(QTextCursor.WordUnderCursor)
            return c.selectedText().strip()

        def who_calls(self, name=None):
            """List every place that calls `name` (or the word under the cursor) with the function it is in."""
            import re as _re
            cur = getattr(self, "cur_file", None)
            name = name or self._word_at_cursor()
            if not cur or not name or not _re.match(r"^[A-Za-z_$][\w$]*$", name):
                self.statusBar().showMessage("Put the cursor on a function name first")
                return
            p = Path(cur[0])
            # where to look: all parts of a split decompile, the whole sources/ tree for Java, else this file
            if p.parent.name == "decompiled" or (p.name == "decompiled.c" and (p.parent / "decompiled").is_dir()):
                files = self._func_files(p)
            elif p.suffix in (".java", ".kt", ".cs") and self.out_dir:
                files = [f for f in Path(self.out_dir).rglob("*" + p.suffix) if f.stat().st_size < 5_000_000]
            else:
                files = [p]
            call = _re.compile(rf"(?<![\w$]){_re.escape(name)}\s*\(")
            defs = {}
            for e in self._func_index(cur[0]):
                defs.setdefault(e[2], []).append((e[3], e[0]))
            QApplication.setOverrideCursor(Qt.WaitCursor)
            hits = []
            try:
                for f in files:
                    try:
                        text = f.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
                    if name not in text:
                        continue
                    flines = text.split("\n")
                    fdefs = defs.get(str(f))
                    if fdefs is None and f.suffix in (".java", ".kt", ".cs"):
                        fdefs = [(e[3], e[0]) for e in self._func_index(str(f))]
                    fdefs = sorted(fdefs or [])
                    for i, line in enumerate(flines, 1):
                        if not call.search(line):
                            continue
                        st = line.strip()
                        if st.startswith(("//", "*", "/*")):
                            continue
                        caller = ""
                        for dl, dn in fdefs:
                            if dl <= i:
                                caller = dn
                            else:
                                break
                        # a declaration or definition ("void foo(", "public int foo(", "undefined8 *FUN_x(") is not a call
                        m = call.search(line)
                        prefix = line[:m.start()].strip()
                        if prefix and _re.fullmatch(r"[\w<>\[\],?@*\s]+", prefix) \
                                and prefix.split()[-1] not in ("return", "new", "await", "throw", "else", "case", "yield", "do"):
                            continue
                        hits.append((str(f), i, caller, st[:110]))
                        if len(hits) >= 1000:
                            break
                    if len(hits) >= 1000:
                        break
            finally:
                QApplication.restoreOverrideCursor()
            self.results.clear()
            self.results.show()
            head = QListWidgetItem(f"Who calls {name}: {len(hits)}{'+' if len(hits) >= 1000 else ''} call site(s)")
            head.setFlags(Qt.ItemIsEnabled)
            self.results.addItem(head)
            for f, i, caller, st in hits:
                rel = os.path.relpath(f, self.out_dir) if self.out_dir else f
                it = QListWidgetItem(f"{caller or '?'}  ->  {name}    {Path(rel).name}:{i}   {st}")
                it.setToolTip(f"{rel}:{i}")
                it.setData(Qt.UserRole, (f, i))
                self.results.addItem(it)
            self.statusBar().showMessage(f"{len(hits)} call site(s) of {name}" + ("" if hits else
                                         "  (nothing calls it directly; it may be called through a pointer, a table or reflection)"))

        def _func_files(self, p):
            p = Path(p)
            if p.name == "decompiled.c" and (p.parent / "decompiled").is_dir():
                return sorted((p.parent / "decompiled").glob("part_*.c"))
            return sorted(p.parent.glob("part_*.c"))

        def viewer_menu(self, pos):
            from PySide6.QtGui import QTextCursor
            w = self.stack.currentWidget()
            if not hasattr(w, "cursorForPosition"):
                return
            c = w.cursorForPosition(pos)
            if not w.textCursor().hasSelection() or not w.textCursor().selectedText().strip():
                c.select(QTextCursor.WordUnderCursor)
                w.setTextCursor(c)
            word = self._word_at_cursor()
            menu = w.createStandardContextMenu(pos) if hasattr(w, "createStandardContextMenu") else None
            from PySide6.QtWidgets import QMenu
            menu = menu or QMenu(self)
            first = menu.actions()[0] if menu.actions() else None
            acts = []
            if word and len(word) < 120 and " " not in word:
                acts = [("Who calls %s?  (Shift+F12)" % word, lambda: self.who_calls(word)),
                        ("Go to definition of %s  (F12)" % word, lambda: self.goto_definition(word))]
            acts += [("Explain this  (Ctrl+E)", self.explain), ("Check for malware  (Ctrl+Shift+M)", self.check_malware)]
            for text, fn in acts:
                a = QAction(text, menu)
                a.triggered.connect(fn)
                menu.insertAction(first, a) if first else menu.addAction(a)
            if first:
                menu.insertSeparator(first)
            menu.exec(w.viewport().mapToGlobal(pos))

        def goto_dialog(self):
            from PySide6.QtWidgets import QDialog, QInputDialog
            cur = getattr(self, "cur_file", None)
            if not cur:
                self.statusBar().showMessage("Open a file first")
                return
            if self.stack.currentWidget() is self.hexview:
                text, ok = QInputDialog.getText(self, "Go to offset", "Offset (hex like 0x1f00, or decimal):")
                if ok and text.strip():
                    try:
                        t = text.strip().lower()
                        off = int(t, 16) if t.startswith("0x") or any(ch in "abcdef" for ch in t) else int(t)
                        self.hexview.goto(off)
                    except ValueError:
                        self.statusBar().showMessage("Not a number: " + text)
                return
            funcs = self._func_index(cur[0])
            if not funcs:
                self.statusBar().showMessage("No functions found in this file")
                return
            dlg = QDialog(self)
            dlg.setWindowTitle(f"Go to function  ({len(funcs):,})")
            dlg.resize(560, 520)
            lay = QVBoxLayout(dlg)
            flt = QLineEdit(placeholderText="Type to filter by name or address...")
            lst = QListWidget()
            lay.addWidget(flt)
            lay.addWidget(lst)

            def fill(q=""):
                lst.clear()
                ql = q.lower()
                n = 0
                for e in funcs:
                    if ql in e[0].lower() or (e[1] and ql in e[1].lower()):
                        it = QListWidgetItem(f"{e[0]}" + (f"    {e[1]}" if e[1] else "") +
                                             (f"    ({Path(e[2]).name})" if Path(e[2]).name != Path(cur[0]).name else ""))
                        it.setData(Qt.UserRole, e)
                        lst.addItem(it)
                        n += 1
                        if n >= 3000:
                            break
                if lst.count():
                    lst.setCurrentRow(0)

            def go(*_):
                it = lst.currentItem()
                if it:
                    e = it.data(Qt.UserRole)
                    dlg.accept()
                    self._jump(e[2], e[3])

            flt.textChanged.connect(fill)
            flt.returnPressed.connect(go)
            lst.itemActivated.connect(go)
            fill()
            dlg.exec()

        def decompile_selected(self):
            p = self._selected_binary()
            if p:
                self.decompile_binary(p, job=self._binary_kind(p))
            else:
                self.statusBar().showMessage("Select a native library (.so, .dll, .exe) or a Unity asset file in the file tree first")

        def tree_menu(self, pos):
            from PySide6.QtWidgets import QMenu
            idx = self.tree.indexAt(pos)
            p = self._selected_binary(idx)
            if not p:
                return
            menu = QMenu(self)
            if self._binary_kind(p) == "unity":
                menu.addAction("Extract assets (images, sounds, models)", lambda: self.decompile_binary(p, job="unity"))
            else:
                menu.addAction("Decompile this binary (Ghidra)", lambda: self.decompile_binary(p))
            menu.exec(self.tree.viewport().mapToGlobal(pos))

        def decompile_binary(self, path, job="native"):
            if (self.worker and self.worker.isRunning()) or (self.native_worker and self.native_worker.isRunning()):
                self.statusBar().showMessage("Wait for the current job to finish (or press Cancel)")
                return
            self.logbox.appendPlainText("")
            self.statusBar().showMessage(("Extracting assets from " if job == "unity" else "Decompiling ") + Path(path).name + "...")
            self.progress.show()
            self.btn_cancel.show()
            self.btn_cancel.setEnabled(True)
            self.btn_log.setChecked(True)
            self.btn_open.setEnabled(False)
            self.native_worker = NativeWorker(path, self.out_dir, job)
            self.native_worker.log.connect(self.logbox.appendPlainText)
            self.native_worker.log.connect(self.working.feed)
            self._prev_widget = self.stack.currentWidget()
            self.working.start(Path(path).name, "Extracting assets from" if job == "unity" else "Decompiling")
            self.binbar.hide()
            self.stack.setCurrentWidget(self.working)
            self.native_worker.finished_ok.connect(self.native_done)
            self.native_worker.failed.connect(self.native_fail)
            self.native_worker.start()

        def _native_idle(self):
            self.progress.hide()
            self.btn_cancel.hide()
            self.btn_open.setEnabled(True)

        def native_done(self, out):
            self._native_idle()
            self.working.finish(lambda: self._after_job(lambda: self._native_show(out)))

        def _native_show(self, out):
            unity = Path(out).parent.name == "unity_assets"
            self.statusBar().showMessage("Assets extracted - see the unity_assets folder in the file tree" if unity
                                         else "Binary decompiled - see the native folder in the file tree")
            c = Path(out) / ("index.txt" if unity else "decompiled.c")
            target = c if c.exists() else next(iter(sorted(Path(out).rglob("*.asm"))), None) or next(iter(sorted(Path(out).glob("*.txt"))), None)
            if target:
                self.show_file(str(target))
                idx = self.model.index(str(target))
                self.tree.scrollTo(idx)
                self.tree.setCurrentIndex(idx)

        def native_fail(self, msg):
            self._native_idle()
            self._leave_working()
            self.logbox.appendPlainText("ERROR: " + msg if msg != "Cancelled." else "Cancelled by user.")
            self.statusBar().showMessage(msg)

        def show_report(self):
            if self.out_dir and (Path(self.out_dir) / "REPORT.md").exists():
                self.show_file(str(Path(self.out_dir) / "REPORT.md"))

        # -------- Explain (explain code with a small local model)
        def _llm_settings(self):
            from PySide6.QtCore import QSettings
            url, model = llm.settings()
            try:
                q = QSettings("DecompilerStudio", "DecompilerStudio")
                url = str(q.value("llm_url", url)) or llm.DEFAULT_URL
                model = str(q.value("llm_model", model))
            except Exception:
                pass
            return url, model

        def _save_llm(self, key, value):
            try:
                from PySide6.QtCore import QSettings
                QSettings("DecompilerStudio", "DecompilerStudio").setValue(key, value)
            except Exception:
                pass

        def install_ai(self):
            from PySide6.QtWidgets import QInputDialog
            if getattr(self, "ai_worker", None) and self.ai_worker.isRunning():
                return
            rec = llm.recommended_model()
            choices = [f"{llm.BIG_MODEL}   (about 9 GB, best; needs a 12 GB+ graphics card or lots of RAM)",
                       f"{llm.SMALL_MODEL}   (about 4.7 GB, good; runs on most computers)",
                       "qwen2.5-coder:3b   (about 1.9 GB, fast but rough)"]
            cur = 0 if rec == llm.BIG_MODEL else 1
            item, ok = QInputDialog.getItem(self, "Install AI model",
                                            "Model for Explain and Malware check. It is downloaded once and runs only on "
                                            "this computer.\nRecommended for this computer: " + rec, choices, cur, False)
            if not ok:
                return
            model = item.split()[0]
            engines.CANCEL.clear()
            self.btn_log.setChecked(True)
            self.progress.show()
            self.btn_cancel.show()
            self.btn_cancel.setEnabled(True)
            self.statusBar().showMessage(f"Installing {model}...")
            self.ai_worker = AiInstallWorker(model)
            self.ai_worker.log.connect(self.logbox.appendPlainText)
            self.ai_worker.log.connect(lambda m: self.statusBar().showMessage(m))
            self.ai_worker.finished_ok.connect(self._ai_installed)
            self.ai_worker.failed.connect(self._ai_failed)
            self.ai_worker.start()

        def _ai_installed(self, model):
            self.progress.hide()
            self.btn_cancel.hide()
            self._save_llm("llm_model", model)
            self.statusBar().showMessage(f"AI ready: {model}")
            QMessageBox.information(self, "Install AI model", f"{model} is installed. Explain (Ctrl+E) and Malware check "
                                                             "(Ctrl+Shift+M) now use it.")

        def _ai_failed(self, msg):
            self.progress.hide()
            self.btn_cancel.hide()
            self.logbox.appendPlainText("AI install: " + msg)
            if msg != "Cancelled.":
                QMessageBox.warning(self, "Install AI model", msg)

        def explain_help(self):
            self.oracle.show()
            self.oracle_title.setText("Explain: setup")
            self.oracle_text.setPlainText(llm.HELP + "\n\nThe code you explain is sent only to the address in Server address "
                                          "(default: this computer).")

        def pick_model(self):
            from PySide6.QtWidgets import QInputDialog
            url, model = self._llm_settings()
            if url.rstrip("/") == llm.DEFAULT_URL:
                llm.start_server()
            try:
                names = [n for n, _ in llm.list_models(url)]
            except llm.LlmError as e:
                self.explain_help() if str(e) == "no_server" else QMessageBox.warning(self, "Explain", str(e))
                return
            if not names:
                self.explain_help()
                return
            cur = names.index(model) if model in names else 0
            name, ok = QInputDialog.getItem(self, "Explain", "Model to explain code with:", names, cur, False)
            if ok:
                self._save_llm("llm_model", name)
                self.statusBar().showMessage(f"Explain model: {name}")

        def pick_server(self):
            from PySide6.QtWidgets import QInputDialog
            url, _m = self._llm_settings()
            text, ok = QInputDialog.getText(self, "Explain", "Model server address (Ollama, or an OpenAI-style address ending in /v1):",
                                            text=url)
            if ok and text.strip():
                self._save_llm("llm_url", text.strip())
                self._save_llm("llm_model", "")
                self.statusBar().showMessage(f"Explain server: {text.strip()}")

        def check_malware(self):
            self.explain(mode="check")

        def explain(self, mode="explain"):
            if self.explainer and self.explainer.isRunning():
                return
            title = "Malware check" if mode == "check" else "Explain"
            w = self.stack.currentWidget()
            if w is self.welcome or not hasattr(w, "toPlainText"):
                self.statusBar().showMessage("Open a recovered file first, then select some code and press Explain")
                return
            cur = w.textCursor()
            sel = cur.selectedText().replace("\u2029", "\n").strip()
            fname = (getattr(self, "cur_file", None) or ("selection",))[0]
            if sel:
                code, label = sel, "the selected code"
            else:
                from PySide6.QtCore import QPoint
                c2 = cur if cur.position() > 0 else w.cursorForPosition(QPoint(8, 8))
                code, label = llm.pick_chunk(w.toPlainText(), c2.blockNumber())
            if not code.strip():
                return
            url, model = self._llm_settings()
            self.oracle.show()
            sizes = self.rsplit.sizes()
            if len(sizes) >= 2 and sum(sizes) > 0 and sizes[1] < sum(sizes) * 0.36:
                total = sum(sizes)
                self.rsplit.setSizes([int(total * 0.55), int(total * 0.45)] + sizes[2:])
            self.oracle_title.setText(f"{title}: {label}")
            self.oracle_text.setPlainText(("Checking..." if mode == "check" else "Thinking...")
                                          + ("" if llm.is_local(url) else f"\n(the code is being sent to {url})"))
            self._oracle_buf = ""
            self._oracle_mode = mode
            self.btn_explain.setEnabled(False)
            self.btn_check.setEnabled(False)
            self.btn_oracle_stop.setEnabled(True)
            self.explainer = ExplainWorker(code, fname, label, url, model, mode)
            self.explainer.model.connect(lambda m: self.oracle_title.setText(f"{title}: {label}  ({m})"))
            self.explainer.piece.connect(self._oracle_piece)
            self.explainer.finished_ok.connect(self._oracle_done)
            self.explainer.failed.connect(self._oracle_failed)
            self.explainer.start()

        @staticmethod
        def _no_think(t):
            import re as _re
            return _re.sub(r"(?s)<think>.*?(</think>|$)", "", t).lstrip()

        def _oracle_piece(self, t):
            self._oracle_buf += t
            self.oracle_text.setPlainText(self._no_think(self._oracle_buf) or "Thinking...")
            sb = self.oracle_text.verticalScrollBar()
            sb.setValue(sb.maximum())

        def _oracle_done(self):
            self._oracle_buf = self._no_think(self._oracle_buf)
            self.btn_explain.setEnabled(True)
            self.btn_check.setEnabled(True)
            self.btn_oracle_stop.setEnabled(False)
            if not self._oracle_buf.strip():
                return
            if getattr(self, "_oracle_mode", "explain") == "check":
                import re as _re
                m = _re.search(r"verdict\W*\s*(clean|suspicious|likely malicious|malicious)", self._oracle_buf, _re.I)
                v = (m.group(1).lower() if m else "")
                color = {"clean": "#22c55e", "suspicious": "#e3a008"}.get(v, "#ef4444" if v else theme.DIM)
                head = (f"<div style='font-size:18px;font-weight:800;color:{color}'>{html.escape(m.group(1).title())}</div>"
                        if m else "")
                body = _re.sub(r"(?im)^\s*\**verdict\**\W*.*$", "", self._oracle_buf, count=1)
                self.oracle_text.setMarkdown(body + "\n\n*AI check, not a virus scanner: a small model can miss things "
                                             "or flag safe code. It only saw this part of the app.*")
                if head:
                    from PySide6.QtGui import QTextCursor
                    c = QTextCursor(self.oracle_text.document())
                    c.movePosition(QTextCursor.Start)
                    c.insertHtml(head)
                    c.insertBlock()
                if v:
                    self.statusBar().showMessage(f"Malware check: {m.group(1).title()}")
            else:
                self.oracle_text.setMarkdown(self._oracle_buf + "\n\n*AI explanation: a small model can be wrong. Check it against the code.*")

        def _oracle_failed(self, msg):
            self.btn_explain.setEnabled(True)
            self.btn_check.setEnabled(True)
            self.btn_oracle_stop.setEnabled(False)
            self.oracle_title.setText("Explain")
            self.oracle_text.setPlainText(msg)

        def explain_stop(self):
            if self.explainer and self.explainer.isRunning():
                self.explainer.stop()

        def set_theme(self, name):
            """Switch palette, stylesheet, glows, icons and the open file's colours; remember the choice."""
            name = theme.apply(name)
            app_ = QApplication.instance()
            app_.setStyleSheet(theme.QSS)
            app_.setWindowIcon(theme.app_icon())
            self.logo_glow.setColor(QColor(*theme.GLOW, 120))
            self.big_glow.setColor(QColor(*theme.BIGGLOW, 150))
            self.model.setIconProvider(IconProvider())      # folder icons are tinted per theme
            for act in self.theme_group.actions():
                act.setChecked(act.text().split("  ")[0] == name)
            for w_ in app_.allWidgets():
                w_.update()
            cur = getattr(self, "cur_file", None)
            if cur and os.path.isfile(cur[0]) and self.stack.currentWidget() is self.viewer:
                self.show_file(cur[0], cur[1])
            try:
                from PySide6.QtCore import QSettings
                QSettings("DecompilerStudio", "DecompilerStudio").setValue("theme", name)
            except Exception:
                pass
            self.statusBar().showMessage(f"Theme: {name}")

        def show_binary_hit(self, p, off, n):
            """A search hit inside a binary: open the hex view at the match, say which section it is in."""
            self.cur_file = (p, None)
            self.pathlabel.show()
            self._show_bar(p)
            rel = os.path.relpath(p, self.out_dir) if self.out_dir else p
            self.pathlabel.setText(f"{rel}  @ 0x{off:x}")
            where = ""
            try:
                from elftools.elf.elffile import ELFFile
                with open(p, "rb") as f:
                    for sec in ELFFile(f).iter_sections():
                        a, sz = sec["sh_offset"], sec["sh_size"]
                        if sec["sh_type"] != "SHT_NOBITS" and a <= off < a + sz:
                            where = f" in section {sec.name}"
                            break
            except Exception:
                pass
            with open(p, "rb") as f:
                head = f.read(64)
            self.hexview.load(p, header=f"{viewers.describe(head, p)}   ·   match at 0x{off:x}{where}")
            self.stack.setCurrentWidget(self.hexview)
            self.hexview.goto(off, n)
            self.hexview.setFocus()

        def show_file(self, p, line=None, as_=None):
            if isinstance(line, tuple) and line and line[0] == "bin":
                self.show_binary_hit(p, line[1], line[2])
                return
            self.cur_file = (p, line)
            self.binbar.hide()
            mk = viewers.kind_of(p) if as_ is None else None
            if mk:
                try:
                    ok = self.media[mk].load(p)
                except Exception as e:
                    ok = False
                    self.statusBar().showMessage(f"Preview failed: {e}")
                if ok:
                    self.pathlabel.show()
                    self.pathlabel.setText(os.path.relpath(p, self.out_dir) if self.out_dir else p)
                    self._media_bar(p, mk)
                    self.stack.setCurrentWidget(self.media[mk])
                    return
            self.pathlabel.show()
            rel = os.path.relpath(p, self.out_dir) if self.out_dir else p
            try:
                size = os.path.getsize(p)
                with open(p, "rb") as f:
                    head = f.read(4096)
            except OSError as e:
                self.stack.setCurrentWidget(self.viewer)
                self.viewer.setPlainText(str(e))
                return
            if as_ == "hex" or (as_ != "text" and _looks_binary(head)):
                self.pathlabel.setText(rel)
                self._show_bar(p)
                self.hexview.load(p, header=f"{viewers.describe(head[:64], p)}   ·   {size:,} bytes")
                self.stack.setCurrentWidget(self.hexview)
                self.hexview.setFocus()
                return
            if size > HIGHLIGHT_LIMIT:
                self.pathlabel.setText(f"{rel}   ({size / 1e6:.1f} MB, syntax colors off for speed)")
                self.statusBar().showMessage(f"Loading {size / 1e6:.1f} MB...")
                QApplication.setOverrideCursor(Qt.WaitCursor)
                try:
                    with open(p, "r", encoding="utf-8", errors="replace") as f:
                        self.bigviewer.setPlainText(f.read())
                finally:
                    QApplication.restoreOverrideCursor()
                self.stack.setCurrentWidget(self.bigviewer)
                self.statusBar().showMessage(f"{rel}: {self.bigviewer.blockCount():,} lines")
            else:
                self.pathlabel.setText(rel)
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
                doc = self.viewer.document()
                if p.lower().endswith(".md"):
                    from PySide6.QtGui import QTextCursor, QTextCharFormat, QColor
                    f = QApplication.font()
                    f.setPointSize(11)
                    doc.setDefaultFont(f)
                    self.viewer.setMarkdown(text)
                    blk = doc.begin()
                    while blk.isValid():
                        if blk.blockFormat().headingLevel() > 0:
                            cur = QTextCursor(blk)
                            cur.select(QTextCursor.BlockUnderCursor)
                            fmt = QTextCharFormat()
                            fmt.setForeground(QColor(theme.RED))
                            cur.mergeCharFormat(fmt)
                        blk = blk.next()
                else:
                    doc.setDefaultFont(theme.mono_font(10))
                    try:
                        lexer = get_lexer_for_filename(p)
                    except ClassNotFound:
                        lexer = TextLexer()
                    body = highlight(text, lexer, theme.formatter())
                    self.viewer.setHtml(f"<body>{body}</body>")
                self.stack.setCurrentWidget(self.viewer)
            if line:
                self.goto_line(line)

        def goto_line(self, n):
            from PySide6.QtGui import QTextCursor
            w = self.stack.currentWidget()
            blk = w.document().findBlockByNumber(max(0, n - 1))
            cur = QTextCursor(blk)
            cur.select(QTextCursor.LineUnderCursor)
            w.setTextCursor(cur)
            w.ensureCursorVisible()

        # -------- search
        def do_search(self):
            q = self.search.text().strip()
            if not q or not self.out_dir:
                return
            self.results.clear()
            self.results.show()
            ql = q.lower()
            count = 0
            for f in Path(self.out_dir).rglob("*"):
                if not f.is_file() or f.stat().st_size > SEARCH_LIMIT:
                    continue
                try:
                    with open(f, "r", encoding="utf-8", errors="strict") as fh:
                        for i, line in enumerate(fh, 1):
                            if ql in line.lower():
                                it = QListWidgetItem(f"{f.relative_to(self.out_dir)}:{i}  {line.strip()[:90]}")
                                it.setData(Qt.UserRole, (str(f), i))
                                self.results.addItem(it)
                                count += 1
                                if count >= 500:
                                    break
                except UnicodeDecodeError:
                    # not text (a library, dex, resources.arsc ...): look for the words inside the bytes
                    for off, ctx in self._binary_hits(f, ql):
                        it = QListWidgetItem(f"{f.relative_to(self.out_dir)} @0x{off:x} (binary)  {ctx}")
                        it.setData(Qt.UserRole, (str(f), ("bin", off, len(q))))
                        self.results.addItem(it)
                        count += 1
                        if count >= 500:
                            break
                except OSError:
                    continue
                if count >= 500:
                    break
            self.statusBar().showMessage(f"{count} match(es)" + (" (showing first 500)" if count >= 500 else ""))

        @staticmethod
        def _binary_hits(f, ql, limit=5):
            """Case-insensitive hits of `ql` inside a binary file, as plain text and as UTF-16: [(offset, context)]."""
            if f.stat().st_size > 400_000_000:
                return []
            data = f.read_bytes()
            low = data.lower()
            out = []
            for needle in (ql.encode("utf-8"), ql.encode("utf-16-le")):
                start = 0
                while len(out) < limit:
                    i = low.find(needle, start)
                    if i < 0:
                        break
                    chunk = data[max(0, i - 30): i + len(needle) + 50]
                    if needle.endswith(b"\x00") or b"\x00" in needle:
                        chunk = chunk.replace(b"\x00", b"")
                    text = "".join(chr(c) if 32 <= c < 127 else " " for c in chunk)
                    out.append((i, " ".join(text.split())[:100]))
                    start = i + len(needle)
            return out

        def on_result(self, item):
            p, line = item.data(Qt.UserRole)
            self.show_file(p, line)

        def export(self):
            if not self.out_dir:
                return
            dest = QFileDialog.getExistingDirectory(self, "Export recovered source into...")
            if not dest:
                return
            import shutil
            target = os.path.join(dest, Path(self.out_dir).name)
            shutil.copytree(self.out_dir, target, dirs_exist_ok=True)
            self.statusBar().showMessage(f"Exported to {target}")

        def closeEvent(self, e):
            engines.cancel_all()
            try:
                llm.stop_server()
            except Exception:
                pass
            if self.explainer and self.explainer.isRunning():
                self.explainer.stop()
                self.explainer.wait(2000)
            import shutil
            shutil.rmtree(self.work_root, ignore_errors=True)
            super().closeEvent(e)

    app = QApplication(sys.argv)
    app.setApplicationName("Decompiler Studio")
    app.setStyle("Fusion")
    try:
        from PySide6.QtCore import QSettings
        theme.apply(str(QSettings("DecompilerStudio", "DecompilerStudio").value("theme", theme.DEFAULT_THEME)))
    except Exception:
        theme.apply(theme.DEFAULT_THEME)
    app.setStyleSheet(theme.QSS)
    app.setWindowIcon(theme.app_icon())
    
    w = Win()
    w.show()
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        w.load(sys.argv[1])
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
