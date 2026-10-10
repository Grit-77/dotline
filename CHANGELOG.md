# Changelog

## Unreleased

- Keep the channel's delivery loop alive after transient mailbox read or claim
  errors, preserving unread messages and the current notification batch.
- Validate stored claim owners and timestamps before evaluating expiry, so
  malformed claims cannot be silently replaced or escape as unexpected errors.
- Publish complete claim files atomically; failed writes leave existing claims
  intact and can be retried without a corrupt partial claim blocking delivery.
- Stop `wait` polling at its deadline and cap request timeouts to the remaining
  polling budget in Python, PowerShell and Bash clients.
- Keep intact mailbox records readable after a write stops inside a UTF-8
  character, and allow later appends and idempotent retries to recover.
- Preserve a watcher's unfinished startup line so a message completed after
  startup is delivered once without replaying earlier complete messages.
- Preserve the recovery `client_id` when a send receives an HTTP error or an
  unusable response. The Python client also handles truncated HTTP responses
  as network failures and retries once with the same ID.
- Decode channel input as UTF-8 on Windows so non-ASCII replies are preserved
  even when the system locale uses a different encoding.
- README FAQ: why a dot that drives a coding agent can read replies but never send
  (the agent's safety review refuses a "read-only" task or an unknown destination),
  and why a message should be sent from a file instead of a quoted shell string.

## 0.1.1 — 2026-09-30

- Idempotent sends. `POST /v1/messages` accepts an optional `client_id` (1 to 64
  characters; longer is 400). The first POST stores it on the record and answers
  201. A later POST with the same client_id creates nothing and answers 200 with
  the original `id` and `ts` and `"duplicate": true`. The lookup and the append
  share the store's lock, so concurrent sends, even from separate processes,
  leave one record.
- `dotline send`, `clients/dotline.sh` and `clients/dotline.ps1` generate a fresh
  UUID4 client_id for every send, retry once with the same client_id after a
  network error and print `already delivered: message <id>` for a duplicate.
  `--client-id` (`-ClientId` in PowerShell) resends with a given one, and a send
  that still fails reports the client_id to reuse.
- Documented the reconcile rule (after an ambiguous send, resend with the same
  client_id, never a new one) in README.md and SECURITY.md, and that dotline does
  not promise exactly-once delivery: a claim lapses after 30 minutes without a
  reply.

## 0.1.0 — 2026-09-30

- Private, local JSONL mailbox with authenticated HTTP access and bounded requests.
- Python CLI, PowerShell and bash/curl clients for send, wait, replies and health.
- Expiring exclusive claims for cooperating Claude Code sessions.
- SessionStart hook, Monitor watch and preview stdio channel with a reply tool.
- Default data trust policy and opt-in orders policy with serious-decision checks.
- Cross-platform acceptance tests and CI; public-repository documentation.
