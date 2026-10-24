# SPDX-License-Identifier: GPL-3.0-or-later
# Decompiler Studio - Copyright (C) 2026 rxvy-dev. Free software under the GNU GPL v3 or later; see LICENSE.
"""Explain: explains a piece of decompiled code in plain English with a small LOCAL language model.

It talks to a model server running on the same computer:
  * Ollama (default, http://127.0.0.1:11434). The installer (or Explain > Install AI model) puts a private copy
    of Ollama and Qwen2.5-Coder in ~/.local/share/decompiler-studio/ai; the app starts it when needed.
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

import shutil
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:11434"
BIG_MODEL, SMALL_MODEL = "qwen2.5-coder:14b", "qwen2.5-coder:7b"
PREFERRED = ("qwen2.5-coder:14b", "qwen2.5-coder:7b", "qwen2.5-coder:32b", "qwen2.5-coder:latest", "qwen2.5-coder:3b",
             "qwen2.5-coder:1.5b", "qwen2.5-coder", "deepseek-coder", "codegemma", "llama3.2", "qwen2.5")
AI_DIR = Path(os.environ.get("DS_AI_DIR") or Path.home() / ".local" / "share" / "decompiler-studio" / "ai")
OLLAMA_URL = "https://github.com/ollama/ollama/releases/latest/download/ollama-linux-amd64.tar.zst"


def gpu_memory_mb():
    """Total memory of the biggest NVIDIA card in MB (0 if none or unknown)."""
    smi = shutil.which("nvidia-smi")
    if not smi:
        return 0
    try:
        out = subprocess.run([smi, "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout
        return max(int(x) for x in out.split() if x.strip().isdigit())
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def recommended_model():
    """Qwen2.5-Coder 14B (about 9 GB) on a 12 GB+ graphics card, otherwise 7B (about 4.7 GB)."""
    return BIG_MODEL if gpu_memory_mb() >= 11500 else SMALL_MODEL


DEFAULT_MODEL = SMALL_MODEL
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


CHECK_SYSTEM = (
    "You are a security reviewer helping someone decide whether a piece of decompiled code is safe to have "
    "on their device. Judge the code you are given; do not write, extend or improve any code.\n"
    "Answer in exactly this format, in plain English a non-expert can follow:\n"
    "Verdict: Clean | Suspicious | Likely malicious\n"
    "**Why:** 2-4 sentences explaining what the code actually does and why that leads to the verdict.\n"
    "**What it could do to you:** one or two sentences on the possible harm (or 'Nothing harmful found').\n"
    "**Evidence:** bullet points, each naming the function or line and what it does there.\n"
    "**Innocent explanation:** what an ordinary app could be doing with the same code (many normal apps send "
    "data to their own servers, show ads or ask for permissions).\n"
    "**How sure:** Low / Medium / High, with one short reason (decompiled names like FUN_xxxx or param_1 hide intent, "
    "and you only see part of the app).\n"
    "**Check next:** 1-2 concrete things to look at in the app to confirm or clear it.\n"
    "Use 'Suspicious' or 'Likely malicious' only for concrete reasons found in the code. Under 300 words.")


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


HELP = ("No local AI model is installed yet. Easiest: Explain menu > Install AI model (downloads Ollama and "
        "Qwen2.5-Coder into the app's own folder, no root needed).\n"
        "Or install Ollama yourself (https://ollama.com) and run:  ollama pull qwen2.5-coder:7b\n"
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
    raise LlmError("The model server has no models yet. Use Explain menu > Install AI model, or run:  ollama pull "
                   + recommended_model())


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


def build_messages(code, filename, label, mode="explain"):
    code, cut = clip(code)
    ext = os.path.splitext(filename)[1].lower()
    lang = {".c": "C (Ghidra decompiler output)", ".java": "Java (jadx decompiler output)", ".cs": "C# (names only)",
            ".py": "Python", ".smali": "smali", ".asm": "assembly"}.get(ext, "code")
    user = (f"File: {os.path.basename(filename) or 'selection'} - {lang}\nExplaining: {label}\n\n```\n{code}\n```\n"
            + ("(The code was cut to fit the model's size.)\n" if cut else "") + "\nExplain what this does.")
    if mode == "check":
        user = user.replace("\nExplain what this does.", "\nIs this code safe or does it look malicious?")
        return [{"role": "system", "content": CHECK_SYSTEM}, {"role": "user", "content": user}]
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def stream_explain(code, filename, label, url, model, should_stop=lambda: False, mode="explain"):
    """Yield the model's answer piece by piece. Raises LlmError with a readable message."""
    url = url or DEFAULT_URL
    try:
        models = list_models(url)
    except LlmError as e:
        if str(e) != "no_server":
            raise
        if not (url.rstrip("/") == DEFAULT_URL and start_server()):
            raise LlmError(HELP)
        models = list_models(url)
    name = choose_model(models, model)
    msgs = build_messages(code, filename, label, mode)
    limit = 1000 if mode == "check" else 700
    base = url.rstrip("/")
    if _openai_style(url):
        body = {"model": name, "messages": msgs, "stream": True, "temperature": 0.2, "max_tokens": limit}
        req = urllib.request.Request(base + "/chat/completions", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
    else:
        body = {"model": name, "messages": msgs, "stream": True,
                "options": {"temperature": 0.2, "num_ctx": 8192, "num_predict": limit}}
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


# ---------------------------------------------------------------- private Ollama + model install
_SERVER = None


def ollama_bin():
    """The app's own Ollama, else one installed on the system."""
    own = AI_DIR / "ollama" / "bin" / "ollama"
    if own.is_file():
        return str(own)
    return shutil.which("ollama")


def server_up(url=DEFAULT_URL):
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/tags", timeout=2):
            return True
    except Exception:
        return False


def start_server(wait=25):
    """Start Ollama in the background if it is not running. True when it answers."""
    global _SERVER
    if server_up():
        return True
    exe = ollama_bin()
    if not exe:
        return False
    env = dict(os.environ, OLLAMA_HOST="127.0.0.1:11434")
    if exe.startswith(str(AI_DIR)):
        env["OLLAMA_MODELS"] = str(AI_DIR / "models")
    AI_DIR.mkdir(parents=True, exist_ok=True)
    logf = open(AI_DIR / "server.log", "ab")
    _SERVER = subprocess.Popen([exe, "serve"], env=env, stdout=logf, stderr=logf, start_new_session=True)
    for _ in range(wait * 4):
        if server_up():
            return True
        if _SERVER.poll() is not None:
            break
        time.sleep(0.25)
    return False


def stop_server():
    """Stop the Ollama this app started (not one the user runs themselves)."""
    global _SERVER
    if _SERVER and _SERVER.poll() is None:
        _SERVER.terminate()
        try:
            _SERVER.wait(5)
        except subprocess.TimeoutExpired:
            _SERVER.kill()
    _SERVER = None


def _download(url, dest, log, check=lambda: None):
    req = urllib.request.Request(url, headers={"User-Agent": "decompiler-studio"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got, shown = 0, -1
        while True:
            check()
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            pct = int(got * 100 / total) if total else -1
            if pct >= 0 and pct // 5 != shown:
                shown = pct // 5
                log(f"Downloading Ollama: {pct}% of {total / 1e9:.1f} GB")


def _untar_zst(arc, dest):
    """Unpack a .tar.zst with Python (no 'zstd' program needed), refusing paths that leave `dest`."""
    import tarfile
    dest = Path(dest).resolve()
    try:
        import zstandard
    except ImportError:
        r = subprocess.run(["tar", "--zstd", "-xf", str(arc), "-C", str(dest)], capture_output=True, text=True)
        if r.returncode != 0:
            raise LlmError("Could not unpack Ollama: install the 'zstd' package or 'pip install zstandard'. "
                           + r.stderr[-200:])
        return
    with open(arc, "rb") as f, zstandard.ZstdDecompressor().stream_reader(f) as reader, \
            tarfile.open(fileobj=reader, mode="r|") as t:
        for m in t:
            target = (dest / m.name).resolve()
            if not str(target).startswith(str(dest)) or m.issym() and os.path.isabs(m.linkname):
                continue
            t.extract(m, dest, filter="tar") if sys.version_info >= (3, 12) else t.extract(m, dest)


def install_ai(log=print, model=None, check=lambda: None):
    """Get Ollama (a private copy, unless one is already installed) and pull the model. Returns the model name."""
    model = model or recommended_model()
    gpu = gpu_memory_mb()
    log(f"AI model: {model}" + (f" (graphics card with {gpu // 1024} GB found)" if gpu else " (no NVIDIA card found: "
        "it runs on the CPU, slower but fine)"))
    if not ollama_bin():
        if sys.platform != "linux":
            raise LlmError("Automatic install is for Linux. On other systems install Ollama from https://ollama.com "
                           f"and run:  ollama pull {model}")
        AI_DIR.mkdir(parents=True, exist_ok=True)
        arc = AI_DIR / "ollama-linux-amd64.tar.zst"
        log("Downloading Ollama (the program that runs the model) from its GitHub releases...")
        _download(OLLAMA_URL, arc, log, check)
        dest = AI_DIR / "ollama"
        dest.mkdir(parents=True, exist_ok=True)
        log("Unpacking Ollama...")
        try:
            _untar_zst(arc, dest)
        finally:
            arc.unlink(missing_ok=True)
        if not (dest / "bin" / "ollama").is_file():
            raise LlmError("Ollama was downloaded but bin/ollama is missing from it.")
        (dest / "bin" / "ollama").chmod(0o755)
        log(f"Ollama installed in {dest}")
    else:
        log(f"Using Ollama at {ollama_bin()}")
    if not start_server():
        raise LlmError(f"Ollama did not start. Its log is in {AI_DIR / 'server.log'}")
    log(f"Downloading the model {model} (one time; it stays on this computer)...")
    body = json.dumps({"model": model, "stream": True}).encode()
    req = urllib.request.Request(DEFAULT_URL + "/api/pull", body, {"Content-Type": "application/json"})
    shown = -1
    with _open(req, 3600) as r:
        for raw in r:
            check()
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            if "error" in obj:
                raise LlmError("Model download failed: " + str(obj["error"]))
            tot, done = obj.get("total"), obj.get("completed")
            if tot and done is not None:
                pct = int(done * 100 / tot)
                if pct // 5 != shown:
                    shown = pct // 5
                    log(f"Model: {pct}% of {tot / 1e9:.1f} GB")
            elif obj.get("status") and obj.get("status") != "pulling manifest":
                log("Model: " + obj["status"])
    log(f"AI ready: {model}. Explain and Malware check will use it.")
    return model


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--install":
        m = next((a.split("=", 1)[1] for a in args if a.startswith("--model=")), None)
        try:
            install_ai(lambda t: print("[decompiler-studio] " + t, flush=True), m)
        except Exception as e:
            print(f"[decompiler-studio] AI model not installed: {e}", file=sys.stderr)
            sys.exit(1)
        finally:
            stop_server()
