@echo off
rem Run this on a Windows PC with Python 3.12+ installed (python.org, tick "Add to PATH").
pip install PySide6-Essentials pyinstaller pygments capstone pefile pyelftools dnfile decompyle3 uncompyle6 || exit /b 1
if not exist backends\jadx (
  mkdir backends
  powershell -Command "Invoke-WebRequest https://github.com/skylot/jadx/releases/download/v1.5.1/jadx-1.5.1.zip -OutFile jadx.zip; Expand-Archive jadx.zip backends\jadx"
)
if not exist backends\jre (
  powershell -Command "Invoke-WebRequest https://github.com/adoptium/temurin25-binaries/releases/download/jdk-25.0.4%%2B7/OpenJDK25U-jre_x64_windows_hotspot_25.0.4_7.zip -OutFile jre.zip; Expand-Archive jre.zip jre_tmp; mkdir backends\jre; Move-Item jre_tmp\*\* backends\jre"
)
if not exist backends\ghidra (
  rem Ghidra (C decompiler). A JDK 25 is needed once, only to compile the small helper in ghidra_helper\.
  powershell -Command "Invoke-WebRequest https://github.com/NationalSecurityAgency/ghidra/releases/download/Ghidra_12.2_build/ghidra_12.2_PUBLIC_20261016.zip -OutFile ghidra.zip; Expand-Archive ghidra.zip ghidra_tmp; Move-Item ghidra_tmp\ghidra_12.2_PUBLIC backends\ghidra"
  powershell -Command "Invoke-WebRequest https://github.com/adoptium/temurin25-binaries/releases/download/jdk-25.0.4%%2B7/OpenJDK25U-jdk_x64_windows_hotspot_25.0.4_7.zip -OutFile jdk.zip; Expand-Archive jdk.zip jdk_tmp"
  for /d %%D in (jdk_tmp\*) do python ghidra_helper\build_helper.py backends\ghidra %%D\bin\javac.exe || exit /b 1
  rmdir /s /q backends\ghidra\docs backends\ghidra\server backends\ghidra\docker backends\ghidra\Extensions backends\ghidra\Ghidra\Features\Decompiler\os\linux_x86_64
)
python -m PyInstaller --noconfirm --onedir --windowed --name decompiler-studio ^
  --add-data "backends\jadx;backends\jadx" --add-data "backends\jre;backends\jre" --add-data "backends\ghidra;backends\ghidra" --hidden-import theme --hidden-import unpackers --hidden-import tidy --hidden-import il2cpp --hidden-import llm --hidden-import helper_blob --hidden-import unity_assets --collect-all UnityPy --collect-all fmod_toolkit --collect-all texture2ddecoder --collect-all etcpak --collect-all astc_encoder --collect-all tpk_ar --collect-all archspec ^
  --collect-submodules decompyle3 --collect-submodules uncompyle6 --collect-all xdis --collect-all dnfile app.py
echo Done: dist\decompiler-studio\decompiler-studio.exe
