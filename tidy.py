# SPDX-License-Identifier: GPL-3.0-or-later
# Decompiler Studio - Copyright (C) 2026 rxvy-dev. Free software under the GNU GPL v3 or later; see LICENSE.
"""Post-processing that makes Ghidra's C read more like hand-written C.

Everything here is a textual, per-function rewrite of decompiled.c. Each rule only fires when it
is safe by construction (explained at each rule); none of them changes what the code does.

  1. raw float bits ("x = 0x3f800000;") become float literals ("x = 1.0;")
  2. gcc's stack-protector canary check (not in any source file) is removed
  3. declarations of variables that are never used are dropped
  4. a temporary that only holds a call's result for one NULL test is folded into the test
  5. "declare at first use": "float x;" ... "x = a * b;"  ->  "float x = a * b;" and
     "for (i = 0; ...)" -> "for (int i = 0; ...)", wherever every use of the variable stays inside
     the block it is first assigned in
  6. arithmetic noise: "x * 1.0" -> "x", "x * -1.0" -> "-x", "a[i + 1 + 24]" -> "a[i + 25]"
  7. "in_stack_NNNN" variables (arguments the caller passed on the stack, which Ghidra could not
     put in the signature) get a comment saying so
"""
import re
import struct

_FUNC_MARK = re.compile(r"^// ---- .* @ [0-9a-fA-F]+ ----$")
_DECL = re.compile(r"^(?P<ind>\s+)(?P<type>[A-Za-z_]\w*(?: [A-Za-z_]\w*)*) (?P<stars>\**)(?P<name>[A-Za-z_]\w*);$")
_HEX_ASSIGN = re.compile(r"^(?P<ind>\s+)(?P<name>[A-Za-z_]\w*) = 0x(?P<hex>[0-9a-fA-F]{8});$")
_STACK = re.compile(r"^in_stack_(?P<off>[0-9a-fA-F]{8})$")
_KEYWORDS = {"return", "goto", "else", "break", "continue", "case", "default", "do", "sizeof", "while",
             "if", "for", "switch"}


def _indent(line):
    return len(line) - len(line.lstrip())


def _nice_float(bits):
    """The float for these 32 bits if it is a 'human' number (1.0, 0.5, 3.14, 255.0), else None."""
    f = struct.unpack("<f", struct.pack("<I", bits))[0]
    if f != f or f in (float("inf"), float("-inf")) or not (1e-3 <= abs(f) <= 1e9):
        return None
    for digits in range(1, 8):                       # shortest decimal that round-trips as a float
        s = f"{f:.{digits}g}"
        if struct.unpack("<f", struct.pack("<f", float(s)))[0] == f:
            if "e" in s:
                return None
            return s if "." in s else s + ".0"
    return None


def _find_parts(lines):
    """Locate the pieces of one function chunk: (sig_start, brace_line, decl_start, decl_end).

    sig_start..: the signature lines; brace_line: line holding the opening '{' (alone, or at the end of
    the signature); decl_start/decl_end: the block of local declarations up to the first blank line."""
    brace = None
    sig_start = 1
    while sig_start < len(lines) and lines[sig_start].strip() == "":
        sig_start += 1
    for i in range(sig_start, len(lines)):       # a signature never contains ';' or '}'
        l = lines[i].rstrip()
        if ";" in l or l.lstrip().startswith("}"):
            break
        if l.endswith("{"):
            brace = i
            break
    if brace is None:
        return None
    start = brace + 1
    end = start
    while end < len(lines) and lines[end].strip() != "":
        end += 1
    return sig_start, brace, start, end


_CANARY_SET = re.compile(r"(?m)^[ ]+(?:\w+ )?(\w+) = \*\(in_FS_OFFSET \+ 40\);\n")
_CANARY_CHECK = (r"(?ms)^([ ]+)if \(%s != \*\(in_FS_OFFSET \+ 40\)\) \{\n"
                 r"(?:[ ]*/\*[^\n]*\*/\n)?[ ]*__stack_chk_fail\(\);\n\1\}\n")


_CANARY_OK = (r"(?ms)^([ ]+)if \(%s == \*\(in_FS_OFFSET \+ 40\)\) \{\n[ ]*(return[^\n]*;)\n\1\}\n"
              r"(?:[ ]*/\*[^\n]*\*/\n)?[ ]*__stack_chk_fail\(\);\n")


def _drop_stack_canary(lines):
    """gcc's stack-protector (-fstack-protector) adds a secret value check to many functions; it is
    in no source file. Remove the matching 'save canary' and 'if (canary != ...) __stack_chk_fail()'."""
    text = "\n".join(lines) + "\n"
    m = _CANARY_SET.search(text)
    if not m:
        return lines
    var = re.escape(m.group(1))
    chk = re.compile(_CANARY_CHECK % var)
    ok = re.compile(_CANARY_OK % var)
    if chk.search(text):
        text = chk.sub("", text, count=1)
    elif ok.search(text):
        text = ok.sub(lambda k: "" if k.group(2) == "return;" else f"{k.group(1)}{k.group(2)}\n", text, count=1)
    else:
        return lines
    text = text[:m.start()] + text[m.end():]
    return text[:-1].split("\n")


def _single_call(expr):
    """True if expr is exactly one function call: name(...) with the last ')' closing the first '('."""
    if not re.match(r"^[A-Za-z_]\w*\(", expr):
        return False
    depth, instr, esc = 0, False, False
    for k, ch in enumerate(expr):
        if instr:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                instr = False
            continue
        if ch == '"':
            instr = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0 and k != len(expr) - 1:
                return False
    return depth == 0 and not instr


def _squash(call):
    return re.sub(r"\n\s*", "", call)


def _inline_null_temps(lines):
    """'p = strstr(a, b);  if (p == NULL) ...'  ->  'if (!strstr(a, b)) ...'

    Only for a temporary whose every mention is such an assignment, the NULL test right after it, or the
    comma form '(p = call(), p == NULL)'; any other use of the variable (it is read later, passed on, ...)
    leaves it alone."""
    text = "\n".join(lines)
    for name in sorted(set(re.findall(r"\b(\w+) (?:==|!=) NULL", text))):
        n = re.escape(name)
        before = len(re.findall(rf"\b{n}\b", text))
        t = text

        def comma(m):
            call = _squash(m.group("call"))
            if not _single_call(call):
                return m.group(0)
            return f"(!{call})" if m.group("op") == "==" else f"({call})"
        t = re.sub(rf"\({n} = (?P<call>[A-Za-z_]\w*\((?:[^;]|\n)*?\)),\s*{n} (?P<op>==|!=) NULL\)", comma, t)
        assigns = list(re.finditer(rf"(?m)^[ ]*{n} = (?P<call>[A-Za-z_]\w*\((?:[^;\n]|\n)*?\));\n", t))
        if not assigns:
            continue
        # every remaining mention must be one of these assignments or the test that follows it
        decl_line = rf"(?m)^[ ]+[A-Za-z_][\w ]*[ *]{n};\n"
        if len(re.findall(rf"\b{n}\b", re.sub(decl_line, "", t))) != 2 * len(assigns):
            continue
        out, pos, ok = [], 0, True
        for m in assigns:
            call = _squash(m.group("call"))
            if not _single_call(call):
                ok = False
                break
            rest = t[m.end():]
            nxt = re.match(rf"(?P<line>[ ]*(?:if|while) \([^\n]*)", rest)
            first = re.search(rf"\b{n}\b", rest)
            if not nxt or not first or first.start() > len(nxt.group("line")):
                ok = False
                break
            tm = re.match(rf"{n} (?P<op>==|!=) NULL", rest[first.start():])
            if not tm:
                ok = False
                break
            out.append((m.start(), m.end(), m.end() + first.start(), m.end() + first.start() + tm.end(),
                        f"!{call}" if tm.group("op") == "==" else call))
        if not ok:
            continue
        res, last = [], 0
        for a, b, c, d, repl in out:
            res.append(t[last:a])             # text before the assignment
            res.append(t[b:c])                # up to the test (assignment line removed)
            res.append(repl)
            last = d
        res.append(t[last:])
        text = "".join(res)
    return text.split("\n")


_TERM = r"[A-Za-z_]\w*(?:\[[^\[\]]*\])?"
_MUL_ONE = re.compile(rf"({_TERM}) \* 1\.0(?![\w.])")
_MUL_NEG_ONE = re.compile(rf"({_TERM}) \* -1\.0(?![\w.])")
_IDX_SUM = re.compile(r"\[([^\[\]\"]*?[\w)]) \+ (\d+) \+ (\d+)\]")


def _simplify_math(lines):
    """Rule 6: identity multiplications and constant index sums. Exact by construction: x*1.0 is x,
    x*-1.0 is -x, and (i + a) + b is i + (a + b) for integer indices. Lines with strings are skipped."""
    out = []
    for l in lines:
        if '"' not in l and "//" not in l and "/*" not in l and l.strip():
            l = _MUL_ONE.sub(r"\1", l)
            l = _MUL_NEG_ONE.sub(r"-\1", l)
            l = re.sub(r"\+ -(?=[A-Za-z_])", "- ", l)
            for _ in range(3):
                l2 = _IDX_SUM.sub(lambda m: f"[{m[1]} + {int(m[2]) + int(m[3])}]", l)
                if l2 == l:
                    break
                l = l2
        out.append(l)
    return out


def _tidy_function(lines):
    """Returns the rewritten lines of one function chunk."""
    parts = _find_parts(lines)
    if not parts:
        return lines
    lines = _drop_stack_canary(lines)
    lines = _inline_null_temps(lines)
    parts = _find_parts(lines)
    lines = lines[:parts[3]] + _simplify_math(lines[parts[3]:])
    sig_start, brace, start, end = parts
    decls = {}                                        # name -> (index, indent, type, stars)
    for i in range(start, end):
        m = _DECL.match(lines[i])
        if m and "[" not in lines[i] and m["type"].split()[0] not in _KEYWORDS:
            decls[m["name"]] = (i, m["ind"], m["type"], m["stars"])
    body = list(range(end, len(lines)))

    # 1) raw float bits stored into a variable the decompiler could not type (undefined4)
    retyped = set()
    for i in body:
        m = _HEX_ASSIGN.match(lines[i])
        if m and m["name"] in decls and decls[m["name"]][2] == "undefined4" and not decls[m["name"]][3]:
            v = _nice_float(int(m["hex"], 16))
            if v is not None:
                lines[i] = f'{m["ind"]}{m["name"]} = {v};'
                retyped.add(m["name"])
    for name in retyped:
        i, ind, _t, stars = decls[name]
        lines[i] = f"{ind}float {name};"
        decls[name] = (i, ind, "float", stars)

    drop = set()
    # 2) declare at first use (see module docstring for why this is safe)
    word = {n: re.compile(rf"\b{re.escape(n)}\b") for n in decls}
    for n, (di, ind, typ, stars) in decls.items():
        hits = [i for i in body if word[n].search(lines[i])]
        if not hits:
            drop.add(di)                       # declared but never used: pure noise (x_local, ...)
            continue
        i, j = hits[0], hits[-1]
        mm = re.match(rf"^(?P<ind>\s*){re.escape(n)} = (?P<expr>.*);$", lines[i])
        mf = re.match(rf"^(?P<ind>\s*)for \({re.escape(n)} = (?P<expr>[^;,()]*);(?P<rest>.*)$", lines[i])
        if mm and not word[n].search(mm["expr"]):
            level = len(mm["ind"])
            if all(_indent(lines[k]) >= level for k in range(i + 1, j + 1) if lines[k].strip()):
                lines[i] = f'{mm["ind"]}{typ} {stars}{n} = {mm["expr"]};'
                drop.add(di)
        elif mf and not word[n].search(mf["expr"]):
            level = len(mf["ind"])
            if lines[i].rstrip().endswith("{"):
                close = next((k for k in range(i + 1, len(lines))
                              if _indent(lines[k]) == level and lines[k].lstrip().startswith("}")), None)
            else:
                close = i + 1
            if close is not None and j <= close:
                lines[i] = f'{mf["ind"]}for ({typ} {stars}{n} = {mf["expr"]};{mf["rest"]}'
                drop.add(di)
    for n, (di, ind, typ, stars) in decls.items():              # rule 7
        if di not in drop and _STACK.match(n):
            lines[di] = lines[di] + "  // extra argument passed on the stack"
    lines = [l for k, l in enumerate(lines) if k not in drop]

    # an empty declaration block leaves a stray blank line right after the '{'
    parts = _find_parts(lines)
    if parts and parts[2] < len(lines) and lines[parts[2]].strip() == "" and parts[2] == parts[3]:
        del lines[parts[2]]
    return lines


def tidy_c(text):
    """Apply the readability rewrites to every function in a decompiled.c text."""
    warn = "/* DISPLAY WARNING: Type casts are NOT being printed */"
    if warn in text:                           # we hide casts on purpose: say so once, not per function
        text = re.sub(r"(?m)^" + re.escape(warn) + r"\n\n?", "", text)
        text = text.replace("best guesses.\n", "best guesses.\n// Casts are not shown and numbers are in decimal, to read like hand-written C.\n", 1)
    out, cur = [], []

    def flush():
        if cur:
            try:
                out.extend(_tidy_function(list(cur)))
            except Exception:                  # never let a tidy-up break the output
                out.extend(cur)
            cur.clear()

    for l in text.split("\n"):
        if _FUNC_MARK.match(l):
            flush()
            cur.append(l)
        elif cur:
            cur.append(l)
        else:
            out.append(l)
    flush()
    # "}" newline "else {"  ->  "} else {"   (also "else if")
    joined = "\n".join(out)
    joined = re.sub(r"\b__isoc2[0-9]_(\w+)\b", r"\1", joined)      # __isoc23_scanf -> scanf
    joined = re.sub(r"(?m)^(\s*)\}\n\s*else( \{| if \()", r"\1} else\2", joined)
    # "for (...; i += 1)" -> "for (...; i++)"
    joined = re.sub(r"(?m)^(\s*for \(.*; )(\w+) \+= 1\) \{$", r"\1\2++) {", joined)
    # calls Ghidra wrapped onto three lines, and a lone ';' on its own line
    joined = re.sub(r"(?m)^([ ]*)(\S[^\n]*\()\n[ ]+([^\n]+)\n[ ]+\);$", r"\1\2\3);", joined)
    joined = re.sub(r"\)\n[ ]+;$", ");", joined, flags=re.M)
    # character arguments: strchr(s, 67) -> strchr(s, 'C'), putchar(10) -> putchar('\n')
    def _chr(n):
        n = int(n)
        return {10: "'\\n'", 9: "'\\t'", 13: "'\\r'", 39: "'\\''", 92: "'\\\\'"}.get(n) or (f"'{chr(n)}'" if 32 <= n < 127 else None)
    joined = re.sub(r"\b(strchr|strrchr|memchr)\((\w+),(\d+)\)",
                    lambda m: f"{m[1]}({m[2]},{_chr(m[3]) or m[3]})", joined)
    joined = re.sub(r"\b(putchar)\((\d+)\)", lambda m: f"{m[1]}({_chr(m[2]) or m[2]})", joined)
    out = joined.split("\n")
    res = []
    for l in out:
        if not l.strip():
            l = ""                             # blank lines carry no trailing spaces
        elif l.endswith(";") and "=" not in l and "(" not in l:
            l = re.sub(r"(\w) \[", r"\1[", l).replace("] [", "][")      # "float v [8] [3];" -> "float v[8][3];"
        res.append(l)
    return "\n".join(res)
