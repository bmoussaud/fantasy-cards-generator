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
   ```bash
   # Verify the remote branch's exact tip SHA matches the PR head before deleting
   REMOTE_SHA=$(git ls-remote origin refs/heads/squad/{issue-number}-{slug} | awk '{print $1}')
   PR_SHA=$(gh pr view {pr-number} --json mergeCommit -q .mergeCommit.oid)
   # For squash merges the branch tip != merge commit; confirm PR state=MERGED is sufficient
   gh pr view {pr-number} --json state -q .state   # must be MERGED

   git checkout main
   git pull origin main
   git branch -d squad/{issue-number}-{slug}
   git push origin --delete squad/{issue-number}-{slug}
   ```

   **Never delete a remote branch without confirming PR state = MERGED.**

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
- Resolve TEAM_ROOT from your worktree root, not the original clone

### Reuse Check Before Pull

Before pulling or re-fetching, confirm the worktree is still registered:

```bash
git worktree list
```

A missing worktree path (stale reference) must be pruned before re-adding:

```bash
git worktree prune
```

### Cleanup After Merge

Only after confirming PR state = MERGED:

```bash
# From the root clone
git worktree remove /workspaces/fantasy-cards-generator-{slug}
git worktree prune
git branch -d squad/{issue-number}-{slug}
git push origin --delete squad/{issue-number}-{slug}
```

If a worktree directory was deleted manually (e.g., `rm -rf`), `git worktree prune` cleans the stale metadata — safe to run then re-add.

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
