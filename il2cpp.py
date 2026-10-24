# SPDX-License-Identifier: GPL-3.0-or-later
# Decompiler Studio - Copyright (C) 2026 rxvy-dev. Free software under the GNU GPL v3 or later; see LICENSE.
"""Read the names out of a Unity IL2CPP `global-metadata.dat`.

A Unity game built with IL2CPP has no Java for its game logic: the C# was translated to C++ and
compiled into libil2cpp.so. What survives is this metadata file, which still holds every class,
field, method, property and parameter NAME plus all string literals. This module writes them out as a
readable C#-like skeleton (names only: the types and method bodies are in the native library).

Layout facts used here (Unity metadata v27-v31, checked against a real v31 file):
  header = sanity, version, then (offset, size) pairs in a fixed order
  type definition = 22 x int32 (88 bytes); field = 12 bytes; method = 36 bytes; property = 20 bytes;
  parameter = 12 bytes; image = 40 bytes; string literal = 8 bytes
Anything that does not look right makes the dump fall back to plain name lists instead of guessing.
"""
import re
import struct
from pathlib import Path

SANITY = 0xFAB11BAF
_PAIRS = ["stringLiteral", "stringLiteralData", "string", "events", "properties", "methods",
          "paramDefaultValues", "fieldDefaultValues", "fieldAndParamDefaultValueData",
          "fieldMarshaledSizes", "parameters", "fields", "genericParameters",
          "genericParameterConstraints", "genericContainers", "nestedTypes", "interfaces",
          "vtableMethods", "interfaceOffsets", "typeDefinitions", "images", "assemblies"]
_IDENT = re.compile(r"^[<A-Za-z_][\w`<>.,$|+=\-\[\]\s]*$")


class MetadataError(Exception):
    pass


class Metadata:
    def __init__(self, path):
        self.b = Path(path).read_bytes()
        b = self.b
        if len(b) < 0x100 or struct.unpack_from("<I", b, 0)[0] != SANITY:
            raise MetadataError("not a plain IL2CPP metadata file (the game may encrypt it)")
        self.version = struct.unpack_from("<i", b, 4)[0]
        if not 24 <= self.version <= 40:
            raise MetadataError(f"unsupported metadata version {self.version}")
        self.sec = {}
        for i, name in enumerate(_PAIRS):
            off, size = struct.unpack_from("<ii", b, 8 + 8 * i)
            if off < 0 or size < 0 or off + size > len(b):
                raise MetadataError("header looks wrong")
            self.sec[name] = (off, size)
        self._strcache = {}

    def string(self, idx):
        if idx < 0:
            return ""
        s = self._strcache.get(idx)
        if s is None:
            base, size = self.sec["string"]
            if idx >= size:
                return ""
            end = self.b.index(b"\0", base + idx)
            s = self.b[base + idx:end].decode("utf-8", "replace")
            self._strcache[idx] = s
        return s

    def table(self, name, rec):
        off, size = self.sec[name]
        if rec <= 0 or size % rec:
            return None
        return off, size // rec

    def rows(self, name, rec):
        t = self.table(name, rec)
        if not t:
            return []
        off, n = t
        fmt = "<%di" % (rec // 4)
        return [struct.unpack_from(fmt, self.b, off + i * rec) for i in range(n)]

    def string_literals(self):
        off, size = self.sec["stringLiteral"]
        data_off, data_size = self.sec["stringLiteralData"]
        out = []
        for i in range(size // 8):
            ln, di = struct.unpack_from("<II", self.b, off + i * 8)
            if di + ln > data_size:
                continue
            out.append(self.b[data_off + di:data_off + di + ln].decode("utf-8", "replace"))
        return out

    def identifiers(self):
        base, size = self.sec["string"]
        return [p.decode("utf-8", "replace") for p in self.b[base:base + size].split(b"\0") if p]


_VIS = {1: "public", 2: "public", 3: "private", 4: "protected", 5: "internal", 6: "protected internal", 7: "private protected"}
_MVIS = {1: "private", 2: "private protected", 3: "internal", 4: "protected", 5: "protected internal", 6: "public"}


def _type_header(flags, name, count_generic=""):
    mods = [_VIS.get(flags & 7, "")]
    if flags & 0x20:
        kind = "interface"
    else:
        kind = "class"
        if flags & 0x80 and flags & 0x100:
            mods.append("static")
        elif flags & 0x80:
            mods.append("abstract")
        elif flags & 0x100:
            mods.append("sealed")
    return " ".join(m for m in mods + [kind, name] if m)


def dump(metadata_path, out_dir, log=print, main_images=("Assembly-CSharp", "Assembly-CSharp-firstpass")):
    """Write the readable dump into out_dir. Returns a one-line summary."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    md = Metadata(metadata_path)

    lits = md.string_literals()
    (out / "string_literals.txt").write_text(
        "\n".join(s.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r") for s in lits), encoding="utf-8")
    idents = sorted(set(md.identifiers()))
    (out / "all_names.txt").write_text("\n".join(idents), encoding="utf-8")

    images = md.rows("images", 40)
    types = md.rows("typeDefinitions", 88) if md.version >= 27 else []
    fields = md.rows("fields", 12)
    methods = md.rows("methods", 36)
    props = md.rows("properties", 20)
    params = md.rows("parameters", 12)
    # sanity: if the layout guess is wrong, say so and keep the plain name lists
    ok = bool(types and images and fields and methods)
    if ok:
        for t in types[:50]:
            if not (0 <= t[0] < md.sec["string"][1]) or not _IDENT.match(md.string(t[0]) or "x"):
                ok = False
                break
    if not ok:
        return (f"IL2CPP metadata v{md.version}: wrote {len(idents)} names and {len(lits)} string literals "
                "(class layout of this version is not supported, so no class skeleton)")

    per_image = {}
    ntypes = len(types)
    # which image owns which type
    owner = {}
    for im in images:
        start, count = im[2], im[3]
        for ti in range(start, min(start + count, ntypes)):
            owner[ti] = im
    # declaringTypeIndex points into the native type table, so nesting comes from the nestedTypes table
    parent = {}
    nested_tbl = md.table("nestedTypes", 4)
    if nested_tbl:
        noff, ncnt = nested_tbl
        for ti, t in enumerate(types):
            ncount = t[18] & 0xFFFF
            if t[12] >= 0 and ncount and t[12] + ncount <= ncnt:
                for k in range(ncount):
                    child = struct.unpack_from("<i", md.b, noff + (t[12] + k) * 4)[0]
                    if 0 <= child < ntypes:
                        parent[child] = ti

    def tname(ti):
        n = md.string(types[ti][0])
        if ti in parent and parent[ti] != ti:
            return tname(parent[ti]) + "." + n
        return n

    for ti, t in enumerate(types):
        im = owner.get(ti)
        if im is None:
            continue
        iname = md.string(im[0]).removesuffix(".dll")
        ns = md.string(t[1])
        fstart, mstart, pstart = t[8], t[9], t[11]
        # u16 counts are packed two per int32 after the 16 index fields
        mcount, pcount = t[16] & 0xFFFF, (t[16] >> 16) & 0xFFFF
        fcount = t[17] & 0xFFFF
        lines = []
        head = _type_header(t[7], md.string(t[0]))
        lines.append(f"    {head}" + (f"  // nested in {tname(parent[ti])}" if ti in parent else "") + " {")
        if fstart >= 0 and fcount:
            for f in fields[fstart:fstart + fcount]:
                lines.append(f"        {md.string(f[0])};")
        if pstart >= 0 and pcount:
            for p in props[pstart:pstart + pcount]:
                acc = ("get; " if p[1] >= 0 else "") + ("set; " if p[2] >= 0 else "")
                lines.append(f"        {md.string(p[0])} {{ {acc}}}")
        if mstart >= 0 and mcount:
            for m in methods[mstart:mstart + mcount]:
                flags = m[7] & 0xFFFF
                mods = [_MVIS.get(flags & 7, "")]
                if flags & 0x10:
                    mods.append("static")
                if flags & 0x400:
                    mods.append("abstract")
                elif flags & 0x40:
                    mods.append("virtual")
                pnames = []
                pstart_, pc = m[4], (m[8] >> 16) & 0xFFFF
                if pstart_ >= 0 and pc:
                    pnames = [md.string(p[0]) for p in params[pstart_:pstart_ + pc]]
                lines.append(f"        {' '.join(x for x in mods if x)} {md.string(m[0])}({', '.join(pnames)});")
        lines.append("    }")
        per_image.setdefault(iname, {}).setdefault(ns, []).append("\n".join(lines))

    main_dir = out
    other_dir = out / "other_assemblies"
    other_dir.mkdir(exist_ok=True)
    written = []
    for iname, by_ns in per_image.items():
        body = [f"// {iname}: names only (types and method bodies live in libil2cpp.so)", ""]
        for ns in sorted(by_ns):
            body.append(f"namespace {ns or '<global>'} {{")
            for block in by_ns[ns]:
                body.append(block)
            body.append("}")
            body.append("")
        target = (main_dir if iname in main_images else other_dir) / f"{iname}.cs"
        target.write_text("\n".join(body), encoding="utf-8")
        written.append(target)
    ntype = sum(len(v) for d in per_image.values() for v in d.values())
    return (f"IL2CPP metadata v{md.version}: {ntype} classes in {len(per_image)} assemblies, "
            f"{len(idents)} names, {len(lits)} string literals")
