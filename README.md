# stl-to-ipt

Convert STL meshes into **lean, editable, parametric Autodesk Inventor part files (`.ipt`)**.

Instead of a heap of layered extrusions, each part gets a short feature tree that a person would have modeled by
hand: a base profile on the natural plane, a few pads/recesses, through cuts on the other axes, and real Inventor
**Hole** features (countersink and counterbore). Every sketch is fully dimensioned, with named parameters you can
edit to resize the part.

- Sketches are lines, arcs and circles fitted to the mesh (tolerance 0.12 mm by default), not triangle soup.
  Arcs that flow into lines or into each other get **tangent constraints**, so a fillet or a lobe keeps its
  continuous curve when a dimension changes.
- Dimensions are **clean numbers**: measurement noise such as 5.015 becomes 5, and nothing carries more than two
  decimals (see [Dimension rounding](#dimension-rounding-and-fits)).
- Round holes that are not visible in the silhouette (blind holes across the extrusion axis) become real Inventor
  **Hole** features, and symmetric parts are built from one side plus a **Mirror** feature.
- Repeated geometry is **patterned**, not drawn copy by copy: rows of identical shapes become a rectangular pattern
  in the sketch (count and pitch are parameters, e.g. the 39 grooves of `Pedal_Main_Body`), and a ring of holes
  becomes one hole plus a circular pattern (e.g. the 8 holes of `Rotation_Bar`).
- A standard involute **spur gear** (`Yaw_Pot_Gear`: module 3, 10 teeth, 20 degrees) is built from its design
  parameters: a root cylinder, one tooth drawn with equation curves and a circular pattern. `module`, `teeth`,
  `pressure_angle`, `width`, the center and the phase are user parameters, so changing them regenerates the gear.
  (Inventor's Design Accelerator generator cannot be scripted: its add-in has no automation object.)
- The extrusion axis is chosen so the part looks like a prism; the sketch goes on XY when possible and is
  extruded along Z.
- Equal diameters share one parameter, rows of identical shapes are pattern copies driven by one `_pitch`
  parameter, and extrusion depths and plane offsets are named parameters too.
- Each result is checked against the source mesh (intersection over union on random points) and, in Inventor,
  by comparing the part volume with the mesh volume.

## Supported platforms

| Step | Platforms |
|---|---|
| STL -> spec conversion, validation, preview, tests | Windows, Linux, macOS (Python 3.10+) |
| Spec -> `.ipt` | Windows with Autodesk Inventor installed (COM API through `pywin32`) |

Release packages are built for **Windows x64**.

## Installation

Download the stable package from the **[Releases](../../releases)** page; you do not need to clone the repository.

1. Download `stl-to-ipt-<version>-windows-x64.zip` and check it against `SHA256SUMS.txt`:

   ```powershell
   (Get-FileHash .\stl-to-ipt-<version>-windows-x64.zip -Algorithm SHA256).Hash
   ```

2. Unzip it and install the wheel together with its dependencies:

   ```powershell
   cd stl-to-ipt-<version>-windows-x64
   python -m pip install .\stl_to_ipt-<version>-py3-none-any.whl
   ```

## Usage

```powershell
# Full pipeline: every STL in a folder -> specs -> .ipt files (needs Inventor)
stl-to-ipt run path\to\stl output

# Individual steps
stl-to-ipt convert part.stl specs                 # STL -> spec JSON (any OS)
stl-to-ipt verify part.stl specs\part.json        # IoU between mesh and spec
stl-to-ipt build specs ipt                        # specs -> .ipt (Windows + Inventor)
stl-to-ipt build specs ipt --validate-only        # validate specs without Inventor
stl-to-ipt preview specs\part.json part.png       # PNG of every sketch
```

`run` creates `<output>/specs` and `<output>/ipt`. Useful flags:

| Flag | Meaning |
|---|---|
| `--tol MM` | contour fitting tolerance (default `0.12`) |
| `--workers N`, `-j N` | parallel conversion workers (default: 75% of the CPU cores) |
| `--no-cache` | ignore checkpoints and reprocess everything |
| `--clean-cache` | delete the cache directory first |
| `--cache-dir DIR` | checkpoint directory (default `.cache`) |
| `--quiet`, `-q` | no logs and no progress bars |

Both stages are resumable: finished parts are recorded under `.cache/convert` and `.cache/build`, keyed by a
digest of the input file and the options. Press Ctrl+C once to stop after the parts that are running (nothing new
is started) and save progress; run the same command again to continue. Press Ctrl+C a second time to quit at once:
the workers are killed, parts they were working on are redone next time, and a part that Inventor is building may
be left open there. The exit code is 130 after an interruption.

The `examples/specs` folder holds the specs of twenty real parts, which `stl-to-ipt build` can turn into `.ipt`
files without any STL. See [docs/spec-format.md](docs/spec-format.md) for the format.

## Dimension rounding and fits

A mesh yields values like 25.859 or 8.986, but the part was designed with a caliper in mind. Every dimension is
snapped to the roundest number that is still within measurement noise: a multiple of 0.5 within 0.04 mm, else of
0.1 within 0.025 mm, else of 0.05 within 0.012 mm, else rounded to two decimals. Coordinates and arc centers that
agree within 0.06 mm become exactly equal.

Parts that must fit together are protected. The STL files of one project share a design frame, so the converter
finds **shaft/hole pairs** (coaxial, overlapping, hole at most 1.5 mm larger) and compares the clearance measured
on the mesh with the clearance after rounding. A change above 0.05 mm, or a clearance that closes, is reported and
repaired by resizing the hole. `stl-to-ipt run` does this automatically; run it by hand with:

```powershell
stl-to-ipt fits specs            # report only
stl-to-ipt fits specs --apply    # repair and rewrite the specs
```

Parts that were rotated before slicing are skipped, because their frame no longer matches the assembly.

## Development

```powershell
git clone <this repository>
cd stl-to-ipt
python -m pip install -e ".[dev]"
python -m pytest -q                     # runs everywhere; no Inventor required
python -m stl_to_ipt --quiet build examples/specs output/ipt --validate-only --no-cache
python scripts/package.py               # builds dist/stl-to-ipt-<version>-windows-x64.zip + SHA256SUMS.txt
```

`python -m pytest -q` is the verification command. The tests cover spec validation and expansion, the contour
fitting, hole detection, the plane mapping against a fake Inventor API, the batch cache and the CLI.
Notes on Inventor API behavior and pitfalls are in [docs/development-notes.md](docs/development-notes.md).

## Configuration and settings

The tool has no settings file and stores nothing per user. Behavior is controlled by command-line flags. The only
files it writes are the output folders you choose and the checkpoint directory (`.cache` by default).

## Updates

The tool does not update itself, because it is a command-line utility that drives a locally installed Inventor.
To update, download the newer package from the **Releases** page and install the new wheel over the old one
(`python -m pip install --upgrade <wheel>`). Release notes on each release list what changed.

## Supported languages

The command-line interface and all documentation are in English. User-facing text is not localized, so there is
no locale mechanism to extend.

## Versioning and releases

- Commits follow [Conventional Commits](https://www.conventionalcommits.org/): `fix:` bumps the patch version,
  `feat:` the minor version, and `feat!:` or a `BREAKING CHANGE:` footer the major version.
- [release-please](https://github.com/googleapis/release-please) opens a release pull request after changes reach
  `main`. Merging it creates the `vMAJOR.MINOR.PATCH` tag and the GitHub Release with generated notes, and the
  release workflow then attaches `stl-to-ipt-<version>-windows-x64.zip` and `SHA256SUMS.txt`.
- The single source of the version number is `src/stl_to_ipt/__init__.py`.

## License

No license has been chosen yet; all rights are reserved until one is added.
