"""Command-line interface: ``stl-to-ipt <command>``."""
import argparse
import logging
import os

from . import __version__


def _setup_logging(quiet):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    if quiet:
        logging.disable(logging.CRITICAL)
        os.environ["TQDM_DISABLE"] = "1"


def _add_cache_flags(p):
    p.add_argument("--no-cache", action="store_true", help="ignore existing checkpoints and reprocess everything")
    p.add_argument("--clean-cache", action="store_true", help="delete the cache directory before starting")
    p.add_argument("--cache-dir", default=".cache", help="checkpoint directory (default: .cache)")


def build_parser():
    ap = argparse.ArgumentParser(prog="stl-to-ipt", description=__doc__)
    ap.add_argument("--version", action="version", version="%(prog)s " + __version__)
    ap.add_argument("--quiet", "-q", action="store_true", help="suppress logs and progress bars")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("convert", help="convert one STL file to a spec JSON")
    p.add_argument("stl")
    p.add_argument("out_dir")
    p.add_argument("--tol", type=float, default=0.12, help="contour fitting tolerance in mm (default: 0.12)")
    p.add_argument("--points", type=int, default=30000, help="random points used to measure IoU")

    p = sub.add_parser("verify", help="measure the IoU between an STL and a spec")
    p.add_argument("stl")
    p.add_argument("spec")
    p.add_argument("--points", type=int, default=30000)

    p = sub.add_parser("build", help="build .ipt files from a folder of specs (Windows + Inventor)")
    p.add_argument("spec_dir")
    p.add_argument("out_dir")
    p.add_argument("--validate-only", action="store_true", help="only validate the specs; do not open Inventor")
    _add_cache_flags(p)

    p = sub.add_parser("run", help="full pipeline: STL folder -> specs -> .ipt files")
    p.add_argument("stl_dir")
    p.add_argument("out_dir", help="creates <out_dir>/specs and <out_dir>/ipt")
    p.add_argument("--tol", type=float, default=0.12)
    p.add_argument("--workers", "-j", type=int, default=None,
                   help="parallel conversion workers (default: 75%% of the CPU cores)")
    p.add_argument("--skip", action="append", default=[], help="skip STL files whose name contains this text")
    _add_cache_flags(p)

    p = sub.add_parser("preview", help="render a PNG preview of a spec's sketches")
    p.add_argument("spec")
    p.add_argument("png")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    _setup_logging(args.quiet)
    log = logging.getLogger("stl_to_ipt")
    if args.command == "convert":
        from .converter import convert_file
        path, spec = convert_file(args.stl, args.out_dir, args.tol, args.points)
        log.info("%s | axis %s | features %d | IoU %s", path, spec["notes"]["extrusion_axis"],
                 len(spec["features"]), spec["notes"]["iou"])
        return 0
    if args.command == "verify":
        from . import spec as sp
        from .converter import iou, load_mesh
        m = load_mesh(args.stl)
        print("IoU = %.4f" % iou(m, sp.load(args.spec), [float(e) for e in m.extents], args.points))
        return 0
    if args.command == "preview":
        from .preview import render
        print("Preview saved:", render(args.spec, args.png))
        return 0
    from . import batch
    batch.prepare_cache(args.cache_dir, args.clean_cache)
    if args.command == "build":
        return batch.build_command(args.spec_dir, args.out_dir, args.cache_dir, not args.no_cache,
                                   args.validate_only)
    skip = tuple(args.skip) or ("Rudder_2021",)
    return batch.run_command(args.stl_dir, args.out_dir, args.tol, args.workers, args.cache_dir,
                             not args.no_cache, skip)
