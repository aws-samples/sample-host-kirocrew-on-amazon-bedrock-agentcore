# Sandbox persistence contract

`/mnt/workspace` is the mutable per-session filesystem. AgentCore managed session storage accelerates stop/resume, while committed encrypted checkpoints are the long-term durability authority.

## Included roots

- `home/.kiro`: Kiro CLI and KiroCrew configuration, agents, memory, knowledge, artifacts, skills, conversations, and scheduled jobs.
- `home/.config`: user-scoped tool configuration except explicitly excluded credential directories.
- `home/.local/share/kiro-cli`: the Kiro CLI state database, including the Builder ID / SSO sign-in it stores in `data.sqlite3`. Without this root every restore comes back signed out.
- `projects`: complete user project workspaces and repository metadata.
- `user`: other user-created durable files.

## Exclusions

The manifest rejects paths outside the included roots and excludes `.agentcore`, `.aws`, `.ssh`, caches, dependency caches, process IDs, sockets, temporary logs, run markers, environment files, FIFOs, device files, and other unsupported special files. Symlinks are recorded without following their targets.

## Checkpoint consistency

The dirty journal is started before KiroCrew is ready. A checkpoint pauses the KiroCrew/Kiro process groups, flushes the filesystem, builds a deterministic metadata-preserving manifest, encrypts and uploads previously unseen content-addressed chunks, encrypts the manifest, and conditionally commits its generation. The local generation pointer and captured journal entries are updated only after the durable commit exists. Mutations arriving after the journal snapshot remain dirty for the next generation.

The default recovery-point objective is 30 seconds from the first uncommitted durable mutation. If the durable store cannot commit before that boundary, the sandbox becomes read-only rather than acknowledging unprotected writes.

## Encryption

Each sandbox receives a 256-bit data key outside the manifest. AES-256-GCM uses sandbox ID and plaintext digest as authenticated context. A keyed deterministic nonce permits deduplication only within that sandbox; another sandbox with the same plaintext produces unrelated ciphertext because it uses a different key and context.

## Brokered long-term durability and restore

S3 checkpoints are the authoritative state; AgentCore managed session storage is only a local acceleration layer. The persistence broker derives `snapshots/<sandboxId>/...` keys itself, issues operation-specific URLs for at most 15 minutes, and requires KMS encryption-context headers containing the validated sandbox ID. Runtime callers cannot submit a bucket prefix or list objects.

Before KiroCrew starts, an existing sandbox restores from S3 whenever the mount is empty, its pointer differs from the latest commit, local integrity sampling fails, managed storage expired, or the runtime version changed. Restore decrypts and validates a manifest, reconstructs an isolated staging tree, enforces allowlisted paths/file-count/size/type limits, verifies every chunk and whole-file digest, then atomically swaps the tree into place. A corrupt latest generation falls back once to the preceding commit. A new logical sandbox may initialize empty; an existing sandbox with no valid retained generation returns `PERSISTENCE_RESTORE_FAILED`.

The latest two committed generations are retained. Reference-aware garbage collection first marks old generations and unreferenced chunks, waits through a grace period, recomputes reachability, and only then sweeps objects that remain unreachable. A scheduled offline auditor decrypts both retained manifests, checks every referenced chunk exists without launching compute, records an audit result, and increments a failure metric.
