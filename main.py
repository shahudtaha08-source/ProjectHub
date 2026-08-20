"""ProjectHub - Phase 1 stabilization and watch mode.

Tracks scattered project folders without moving them. Auto Mode detects project
changes, initializes Git when needed, optionally creates a GitHub repository,
commits, and pushes. Watch Mode uses polling so it needs no third-party Python
package and debounces noisy batch changes such as ZIP extraction.
"""
import argparse
import difflib
import hashlib
import json
import os
import platform
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OWNER = "shahudtaha08-source"
IGNORED_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    ".idea", ".vscode", "dist", "build",
}


def load_env(path: Path) -> dict:
    values = {}
    if not path.exists():
        return values
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


ENV = load_env(SCRIPT_DIR / ".env")
HUB_DIR = Path(ENV.get("PROJECT_HUB") or SCRIPT_DIR).expanduser().resolve()
INDEX_FILE = HUB_DIR / "projects_index.json"
CACHE_FILE = HUB_DIR / ".cache.json"
GITHUB_OWNER = ENV.get("GITHUB_OWNER") or DEFAULT_OWNER
GITHUB_TOKEN = ENV.get("GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN", "")
GITHUB_VISIBILITY = (ENV.get("GITHUB_VISIBILITY") or "private").lower()
AUTO_CREATE_REPOS = (ENV.get("AUTO_CREATE_REPOS") or "false").lower() in {"1", "true", "yes", "on"}
WATCH_INTERVAL = max(1.0, float(ENV.get("WATCH_INTERVAL_SECONDS") or "5"))
WATCH_DEBOUNCE = max(2.0, float(ENV.get("WATCH_DEBOUNCE_SECONDS") or "10"))


def normalize_path(raw: str) -> str:
    if not raw:
        return ""
    return os.path.normpath(os.path.abspath(os.path.expanduser(raw.strip().strip('"').strip("'"))))


def load_index() -> dict:
    if not INDEX_FILE.exists():
        print(f"[ERROR] Index file not found: {INDEX_FILE}")
        return {}
    try:
        data = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[ERROR] Could not parse {INDEX_FILE.name}: {exc}")
        return {}
    if not isinstance(data, dict):
        print("[ERROR] projects_index.json must contain a JSON object.")
        return {}
    projects = {}
    for name, info in data.items():
        if not isinstance(info, dict):
            print(f"[WARN] Skipping invalid project entry: {name}")
            continue
        info = dict(info)
        info["path"] = normalize_path(info.get("path", ""))
        projects[name] = info
    return projects


def validate_paths(projects: dict) -> None:
    for name, info in projects.items():
        path = info.get("path", "")
        tag = "VALID" if path and os.path.isdir(path) else "MISSING"
        print(f"[{tag:<7}] {name} -> {path}")


def display_projects(projects: dict) -> None:
    if not projects:
        print("No projects found.")
        return
    nw = max(len("PROJECT NAME"), *(len(x) for x in projects))
    sw = max(len("STATUS"), *(len(x.get("status", "unknown")) for x in projects.values()))
    print(f"\n{'PROJECT NAME':<{nw}}  {'STATUS':<{sw}}  PATH")
    print("-" * (nw + sw + 32))
    for name, info in projects.items():
        print(f"{name:<{nw}}  {info.get('status', 'unknown'):<{sw}}  {info.get('path', '')}")


def key(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch not in " _-")


def find_project(query: str, projects: dict):
    lookup = {key(name): name for name in projects}
    exact = lookup.get(key(query))
    if exact:
        return exact
    close = difflib.get_close_matches(key(query), lookup.keys(), n=1, cutoff=0.6)
    if close:
        print(f"[NOT FOUND] Did you mean: {lookup[close[0]]}?")
    else:
        print(f"[NOT FOUND] '{query}' is not in the index.")
    return None


def open_project(name: str, projects: dict) -> None:
    path = projects[name].get("path", "")
    if not os.path.isdir(path):
        print(f"[ERROR] Path does not exist for '{name}': {path}")
        return
    try:
        if platform.system() == "Windows":
            os.startfile(path)  # type: ignore[attr-defined]
        elif platform.system() == "Darwin":
            subprocess.run(["open", path], check=True)
        else:
            subprocess.run(["xdg-open", path], check=True)
        print(f"[OK] Opened '{name}'")
    except Exception as exc:
        print(f"[ERROR] Could not open folder: {exc}")


def scan_signature(path: str) -> dict:
    entries = []
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
        for filename in files:
            full = os.path.join(root, filename)
            try:
                stat = os.stat(full)
            except OSError:
                continue
            rel = os.path.relpath(full, path).replace("\\", "/")
            entries.append(f"{rel}|{stat.st_size}|{stat.st_mtime_ns}")
    entries.sort()
    digest = hashlib.sha256("\n".join(entries).encode("utf-8", "ignore")).hexdigest()
    return {"hash": digest, "file_count": len(entries)}


def load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8")) if CACHE_FILE.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_cache(cache: dict) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = CACHE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
    temp.replace(CACHE_FILE)


def git(path: str, *args: str):
    return subprocess.run(["git", *args], cwd=path, capture_output=True, text=True)


def is_git_repo(path: str) -> bool:
    if shutil.which("git") is None:
        return False
    return git(path, "rev-parse", "--is-inside-work-tree").returncode == 0


def ensure_git_repo(path: str, branch: str):
    if is_git_repo(path):
        return True, "existing git repository"
    if shutil.which("git") is None:
        return False, "git is not installed or not on PATH"
    result = git(path, "init")
    if result.returncode:
        return False, result.stderr.strip() or "git init failed"
    branch_result = git(path, "branch", "-M", branch or "main")
    if branch_result.returncode:
        return False, branch_result.stderr.strip() or "could not set initial branch"
    return True, "initialized git repository"


def get_remote(path: str) -> str:
    result = git(path, "remote", "get-url", "origin")
    return result.stdout.strip() if result.returncode == 0 else ""


def repo_slug(name: str) -> str:
    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in name)
    slug = "-".join(part for part in slug.split("-") if part)
    return slug or "project"


def github_api_create_repo(name: str):
    if not GITHUB_TOKEN:
        return False, "GITHUB_TOKEN is missing"
    payload = json.dumps({
        "name": name,
        "private": GITHUB_VISIBILITY != "public",
        "auto_init": False,
    }).encode("utf-8")
    request = urllib.request.Request(
        "https://api.github.com/user/repos",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "ProjectHub",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
        return True, data["clone_url"]
    except urllib.error.HTTPError as exc:
        if exc.code == 422:
            return True, f"https://github.com/{GITHUB_OWNER}/{name}.git"
        try:
            detail = exc.read().decode("utf-8", "ignore")
        except Exception:
            detail = ""
        return False, f"GitHub API HTTP {exc.code}: {detail or exc.reason}"
    except Exception as exc:
        return False, f"GitHub repo creation failed: {exc}"


def ensure_remote(name: str, path: str):
    remote = get_remote(path)
    if remote:
        return True, remote
    if not AUTO_CREATE_REPOS:
        return False, "no remote configured (AUTO_CREATE_REPOS=false)"
    slug = repo_slug(name)
    ok, detail = github_api_create_repo(slug)
    if not ok:
        return False, detail
    result = git(path, "remote", "add", "origin", detail)
    if result.returncode:
        return False, result.stderr.strip() or "could not add GitHub remote"
    return True, f"created/linked {detail}"


def auto_project(name: str, info: dict, cache: dict):
    path = info.get("path", "")
    if not os.path.isdir(path):
        return "skipped", "path missing"
    current = scan_signature(path)
    previous = cache.get(name)
    if previous is None:
        cache[name] = current
        return "baseline", "baseline created"
    if current.get("hash") == previous.get("hash"):
        return "skipped", "no detected changes"

    branch = str(info.get("branch") or "main")
    ok, detail = ensure_git_repo(path, branch)
    if not ok:
        return "error", detail
    ok, detail = ensure_remote(name, path)
    if not ok:
        return "skipped", detail

    add = git(path, "add", ".")
    if add.returncode:
        return "error", add.stderr.strip() or "git add failed"
    status = git(path, "status", "--porcelain")
    if status.returncode:
        return "error", status.stderr.strip() or "git status failed"
    if not status.stdout.strip():
        cache[name] = current
        return "skipped", "filesystem changed only in ignored files"

    commit = git(path, "commit", "-m", f"chore: automated project update for {name}")
    if commit.returncode:
        return "error", commit.stderr.strip() or commit.stdout.strip()

    current_branch = git(path, "branch", "--show-current")
    active_branch = current_branch.stdout.strip() or branch
    push = git(path, "push", "-u", "origin", active_branch)
    if push.returncode:
        return "error", push.stderr.strip() or "git push failed"

    cache[name] = current
    return "updated", detail


def run_auto(projects: dict, only_names=None) -> None:
    cache = load_cache()
    summary = {"updated": 0, "skipped": 0, "errors": 0, "baselined": 0}
    allowed = set(only_names) if only_names else None
    for name, info in projects.items():
        if allowed is not None and name not in allowed:
            continue
        status, detail = auto_project(name, info, cache)
        if status == "updated":
            print(f"[UPDATED] {name} ({detail})")
            summary["updated"] += 1
        elif status == "baseline":
            print(f"[BASELINE] {name}")
            summary["baselined"] += 1
        elif status == "error":
            print(f"[ERROR] {name}: {detail}")
            summary["errors"] += 1
        else:
            print(f"[SKIPPED] {name} ({detail})")
            summary["skipped"] += 1
    save_cache(cache)
    print("\n===== Auto Mode Summary =====")
    print(f"[UPDATED]   {summary['updated']}")
    print(f"[BASELINE]  {summary['baselined']}")
    print(f"[SKIPPED]   {summary['skipped']}")
    print(f"[ERROR]     {summary['errors']}")


def watch_projects(projects: dict) -> None:
    """Poll scattered projects and process each only after its changes are stable."""
    print(f"[WATCH] Interval: {WATCH_INTERVAL:.1f}s | Debounce: {WATCH_DEBOUNCE:.1f}s")
    print("[WATCH] Baseline scan started. Press Ctrl+C to stop.")
    observed = {}
    pending = {}
    for name, info in projects.items():
        path = info.get("path", "")
        observed[name] = scan_signature(path) if os.path.isdir(path) else None

    try:
        while True:
            now = time.monotonic()
            for name, info in projects.items():
                path = info.get("path", "")
                current = scan_signature(path) if os.path.isdir(path) else None
                if current != observed.get(name):
                    observed[name] = current
                    pending[name] = now
                    print(f"[DETECTED] {name}")

            ready = [name for name, changed_at in pending.items() if now - changed_at >= WATCH_DEBOUNCE]
            for name in ready:
                pending.pop(name, None)
                print(f"[PROCESSING] {name} (changes stable)")
                run_auto(projects, only_names=[name])
                path = projects[name].get("path", "")
                observed[name] = scan_signature(path) if os.path.isdir(path) else None
            time.sleep(WATCH_INTERVAL)
    except KeyboardInterrupt:
        print("\n[WATCH] Stopped.")


def menu(projects: dict):
    while True:
        print("\n===== Project Hub =====")
        print("1. List Projects\n2. Open Project\n3. Run Auto Mode\n4. Watch Projects\n5. Exit")
        try:
            choice = input("Choose an option (1-5): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            return
        if choice == "1":
            display_projects(projects)
        elif choice == "2":
            name = find_project(input("Project name: ").strip(), projects)
            if name:
                open_project(name, projects)
        elif choice == "3":
            run_auto(projects)
        elif choice == "4":
            watch_projects(projects)
        elif choice == "5":
            print("Goodbye.")
            return
        else:
            print("[INVALID] Please choose 1-5.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--auto", action="store_true")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--open", nargs="?")
    args = parser.parse_args()

    print(f"Loading index from: {INDEX_FILE}\n")
    projects = load_index()
    if not projects:
        return
    print("Validating project paths...")
    validate_paths(projects)

    if args.validate:
        display_projects(projects)
        return
    if args.open is not None:
        name = find_project(args.open or input("Project name: ").strip(), projects)
        if name:
            open_project(name, projects)
        return
    if args.auto:
        run_auto(projects)
        return
    if args.watch:
        watch_projects(projects)
        return
    display_projects(projects)
    menu(projects)


if __name__ == "__main__":
    main()
