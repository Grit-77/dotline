# Security

dotline bridges an authenticated mailbox into an agent session. It does not
execute messages itself and cannot enforce an agent's decisions.

## Threat model

The bearer grants read access to every inbox message and reply and write access
to the inbox. It does not grant HTTP reply or shell-execution access. An agent
that acts on messages can, however, exercise its own permissions. An attacker
with the bearer, a compromised dot or attacker-controlled content read by the
dot can try to influence those actions.

Prompt injection is a central risk. Email and web pages may contain a request
to forward instructions, leak files or change security settings. Authentication
does not prove the owner approved that request. The default `data` mode requires
chat confirmation for state changes. The opt-in `orders` mode delegates routine
work, but still requires the owner's chat approval for irreversible actions,
money/accounts, credentials/security, messages to others, new data recipients,
disabled checks and out-of-character requests. These are model instructions;
protect valuable systems with their own permissions and review boundaries.

Local users who can read the configuration directory can read the bearer and
mailbox. Local users who can write the mailbox can forge messages or replies.
Claims coordinate cooperating sessions; they do not authenticate local users.
Mailbox files, claims and backups contain plaintext data. There is no encryption
at rest, per-dot identity, audit retention or filesystem symlink defense against
an attacker already able to write the owner's private directory.

## Token and transport handling

- Keep the config directory private. POSIX modes are 0700 for directories and
  0600 for data files. On Windows use a private directory with restrictive ACLs.
- Init never displays or rotates the token. The explicit `token --show` command
  warns and displays it only for the owner's private terminal. Avoid recordings.
- Give clients a token **file**, not a token in arguments or URLs. The shell
  helper uses a private temporary header file, removed when the process exits.
  Abrupt termination may leave temporary files; protect and clean your temp dir.
- Do not paste real tokens into issues, examples, commits or shared transcripts.
  Cloud-dot instructions containing a token expand the places it can escape.
- Bind to loopback and use HTTPS tunnels for remote access. Clients reject
  remote plaintext HTTP and redirects, and ignore ambient HTTP proxies to avoid
  forwarding the bearer somewhere else. Do not enable curl tracing or debug
  recordings around requests.
- Rotate an exposed token by stopping the server, securely replacing the token
  file, updating client copies and restarting. The running server holds its
  initial token in memory until restart.
- Request logs contain only fixed route names, methods and status codes. HTTP
  error bodies and client errors do not echo request text or credentials.

The 30/minute global limit and bounded POST bodies reduce accidental abuse.
They are not comprehensive denial-of-service protection: health is public, GET
results and mailbox history can grow, and slow connections still consume
threads. Put appropriate access limits at your tunnel or reverse proxy for a
public deployment. Disk space and local file integrity remain operator duties.

## Reporting a vulnerability

Use the repository's private vulnerability reporting feature if enabled. If it
is unavailable, open an issue asking for a private reporting route **without
exploit details, private message content or tokens**. Wait for a private channel
before sharing reproduction details. Do not disclose another person's data.

Version 0.1.x is the initial supported line. Public hosting and a private
reporting route will be selected when this project is published.
