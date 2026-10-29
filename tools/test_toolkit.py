"""Tests for the maintenance tools, run against a copy of this repository.

    uv run python tools/test_toolkit.py

The parsing is tested on strings, because that is where the judgement is: which bullet is
a change, which commit is noise, what a value looks like when it is written back. The
publishing is tested on a temporary copy of the repository through the command line, so
that what is tested is what a person runs, and so that a broken test cannot write into the
working tree.
"""

from datetime import UTC, datetime
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any
import unittest

import content
import formats
import sources

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent

IGNORED = shutil.ignore_patterns(".git", ".venv", ".idea", ".vscode", "drafts", "__pycache__")


class ReleaseTests(unittest.TestCase):
    """A release body, which somebody already wrote for a reader."""

    def test_generated_notes(self):
        body = (
            "## What's Changed\n"
            "* Fix the wiki redirect by @someone in https://github.com/o/r/pull/501\n"
            "* feat(wiki): serve ja articles by @other in https://github.com/o/r/pull/498\n"
            "\n## New Contributors\n"
            "* @newbie made their first contribution in https://github.com/o/r/pull/500\n"
            "\n**Full Changelog**: https://github.com/o/r/compare/a...b\n"
        )
        candidates = sources.release_candidates(body, repository="o/r", tag="2026.1009.0")
        self.assertEqual([item.entry["title"] for item in candidates], ["Fix the wiki redirect", "Serve ja articles"])

        first = candidates[0]
        self.assertEqual(first.entry["url"], "https://github.com/o/r/pull/501")
        self.assertEqual(first.entry["github_pull_request_id"], 501)
        self.assertEqual(first.entry["github_user"]["github_username"], "someone")
        self.assertNotIn("category", first.entry, "`What's Changed` names the list, not a group")

        second = candidates[1]
        self.assertEqual(second.entry["type"], "add")
        self.assertEqual(second.entry["category"], "Wiki")

    def test_sections_decide_the_type(self):
        body = "## Bug Fixes\n* The room is dropped\n\n### Breaking changes\n* The `/api/v1` prefix is gone\n"
        candidates = sources.release_candidates(body, repository="o/r", tag="t")
        self.assertEqual(candidates[0].entry["type"], "fix")
        self.assertTrue(candidates[1].entry.get("major"))
        self.assertEqual(candidates[1].entry["title"], "The `/api/v1` prefix is gone")

    def test_a_pull_request_is_read_once(self):
        body = (
            "* Fix it by @a in https://github.com/o/r/pull/7\n* Fix it again by @b in https://github.com/o/r/pull/7\n"
        )
        candidates = sources.release_candidates(body, repository="o/r", tag="t")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].provenance["commits"], 2)

    def test_backticks_survive(self):
        body = "* `!mp` commands echo twice\n"
        candidates = sources.release_candidates(body, repository="o/r", tag="t")
        self.assertEqual(candidates[0].entry["title"], "`!mp` commands echo twice")


class CommitTests(unittest.TestCase):
    """Commits, which nobody wrote for a reader."""

    def test_prefixes_decide_the_entry(self):
        commits = [
            sources.Commit(sha="1" * 40, subject="feat(api): add a route", body="Body line.\n\nRefs: x"),
            sources.Commit(sha="2" * 40, subject="chore(deps): bump jsonschema"),
            sources.Commit(sha="3" * 40, subject="Merge pull request #4 from a/b"),
            sources.Commit(sha="4" * 40, subject="fix!: drop the old prefix"),
            sources.Commit(sha="5" * 40, subject="plain work", author="Ann"),
        ]
        candidates = sources.commit_candidates(commits, repository="o/r", reference="HEAD")
        self.assertEqual(
            [item.entry["title"] for item in candidates], ["Add a route", "Drop the old prefix", "Plain work"]
        )

        self.assertEqual(candidates[0].entry["category"], "API")
        self.assertEqual(candidates[0].entry["message"], "Body line.")
        self.assertTrue(candidates[1].entry["major"])
        self.assertEqual(candidates[2].entry["github_user"], {"display_name": "Ann"})

    def test_everything_keeps_the_noise(self):
        commits = [sources.Commit(sha="2" * 40, subject="chore(deps): bump jsonschema")]
        self.assertEqual(sources.commit_candidates(commits, repository="o/r", reference="HEAD"), [])
        self.assertEqual(
            len(sources.commit_candidates(commits, repository="o/r", reference="HEAD", everything=True)), 1
        )

    def test_one_pull_request_is_one_entry(self):
        commits = [
            sources.Commit(sha="1" * 40, subject="feat: first (#9)"),
            sources.Commit(sha="2" * 40, subject="feat: first again (#9)"),
            sources.Commit(sha="3" * 40, subject="fix: second"),
        ]
        candidates = sources.commit_candidates(commits, repository="o/r", reference="HEAD")
        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0].provenance["commits"], 2)


class FormatTests(unittest.TestCase):
    """The formats, in both directions."""

    def test_front_matter_round_trip(self):
        values = {
            "title": "Autumn: the update",
            "author": "GooGuTeam",
            "date": "2026-10-28 08:00:00 +0000",
            "id": 20261028080000,
            "series": "game_updates",
        }
        text = formats.render_front_matter(values, ("title", "author", "date", "id", "series"))
        self.assertEqual(formats.split_front_matter(text), values)

    def test_a_number_of_its_own_is_quoted(self):
        text = formats.render_front_matter({"title": "2026"}, ("title",))
        self.assertIn('title: "2026"', text)
        self.assertEqual(formats.split_front_matter(text)["title"], "2026")

    def test_an_empty_value_is_refused(self):
        with self.assertRaises(ValueError):
            formats.render_front_matter({"title": "  "}, ("title",))

    def test_an_unknown_key_is_refused(self):
        with self.assertRaises(ValueError):
            formats.render_front_matter({"titel": "x"}, ("title",))

    def test_versions(self):
        self.assertEqual(formats.version_suffix("2026.1009.0-g0v0"), "g0v0")
        self.assertEqual(formats.version_suffix("2026.1009.0"), "")
        self.assertEqual(formats.display_version("2026.1009.0-g0v0"), "2026.1009.0")
        self.assertEqual(formats.build_id("2026.1009.0"), 202610090)
        self.assertEqual(formats.date_from_version("2026.1009.0"), datetime(2026, 10, 9, tzinfo=UTC))
        self.assertIsNone(formats.date_from_version("1.2.3"))
        self.assertTrue(formats.VERSION.match("2026.1009.0-g0v0"))
        self.assertFalse(formats.VERSION.match("2026.10.9"))

    def test_a_chinese_title_leaves_no_slug(self):
        self.assertEqual(formats.slugify("秋季更新：观战"), "")
        self.assertEqual(formats.slugify("Autumn Update!"), "autumn-update")

    def test_a_post_round_trip(self):
        front = {"title": "秋季更新：观战", "author": "GooGuTeam", "date": "2026-10-28 08:00:00 +0000"}
        text = content.render_post(front, "## A heading\n\nBody.\n")
        parsed, body = formats.split_markdown(text)
        self.assertEqual(parsed, front)
        self.assertEqual(body.strip(), "## A heading\n\nBody.")


class DraftTests(unittest.TestCase):
    """A draft, which usually holds more than one repository's worth of changes."""

    def make(self):
        return content.Draft(
            path=Path("drafts/2026.1029.0-g0v0.json"),
            document={"version": "2026.1029.0-g0v0", "stream": "g0v0", "sources": [], "entries": []},
        )

    def test_an_entry_is_added_by_hand(self):
        draft = self.make()
        content.add_entry(draft, {"type": "add", "title": "A"})
        item = draft.entries[0]
        self.assertEqual(item["status"], "keep")
        self.assertEqual(item["provenance"], {}, "nothing was read, so nothing is recorded")
        self.assertEqual(item["entry"], {"type": "add", "title": "A"})
        with self.assertRaises(content.ContentError):
            content.add_entry(draft, {"type": "nope", "title": "B"})
        with self.assertRaises(content.ContentError):
            content.add_entry(draft, {"type": "add"})

    def test_a_second_source_is_added_once(self):
        draft = self.make()
        first = {"kind": "release", "repository": "o/r", "reference": "t1"}
        content.append_to_draft(draft, entries=[{"entry": {"type": "add", "title": "A", "url": "u1"}}], source=first)
        content.append_to_draft(
            draft,
            entries=[
                {"entry": {"type": "add", "title": "A", "url": "u1"}},
                {"entry": {"type": "add", "title": "B", "url": "u2"}},
            ],
            source={"kind": "release", "repository": "o/r", "reference": "t2"},
        )
        self.assertEqual([item["entry"]["title"] for item in draft.entries], ["A", "B"])
        self.assertEqual(len(draft.document["sources"]), 2)

    def test_a_source_that_adds_nothing_is_refused(self):
        draft = self.make()
        entry = {"entry": {"type": "add", "title": "A", "url": "u1"}}
        content.append_to_draft(draft, entries=[entry], source={"kind": "release"})
        with self.assertRaises(content.ContentError):
            content.append_to_draft(draft, entries=[entry], source={"kind": "release"})


class RepositoryTests(unittest.TestCase):
    """The rules that only exist once a directory is known."""

    def test_a_path_outside_the_repository_is_refused(self):
        with self.assertRaises(content.ContentError):
            content.resolve("../../pyproject.toml", under=content.DRAFTS)

    def test_a_build_is_only_written_where_it_validates(self):
        # The document is refused before anything is written, so pointing the module at
        # the real tree is safe here: this is the check, not a write.
        with self.assertRaises(content.ContentError):
            content.write_build({"version": "1.2.3", "entries": [{"type": "add", "title": "x"}]}, "g0v0")


class EmptyTreeTests(unittest.TestCase):
    """A repository nobody has published to yet, which is the state this one is in."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name) / "repo"
        shutil.copytree(REPO, self.repo, ignore=IGNORED)
        self.addCleanup(self.temporary.cleanup)
        # Whatever content the working copy happens to hold, the check is run against a
        # tree with none: each stream directory and each year of posts goes.
        for tree in ("changelog", "news"):
            for directory in (self.repo / tree).iterdir():
                if directory.is_dir():
                    shutil.rmtree(directory)

    def check(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            [sys.executable, str(self.repo / "tools" / "validate.py")],
            cwd=self.repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

    def test_an_empty_tree_passes_with_warnings(self):
        checked = self.check()
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        self.assertIn("0 error(s)", checked.stdout)
        self.assertIn("declares `g0v0`, but changelog/g0v0/ does not exist yet", checked.stdout)
        self.assertIn("holds no post", checked.stdout)

    def test_a_declared_stream_is_required_once_anything_is_built(self):
        # One build in one stream is enough for the declaration to be held to account: at
        # that point a missing directory is a mistake rather than an empty start.
        build = self.repo / "changelog" / "robin" / "2026.1015.0-robin.json"
        build.parent.mkdir()
        build.write_text(
            json.dumps({"version": "2026.1015.0-robin", "entries": [{"type": "add", "title": "x"}]}),
            encoding="utf-8",
            newline="\n",
        )

        checked = self.check()
        self.assertEqual(checked.returncode, 1, checked.stdout + checked.stderr)
        self.assertIn("declares `g0v0`, but changelog/g0v0/ does not exist", checked.stdout)
        self.assertNotIn("does not exist yet", checked.stdout)


class PublishTests(unittest.TestCase):
    """The whole way from a draft to a build, through the command line."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name) / "repo"
        shutil.copytree(REPO, self.repo, ignore=IGNORED)
        self.addCleanup(self.temporary.cleanup)

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        # `sys.executable` is the interpreter running this test, and the script path is the
        # copy just made: nothing here comes from outside.
        return subprocess.run(  # noqa: S603
            [sys.executable, str(self.repo / "tools" / "newsctl.py"), *arguments],
            cwd=self.repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

    def write_draft(self, document: dict[str, Any]) -> None:
        path = self.repo / "drafts" / f"{document['version']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8", newline="\n")

    def draft(self, version: str = "2026.1029.0-g0v0", stream: str = "g0v0") -> dict[str, Any]:
        return {
            "version": version,
            "stream": stream,
            "generated_at": "2026-10-29 08:00:00 +0000",
            "sources": [{"kind": "release", "repository": "GooGuTeam/g0v0-server", "reference": "2026.1029.0"}],
            "entries": [
                {
                    "status": "keep",
                    "entry": {"type": "add", "category": "Client", "title": "Something new"},
                    "provenance": {"source": "release", "pull_request": 1},
                },
                {
                    "status": "drop",
                    "entry": {"type": "misc", "title": "Nobody needs this"},
                    "provenance": {"source": "release", "pull_request": 2},
                },
            ],
        }

    def test_publishing_writes_only_what_was_kept(self):
        self.write_draft(self.draft())
        result = self.run_cli("publish", "2026.1029.0-g0v0", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)

        written = json.loads((self.repo / "changelog" / "g0v0" / "2026.1029.0-g0v0.json").read_text(encoding="utf-8"))
        self.assertEqual(written["$schema"], "../build.schema.json")
        self.assertEqual([entry["title"] for entry in written["entries"]], ["Something new"])
        self.assertNotIn("provenance", written["entries"][0])
        self.assertNotIn("status", written["entries"][0])

        checked = subprocess.run(  # noqa: S603
            [sys.executable, str(self.repo / "tools" / "validate.py")],
            cwd=self.repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)

    def test_an_existing_build_is_not_replaced_silently(self):
        self.write_draft(self.draft())
        self.assertEqual(self.run_cli("publish", "2026.1029.0-g0v0", "--yes").returncode, 0)
        again = self.run_cli("publish", "2026.1029.0-g0v0", "--yes")
        self.assertEqual(again.returncode, 1)
        self.assertIn("already exists", again.stderr)

    def test_a_draft_that_dropped_everything_writes_nothing(self):
        document = self.draft()
        document["entries"] = [{"status": "drop", "entry": {"type": "add", "title": "x"}, "provenance": {}}]
        self.write_draft(document)
        result = self.run_cli("publish", "2026.1029.0-g0v0", "--yes")
        self.assertEqual(result.returncode, 1)
        self.assertIn("every entry was dropped", result.stderr)

    def test_a_suffix_naming_another_stream_is_refused(self):
        self.write_draft(self.draft(version="2026.1029.0-robin", stream="g0v0"))
        result = self.run_cli("publish", "2026.1029.0-robin", "--yes")
        self.assertEqual(result.returncode, 1)
        self.assertIn("names the stream", result.stderr)

    def test_status_reads_the_tree(self):
        result = self.run_cli("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("2026.1009.0-g0v0", result.stdout)
        self.assertIn("checks: 0 error(s)", result.stdout)

    def test_a_blank_draft_is_written_by_hand(self):
        started = self.run_cli("draft", "--blank", "--version", "2026.1030.0-g0v0", "--users", "7")
        self.assertEqual(started.returncode, 0, started.stderr)
        draft = json.loads((self.repo / "drafts" / "2026.1030.0-g0v0.json").read_text(encoding="utf-8"))
        self.assertEqual(draft["entries"], [], "a blank draft starts with no entry")
        self.assertEqual(draft["sources"], [], "and records that nothing was read")

        empty = self.run_cli("publish", "2026.1030.0-g0v0", "--yes")
        self.assertEqual(empty.returncode, 1, "a draft with no entry is not a build")
        self.assertIn("no entry yet", empty.stderr)

        added = self.run_cli(
            "entry",
            "2026.1030.0-g0v0",
            "--title",
            "Spectator rooms stay open",
            "--type",
            "fix",
            "--category",
            "Spectator",
            "--major",
        )
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertIn("entry 1", added.stdout)

        published = self.run_cli("publish", "2026.1030.0-g0v0", "--yes")
        self.assertEqual(published.returncode, 0, published.stderr)
        written = json.loads((self.repo / "changelog" / "g0v0" / "2026.1030.0-g0v0.json").read_text(encoding="utf-8"))
        self.assertEqual(written["users"], 7)
        self.assertEqual(
            written["entries"],
            [{"type": "fix", "category": "Spectator", "title": "Spectator rooms stay open", "major": True}],
        )

    def test_an_entry_that_says_nothing_is_refused(self):
        self.run_cli("draft", "--blank", "--version", "2026.1030.0-g0v0")
        result = self.run_cli("entry", "2026.1030.0-g0v0", "--type", "add")
        self.assertEqual(result.returncode, 1)
        self.assertIn("needs a `title` or a `message`", result.stderr)

    def test_a_blank_draft_needs_a_version_and_no_source(self):
        self.assertEqual(self.run_cli("draft", "--blank").returncode, 1)
        mixed = self.run_cli("draft", "--blank", "--version", "2026.1030.0-g0v0", "--release", "x")
        self.assertEqual(mixed.returncode, 1)
        self.assertIn("takes no source", mixed.stderr)

    def test_a_post_is_named_after_its_own_date(self):
        result = self.run_cli(
            "post",
            "new",
            "--title",
            "Autumn update",
            "--author",
            "GooGuTeam",
            "--slug",
            "autumn-update",
            "--date",
            "2026-09-30 10:00:00 +0000",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.repo / "news" / "2026" / "20260930-autumn-update.md").is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
