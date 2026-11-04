"""Build the release artifact: ``dist/stl-to-ipt-<version>-windows-x64.zip`` plus ``SHA256SUMS.txt``.

The zip contains the wheel, ``requirements.txt``, the README and the example specs. The build is
deterministic and non-interactive: ``python scripts/package.py``.
"""
import hashlib
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
FIXED_TIME = (2020, 1, 1, 0, 0, 0)  # constant timestamps keep the zip reproducible


def read_version():
    text = (ROOT / "src" / "stl_to_ipt" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'__version__ = "([^"]+)"', text).group(1)


def sha256(path):
    h = hashlib.sha256()
    h.update(Path(path).read_bytes())
    return h.hexdigest()


def main():
    version = read_version()
    wheel_dir = DIST / "wheel"
    wheel_dir.mkdir(parents=True, exist_ok=True)
    subprocess.check_call([sys.executable, "-m", "pip", "wheel", str(ROOT), "--no-deps", "-w", str(wheel_dir), "-q"])
    wheels = sorted(wheel_dir.glob("stl_to_ipt-%s-*.whl" % version))
    if not wheels:
        raise SystemExit("wheel for version %s not found in %s" % (version, wheel_dir))

    name = "stl-to-ipt-%s-windows-x64" % version
    out = DIST / (name + ".zip")
    files = [(wheels[-1], wheels[-1].name), (ROOT / "requirements.txt", "requirements.txt"),
             (ROOT / "README.md", "README.md")]
    files += [(p, "examples/specs/" + p.name) for p in sorted((ROOT / "examples" / "specs").glob("*.json"))]
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for src, arc in files:
            info = zipfile.ZipInfo("%s/%s" % (name, arc), FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, Path(src).read_bytes())
    sums = DIST / "SHA256SUMS.txt"
    sums.write_text("%s  %s\n" % (sha256(out), out.name), encoding="utf-8")
    print("Built", out)
    print("Checksum file", sums)


if __name__ == "__main__":
    main()
