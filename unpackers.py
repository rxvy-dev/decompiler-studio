"""Unpackers for programs that embed their real code inside an executable."""
import json
import marshal
import struct
import zlib
from pathlib import Path

PYI_MAGIC = b"MEI\x0c\x0b\x0a\x0b\x0e"


def find_pyinstaller(path):
    """Return (cookie_pos, is21) if the file carries a PyInstaller archive, else None."""
    data = Path(path).read_bytes()
    pos = data.rfind(PYI_MAGIC)
    if pos < 0:
        return None
    return pos


def extract_pyinstaller(path, out, log):
    """Extract a PyInstaller bundle. Returns list of recovered .pyc files (Paths)."""
    out = Path(out)
    data = Path(path).read_bytes()
    size = len(data)
    cookie = data.rfind(PYI_MAGIC)
    if cookie < 0:
        raise RuntimeError("Not a PyInstaller archive.")
    new_fmt = b"python" in data[cookie + 24:cookie + 88].lower()
    _, pkg_len, toc_pos, toc_len, pyver = struct.unpack("!8siiii", data[cookie:cookie + 24])
    cookie_size = 88 if new_fmt else 24
    tail = size - cookie - cookie_size
    overlay_pos = size - (pkg_len + max(tail, 0))
    if pyver >= 100:
        pymajor, pyminor = pyver // 100, pyver % 100
    else:
        pymajor, pyminor = pyver // 10, pyver % 10
    log(f"PyInstaller archive found, built with Python {pymajor}.{pyminor}.")

    toc_data = data[overlay_pos + toc_pos: overlay_pos + toc_pos + toc_len]
    entries = []
    p = 0
    while p < len(toc_data):
        (elen,) = struct.unpack("!i", toc_data[p:p + 4])
        epos, csize, usize, flag, typ = struct.unpack("!iiiBc", toc_data[p + 4:p + 18])
        name = toc_data[p + 18:p + elen].split(b"\x00", 1)[0].decode("utf-8", "replace")
        entries.append((name, epos, csize, usize, flag, typ.decode("latin1")))
        p += elen

    raw_dir = out / "pyinstaller_raw"
    src_dir = out / "python_modules"
    raw_dir.mkdir(parents=True, exist_ok=True)
    src_dir.mkdir(parents=True, exist_ok=True)

    magic = None
    pycs = []

    def blob(e):
        _, epos, csize, usize, flag, _t = e
        b = data[overlay_pos + epos: overlay_pos + epos + csize]
        return zlib.decompress(b) if flag == 1 else b

    # find the pyc magic from the PYZ header first
    for e in entries:
        if e[5] in ("z", "Z"):
            pyz = blob(e)
            magic = pyz[4:8]
            break
    if magic is None:
        import importlib.util
        magic = importlib.util.MAGIC_NUMBER
    header = magic + b"\x00" * (12 if (pymajor, pyminor) >= (3, 7) else 4)

    def safe(rel):
        rel = rel.replace("\\", "/").lstrip("/")
        parts = [x for x in rel.split("/") if x not in ("", ".", "..")]
        return Path(*parts) if parts else Path("unnamed")

    import engines
    for e in entries:
        engines.check_cancel()
        name, typ = e[0], e[5]
        try:
            b = blob(e)
        except Exception as ex:
            log(f"  skip {name}: {ex}")
            continue
        if typ == "s":  # entry-point script: marshalled code without header
            f = src_dir / (safe(name).as_posix() + ".pyc")
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(header + b)
            pycs.append(f)
        elif typ in ("m", "M"):
            f = src_dir / (safe(name).as_posix().replace(".", "/") + ".pyc")
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b if b[:4] == magic else header + b)
            pycs.append(f)
        elif typ in ("z", "Z"):
            pycs += _extract_pyz(b, magic, header, src_dir, log)
        elif typ in ("b", "d", "x", "a"):
            f = raw_dir / safe(name)
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b)
    log(f"Recovered {len(pycs)} Python modules.")
    return pycs, (pymajor, pyminor)


def _extract_pyz(pyz, magic, header, src_dir, log):
    res = []
    if pyz[:4] != b"PYZ\x00":
        return res
    (toc_pos,) = struct.unpack("!i", pyz[8:12])
    try:
        toc = marshal.loads(pyz[toc_pos:])
    except Exception as ex:
        log(f"  could not read PYZ table (different Python version/encrypted): {ex}")
        return res
    if isinstance(toc, list):
        toc = dict(toc)
    for name, (ispkg, pos, length) in toc.items():
        nm = name.decode() if isinstance(name, bytes) else name
        raw = pyz[pos:pos + length]
        try:
            code = zlib.decompress(raw)
        except Exception:
            continue  # encrypted PYZ
        rel = nm.replace(".", "/") + ("/__init__" if ispkg else "")
        f = src_dir / (rel + ".pyc")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(header + code)
        res.append(f)
    return res


# ---------------------------------------------------------------- Electron asar
def extract_asar(path, out, log):
    out = Path(out)
    with open(path, "rb") as f:
        head = f.read(16)
        _, _, _, json_len = struct.unpack("<IIII", head)
        header = json.loads(f.read(json_len).decode("utf-8"))
        base = 8 + struct.unpack("<I", head[4:8])[0]
        count = 0

        def walk(node, dest):
            nonlocal count
            import engines
            for name, meta in node.get("files", {}).items():
                engines.check_cancel()
                t = dest / name
                if "files" in meta:
                    t.mkdir(parents=True, exist_ok=True)
                    walk(meta, t)
                elif "offset" in meta and not meta.get("unpacked"):
                    f.seek(base + int(meta["offset"]))
                    t.parent.mkdir(parents=True, exist_ok=True)
                    t.write_bytes(f.read(int(meta["size"])))
                    count += 1

        out.mkdir(parents=True, exist_ok=True)
        walk(header, out)
    log(f"Extracted {count} files from the Electron app bundle (original JavaScript/HTML).")
