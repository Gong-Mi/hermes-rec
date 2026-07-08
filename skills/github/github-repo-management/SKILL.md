---
name: github-repo-management
description: "Clone/create/fork repos; manage remotes, releases."
triggers:
  - "Clone, create, or fork GitHub repositories"
  - "Manage git remotes, releases, or repository settings"
  - "Use gh CLI for repository operations"
version: 1.1.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [GitHub, Repositories, Git, Releases, Secrets, Configuration, Authentication, Issues]
    related_skills: [pr-review-workflow, github-actions-run-inspect]
---

# GitHub Repository Management

This is the GitHub repo-lifecycle router and safe minimal workflow skill. Use it to decide the correct GitHub operation path, perform low/medium-risk repository actions, and route high-risk or specialized work to narrower skills before acting.

Core scope:
- Clone, create, fork, and inspect repositories.
- Manage remotes, repo metadata, releases, issues, secrets, branch protection, and basic Actions workflow operations.
- Prefer `gh` when authenticated; fall back to `git` + GitHub REST API via `curl` when `gh` is unavailable.

Routing rules:
- If the task is PR creation, push-for-review, merge, or upstream contribution, load `pr-review-workflow` first. This skill may supply GitHub commands, but it must not override PR safety discipline.
- If the task is CI run inspection, failed logs, or reruns, load `github-actions-run-inspect` first when available.
- If the task changes secrets, visibility, branch protection, repository deletion, release publication, or merge state, treat it as high-risk: verify the target repo/branch and get explicit user scope before applying changes.
- If submodules fail, use this skill's submodule section or split it into a dedicated reference before doing destructive cleanup.

Safety defaults:
- Stage only explicit intended files; never use broad `git add .` in shared or dirty worktrees.
- Before push/PR/release/settings changes, inspect `git status`, current remote, branch, and relevant diff.
- Do not push local configs, credentials, private paths, Termux-only patches, or unrelated worktree changes.

## Prerequisites

- Authenticated with GitHub. Prefer `gh auth status`; otherwise use `GITHUB_TOKEN` from the environment or credential helper.

### Setup

```bash
if command -v gh &>/dev/null && gh auth status &>/dev/null; then
  AUTH="gh"
else
  AUTH="git"
  if [ -z "$GITHUB_TOKEN" ]; then
    if [ -f ~/.hermes/.env ] && grep -q "^GITHUB_TOKEN=" ~/.hermes/.env; then
      GITHUB_TOKEN=$(grep "^GITHUB_TOKEN=" ~/.hermes/.env | head -1 | cut -d= -f2 | tr -d '\n\r')
    elif grep -q "github.com" ~/.git-credentials 2>/dev/null; then
      GITHUB_TOKEN=$(grep "github.com" ~/.git-credentials 2>/dev/null | head -1 | sed 's|https://[^:]*:\([^@]*\)@.*|\1|')
    fi
  fi
fi

# Get your GitHub username (needed for several operations)
if [ "$AUTH" = "gh" ]; then
  GH_USER=$(gh api user --jq '.login')
else
  GH_USER=$(curl -s -H "Authorization: token $GITHUB_TOKEN" https://api.github.com/user | python3 -c "import sys,json; print(json.load(sys.stdin)['login'])")
fi
```

If you're inside a repo already:

```bash
REMOTE_URL=$(git remote get-url origin)
OWNER_REPO=$(echo "$REMOTE_URL" | sed -E 's|.*github\.com[:/]||; s|\.git$||')
OWNER=$(echo "$OWNER_REPO" | cut -d/ -f1)
REPO=$(echo "$OWNER_REPO" | cut -d/ -f2)
```

---

## 0. Submodule Management

Submodules often point to a **different remote** than the parent repo — `.gitmodules` can reference GitHub SSH while the parent was cloned from a self-hosted Gitea/GitLab, or vice versa. This section covers diagnosing and fixing remote mismatches.

### Diagnosing Submodule Remote Mismatch

When `git submodule update --init` fails with `Repository not found` or `Could not read from remote repository`:

1. Check the parent repo's origin: `git remote -v`
2. Check the submodule's configured URLs: `cat .gitmodules`
3. If they point to different servers, the submodule URLs need updating

Also check if `.gitmodules` history changed — a commit like `"chore: point submodules at GitHub"` indicates the URLs were recently rewritten and may point to a repo you can't access.

### Fixing Submodule URLs

```bash
# Update submodule URLs to the alternate remote
git submodule set-url <submodule-name> <new-url>

# Then re-init and update (with --depth 1 for speed)
git submodule update --init --recursive --depth 1
```

### Handling Missing Pinned Commits

Even after fixing the URL, a submodule may fail with:

```
fatal: remote error: upload-pack: not our ref <commit-hash>
fatal: Fetched in submodule path '<path>', but it did not contain <commit-hash>.
```

This means the parent repo pinned a commit that exists on the original remote but **not** on the alternate remote. To fix:

1. Check what branches/tags exist on the alternate remote: `git ls-remote --heads <new-url>`
2. Init the submodule manually by cloning from the correct remote at depth 1:
   ```bash
   rm -rf <path> && git clone --depth 1 -b <branch> <new-url> <path>
   ```

### Submodule Clone Timeout / Name-Resolution Failure

When `git submodule update --init` times out (common on Termux/Android with slow or flaky connections to self-hosted Git servers like Gitea), the submodule directory may be left empty or partially cloned. The `--depth 1` flag helps but isn't always enough.

**Full recovery procedure:**

1. **First, clean up stale state** — both the submodule path AND the superproject's module cache:
   ```bash
   git submodule deinit -f <name>
   rm -rf <path> .git/modules/<name>/
   ```

2. **Fix the submodule URL** if it points to the wrong remote (see "Fixing Submodule URLs" above).

3. **If `git submodule update --init --depth 1` still times out**, bypass the submodule machinery entirely:
   ```bash
   # Direct clone from the working remote at depth 1
   git clone --depth 1 <new-url> <path>
   ```

4. **If the submodule entry is stuck in the index** (`git rm` won't work due to staged changes):
   ```bash
   git add .gitmodules          # stage the URL fix first
   git rm --cached <name>       # remove from index
   rm -rf <path> .git/modules/<name>/   # clean files
   git submodule add <new-url> <path>   # re-register
   ```

**Pitfalls:**
- `git stash` on `.gitmodules` can silently revert your URL fix — always check the file after `stash pop`
- After `git rm --cached`, the `.gitmodules` changes must be staged first, or subsequent `git submodule add` will refuse with `please stage your changes to .gitmodules`
- The `--depth 1` flag does NOT work as a positional argument to `git submodule update --init` on older git versions — use it with direct `git clone` instead

### Pitfalls

- **Always check `.gitmodules` AND `.git/config`** — the submodule URL can differ between the two files. `.gitmodules` is what `git submodule update` reads; `.git/config` has the cached version from the last init.
- **The branch field in `.gitmodules` is NOT the pinned commit** — it's a hint for detached-HEAD updates. The actual commit hash is stored in the parent repo's tree (in the index).
- **`--depth 1` is essential on Termux/Android** where network and disk I/O are constrained. Without it, a full submodule clone can time out on slow connections.
- **Ask before `rm -rf` on submodule directories** — destructive operations need user confirmation.

---

## 1. Cloning Repositories

Cloning is pure `git` — works identically either way:

```bash
# Clone via HTTPS (works with credential helper or token-embedded URL)
git clone https://github.com/owner/repo-name.git

# Clone into a specific directory
git clone https://github.com/owner/repo-name.git ./my-local-dir

# Shallow clone (faster for large repos)
git clone --depth 1 https://github.com/owner/repo-name.git

# Clone a specific branch
git clone --branch develop https://github.com/owner/repo-name.git

# Clone via SSH (if SSH is configured)
git clone git@github.com:owner/repo-name.git
```

**With gh (shorthand):**

```bash
gh repo clone owner/repo-name
gh repo clone owner/repo-name -- --depth 1
```

### Protocol Fallback

`gh repo clone` defaults to SSH. On networks that block port 22, or when SSH keys are not configured, the clone will fail with `Connection closed` or `Could not read from remote repository`. In that case, fall back to HTTPS immediately:

```bash
# If gh clone fails, use git with HTTPS — no SSH keys needed
git clone https://github.com/owner/repo-name.git
```

This is especially common in restricted networks, containers, or fresh environments where `gh` is installed but SSH is not set up.

## 2. Creating Repositories

**With gh:**

```bash
# Create a public repo and clone it
gh repo create my-new-project --public --clone

# Private, with description and license
gh repo create my-new-project --private --description "A useful tool" --license MIT --clone

# Under an organization
gh repo create my-org/my-new-project --public --clone

# From existing local directory
cd /path/to/existing/project
gh repo create my-project --source . --public --push
```

**With git + curl:**

```bash
# Create the remote repo via API
curl -s -X POST \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/user/repos \
  -d '{
    "name": "my-new-project",
    "description": "A useful tool",
    "private": false,
    "auto_init": true,
    "license_template": "mit"
  }'

# Clone it
git clone https://github.com/$GH_USER/my-new-project.git
cd my-new-project

# -- OR -- push an existing local directory to the new repo
cd /path/to/existing/project
git init
git add <explicit-project-files>
git diff --cached --stat
git commit -m "Initial commit"
git remote add origin https://github.com/$GH_USER/my-new-project.git
git push -u origin main
```

To create under an organization:

```bash
curl -s -X POST \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/orgs/my-org/repos \
  -d '{"name": "my-new-project", "private": false}'
```

### From a Template

**With gh:**

```bash
gh repo create my-new-app --template owner/template-repo --public --clone
```

**With curl:**

```bash
curl -s -X POST \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/owner/template-repo/generate \
  -d '{"owner": "'"$GH_USER"'", "name": "my-new-app", "private": false}'
```

## 3. Forking Repositories

**With gh:**

```bash
gh repo fork owner/repo-name --clone
```

**With git + curl:**

```bash
# Create the fork via API
curl -s -X POST \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/owner/repo-name/forks

# Wait a moment for GitHub to create it, then clone
sleep 3
git clone https://github.com/$GH_USER/repo-name.git
cd repo-name

# Add the original repo as "upstream" remote
git remote add upstream https://github.com/owner/repo-name.git
```

### Keeping a Fork in Sync

```bash
# Pure git — works everywhere
git fetch upstream
git checkout main
git merge upstream/main
git push origin main
```

**With gh (shortcut):**

```bash
gh repo sync $GH_USER/repo-name
```

## 4. Repository Information

**With gh:**

```bash
gh repo view owner/repo-name
gh repo list --limit 20
gh search repos "machine learning" --language python --sort stars
```

**With curl:**

```bash
# View repo details
curl -s \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO \
  | python3 -c "
import sys, json
r = json.load(sys.stdin)
print(f\"Name: {r['full_name']}\")
print(f\"Description: {r['description']}\")
print(f\"Stars: {r['stargazers_count']}  Forks: {r['forks_count']}\")
print(f\"Default branch: {r['default_branch']}\")
print(f\"Language: {r['language']}\")"

# List your repos
curl -s \
  -H "Authorization: token $GITHUB_TOKEN" \
  "https://api.github.com/user/repos?per_page=20&sort=updated" \
  | python3 -c "
import sys, json
for r in json.load(sys.stdin):
    vis = 'private' if r['private'] else 'public'
    print(f\"  {r['full_name']:40}  {vis:8}  {r.get('language', ''):10}  ★{r['stargazers_count']}\")"

# Search repos
curl -s \
  "https://api.github.com/search/repositories?q=machine+learning+language:python&sort=stars&per_page=10" \
  | python3 -c "
import sys, json
for r in json.load(sys.stdin)['items']:
    print(f\"  {r['full_name']:40}  ★{r['stargazers_count']:6}  {r['description'][:60] if r['description'] else ''}\")"
```

## 5. Repository Settings

**With gh:**

```bash
gh repo edit --description "Updated description" --visibility public
gh repo edit --enable-wiki=false --enable-issues=true
gh repo edit --default-branch main
gh repo edit --add-topic "machine-learning,python"
gh repo edit --enable-auto-merge
```

**With curl:**

```bash
curl -s -X PATCH \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO \
  -d '{
    "description": "Updated description",
    "has_wiki": false,
    "has_issues": true,
    "allow_auto_merge": true
  }'

# Update topics
curl -s -X PUT \
  -H "Authorization: token $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github.mercy-preview+json" \
  https://api.github.com/repos/$OWNER/$REPO/topics \
  -d '{"names": ["machine-learning", "python", "automation"]}'
```

## 6. Branch Protection

```bash
# View current protection
curl -s \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/branches/main/protection

# Set up branch protection
curl -s -X PUT \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/branches/main/protection \
  -d '{
    "required_status_checks": {
      "strict": true,
      "contexts": ["ci/test", "ci/lint"]
    },
    "enforce_admins": false,
    "required_pull_request_reviews": {
      "required_approving_review_count": 1
    },
    "restrictions": null
  }'
```

## 7. Secrets Management (GitHub Actions)

**With gh:**

```bash
gh secret set API_KEY --body "your-secret-value"
gh secret set SSH_KEY < ~/.ssh/id_rsa
gh secret list
gh secret delete API_KEY
```

**With curl:**

Secrets require encryption with the repo's public key — more involved via API:

```bash
# Get the repo's public key for encrypting secrets
curl -s \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/actions/secrets/public-key

# Encrypt and set (requires Python with PyNaCl)
python3 -c "
from base64 import b64encode
from nacl import encoding, public
import json, sys

# Get the public key
key_id = '<key_id_from_above>'
public_key = '<base64_key_from_above>'

# Encrypt
sealed = public.SealedBox(
    public.PublicKey(public_key.encode('utf-8'), encoding.Base64Encoder)
).encrypt('your-secret-value'.encode('utf-8'))
print(json.dumps({
    'encrypted_value': b64encode(sealed).decode('utf-8'),
    'key_id': key_id
}))"

# Then PUT the encrypted secret
curl -s -X PUT \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/actions/secrets/API_KEY \
  -d '<output from python script above>'

# List secrets (names only, values hidden)
curl -s \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/actions/secrets \
  | python3 -c "
import sys, json
for s in json.load(sys.stdin)['secrets']:
    print(f\"  {s['name']:30}  updated: {s['updated_at']}\")"
```

Note: For secrets, `gh secret set` is dramatically simpler. If setting secrets is needed and `gh` isn't available, recommend installing it for just that operation.

## 8. Releases

**With gh:**

```bash
gh release create v1.0.0 --title "v1.0.0" --generate-notes
gh release create v2.0.0-rc1 --draft --prerelease --generate-notes
gh release create v1.0.0 ./dist/binary --title "v1.0.0" --notes "Release notes"
gh release list
gh release download v1.0.0 --dir ./downloads
```

**With curl:**

```bash
# Create a release
curl -s -X POST \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/releases \
  -d '{
    "tag_name": "v1.0.0",
    "name": "v1.0.0",
    "body": "## Changelog\n- Feature A\n- Bug fix B",
    "draft": false,
    "prerelease": false,
    "generate_release_notes": true
  }'

# List releases
curl -s \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/releases \
  | python3 -c "
import sys, json
for r in json.load(sys.stdin):
    tag = r.get('tag_name', 'no tag')
    print(f\"  {tag:15}  {r['name']:30}  {'draft' if r['draft'] else 'published'}\")"

# Upload a release asset (binary file)
RELEASE_ID=<id_from_create_response>
curl -s -X POST \
  -H "Authorization: token $GITHUB_TOKEN" \
  -H "Content-Type: application/octet-stream" \
  "https://uploads.github.com/repos/$OWNER/$REPO/releases/$RELEASE_ID/assets?name=binary-amd64" \
  --data-binary @./dist/binary-amd64
```

## 9. GitHub Actions Workflows

**With gh:**

```bash
gh workflow list
gh run list --limit 10
gh run view <RUN_ID>
gh run view <RUN_ID> --log-failed
gh run rerun <RUN_ID>
gh run rerun <RUN_ID> --failed
gh workflow run ci.yml --ref main
gh workflow run deploy.yml -f environment=staging
```

**With curl:**

```bash
# List workflows
curl -s \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/actions/workflows \
  | python3 -c "
import sys, json
for w in json.load(sys.stdin)['workflows']:
    print(f\"  {w['id']:10}  {w['name']:30}  {w['state']}\")"

# List recent runs
curl -s \
  -H "Authorization: token $GITHUB_TOKEN" \
  "https://api.github.com/repos/$OWNER/$REPO/actions/runs?per_page=10" \
  | python3 -c "
import sys, json
for r in json.load(sys.stdin)['workflow_runs']:
    print(f\"  Run {r['id']}  {r['name']:30}  {r['conclusion'] or r['status']}\")"

# Download failed run logs
RUN_ID=<run_id>
curl -s -L \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/actions/runs/$RUN_ID/logs \
  -o /tmp/ci-logs.zip
cd /tmp && unzip -o ci-logs.zip -d ci-logs

# Re-run a failed workflow
curl -s -X POST \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/actions/runs/$RUN_ID/rerun

# Re-run only failed jobs
curl -s -X POST \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/actions/runs/$RUN_ID/rerun-failed-jobs

# Trigger a workflow manually (workflow_dispatch)
WORKFLOW_ID=<workflow_id_or_filename>
curl -s -X POST \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/actions/workflows/$WORKFLOW_ID/dispatches \
  -d '{"ref": "main", "inputs": {"environment": "staging"}}'
```

## 10. Gists

**With gh:**

```bash
gh gist create script.py --public --desc "Useful script"
gh gist list
```

**With curl:**

```bash
# Create a gist
curl -s -X POST \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/gists \
  -d '{
    "description": "Useful script",
    "public": true,
    "files": {
      "script.py": {"content": "print(\"hello\")"}
    }
  }'

# List your gists
curl -s \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/gists \
  | python3 -c "
import sys, json
for g in json.load(sys.stdin):
    files = ', '.join(g['files'].keys())
    print(f\"  {g['id']}  {g['description'] or '(no desc)':40}  {files}\")"
```

## 10. Authentication Setup

See `scripts/gh-env.sh` for the auth-detection boilerplate used throughout this skill. For full authentication setup (HTTPS tokens, SSH keys, gh CLI login), the key patterns are:

**Detection:**
```bash
if command -v gh &>/dev/null && gh auth status &>/dev/null; then
  AUTH="gh"
else
  AUTH="git"
  # Extract token from ~/.hermes/.env or ~/.git-credentials
fi
```

**Git-only (no gh):**
```bash
git config --global credential.helper store
git ls-remote https://github.com/<user>/<repo>.git
# Enter username + personal access token as password
```

**gh CLI:**
```bash
gh auth login          # Browser or token
echo "<token>" | gh auth login --with-token
gh auth setup-git
```

## 11. Issues Management

### List Issues
```bash
gh issue list --state open --label "bug"
```

### Create Issue
```bash
gh issue create --title "Bug: ..." --body "## Description\n..." --label "bug"
```

### Close/Reopen
```bash
gh issue close 42 --reason "not planned"
gh issue reopen 42
```

### Templates
- `templates/bug-report.md` — Bug report template
- `templates/feature-request.md` — Feature request template

## PR Workflow Quick Reference

### Branch Creation
```bash
git fetch origin
git checkout main && git pull origin main
git checkout -b feat/description
```

### Making Commits
Stage only intended files when the worktree has unrelated changes:
```bash
git status --short
git add src/auth.py tests/test_auth.py
git diff --cached --stat
git commit -m "feat: description"
```

### Pushing and Creating a PR
```bash
git push -u origin HEAD

# With gh
gh pr create --title "feat: description" --body "## Summary\n..."

# With curl (when gh unavailable)
BRANCH=$(git branch --show-current)
curl -s -X POST -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/pulls \
  -d "{\"title\":\"feat: description\",\"head\":\"$BRANCH\",\"base\":\"main\"}"
```

### Monitoring CI Status
```bash
gh pr checks
gh pr checks --watch
```

### Merging
```bash
gh pr merge --squash --delete-branch
gh pr merge --auto --squash --delete-branch
```

### Auto-Fixing CI Failures Loop
1. Check CI status → identify failures
2. Read failure logs → understand the error
3. Use `read_file` + `patch`/`write_file` → fix the code
4. Stage only intended files, then commit and push:
   ```bash
   git status --short
   git add <explicit-file-1> <explicit-file-2>
   git diff --cached --stat
   git commit -m "fix: ..."
   git push
   ```
5. Wait for CI → re-check status
6. Repeat if still failing (up to 3 attempts, then ask user)

## 13. Repo Update Triage

Review remote updates safely before pulling, especially with a dirty local checkout.

### Workflow
1. Fetch first: `git fetch --prune`
2. Report current branch state: `git status --short --branch`
3. Compare local HEAD against upstream:
   ```bash
   git rev-list --left-right --count HEAD...@{upstream}
   git log --oneline --decorate --left-right --cherry-pick HEAD...@{upstream}
   ```
4. Check whether incoming upstream files overlap local dirty files:
   ```bash
   comm -12 \
     <(git diff --name-only HEAD..@{upstream} | sort) \
     <((git diff --name-only; git diff --cached --name-only; git ls-files --others --exclude-standard) | sort -u)
   ```
5. If no overlap and no local-only commits, recommend fast-forward:
   ```bash
   git branch backup/update-wip-before-pull
   git stash push -u -m "wip before update"
   git pull --ff-only
   git stash pop
   ```
6. If overlap exists, do not claim it is safe. Recommend inspecting upstream diff or committing/stashing local work first.

### Shallow Clone Triage
For huge upstreams (LLVM-style), avoid full `--unshallow`:
```bash
git fetch --depth=1 origin 'refs/tags/<tag>:refs/tags/<tag>'
git fetch --update-shallow --shallow-exclude=<tag> --tags --prune origin '+refs/heads/*:refs/remotes/origin/*'
```

## 14. Codebase Inspection (pygount)

Analyze repositories for lines of code, language breakdown, and code-vs-comment ratios.

```bash
pip install pygount
pygount --format=summary --folders-to-skip=".git,node_modules,venv" .
```

**Always use `--folders-to-skip`** to exclude dependency/build directories, otherwise pygount will crawl them and may hang.

### Common exclusions by project type:
- Python: `.git,venv,.venv,__pycache__,.cache,dist,build,.tox`
- JavaScript: `.git,node_modules,dist,build,.next,.cache`

### Output formats
- `--format=summary` — language breakdown table (default recommendation)
- `--format=json` — for programmatic use
- `--suffix=py,rs` — filter by specific languages

## Quick Reference Table

| Action | gh | git + curl |
|--------|-----|-----------|
| Clone | `gh repo clone o/r` | `git clone https://github.com/o/r.git` |
| Create repo | `gh repo create name --public` | `curl POST /user/repos` |
| Fork | `gh repo fork o/r --clone` | `curl POST /repos/o/r/forks` + `git clone` |
| Repo info | `gh repo view o/r` | `curl GET /repos/o/r` |
| Edit settings | `gh repo edit --...` | `curl PATCH /repos/o/r` |
| Create release | `gh release create v1.0` | `curl POST /repos/o/r/releases` |
| List workflows | `gh workflow list` | `curl GET /repos/o/r/actions/workflows` |
| Rerun CI | `gh run rerun ID` | `curl POST /repos/o/r/actions/runs/ID/rerun` |
| Set secret | `gh secret set KEY` | `curl PUT /repos/o/r/actions/secrets/KEY` (+ encryption) |
| List issues | `gh issue list` | `curl GET /repos/o/r/issues` |
| Create issue | `gh issue create` | `curl POST /repos/o/r/issues` |
