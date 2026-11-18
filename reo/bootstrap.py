"""Start-up dependency check for `python run.py`: installs what is missing and makes sure the llama.cpp
runtime actually loads (a wheel can install fine and still fail with "Could not find module llama.dll").
Skipped in the packaged Reo.exe, where everything is bundled."""
from __future__ import annotations

import glob
import importlib.metadata as md
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQUIRED = {"webview": "pywebview>=5.3", "httpx": "httpx>=0.27", "psutil": "psutil>=5.9", "keyring": "keyring>=25",
            "PIL": "pillow>=10.1", "numpy": "numpy>=1.24"}
OPTIONAL = {"pynvml": "nvidia-ml-py>=12", "sounddevice": "sounddevice>=0.4", "faster_whisper": "faster-whisper>=1.0",
            "piper": "piper-tts>=1.3", "pypdf": "pypdf>=4.0"}                       # offline neural voices
LLAMA_DEPS = ["typing-extensions>=4.5.0", "numpy>=1.20.0", "diskcache>=5.6.1", "jinja2>=2.11.3"]
WHEELS = "https://abetlen.github.io/llama-cpp-python/whl/"
CUDA_PIP = ["nvidia-cuda-runtime-cu12", "nvidia-cublas-cu12"]     # the CUDA DLLs the GPU wheel needs
VCREDIST = "https://aka.ms/vs/17/release/vc_redist.x64.exe"

_dll_handles: list = []      # os.add_dll_directory() is undone when its handle is garbage collected


def _has(mod: str) -> bool:
    return importlib.util.find_spec(mod) is not None


def _pip(*args: str) -> bool:
    return subprocess.call([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *args]) == 0


def add_cuda_dll_dirs() -> None:
    """Let Windows find CUDA DLLs shipped by the nvidia-* pip packages (or a CUDA Toolkit install)."""
    if os.name != "nt":
        return
    import site
    roots = set(site.getsitepackages() + [site.getusersitepackages()])
    dirs = [d for r in roots for d in glob.glob(os.path.join(r, "nvidia", "*", "bin"))]
    if os.environ.get("CUDA_PATH"):
        dirs.append(os.path.join(os.environ["CUDA_PATH"], "bin"))
    for d in dirs:
        if os.path.isdir(d):
            try:
                _dll_handles.append(os.add_dll_directory(d))
            except OSError:
                pass
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


def _check() -> tuple[bool, str]:
    """Import llama_cpp in a fresh process. Returns (works, "GPU" | "CPU" | error text)."""
    code = (f"import sys; sys.path.insert(0, {str(ROOT)!r}); from reo import bootstrap; bootstrap.add_cuda_dll_dirs(); "
            "import llama_cpp; print('GPU' if llama_cpp.llama_supports_gpu_offload() else 'CPU')")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip() in ("GPU", "CPU"):
        return True, r.stdout.strip()
    return False, (r.stderr.strip().splitlines() or ["import failed"])[-1]


def _marker() -> Path:
    from .config import app_dir
    return app_dir() / "runtime_ok.json"


def _signature() -> str:
    try:
        return f"{sys.version_info[:2]}-{md.version('llama-cpp-python')}"
    except md.PackageNotFoundError:
        return ""


def _runtime_known_good() -> bool:
    sig = _signature()
    try:
        return bool(sig) and json.loads(_marker().read_text()).get("sig") == sig
    except (OSError, ValueError):
        return False


def _install_llama(variants: list[str]) -> bool:
    _pip(*LLAMA_DEPS)
    last = ""
    for v in variants:
        print(f"Reo: installing the llama.cpp {'GPU (CUDA)' if v == 'cuda' else 'CPU'} runtime...")
        subprocess.call([sys.executable, "-m", "pip", "uninstall", "-y", "llama-cpp-python"], stdout=subprocess.DEVNULL)
        if v == "cuda":
            _pip(*CUDA_PIP)
        url = WHEELS + ("cu124" if v == "cuda" else "cpu")
        if not _pip("--no-deps", "--only-binary=:all:", "--no-cache-dir", "--index-url", url, "llama-cpp-python"):
            last = f"no {v} build is available for Python {sys.version_info.major}.{sys.version_info.minor}"
            continue
        ok, kind = _check()
        if ok and (v == "cpu" or kind == "GPU"):
            _marker().write_text(json.dumps({"sig": _signature(), "kind": kind}))
            print(f"Reo: runtime ready ({kind}).")
            return True
        last = kind
        print(f"Reo: the {v} build did not start ({last}).")
    print(f"\nReo could not set up its llama.cpp runtime: {last}\n"
          f"  - Use 64-bit Python 3.12 (prebuilt wheels exist for 3.10-3.12; newer versions may not have them).\n"
          f"  - Install the Microsoft Visual C++ Redistributable: {VCREDIST}")
    return False


def _wait_for_parent() -> None:
    pid = os.environ.pop("REO_RESTART_WAIT", "")
    if not pid.isdigit():
        return
    import psutil
    end = time.time() + 20
    while psutil.pid_exists(int(pid)) and time.time() < end:
        time.sleep(0.3)


def _pop_request() -> str:
    from .config import app_dir
    p = app_dir() / "settings.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    req = data.get("runtime_request", "")
    if req:
        data["runtime_request"] = ""
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return req if req in ("cpu", "cuda") else ""


def ensure() -> None:
    if getattr(sys, "frozen", False):
        return
    need = [req for mod, req in REQUIRED.items() if not _has(mod)]
    if need:
        print("Reo: installing missing packages:", ", ".join(need))
        if not _pip(*need):
            sys.exit("Could not install the required packages. Check your internet connection and try again.")
    opt = dict(OPTIONAL)
    if os.name == "nt":
        opt["comtypes"] = "comtypes>=1.4"                     # spoken replies (Windows speech engine)
    for mod, req in opt.items():
        if not _has(mod):
            _pip(req)                                        # best effort

    _wait_for_parent()
    request = _pop_request()
    nvidia = bool(shutil.which("nvidia-smi"))
    if request:
        _install_llama(["cuda", "cpu"] if request == "cuda" else ["cpu"])
    elif not _has("llama_cpp"):
        _install_llama(["cuda", "cpu"] if nvidia else ["cpu"])
    elif not _runtime_known_good():
        add_cuda_dll_dirs()
        ok, kind = _check()
        if ok:
            _marker().write_text(json.dumps({"sig": _signature(), "kind": kind}))
        else:
            print(f"Reo: the installed llama.cpp runtime does not start ({kind}). Repairing...")
            _install_llama(["cuda", "cpu"] if nvidia else ["cpu"])
    add_cuda_dll_dirs()
