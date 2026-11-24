# Contributing

This is a personal portfolio project. These conventions keep the history readable.

## Environment

```bash
uv sync
uv run pytest
```

Run the CLI with `uv run maude-lakehouse --help`.

## Branches

- During early exploration, small commits go straight to `main`.
- Once CI exists: one short-lived branch per change (`feat/bronze-parser`, `fix/embedded-newlines`,
  `docs/adr-0003-table-format`), merged by pull request.
- Pull request descriptions say what changed, why, which alternatives were considered, and how it was tested.
- Squash-merge, so `main` has one scoped commit per pull request.
- `main` is protected once CI exists: checks must pass before merging.

## Commit messages

[Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`, `docs:`, `test:`, `refactor:`,
`chore:`. Example: `feat: record every landing in source_file_observations`.

## Releases

Each milestone is tagged (`v0.1.0`, `v0.2.0`, ...) with a changelog entry.

## Notebooks

Exploration only, in `notebooks/`, with outputs stripped (`nbstripout`). Logic that works moves into the
package with tests.

## Decisions

Significant design decisions are recorded as ADRs in `docs/adr/`: context, decision, alternatives considered,
consequences.

## Data

No data is committed. `data/` and `metadata/` are ignored. Published analysis stays at product-category level,
with no manufacturer comparisons or rankings.
