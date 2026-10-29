"""A stand-in for the GitHub API, answering for this checkout and for a source repository.

    uv run python tools/fake_api.py
    uv run python tools/fake_api.py --clone ../g0v0-server
    GITHUB_API_URL=http://127.0.0.1:8766 uv run python tools/newsctl.py draft \
        --repository GooGuTeam/g0v0-server --release 2026.1030.0-g0v0

Two readers around this repository talk to GitHub, and neither can be exercised without it:
the tools read a release and a range of commits, and the server lists a directory and then
fetches each file. One script answers both shapes from what is already on this disk -- the
checkout for `contents`, a clone or a document written down for `releases` and `compare` --
so a draft can be written without a token, and a checkout can be looked at before it is
pushed.

Four routes, and nothing else:

| Route | Answered from |
| --- | --- |
| `GET /repos/{owner}/{name}/contents/{path}` | `--root`, the checkout |
| `GET /{owner}/{name}/{ref}/{path}` | the same file, as bytes; `{ref}` is not read |
| `GET /repos/{owner}/{name}/releases/tags/{tag}` | `--clone`, or `--fixture`, or the demo |
| `GET /repos/{owner}/{name}/compare/{base}...{head}` | the same |

A clone is read when the request arrives rather than walked at startup, so any range a
person types works, and a tag nobody tagged is the 404 it would be on the real API. The
demo is enough to run the whole draft flow with no clone, no token and no network.

This is not GitHub: there is no authentication, no pagination and no write, and a route
outside the table answers 404. That is the point -- a tag that was never published should
fail here the way it fails there, or a draft that "worked locally" is a surprise in CI.
"""

import argparse
from base64 import b64encode
from dataclasses import dataclass
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import sys
from typing import Any, cast
from urllib.parse import unquote, urlparse

from content import ContentError
import sources

ROOT = Path(__file__).resolve().parent.parent
PORT = 8766

# What a checkout has and a repository does not: caches, editor state, the virtual
# environment. A listing that showed them would be a listing of this machine.
LOCAL = frozenset({".git", ".venv", "__pycache__", ".ruff_cache", ".idea", ".vscode", ".workbuddy"})

# The API inlines a file's bytes up to this size and leaves larger ones to the raw route,
# which is a shape a consumer gets to survive rather than a rule worth improving on.
INLINE = 1024 * 1024

_CONTENTS = re.compile(r"^/repos/[^/]+/[^/]+/contents(?:/(?P<name>.*))?$")


# -- the checkout, as the contents API describes it -----------------------------------


def blob_sha(data: bytes) -> str:
    """Git's own id for a blob, so that a file's `sha` is the one `git hash-object` gives.

    The server caches by this id, so a made-up one would look like a change on every
    request. SHA-1 is not a security choice here: it is the format Git writes.
    """
    return hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()  # noqa: S324


def _tree_sha(path: Path) -> str:
    """An id for a listing, made of what it holds.

    Git's tree object would need the index; a consumer only uses this as a cache key, so it
    has to change when the directory does, and nothing else.
    """
    digest = hashlib.sha1()  # noqa: S324
    for child in sorted(path.iterdir()):
        if child.name in LOCAL:
            continue
        digest.update(f"{child.name}\x00{_entry_sha(child)}\n".encode())
    return digest.hexdigest()


def _entry_sha(path: Path) -> str:
    return _tree_sha(path) if path.is_dir() else blob_sha(path.read_bytes())


@dataclass
class Snapshot:
    """A checkout, described the way the contents API describes a repository.

    `base` is where this server answers, because a listing carries links back to itself: a
    consumer that follows `download_url` has to land here rather than on GitHub. `root` is
    resolved once, so that every path below is compared against it the same way.
    """

    root: Path
    repository: str
    base: str

    def __post_init__(self) -> None:
        self.root = self.root.resolve()

    def contents(self, name: str) -> Any:
        """A listing, or one file, or nothing when the path is not in this checkout.

        `..` is resolved before the check, so a path that climbs out of the checkout
        answers nothing rather than reading the machine -- the rule the console applies to
        the paths a request names, applied to the paths a repository name implies.
        """
        path = (self.root / name).resolve()
        if not path.is_relative_to(self.root) or not path.exists():
            return None
        if path.is_dir():
            return [self.entry(child) for child in sorted(path.iterdir()) if child.name not in LOCAL]
        return self.entry(path)

    def raw(self, name: str) -> bytes | None:
        """The bytes of one file, which is what a raw base URL is asked for."""
        path = (self.root / name).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            return None
        return path.read_bytes()

    def entry(self, path: Path) -> dict[str, Any]:
        relative = path.relative_to(self.root).as_posix()
        if path.is_dir():
            digest = _tree_sha(path)
            return {
                "type": "dir",
                "size": 0,
                "name": path.name,
                "path": relative,
                "sha": digest,
                "url": f"{self.base}/repos/{self.repository}/contents/{relative}",
                "html_url": f"https://github.com/{self.repository}/tree/HEAD/{relative}",
                "git_url": f"{self.base}/repos/{self.repository}/git/trees/{digest}",
                "download_url": None,
            }

        data = path.read_bytes()
        digest = blob_sha(data)
        inline = len(data) <= INLINE
        return {
            "type": "file",
            "encoding": "base64" if inline else "none",
            "size": len(data),
            "name": path.name,
            "path": relative,
            "sha": digest,
            "content": b64encode(data).decode("ascii") if inline else "",
            "url": f"{self.base}/repos/{self.repository}/contents/{relative}",
            "html_url": f"https://github.com/{self.repository}/blob/HEAD/{relative}",
            "git_url": f"{self.base}/repos/{self.repository}/git/blobs/{digest}",
            "download_url": f"{self.base}/{self.repository}/HEAD/{relative}",
        }


# -- the documents the release and compare routes answer -----------------------------

DEMO_REPOSITORY = "GooGuTeam/g0v0-server"
DEMO_TAG = "2026.1030.0-g0v0"
DEMO_BASE = "2026.1009.0-g0v0"
DEMO_PUBLISHED = "2026-10-30T12:00:00Z"

# GitHub's generated template with the awkward cases left in: a breaking change written as
# a commit, a section that is about the release rather than about a change, and a bullet
# with no prefix at all. A demo that only had the easy case would let a change to the
# reader pass here and fail on the first real release.
DEMO_BODY = """\
## What's Changed

### Features
* feat(spectator): keep a room open when the host leaves by @CloneWith in https://github.com/GooGuTeam/g0v0-server/pull/512
* Beatmap downloads resume after a dropped connection by @MoeMachine in https://github.com/GooGuTeam/g0v0-server/pull/509

### Breaking changes
* feat(api)!: drop the `/api/v1` prefix by @CloneWith in https://github.com/GooGuTeam/g0v0-server/pull/520

## Bug Fixes
* fix(api): the changelog no longer 500s on a build with no `users` by @CloneWith in https://github.com/GooGuTeam/g0v0-server/pull/514

## New Contributors
* @newbie made their first contribution in https://github.com/GooGuTeam/g0v0-server/pull/511

**Full Changelog**: https://github.com/GooGuTeam/g0v0-server/compare/2026.1009.0-g0v0...2026.1030.0-g0v0
"""

# (sha, subject, body, author, login), in the order they landed.
DEMO_COMMITS: tuple[tuple[str, str, str, str, str | None], ...] = (
    (
        "4f1c2a7e9b3d5c6a8e0f1b2c3d4e5f6a7b8c9d01",
        "feat(spectator): keep a room open when the host leaves (#512)",
        "The room is closed by the last player, not by the host.",
        "CloneWith",
        "CloneWith",
    ),
    (
        "4f1c2a7e9b3d5c6a8e0f1b2c3d4e5f6a7b8c9d02",
        "fix(api): stop the changelog answering 500 for a build with no users (#514)",
        "",
        "CloneWith",
        "CloneWith",
    ),
    (
        "4f1c2a7e9b3d5c6a8e0f1b2c3d4e5f6a7b8c9d03",
        "chore(deps): bump jsonschema",
        "",
        "MoeMachine",
        "MoeMachine",
    ),
    (
        "4f1c2a7e9b3d5c6a8e0f1b2c3d4e5f6a7b8c9d04",
        "Merge pull request #514 from GooGuTeam/fix-changelog",
        "",
        "CloneWith",
        "CloneWith",
    ),
    (
        "4f1c2a7e9b3d5c6a8e0f1b2c3d4e5f6a7b8c9d05",
        "feat(api)!: drop the `/api/v1` prefix (#520)",
        "BREAKING CHANGE: clients have to move to /api/v2.",
        "CloneWith",
        "CloneWith",
    ),
)


def _release_path(repository: str, tag: str) -> str:
    return f"/repos/{repository}/releases/tags/{tag}"


def _compare_path(repository: str, base: str, head: str) -> str:
    return f"/repos/{repository}/compare/{base}...{head}"


def _commit_document(
    repository: str, *, sha: str, message: str, name: str, login: str | None, date: str | None = None
) -> dict[str, Any]:
    """One commit, in the shape the compare route reports it."""
    return {
        "sha": sha,
        "html_url": f"https://github.com/{repository}/commit/{sha}",
        "commit": {"message": message, "author": {"name": name, "email": "", "date": date}},
        "author": {"login": login} if login else None,
    }


def _compare_document(repository: str, base: str, head: str, commits: list[dict[str, Any]]) -> dict[str, Any]:
    """A comparison: the commits between two points, oldest first."""
    return {
        "url": f"https://api.github.com/repos/{repository}/compare/{base}...{head}",
        "html_url": f"https://github.com/{repository}/compare/{base}...{head}",
        "status": "ahead",
        "ahead_by": len(commits),
        "behind_by": 0,
        "total_commits": len(commits),
        "commits": commits,
    }


def demo() -> dict[str, Any]:
    """A release and a range, as this file wrote them.

    Enough to run the whole draft flow with nothing on the disk and nothing on the network;
    it is also the answer to "what does `--release` actually read?".
    """
    release = {
        "url": f"https://api.github.com/repos/{DEMO_REPOSITORY}/releases/1",
        "html_url": f"https://github.com/{DEMO_REPOSITORY}/releases/tag/{DEMO_TAG}",
        "tag_name": DEMO_TAG,
        "name": DEMO_TAG,
        "draft": False,
        "prerelease": False,
        "created_at": DEMO_PUBLISHED,
        "published_at": DEMO_PUBLISHED,
        "body": DEMO_BODY,
        "author": {"login": "CloneWith", "html_url": "https://github.com/CloneWith"},
    }
    commits = [
        _commit_document(
            DEMO_REPOSITORY,
            sha=sha,
            message="\n\n".join(part for part in (subject, body) if part),
            name=author,
            login=login,
        )
        for sha, subject, body, author, login in DEMO_COMMITS
    ]
    return {
        _release_path(DEMO_REPOSITORY, DEMO_TAG): release,
        _compare_path(DEMO_REPOSITORY, DEMO_BASE, "HEAD"): _compare_document(
            DEMO_REPOSITORY, DEMO_BASE, "HEAD", commits
        ),
    }


def _body_from(commits: list[sources.Commit]) -> str:
    """A generated body: the list GitHub writes, with the subjects as they were committed.

    The subjects keep their conventional-commit prefixes on purpose, since that is what the
    reader uses to decide a type and a category. A body that had already digested them
    would answer the question the draft exists to ask.
    """
    if not commits:
        return "## What's Changed\n\nNothing landed between the previous tag and this one.\n"
    return "## What's Changed\n\n" + "\n".join(f"* {commit.subject}" for commit in commits) + "\n"


def _commit_from(repository: str, commit: sources.Commit) -> dict[str, Any]:
    message = "\n\n".join(part for part in (commit.subject, commit.body) if part)
    return _commit_document(
        repository, sha=commit.sha, message=message, name=commit.author, login=commit.login, date=commit.date
    )


def release_from_clone(clone: Path, repository: str, tag: str) -> dict[str, Any]:
    """A release built from a tag's own commits: what `--release` reads, minus the prose.

    Nobody wrote these notes, so the body is the commit list and no bullet carries a pull
    request. That is the honest outcome rather than a limitation of this file.
    """
    found = next((item for item in sources.tags(clone) if item.name == tag), None)
    if found is None:
        raise ContentError(f"{clone} has no tag {tag}")
    commits = sources.local_commits(clone, tag)
    return {
        "url": f"https://api.github.com/repos/{repository}/releases/tags/{tag}",
        "html_url": f"https://github.com/{repository}/releases/tag/{tag}",
        "tag_name": tag,
        "name": tag,
        "draft": False,
        "prerelease": False,
        "created_at": found.date,
        "published_at": found.date,
        "body": _body_from(commits),
    }


def compare_from_clone(clone: Path, repository: str, base: str, head: str) -> dict[str, Any]:
    """The commits in a range, read out of a clone: any range, not only the ones between tags."""
    commits = sources.local_commits(clone, f"{base}..{head}")
    return _compare_document(repository, base, head, [_commit_from(repository, commit) for commit in commits])


def load_fixture(path: Path) -> dict[str, Any]:
    """A world written down: a JSON object of path to document.

    Keys are the paths the tools ask for, so a response copied out of `gh api` can be
    pasted in as it is, and a case that is awkward to produce with a clone -- an empty
    body, a build with no `users`, the API's 250-commit limit -- is written by hand.
    """
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"{path} holds {type(document).__name__}, not an object of path to document")
    return {str(key): value for key, value in document.items()}


class World:
    """What the document routes answer: written down, or read out of a clone when asked.

    Reading on demand is what makes `--range <any tag>..HEAD` work without enumerating
    every range up front: the clone is the source of truth, and a range it cannot resolve
    is the 404 the API would give. A document in hand wins over the clone, so a fixture can
    describe the awkward case and leave the rest to Git.
    """

    def __init__(
        self,
        *,
        repository: str,
        documents: dict[str, Any] | None = None,
        clone: Path | None = None,
    ) -> None:
        self.repository = repository
        self.documents: dict[str, Any] = dict(documents or {})
        self.clone = clone

    def lookup(self, path: str) -> Any:
        document = self.documents.get(path)
        if document is not None:
            return document
        clone = self.clone
        if clone is None:
            return None
        found = self._read(clone, path)
        if found is not None:
            self.documents[path] = found
        return found

    def _read(self, clone: Path, path: str) -> Any:
        prefix = f"/repos/{self.repository}/"
        if not path.startswith(prefix):
            return None
        route = path[len(prefix) :]
        try:
            if route.startswith("releases/tags/"):
                return release_from_clone(clone, self.repository, route[len("releases/tags/") :])
            if route.startswith("compare/"):
                base, _, head = route[len("compare/") :].partition("...")
                if base and head:
                    return compare_from_clone(clone, self.repository, base, head)
        except ContentError:
            # A tag or a revision the clone does not have: the answer is the same 404 the
            # API gives for one it does not have either.
            return None
        return None

    def releases(self) -> list[str]:
        """The tags a clone could answer for, for the index and for a person reading it."""
        if self.clone is None:
            return []
        try:
            return [tag.name for tag in sources.tags(self.clone)]
        except ContentError:
            return []


# -- the server ----------------------------------------------------------------------


class FakeAPI(ThreadingHTTPServer):
    """A server that answers GitHub's shapes from a checkout and a world of documents."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], *, root: Path, world: World) -> None:
        super().__init__(address, Handler)
        self.root = root.resolve()
        self.world = world

    @property
    def base(self) -> str:
        """Where this server is, as a URL a request could be sent to."""
        return f"http://{self.server_address[0]}:{self.server_address[1]}"

    def snapshot(self) -> Snapshot:
        return Snapshot(root=self.root, repository=self.world.repository, base=self.base)


class Handler(BaseHTTPRequestHandler):
    """The four routes, and GitHub's 404 for everything else."""

    server_version = "g0v0-news-fake-api"
    protocol_version = "HTTP/1.1"

    @property
    def api(self) -> FakeAPI:
        # `BaseHTTPRequestHandler` types the server as `BaseServer`; the only one it is ever
        # given here is the subclass above, which is what holds the world.
        return cast(FakeAPI, self.server)

    def do_GET(self) -> None:
        path = unquote(urlparse(self.path).path)
        if path in ("", "/"):
            return self._json(self._index())
        if path.startswith("/repos/"):
            document = self.api.world.lookup(path)
            if document is not None:
                return self._json(document)
            if _CONTENTS.match(path) is not None:
                return self._contents(path)
            return self._not_found(path)
        return self._raw(path)

    def _contents(self, path: str) -> None:
        matched = _CONTENTS.match(path)
        name = (matched.group("name") if matched is not None else None) or ""
        answer = self.api.snapshot().contents(name)
        if answer is None:
            return self._not_found(path)
        self._json(answer)

    def _raw(self, path: str) -> None:
        parts = path.strip("/").split("/")
        if len(parts) < 4:
            return self._not_found(path)
        data = self.api.snapshot().raw("/".join(parts[3:]))
        if data is None:
            return self._not_found(path)
        self._body(data, "text/plain; charset=utf-8")

    def _index(self) -> dict[str, Any]:
        """What this server can answer, and the variables that point a reader at it."""
        return {
            "fake_api": self.api.base,
            "repository": self.api.world.repository,
            "root": self.api.root.as_posix(),
            "contents": f"{self.api.base}/repos/{self.api.world.repository}/contents/",
            "documents": sorted(self.api.world.documents),
            "releases": self.api.world.releases(),
            "environment": {
                "GITHUB_API_URL": self.api.base,
                "CONTENT_LISTING_BASE_URL": self.api.base,
                "CONTENT_RAW_BASE_URL": self.api.base,
                "CONTENT_REPOSITORY": self.api.world.repository,
            },
        }

    def _not_found(self, path: str) -> None:
        self._json(
            {"message": "Not Found", "path": path, "documentation_url": "https://docs.github.com/rest"},
            status=404,
        )

    def _json(self, payload: Any, *, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._body(body, "application/vnd.github+json; charset=utf-8", status=status)

    def _body(self, body: bytes, content_type: str, *, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if status != 200:
            # A refused request may not have had its body read, and what is left of it would
            # be parsed as the next request on a kept-alive connection.
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    # The parameter names are the base class's, which is what makes this an override rather
    # than a method that happens to be named the same; `format` is unused on purpose.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: ARG002
        """One line per request, so that a draft can be traced to what it read."""
        print(f"{self.command} {unquote(self.path)} -> {args[1]}")


def run(*, root: Path, world: World, host: str = "127.0.0.1", port: int = PORT) -> int:
    """Serve until interrupted."""
    server = FakeAPI((host, port), root=root, world=world)
    address = server.base
    # Flushed one by one: these lines are read to find the port, so they have to be there
    # before the first request rather than when the buffer fills up.
    print(f"fake api on {address}/", flush=True)
    print(f"  contents of {server.root} as {world.repository}", flush=True)
    for path in sorted(world.documents)[:4]:
        print(f"  document {path}", flush=True)
    releases = world.releases()
    if releases:
        print(f"  {len(releases)} tag(s) from {world.clone}, each a release and a range", flush=True)
    print(f"  GITHUB_API_URL={address} for `newsctl draft --release`", flush=True)
    print(f"  CONTENT_LISTING_BASE_URL and CONTENT_RAW_BASE_URL={address} for the server", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fake_api.py",
        description="Answer the GitHub routes this repository's tools and server read, from local files.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=PORT, help=f"default {PORT}; 0 asks for a free one")
    parser.add_argument("--root", default=str(ROOT), help="the checkout the contents routes serve")
    parser.add_argument("--repository", help="owner/name the checkout, the clone and the fixture answer as")
    parser.add_argument("--clone", help="a clone to answer a release per tag and any range from")
    parser.add_argument("--fixture", help="a JSON file of documents by path, instead of the demo")
    arguments = parser.parse_args(argv)

    root = Path(arguments.root).expanduser()
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 1

    clone: Path | None = None
    if arguments.clone:
        clone = Path(arguments.clone).expanduser()
        if not (clone / ".git").exists():
            print(f"error: {clone} is not a clone; there is no .git in it", file=sys.stderr)
            return 1

    try:
        documents = load_fixture(Path(arguments.fixture).expanduser()) if arguments.fixture else demo()
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    repository = arguments.repository or (sources.repository_of(clone) if clone else None) or "local/g0v0-news"
    world = World(repository=repository, documents=documents, clone=clone)
    return run(root=root, world=world, host=arguments.host, port=arguments.port)


if __name__ == "__main__":
    raise SystemExit(main())
