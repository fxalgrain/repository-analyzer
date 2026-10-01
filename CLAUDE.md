# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Single-file Python CLI (`dora_metrics.py`) that computes the DORA "Lead Time for Change" metric (plus monthly release counts) from a local Git repository's commits and **annotated** tags, using GitPython. The only dependency is `GitPython` (`requirements.txt`).

## Commands

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

python dora_metrics.py --help
python dora_metrics.py -r <repo_path>                       # monorepo mode (default) → dora.csv + dora_summary.csv
python dora_metrics.py -r <repo_path> --no-monorepo         # single-service repo
python dora_metrics.py -r <repo_path> -o out -f json        # → out.json + out_summary.csv (summary is always CSV)
python dora_metrics.py -r <repo_path> --group-by team --teams-config teams.json   # per-team report (see teams.example.json)
```

There is no test suite, linter, or build configuration. `dora.csv` / `dora_summary.csv` in the repo root are gitignored sample outputs from a real run, useful as a reference for output shape.

## How the calculation works

Pipeline in `main()`: `calculate_lead_time_metrics` → `write_output` (detailed report) → `generate_summary_data` → `write_summary_report`.

- **Tags**: only annotated tags with an integer `tagged_date` are used (lightweight tags are ignored); they are sorted chronologically by tag creation date, not by commit date. Release date = tag creation date (UTC).
- **Commit range per tag**: `previous_release_commit..current_release_commit`. For the first tag (globally, or per service in monorepo mode), the range is the full history reachable from the tag commit.
- **Filtering**: `iter_commits(no_merges=True, invert_grep=True, grep="chore(.*): release .*")` — merge commits and release-bump commits are excluded.
- **Monorepo mode** (default, `--monorepo`/`--no-monorepo` via `argparse.BooleanOptionalAction`, so Python 3.9+ is actually required despite the README saying 3.7+):
  - Tags must be `{service}/{version}`; non-matching tags are skipped with a warning.
  - The previous-tag pointer is tracked per service.
  - A commit counts for a service only if it touches a path under `services/{service}/` (root commits are checked by traversing the tree). This path convention is hardcoded.
- **Lead time** = tag date − `commit.committed_datetime` (committer date, even though the output column is named `commit_author_date`), in fractional days.
- **Summary**: grouped by (`YYYY-MM` of release date, service); `average_lead_time_days` is the mean over commits (rounded to 4 decimals), `release_count` is the number of distinct tags. In non-monorepo mode the service name is `_overall_`.

## Team mode

`--group-by team --teams-config <json>` reuses the same tag/range/service-path pipeline, then `assign_metrics_to_teams` keeps only commits whose **author** (email or name, case-insensitive) is listed in a team, emitting one row per team (a person in several teams is counted in each). So a release counts for a team only if a member authored a commit in it, and lead time is averaged over the members' commits only. Detailed CSV gains `team_name`, `author_name`, `author_email`; the summary is grouped by (`YYYY-MM`, `team_name`).

## Cache

`calculate_lead_time_metrics` caches the entries of each tag range in `.dora_cache.json` (`--cache/--no-cache`, `--cache-file`, `--clear-cache`). The key (`make_cache_key`) is repo path + tag name + tag date + `rev_spec` (commit SHAs) + service + grep pattern, so immutable git data never goes stale. The cache stores pre-team-filter entries. Bump `CACHE_VERSION` whenever the entry shape or the commit-selection logic (e.g. the `services/{service}/` convention) changes, otherwise old entries will be served.

## Conventions

- Errors and warnings go to `stderr`; progress/info messages go to `stdout`. Failures generally log and return empty results rather than raising.
- Commit messages follow gitmoji-style conventional commits (e.g. `feat: :tada: ...`).
