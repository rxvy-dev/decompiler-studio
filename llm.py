# SPDX-License-Identifier: GPL-3.0-or-later
# Decompiler Studio - Copyright (C) 2026 rxvy-dev. Free software under the GNU GPL v3 or later; see LICENSE.
"""Explain: explains a piece of decompiled code in plain English with a small LOCAL language model.

No model is bundled (they are 1 GB and up). It talks to a model server running on the same computer:
  * Ollama (default, http://127.0.0.1:11434)  ->  ollama pull qwen2.5-coder:1.5b
  * any OpenAI-compatible server (llama.cpp's llama-server, LM Studio, ...): set the address to
    something ending in /v1, e.g. http://127.0.0.1:8080/v1
The code is sent only to the address you configure; with the default it never leaves the machine.
"""
import json
import os
import re
import urllib.error
import urllib.request
from urllib.parse import urlparse

DEFAULT_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen2.5-coder:1.5b"
PREFERRED = ("qwen2.5-coder:1.5b", "qwen2.5-coder:3b", "qwen2.5-coder", "deepseek-coder", "codegemma",
             "llama3.2:1b", "llama3.2", "qwen2.5")
MAX_CHARS = 6000

SYSTEM = (
    "You are a reverse-engineering tutor. A student gives you code that was recovered by a decompiler and "
    "wants to know what it does. Explain it in plain English.\n"
    "Rules:\n"
    "- Start with one sentence: what the function/class does overall.\n"
    "- Then short bullet points in the order things happen. Mention loops, conditions and calls.\n"
    "- Say what the parameters and important variables are probably for. Names like param_1, local_88, "
    "iVar3, FUN_00101234 or a, b, a0 are decompiler/obfuscation guesses, not the original names.\n"
    "- If something is unclear, write 'unclear' instead of guessing. Never invent code that is not there.\n"
    "- Do not rewrite the code. Keep it under 200 words.")


class LlmError(Exception):
    pass


def settings():
    """(url, model) from the environment (DS_LLM_URL / DS_LLM_MODEL); the app overrides them from its settings."""
    return (os.environ.get("DS_LLM_URL") or DEFAULT_URL, os.environ.get("DS_LLM_MODEL") or "")


def _openai_style(url):
    return urlparse(url).path.rstrip("/").endswith("/v1")


def is_local(url):
    return (urlparse(url).hostname or "") in ("127.0.0.1", "localhost", "::1")


def _open(req, timeout):
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        raise LlmError(f"The model server answered {e.code}: {body}")
    except (urllib.error.URLError, OSError) as e:
        raise LlmError("no_server")


HELP = ("No local model server answered. Easiest setup:\n"
        "  1. install Ollama (https://ollama.com)\n"
        "  2. run:  ollama pull qwen2.5-coder:1.5b\n"
        "  3. press Explain again\n"
        "Another server works too (llama.cpp, LM Studio): Explain menu > Server address, and use an address "
        "ending in /v1, for example http://127.0.0.1:8080/v1")


def list_models(url):
    """[(name, size_bytes_or_0)] of the models the server has."""
    base = url.rstrip("/")
    if _openai_style(url):
        req = urllib.request.Request(base + "/models")
        with _open(req, 6) as r:
            data = json.load(r)
        return [(m.get("id", ""), 0) for m in data.get("data", []) if m.get("id")]
    req = urllib.request.Request(base + "/api/tags")
    with _open(req, 6) as r:
        data = json.load(r)
    return [(m.get("name", ""), int(m.get("size", 0) or 0)) for m in data.get("models", []) if m.get("name")]


def choose_model(models, wanted=""):
    names = [n for n, _ in models]
    if wanted:
        if wanted in names:
            return wanted
        base = [n for n in names if n.split(":")[0] == wanted.split(":")[0]]
        if base:
            return base[0]
        raise LlmError(f"The model '{wanted}' is not installed on that server. Installed: {', '.join(names) or 'none'}.\n"
                       f"Install it with:  ollama pull {wanted}")
    for p in PREFERRED:
        for n in names:
            if n == p or n.startswith(p):
                return n
    if names:
        return min(models, key=lambda m: m[1] or 1 << 60)[0]      # smallest installed model
    raise LlmError("The server has no models installed.\nInstall one with:  ollama pull " + DEFAULT_MODEL)


def pick_chunk(text, line):
    """The part of text to explain when nothing is selected: the decompiled function around `line`
    (functions are separated by '// ---- name @ addr ----' lines), else a window around the line."""
    lines = text.split("\n")
    line = max(0, min(line, len(lines) - 1))
    marks = [i for i, l in enumerate(lines) if re.match(r"^// ---- .* @ [0-9a-fA-F]+ ----$", l)]
    if marks:
        start = max([m for m in marks if m <= line], default=marks[0])
        nxt = [m for m in marks if m > start]
        end = nxt[0] if nxt else len(lines)
        name = re.match(r"^// ---- (.*) @ ", lines[start]).group(1)
        return "\n".join(lines[start:end]).strip(), f"function {name}"
    if len(text) <= MAX_CHARS:
        return text.strip(), "this file"
    # no markers: walk back to something that looks like the start of a method/function
    sig = re.compile(r"^\s{0,4}(?:[\w<>\[\],.* ]+\s+)+\w+\s*\([^;]*\)\s*(?:throws [\w, .]+)?\{?\s*$")
    start = line
    while start > 0 and not sig.match(lines[start]):
        start -= 1
    if line - start > 150:
        start = max(0, line - 40)
    return "\n".join(lines[start:start + 200]).strip(), f"the code near line {line + 1}"


def clip(code):
    if len(code) <= MAX_CHARS:
        return code, False
    # long function: keep the start and the end, which hold the setup and the result
    return code[:4000] + "\n    /* ... middle of the function left out ... */\n" + code[-1800:], True


def build_messages(code, filename, label):
    code, cut = clip(code)
    ext = os.path.splitext(filename)[1].lower()
    lang = {".c": "C (Ghidra decompiler output)", ".java": "Java (jadx decompiler output)", ".cs": "C# (names only)",
            ".py": "Python", ".smali": "smali", ".asm": "assembly"}.get(ext, "code")
    user = (f"File: {os.path.basename(filename) or 'selection'} - {lang}\nExplaining: {label}\n\n```\n{code}\n```\n"
            + ("(The code was cut to fit the model's size.)\n" if cut else "") + "\nExplain what this does.")
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def stream_explain(code, filename, label, url, model, should_stop=lambda: False):
    """Yield the model's answer piece by piece. Raises LlmError with a readable message."""
    url = url or DEFAULT_URL
    try:
        models = list_models(url)
    except LlmError as e:
        raise LlmError(HELP if str(e) == "no_server" else str(e))
    name = choose_model(models, model)
    msgs = build_messages(code, filename, label)
    base = url.rstrip("/")
    if _openai_style(url):
        body = {"model": name, "messages": msgs, "stream": True, "temperature": 0.2, "max_tokens": 700}
        req = urllib.request.Request(base + "/chat/completions", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
    else:
        body = {"model": name, "messages": msgs, "stream": True,
                "options": {"temperature": 0.2, "num_ctx": 8192, "num_predict": 700}}
        req = urllib.request.Request(base + "/api/chat", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
    try:
        with _open(req, 300) as r:
            yield ("model", name)
            for raw in r:
                if should_stop():
                    return
                raw = raw.strip()
                if not raw:
                    continue
                if raw.startswith(b"data:"):
                    raw = raw[5:].strip()
                    if raw == b"[DONE]":
                        return
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                if "error" in obj:
                    raise LlmError(str(obj["error"]))
                if "choices" in obj:
                    piece = (obj["choices"][0].get("delta") or {}).get("content") or ""
                    done = obj["choices"][0].get("finish_reason") is not None
                else:
                    piece = (obj.get("message") or {}).get("content", "")
                    done = bool(obj.get("done"))
                if piece:
                    yield ("text", piece)
                if done:
                    return
    except LlmError as e:
        raise LlmError(HELP if str(e) == "no_server" else str(e))
    except (OSError, ValueError) as e:
        raise LlmError(f"The model server stopped answering: {e}")
