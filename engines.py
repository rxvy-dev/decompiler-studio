"""Decompiler back-ends for Decompiler Studio.

Each engine takes (input_path, out_dir, log) and writes recovered source /
disassembly / extracted resources into out_dir as a normal folder tree.
"""
import io
import os
import re
import shutil
import struct
import subprocess
import sys
import zipfile
from pathlib import Path


def app_dir() -> Path:
    """Folder holding bundled backends (works in PyInstaller and from source)."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


IS_WIN = os.name == "nt"


def find_jadx():
    name = "jadx.bat" if IS_WIN else "jadx"
    cand = app_dir() / "backends" / "jadx" / "bin" / name
    if cand.exists():
        return str(cand)
    return shutil.which("jadx")


def find_java():
    """Bundled JRE first, then JAVA_HOME, then PATH."""
    exe = "java.exe" if IS_WIN else "java"
    for base in (app_dir() / "backends" / "jre", Path(os.environ.get("JAVA_HOME", "/nonexistent"))):
        c = base / "bin" / exe
        if c.exists():
            return str(c)
    return shutil.which("java")


def have_java():
    j = find_java()
    if j:  # make jadx's launcher script use the same JRE
        os.environ["JAVA_HOME"] = str(Path(j).resolve().parent.parent)
        os.environ["PATH"] = str(Path(j).parent) + os.pathsep + os.environ.get("PATH", "")
    return j is not None


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------
def detect(path: str) -> str:
    p = Path(path)
    ext = p.suffix.lower()
    with open(path, "rb") as f:
        head = f.read(4096)

    if head[:4] == b"dex\n":
        return "dex"
    if head[:4] == b"\xca\xfe\xba\xbe" and ext == ".class":
        return "class"
    if ext == ".asar":
        return "asar"
    if head[:4] == b"\x7fELF" or head[:2] == b"MZ":
        import unpackers
        if unpackers.find_pyinstaller(path) is not None:
            return "pyinstaller"
        if head[:4] == b"\x7fELF":
            return "elf"
        return "dotnet" if _is_dotnet(path) else "pe"
    if head[:2] == b"PK":
        try:
            with zipfile.ZipFile(path) as z:
                names = z.namelist()
        except zipfile.BadZipFile:
            return "unknown"
        if "AndroidManifest.xml" in names or any(n.endswith(".dex") for n in names):
            return "apk"
        if any(n.endswith(".class") for n in names):
            return "jar"
        return "zip"
    if ext in (".pyc", ".pyo") or _looks_like_pyc(head):
        return "pyc"
    if ext in (".c", ".h", ".cpp", ".cc", ".hpp", ".py", ".js", ".java", ".cs", ".rs", ".go", ".txt", ".md"):
        return "source"
    return "unknown"


def _looks_like_pyc(head: bytes) -> bool:
    return len(head) > 16 and head[2:4] == b"\r\n"


def _is_dotnet(path: str) -> bool:
    try:
        import pefile
        pe = pefile.PE(path, fast_load=True)
        idx = pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_COM_DESCRIPTOR"]
        d = pe.OPTIONAL_HEADER.DATA_DIRECTORY[idx]
        return d.VirtualAddress != 0 and d.Size != 0
    except Exception:
        return False


KIND_LABEL = {
    "apk": "Android APK",
    "dex": "Android DEX",
    "jar": "Java JAR",
    "class": "Java class",
    "dotnet": ".NET assembly",
    "elf": "Native ELF (Linux)",
    "pe": "Native PE (Windows)",
    "pyc": "Python bytecode",
    "pyinstaller": "Python program packed with PyInstaller",
    "asar": "Electron app bundle (asar)",
    "zip": "ZIP archive",
    "source": "Source file",
    "unknown": "Unknown file",
}


# --------------------------------------------------------------------------
# Engines
# --------------------------------------------------------------------------
import threading
import time as _time

CANCEL = threading.Event()          # set by the GUI's Cancel button
_PROCS = set()


class Cancelled(Exception):
    pass


class TimedOut(Exception):
    pass


class Stalled(TimedOut):
    pass


def _children(pid):
    """Direct child PIDs (Linux /proc, else pgrep)."""
    kids = []
    if os.path.isdir("/proc"):
        try:
            for d in os.listdir("/proc"):
                if d.isdigit():
                    try:
                        with open(f"/proc/{d}/stat", "rb") as f:
                            st = f.read().decode("latin1")
                        if int(st[st.rindex(")") + 2:].split()[1]) == pid:
                            kids.append(int(d))
                    except (OSError, ValueError):
                        pass
            return kids
        except OSError:
            pass
    try:
        r = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True)
        return [int(x) for x in r.stdout.split()]
    except Exception:
        return []


def _tree(pid):
    out, todo = [], [pid]
    while todo:
        p = todo.pop()
        for c in _children(p):
            if c not in out:
                out.append(c)
                todo.append(c)
    return out


def _kill(proc):
    """Stop a process and everything it started (some tools start helpers in their own group)."""
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            return
        import signal
        victims = _tree(proc.pid)          # collect first: children get re-parented once the parent dies
        for p in [proc.pid] + victims:
            try:
                os.kill(p, signal.SIGSTOP)  # freeze so nothing new gets spawned
            except OSError:
                pass
        victims = list(dict.fromkeys(victims + _tree(proc.pid)))
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        for p in victims + [proc.pid]:
            try:
                os.kill(p, signal.SIGKILL)
            except OSError:
                pass
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _dur(s):
    return f"{s // 60} min" if s >= 60 else f"{s} s"


def check_cancel():
    if CANCEL.is_set():
        raise Cancelled()


def cancel_all():
    CANCEL.set()
    for p in list(_PROCS):
        _kill(p)


def run_cmd(cmd, log, cwd=None, timeout=None, stall=None):
    """Run a command, streaming its output to log.

    timeout: seconds before the command is killed (raises TimedOut).
    stall:   (seconds, test) - if the last output line passes test(line) and nothing new
             is printed for that many seconds, the command is killed (raises Stalled).
    Also raises Cancelled when the user presses Cancel.
    """
    log("$ " + " ".join(cmd))
    kw = {}
    if IS_WIN:
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        kw["start_new_session"] = True
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, errors="replace", **kw)
    _PROCS.add(proc)

    last = {"line": "", "t": _time.time()}

    def pump():
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                last["line"], last["t"] = line, _time.time()
                log(line)

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    start = _time.time()
    try:
        while proc.poll() is None:
            if CANCEL.is_set():
                _kill(proc)
                raise Cancelled()
            if timeout and _time.time() - start > timeout:
                _kill(proc)
                raise TimedOut()
            if stall and _time.time() - last["t"] > stall[0] and stall[1](last["line"]):
                _kill(proc)
                raise Stalled(last["line"])
            _time.sleep(0.2)
        t.join(timeout=2)
        return proc.returncode
    finally:
        _PROCS.discard(proc)


def decompile_jadx(path, out, log):
    jadx = find_jadx()
    if not jadx:
        raise RuntimeError("jadx backend not found (expected next to the app in backends/jadx).")
    if not have_java():
        raise RuntimeError("Java runtime not found. Install one to decompile APK/DEX/JAR/class files: "
                           "Linux: 'sudo apt install default-jre' / 'sudo dnf install java-latest-openjdk' / "
                           "'sudo pacman -S jre-openjdk'. Windows: install Temurin JRE from adoptium.net "
                           "(tick 'Set JAVA_HOME' / 'Add to PATH').")
    # Launch the jar directly with the bundled/system JRE (no shell script needed)
    lib = Path(jadx).resolve().parent.parent / "lib"
    jars = sorted(lib.glob("*.jar"))
    if jars:
        cmd = [find_java(), "-cp", os.pathsep.join(map(str, jars)), "jadx.cli.JadxCLI"]
    else:
        cmd = [jadx]
    limit = max(1, int(1800 * float(os.environ.get("DECOMPILER_STUDIO_TIMEOUT_SCALE", "1"))))
    try:
        rc = run_cmd(cmd + ["--show-bad-code", "-d", str(out), path], log, timeout=limit)
    except TimedOut:
        rc = "timeout"
        log(f"jadx was still running after {_dur(limit)} and was stopped; showing what it finished.")
    # jadx returns non-zero if some methods failed but still writes output
    if not any(Path(out).rglob("*.java")) and not any(Path(out).rglob("*.xml")):
        if rc == "timeout":
            raise RuntimeError("jadx ran out of time before writing any files.")
        raise RuntimeError(f"jadx produced no output (exit {rc}).")
    log(f"jadx finished (exit {rc}); methods it could not recover are marked in the code.")
    try:
        for n in apk_insights(out):
            log("About this app: " + n)
    except Exception:
        pass
    try:
        if (Path(out) / "resources" / "AndroidManifest.xml").is_file():      # APKs only, not plain jars
            build_maincode(out, log)
    except Exception as e:
        log(f"(maincode folder skipped: {e})")


_SDK_ROOTS = {
    # libraries and ad/analytics SDKs that are not the app's own code
    "google", "facebook", "applovin", "ironsource", "unity3d", "unity", "bytedance", "mbridge", "vungle",
    "inmobi", "fyber", "tiktok", "moloco", "helpshift", "squareup", "singular", "digitalturbine",
    "explorestack", "iab", "bykv", "pgl", "amazon", "android", "androidx", "kotlin", "kotlinx", "okhttp3",
    "okio", "retrofit2", "javax", "org", "io", "net", "apm", "dikra", "adjust", "appsflyer", "chartboost",
    "bidmachine", "attribution", "mopub", "tapjoy", "yandex", "mintegral", "pangle", "ogury", "smaato", "criteo", "bumptech", "airbnb",
}


def _manifest_prefixes(manifest_text):
    """('com.kiloo', 'com.sybogames', ...): the first two name parts of the app's own package and of the
    classes the manifest wires up, minus well-known libraries."""
    prefixes = []
    pkg = re.search(r'package="([\w.]+)"', manifest_text)
    names = [n for n in re.findall(r'android:name="([A-Za-z_][\w.$]*\.[A-Z]\w*)"', manifest_text) if not n.rsplit(".", 1)[1].isupper()]
    for full in ([pkg.group(1)] if pkg else []) + names:
        parts = full.split(".")
        pre = ".".join(parts[:2]) if parts[0] in ("com", "org", "net", "io", "de", "fr", "jp", "cn", "ru") else parts[0]
        root = parts[1] if len(parts) > 1 and parts[0] in ("com", "org", "net", "io", "de", "fr", "jp", "cn", "ru") else parts[0]
        if root in _SDK_ROOTS or parts[0] in ("android", "androidx", "java", "javax", "kotlin"):
            continue
        if "." not in pre and pkg and not pkg.group(1).startswith(pre + "."):
            continue                       # a bare one-word root ("tv") is a library, not the app
        if pre not in prefixes:
            prefixes.append(pre)
    return prefixes


def _link_tree(src, dst):
    """Mirror src into dst with hard links (no extra disk space); copy when linking is not possible."""
    for p in src.rglob("*"):
        if p.is_file():
            t = dst / p.relative_to(src)
            if t.exists():
                continue                   # overlapping prefixes (com.kiloo and com.kiloo.subwaysurf)
            t.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(p, t)
            except OSError:
                try:
                    shutil.copy2(p, t)
                except shutil.SameFileError:
                    pass


_ASCII_RUN = re.compile(rb"[\x20-\x7e\t\n]{4,}")
_UTF16_RUN = re.compile(rb"(?:[\x20-\x7e]\x00){4,}")


def dump_native_strings(lib, dest, log, kind="native"):
    """Write the readable text inside a compiled library (ASCII and UTF-16 runs) to dest/strings.txt.

    Flutter keeps every Dart string (screen text, class and function names, package: paths) inside
    libapp.so as plain bytes, so this is how text like "Drop a flare" becomes searchable."""
    data = Path(lib).read_bytes()
    found = []
    for m in _ASCII_RUN.finditer(data):
        t = m.group().decode("ascii").strip()
        if len(t) >= 4 and any(c.isalpha() for c in t):
            found.append((m.start(), t.replace("\n", "\\n").replace("\t", "\\t")))
    for m in _UTF16_RUN.finditer(data):
        t = m.group().decode("utf-16-le").strip()
        if any(c.isalpha() for c in t):
            found.append((m.start(), t))
    found.sort()
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "strings.txt").write_text(
        f"# Text found inside {Path(lib).name} ({kind}); offset in hex, then the text. A few characters of "
        "binary noise can stick to the front of a line.\n"
        + "\n".join(f"{off:08x}  {t}" for off, t in found), encoding="utf-8")
    files = sorted({t for _o, t in found for t in re.findall(r"package:[\w./-]+\.dart|[\w/.-]+\.dart\b", t)})
    if files:
        (dest / "dart_files.txt").write_text("\n".join(files), encoding="utf-8")
    return len(found), len(files)


def build_maincode(out, log):
    """Create maincode/: the app's own code, apart from the thousands of library/SDK files around it."""
    out = Path(out)
    res, src = out / "resources", out / "sources"
    if not (res.is_dir() or src.is_dir()):
        return
    main = out / "maincode"
    main.mkdir(exist_ok=True)
    notes = ["# maincode", "",
             "The app's own code, picked out of the pile. Everything jadx produced is still in `sources/` and "
             "`resources/`; this folder is the shortcut.", ""]
    manifest = res / "AndroidManifest.xml"
    mtext = manifest.read_text(encoding="utf-8", errors="replace") if manifest.is_file() else ""
    if mtext:
        shutil.copy2(manifest, main / "AndroidManifest.xml")
    prefixes = _manifest_prefixes(mtext) if mtext else []
    java_dir = main / "java"
    linked = []
    prefixes = [p for p in prefixes if src.joinpath(*p.split(".")).is_dir()]
    for pre in prefixes:
        d = src.joinpath(*pre.split("."))
        if d.is_dir():
            _link_tree(d, java_dir / Path(*pre.split(".")))
            n = sum(1 for _ in d.rglob("*.java"))
            linked.append(f"`java/{pre.replace('.', '/')}` ({n} files)")
    if linked:
        notes += ["## Java / Kotlin", "", "The app's own Android-side code (named after the app's package and the classes its "
                  "manifest starts): " + ", ".join(linked) + ".", ""]
    else:
        notes += ["## Java / Kotlin", "", "No readable package of the app's own was found (everything is obfuscated or "
                  "from libraries). Look in `sources/` for single-letter packages.", ""]
    # other readable packages under com/: the user can pull in more if the guess missed
    com = src / "com"
    if com.is_dir():
        picked = {p.split(".")[1] for p in prefixes if p.startswith("com.")}
        others = []
        for p in sorted(com.iterdir()):
            if p.is_dir() and p.name not in picked and p.name not in _SDK_ROOTS and len(p.name) > 2:
                others.append((sum(1 for _ in p.rglob("*.java")), p.name))
        if others:
            others.sort(reverse=True)
            (main / "other_packages.txt").write_text(
                "Readable packages under sources/com that were NOT put in maincode (libraries or unknown):\n"
                + "\n".join(f"{n:6d} files  com/{name}" for n, name in others), encoding="utf-8")
            notes += ["`other_packages.txt` lists the remaining readable packages, in case the real game code is one of them.", ""]
    # Unity
    meta = next(res.rglob("global-metadata.dat"), None) if res.is_dir() else None
    if meta is not None:
        try:
            import il2cpp
            summary = il2cpp.dump(meta, main / "unity", log)
            log("About this app: Unity IL2CPP game. " + summary)
            notes += ["## Unity game code (IL2CPP)", "",
                      "This game's own code is C# that was compiled to native code, so it is not Java. What the game still "
                      "carries is a list of every class, field and method NAME. They are written to `unity/`:", "",
                      "- `unity/Assembly-CSharp.cs`: the game's classes with their fields, properties and methods "
                      "(names and parameter names only: types and method bodies are in `lib/*/libil2cpp.so`)",
                      "- `unity/other_assemblies/`: the same for Unity, plugins and libraries",
                      "- `unity/string_literals.txt`: every text string in the game's code",
                      "- `unity/all_names.txt`: every identifier, one per line (good for searching)", "",
                      f"({summary})", ""]
        except Exception as e:
            log(f"(Unity metadata could not be read: {e}. The game may encrypt it.)")
            notes += ["## Unity game code (IL2CPP)", "", f"The metadata file is there but could not be read ({e}); the game may encrypt it.", ""]
    elif res.is_dir() and any(res.rglob("Assembly-CSharp.dll")):
        dll = next(res.rglob("Assembly-CSharp.dll"))
        (main / "unity").mkdir(exist_ok=True)
        shutil.copy2(dll, main / "unity" / dll.name)
        notes += ["## Unity game code (Mono)", "", "`unity/Assembly-CSharp.dll` is the game's C# code: drop it on Decompiler Studio to read it.", ""]
    # Flutter: the real code is Dart inside libapp.so; make its text searchable
    libs = sorted(res.rglob("libapp.so")) if res.is_dir() else []
    if libs and any(res.rglob("libflutter.so")):
        lib = next((l for l in libs if "arm64" in str(l)), libs[0])
        try:
            n, nf = dump_native_strings(lib, main / "flutter", log, "Flutter Dart snapshot")
            log(f"Flutter app: {n} text strings from {lib.name} written to maincode/flutter/strings.txt ({nf} Dart file paths).")
            notes += ["## Flutter app (Dart)", "",
                      "This app's own code is Dart, compiled into `lib/*/libapp.so`, so it is not in the Java output. "
                      "Everything readable inside it is dumped to `flutter/`:", "",
                      "- `flutter/strings.txt`: every text string (screen text such as button labels, class and function names, URLs)",
                      "- `flutter/dart_files.txt`: the app's Dart source file paths (`package:app/...`)", "",
                      "That gives names and text, not method bodies. For full Dart code use blutter or reFlutter on libapp.so.", ""]
        except Exception as e:
            log(f"(Flutter strings could not be read: {e})")
    (main / "README.md").write_text("\n".join(notes), encoding="utf-8")
    log(f"maincode folder ready ({', '.join(prefixes) or 'no own packages found'}).")


def apk_insights(out):
    """Plain-language findings about an unpacked APK that explain why the Java may look 'wrong'."""
    out = Path(out)
    res = out / "resources"
    notes = []

    def has(pattern):
        return any(res.rglob(pattern)) if res.is_dir() else False

    if has("libil2cpp.so") or has("global-metadata.dat"):
        notes.append("This is a Unity game built with IL2CPP. The game's own code was compiled to native code "
                     "(lib/*/libil2cpp.so, described by assets/bin/Data/Managed/Metadata/global-metadata.dat) and is NOT in "
                     "the Java output. The Java you see is only the Android wrapper plus ad/analytics libraries. "
                     "To get the game's class and method names, run a tool made for this (Il2CppDumper or Cpp2IL) on "
                     "those two files; dropping libil2cpp.so here gives C that is hard to read because it has no names.")
    elif has("Assembly-CSharp.dll"):
        notes.append("This is a Unity game (Mono). The game's code is C# in assets/bin/Data/Managed/Assembly-CSharp.dll: "
                     "drop that file on this window to read it.")
    if has("libflutter.so") and has("libapp.so"):
        notes.append("This is a Flutter app. Its real code is Dart, compiled into lib/*/libapp.so, not Java. Its text (screen labels, class and function names) is dumped to maincode/flutter/strings.txt so search can find it.")
    if has("index.android.bundle"):
        notes.append("This is a React Native app. Its real code is JavaScript in assets/index.android.bundle.")
    src = out / "sources"
    if src.is_dir():
        tops = [p for p in src.iterdir() if p.is_dir()]
        short = [p for p in tops if len(p.name) <= 2]
        if len(tops) >= 20 and len(short) * 3 >= len(tops):
            notes.append(f"The code is obfuscated (shrunk by R8/ProGuard): {len(short)} of {len(tops)} top-level packages "
                         "have names like a, b, a0. Original class, method and variable names are not stored in the app, "
                         "so no tool can bring them back. Look for readable names in the packages that were left alone.")
        com = src / "com"
        if com.is_dir():
            counts = []
            for p in com.iterdir():
                if p.is_dir():
                    counts.append((sum(1 for _ in p.rglob("*.java")), p.name))
            counts.sort(reverse=True)
            if counts:
                notes.append("Biggest packages under sources/com: " + ", ".join(f"{n} ({c} files)" for c, n in counts[:6]) + ".")
    if (res / "AndroidManifest.xml").is_file():
        notes.append("Start in the maincode folder: it holds the app's own code and, for Unity games, the class and method names. The resources folder is everything else inside the APK (manifest, images, assets, the original "
                     ".dex files, native .so libraries). It is not source code.")
    return notes


def extract_zip(path, out, log):
    with zipfile.ZipFile(path) as z:
        z.extractall(out)
    log("Extracted archive contents.")


def _decompile_one_pyc(pyc, dest_dir, log_fail):
    """pyc -> .py when a decompiler supports it, always also write a .disasm.txt on failure."""
    pyc = Path(pyc)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    py = dest_dir / (pyc.stem + ".py")
    err = None
    try:
        from uncompyle6.main import decompile_file
        with open(py, "w", encoding="utf-8") as fh:
            decompile_file(str(pyc), fh)
        if py.stat().st_size > 0:
            return True
        err = "empty output"
    except BaseException as ex:  # decompilers raise odd things on unsupported versions
        err = str(ex)[:100]
    if py.exists():
        py.unlink()
    dis_out = dest_dir / (pyc.stem + ".disassembly.txt")
    try:
        from xdis.disasm import disassemble_file
        with open(dis_out, "w", encoding="utf-8") as fh:
            disassemble_file(str(pyc), fh)
    except BaseException:
        try:
            import marshal, dis
            code = marshal.loads(Path(pyc).read_bytes()[16:])
            with open(dis_out, "w", encoding="utf-8") as fh:
                dis.dis(code, file=fh)
        except BaseException as ex2:
            log_fail(f"{pyc.name}: {err}; disassembly failed too: {str(ex2)[:60]}")
            return False
    return False


PY_FILE_LIMIT = 60   # seconds per .pyc before it is skipped


def _stdlib_disasm(pyc, dest_dir, note):
    import marshal, dis
    Path(dest_dir).mkdir(parents=True, exist_ok=True)
    dest = Path(dest_dir) / (Path(pyc).stem + ".disassembly.txt")
    try:
        code = marshal.loads(Path(pyc).read_bytes()[16:])
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(f"# {note}\n")
            dis.dis(code, file=fh)
    except Exception as ex:
        dest.write_text(f"# {note}\n# bytecode could not be listed either: {ex}\n")


def python_batch_worker(listfile):
    """Runs in a child process: decompile each .pyc listed, reporting progress on stdout."""
    import sys as _s
    items = [l.rstrip("\n").split("\t") for l in open(listfile, encoding="utf-8") if l.strip()]
    for i, (pyc, dest) in enumerate(items):
        print(f"START {i}", flush=True)
        try:
            ok = _decompile_one_pyc(pyc, dest, lambda m: None)
        except BaseException:
            ok = False
        print(f"{'OK' if ok else 'BAD'} {i}", flush=True)
    _s.stdout.flush()


def decompile_pyc_batch(items, log):
    """items: list of (pyc_path, dest_dir). Each file gets PY_FILE_LIMIT seconds; a stuck
    file is killed and listed as bytecode instead, then the rest continue. Returns (ok, bad, stuck)."""
    import queue, tempfile
    ok = bad = stuck = 0
    limit = max(1, int(PY_FILE_LIMIT * float(os.environ.get("DECOMPILER_STUDIO_TIMEOUT_SCALE", "1"))))
    todo = list(items)
    while todo:
        check_cancel()
        fd, listfile = tempfile.mkstemp(suffix=".txt", prefix="ds-pyc-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for pyc, dest in todo:
                fh.write(f"{pyc}\t{dest}\n")
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--py-batch", listfile]
        else:
            cmd = [sys.executable, str(Path(__file__).with_name("app.py")), "--py-batch", listfile]
        kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WIN else {"start_new_session": True}
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, **kw)
        _PROCS.add(proc)
        q = queue.Queue()
        threading.Thread(target=lambda: [q.put(l.strip()) for l in proc.stdout] + [q.put(None)],
                         daemon=True).start()
        current, started, done_upto = None, _time.time(), 0
        try:
            while True:
                if CANCEL.is_set():
                    _kill(proc)
                    raise Cancelled()
                try:
                    line = q.get(timeout=0.2)
                except queue.Empty:
                    line = ""
                if line is None:
                    break
                if line.startswith("START "):
                    current, started = int(line[6:]), _time.time()
                elif line.startswith(("OK ", "BAD ")):
                    idx = int(line.split()[1])
                    if line.startswith("OK"):
                        ok += 1
                    else:
                        bad += 1
                    done_upto, current = idx + 1, None
                    if (ok + bad) % 25 == 0:
                        log(f"  ...{ok + bad + stuck} Python files processed")
                if current is not None and _time.time() - started > limit:
                    _kill(proc)
                    pyc, dest = todo[current]
                    log(f"  {Path(pyc).name}: decompiler was stuck for {limit}s, skipped (bytecode listing written instead)")
                    _stdlib_disasm(pyc, dest, f"Decompiler gave up after {limit}s on this file; bytecode listing instead.")
                    stuck += 1
                    done_upto, current = current + 1, None
                    break
        finally:
            _PROCS.discard(proc)
            try:
                os.unlink(listfile)
            except OSError:
                pass
        proc.wait()
        if current is not None:
            # worker died while on this file: list its bytecode instead and continue after it
            pyc, dest = todo[current]
            _stdlib_disasm(pyc, dest, "Decompiler crashed on this file; bytecode listing instead.")
            bad += 1
            done_upto = current + 1
        elif done_upto == 0:
            # worker could not start at all: list everything as bytecode
            log("  Python decompiler could not start; writing bytecode listings instead.")
            for pyc, dest in todo:
                _stdlib_disasm(pyc, dest, "Python decompiler could not start; bytecode listing instead.")
            bad += len(todo)
            done_upto = len(todo)
        todo = todo[done_upto:]
    return ok, bad, stuck


def decompile_python(path, out, log):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    o, b, s = decompile_pyc_batch([(str(path), str(out))], log)
    ok = o > 0
    log("Recovered Python source." if ok else
        "This bytecode is from a newer Python than the available decompilers support; wrote readable bytecode "
        "(.disassembly.txt) instead of source.")


STDLIB = set(getattr(sys, "stdlib_module_names", ())) | {"encodings", "_distutils_hack", "pkg_resources"}


def decompile_pyinstaller(path, out, log):
    import unpackers
    out = Path(out)
    pycs, ver = unpackers.extract_pyinstaller(path, out, log)
    src = out / "python_modules"
    skipped = 0
    batch = []
    for pyc in sorted(pycs):
        check_cancel()
        rel = pyc.relative_to(src)
        top = rel.parts[0].replace(".pyc", "")
        if top in STDLIB or top.startswith(("pyimod", "pyi_rth", "_pyi_rth")):
            skipped += 1
            pyc.unlink()  # standard library: not the program's own code
            continue
        batch.append((str(pyc), str(pyc.parent)))
    ok, bad, stuck = decompile_pyc_batch(batch, log)
    bad += stuck
    for pyc, _ in batch:
        try:
            os.unlink(pyc)
        except OSError:
            pass
    for d in sorted((p for p in src.rglob("*") if p.is_dir()), reverse=True):
        if not any(d.iterdir()):
            d.rmdir()
    log(f"Python modules: {ok} decompiled to .py, {bad} left as bytecode listings, {skipped} standard-library files skipped.")
    if bad and ver >= (3, 9):
        log(f"Built with Python {ver[0]}.{ver[1]}: no available decompiler can rebuild source for this version, "
            "so those modules are readable bytecode (*.disassembly.txt).")


def decompile_dotnet(path, out, log):
    out = Path(out)
    ilspy = shutil.which("ilspycmd")
    if ilspy:
        limit = max(1, int(1200 * float(os.environ.get("DECOMPILER_STUDIO_TIMEOUT_SCALE", "1"))))
        try:
            run_cmd([ilspy, "-p", "-o", str(out), path], log, timeout=limit)
        except TimedOut:
            log(f"ilspycmd was still running after {_dur(limit)} and was stopped; showing what it finished.")
        return
    import dnfile
    log("ilspycmd not installed; writing a structure listing (namespaces, types, methods) "
        "from the assembly metadata. Install ilspycmd for full C# output.")
    pe = dnfile.dnPE(path)
    md = pe.net.mdtables
    strings = pe.net.mdstream_streams.get("#Strings") if hasattr(pe.net, "mdstream_streams") else None
    types = {}
    if md.TypeDef:
        for row_idx, t in enumerate(md.TypeDef, 1):
            ns = str(t.TypeNamespace or "").strip() or "_global"
            types.setdefault(ns, []).append(t)
    for ns, tlist in types.items():
        for t in tlist:
            tname = str(t.TypeName)
            lines = [f"// namespace {ns}", f"class {tname} {{"]
            for m in (t.MethodList or []):
                try:
                    mname = str(m.row.Name)
                except Exception:
                    mname = str(getattr(m, "Name", "?"))
                lines.append(f"    // method: {mname}()")
            lines.append("}")
            d = out / ns.replace(".", "/")
            d.mkdir(parents=True, exist_ok=True)
            safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in tname)
            (d / f"{safe}.cs").write_text("\n".join(lines) + "\n")
    refs = []
    if md.AssemblyRef:
        refs = [str(r.Name) for r in md.AssemblyRef]
    (out / "_references.txt").write_text("\n".join(refs) + "\n")


def find_ghidra():
    """Bundled Ghidra first, then GHIDRA_INSTALL_DIR."""
    for base in (app_dir() / "backends" / "ghidra", Path(os.environ.get("GHIDRA_INSTALL_DIR", "/nonexistent"))):
        if (base / "Ghidra" / "application.properties").exists():
            return base
    return None


def _java_major(java):
    """Major version of a java executable (25 for '25.0.4', 8 for '1.8.0'), or None."""
    try:
        r = subprocess.run([java, "-version"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "JAVA_TOOL_OPTIONS": ""})
        m = re.search(r'version "(\d+)(?:\.(\d+))?', r.stderr + r.stdout)
        if m:
            return int(m.group(2)) if m.group(1) == "1" and m.group(2) else int(m.group(1))
    except Exception:
        pass
    return None


def _ghidra_min_java(ghidra):
    try:
        for line in (ghidra / "Ghidra" / "application.properties").read_text().splitlines():
            if line.startswith("application.java.min="):
                return int(line.split("=", 1)[1])
    except Exception:
        pass
    return 21


def _stamp_notice(target):
    text = target.read_text(encoding="utf-8", errors="replace")
    target.write_text(f"// WARNING: {NOTICE}\n" + text, encoding="utf-8")


def ensure_helper(jar, log):
    """Make sure the Ghidra helper on disk is the one this app was built with. A patch that only replaced
    part of the files (or a read-only jar) would otherwise leave an old helper in charge."""
    try:
        import base64
        import hashlib
        import helper_blob
    except Exception:
        return None
    want = base64.b64decode(helper_blob.DATA)
    try:
        have = Path(jar).read_bytes() if Path(jar).exists() else b""
        if hashlib.sha256(have).digest() != hashlib.sha256(want).digest():
            try:
                os.chmod(jar, 0o644)
            except OSError:
                pass
            tmp = Path(str(jar) + ".new")
            tmp.write_bytes(want)
            os.replace(tmp, jar)
            log(f"Updated the Ghidra helper to v{helper_blob.VERSION}.")
    except OSError as e:
        log(f"WARNING: the Ghidra helper on disk is out of date and could not be replaced ({e}). "
            f"Copy ds-ghidra.jar from the patch over {jar} yourself.")
    return helper_blob.VERSION


def decompile_ghidra(path, out, log):
    """Native binary -> C-like source using the bundled Ghidra (headless). Returns True on success."""
    ghidra = find_ghidra()
    if ghidra is None:
        log("Ghidra backend not found (expected next to the app in backends/ghidra).")
        return False
    java = find_java()
    if not java:
        log("Ghidra needs Java, and no Java runtime was found.")
        return False
    need = _ghidra_min_java(ghidra)
    have = _java_major(java)
    if have is not None and have < need:
        log(f"Ghidra needs Java {need} or newer, but the Java found ({java}) is version {have}.")
        return False
    utility = ghidra / "Ghidra" / "Framework" / "Utility" / "lib" / "Utility.jar"
    module = ghidra / "Ghidra" / "Features" / "DecompilerStudio"
    try:                                       # a plain Ghidra (source install, GHIDRA_INSTALL_DIR) gets our module added
        (module / "lib").mkdir(parents=True, exist_ok=True)
        if not (module / "Module.manifest").exists():
            (module / "Module.manifest").write_text("")
    except OSError:
        pass
    expect = ensure_helper(module / "lib" / "ds-ghidra.jar", log)
    if not (module / "lib" / "ds-ghidra.jar").exists():
        log("Ghidra backend is incomplete (ds-ghidra.jar is missing and could not be written).")
        return False
    target = Path(out) / "decompiled.c"
    base_cmd = [
        java,
        "-Djava.system.class.loader=ghidra.GhidraClassLoader",
        "-Dfile.encoding=UTF8", "-Duser.country=US", "-Duser.language=en",
        "-Djava.awt.headless=true", "-Xshare:off", "-XX:MaxRAMPercentage=60",
        "--enable-native-access=ALL-UNNAMED", "--sun-misc-unsafe-memory-access=allow",
        "-cp", str(utility), "ghidra.Ghidra", "DsGhidra",
    ]
    # Ghidra's own analysis + decompiler can take a long time on big programs. Each attempt has
    # time limits that the helper obeys itself (it keeps whatever finished); a second attempt
    # skips Ghidra's analysis, which is the slow part, and only decompiles what the file declares.
    scale = float(os.environ.get("DECOMPILER_STUDIO_GHIDRA_TIMEOUT_SCALE", "1"))
    attempts = [
        ("normal", "1", 600, 900),
        ("in quick mode, without Ghidra's analysis step", "0", 0, 600),
    ]

    def show(line):
        # Ghidra prints pages of analyser timings; show only our progress lines and real problems
        if line.startswith("[ds] "):
            log(line[5:])
            m = re.search(r"\(helper v(\S+)\)", line)
            if m and expect and m.group(1) != expect:
                log(f"WARNING: an old Ghidra helper (v{m.group(1)}) is running, this app expects v{expect}. "
                    "Output will not look right until ds-ghidra.jar is replaced.")
        elif line.startswith(("Exception in thread", "Caused by:")) or "java.lang." in line \
                or "UnsupportedClassVersionError" in line:
            log(line)

    for i, (label, analyze, a_s, d_s) in enumerate(attempts):
        a_s, d_s = max(1, int(a_s * scale)), max(1, int(d_s * scale))
        hard = a_s + d_s + 240        # the helper stops itself well before this
        if i == 0:
            log(f"Decompiling to C with Ghidra (analysis up to {_dur(a_s)}, decompiling up to {_dur(d_s)})...")
        else:
            log(f"Retrying Ghidra {label}...")
        for leftover in list(Path(out).glob("decompiled*")) + [Path(out) / "types.h"]:
            if leftover.is_file():
                leftover.unlink()
        try:
            rc = run_cmd(base_cmd + [str(path), str(target), str(a_s), str(d_s), analyze], show, timeout=hard)
        except TimedOut:
            log(f"Ghidra took longer than {_dur(hard)} and was stopped.")
            rc = "timeout"
        if target.exists() and target.stat().st_size > 0 and rc == 0:
            try:                                       # readability pass (see tidy.py); never fatal
                import tidy
                raw = target.read_text(encoding="utf-8", errors="replace")
                target.write_text(tidy.tidy_c(raw), encoding="utf-8")
                _stamp_notice(target)
            except Exception as e:
                log(f"(readability pass skipped: {e})")
            if i > 0:
                log(f"Ghidra finished on attempt {i + 1} ({label}).")
            return True
        log(f"Ghidra did not produce C code (exit {rc}).")
    return target.exists() and target.stat().st_size > 0


def _has_debug_info(path, kind):
    """True/False: does the file carry debug info (DWARF, e.g. built with -g)? None if unknown."""
    try:
        if kind == "elf":
            from elftools.elf.elffile import ELFFile
            with open(path, "rb") as f:
                # not has_dwarf_info(): that is also true for plain .eh_frame unwind data
                return any(sec.name in (".debug_info", ".zdebug_info") for sec in ELFFile(f).iter_sections())
        import pefile
        pe = pefile.PE(path, fast_load=True)
        for sec in pe.sections:
            name = sec.Name.rstrip(b"\x00")
            if name.startswith(b".debug") or re.fullmatch(rb"/\d+", name):   # MinGW/GCC -g
                return True
        return False
    except Exception:
        return None


NO_DEBUG_HINT = ("No debug info in this file (it was built without -g), so original variable names, "
                 "parameter names and struct layouts are not stored in it and cannot be recovered. "
                 "If this is your own program, rebuild it with -g (add -O0 for the closest match to "
                 "your source) and open it again.")


def decompile_native(path, out, log, kind):
    """Native binaries: C-like source via Ghidra, plus raw disassembly in disassembly/."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    got_c = False
    try:
        got_c = decompile_ghidra(path, out, log)
    except Cancelled:
        raise
    except Exception as e:
        log(f"Ghidra failed: {e}")
    if got_c:
        log("C source written to decompiled.c (names of functions/variables in stripped binaries are lost for good).")
        dbg = _has_debug_info(path, kind)
        if dbg is False:
            log(NO_DEBUG_HINT)
        elif dbg:
            log("Debug info found in the file: names and struct types come from it, so the C should be close to the original.")
        asm_out = out / "disassembly"
        asm_out.mkdir()
        out_real, out = out, asm_out
    else:
        log("Ghidra unavailable or failed; falling back to assembly output.")
    r2 = shutil.which("r2") or shutil.which("radare2")
    if r2:
        log("radare2 found: generating pseudo-C for functions.")
        try:
            res = subprocess.run([r2, "-q", "-e", "scr.color=0", "-c", "aaa; afl", path],
                                 capture_output=True, text=True, timeout=300)
            (out / "functions.txt").write_text(res.stdout)
        except Exception as e:
            log(f"radare2 step failed: {e}")
    import capstone
    if kind == "elf":
        _native_elf(path, out, log, capstone)
    else:
        _native_pe(path, out, log, capstone)
    _strings(path, out)
    if got_c:
        shutil.move(str(out / "_strings.txt"), str(out_real / "_strings.txt"))
        shutil.move(str(out / "_info.txt"), str(out_real / "_info.txt")) if (out / "_info.txt").exists() else None


def _write_funcs(out, code_bytes, base, funcs, md, log, label):
    fdir = Path(out) / "functions"
    fdir.mkdir(parents=True, exist_ok=True)
    all_lines = []
    written = 0
    for name, addr, size in funcs:
        check_cancel()
        lo = addr - base
        if lo < 0 or lo >= len(code_bytes) or size <= 0:
            continue
        chunk = code_bytes[lo:lo + size]
        lines = [f"; {name}  @ 0x{addr:x}  size {size}"]
        for ins in md.disasm(chunk, addr):
            lines.append(f"  0x{ins.address:x}:  {ins.mnemonic:<8} {ins.op_str}")
        txt = "\n".join(lines) + "\n"
        safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in name)[:80] or f"sub_{addr:x}"
        (fdir / f"{safe}.asm").write_text(txt)
        all_lines.append(txt)
        written += 1
    (Path(out) / f"{label}_full_disassembly.asm").write_text("\n".join(all_lines))
    log(f"Disassembled {written} functions.")


def _native_elf(path, out, log, capstone):
    from elftools.elf.elffile import ELFFile
    with open(path, "rb") as f:
        elf = ELFFile(f)
        arch = elf.get_machine_arch()
        arch_map = {
            "x64": (capstone.CS_ARCH_X86, capstone.CS_MODE_64),
            "x86": (capstone.CS_ARCH_X86, capstone.CS_MODE_32),
            "AArch64": (capstone.CS_ARCH_ARM64, capstone.CS_MODE_ARM),
            "ARM": (capstone.CS_ARCH_ARM, capstone.CS_MODE_ARM),
        }
        if arch not in arch_map:
            raise RuntimeError(f"Architecture {arch} is not supported for disassembly yet.")
        md = capstone.Cs(*arch_map[arch])
        text = elf.get_section_by_name(".text")
        info = [f"Architecture: {arch}", f"Entry point: 0x{elf['e_entry']:x}", "", "Sections:"]
        for s in elf.iter_sections():
            info.append(f"  {s.name:<20} addr=0x{s['sh_addr']:x} size={s['sh_size']}")
        (Path(out) / "_info.txt").write_text("\n".join(info) + "\n")
        funcs = []
        for symtab_name in (".symtab", ".dynsym"):
            sym = elf.get_section_by_name(symtab_name)
            if not sym:
                continue
            for s in sym.iter_symbols():
                if s["st_info"]["type"] == "STT_FUNC" and s["st_size"] > 0 and s["st_shndx"] != "SHN_UNDEF":
                    funcs.append((s.name, s["st_value"], s["st_size"]))
            if funcs:
                break
        if text is None:
            raise RuntimeError("No .text section found.")
        code = text.data()
        base = text["sh_addr"]
        if not funcs:
            log("Binary is stripped: disassembling the whole .text section.")
            funcs = [("text_section", base, len(code))]
        funcs = sorted(set(funcs), key=lambda x: x[1])
        _write_funcs(out, code, base, funcs, md, log, "elf")


def _native_pe(path, out, log, capstone):
    import pefile
    pe = pefile.PE(path)
    is64 = pe.FILE_HEADER.Machine == 0x8664
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64 if is64 else capstone.CS_MODE_32)
    base = pe.OPTIONAL_HEADER.ImageBase
    info = [f"Architecture: {'x64' if is64 else 'x86'}",
            f"Image base: 0x{base:x}",
            f"Entry point RVA: 0x{pe.OPTIONAL_HEADER.AddressOfEntryPoint:x}", "", "Sections:"]
    for s in pe.sections:
        info.append(f"  {s.Name.rstrip(b'\\x00').decode(errors='replace'):<10} "
                    f"rva=0x{s.VirtualAddress:x} size={s.Misc_VirtualSize}")
    info.append("\nImports:")
    for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", []):
        info.append(f"  {entry.dll.decode(errors='replace')}")
        for imp in entry.imports:
            info.append(f"      {imp.name.decode(errors='replace') if imp.name else imp.ordinal}")
    exports = []
    if hasattr(pe, "DIRECTORY_ENTRY_EXPORT"):
        info.append("\nExports:")
        for e in pe.DIRECTORY_ENTRY_EXPORT.symbols:
            nm = e.name.decode(errors="replace") if e.name else f"ord_{e.ordinal}"
            info.append(f"  {nm}  rva=0x{e.address:x}")
            exports.append((nm, e.address))
    (Path(out) / "_info.txt").write_text("\n".join(info) + "\n")
    code_sec = next((s for s in pe.sections if s.Characteristics & 0x20000000), pe.sections[0])
    code = code_sec.get_data()
    sec_rva = code_sec.VirtualAddress
    funcs = []
    exports = sorted(e for e in exports if sec_rva <= e[1] < sec_rva + len(code))
    for i, (nm, rva) in enumerate(exports):
        end = exports[i + 1][1] if i + 1 < len(exports) else sec_rva + len(code)
        funcs.append((nm, base + rva, min(end - rva, 4096)))
    ep = pe.OPTIONAL_HEADER.AddressOfEntryPoint
    if sec_rva <= ep < sec_rva + len(code):
        funcs.append(("entry_point", base + ep, min(sec_rva + len(code) - ep, 4096)))
    if not funcs:
        funcs = [("code_section", base + sec_rva, len(code))]
    _write_funcs(out, code, base + sec_rva, funcs, md, log, "pe")


def _strings(path, out, minlen=5):
    import re
    data = Path(path).read_bytes()
    found = re.findall(rb"[\x20-\x7e]{%d,}" % minlen, data)
    (Path(out) / "_strings.txt").write_text("\n".join(s.decode() for s in found[:20000]) + "\n")


NOTES = {
    "apk": ("jadx", "Java source. Names are kept unless the app was obfuscated (then you will see a.b.c style names). Comments and original formatting are not stored in the app."),
    "dex": ("jadx", "Java source. Comments and formatting are not stored."),
    "jar": ("jadx", "Java source. Comments and formatting are not stored; local variable names are lost unless the jar was built with debug info."),
    "class": ("jadx", "Java source for one class."),
    "dotnet": ("metadata reader", "Only a list of namespaces, classes and methods. Install ilspycmd for full C# output."),
    "elf": ("Ghidra + capstone", "C-like code reconstructed from machine code. This is NOT the original source: names, types, comments and macros are mostly gone, and the C may not be exactly equivalent. Check against disassembly/ when something looks wrong."),
    "pe": ("Ghidra + capstone", "C-like code reconstructed from machine code. This is NOT the original source: names, types, comments and macros are mostly gone, and the C may not be exactly equivalent. Check against disassembly/ when something looks wrong."),
    "pyinstaller": ("built-in PyInstaller unpacker + uncompyle6/decompyle3", "Python modules extracted from the bundle. Source is rebuilt only for Python 3.8 and older; newer versions give bytecode listings."),
    "pyc": ("uncompyle6/decompyle3", "Source for Python 3.8 and older, bytecode listing for newer versions."),
    "asar": ("built-in asar reader", "Original JavaScript/HTML/CSS files (Electron apps ship their source)."),
    "zip": ("zip extractor", "Archive contents, unchanged."),
    "source": ("none", "Plain source file shown as-is."),
}


NOTICE = "USE FOR PERSONAL AND EDUCATIONAL PURPOSES ONLY"


def write_report(path, out, kind, log_lines):
    tool, note = NOTES.get(kind, ("unknown", ""))
    files = [p for p in Path(out).rglob("*") if p.is_file() and p.name != "REPORT.md"]
    exts = {}
    for p in files:
        exts[p.suffix or "(none)"] = exts.get(p.suffix or "(none)", 0) + 1
    lines = [f"# Decompiler Studio report", "", f"> **WARNING: {NOTICE}**", "",
             f"- Input: `{Path(path).name}`",
             f"- Detected as: {KIND_LABEL.get(kind, kind)}",
             f"- Method: {tool}",
             f"- Output files: {len(files)} ({', '.join(f'{n} {e}' for e, n in sorted(exts.items(), key=lambda x: -x[1])[:6])})",
             "", "## What to expect", note, ""]
    warn = [l for l in log_lines
            if any(k in l.lower() for k in ("could not", "failed", "unavailable", "falling back", "no decompiler", "newer python"))
            and "(exit 0)" not in l]
    if warn:
        lines += ["## Problems during this run"] + [f"- {w}" for w in warn] + [""]
    about = [l[len("About this app: "):] for l in log_lines if l.startswith("About this app: ")]
    if about:
        lines += ["## About this app"] + [f"- {a}" for a in about] + [""]
    if any(l.startswith("No debug info") for l in log_lines):
        lines += ["## Getting closer to the original", NO_DEBUG_HINT, "",
                  "With -g, Ghidra reads the debug info and restores parameter and variable names, "
                  "struct types and field names; code that looked like `*(float *)(CubeObject + i * 0x40 + 4) = y` "
                  "comes back as `CubeObject[i].y = y`.", ""]
    (Path(out) / "REPORT.md").write_text("\n".join(lines))


def decompile(path: str, out_dir: str, log=print) -> str:
    CANCEL.clear()
    kind = detect(path)
    seen = []
    _log = log
    def log(m, _l=_log):
        seen.append(str(m))
        _l(m)
    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    log(f"WARNING: {NOTICE}.")
    log(f"Detected: {KIND_LABEL.get(kind, kind)}")
    if kind in ("apk", "dex", "jar", "class"):
        decompile_jadx(path, out, log)
    elif kind == "zip":
        extract_zip(path, out, log)
    elif kind == "pyc":
        decompile_python(path, out, log)
    elif kind == "dotnet":
        decompile_dotnet(path, out, log)
    elif kind == "pyinstaller":
        decompile_pyinstaller(path, out, log)
    elif kind == "asar":
        import unpackers
        unpackers.extract_asar(path, out, log)
    elif kind in ("elf", "pe"):
        decompile_native(path, out, log, kind)
    elif kind == "source":
        shutil.copy(path, out / Path(path).name)
        log("Plain source file: shown as-is.")
    else:
        raise RuntimeError("Unrecognised file type.")
    try:
        write_report(path, out, kind, seen)
    except Exception as ex:
        log(f"(could not write REPORT.md: {ex})")
    n = sum(1 for _ in out.rglob("*") if _.is_file())
    log(f"Done: {n} files written.")
    return kind
