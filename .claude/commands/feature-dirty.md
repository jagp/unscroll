---
description: Stash dirty work, branch to feature/<name>, then restore the changes
argument-hint: <feature name>
allowed-tools: Bash(git:*)
---

Move all uncommitted work in the current working tree onto a fresh `feature/`
branch named after the user's input: **$ARGUMENTS**

Run the script below exactly as written, then report the resulting branch name
and whether any changes were carried over. If any command fails, stop and show
the error rather than continuing.

```bash
# Sanitize input into a git-safe slug: lowercase, non-alphanumerics -> single
# hyphen, trim leading/trailing hyphens.
name=$(printf '%s' "$ARGUMENTS" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//')
[ -z "$name" ] && { echo "Provide a feature name, e.g. /feature-dirty add login form"; exit 1; }
branch="feature/$name"

# Fail early if the branch already exists, so we never orphan a stash.
git show-ref --verify --quiet "refs/heads/$branch" && { echo "Branch $branch already exists"; exit 1; }

# 1) Stash all uncommitted work (including untracked files).
before=$(git rev-parse -q --verify refs/stash || true)
git stash push -u -m "feature-dirty: $name"
after=$(git rev-parse -q --verify refs/stash || true)
[ "$before" != "$after" ] && stashed=1 || stashed=0

# 2 & 3) Create and check out the new feature branch.
git checkout -b "$branch"

# 4 & 5) Re-apply the stashed work and delete the stash. `git stash pop` does
# both, and only drops the stash if the apply succeeds cleanly.
if [ "$stashed" -eq 1 ]; then
  git stash pop
else
  echo "No uncommitted changes were present to carry over."
fi

echo "Now on $branch"
```
