#!/usr/bin/env python3
"""paper-poller-watchdog: the external half of the poller's restart story.

Separate pitchfork daemon (additive — it never touches the supervisor itself).
Every WATCHDOG_INTERVAL seconds it GETs the poller's /health (5s timeout).
Recovery ladder:
  1. 3 consecutive health failures, or last_poll stale > STALE_AFTER
     -> SIGKILL the poller PID from state/poller.pid (a wedged-but-alive
        process is the failure mode pitchfork retry=true cannot see)
  2. wait 45s; if still unhealthy -> `pitchfork start paper-poller`
     (plain start, NEVER --force: --force restarts have killed children before)
  3. log everything to state/watchdog.log; keep failing loudly, never silently.

Own /health on 127.0.0.1:WATCHDOG_PORT so the fleet can watch the watcher.

Env: WATCHDOG_PORT (default 25150), WATCHDOG_INTERVAL (default 30),
     POLLER_HEALTH (default http://127.0.0.1:25149/health),
     PAPER_STATE (default <repo>/state), POLLER_NAME (default paper-poller).
Stdlib only.
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = int(os.environ.get("WATCHDOG_PORT", "25150"))
INTERVAL = int(os.environ.get("WATCHDOG_INTERVAL", "30"))
POLLER_HEALTH = os.environ.get("POLLER_HEALTH", "http://127.0.0.1:25149/health")
POLLER_NAME = os.environ.get("POLLER_NAME", "paper-poller")
STATE = os.environ.get("PAPER_STATE", os.path.join(REPO, "state"))
STALE_AFTER = int(os.environ.get("WATCHDOG_STALE_AFTER", "180"))
MAX_FAILS = 3

stats = {"boot": time.time(), "checks": 0, "fails": 0, "recoveries": 0,
         "last_ok": 0.0}


def log(msg):
    line = "%s [watchdog] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(os.path.join(STATE, "watchdog.log"), "a") as f:
            f.write(line)
    except OSError:
        pass
    print(line, end="", flush=True)


def poller_health():
    """(healthy_bool, detail). Healthy = /health 200 and last_poll fresh."""
    try:
        req = urllib.request.Request(POLLER_HEALTH, headers={"User-Agent": "paper-poller-watchdog/1.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            body = json.loads(r.read().decode())
        age = time.time() - (body.get("last_poll") or 0)
        if age > STALE_AFTER:
            return False, "stale last_poll %.0fs" % age
        return True, "ok age=%.0fs" % age
    except Exception as e:
        return False, "health fetch failed: %s" % str(e)[:100]


def poller_pid():
    try:
        with open(os.path.join(STATE, "poller.pid")) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def recover(reason):
    stats["recoveries"] += 1
    log("RECOVER triggered: %s" % reason)
    pid = poller_pid()
    if pid and pid_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
            log("SIGKILLed wedged poller pid %d" % pid)
        except OSError as e:
            log("SIGKILL failed: %r" % e)
    else:
        log("no live poller pid (pid=%s)" % pid)
    time.sleep(45)  # give pitchfork retry=true room to restart it
    ok, detail = poller_health()
    if ok:
        log("recovered via SIGKILL+retry: %s" % detail)
        return
    log("still unhealthy after SIGKILL+45s (%s) — escalating to `pitchfork start`" % detail)
    try:
        r = subprocess.run(["pitchfork", "start", POLLER_NAME],
                           capture_output=True, text=True, timeout=60)
        log("pitchfork start rc=%d out=%s err=%s"
            % (r.returncode, r.stdout.strip()[:200], r.stderr.strip()[:200]))
    except Exception as e:
        log("pitchfork start failed: %r" % e)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"ok": True, "uptime_s": round(time.time() - stats["boot"], 1),
                           "checks": stats["checks"], "fails": stats["fails"],
                           "recoveries": stats["recoveries"],
                           "last_ok_age_s": round(time.time() - stats["last_ok"], 1)
                           if stats["last_ok"] else None}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    os.makedirs(STATE, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log("boot: watching %s every %ds" % (POLLER_HEALTH, INTERVAL))
    fails = 0
    while True:
        try:
            ok, detail = poller_health()
            stats["checks"] += 1
            if ok:
                fails = 0
                stats["last_ok"] = time.time()
            else:
                fails += 1
                stats["fails"] += 1
                log("unhealthy (%d/%d): %s" % (fails, MAX_FAILS, detail))
                if fails >= MAX_FAILS:
                    fails = 0
                    recover(detail)
        except Exception as e:
            log("ERROR in check loop: %r" % e)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
