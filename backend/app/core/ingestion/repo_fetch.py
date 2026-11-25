"""GitLab repo fetching — public repos work as-is; private repos use SecretStore.

A repo resource with no `credential_ref` is treated as public and cloned
anonymously — that works in the starter today. A private repo (the course's
lecture and team-project repos) carries a `credential_ref` and is fetched
with the PAT decrypted by `SecretStore.get`, which raises an actionable
`MissingCredential` when no token is stored. The rest of the pipeline (walking
files, per-file classification) is shared by both paths.

SECURITY: the token never goes in the clone URL. Git saves the remote URL
in the clone's .git/config, so a token there would sit on disk in plaintext.
It is passed to each git process as an HTTP header through environment
config instead (not argv, so it doesn't show in a process listing). Never
persist, print, or log the token.

SYMLINK POLICY (walk_repo): a cloned repo is untrusted content, and git
checks symlinks out as symlinks. A file named `notes.md` can therefore point
at anything the worker can read (`/etc/passwd`, the SecretStore key under
/data/secrets). Ingestion uses the same containment rule as the repo tools,
`tools.resolve_inside`, before reading a single byte:

- Every entry's path is resolved inside the clone. A link whose final target
  is outside the clone, or anywhere inside `.git/`, is refused. It is not
  listed, not read, not stored, and never reaches the classifier prompt.
- Broken links and link cycles are skipped. They have no readable target.
- Directory symlinks are never descended into (`os.walk(followlinks=False)`),
  so a linked tree outside the repo can't be enumerated.
- A link that stays inside the clone is kept, and its text is extracted from
  the validated, fully resolved target path, never by following the link
  again.
"""

import base64
import logging
import os
import subprocess
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.secret_store import SecretStore
from app.core.tools import ToolRefused, resolve_inside

logger = logging.getLogger(__name__)

# Files worth extracting text from when walking a repo.
DOC_SUFFIXES = {".md", ".txt", ".markdown", ".rst", ".pdf"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", "dist", ".venv", "venv"}


def _git_env(token: str | None) -> dict[str, str]:
    """Environment for a git process, carrying the PAT as an auth header.

    GitLab accepts HTTP Basic auth with user `oauth2` and the PAT as the
    password. GIT_CONFIG_* applies the header to this one process only;
    nothing is written to the clone's .git/config.
    """
    env = dict(os.environ)
    if token:
        basic = base64.b64encode(f"oauth2:{token}".encode()).decode()
        env.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
            }
        )
    return env


def local_clone_path(repo_url: str) -> Path:
    """Where clone_or_pull() keeps the local clone of `repo_url`."""
    return Path(get_settings().repo_clone_dir) / _slug(repo_url)


def clone_or_pull(db: Session, repo_url: str, credential_ref: str | None) -> Path:
    """Clone the repo (or pull if already cloned); returns the local path.

    `credential_ref=None` means a public repo — cloned anonymously, no
    SecretStore involved. With a credential_ref and no stored token, this
    raises SecretStore's MissingCredential (shown on the resource).
    """
    token = SecretStore(db).get(credential_ref) if credential_ref else None
    dest = local_clone_path(repo_url)
    if (dest / ".git").exists():
        command = ["git", "-C", str(dest), "pull", "--ff-only"]
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        command = ["git", "clone", repo_url, str(dest)]
    result = subprocess.run(
        command, capture_output=True, text=True, env=_git_env(token)
    )
    if result.returncode != 0:
        # stderr may echo the remote URL — scrub before surfacing.
        message = result.stderr.replace(token, "***") if token else result.stderr
        raise RuntimeError(f"git failed for {repo_url}: {message}")
    return dest


def _slug(repo_url: str) -> str:
    return urlparse(repo_url).path.strip("/").replace("/", "__").removesuffix(".git")


def walk_repo(repo_path: Path) -> list[dict]:
    """List the repo's taggable sub-items.

    Doc-like files get their text extracted for LLM classification; other
    files are listed with empty content and can be classified
    deterministically (in a project repo they default to `project-code`).

    Containment comes first (see SYMLINK POLICY above): an entry is listed,
    and its text extracted, only once `contained_file` has validated it.
    """
    from app.core.ingestion.extract import MAX_CONTENT_CHARS, extract_file

    root = Path(repo_path)
    items: list[dict] = []
    for rel in _repo_entries(root):
        target = contained_file(root, rel)
        if target is None:
            continue
        name = PurePosixPath(rel).name
        entry = {"path": rel, "title": name, "content": ""}
        if PurePosixPath(rel).suffix.lower() in DOC_SUFFIXES:
            try:
                entry["content"] = extract_file(str(target))[:MAX_CONTENT_CHARS]
            except Exception:
                pass
        items.append(entry)
    return items


def _repo_entries(root: Path) -> list[str]:
    """Relative paths of every non-directory entry (regular files and
    symlinks of any kind) under `root`, in stable path order. Never follows a
    directory symlink and never enters a SKIP_DIRS directory."""
    found: list[PurePosixPath] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        # os.walk lists directory symlinks in dirnames but (with
        # followlinks=False) never enters them; they're dropped either way.
        dirnames[:] = [
            d for d in dirnames if d not in SKIP_DIRS and not (here / d).is_symlink()
        ]
        for name in filenames:
            found.append(PurePosixPath((here / name).relative_to(root).as_posix()))
    return [str(p) for p in sorted(found)]


def contained_file(root: Path, rel: str) -> Path | None:
    """The validated, fully resolved file `rel` names inside `root`, or None.

    None means the entry must not be ingested: its target escapes the clone
    or lands in `.git/` (resolve_inside refuses it), the link is broken or
    cyclic, or the target isn't a regular file.
    """
    try:
        target = resolve_inside(root, rel)
    except ToolRefused as exc:
        logger.warning("repo ingestion skipped %r: %s", rel, exc)
        return None
    except (OSError, RuntimeError) as exc:  # symlink loop (RuntimeError < 3.13)
        logger.warning("repo ingestion skipped %r: unresolvable link (%s)", rel, exc)
        return None
    try:
        if not target.is_file() or target.is_symlink():
            # Missing target (broken link), a directory, or a device etc.
            return None
    except OSError:
        return None
    return target


def repo_git_evidence(repo_path: Path) -> dict:
    """Structured git metadata for `project-code` items (the evidence
    source_refs carry instead of a prose excerpt)."""

    def _git(*args: str) -> str:
        out = subprocess.run(
            ["git", "-C", str(repo_path), *args], capture_output=True, text=True
        )
        return out.stdout.strip()

    return {
        "head_sha": _git("rev-parse", "HEAD"),
        "head_date": _git("log", "-1", "--format=%cI"),
        "commit_count": _git("rev-list", "--count", "HEAD"),
        "recent_commits": _git("log", "-5", "--format=%h %cI %s").splitlines(),
    }
