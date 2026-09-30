# Changelog

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
