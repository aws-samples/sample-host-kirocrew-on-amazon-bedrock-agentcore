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

The manifest rejects paths outside the included roots and excludes `.agentcore`, `.aws`, `.ssh`, caches, dependency caches, process IDs, sockets, temporary logs, run markers, environment files, FIFOs, device files, and other unsupported special files. Symlinks are recorded without following their targets.

## Checkpoint consistency

The dirty journal is started before KiroCrew is ready. A checkpoint pauses the KiroCrew/Kiro process groups (SIGSTOP), flushes the filesystem, builds a deterministic metadata-preserving manifest with every chunk read into memory, and then — for non-final checkpoints — resumes the process groups before it encrypts and uploads previously unseen content-addressed chunks, encrypts the manifest, and conditionally commits its generation. The pause covers only the in-memory snapshot: the upstream gateway hard-exits when its event loop is silent longer than its loop-stall budget (`dashboard.loop_stall_exit_after_secs`, which the supervisor pins to the upstream maximum of 300 through `config.local.json`), and a freeze spanning every S3 round trip crossed the former 25s default on real workspaces. A final checkpoint keeps the processes frozen through the commit because the sandbox is stopping. The local generation pointer and captured journal entries are updated only after the durable commit exists. Mutations arriving after the journal snapshot remain dirty for the next generation.

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
| `acquireInit` | Claims initialization ownership (`initOwner`, 90-second lease) and returns `runtimeSessionToken` when the claim applied. |
| `heartbeatInit` / `heartbeatLease` | Extend the initialization lease or the start lease; a rejected condition means a newer container or start owns it. |
| `healReady` / `markReady` / `markError` | Publish the lifecycle transitions the container is entitled to, each conditioned on the session and, where relevant, the initialization owner. |

A rejected condition is a normal result (`applied: false`); the runtime maps it to its lifecycle decisions. A refused token is a broker error, which the lease heartbeat treats as supersession.

Before KiroCrew starts, an existing sandbox restores from S3 whenever the mount is empty, its pointer differs from the latest commit, local integrity sampling fails, managed storage expired, or the runtime version changed. Restore decrypts and validates a manifest, reconstructs an isolated staging tree, enforces allowlisted paths/file-count/size/type limits, verifies every chunk and whole-file digest, then atomically swaps the tree into place. A corrupt latest generation falls back once to the preceding commit. A new logical sandbox may initialize empty; an existing sandbox with no valid retained generation returns `PERSISTENCE_RESTORE_FAILED`.

The latest two committed generations are retained. Reference-aware garbage collection first marks old generations and unreferenced chunks, waits through a grace period, recomputes reachability, and only then sweeps objects that remain unreachable. A scheduled offline auditor decrypts both retained manifests, checks every referenced chunk exists without launching compute, records an audit result, and increments a failure metric.
