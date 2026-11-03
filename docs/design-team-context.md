# Team context: shared steering and knowledge across sandboxes

Status: phase 1 implemented (opt-in, `team_context_repo_url`). Phases 2-3 below are not.

## Problem

Each sandbox is one person's KiroCrew. Steering, Lessons, memory and the
knowledge library all live in that person's workspace, so a team that wants
"everyone's agent follows the same rules and can look up the same runbooks"
has to copy files into every sandbox by hand, and the copies drift.

## What Kiro already offers, and what is missing here

| Need | Kiro / KiroCrew today | Gap in this deployment |
|---|---|---|
| Rules for one repository | Workspace steering in `.kiro/steering/` and `AGENTS.md`, committed with the code | None. A cloned repository brings its own steering. |
| Rules for a whole team | [Global steering](https://kiro.dev/docs/steering/) in `~/.kiro/steering/`; Kiro documents "team steering" as files from a central repository placed there by MDM or a login script | A sandbox has no MDM or login script. Nobody puts the files there. |
| Shared reference documents | KiroCrew knowledge library, including folder sources that re-index on change | Each sandbox has its own library; nothing shared to point it at. |
| Shared learned memory | Kiro Memory is per user | Out of scope: learned memory is not something to merge through Git. |

So the missing piece is distribution, not a new context system.

## Decision: Git as the source of truth, mirrored into every sandbox

The team keeps one Git repository:

```
steering/    *.md, team-wide steering
knowledge/   shared documents
```

Each sandbox keeps a read-only mirror of it and installs `steering/*.md` as
global steering. Git supplies what a shared context needs and a bucket or a
database would have to rebuild: review (pull requests), history and blame,
rollback, and access control the team already manages.

Git alone is not enough, which is why the runtime does the distribution:

- It has to arrive without anyone running `git pull`: the runtime syncs before
  the gateway starts and every 30 minutes (`TEAM_CONTEXT_REFRESH_SECONDS`).
- An unreachable repository must not stop a sandbox: the mirror and installed
  files live under `~/.kiro`, a persisted path, so the last good copy stays.
- It must not clobber a user's own steering: installed files are named
  `team--<name>.md`, ownership is tracked in a manifest, and a file the sync did
  not install is never overwritten or deleted.
- Learned, per-person state (Lessons, memory) does not belong in Git: it is
  written constantly by the agent and would conflict on every merge.

### Alternatives considered

| Option | Why not (for now) |
|---|---|
| Shared S3 prefix mounted or copied into every sandbox | No review, no history; anyone with write access changes every agent silently. |
| A shared KiroCrew instance per team | Breaks the one-person-one-sandbox isolation every other part of this design relies on. |
| Writing into each sandbox's knowledge database from outside | Couples this repository to KiroCrew's private storage schema. |

## Phase 1 (implemented)

- Terraform: `team_context_repo_url` (https only), `team_context_ref`,
  `team_context_subdir`, passed to both Runtimes. Empty URL disables everything.
- Runtime `team_context.py`: shallow clone or fetch into
  `~/.kiro/team-context/repo`, hard reset to the fetched revision (local edits
  are discarded: it is a mirror), install `steering/*.md` as
  `~/.kiro/steering/team--<name>.md` with mode 600, remove files it installed
  that upstream deleted.
- Safety: only `https://` remotes, Git's other transports refused
  (`protocol.allow=never`), hooks disabled, LFS smudge off, symlinks and files
  over 256 KB skipped, at most 200 files, no Git stderr in logs (it can carry
  the remote URL).
- Knowledge: the mirror's `knowledge/` directory is ready to be added once as a
  KiroCrew folder source; the source definition is part of the persisted
  workspace and the folder watcher re-indexes when the mirror changes.

Limits of phase 1:

- Public repositories only. Private repositories need a credential in the
  sandbox (phase 2).
- Steering changes reach new sessions after the next refresh, not instantly.
- The knowledge source is added by hand once per sandbox.

## Phase 2 (planned)

- Private repositories: a read-only deploy token in Secrets Manager, readable by
  the Runtime role for that one secret, handed to Git through `GIT_ASKPASS` so it
  is never written to disk or a Git config. Adds one IAM grant.
- Register the knowledge folder source automatically on first start.
- Per-group content: `steering/<group>/` installed only for members of that
  Cognito group, reusing the groups from tool authorization.

## Phase 3 (watch, do not build yet)

If Kiro or KiroCrew ship first-party team context or team memory, map this
repository onto that instead of extending phase 2.
