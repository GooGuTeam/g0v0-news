"""The same tools as `newsctl.py`, with a page in front of them.

A single-threaded-per-request server out of the standard library, and one HTML file with no
build step, so that the console is something you run -- `uv run python tools/newsctl.py
serve` -- rather than something to install. It binds to `127.0.0.1` and has no
authentication, because it is a tool for the person at this keyboard: it writes into the
working tree, and a checkout is the only thing it should be able to reach.

Every path a request names is resolved through `content.resolve`, so the page can ask for
`news/2026/whatever.md` and not for `C:/Windows`.
"""

import argparse
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from typing import Any
from urllib.parse import ParseResult, parse_qs, unquote, urlparse
import webbrowser

import content
import formats
import sources
import validate

STATIC = Path(__file__).resolve().parent / "static"
PAGE = STATIC / "index.html"
LIMIT = 8 * 1024 * 1024


class BadRequestError(Exception):
    """A request this server will not carry out, with the reason to show."""


# -- the API -------------------------------------------------------------------------


def overview() -> dict[str, Any]:
    """Everything the first screen shows, in one round trip."""
    report = validate.check()
    by_stream: dict[str, list[dict[str, Any]]] = {}
    for build in content.builds():
        by_stream.setdefault(build.stream, []).append(
            {
                "version": build.version,
                "path": build.name,
                "display": build.display,
                "released": build.published.strftime("%Y-%m-%d") if build.published else None,
                "entries": len(build.entries),
                "users": build.document.get("users", 0),
                "major": sum(1 for entry in build.entries if entry.get("major")),
            }
        )

    streams = [
        {
            "name": stream["name"],
            "display_name": stream.get("display_name", stream["name"]),
            "is_featured": bool(stream.get("is_featured")),
            "undeclared": bool(stream.get("undeclared")),
            "builds": by_stream.get(stream["name"], []),
        }
        for stream in content.streams()
    ]

    return {
        "repository": content.ROOT.as_posix(),
        "streams": streams,
        "drafts": [
            {
                "path": draft.name,
                "version": draft.version,
                "stream": draft.stream,
                "kept": len(draft.kept()),
                "dropped": len(draft.dropped()),
                "generated_at": draft.document.get("generated_at"),
                "sources": [
                    f"{source.get('kind')} {source.get('repository')}@{source.get('reference')}"
                    for source in draft.sources
                ],
            }
            for draft in content.drafts()
        ],
        "posts": [
            {
                "path": post.name,
                "slug": post.slug,
                "title": post.front_matter.get("title"),
                "author": post.front_matter.get("author"),
                "date": post.front_matter.get("date"),
                "series": post.front_matter.get("series"),
            }
            for post in content.posts()
        ],
        "checks": {"errors": report.errors, "warnings": report.warnings},
    }


def draft_create(body: dict[str, Any]) -> dict[str, Any]:
    """Open an empty draft: the way in when nobody wrote notes for a build.

    The same file the extractor writes, so a hand-written build is reviewed, ordered and
    published by the same code -- it just starts with no entry instead of a candidate per
    pull request.
    """
    version = _text(body, "version") or ""
    content.ensure_version(version)
    stream = _text(body, "stream") or formats.version_suffix(version)
    if not stream:
        raise BadRequestError("the version carries no stream suffix; fill in the stream field")
    if content.draft_path(version).exists():
        raise BadRequestError(f"drafts/{version}.json already exists; open it instead of starting over")

    made = content.blank_draft(
        version=version,
        stream=stream,
        created_at=_text(body, "created_at"),
        users=int(body["users"]) if body.get("users") else None,
    )
    content.save_draft(made)
    return {"path": made.name, "version": version, "stream": stream, "entries": 0}


def draft_get(query: dict[str, list[str]]) -> dict[str, Any]:
    draft = content.load_draft(_one(query, "path"))
    return {"path": draft.name, "document": draft.document}


def draft_put(query: dict[str, list[str]], body: dict[str, Any]) -> dict[str, Any]:
    path = content.resolve(_one(query, "path"), under=content.DRAFTS)
    saved = content.save_draft(content.Draft(path=path, document=body))
    return {"path": content.relative(saved), "saved": True}


def draft_delete(query: dict[str, list[str]]) -> dict[str, Any]:
    draft = content.load_draft(_one(query, "path"))
    content.delete_draft(draft)
    return {"deleted": draft.name}


def extract(body: dict[str, Any]) -> dict[str, Any]:
    """Read a source and leave a draft behind. The whole point of the console."""
    collection = sources.collect(
        repository=_text(body, "repository"),
        release=_text(body, "release"),
        commit_range=_text(body, "range"),
        repo_path=_text(body, "repo_path"),
        first_parent=bool(body.get("first_parent")),
        everything=bool(body.get("everything")),
        limit=int(body["limit"]) if body.get("limit") else None,
    )

    version = _text(body, "version") or collection.version
    if not version:
        raise BadRequestError("this source does not name a version; fill in the version field")
    stream = _text(body, "stream") or formats.version_suffix(version)
    if not stream:
        raise BadRequestError("the version carries no stream suffix; fill in the stream field")

    path = content.draft_path(version)
    mode = _text(body, "mode") or "new"
    if mode not in ("new", "replace", "append"):
        raise BadRequestError(f"`{mode}` is not a way to read a source; use new, replace or append")

    if path.exists() and mode == "append":
        made = content.append_to_draft(content.load_draft(path), entries=collection.items(), source=collection.source)
        content.save_draft(made)
        return {"path": made.name, "version": version, "stream": made.stream, "entries": len(made.entries)}
    if path.exists() and mode == "new":
        raise BadRequestError(f"drafts/{version}.json already exists; read it in as a replace or an append")

    made = content.new_draft(
        version=version,
        stream=stream,
        entries=collection.items(),
        sources=[collection.source],
        created_at=_text(body, "created_at") or collection.created_at,
        users=int(body["users"]) if body.get("users") else None,
    )
    content.save_draft(made)
    return {"path": made.name, "version": version, "stream": stream, "entries": len(made.entries)}


def publish(body: dict[str, Any]) -> dict[str, Any]:
    draft = content.load_draft(_text(body, "path") or "")
    path = content.publish(draft, force=bool(body.get("force")))
    return {"path": content.relative(path)}


def build_get(query: dict[str, list[str]]) -> dict[str, Any]:
    path = content.resolve(_one(query, "path"), under=content.CHANGELOG)
    if not path.is_file():
        raise BadRequestError(f"{content.relative(path)} does not exist")
    return {"path": content.relative(path), "document": json.loads(path.read_text(encoding="utf-8"))}


def build_put(query: dict[str, list[str]], body: dict[str, Any]) -> dict[str, Any]:
    path = content.resolve(_one(query, "path"), under=content.CHANGELOG)
    if not path.is_file():
        raise BadRequestError(f"{content.relative(path)} does not exist; a draft is published, not typed in")
    if str(body.get("version", path.stem)) != path.stem:
        raise BadRequestError("a build is named after its version, so renaming one is a file move, not an edit")
    written = content.write_build(body, path.parent.name, force=True)
    return {"path": content.relative(written), "saved": True}


def post_get(query: dict[str, list[str]]) -> dict[str, Any]:
    post = content.load_post(_one(query, "path"))
    return {"path": post.name, "front_matter": post.front_matter, "body": post.body}


def post_put(query: dict[str, list[str]], body: dict[str, Any]) -> dict[str, Any]:
    path = content.resolve(_one(query, "path"), under=content.NEWS)
    front_matter = body.get("front_matter")
    if not isinstance(front_matter, dict):
        raise BadRequestError("front_matter has to be an object")
    content.save_post(path, front_matter, str(body.get("body", "")))
    return {"path": content.relative(path), "saved": True}


def post_create(body: dict[str, Any]) -> dict[str, Any]:
    date = _text(body, "date")
    moment = formats.parse_timestamp(date) if date else None
    if date and moment is None:
        raise BadRequestError(f"{date!r} is not a date this repository reads")
    post = content.create_post(
        title=_text(body, "title") or "",
        author=_text(body, "author") or "",
        date=moment,
        slug=_text(body, "slug"),
        series=_text(body, "series"),
        body=str(body.get("body", "")),
    )
    return {"path": post.name}


def check() -> dict[str, Any]:
    report = validate.check()
    return {"errors": report.errors, "warnings": report.warnings}


def _one(query: dict[str, list[str]], name: str) -> str:
    values = query.get(name)
    if not values or not values[0]:
        raise BadRequestError(f"`{name}` has to be given")
    return values[0]


def _text(body: dict[str, Any], key: str) -> str | None:
    value = body.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


# A read route is handed the query the request carried; a write route is handed the query and
# the body. The aliases are what the lambdas below get checked against, which is why `_query`
# can be left unnamed on a route that only writes.
Query = dict[str, list[str]]
Payload = dict[str, Any]
ReadRoute = Callable[[Query], Payload]
WriteRoute = Callable[[Query, Payload], Payload]

GET: dict[str, ReadRoute] = {
    "/api/overview": lambda _query: overview(),
    "/api/draft": draft_get,
    "/api/build": build_get,
    "/api/post": post_get,
    "/api/check": lambda _query: check(),
}

POST: dict[str, WriteRoute] = {
    "/api/draft": lambda _query, body: draft_create(body),
    "/api/extract": lambda _query, body: extract(body),
    "/api/publish": lambda _query, body: publish(body),
    "/api/post": lambda _query, body: post_create(body),
}

PUT: dict[str, WriteRoute] = {
    "/api/draft": draft_put,
    "/api/build": build_put,
    "/api/post": post_put,
}

DELETE: dict[str, ReadRoute] = {"/api/draft": lambda query: draft_delete(query)}


# -- the server ----------------------------------------------------------------------


class Console(BaseHTTPRequestHandler):
    server_version = "g0v0-news-console"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            return self._page()
        self._read(GET, parsed)

    def do_DELETE(self) -> None:
        self._read(DELETE, urlparse(self.path))

    def do_POST(self) -> None:
        self._write(POST, urlparse(self.path))

    def do_PUT(self) -> None:
        self._write(PUT, urlparse(self.path))

    def _read(self, routes: dict[str, ReadRoute], parsed: ParseResult) -> None:
        route = routes.get(parsed.path)
        if route is None:
            return self._json({"error": f"no route for {parsed.path}"}, status=404)
        self._answer(lambda: route(parse_qs(parsed.query)))

    def _write(self, routes: dict[str, WriteRoute], parsed: ParseResult) -> None:
        route = routes.get(parsed.path)
        if route is None:
            return self._json({"error": f"no route for {parsed.path}"}, status=404)
        self._answer(lambda: route(parse_qs(parsed.query), self._read_json()))

    def _answer(self, call: Callable[[], Payload]) -> None:
        """Run one route, and turn the ways it can refuse into the status that says so."""
        try:
            self._json(call())
        except (content.ContentError, BadRequestError) as error:
            self._json({"error": str(error)}, status=400)
        except json.JSONDecodeError as error:
            self._json({"error": f"the body is not JSON: {error}"}, status=400)
        except OSError as error:
            self._json({"error": str(error)}, status=500)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > LIMIT:
            raise BadRequestError("that body is larger than this console will read")
        raw = self.rfile.read(length)
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise BadRequestError("the body has to be an object")
        return value

    def _json(self, payload: dict[str, Any], *, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if status != 200:
            # A refused request may not have had its body read, and what is left of it
            # would be parsed as the next request on a kept-alive connection.
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def _page(self) -> None:
        try:
            body = PAGE.read_bytes()
        except OSError:
            return self._json({"error": f"{PAGE} is missing"}, status=500)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # The parameter names are the base class's, which is what makes this an override rather
    # than a method that happens to be named the same; `format` is unused on purpose.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: ARG002
        """One line per request, without the address this console already knows."""
        print(f"{self.command} {unquote(self.path)} -> {args[1]}")


def run(host: str = "127.0.0.1", port: int = 8765, *, open_browser: bool = True) -> int:
    """Serve the console until interrupted."""
    server = ThreadingHTTPServer((host, port), Console)
    address = f"http://{host}:{port}/"
    print(f"console on {address} -- writing into {content.ROOT}")
    print("the page has no login: it is bound to this machine, and that is the whole guard")
    if open_browser:
        webbrowser.open(address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve the content console.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true", help="print the address instead of opening it")
    arguments = parser.parse_args(argv)
    return run(host=arguments.host, port=arguments.port, open_browser=not arguments.no_open)


if __name__ == "__main__":
    raise SystemExit(main())
