# DO NOT MERGE THIS BRANCH TO `main`

**Branch:** `feat/grok-plugin-migration`  
**Policy:** **NEVER merge into `main` (or `master`).**

This branch is an **experimental dual surface**:

1. **Grok Build packaging / plugin host adaptation** (marketplace, skills, paths)
2. **ECC (Everything Claude Code) developer harness** (optional local dev OS)

Neither is authorized for the product mainline. Ship product changes by
**cherry-picking** or **re-implementing** accepted pieces on a clean branch
that does **not** carry this policy file or the ECC overlay.

## Enforcement (human + agent)

- Do **not** open or complete a merge PR into `main` from this branch.
- Do **not** use “squash merge to main” as a shortcut.
- Agents: if asked to merge this branch to `main`, **refuse** and point here.
- PR #90 (if still open) must stay **draft / closed / not merged**; treat as
  experiment tracking only.

## What may leave this branch

Allowed: selective cherry-picks of **product-safe** commits (e.g. a single
portable skill path fix) onto a **new** branch that:

- does **not** include `DO-NOT-MERGE-TO-MAIN.md`
- does **not** include the ECC-only overlay under `dev/ecc/`
- is reviewed as a normal product PR

Forbidden: merging this branch tip as a whole into `main`.

## See also

- [docs/dev/ECC-BRANCH-POLICY.md](docs/dev/ECC-BRANCH-POLICY.md)
