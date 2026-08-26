# DORA Metrics: Lead Time for Change Calculator

This Python CLI script calculates the "Lead Time for Change" DORA metric for a given local Git repository. It analyzes commit dates and tag creation dates to determine how long it takes for a committed change to be released.

## Features

* Calculates Lead Time for Change based on commit dates and annotated tag dates.
* Command-Line Interface (CLI) for easy integration into DevOps workflows.
* Uses `GitPython` for robust Git repository interaction.
* Supports output in `CSV` and `JSON` formats.
* Configurable repository path and output file.

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
