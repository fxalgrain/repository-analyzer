#!/usr/bin/env python3
# dora_metrics.py

import argparse
import csv
import json
from datetime import datetime, timezone
import os
import sys
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


def calculate_lead_time_metrics(repo_path: str, is_monorepo: bool) -> List[MetricEntry]:
    """
    Calculates Lead Time for Change.
    - Uses repo.iter_commits with no_merges=True and invert_grep=True for filtering.
    - If is_monorepo:
        - Tracks previous tag commit per service.
        - Filters commits based on paths like "services/{service_name}/...".
    """
    try:
        repo = git.Repo(repo_path, search_parent_directories=True)
    except git.exc.InvalidGitRepositoryError:
        print(f"Error: '{repo_path}' is not a valid Git repository or a '.git' "
              "directory was not found in its path or parent directories.", file=sys.stderr)
        return []
    except git.exc.NoSuchPathError:
        print(f"Error: Repository path '{repo_path}' does not exist.", file=sys.stderr)
        return []
    except Exception as e:
        print(f"Error initializing Git repository at '{repo_path}': {e}", file=sys.stderr)
        return []

    sorted_annotated_tags = get_annotated_tags_sorted(repo)

    if not sorted_annotated_tags:
        print("No suitable annotated tags found (or tags lack 'tagged_date' info). "
              "Cannot calculate Lead Time for Change.", file=sys.stderr)
        return []

    all_metrics: List[MetricEntry] = []
    previous_tag_commits_per_service: Dict[str, str] = {}
    global_previous_tag_commit_sha: Optional[str] = None

    release_commit_grep_pattern = r"chore(.*): release .*"
    print(f"INFO: Commits will be filtered using iter_commits with no_merges=True "
          f"and excluding messages matching (via invert_grep): '{release_commit_grep_pattern}'")

    print(f"Processing {len(sorted_annotated_tags)} annotated tags...")
    if is_monorepo:
        print("INFO: Monorepo mode active. Tags expected: '{service}/{version}'. "
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
                # print(f"  Processing monorepo tag: '{current_tag_ref.name}' for service: '{service_name_for_tag}'") # Verbose
                prev_service_commit_sha = previous_tag_commits_per_service.get(service_name_for_tag)
                if prev_service_commit_sha:
                    rev_spec = f"{prev_service_commit_sha}..{current_release_commit_hexsha}"
                else:
                    rev_spec = current_release_commit_hexsha
            else:
                print(f"  Warning: Tag '{current_tag_ref.name}' does not match monorepo pattern "
                      "'{service}/{version}'. Skipping this tag's processing in monorepo mode.", file=sys.stderr)
                continue 
        else:
            if global_previous_tag_commit_sha:
                rev_spec = f"{global_previous_tag_commit_sha}..{current_release_commit_hexsha}"
            else:
                rev_spec = current_release_commit_hexsha
        
        try:
            commits_for_tag_iterator = repo.iter_commits(
                rev=rev_spec,
                no_merges=True,
                invert_grep=True,
                grep=release_commit_grep_pattern
            )
        except git.exc.GitCommandError as e:
            print(f"Warning: Could not retrieve and filter commits for range '{rev_spec}' "
                  f"(tag '{current_tag_ref.name}'). Error: {e}. Skipping this tag's new commits.", file=sys.stderr)
            if is_monorepo and service_name_for_tag:
                previous_tag_commits_per_service[service_name_for_tag] = current_release_commit_hexsha
            elif not is_monorepo:
                global_previous_tag_commit_sha = current_release_commit_hexsha
            continue

        commits_processed_for_this_tag = 0
        for commit in commits_for_tag_iterator:
            if is_monorepo and service_name_for_tag:
                is_relevant_to_service = False
                service_path_prefix = f"services/{service_name_for_tag}/"
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
            commit_summary = commit.summary
            lead_time_delta = release_datetime - commit_start_datetime
            lead_time_days = lead_time_delta.total_seconds() / (24 * 60 * 60)

            metric_entry: MetricEntry = {
                "commit_hash": commit.hexsha,
                "commit_summary": commit_summary,
                "commit_author_date": commit_start_datetime.isoformat(),
                "release_tag": current_tag_ref.name,
                "release_date": release_datetime.isoformat(),
                "lead_time_days": lead_time_days,
                "service_name": service_name_for_tag if is_monorepo else None
            }
            all_metrics.append(metric_entry)
            commits_processed_for_this_tag +=1
        
        tag_label = f"Tag '{current_tag_ref.name}'"
        if is_monorepo and service_name_for_tag:
            tag_label += f" (Service: {service_name_for_tag})"

        if commits_processed_for_this_tag > 0:
             print(f"  {tag_label} (Release Date: "
                   f"{release_datetime.strftime('%Y-%m-%d %H:%M:%S %Z')}): "
                   f"Processed {commits_processed_for_this_tag} relevant commits.")
        elif (not is_monorepo) or (is_monorepo and service_name_for_tag):
            if rev_spec != current_release_commit_hexsha :
                print(f"  {tag_label}: No new relevant commits found for range '{rev_spec}' after filtering.")
            else:
                print(f"  {tag_label} (Release Date: "
                      f"{release_datetime.strftime('%Y-%m-%d %H:%M:%S %Z')}): "
                      f"Processed {commits_processed_for_this_tag} initial relevant commits after filtering.")

        if is_monorepo and service_name_for_tag:
            previous_tag_commits_per_service[service_name_for_tag] = current_release_commit_hexsha
        elif not is_monorepo:
            global_previous_tag_commit_sha = current_release_commit_hexsha
            
    return all_metrics


def generate_summary_data(metrics_data: List[MetricEntry], is_monorepo: bool) -> List[SummaryEntry]:
    """
    Aggregates detailed metrics data to produce a summary per (Year-Month, Service).
    Calculates average lead time and count of releases (unique tags).
    """
    if not metrics_data:
        return []

    grouped_data: Dict[Tuple[str, str], Dict[str, Any]] = defaultdict(
        lambda: {'lead_times_for_avg': [], 'release_tags_for_count': set()}
    )

    for metric in metrics_data:
        try:
            release_dt = datetime.fromisoformat(metric['release_date'])
        except ValueError:
            print(f"Warning: Could not parse release_date '{metric['release_date']}' for metric. Skipping for summary.", file=sys.stderr)
            continue
            
        year_month = release_dt.strftime("%Y-%m")
        
        raw_service_name = metric.get('service_name')
        if is_monorepo and raw_service_name:
            current_service_name_for_grouping = raw_service_name
        else:
            current_service_name_for_grouping = DEFAULT_SERVICE_NAME_FOR_SUMMARY

        key = (year_month, current_service_name_for_grouping)
        
        grouped_data[key]['lead_times_for_avg'].append(metric['lead_time_days'])
        grouped_data[key]['release_tags_for_count'].add(metric['release_tag'])

    final_summary_list: List[SummaryEntry] = []
    for (year_month, service_name), data in grouped_data.items():
        avg_lead_time = (
            sum(data['lead_times_for_avg']) / len(data['lead_times_for_avg'])
            if data['lead_times_for_avg']
            else 0.0
        )
        tag_count = len(data['release_tags_for_count'])
        
        final_summary_list.append({
            "year_month": year_month,
            "service_name": service_name,
            "average_lead_time_days": round(avg_lead_time, 4),
            "release_count": tag_count
        })

    final_summary_list.sort(key=lambda x: (x['service_name'], x['year_month']))
    return final_summary_list


def write_output(metrics_data: List[MetricEntry], output_file: str, file_format: str):
    """
    Writes the detailed collected metrics data to the specified output file.
    """
    print(f"Writing detailed report to '{output_file}' in {file_format} format...")
    try:
        with open(output_file, 'w', newline='', encoding='utf-8') as f:
            if file_format == 'csv':
                fieldnames = [
                    "commit_hash", "commit_summary", "commit_author_date", 
                    "release_tag", "release_date", "lead_time_days", "service_name"
                ]
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                if metrics_data:
                    writer.writerows(metrics_data)
            elif file_format == 'json':
                json.dump(metrics_data, f, indent=4, ensure_ascii=False)
        print(f"Detailed report successfully written to '{output_file}'")
    except IOError as e:
        print(f"Error writing to detailed output file '{output_file}': {e}", file=sys.stderr)
    except Exception as e:
        print(f"An unexpected error occurred while writing detailed output file '{output_file}': {e}", file=sys.stderr)


def write_summary_report(summary_data: List[SummaryEntry], summary_output_file: str):
    """
    Writes the aggregated summary data to a CSV file.
    """
    if not summary_data:
        print(f"No summary data to write to '{summary_output_file}'. File will not be created or will be empty with headers.")
    
    print(f"Writing summary report to '{summary_output_file}'...")
    try:
        with open(summary_output_file, 'w', newline='', encoding='utf-8') as f:
            fieldnames = ["year_month", "service_name", "average_lead_time_days", "release_count"]
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
            "Calculate Lead Time for Change DORA metric for a Git repository. "
            "Outputs a detailed per-commit report and a monthly summary report per service."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter # Shows defaults in help
    )
    parser.add_argument(
        "-r", "--repo-path",
        type=str,
        default=".",
        help="File path to the local Git repository."
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

    # Construct full output filename with extension based on format
    base_name_from_arg = args.output_file
    # Remove any user-supplied extension from the base name if they provided one,
    # as we'll append the one from --format.
    base_output_name, _ = os.path.splitext(base_name_from_arg)
    detailed_output_filename = f"{base_output_name}.{args.format}"

    abs_repo_path = os.path.abspath(args.repo_path)
    
    print(f"Analyzing Git repository at: {abs_repo_path}")
    if args.monorepo:
        print("INFO: Monorepo mode is ENABLED.")
    else:
        print("INFO: Monorepo mode is DISABLED.")

    detailed_metrics_data = calculate_lead_time_metrics(abs_repo_path, args.monorepo)
    write_output(detailed_metrics_data, detailed_output_filename, args.format)

    if detailed_metrics_data:
        print(f"Generated {len(detailed_metrics_data)} detailed lead time entries.")
        summary_data = generate_summary_data(detailed_metrics_data, args.monorepo)
        
        summary_base_name, _ = os.path.splitext(detailed_output_filename) # Use base from detailed
        summary_output_filename = f"{summary_base_name}_summary.csv"
        
        write_summary_report(summary_data, summary_output_filename)
        if summary_data:
            print(f"Generated {len(summary_data)} summary entries.")
        else:
            print("No summary entries were generated from the detailed metrics.")
    else:
        print("No detailed DORA Lead Time for Change metrics were generated. "
              "The output file might be empty or contain only headers. Summary report will not be generated.", file=sys.stderr)

if __name__ == "__main__":
    main()