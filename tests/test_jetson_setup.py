"""Exercise pinned Eigen setup with local Git fixtures, without CUDA or network."""

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "jetson_ai.sh"


def git(repository, *arguments):
    return subprocess.check_output(
        ["git", "-C", str(repository), *arguments], text=True
    ).strip()


def fixture_repository(tmp_path, *, headers=True):
    repository = tmp_path / "upstream"
    repository.mkdir()
    git(repository, "init", "--quiet")
    if headers:
        (repository / "Eigen").mkdir()
        (repository / "Eigen" / "Core").write_text("// fixture headers\n")
    else:
        (repository / "README").write_text("Missing headers\n")
    git(repository, "add", ".")
    git(
        repository,
        "-c", "user.name=Test",
        "-c", "user.email=test@example.invalid",
        "commit", "--quiet", "-m", "Fixture",
    )
    return repository, git(repository, "rev-parse", "HEAD")


def prepare(tmp_path, repository, revision):
    environment = {
        **os.environ,
        "JETSON_AI_DATA_DIR": str(tmp_path / "data"),
        "TEST_EIGEN_URL": str(repository),
        "TEST_EIGEN_REF": revision,
    }
    return subprocess.run(
        [
            "bash", "-c",
            'source "$1"; VIVI_EIGEN_URL="$TEST_EIGEN_URL"; '
            'VIVI_EIGEN_REF="$TEST_EIGEN_REF"; prepare_eigen',
            "test", str(SCRIPT),
        ],
        env=environment, capture_output=True, text=True, check=False,
    )


def test_eigen_fetch_pins_revision_and_reuses_checkout_offline(tmp_path):
    repository, revision = fixture_repository(tmp_path)
    result = prepare(tmp_path, repository, revision)
    assert result.returncode == 0, result.stderr
    checkout = tmp_path / "data" / "build" / "eigen"
    assert git(checkout, "rev-parse", "HEAD") == revision
    assert (checkout / "Eigen" / "Core").is_file()
    repository.rename(tmp_path / "unavailable")
    result = prepare(tmp_path, repository, revision)
    assert result.returncode == 0, result.stderr


def test_eigen_refuses_modified_cached_headers(tmp_path):
    repository, revision = fixture_repository(tmp_path)
    assert prepare(tmp_path, repository, revision).returncode == 0
    header = tmp_path / "data" / "build" / "eigen" / "Eigen" / "Core"
    header.write_text("// modified\n")
    result = prepare(tmp_path, repository, revision)
    assert result.returncode != 0
    assert "checkout was modified" in result.stderr
    assert header.read_text() == "// modified\n"


def test_eigen_refuses_revision_without_headers(tmp_path):
    repository, revision = fixture_repository(tmp_path, headers=False)
    result = prepare(tmp_path, repository, revision)
    assert result.returncode != 0
    assert "revision or headers do not match" in result.stderr
