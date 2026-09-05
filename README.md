# BibAudit 📚🔍

**BibAudit** is a Python tool suite designed to audit, resolve, and conservatively correct BibTeX bibliographies using scholarly metadata APIs (**Crossref**, **DataCite**, and **OpenAlex**).

It provides both a **command-line interface (CLI)** for automated batch pipelines / terminal-based review and a rich **Graphical User Interface (GUI)** for visual inspection, sensitivity adjustments, and interactive entry-by-entry decision making.

---

## Table of Contents

- [Key Features](#key-features)
- [Prerequisites & Installation](#prerequisites--installation)
- [Graphical User Interface (GUI) Guide](#graphical-user-interface-gui-guide)
  - [Launching the GUI](#launching-the-gui)
  - [GUI Walkthrough & Features](#gui-walkthrough--features)
  - [Sensitivity Presets & Sliders](#sensitivity-presets--sliders)
- [Command-Line Interface (CLI) Guide](#command-line-interface-cli-guide)
  - [Basic Usage](#basic-usage)
  - [Interactive Terminal Feedback Mode](#interactive-terminal-feedback-mode)
  - [CLI Command-Line Options](#cli-command-line-options)
  - [Practical CLI Examples](#practical-cli-examples)
- [Execution Modes](#execution-modes)
- [Understanding Statuses & Reports](#understanding-statuses--reports)
- [Persistent Cache & Decisions Store](#persistent-cache--decisions-store)
- [Running Unit Tests](#running-unit-tests)

---

## Key Features

- **Multi-Source Scholarly Resolution**:
  - Direct DOI lookups via Crossref, DataCite, and OpenAlex.
  - Multi-engine title search when DOI is missing, invalid, or mismatched.
- **Weighted Multi-Metric Scoring**:
  - Compares titles (fuzzy token + sequence matching), author family names, publication years, and container/journal titles.
  - Computes confidence scores and enforces safety margins between candidates to prevent false positives.
- **Conservative & Non-Destructive**:
  - Original entries and custom BibTeX fields are preserved unless high-confidence updates are identified.
  - Handles complex LaTeX macros, accents, and combining characters safely without corrupting formatting.
- **Dual Interface**:
  - **Modern Tkinter GUI (`bib_audit_gui.py`)**: Visual diffs, interactive review hub, sensitivity sliders, presets, and live logging.
  - **CLI Script (`bib_audit_cli.py`)**: Scriptable, batch-friendly, with optional interactive terminal prompt.
- **Persistent State**:
  - Resumable query cache (`.bib-audit-cache.json`) to minimize HTTP requests and respect rate limits.
  - Persistent manual decisions store (`.bib-audit-decisions.json`) so your reviews are remembered across runs.
- **Comprehensive Reporting**:
  - Cleaned & enriched `.bib` output.
  - Detailed `.csv` audit matrix.
  - Human-readable `.md` summary report highlighting problematic references.

---

## Prerequisites & Installation

### 1. Requirements

- **Python 3.8+**
- Standard Python libraries (`tkinter`, `urllib3`, `requests`, `bibtexparser 1.4.3`)

### 2. Install Python Dependencies

Clone or download the repository, set up a virtual environment (recommended), and install the required packages:

```bash
# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install requirements
pip install -r requirements.txt
```

### 3. Tkinter (for GUI)

Tkinter is bundled with standard Python distributions on Windows and macOS. If you encounter a `ModuleNotFoundError: No module named 'tkinter'`, install it via your system package manager:

- **Ubuntu / Debian**:
  ```bash
  sudo apt-get install python3-tk
  ```
- **macOS (Homebrew Python)**:
  ```bash
  brew install python-tk
  ```
- **Fedora / RHEL**:
  ```bash
  sudo dnf install python3-tkinter
  ```

---

## Graphical User Interface (GUI) Guide

The GUI version offers a complete workspace to run audits, adjust sensitivity thresholds in real-time, inspect diffs, and review ambiguous references side-by-side.

### Launching the GUI

```bash
python bib_audit_gui.py
```

```
+------------------------------------------------------------------------------------+
|  BibAudit v4.2.0-gui                                                               |
+------------------------------------------------------------------------------------+
|  Input File: [ my_thesis.bib             ] [Browse...]                             |
|  Output:     [ bib-verified.bib          ] [Browse...]                             |
|  Reports:    [ CSV ] [ Markdown ]                                                  |
|  Mode:       ( ) Automatic     (*) Hybrid (User Feedback)    ( ) Manual            |
|  [ ▶ Start Audit ]  [ ⏸ Pause ]  [ ⏹ Stop ]                                        |
+------------------------------------------------------------------------------------+
| [ 📋 Interactive Review ] [ ⚙️ Settings & Sliders ] [ 🔍 Diff ] [ 📜 Console ]      |
| +-----------------------------------+--------------------------------------------+ |
| | Key       | Status   | Title      | Selected Entry Inspector                   | |
| |-----------+----------+------------| Original vs Candidate comparison           | |
| | smith2020 | Corrected| Deep Learn | [Accept Candidate] [Keep Original] [Edit]  | |
| | doe2022   | To review| Quantum ML | Live Search Box for DOI / Title lookup     | |
| +-----------------------------------+--------------------------------------------+ |
+------------------------------------------------------------------------------------+
```

### GUI Walkthrough & Features

#### 1. Top Configuration Bar
- **File Pickers**: Select input `.bib` file, destination `.bib` file, CSV report, Markdown summary, and persistent storage files.
- **Execution Mode**: Choose between **Full Automatic**, **Hybrid (User Feedback)**, or **Full Manual**.
- **Run Controls**: Start (`▶`), Pause (`⏸`), or Stop (`⏹`) background processing with real-time progress indicators.

#### 2. Tab: 📋 Interactive Review & Decisions
- **Status Filter**: Filter entries by `All`, `To review`, `Not found`, `Corrected`, `Verified`, or `Error`.
- **Search Bar**: Quick text search over citation keys and titles.
- **Side-by-Side Inspector**: Compares original BibTeX metadata with candidate data side by side (Title, DOI, Authors, Year, Journal/Booktitle, Source, Confidence score).
- **Actions**:
  - **Accept Candidate**: Applies candidate metadata and DOI.
  - **Keep Original**: Marks entry as reviewed while keeping original fields intact.
  - **Manually Edit**: Saves manual in-place field edits.
  - **Search Online...**: Query Crossref / OpenAlex manually with custom DOI or search query.

#### 3. Tab: ⚙️ Settings & Sensitivity Sliders
- Adjust similarity thresholds (`title_strong`, `title_good`, `author_min`, `year_min`, `margin_min`).
- Configure weights for overall confidence calculation (Title, Author, Year, Container).
- Toggle options such as overwriting existing non-empty bibliographic fields (`overwrite_bibliographic_fields`).
- **One-Click Presets**:
  - **Very Strict (Prudent)**: High safety standards; avoids modifications unless matches are unambiguous.
  - **Balanced (Standard)**: Recommended balance of precision and recall.
  - **Permissive (Flexible)**: More lenient matching thresholds for difficult or incomplete bibliographies.

#### 4. Tab: 🔍 BibTeX Preview & Diff
- Shows side-by-side formatted BibTeX snippets comparing the original entry with the updated entry.

#### 5. Tab: 📜 Console & Logs
- Displays real-time logging of network requests, API responses, cache hits, scoring steps, and status changes.

---

## Command-Line Interface (CLI) Guide

The CLI script `bib_audit_cli.py` is suitable for server environments, automated workflows, and terminal users.

### Basic Usage

Run a non-interactive audit with default settings:

```bash
python bib_audit_cli.py input.bib
```

This generates:
- `bib-verified.bib`: Corrected BibTeX file.
- `audit-report.csv`: Detailed audit report.
- `audit-summary.md`: Markdown summary.
- `.bib-audit-cache.json`: Saved API responses cache.
- `.bib-audit-decisions.json`: Saved user decisions.

### Interactive Terminal Feedback Mode

Use `--feedback` to be prompted interactively in the terminal for any ambiguous reference (`to review` or `not found`):

```bash
python bib_audit_cli.py input.bib --feedback
```

When an entry needs review, an interactive prompt appears:

```text
==============================================================================
 Entry [14/240]: vanella_evolution_2025 
==============================================================================
Bib Title : Evolution of Digital Identity in Europe
Bib DOI   : [absent]
Authors   : Vanella, J. and Smith, A.
Year/Jour : 2025 / Journal of Digital Trust
------------------------------------------------------------------------------
Candidate : Evolution of Digital Identity in Europe: eIDAS 2.0
Found DOI : 10.1016/j.trust.2025.100123
Source    : Crossref
Scores    : Title=0.91 | Authors=0.85 | Year=1.00 | Overall=0.91
Reason    : score moderate; manual validation recommended

[a] accept candidate  [m] manually edit  [k] keep original  [q] quit and save
Your choice [a/m/k/q]: 
```

**Actions available in prompt**:
- `a`: Accept candidate and apply DOI/metadata.
- `m`: Manually edit Title, DOI, Authors, or Year with inline defaults.
- `k`: Keep original entry unchanged.
- `q`: Gracefully exit and save all work done so far to disk.

### CLI Command-Line Options

| Option | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `input` | `Path` | *Required* | Path to input `.bib` or `.bib.txt` file. |
| `--output` | `Path` | `bib-verified.bib` | Path to output corrected BibTeX file. |
| `--report` | `Path` | `audit-report.csv` | Path to output detailed CSV report. |
| `--summary` | `Path` | `audit-summary.md` | Path to output Markdown summary. |
| `--cache` | `Path` | `.bib-audit-cache.json` | Path to persistent HTTP API cache file. |
| `--decisions` | `Path` | `.bib-audit-decisions.json` | Path to persistent manual decisions file. |
| `--reset-decisions` | `Flag` | `False` | Reset previously saved manual decisions. |
| `--feedback` | `Flag` | `False` | Enable interactive terminal prompt for ambiguous entries. |
| `--overwrite-bibliographic-fields` | `Flag` | `False` | Overwrite existing `journal`/`booktitle`, `volume`, `pages`, `publisher` fields even if already populated. |
| `--delay` | `Float` | `0.5` | Minimum delay between HTTP requests (in seconds). |
| `--limit` | `Int` | `0` | Process only first *N* entries (0 = process all). |
| `--openalex-api-key` | `String` | `""` | Optional OpenAlex API key for higher rate limits. |
| `--verbose` | `Flag` | `False` | Enable debug logging output. |

### Practical CLI Examples

#### 1. Quick Dry-Run on First 10 References
```bash
python bib_audit_cli.py references.bib --limit 10 --verbose
```

#### 2. Full Batch Run with Custom Output Paths
```bash
python bib_audit_cli.py thesis.bib \
  --output thesis_clean.bib \
  --report audit_matrix.csv \
  --summary audit_summary.md
```

#### 3. Interactive Review Resuming Previous Session
```bash
python bib_audit_cli.py thesis.bib --feedback
```
*(Decisions already recorded in `.bib-audit-decisions.json` will be automatically applied without re-prompting).*

---

## Execution Modes

| Mode | Behavior | Ideal Use Case |
| :--- | :--- | :--- |
| **Automatic (`auto`)** | Resolves all entries using score thresholds. Ambiguous entries are flagged in reports without blocking. | CI/CD pipelines, large bibliographies, initial audit runs. |
| **Hybrid (`hybrid` / `--feedback`)** | Automatically applies high-confidence matches; pauses or prompts for ambiguous / missing matches. | Daily thesis / paper curation with minimal human effort. |
| **Manual (`manual`)** | Prompts confirmation for every entry having a candidate or modification. | High-stakes publications requiring full verification control. |

---

## Understanding Statuses & Reports

Every entry is assigned one of the following audit statuses:

- `verified`: The entry already had a valid DOI that was confirmed by Crossref/DataCite/OpenAlex.
- `corrected`: A high-confidence match was discovered and applied (e.g. missing DOI added, metadata completed).
- `to review`: Candidates found but similarity scores or margins were ambiguous. Requires human review.
- `not found`: No matching publication could be identified with sufficient similarity.
- `error`: Network timeout, HTTP error, or BibTeX parsing failure.
- `kept`: The original entry was preserved based on user feedback.
- `manually corrected`: The entry was directly edited by the user.

### Output Files

1. **Cleaned BibTeX (`bib-verified.bib`)**:
   - Preserves citation keys.
   - Cleans and standardizes `doi = {10.xxxx/...}` fields.
   - Keeps comments and custom fields.
2. **CSV Matrix (`audit-report.csv`)**:
   - Columns: `key`, `entry_type`, `status`, `source`, `confidence`, `original_doi`, `resolved_doi`, `title_score`, `author_score`, `year_score`, `changes`, `warnings`, `original_title`, `resolved_title`.
3. **Markdown Summary (`audit-summary.md`)**:
   - Count of verified, corrected, and problematic references.
   - Dedicated list of references requiring manual review with candidate suggestions and explanation notes.

---

## Persistent Cache & Decisions Store

- **Cache (`.bib-audit-cache.json`)**:
  Stores all raw API query results indexed by hash. If you stop the script and run it again, previously fetched references resolve instantly without hitting external servers.
- **Decisions (`.bib-audit-decisions.json`)**:
  Stores all manual decisions (`accept`, `keep_original`, `manual_edit`) keyed by citation key and normalized title. You never have to re-review the same reference twice.

---

## Running Unit Tests

Unit tests for the scoring engine, TeX normalization, DOI parsing, and candidate ranking are included in `test_engine.py`:

```bash
python -m unittest test_engine.py
```

---

## License & Contributing

This project is distributed under the MIT License. See the LICENSE file for more details.

Built for robust academic bibliography management and research manuscript preparation. Contributions and feedback are welcome!

