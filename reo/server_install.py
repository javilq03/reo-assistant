"""Download the official llama.cpp `llama-server` for Windows into Reo's folder (the managed-server runtime, like Capri's).
Nothing is installed system-wide. `pick_assets` is pure so it can be tested without the network."""
from __future__ import annotations

import logging
import re
import shutil
import zipfile
from pathlib import Path
from typing import Callable

from .config import app_dir

log = logging.getLogger("reo.server_install")
RELEASE_API = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
KINDS = ("cpu", "vulkan", "cuda")


class ServerInstallError(RuntimeError):
    pass


def server_dir() -> Path:
    return app_dir() / "llama-server"


def exe_path() -> Path | None:
    d = server_dir()
    for name in ("llama-server.exe", "llama-server"):
        p = d / name
        if p.is_file():
            return p
        hits = list(d.rglob(name)) if d.is_dir() else []
        if hits:
            return hits[0]
    return None


def pick_assets(release: dict, kind: str = "cpu") -> list[dict]:
    """The zip(s) to download for this kind of machine: [main build] plus the CUDA runtime zip for 'cuda'."""
    kind = kind if kind in KINDS else "cpu"
    assets = [a for a in release.get("assets", []) if str(a.get("name", "")).endswith(".zip")]

    def main(a):
        n = a["name"].lower()
        return "bin-win" in n and "x64" in n and not n.startswith("cudart") and re.search(rf"[-_]{kind}[-_.]", n) and "hip" not in n
    mains = sorted((a for a in assets if main(a)), key=lambda a: a["name"], reverse=True)
    if not mains:
        return []
    chosen = [mains[0]]
    if kind == "cuda":
        tag = re.search(r"cuda-([\d.]+)", mains[0]["name"].lower())
        rt = [a for a in assets if a["name"].lower().startswith("cudart") and (not tag or tag.group(1) in a["name"].lower())]
        if rt:
            chosen.append(rt[0])
    return chosen


def install(emit: Callable, kind: str = "cpu", client_factory: Callable | None = None) -> Path:
    """emit(state=..., message=..., done=..., total=...). Returns the folder with llama-server."""
    import time
    final = server_dir()
    work = final.parent / "_server_work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    try:
        if client_factory is None:
            import httpx
            client_factory = lambda: httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30.0, read=120.0), headers={"User-Agent": "Reo/1.0"})
        with client_factory() as client:
            emit(state="installing", message="Looking up llama-server...", done=0, total=0)
            r = client.get(RELEASE_API)
            r.raise_for_status()
            plan = pick_assets(r.json(), kind)
            if not plan:
                raise ServerInstallError(f"No Windows '{kind}' build of llama-server was found in the latest llama.cpp release.")
            total = sum(int(a.get("size") or 0) for a in plan)
            done, last = 0, 0.0
            for a in plan:
                part = work / (a["name"] + ".part")
                with client.stream("GET", a["browser_download_url"]) as resp:
                    resp.raise_for_status()
                    with open(part, "wb") as fh:
                        for chunk in resp.iter_bytes():
                            fh.write(chunk)
                            done += len(chunk)
                            if time.time() - last >= 0.15:
                                emit(state="downloading", message="Downloading llama-server...", done=done, total=max(total, done))
                                last = time.time()
                part.replace(work / a["name"])
            emit(state="installing", message="Unpacking llama-server...", done=total, total=total)
            stage = work / "stage"
            for a in plan:
                with zipfile.ZipFile(work / a["name"]) as z:
                    for m in z.namelist():                      # refuse paths that escape the folder
                        if Path(m).is_absolute() or ".." in Path(m).parts:
                            raise ServerInstallError("The download contained an unsafe path.")
                    z.extractall(stage)
            if not (list(stage.rglob("llama-server.exe")) or list(stage.rglob("llama-server"))):
                raise ServerInstallError("The download did not contain llama-server.")
            if final.exists():
                shutil.rmtree(final)
            final.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(stage), str(final))
    except ServerInstallError:
        raise
    except Exception as e:                                       # noqa: BLE001
        log.exception("llama-server install failed")
        raise ServerInstallError(f"{type(e).__name__}: {e}") from e
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return final
