#!/usr/bin/env bash
# Decompiler Studio installer (Linux x86_64).
#   install / update:  curl -fsSL https://raw.githubusercontent.com/rxvy-dev/decompiler-studio/main/install.sh | bash
#   uninstall:         curl -fsSL https://raw.githubusercontent.com/rxvy-dev/decompiler-studio/main/install.sh | bash -s -- --uninstall
#   without the AI model:                                                                       ... | bash -s -- --no-ai
#   pick the AI model yourself:                                                                 ... | bash -s -- --model=qwen2.5-coder:7b
# Installs into ~/.local/share/decompiler-studio (override with DS_DIR) and puts `decompiler-studio` in ~/.local/bin.
# Needs: python3 (3.9+ with venv), git, curl. It downloads jadx, a Java 25 runtime and Ghidra (about 600 MB) from
# their official GitHub releases, plus Ollama and the Qwen2.5-Coder model for Explain / Malware check (4.7 GB, or
# about 9 GB on a 12 GB+ graphics card). Nothing is installed system-wide and no root is needed.
set -euo pipefail

DIR="${DS_DIR:-$HOME/.local/share/decompiler-studio}"
BIN="${DS_BIN:-$HOME/.local/bin}"
REPO="${DS_REPO:-https://github.com/rxvy-dev/decompiler-studio}"
JADX_URL="https://github.com/skylot/jadx/releases/download/v1.5.1/jadx-1.5.1.zip"
JRE_URL="https://github.com/adoptium/temurin25-binaries/releases/download/jdk-25.0.4%2B7/OpenJDK25U-jre_x64_linux_hotspot_25.0.4_7.tar.gz"
GHIDRA_URL="https://github.com/NationalSecurityAgency/ghidra/releases/download/Ghidra_12.2_build/ghidra_12.2_PUBLIC_20261016.zip"

say() { printf '\033[1;31m[decompiler-studio]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[decompiler-studio] %s\033[0m\n' "$*" >&2; exit 1; }

NO_AI=0; MODEL=""; UNINSTALL=0
for a in "$@"; do
  case "$a" in
    --uninstall) UNINSTALL=1 ;;
    --no-ai) NO_AI=1 ;;
    --model=*) MODEL="${a#--model=}" ;;
  esac
done

if [ "$UNINSTALL" = 1 ]; then
  rm -rf "$DIR" "$BIN/decompiler-studio" "$HOME/.local/share/applications/decompiler-studio.desktop" \
         "$HOME/.local/share/icons/decompiler-studio.png"
  say "removed."
  exit 0
fi

[ "$(uname -s)" = "Linux" ] || die "this installer is for Linux (Windows: see the README)."
[ "$(uname -m)" = "x86_64" ] || die "x86_64 only (Ghidra's decompiler ships for x86_64)."
for c in python3 git curl; do command -v "$c" >/dev/null || die "missing '$c': install it with your package manager and run this again."; done
python3 -c 'import sys, venv; assert sys.version_info >= (3, 9)' 2>/dev/null \
  || die "need python3 >= 3.9 with venv (Debian/Ubuntu: sudo apt install python3-venv)."

mkdir -p "$DIR" "$BIN"
APP="$DIR/app"

# ---- 1. the app
if [ -n "${DS_SRC:-}" ]; then
  say "using local source $DS_SRC"
  mkdir -p "$APP"; cp -a "$DS_SRC"/. "$APP"/
elif [ -d "$APP/.git" ]; then
  say "updating the app..."
  git -C "$APP" pull --ff-only -q
else
  say "downloading the app..."
  rm -rf "$APP"; git clone --depth 1 -q "$REPO" "$APP"
fi

# ---- 2. helpers (skipped when already there)
fetch() {  # url out
  curl -fL --retry 3 --progress-bar -o "$2" "$1"
}
unzip_keep_modes() {  # zip dest   (python's zipfile drops the executable bit; put it back)
  python3 - "$1" "$2" <<'PY'
import os, sys, zipfile
z = zipfile.ZipFile(sys.argv[1]); dest = sys.argv[2]
for i in z.infolist():
    p = z.extract(i, dest)
    mode = i.external_attr >> 16
    if mode and not i.is_dir():
        os.chmod(p, mode & 0o7777)
PY
}
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
B="$APP/backends"; mkdir -p "$B"

if [ ! -x "$B/jadx/bin/jadx" ]; then
  say "downloading jadx (Java/Android decompiler)..."
  fetch "$JADX_URL" "$TMP/jadx.zip"
  rm -rf "$B/jadx"; unzip_keep_modes "$TMP/jadx.zip" "$B/jadx"; chmod +x "$B/jadx/bin/jadx"
fi
if [ ! -x "$B/jre/bin/java" ]; then
  say "downloading Java 25 runtime..."
  fetch "$JRE_URL" "$TMP/jre.tgz"
  rm -rf "$B/jre"; mkdir -p "$B/jre"; tar -xzf "$TMP/jre.tgz" -C "$B/jre" --strip-components=1
fi
if [ ! -f "$B/ghidra/Ghidra/application.properties" ]; then
  say "downloading Ghidra (native-code decompiler, about 500 MB)..."
  fetch "$GHIDRA_URL" "$TMP/ghidra.zip"
  rm -rf "$B/ghidra" "$TMP/g"; unzip_keep_modes "$TMP/ghidra.zip" "$TMP/g"
  mv "$TMP/g"/ghidra_* "$B/ghidra"
  rm -rf "$B/ghidra/docs" "$B/ghidra/server" "$B/ghidra/docker" "$B/ghidra/Extensions" \
         "$B/ghidra/Ghidra/Features/GhidraServer" "$B/ghidra/Ghidra/Features/Decompiler/os/win_x86_64"
fi
# the tiny helper module Decompiler Studio adds to Ghidra (the app writes ds-ghidra.jar itself on first use)
mkdir -p "$B/ghidra/Ghidra/Features/DecompilerStudio/lib"
: > "$B/ghidra/Ghidra/Features/DecompilerStudio/Module.manifest"

# ---- 3. python environment
say "installing Python packages..."
[ -x "$DIR/venv/bin/python" ] || python3 -m venv "$DIR/venv"
"$DIR/venv/bin/python" -m pip install -q --upgrade pip
"$DIR/venv/bin/python" -m pip install -q -r "$APP/requirements.txt"

# ---- 4. launcher, icon, menu entry
cat > "$BIN/decompiler-studio" <<LAUNCH
#!/usr/bin/env bash
exec "$DIR/venv/bin/python" "$APP/app.py" "\$@"
LAUNCH
chmod +x "$BIN/decompiler-studio"
mkdir -p "$HOME/.local/share/icons" "$HOME/.local/share/applications"
QT_QPA_PLATFORM=offscreen "$DIR/venv/bin/python" - "$HOME/.local/share/icons/decompiler-studio.png" "$APP" <<'PY' 2>/dev/null || true
import sys
sys.path.insert(0, sys.argv[2])
from PySide6.QtWidgets import QApplication
import theme
a = QApplication([])
theme.app_icon(256).pixmap(256, 256).save(sys.argv[1])
PY
cat > "$HOME/.local/share/applications/decompiler-studio.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=Decompiler Studio
Comment=APK, JAR, EXE and ELF to a browsable source tree
Exec=$BIN/decompiler-studio %f
Icon=$HOME/.local/share/icons/decompiler-studio.png
Terminal=false
Categories=Development;
DESK

# ---- 5. the AI model for Explain / Malware check (a private Ollama + Qwen2.5-Coder)
if [ "$NO_AI" = 1 ]; then
  say "skipping the AI model (--no-ai). Add it later from the app: Explain > Install AI model."
else
  say "installing the AI model for Explain / Malware check (several GB, one time; skip with --no-ai)..."
  "$DIR/venv/bin/python" "$APP/llm.py" --install ${MODEL:+--model=$MODEL} \
    || say "the AI model was not installed; the app works without it (Explain > Install AI model to retry)."
fi

say "done. Run:  decompiler-studio   (or find it in your app menu)"
case ":$PATH:" in *":$BIN:"*) ;; *) say "add $BIN to your PATH to run it by name: export PATH=\"$BIN:\$PATH\"";; esac
say "USE FOR PERSONAL AND EDUCATIONAL PURPOSES ONLY."
