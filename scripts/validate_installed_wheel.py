from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path


def _venv_python(venv_root: Path) -> Path:
    if os.name == "nt":
        return venv_root / "Scripts" / "python.exe"
    return venv_root / "bin" / "python"


def main() -> int:
    repository_root = Path(__file__).resolve().parents[1]
    wheels = sorted((repository_root / "dist").glob("production_harness-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"expected exactly one wheel, found {len(wheels)}")

    with tempfile.TemporaryDirectory(prefix="production-harness-wheel-") as raw:
        temporary_root = Path(raw)
        venv_root = temporary_root / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(venv_root)
        python = _venv_python(venv_root)

        clean_env = dict(os.environ)
        clean_env.pop("PYTHONPATH", None)
        clean_env["PYTHONNOUSERSITE"] = "1"

        subprocess.run(
            [str(python), "-m", "pip", "install", "--no-deps", str(wheels[0])],
            cwd=temporary_root,
            env=clean_env,
            check=True,
        )
        subprocess.run(
            [str(python), str(repository_root / "examples" / "consumer_fixture.py")],
            cwd=temporary_root,
            env=clean_env,
            check=True,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
