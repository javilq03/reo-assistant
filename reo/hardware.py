"""Hardware scan and model recommendation.

scan()                      -> dict describing CPU / RAM / GPUs / disk
recommend(catalog, hw, ctx) -> ranked list of catalog models with fit tier, n_gpu_layers and a speed estimate

The speed number is a rough memory-bandwidth estimate (decode is bandwidth-bound),
good enough to rank models, not a benchmark.
"""
from __future__ import annotations

import ctypes
import json
import math
import os
import platform
import re
import subprocess
import sys
from pathlib import Path

import psutil

GB = 1024 ** 3
IS_WIN = sys.platform == "win32"


# --------------------------------------------------------------------------- CPU
def _cpu_features() -> dict:
    feats = {"avx2": False, "avx512": False}
    if IS_WIN:
        try:
            k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            feats["avx2"] = bool(k32.IsProcessorFeaturePresent(40))      # PF_AVX2_INSTRUCTIONS_AVAILABLE
            feats["avx512"] = bool(k32.IsProcessorFeaturePresent(41))    # PF_AVX512F_INSTRUCTIONS_AVAILABLE
        except Exception:
            pass
    else:
        try:
            flags = Path("/proc/cpuinfo").read_text()
            feats["avx2"] = " avx2" in flags
            feats["avx512"] = "avx512f" in flags
        except OSError:
            pass
    return feats


def _cpu_name() -> str:
    if IS_WIN:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                return str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()
        except Exception:
            pass
    return platform.processor() or platform.machine()


# --------------------------------------------------------------------------- GPU
def _no_window():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WIN else 0


def _gpus_nvml() -> list[dict]:
    try:
        import pynvml  # type: ignore  (package: nvidia-ml-py)
        pynvml.nvmlInit()
        out = []
        for i in range(pynvml.nvmlDeviceGetCount()):
            h = pynvml.nvmlDeviceGetHandleByIndex(i)
            name = pynvml.nvmlDeviceGetName(h)
            name = name.decode() if isinstance(name, bytes) else name
            mem = pynvml.nvmlDeviceGetMemoryInfo(h)
            out.append({"name": name, "vendor": "nvidia", "vram_gb": mem.total / GB, "free_gb": mem.free / GB,
                        "integrated": False, "source": "nvml"})
        pynvml.nvmlShutdown()
        return out
    except Exception:
        return []


def _gpus_nvidia_smi() -> list[dict]:
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=6, creationflags=_no_window())
        if r.returncode != 0:
            return []
        out = []
        for line in r.stdout.strip().splitlines():
            name, total, free = [x.strip() for x in line.split(",")]
            out.append({"name": name, "vendor": "nvidia", "vram_gb": float(total) / 1024, "free_gb": float(free) / 1024,
                        "integrated": False, "source": "nvidia-smi"})
        return out
    except Exception:
        return []


_INTEGRATED = re.compile(r"(uhd|iris|hd graphics|radeon\(tm\) graphics|radeon graphics|vega \d+ graphics|"
                         r"radeon 680m|radeon 780m|radeon 890m|arc graphics|apple)", re.I)


def _vendor(name: str) -> str:
    n = name.lower()
    if "nvidia" in n or "geforce" in n or "rtx" in n or "quadro" in n:
        return "nvidia"
    if "amd" in n or "radeon" in n:
        return "amd"
    if "intel" in n or "arc" in n:
        return "intel"
    return "other"


def _gpus_registry() -> list[dict]:
    """Display adapters from the Windows registry. qwMemorySize is accurate above 4 GB (unlike WMI)."""
    if not IS_WIN:
        return []
    out = []
    try:
        import winreg
        base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as root:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                if not sub.isdigit():
                    continue
                try:
                    with winreg.OpenKey(root, sub) as k:
                        name = str(winreg.QueryValueEx(k, "DriverDesc")[0])
                        mem = 0
                        for val in ("HardwareInformation.qwMemorySize", "HardwareInformation.MemorySize"):
                            try:
                                v = winreg.QueryValueEx(k, val)[0]
                                mem = int.from_bytes(v, "little") if isinstance(v, (bytes, bytearray)) else int(v)
                                if mem:
                                    break
                            except OSError:
                                continue
                except OSError:
                    continue
                if "basic" in name.lower() or "remote" in name.lower():
                    continue
                out.append({"name": name, "vendor": _vendor(name), "vram_gb": mem / GB, "free_gb": None,
                            "integrated": bool(_INTEGRATED.search(name)), "source": "registry"})
    except Exception:
        pass
    return out


def _gpus() -> list[dict]:
    gpus = _gpus_nvml() or _gpus_nvidia_smi()
    seen = {g["name"].lower() for g in gpus}
    for g in _gpus_registry():
        if g["name"].lower() in seen or (g["vendor"] == "nvidia" and gpus):
            continue
        gpus.append(g)
    return gpus


# --------------------------------------------------------------------------- scan
def scan(models_folder: Path | None = None) -> dict:
    vm = psutil.virtual_memory()
    feats = _cpu_features()
    gpus = _gpus()
    discrete = [g for g in gpus if not g["integrated"] and g["vram_gb"] >= 2]
    best = max(discrete, key=lambda g: g["vram_gb"], default=None)
    if best and best["vendor"] == "nvidia":
        backend = "cuda"
    elif best:
        backend = "vulkan"
    else:
        backend = "cpu"
    disk_free = None
    try:
        disk_free = psutil.disk_usage(str(models_folder or Path.home())).free / GB
    except Exception:
        pass
    return {
        "os": f"{platform.system()} {platform.release()}",
        "cpu": {"name": _cpu_name(), "physical": psutil.cpu_count(logical=False) or 1,
                "logical": psutil.cpu_count(logical=True) or 1, **feats},
        "ram_gb": vm.total / GB,
        "ram_free_gb": vm.available / GB,
        "gpus": gpus,
        "best_gpu": best,
        "recommended_backend": backend,
        "disk_free_gb": disk_free,
    }


# --------------------------------------------------------------------------- recommendation
def _gpu_bandwidth(g: dict) -> float:
    if g["vendor"] == "nvidia":
        return 320.0 if g["vram_gb"] >= 10 else 220.0
    return 220.0


def recommend(catalog: list[dict], hw: dict, n_ctx: int = 4096, gpu_offload: bool = True) -> list[dict]:
    """Rank catalog entries for this machine. Returns copies with: tier, n_gpu_layers, need_gb, est_tps, fits."""
    gpu = hw.get("best_gpu") if gpu_offload else None
    gpu_budget = None
    if gpu:
        avail = gpu["free_gb"] if gpu.get("free_gb") else gpu["vram_gb"]
        gpu_budget = max(0.0, min(avail, gpu["vram_gb"]) * 0.92 - 0.6)   # leave room for the desktop
    ram_budget = max(0.0, min(hw["ram_free_gb"], hw["ram_gb"] * 0.7) - 1.0)   # leave room for Windows + Reo
    cpu_bw = 55.0 if hw["cpu"]["physical"] >= 8 else 38.0

    ranked = []
    for m in catalog:
        kv = m.get("kv_mb_per_1k", 100) * (n_ctx / 1000) / 1024
        need = m["size_gb"] + kv + 0.4
        layers = m.get("layers", 32)
        tier, n_layers, tps = "too_big", 0, 0.0
        if gpu_budget is not None and need <= gpu_budget:
            tier, n_layers = "gpu", -1
            tps = _gpu_bandwidth(gpu) * 0.55 / m["size_gb"]
        elif gpu_budget is not None and gpu_budget > 1.5 and need <= gpu_budget + ram_budget:
            frac = max(0.0, min(1.0, (gpu_budget - kv - 0.4) / m["size_gb"]))
            n_layers = max(1, math.floor(layers * frac))
            tier = "hybrid"
            tps = 1.0 / (frac * m["size_gb"] / (_gpu_bandwidth(gpu) * 0.55) + (1 - frac) * m["size_gb"] / (cpu_bw * 0.6))
        elif need <= ram_budget:
            tier, tps = "cpu", cpu_bw * 0.6 / m["size_gb"]
        ranked.append({**m, "tier": tier, "n_gpu_layers": n_layers, "need_gb": round(need, 1),
                       "est_tps": round(tps, 1), "fits": tier != "too_big"})

    # Pick: biggest model that still decodes at a comfortable >= 6 tok/s; else fastest that fits.
    fits = [m for m in ranked if m["fits"]]
    comfy = [m for m in fits if m["est_tps"] >= 6]
    on_gpu = [m for m in comfy if m["tier"] == "gpu"]      # fully offloaded beats a bigger split model
    pool = on_gpu or comfy or sorted(fits, key=lambda m: -m["est_tps"])[:1]
    best = max(pool, key=lambda m: (m["params_b"], m["est_tps"]), default=None)
    for m in ranked:
        m["recommended"] = bool(best and m["id"] == best["id"])
    ranked.sort(key=lambda m: (not m["recommended"], not m["fits"], -m["params_b"]))
    return ranked


def load_catalog(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["models"]
