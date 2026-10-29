"""Turning something that has already shipped into candidate changelog entries.

Two sources, because they answer two different questions. A release body is the summary
somebody wrote next to a tag: already phrased for a reader, already grouped, but only as
complete as whoever wrote it. A range of commits is what actually landed between two
points: complete, unphrased, and full of work nobody needs told about.

Neither is taken as the final word. What this module returns is a list of candidates, and
a person decides which of them survive into a build -- which is why the awkward cases
below are resolved towards "keep it and let a person drop it" rather than towards
guessing.

Nothing here writes anything.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any
import urllib.error
import urllib.parse
from urllib.request import OpenerDirector, ProxyHandler, Request, build_opener

from content import ContentError
import formats

API = "https://api.github.com"

# Hosts that answer on this machine. A request to one of them is not sent to a proxy: it
# would be the proxy's opinion of `127.0.0.1`, not this machine's.
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})

# What a conventional-commit prefix means for an entry. A prefix that is not listed is
# read as `misc`: an unusual word is still a change, and dropping it would be a guess.
TYPE_BY_PREFIX = {
    "feat": "add",
    "feature": "add",
    "add": "add",
    "fix": "fix",
    "bugfix": "fix",
    "hotfix": "fix",
    "perf": "misc",
    "refactor": "misc",
    "revert": "misc",
    "remove": "misc",
    "update": "misc",
}

# Prefixes that describe the repository rather than the build. A changelog is read by
# players, so a dependency bump is noise by default and kept only when asked for.
TRIVIAL_PREFIXES = {"chore", "ci", "build", "deps", "docs", "doc", "style", "test", "tests", "release", "lint"}

# Scopes that already have a category in the committed files, so that a draft lands in the
# same group the hand-written entries use instead of inventing a second spelling.
CATEGORY_BY_SCOPE = {
    "api": "API",
    "client": "Client",
    "spectator": "Client",
    "perf": "Performance",
    "performance": "Performance",
    "wiki": "Wiki",
    "news": "News",
    "changelog": "Changelog",
}

_COMMIT_PREFIX = re.compile(r"^(?P<prefix>[A-Za-z]+)(?:\((?P<scope>[^)]+)\))?(?P<breaking>!)?:\s*(?P<subject>.+)$")
_HEADING = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<text>.+?)\s*#*\s*$")
_BULLET = re.compile(r"^\s{0,3}[-*+]\s+(?P<text>\S.*?)\s*$")
_BYLINE = re.compile(r"\s+by\s+@(?P<user>[A-Za-z0-9][A-Za-z0-9-]*)\s+in\s+(?P<url>https?://\S+?)\s*$")
_MARKDOWN_LINK = re.compile(r"\[(?P<text>[^\]]*)\]\((?P<url>https?://[^)\s]+)\)")
_PULL_URL = re.compile(r"https?://github\.com/(?P<repository>[\w.-]+/[\w.-]+)/pull/(?P<number>\d+)")
_TRAILING_PULL = re.compile(r"\s*\(#(?P<number>\d+)\)\s*$")
_MERGE_SUBJECT = re.compile(r"^Merge (?:pull request|branch|remote-tracking|commit)\b")
_TRAILER = re.compile(r"^(?:co-authored-by|signed-off-by|reviewed-by|change-id|refs|see also|cc)\s*:", re.IGNORECASE)
_BREAKING = re.compile(r"^BREAKING[ -]CHANGE\b\s*:?\s*", re.IGNORECASE)

# A section heading that says what kind of change is under it. A `None` type says the
# heading only adds something -- a breaking change can be either kind -- so the type the
# bullets already had stands. Order matters: "Bug Fixes" matches the second entry before
# the third one can read it as a feature.
_SECTION_TYPES: tuple[tuple[re.Pattern[str], str | None, bool], ...] = (
    (re.compile(r"\bbreaking\b", re.IGNORECASE), None, True),
    (re.compile(r"\b(?:bug ?fix(?:es|ed)?|fix(?:es|ed)?|fixed|hotfix(?:es)?)\b", re.IGNORECASE), "fix", False),
    (re.compile(r"\b(?:features?|added|new|enhancements?|improvements?)\b", re.IGNORECASE), "add", False),
    (
        re.compile(
            r"\b(?:performance|internal|maintenance|other|misc|refactor\w*|chores?|dependenc\w*)\b",
            re.IGNORECASE,
        ),
        "misc",
        False,
    ),
)

# Sections that are about the release rather than the changes in it.
_SKIP_SECTIONS = re.compile(
    r"\b(?:new contributors?|contributors?|full changelog|installation|checksums?|assets|downloads?)\b", re.IGNORECASE
)

# Headings GitHub's own release template writes around the list. They name the list, not
# a group of changes, so the bullets under them get no category rather than that one.
_GENERIC_SECTIONS = re.compile(r"^(?:what'?s changed|changes?|changelog|summary|highlights?|notes?)$", re.IGNORECASE)


@dataclass
class Candidate:
    """One change, as a build entry and as the record of where it came from."""

    entry: dict[str, Any]
    provenance: dict[str, Any] = field(default_factory=dict)

    def as_item(self) -> dict[str, Any]:
        """The shape a draft holds an entry in: the decision, the entry, and its origin."""
        return {"status": "keep", "entry": dict(self.entry), "provenance": dict(self.provenance)}


@dataclass
class Collection:
    """Everything one run of a source produced, ready to become a draft."""

    candidates: list[Candidate]
    source: dict[str, Any]
    version: str | None = None
    created_at: str | None = None

    def items(self) -> list[dict[str, Any]]:
        return [candidate.as_item() for candidate in self.candidates]


# -- release bodies ------------------------------------------------------------------


def release_candidates(body: str, *, repository: str, tag: str) -> list[Candidate]:
    """Read a release body into candidates.

    A release body is Markdown written by whoever cut the release, and GitHub's generated
    template is the case worth handling: `## What's Changed` above a list of
    `* Subject by @user in <pull request>`. Headings that name a kind of change
    (`## Bug Fixes`, `### Features`) set the type of the bullets under them, other
    headings become the category, and a bullet that carries its own `type(scope):` prefix
    overrides both.
    """
    candidates: list[Candidate] = []
    type_hint: str | None = None
    category: str | None = None
    major = False
    skip = False

    for line in body.splitlines():
        heading = _HEADING.match(line)
        if heading is not None:
            title = heading.group("text").strip()
            skip = bool(_SKIP_SECTIONS.search(title))
            matched = next(
                ((kind, notable) for pattern, kind, notable in _SECTION_TYPES if pattern.search(title)), None
            )
            if matched is not None:
                kind, notable = matched
                type_hint = kind or type_hint
                major = notable
                category = None
            elif not skip and not _GENERIC_SECTIONS.match(title):
                category = title
            continue

        bullet = _BULLET.match(line)
        if bullet is None or skip:
            continue

        candidate = _candidate_from_bullet(
            bullet.group("text"), repository=repository, type_hint=type_hint, category=category, major=major
        )
        if candidate is not None:
            candidate.provenance.setdefault("source", "release")
            candidate.provenance.setdefault("reference", tag)
            candidates.append(candidate)

    return dedupe(candidates)


def _candidate_from_bullet(
    text: str, *, repository: str, type_hint: str | None, category: str | None, major: bool
) -> Candidate | None:
    """Read one bullet. Returns None for a line that is not a change of its own."""
    text = text.strip()
    if not text or text.startswith("**Full Changelog**") or text.startswith("<!--"):
        return None

    author: str | None = None
    pull_url: str | None = None
    pull_repository = repository
    pull_number: int | None = None

    byline = _BYLINE.search(text)
    if byline is not None:
        author = byline.group("user")
        pull_url = byline.group("url")
        text = text[: byline.start()].strip()

    matched = _MARKDOWN_LINK.search(text)
    reference = _PULL_URL.search(matched.group("url")) if matched is not None else None
    if matched is not None and reference is not None:
        # `Subject ([#123](https://…/pull/123))`: the link is the reference, and the text
        # inside it is the number, so the whole thing goes rather than leaving `()` behind.
        pull_url = matched.group("url")
        pull_repository = reference.group("repository")
        pull_number = int(reference.group("number"))
        label = matched.group("text").strip()
        kept_label = "" if re.fullmatch(r"#?\d+", label) else label
        text = (text[: matched.start()] + kept_label + text[matched.end() :]).strip()
    else:
        matched = _PULL_URL.search(text)
        if matched is not None:
            pull_url = matched.group(0)
            pull_repository = matched.group("repository")
            pull_number = int(matched.group("number"))
            text = (text[: matched.start()] + text[matched.end() :]).strip()

    trailing = _TRAILING_PULL.search(text)
    if trailing is not None:
        pull_number = int(trailing.group("number"))
        text = text[: trailing.start()].strip()

    if pull_url is not None and pull_number is None:
        found = _PULL_URL.search(pull_url)
        pull_number = int(found.group("number")) if found is not None else None

    parsed = _COMMIT_PREFIX.match(text)
    entry_type = type_hint or "misc"
    entry_major = major
    entry_category = category
    if parsed is not None:
        prefix = parsed.group("prefix").lower()
        entry_type = TYPE_BY_PREFIX.get(prefix, "misc")
        entry_major = entry_major or bool(parsed.group("breaking"))
        entry_category = _category_for(parsed.group("scope")) or entry_category
        text = parsed.group("subject").strip()

    title = _tidy(text)
    if not title:
        return None

    entry: dict[str, Any] = {"type": entry_type}
    if entry_category:
        entry["category"] = entry_category
    entry["title"] = title
    if pull_url or pull_number:
        if pull_url:
            entry["url"] = pull_url
        elif pull_number:
            entry["url"] = f"https://github.com/{pull_repository}/pull/{pull_number}"
        if pull_number:
            entry["github_pull_request_id"] = pull_number
    if entry_major:
        entry["major"] = True
    if author:
        entry["github_user"] = {
            "display_name": author,
            "github_username": author,
            "github_url": f"https://github.com/{author}",
        }

    provenance = {"source": "release", "repository": pull_repository}
    if pull_url:
        provenance["url"] = pull_url
    if pull_number:
        provenance["pull_request"] = pull_number
    if author:
        provenance["author"] = author
    return Candidate(entry=entry, provenance=provenance)


# -- commits -------------------------------------------------------------------------


@dataclass
class Commit:
    """One commit, whichever of the two ways it was read."""

    sha: str
    subject: str
    body: str = ""
    author: str = ""
    login: str | None = None
    date: str | None = None


def commit_candidates(
    commits: Iterable[Commit], *, repository: str, reference: str, everything: bool = False
) -> list[Candidate]:
    """Read commits into candidates, one per change worth telling a player about.

    A conventional-commit prefix decides the type and the category; without one the
    commit is `misc` under no particular category. Merge commits are structural and are
    dropped, and so are the prefixes that describe the repository rather than the build --
    unless `everything` is set, because that is the whole point of the flag.
    """
    candidates: list[Candidate] = []
    for commit in commits:
        candidate = _candidate_from_commit(commit, repository=repository, everything=everything)
        if candidate is None:
            continue
        candidate.provenance["reference"] = reference
        candidates.append(candidate)
    return dedupe(candidates)


def _candidate_from_commit(commit: Commit, *, repository: str, everything: bool) -> Candidate | None:
    subject = commit.subject.strip()
    if not subject or _MERGE_SUBJECT.match(subject):
        return None

    parsed = _COMMIT_PREFIX.match(subject)
    entry_type = "misc"
    entry_category: str | None = None
    entry_major = False
    if parsed is not None:
        prefix = parsed.group("prefix").lower()
        if prefix in TRIVIAL_PREFIXES and not everything:
            return None
        entry_type = TYPE_BY_PREFIX.get(prefix, "misc")
        entry_category = _category_for(parsed.group("scope"))
        entry_major = bool(parsed.group("breaking"))
        subject = parsed.group("subject").strip()

    body = _body_paragraph(commit.body)
    if _BREAKING.search(body or ""):
        entry_major = True
    body = _BREAKING.sub("", body).strip() if body else ""

    title = _tidy(subject)
    if not title:
        return None

    pull_number: int | None = None
    trailing = _TRAILING_PULL.search(title)
    if trailing is not None:
        pull_number = int(trailing.group("number"))
        title = _tidy(title[: trailing.start()])

    entry: dict[str, Any] = {"type": entry_type}
    if entry_category:
        entry["category"] = entry_category
    entry["title"] = title
    if body and body.lower() != title.lower():
        entry["message"] = body
    if pull_number is not None:
        entry["url"] = f"https://github.com/{repository}/pull/{pull_number}"
        entry["github_pull_request_id"] = pull_number
    else:
        entry["url"] = f"https://github.com/{repository}/commit/{commit.sha}"
    entry["repository"] = repository
    if entry_major:
        entry["major"] = True

    user = _github_user(commit)
    if user:
        entry["github_user"] = user

    provenance: dict[str, Any] = {"source": "commits", "repository": repository, "commit": commit.sha}
    if pull_number is not None:
        provenance["pull_request"] = pull_number
        provenance["url"] = entry["url"]
    if commit.login:
        provenance["author"] = commit.login
    return Candidate(entry=entry, provenance=provenance)


def _github_user(commit: Commit) -> dict[str, Any] | None:
    """Who to credit. A local clone knows a name; only the API knows the login."""
    if commit.login:
        user: dict[str, Any] = {
            "display_name": commit.author or commit.login,
            "github_username": commit.login,
            "github_url": f"https://github.com/{commit.login}",
        }
        return user
    if commit.author:
        return {"display_name": commit.author}
    return None


def _body_paragraph(body: str) -> str:
    """The first paragraph of a commit body, without the trailers underneath it."""
    if not body:
        return ""
    kept: list[str] = []
    for line in body.splitlines():
        if _TRAILER.match(line.strip()):
            break
        kept.append(line)
    for paragraph in "\n".join(kept).split("\n\n"):
        text = paragraph.strip()
        if text and not text.startswith(("#", "<!--")):
            return re.sub(r"\s+", " ", text)
    return ""


# -- shared --------------------------------------------------------------------------


def dedupe(candidates: list[Candidate]) -> list[Candidate]:
    """Merge candidates that describe the same change.

    Two commits behind one pull request become one entry, and the second one's mention is
    recorded as a count rather than lost: the draft says how many commits it stands for,
    which is what a person needs to judge whether the entry is big enough to be `major`.
    """
    seen: dict[str, Candidate] = {}
    ordered: list[Candidate] = []
    for candidate in candidates:
        key = _identity(candidate)
        if key in seen:
            first = seen[key]
            first.provenance["commits"] = first.provenance.get("commits", 1) + 1
            continue
        seen[key] = candidate
        ordered.append(candidate)
    return ordered


def _identity(candidate: Candidate) -> str:
    url = candidate.entry.get("url")
    if isinstance(url, str) and url:
        return url
    return str(candidate.entry.get("title", "")).strip().lower()


def _category_for(scope: str | None) -> str | None:
    """A group heading for a scope, spelled the way the committed files spell it."""
    if not scope:
        return None
    normalised = scope.strip().lower().replace("_", "-")
    if normalised in CATEGORY_BY_SCOPE:
        return CATEGORY_BY_SCOPE[normalised]
    words = normalised.replace("-", " ")
    return words[:1].upper() + words[1:]


def _tidy(title: str) -> str:
    """Trim a title and give it a capital, leaving the rest of the wording alone.

    Emphasis is unwrapped only when it wraps the whole title: a backtick that is part of
    the text -- `` `!mp` `` is a command, not markup -- is left where it was.
    """
    text = re.sub(r"\s+", " ", title).strip()
    for marker in ("**", "__", "*", "_", "`"):
        if len(text) > 2 * len(marker) and text.startswith(marker) and text.endswith(marker):
            text = text[len(marker) : -len(marker)].strip()
            break
    text = re.sub(r"\(\s*\)", "", text)
    text = re.sub(r"\s{2,}", " ", text).strip(" -–—")
    if not text:
        return ""
    return text[:1].upper() + text[1:]


# -- GitHub --------------------------------------------------------------------------


def token() -> str | None:
    """A token to talk to the API with, or None to go without.

    The environment wins, so a CI job can hand one over; then `gh auth token`, because a
    machine that has the CLI installed has already been through the login flow once.
    """
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(name)
        if value:
            return value.strip()
    if shutil.which("gh") is None:
        return None
    try:
        # `gh` is looked up on PATH on purpose -- that is what `shutil.which` checked above --
        # and the argument list is a constant here, not something a caller supplies.
        result = subprocess.run(
            ["gh", "auth", "token"],  # noqa: S607
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def api_base() -> str:
    """Where the API is, so that a stand-in on this machine can answer instead of GitHub.

    `GITHUB_API_URL` is the name GitHub Actions and the `gh` CLI already give this, and an
    environment variable is the only way in: `serve` reaches the API from a server process
    that nobody passes flags to. Nothing else changes -- a stand-in answers the same two
    routes with the same documents, or the tool fails the way it would have.
    """
    return (os.environ.get("GITHUB_API_URL") or API).rstrip("/")


def request_headers(base: str) -> dict[str, str]:
    """The headers one request carries -- and the one it does not.

    A token goes to GitHub and nowhere else. A stand-in has no use for it, and a mistyped
    `GITHUB_API_URL` should not be a way to carry a credential off this machine.
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "g0v0-news-newsctl",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    value = token() if base == API else None
    if value:
        headers["Authorization"] = f"Bearer {value}"
    return headers


def _opener(url: str) -> OpenerDirector:
    """An opener for one request, which is how a call to this machine escapes the proxy.

    `urlopen` reads `http_proxy` and sends everything through it, including a request to
    `127.0.0.1` -- which a proxy outside this machine cannot answer. An explicit empty
    `ProxyHandler` is the only way to say "not this one".
    """
    if (urllib.parse.urlsplit(url).hostname or "") in _LOOPBACK:
        return build_opener(ProxyHandler({}))
    return build_opener()


def api(path: str, *, params: dict[str, Any] | None = None) -> Any:
    """One request to the GitHub API, with the failures a person can act on."""
    base = api_base()
    url = f"{base}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    # The scheme comes from `GITHUB_API_URL` when it is set, which is the operator's own
    # configuration rather than anything a release body or a commit message can reach.
    request = Request(url, headers=request_headers(base))  # noqa: S310

    try:
        with _opener(url).open(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise ContentError(f"the API has nothing at {path} -- check the repository and the reference") from error
        if error.code in (403, 429):
            raise ContentError(
                "the API refused the request: the rate limit is spent. Set GITHUB_TOKEN, "
                "or run `gh auth login` so that a token can be found"
            ) from error
        raise ContentError(f"the API answered {error.code} at {path}") from error
    except urllib.error.URLError as error:
        raise ContentError(
            f"could not reach the API at {base} ({error.reason}); a commit range can be read from a local clone "
            "instead, with --repo-path"
        ) from error


def fetch_release(repository: str, tag: str) -> dict[str, Any]:
    """One release, by tag."""
    return api(f"/repos/{repository}/releases/tags/{urllib.parse.quote(tag)}")


def fetch_compare(repository: str, base: str, head: str) -> list[Commit]:
    """The commits between two points, as the compare API reports them.

    The API stops at 250 commits per comparison. That is a limit of the endpoint rather
    than of this tool, which is why a range that large should be read from a clone.
    """
    comparison = api(f"/repos/{repository}/compare/{urllib.parse.quote(base)}...{urllib.parse.quote(head)}")
    commits: list[Commit] = []
    for item in comparison.get("commits", []):
        message = str((item.get("commit") or {}).get("message", ""))
        author = item.get("author") or {}
        commits.append(
            Commit(
                sha=str(item.get("sha", "")),
                subject=message.splitlines()[0] if message else "",
                body="\n".join(message.splitlines()[1:]).strip(),
                author=str(((item.get("commit") or {}).get("author") or {}).get("name", "")),
                login=author.get("login"),
            )
        )
    return commits


# -- local clone ---------------------------------------------------------------------


def _git(repo_path: Path, *arguments: str) -> str:
    try:
        # Git comes from PATH, which is the point of asking for a local clone; the arguments
        # are assembled here and never come from a release body or a commit message.
        result = subprocess.run(  # noqa: S603
            ["git", "-C", str(repo_path), *arguments],  # noqa: S607
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as error:
        raise ContentError(f"could not run git: {error}") from error
    if result.returncode != 0:
        raise ContentError(f"git {' '.join(arguments)} failed in {repo_path}:\n{result.stderr.strip()}")
    return result.stdout


def local_commits(repo_path: Path, revision_range: str, *, first_parent: bool = False) -> list[Commit]:
    """Read a range out of a clone, oldest first.

    Merge commits are dropped rather than read: their subject names a branch, not a
    change, and their content is already covered by the commits they merge. What that
    leaves depends on how the repository merges -- one commit per pull request where they
    are squashed, one per commit where they are not -- which is a difference a person
    reviewing the draft can see and this reader cannot.
    """
    arguments = ["log", "--no-merges", "--reverse", "--format=%H%x1f%an%x1f%aI%x1f%s%x1f%b%x1e"]
    if first_parent:
        arguments.insert(2, "--first-parent")
    arguments.append(revision_range)
    commits: list[Commit] = []
    for record in _git(repo_path, *arguments).split("\x1e"):
        fields = record.strip("\n").split("\x1f")
        if len(fields) < 5 or not fields[0]:
            continue
        commits.append(
            Commit(sha=fields[0], author=fields[1], date=fields[2] or None, subject=fields[3], body=fields[4].strip())
        )
    return commits


@dataclass(frozen=True)
class Tag:
    """One tag in a clone: the name somebody types, the commit it is on, and when."""

    name: str
    sha: str
    date: str | None = None


def tags(repo_path: Path) -> list[Tag]:
    """Every tag in a clone, newest first, so the releases a repository has can be walked.

    A tag name cannot contain a space -- Git's own rule -- which is what makes a space a
    safe separator here.
    """
    output = _git(
        repo_path,
        "for-each-ref",
        "--sort=-creatordate",
        "--format=%(refname:short) %(objectname) %(creatordate:iso-strict)",
        "refs/tags",
    )
    found: list[Tag] = []
    for line in output.splitlines():
        name, _, rest = line.partition(" ")
        sha, _, date = rest.partition(" ")
        if name and sha:
            found.append(Tag(name=name, sha=sha, date=date or None))
    return found


def latest_tag(repo_path: Path) -> str | None:
    """The most recent tag reachable from HEAD, if the clone has any."""
    try:
        return _git(repo_path, "describe", "--tags", "--abbrev=0", "HEAD").strip() or None
    except ContentError:
        return None


def default_range(repo_path: Path) -> str:
    """Everything since the last release, which is what a new build usually is."""
    tag = latest_tag(repo_path)
    return f"{tag}..HEAD" if tag else "HEAD"


def repository_of(repo_path: Path) -> str | None:
    """`owner/name`, read from the clone's `origin`, so it can be asked for once."""
    try:
        url = _git(repo_path, "config", "--get", "remote.origin.url").strip()
    except ContentError:
        return None
    matched = re.search(r"github\.com[:/](?P<repository>[\w.-]+/[\w.-]+?)(?:\.git)?$", url)
    return matched.group("repository") if matched is not None else None


# -- one call for both sources -------------------------------------------------------


def collect(
    *,
    repository: str | None = None,
    release: str | None = None,
    commit_range: str | None = None,
    repo_path: Path | str | None = None,
    first_parent: bool = False,
    everything: bool = False,
    limit: int | None = None,
) -> Collection:
    """Read one source, and say what it suggests the build should be called.

    A release suggests a version when its tag already looks like one -- the streams here
    tag their builds `2026.1009.0-g0v0` -- and a date from when it was published. A range
    of commits suggests neither: a version is a decision, and a decision is not something
    a commit can make.
    """
    if release and commit_range:
        raise ContentError("a collection reads one source at a time: pass either a release or a range, not both")

    if release:
        if not repository:
            raise ContentError("a release is read from a repository: pass --repository owner/name")
        document = fetch_release(repository, release)
        body = str(document.get("body") or "")
        candidates = release_candidates(body, repository=repository, tag=release)
        published = formats.parse_timestamp(document.get("published_at"))
        source = {"kind": "release", "repository": repository, "reference": release}
        if document.get("html_url"):
            source["url"] = document["html_url"]
        return Collection(
            candidates=_limited(candidates, limit),
            source=source,
            version=release if formats.VERSION.match(release) else None,
            created_at=formats.format_timestamp(published) if published else None,
        )

    if repo_path is not None:
        path = Path(repo_path).expanduser()
        if not path.is_dir():
            raise ContentError(f"{path} is not a directory")
        reference = commit_range or default_range(path)
        commits = local_commits(path, _local_range(reference), first_parent=first_parent)
        repository = repository or repository_of(path)
    elif commit_range:
        if not repository:
            raise ContentError("a range is read from a repository: pass --repository owner/name, or --repo-path")
        base, _, head = commit_range.replace("...", "..").partition("..")
        commits = fetch_compare(repository, base, head) if base and head else []
    else:
        raise ContentError("nothing to read: pass --release <tag>, or --range <base..head>, or --repo-path <dir>")

    if not repository:
        raise ContentError("the commits were read, but no repository is known to attribute them to")

    candidates = commit_candidates(
        commits, repository=repository, reference=commit_range or "HEAD", everything=everything
    )
    source = {"kind": "commits", "repository": repository, "reference": commit_range or "(since the last tag)"}
    return Collection(candidates=_limited(candidates, limit), source=source)


def _local_range(reference: str) -> str:
    """A range git will accept: `..` is fine, `...` is a spelling the API taught people."""
    return reference.replace("...", "..")


def _limited(candidates: list[Candidate], limit: int | None) -> list[Candidate]:
    if limit is None or limit <= 0:
        return candidates
    return candidates[:limit]
