"""Reading the schemas, and turning a failure into a line somebody can act on.

The schemas are the contract the server also enforces, so they are read from the files
next to the content rather than restated in code: adding a field means editing a schema,
not a script. Both the checker and the writer of this repository come through here, which
is what keeps "it validates" and "it is written correctly" the same statement.
"""

from functools import cache
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker, exceptions

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent


@cache
def read_schema(path: str | Path) -> Draft202012Validator:
    """Read a schema, and refuse a file that is not a valid schema itself."""
    schema: dict[str, Any] = json.loads(Path(path).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def describe(error: exceptions.ValidationError) -> str:
    """Render one failure as `/<field>: reason`.

    `anyOf` and `oneOf` only report that no branch matched, which says nothing about what
    is actually wrong; the branch that got furthest says it, so that is the one shown.
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


def load_json(path: Path) -> Any:
    """Read a JSON file, letting the error say which file it was."""
    return json.loads(path.read_text(encoding="utf-8"))


def render_json(document: Any, order: list[str] | tuple[str, ...] | None = None) -> str:
    """Write JSON the way the files here are written: two spaces, UTF-8, one trailing LF.

    `order` fixes the key order, so that a file written by a tool and a file edited by
    hand stay comparable line for line. Keys the order does not name follow in the order
    they were in, which keeps a document the order was not taught about writable.
    """
    if order is not None and isinstance(document, dict):
        document = ordered(document, order)
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def ordered(document: dict[str, Any], order: list[str] | tuple[str, ...]) -> dict[str, Any]:
    """The same mapping, with the keys the order names first and in that order."""
    result = {key: document[key] for key in order if key in document}
    result.update((key, value) for key, value in document.items() if key not in result)
    return result
