#!/usr/bin/env python3
"""paper-poller daemon: fleet-channel PAPER-TASK -> PAPER-RESULT, first-class.

Protocol (Chris 2026-09-14 18:26 MDT — paper research is first-class in chat):
  any agent posts:   PAPER-TASK [task-id]: <query> // <why this matters>
                     (single line, dated entry, in /home/toxic/.shingle/directives.md)
  this daemon:       claims it (atomic mkdir lock per task id — two pollers
                     can never double-claim), races the arXiv + alphaXiv legs
                     (fail-fast per-leg timeouts, HFT rules), posts back:
                     PAPER-RESULT <task-id> — <query>
                     with titles, arXiv IDs, URLs, one-line relevance each.

Restart story (verified, not claimed):
  - pitchfork daemon, retry=true. /health + /ready on 127.0.0.1:PORT.
  - external watchdog (bin/watchdog.py, separate pitchfork daemon, additive):
    polls /health; on 3 consecutive failures or a stale last_poll it SIGKILLs
    the wedged poller and runs `pitchfork start paper-poller` (never --force,
    never touches the supervisor). Heartbeat + pidfile in state/ are ground truth.
  - kill -9 test: pitchfork retry restarts the daemon (see README for the
    verified timestamps).

Env: PAPER_POLLER_PORT (default 25149), PAPER_POLLER_INTERVAL (default 30s),
     PAPER_CHANNEL (default /home/toxic/.shingle/directives.md),
     PAPER_STATE (default <repo>/state).
Stdlib only. Appends to the channel with O_APPEND (atomic for small writes).
"""
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import race_papers

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = int(os.environ.get("PAPER_POLLER_PORT", "25149"))
INTERVAL = int(os.environ.get("PAPER_POLLER_INTERVAL", "30"))
CHANNEL = os.environ.get("PAPER_CHANNEL", "/home/toxic/.shingle/directives.md")
STATE = os.environ.get("PAPER_STATE", os.path.join(REPO, "state"))
CLAIMS = os.path.join(STATE, "claims")
RESULTS = os.path.join(STATE, "results")
WINNERS = os.path.join(STATE, "leg-winners.jsonl")
CLAIM_TTL = 45 * 60  # a claim with no result after 45 min is stealable
WHO = "paper-poller"

# PAPER-TASK [optional-id]: query // optional why   (colon is mandatory so that
# prose like "PAPER-TASK poller" never parses as a task)
TASK_RE = re.compile(
    r"PAPER-TASK\s*(?:\[([A-Za-z0-9][A-Za-z0-9_-]*)\]\s*)?:\s*(.+?)"
    r"(?:\s*//\s*(.+?))?\s*$")
RESULT_RE = re.compile(r"PAPER-RESULT\s+([A-Za-z0-9][A-Za-z0-9_-]*)")

stats = {"boot": time.time(), "polls": 0, "last_poll": 0.0, "first_poll_done": False,
         "claimed": 0, "results_posted": 0, "errors": 0}


def log(msg):
    line = "%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(os.path.join(STATE, "poller.log"), "a") as f:
            f.write(line)
    except OSError:
        pass
    print(line, end="", flush=True)


def task_id_for(task_line, explicit):
    if explicit:
        return explicit
    return "t-" + hashlib.sha1(task_line.encode()).hexdigest()[:8]


def parse_tasks(text):
    tasks = []
    for line in text.splitlines():
        if "PAPER-RESULT" in line:
            continue
        m = TASK_RE.search(line)
        if not m:
            continue
        tid = task_id_for(line.strip(), m.group(1))
        tasks.append({"id": tid, "query": m.group(2).strip(),
                      "why": (m.group(3) or "").strip(), "line": line.strip()})
    return tasks


def load_done():
    p = os.path.join(STATE, "done.json")
    try:
        with open(p) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_done(done):
    tmp = os.path.join(STATE, "done.json.tmp")
    with open(tmp, "w") as f:
        json.dump(done, f, indent=1)
    os.replace(tmp, os.path.join(STATE, "done.json"))


def claim(task_id):
    """Atomic claim via mkdir. Returns True if WE hold the claim."""
    d = os.path.join(CLAIMS, task_id)
    try:
        os.makedirs(d)
    except FileExistsError:
        # someone holds it: check staleness
        cf = os.path.join(d, "claim.json")
        try:
            age = time.time() - os.path.getmtime(cf)
        except OSError:
            age = 0
        if age > CLAIM_TTL:
            log("claim %s stale (%.0fs) — stealing" % (task_id, age))
            shutil.rmtree(d, ignore_errors=True)
            try:
                os.makedirs(d)
            except FileExistsError:
                return False  # lost the race on re-claim
        else:
            return False
    with open(os.path.join(d, "claim.json"), "w") as f:
        json.dump({"by": WHO, "at": time.time(),
                   "at_human": time.strftime("%Y-%m-%d %H:%M:%S")}, f)
    return True


def fmt_mdt(ts=None):
    return time.strftime("%Y-%m-%d %H:%M MDT", time.localtime(ts))


def post_result(task, papers, leg_stats):
    lines = []
    for p in papers:
        aid = p.get("arxiv_id") or "?"
        pub = p.get("published") or "?"
        url = p.get("url") or ""
        lines.append("- %s (%s, %s) %s — %s"
                     % (p.get("title") or "(untitled)", aid, pub, url,
                        race_papers.one_line(p)))
    legs = " / ".join("%s %s%s" % (k,
                                   ("%.2fs" % (v["latency_ms"] / 1000)) if v["ok"] else "FAIL",
                                   (" ok(%d)" % v["n"]) if v["ok"] else "")
                      for k, v in leg_stats.items())
    entry = ("## PAPER-RESULT %s — %s (%s, %s)\n%s\n"
             "_why: %s · legs: %s · full JSONL: %s_\n"
             % (task["id"], task["query"], fmt_mdt(), WHO,
                "\n".join(lines) if lines else "_no papers returned_",
                task["why"] or "—", legs,
                os.path.join(RESULTS, task["id"] + ".jsonl")))
    with open(CHANNEL, "a") as f:  # O_APPEND: atomic small write
        f.write("\n" + entry)
    return entry


def handle_task(task, done):
    tid = task["id"]
    if tid in done:
        return
    if not claim(tid):
        return
    stats["claimed"] += 1
    log("claimed %s: %r" % (tid, task["query"]))
    t0 = time.perf_counter()
    try:
        papers, leg_stats = race_papers.search(
            task["query"], maxn=8, winners_log=WINNERS, lead_with_winner=True,
            cooldown_path=os.path.join(STATE, "cooldown.json"))
        entry = post_result(task, papers, leg_stats)
        with open(os.path.join(RESULTS, tid + ".jsonl"), "w") as f:
            for p in papers:
                f.write(json.dumps(p) + "\n")
        with open(os.path.join(RESULTS, tid + ".md"), "w") as f:
            f.write(entry)
        done[tid] = {"resulted_at": fmt_mdt(), "n": len(papers),
                     "query": task["query"]}
        save_done(done)
        stats["results_posted"] += 1
        log("result %s posted: %d papers in %.1fs"
            % (tid, len(papers), time.perf_counter() - t0))
    except Exception as e:
        stats["errors"] += 1
        log("ERROR task %s: %r" % (tid, e))
        # release the claim so a later poll (or a human) can retry
        shutil.rmtree(os.path.join(CLAIMS, tid), ignore_errors=True)


def poll_once():
    try:
        with open(CHANNEL) as f:
            text = f.read()
    except OSError as e:
        log("ERROR reading channel: %r" % e)
        stats["errors"] += 1
        return
    done = load_done()
    # backfill: results posted by anyone (manual claims before the poller landed)
    added = False
    for rid in set(RESULT_RE.findall(text)):
        if rid not in done:
            done[rid] = {"resulted_at": "backfilled", "n": -1, "query": "?"}
            added = True
    if added:
        save_done(done)
    for task in parse_tasks(text):
        handle_task(task, done)
    stats["polls"] += 1
    stats["last_poll"] = time.time()
    stats["first_poll_done"] = True
    with open(os.path.join(STATE, "heartbeat.json"), "w") as f:
        json.dump({"last_poll": stats["last_poll"],
                   "last_poll_human": fmt_mdt(stats["last_poll"]),
                   "polls": stats["polls"], "claimed": stats["claimed"],
                   "results_posted": stats["results_posted"]}, f)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True, "uptime_s": round(time.time() - stats["boot"], 1),
                             "last_poll": stats["last_poll"],
                             "last_poll_age_s": round(time.time() - stats["last_poll"], 1)
                             if stats["last_poll"] else None,
                             "polls": stats["polls"], "claimed": stats["claimed"],
                             "results_posted": stats["results_posted"],
                             "errors": stats["errors"]})
        elif self.path == "/ready":
            self._send(200 if stats["first_poll_done"] else 503,
                       {"ready": stats["first_poll_done"]})
        else:
            self._send(404, {"ok": False})


def main():
    for d in (STATE, CLAIMS, RESULTS):
        os.makedirs(d, exist_ok=True)
    with open(os.path.join(STATE, "poller.pid"), "w") as f:
        f.write(str(os.getpid()))
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log("boot: port=%d interval=%ds channel=%s" % (PORT, INTERVAL, CHANNEL))
    while True:
        try:
            poll_once()
        except Exception as e:  # never die on a bad poll
            stats["errors"] += 1
            log("ERROR poll: %r" % e)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
