"""The text formats this repository is written in, in both directions.

Front matter is a flat YAML subset and timestamps are read leniently, both on purpose, and
the server reads them the same way: a value written differently from the way it is read
would pass validation here and be refused there. So this module owns the parser *and* the
writer, and `validate.py` reads through it instead of keeping a second copy.

Versions are parsed here as well, because a version carries the release date, the stream
suffix and the identifier the API addresses a build by.
"""

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
import re
from typing import Any
import zlib

_FRONT_MATTER = re.compile(r"\A---[ \t]*\r?\n(?P<body>.*?)(?:\r?\n)?---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_TIMESTAMP_OFFSET = re.compile(r"([+-])(\d{2}):?(\d{2})$")
_VERSION_DATE = re.compile(r"^(?P<year>\d{4})\.(?P<monthday>\d{1,4})\.")
_VERSION_DIGITS = re.compile(r"[^0-9.]")

# `2026.1009.0-g0v0`: the shape the schema accepts, and the shape a version has to have
# for its date to be readable.
VERSION = re.compile(r"^[0-9]{4}\.[0-9]{3,4}\.[0-9]+(?:-[0-9A-Za-z._-]+)?$")
# `20261028-some-title`, the name of a post.
SLUG = re.compile(r"^(?P<date>[0-9]{8})-[a-z0-9-]+$")


def parse_scalar(value: str) -> Any:
    """Read one front matter value, as the server reads it.

    Quoted strings, inline lists, booleans, nulls and integers, and everything else as
    text. A front matter this reads differently from the server would pass here and be
    refused there.
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

    Flat keys only, and a key with an empty value is left out rather than stored as an
    empty string.
    """
    match = _FRONT_MATTER.match(markdown.lstrip("\ufeff"))
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


def split_markdown(markdown: str) -> tuple[dict[str, Any], str]:
    """Split a post into its front matter and the body written under it."""
    text = markdown.lstrip("\ufeff")
    match = _FRONT_MATTER.match(text)
    body = text[match.end() :] if match is not None else text
    return split_front_matter(text), body.lstrip("\r\n")


def render_scalar(value: Any) -> str:
    """Write one front matter value so that `parse_scalar` reads it back unchanged.

    The round trip is the whole rule: a value `parse_scalar` would read as something else
    -- `2026` as an integer, `true` as a boolean, `[a]` as a list -- is quoted, and a
    value it reads as itself is written bare. Nothing here decides what a value means.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)

    text = str(value)
    if not text.strip():
        raise ValueError("a front matter value cannot be empty; leave the key out instead")
    if "\n" in text or "\r" in text:
        raise ValueError(f"{text!r} spans lines, which a flat front matter cannot hold")
    if parse_scalar(text) == text:
        return text
    if '"' not in text:
        return f'"{text}"'
    if "'" not in text:
        return f"'{text}'"
    raise ValueError(f"{text!r} holds both kinds of quote, which this writer will not guess at")


def render_front_matter(values: Mapping[str, Any], order: Iterable[str]) -> str:
    """Write a front matter block, with the keys in the order a reader expects.

    A key the order does not name is refused rather than appended: it is either a typo or
    a field the schema has not been taught yet, and both are worth stopping for.
    """
    order = list(order)
    unknown = [key for key in values if key not in order]
    if unknown:
        raise ValueError(f"unknown front matter key(s): {', '.join(sorted(unknown))}")

    lines = ["---"]
    for key in order:
        value = values.get(key)
        if value is None:
            continue
        lines.append(f"{key}: {render_scalar(value)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def parse_timestamp(value: object) -> datetime | None:
    """Read a timestamp, as the server reads it: without an offset it is UTC."""
    if not isinstance(value, str):
        return None

    text = re.sub(r"\s+", " ", value.strip())
    if not text:
        return None

    text = text.replace(" ", "T", 1).replace(" ", "")
    text = _TIMESTAMP_OFFSET.sub(r"\1\2:\3", text)
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def format_timestamp(moment: datetime) -> str:
    """Write a timestamp in the shape the other files use, in UTC."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S +0000")


def date_from_version(version: str) -> datetime | None:
    """Read the release date a version carries, as the server reads it."""
    match = _VERSION_DATE.match(version)
    if match is None:
        return None

    monthday = match.group("monthday").zfill(4)
    try:
        return datetime(int(match.group("year")), int(monthday[:2]), int(monthday[2:]), tzinfo=UTC)
    except ValueError:
        return None


def build_id(version: str) -> int:
    """Derive the identifier of a build, as the server derives it."""
    digits = _VERSION_DIGITS.sub("", version.split("-", 1)[0]).replace(".", "")
    return int(digits) if digits.isdigit() else zlib.crc32(version.encode())


def version_suffix(version: str) -> str:
    """The stream a version names, that is what follows the first dash.

    A version has at most one suffix: `2026.1009.0-g0v0` names the stream `g0v0`, and
    `2026.1009.0` names none.
    """
    return version.partition("-")[2]


def display_version(version: str) -> str:
    """What the client shows for a version: the suffix and every non-digit dropped."""
    return re.sub(r"[^0-9.]", "", version.partition("-")[0]).strip(".")


def slugify(title: str) -> str:
    """Turn a title into the tail of a file name, as far as that is possible.

    A title in a script without an ASCII reading -- the posts here are written in Chinese
    as often as in English -- leaves nothing behind, which is a blank rather than an
    error: the caller is expected to ask for a slug instead of inventing one.
    """
    lowered = title.lower()
    kept = [character if character.isalnum() and character.isascii() else "-" for character in lowered]
    return re.sub(r"-{2,}", "-", "".join(kept)).strip("-")
