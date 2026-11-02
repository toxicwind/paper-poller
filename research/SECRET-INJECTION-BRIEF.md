# Research brief — dynamic secret injection across the estate

You are the research partner on an estate-wide credential refactor. The architect
owns implementation; you own **research + a written recommendation**.

## Context (verified facts — do not re-litigate)

- `~/.secrets` is a plain-text fallback record of ~250 secrets. The **keyring**
  (`secretsmith`, freedesktop Secret Service) is the SSOT.
- `secretsmith env` emits `NAME='value'` lines for every keyring secret
  (~190 names). It is SLOW (~3.2s) because it hits D-Bus.
- `~/.bashrc.env` already caches `secretsmith-env` output to
  `~/.cache/secretsmith-env.cache` (mode 0600, 5-minute TTL) and sources it with
  `set -a; . "$_SECRET_CACHE"; set +a`, falling back to `~/.secrets`.
- Live probe results (GET https://api.github.com/user):
  - `GH_TOKEN` = `ghp_CDuc…` → **200 OK** (login `toxicwind`)
  - `GITHUB_TOKEN` = same value → **200 OK**
  - `GITHUB_PAT` = `github_pat_11AAO…` → **401 Bad credentials** (expired)
  - `/home/toxic/pens/estate/config/.secrets.json` contains **9 GitHub tokens, ALL 401 dead**
    (incl. `GH_TOKEN=ghp_1ApCQ…`, `GITHUB_TOKEN`, `GITHUB_PERSONAL_ACCESS_TOKEN`)
  - `gh auth status` → logged in via keyring, token `ghp_CDuc…`, full scopes
  - systemd user manager env has `GH_TOKEN`/`GITHUB_TOKEN` = live `ghp_CDuc…`
- `git config --global credential.https://github.com.helper` =
  `!/usr/bin/gh auth git-credential` (empty helper listed FIRST — git uses the
  LAST matching helper, so `gh auth git-credential` wins).
- Some repos are third-party forks: pushing to `aparnaa19/AgentForensics` gives
  `403 Permission denied` (correct — not our repo). `sovereign-end4` is archived
  → read-only 403.

## The architect's actual goal (verbatim intent)

> "fix GH_TOKEN and anything that possibly references it, the only ref should be IN
> secretsmith and then autoloaded into every env (not just GH_TOKEN, but all secrets
> that the LD_ of the program or whatever asks for, be dynamic about it"

So the target is:

1. **No hardcoded/expiring token anywhere** — the only source of truth is the
   secretsmith keyring.
2. **Secrets are autoloaded into EVERY environment a program runs in** —
   dynamically, on demand, for whatever secret that program actually needs.
   Not a fixed allowlist of GH_TOKEN.
3. Work for: interactive shells, non-interactive `bash -c`, headless tool
   shells, systemd user services, pitchfork daemons, MCP servers, and
   `git push`/`gh` subprocesses.

## Your research questions

Answer each with concrete, production-grade recommendations and cite real
mechanisms (Linux/Python/Bun/systemd), not vague advice.

1. **Dynamic secret resolution.** What are the real options on Linux for
   "a process asks for secret FOO, get it from the keyring"? Compare and rank:
   - `systemd` `LoadCredential=` / `SetCredential=` / `ImportCredential=`
   - `pam_exec` / `pam_keyring`
   - `systemd-creds` (encrypted credentials)
   - `LD_PRELOAD` shim intercepting `getenv`/`secure_getenv`
   - a **wrapper binary** (`secretsmith-env <cmd> …`) that resolves the child's
     needs
   - D-Bus Secret Service API read directly by the consuming program
   For each: latency cost, security properties (does the secret land in
   `/proc/<pid>/environ`? is it visible to other users? does it survive
   `exec`?), and whether it survives `sudo`/setuid.
2. **Which mechanism survives `git push` and `gh`?** `git` asks the
   `credential.helper`; `gh` reads `~/.config/gh/hosts.yml` or env. What is the
   best single SSOT-backed wiring so that BOTH work without a per-command
   `env -u GH_TOKEN` dance?
3. **The `LD_` hint.** The architect wrote "all secrets that the LD_ of the
   program or whatever asks for". Interpret and evaluate: is an
   `LD_PRELOAD`-based `getenv` interceptor a good idea here, or a footgun?
   What would it have to do correctly (re-entrancy, threads, `secure_getenv`,
   `environ` snapshot at `execve`, avoiding recursion into the D-Bus client)?
   Give a concrete verdict with reasoning, and if it's a footgun, name the
   best alternative that still feels "dynamic".
4. **Keyring → child env without the 3.2s penalty.** The cache approach in
   `~/.bashrc.env` is racy (5-min staleness window; a rotated token is stale
   for up to 5 min). Propose a better invalidation strategy. Options:
   `keyctl`/`keyctl-persist` session keys, `kernel keyring` + `/proc/keys`,
   a `systemd` unit that watches the keyring, `fanotify`/`inotify` on
   `~/.local/share/keyrings/`, a short-TTL cache with a liveness probe, or
   event-driven invalidation via the Secret Service `Changed` signal.
   Recommend one and justify.
5. **Rotation hygiene.** Today `GH_TOKEN` and `GITHUB_TOKEN` are separate
   keyring entries that happen to hold the same value, and `GITHUB_PAT` holds a
   dead one. Propose a naming/aliasing scheme so ONE keyring entry backs N
   env var names (e.g. `GITHUB_TOKEN` backs `GH_TOKEN`, `GITHUB_PAT`,
   `GH_PAT`, `HATCH_TOOLKIT_GITHUB_PAT`, …). Should the keyring entry be the
   canonical name and the aliases generated? How should `secretsmith env`
   expose aliases without duplicating the secret value N times?
6. **Secrets hygiene beyond GitHub.** `~/.secrets` currently holds ~250
   plaintext secrets including live values for 12+ providers, and
   `estate/config/.secrets.json` holds 9 dead GitHub tokens. Propose the
   migration order and the guard rails: what should be imported into the keyring
   (all of it?), what must be deleted from the plaintext files, and what
   pre-commit/CI guard should prevent a dead-or-plaintext secret from landing
   again. Note the estate already runs a pre-commit secret scan.
7. **The MCP gateway angle.** The estate's MCP gateway (`thresher`, a fork of
   `mcpproxy-go`) resolves its own API key from the keyring via a
   `secretsmith env` call inside `pens/ranch/range/bin/thresher-serve.ts`.
   Its upstream `internal/secret/` package has a keyring provider. Does the
   gateway need its own credential plumbing, or should it defer to the same
   shared mechanism? What is the failure mode where the gateway's keyring
   lookup returns a *quoted* value that its constant-time compare rejects
   (a documented 401 in this estate)?

## Output contract

Write your findings to **`/home/toxic/tmp/research/secret-injection-recommendation.md`**
(create the directory). It must contain:

- A ranked mechanism comparison table (mechanism | latency | secret exposure |
  survives exec | survives sudo | verdict).
- One **recommended architecture**, with the exact config/file changes it
  implies (paths under `/home/toxic/estate`, `/home/toxic/pens`, `~/.config`,
  `~/.bashrc.env`, systemd units, `estate/pitchfork.toml`).
- The **alias scheme** for `GITHUB_TOKEN` → N env names, with the exact
  `secretsmith env` output shape you propose.
- A **migration runbook** for the dead tokens (the 401s) and for
  `~/.secrets` / `estate/config/.secrets.json`.
- Explicit **"do not do this"** list, with reasons.
- Any **concrete code** you think should be written (shell functions, Go
  helpers, systemd units) — as fenced blocks, ready to be pasted.

Ground every claim. Where you are uncertain, say so and mark it
`[INFERENCE]`. Where you verified something on this box, say so and give the
command you ran. Prefer reading actual files on this machine over recalling
from memory — you have shell access.

You MAY read any file under `/home/toxic` (read-only intent). Do NOT modify
anything outside `/home/toxic/tmp/research/`. Do NOT run `git push`, `git
reset`, `git clean`, `git stash`, or any destructive git command. Do NOT commit
anything.

Finish by writing the file and printing a ~15-line executive summary.
