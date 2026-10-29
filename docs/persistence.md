# Sandbox persistence contract

`/mnt/workspace` is the mutable per-session filesystem on the microVM's container disk. It is ephemeral by design: committed encrypted checkpoints in S3 are the sole durability authority. (AgentCore managed session storage was dropped: its 1GB quota and 14-day retention caused repeated incidents, and the checkpoint/restore engine already provides the durability it promised.)

## Included roots

- `home/.kiro`: Kiro CLI and KiroCrew configuration, agents, memory, knowledge, artifacts, skills, conversations, and scheduled jobs.
- `home/.config`: user-scoped tool configuration except explicitly excluded credential directories.
- `home/.local/share/kiro-cli`: the Kiro CLI state database, including the Builder ID / SSO sign-in it stores in `data.sqlite3`. Without this root every restore comes back signed out.
- `artifacts`, `knowledge`, `memory`: the top-level directories the runtime creates next to the projects root; anything a user or feature drops there survives a restore.
- `projects`: complete user project workspaces and repository metadata.
- `user`: other user-created durable files.

## Exclusions

The manifest rejects paths outside the included roots and excludes `.agentcore`, `.aws`, `.ssh`, caches, dependency caches, process IDs, sockets, temporary logs, run markers, environment files, FIFOs, device files, and other unsupported special files. Symlinks are recorded without following their targets. Git repositories under the roots are captured whole, object store included, so unpushed commits survive a restore.

## Why checkpoints are not Git commits

A checkpoint "commit" is not a Git commit, and Git is not used to store workspaces, for four reasons:

- **The content is not source code.** The durable roots hold the Kiro sign-in (`data.sqlite3`), the KiroCrew session and memory databases, and binary artifacts. SQLite files must be captured at a consistent instant with the writers paused, which a checkpoint does and `git add` does not.
- **Everything leaves the microVM encrypted.** Chunks and manifests are encrypted in the sandbox with that sandbox's data key before upload. A Git remote would receive plaintext, or need an encrypting layer that is the checkpoint engine again.
- **Two containers must not both win.** A commit is conditional on the generation the container restored from, enforced by the broker (write-once S3 objects plus a base-generation check). That is what stops a stale container from overwriting a newer one; a Git push has no equivalent the broker can enforce.
- **The microVM holds no storage credentials.** Every read and write goes through short-lived presigned URLs from the broker. A Git remote would put a long-lived credential in the sandbox.

Repositories the user works on are ordinary Git repositories inside `projects/`, pushed to their own remotes; the checkpoint simply carries the working copy.

## Older generations

Every committed generation keeps its manifest, so an earlier state exists in S3. There is no supported way to restore one: restore always takes the latest valid generation and falls back to the previous one only when the latest is corrupt.

## Checkpoint consistency

The dirty journal is started before KiroCrew is ready. A checkpoint pauses the KiroCrew/Kiro process groups (SIGSTOP), flushes the filesystem, builds a deterministic metadata-preserving manifest with every chunk read into memory, and then — for non-final checkpoints — resumes the process groups before it encrypts and uploads previously unseen content-addressed chunks, encrypts the manifest, and conditionally commits its generation. The pause covers only the in-memory snapshot: the upstream gateway hard-exits when its event loop is silent longer than its loop-stall budget (`dashboard.loop_stall_exit_after_secs`, which the supervisor pins to the upstream maximum of 300 through `config.local.json`), and a freeze spanning every S3 round trip crossed the former 25s default on real workspaces. A final checkpoint keeps the processes frozen through the commit because the sandbox is stopping. The local generation pointer and captured journal entries are updated only after the durable commit exists. Mutations arriving after the journal snapshot remain dirty for the next generation.

"Previously unseen" is decided against a chunk index the runtime loads once per container: the first existence question lists the sandbox's whole `chunks/` prefix through the broker, and every later question is answered from that set (plus the chunks this container uploaded). Asking the broker once per chunk instead cost one Lambda round trip per chunk — 14-17 minutes for a 5,000-chunk workspace — long enough that a periodic checkpoint could not commit before idle reclaim and a final checkpoint froze the gateway for the whole walk. A failed listing falls back to the per-chunk question rather than failing the checkpoint. The index is only sound because of this invariant: **a chunk is never deleted from the prefix of a sandbox that may be running.** The broker exposes no delete operation today; any future garbage collector must delete only chunks unreferenced by every retained generation and must not run while a container holds the sandbox's lease. The offline integrity auditor does not use the index, because noticing a vanished chunk is its job.

Beyond the final checkpoint a Stop safely triggers, the runtime commits non-final durability checkpoints in the background: after a successful Kiro sign-in or sign-out, on a periodic interval (`KIROCREW_CHECKPOINT_INTERVAL_SECONDS`, default 300, `0` disables) whenever the durable workspace fingerprint changed, when background work (task runner, subagents, workflows) transitions from busy to idle — the moment the sandbox becomes eligible for idle reclaim again — and as a best-effort final checkpoint on graceful shutdown (SIGTERM). While such background work is running the `/ping` answer is `HealthyBusy` (probed on the loopback gateway with a `KIROCREW_BUSY_PROBE_TTL_SECONDS` cache, default 10, `0` disables; a stuck-busy task is cut off after `KIROCREW_BUSY_MAX_SECONDS`, default 14400), so AgentCore does not reclaim a sandbox that is still working for a disconnected user. Idle intervals skip the commit entirely, so a quiet sandbox never pauses its gateway. Non-final commits do not advance the sandbox state machine toward STOPPING, and a failure only logs a warning while the session keeps working. Because a background commit can reach S3 and die before its receipt lands in DynamoDB, restore tolerates a durable store that is ahead of the recorded generation pointer; only a store behind the pointer fails the restore.

The default recovery-point objective is 30 seconds from the first uncommitted durable mutation. If the durable store cannot commit before that boundary, the sandbox becomes read-only rather than acknowledging unprotected writes.

## Encryption

Each sandbox receives a 256-bit data key outside the manifest. AES-256-GCM uses sandbox ID and plaintext digest as authenticated context. A keyed deterministic nonce permits deduplication only within that sandbox; another sandbox with the same plaintext produces unrelated ciphertext because it uses a different key and context.

## Brokered long-term durability and restore

S3 checkpoints are the authoritative state; the workspace disk is ephemeral scratch space. The persistence broker derives `snapshots/<sandboxId>/...` keys itself, issues operation-specific URLs for at most 5 minutes, and requires KMS encryption-context headers containing the validated sandbox ID. Runtime callers cannot submit a bucket prefix or list objects.

The broker is also the runtime's only path to its sandbox record. The microVM's execution role is shared by every process in the sandbox, including the user's terminal, so it holds no DynamoDB, S3, or data-key KMS grants. Every broker call carries a token under `bindingToken`: either the control plane's browser-scoped binding token (`type: binding`, 30 minutes) or a runtime-session token (`type: runtime-session`) the broker mints when a container wins `acquireInit`, carrying the same `sandboxId`, `runtimeSessionId`, and `subjectHash` claims and expiring no later than the platform session lifetime (`runtime_max_lifetime_seconds`). The broker verifies the signature, matches the claims against the request, and, for every operation except `readRecord`, confirms the record still names that session and is in a live state (or holds an unexpired initialization lease). Record operations are narrow and server-defined; the caller supplies only validated values, never expressions:

| Operation | Effect on the caller's own record |
|---|---|
| `readRecord` | Returns `runtimeSessionId`, `state`, and `lastCheckpointGeneration` so a starting container can detect a rotated session and walk away. |
| `lease` | Pure authorization; the adapter calls it per invocation and caches approvals for 15 seconds per binding token. |
| `acquireInit` | Claims initialization ownership (`initOwner`, 90-second lease) and returns `runtimeSessionToken` when the claim applied. Claimable from `STARTING`, `RESTORING`, or `READY` **with a dead start lease** — the last case is how a sandbox reclaimed by idle scale-down is recovered instead of staying `READY` forever. A refusal carries a `reason` naming the clause that rejected it. |
| `heartbeatInit` / `heartbeatLease` | Extend the initialization lease or the start lease; a rejected condition means a newer container or start owns it. |
| `healReady` / `markReady` / `markError` | Publish the lifecycle transitions the container is entitled to, each conditioned on the session and, where relevant, the initialization owner. |

A rejected condition is a normal result (`applied: false`); the runtime maps it to its lifecycle decisions. A refused token is a broker error, which the lease heartbeat treats as supersession.

Before KiroCrew starts, an existing sandbox restores from S3 whenever the mount is empty, its pointer differs from the latest commit, local integrity sampling fails, managed storage expired, or the runtime version changed. Restore decrypts and validates a manifest, reconstructs an isolated staging tree, enforces allowlisted paths/file-count/size/type limits, verifies every chunk and whole-file digest, then atomically swaps the tree into place. A corrupt latest generation falls back once to the preceding commit. A new logical sandbox may initialize empty; an existing sandbox with no valid retained generation returns `PERSISTENCE_RESTORE_FAILED`.

**Retention is not enforced today: every committed generation and every chunk is kept.** `RetentionManager` in `durability.py` implements reference-aware garbage collection (mark old generations and unreferenced chunks, wait through a grace period, recompute reachability, then sweep), but nothing in the runtime or the broker calls it, and the broker exposes no delete operation. Storage therefore grows with every generation; a long-lived sandbox measured 618 manifests (222 MB) and 8,285 chunks (1.8 GB). Wiring retention must respect the chunk-index invariant above: never delete from the prefix of a sandbox that may be running. The scheduled audit rule reaches the broker, whose audit operation is currently a placeholder.
