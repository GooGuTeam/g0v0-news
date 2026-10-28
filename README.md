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
```

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

`uv.lock` is committed, so the version `jsonschema` resolves to is the one the schema
checks were written against. It records whichever package index resolved it; if your
index differs from the one in the lock, `uv lock --upgrade` resolves it again.

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
instead, start a static server and a fake contents API over it, then aim the two
base URLs at that server:

```ini
CONTENT_RAW_BASE_URL=http://127.0.0.1:8000
CONTENT_LISTING_BASE_URL=http://127.0.0.1:8000
CONTENT_REPOSITORY=local/g0v0-news
```

The listing needs a GitHub-compatible `GET /repos/{repository}/contents/{path}`
response — `type`, `name`, `path` and `sha` per entry — which a static server does not
provide on its own. Without it both listings answer `503`, because the server refuses
to mistake "could not list" for "nothing published".

## What is not here

Post bodies and changelog messages are served as Markdown. `content` — the rendered
body of a post — and `message_html` are always `null`, since the server has no
Markdown renderer. The Markdown itself is still returned unless the client asks only
for a format the server cannot produce: `content_formats[]` or `message_formats[]`
without `markdown` leaves `content_markdown` or `message` at `null`.
