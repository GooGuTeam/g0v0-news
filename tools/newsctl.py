"""The command line for maintaining this repository.

    uv run python tools/newsctl.py status
    uv run python tools/newsctl.py draft --repository GooGuTeam/g0v0-server --release 2026.1009.0-g0v0
    uv run python tools/newsctl.py draft --blank --version 2026.1030.0-g0v0
    uv run python tools/newsctl.py entry 2026.1030.0-g0v0 --title "Spectator rooms stay open"
    uv run python tools/newsctl.py show 2026.1009.0-g0v0
    uv run python tools/newsctl.py publish 2026.1009.0-g0v0
    uv run python tools/newsctl.py serve

A draft can start from a source or from nothing: `--blank` opens one with no entries, and
`entry` adds them one at a time. Both routes end in the same file, which is reviewed the
same way, so a change nobody published notes for is not a second-class citizen.

Nothing here publishes on its own: a draft is written, the entries somebody did not want
are dropped -- in a text editor or in the console `serve` opens -- and only then is a
build file written. The step this tool cannot take for you is deciding what a release
means to a player, so it stops there on purpose.
"""

import argparse
from datetime import UTC, datetime
import sys
import unicodedata

import content
import formats
import sources
import validate


def _width(text: str) -> int:
    """How wide a string prints, counting a wide character as the two cells it takes."""
    return sum(2 if unicodedata.east_asian_width(character) in "WF" else 1 for character in text)


def _table(headers: list[str], rows: list[list[str]]) -> str:
    """Align a table by what the terminal shows rather than by character count."""
    if not rows:
        return "  ".join(headers)
    widths = [max(_width(row[column]) for row in [headers, *rows]) for column in range(len(headers))]
    lines = []
    for index, row in enumerate([headers, *rows]):
        cells = [cell + " " * (width - _width(cell)) for cell, width in zip(row, widths)]
        lines.append("  ".join(cells).rstrip())
        if index == 0:
            lines.append("  ".join("-" * width for width in widths))
    return "\n".join(lines)


def _confirm(question: str) -> bool:
    """Ask, and take silence for no: this runs in terminals and in scripts alike."""
    if not sys.stdin.isatty():
        print("not a terminal, so nothing was confirmed; pass --yes to go ahead", file=sys.stderr)
        return False
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


# -- commands ------------------------------------------------------------------------


def status() -> int:
    """What is in the tree, in one screen."""
    trees = content.streams()
    for stream in trees:
        builds = [build for build in content.builds() if build.stream == stream["name"]]
        flags = []
        if stream.get("is_featured"):
            flags.append("featured")
        if stream.get("undeclared"):
            flags.append("not in streams.json")
        print(f"{stream['name']}{'  (' + ', '.join(flags) + ')' if flags else ''}")
        if not builds:
            print("  no build")
            continue
        rows = [
            [
                build.version,
                build.display,
                build.published.strftime("%Y-%m-%d") if build.published else "-",
                str(len(build.entries)),
                str(build.document.get("users", 0)),
            ]
            for build in builds
        ]
        table = _table(["version", "shown as", "released", "entries", "users"], rows)
        print("\n".join("  " + line for line in table.splitlines()))
        print()

    pending = content.drafts()
    print(f"drafts: {len(pending)}")
    for draft in pending:
        print(
            f"  {draft.name}  {draft.version}  {len(draft.kept())} kept, {len(draft.dropped())} dropped, "
            f"from {len(draft.sources)} source(s)"
        )

    written = content.posts()
    print(f"\nposts: {len(written)}")
    for post in written:
        moment = post.published.strftime("%Y-%m-%d") if post.published else "-"
        print(f"  {moment}  {post.front_matter.get('title', post.slug)}")

    report = validate.check()
    print(f"\nchecks: {len(report.errors)} error(s), {len(report.warnings)} warning(s)")
    return 0


def check() -> int:
    """Run the same check CI runs, and say what it found."""
    report = validate.check()
    for message in report.errors:
        print(f"error    {message}")
    for message in report.warnings:
        print(f"warning  {message}")
    print(f"\n{len(report.errors)} error(s), {len(report.warnings)} warning(s)")
    return 0 if not report.errors else 1


def draft(arguments: argparse.Namespace) -> int:
    """Read a source into a draft, and print what came back."""
    if arguments.blank:
        return start_blank(arguments)

    collection = sources.collect(
        repository=arguments.repository,
        release=arguments.release,
        commit_range=arguments.range,
        repo_path=arguments.repo_path,
        first_parent=arguments.first_parent,
        everything=arguments.everything,
        limit=arguments.limit,
    )

    version = arguments.version or collection.version
    if not version:
        print(
            "error: this source does not name a version; pass --version, for example 2026.1029.0-g0v0",
            file=sys.stderr,
        )
        return 1
    stream = arguments.stream or formats.version_suffix(version)
    if not stream:
        print("error: the version carries no stream suffix; pass --stream, for example g0v0", file=sys.stderr)
        return 1

    path = content.draft_path(version)
    if path.exists() and not (arguments.force or arguments.append):
        print(f"error: {content.relative(path)} already exists; pass --force or --append", file=sys.stderr)
        return 1

    if path.exists() and arguments.append:
        made = content.append_to_draft(content.load_draft(path), entries=collection.items(), source=collection.source)
        content.save_draft(made)
        print(f"{content.relative(made.path)}: appended {len(made.entries)} entry(s) in total for {version}")
    else:
        made = content.new_draft(
            version=version,
            stream=stream,
            entries=collection.items(),
            sources=[collection.source],
            created_at=arguments.created_at or collection.created_at,
            users=arguments.users,
        )
        content.save_draft(made)
        print(f"{content.relative(made.path)}: {len(made.entries)} candidate(s) for {version}, stream `{stream}`")
    rows = [
        [
            str(number),
            str(item["entry"].get("type", "")),
            str(item["entry"].get("category", "")),
            str(item["entry"].get("title") or item["entry"].get("message", ""))[:72],
        ]
        for number, item in enumerate(made.entries, start=1)
    ]
    print(_table(["#", "type", "category", "title"], rows))
    print("\nnext: drop what nobody needs (`newsctl show` to read it again), then `newsctl publish " + version + "`")
    return 0


def start_blank(arguments: argparse.Namespace) -> int:
    """Open a draft with nothing in it, for a build nobody wrote notes for.

    Half of what lands in a build never had a release body: an outage, a decision, a fix
    that shipped quietly. Making those go through a source would have meant writing a
    fake one, so they get their own way in -- and the same file, review and publish.
    """
    if arguments.repository or arguments.release or arguments.range or arguments.repo_path:
        print("error: --blank starts from nothing, so it takes no source", file=sys.stderr)
        return 1
    if not arguments.version:
        print("error: --blank needs --version, for example 2026.1030.0-g0v0", file=sys.stderr)
        return 1

    version = arguments.version
    content.ensure_version(version)
    stream = arguments.stream or formats.version_suffix(version)
    if not stream:
        print("error: the version carries no stream suffix; pass --stream, for example g0v0", file=sys.stderr)
        return 1

    path = content.draft_path(version)
    if path.exists() and not arguments.force:
        print(f"error: {content.relative(path)} already exists; pass --force to replace it", file=sys.stderr)
        return 1

    made = content.blank_draft(
        version=version,
        stream=stream,
        created_at=arguments.created_at,
        users=arguments.users,
    )
    content.save_draft(made)
    print(f"{content.relative(made.path)}: an empty draft for {version}, stream `{stream}`")
    print(f'next: `newsctl entry {version} --title "..."`, or `newsctl serve` to write it in the console')
    return 0


def entry(arguments: argparse.Namespace) -> int:
    """Add one entry to a draft, written rather than read from a source."""
    made = content.load_draft(arguments.draft)
    candidate: dict[str, object] = {"type": arguments.kind}
    for key, value in (
        ("title", arguments.title),
        ("message", arguments.message),
        ("category", arguments.category),
        ("url", arguments.url),
        ("id", arguments.identifier),
        ("major", True if arguments.major else None),
    ):
        if value not in (None, ""):
            candidate[key] = value

    content.add_entry(made, candidate)
    content.save_draft(made)
    number = len(made.entries)
    summary = str(candidate.get("title") or candidate.get("message"))
    print(f"{content.relative(made.path)}: entry {number} — {arguments.kind} / {summary}")
    print(f"next: `newsctl show {made.version}` to read it in place, or `newsctl publish {made.version}`")
    return 0


def drafts() -> int:
    """The drafts waiting for a decision."""
    pending = content.drafts()
    if not pending:
        print("no draft is waiting")
        return 0
    rows = [
        [
            item.name,
            item.version,
            item.stream,
            str(len(item.kept())),
            str(len(item.dropped())),
            str(item.document.get("generated_at", "")),
        ]
        for item in pending
    ]
    print(_table(["file", "version", "stream", "kept", "dropped", "generated"], rows))
    return 0


def show(arguments: argparse.Namespace) -> int:
    """Read one draft, entry by entry."""
    made = content.load_draft(arguments.draft)
    print(f"{made.name}: {made.version} in stream `{made.stream}`")
    for source in made.sources:
        print(f"  from {source.get('kind')} {source.get('repository')} @ {source.get('reference')}")
    if made.document.get("created_at"):
        print(f"  released {made.document['created_at']}")

    for number, item in enumerate(made.entries, start=1):
        entry = item.get("entry", {})
        state = "keep" if item.get("status", "keep") == "keep" else "drop"
        print(
            f"\n{number:>2}. [{state}] {entry.get('type', '?')} / {entry.get('category', 'Miscellaneous')}"
            + ("  (major)" if entry.get("major") else "")
        )
        print(f"    {entry.get('title') or entry.get('message', '')}")
        if entry.get("message") and entry.get("title"):
            print(f"    {entry['message']}")
        if entry.get("url"):
            print(f"    {entry['url']}")
        provenance = item.get("provenance") or {}
        origin = provenance.get("commit") or provenance.get("pull_request") or provenance.get("author")
        if origin:
            print(f"    from {origin}")
    return 0


def publish(arguments: argparse.Namespace) -> int:
    """Write the build a draft describes, once somebody has said yes."""
    made = content.load_draft(arguments.draft)
    kept = made.kept()
    if not kept:
        reason = "this draft has no entry yet" if not made.entries else "every entry was dropped"
        print(f"error: {reason}, so there is no build to write", file=sys.stderr)
        return 1

    print(f"{made.version} in stream `{made.stream}`: {len(kept)} of {len(made.entries)} entrie(s) kept")
    for item in kept:
        print(f"  {item['entry'].get('type', '?')}  {item['entry'].get('title') or item['entry'].get('message')}")

    target = content.CHANGELOG / made.stream / f"{made.version}.json"
    if not arguments.yes and not _confirm(f"write {content.relative(target)}?"):
        print("nothing was written")
        return 1

    path = content.publish(made, force=arguments.force)
    print(f"wrote {content.relative(path)}")
    print("next: `uv run python tools/validate.py`, then read the diff before committing")
    return 0


def post_new(arguments: argparse.Namespace) -> int:
    """Start a post, with the name the listing will use."""
    moment = datetime.now(UTC) if not arguments.date else formats.parse_timestamp(arguments.date)
    if arguments.date and moment is None:
        print(f"error: {arguments.date!r} is not a date this repository reads", file=sys.stderr)
        return 1

    made = content.create_post(
        title=arguments.title,
        author=arguments.author,
        date=moment,
        slug=arguments.slug,
        series=arguments.series,
        body=arguments.body or "",
    )
    print(f"wrote {made.name}")
    print("next: write the body, then `uv run python tools/validate.py`")
    return 0


def post_list() -> int:
    """The posts that exist, newest first."""
    written = content.posts()
    if not written:
        print("no post is written yet")
        return 0
    rows = [
        [
            post.published.strftime("%Y-%m-%d") if post.published else "-",
            post.name,
            str(post.front_matter.get("series", "")),
            str(post.front_matter.get("title", "")),
        ]
        for post in written
    ]
    print(_table(["date", "file", "series", "title"], rows))
    return 0


def serve(arguments: argparse.Namespace) -> int:
    """Open the console, which is the same tools with a page in front of them."""
    import console

    return console.run(host=arguments.host, port=arguments.port, open_browser=not arguments.no_open)


# -- the parser ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="newsctl.py",
        description=(
            "Maintain the changelog and news trees, and draft a build from a release, a range of commits, or by hand."
        ),
    )
    commands = parser.add_subparsers(dest="command", metavar="<command>")

    commands.add_parser("status", help="what is in the tree, and what the checker says about it").set_defaults(
        run=lambda _arguments: status()
    )
    commands.add_parser("check", help="run the same check CI runs").set_defaults(run=lambda _arguments: check())
    commands.add_parser("drafts", help="the drafts waiting for a decision").set_defaults(
        run=lambda _arguments: drafts()
    )
    commands.add_parser("posts", help="the posts that exist").set_defaults(run=lambda _arguments: post_list())

    make_draft = commands.add_parser("draft", help="read a release or a range of commits into a draft")
    make_draft.add_argument(
        "--blank", action="store_true", help="start an empty draft instead of reading a source, then fill it in by hand"
    )
    make_draft.add_argument("--repository", "-r", help="owner/name of the repository the changes landed in")
    make_draft.add_argument("--release", help="tag of a published release to read; GITHUB_API_URL points it elsewhere")
    make_draft.add_argument("--range", help="commits to read, as `base..head`")
    make_draft.add_argument("--repo-path", help="a clone to read the range from, instead of the API")
    make_draft.add_argument("--first-parent", action="store_true", help="walk only the mainline of the clone")
    make_draft.add_argument("--everything", action="store_true", help="keep chores, docs and dependency bumps")
    make_draft.add_argument("--limit", type=int, help="stop after this many candidates")
    make_draft.add_argument("--version", help="version of the build; defaults to the release tag when it is one")
    make_draft.add_argument("--stream", help="stream the build belongs to; defaults to the version suffix")
    make_draft.add_argument("--created-at", dest="created_at", help="release time, when the source does not say")
    make_draft.add_argument("--users", type=int, help="users running the build, when that is known")
    make_draft.add_argument("--force", action="store_true", help="replace a draft of the same version")
    make_draft.add_argument("--append", action="store_true", help="add to a draft of the same version instead")
    make_draft.set_defaults(run=draft)

    read_draft = commands.add_parser("show", help="read one draft")
    read_draft.add_argument("draft", help="the version the draft is named after, or its path")
    read_draft.set_defaults(run=show)

    write_entry = commands.add_parser("entry", help="add one entry to a draft, written by hand")
    write_entry.add_argument("draft", help="the version the draft is named after, or its path")
    write_entry.add_argument(
        "--type",
        dest="kind",
        choices=("add", "fix", "misc"),
        default="add",
        help="how the client marks the change (default: add)",
    )
    write_entry.add_argument("--title", help="one line, the thing a reader reads")
    write_entry.add_argument("--message", help="Markdown body shown under the title")
    write_entry.add_argument("--category", help="group heading it is listed under; defaults to Miscellaneous")
    write_entry.add_argument("--url", help="where it can be read, when that is not a pull request")
    write_entry.add_argument("--id", type=int, dest="identifier", help="identifier, when one has to be fixed")
    write_entry.add_argument("--major", action="store_true", help="mark it as notable in the client")
    write_entry.set_defaults(run=entry)

    write_build = commands.add_parser("publish", help="write the build a draft describes")
    write_build.add_argument("draft", help="the version the draft is named after, or its path")
    write_build.add_argument("--force", action="store_true", help="replace an existing build file")
    write_build.add_argument("--yes", action="store_true", help="do not ask")
    write_build.set_defaults(run=publish)

    posts = commands.add_parser("post", help="start a post")
    posts_commands = posts.add_subparsers(dest="post_command", metavar="<command>")
    new_post = posts_commands.add_parser("new", help="write a new post's front matter and file")
    new_post.add_argument("--title", required=True)
    new_post.add_argument("--author", required=True)
    new_post.add_argument("--slug", help="the tail of the file name; derived from the title when it is ASCII")
    new_post.add_argument("--date", help="publication time; defaults to now")
    new_post.add_argument("--series")
    new_post.add_argument("--body", help="the Markdown body, if it is already written")
    new_post.set_defaults(run=post_new)
    posts.set_defaults(run=lambda _arguments: posts.print_help() or 1)

    console = commands.add_parser("serve", help="open the console in a browser")
    console.add_argument("--host", default="127.0.0.1")
    console.add_argument("--port", type=int, default=8765)
    console.add_argument("--no-open", action="store_true", help="print the address instead of opening it")
    console.set_defaults(run=serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if not getattr(arguments, "run", None):
        parser.print_help()
        return 1
    try:
        return int(arguments.run(arguments) or 0)
    except content.ContentError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
