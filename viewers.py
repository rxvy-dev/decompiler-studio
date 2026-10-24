# SPDX-License-Identifier: GPL-3.0-or-later
# Decompiler Studio - Copyright (C) 2026 rxvy-dev. Free software under the GNU GPL v3 or later; see LICENSE.
"""Built-in viewers for what comes out of an app: images, sounds, 3D models, fonts and a real hex
viewer (with an entropy map and a data inspector) for every other binary. Also file facts
(hashes, entropy) that reverse engineers look up first.

Built lazily (make_viewers) so importing this module needs no Qt.
"""
import hashlib
import math
import os
import shutil
import struct
import subprocess
import tempfile
import wave
from pathlib import Path

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".tga", ".ppm", ".pgm", ".xpm", ".svg"}
AUDIO_EXT = {".wav", ".ogg", ".mp3", ".flac", ".opus", ".m4a", ".aac", ".oga"}
FONT_EXT = {".ttf", ".otf"}
MODEL_EXT = {".obj"}


def kind_of(path):
    ext = Path(path).suffix.lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in FONT_EXT:
        return "font"
    if ext in MODEL_EXT:
        return "model"
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return None
    if head.startswith((b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"BM")) or (head[:4] == b"RIFF" and head[8:12] == b"WEBP"):
        return "image"
    if (head[:4] == b"RIFF" and head[8:12] == b"WAVE") or head.startswith((b"OggS", b"fLaC", b"ID3")):
        return "audio"
    if head[:4] in (b"\x00\x01\x00\x00", b"OTTO", b"true"):
        return "font"
    return None


# ---------------------------------------------------------------- facts about a file
def entropy(data):
    if not data:
        return 0.0
    n = len(data)
    e = 0.0
    for b in range(256):
        c = data.count(b)
        if c:
            p = c / n
            e -= p * math.log2(p)
    return e


def entropy_map(path, segments=512, sample=4096):
    """Entropy (0-8 bits per byte) of `segments` evenly spaced samples of the file."""
    size = os.path.getsize(path)
    if size == 0:
        return []
    segments = max(1, min(segments, size // 256 or 1))
    step = size / segments
    out = []
    with open(path, "rb") as f:
        for i in range(segments):
            f.seek(int(i * step))
            out.append(entropy(f.read(min(sample, max(1, int(step))))))
    return out


def facts(path):
    """Type, size, hashes and overall entropy, as (label, value) pairs."""
    size = os.path.getsize(path)
    rows = [("Size", f"{size:,} bytes" + (f"  ({size / 1e6:.1f} MB)" if size > 1e6 else ""))]
    with open(path, "rb") as f:
        head = f.read(64)
    rows.insert(0, ("Type", describe(head, path)))
    if size <= 512_000_000:
        md5, sha1, sha256 = hashlib.md5(), hashlib.sha1(), hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                md5.update(chunk)
                sha1.update(chunk)
                sha256.update(chunk)
        rows += [("MD5", md5.hexdigest()), ("SHA-1", sha1.hexdigest()), ("SHA-256", sha256.hexdigest())]
    em = entropy_map(path, 256)
    if em:
        avg = sum(em) / len(em)
        verdict = ("looks compressed or encrypted" if avg > 7.5 else "partly compressed/encrypted" if max(em) > 7.6
                   else "plain code/data")
        rows.append(("Entropy", f"{avg:.2f} bits/byte average, {max(em):.2f} max: {verdict}"))
    return rows


def describe(head, path=""):
    if head[:4] == b"\x7fELF":
        bits = "64-bit" if head[4] == 2 else "32-bit"
        mach = struct.unpack_from("<H", head, 18)[0] if len(head) > 20 else 0
        arch = {0x3E: "x86-64", 0x03: "x86", 0xB7: "ARM64", 0x28: "ARM", 0xF3: "RISC-V", 0x08: "MIPS"}.get(mach, f"machine {mach}")
        typ = {2: "executable", 3: "shared library / PIE"}.get(struct.unpack_from("<H", head, 16)[0] if len(head) > 18 else 0, "object")
        return f"ELF {bits} {arch} {typ}"
    if head[:2] == b"MZ":
        return "Windows PE (EXE/DLL)"
    if head[:4] == b"dex\n":
        return "Android DEX bytecode"
    if head[:4] == b"PK\x03\x04":
        return "ZIP archive (APK/JAR/...)"
    if head[:4] == b"\xca\xfe\xba\xbe":
        return "Java class / Mach-O fat binary"
    if head[:4] in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe"):
        return "Mach-O (macOS/iOS)"
    if head.startswith(b"UnityFS"):
        return "Unity asset bundle"
    if head.startswith(b"\x89PNG"):
        return "PNG image"
    if head.startswith(b"\xff\xd8\xff"):
        return "JPEG image"
    if head[:4] == b"RIFF":
        return "RIFF (" + head[8:12].decode("latin-1") + ")"
    if head.startswith(b"OggS"):
        return "Ogg audio"
    if head[:4] == b"\xaf\x1b\xb1\xfa":
        return "Unity IL2CPP global-metadata"
    if head[:4] == b"\x90\xab\xcd\xef":
        return "Dart kernel snapshot"
    return "data"


# ---------------------------------------------------------------- audio decoding / playback
def read_wav(path, max_points=4000):
    """Peaks for a waveform: list of (min, max) in -1..1, plus duration in seconds. None if not a PCM WAV."""
    flt = False
    try:
        with wave.open(str(path), "rb") as w:
            ch, sw, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            raw = w.readframes(n)
    except Exception:
        r = _read_float_wav(path)                 # 32-bit float WAVs (format 3), which wave cannot open
        if r is None:
            return None
        ch, sw, rate, raw = r
        n = len(raw) // (sw * ch)
        flt = True
    if not rate or sw not in (1, 2, 3, 4) or not n:
        return None
    frames = len(raw) // (sw * ch)
    step = max(1, frames // max_points)
    peaks = []
    scale = float(1 << (8 * sw - 1))
    for i in range(0, frames, step):
        lo, hi = 1.0, -1.0
        for j in range(i, min(frames, i + step), max(1, step // 64)):
            off = j * sw * ch
            if sw == 1:
                v = (raw[off] - 128) / 128.0
            elif sw == 2:
                v = struct.unpack_from("<h", raw, off)[0] / scale
            elif flt:
                v = max(-1.0, min(1.0, struct.unpack_from("<f", raw, off)[0]))
            elif sw == 3:
                b = raw[off:off + 3]
                v = int.from_bytes(b, "little", signed=True) / scale
            else:
                v = struct.unpack_from("<i", raw, off)[0] / scale
            lo, hi = min(lo, v), max(hi, v)
        peaks.append((lo, hi))
    return peaks, frames / rate


def _read_float_wav(path):
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    i, fmt, body = 12, None, None
    while i + 8 <= len(data):
        cid, size = data[i:i + 4], struct.unpack_from("<I", data, i + 4)[0]
        if cid == b"fmt ":
            fmt = data[i + 8:i + 8 + size]
        elif cid == b"data":
            body = data[i + 8:i + 8 + size]
        i += 8 + size + (size & 1)
    if not fmt or body is None:
        return None
    tag, ch, rate = struct.unpack_from("<HHI", fmt, 0)
    bits = struct.unpack_from("<H", fmt, 14)[0]
    if tag == 0xFFFE and len(fmt) >= 26:
        tag = struct.unpack_from("<H", fmt, 24)[0]
    if tag != 3 or bits != 32:
        return None
    return ch, 4, rate, body


def audio_player():
    for cmd in ("pw-play", "paplay", "ffplay", "aplay"):
        if shutil.which(cmd):
            return cmd
    return None


def play(path, start=0.0):
    """Start playing `path` from `start` seconds with whatever player the system has. Returns the Popen."""
    cmd = audio_player()
    if not cmd:
        return None
    src = str(path)
    if start > 0.05 and cmd != "ffplay":
        src = _wav_from(path, start) or src
    args = {"pw-play": ["pw-play", src], "paplay": ["paplay", src], "aplay": ["aplay", "-q", src],
            "ffplay": ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "-ss", f"{start:.2f}", src]}[cmd]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proc.ds_tmp = src if src != str(path) else None
    return proc


def _wav_from(path, start):
    try:
        with wave.open(str(path), "rb") as w:
            params = w.getparams()
            w.setpos(min(params.nframes, int(start * params.framerate)))
            rest = w.readframes(params.nframes)
        fd, tmp = tempfile.mkstemp(suffix=".wav", prefix="ds-play-")
        os.close(fd)
        with wave.open(tmp, "wb") as o:
            o.setparams(params)
            o.writeframes(rest)
        return tmp
    except Exception:
        return None


# ---------------------------------------------------------------- OBJ models
def load_obj(path, max_faces=60000):
    verts, faces = [], []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("v "):
                p = line.split()
                try:
                    verts.append((float(p[1]), float(p[2]), float(p[3])))
                except (ValueError, IndexError):
                    verts.append((0.0, 0.0, 0.0))
            elif line.startswith("f "):
                idx = []
                for tok in line.split()[1:]:
                    try:
                        k = int(tok.split("/")[0])
                    except ValueError:
                        continue
                    idx.append(k - 1 if k > 0 else len(verts) + k)
                for i in range(1, len(idx) - 1):               # fan into triangles
                    faces.append((idx[0], idx[i], idx[i + 1]))
    if len(faces) > max_faces:
        step = len(faces) / max_faces
        faces = [faces[int(i * step)] for i in range(max_faces)]
    if verts:
        xs, ys, zs = zip(*verts)
        cx, cy, cz = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2, (max(zs) + min(zs)) / 2
        r = max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)) / 2 or 1.0
        verts = [((x - cx) / r, (y - cy) / r, (z - cz) / r) for x, y, z in verts]
    faces = [f for f in faces if all(0 <= i < len(verts) for i in f)]
    return verts, faces


# ---------------------------------------------------------------- widgets
def make_viewers(theme):
    from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
    from PySide6.QtGui import (QBrush, QColor, QFont, QFontDatabase, QImage, QPainter, QPen, QPolygonF,
                               QGuiApplication)
    from PySide6.QtWidgets import (QAbstractScrollArea, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
                                   QSlider)

    def col(name, alpha=None):
        c = QColor(getattr(theme, name))
        if alpha is not None:
            c.setAlpha(alpha)
        return c

    # ------------------------------------------------ image
    class ImageView(QWidget):
        status = Signal(str)

        def __init__(self):
            super().__init__()
            self.img = None
            self.zoom, self.off = 1.0, QPointF(0, 0)
            self.drag = None
            self.setMouseTracking(True)
            self.setMinimumSize(200, 200)

        def load(self, path):
            img = QImage(str(path))
            self.img = None if img.isNull() else img
            self.fit()
            return self.img is not None

        def fit(self):
            if not self.img:
                return
            w, h = max(1, self.width() - 40), max(1, self.height() - 40)
            self.zoom = min(w / self.img.width(), h / self.img.height(), 8.0)
            if self.zoom >= 1:
                self.zoom = max(1.0, math.floor(self.zoom))       # whole steps keep pixel art crisp
            self.center()

        def actual(self):
            self.zoom = 1.0
            self.center()

        def center(self):
            if self.img:
                self.off = QPointF((self.width() - self.img.width() * self.zoom) / 2,
                                   (self.height() - self.img.height() * self.zoom) / 2)
            self.update()

        def resizeEvent(self, e):
            super().resizeEvent(e)
            self.center()

        def wheelEvent(self, e):
            if not self.img:
                return
            f = 1.25 if e.angleDelta().y() > 0 else 0.8
            pos = e.position()
            old = self.zoom
            self.zoom = max(0.02, min(64.0, self.zoom * f))
            self.off = pos - (pos - self.off) * (self.zoom / old)
            self.update()

        def mousePressEvent(self, e):
            self.drag = (e.position(), QPointF(self.off))

        def mouseReleaseEvent(self, e):
            self.drag = None

        def mouseDoubleClickEvent(self, e):
            self.fit() if abs(self.zoom - 1.0) < 1e-6 else self.actual()

        def mouseMoveEvent(self, e):
            if self.drag:
                self.off = self.drag[1] + (e.position() - self.drag[0])
                self.update()
            if self.img:
                x = int((e.position().x() - self.off.x()) / self.zoom)
                y = int((e.position().y() - self.off.y()) / self.zoom)
                base = f"{self.img.width()} x {self.img.height()}   zoom {self.zoom * 100:.0f}%"
                if 0 <= x < self.img.width() and 0 <= y < self.img.height():
                    c = self.img.pixelColor(x, y)
                    base += (f"   pixel ({x}, {y})  #{c.red():02x}{c.green():02x}{c.blue():02x}{c.alpha():02x}"
                             f"  rgba({c.red()}, {c.green()}, {c.blue()}, {c.alpha()})")
                self.status.emit(base)

        def paintEvent(self, e):
            p = QPainter(self)
            p.fillRect(self.rect(), col("CODE_BG"))
            if not self.img:
                p.setPen(col("DIM"))
                p.drawText(self.rect(), Qt.AlignCenter, "This image could not be decoded.")
                return
            r = QRectF(self.off.x(), self.off.y(), self.img.width() * self.zoom, self.img.height() * self.zoom)
            # checkerboard behind transparent pixels
            p.save()
            p.setClipRect(r)
            s = 10
            a, b = QColor(60, 64, 72), QColor(44, 48, 56)
            if theme.LIGHT:
                a, b = QColor(235, 235, 235), QColor(205, 205, 205)
            x0, y0 = int(r.left()), int(r.top())
            for yy in range(y0, int(r.bottom()) + s, s):
                for xx in range(x0, int(r.right()) + s, s):
                    p.fillRect(xx, yy, s, s, a if ((xx - x0) // s + (yy - y0) // s) % 2 == 0 else b)
            p.restore()
            p.setRenderHint(QPainter.SmoothPixmapTransform, self.zoom < 1.0)
            p.drawImage(r, self.img)
            p.setPen(QPen(col("LINE"), 1))
            p.drawRect(r)

    # ------------------------------------------------ audio
    class AudioView(QWidget):
        status = Signal(str)

        def __init__(self):
            super().__init__()
            self.path, self.peaks, self.dur = None, None, 0.0
            self.proc, self.pos, self.t0, self.start_at = None, 0.0, None, 0.0
            lay = QVBoxLayout(self)
            lay.setContentsMargins(20, 20, 20, 20)
            self.title = QLabel(objectName="panelTitle")
            lay.addWidget(self.title)
            self.wave = QWidget()
            self.wave.setMinimumHeight(180)
            self.wave.paintEvent = self._paint_wave
            self.wave.mousePressEvent = self._seek_click
            lay.addWidget(self.wave, 1)
            row = QHBoxLayout()
            self.btn = QPushButton("Play", objectName="primary", clicked=self.toggle)
            self.btn.setMinimumWidth(110)
            self.timel = QLabel("0:00 / 0:00")
            row.addWidget(self.btn)
            row.addSpacing(10)
            row.addWidget(self.timel)
            row.addStretch(1)
            self.note = QLabel("", objectName="hint")
            row.addWidget(self.note)
            lay.addLayout(row)
            self.timer = QTimer(self, interval=50, timeout=self._tick)

        def load(self, path):
            self.stop()
            self.path = Path(path)
            self.title.setText(self.path.name)
            if not self.path.is_file():
                return False
            r = read_wav(path)
            self.peaks, self.dur = (r if r else (None, 0.0))
            self.pos = 0.0
            player = audio_player()
            self.note.setText(f"plays through {player}" if player else "no audio player found (install pipewire or ffmpeg)")
            self.btn.setEnabled(player is not None)
            self._update_time()
            self.wave.update()
            if self.peaks is None:
                self.status.emit(f"{self.path.suffix} audio: no waveform for this format, but it can still play")
            return True

        def toggle(self):
            if self.proc and self.proc.poll() is None:
                self.pause()
            else:
                self.start(self.pos if self.pos < self.dur - 0.05 else 0.0)

        def start(self, at):
            import time
            self.stop()
            self.proc = play(self.path, at)
            if self.proc:
                self.start_at, self.t0 = at, time.monotonic()
                self.btn.setText("Pause")
                self.timer.start()

        def pause(self):
            self._tick()
            self.stop()

        def stop(self):
            self.timer.stop()
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(1)
                except Exception:
                    pass
            if self.proc is not None and getattr(self.proc, "ds_tmp", None):
                try:
                    os.unlink(self.proc.ds_tmp)
                except OSError:
                    pass
            self.proc = None
            self.btn.setText("Play")

        def hideEvent(self, e):
            self.stop()
            super().hideEvent(e)

        def _tick(self):
            import time
            if self.t0 is not None and self.proc:
                self.pos = self.start_at + time.monotonic() - self.t0
                if self.dur:
                    self.pos = min(self.pos, self.dur)
            if self.proc and self.proc.poll() is not None:
                self.timer.stop()
                self.btn.setText("Play")
                self.proc = None
                if self.dur:
                    self.pos = self.dur
            self._update_time()
            self.wave.update()

        def _update_time(self):
            f = lambda s: f"{int(s) // 60}:{int(s) % 60:02d}"
            self.timel.setText(f"{f(self.pos)} / {f(self.dur) if self.dur else '?'}")

        def _seek_click(self, e):
            if self.dur:
                self.pos = max(0.0, min(self.dur, e.position().x() / max(1, self.wave.width()) * self.dur))
                if self.proc:
                    self.start(self.pos)
                self._update_time()
                self.wave.update()

        def _paint_wave(self, e):
            p = QPainter(self.wave)
            p.setRenderHint(QPainter.Antialiasing)
            w, h = self.wave.width(), self.wave.height()
            p.fillRect(0, 0, w, h, col("CODE_BG"))
            mid = h / 2
            p.setPen(QPen(col("LINE"), 1))
            p.drawLine(0, int(mid), w, int(mid))
            if not self.peaks:
                p.setPen(col("DIM"))
                p.drawText(self.wave.rect(), Qt.AlignCenter, "(no waveform for this format)")
                return
            played = (self.pos / self.dur) if self.dur else 0
            n = len(self.peaks)
            for x in range(w):
                i0 = int(x / w * n)
                i1 = max(i0 + 1, int((x + 1) / w * n))
                lo = min(pk[0] for pk in self.peaks[i0:i1])
                hi = max(pk[1] for pk in self.peaks[i0:i1])
                p.setPen(col("RED") if x / w <= played else col("DIM"))
                p.drawLine(x, int(mid - hi * mid * 0.92), x, int(mid - lo * mid * 0.92))
            px = int(played * w)
            p.setPen(QPen(col("RED_HI"), 2))
            p.drawLine(px, 0, px, h)

    # ------------------------------------------------ 3D model
    class ModelView(QWidget):
        status = Signal(str)

        def __init__(self):
            super().__init__()
            self.verts, self.faces = [], []
            self.yaw, self.pitch, self.zoom = 0.6, -0.35, 1.0
            self.drag = None
            self.wire = False
            self.setMinimumSize(200, 200)

        def load(self, path):
            self.verts, self.faces = load_obj(path)
            self.yaw, self.pitch, self.zoom = 0.6, -0.35, 1.0
            self.status.emit(f"{len(self.verts):,} vertices, {len(self.faces):,} triangles   "
                             "drag to rotate, wheel to zoom, double-click: wireframe")
            self.update()
            return bool(self.verts)

        def mousePressEvent(self, e):
            self.drag = (e.position(), self.yaw, self.pitch)

        def mouseReleaseEvent(self, e):
            self.drag = None
            self.update()

        def mouseMoveEvent(self, e):
            if self.drag:
                d = e.position() - self.drag[0]
                self.yaw = self.drag[1] + d.x() * 0.01
                self.pitch = max(-1.5, min(1.5, self.drag[2] + d.y() * 0.01))
                self.update()

        def mouseDoubleClickEvent(self, e):
            self.wire = not self.wire
            self.update()

        def wheelEvent(self, e):
            self.zoom = max(0.2, min(8.0, self.zoom * (1.15 if e.angleDelta().y() > 0 else 1 / 1.15)))
            self.update()

        def paintEvent(self, e):
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            w, h = self.width(), self.height()
            p.fillRect(self.rect(), col("CODE_BG"))
            if not self.verts:
                p.setPen(col("DIM"))
                p.drawText(self.rect(), Qt.AlignCenter, "No geometry in this file.")
                return
            cy, sy, cp, sp = math.cos(self.yaw), math.sin(self.yaw), math.cos(self.pitch), math.sin(self.pitch)
            scale = min(w, h) * 0.38 * self.zoom
            pts, rot = [], []
            for x, y, z in self.verts:
                x1, z1 = x * cy + z * sy, -x * sy + z * cy
                y1, z2 = y * cp - z1 * sp, y * sp + z1 * cp
                k = 3.0 / (3.0 + z2)                               # a little perspective
                pts.append((w / 2 + x1 * scale * k, h / 2 - y1 * scale * k, z2))
                rot.append((x1, y1, z2))
            faces = self.faces
            if self.drag and len(faces) > 8000:                   # stay smooth while rotating
                faces = faces[::max(1, len(faces) // 8000)]
            accent = col("RED")
            lx, ly, lz = 0.35, 0.55, -0.76                         # light from top-left, towards the viewer
            tris = []
            for a, b, c in faces:
                A, B, C = rot[a], rot[b], rot[c]
                ux, uy, uz = B[0] - A[0], B[1] - A[1], B[2] - A[2]
                vx, vy, vz = C[0] - A[0], C[1] - A[1], C[2] - A[2]
                nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
                ln = math.sqrt(nx * nx + ny * ny + nz * nz) or 1
                shade = abs(nx * lx + ny * ly + nz * lz) / ln
                tris.append((A[2] + B[2] + C[2], pts[a], pts[b], pts[c], shade))
            tris.sort(key=lambda t: t[0], reverse=True)
            edge = QPen(col("RED_HI", 90), 0.6)
            if self.wire:
                p.setPen(QPen(col("RED_HI", 160), 0.7))
                p.setBrush(Qt.NoBrush)
            for _, A, B, C, shade in tris:
                poly = QPolygonF([QPointF(A[0], A[1]), QPointF(B[0], B[1]), QPointF(C[0], C[1])])
                if not self.wire:
                    k = 0.18 + 0.82 * shade ** 1.3
                    hi = max(0.0, shade - 0.85) * 4                    # small specular-ish highlight
                    p.setBrush(QColor(min(255, int(accent.red() * k + 255 * hi * .5)), min(255, int(accent.green() * k + 255 * hi * .5)),
                                      min(255, int(accent.blue() * k + 255 * hi * .5))))
                    p.setPen(edge if len(tris) < 3000 else Qt.NoPen)
                p.drawPolygon(poly)

    # ------------------------------------------------ font
    class FontView(QWidget):
        status = Signal(str)

        def __init__(self):
            super().__init__()
            self.family = None
            self.setMinimumSize(200, 200)

        def load(self, path):
            fid = QFontDatabase.addApplicationFont(str(path))
            fams = QFontDatabase.applicationFontFamilies(fid) if fid >= 0 else []
            self.family = fams[0] if fams else None
            self.status.emit(f"font family: {self.family}" if self.family else "this font could not be loaded")
            self.update()
            return self.family is not None

        def paintEvent(self, e):
            p = QPainter(self)
            p.fillRect(self.rect(), col("CODE_BG"))
            if not self.family:
                p.setPen(col("DIM"))
                p.drawText(self.rect(), Qt.AlignCenter, "This font could not be loaded.")
                return
            y = 30
            p.setPen(col("DIM"))
            p.setFont(QFont(self.font().family(), 11))
            p.drawText(24, y, self.family)
            y += 20
            p.setPen(col("FG"))
            for size, text in ((56, "Aa Bb Cc 123"), (32, "The quick brown fox jumps over the lazy dog"),
                               (22, "ABCDEFGHIJKLMNOPQRSTUVWXYZ"), (22, "abcdefghijklmnopqrstuvwxyz"),
                               (22, "0123456789 !?&@#$%*()[]{}<>+-=/\\"), (16, "Score: 12,345   Level 7   GAME OVER")):
                f = QFont(self.family)
                f.setPixelSize(size)
                p.setFont(f)
                y += int(size * 1.35)
                p.drawText(24, y, text)

    # ------------------------------------------------ hex
    class HexView(QAbstractScrollArea):
        status = Signal(str)
        ROW = 16

        def __init__(self):
            super().__init__()
            self.path, self.size, self.f = None, 0, None
            self.ent = []
            self.sel = None                      # (offset, length)
            self.cursor = None
            self.header = ""
            self.font_ = theme.mono_font(10)
            self.viewport().setMouseTracking(True)
            self.setFocusPolicy(Qt.StrongFocus)

        def load(self, path, header=""):
            if self.f:
                self.f.close()
            self.path, self.size = Path(path), os.path.getsize(path)
            self.f = open(path, "rb")
            self.ent = entropy_map(path, 400)
            self.header, self.sel, self.cursor = header, None, None
            if self.ent:
                avg = sum(self.ent) / len(self.ent)
                self.header += f"   ·   entropy {avg:.2f}/8" + ("  (compressed/encrypted?)" if avg > 7.5 else "")
            self._range()
            self.verticalScrollBar().setValue(0)
            self.viewport().update()
            return True

        def _metrics(self):
            from PySide6.QtGui import QFontMetrics
            fm = QFontMetrics(self.font_)
            return fm, fm.horizontalAdvance("0"), fm.height() + 3

        def _range(self):
            fm, cw, lh = self._metrics()
            rows = (self.size + self.ROW - 1) // self.ROW
            vis = max(1, (self.viewport().height() - 34) // lh)
            self.verticalScrollBar().setRange(0, max(0, rows - vis))
            self.verticalScrollBar().setPageStep(vis)

        def resizeEvent(self, e):
            super().resizeEvent(e)
            self._range()

        def goto(self, off, length=1):
            off = max(0, min(self.size - 1, off))
            self.sel = (off, max(1, length))
            self.cursor = off
            fm, cw, lh = self._metrics()
            vis = max(1, (self.viewport().height() - 34) // lh)
            self.verticalScrollBar().setValue(max(0, off // self.ROW - vis // 3))
            self._inspect(off)
            self.viewport().update()

        def _layout(self):
            fm, cw, lh = self._metrics()
            x_hex = 12 + cw * 10
            x_asc = x_hex + cw * (self.ROW * 3 + 1)
            return fm, cw, lh, x_hex, x_asc

        def _hit(self, pos):
            fm, cw, lh, x_hex, x_asc = self._layout()
            row = int((pos.y() - 30) // lh)
            if row < 0:
                return None
            x = pos.x()
            if x_hex <= x < x_hex + cw * self.ROW * 3:
                col_ = int((x - x_hex) // (cw * 3))
            elif x_asc <= x < x_asc + cw * self.ROW:
                col_ = int((x - x_asc) // cw)
            else:
                return None
            off = (self.verticalScrollBar().value() + row) * self.ROW + col_
            return off if off < self.size else None

        def mousePressEvent(self, e):
            ew = 26
            if e.position().x() > self.viewport().width() - ew and self.ent:    # entropy strip: jump
                frac = (e.position().y() - 30) / max(1, self.viewport().height() - 36)
                self.goto(int(max(0, min(1, frac)) * self.size))
                return
            off = self._hit(e.position())
            if off is not None:
                self.cursor = off
                self.sel = (off, 1)
                self._inspect(off)
                self.viewport().update()

        def keyPressEvent(self, e):
            if self.cursor is None:
                return super().keyPressEvent(e)
            k = e.key()
            d = {Qt.Key_Left: -1, Qt.Key_Right: 1, Qt.Key_Up: -self.ROW, Qt.Key_Down: self.ROW}.get(k)
            if d:
                self.goto(self.cursor + d)
            else:
                super().keyPressEvent(e)

        def _inspect(self, off):
            self.f.seek(off)
            b = self.f.read(8).ljust(8, b"\0")
            parts = [f"offset 0x{off:x} ({off:,})", f"u8 {b[0]}", f"i8 {struct.unpack('<b', b[:1])[0]}",
                     f"u16 {struct.unpack('<H', b[:2])[0]}", f"u32 {struct.unpack('<I', b[:4])[0]}",
                     f"i32 {struct.unpack('<i', b[:4])[0]}", f"f32 {struct.unpack('<f', b[:4])[0]:.6g}",
                     f"u64 0x{struct.unpack('<Q', b)[0]:x}", f"f64 {struct.unpack('<d', b)[0]:.6g}"]
            self.status.emit("   ".join(parts) + "   (little-endian)")

        def paintEvent(self, e):
            p = QPainter(self.viewport())
            p.setFont(self.font_)
            fm, cw, lh, x_hex, x_asc = self._layout()
            vw, vh = self.viewport().width(), self.viewport().height()
            p.fillRect(0, 0, vw, vh, col("CODE_BG"))
            # header line
            p.fillRect(0, 0, vw, 26, col("PANEL"))
            p.setPen(col("DIM"))
            p.drawText(12, 18, fm.elidedText(self.header or f"{self.path.name if self.path else ''}  {self.size:,} bytes",
                                             Qt.ElideRight, vw - 60))
            if not self.f:
                return
            first = self.verticalScrollBar().value()
            rows = (vh - 34) // lh + 1
            self.f.seek(first * self.ROW)
            data = self.f.read(rows * self.ROW)
            sel = self.sel
            hl = col("RED", 110)
            curc = col("RED_HI", 200)
            for r in range(rows):
                base = (first + r) * self.ROW
                if base >= self.size:
                    break
                y = 30 + r * lh
                chunk = data[r * self.ROW:(r + 1) * self.ROW]
                p.setPen(col("DIM"))
                p.drawText(12, y + fm.ascent(), f"{base:08x}")
                for i, b in enumerate(chunk):
                    off = base + i
                    hx = x_hex + i * cw * 3 + (cw if i >= 8 else 0) // 2
                    ax = x_asc + i * cw
                    if sel and sel[0] <= off < sel[0] + sel[1]:
                        p.fillRect(QRectF(hx - 2, y, cw * 2 + 4, lh - 1), hl)
                        p.fillRect(QRectF(ax, y, cw, lh - 1), hl)
                    if off == self.cursor:
                        p.setPen(QPen(curc, 1.5))
                        p.drawRect(QRectF(hx - 2, y, cw * 2 + 4, lh - 1))
                        p.drawRect(QRectF(ax, y, cw, lh - 1))
                    p.setPen(col("FAINT") if b == 0 else (col("FG") if 32 <= b < 127 else col("RED_HI")))
                    p.drawText(int(hx), y + fm.ascent(), f"{b:02x}")
                    p.setPen(col("FG") if 32 <= b < 127 else col("FAINT"))
                    p.drawText(int(ax), y + fm.ascent(), chr(b) if 32 <= b < 127 else ".")
            # entropy strip
            if self.ent:
                ew, top, H = 22, 30, vh - 36
                x = vw - ew - 4
                n = len(self.ent)
                for i, ev in enumerate(self.ent):
                    t = ev / 8.0
                    c = QColor(int(40 + 215 * t), int(70 + 120 * (1 - abs(t - 0.5) * 2)), int(200 * (1 - t) + 40))
                    y0 = top + i / n * H
                    p.fillRect(QRectF(x, y0, ew, H / n + 1), c)
                vis0 = first * self.ROW / max(1, self.size)
                vis1 = min(1.0, (first + rows) * self.ROW / max(1, self.size))
                p.setPen(QPen(QColor("#ffffff"), 2))
                p.setBrush(Qt.NoBrush)
                p.drawRect(QRectF(x - 1, top + vis0 * H, ew + 2, max(3, (vis1 - vis0) * H)))
                p.setPen(col("DIM"))
                p.drawText(int(x - 2), 18, "ent")

    return ImageView, AudioView, ModelView, FontView, HexView
