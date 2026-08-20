"""ProjectHub Phase 5: safe remote/repo bootstrap, README foundation and Ollama hook.
Run with: python phase5.py [--project NAME] [--apply] [--push] [--repair-remote]
Dry-run is the default; --apply performs local Git/GitHub changes.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
INDEX_FILE = ROOT / "projects_index.json"


def load_env():
    env = {}
    if ENV_FILE.exists():
        for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def git(path, *args):
    return subprocess.run(["git", *args], cwd=path, capture_output=True, text=True)


def slug(text):
    value = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return value or "project"


def is_repo(path):
    return git(path, "rev-parse", "--is-inside-work-tree").returncode == 0


def remote(path):
    result = git(path, "remote", "get-url", "origin")
    return result.stdout.strip() if result.returncode == 0 else ""


def branch(path, fallback="main"):
    result = git(path, "branch", "--show-current")
    return result.stdout.strip() or fallback


def github_request(method, url, token, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "ProjectHub-Phase5",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return response.status, json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        return exc.code, {"message": body or exc.reason}


def repo_exists(owner, repo, token):
    code, data = github_request("GET", f"https://api.github.com/repos/{owner}/{repo}", token)
    return code == 200, data


def create_repo(owner, repo, token, private=True):
    code, data = github_request("POST", "https://api.github.com/user/repos", token, {
        "name": repo, "private": private, "auto_init": False,
    })
    if code in (200, 201):
        return True, data.get("clone_url", f"https://github.com/{owner}/{repo}.git")
    if code == 422:
        exists, _ = repo_exists(owner, repo, token)
        if exists:
            return True, f"https://github.com/{owner}/{repo}.git"
    return False, data.get("message", f"GitHub API HTTP {code}")


def readme_text(name, info):
    title = name.replace("_", " ").replace("-", " ").title()
    status = info.get("status", "in-progress")
    branch_name = info.get("branch", "main")
    return f"# {title}\n\nProject status: **{status}**.\n\nManaged by ProjectHub. Default branch: `{branch_name}`.\n\n## Development\n\nThis README was generated only because the project did not already contain one. Add the project-specific setup, architecture and usage details here.\n"


def ensure_readme(path, name, info, apply):
    target = Path(path) / "README.md"
    if target.exists():
        return "existing README preserved"
    if not apply:
        return "would create README.md"
    target.write_text(readme_text(name, info), encoding="utf-8")
    return "created README.md"


def ollama_suggest(path, fallback):
    url = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
    model = os.getenv("OLLAMA_MODEL", "")
    if not model:
        return fallback + " (Ollama disabled: OLLAMA_MODEL is empty)"
    prompt = "Write one concise Conventional Commit message for the current project changes. Return only the message."
    try:
        req = urllib.request.Request(url, data=json.dumps({"model": model, "prompt": prompt, "stream": False}).encode(), method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as response:
            text = json.loads(response.read().decode()).get("response", "").strip().splitlines()[0]
        return text[:200] or fallback
    except Exception:
        return fallback + " (Ollama unavailable; fallback used)"


def process(name, info, env, args):
    path = Path(info.get("path", ""))
    if not path.is_dir():
        print(f"[SKIP] {name}: path missing: {path}")
        return
    owner = env.get("GITHUB_OWNER", "shahudtaha08-source")
    token = env.get("GITHUB_TOKEN", "")
    repo = slug(info.get("repo") or name)
    expected = f"https://github.com/{owner}/{repo}.git"
    print(f"\n== {name} ==")
    print(f"path: {path}")
    print(f"expected repo: {owner}/{repo}")
    if not is_repo(path):
        print("[PLAN] git init")
        if args.apply:
            result = git(path, "init")
            if result.returncode:
                print("[ERROR] git init failed:", result.stderr.strip()); return
            git(path, "branch", "-M", info.get("branch", "main"))
            print("[OK] git initialized")
    current = remote(path) if is_repo(path) else ""
    if current and current != expected:
        print(f"[WARN] existing remote differs: {current}")
        if args.repair_remote and args.apply:
            result = git(path, "remote", "set-url", "origin", expected)
            print("[OK] remote repaired" if result.returncode == 0 else f"[ERROR] {result.stderr.strip()}")
        else:
            print("[SAFE] not overwriting remote without --repair-remote --apply")
            return
    if not current:
        if not token:
            print("[SKIP] no origin and GITHUB_TOKEN is missing")
            return
        exists, _ = repo_exists(owner, repo, token)
        if not exists:
            print("[PLAN] create GitHub repository")
            if args.apply:
                ok, detail = create_repo(owner, repo, token, env.get("GITHUB_VISIBILITY", "private").lower() != "public")
                if not ok:
                    print(f"[ERROR] repo creation failed: {detail}"); return
                print(f"[OK] {detail}")
        if args.apply:
            result = git(path, "remote", "add", "origin", expected)
            if result.returncode:
                print(f"[ERROR] adding remote failed: {result.stderr.strip()}"); return
            print("[OK] origin linked")
        else:
            print("[PLAN] link origin")
    print("[README]", ensure_readme(path, name, info, args.apply))
    if not args.apply:
        print("[DRY RUN] no files, Git config or GitHub repositories changed")
        return
    add = git(path, "add", ".")
    if add.returncode:
        print(f"[ERROR] git add failed: {add.stderr.strip()}"); return
    status = git(path, "status", "--porcelain")
    if status.stdout.strip():
        message = ollama_suggest(path, f"chore: update {name}")
        commit = git(path, "commit", "-m", message)
        if commit.returncode:
            print(f"[ERROR] commit failed: {commit.stderr.strip()}"); return
        print(f"[OK] committed: {message}")
    else:
        print("[OK] no new changes to commit")
    if args.push:
        active = branch(path, info.get("branch", "main"))
        push = git(path, "push", "-u", "origin", active)
        if push.returncode:
            print("[ERROR] push failed; remote history was not overwritten:", push.stderr.strip())
        else:
            print(f"[OK] pushed {active}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", help="exact project name from projects_index.json")
    parser.add_argument("--apply", action="store_true", help="perform local/GitHub changes; default is dry-run")
    parser.add_argument("--push", action="store_true", help="push after a successful --apply")
    parser.add_argument("--repair-remote", action="store_true", help="allow expected-origin replacement with --apply")
    args = parser.parse_args()
    env = load_env()
    if not INDEX_FILE.exists():
        print(f"[ERROR] missing {INDEX_FILE}"); sys.exit(1)
    projects = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    if args.project:
        if args.project not in projects:
            print(f"[ERROR] project not found: {args.project}"); sys.exit(1)
        projects = {args.project: projects[args.project]}
    for name, info in projects.items():
        process(name, info, env, args)


if __name__ == "__main__":
    main()
