"""The content tree, and the drafts that sit between a source and a build file.

Everything that writes goes through here, so that the ways a file can be wrong are checked
in one place: a build is validated against its schema before it is written, an identifier
is compared against the identifiers already in use, and a post's front matter goes out
through the same writer that reads it back.

A draft is the half-way state. It is a JSON file under `drafts/`, next to the content trees
rather than inside them, because the server serves whatever it finds under `changelog/`
and `news/`: a draft stored there would be published by being saved.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import re
from typing import Any

import formats
import schemas

ROOT = schemas.ROOT
CHANGELOG = ROOT / "changelog"
NEWS = ROOT / "news"
DRAFTS = ROOT / "drafts"

STREAMS_FILE = CHANGELOG / "streams.json"
BUILD_SCHEMA = CHANGELOG / "build.schema.json"
STREAMS_SCHEMA = CHANGELOG / "streams.schema.json"
POST_SCHEMA = NEWS / "post.schema.json"

# The key order the committed files are in. Writing in the same order keeps a diff between
# a file the tools wrote and a file somebody wrote by hand down to the lines that changed.
BUILD_ORDER = ("$schema", "version", "id", "display_version", "created_at", "users", "youtube_id", "entries")
ENTRY_ORDER = (
    "id",
    "type",
    "category",
    "title",
    "message",
    "url",
    "repository",
    "github_url",
    "github_pull_request_id",
    "major",
    "created_at",
    "github_user",
)
POST_ORDER = ("title", "author", "date", "updated", "id", "series")
DRAFT_ORDER = ("version", "stream", "created_at", "users", "youtube_id", "generated_at", "sources", "entries")

_STREAM_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")

__all__ = [
    "Build",
    "ContentError",
    "Draft",
    "Post",
    "build_document",
    "builds",
    "create_post",
    "delete_draft",
    "drafts",
    "load_draft",
    "load_post",
    "new_draft",
    "posts",
    "publish",
    "relative",
    "resolve",
    "save_draft",
    "save_post",
    "streams",
    "write_build",
]


class ContentError(Exception):
    """A refusal, with a reason that is meant to be read by a person."""


def relative(path: Path | str) -> str:
    """Name a path the way a reader of this repository sees it."""
    path = Path(path)
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def resolve(name: str, *, under: Path | None = None) -> Path:
    """Turn a path a request named into one inside the repository, or refuse it.

    The check is on the resolved path, not on the text, so `news/../changelog` and a
    symlink out of the tree are refused the same way. This is what keeps the web console
    from being a way to read or write anything on the machine.
    """
    candidate = (ROOT / name).resolve()
    if not candidate.is_relative_to(ROOT):
        raise ContentError(f"{name} is outside this repository")
    if under is not None and not candidate.is_relative_to(under.resolve()):
        raise ContentError(f"{name} is not under {relative(under)}")
    return candidate


def write_text(path: Path, text: str) -> None:
    """Write UTF-8 with LF endings, whatever the platform would rather do."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


# -- streams -------------------------------------------------------------------------


def _load_streams_file() -> list[dict[str, Any]]:
    if not STREAMS_FILE.is_file():
        return []
    document = json.loads(STREAMS_FILE.read_text(encoding="utf-8"))
    listed = document.get("streams") if isinstance(document, dict) else document
    return [stream for stream in listed if isinstance(stream, dict)] if isinstance(listed, list) else []


def stream_directories() -> list[str]:
    """Every directory under changelog/, which is every stream that has a build."""
    if not CHANGELOG.is_dir():
        return []
    return sorted(path.name for path in CHANGELOG.iterdir() if path.is_dir())


def streams() -> list[dict[str, Any]]:
    """The streams as the client sees them: declared ones first, then the rest.

    An undeclared directory still shows up with its own name, which is the server's rule,
    so the same rule is applied here rather than a stricter one.
    """
    declared = _load_streams_file()
    names = {stream.get("name") for stream in declared}
    for name in stream_directories():
        if name not in names:
            declared.append({"name": name, "display_name": name, "is_featured": False, "undeclared": True})
    return declared


def stream_names() -> list[str]:
    return [stream["name"] for stream in streams() if isinstance(stream.get("name"), str)]


def ensure_version(version: str) -> None:
    """Refuse a version before a file is named after it."""
    if not formats.VERSION.match(version):
        raise ContentError(f"{version!r} is not a version: expected `YYYY.MDD.N`, optionally with a `-suffix`")


def ensure_stream(name: str) -> None:
    """Refuse a stream the schema would not accept, before a directory is made for it."""
    if not _STREAM_NAME.match(name):
        raise ContentError(f"{name!r} is not a stream name: lowercase letters, digits and dashes, not at the start")


# -- builds --------------------------------------------------------------------------


@dataclass(frozen=True)
class Build:
    """One file under `changelog/<stream>/`."""

    path: Path
    stream: str
    document: dict[str, Any]

    @property
    def version(self) -> str:
        return str(self.document.get("version", ""))

    @property
    def name(self) -> str:
        """The path a reader of this repository would type."""
        return relative(self.path)

    @property
    def entries(self) -> list[dict[str, Any]]:
        entries = self.document.get("entries")
        return entries if isinstance(entries, list) else []

    @property
    def published(self) -> datetime | None:
        """When the build was released, either declared or read from its version."""
        declared = formats.parse_timestamp(self.document.get("created_at"))
        return declared or formats.date_from_version(self.version)

    @property
    def display(self) -> str:
        return str(self.document.get("display_version") or formats.display_version(self.version))


def _build_sort_key(build: Build) -> tuple[float, str]:
    moment = build.published
    return (-(moment.timestamp() if moment else 0.0), build.version)


def builds() -> list[Build]:
    """Every build in the repository, newest first."""
    found: list[Build] = []
    for stream in stream_directories():
        for path in sorted((CHANGELOG / stream).glob("*.json")):
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                # A file that does not parse is still a file: it is listed so that the
                # checker can say so, rather than disappearing from the overview.
                document = {"version": path.stem, "entries": [], "unreadable": True}
            found.append(Build(path=path, stream=stream, document=document))
    return sorted(found, key=_build_sort_key)


def find_build(version: str) -> Build | None:
    return next((build for build in builds() if build.version == version), None)


def build_problems(document: dict[str, Any]) -> list[str]:
    """Check one build against the schema, and nothing else."""
    return schemas.validate(document, schemas.read_schema(BUILD_SCHEMA))


def write_build(document: dict[str, Any], stream: str, *, force: bool = False) -> Path:
    """Write one build, after everything that can be known about it has been checked.

    The schema is only half of it: the file name, the stream suffix and the identifier are
    rules that only make sense once the directory is known, and they are the ones a person
    gets wrong by hand, so they are enforced here as well as by the checker.
    """
    version = document.get("version")
    if not isinstance(version, str):
        raise ContentError(f"{version!r} is not a version: expected `YYYY.MDD.N`, optionally with a `-suffix`")
    ensure_version(version)

    problems = build_problems(document)
    if problems:
        raise ContentError("the build does not validate:\n  " + "\n  ".join(problems))

    suffix = formats.version_suffix(version)
    if suffix and suffix != stream:
        raise ContentError(f"{version} names the stream `{suffix}`, so it does not belong in changelog/{stream}/")

    ensure_stream(stream)
    path = CHANGELOG / stream / f"{version}.json"
    if path.exists() and not force:
        raise ContentError(f"{relative(path)} already exists; pass --force to replace it")

    identifier = document.get("id")
    if identifier is None:
        identifier = formats.build_id(version)
    for build in builds():
        if build.path == path:
            continue
        other = build.document.get("id")
        if other is None:
            other = formats.build_id(build.version)
        if other == identifier:
            raise ContentError(
                f"build id {identifier} is already used by {build.name}; declare an `id` to tell the two apart"
            )

    write_text(path, schemas.render_json(schemas.ordered(document, BUILD_ORDER)))
    return path


# -- drafts --------------------------------------------------------------------------


@dataclass
class Draft:
    """Candidate entries, and the decision about each of them, before there is a build."""

    path: Path
    document: dict[str, Any]

    @property
    def name(self) -> str:
        return relative(self.path)

    @property
    def version(self) -> str:
        return str(self.document.get("version", ""))

    @property
    def stream(self) -> str:
        stream = self.document.get("stream")
        if isinstance(stream, str) and stream:
            return stream
        return formats.version_suffix(self.version)

    @property
    def entries(self) -> list[dict[str, Any]]:
        entries = self.document.get("entries")
        return entries if isinstance(entries, list) else []

    def kept(self) -> list[dict[str, Any]]:
        return [entry for entry in self.entries if _is_kept(entry)]

    def dropped(self) -> list[dict[str, Any]]:
        return [entry for entry in self.entries if not _is_kept(entry)]

    @property
    def sources(self) -> list[dict[str, Any]]:
        sources = self.document.get("sources")
        return sources if isinstance(sources, list) else []


def _is_kept(entry: dict[str, Any]) -> bool:
    """Whether a reviewed entry survives into the build. Untouched candidates do."""
    return entry.get("status", "keep") == "keep" and isinstance(entry.get("entry"), dict)


def draft_path(version: str) -> Path:
    return DRAFTS / f"{version}.json"


def drafts() -> list[Draft]:
    """Every draft on disk, newest first."""
    if not DRAFTS.is_dir():
        return []
    found: list[Draft] = []
    for path in sorted(DRAFTS.glob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as error:
            raise ContentError(f"{relative(path)} is not valid JSON: {error}") from error
        found.append(Draft(path=path, document=document))
    return sorted(found, key=lambda draft: str(draft.document.get("generated_at", "")), reverse=True)


def load_draft(name: str | Path) -> Draft:
    """Load a draft by path, or by the bare version it is named after."""
    text = str(name)
    candidate = Path(text)
    if candidate.suffix != ".json":
        candidate = draft_path(text)
    path = resolve(candidate.as_posix(), under=DRAFTS)
    if not path.is_file():
        raise ContentError(f"{relative(path)} does not exist")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as error:
        raise ContentError(f"{relative(path)} is not valid JSON: {error}") from error
    return Draft(path=path, document=document)


def new_draft(
    *,
    version: str,
    stream: str,
    entries: Iterable[dict[str, Any]],
    sources: Iterable[dict[str, Any]],
    created_at: str | None = None,
    users: int | None = None,
    youtube_id: str | None = None,
    allow_empty: bool = False,
) -> Draft:
    """A draft holding candidates, each kept until somebody says otherwise."""
    ensure_version(version)
    ensure_stream(stream)

    suffix = formats.version_suffix(version)
    if suffix and suffix != stream:
        raise ContentError(f"{version} names the stream `{suffix}`, so it cannot be a build of `{stream}`")

    document: dict[str, Any] = {"version": version, "stream": stream}
    if created_at:
        document["created_at"] = created_at
    if users is not None:
        document["users"] = users
    if youtube_id:
        document["youtube_id"] = youtube_id
    document["generated_at"] = formats.format_timestamp(datetime.now(UTC))
    document["sources"] = list(sources)
    document["entries"] = [_draft_item(candidate) for candidate in entries]

    if not document["entries"] and not allow_empty:
        raise ContentError("the source produced no entry, so there is nothing to review")

    return Draft(path=draft_path(version), document=document)


def blank_draft(
    *,
    version: str,
    stream: str,
    created_at: str | None = None,
    users: int | None = None,
    youtube_id: str | None = None,
) -> Draft:
    """An empty draft: the way in when nobody wrote the notes for you.

    A draft is where entries live before they are a build, and it does not have to come
    from a release. A change nobody wrote down, an outage worth explaining, a summary
    written from memory -- all of it goes through the same file, so that it is reviewed,
    ordered and published the same way as anything read from a source. `sources` stays
    empty, which is how a reader of the draft can tell nothing was extracted.
    """
    return new_draft(
        version=version,
        stream=stream,
        entries=[],
        sources=[],
        created_at=created_at,
        users=users,
        youtube_id=youtube_id,
        allow_empty=True,
    )


def add_entry(draft: Draft, entry: dict[str, Any]) -> Draft:
    """Put one hand-written entry at the end of a draft.

    The two things a source would have got right for you -- that the type is one the
    client knows, and that the entry says what changed -- are checked here, because a
    draft is not validated against a schema: it is a work file, and a half-typed entry is
    exactly what it is meant to hold.
    """
    kind = entry.get("type")
    if kind not in ("add", "fix", "misc"):
        raise ContentError(f"{kind!r} is not a type the client knows: `add`, `fix` or `misc`")
    if not str(entry.get("title") or entry.get("message") or "").strip():
        raise ContentError("an entry needs a `title` or a `message`; that is the part a reader reads")
    draft.document.setdefault("entries", []).append(_draft_item({"entry": entry}))
    return draft


def _draft_item(candidate: dict[str, Any]) -> dict[str, Any]:
    """One entry as a draft keeps it: the decision, the entry, and where it came from."""
    return {
        "status": candidate.get("status", "keep"),
        "entry": schemas.ordered(candidate["entry"], ENTRY_ORDER),
        "provenance": candidate.get("provenance", {}),
    }


def append_to_draft(draft: Draft, *, entries: Iterable[dict[str, Any]], source: dict[str, Any]) -> Draft:
    """Add a second source's candidates to a draft that is already being worked on.

    A build usually summarizes more than one repository -- a server change and a client
    change land in different places and in the same release -- and each is read with its
    own command. Appending is what lets the second read happen without throwing away the
    decisions already made about the first. An entry already in the draft is skipped, so
    reading the same repository twice does not double it.
    """
    known = {
        item.get("entry", {}).get("url") or str(item.get("entry", {}).get("title", "")).strip().lower()
        for item in draft.entries
    }
    added = 0
    for candidate in entries:
        item = _draft_item(candidate)
        identity = item["entry"].get("url") or str(item["entry"].get("title", "")).strip().lower()
        if identity in known:
            continue
        known.add(identity)
        draft.entries.append(item)
        added += 1

    if added == 0:
        raise ContentError("every candidate from that source is already in the draft")

    draft.document.setdefault("sources", []).append(source)
    return draft


def save_draft(draft: Draft) -> Path:
    """Write a draft back, with the entries in the order they should be listed.

    A draft is not validated against a schema: it is a work file, and a half-typed entry
    is exactly what it is meant to hold. What is checked is that nothing else is written
    into `drafts/` by mistake.
    """
    path = resolve(draft.path.as_posix(), under=DRAFTS)
    write_text(path, schemas.render_json(schemas.ordered(draft.document, DRAFT_ORDER)))
    return path


def delete_draft(draft: Draft) -> None:
    path = resolve(draft.path.as_posix(), under=DRAFTS)
    if not path.is_file():
        raise ContentError(f"{relative(path)} does not exist")
    path.unlink()


def build_document(draft: Draft) -> dict[str, Any]:
    """Turn a draft into the build file it would publish.

    Only the kept entries survive, and their provenance is left behind: it is how the
    draft was made, not part of what the client is served.
    """
    kept = draft.kept()
    if not kept:
        # Two ways to arrive with nothing, and they are not the same mistake: an empty
        # draft has not been written yet, a fully dropped one has been written and emptied.
        if not draft.entries:
            raise ContentError("this draft has no entry yet, so there is nothing to publish")
        raise ContentError("every entry was dropped, so there is no build to publish")

    document: dict[str, Any] = {"$schema": "../build.schema.json", "version": draft.version}
    for key in ("display_version", "created_at", "users", "youtube_id"):
        value = draft.document.get(key)
        if value not in (None, ""):
            document[key] = value
    document["entries"] = [schemas.ordered(item["entry"], ENTRY_ORDER) for item in kept]
    return document


def publish(draft: Draft, *, force: bool = False) -> Path:
    """Write the build a draft describes, and leave the draft where it is."""
    return write_build(build_document(draft), draft.stream, force=force)


# -- posts ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Post:
    """One file under `news/`."""

    path: Path
    front_matter: dict[str, Any]
    body: str

    @property
    def name(self) -> str:
        return relative(self.path)

    @property
    def slug(self) -> str:
        return self.path.stem

    @property
    def published(self) -> datetime | None:
        return formats.parse_timestamp(self.front_matter.get("date"))


def posts() -> list[Post]:
    """Every post, newest first."""
    if not NEWS.is_dir():
        return []
    found = [load_post(path) for path in sorted(NEWS.rglob("*.md"))]
    return sorted(found, key=lambda post: post.published or datetime.min.replace(tzinfo=UTC), reverse=True)


def load_post(name: str | Path) -> Post:
    text = str(name)
    candidate = Path(text)
    if candidate.suffix != ".md":
        candidate = candidate.with_suffix(".md")
    path = resolve(candidate.as_posix(), under=NEWS)
    if not path.is_file():
        raise ContentError(f"{relative(path)} does not exist")
    front_matter, body = formats.split_markdown(path.read_text(encoding="utf-8"))
    return Post(path=path, front_matter=front_matter, body=body)


def post_problems(front_matter: dict[str, Any]) -> list[str]:
    return schemas.validate(front_matter, schemas.read_schema(POST_SCHEMA))


def render_post(front_matter: dict[str, Any], body: str) -> str:
    """Write a post: front matter, a blank line, then the body."""
    return formats.render_front_matter(front_matter, POST_ORDER) + "\n" + body.strip("\n") + "\n"


def save_post(path: Path, front_matter: dict[str, Any], body: str) -> Path:
    problems = post_problems(front_matter)
    if problems:
        raise ContentError("the front matter does not validate:\n  " + "\n  ".join(problems))
    path = resolve(path.as_posix(), under=NEWS)
    write_text(path, render_post(front_matter, body))
    return path


def create_post(
    *,
    title: str,
    author: str,
    date: datetime | None = None,
    slug: str | None = None,
    series: str | None = None,
    body: str = "",
) -> Post:
    """Start a post, named after its own publication date.

    The slug is derived from the title when it can be, and asked for when it cannot: a
    title in Chinese leaves nothing behind, and inventing a name for it would be worse
    than asking.
    """
    moment = (date or datetime.now(UTC)).astimezone(UTC)
    if not title.strip() or not author.strip():
        raise ContentError("a post needs a title and an author; the listing shows both")
    chosen = (slug or formats.slugify(title)).strip("-")
    if not chosen:
        raise ContentError("the title leaves no slug behind; pass one, for example `autumn-update`")
    if not all(character.isalnum() or character == "-" for character in chosen) or chosen.lower() != chosen:
        raise ContentError(f"{chosen!r} is not a slug: lowercase letters, digits and dashes only")

    front_matter: dict[str, Any] = {
        "title": title,
        "author": author,
        "date": formats.format_timestamp(moment),
    }
    if series:
        front_matter["series"] = series

    path = NEWS / moment.strftime("%Y") / f"{moment.strftime('%Y%m%d')}-{chosen}.md"
    if path.exists():
        raise ContentError(f"{relative(path)} already exists")
    save_post(path, front_matter, body)
    return load_post(path)
