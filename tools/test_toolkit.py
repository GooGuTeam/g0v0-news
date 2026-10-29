"""Tests for the maintenance tools, run against a copy of this repository.

    uv run python tools/test_toolkit.py

The parsing is tested on strings, because that is where the judgement is: which bullet is
a change, which commit is noise, what a value looks like when it is written back. The
publishing is tested on a temporary copy of the repository through the command line, so
that what is tested is what a person runs, and so that a broken test cannot write into the
working tree. What reads a remote repository is tested against `fake_api.py` on a loopback
port, so that no test needs the network and a failure is always this repository's fault.
"""

import base64
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
from typing import Any
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

import content
import fake_api
import formats
import sources

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent

IGNORED = shutil.ignore_patterns(".git", ".venv", ".idea", ".vscode", "drafts", "__pycache__")


def copy_repository(parent: Path) -> Path:
    """Copy this repository into `parent`, leaving behind whatever content it holds.

    `changelog/` and `news/` here hold what the person running the tests has published, and
    a checkout on CI holds none of it: a test that read those files would pass on one
    machine and fail on the other. A copy therefore starts empty, and each test writes the
    content it is about.
    """
    repo = parent / "repo"
    shutil.copytree(REPO, repo, ignore=IGNORED)
    for tree in ("changelog", "news"):
        for directory in sorted((repo / tree).iterdir()):
            if directory.is_dir():
                shutil.rmtree(directory)
    return repo


def clone_with(parent: Path, history: list[tuple[str, str | None]]) -> Path:
    """A tiny repository: one empty commit per message, tagged where a tag is given.

    Signing is switched off per command, because a machine that signs commits by default --
    this one does -- would stop at the first empty commit waiting for a key.
    """
    path = parent / "clone"
    path.mkdir()

    def git(*arguments: str) -> None:
        # Everything here is a constant in this file: the executables come from PATH on
        # purpose, since the test is about what `git` reported.
        subprocess.run(  # noqa: S603
            [  # noqa: S607
                "git",
                "-C",
                str(path),
                "-c",
                "user.name=Ann",
                "-c",
                "user.email=ann@example.com",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "tag.gpgsign=false",
                *arguments,
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    git("init", "-q")
    for message, tag in history:
        git("commit", "-q", "--allow-empty", "-m", message)
        if tag is not None:
            git("tag", tag)
    return path


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


class ApiTests(unittest.TestCase):
    """Where the API is, and what one request to it carries."""

    def test_the_base_is_github_unless_the_environment_says_otherwise(self):
        with patch.dict(os.environ, {"GITHUB_API_URL": ""}):
            self.assertEqual(sources.api_base(), sources.API)
        with patch.dict(os.environ, {"GITHUB_API_URL": "http://127.0.0.1:8766/"}):
            self.assertEqual(sources.api_base(), "http://127.0.0.1:8766", "the trailing slash is not part of a path")

    def test_a_token_goes_to_github_and_nowhere_else(self):
        with patch.object(sources, "token", return_value="secret"):
            self.assertEqual(sources.request_headers(sources.API)["Authorization"], "Bearer secret")
            self.assertNotIn(
                "Authorization",
                sources.request_headers("http://127.0.0.1:8766"),
                "a stand-in has no use for a token, and a mistyped base should not carry one",
            )

    def test_a_blob_is_named_the_way_git_names_it(self):
        self.assertEqual(fake_api.blob_sha(b"hello\n"), "ce013625030ba8dba906f756967f9e9ca394464a")


class StandInTests(unittest.TestCase):
    """The tools reading a repository that is not on GitHub, which is what `fake_api.py` is for."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = copy_repository(Path(self.temporary.name))
        self.base = self.start(fake_api.World(repository="local/g0v0-news", documents=fake_api.demo()))

    def start(self, world: fake_api.World) -> str:
        """Serve a world on a free port, and stop it when the test is over."""
        server = fake_api.FakeAPI(("127.0.0.1", 0), root=self.repo, world=world)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.base

    def get(self, path: str, base: str | None = None) -> tuple[int, Any]:
        try:
            with urlopen(f"{base or self.base}{path}") as response:  # noqa: S310
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def collect(self, *, base: str | None = None, **kwargs: Any) -> sources.Collection:
        with patch.dict(os.environ, {"GITHUB_API_URL": base or self.base}):
            return sources.collect(**kwargs)

    def test_the_index_says_where_to_point_a_reader(self):
        status, index = self.get("/")
        self.assertEqual(status, 200)
        self.assertEqual(index["environment"]["GITHUB_API_URL"], self.base)
        self.assertEqual(index["environment"]["CONTENT_REPOSITORY"], "local/g0v0-news")
        self.assertIn(
            f"/repos/{fake_api.DEMO_REPOSITORY}/releases/tags/{fake_api.DEMO_TAG}",
            index["documents"],
        )

    def test_a_directory_and_a_file_come_from_the_checkout(self):
        status, listing = self.get("/repos/local/g0v0-news/contents/changelog")
        self.assertEqual(status, 200)
        self.assertEqual(
            {entry["name"]: entry["type"] for entry in listing},
            {"build.schema.json": "file", "streams.json": "file", "streams.schema.json": "file"},
            "the copy starts with no build in it, and a listing shows what is there",
        )

        status, entry = self.get("/repos/local/g0v0-news/contents/changelog/streams.json")
        self.assertEqual(status, 200)
        data = base64.b64decode(entry["content"])
        self.assertEqual(data, (self.repo / "changelog" / "streams.json").read_bytes())
        self.assertEqual(entry["sha"], fake_api.blob_sha(data), "the id the server caches by")
        self.assertEqual(entry["download_url"], f"{self.base}/local/g0v0-news/HEAD/changelog/streams.json")

    def test_the_raw_route_answers_the_same_bytes(self):
        with urlopen(f"{self.base}/local/g0v0-news/HEAD/README.md") as response:  # noqa: S310
            self.assertEqual(response.read(), (self.repo / "README.md").read_bytes())

    def test_a_path_outside_the_checkout_is_not_found(self):
        self.assertEqual(self.get("/repos/local/g0v0-news/contents/../../pyproject.toml")[0], 404)
        self.assertEqual(self.get("/repos/local/g0v0-news/contents/no-such-file.json")[0], 404)

    def test_a_release_is_read_without_the_network(self):
        collection = self.collect(repository=fake_api.DEMO_REPOSITORY, release=fake_api.DEMO_TAG)
        self.assertEqual(collection.version, fake_api.DEMO_TAG, "the tag is a version, so it names the build")
        self.assertEqual(collection.created_at, "2026-10-30 12:00:00 +0000")
        self.assertEqual(
            [(item.entry["type"], item.entry["title"]) for item in collection.candidates],
            [
                ("add", "Keep a room open when the host leaves"),
                ("add", "Beatmap downloads resume after a dropped connection"),
                ("add", "Drop the `/api/v1` prefix"),
                ("fix", "The changelog no longer 500s on a build with no `users`"),
            ],
        )
        self.assertEqual(collection.candidates[0].entry["category"], "Client")
        self.assertTrue(collection.candidates[2].entry.get("major"), "`!` outranks the section heading")

    def test_a_range_is_read_without_the_network(self):
        plain = self.collect(repository=fake_api.DEMO_REPOSITORY, commit_range="2026.1009.0-g0v0...HEAD")
        self.assertEqual(
            [item.entry["title"] for item in plain.candidates],
            [
                "Keep a room open when the host leaves",
                "Stop the changelog answering 500 for a build with no users",
                "Drop the `/api/v1` prefix",
            ],
            "a merge commit and a chore are not in a draft unless `everything` asks for them",
        )
        everything = self.collect(
            repository=fake_api.DEMO_REPOSITORY, commit_range="2026.1009.0-g0v0...HEAD", everything=True
        )
        self.assertIn("Bump jsonschema", [item.entry["title"] for item in everything.candidates])

    def test_a_tag_nobody_published_fails_the_way_it_fails_on_github(self):
        with self.assertRaises(content.ContentError) as caught:
            self.collect(repository=fake_api.DEMO_REPOSITORY, release="2026.0001.0-g0v0")
        self.assertIn("the API has nothing at", str(caught.exception))

    def test_a_call_to_this_machine_escapes_the_proxy(self):
        # A proxy in the environment would otherwise be asked to reach 127.0.0.1, which it
        # cannot, so this is the test that the loopback opener is doing its job.
        dead = {"http_proxy": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9", "no_proxy": "", "NO_PROXY": ""}
        with patch.dict(os.environ, {**dead, "GITHUB_API_URL": self.base}):
            self.assertEqual(sources.api("/")["repository"], "local/g0v0-news")

    def test_a_fixture_is_answered_by_path(self):
        fixture = Path(self.temporary.name) / "world.json"
        fixture.write_text(
            json.dumps(
                {
                    "/repos/o/r/compare/v1...v2": {
                        "commits": [
                            {
                                "sha": "a" * 40,
                                "commit": {"message": "fix: from the fixture", "author": {"name": "Ann"}},
                                "author": {"login": "ann"},
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
            newline="\n",
        )
        base = self.start(fake_api.World(repository="o/r", documents=fake_api.load_fixture(fixture)))
        collection = self.collect(base=base, repository="o/r", commit_range="v1...v2")
        self.assertEqual([item.entry["title"] for item in collection.candidates], ["From the fixture"])
        self.assertEqual(self.get("/", base=base)[1]["repository"], "o/r")

    def test_a_clone_answers_any_range_on_demand(self):
        clone = clone_with(
            Path(self.temporary.name),
            [
                ("feat: the first thing", "2026.1009.0-g0v0"),
                ("fix(api): the second thing", None),
                ("chore(deps): bump x", "2026.1030.0-g0v0"),
            ],
        )
        base = self.start(fake_api.World(repository="o/r", clone=clone))

        self.assertEqual(set(self.get("/", base=base)[1]["releases"]), {"2026.1030.0-g0v0", "2026.1009.0-g0v0"})

        release = self.collect(base=base, repository="o/r", release="2026.1030.0-g0v0")
        ranged = self.collect(base=base, repository="o/r", commit_range="2026.1009.0-g0v0...HEAD")

        self.assertEqual(release.version, "2026.1030.0-g0v0")
        self.assertTrue(release.created_at, "a tag knows when it was made")
        self.assertEqual(
            [item.entry["title"] for item in release.candidates],
            ["The first thing", "The second thing", "Bump x"],
            "a body built from a tag lists every commit up to it, because a release is read as prose",
        )
        self.assertEqual(
            [item.entry["title"] for item in ranged.candidates],
            ["The second thing"],
            "a range read from a clone drops the chore, exactly as one read from GitHub does",
        )

    def test_a_draft_is_written_from_the_stand_in(self):
        environment = {**os.environ, "GITHUB_API_URL": self.base}
        # `sys.executable` is the interpreter running this test and the script is the copy
        # just made: what is tested is the command a person runs, pointed at a stand-in.
        drafted = subprocess.run(  # noqa: S603
            [
                sys.executable,
                str(self.repo / "tools" / "newsctl.py"),
                "draft",
                "--repository",
                fake_api.DEMO_REPOSITORY,
                "--release",
                fake_api.DEMO_TAG,
            ],
            cwd=self.repo,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(drafted.returncode, 0, drafted.stderr)
        self.assertIn("4 candidate(s)", drafted.stdout)

        draft = json.loads((self.repo / "drafts" / f"{fake_api.DEMO_TAG}.json").read_text(encoding="utf-8"))
        self.assertEqual(draft["stream"], "g0v0")
        self.assertEqual(draft["sources"][0]["kind"], "release")
        self.assertEqual(len(draft["entries"]), 4)

        published = subprocess.run(  # noqa: S603
            [sys.executable, str(self.repo / "tools" / "newsctl.py"), "publish", fake_api.DEMO_TAG, "--yes"],
            cwd=self.repo,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(published.returncode, 0, published.stderr)
        build = json.loads((self.repo / "changelog" / "g0v0" / f"{fake_api.DEMO_TAG}.json").read_text(encoding="utf-8"))
        self.assertEqual(
            [entry["title"] for entry in build["entries"]],
            [
                "Keep a room open when the host leaves",
                "Beatmap downloads resume after a dropped connection",
                "Drop the `/api/v1` prefix",
                "The changelog no longer 500s on a build with no `users`",
            ],
        )
        self.assertNotIn("provenance", build["entries"][0], "a build is what the client is served, nothing else")


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
        self.repo = copy_repository(Path(self.temporary.name))
        self.addCleanup(self.temporary.cleanup)

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
        self.repo = copy_repository(Path(self.temporary.name))
        self.addCleanup(self.temporary.cleanup)
        # Every stream `streams.json` declares gets a build: once anything is built, a
        # declared stream with no directory is an error, and most of these tests build into
        # one of them and then run the checker over the result.
        self.write_build("2026.1008.0-g0v0", "g0v0")
        self.write_build("2026.1015.0-robin", "robin")

    def write_build(self, version: str, stream: str) -> None:
        path = self.repo / "changelog" / stream / f"{version}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"version": version, "entries": [{"type": "add", "title": "Already shipped"}]}),
            encoding="utf-8",
            newline="\n",
        )

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
        self.assertIn("2026.1008.0-g0v0", result.stdout, "the build written into the copy is the one listed")
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
