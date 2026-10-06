"""Exercise actual pack gates with invented files and a non-HA Python stand-in."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest

PACK = Path(__file__).parents[1] / "tools/issue25/pack.sh"


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True, timeout=10).stdout.strip()


@pytest.fixture
def pack(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    for name in ("custom_components/invented/__init__.py", "tools/pytest_phases.py",
                 "tools/issue25/test_restore.py", "tools/issue25/conftest.py"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# invented control input\n")
    git(root, "init", "-q")
    git(root, "add", ".")
    git(root, "-c", "user.name=Invented", "-c", "user.email=invented@example.invalid",
        "commit", "-qm", "invented pack control")
    commit, tree = git(root, "rev-parse", "HEAD"), git(root, "rev-parse", "HEAD^{tree}")
    target = tmp_path / "export"
    shutil.copytree(root, target, ignore=shutil.ignore_patterns(".git"))
    inventory = tmp_path / "inventory"
    inventory.write_text("invented inventory\n")
    executable = tmp_path / "bin/python"
    executable.parent.mkdir()
    executable.write_text('''#!/usr/bin/env bash
set -eu
if test "$1" = -m; then
  echo entered > "$PACK_MARKER"
  case "${PACK_FAULT:-}" in
    post-content) echo changed >> custom_components/invented/__init__.py ;;
    post-path) touch custom_components/invented/extra.py ;;
    post-mode) chmod 755 custom_components/invented/__init__.py ;;
    post-harness) echo changed >> tools/issue25/test_restore.py ;;
  esac
  echo PHASES_VALID=true
  exit "${PACK_PYTEST_EXIT:-0}"
fi
case "${PACK_FAULT:-}" in
  inventory-failure) exit 7 ;;
  inventory-mismatch) echo changed; exit 0 ;;
  post-inventory)
    if test -f "$PACK_MARKER"; then echo changed; exit 0; fi ;;
esac
echo 'invented inventory'
''')
    executable.chmod(0o755)
    env = dict(os.environ, PATH=f"{executable.parent}:{os.environ['PATH']}",
               RUNNER_TEMP=str(tmp_path), PACK_MARKER=str(tmp_path / "pytest-entered"))
    return root, target, commit, tree, inventory, env


def invoke(pack):
    root, target, commit, tree, inventory, env = pack
    return subprocess.run(["bash", str(PACK), str(root), str(target), "invented", commit, tree,
                           str(inventory)], env=env, capture_output=True, text=True, timeout=15)


@pytest.mark.parametrize("pytest_exit", [0, 1])
def test_valid_pack_reaches_pytest_and_preserves_ordinary_red(pack, pytest_exit):
    pack[-1]["PACK_PYTEST_EXIT"] = str(pytest_exit)
    result = invoke(pack)
    assert result.returncode == pytest_exit, result.stdout + result.stderr
    assert Path(pack[-1]["PACK_MARKER"]).exists()
    assert f"PACK_EXIT={pytest_exit}" in result.stdout
    assert "PACK_COMPLETE=invented" in result.stdout


@pytest.mark.parametrize("fault", [
    "commit", "missing-commit", "tree", "target", "root", "content", "path", "missing-path",
    "mode", "symlink", "harness", "inventory-failure", "inventory-mismatch", "missing-inventory",
])
def test_failed_gate_stops_before_pytest(pack, fault):
    values = list(pack)
    root, target, commit, tree, inventory, env = values
    product = target / "custom_components/invented/__init__.py"
    if fault == "commit":
        values[2] = commit[:8]  # Resolvable but not the exact promised identity.
    elif fault == "missing-commit":
        values[2] = "0" * 40
    elif fault == "tree":
        values[3] = "0" * 40
    elif fault == "target":
        values[1] = target / "missing"
    elif fault == "root":
        values[0] = root / "missing"
    elif fault == "content":
        product.write_text("changed\n")
    elif fault == "path":
        (product.parent / "extra.py").write_text("extra\n")
    elif fault == "missing-path":
        product.unlink()
    elif fault == "mode":
        product.chmod(0o755)
    elif fault == "symlink":
        product.unlink()
        product.symlink_to(root / "custom_components/invented/__init__.py")
    elif fault == "harness":
        (target / "tools/issue25/test_restore.py").write_text("changed\n")
    elif fault == "missing-inventory":
        inventory.unlink()
    else:
        env["PACK_FAULT"] = fault
    result = invoke(values)
    assert result.returncode != 0, result.stdout + result.stderr
    assert not Path(env["PACK_MARKER"]).exists(), result.stdout
    assert "PACK_EXIT=" not in result.stdout
    assert "PACK_COMPLETE=" not in result.stdout


@pytest.mark.parametrize("fault", ["post-content", "post-path", "post-mode", "post-harness", "post-inventory"])
def test_post_execution_gate_cannot_emit_pack_complete(pack, fault):
    pack[-1]["PACK_FAULT"] = fault
    result = invoke(pack)
    assert result.returncode != 0, result.stdout + result.stderr
    assert Path(pack[-1]["PACK_MARKER"]).exists()
    assert "PACK_EXIT=0" in result.stdout
    assert "PACK_COMPLETE=" not in result.stdout
