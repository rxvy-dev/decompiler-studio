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

    class NativeWorker(QThread):
        log = Signal(str)
        finished_ok = Signal(str)
        failed = Signal(str)

        def __init__(self, path, root):
            super().__init__()
            self.path, self.root = path, root

        def run(self):
            try:
                out = engines.decompile_embedded(self.path, self.root, self.log.emit)
                self.finished_ok.emit(str(out))
            except engines.Cancelled:
                self.failed.emit("Cancelled.")
            except Exception as e:
                self.failed.emit(str(e))

    HeaderBar, WelcomeCanvas = theme.make_widgets()
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

        def __init__(self, code, filename, label, url, model):
            super().__init__()
            self.args = (code, filename, label, url, model)
            self._stop = False

        def stop(self):
            self._stop = True

        def run(self):
            try:
                for kind, text in llm.stream_explain(*self.args, should_stop=lambda: self._stop):
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

            em = bar.addMenu("&Explain")
            em.addAction(QAction("&Explain selection / function", self, shortcut="Ctrl+E", triggered=self.explain))
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
            self.btn_explain = QPushButton("Explain", clicked=self.explain,
                                           toolTip="Explain the selected code (or the function you are looking at) in plain "
                                                   "English with a small local AI model (Ctrl+E)")
            self.btn_cancel = QPushButton("Cancel", clicked=self.cancel,
                                          toolTip="Cancel: stop the decompile and every helper it started")
            self.btn_cancel.hide()
            for b in (self.btn_cancel, self.btn_report, self.btn_export, self.btn_explain, self.btn_log, self.btn_open):
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
            bb.addWidget(self.binlabel, 1)
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
            self.expand_first(out)

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
            if msg == "Cancelled.":
                self.btn_open.setEnabled(True)
                self.filelabel.setText("cancelled")
                self.statusBar().showMessage("Cancelled")
                self.logbox.appendPlainText("Cancelled by user.")
                return
            self.btn_open.setEnabled(True)
            self.filelabel.setText("the ritual failed - see log")
            self.statusBar().showMessage("Failed")
            self.logbox.appendPlainText("ERROR: " + msg)
            QMessageBox.warning(self, "The ritual failed", msg)

        def on_pick(self, idx):
            p = self.model.filePath(idx)
            if os.path.isfile(p):
                self.show_file(p)

        def _selected_binary(self, idx=None):
            idx = idx if idx is not None and idx.isValid() else self.tree.currentIndex()
            if not idx.isValid():
                return None
            p = self.model.filePath(idx)
            return p if os.path.isfile(p) and engines.native_magic(p) else None

        def on_double(self, idx):
            p = self._selected_binary(idx)
            if p:
                self.decompile_binary(p)

        def decompile_current(self):
            cur = getattr(self, "cur_file", None)
            if cur and os.path.isfile(cur[0]) and engines.native_magic(cur[0]):
                self.decompile_binary(cur[0])

        def decompile_selected(self):
            p = self._selected_binary()
            if p:
                self.decompile_binary(p)
            else:
                self.statusBar().showMessage("Select a native library (.so, .dll, .exe) in the file tree first")

        def tree_menu(self, pos):
            from PySide6.QtWidgets import QMenu
            idx = self.tree.indexAt(pos)
            p = self._selected_binary(idx)
            if not p:
                return
            menu = QMenu(self)
            menu.addAction("Decompile this binary (Ghidra)", lambda: self.decompile_binary(p))
            menu.exec(self.tree.viewport().mapToGlobal(pos))

        def decompile_binary(self, path):
            if (self.worker and self.worker.isRunning()) or (self.native_worker and self.native_worker.isRunning()):
                self.statusBar().showMessage("Wait for the current job to finish (or press Cancel)")
                return
            self.logbox.appendPlainText("")
            self.statusBar().showMessage(f"Decompiling {Path(path).name}...")
            self.progress.show()
            self.btn_cancel.show()
            self.btn_cancel.setEnabled(True)
            self.btn_log.setChecked(True)
            self.btn_open.setEnabled(False)
            self.native_worker = NativeWorker(path, self.out_dir)
            self.native_worker.log.connect(self.logbox.appendPlainText)
            self.native_worker.finished_ok.connect(self.native_done)
            self.native_worker.failed.connect(self.native_fail)
            self.native_worker.start()

        def _native_idle(self):
            self.progress.hide()
            self.btn_cancel.hide()
            self.btn_open.setEnabled(True)

        def native_done(self, out):
            self._native_idle()
            self.statusBar().showMessage("Binary decompiled - see the native folder in the file tree")
            c = Path(out) / "decompiled.c"
            target = c if c.exists() else next(iter(sorted(Path(out).rglob("*.asm"))), None) or next(iter(sorted(Path(out).glob("*.txt"))), None)
            if target:
                self.show_file(str(target))
                idx = self.model.index(str(target))
                self.tree.scrollTo(idx)
                self.tree.setCurrentIndex(idx)

        def native_fail(self, msg):
            self._native_idle()
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

        def explain_help(self):
            self.oracle.show()
            self.oracle_title.setText("Explain: setup")
            self.oracle_text.setPlainText(llm.HELP + "\n\nThe code you explain is sent only to the address in Server address "
                                          "(default: this computer).")

        def pick_model(self):
            from PySide6.QtWidgets import QInputDialog
            url, model = self._llm_settings()
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

        def explain(self):
            if self.explainer and self.explainer.isRunning():
                return
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
            self.oracle_title.setText(f"Explain: {label}")
            self.oracle_text.setPlainText("Thinking..." + ("" if llm.is_local(url) else f"\n(the code is being sent to {url})"))
            self._oracle_buf = ""
            self.btn_explain.setEnabled(False)
            self.btn_oracle_stop.setEnabled(True)
            self.explainer = ExplainWorker(code, fname, label, url, model)
            self.explainer.model.connect(lambda m: self.oracle_title.setText(f"Explain: {label}  ({m})"))
            self.explainer.piece.connect(self._oracle_piece)
            self.explainer.finished_ok.connect(self._oracle_done)
            self.explainer.failed.connect(self._oracle_failed)
            self.explainer.start()

        def _oracle_piece(self, t):
            self._oracle_buf += t
            self.oracle_text.setPlainText(self._oracle_buf)
            sb = self.oracle_text.verticalScrollBar()
            sb.setValue(sb.maximum())

        def _oracle_done(self):
            self.btn_explain.setEnabled(True)
            self.btn_oracle_stop.setEnabled(False)
            if self._oracle_buf.strip():
                self.oracle_text.setMarkdown(self._oracle_buf + "\n\n*AI explanation: a small model can be wrong. Check it against the code.*")

        def _oracle_failed(self, msg):
            self.btn_explain.setEnabled(True)
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
            """A search hit inside a binary: say where it is, show the readable text around it and a hex dump."""
            self.cur_file = (p, None)
            self.pathlabel.show()
            self.binbar.setVisible(bool(engines.native_magic(p)))
            rel = os.path.relpath(p, self.out_dir) if self.out_dir else p
            self.pathlabel.setText(f"{rel}  @ 0x{off:x}")
            self.stack.setCurrentWidget(self.viewer)
            with open(p, "rb") as f:
                size = os.path.getsize(p)
                f.seek(max(0, off - 400))
                base = max(0, off - 400)
                data = f.read(900)
            where = ""
            try:
                from elftools.elf.elffile import ELFFile
                with open(p, "rb") as f:
                    for sec in ELFFile(f).iter_sections():
                        a, sz = sec["sh_offset"], sec["sh_size"]
                        if sec["sh_type"] != "SHT_NOBITS" and a <= off < a + sz:
                            where = f"  in section {sec.name}"
                            break
            except Exception:
                pass
            def readable(b):
                out = []
                for i in range(0, len(b)):
                    c = b[i]
                    out.append(chr(c) if 32 <= c < 127 else ("\n" if c in (10, 13) else "\0"))
                text = "".join(out)
                return [t.strip() for t in text.replace("\0", "\n").split("\n") if len(t.strip()) >= 3]
            lines = [f"Match at byte offset 0x{off:x} ({off:,}) of {size:,}{where}", "",
                     "Text stored around it (compiled code has no source lines; this is the data next to the match):", ""]
            lines += ["    " + t for t in readable(data)] or ["    (nothing readable)"]
            lines += ["", "Hex dump (the match starts at the marked offset):", ""]
            for i in range(0, len(data), 16):
                row = data[i:i + 16]
                hx = " ".join(f"{b:02x}" for b in row).ljust(47)
                asc = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
                mark = ">" if base + i <= off < base + i + 16 else " "
                lines.append(f"{mark}{base + i:08x}  {hx}  {asc}")
            lines += ["", "To read this as code: double-click the file in the tree (Ctrl+D) to decompile it with Ghidra.",
                      "For Flutter apps the same text is also listed in maincode/flutter/strings.txt."]
            self.viewer.setPlainText("\n".join(lines))

        def show_file(self, p, line=None):
            if isinstance(line, tuple) and line and line[0] == "bin":
                self.show_binary_hit(p, line[1], line[2])
                return
            self.cur_file = (p, line)
            self.binbar.hide()
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
            if b"\x00" in head:
                self.pathlabel.setText(rel)
                self.binbar.setVisible(bool(engines.native_magic(p)))
                self.stack.setCurrentWidget(self.viewer)
                msg = f"[binary file, {size:,} bytes]"
                if engines.native_magic(p):
                    msg += ("\n\nThis is a native program/library. Press the 'Decompile this binary' button above (or double-click it in the\n"
                            "file tree, or Ctrl+D). Ghidra does the work; big files take a while.\n"
                            "The result goes into the 'native' folder.")
                self.viewer.setPlainText(msg)
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
