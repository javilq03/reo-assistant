# PyInstaller spec: one-folder Windows build, no console window.
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_all

datas = [("reo/ui", "reo/ui"), ("reo/data", "reo/data")] + collect_data_files("llama_cpp")
binaries = collect_dynamic_libs("llama_cpp")
for pkg in ("faster_whisper", "ctranslate2", "onnxruntime", "av", "sounddevice", "_sounddevice_data", "piper", "comtypes"):
    try:
        d, b, hi = collect_all(pkg); datas += d; binaries += b
    except Exception as e:
        print(f"WARNING: {pkg} could not be bundled ({e}). Reo can still download the speech engine at run time, but bundle it if you can.")

a = Analysis(["run.py"], datas=datas, binaries=binaries,
             hiddenimports=["keyring.backends.Windows", "pynvml", "pypdf"], excludes=["tkinter"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Reo", console=False, icon="reo/ui/assets/reo.ico")
coll = COLLECT(exe, a.binaries, a.datas, name="Reo")
