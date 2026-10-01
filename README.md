# DORA Metrics: Lead Time for Change Calculator

This Python CLI script calculates the "Lead Time for Change" DORA metric for a given local Git repository. It analyzes commit dates and tag creation dates to determine how long it takes for a committed change to be released.

## Features

* Calculates Lead Time for Change based on commit dates and annotated tag dates.
* Command-Line Interface (CLI) for easy integration into DevOps workflows.
* Uses `GitPython` for robust Git repository interaction.
* Supports output in `CSV` and `JSON` formats.
* Configurable repository path and output file.
* Analyses several repositories in one run (repeat `-r`, or list them in the config file) and tags every row with its repository.
* Caches per-tag results between runs, so only new tags are processed after the first run.
* Reports per service (default) or per team, using a team membership config file.

## Prerequisites

* Python 3.7+
* Git installed and accessible in your system's PATH.
* The target Git repository must have **annotated tags** with valid tagger dates for releases to be identified. Lightweight tags are ignored.

## Installation

1. **Clone the repository (or download the script):**
    If this script is part of a larger project:

    ```bash
    git clone <repository_url>
    cd <repository_directory>
    ```

    If you only have the `dora_metrics.py` and `requirements.txt` files, place them in a directory.

2. **Create and activate a virtual environment (recommended):**

    ```bash
    python3 -m venv venv
    source venv/bin/activate  # On Windows: venv\Scripts\activate
    ```

3. **Install dependencies:**

    ```bash
    pip install -r requirements.txt
    ```

    This will install `GitPython`.

## Usage

The script is run from the command line.

```bash
python dora_metrics.py --help
python dora_metrics.py --repo-path=<repo_path> [options]
```

### Reporting per team

Without teams, metrics are reported per service. To report per team, describe your teams in a JSON file named `config.json` (see `config.example.json`). `config.json` in the current directory is picked up automatically and is gitignored; use `--config PATH` for another file:

```json
{
  "teams": {
    "team-a": ["alice@example.com", "Bob Smith"],
    "team-b": ["carol@example.com"]
  }
}
```

Members are matched, case-insensitively, against the commit **author's** email or name. Then run:

```bash
python dora_metrics.py -r <repo_path>      # config.json found → per-team report
```

* A release counts for a team when at least one of its members authored a commit in it.
* Lead time is computed from the members' commits only.
* A person listed in several teams is counted in each of them.
* The detailed report adds `team_name`, `author_name` and `author_email` columns; the summary is grouped by month, repository and team (`year_month,repo_name,team_name,average_lead_time_days,release_count,commit_count`).

### Several repositories

Pass `-r` several times, or list the repositories in the config file (the same file as the teams, see `config.example.json`):

```json
{
  "teams": { "team-a": ["alice@example.com"] },
  "repositories": [
    "../service-repo",
    {"path": "../frontend", "name": "web", "monorepo": false}
  ]
}
```

```bash
python dora_metrics.py                                         # config.json: every listed repo, per team if it defines teams
python dora_metrics.py --group-by service                      # same repos, per service instead
python dora_metrics.py --config other.json                     # another config file
python dora_metrics.py -r ../a -r ../b                        # without a config file
```

* Relative paths in the config are resolved against the config file's directory; `~` is expanded. `-r` takes precedence over the config's `repositories`.
* `name` (default: directory name) is the `repo_name` in the output; duplicates get a numeric suffix. `monorepo` overrides `--monorepo` for that repository.
* Both reports gain a `repo_name` column, and the summary is grouped by month, repository and service/team. Non-monorepo repositories appear as `_overall_`.
* The cache is shared by all repositories (the repository path is part of each key).

### HTML report

Open `dora_report.html` and drop `dora.csv` / `dora_summary.csv` onto it (or serve the folder with `python3 -m http.server`). When the data covers several repositories, a filter lets you pick the repositories to include and switch the charts between service/team and repository grouping.

### Caching

The slow part of a run is walking each tag's commits. Results are therefore cached per tag in `.dora_cache.json` (gitignored), and later runs only compute tags that are new.

* Cache entries are keyed by the tag, its date and the exact commit range (SHAs), so a re-created tag or rewritten history is recomputed rather than served stale.
* The cache holds the data from before team filtering, so it is shared by service and team runs.
* `--no-cache` bypasses it for one run, `--clear-cache` deletes it first, and `--cache-file PATH` changes its location.
* A corrupt or incompatible cache file is ignored and rebuilt.
