# Contributing

This is a personal portfolio project. These conventions keep the history readable.

## Environment

```bash
uv sync
uv run prek install         # run the lint, format, and whitespace hooks on every commit
uv run pytest
uv run prek run --all-files # the same hooks CI runs
```

Run the CLI with `uv run maude-lakehouse --help`.

CI installs with `uv sync --locked`, so commit `uv.lock` whenever `pyproject.toml` changes.

## Branches and pull requests

- Every change goes through a pull request from a short-lived branch (`feat/bronze-parser`,
  `fix/embedded-newlines`, `docs/adr-0005-upload`).
- `main` is protected: the CI `check` job (hooks and tests) must pass before merging.
- Pull request descriptions say what changed, why, which alternatives were considered, and how it was tested.
- Squash-merge, so `main` has one scoped commit per pull request. The pull request title becomes that commit's
  message, so it follows the commit message format below.

## Commit messages

[Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`, `docs:`, `test:`, `refactor:`,
`chore:`, `ci:`. Example: `feat: record every landing in source_file_observations`.

## Releases

Starting with `v0.1.0` at the end of the spike, each milestone is tagged (`v0.1.0`, `v0.2.0`, ...) with a
changelog entry.

## Notebooks

Exploration only, in `notebooks/`, with outputs stripped before committing (for example
`uvx nbstripout notebooks/*.ipynb`). Logic that works moves into the package with tests.

## Decisions

Significant design decisions are recorded as ADRs in `docs/adr/`, numbered in order: context, decision,
alternatives considered, consequences.

- Status is `Proposed` while open and `Accepted` once decided.
- An accepted ADR is not rewritten. Corrections and dated update notes are fine; a changed decision gets a new
  ADR, and the old one's status becomes `Superseded by ADR NNNN`.

## Data

No data is committed. `data/` and `metadata/` are ignored. Profile reports made with `--samples` contain raw
values (names and narrative text) and are never committed. Published analysis stays at product-category level,
with no manufacturer comparisons or rankings.
