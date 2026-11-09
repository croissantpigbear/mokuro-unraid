#!/usr/bin/env python3
"""
mokuro web UI + background worker.

- No arguments: starts the web UI (port WEBUI_PORT) plus a worker that runs
  mokuro on queued volumes, and a scanner that auto-queues new volumes when
  auto-processing is on.
- With arguments: runs `mokuro <args>` once and exits (manual mode).
"""
import base64
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import List, Optional

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".avif"}
ARCHIVE_EXTS = {".cbz", ".zip"}

MANGA_DIR = Path(os.environ.get("MANGA_DIR", "/manga"))
CONFIG_DIR = Path(os.environ.get("HOME", "/config"))
WATCH = os.environ.get("WATCH", "true").lower() == "true"
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL", "300"))
SETTLE_SECONDS = int(os.environ.get("SETTLE_SECONDS", "120"))
FORCE_CPU = os.environ.get("FORCE_CPU", "false").lower() == "true"
def _normalise_args(args):
    """mokuro's README mentions --disable_html, but the released 0.2.x CLI calls it --legacy_html."""
    out = []
    for a in args:
        name, _, val = a.partition("=")
        if name == "--disable_html":
            if val.lower() in ("", "true", "1"):
                out.append("--legacy_html=False")
            continue
        out.append(a)
    return out


EXTRA_ARGS = _normalise_args(os.environ.get("EXTRA_ARGS", "").split())
PUID = os.environ.get("PUID")
PGID = os.environ.get("PGID")
UMASK = os.environ.get("UMASK")
WEBUI_PORT = int(os.environ.get("WEBUI_PORT", "8090"))
WEBUI_PASSWORD = os.environ.get("WEBUI_PASSWORD", "")
STATIC_DIR = Path(__file__).parent / "static"
STATE_FILE = CONFIG_DIR / "webui-state.json"

TQDM_RE = re.compile(r"\|\s*(\d+)/(\d+)\s*\[([^\]]*)\]")


# --------------------------------------------------------------------------- state

class State:
    def __init__(self):
        self.lock = threading.RLock()
        self.queue = []            # [{path, force, no_cache, added}]
        self.current = None        # {path, started, done, total, rate, eta, stage}
        self.history = deque(maxlen=100)
        self.failures = {}         # path -> {error, time}
        self.logs = deque(maxlen=2000)
        self.log_seq = 0
        self.watch = WATCH
        self.last_scan = None
        self.proc = None
        self.cancel_requested = False
        self.cuda = None           # torch.cuda.is_available() inside the container
        self.wake = threading.Event()
        self.scan_now = threading.Event()
        self._lib_cache = None
        self._lib_time = 0.0

    def load(self):
        try:
            data = json.loads(STATE_FILE.read_text())
        except (OSError, ValueError):
            return
        self.failures = data.get("failures", {})
        self.history.extend(data.get("history", []))
        self.queue = data.get("queue", [])
        if "watch" in data:
            self.watch = data["watch"]

    def save(self):
        with self.lock:
            data = {
                "failures": self.failures,
                "history": list(self.history),
                "queue": self.queue,
                "watch": self.watch,
            }
        try:
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(STATE_FILE)
        except OSError as e:
            print(f"could not save state: {e}", flush=True)

    def log(self, msg):
        with self.lock:
            self.log_seq += 1
            self.logs.append((self.log_seq, time.time(), msg))
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

    def invalidate(self):
        with self.lock:
            self._lib_cache = None


S = State()


# --------------------------------------------------------------------------- helpers

def drop_privileges():
    """Run as Unraid's nobody:users (99:100) so files on the share stay editable."""
    if UMASK:
        os.umask(int(UMASK, 8))
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if os.getuid() != 0 or not PUID or not PGID:
        return
    uid, gid = int(PUID), int(PGID)
    subprocess.run(["chown", "-R", f"{uid}:{gid}", str(CONFIG_DIR)], check=False)
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)


def resolve_rel(rel: str) -> Path:
    root = MANGA_DIR.resolve()
    p = (root / rel).resolve()
    if p != root and root not in p.parents:
        raise ValueError("path outside manga folder")
    return p


def rel(p: Path) -> str:
    return p.resolve().relative_to(MANGA_DIR.resolve()).as_posix()


def target_for(vol: Path) -> Path:
    if vol.is_file():
        return vol.with_suffix(".mokuro")
    return vol.parent / f"{vol.name}.mokuro"


def newest_mtime(p: Path) -> float:
    try:
        t = p.stat().st_mtime
    except FileNotFoundError:
        return 0
    if p.is_dir():
        for child in p.iterdir():
            try:
                t = max(t, child.stat().st_mtime)
            except FileNotFoundError:
                pass
    return t


def natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def build_cmd(args):
    cmd = ["mokuro", "--disable_confirmation=True"]
    if FORCE_CPU:
        cmd.append("--force_cpu=True")
    return cmd + EXTRA_ARGS + [str(a) for a in args]


def find_volumes():
    vols = []
    root = MANGA_DIR
    if not root.is_dir():
        return vols
    for dirpath, dirs, files in os.walk(root):
        d = Path(dirpath)
        dirs[:] = [x for x in dirs if not x.startswith(".") and not x.endswith("_ocr")]
        if d != root and any(Path(f).suffix.lower() in IMAGE_EXTS for f in files):
            vols.append(d)
            dirs[:] = []  # don't descend into a volume
        for f in files:
            fp = d / f
            if fp.suffix.lower() in ARCHIVE_EXTS and not fp.with_suffix("").is_dir():
                vols.append(fp)
    return vols


def library(refresh=False):
    with S.lock:
        if not refresh and S._lib_cache and time.time() - S._lib_time < 5:
            return S._lib_cache
        queued = {q["path"] for q in S.queue}
        current = S.current["path"] if S.current else None
        failures = dict(S.failures)

    now = time.time()
    groups = {}
    for vol in find_volumes():
        r = rel(vol)
        target = target_for(vol)
        if r == current:
            status = "processing"
        elif r in queued:
            status = "queued"
        elif target.exists():
            status = "done"
        elif r in failures:
            status = "failed"
        elif now - newest_mtime(vol) < SETTLE_SECONDS:
            status = "settling"
        else:
            status = "pending"
        series = vol.parent
        s_rel = "" if series.resolve() == MANGA_DIR.resolve() else rel(series)
        groups.setdefault(s_rel, []).append({
            "path": r,
            "name": vol.name,
            "kind": "archive" if vol.is_file() else "folder",
            "status": status,
            "error": failures.get(r, {}).get("error") if status == "failed" else None,
        })

    series_list = []
    counts = {"all": 0, "pending": 0, "done": 0, "failed": 0, "queued": 0, "processing": 0, "settling": 0}
    for s_rel in sorted(groups, key=natural_key):
        vols = sorted(groups[s_rel], key=lambda v: natural_key(v["name"]))
        for v in vols:
            counts["all"] += 1
            counts[v["status"]] += 1
        series_list.append({"path": s_rel, "name": s_rel or "(top level)", "volumes": vols})

    result = {"series": series_list, "counts": counts, "scanned": now}
    with S.lock:
        S._lib_cache, S._lib_time = result, now
    return result


def enqueue(r: str, force=False, no_cache=False, front=False) -> bool:
    with S.lock:
        if S.current and S.current["path"] == r:
            return False
        if any(q["path"] == r for q in S.queue):
            return False
        S.failures.pop(r, None)
        item = {"path": r, "force": force, "no_cache": no_cache, "added": time.time()}
        if front:
            S.queue.insert(0, item)
        else:
            S.queue.append(item)
    S.invalidate()
    S.save()
    S.wake.set()
    return True


def queue_pending(series: Optional[str] = None) -> int:
    n = 0
    for s in library(refresh=True)["series"]:
        if series is not None and s["path"] != series:
            continue
        for v in s["volumes"]:
            if v["status"] == "pending" and enqueue(v["path"]):
                n += 1
    return n


# --------------------------------------------------------------------------- gpu

_gpu_cache = {"t": 0, "v": None}


def gpu_info():
    if time.time() - _gpu_cache["t"] < 2:
        return _gpu_cache["v"]
    info = {"available": False, "cuda": S.cuda, "force_cpu": FORCE_CPU}
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
        )
        line = out.stdout.strip().splitlines()[0]
        name, util, used, total, temp = [x.strip() for x in line.split(",")]

        def num(x):
            try:
                return float(x)
            except ValueError:
                return None

        info.update(available=True, name=name, util=num(util), mem_used=num(used),
                    mem_total=num(total), temp=num(temp))
    except Exception:  # noqa: BLE001
        pass
    _gpu_cache.update(t=time.time(), v=info)
    return info


def check_cuda():
    try:
        out = subprocess.run(
            [sys.executable, "-c", "import torch;print(torch.cuda.is_available())"],
            capture_output=True, text=True, timeout=120,
        )
        S.cuda = out.stdout.strip() == "True"
    except Exception:  # noqa: BLE001
        S.cuda = False
    if FORCE_CPU:
        S.log("FORCE_CPU is on, mokuro will run on the CPU")
    elif S.cuda:
        g = gpu_info()
        S.log(f"GPU ready: {g.get('name') or 'CUDA device'}")
    else:
        S.log("WARNING: no CUDA GPU visible, mokuro will fall back to CPU (slow). "
              "Check --runtime=nvidia and NVIDIA_VISIBLE_DEVICES.")


# --------------------------------------------------------------------------- worker

def parse_tqdm(line):
    m = TQDM_RE.search(line)
    if not m:
        return None
    done, total, inner = int(m.group(1)), int(m.group(2)), m.group(3)
    parts = [p.strip() for p in inner.split(",")]
    eta, rate = None, None
    if parts and "<" in parts[0]:
        eta = parts[0].split("<", 1)[1] or None
        if eta == "?":
            eta = None
    if len(parts) > 1:
        rate = parts[1] if "?" not in parts[1] else None
    return done, total, eta, rate


def run_job(item):
    r = item["path"]
    try:
        vol = resolve_rel(r)
    except ValueError:
        vol = None
    if vol is None or not vol.exists():
        S.log(f"Skipping, not found: {r}")
        with S.lock:
            S.current = None
        S.invalidate()
        return

    target = target_for(vol)
    if item.get("force"):
        try:
            target.unlink()
        except FileNotFoundError:
            pass

    args = []
    if item.get("no_cache"):
        args.append("--no_cache=True")
    cmd = build_cmd(args + [vol])

    started = time.time()
    with S.lock:
        S.current = {"path": r, "started": started, "done": 0, "total": 0,
                     "eta": None, "rate": None, "stage": "Loading models"}
        S.cancel_requested = False
    S.invalidate()
    S.log(f"▶ {r}")
    S.log("$ " + " ".join(cmd))

    tail = deque(maxlen=8)
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                bufsize=0, env=env, start_new_session=True)
    except OSError as e:
        S.log(f"Could not start mokuro: {e}")
        with S.lock:
            S.current = None
            S.failures[r] = {"error": str(e), "time": time.time()}
        S.save()
        return
    with S.lock:
        S.proc = proc

    def handle(raw: bytes):
        line = raw.decode("utf-8", "replace").rstrip()
        if not line.strip():
            return
        prog = parse_tqdm(line)
        if prog:
            done, total, eta, rate = prog
            with S.lock:
                if S.current:
                    S.current.update(done=done, total=total, eta=eta, rate=rate, stage="OCR")
            return
        tail.append(line)
        S.log(line)

    buf = b""
    while True:
        chunk = proc.stdout.read(4096)
        if not chunk:
            break
        buf += chunk
        parts = re.split(rb"[\r\n]", buf)
        buf = parts.pop()
        for p in parts:
            handle(p)
    handle(buf)
    rc = proc.wait()

    duration = time.time() - started
    with S.lock:
        cancelled = S.cancel_requested
        S.proc = None
        S.current = None
        ok = rc == 0 and target.exists() and not cancelled
        if cancelled:
            result = "cancelled"
        elif ok:
            result = "done"
            S.failures.pop(r, None)
        else:
            result = "failed"
            err = next((l for l in reversed(tail) if "error" in l.lower()), None) or (tail[-1] if tail else f"exit code {rc}")
            S.failures[r] = {"error": err[-400:], "time": time.time()}
        S.history.appendleft({"path": r, "result": result, "duration": duration, "finished": time.time()})
    S.invalidate()
    S.save()
    S.log(f"{'✔' if ok else '✖'} {r} — {result} in {duration:.0f}s")


def worker():
    while True:
        with S.lock:
            item = S.queue.pop(0) if S.queue else None
            if item is not None:  # claim it atomically so the scanner can't re-queue it
                S.current = {"path": item["path"], "started": time.time(), "done": 0, "total": 0,
                             "eta": None, "rate": None, "stage": "Starting"}
        if item is None:
            S.wake.wait(5)
            S.wake.clear()
            continue
        S.save()
        try:
            run_job(item)
        except Exception as e:  # noqa: BLE001
            S.log(f"Worker error on {item.get('path')}: {e}")
            with S.lock:
                S.current = None
                S.proc = None


def scanner():
    while True:
        manual = S.scan_now.is_set()
        S.scan_now.clear()
        if S.watch or manual:
            try:
                n = queue_pending()
                with S.lock:
                    S.last_scan = time.time()
                if n:
                    S.log(f"Scan: queued {n} new volume(s)")
                elif manual:
                    S.log("Scan: nothing new to process")
            except Exception as e:  # noqa: BLE001
                S.log(f"Scan error: {e}")
        S.scan_now.wait(SCAN_INTERVAL)


# --------------------------------------------------------------------------- web app

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, Response  # noqa: E402
from pydantic import BaseModel  # noqa: E402

app = FastAPI(title="mokuro", docs_url=None, redoc_url=None)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if WEBUI_PASSWORD and request.url.path != "/healthz":
        ok = False
        h = request.headers.get("authorization", "")
        if h.startswith("Basic "):
            try:
                _, _, pw = base64.b64decode(h[6:]).decode().partition(":")
                ok = secrets.compare_digest(pw, WEBUI_PASSWORD)
            except Exception:  # noqa: BLE001
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="mokuro"'})
    return await call_next(request)


class PathBody(BaseModel):
    path: str
    force: bool = False
    no_cache: bool = False
    front: bool = False


class SeriesBody(BaseModel):
    series: Optional[str] = None


class WatchBody(BaseModel):
    enabled: bool


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/icon.svg")
def icon():
    return FileResponse(STATIC_DIR / "icon.svg", media_type="image/svg+xml")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/status")
def status(since: int = 0):
    with S.lock:
        logs = [{"seq": s, "t": t, "msg": m} for s, t, m in S.logs if s > since]
        data = {
            "current": dict(S.current) if S.current else None,
            "queue": list(S.queue),
            "history": list(S.history)[:25],
            "watch": S.watch,
            "last_scan": S.last_scan,
            "logs": logs[-500:],
            "log_seq": S.log_seq,
        }
    data["gpu"] = gpu_info()
    data["settings"] = {
        "scan_interval": SCAN_INTERVAL, "settle_seconds": SETTLE_SECONDS,
        "extra_args": " ".join(EXTRA_ARGS), "manga_dir": str(MANGA_DIR),
    }
    return data


@app.get("/api/library")
def get_library(refresh: bool = False):
    return library(refresh)


@app.post("/api/queue")
def add_queue(body: PathBody):
    try:
        vol = resolve_rel(body.path)
    except ValueError:
        raise HTTPException(400, "invalid path")
    if not vol.exists():
        raise HTTPException(404, "volume not found")
    if not enqueue(rel(vol), force=body.force, no_cache=body.no_cache, front=body.front):
        raise HTTPException(409, "already queued or processing")
    S.log(f"Queued: {rel(vol)}" + (" (re-run)" if body.force else ""))
    return {"ok": True}


@app.post("/api/queue/pending")
def add_pending(body: SeriesBody):
    n = queue_pending(body.series)
    if n:
        S.log(f"Queued {n} pending volume(s)")
    return {"queued": n}


@app.post("/api/queue/remove")
def remove_queue(body: PathBody):
    with S.lock:
        before = len(S.queue)
        S.queue = [q for q in S.queue if q["path"] != body.path]
        removed = before != len(S.queue)
    S.invalidate()
    S.save()
    return {"removed": removed}


@app.post("/api/queue/clear")
def clear_queue():
    with S.lock:
        n = len(S.queue)
        S.queue = []
    S.invalidate()
    S.save()
    S.log(f"Cleared {n} item(s) from the queue")
    return {"cleared": n}


@app.post("/api/cancel")
def cancel():
    with S.lock:
        proc = S.proc
        if not proc:
            return {"cancelled": False}
        S.cancel_requested = True
    try:
        os.killpg(proc.pid, 15)
    except ProcessLookupError:
        pass
    S.log("Cancelling current job…")
    return {"cancelled": True}


@app.post("/api/scan")
def scan():
    S.invalidate()
    S.scan_now.set()
    return {"ok": True}


@app.post("/api/watch")
def set_watch(body: WatchBody):
    with S.lock:
        S.watch = body.enabled
    S.save()
    S.log(f"Auto-processing {'on' if body.enabled else 'off'}")
    if body.enabled:
        S.scan_now.set()
    return {"watch": S.watch}


def clean_parts(path: str):
    parts = [p for p in re.split(r"[\\/]+", path) if p not in ("", ".", "..")]
    return [p for p in parts if not p.startswith(".")]


@app.post("/api/upload")
def upload(series: str = Form(""), files: List[UploadFile] = File(...), paths: List[str] = Form(default=[])):
    s_parts = clean_parts(series)
    try:
        dest = resolve_rel("/".join(s_parts)) if s_parts else MANGA_DIR.resolve()
    except ValueError:
        raise HTTPException(400, "invalid series name")
    dest.mkdir(parents=True, exist_ok=True)

    volumes, saved, skipped = set(), 0, 0
    for i, f in enumerate(files):
        relpath = paths[i] if i < len(paths) and paths[i] else (f.filename or "")
        parts = clean_parts(relpath)
        if not parts:
            skipped += 1
            continue
        ext = Path(parts[-1]).suffix.lower()
        if ext in ARCHIVE_EXTS:
            parts = parts[-1:]  # archives always go straight into the series folder
        elif ext not in IMAGE_EXTS or len(parts) < 2:
            skipped += 1        # loose images need a volume folder
            continue
        out = (dest / Path(*parts)).resolve()
        if dest.resolve() not in out.parents:
            skipped += 1
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "wb") as fh:
            shutil.copyfileobj(f.file, fh, 1024 * 1024)
        saved += 1
        volumes.add(out if ext in ARCHIVE_EXTS else out.parent)

    queued = 0
    for v in sorted(volumes, key=lambda p: natural_key(str(p))):
        if not target_for(v).exists() and enqueue(rel(v)):
            queued += 1
    S.invalidate()
    S.log(f"Upload: {saved} file(s) saved to {rel(dest) or '/'}, {queued} volume(s) queued"
          + (f", {skipped} skipped" if skipped else ""))
    return {"saved": saved, "skipped": skipped, "queued": queued}


# --------------------------------------------------------------------------- main

def main():
    drop_privileges()

    if len(sys.argv) > 1:
        sys.exit(subprocess.run(build_cmd(sys.argv[1:])).returncode)

    tmp = CONFIG_DIR / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(tmp)  # big uploads spool here, not inside docker.img

    S.load()
    # failures caused by bad command-line options aren't the volume's fault: let them be retried
    stale = [p for p, f in S.failures.items() if "Could not consume arg" in (f.get("error") or "")]
    for p in stale:
        S.failures.pop(p)
    if stale:
        S.log(f"Cleared {len(stale)} failure(s) caused by an invalid mokuro option")
        S.save()
    if not MANGA_DIR.is_dir():
        S.log(f"WARNING: {MANGA_DIR} does not exist, mount your manga share there")
    S.log(f"Web UI on port {WEBUI_PORT}" + (" (password protected)" if WEBUI_PASSWORD else ""))

    for target in (check_cuda, worker, scanner):
        threading.Thread(target=target, daemon=True).start()

    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=WEBUI_PORT, log_level="warning")


if __name__ == "__main__":
    main()
