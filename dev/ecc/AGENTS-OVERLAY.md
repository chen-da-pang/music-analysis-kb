# Agent overlay (experiment branch)

## Hard rules

1. **Branch `feat/grok-plugin-migration` must never be merged into `main`.**
2. Read `/DO-NOT-MERGE-TO-MAIN.md` before any git push of a merge.
3. Prefer **direct** download executors; do not recommend `--executor claude`
   for Grok sessions.
4. Keep product data out of git (`data/`, audio, sqlite, peers.toml secrets).

## Roots

| Variable | Meaning |
| --- | --- |
| `MUSIC_KB_PLUGIN` | `…/plugins/music-kb` (checkout or install) |
| `MUSIC_WORKSPACE` | Publisher data workspace (charts/audio/receipts) |
| `MUSIC_KB_REPO` | Git monorepo root (CNB only; real `.git`) |

## Identity

- This repo ships a **music knowledge-base plugin**, not a generic ECC app.
- ECC here is a **dev overlay** under `dev/ecc/` only.
