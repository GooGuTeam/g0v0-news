# g0v0-news

Content repository behind the `changelog` and `news` endpoints of the g0v0 server
(`/api/v2/changelog`, `/api/v2/news`). Two content trees share this repository;
either one can be moved to a repository of its own by changing two settings.

- `news/` — one Markdown file per post, laid out like `ppy/osu-wiki`'s `news/`.
- `changelog/` — one JSON file per build, in a directory per update stream.

Nothing here is copied: the server reads the files as they are, caches them by Git
blob SHA, and renders no HTML. Posts and changelog messages are returned as Markdown,
and the osu! client does the rendering.

Every file is validated against the schemas in this repository before it is pushed, and
the server enforces the same rules again: a file that breaks them is skipped whole,
with the field it broke on written to the server log. There is no repair and no
fallback, so a field left out is not filled in from somewhere else.

## Layout

```plain
news/
  2026/
    20261028-autumn-update.md
changelog/
  streams.json
  g0v0/
    2026.1009.0-g0v0.json
    2026.1002.0-g0v0.json
  robin/
    2026.1015.0-robin.json
drafts/
  2026.1029.0-g0v0.json
```

`drafts/` is where `tools/newsctl.py` leaves a build that has not been published yet; it
sits outside both content trees on purpose, and nothing under it is served.

Posts may also sit flat under `news/`; the year directory is a convention taken from
osu-wiki, not a requirement. The directory under `changelog/` is the stream name that
appears in the URL, so `changelog/g0v0/` is served at `/api/v2/changelog/g0v0/...`.

Two streams, matching the two builds on the download page: `g0v0` is the release build,
`robin` the test one. A build carries its stream as a version suffix — `-g0v0`,
`-robin` — which is the same suffix the client hides when it shows the version, so the
stream a file sits in and the version it holds agree.

The file name of a post — minus `.md` — is its slug, which is what
`/api/v2/news/{news}` accepts. `changelog/streams.json` only describes the streams it
wants to control; a stream directory nobody describes still shows up, taking the
directory name as its display name.

## A news post

```markdown
---
title: Title shown in the listing and the notification
author: GooGuTeam
date: 2026-10-28 08:00:00 +0000
updated: 2026-10-28 09:30:00 +0000
series: game_updates
---

![banner](/wiki/shared/news/2026-10-28-autumn-update/banner.jpg)

Opening paragraph, which becomes the preview in the listing.
```

| Key | Required | Notes |
| --- | --- | --- |
| `title` | yes | Shown in the listing above the preview. Nothing reads the first heading of the body instead. |
| `author` | yes | Credited as written. A `— Author` line at the end of the body is prose, not data. |
| `date` | yes | Publication time. Orders the listing, and the post is listed from this moment on. |
| `updated` | no | Defaults to `date`. |
| `id` | no | Numeric identifier. Derived from `date` to the second when absent, so a post keeps its identifier without one. |
| `series` | no | Free text, not checked against a list: how posts are grouped is still being designed. Leaving it out files the post under `misc`, which is the default. Expect this to become an enumeration once the set is settled. |

The front matter is read by a small flat YAML parser: `key: value` on one line, and
inline lists such as `tags: [a, b]`. Nested blocks are ignored, so keep it flat. A bare
number is read as an integer, which is what lets `id: 20261028080000` be a number rather
than text.

The first image of the body becomes the listing banner. A path starting with `/wiki/`
is served by the server's wiki image endpoint — the same one the wiki articles use —
so shared banners belong under `ppy/osu-wiki`'s `wiki/shared/news/`. An absolute
`http(s)://` URL is passed through untouched, and any other relative path is resolved
against this repository.

## A changelog build

```json
{
  "$schema": "../build.schema.json",
  "version": "2026.1009.0-g0v0",
  "created_at": "2026-10-09 12:00:00 +0000",
  "entries": [
    {
      "type": "add",
      "category": "Client",
      "title": "Spectator mode",
      "message": "Watching a multiplayer room no longer requires a client restart.",
      "url": "https://github.com/GooGuTeam/g0v0-server/pull/512"
    }
  ]
}
```

| Key | Required | Notes |
| --- | --- | --- |
| `version` | yes | `2026.1009.0` and `2026.1009.0-g0v0` are both accepted. A `-suffix` names the stream the build was made for, so it has to match the directory the file sits in. The shape is fixed because the release date is read from it when `created_at` is absent. |
| `id` | no | Derived from the digits of `version`. Declare it when two builds spell the same digits. |
| `display_version` | no | Defaults to `version` with the `-suffix` and any non-numeric characters dropped. |
| `created_at` | no | Defaults to the date carried by `version`, at midnight UTC. A version of another shape and no `created_at` together leave the build undated, which is refused. |
| `users` | no | Users running the build at release. Defaults to `0`. Summed per stream into the `user_count` the API reports. |
| `youtube_id` | no | Video shown above the changelog, as the bare identifier. |
| `entries` | yes | At least one change. A build that lists nothing is a file left half-written. |

| Entry key | Notes |
| --- | --- |
| `type` | Required. `add`, `fix` or `misc` — the client has an icon per type and nothing for the rest, so an unknown one is refused. |
| `category` | Group heading, defaults to `Miscellaneous`. |
| `title` | Required unless `message` is set. An entry with neither is refused. |
| `message` | Markdown. Returned as `null` when the request asks for a format the server cannot produce, that is `message_formats[]` without `markdown`. |
| `url`, `repository` | Link targets, served as written. |
| `github_url`, `github_pull_request_id` | Reference to the pull request. |
| `major` | `true` to mark the change as notable. |
| `created_at` | Defaults to the build's `created_at`. |
| `github_user` | `display_name`, `github_username`, `github_url`, `osu_username`, `user_id`, `user_url`. An empty object is refused: it names nobody. |

The stream is the directory the file sits in. A build that writes a `stream` of its own
is refused rather than believed, since the two could disagree.

## stream declarations

`changelog/streams.json` — the list may be top level or under a `streams` key:

```json
{
  "streams": [
    { "name": "g0v0", "id": 1, "display_name": "g0v0", "is_featured": true },
    { "name": "robin", "id": 2, "display_name": "robin" }
  ]
}
```

| Key | Notes |
| --- | --- |
| `name` | Directory name, and the `{stream}` of the URL. Required, and limited to lowercase letters, digits and dashes. |
| `id` | Defaults to the position in the list. |
| `display_name` | Defaults to `name`. |
| `is_featured` | Featured streams are listed first. |

There is deliberately no `user_count` here. It is derived: the server sums the `users`
of the stream's builds, the same way osu-web does, so a number typed into this file
would only be a copy that goes stale as soon as a new build is added. `users` therefore
belongs to the build files (see above), and `user_count` only ever appears in the API
response. The schema rejects the key if it is added anyway.

## Checking before pushing

The schemas are the contract. `tools/validate.py` reads them, and the server enforces
the same rules, so a file that fails here is a file that would never appear on the site.

```bash
uv sync                                        # once, and whenever the dependency changes
uv run python tools/validate.py                # the content
uv run python tools/validate.py --self-test    # the schemas themselves
```

`--self-test` runs the build schema against cases it has to accept and refuse. It exists
because a schema that is quietly loosened — a keyword misspelt, a `required` dropped —
still passes the content check, and this is where that shows up.

Beside the schema rules, `validate.py` checks what a schema cannot express:

- identifiers are unique across builds, and across posts;
- a build file is named after the version it holds;
- a build suffixed with a stream name sits in that stream's directory;
- a stream declared in `streams.json` has a directory, and a directory has at least one
  build — a stream without one is dropped from the changelog entirely;
- a post's slug starts with its own publication date.

Errors exit with `1`; warnings do not fail the run. Both commands run on every push and
pull request, in `.github/workflows/validate.yml`.

A tree with nothing in it yet passes, with a warning per stream that has no build and one
for a `news/` with no post. Those rules describe what is built, and an empty tree has no
build to hold them against; the first one makes them binding again, and a declared stream
whose directory is missing is an error from then on.

`uv.lock` is committed, so the version `jsonschema` resolves to is the one the schema
checks were written against. It records whichever package index resolved it; if your
index differs from the one in the lock, `uv lock --upgrade` resolves it again.

## Keeping it up to date

Two ways into the same set of modules in `tools/`: a command line, and a page it serves.

```bash
uv run python tools/newsctl.py status          # what is in the tree, and what the checker says
uv run python tools/newsctl.py serve           # the same, as a page on 127.0.0.1:8765
```

`status`, `check`, `draft`, `drafts`, `show`, `entry`, `publish` and `post new` are the
commands;
`serve` opens the console, which edits the same files through the same functions. The
console is built from the standard library and one HTML file with no build step, so it is
something you run rather than something you install, and it binds to `127.0.0.1` with no
login because it writes into this checkout and nothing else.

### Drafts, and why they are not in `changelog/`

A build file is the end of a process, and the tools start it from a source: a release
somebody published, or a range of commits.

```bash
uv run python tools/newsctl.py draft --repository GooGuTeam/g0v0-server --release 2026.1009.0-g0v0
uv run python tools/newsctl.py draft --repository GooGuTeam/g0v0-server \
    --range 2026.1002.0..2026.1009.0 --repo-path ../g0v0-server
uv run python tools/newsctl.py show 2026.1009.0-g0v0
uv run python tools/newsctl.py publish 2026.1009.0-g0v0
```

That leaves a draft at `drafts/<version>.json`, beside the content trees rather than
inside one of them: the server publishes whatever it finds under `changelog/`, so a draft
stored there would be published by being saved. A build usually summarizes more than one
repository, so a second read of the same version is `--append`, which adds what is not
already there and keeps the decisions made about what is; `--force` replaces the draft
instead.

A draft is a list of candidate entries, each carrying `status: keep` or `status: drop` and
a `provenance` that says which commit or pull request it came from. `publish` writes the
kept ones, in order, with the provenance and the status left behind -- it is how the draft
was made, not part of what the client is served. It refuses to replace an existing build,
and it refuses one whose `-suffix` disagrees with the directory.

Nothing is committed, and nothing is published without `publish`. Deciding what a change
means to a player is the step these tools cannot take.

### Writing one from nothing

Not every change has a release behind it: an outage, a fix that shipped quietly, a decision
worth explaining. Those start from an empty draft rather than a source, and end up in the
same file as anything extracted.

```bash
uv run python tools/newsctl.py draft --blank --version 2026.1030.0-g0v0
uv run python tools/newsctl.py entry 2026.1030.0-g0v0 \
    --title "Spectator rooms stay open when the host leaves" \
    --type fix --category Spectator --message "The room is closed by the last player, not the host."
uv run python tools/newsctl.py publish 2026.1030.0-g0v0
```

A blank draft records `sources: []`, which is how a reader of the draft can tell nothing was
extracted. `entry` checks the two things a source would have got right for you -- that the
type is one the client knows, and that the entry says what changed -- and decides nothing
else. Publishing an empty draft is refused, because a build that lists nothing is a file
left half-written.

The console has the same two ways in: "start from nothing" on the overview opens a draft or
a post, and the draft editor adds and removes entries, so a build can be written there
without touching the JSON. A post is `post new --title ... --author ...`, or the second card
on that same screen; either way the file is named after the date it is published on, and the
body is written in the editor that opens.

### What a source can and cannot know

| Source | Gives | Does not give |
| --- | --- | --- |
| `--release <tag>` | A body already written for a reader, its publication time, and a version when the tag is one | Anything the author left out |
| `--range <base..head>` | Every commit that landed, including the ones nobody would write down | Wording, grouping, or a version |

A release body is read with GitHub's own template in mind: `## What's Changed` names the
list and is not a category, headings that name a kind of change (`## Bug Fixes`,
`### Breaking Changes`) set the type of the bullets under them, and any other heading
becomes the category. A bullet carrying its own `type(scope):` prefix overrides both.

Commits are read as conventional commits: the prefix decides the type, the scope becomes
the category (`api` is `API`, because that is how the committed files spell it), and `!`
or a `BREAKING CHANGE` trailer sets `major`. Merge commits are dropped, since their
subject names a branch rather than a change, and so are `chore`, `ci`, `docs`, `build`,
`test`, `style` and `release` -- pass `--everything` when they matter. Two commits that
mention the same pull request collapse into one entry, which records how many commits it
stands for.

A range is read from a clone when `--repo-path` is given, and from the compare API
otherwise; the API stops at 250 commits, which is a limit of the endpoint rather than of
this tool. Reading from a clone needs no network at all, and `--repository` is then only
needed for the URLs the entries carry. Whichever is used, a repository that merges pull
requests without squashing produces one entry per commit, which is a reason drafts have a
`drop` state rather than a reason to trust the output.

The API is reached with `GITHUB_TOKEN` or `GH_TOKEN` when either is set, and through
`gh auth token` when it is not, so a machine where `gh` is logged in already has one.
Without a token it still works, at sixty requests an hour. Which API that is comes from
`GITHUB_API_URL`, defaulting to `https://api.github.com`: the name `gh` and GitHub Actions
already use for it, and the hook the next section hangs on.

The tools' own tests run against a temporary copy of this repository:

```bash
uv run python tools/test_toolkit.py
```

### Working without the network

Both of the things the tools read -- a release, and a range of commits -- can be answered
without GitHub, by `tools/fake_api.py`:

```bash
uv run python tools/fake_api.py                        # this checkout, and a demo release
uv run python tools/fake_api.py --clone ../g0v0-server # a release per tag, and any range
```

Nothing else changes: the tools are pointed at it with `GITHUB_API_URL`, the same variable
that names a different GitHub.

```bash
GITHUB_API_URL=http://127.0.0.1:8766 uv run python tools/newsctl.py draft \
    --repository GooGuTeam/g0v0-server --release 2026.1030.0-g0v0
```

Without `--clone` or `--fixture` it serves a demo built into the file: one release and one
range, with the awkward cases left in -- a breaking change written as a commit, a section
about the release rather than about a change, a bullet with no prefix at all. That is enough
to see what a draft looks like before trusting one on a real repository.

A clone is read when a request arrives rather than walked at startup, so any range a person
types works, and a tag nobody tagged answers 404 -- which is what it would answer on the
real API, and the reason a draft that "worked locally" does not become a surprise later.
`--fixture` takes a JSON object of path to document instead, so a response copied out of
`gh api` can be pasted in as it is.

There is no authentication, no pagination and no write. The four routes it serves are the
ones the two readers use, and anything else is a 404 rather than an invention; the index at
`http://127.0.0.1:8766/` lists what it knows and the variables that point a reader at it.

### Linting the tools

`ruff` and `pyright` are in the `dev` dependency group, so the environment `uv sync` builds
has them:

```bash
uv run ruff check tools/            # imports, shadowed names, simplifications, bandit
uv run ruff format --check tools/   # formatting is mechanical, so it is not argued about
uv run pyright tools/               # types: `standard`, plus a complaint about a bare `dict`
```

CI runs all three, and the tools' tests with them, in `.github/workflows/quality.yml`: it
is a job of its own, separate from the content check, because a tool that stopped writing
what it is supposed to write is a different failure from a file that does not validate.
`# noqa` appears only where a rule is wrong for this repository, with the reason beside it:
the `git` and `gh` calls, whose executables come from `PATH` on purpose; the one `urllib`
request, whose target is the operator's `GITHUB_API_URL`; and the SHA-1 in `fake_api.py`,
which is the format Git writes rather than a security choice. The tests are `unittest`, and
`tools/test_toolkit.py` is exempted from the pytest-style rules rather than from the runner
it uses.

## Editor support

Schemas describe the files, so that a misspelt key or a malformed version is caught
while typing rather than by a failed push:

- `changelog/build.schema.json` — one build, that is anything in `changelog/<stream>/`.
- `changelog/streams.schema.json` — `changelog/streams.json`, in either of its shapes.
- `news/post.schema.json` — the front matter of a post.

The JSON files point at their schema with `$schema`, which the server ignores. For new
files that do not carry one, `.vscode/settings.json` maps the same patterns.

A Markdown front matter is YAML, which editors do not validate from a schema on their
own, so `news/post.schema.json` is enforced by `tools/validate.py` rather than while
typing.

The schemas reject unknown keys on purpose: `entry` where `entries` was meant is exactly
the mistake worth catching, and the server would refuse the whole file over it. Adding a
key to a build therefore means adding it to the schema first.

## Reading it locally

The server points at `GooGuTeam/g0v0-news@main` by default. To serve this checkout
instead, start `tools/fake_api.py` and aim the two base URLs at it:

```bash
uv run python tools/fake_api.py   # this checkout on 127.0.0.1:8766, plus a demo release
```

```ini
CONTENT_RAW_BASE_URL=http://127.0.0.1:8766
CONTENT_LISTING_BASE_URL=http://127.0.0.1:8766
CONTENT_REPOSITORY=local/g0v0-news
```

The listing needs a GitHub-compatible `GET /repos/{repository}/contents/{path}`
response — `type`, `name`, `path` and `sha` per entry — which a static server does not
provide on its own and this one does, with `sha` the Git blob id the server caches by. A
file's bytes are served at `/{repository}/{ref}/{path}`, the shape of a raw base URL. Without
a listing both listings answer `503`, because the server refuses to mistake "could not list"
for "nothing published".

## What is not here

Post bodies and changelog messages are served as Markdown. `content` — the rendered
body of a post — and `message_html` are always `null`, since the server has no
Markdown renderer. The Markdown itself is still returned unless the client asks only
for a format the server cannot produce: `content_formats[]` or `message_formats[]`
without `markdown` leaves `content_markdown` or `message` at `null`.
