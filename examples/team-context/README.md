# Example team context repository

Point `team_context_repo_url` at a repository laid out like this directory
(set `team_context_subdir` when it is not the repository root). Every sandbox
mirrors it and installs `steering/*.md` as global Kiro steering, so the whole
team works from the same rules. See `docs/design-team-context.md`.

```
steering/   team-wide Kiro steering files (*.md), installed as ~/.kiro/steering/team--<name>.md
knowledge/  shared documents; add ~/.kiro/team-context/repo/<subdir>/knowledge once as a
            KiroCrew knowledge folder source and it stays indexed as the mirror updates
```

Change these files with a pull request. The copy inside a sandbox is a read-only
mirror: local edits are discarded on the next sync.
