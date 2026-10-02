#!/usr/bin/env python3
# dora_metrics.py

import argparse
import csv
import json
from datetime import datetime, timedelta, timezone
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Any, Optional, Iterable, Tuple
from collections import defaultdict # For easier aggregation

try:
    import git
except ImportError:
    print("Error: GitPython is not installed. Please install it by running 'pip install GitPython'", file=sys.stderr)
    sys.exit(1)

# Define type aliases for clarity
MetricEntry = Dict[str, Any]
SummaryEntry = Dict[str, Any]

DEFAULT_SERVICE_NAME_FOR_SUMMARY = "_overall_"

SERVICE_FIELDNAMES = [
    "commit_hash", "commit_summary", "commit_author_date",
    "release_tag", "release_date", "lead_time_days", "service_name", "repo_name"
]
TEAM_FIELDNAMES = [
    "commit_hash", "commit_summary", "commit_author_date",
    "release_tag", "release_date", "lead_time_days", "service_name", "repo_name",
    "team_name", "author_name", "author_email"
]

DEFAULT_CONFIG_FILE = "config.json"
DEFAULT_CACHE_FILE = ".dora_cache.json"
# Bump when the shape or semantics of cached entries change (e.g. path convention, new fields).
CACHE_VERSION = 1

Cache = Dict[str, Any]


def load_cache(cache_path: str) -> Cache:
    """Loads the cache file; returns an empty cache if missing, unreadable or from another version."""
    empty: Cache = {"version": CACHE_VERSION, "entries": {}}
    if not os.path.exists(cache_path):
        return empty
    try:
        with open(cache_path, 'r', encoding='utf-8') as f:
            cache = json.load(f)
    except (IOError, json.JSONDecodeError) as e:
        print(f"Warning: Ignoring unreadable cache '{cache_path}': {e}", file=sys.stderr)
        return empty
    if not isinstance(cache, dict) or cache.get("version") != CACHE_VERSION \
            or not isinstance(cache.get("entries"), dict):
        print(f"INFO: Cache '{cache_path}' has an incompatible format; starting a fresh one.")
        return empty
    return cache


def save_cache(cache: Cache, cache_path: str) -> None:
    """Writes the cache atomically so an interrupted run cannot corrupt it."""
    tmp_path = f"{cache_path}.tmp"
    try:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(cache, f, ensure_ascii=False)
        os.replace(tmp_path, cache_path)
    except IOError as e:
        print(f"Warning: Could not write cache '{cache_path}': {e}", file=sys.stderr)


def make_cache_key(repo_path: str, tag_name: str, tag_timestamp: int, rev_spec: str,
                   service_name: Optional[str], grep_pattern: str) -> str:
    """
    Everything a tag's entries depend on. rev_spec holds the commit SHAs, so a moved or
    re-created tag, or rewritten history, yields a different key instead of stale data.
    """
    return "|".join([repo_path, tag_name, str(tag_timestamp), rev_spec,
                     service_name or "", grep_pattern])


def collect_tag_entries(repo: git.Repo, rev_spec: str, grep_pattern: str,
                        release_datetime: datetime, tag_name: str,
                        service_name: Optional[str]) -> List[MetricEntry]:
    """
    Computes the metric entries for one tag's commit range. When service_name is set,
    only commits touching 'services/{service_name}/' are kept.
    """
    entries: List[MetricEntry] = []
    for commit in repo.iter_commits(rev=rev_spec, no_merges=True, invert_grep=True, grep=grep_pattern):
        if service_name:
            is_relevant_to_service = False
            service_path_prefix = f"services/{service_name}/"
            if not commit.parents:
                for item in commit.tree.traverse(): # type: ignore
                    if item.type == 'blob' and item.path.startswith(service_path_prefix):
                        is_relevant_to_service = True
                        break
            else:
                for file_path in commit.stats.files.keys():
                    if file_path.startswith(service_path_prefix):
                        is_relevant_to_service = True
                        break
            if not is_relevant_to_service:
                continue

        commit_start_datetime = commit.committed_datetime
        lead_time_delta = release_datetime - commit_start_datetime
        entries.append({
            "commit_hash": commit.hexsha,
            "commit_summary": commit.summary,
            "commit_author_date": commit_start_datetime.isoformat(),
            "release_tag": tag_name,
            "release_date": release_datetime.isoformat(),
            "lead_time_days": lead_time_delta.total_seconds() / (24 * 60 * 60),
            "service_name": service_name,
            "author_name": commit.author.name,
            "author_email": commit.author.email
        })
    return entries


def get_annotated_tags_sorted(repo: git.Repo) -> List[git.TagReference]:
    """
    Fetches all annotated tags from the repository and sorts them chronologically
    by their 'tagged_date' (the creation timestamp of the tag object).
    """
    annotated_tags_refs: List[git.TagReference] = []
    for tag_ref in repo.tags:
        if tag_ref.tag is not None:
            if hasattr(tag_ref.tag, 'tagged_date') and \
               isinstance(tag_ref.tag.tagged_date, int):
                annotated_tags_refs.append(tag_ref)
            else:
                print(f"Warning: Annotated tag '{tag_ref.name}' is missing valid 'tagged_date' "
                      f"information (expected an integer timestamp). Skipping this tag for sorting.", file=sys.stderr)
    
    if not annotated_tags_refs:
        return []

    try:
        sorted_tags = sorted(annotated_tags_refs, key=lambda t_ref: t_ref.tag.tagged_date) # type: ignore[no-any-return,unused-ignore]
        return sorted_tags
    except AttributeError as e:
        print(f"Error sorting tags due to problematic 'tagged_date': {e}. "
              "Please check tag data integrity.", file=sys.stderr)
        return []


def parse_monorepo_tag(tag_name: str) -> Optional[Tuple[str, str]]:
    """
    Parses a tag name assuming the monorepo pattern "{service}/{version}".
    Returns (service_name, version_str) or None if not matching.
    """
    parts = tag_name.split('/', 1)
    if len(parts) == 2 and parts[0] and parts[1]:
        return parts[0], parts[1]
    return None


_log_lock = threading.Lock()


def make_logger(prefix: str):
    """print() replacement that tags lines with the repository and stays readable across threads."""
    def log(*args: Any, **kwargs: Any) -> None:
        with _log_lock:
            print(prefix, *args, **kwargs)
    return log


def parse_since(value: str, now: Optional[datetime] = None) -> datetime:
    """
    Parses --since: an ISO date ('2025-01-01') or a relative age ('1y', '6m', '8w', '90d')
    counted back from now. Returns a UTC datetime. Raises ValueError on anything else.
    """
    now = now or datetime.now(timezone.utc)
    value = value.strip()
    match = re.fullmatch(r"(\d+)([ymwd])", value.lower())
    if match:
        amount, unit = int(match.group(1)), match.group(2)
        if unit in "wd":
            return now - timedelta(days=amount * (7 if unit == "w" else 1))
        months = amount * (12 if unit == "y" else 1)
        index = now.year * 12 + (now.month - 1) - months
        year, month = divmod(index, 12)
        month += 1
        last_day = (datetime(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)).day
        return now.replace(year=year, month=month, day=min(now.day, last_day))
    try:
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    except ValueError:
        raise ValueError(f"invalid --since value '{value}': use YYYY-MM-DD or a relative age such as 1y, 6m, 8w, 90d")


def calculate_lead_time_metrics(repo_path: str, is_monorepo: bool,
                                cache: Optional[Cache] = None,
                                repo_name: Optional[str] = None,
                                since: Optional[datetime] = None) -> List[MetricEntry]:
    """
    Calculates Lead Time for Change for one repository. Every entry is labelled with repo_name
    (kept out of the cache, so renaming a repository in the config never serves stale labels).
    New entries are added to `cache` in place; the caller persists it.
    Releases tagged before `since` are skipped (not computed), but still advance the previous-tag
    pointers, so the first release inside the window gets the same commit range as in a full run.
    - Uses repo.iter_commits with no_merges=True and invert_grep=True for filtering.
    - If is_monorepo:
        - Tracks previous tag commit per service.
        - Filters commits based on paths like "services/{service_name}/...".
    """
    log = make_logger(f"[{repo_name or os.path.basename(os.path.normpath(repo_path))}]")
    try:
        repo = git.Repo(repo_path, search_parent_directories=True)
    except git.exc.InvalidGitRepositoryError:
        log(f"Error: '{repo_path}' is not a valid Git repository or a '.git' "
              "directory was not found in its path or parent directories.", file=sys.stderr)
        return []
    except git.exc.NoSuchPathError:
        log(f"Error: Repository path '{repo_path}' does not exist.", file=sys.stderr)
        return []
    except Exception as e:
        log(f"Error initializing Git repository at '{repo_path}': {e}", file=sys.stderr)
        return []

    sorted_annotated_tags = get_annotated_tags_sorted(repo)

    if not sorted_annotated_tags:
        log("No suitable annotated tags found (or tags lack 'tagged_date' info). "
              "Cannot calculate Lead Time for Change.", file=sys.stderr)
        return []

    all_metrics: List[MetricEntry] = []
    previous_tag_commits_per_service: Dict[str, str] = {}
    global_previous_tag_commit_sha: Optional[str] = None
    repo_name = repo_name or os.path.basename(os.path.normpath(repo_path))
    cache_hits = 0
    skipped_before_since = 0

    release_commit_grep_pattern = r"chore(.*): release .*"
    log(f"INFO: Commits will be filtered using iter_commits with no_merges=True "
          f"and excluding messages matching (via invert_grep): '{release_commit_grep_pattern}'")

    log(f"Processing {len(sorted_annotated_tags)} annotated tags...")
    if is_monorepo:
        log("INFO: Monorepo mode active. Tags expected: '{service}/{version}'. "
              "Commit ranges and paths will be service-specific (e.g., 'services/{service_name}/...').")

    for i, current_tag_ref in enumerate(sorted_annotated_tags):
        current_tag_object = current_tag_ref.tag
        release_timestamp = current_tag_object.tagged_date # type: ignore[attr-defined]
        release_datetime = datetime.fromtimestamp(release_timestamp, tz=timezone.utc)
        current_release_commit = current_tag_ref.commit
        current_release_commit_hexsha = current_release_commit.hexsha

        service_name_for_tag: Optional[str] = None
        rev_spec: str

        if is_monorepo:
            parsed_tag = parse_monorepo_tag(current_tag_ref.name)
            if parsed_tag:
                service_name_for_tag, _ = parsed_tag
                # log(f"  Processing monorepo tag: '{current_tag_ref.name}' for service: '{service_name_for_tag}'") # Verbose
                prev_service_commit_sha = previous_tag_commits_per_service.get(service_name_for_tag)
                if prev_service_commit_sha:
                    rev_spec = f"{prev_service_commit_sha}..{current_release_commit_hexsha}"
                else:
                    rev_spec = current_release_commit_hexsha
            else:
                log(f"  Warning: Tag '{current_tag_ref.name}' does not match monorepo pattern "
                      "'{service}/{version}'. Skipping this tag's processing in monorepo mode.", file=sys.stderr)
                continue 
        else:
            if global_previous_tag_commit_sha:
                rev_spec = f"{global_previous_tag_commit_sha}..{current_release_commit_hexsha}"
            else:
                rev_spec = current_release_commit_hexsha
        
        if since and release_datetime < since:
            skipped_before_since += 1
            if is_monorepo and service_name_for_tag:
                previous_tag_commits_per_service[service_name_for_tag] = current_release_commit_hexsha
            elif not is_monorepo:
                global_previous_tag_commit_sha = current_release_commit_hexsha
            continue

        cache_key = make_cache_key(repo_path, current_tag_ref.name, release_timestamp,
                                   rev_spec, service_name_for_tag, release_commit_grep_pattern)
        tag_entries: Optional[List[MetricEntry]] = cache["entries"].get(cache_key) if cache else None
        if tag_entries is not None:
            cache_hits += 1
        else:
            try:
                tag_entries = collect_tag_entries(repo, rev_spec, release_commit_grep_pattern,
                                                  release_datetime, current_tag_ref.name,
                                                  service_name_for_tag)
            except git.exc.GitCommandError as e:
                log(f"Warning: Could not retrieve and filter commits for range '{rev_spec}' "
                      f"(tag '{current_tag_ref.name}'). Error: {e}. Skipping this tag's new commits.", file=sys.stderr)
                if is_monorepo and service_name_for_tag:
                    previous_tag_commits_per_service[service_name_for_tag] = current_release_commit_hexsha
                elif not is_monorepo:
                    global_previous_tag_commit_sha = current_release_commit_hexsha
                continue
            if cache is not None:
                cache["entries"][cache_key] = tag_entries

        all_metrics.extend({**entry, "repo_name": repo_name} for entry in tag_entries)
        commits_processed_for_this_tag = len(tag_entries)

        tag_label = f"Tag '{current_tag_ref.name}'"
        if is_monorepo and service_name_for_tag:
            tag_label += f" (Service: {service_name_for_tag})"

        if commits_processed_for_this_tag > 0:
             log(f"  {tag_label} (Release Date: "
                   f"{release_datetime.strftime('%Y-%m-%d %H:%M:%S %Z')}): "
                   f"Processed {commits_processed_for_this_tag} relevant commits.")
        elif (not is_monorepo) or (is_monorepo and service_name_for_tag):
            if rev_spec != current_release_commit_hexsha :
                log(f"  {tag_label}: No new relevant commits found for range '{rev_spec}' after filtering.")
            else:
                log(f"  {tag_label} (Release Date: "
                      f"{release_datetime.strftime('%Y-%m-%d %H:%M:%S %Z')}): "
                      f"Processed {commits_processed_for_this_tag} initial relevant commits after filtering.")

        if is_monorepo and service_name_for_tag:
            previous_tag_commits_per_service[service_name_for_tag] = current_release_commit_hexsha
        elif not is_monorepo:
            global_previous_tag_commit_sha = current_release_commit_hexsha

    if since:
        log(f"INFO: {skipped_before_since} tags released before {since.date()} were skipped.")
    if cache is not None:
        log(f"INFO: Cache: {cache_hits} of {len(sorted_annotated_tags) - skipped_before_since} tags reused.")
    return all_metrics


RepoSpec = Dict[str, Any]  # {"path": str, "name": Optional[str], "monorepo": Optional[bool]}


CONFIG_KEYS = {"teams", "repositories", "since"}
REPO_KEYS = {"path", "name", "monorepo"}


def load_config(config_path: str) -> Optional[Tuple[Dict[str, List[str]], List[RepoSpec], Optional[str]]]:
    """
    Loads and validates the JSON config file:
      {
        "teams": {"team-name": ["email-or-name", ...]},                       (optional)
        "repositories": ["path", {"path": "...", "name": "...", "monorepo": false}],   (optional)
        "since": "1y"                                                          (optional, see --since)
      }
    Team members are matched case-insensitively against the commit author's email or name.
    Relative repository paths are resolved against the config file's directory.
    Every problem found is reported (not just the first), and unknown keys are rejected so that
    typos such as "repository" do not silently change the analysis.
    Returns ({team_name: [lowercased identifiers]}, [repo specs], since or None) or None if invalid.
    """
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
    except (IOError, json.JSONDecodeError) as e:
        print(f"Error reading config '{config_path}': {e}", file=sys.stderr)
        return None
    if not isinstance(config, dict):
        print(f"Error: config '{config_path}' must be a JSON object.", file=sys.stderr)
        return None

    errors: List[str] = []
    for key in config:
        if key not in CONFIG_KEYS:
            errors.append(f"unknown key '{key}' (expected: {', '.join(sorted(CONFIG_KEYS))})")

    teams: Dict[str, List[str]] = {}
    teams_raw = config.get("teams", {})
    if not isinstance(teams_raw, dict):
        errors.append("'teams' must map team names to lists of members")
    else:
        for team_name, members in teams_raw.items():
            if not isinstance(members, list) or not all(isinstance(m, str) for m in members):
                errors.append(f"team '{team_name}': members must be a list of strings")
                continue
            identifiers = [m.strip().lower() for m in members if m.strip()]
            if not identifiers:
                errors.append(f"team '{team_name}' has no members")
            teams[team_name] = identifiers

    repos: List[RepoSpec] = []
    repos_raw = config.get("repositories", [])
    base_dir = os.path.dirname(os.path.abspath(config_path))
    if not isinstance(repos_raw, list):
        errors.append("'repositories' must be a list")
        repos_raw = []
    for index, item in enumerate(repos_raw):
        spec = {"path": item} if isinstance(item, str) else item
        where = f"repositories[{index}]"
        if not isinstance(spec, dict) or not isinstance(spec.get("path"), str) or not spec["path"].strip():
            errors.append(f"{where}: must be a path string or an object with a non-empty 'path'")
            continue
        where = f"repositories[{index}] ('{spec['path']}')"
        for key in spec:
            if key not in REPO_KEYS:
                errors.append(f"{where}: unknown key '{key}' (expected: {', '.join(sorted(REPO_KEYS))})")
        if "name" in spec and (not isinstance(spec["name"], str) or not spec["name"].strip()):
            errors.append(f"{where}: 'name' must be a non-empty string")
        if "monorepo" in spec and not isinstance(spec["monorepo"], bool):
            errors.append(f"{where}: 'monorepo' must be true or false")
        repos.append({
            "path": os.path.normpath(os.path.join(base_dir, os.path.expanduser(spec["path"]))),
            "name": spec["name"].strip() if isinstance(spec.get("name"), str) and spec["name"].strip() else None,
            "monorepo": spec["monorepo"] if isinstance(spec.get("monorepo"), bool) else None,
        })
    errors.extend(check_repositories(repos, where_prefix="repositories"))

    since = config.get("since")
    if since is not None:
        if not isinstance(since, str):
            errors.append("'since' must be a string such as \"2025-01-01\" or \"1y\"")
        else:
            try:
                parse_since(since)
            except ValueError as e:
                errors.append(str(e).replace("--since value", "'since' value"))

    if errors:
        print(f"Error: invalid config '{config_path}':", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return None
    return teams, repos, since


def check_repositories(specs: List[RepoSpec], where_prefix: str = "repository") -> List[str]:
    """Returns one message per repository whose path is missing or is not inside a Git repository."""
    errors: List[str] = []
    for spec in specs:
        path = spec["path"]
        if not os.path.isdir(path):
            errors.append(f"{where_prefix} '{path}': directory does not exist")
            continue
        try:
            git.Repo(path, search_parent_directories=True)
        except git.exc.InvalidGitRepositoryError:
            errors.append(f"{where_prefix} '{path}': not a Git repository")
        except Exception as e:
            errors.append(f"{where_prefix} '{path}': cannot open ({e})")
    return errors


def unique_repo_names(specs: List[RepoSpec]) -> List[str]:
    """Display name per repo: explicit name, else directory name; duplicates get a numeric suffix."""
    seen: Dict[str, int] = {}
    names: List[str] = []
    for spec in specs:
        base = spec.get("name") or os.path.basename(os.path.normpath(spec["path"]))
        seen[base] = seen.get(base, 0) + 1
        if seen[base] > 1:
            name = f"{base}-{seen[base]}"
            print(f"Warning: repository name '{base}' is used more than once; "
                  f"'{spec['path']}' is reported as '{name}'. Set a 'name' to choose.", file=sys.stderr)
        else:
            name = base
        names.append(name)
    return names


def assign_metrics_to_teams(metrics_data: List[MetricEntry],
                            teams: Dict[str, List[str]]) -> List[MetricEntry]:
    """
    Keeps only commits authored by a team member and tags each with its team.
    A commit authored by someone in several teams yields one entry per team.
    Releases without any member commit therefore produce no entry for that team.
    """
    team_metrics: List[MetricEntry] = []
    for metric in metrics_data:
        identifiers = {
            (metric.get("author_email") or "").lower(),
            (metric.get("author_name") or "").lower(),
        }
        for team_name, members in teams.items():
            if identifiers.intersection(members):
                team_metrics.append({**metric, "team_name": team_name})
    return team_metrics


def generate_summary_data(metrics_data: List[MetricEntry],
                          group_field: str = "service_name") -> List[SummaryEntry]:
    """
    Aggregates detailed metrics data to produce a summary per (Year-Month, Repository, Service)
    or, with group_field="team_name", per (Year-Month, Repository, Team).
    Calculates average lead time, count of releases (unique tags per repository) and commits.
    Entries without a service (non-monorepo repositories) are grouped as '_overall_'.
    """
    if not metrics_data:
        return []

    grouped_data: Dict[Tuple[str, str, str], Dict[str, Any]] = defaultdict(
        lambda: {'lead_times_for_avg': [], 'release_tags_for_count': set()}
    )

    for metric in metrics_data:
        try:
            release_dt = datetime.fromisoformat(metric['release_date'])
        except ValueError:
            print(f"Warning: Could not parse release_date '{metric['release_date']}' for metric. Skipping for summary.", file=sys.stderr)
            continue

        year_month = release_dt.strftime("%Y-%m")
        group_name = metric.get(group_field) or DEFAULT_SERVICE_NAME_FOR_SUMMARY
        key = (year_month, metric.get('repo_name') or "", group_name)

        grouped_data[key]['lead_times_for_avg'].append(metric['lead_time_days'])
        grouped_data[key]['release_tags_for_count'].add(metric['release_tag'])

    final_summary_list: List[SummaryEntry] = []
    for (year_month, repo_name, group_name), data in grouped_data.items():
        lead_times = data['lead_times_for_avg']
        final_summary_list.append({
            "year_month": year_month,
            "repo_name": repo_name,
            group_field: group_name,
            "average_lead_time_days": round(sum(lead_times) / len(lead_times), 4),
            "release_count": len(data['release_tags_for_count']),
            "commit_count": len(lead_times)
        })

    final_summary_list.sort(key=lambda x: (x['repo_name'], x[group_field], x['year_month']))
    return final_summary_list


def write_output(metrics_data: List[MetricEntry], output_file: str, file_format: str,
                 fieldnames: List[str] = SERVICE_FIELDNAMES):
    """
    Writes the detailed collected metrics data to the specified output file.
    """
    print(f"Writing detailed report to '{output_file}' in {file_format} format...")
    try:
        with open(output_file, 'w', newline='', encoding='utf-8') as f:
            if file_format == 'csv':
                writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
                writer.writeheader()
                if metrics_data:
                    writer.writerows(metrics_data)
            elif file_format == 'json':
                projected = [{k: m.get(k) for k in fieldnames} for m in metrics_data]
                json.dump(projected, f, indent=4, ensure_ascii=False)
        print(f"Detailed report successfully written to '{output_file}'")
    except IOError as e:
        print(f"Error writing to detailed output file '{output_file}': {e}", file=sys.stderr)
    except Exception as e:
        print(f"An unexpected error occurred while writing detailed output file '{output_file}': {e}", file=sys.stderr)


def write_summary_report(summary_data: List[SummaryEntry], summary_output_file: str,
                         group_field: str = "service_name"):
    """
    Writes the aggregated summary data to a CSV file.
    """
    if not summary_data:
        print(f"No summary data to write to '{summary_output_file}'. File will not be created or will be empty with headers.")
    
    print(f"Writing summary report to '{summary_output_file}'...")
    try:
        with open(summary_output_file, 'w', newline='', encoding='utf-8') as f:
            fieldnames = ["year_month", "repo_name", group_field, "average_lead_time_days",
                          "release_count", "commit_count"]
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            if summary_data:
                writer.writerows(summary_data)
        print(f"Summary report successfully written to '{summary_output_file}'")
    except IOError as e:
        print(f"Error writing to summary output file '{summary_output_file}': {e}", file=sys.stderr)
    except Exception as e:
        print(f"An unexpected error occurred while writing summary output file '{summary_output_file}': {e}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Calculate Lead Time for Change DORA metric for one or several Git repositories. "
            "Outputs a detailed per-commit report and a monthly summary report per repository and service/team."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter # Shows defaults in help
    )
    parser.add_argument(
        "-r", "--repo-path",
        action="append",
        default=None,
        help=("File path to a local Git repository; repeat to analyse several. "
              "Defaults to the 'repositories' of --config, else the current directory.")
    )
    parser.add_argument(
        "-o", "--output-file",
        type=str,
        default='dora',
        help=("Base name of the file for the detailed report. "
              "Extension will be added based on --format (e.g., 'dora.csv', 'dora.json').")
    )
    parser.add_argument(
        "-f", "--format",
        type=str,
        choices=['csv', 'json'],
        default='csv',
        help="Desired output format for the detailed report."
    )
    
    parser.add_argument(
        "--group-by",
        choices=['service', 'team'],
        default=None,
        help=("Report per service or per team. Default: 'team' when the config defines teams, "
              "else 'service'. In 'team' mode a release counts for a "
              "team when at least one of its members authored a commit in it, and only the "
              "members' commits are used for lead time. Requires teams in --config.")
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help=(f"JSON config file (default: ./{DEFAULT_CONFIG_FILE} when it exists): "
              '{"teams": {"team-name": ["member@example.com", "Member Name"]}, '
              '"repositories": ["path", {"path": "...", "name": "...", "monorepo": false}]}. '
              "Team members are matched on commit author email or name (case-insensitive). "
              "See config.example.json.")
    )

    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help=("Only report releases (tag dates) on or after this date: YYYY-MM-DD or a relative age "
              "such as 1y, 6m, 8w, 90d. Skipped releases are not computed; later releases keep their "
              "exact commit range. Can also be set as \"since\" in the config file.")
    )
    parser.add_argument(
        "-j", "--jobs",
        type=int,
        default=4,
        help="Number of repositories analysed in parallel."
    )
    parser.add_argument(
        "--cache",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=("Reuse per-tag results from previous runs. Cached entries are keyed by tag and "
              "commit SHAs, so new tags are the only ones computed on later runs.")
    )
    parser.add_argument(
        "--cache-file",
        type=str,
        default=DEFAULT_CACHE_FILE,
        help="Path of the cache file."
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="Discard the existing cache before running."
    )

    # For Python 3.9+
    parser.add_argument(
        '--monorepo',
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable monorepo mode (default). Use --no-monorepo to disable."
    )
    # Fallback for Python < 3.9 (if BooleanOptionalAction is not available):
    # parser.add_argument(
    #     '--no-monorepo',
    #     action='store_false',
    #     dest='monorepo',
    #     help="Disable monorepo mode (monorepo mode is enabled by default)."
    # )
    # parser.set_defaults(monorepo=True)


    args = parser.parse_args()

    teams: Optional[Dict[str, List[str]]] = None
    config_repos: List[RepoSpec] = []
    config_since: Optional[str] = None
    config_path = args.config
    if not config_path and os.path.isfile(DEFAULT_CONFIG_FILE):
        config_path = DEFAULT_CONFIG_FILE
        print(f"INFO: Using default config '{DEFAULT_CONFIG_FILE}'.")
    if config_path:
        loaded = load_config(config_path)
        if loaded is None:
            sys.exit(1)
        config_teams, config_repos, config_since = loaded
        teams = config_teams or None
    group_by = args.group_by or ('team' if teams else 'service')
    if group_by == 'team' and not teams:
        parser.error("--group-by team requires a non-empty 'teams' object in the config file")
    if group_by != 'team':
        teams = None

    if args.repo_path:
        repo_specs: List[RepoSpec] = [{"path": os.path.abspath(p), "name": None, "monorepo": None}
                                      for p in args.repo_path]
    elif config_repos:
        repo_specs = config_repos
    else:
        repo_specs = [{"path": os.path.abspath("."), "name": None, "monorepo": None}]
    if args.repo_path or not config_repos:
        # repositories listed in the config were already checked by load_config
        repo_errors = check_repositories(repo_specs)
        if repo_errors:
            print("Error: invalid repositories:", file=sys.stderr)
            for error in repo_errors:
                print(f"  - {error}", file=sys.stderr)
            sys.exit(1)
    repo_names = unique_repo_names(repo_specs)

    since: Optional[datetime] = None
    since_raw = args.since or config_since
    if since_raw:
        try:
            since = parse_since(since_raw)
        except ValueError as e:
            parser.error(str(e))
        print(f"INFO: Only releases on or after {since.date()} are analysed.")
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")

    # Construct full output filename with extension based on format
    base_name_from_arg = args.output_file
    # Remove any user-supplied extension from the base name if they provided one,
    # as we'll append the one from --format.
    base_output_name, _ = os.path.splitext(base_name_from_arg)
    detailed_output_filename = f"{base_output_name}.{args.format}"

    if args.clear_cache and os.path.exists(args.cache_file):
        os.remove(args.cache_file)
        print(f"INFO: Removed cache '{args.cache_file}'.")

    cache: Optional[Cache] = load_cache(args.cache_file) if args.cache else None
    cached_before = len(cache["entries"]) if cache is not None else 0

    def analyse(item: Tuple[RepoSpec, str]) -> List[MetricEntry]:
        spec, repo_name = item
        is_monorepo = args.monorepo if spec.get("monorepo") is None else spec["monorepo"]
        print(f"Analyzing Git repository '{repo_name}' at: {spec['path']} "
              f"(monorepo mode {'ENABLED' if is_monorepo else 'DISABLED'})")
        return calculate_lead_time_metrics(spec["path"], is_monorepo, cache, repo_name, since)

    # Repositories are independent and the work is git-subprocess bound, so threads are enough.
    # map() keeps the config order, so the reports are identical whatever --jobs is.
    detailed_metrics_data: List[MetricEntry] = []
    with ThreadPoolExecutor(max_workers=min(args.jobs, len(repo_specs))) as pool:
        for repo_metrics in pool.map(analyse, zip(repo_specs, repo_names)):
            detailed_metrics_data.extend(repo_metrics)

    if cache is not None and len(cache["entries"]) != cached_before:
        save_cache(cache, args.cache_file)

    group_field = "service_name"
    output_fieldnames = SERVICE_FIELDNAMES
    if teams is not None:
        detailed_metrics_data = assign_metrics_to_teams(detailed_metrics_data, teams)
        group_field = "team_name"
        output_fieldnames = TEAM_FIELDNAMES
    write_output(detailed_metrics_data, detailed_output_filename, args.format, output_fieldnames)

    if detailed_metrics_data:
        print(f"Generated {len(detailed_metrics_data)} detailed lead time entries.")
        summary_data = generate_summary_data(detailed_metrics_data, group_field)
        
        summary_base_name, _ = os.path.splitext(detailed_output_filename) # Use base from detailed
        summary_output_filename = f"{summary_base_name}_summary.csv"
        
        write_summary_report(summary_data, summary_output_filename, group_field)
        if summary_data:
            print(f"Generated {len(summary_data)} summary entries.")
        else:
            print("No summary entries were generated from the detailed metrics.")
    else:
        print("No detailed DORA Lead Time for Change metrics were generated. "
              "The output file might be empty or contain only headers. Summary report will not be generated.", file=sys.stderr)

if __name__ == "__main__":
    main()