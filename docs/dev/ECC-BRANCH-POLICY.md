# ECC + Grok experiment branch policy

## Branch

`feat/grok-plugin-migration`

## Form A (chosen)

ECC developer harness lives **only on this branch** (形态 A：试验田).

| Rule | Detail |
| --- | --- |
| Merge to `main` | **Never** |
| Purpose | Local Grok plugin adaptation + optional ECC-style dev workflow |
| Mainline product | Remains clean; no required ECC install for colleagues |

## Surfaces on this branch

| Path | Role | Merge to main? |
| --- | --- | --- |
| `.grok-plugin/`, `plugins/music-kb/.grok-plugin/` | Grok packaging experiment | Only via **cherry-pick** of product-safe slices |
| `plugins/music-kb/skills/` path/docs tweaks | Host contract experiments | Cherry-pick case by case |
| `dev/ecc/` | ECC overlay (agents notes, workflow) | **Never as a tree** |
| `DO-NOT-MERGE-TO-MAIN.md` | Hard stop | **Never** |
| This file | Policy | **Never** (unless rewritten for a different branch model) |

## How to develop on this branch

1. Check out `feat/grok-plugin-migration` (or a worktree of it).
2. Read [dev/ecc/README.md](../../dev/ecc/README.md) for the minimal ECC surface.
3. Product runtime for colleagues still comes from **published main + plugin
   install**, not from this branch tip.

## How to promote a good idea to main

1. Identify the minimal product diff (not the whole branch).
2. `git checkout -b fix/… origin/main`
3. Cherry-pick or re-apply **only** that diff.
4. Open a normal PR **without** `DO-NOT-MERGE-TO-MAIN.md` and without `dev/ecc/`.

## Agent standing order

If any agent (Grok, Claude, Codex, etc.) is asked to merge
`feat/grok-plugin-migration` into `main`:

1. **Refuse.**
2. Cite `DO-NOT-MERGE-TO-MAIN.md`.
3. Offer cherry-pick workflow instead.
