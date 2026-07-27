# v0.1.0 release checklist

This checklist is completed after the release-evidence pull request is merged.
A tag must point to the exact verified `main` commit; do not tag a branch name
without recording its resolved SHA.

## Source and scope

- [ ] `main` contains the merged release-evidence pull request.
- [ ] `pyproject.toml` declares version `0.1.0`.
- [ ] `CHANGELOG.md` contains the `0.1.0` entry.
- [ ] No Chaos code, private paths, Stage assets, media, logs, caches, or
      `validation_output/` files are present.
- [ ] No submodule integration is included.

## Validation

- [ ] Linux / Python 3.11 CI succeeds.
- [ ] Linux / Python 3.12 CI succeeds.
- [ ] Windows / Python 3.12 CI succeeds.
- [ ] The wheel builds with `--no-deps --no-build-isolation` after the packaging toolchain is installed explicitly.
- [ ] The wheel installs into a clean temporary virtual environment.
- [ ] `examples/consumer_fixture.py` runs outside the repository source tree.
- [ ] The fixture records exactly `start -> resume` and completes successfully.
- [ ] Packaged JSON Schemas load from the installed wheel.

## Publication

- [ ] Record the verified `main` SHA below.
- [ ] Create annotated tag `v0.1.0` at that exact SHA.
- [ ] Create the GitHub Release from `CHANGELOG.md` without attaching private or
      generated validation artifacts.
- [ ] Confirm the tag and release resolve to the same commit.

Verified `main` SHA: `________________________`

## Consumer adoption boundary

Creating `v0.1.0` does not automatically authorize Chaos integration. Chaos may
pin the release only after a separate adapter/parity test proves its existing
foreground behavior is equivalent. Submodule addition remains a later decision.
