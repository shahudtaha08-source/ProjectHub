"""ProjectHub Phase 5: safe GitHub bootstrap and remote verification.
Dry-run is default. Existing remotes are verified before any repair.
"""
import argparse, json, os, re, subprocess, sys, urllib.error, urllib.request
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
                k, v = line.split("=", 1); env[k.strip()] = v.strip().strip('"').strip("'")
    return env

def git(path, *args):
    return subprocess.run(["git", *args], cwd=path, capture_output=True, text=True)

def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "project"

def is_repo(path): return git(path, "rev-parse", "--is-inside-work-tree").returncode == 0

def remote(path):
    r = git(path, "remote", "get-url", "origin")
    return r.stdout.strip() if r.returncode == 0 else ""

def branch(path, fallback="main"):
    r = git(path, "branch", "--show-current")
    return r.stdout.strip() or fallback

def github_parts(url):
    value = url.strip().replace("\\", "/")
    m = re.search(r"github\.com[/:]([^/]+)/([^/]+?)(?:\.git)?/?$", value, re.I)
    return (m.group(1), m.group(2)) if m else ("", "")

def github_request(method, url, token="", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Accept":"application/vnd.github+json", "User-Agent":"ProjectHub-Phase5", "Content-Type":"application/json"}
    if token: headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return response.status, json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, {"message": exc.read().decode(errors="replace") or exc.reason}
    except Exception as exc:
        return 0, {"message": str(exc)}

def repo_state(owner, repo, token=""):
    code, data = github_request("GET", f"https://api.github.com/repos/{owner}/{repo}", token)
    return code, data

def create_repo(owner, repo, token, private=True):
    code, data = github_request("POST", "https://api.github.com/user/repos", token, {"name":repo,"private":private,"auto_init":False})
    if code in (200,201): return True, data.get("clone_url", f"https://github.com/{owner}/{repo}.git")
    if code == 422:
        exists, _ = repo_state(owner, repo, token)
        if exists == 200: return True, f"https://github.com/{owner}/{repo}.git"
    return False, data.get("message", f"GitHub API HTTP {code}")

def expected_repo(name, info): return str(info.get("github_repo") or info.get("repo") or slug(name))
def expected_url(owner, repo): return f"https://github.com/{owner}/{repo}.git"

def ensure_readme(path, name, info, apply):
    target = Path(path) / "README.md"
    if target.exists(): return "existing README preserved"
    if not apply: return "would create README.md"
    title = name.replace("_"," ").replace("-"," ").title()
    target.write_text(f"# {title}\n\nProject status: **{info.get('status','in-progress')}**.\n\nManaged by ProjectHub.\n", encoding="utf-8")
    return "created README.md"

def commit_changes(path, name, info):
    add = git(path, "add", ".")
    if add.returncode: return False, add.stderr.strip()
    status = git(path, "status", "--porcelain")
    if not status.stdout.strip(): return True, "no new changes to commit"
    message = f"chore: update {name}"
    commit = git(path, "commit", "-m", message)
    return (commit.returncode == 0, message if commit.returncode == 0 else commit.stderr.strip())

def process(name, info, env, args):
    path = Path(info.get("path", ""))
    if not path.is_dir(): print(f"[SKIP] {name}: path missing: {path}"); return
    owner = env.get("GITHUB_OWNER", "shahudtaha08-source")
    token = env.get("GITHUB_TOKEN", "")
    repo = expected_repo(name, info)
    expected = expected_url(owner, repo)
    print(f"\n== {name} ==\npath: {path}\nexpected repo: {owner}/{repo}")
    if not is_repo(path):
        print("[PLAN] git init")
        if args.apply:
            r = git(path, "init")
            if r.returncode: print("[ERROR] git init failed:", r.stderr.strip()); return
            git(path, "branch", "-M", info.get("branch", "main")); print("[OK] git initialized")
    current = remote(path) if is_repo(path) else ""
    if current:
        cur_owner, cur_repo = github_parts(current)
        if not cur_owner:
            print(f"[WARN] origin is not a recognized GitHub URL: {current}")
            if not (args.repair_remote and args.apply): print("[SAFE] manual/explicit repair required"); return
        else:
            code, data = repo_state(cur_owner, cur_repo, token)
            if code == 200:
                canonical = data.get("clone_url", expected_url(cur_owner, cur_repo))
                if cur_owner.lower() == owner.lower() and cur_repo.lower() == repo.lower():
                    print(f"[OK] existing origin verified: {canonical}")
                else:
                    print(f"[PRESERVE] existing origin verified: {canonical}")
                    print("[SAFE] project mapping differs; not replacing a real repository")
                    return
            elif code in (401,403):
                print(f"[WARN] cannot verify existing origin without a valid token: {current}")
                print("[SAFE] not replacing origin"); return
            else:
                print(f"[BROKEN] existing origin not found/reachable ({code or 'network'}): {current}")
                if not (args.repair_remote and args.apply): print("[SAFE] use --repair-remote --apply only after review"); return
                r = git(path, "remote", "set-url", "origin", expected)
                if r.returncode: print(f"[ERROR] remote repair failed: {r.stderr.strip()}"); return
                print(f"[OK] origin repaired -> {expected}")
    else:
        code, _ = repo_state(owner, repo, token)
        if code == 200:
            print("[OK] expected GitHub repo already exists")
        elif not token:
            print("[SKIP] no origin; expected repo missing/unverified and GITHUB_TOKEN is missing"); return
        else:
            print("[PLAN] create GitHub repository")
            if args.apply:
                ok, detail = create_repo(owner, repo, token, env.get("GITHUB_VISIBILITY","private").lower() != "public")
                if not ok: print(f"[ERROR] repo creation failed: {detail}"); return
                print(f"[OK] {detail}")
        if args.apply:
            r = git(path, "remote", "add", "origin", expected)
            if r.returncode: print(f"[ERROR] adding remote failed: {r.stderr.strip()}"); return
            print("[OK] origin linked")
        else: print("[PLAN] link origin")
    print("[README]", ensure_readme(path, name, info, args.apply))
    if not args.apply: print("[DRY RUN] no files, remotes or GitHub repositories changed"); return
    ok, detail = commit_changes(path, name, info)
    if not ok: print(f"[ERROR] commit failed: {detail}"); return
    print(f"[OK] {detail}")
    if args.push:
        active = branch(path, info.get("branch", "main")); push = git(path, "push", "-u", "origin", active)
        if push.returncode: print("[ERROR] push failed; remote history was not overwritten:", push.stderr.strip())
        else: print(f"[OK] pushed {active}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project"); parser.add_argument("--apply", action="store_true"); parser.add_argument("--push", action="store_true"); parser.add_argument("--repair-remote", action="store_true")
    args = parser.parse_args(); env = load_env()
    if not INDEX_FILE.exists(): print(f"[ERROR] missing {INDEX_FILE}"); sys.exit(1)
    projects = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    if args.project:
        if args.project not in projects: print(f"[ERROR] project not found: {args.project}"); sys.exit(1)
        projects = {args.project: projects[args.project]}
    for name, info in projects.items(): process(name, info, env, args)
if __name__ == "__main__": main()
