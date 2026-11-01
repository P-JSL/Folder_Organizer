"""Failure-path checks for the newly durable package evidence, no app launches."""
import hashlib
import json
from pathlib import Path

import pytest

from scripts import windows_python_bundle_v070 as bundle


def test_failed_later_check_keeps_capture_hash_and_records_failed_phase(tmp_path):
    screenshot = tmp_path / "PYTHON_PACKAGE_GUI.png"
    screenshot.write_bytes(b"already captured native image bytes")
    with pytest.raises(RuntimeError, match="GUI exit failed"):
        with bundle.PackageEvidence(tmp_path) as evidence:
            evidence.save(phase="native-gui", gui={"artifact": bundle.file_evidence(screenshot)})
            evidence.record["commands"].append({"name": "native-widgets-gui", "exit_code": 7})
            raise RuntimeError("GUI exit failed: 한글 오류")
    record = json.loads((tmp_path / "PYTHON_PACKAGE_CHECK.json").read_text(encoding="utf-8"))
    assert record["status"] == "failed" and record["phase"] == "native-gui"
    assert record["commands"][0]["exit_code"] == 7
    assert record["gui"]["artifact"]["sha256"] == hashlib.sha256(screenshot.read_bytes()).hexdigest()
    assert record["error"] == {"type": "RuntimeError", "message": "GUI exit failed: 한글 오류"}
    assert screenshot.read_bytes() == b"already captured native image bytes"


def test_interrupted_receipt_replacement_preserves_previous_complete_json(tmp_path, monkeypatch):
    evidence = bundle.PackageEvidence(tmp_path)
    evidence.save(phase="image-and-exact-cli", fixture_files=9)
    original = evidence.path.read_bytes()

    def interrupted(_source: Path, _target: Path):
        raise OSError("simulated rename interruption")

    monkeypatch.setattr(bundle.os, "replace", interrupted)
    with pytest.raises(OSError, match="rename interruption"):
        evidence.save(status="passed", phase="completed")
    assert evidence.path.read_bytes() == original
    assert json.loads(original)["status"] == "running"
    assert list(tmp_path.glob(".package-evidence-*.tmp")) == []
