import hashlib
import os
import subprocess
from pathlib import Path


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout.rstrip("\r\n")


def snapshot(repo: Path | str, revision: str = "HEAD") -> tuple[str | None, str | None]:
    if not repo or not Path(repo).is_dir():
        return None, None
    result = subprocess.run(["git", "-C", str(repo), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if result.returncode or Path(result.stdout.strip()).resolve() != Path(repo).resolve():
        return None, None
    patch = git(
        Path(repo),
        "diff",
        "--no-ext-diff",
        "--binary",
        revision,
        "--",
        ".",
        ":(exclude)*.pyc",
        ":(exclude).pytest_cache",
    )
    files = git(Path(repo), "ls-files", "--others", "--exclude-standard").splitlines()
    current_patch = patch if revision == "HEAD" else git(Path(repo), "diff", "--no-ext-diff", "--binary", "HEAD")
    digest = hashlib.sha256((git(Path(repo), "rev-parse", "HEAD^{tree}") + current_patch).encode())
    for name in files:
        path = Path(repo) / name
        if path.is_file() and "__pycache__" not in path.parts and ".pytest_cache" not in path.parts:
            digest.update(name.encode() + b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
            added = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "diff",
                    "--no-index",
                    "--no-ext-diff",
                    "--binary",
                    "--",
                    "NUL" if os.name == "nt" else "/dev/null",
                    name,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            if added.returncode not in (0, 1):
                added.check_returncode()
            patch += "\n" + added.stdout
    return patch or None, digest.hexdigest()


def isolated_checkout(repo: Path, revision: str, destination: Path) -> Path:
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "clone", "--no-hardlinks", "--quiet", str(repo.resolve()), str(destination.resolve()))
    git(destination, "checkout", "--quiet", "--detach", revision)
    return destination.resolve()
