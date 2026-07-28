# Minimal ECC surface (this branch only)

> **Never merge this tree to `main`.** See `/DO-NOT-MERGE-TO-MAIN.md`.

This is a **thin** ECC-style developer overlay for working on music-kb on
`feat/grok-plugin-migration`. It is **not** a full ECC install and does **not**
copy the global `~/.claude` skill inventory into the repo.

## Intent

- Use ECC *habits* (plan → small change → verify → review) while editing this
  plugin.
- Keep the product plugin identity (`plugins/music-kb`) separate from the
  global ECC OS.

## What is in-scope here

| Item | Notes |
| --- | --- |
| Workflow checklist | [WORKFLOW.md](WORKFLOW.md) |
| Standing orders for agents | [AGENTS-OVERLAY.md](AGENTS-OVERLAY.md) |
| Optional personal links | Point at `~/.claude/skills/…` instead of vendoring 180+ skills |

## What is out-of-scope

- Vendoring the full ECC agents/skills/commands/hooks tree into this repo
- Making colleagues install ECC to use music-kb
- Replacing Grok/Codex plugin packaging with ECC

## Suggested session start

```bash
cd /path/to/music-analysis-kb-production-main   # this worktree
git checkout feat/grok-plugin-migration
# optional: export ECC skills from user install, do not copy into git
test -f DO-NOT-MERGE-TO-MAIN.md && echo "experiment branch OK"
```

## Promote to main

Never merge this branch. Cherry-pick product-safe commits only onto a branch
from `main` (see [docs/dev/ECC-BRANCH-POLICY.md](../../docs/dev/ECC-BRANCH-POLICY.md)).
