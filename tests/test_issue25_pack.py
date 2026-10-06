"""Exercise actual pack gates with invented files and a non-HA Python stand-in."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

PACK = Path(__file__).parents[1] / "tools/issue25/pack.sh"


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True, timeout=10).stdout.strip()


@pytest.fixture
def pack(tmp_path, request):
    root = tmp_path / "source"
    root.mkdir()
    for name in ("custom_components/invented/__init__.py", "custom_components/invented/executable", "tools/pytest_phases.py",
                 "tools/issue25/test_restore.py", "tools/issue25/conftest.py"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# invented control input\n")
    (root / "custom_components/invented/executable").chmod(0o755)
    git(root, "init", "-q")
    git(root, "add", ".")
    git(root, "-c", "user.name=Invented", "-c", "user.email=invented@example.invalid",
        "commit", "-qm", "invented pack control")
    commit, tree = git(root, "rev-parse", "HEAD"), git(root, "rev-parse", "HEAD^{tree}")
    target = tmp_path / "export"
    target.mkdir()
    mask = getattr(request, "param", "022")
    subprocess.run(["bash", "-euo", "pipefail", "-c",
                    'umask "$1"; git -C "$2" archive "$3" | tar -x -C "$4"',
                    "export", mask, str(root), commit, str(target)],
                   check=True, capture_output=True, text=True, timeout=10)
    # The same harness overlay as the parent runner, after a real archive export.
    (target / "tools/issue25").mkdir(parents=True, exist_ok=True)
    for name in ("tools/pytest_phases.py", "tools/issue25/test_restore.py", "tools/issue25/conftest.py"):
        shutil.copyfile(root / name, target / name)
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
    post-executable) chmod 644 custom_components/invented/executable ;;
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
                           str(inventory)], env=env, check=False, capture_output=True, text=True, timeout=15)


@pytest.mark.parametrize("pytest_exit", [0, 1])
@pytest.mark.parametrize("pack", ["022", "002"], indirect=True)
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
    "executable", "type", "equal-target", "relative-target", "empty-commit",
])
def test_failed_gate_stops_before_pytest(pack, fault):
    values = list(pack)
    root, target, commit, _tree, inventory, env = values
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
    elif fault == "empty-commit":
        values[2] = ""
    elif fault == "equal-target":
        values[1] = root
    elif fault == "relative-target":
        values[1] = Path("../export")
    elif fault == "content":
        product.write_text("changed\n")
    elif fault == "path":
        (product.parent / "extra.py").write_text("extra\n")
    elif fault == "missing-path":
        product.unlink()
    elif fault == "mode":
        product.chmod(0o755)
    elif fault == "executable":
        (target / "custom_components/invented/executable").chmod(0o644)
    elif fault == "type":
        product.unlink()
        product.mkdir()
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


@pytest.mark.parametrize("fault", ["post-content", "post-path", "post-mode", "post-harness", "post-inventory", "post-executable"])
def test_post_execution_gate_cannot_emit_pack_complete(pack, fault):
    pack[-1]["PACK_FAULT"] = fault
    result = invoke(pack)
    assert result.returncode != 0, result.stdout + result.stderr
    assert Path(pack[-1]["PACK_MARKER"]).exists()
    assert "PACK_EXIT=0" in result.stdout
    assert "PACK_COMPLETE=" not in result.stdout


def test_git_unrecorded_write_bit_is_not_drift(pack):
    product = pack[1] / "custom_components/invented/__init__.py"
    product.chmod(product.stat().st_mode ^ 0o020)
    result = invoke(pack)
    assert result.returncode == 0, result.stdout + result.stderr
    assert Path(pack[-1]["PACK_MARKER"]).exists()
    assert "PACK_COMPLETE=invented" in result.stdout


@pytest.mark.parametrize("fault", ["none", "red", "archive", "tar", "missing-complete", "unknown",
                                    "missing-phases", "wrong-exit", "duplicate-complete", "bad-exit"])
def test_parent_stops_before_next_base_on_export_or_marker_failure(tmp_path, fault):
    root = tmp_path / "source"
    (root / "tools/issue25").mkdir(parents=True)
    for name in ("tools/pytest_phases.py", "tools/issue25/test_restore.py", "tools/issue25/conftest.py"):
        (root / name).write_text("# invented\n")
    (root / "tools/issue25/pack.sh").write_text('''#!/bin/bash
echo "$3" >> "$PARENT_CALLS"
case "$PARENT_FAULT" in
  missing-complete) ;;
  duplicate-complete) echo "PACK_COMPLETE=$3"; echo "PACK_COMPLETE=$3" ;;
  *) echo "PACK_COMPLETE=$3" ;;
esac
test "$PARENT_FAULT" = missing-phases || echo PHASES_VALID=true
test "$PARENT_FAULT" != unknown || echo '{"category": "unknown"}'
code=0
test "$PARENT_FAULT" != red || code=1
test "$PARENT_FAULT" != bad-exit || code=7
if test "$PARENT_FAULT" = wrong-exit; then echo PACK_EXIT=1; else echo "PACK_EXIT=$code"; fi
exit "$code"
''')
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shims = {
        "git": '''#!/bin/bash
case "$1" in
 rev-parse) echo aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa ;;
 diff) exit 0 ;;
 archive) echo "$2" >> "$PARENT_EXPORTS"; test "$PARENT_FAULT" != archive ;;
 *) exit 9 ;;
esac
''',
        "tar": '#!/bin/bash\ncat >/dev/null\ntest "$PARENT_FAULT" != tar\n',
        "python": '#!/bin/bash\necho invented-inventory\n',
    }
    for name, source in shims.items():
        path = bin_dir / name
        path.write_text(source)
        path.chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", RUNNER_TEMP=str(tmp_path),
               GITHUB_EVENT_NAME="push", GITHUB_REF="refs/heads/ci/issue25-discriminator",
               GITHUB_RUN_ATTEMPT="1", GITHUB_SHA="a" * 40, REPOSITORY_PRIVATE="false",
               RUNNER_ENVIRONMENT="github-hosted", RUNNER_ARCH="X64", PARENT_FAULT=fault,
               PARENT_CALLS=str(tmp_path / "calls"), PARENT_EXPORTS=str(tmp_path / "exports"))
    result = subprocess.run(["bash", str(PACK.with_name("run.sh"))], cwd=root, env=env,
                            check=False, capture_output=True, text=True, timeout=15)
    calls = (tmp_path / "calls").read_text().splitlines() if (tmp_path / "calls").exists() else []
    exports = (tmp_path / "exports").read_text().splitlines()
    if fault in ("none", "red"):
        assert calls == ["released", "main"] and len(exports) == 2
        assert result.returncode == (0 if fault == "none" else 1)
    else:
        assert result.returncode != 0
        assert len(exports) == 1
        assert calls == ([] if fault in ("archive", "tar") else ["released"])
