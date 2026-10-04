"""Offline regression for the first-party source layout declared by Dockerfile."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_dockerfile_copies_importable_cache_package(tmp_path):
    stage = tmp_path / "app"
    stage.mkdir()
    copies = [line.split() for line in (ROOT / "Dockerfile").read_text().splitlines()
              if line.strip().startswith("COPY ")]
    assert copies, "Dockerfile has no simple COPY instructions"
    for copy in copies:
        assert len(copy) == 3 and copy[0] == "COPY", f"Unsupported COPY layout: {copy}"
        _, source, destination = copy
        assert (source, destination) in {
            ("requirements.txt", "."),
            ("cascade.py", "."),
            ("cascade_lib/", "cascade_lib/"),
        }, f"Unexpected COPY layout: {copy}"
        origin = ROOT / source
        assert origin.exists(), f"Missing Dockerfile COPY source: {source}"
        if origin.is_dir():
            shutil.copytree(origin, stage / destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(origin, stage / destination)

    env = {key: value for key, value in os.environ.items()
           if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "COMSPEC"}}
    result = subprocess.run(
        [sys.executable, "-I", "-c",
         ("import sys; sys.path.insert(0, sys.argv[1]); "
          "from cascade_lib.cache import ResponseCache; "
          "assert ResponseCache.__module__ == 'cascade_lib.cache'"),
         str(stage)],
        cwd=stage, env=env, capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == 0, f"Staged source package is not importable:\n{result.stderr}"
