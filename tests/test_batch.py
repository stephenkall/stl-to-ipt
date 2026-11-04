from pathlib import Path

import pytest

from stl_to_ipt import batch
from stl_to_ipt.cli import build_parser, main


def test_worker_count_defaults_to_three_quarters_of_cores(monkeypatch):
    monkeypatch.setattr(batch.os, "cpu_count", lambda: 8)
    assert batch.worker_count() == 6
    assert batch.worker_count(2) == 2
    assert batch.worker_count(64) == 8  # never more than the available cores


def test_worker_count_is_at_least_one(monkeypatch):
    monkeypatch.setattr(batch.os, "cpu_count", lambda: 1)
    assert batch.worker_count() == 1


def test_digest_changes_with_content_and_options(tmp_path):
    f = tmp_path / "a.stl"
    f.write_bytes(b"solid a")
    d1 = batch.file_digest(str(f), "tol=0.12")
    assert d1 == batch.file_digest(str(f), "tol=0.12")
    assert d1 != batch.file_digest(str(f), "tol=0.2")
    f.write_bytes(b"solid b")
    assert d1 != batch.file_digest(str(f), "tol=0.12")


def test_stage_cache_roundtrip(tmp_path):
    cache = batch.StageCache(tmp_path, "convert")
    assert not cache.is_done("part", "abc")
    cache.mark_done("part", "abc")
    assert cache.is_done("part", "abc")
    assert not cache.is_done("part", "different-digest")


def test_disabled_cache_never_reports_done(tmp_path):
    batch.StageCache(tmp_path, "convert").mark_done("part", "abc")
    assert not batch.StageCache(tmp_path, "convert", enabled=False).is_done("part", "abc")


def test_clean_cache_removes_directory(tmp_path):
    cache_dir = tmp_path / ".cache"
    batch.StageCache(cache_dir, "convert").mark_done("part", "abc")
    batch.prepare_cache(str(cache_dir), clean=True)
    assert not Path(cache_dir).exists()


def test_cli_exposes_all_commands_and_flags():
    parser = build_parser()
    args = parser.parse_args(["run", "stl", "out", "-j", "3", "--no-cache", "--tol", "0.2"])
    assert (args.workers, args.no_cache, args.tol) == (3, True, 0.2)
    assert parser.parse_args(["build", "specs", "ipt", "--validate-only"]).validate_only is True
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_build_validate_only_runs_without_inventor(tmp_path):
    examples = Path(__file__).parent.parent / "examples" / "specs"
    code = main(["--quiet", "build", str(examples), str(tmp_path / "ipt"), "--validate-only",
                 "--cache-dir", str(tmp_path / ".cache")])
    assert code == 0


def test_run_reports_missing_stl_folder(tmp_path):
    code = main(["--quiet", "run", str(tmp_path / "nothing"), str(tmp_path / "out"),
                 "--cache-dir", str(tmp_path / ".cache")])
    assert code == 1


def test_convert_stage_runs_in_parallel_and_resumes_from_cache(tmp_path, caplog):
    trimesh = pytest.importorskip("trimesh")
    stls = []
    for name, size in (("plate_a", (20, 20, 4)), ("plate_b", (30, 10, 3))):
        path = tmp_path / (name + ".stl")
        trimesh.creation.box(extents=size).export(path)
        stls.append(str(path))
    spec_dir = tmp_path / "specs"
    cache = batch.StageCache(tmp_path / ".cache", "convert")
    specs = batch.convert_stage(stls, str(spec_dir), 0.12, 2, cache, True)
    assert all(Path(s).exists() for s in specs)
    first_write = {s: Path(s).stat().st_mtime_ns for s in specs}
    # second run: every item is in the cache, so nothing is rewritten
    batch.convert_stage(stls, str(spec_dir), 0.12, 2, cache, True)
    assert first_write == {s: Path(s).stat().st_mtime_ns for s in specs}
    # --no-cache reprocesses everything
    batch.convert_stage(stls, str(spec_dir), 0.12, 2, cache, False)
    assert any(first_write[s] != Path(s).stat().st_mtime_ns for s in specs)
