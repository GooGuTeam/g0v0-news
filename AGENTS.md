# AGENTS.md

Notes for an agent -- or a person in a hurry -- changing this repository. `README.md` says
what the content means; this file says how to change it without breaking the server that
reads it.

## What this repository is

Two content trees, served as they are: `changelog/` (one JSON file per build) and `news/`
(one Markdown file per post). The server reads the files, caches them by Git blob SHA, and
renders nothing -- posts and entries are Markdown handed to the client. There is no build
step and no database, so a file that reaches `main` is published.

## Rules

1. **The schemas are the contract.** `changelog/build.schema.json`,
   `changelog/streams.schema.json` and `news/post.schema.json` are what the server
   enforces. Change a rule by changing the schema and its case in
   `tools/validate.py --self-test`; do not teach the checker an exception.
2. **Nothing under `changelog/` or `news/` is a draft.** Whatever is there is served. A
   draft belongs in `drafts/`, which no route reads.
3. **Do not rewrite published content to change what it means.** A typo is a fix; a
   different claim is a new build.
4. **The tools use the standard library.** `jsonschema` is the one dependency. HTTP, the
   GitHub API and git go through `urllib`, `subprocess` and `json`; a client library needs
   a reason larger than convenience.
5. **Commit tooling and documentation, not content.** `changelog/<stream>/` and `news/` are
   the maintainers' decisions. Leave those trees out of an agent's commit unless a specific
   file was asked for.

## Layout

| Path | What lives there |
| --- | --- |
| `changelog/<stream>/<version>.json` | one published build |
| `changelog/streams.json` | display names for the streams somebody wants to control |
| `news/<year>/<YYYYMMDD>-<slug>.md` | one published post; the file name is its slug |
| `drafts/<version>.json` | a build under review, one `{status, entry, provenance}` per entry |
| `tools/` | scripts, imported by bare name; deliberately not a package |
| `tools/static/index.html` | the console page: one file, no build step |

## Checks

```bash
uv run python tools/validate.py              # the content, against the schemas
uv run python tools/validate.py --self-test  # the schemas themselves
uv run python tools/test_toolkit.py          # the tools, against a temporary copy
uv run ruff check tools/ && uv run ruff format --check tools/
uv run pyright tools/
```

CI runs all five and a failure is a red build, not a note in review. Run them before saying
the work is done.

## Traps

- **`entries` has two shapes.** A draft stores `{status, entry, provenance}`; a published
  build stores the entry alone. The console converts at the view boundary
  (`wrapEntries` / `unwrapEntries`), and anything else reading both has to do the same --
  reading `item.entry` off a build gives `undefined` for every field.
- **The console's `h()` wants `on: { click: handler }`.** Written as `onclick:`, the
  function goes through `setAttribute`, which stringifies it into an invalid handler: the
  button renders and does nothing.
- **LF, always.** Some checkouts run with `core.autocrlf` on, so write files with
  `newline="\n"` or `ruff format --check` will disagree with what you committed.
- **A version carries its stream.** `2026.1009.0-g0v0` belongs in `changelog/g0v0/`, and
  the suffix is the stream: a mismatch is refused rather than guessed at.
- **Front matter is flat.** `key: value`, or an inline list. Nested blocks are read past,
  silently.

## Adding to `tools/`

Import neighbouring modules by bare name (`import content`). Give a public function a
docstring that says what it guarantees, not what it does -- the existing ones carry the
reason a rule exists, and that is what keeps a later change from undoing it. Add a test to
`tools/test_toolkit.py`, which runs against a temporary copy of this repository so it
cannot touch the checkout. Prose documentation belongs in `README.md`, not in a second
file.
