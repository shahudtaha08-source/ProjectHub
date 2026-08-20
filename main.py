"""ProjectHub - project registry, change detection and safe GitHub automation."""
import argparse
import difflib
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OWNER = "shahudtaha08-source"
IGNORED_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vscode", "dist", "build"}


def load_env(path):
    values = {}
    if path.exists():
        try:
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    values[k.strip()] = v.strip().strip('"').strip("'")
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
SEARCH_ROOTS = [Path(x.strip()).expanduser() for x in ENV.get("PROJECT_SEARCH_ROOTS", "D:/projects").split(";") if x.strip()]


def normalize_path(raw):
    return os.path.normpath(os.path.abspath(os.path.expanduser(str(raw).strip().strip('"').strip("'")))) if raw else ""


def load_index():
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
    for info in data.values():
        if isinstance(info, dict):
            info["path"] = normalize_path(info.get("path", ""))
    return data


def save_index(projects):
    payload = {}
    for name, info in projects.items():
        item = dict(info)
        item["path"] = item.get("path", "").replace("\\", "/")
        payload[name] = item
    INDEX_FILE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def project_tokens(text):
    return [x for x in "".join(ch.lower() if ch.isalnum() else " " for ch in text).split() if x]


def rename_score(project_name, folder_name):
    a, b = project_tokens(project_name), project_tokens(folder_name)
    if not a or not b:
        return 0.0
    first = difflib.SequenceMatcher(None, a[0], b[0]).ratio()
    full = difflib.SequenceMatcher(None, "".join(a), "".join(b)).ratio()
    return max(first * 0.8 + full * 0.2, full)


def find_renamed_folder(name, old_path):
    old = Path(old_path)
    roots = [old.parent, *SEARCH_ROOTS]
    seen = set()
    candidates = []
    for root in roots:
        try:
            root = root.resolve()
        except OSError:
            continue
        if root in seen or not root.is_dir():
            continue
        seen.add(root)
        try:
            for child in root.iterdir():
                if child.is_dir() and child.name not in IGNORED_DIRS:
                    score = rename_score(name, child.name)
                    if score >= 0.82:
                        candidates.append((score, child))
        except OSError:
            continue
    candidates.sort(key=lambda x: x[0], reverse=True)
    if not candidates:
        return None, 0.0
    best_score, best = candidates[0]
    if len(candidates) > 1 and best_score - candidates[1][0] < 0.08:
        return None, 0.0
    return str(best), best_score


def resolve_missing_paths(projects, persist=True):
    changed = False
    for name, info in projects.items():
        path = info.get("path", "")
        if path and os.path.isdir(path):
            continue
        candidate, score = find_renamed_folder(name, path)
        if candidate:
            print(f"[RELOCATED] {name}: {path} -> {candidate} (confidence {score:.2f})")
            info["path"] = normalize_path(candidate)
            changed = True
    if changed and persist:
        save_index(projects)
        print(f"[INDEX] Updated {INDEX_FILE.name} with relocated project paths.")
    return changed


def validate_paths(projects):
    resolve_missing_paths(projects)
    for name, info in projects.items():
        path = info.get("path", "")
        tag = "VALID" if path and os.path.isdir(path) else "MISSING"
        print(f"[{tag:<7}] {name} -> {path}")


def display_projects(projects):
    if not projects:
        print("No projects found.")
        return
    nw = max(len("PROJECT NAME"), *(len(x) for x in projects))
    sw = max(len("STATUS"), *(len(str(x.get("status", "unknown"))) for x in projects.values()))
    print(f"\n{'PROJECT NAME':<{nw}}  {'STATUS':<{sw}}  PATH")
    print("-" * (nw + sw + 32))
    for name, info in projects.items():
        print(f"{name:<{nw}}  {info.get('status', 'unknown'):<{sw}}  {info.get('path', '')}")


def key(text):
    return "".join(ch for ch in text.lower() if ch not in " _-")


def find_project(query, projects):
    lookup = {key(name): name for name in projects}
    exact = lookup.get(key(query))
    if exact:
        return exact
    close = difflib.get_close_matches(key(query), lookup.keys(), n=1, cutoff=0.6)
    print(f"[NOT FOUND] Did you mean: {lookup[close[0]]}?" if close else f"[NOT FOUND] '{query}' is not in the index.")
    return None


def open_project(name, projects):
    path = projects[name].get("path", "")
    if not os.path.isdir(path):
        print(f"[ERROR] Path does not exist for '{name}': {path}")
        return
    try:
        if platform.system() == "Windows":
            os.startfile(path)
        elif platform.system() == "Darwin":
            subprocess.run(["open", path], check=True)
        else:
            subprocess.run(["xdg-open", path], check=True)
        print(f"[OK] Opened '{name}'")
    except Exception as exc:
        print(f"[ERROR] Could not open folder: {exc}")


def scan_signature(path):
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
    return {"hash": hashlib.sha256("\n".join(entries).encode("utf-8", "ignore")).hexdigest(), "file_count": len(entries)}


def load_cache():
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8")) if CACHE_FILE.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_cache(cache):
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(CACHE_FILE)


def git(path, *args):
    return subprocess.run(["git", *args], cwd=path, capture_output=True, text=True)


def is_git_repo(path):
    return shutil.which("git") is not None and git(path, "rev-parse", "--is-inside-work-tree").returncode == 0


def ensure_git_repo(path, branch):
    if is_git_repo(path):
        return True, False, "existing git repository"
    if shutil.which("git") is None:
        return False, False, "git is not installed or not on PATH"
    result = git(path, "init")
    if result.returncode:
        return False, False, result.stderr.strip() or "git init failed"
    result = git(path, "branch", "-M", branch or "main")
    if result.returncode:
        return False, False, result.stderr.strip() or "could not set initial branch"
    return True, True, "initialized git repository"


def get_remote(path):
    result = git(path, "remote", "get-url", "origin")
    return result.stdout.strip() if result.returncode == 0 else ""


def repo_slug(name, info):
    configured = info.get("github_repo") or info.get("repo")
    if configured:
        return str(configured)
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "project"


def github_api_request(method, url, token, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "ProjectHub",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return response.status, json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, {"message": exc.read().decode(errors="replace") or exc.reason}
    except Exception as exc:
        return 0, {"message": str(exc)}


def github_repo_exists(owner, repo, token):
    if not token:
        return False, "GITHUB_TOKEN is missing"
    code, data = github_api_request("GET", f"https://api.github.com/repos/{owner}/{repo}", token)
    if code == 200:
        return True, "exists"
    if code == 404:
        return False, "not found"
    return False, f"GitHub API HTTP {code}: {data.get('message', 'unknown error')}"


def github_create_repo(owner, repo, token, private=True):
    if not token:
        return False, "GITHUB_TOKEN is missing"
    code, data = github_api_request("POST", "https://api.github.com/user/repos", token, {
        "name": repo,
        "private": private,
        "auto_init": False,
    })
    if code in (200, 201):
        return True, data.get("clone_url", f"https://github.com/{owner}/{repo}.git")
    if code == 422:
        exists, detail = github_repo_exists(owner, repo, token)
        if exists:
            return True, f"https://github.com/{owner}/{repo}.git"
        return False, detail
    return False, f"GitHub API HTTP {code}: {data.get('message', 'repository creation failed')}"


def normalize_remote(url):
    value = (url or "").strip().removesuffix("/")
    if value.endswith(".git"):
        value = value[:-4]
    return value.lower()


def expected_remote(owner, repo):
    return f"https://github.com/{owner}/{repo}.git"


def remote_status(owner, repo, path, token):
    current = get_remote(path)
    if not current:
        return "missing", ""
    target = expected_remote(owner, repo)
    if normalize_remote(current) == normalize_remote(target):
        return "valid", current
    if token:
        existing, detail = github_repo_exists_from_remote(current, token)
        if existing:
            return "valid-mapped", current
        if detail == "not found":
            return "broken", current
        return "unknown", f"{current} ({detail})"
    return "mismatch", current


def github_repo_exists_from_remote(url, token):
    match = re.search(r"github\.com[/:]([^/]+)/([^/#]+)", url or "", re.IGNORECASE)
    if not match:
        return False, "non-GitHub remote"
    owner, repo = match.group(1), match.group(2).removesuffix(".git")
    return github_repo_exists(owner, repo, token)


def ensure_remote_for_project(name, info, path, apply):
    owner = GITHUB_OWNER
    repo = repo_slug(name, info)
    target = expected_remote(owner, repo)
    current = get_remote(path)
    if current:
        status, detail = remote_status(owner, repo, path, GITHUB_TOKEN)
        if status in {"valid", "valid-mapped"}:
            return True, f"preserved existing remote {detail}"
        if status == "broken":
            print(f"[BROKEN] {name}: remote does not exist on GitHub: {current}")
            if not apply:
                return False, "broken remote requires repair"
            if not GITHUB_TOKEN:
                return False, "GITHUB_TOKEN is missing for remote repair"
            if not info.get("auto_repair_remote", False):
                return False, "auto repair disabled; mark auto_repair_remote=true explicitly"
            result = git(path, "remote", "set-url", "origin", target)
            return (True, f"repaired remote to {target}") if result.returncode == 0 else (False, result.stderr.strip() or "remote repair failed")
        if status == "mismatch":
            return False, f"remote differs; preserved: {current}"
        return False, f"remote could not be verified; preserved: {detail}"

    if not GITHUB_TOKEN:
        return False, "no origin and GITHUB_TOKEN is missing"
    exists, detail = github_repo_exists(owner, repo, GITHUB_TOKEN)
    if not exists and detail not in {"not found"}:
        return False, detail
    if not exists and apply:
        ok, created = github_create_repo(owner, repo, GITHUB_TOKEN, GITHUB_VISIBILITY != "public")
        if not ok:
            return False, created
        target = created
    elif not exists:
        return False, f"repo {owner}/{repo} missing; would create"
    if not apply:
        return False, f"no origin; would link {target}"
    result = git(path, "remote", "add", "origin", target)
    return (True, f"linked origin {target}") if result.returncode == 0 else (False, result.stderr.strip() or "could not add origin")


def ensure_readme(path, name, info, apply):
    target = Path(path) / "README.md"
    if target.exists():
        return "existing README preserved"
    if not apply:
        return "README missing; would create basic README"
    title = name.replace("_", " ").replace("-", " ").title()
    text = f"# {title}\n\nProject status: **{info.get('status', 'in-progress')}**.\n\nManaged by ProjectHub.\n"
    target.write_text(text, encoding="utf-8")
    return "created basic README.md"


def ollama_commit_message(name):
    model = ENV.get("OLLAMA_MODEL") or os.getenv("OLLAMA_MODEL", "")
    url = ENV.get("OLLAMA_URL") or os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
    fallback = f"chore: update {name}"
    if not model:
        return fallback
    try:
        req = urllib.request.Request(url, data=json.dumps({"model": model, "prompt": f"Write one concise Conventional Commit message for changes in project {name}. Return only the message.", "stream": False}).encode(), method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as response:
            text = json.loads(response.read().decode()).get("response", "").strip().splitlines()[0]
        return text[:200] or fallback
    except Exception:
        return fallback


def auto_project(name, info, cache):
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

    branch_name = str(info.get("branch") or "main")
    ok, initialized, git_detail = ensure_git_repo(path, branch_name)
    if not ok:
        return "error", git_detail

    remote_ok, remote_detail = ensure_remote_for_project(name, info, path, apply=True)
    if not remote_ok:
        return "skipped", remote_detail

    readme_detail = ensure_readme(path, name, info, apply=True)
    add = git(path, "add", ".")
    if add.returncode:
        return "error", add.stderr.strip() or "git add failed"

    status = git(path, "status", "--porcelain")
    if status.returncode:
        return "error", status.stderr.strip() or "git status failed"
    if not status.stdout.strip():
        cache[name] = current
        return "skipped", "no commit-worthy changes"

    message = ollama_commit_message(name)
    commit = git(path, "commit", "-m", message)
    if commit.returncode:
        return "error", commit.stderr.strip() or commit.stdout.strip() or "git commit failed"

    active = git(path, "branch", "--show-current").stdout.strip() or branch_name
    push = git(path, "push", "-u", "origin", active)
    if push.returncode:
        return "error", "push failed without force-overwriting remote: " + (push.stderr.strip() or "unknown git push error")

    cache[name] = current
    return "updated", f"{git_detail}; {remote_detail}; {readme_detail}; committed '{message}'; pushed {active}"


def run_auto(projects, only_names=None):
    resolve_missing_paths(projects)
    cache, summary = load_cache(), {"updated": 0, "skipped": 0, "errors": 0, "baselined": 0}
    allowed = set(only_names) if only_names else None
    for name, info in projects.items():
        if allowed is not None and name not in allowed:
            continue
        status, detail = auto_project(name, info, cache)
        label = {"updated": "UPDATED", "baseline": "BASELINE", "error": "ERROR", "skipped": "SKIPPED"}[status]
        if status == "error":
            print(f"[{label}] {name}: {detail}")
        else:
            print(f"[{label}] {name} ({detail})")
        summary[{"updated": "updated", "baseline": "baselined", "error": "errors"}.get(status, "skipped")] += 1
    save_cache(cache)
    print("\n===== Auto Mode Summary =====")
    for k in ("updated", "baselined", "skipped", "errors"):
        print(f"[{k.upper():<9}] {summary[k]}")


def watch_projects(projects):
    resolve_missing_paths(projects)
    print(f"[WATCH] Interval: {WATCH_INTERVAL:.1f}s | Debounce: {WATCH_DEBOUNCE:.1f}s")
    print("[WATCH] Baseline scan started. Press Ctrl+C to stop.")
    observed, pending = {}, {}
    for name, info in projects.items():
        path = info.get("path", "")
        observed[name] = scan_signature(path) if os.path.isdir(path) else None
    try:
        while True:
            now = time.monotonic()
            resolve_missing_paths(projects)
            for name, info in projects.items():
                path = info.get("path", "")
                current = scan_signature(path) if os.path.isdir(path) else None
                if current != observed.get(name):
                    observed[name] = current
                    pending[name] = now
                    print(f"[DETECTED] {name}")
            for name in [n for n, t in pending.items() if now - t >= WATCH_DEBOUNCE]:
                pending.pop(name, None)
                print(f"[PROCESSING] {name} (changes stable)")
                run_auto(projects, [name])
                path = projects[name].get("path", "")
                observed[name] = scan_signature(path) if os.path.isdir(path) else None
            time.sleep(WATCH_INTERVAL)
    except KeyboardInterrupt:
        print("\n[WATCH] Stopped.")


def menu(projects):
    while True:
        print("\n===== Project Hub =====\n1. List Projects\n2. Open Project\n3. Run Auto Mode\n4. Watch Projects\n5. Exit")
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
