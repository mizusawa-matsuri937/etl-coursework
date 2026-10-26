import gzip
import hashlib
import json

import pytest

from etl_task1.source import SourceError, load_manifest, open_gzip, verify_source

import build_fixtures as bf


def make_source(tmp_path, payload=b"hello\n"):
    gz = tmp_path / "x.gz"
    gz.write_bytes(gzip.compress(payload, mtime=0))
    manifest = tmp_path / "x.json"
    bf.write_manifest(manifest, gz.read_bytes())
    return gz, manifest


def test_correct_input_has_measured_values(tmp_path):
    gz, manifest = make_source(tmp_path)
    v = verify_source(gz, manifest, clock=lambda: "2026-01-01T00:00:00+00:00")
    assert v.measured_size_bytes == v.recorded_size_bytes == gz.stat().st_size
    assert v.measured_sha256 == hashlib.sha256(gz.read_bytes()).hexdigest() == v.source_file_id
    assert v.verified_at == "2026-01-01T00:00:00+00:00"
    assert v.download_completed_at is None  # 未知时保持未知，不用验证时间代替
    assert v.sha256_is_official is False


def test_download_and_verify_times_kept_apart(tmp_path):
    gz, manifest = make_source(tmp_path)
    data = json.loads(manifest.read_text())
    data["download_completed_at"] = "2026-10-21T10:25:40+08:00"
    manifest.write_text(json.dumps(data))
    v = verify_source(gz, manifest, clock=lambda: "2030-01-01T00:00:00+00:00")
    assert v.download_completed_at == "2026-10-21T10:25:40+08:00"
    assert v.verified_at != v.download_completed_at


def test_hash_mismatch(tmp_path):
    gz, manifest = make_source(tmp_path)
    data = json.loads(manifest.read_text())
    data["sha256"] = "0" * 64
    manifest.write_text(json.dumps(data))
    with pytest.raises(SourceError) as exc:
        verify_source(gz, manifest)
    assert exc.value.code == "hash_mismatch"


def test_size_mismatch(tmp_path):
    gz, manifest = make_source(tmp_path)
    data = json.loads(manifest.read_text())
    data["size_bytes"] += 1
    manifest.write_text(json.dumps(data))
    with pytest.raises(SourceError) as exc:
        verify_source(gz, manifest)
    assert exc.value.code == "size_mismatch"


def test_missing_inputs(tmp_path):
    gz, manifest = make_source(tmp_path)
    with pytest.raises(SourceError) as exc:
        verify_source(tmp_path / "nope.gz", manifest)
    assert exc.value.code == "input_missing"
    with pytest.raises(SourceError) as exc:
        verify_source(gz, tmp_path / "nope.json")
    assert exc.value.code == "input_missing"


@pytest.mark.parametrize("drop", ["source_url", "size_bytes", "sha256"])
def test_missing_required_metadata(tmp_path, drop):
    gz, manifest = make_source(tmp_path)
    data = json.loads(manifest.read_text())
    del data[drop]
    manifest.write_text(json.dumps(data))
    with pytest.raises(SourceError) as exc:
        verify_source(gz, manifest)
    assert exc.value.code == "manifest_invalid" and drop in str(exc.value)


@pytest.mark.parametrize("text", ["not json", "[]"])
def test_unparseable_manifest(tmp_path, text):
    p = tmp_path / "m.json"
    p.write_text(text)
    with pytest.raises(SourceError) as exc:
        load_manifest(p)
    assert exc.value.code == "manifest_invalid"


def test_truncated_gzip_raises_on_read(tmp_path):
    gz = tmp_path / "t.gz"
    gz.write_bytes(gzip.compress(b"x" * 100_000, mtime=0)[:-20])
    with pytest.raises(EOFError), open_gzip(gz) as fh:
        while fh.read(1 << 16):
            pass
