"""Check this repository before it is pushed.

The server reads the same content, so this script is where a mistake is supposed to be
caught: a field that is missing, a version that does not match its file name, a stream
declared but never built. It reads the schemas next to the files it checks, so adding a
field means editing the schema rather than this script.

    uv run python tools/validate.py

Exits with 1 when anything is wrong, so a CI job can run it as is. The rules below that
a schema cannot express -- identifiers being unique, a version matching its file name, a
stream having a directory -- are the reason this is a script rather than a linter.
"""

import argparse
import json
import re
import zlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker, exceptions

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "changelog"
NEWS = ROOT / "news"

# Files at the root of changelog/ that are not builds.
CHANGELOG_ROOT_FILES = frozenset({"streams.json", "build.schema.json", "streams.schema.json"})

_FRONT_MATTER_PATTERN = re.compile(r"\A---[ \t]*\r?\n(?P<body>.*?)(?:\r?\n)?---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_TIMESTAMP_OFFSET_PATTERN = re.compile(r"([+-])(\d{2}):?(\d{2})$")
_VERSION_DATE_PATTERN = re.compile(r"^(?P<year>\d{4})\.(?P<monthday>\d{1,4})\.")
_VERSION_DIGITS_PATTERN = re.compile(r"[^0-9.]")
_SLUG_PATTERN = re.compile(r"^(?P<date>[0-9]{8})-[a-z0-9-]+$")


class Report:
    """The problems found, and where they were found."""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, path: str, reason: str) -> None:
        self.errors.append(f"{path}: {reason}")

    def warning(self, path: str, reason: str) -> None:
        self.warnings.append(f"{path}: {reason}")

    def extend(self, path: str, reasons: list[str]) -> None:
        for reason in reasons:
            self.error(path, reason)


def relative(path: Path) -> str:
    """Name a path the way a reader of this repository sees it."""
    return path.relative_to(ROOT).as_posix()


def load_json(path: Path, report: Report) -> Any | None:
    """Read a JSON file, reporting why it failed rather than raising."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as error:
        report.error(relative(path), f"is not valid JSON: {error}")
        return None


def read_schema(path: Path) -> Draft202012Validator:
    """Read a schema, and refuse a file that is not a valid schema itself."""
    schema: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def describe(error: exceptions.ValidationError) -> str:
    """Render one failure as `/<field>: reason`.

    `anyOf` and `oneOf` only report that no branch matched, which says nothing about
    what is actually wrong; the branch that got furthest says it, so that is the one
    shown.
    """
    parts = list(error.absolute_path)
    message = error.message

    if error.context:
        best = exceptions.best_match(error.context)
        if best is not None:
            parts.extend(best.absolute_path)
            message = best.message

    location = "/" + "/".join(str(part) for part in parts) if parts else "/"
    return f"{location}: {message}"


def validate(instance: Any, validator: Draft202012Validator) -> list[str]:
    """Check an instance against a schema, one line per failure."""
    return [describe(error) for error in validator.iter_errors(instance)]


def parse_scalar(value: str) -> Any:
    """Read one front matter value, as the server reads it.

    Kept in step with `parse_scalar` in g0v0-server: quoted strings, inline lists,
    booleans, nulls and integers, and everything else as text. A front matter that this
    reads differently from the server would pass here and be refused there.
    """
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        return [parse_scalar(part) for part in inner.split(",")] if inner else []
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    if value.lower() in ("null", "~"):
        return None
    if value.lstrip("-").isdigit():
        return int(value)
    return value


def split_front_matter(markdown: str) -> dict[str, Any]:
    """Read the front matter block of a post.

    Kept in step with `split_front_matter` in g0v0-server: flat keys only, and a key
    with an empty value is left out rather than stored as an empty string.
    """
    match = _FRONT_MATTER_PATTERN.match(markdown.lstrip("\ufeff"))
    if match is None:
        return {}

    values: dict[str, Any] = {}
    for line in match.group("body").splitlines():
        content = line.strip()
        if not content or content.startswith("#") or line[0] in " \t":
            continue

        key, separator, value = line.partition(":")
        if not separator or not value.strip():
            continue
        values[key.strip()] = parse_scalar(value)

    return values


def parse_timestamp(value: object) -> datetime | None:
    """Read a timestamp, as the server reads it: without an offset it is UTC."""
    if not isinstance(value, str):
        return None

    text = re.sub(r"\s+", " ", value.strip())
    if not text:
        return None

    text = text.replace(" ", "T", 1).replace(" ", "")
    text = _TIMESTAMP_OFFSET_PATTERN.sub(r"\1\2:\3", text)
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def date_from_version(version: str) -> datetime | None:
    """Read the release date a version carries, as the server reads it."""
    match = _VERSION_DATE_PATTERN.match(version)
    if match is None:
        return None

    monthday = match.group("monthday").zfill(4)
    try:
        return datetime(int(match.group("year")), int(monthday[:2]), int(monthday[2:]), tzinfo=UTC)
    except ValueError:
        return None


def build_id(version: str) -> int:
    """Derive the identifier of a build, as the server derives it."""
    digits = _VERSION_DIGITS_PATTERN.sub("", version.split("-", 1)[0]).replace(".", "")
    return int(digits) if digits.isdigit() else zlib.crc32(version.encode())


def check_changelog(report: Report) -> None:
    """Check every stream and build under changelog/."""
    if not CHANGELOG.is_dir():
        report.error("changelog/", "does not exist")
        return

    build_schema = read_schema(CHANGELOG / "build.schema.json")

    declared: dict[str, dict[str, Any]] = {}
    streams_file = CHANGELOG / "streams.json"
    if streams_file.is_file():
        document = load_json(streams_file, report)
        if document is not None:
            streams_schema = read_schema(CHANGELOG / "streams.schema.json")
            report.extend(relative(streams_file), validate(document, streams_schema))
            listed = document.get("streams") if isinstance(document, dict) else document
            for stream in listed if isinstance(listed, list) else []:
                if isinstance(stream, dict) and isinstance(stream.get("name"), str):
                    declared[stream["name"]] = stream
    else:
        report.warning("changelog/", "holds no streams.json, so every stream shows up with its directory name")

    for entry in sorted(CHANGELOG.iterdir()):
        if entry.is_file() and entry.name not in CHANGELOG_ROOT_FILES:
            report.error(relative(entry), "only streams.json and the schemas belong at the root of changelog/")

    directories = {path.name: path for path in sorted(CHANGELOG.iterdir()) if path.is_dir()}
    identifiers: dict[int, str] = {}

    for name, directory in directories.items():
        if name not in declared:
            report.warning(relative(directory), "is not declared in streams.json, so the client lists it as-is")

        builds = sorted(directory.glob("*.json"))
        if not builds:
            report.error(relative(directory), "holds no build, so the stream is dropped from the changelog")

        for path in sorted(directory.iterdir()):
            if path.is_file() and path.suffix != ".json":
                report.warning(relative(path), "is not a build, so it is ignored")

        for path in builds:
            check_build(path, report, build_schema, identifiers, name)

    for name in sorted(set(declared) - set(directories)):
        report.error("changelog/streams.json", f"declares `{name}`, but changelog/{name}/ does not exist")


def check_build(
    path: Path,
    report: Report,
    schema: Draft202012Validator,
    identifiers: dict[int, str],
    stream: str,
) -> None:
    """Check one build, and the rules that only make sense across files."""
    document = load_json(path, report)
    if document is None:
        return

    problems = validate(document, schema)
    report.extend(relative(path), problems)
    if problems:
        # Everything below reads fields the schema just said may not be there.
        return

    version = document["version"]
    if path.stem != version:
        report.error(relative(path), f"is named after {path.stem!r} but holds version {version!r}")

    # A build may be suffixed with the stream it was built for, and the server strips
    # that suffix to get the version it displays. A suffix naming another stream would
    # be shown as the version of a build this stream never published.
    suffix = version.partition("-")[2]
    if suffix and suffix != stream:
        report.error(relative(path), f"is a `-{suffix}` build but sits in changelog/{stream}/")

    identifier = document.get("id")
    if identifier is None:
        identifier = build_id(version)
    if identifier in identifiers:
        report.error(relative(path), f"has build id {identifier}, which {identifiers[identifier]} already uses")
    else:
        identifiers[identifier] = relative(path)

    created_at = document.get("created_at")
    released = date_from_version(version)
    if created_at is not None and released is not None:
        published = parse_timestamp(created_at)
        if published is None:
            report.error(relative(path), f"/created_at: {created_at!r} is not a date that exists")
        elif published.date() != released.date():
            report.warning(
                relative(path),
                f"is dated {published.date()} but its version says {released.date()}, which orders it by the first",
            )


def check_news(report: Report) -> None:
    """Check every post under news/."""
    if not NEWS.is_dir():
        report.error("news/", "does not exist")
        return

    schema = read_schema(NEWS / "post.schema.json")
    posts = sorted(NEWS.rglob("*.md"))
    if not posts:
        report.error("news/", "holds no post")

    for directory in sorted(path for path in NEWS.iterdir() if path.is_dir()):
        if re.fullmatch(r"[0-9]{4}", directory.name) is None:
            report.warning(relative(directory), "is not named after a year, which is where the server looks for posts")

    identifiers: dict[int, str] = {}
    for path in posts:
        front_matter = split_front_matter(path.read_text(encoding="utf-8"))
        problems = validate(front_matter, schema)
        report.extend(relative(path), problems)
        if problems:
            continue

        published = parse_timestamp(front_matter["date"])
        if published is None:
            report.error(relative(path), f"/date: {front_matter['date']!r} is not a date that exists")
            continue

        identifier = front_matter.get("id") or int(published.strftime("%Y%m%d%H%M%S"))
        if identifier in identifiers:
            report.error(relative(path), f"has post id {identifier}, which {identifiers[identifier]} already uses")
        else:
            identifiers[identifier] = relative(path)

        slug = _SLUG_PATTERN.match(path.stem)
        if slug is None:
            report.warning(relative(path), "is not named `YYYYMMDD-some-title`, which is how the other posts are")
        elif slug.group("date") != published.strftime("%Y%m%d"):
            report.warning(relative(path), f"is named after {slug.group('date')} but is dated {published.date()}")


def run_cases(schema: Draft202012Validator, cases: list[tuple[str, Any, bool]]) -> int:
    """Check cases against one schema, and count the ones that behaved differently."""
    failures = 0
    for name, instance, expected in cases:
        accepted = not validate(instance, schema)
        if accepted == expected:
            print(f"  ok    {name}")
            continue
        failures += 1
        print(f"  FAIL  {name}: expected {'accepted' if expected else 'refused'}")
    return failures


def self_test() -> int:
    """Check the schemas against cases they have to accept and refuse.

    The schemas are the contract the server also enforces, so a keyword that is
    misspelt or a rule that was loosened on purpose shows up here as a case that no
    longer behaves.
    """
    build = {
        "version": "2026.1009.0",
        "entries": [{"type": "add", "title": "Something", "github_user": {"display_name": "Someone"}}],
    }
    post = {"title": "Autumn update", "author": "GooGuTeam", "date": "2026-10-28 08:00:00 +0000"}

    build_cases: list[tuple[str, Any, bool]] = [
        ("a minimal build", build, True),
        ("an editor hint", {**build, "$schema": "../build.schema.json"}, True),
        ("a relatived link", {**build, "entries": [{"type": "add", "title": "x", "url": "/wiki/Main_page"}]}, True),
        ("a link with a space", {**build, "entries": [{"type": "add", "title": "x", "url": "not a url"}]}, False),
        ("an entry without a type", {**build, "entries": [{"title": "x"}]}, False),
        ("an entry without a body", {**build, "entries": [{"type": "add"}]}, False),
        ("an unknown entry key", {**build, "entries": [{"type": "add", "title": "x", "minor": True}]}, False),
        ("an unknown build key", {**build, "stream": "g0v0"}, False),
        ("an empty entry list", {**build, "entries": []}, False),
        ("a version of the wrong shape", {**build, "version": "1.2.3"}, False),
        ("a string where a number belongs", {**build, "users": "42"}, False),
        ("a negative count", {**build, "users": -1}, False),
        ("an unknown change type", {**build, "entries": [{"type": "feature", "title": "x"}]}, False),
        ("a timestamp written as prose", {**build, "created_at": "2026/10/09"}, False),
        ("an empty github_user", {**build, "entries": [{"type": "add", "title": "x", "github_user": {}}]}, False),
    ]

    post_cases: list[tuple[str, Any, bool]] = [
        ("a minimal post", post, True),
        ("a series of its own", {**post, "series": "whatever the team calls it"}, True),
        ("a post without an author", {key: value for key, value in post.items() if key != "author"}, False),
        ("a post without a date", {key: value for key, value in post.items() if key != "date"}, False),
        ("a date written as prose", {**post, "date": "October 28"}, False),
        ("a blank series", {**post, "series": ""}, False),
        ("an unknown key", {**post, "summary": "A hand-written summary"}, False),
    ]

    print("build.schema.json")
    failures = run_cases(read_schema(CHANGELOG / "build.schema.json"), build_cases)
    print("\npost.schema.json")
    failures += run_cases(read_schema(NEWS / "post.schema.json"), post_cases)

    total = len(build_cases) + len(post_cases)
    print(f"\n{total - failures}/{total} self-test case(s) passed")
    return 0 if failures == 0 else 1


def main(argv: list[str] | None = None) -> int:
    """Check the repository, or the schemas, and report what is wrong."""
    parser = argparse.ArgumentParser(description="Check the content of this repository against its schemas.")
    parser.add_argument("--self-test", action="store_true", help="check the schemas against known cases")
    arguments = parser.parse_args(argv)

    if arguments.self_test:
        return self_test()

    report = Report()
    check_changelog(report)
    check_news(report)

    for message in report.errors:
        print(f"error    {message}")
    for message in report.warnings:
        print(f"warning  {message}")

    print(f"\n{len(report.errors)} error(s), {len(report.warnings)} warning(s)")
    return 0 if not report.errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
