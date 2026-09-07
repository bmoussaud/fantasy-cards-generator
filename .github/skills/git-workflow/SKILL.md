---
name: "git-workflow"
description: "Squad branching model: main-based workflow for this Python application"
domain: "version-control"
confidence: "high"
source: "team-decision"
updated: "2026-09-07"
---

## Context

This repository is a **Python application** (not an npm package). It uses a **single-trunk model**: all feature work branches from `main` and PRs target `main` directly. There is no `dev`, `insiders`, or npm publish pipeline.

| Branch | Purpose |
|--------|---------|
| `main` | Single source of truth — all PRs target here |
| `squad/{issue-number}-{slug}` | Feature/fix work per issue |

## Dirty-State Preflight

**Before any branch operation**, verify the working tree is clean:

```bash
git status --short
```

If there are uncommitted changes:
1. **Do not** run `git add -A`, reset, or stash blindly.
2. Identify ownership: is this your WIP or someone else's?
3. If yours, commit to a dedicated WIP branch (`squad/wip-{slug}`) first.
4. If uncertain, **STOP and report** — do not overwrite or stash unowned work.

## Branch Naming Convention

Issue branches MUST use: `squad/{issue-number}-{kebab-case-slug}`

Non-issue housekeeping branches (no issue number needed): `squad/{kebab-case-slug}`

Examples:
- `squad/195-fix-version-stamp-bug`
- `squad/42-add-profile-api`
- `squad/git-workflow-hygiene`

## Workflow for Issue Work

1. **Verify root is clean, then branch from main:**
   ```bash
   git status --short   # must be empty
   git fetch origin main
   git checkout -b squad/{issue-number}-{slug} origin/main
   ```

2. **Mark issue in-progress:**
   ```bash
   gh issue edit {number} --add-label "status:in-progress"
   ```

3. **Create draft PR targeting main:**
   ```bash
   gh pr create --base main --title "{description}" --body "Closes #{issue-number}" --draft
   ```

4. **Do the work.** Stage files explicitly — never use `git add -A` unless you have verified every changed file.

5. **Push and mark ready:**
   ```bash
   git push -u origin squad/{issue-number}-{slug}
   gh pr ready
   ```

6. **After PR is merged — safe cleanup only:**

   **Preflight:** Confirm your working tree and index are clean (`git status --short` must be empty), and no other active agent currently owns the root checkout you intend to switch into.

   ```bash
   BRANCH=squad/{issue-number}-{slug}

   # 1. Confirm merged state
   STATE=$(gh pr view {pr-number} --json state -q .state)
   [ "$STATE" = "MERGED" ] || { echo "PR not merged — aborting"; exit 1; }

   # 2. Fetch the exact tip SHA that was on the branch at PR submission/merge time
   #    Use headRefOid, NOT mergeCommit — for squash/rebase merges those are always different SHAs:
   #    mergeCommit is the landing commit on main; headRefOid is the original branch tip
   PR_HEAD=$(gh pr view {pr-number} --json headRefOid -q .headRefOid)

   # 3. Compare the live remote tip to the recorded PR head
   #    If they differ, new commits were pushed to the branch after the merge — do not delete
   REMOTE_SHA=$(git ls-remote origin "refs/heads/$BRANCH" | awk '{print $1}')
   [ -z "$REMOTE_SHA" ] || [ "$REMOTE_SHA" = "$PR_HEAD" ] \
     || { echo "Remote tip ($REMOTE_SHA) != PR head ($PR_HEAD) — inspect before deleting"; exit 1; }

   # 4. Delete remote branch only when tip still matches (lease prevents a TOCTOU race)
   [ -n "$REMOTE_SHA" ] && \
     git push origin --delete "$BRANCH" --force-with-lease="$BRANCH:$PR_HEAD"

   # 5. Squash/rebase integration means the original branch tip is NOT an ancestor of main
   #    Never use -D (force-delete) to bypass the ancestry check — that permanently discards commits
   #    Create an archive tag first if the head is not reachable from main
   git merge-base --is-ancestor "$BRANCH" origin/main 2>/dev/null \
     || git tag "archive/pr-{pr-number}-head" "$BRANCH"
   git branch -d "$BRANCH"
   # If -d still fails: the branch has commits not reachable from main beyond the archive tag.
   # Confirm the tag was created and captures everything needed before considering -D.
   ```

   To update main in your own clean worktree — only after confirming this worktree is on `main`, is clean, and no other workstream owns root:
   ```bash
   git pull --ff-only origin main   # --ff-only: refuses to proceed if main diverged unexpectedly
   ```

   **Never delete a remote branch on state=MERGED alone — always compare the live remote tip to `headRefOid` first.**

## Parallel Multi-Issue Work (Worktrees)

Use `git worktree` when two or more issues must proceed simultaneously so that each agent has an isolated working directory — no branch-switching collisions.

### When to Use Worktrees

| Scenario | Strategy |
|----------|----------|
| Single issue | Standard workflow above — no worktree needed |
| 2+ simultaneous issues in same repo | One worktree per issue |
| Work spanning multiple repos | Separate clones as siblings (see below) |

### Setup — Explicit, Per-Workstream

Worktrees are **not** enabled automatically. Each workstream requires an explicit `git worktree add` with a named branch from main:

```bash
# From the root clone — root must be clean first
git status --short          # must be empty before adding worktrees
git fetch origin main

# Sibling naming convention: {repo-name}-{slug}
git worktree add /workspaces/fantasy-cards-generator-{slug} \
    -b squad/{issue-number}-{slug} origin/main
```

Worktree path: sibling directory `/workspaces/{repo-name}-{slug}`.

Each worktree:
- Has its own working directory and index
- Is on its own `squad/{number}-{slug}` branch from **main**
- Shares the same `.git` object store (disk-efficient)
- Resolves `TEAM_ROOT` to **its own** root — all `.squad/` writes go there

### Per-Worktree Agent Workflow

Each agent works inside its own worktree. Stage explicitly:

```bash
cd /workspaces/fantasy-cards-generator-{slug}

# Stage only files you own — never git add -A blindly
git add path/to/changed/file.py
git commit -m "fix: description (#195)

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
git push -u origin squad/195-fix-stamp-bug

gh pr create --base main --title "fix: description" --body "Closes #195" --draft
```

### .squad/ State in Worktrees

The `.squad/` directory exists in each worktree as checked-out content:
- `.gitattributes` declares `merge=union` on append-only files (history.md, decisions.md, logs)
- **Rule:** append only — never rewrite or reorder `.squad/` files in a worktree
- `merge=union` preserves every line from both sides but does **not** verify semantic correctness — duplicate entries or contradictory state are possible; review `.squad/` merge results before committing
- Resolve TEAM_ROOT from your worktree root, not the original clone

### Shared vs. Isolated State Across Worktrees

Each worktree has its **own** working directory and index — file edits and staged changes in one worktree are invisible to others. However, the following are **shared** across all worktrees that reference the same `.git` directory:

- Git refs (branches, tags) — a branch delete or tag create in one worktree is immediately visible in all others
- The stash — `git stash`, `git stash pop`, or `git stash drop` in any worktree affects every worktree
- Config (`git config`) and hooks

Do not stash, drop stashes, delete branches, or modify tags from one worktree on behalf of work that originated in another.

### Reuse Check Before Pull

Before pulling or re-fetching, confirm the worktree is still registered:

```bash
git worktree list
```

A missing worktree path (stale reference) must be pruned before re-adding. Use `--dry-run` first to confirm only truly-gone paths will be removed — never prune to fix a missing directory that may still be mounted or owned by another active session:

```bash
git worktree prune --dry-run   # inspect; confirm the listed path is truly unmounted/gone
git worktree prune             # only after verifying dry-run output is correct
```

### Cleanup After Merge

**Preflight:** Confirm no active agent owns the worktree, the working directory is clean, and no uncommitted work or stash entries remain from the session before removing.

Only after performing the full branch-safety checks from "After PR is merged — safe cleanup only" above (state=MERGED, headRefOid comparison, lease, ancestry check, archive tag if needed):

```bash
# From the root clone — remove the worktree working directory
git worktree remove /workspaces/fantasy-cards-generator-{slug}

# Dry-run prune first to confirm only truly-gone paths will be affected
git worktree prune --dry-run   # inspect output before proceeding
git worktree prune

# For remote and local branch deletion, follow the steps in
# "After PR is merged — safe cleanup only" — do not delete branches
# here without the headRefOid comparison and archive-tag guard.
```

If a worktree directory was deleted manually (e.g., `rm -rf`), run `git worktree prune --dry-run` first to confirm the stale entry is the one you intend to remove before running without `--dry-run`.

---

## Multi-Repo Downstream Scenarios

When work spans multiple repositories, use sibling clones:

```
/workspaces/
  fantasy-cards-generator/   # this repo
  other-dependency/          # sibling repo
```

Each repo follows its own branching convention. Coordinate via linked PRs.

---

## Anti-Patterns

- ❌ Branching from anything other than `main` (no `dev`, no `insiders`)
- ❌ PR targeting anything other than `main`
- ❌ Non-conforming branch names (must be `squad/{number}-{slug}` or `squad/{slug}`)
- ❌ Using `git add -A` without inspecting every changed file first
- ❌ Switching branches in the root clone while active worktrees exist — use worktrees instead
- ❌ Deleting a remote branch before confirming PR state = MERGED
- ❌ Applying, dropping, or clearing stashes that belong to another agent or workstream
- ❌ Running `git reset --hard`, `git checkout -- .`, or `git clean -fd` when there is unverified uncommitted work
- ❌ Claiming worktrees are "auto-enabled" — they require explicit `git worktree add` per workstream
- ❌ Using `mergeCommit` OID instead of `headRefOid` when verifying branch tip identity before deletion
- ❌ Force-deleting a local branch (`-D`) to bypass an ancestry failure without first creating an archive tag
- ❌ Running `git pull` without `--ff-only` or without a clean correct-branch preflight
- ❌ Running `git worktree prune` without `--dry-run` first to confirm only stale/unmounted paths are affected
