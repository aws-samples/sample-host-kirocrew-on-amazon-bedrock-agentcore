# Protocol compatibility policy

`kirocrew-agentcore.v1` is the browser, headless-client, control-plane, and runtime compatibility boundary.

## Compatible changes

- Add an optional object property whose absence preserves existing behavior.
- Add a server event that old clients may ignore after validating the common envelope.
- Add an error code while preserving the existing category, retryability, and safe-message fields.
- Increase a documented size or timeout limit without changing message meaning.

## Incompatible changes

- Remove or rename a field, operation, event, or error code.
- Change a required field's type, meaning, ordering, authentication, or idempotency behavior.
- Make an optional field required or narrow an accepted enum/range.
- Change the Cognito bearer or sandbox-binding authority model.

An incompatible change requires a new protocol identifier and parallel adapter support during migration. Generated models must be regenerated from the canonical schemas, and `make contract` fails when generated output or compatibility fixtures are stale.
