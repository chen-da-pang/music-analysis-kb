---
name: music-kb-weekly-publisher
description: Publish a completed Music Flamingo delivery into the local writable Music KB, create a verified immutable snapshot, and fan it out to configured colleague Macs over SSH. Use only on the publisher machine.
---

# Music KB Weekly Publisher

This is the publisher-side workflow. It is not a colleague-side retrieval
skill and it must never be installed as a write-capable workflow on client
machines.

## Non-negotiable boundaries

- The writable database is only `~/.music-kb/music-master.sqlite`.
- Never sync the writable master database.
- Client machines receive a verified release and atomically switch
  `~/.music-kb/current.sqlite`.
- The GitHub repository contains code and fixtures, not production databases,
  raw analyses, audio, SSH keys, or peer inventory.
- The database contains Music Flamingo output and retrieval metadata only.
  Do not import Feigua tags or generate Suno prompts here.
- A recording exposes one public canonical analysis. Replaced revisions stay
  on the publisher for audit and do not enter client snapshots.

## Inputs

Required input is a completed, quality-gated canonical delivery manifest from
CNB. Do not read an in-progress runner log as the database input.

Default local configuration:

```text
~/.config/music-kb/peers.toml
```

The peer inventory is private and contains connection facts only. The skill
must load it before planning a fan-out and must not silently invent a host,
user, key, or target path.

## Canonical workflow

Run the CLI workflow from the installed plugin project:

```bash
music-kb weekly-update \
  --db "$HOME/.music-kb/music-master.sqlite" \
  --input /secure/path/canonical-delivery.jsonl \
  --input-kind campaign \
  --expected-count 232 \
  --output-dir "$HOME/.music-kb/releases" \
  --release-name music-kb-2026w30 \
  --peers-file "$HOME/.config/music-kb/peers.toml" \
  --state-file "$HOME/.music-kb/state/publish-state.json"
```

The command prepares the master and release, then performs SSH dry-run only.
After the output has been reviewed, use the exact same inputs with
`--publish`:

```bash
music-kb weekly-update \
  --db "$HOME/.music-kb/music-master.sqlite" \
  --input /secure/path/canonical-delivery.jsonl \
  --input-kind campaign \
  --expected-count 232 \
  --output-dir "$HOME/.music-kb/releases" \
  --release-name music-kb-2026w30 \
  --peers-file "$HOME/.config/music-kb/peers.toml" \
  --state-file "$HOME/.music-kb/state/publish-state.json" \
  --publish
```

Never reuse a release name. A correction creates a new release such as
`music-kb-2026w30-r2`.

## Stage-by-stage review gates

The skill must stop at the first failed gate and report the stage, artifact,
and bounded error. It must not claim a weekly update succeeded merely because
the import command returned zero.

1. **Delivery preflight** — physical-LF JSONL, expected count, unique IDs,
   audio/output hashes, declared model contract, and no malformed records.
2. **Quality audit** — reject empty output, repeated-tail degeneration,
   unexpected truncation, hash mismatch, or identity conflict. Lyrics are not
   rejected by this workflow.
3. **Canonical reconcile** — import idempotently and promote only a passed
   analysis. Confirm one canonical pointer per recording.
4. **Retrieval tag enrichment** — derive title, artist, musical, production,
   structural, mood, and lyric/theme retrieval tags. Do not filter them for
   Suno.
5. **Master validation** — `music-kb validate` must report `valid=true` and a
   current search projection.
6. **Release verification** — snapshot manifest, SHA-256, SQLite integrity,
   canonical-only counts, and release name must agree.
7. **Peer dry-run review** — list every enabled peer, target directory, and
   intended release. Disabled peers are not included unless explicitly named
   for a retry.
8. **Fan-out verification** — each peer is staged, verified remotely, and
   atomically installed. One offline peer does not cancel other peers.
9. **State review** — `publish-state.json` records release SHA, per-peer
   attempt status, and last successful release without raw command output.

## Failure and retry rules

- Do not overwrite an existing release.
- Do not use `rsync --inplace` for SQLite.
- Do not change `current.sqlite` before remote verification succeeds.
- A failed peer is retried by name with `--peer <name>` after reachability is
  restored.
- A failed import or validation blocks snapshot creation and fan-out.
- A dry-run must not create or modify `publish-state.json`.

## Client-side boundary

Colleagues install the retrieval plugin themselves. The publisher skill only
pushes database releases over SSH; it does not install or modify Codex
plugins on colleague machines. Client MCP tools remain read-only and query
the local `current.sqlite`.
