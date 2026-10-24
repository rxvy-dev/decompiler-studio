#!/usr/bin/env python3
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
                                   QFileIconProvider, QGraphicsDropShadowEffect)
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

    DripBar, SigilCanvas = theme.make_widgets()
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
            a = QAction("&Summon file...", self, shortcut=QKeySequence.Open, triggered=self.open_dialog)
            m.addAction(a)
            a = QAction("&Extract source to folder...", self, triggered=self.export)
            m.addAction(a)
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
                label = tname + ("  (main)" if tname == theme.DEFAULT_THEME else "")
                ta = QAction(label, self, checkable=True, checked=(tname == theme.CURRENT))
                ta.triggered.connect(lambda _=False, n=tname: self.set_theme(n))
                self.theme_group.addAction(ta)
                tm.addAction(ta)

            # ---- header bar
            header = DripBar(objectName="header")
            hl = QHBoxLayout(header)
            hl.setContentsMargins(14, 6, 10, 6 + DripBar.DRIP_ROOM)
            logo = QLabel("Decompiler Studio", objectName="logo")
            logo.setFont(title_font(30))
            glow = QGraphicsDropShadowEffect(logo)
            glow.setColor(QColor(*theme.GLOW, 190))
            self.logo_glow = glow
            glow.setBlurRadius(22)
            glow.setOffset(0, 0)
            logo.setGraphicsEffect(glow)
            self.filelabel = QLabel("", objectName="subtitle")
            hl.addWidget(logo)
            hl.addSpacing(18)
            hl.addWidget(self.filelabel, 1)
            self.btn_open = QPushButton("Summon", objectName="primary", clicked=self.open_dialog,
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
            self.btn_cancel = QPushButton("Banish", clicked=self.cancel,
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
            self.treehint = QLabel("Nothing devoured yet.\nRecovered folders and files\nwill rise here.",
                                   objectName="hint", alignment=Qt.AlignTop | Qt.AlignLeft)
            self.tree.clicked.connect(self.on_pick)

            self.search = QLineEdit(placeholderText="Hunt through every recovered file (Enter)...")
            self.search.returnPressed.connect(self.do_search)
            self.results = QListWidget()
            self.results.itemActivated.connect(self.on_result)
            self.results.itemClicked.connect(self.on_result)
            self.results.hide()

            left = QWidget()
            lv = QVBoxLayout(left)
            lv.setContentsMargins(8, 4, 4, 8)
            lv.addWidget(QLabel("The remains", objectName="panelTitle"))
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
            self.welcome = SigilCanvas()
            wl = QVBoxLayout(self.welcome)
            wl.addStretch(2)
            big = QLabel("Decompiler Studio", objectName="biglogo", alignment=Qt.AlignCenter)
            big.setFont(title_font(72))
            bigglow = QGraphicsDropShadowEffect(big)
            bigglow.setColor(QColor(*theme.BIGGLOW, 230))
            self.big_glow = bigglow
            bigglow.setOffset(0, 0)
            bigglow.setBlurRadius(40)
            big.setGraphicsEffect(bigglow)
            self.welcome.glow_effect = bigglow
            wl.addWidget(big)
            sub = QLabel("Feed it an APK, JAR, EXE, ELF, DLL, .pyc or .asar. Drop it anywhere on this window.",
                         objectName="subtitle", alignment=Qt.AlignCenter)
            wl.addWidget(sub)
            wl.addSpacing(22)
            row = QHBoxLayout()
            row.addStretch(1)
            ob = QPushButton("Summon a file", objectName="primary", clicked=self.open_dialog)
            ob.setCursor(Qt.PointingHandCursor)
            ob.setMinimumWidth(160)
            row.addWidget(ob)
            row.addStretch(1)
            wl.addLayout(row)
            wl.addSpacing(26)
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
            self.oracle = QWidget(objectName="oracle")
            ov = QVBoxLayout(self.oracle)
            ov.setContentsMargins(8, 4, 8, 6)
            oh = QHBoxLayout()
            self.oracle_title = QLabel("The Oracle", objectName="panelTitle")
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
            self.statusBar().showMessage("Waiting for something to dissect")

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
            p, _ = QFileDialog.getOpenFileName(self, "Summon a file to decompile")
            if p:
                self.load(p)

        def load(self, path):
            if self.worker and self.worker.isRunning():
                return
            out = os.path.join(self.work_root, Path(path).name + ".src")
            self.logbox.clear()
            self.logbox.appendPlainText(f"Opening {path}")
            self.statusBar().showMessage("Devouring...")
            self.progress.show()
            self.worker = Worker(path, out)
            self.worker.log.connect(self.logbox.appendPlainText)
            self.worker.finished_ok.connect(self.done)
            self.worker.failed.connect(self.fail)
            self.worker.start()
            self.setWindowTitle(f"Decompiler Studio - {Path(path).name}")
            self.filelabel.setText(f"devouring  {Path(path).name} ...")
            self.btn_log.setChecked(True)
            self.btn_open.setEnabled(False)
            self.btn_cancel.show()
            self.btn_cancel.setEnabled(True)

        def cancel(self):
            self.btn_cancel.setEnabled(False)
            self.statusBar().showMessage("Banishing...")
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
            self.statusBar().showMessage(f"{engines.KIND_LABEL.get(kind, kind)} devoured - source tree loaded (Report explains what was recovered)")
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
                self.filelabel.setText("banished")
                self.statusBar().showMessage("Banished")
                self.logbox.appendPlainText("Cancelled by user (banished).")
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

        def show_report(self):
            if self.out_dir and (Path(self.out_dir) / "REPORT.md").exists():
                self.show_file(str(Path(self.out_dir) / "REPORT.md"))

        # -------- the Oracle (explain code with a small local model)
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
            self.oracle_title.setText("The Oracle: setup")
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
            self.oracle_title.setText(f"The Oracle: {label}")
            self.oracle_text.setPlainText("Listening..." + ("" if llm.is_local(url) else f"\n(the code is being sent to {url})"))
            self._oracle_buf = ""
            self.btn_explain.setEnabled(False)
            self.btn_oracle_stop.setEnabled(True)
            self.explainer = ExplainWorker(code, fname, label, url, model)
            self.explainer.model.connect(lambda m: self.oracle_title.setText(f"The Oracle: {label}  ({m})"))
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
            self.oracle_title.setText("The Oracle")
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
            self.logo_glow.setColor(QColor(*theme.GLOW, 190))
            self.big_glow.setColor(QColor(*theme.BIGGLOW, 230))
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

        def show_file(self, p, line=None):
            self.cur_file = (p, line)
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
                self.stack.setCurrentWidget(self.viewer)
                self.viewer.setPlainText(f"[binary file, {size:,} bytes]")
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
                except (UnicodeDecodeError, OSError):
                    continue
                if count >= 500:
                    break
            self.statusBar().showMessage(f"{count} match(es)" + (" (showing first 500)" if count >= 500 else ""))

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
    UI["title_font"] = theme.load_fonts(engines.app_dir())
    w = Win()
    w.show()
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        w.load(sys.argv[1])
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
