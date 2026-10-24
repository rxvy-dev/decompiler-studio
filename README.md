Decompiler Studio (Linux x86_64) - run ./decompiler-studio, drop a file on the window.

WARNING: USE FOR PERSONAL AND EDUCATIONAL PURPOSES ONLY. Only decompile software you own or have permission to study.
Java 25, jadx and Ghidra are bundled; nothing to install. Every run writes REPORT.md (Report button) explaining what was recovered.
Files of any size open in full; files over 1.5 MB are shown without syntax colors so the app stays fast.

Buttons: Summon = open a file, Extract = export the recovered source, Banish = cancel the run.

Nothing can hang forever: the Banish (cancel) button stops any run (and all helper processes), and:
  Native (Ghidra): analysis gets up to 10 min and decompiling up to 15 min. When a limit is hit, everything
                   that finished is kept and written to decompiled.c (the log says how many functions were
                   not reached). If that produces nothing, Ghidra is retried in quick mode (no analysis step,
                   10 min). If everything fails you still get the disassembly.
  Java/APK (jadx): 30 min, then shows whatever it finished.
  .NET (ilspycmd): 20 min, then shows whatever it finished.
  Python: 60 s per file; a stuck file gets a bytecode listing and the rest continue.

APK/DEX/JAR/class     -> Java source
Windows .exe/.dll, ELF -> decompiled.c (C-like, reconstructed by Ghidra) + types.h + disassembly/ folder
PyInstaller programs   -> embedded Python modules (source for Python <=3.8, bytecode listing for newer)
Electron .asar         -> original JavaScript/HTML/CSS files
.NET .exe/.dll         -> class/method listing (full C# needs ilspycmd)
.pyc                   -> source for Python <=3.8, else bytecode listing

Getting native code closer to the original: a compiled program does not contain its variable names, parameter
names or struct layouts, so without debug info the C is a reconstruction (param_1, local_58, CubeObject + i * 0x40).
If it is your own program, build it with -g (and -O0 for the closest match) and open it again: Ghidra then reads the
debug info and the output uses your real names and structs. The log and REPORT.md tell you when debug info is missing.
Readability pass: casts are hidden, numbers are decimal, raw float bits become 1.0/0.5, variables are declared where first used,
x * 1.0 -> x, i + 1 + 24 -> i + 25, for (int i = 0; ...; i++) loops and "} else {" are restored, and gcc's stack-protector check is removed. Without debug info the
names are still generic (param_1, iVar3, lVar2) and struct fields are still offsets (p[16] instead of cube.x): that is not in the file.
Compiler start-up code (_start, _init, PLT stubs...) is hidden; set DS_KEEP_BOILERPLATE=1 to see it.

APK / Android: the output has a maincode/ folder with the app's own code (manifest + the app's packages, hard-linked so it costs no
extra space). Unity IL2CPP games keep their C# in native code, so maincode/unity/ holds a names-only dump read from
global-metadata.dat: every class, field, property, method and parameter name (e.g. DaysPlayed) plus all string literals.
Method bodies stay in libil2cpp.so; use Il2CppDumper/Cpp2IL for full types. Obfuscated packages (a/, a0/...) have no original names.
The app repairs an out-of-date Ghidra helper jar by itself (log shows "helper v5").

Flutter apps: their real code is Dart inside lib/*/libapp.so. maincode/flutter/strings.txt lists every text string found in it
(screen text like "Drop a flare", class and function names) and dart_files.txt lists the Dart file paths, so search finds them.
Method bodies are not recoverable that way (use blutter or reFlutter for that).

Explain (the Oracle): select some code (or just put the cursor in a function) and press Explain / Ctrl+E. A small AI model explains it
in plain English. No model is bundled: it uses one running on your own computer, so nothing leaves the machine.
  Easiest: install Ollama, then  ollama pull qwen2.5-coder:1.5b   (any OpenAI-style server works too: Explain menu > Server address,
  address ending in /v1, e.g. http://127.0.0.1:8080/v1). Explain menu > Choose model to pick another. Env: DS_LLM_URL, DS_LLM_MODEL.
  Small models can be wrong: treat it as a hint and check it against the code.

Themes: Theme menu -> Evil (main, default), Void, Toxic, Frost, Bone (light). The choice is remembered.

Ghidra notes: big programs take a while (Ghidra analyses first, then decompiles every function; several
CPU cores are used). It needs RAM: up to 60% of the machine's memory is given to it. Time limits can be
scaled for slow machines with the environment variable DECOMPILER_STUDIO_GHIDRA_TIMEOUT_SCALE (e.g. 2).
Ghidra is Apache-2.0 licensed (NSA); its notices are in _internal/backends/ghidra/LICENSE.

Title font: Metal Mania (SIL Open Font License, see _internal/assets/MetalMania-OFL.txt).
Windows/macOS builds: push source/ to GitHub; the included workflow builds them (Windows gets Ghidra;
macOS has no Ghidra decompiler binary, so it falls back to assembly like before).
