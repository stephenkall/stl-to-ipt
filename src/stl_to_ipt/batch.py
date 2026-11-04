"""Batch pipeline: STL folder -> spec JSON files -> Inventor .ipt files.

Heavy-processing features:
  * per-stage checkpoints under ``.cache/<stage>/`` (a finished item is skipped on the next run),
  * Ctrl+C-safe interruption (the current item finishes, progress is saved, re-run to continue),
  * progress bars (tqdm) and structured logging,
  * configurable worker count for the CPU-bound conversion stage.
"""
import glob
import hashlib
import json
import logging
import os
import shutil
import signal
import sys
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from tqdm import tqdm

from . import __version__
from . import spec as sp

log = logging.getLogger("stl_to_ipt")

_stop = False


def _on_signal(sig, frame):
    global _stop
    if _stop:
        print("\nForce-quitting.")
        sys.exit(1)
    print("\n[Ctrl+C] Finishing the current item and saving progress. Press again to force-quit.")
    _stop = True


def install_signal_handlers():
    signal.signal(signal.SIGINT, _on_signal)
    if sys.platform != "win32":
        signal.signal(signal.SIGTERM, _on_signal)


def stop_requested():
    return _stop


def worker_count(override=None, fraction=0.75):
    cores = os.cpu_count() or 1
    return min(override, cores) if override else max(1, int(cores * fraction))


# ----------------------------------------------------------------------------- cache
def file_digest(path, extra=""):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    h.update(("%s|%s" % (__version__, extra)).encode())
    return h.hexdigest()[:16]


class StageCache:
    """One cache folder per processing stage. An item is done when its ``.done`` marker matches its digest."""

    def __init__(self, root, stage, enabled=True):
        self.dir = Path(root) / stage
        self.enabled = enabled

    def _marker(self, name):
        return self.dir / (name + ".done")

    def is_done(self, name, digest):
        m = self._marker(name)
        return self.enabled and m.exists() and m.read_text() == digest

    def mark_done(self, name, digest):
        self.dir.mkdir(parents=True, exist_ok=True)
        self._marker(name).write_text(digest)

    def path(self, name, suffix):
        self.dir.mkdir(parents=True, exist_ok=True)
        return self.dir / (name + suffix)


def prepare_cache(cache_dir, clean):
    if clean:
        shutil.rmtree(cache_dir, ignore_errors=True)
        log.info("Cache cleared.")


# ----------------------------------------------------------------------------- stage 1: STL -> spec
def _ignore_sigint():
    """Workers ignore Ctrl+C: only the main process decides when to stop."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def _convert_one(stl_path, spec_dir, tol):
    """Runs in a worker process."""
    from .converter import convert_file
    path, spec = convert_file(stl_path, spec_dir, tol)
    return path, spec["notes"]["iou"], len(spec["features"])


def convert_stage(stl_files, spec_dir, tol, workers, cache, use_cache):
    """Convert every STL to a spec JSON. Return the list of spec paths that exist afterwards."""
    todo, specs = [], []
    for stl in stl_files:
        name = Path(stl).stem
        digest = file_digest(stl, "tol=%s" % tol)
        out = Path(spec_dir) / (name + ".json")
        specs.append(str(out))
        if use_cache and out.exists() and cache.is_done(name, digest):
            log.info("%s: spec found in cache, skipping", name)
        else:
            todo.append((stl, name, digest))
    if not todo:
        return specs
    log.info("Converting %d STL file(s) with %d worker(s)", len(todo), workers)
    with ProcessPoolExecutor(max_workers=workers, initializer=_ignore_sigint) as pool, \
            tqdm(total=len(todo), desc="Converting", unit="part") as bar:
        futures = {pool.submit(_convert_one, stl, spec_dir, tol): (name, digest) for stl, name, digest in todo}
        for fut in as_completed(futures):
            name, digest = futures[fut]
            try:
                _, value, _ = fut.result()
                cache.mark_done(name, digest)
                bar.set_postfix_str("%s IoU %.3f" % (name, value))
            except Exception as e:  # keep going with the next part
                log.error("%s: conversion failed: %s", name, e)
            bar.update(1)
            if _stop:  # finish the running items, drop the queued ones
                pool.shutdown(wait=True, cancel_futures=True)
                break
    return specs


# ----------------------------------------------------------------------------- stage 2: spec -> ipt
def build_stage(spec_files, out_dir, cache, use_cache, validate_only=False):
    """Create one .ipt per spec in a single Inventor session. Return (ok, failures)."""
    from . import inventor
    os.makedirs(out_dir, exist_ok=True)
    app = None
    ok, failures = [], []
    for path in tqdm(sorted(spec_files), desc="Building", unit="part"):
        if _stop:
            log.warning("Interrupted: progress saved. Re-run to continue.")
            break
        name = Path(path).stem
        try:
            spec = sp.load(path)
            errors, warnings = sp.validate(spec)
            for w in warnings:
                log.warning("[%s] %s", name, w)
            if errors:
                raise ValueError("; ".join(errors))
            target = os.path.join(os.path.abspath(out_dir), name + ".ipt")
            digest = file_digest(path)
            if not validate_only:
                if use_cache and os.path.exists(target) and cache.is_done(name, digest):
                    log.info("%s: .ipt found in cache, skipping", name)
                else:
                    if app is None:
                        import win32com.client.dynamic
                        app = win32com.client.dynamic.Dispatch("Inventor.Application")
                    inventor.create_ipt(spec, target, app=app, close=True)
                    cache.mark_done(name, digest)
            ok.append(name)
        except Exception as e:  # keep going with the next part
            failures.append((name, str(e)))
            log.error("[%s] FAILED: %s", name, e)
            logging.getLogger("stl_to_ipt.trace").debug(traceback.format_exc())
    return ok, failures


def build_command(spec_dir, out_dir, cache_dir=".cache", use_cache=True, validate_only=False):
    install_signal_handlers()
    specs = sorted(glob.glob(os.path.join(spec_dir, "*.json")))
    ok, failures = build_stage(specs, out_dir, StageCache(cache_dir, "build", use_cache), use_cache, validate_only)
    log.info("Done: %d ok, %d failed.", len(ok), len(failures))
    for n, e in failures:
        log.error(" - %s: %s", n, e)
    return 0 if not failures else 3


def run_command(stl_dir, out_dir, tol=0.12, workers=None, cache_dir=".cache", use_cache=True,
                skip=("Rudder_2021",)):
    """Full pipeline: STL folder -> specs (``<out>/specs``) -> IPT files (``<out>/ipt``)."""
    install_signal_handlers()
    stl_files = [f for f in sorted(glob.glob(os.path.join(stl_dir, "*.stl")))
                 if not any(s in os.path.basename(f) for s in skip)]
    if not stl_files:
        log.error("No STL files found in %s", stl_dir)
        return 1
    spec_dir = os.path.join(out_dir, "specs")
    ipt_dir = os.path.join(out_dir, "ipt")
    n_workers = worker_count(workers)
    log.info("Stage 1/2 - Converting %d STL file(s) to specs", len(stl_files))
    specs = convert_stage(stl_files, spec_dir, tol, n_workers, StageCache(cache_dir, "convert", use_cache), use_cache)
    if _stop:
        log.warning("Interrupted: progress saved to %s. Re-run to continue.", cache_dir)
        return 130
    log.info("Stage 2/2 - Building .ipt files in Inventor")
    existing = [s for s in specs if os.path.exists(s)]
    ok, failures = build_stage(existing, ipt_dir, StageCache(cache_dir, "build", use_cache), use_cache)
    log.info("Done: %d ok, %d failed.", len(ok), len(failures))
    for n, e in failures:
        log.error(" - %s: %s", n, e)
    return 0 if not failures else 3


def summary_json(spec_dir):
    """Small helper: part name -> (features, IoU) for the specs in a folder."""
    out = {}
    for p in sorted(glob.glob(os.path.join(spec_dir, "*.json"))):
        s = json.load(open(p, encoding="utf-8"))
        out[Path(p).stem] = (len(s["features"]), s.get("notes", {}).get("iou"))
    return out
