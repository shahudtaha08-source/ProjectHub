"""
Project Automation System - Phase 2 (v3)
-------------------------------------------
Hub location: D:\\ProjectHub\\files\\

This file is the ONLY place that decides where the index/cache live.
Everything else in the program imports INDEX_FILE / CACHE_FILE from here -
that's the fix for the old "projects/_index.json"-style path bug: there is
now a single constant, not several places that each build the path.

Config precedence for the hub folder:
  1. PROJECT_HUB in a .env file (if present)
  2. Hardcoded default: D:\\ProjectHub\\files

PROJECTS_ROOT (optional, also from .env) is used only to resolve project
paths in the index that are stored as *relative* paths. Absolute paths in
projects_index.json are never touched by this - it's an opt-in convenience,
not a requirement.

Safety reminder: nothing in this file moves, renames, or deletes project
folders. It only computes where to read config FROM.
"""

import os
import platform
import subprocess
import sys
import json
import hashlib
import difflib
from pathlib import Path

# ---------------------------------------------------------------------------
# .env loading (no external dependency - simple manual parser)
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent


def _load_dotenv(env_path: Path) -> dict:
    """
    Minimal .env parser: KEY=VALUE per line, '#' comments, blank lines
    ignored, optional surrounding quotes on the value stripped. Never
    raises - returns {} if the file can't be read.
    """
    values = {}
    if not env_path.exists():
        return values
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key:
                    values[key] = value
    except OSError:
        pass
    return values


def _find_env() -> dict:
    """Look for .env next to this script, then one folder up. First found wins."""
    for candidate in (SCRIPT_DIR / ".env", SCRIPT_DIR.parent / ".env"):
        if candidate.exists():
            return _load_dotenv(candidate)
    return {}


_ENV = _find_env()

# ---------------------------------------------------------------------------
# Hub path constants (single source of truth)
# ---------------------------------------------------------------------------

DEFAULT_HUB_DIR = r"D:\ProjectHub\files"

# PROJECT_HUB from .env if present and non-empty, else the hardcoded default.
HUB_DIR = Path(os.path.normpath(_ENV.get("PROJECT_HUB") or DEFAULT_HUB_DIR))

# Optional root used only to resolve *relative* project paths in the index.
# Empty string means "not configured" - relative paths are then resolved
# against the current working directory as a last resort.
PROJECTS_ROOT = _ENV.get("PROJECTS_ROOT", "").strip()

INDEX_FILE = HUB_DIR / "projects_index.json"
CACHE_FILE = HUB_DIR / ".cache.json"

# Folders skipped when scanning a project for changes.
IGNORED_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vscode"}


# ---------------------------------------------------------------------------
# Path + name normalization
# ---------------------------------------------------------------------------

def normalize_path(raw_path: str) -> str:
    """
    Normalize a path for reliable comparison/display.
      - Strips stray quotes/whitespace
      - If relative and PROJECTS_ROOT is configured, resolves against it
      - Expands to an absolute, OS-correct path
    Never modifies anything on disk - this is pure string/path math.
    """
    if not raw_path:
        return ""
    cleaned = raw_path.strip().strip('"').strip("'")

    p = Path(cleaned)
    if not p.is_absolute() and PROJECTS_ROOT:
        p = Path(PROJECTS_ROOT) / p

    return os.path.normpath(os.path.abspath(os.path.expanduser(str(p))))


def normalize_input(text: str) -> str:
    """
    Build a comparison key for project-name matching:
      trim -> lowercase -> strip spaces/underscores/dashes.
    This makes "RuhumAI", "ruhum ai", and "ruhum_ai" all collapse to
    the same key ("ruhumai").
    """
    if not text:
        return ""
    trimmed = " ".join(text.strip().split())  # trim + collapse internal whitespace
    key = trimmed.lower()
    for ch in (" ", "_", "-"):
        key = key.replace(ch, "")
    return key


# ---------------------------------------------------------------------------
# A. Load Index
# ---------------------------------------------------------------------------

def load_index(index_path: Path = None) -> dict:
    """
    Load the project index from the single INDEX_FILE constant (or an
    explicit override, used by tests). Never crashes on a missing or
    malformed file - returns {} with a clear message instead.
    """
    index_path = Path(index_path) if index_path is not None else INDEX_FILE

    if not index_path.exists():
        print(f"[WARN] Index file not found: {index_path}")
        return {}

    try:
        with open(index_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"[ERROR] Could not parse {index_path.name}: {e}")
        return {}

    if not isinstance(data, dict):
        print(f"[ERROR] {index_path.name} must contain a JSON object at the top level.")
        return {}

    for name, info in data.items():
        if isinstance(info, dict) and "path" in info:
            info["path"] = normalize_path(info["path"])

    return data


# ---------------------------------------------------------------------------
# B. Validate Paths
# ---------------------------------------------------------------------------

def validate_paths(projects: dict) -> dict:
    """Check each project's path on disk. Never raises. Returns {name: bool}."""
    results = {}
    for name, info in projects.items():
        path_str = info.get("path", "")
        exists = bool(path_str) and os.path.exists(path_str)
        results[name] = exists

        if exists:
            print(f"  [VALID]   {name} -> {path_str}")
        else:
            print(f"  [MISSING] {name} -> {path_str}")
            print(f"            (path not found - check for typos, drive letters, or a moved folder)")

    return results


# ---------------------------------------------------------------------------
# C. Display Projects (aligned table)
# ---------------------------------------------------------------------------

def display_projects(projects: dict) -> None:
    """Print all tracked projects with status, aligned into clean columns."""
    if not projects:
        print("No projects found in index.")
        return

    name_w = max(len("PROJECT NAME"), max(len(n) for n in projects))
    status_w = max(len("STATUS"), max(len(i.get("status", "unknown")) for i in projects.values()))

    header = f"{'PROJECT NAME':<{name_w}}  {'STATUS':<{status_w}}  PATH"
    print(f"\n{header}")
    print("-" * len(header))
    for name, info in projects.items():
        status = info.get("status", "unknown")
        path_str = info.get("path", "N/A")
        print(f"{name:<{name_w}}  {status:<{status_w}}  {path_str}")


# ---------------------------------------------------------------------------
# Project name matching (case/separator-insensitive + fuzzy suggestion)
# ---------------------------------------------------------------------------

def find_project(query: str, projects: dict):
    """
    Resolve a user-typed project name to the real key in `projects`.
    Case-insensitive, ignores spaces/underscores/dashes. Falls back to a
    fuzzy "did you mean" suggestion when there's no exact match.
    """
    if not query:
        return None

    target_key = normalize_input(query)
    lookup = {normalize_input(name): name for name in projects}

    if target_key in lookup:
        return lookup[target_key]

    close = difflib.get_close_matches(target_key, lookup.keys(), n=1, cutoff=0.6)
    if close:
        suggested_name = lookup[close[0]]
        print(f"[NOT FOUND] '{query}' isn't in the index. Did you mean: {suggested_name}?")
    else:
        print(f"[NOT FOUND] '{query}' isn't in the index, and no close match was found.")

    return None


# ---------------------------------------------------------------------------
# D. Open Project Function
# ---------------------------------------------------------------------------

def open_project(name: str, projects: dict) -> bool:
    """Open the given project's folder in the OS file explorer."""
    info = projects.get(name)
    if info is None:
        print(f"[ERROR] Project '{name}' not found in index.")
        return False

    path_str = info.get("path", "")
    if not path_str or not os.path.exists(path_str):
        print(f"[ERROR] Path does not exist for '{name}': {path_str}")
        return False

    system = platform.system()
    try:
        if system == "Windows":
            os.startfile(path_str)  # type: ignore[attr-defined]
        elif system == "Darwin":
            subprocess.run(["open", path_str], check=True)
        else:
            subprocess.run(["xdg-open", path_str], check=True)
        print(f"[OK] Opened '{name}' at {path_str}")
        return True
    except Exception as e:
        print(f"[ERROR] Could not open folder: {e}")
        return False


# ---------------------------------------------------------------------------
# PHASE 2: change detection (mtime + file-list hash)
# ---------------------------------------------------------------------------

def is_git_repo(path_str: str) -> bool:
    """True if the project folder contains a .git directory."""
    return os.path.isdir(os.path.join(path_str, ".git"))


def _scan_signature(path_str: str) -> dict:
    """
    Build a lightweight signature of a project's current state: a hash of
    every (relative path, modified time) pair, sorted, skipping
    IGNORED_DIRS. This catches additions, deletions, renames, AND edits -
    not just "did the newest file change" - while staying cheap (no file
    content is read or hashed).
    """
    entries = []
    for root, dirs, files in os.walk(path_str):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
        for fname in files:
            fpath = os.path.join(root, fname)
            try:
                mtime = os.path.getmtime(fpath)
            except OSError:
                continue
            relpath = os.path.relpath(fpath, path_str)
            entries.append(f"{relpath}:{mtime}")

    entries.sort()
    digest = hashlib.sha256("|".join(entries).encode("utf-8", "ignore")).hexdigest()
    return {"file_count": len(entries), "hash": digest}


def load_cache(cache_path: Path = None) -> dict:
    """Load the change-detection cache. Returns {} if missing/corrupt."""
    cache_path = Path(cache_path) if cache_path is not None else CACHE_FILE
    if not cache_path.exists():
        return {}
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        print(f"[WARN] {cache_path.name} was corrupt - starting a fresh cache.")
        return {}


def save_cache(cache: dict, cache_path: Path = None) -> None:
    """Persist the change-detection cache to disk."""
    cache_path = Path(cache_path) if cache_path is not None else CACHE_FILE
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2)
    except OSError as e:
        print(f"[ERROR] Could not write cache file: {e}")


def has_changes(name: str, path_str: str, cache: dict) -> bool:
    """True if this project's signature differs from what's cached (or is new)."""
    current = _scan_signature(path_str)
    previous = cache.get(name)
    if previous is None:
        return True
    return current["hash"] != previous.get("hash")


def _update_cache_entry(name: str, path_str: str, cache: dict) -> None:
    cache[name] = _scan_signature(path_str)


# ---------------------------------------------------------------------------
# Git remote safety (fixes the "my-webapp" 404 crash-on-push case)
# ---------------------------------------------------------------------------

# Substrings that indicate "this remote is missing/invalid" rather than a
# transient or unexpected error - matched case-insensitively against git's
# combined stdout+stderr.
_INVALID_REMOTE_SIGNS = (
    "not found",
    "404",
    "repository not found",
    "could not read from remote repository",
    "does not appear to be a git repository",
    "no such device or address",
)


def get_remote_url(path_str: str):
    """Return the 'origin' remote URL, or None if no remote is configured."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=path_str, capture_output=True, text=True,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    return url or None


def verify_remote(path_str: str):
    """
    Confirm the project's remote exists and is reachable BEFORE we ever
    attempt a push. Returns (True, "") if usable, or (False, reason) if
    the remote is missing, invalid, or unreachable (covers the 404 case).
    """
    url = get_remote_url(path_str)
    if not url:
        return False, "no remote configured"

    try:
        result = subprocess.run(
            ["git", "ls-remote", "--exit-code", url],
            cwd=path_str, capture_output=True, text=True, timeout=15,
        )
    except subprocess.TimeoutExpired:
        return False, "remote check timed out"
    except FileNotFoundError:
        return False, "git is not installed or not on PATH"

    if result.returncode != 0:
        combined = f"{result.stdout}\n{result.stderr}".lower()
        if any(sign in combined for sign in _INVALID_REMOTE_SIGNS):
            return False, "remote not found (404 / invalid repository)"
        return False, (result.stderr.strip() or "remote unreachable")

    return True, ""


def run_git_automation(name: str, path_str: str) -> tuple:
    """
    Run git add -> commit -> push for a project that already has confirmed
    changes AND is a confirmed git repo. Returns (status, detail) where
    status is one of: "updated", "skipped_no_changes", "skipped_remote", "error".

    All commands run with cwd=path_str, so this can never touch anything
    outside that project's own repo.
    """
    # 1) Verify remote BEFORE touching anything - this is what prevents
    #    the my-webapp-style crash: a bad remote is caught here, not
    #    discovered mid-push.
    remote_ok, remote_reason = verify_remote(path_str)
    if not remote_ok:
        return "skipped_remote", remote_reason

    try:
        subprocess.run(
            ["git", "add", "."],
            cwd=path_str, check=True, capture_output=True, text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        detail = e.stderr.strip() if hasattr(e, "stderr") and e.stderr else str(e)
        return "error", detail

    commit_msg = f"Auto update: {name}"
    commit_result = subprocess.run(
        ["git", "commit", "-m", commit_msg],
        cwd=path_str, capture_output=True, text=True,
    )
    if commit_result.returncode != 0:
        if "nothing to commit" in commit_result.stdout.lower():
            return "skipped_no_changes", ""
        return "error", commit_result.stderr.strip() or commit_result.stdout.strip()

    push_result = subprocess.run(
        ["git", "push"],
        cwd=path_str, capture_output=True, text=True,
    )
    if push_result.returncode != 0:
        combined = f"{push_result.stdout}\n{push_result.stderr}".lower()
        if any(sign in combined for sign in _INVALID_REMOTE_SIGNS):
            return "skipped_remote", "push rejected: remote not found (404 / invalid repository)"
        return "error", push_result.stderr.strip() or "git push failed"

    return "updated", ""


def run_auto_mode(projects: dict) -> None:
    """
    Scan every project and, only where genuinely needed, run git automation:
      - missing path            -> [SKIPPED] (path missing)
      - not a git repo          -> [SKIPPED] (not a git repo)
      - no detected changes     -> [SKIPPED] (no changes)
      - invalid/missing remote  -> [SKIPPED] (invalid or missing remote)
      - commit/push succeeded   -> [UPDATED]
      - anything else that fails -> [ERROR] with a readable reason

    Never crashes the whole run - one project's failure never stops the
    rest from being processed.
    """
    cache = load_cache()
    summary = {"updated": 0, "skipped": 0, "errors": 0}

    for name, info in projects.items():
        path_str = info.get("path", "")

        if not path_str or not os.path.exists(path_str):
            print(f"[SKIPPED] {name} (path missing: {path_str})")
            summary["skipped"] += 1
            continue

        if not is_git_repo(path_str):
            print(f"[SKIPPED] {name} (not a git repo)")
            summary["skipped"] += 1
            continue

        if not has_changes(name, path_str, cache):
            print(f"[SKIPPED] {name} (no changes)")
            summary["skipped"] += 1
            continue

        status, detail = run_git_automation(name, path_str)
        _update_cache_entry(name, path_str, cache)  # refresh signature regardless of outcome

        if status == "updated":
            print(f"[UPDATED] {name}")
            summary["updated"] += 1
        elif status == "skipped_remote":
            print(f"[SKIPPED] {name} (invalid or missing remote)")
            summary["skipped"] += 1
        elif status == "skipped_no_changes":
            print(f"[SKIPPED] {name} (no changes to commit)")
            summary["skipped"] += 1
        else:
            print(f"[ERROR] {name}: {detail}")
            summary["errors"] += 1

    save_cache(cache)

    print("\n===== Auto Mode Summary =====")
    print(f"[UPDATED] {summary['updated']}")
    print(f"[SKIPPED] {summary['skipped']}")
    print(f"[ERROR] {summary['errors']}")


# ---------------------------------------------------------------------------
# CLI Menu
# ---------------------------------------------------------------------------

def print_menu() -> None:
    print("\n===== Project Hub =====")
    print("1. List Projects")
    print("2. Open Project")
    print("3. Run Auto Mode (Git automation)")
    print("4. Exit")


def run_menu(projects: dict) -> None:
    while True:
        print_menu()
        try:
            choice = input("\nChoose an option (1-4): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            return

        if choice == "1":
            display_projects(projects)

        elif choice == "2":
            try:
                query = input("Project name: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nExiting.")
                return
            match = find_project(query, projects)
            if match:
                open_project(match, projects)

        elif choice == "3":
            run_auto_mode(projects)

        elif choice == "4":
            print("Goodbye.")
            return

        else:
            print("[INVALID] Please choose 1, 2, 3, or 4.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    print(f"Loading index from: {INDEX_FILE}\n")
    projects = load_index()

    print("Validating project paths...")
    validate_paths(projects)

    if not projects:
        return

    argv = sys.argv[1:]

    # `python main.py --auto` -> scan + git automation only, no menu.
    if "--auto" in argv:
        run_auto_mode(projects)
        return

    # `python main.py --validate` -> load + validate + list, then exit.
    if "--validate" in argv:
        display_projects(projects)
        return

    # `python main.py --open <name>` -> resolve + open, then exit.
    if "--open" in argv:
        idx = argv.index("--open")
        name_arg = argv[idx + 1] if idx + 1 < len(argv) else None
        if not name_arg:
            try:
                name_arg = input("Project name: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nExiting.")
                return
        match = find_project(name_arg, projects)
        if match:
            open_project(match, projects)
        return

    # No flags -> interactive menu.
    display_projects(projects)
    run_menu(projects)


if __name__ == "__main__":
    main()
