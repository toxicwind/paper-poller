# paper-poller

PAPER-TASK → PAPER-RESULT as a first-class fleet capability (Chris 2026-09-14 18:26 MDT:
paper research is first-class in the chat).

Any agent posts a dated single-line entry to `/home/toxic/.shingle/directives.md`:

```
## PAPER-TASK [t-optional-id]: <query> // <why this matters>
```

The poller daemon (pitchfork-managed, awrawr-pc) claims it with an **atomic mkdir
lock** (`state/claims/<task-id>/` — two pollers can never double-claim), races the
arXiv + alphaXiv legs (fail-fast per-leg timeouts, HFT rules), and posts back:

```
## PAPER-RESULT <task-id> — <query> (timestamp, paper-poller)
- Title (arXiv ID, date) https://arxiv.org/abs/… — one-line relevance
```

## Layout

- `bin/race_papers.py` — stdlib-only leg racer (arXiv Atom + `api.alphaxiv.org/v1/search/paper`,
  public, no key). One-shot: `bin/race_papers.py "query" [--maxn 8] [--jsonl out]`.
  Per-leg latency → winners JSONL (keep the fast path hot; `--lead-with-winner`).
- `bin/poller.py` — the daemon. Polls the channel every `PAPER_POLLER_INTERVAL`
  (30s), claims, searches, posts. `/health` + `/ready` on 127.0.0.1:25149.
- `bin/watchdog.py` — external watchdog (separate pitchfork daemon). Polls the
  poller's `/health`; on 3 consecutive failures or stale `last_poll`: SIGKILL the
  wedged PID, wait 45s, escalate to `pitchfork start paper-poller` (never `--force`).
  Own `/health` on 127.0.0.1:25150.
- `state/` — runtime state (claims, results, done.json, heartbeat, pidfile, logs,
  winners). Git-ignored; survives restarts on disk.
- `seed-demo/` — the 2026-09-14 18:26 MDT seed demo (`race.py`, 24-paper `out.jsonl`).

Evolved from the seed demo `/tmp/shingle-paper-race/race.py`; endpoint shapes
borrowed from `emergent-enrich/bin/route.py`. Skill docs:
`/home/toxic/workspace/skills/paper-search/SKILL.md` (composes the `hft-latency`
skill — doctrine referenced, not redefined).

## pitchfork stanzas (hand-edited into `/home/toxic/sovereign/pitchfork.toml`)

```toml
[daemons.paper-poller]
run = "exec /usr/bin/python3 /home/toxic/paper-poller/bin/poller.py"
dir = "/home/toxic/paper-poller"
mise = false
retry = true
boot_start = true
ready_http = "http://127.0.0.1:25149/ready"
env = { PAPER_POLLER_PORT = "25149", PAPER_POLLER_INTERVAL = "30" }
auto = ["start"]

[daemons.paper-poller-watchdog]
run = "exec /usr/bin/python3 /home/toxic/paper-poller/bin/watchdog.py"
dir = "/home/toxic/paper-poller"
mise = false
retry = true
boot_start = true
ready_http = "http://127.0.0.1:25150/health"
env = { WATCHDOG_PORT = "25150" }
auto = ["start"]
```

## Restart story (verified 2026-09-14, not claimed)

- `kill -9 <poller pid>` → pitchfork `retry=true` restarts it; `/health` 200 again.
- Wedge (`kill -STOP`, process alive, health dead) → watchdog fires after 3 failed
  checks: SIGKILL + `pitchfork start` recovery. (Timestamps in `state/watchdog.log`.)
- Silent-death mode seen fleet-wide today (process "running", health dead, pitchfork
  unaware — the buildsrv RLock wedge) is exactly what the external watchdog covers.

## Verification log (2026-09-14, all MDT, all verified live)

- E2E: PAPER-TASK `t-lane7e2e` posted 18:42 -> claimed 18:42:45 ->
  PAPER-RESULT landed (channel line 1027, 18:42): 8 papers with
  titles/IDs/URLs/one-line relevance.
- kill -9 @18:43:16 -> pitchfork retry=true restarted: new PID by 18:43:28,
  /health 200, first poll already done.
- Wedge (kill -STOP @18:43:32; health dead, pitchfork still "running"):
  watchdog 3 consecutive failures (18:43:51/18:44:26/18:45:01) ->
  SIGKILL 18:45:01 -> recovered 18:45:46 via retry (new PID, /health 200,
  polling). The exact silent-wedge mode seen fleet-wide today, covered.
- Per-leg latencies (bridge-side): alphaXiv 0.7-1.1s ok; arXiv HTTP-429 /
  tarpit during the post-demo burst -> per-leg 429 cooldown (5 min) added.
- Seed demo (18:26): 24 papers / 3 queries in ~14s (both legs healthy then).
- Incidents fixed forward, no rollbacks:
  1. poller v1 parsed the protocol TEMPLATE line as a task -> bogus
     PAPER-RESULT t-28c6529f (VOIDed in-channel; parser fixed in 61ae522).
  2. committed TOML merge-conflict markers made pitchfork.toml unparseable
     fleet-wide -> markers resolved (kept all stanzas), pushed 7e279c6bb2;
     supervisor adopts new stanzas via `pitchfork start` run from the
     sovereign dir (CLI reads ./pitchfork.toml; supervisor hot-reload of
     the file is not observed in 2.16.0).
