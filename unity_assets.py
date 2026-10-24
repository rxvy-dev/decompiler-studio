# SPDX-License-Identifier: GPL-3.0-or-later
# Decompiler Studio - Copyright (C) 2026 rxvy-dev. Free software under the GNU GPL v3 or later; see LICENSE.
"""Unity asset files (sharedassets0.assets, level0, *.bundle, data.unity3d ...): pull out what is inside.

These hold a game's content, not its code: textures, sprites, models, sounds, text and the saved
fields of scripts (MonoBehaviour). The code of an IL2CPP game is in libil2cpp.so + global-metadata.dat.
Uses UnityPy (MIT). Big .assets files are often cut into name.assets.split0, .split1, ... inside the
APK; they are joined back first, the way the game does it.
"""
import json
import os
import re
from pathlib import Path

ASSET_EXT = (".assets", ".bundle", ".unity3d", ".ab", ".resource", ".ress")
NAME_RE = re.compile(r"^(sharedassets\d+|level\d+|globalgamemanagers|resources|unity default resources|"
                     r"unity_builtin_extra|data)(\.assets|\.unity3d|\.resource|\.ress)?(\.split\d+)?$", re.I)


def is_unity_file(path):
    p = Path(path)
    name = p.name.lower()
    base = re.sub(r"\.split\d+$", "", name)
    if NAME_RE.match(name) or base.endswith(ASSET_EXT):
        return True
    try:
        with open(path, "rb") as f:
            head = f.read(64)
    except OSError:
        return False
    if head.startswith((b"UnityFS", b"UnityWeb", b"UnityRaw", b"UnityArchive")):
        return True
    # a plain serialized file has its Unity version ("2021.3.5f1") near the start
    return "Data" in p.parts and re.search(rb"(?:19|20)\d\d\.\d+\.\d+[abfp]\d+", head) is not None


def source_of(path):
    """The asset file to open for `path`: a .splitN piece -> the whole file; a .resS/.resource -> its .assets."""
    p = Path(path)
    m = re.match(r"^(.*)\.split\d+$", p.name, re.I)
    if m:
        return p.with_name(m.group(1)), "split"
    low = p.name.lower()
    if low.endswith((".ress", ".resource")):
        owner = p.with_name(p.name.rsplit(".", 1)[0] + ".assets")
        if owner.exists() or any(owner.parent.glob(owner.name + ".split*")):
            return owner, "owner"
    return p, "file"


def _join_splits(whole, dest, log):
    parts = sorted(whole.parent.glob(whole.name + ".split*"), key=lambda x: int(re.sub(r"\D", "", x.suffix) or 0))
    if not parts:
        return whole
    target = dest / whole.name
    with open(target, "wb") as w:
        for part in parts:
            w.write(part.read_bytes())
    log(f"Joined {len(parts)} pieces ({whole.name}.split0 ... .split{len(parts) - 1}) into one file.")
    return target


def _safe(name, fallback):
    name = "".join(c if c.isalnum() or c in " ._-()" else "_" for c in (name or "")).strip(" .")
    return (name or fallback)[:80]


def extract(path, root, log, check_cancel=lambda: None):
    """Extract one Unity asset file into <root>/unity_assets/<name>/. Returns that folder."""
    import UnityPy
    src, how = source_of(path)
    root = Path(root)
    out = root / "unity_assets" / _safe(src.name, "assets")
    out.mkdir(parents=True, exist_ok=True)
    if how == "split":
        log(f"{Path(path).name} is one piece of {src.name}; extracting the whole file.")
    elif how == "owner":
        log(f"{Path(path).name} holds raw texture/sound data for {src.name}; extracting {src.name}.")
    work = out / "_joined"
    work.mkdir(exist_ok=True)
    real = _join_splits(src, work, log) if not src.exists() else src
    if not real.exists():
        raise RuntimeError(f"Could not find {src.name}.")
    # textures/sounds of a .assets live in a .resS/.resource beside it: make them findable next to the joined copy
    if real.parent == work:
        for side in src.parent.glob(src.stem + ".*"):
            if side.suffix.lower() in (".ress", ".resource") and not (work / side.name).exists():
                try:
                    os.link(side, work / side.name)
                except OSError:
                    import shutil
                    shutil.copy(side, work / side.name)
    env = UnityPy.load(str(real))
    counts, index, made = {}, [], 0
    used = set()
    objs = list(env.objects)
    log(f"{len(objs)} object(s) inside. Extracting textures, sprites, sounds, text, models and script data...")
    for n, obj in enumerate(objs, 1):
        check_cancel()
        t = obj.type.name
        counts[t] = counts.get(t, 0) + 1
        if n % 500 == 0:
            log(f"Extracting: {n}/{len(objs)}")
        if t not in ("Texture2D", "Sprite", "TextAsset", "AudioClip", "Mesh", "MonoBehaviour", "Font", "Shader"):
            continue
        try:
            data = obj.read()
            name = _safe(getattr(data, "m_Name", "") or getattr(data, "name", ""), f"{t}_{obj.path_id}")
            folder = out / t
            folder.mkdir(exist_ok=True)

            def unique(fn):
                p = folder / fn
                k = 1
                while str(p) in used or p.exists():
                    stem, ext = os.path.splitext(fn)
                    p = folder / f"{stem}_{k}{ext}"
                    k += 1
                used.add(str(p))
                return p

            wrote = None
            if t in ("Texture2D", "Sprite"):
                img = data.image
                if img is not None and img.width and img.height:
                    wrote = unique(name + ".png")
                    img.save(wrote)
            elif t == "TextAsset":
                raw = data.m_Script
                raw = raw.encode("utf-8", "surrogateescape") if isinstance(raw, str) else bytes(raw)
                try:
                    raw.decode("utf-8")
                    ext = ".txt" if "." not in name else ""
                except UnicodeDecodeError:
                    ext = ".bytes" if "." not in name else ""
                wrote = unique(name + ext)
                wrote.write_bytes(raw)
            elif t == "AudioClip":
                for sname, sdata in (data.samples or {}).items():
                    wrote = unique(_safe(sname, name) if sname.lower().endswith(".wav") else _safe(sname, name) + ".wav")
                    wrote.write_bytes(sdata)
            elif t == "Mesh":
                text = data.export()
                if text:
                    wrote = unique(name + ".obj")
                    wrote.write_text(text, encoding="utf-8")
            elif t == "MonoBehaviour":
                try:
                    tree = obj.read_typetree()
                except Exception:
                    tree = None
                if tree:
                    wrote = unique(name + ".json")
                    wrote.write_text(json.dumps(tree, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
            elif t == "Font":
                fd = getattr(data, "m_FontData", None)
                if fd:
                    raw = bytes(fd)
                    wrote = unique(name + (".otf" if raw[:4] == b"OTTO" else ".ttf"))
                    wrote.write_bytes(raw)
            elif t == "Shader":
                try:
                    text = data.export()
                except Exception:
                    text = None
                if text:
                    wrote = unique(name + ".shader")
                    wrote.write_text(text, encoding="utf-8")
            if wrote:
                made += 1
                index.append(f"{t:<14} {wrote.relative_to(out)}")
        except Exception as e:
            index.append(f"{t:<14} (could not extract object {obj.path_id}: {str(e)[:80]})")
    summary = ", ".join(f"{v} {k}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))
    (out / "index.txt").write_text(
        f"Unity asset file: {src.name}\nObjects inside: {summary}\n\n"
        "Extracted files (images as PNG, sounds as WAV, models as OBJ, script data as JSON):\n\n"
        + "\n".join(index) + "\n\nNote: this is the game's content, not its code. For IL2CPP games the code is in "
        "libil2cpp.so (use Decompile this binary) and the names are in maincode/unity/.\n", encoding="utf-8")
    import shutil
    shutil.rmtree(work, ignore_errors=True)
    log(f"Extracted {made} file(s) into unity_assets/{out.name} ({summary}).")
    return out
