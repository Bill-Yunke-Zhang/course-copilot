"""resolve_inside confines every repo-tool path to the repo root (T4)."""

import pytest

from app.core.tools import ToolRefused, repo_tools, resolve_inside


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text("print('hi')\n")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n")
    (tmp_path / "secret.txt").write_text("outside\n")
    return root


@pytest.mark.parametrize("relative", ["src/main.py", "src", "./src/main.py"])
def test_paths_inside_the_root_resolve(repo, relative):
    assert resolve_inside(repo, relative) == (repo / relative).resolve()


def test_empty_string_is_the_root(repo):
    assert resolve_inside(repo, "") == repo.resolve()


def test_dotdot_that_stays_inside_is_allowed(repo):
    assert (
        resolve_inside(repo, "src/../src/main.py") == (repo / "src/main.py").resolve()
    )


def test_missing_file_inside_the_root_still_resolves(repo):
    # The tools report "not a file" themselves; confinement only judges the path.
    assert resolve_inside(repo, "src/new.py") == (repo / "src/new.py").resolve()


@pytest.mark.parametrize("relative", ["/etc/passwd", "/", "/tmp/x"])
def test_absolute_paths_are_refused(repo, relative):
    with pytest.raises(ToolRefused, match="absolute"):
        resolve_inside(repo, relative)


@pytest.mark.parametrize(
    "relative", ["../secret.txt", "../../etc/passwd", "src/../../x", "src/../.."]
)
def test_climbing_out_is_refused(repo, relative):
    with pytest.raises(ToolRefused, match="escapes"):
        resolve_inside(repo, relative)


def test_symlink_leading_out_is_refused(repo, tmp_path):
    (repo / "link").symlink_to(tmp_path / "secret.txt")
    (repo / "dirlink").symlink_to(tmp_path)
    with pytest.raises(ToolRefused, match="escapes"):
        resolve_inside(repo, "link")
    with pytest.raises(ToolRefused, match="escapes"):
        resolve_inside(repo, "dirlink/secret.txt")


def test_symlink_staying_inside_is_allowed(repo):
    (repo / "alias.py").symlink_to(repo / "src" / "main.py")
    assert resolve_inside(repo, "alias.py") == (repo / "src/main.py").resolve()


@pytest.mark.parametrize(
    "relative",
    [".git", ".git/config", "src/../.git/config", ".GIT/config", "sub/.git/HEAD"],
)
def test_git_metadata_is_refused(repo, relative):
    with pytest.raises(ToolRefused, match=r"\.git"):
        resolve_inside(repo, relative)


def test_symlink_into_git_is_refused(repo):
    (repo / "meta").symlink_to(repo / ".git")
    with pytest.raises(ToolRefused, match=r"\.git"):
        resolve_inside(repo, "meta/config")


def test_names_that_only_contain_git_are_allowed(repo):
    (repo / ".gitignore").write_text("*.pyc\n")
    assert resolve_inside(repo, ".gitignore") == (repo / ".gitignore").resolve()


def test_nul_byte_is_refused(repo):
    with pytest.raises(ToolRefused):
        resolve_inside(repo, "src/main.py\x00.txt")


def test_repo_tools_refuse_escapes_instead_of_reading(repo):
    tools = {t.name: t for t in repo_tools(repo)}
    with pytest.raises(ToolRefused):
        tools["read_file"].handler({"path": "../secret.txt"})
    with pytest.raises(ToolRefused):
        tools["read_file"].handler({"path": ".git/config"})
    with pytest.raises(ToolRefused):
        tools["list_files"].handler({"path": "/"})
    assert (
        tools["read_file"].handler({"path": "src/main.py"})["content"]
        == "print('hi')\n"
    )
