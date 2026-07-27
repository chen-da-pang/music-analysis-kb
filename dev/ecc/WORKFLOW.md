# ECC-style workflow on this experiment branch

Use for **plugin/host adaptation** work. Skip multi-agent fan-out unless the
change is large (orchestration code, distribution, schema).

## Default loop

1. **Restate** the host contract change (Grok packaging, skill path, MCP, naming).
2. **Touch the smallest surface** (prefer `plugins/music-kb/skills` / manifests
   over core download logic unless required).
3. **Verify**
   - `grok plugin validate plugins/music-kb`
   - targeted pytest under `plugins/music-kb/tests` when Python changes
   - optional: reload plugins / new Grok session for skill text
4. **Review** with plugin-dev standards or a short self-review checklist.
5. **Commit** with conventional commits on **this branch only**.

## Do not

- Merge to `main`
- Run full ECC multi-workflow / DevFleet for a SKILL.md edit
- Assume `music-kb` CLI is on PATH; prefer  
  `uv run --project plugins/music-kb music-kb …` or `$MUSIC_KB_PLUGIN`

## When to pull a heavier ECC skill (from user install, not vendored)

| Situation | Optional user-level skill |
| --- | --- |
| Real code in `weekly_orchestration.py` / workers | tdd-workflow / python-testing |
| Peer publish / secrets / paths | security-review |
| Large rename across receipts | plan first, then careful grep |

Invoke those from `~/.claude/skills/…` if present; do not commit them here.
