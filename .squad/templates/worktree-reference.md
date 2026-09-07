# Worktree Reference

### Worktree Awareness

Squad and all spawned agents may be running inside a **git worktree** rather than the main checkout. All `.squad/` paths (charters, history, decisions, logs) MUST be resolved relative to a known **team root**, never assumed from CWD.

**Two strategies for resolving the team root:**

| Strategy | Team root | State scope | When to use |
|----------|-----------|-------------|-------------|
| **worktree-local** | Current worktree root | Branch-local — each worktree has its own `.squad/` state | Feature branches that need isolated decisions and history |
| **main-checkout** | Main working tree root | Shared — all worktrees read/write the main checkout's `.squad/` | Single source of truth for memories, decisions, and logs across all branches |

**How the Coordinator resolves the team root (on every session start):**

0. **Check config.json overrides first** — read `.squad/config.json` in the current directory (or at the git root):
   - If `teamRoot` is set → Team root = that path. **STOP — do not walk further.**
   - If `stateLocation` is `"external"` → Resolve external AppData path. Team root = external path. **STOP.**
   - Otherwise → continue to step 1.
1. **Check CWD first** — does `.squad/` exist in the current working directory?
   - **Yes** → Team root = CWD. This handles monorepos where `.squad/` lives in a subfolder.
2. If not, run `git rev-parse --show-toplevel` to get the current worktree root.
3. Check if `.squad/` exists at that root (fall back to `.ai-team/` for repos that haven't migrated yet).
   - **Yes** → use **worktree-local** strategy. Team root = current worktree root.
   - **No** → use **main-checkout** strategy. Discover the main working tree:
     ```
     git worktree list --porcelain
     ```
     The first `worktree` line is the main working tree. Team root = that path.
4. The user may override the strategy at any time (e.g., *"use main checkout for team state"* or *"keep team state in this worktree"*).

**Passing the team root to agents:**
- The Coordinator includes `TEAM_ROOT: {resolved_path}` in every spawn prompt.
- Agents resolve ALL `.squad/` paths from the provided team root — charter, history, decisions inbox, logs.
- Agents never discover the team root themselves. They trust the value from the Coordinator.

**Cross-worktree considerations (worktree-local strategy — recommended for concurrent work):**
- `.squad/` files are **branch-local**. Each worktree works independently — no locking, no shared-state races.
- When branches merge into main, `.squad/` state merges with them. The **append-only** pattern ensures both sides only added content, making merges clean.
- A `merge=union` driver in `.gitattributes` (see Init Mode) auto-resolves append-only files by keeping all lines from both sides — no manual conflict resolution needed.
- The Scribe commits `.squad/` changes to the worktree's branch. State flows to other branches through normal git merge / PR workflow.

**Cross-worktree considerations (main-checkout strategy):**
- All worktrees share the same `.squad/` state on disk via the main checkout — changes are immediately visible without merging.
- **Not safe for concurrent sessions.** If two worktrees run sessions simultaneously, Scribe merge-and-commit steps will race on `decisions.md` and git index. Use only when a single session is active at a time.
- Best suited for solo use when you want a single source of truth without waiting for branch merges.

### Worktree Lifecycle Management

**Project Policy (overrides runtime defaults):** In this repository, the coordinator MUST explicitly create or reuse an isolated worktree for each independent parallel writing workstream, regardless of whether `SQUAD_WORKTREES` is set or any runtime worktree-auto-creation flag is enabled. This is a manual enforcement procedure, not an automatic tool hook.

- One issue may require **multiple independent writers** — each must have its own dedicated branch and worktree, or work must be explicitly serialized (one writer active at a time).
- The same shared checkout is **not parallel-safe** for concurrent writers, even when they appear to modify different files.
- Each worktree's `TEAM_ROOT` resolves to its own worktree root by default; all `.squad/` writes go there.

**Worktree mode activation (runtime options — project policy above takes precedence):**
- Explicit: `worktrees: true` in project config (squad.config.ts or package.json `squad` section)
- Environment: `SQUAD_WORKTREES=1` set in environment variables
- Default: `false` (backward compatibility — agents work in the main repo)
- These runtime flags affect auto-creation behavior but do not change the project enforcement requirement above.

**Creating worktrees:**
- One worktree per independent writing workstream (a single issue may need multiple worktrees if it has multiple concurrent writers)
- Multiple agents on the same issue and same workstream share a worktree only when they do **not** write concurrently
- Path convention: `{repo-parent}/{repo-name}-{issue-number}` (or `-{slug}` for non-issue work)
  - Example: Working on issue #42 in `/workspaces/fantasy-cards-generator` → worktree at `/workspaces/fantasy-cards-generator-42`
- Branch: `squad/{issue-number}-{kebab-case-slug}` (created from `main`)

**Dependency management (Python/uv):**
- This is a Python application — do not link or install `node_modules`.
- Each worktree uses its own uv environment. Only restore/install dependencies if:
  - The chosen command fails due to missing dependencies, OR
  - `pyproject.toml` or `uv.lock` changed since the last install.
- The existing uv runner handles dependency restoration automatically when needed; do not install proactively.

**Reusing worktrees:**
- Before creating a new worktree, check if one exists: `git worktree list`
- **Only reuse after all of the following are confirmed:**
  1. The path exists and the branch matches the intended workstream
  2. `git status --short` is empty — no uncommitted work from a prior session
  3. No other active agent currently owns that worktree
  4. The local branch has not diverged from origin (check `git status -sb` before syncing)
- To sync an eligible worktree: `git pull --ff-only origin {branch}` — if this fails, the branch has diverged; do not force.
- **Do not share a worktree across concurrent independent writing agents.** Even modifying different files is not parallel-safe: a global stash, reset, or clean from one agent can destroy another agent's uncommitted work. Independent parallel writers require separate branches and separate worktrees with explicit ownership assignment.

**Cleanup:**
- After a PR is merged, follow the full safe cleanup steps from the git-workflow skill (`SKILL.md`) — including headRefOid comparison, `--force-with-lease` for remote deletion, ancestry check, and archive tag if the original head is not reachable from main.
- **Preflight:** Confirm no active agent owns the worktree and there is no uncommitted work before removing.
- `git worktree remove {path}` removes the working directory only — the branch and stash are not affected.
- Run `git worktree prune --dry-run` before `git worktree prune` to confirm only stale/unmounted paths will be removed.
- Ralph heartbeat can trigger cleanup checks for merged branches.

### Pre-Spawn: Worktree Setup

When spawning an agent for issue-based work (user request references an issue number, or agent is working on a GitHub issue):

**1. Check worktree mode:**
- Is `SQUAD_WORKTREES=1` set in the environment?
- Or does the project config have `worktrees: true`?
- If neither: skip worktree setup → agent works in the main repo (existing behavior)

**2. If worktrees enabled:**

a. **Determine the worktree path:**
   - Parse issue number from context (e.g., `#42`, `issue 42`, GitHub issue assignment)
   - Calculate path: `{repo-parent}/{repo-name}-{issue-number}`
   - Example: Main repo at `C:\src\squad`, issue #42 → `C:\src\squad-42`

b. **Check if worktree already exists:**
   - Run `git worktree list` to see all active worktrees
   - If the worktree path already exists → **check before reusing**:
     - Verify the branch is correct (should be `squad/{issue-number}-*`)
     - Verify `git status --short` is empty and no other active agent owns this path
     - If clean and unowned: `git pull --ff-only origin {branch}` (fail if diverged — do not force)
     - Skip to step (e)

c. **Create the worktree:**
   - Determine branch name: `squad/{issue-number}-{kebab-case-slug}` (derive slug from issue title if available)
   - Determine base branch (typically `main`, check default branch if needed)
   - Run: `git worktree add {path} -b {branch} {baseBranch}`
   - Example: `git worktree add C:\src\squad-42 -b squad/42-fix-login main`

d. **Set up dependencies (Python/uv):**
   - This is a Python application — do not link or install `node_modules`.
   - Only restore uv dependencies if the chosen command fails with a missing-dependency error or if `pyproject.toml`/`uv.lock` changed since last install.
   - The existing uv runner handles restoration automatically; do not install proactively.

e. **Include worktree context in spawn:**
   - Set `WORKTREE_PATH` to the resolved worktree path
   - Set `WORKTREE_MODE` to `true`
   - Add worktree instructions to the spawn prompt (see template below)

**3. If worktrees disabled:**
- Set `WORKTREE_PATH` to `"n/a"`
- Set `WORKTREE_MODE` to `false`
- Use existing `git checkout -b` flow (no changes to current behavior)
