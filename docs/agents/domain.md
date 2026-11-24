# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

## Before exploring, read these

- **`GLOSSARY.md`** at the repo root, or
- **`GLOSSARY-MAP.md`** at the repo root if it exists: it points at one `GLOSSARY.md` per context. Read each one relevant to the topic.
- **`docs/adr/`**: read ADRs that touch the area you're about to work in. In multi-context repos, also check `src/<context>/docs/adr/` for context-scoped decisions.

If any of these files don't exist, **proceed silently**. Don't flag their absence; don't suggest creating them upfront. The `/domain-modeling` skill (reached via `/grill-with-docs` and `/improve-codebase-architecture`) creates them lazily when terms or decisions actually get resolved.

## File structure

Single-context repo (most repos):

```
/
├── GLOSSARY.md
├── docs/adr/
│   ├── 0001-event-sourced-orders.md
│   └── 0002-postgres-for-write-model.md
└── src/
```

Multi-context repo (presence of `GLOSSARY-MAP.md` at the root):

```
/
├── GLOSSARY-MAP.md
├── docs/adr/                          ← system-wide decisions
└── src/
    ├── ordering/
    │   ├── GLOSSARY.md
    │   └── docs/adr/                  ← context-specific decisions
    └── billing/
        ├── GLOSSARY.md
        └── docs/adr/
```

## Use the glossary's vocabulary

When your output names a domain concept (in an issue title, a refactor proposal, a hypothesis, a test name), use the term as defined in `GLOSSARY.md`. Don't drift to synonyms the glossary explicitly avoids.

If the concept you need isn't in the glossary yet, that's a signal: either you're inventing language the project doesn't use (reconsider) or there's a real gap (note it for `/domain-modeling`).

## Flag ADR conflicts

If your output contradicts an existing ADR, surface it explicitly rather than silently overriding:

> _Contradicts ADR-0007 (event-sourced orders), but worth reopening because…_

## sonde: where the design lives today

sonde has no `GLOSSARY.md` or `docs/adr/` yet. Until it does, read these instead (see the table in `CLAUDE.md`):

- **`PLAN.md`**: the design, and the rules the code must obey. Treat it as the ADRs: if your output contradicts it, say so explicitly, as for an ADR above.
- **`HISTORY.md`**: why things were built as they were, and where each threshold's justification lives.
- **`SCORING.md`**: the influence score in full.
- **`ACCESS.md`**: who can see what.
- **`GLOSSARY.md`**: the domain language, settled 2026-11-24.

## sonde: code names lag the glossary

The code predates the glossary and still says `group`, `ignored`, `organisation`, `lost` and `known_followers`. The rule:

- **Python names and page text** follow the glossary whenever a change touches them anyway. One deliberate cleanup pass goes with BUG-06 and BUG-10.
- **Database columns and URLs keep their old names.** Renaming them costs a migration or redirects and buys only tidiness. The old words appear in the glossary's _Avoid_ lists, so a reader can map them.
- New code uses the glossary's terms from the start.
