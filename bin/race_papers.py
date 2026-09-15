#!/usr/bin/env python3
"""paper-poller leg racer: arXiv + alphaXiv, stdlib-only, HFT-like.

Evolved from /tmp/shingle-paper-race/race.py (seed demo, 2026-09-14 18:26 MDT).
Endpoint shapes borrowed from emergent-enrich route.py (public, no key):
  arXiv:    https://export.arxiv.org/api/query  (Atom feed)
  alphaXiv: https://api.alphaxiv.org/v1/search/paper  (public path; alphaXiv
            403s KEYED requests on public paths, so no auth header is sent)

HFT rules enforced:
  - legs race concurrently per query (ThreadPoolExecutor); a dead leg never
    blocks the other
  - fail-fast per-leg timeout (LEG_TIMEOUT); slow is a kind of wrong
  - per-leg latency measured with perf_counter and appended to the winners
    JSONL (keep the fast path hot: --lead-with-winner orders legs by it)
  - first VALID result wins per leg; losers are logged, not retried

One-shot CLI:
  race_papers.py "query words" [--maxn 8] [--jsonl out.jsonl]
Prints one JSON object per paper to stdout, SUMMARY to stderr.
"""
import argparse
import concurrent.futures as cf
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

UA = {"User-Agent": "shingle-paper-poller/1.0"}
LEG_TIMEOUT = 10
ARXIV_BASE = "https://export.arxiv.org/api/query"
ALPHAXIV_SEARCH = "https://api.alphaxiv.org/v1/search/paper"
DEFAULT_WINNERS = os.path.expanduser("~/.cache/shingle/paper_leg_winners.jsonl")
DEFAULT_COOLDOWN = os.path.join(os.path.dirname(DEFAULT_WINNERS), "cooldown.json")
COOLDOWN_SECONDS = 300  # after an HTTP 429, spare the leg for 5 min
_ARXIV_VER_RE = re.compile(r"v\d+$")


def _strip_ver(aid):
    return _ARXIV_VER_RE.sub("", aid or "")


def fetch(url, timeout=LEG_TIMEOUT):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def arxiv_leg(query, maxn=8):
    q = "all:" + "+AND+".join(urllib.parse.quote(w) for w in query.split()[:6])
    url = ("%s?search_query=%s&start=0&max_results=%d"
           "&sortBy=relevance&sortOrder=descending" % (ARXIV_BASE, q, maxn))
    data = fetch(url)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out = []
    for e in ET.fromstring(data).findall("a:entry", ns):
        aid = _strip_ver((e.findtext("a:id", default="", namespaces=ns) or "")
                         .rsplit("/abs/", 1)[-1])
        summary = re.sub(r"\s+", " ",
                         e.findtext("a:summary", default="", namespaces=ns) or "").strip()
        out.append({
            "source": "arxiv",
            "title": re.sub(r"\s+", " ",
                            e.findtext("a:title", default="", namespaces=ns) or "").strip(),
            "arxiv_id": aid,
            "url": "https://arxiv.org/abs/" + aid,
            "published": (e.findtext("a:published", default="", namespaces=ns) or "")[:10],
            "summary": summary,
            "votes": None,
            "topics": [],
        })
    return out


def alphaxiv_leg(query, maxn=8):
    url = ALPHAXIV_SEARCH + "?q=" + urllib.parse.quote(query)
    data = json.loads(fetch(url).decode("utf-8"))
    items = data if isinstance(data, list) else []
    out = []
    for r in items[:maxn]:
        aid = _strip_ver(r.get("universal_paper_id") or r.get("canonical_id") or "")
        m = r.get("metrics") or {}
        out.append({
            "source": "alphaxiv",
            "title": r.get("title"),
            "arxiv_id": aid or None,
            "url": "https://arxiv.org/abs/" + aid if aid else None,
            "published": (r.get("publication_date") or "")[:10],
            "summary": (r.get("abstract") or "")[:600],
            "votes": m.get("total_votes"),
            "topics": r.get("topics") or [],
        })
    return out


LEGS = (("arxiv", arxiv_leg), ("alphaxiv", alphaxiv_leg))


def _check_cooldown(cooldown_path, leg):
    """Return a reason string if the leg is cooling down, else None."""
    try:
        with open(cooldown_path) as f:
            until = json.load(f).get(leg, 0)
        if time.time() < until:
            return "cooling until %s" % time.strftime("%H:%M:%S", time.localtime(until))
    except (OSError, ValueError):
        pass
    return None


def _set_cooldown(cooldown_path, leg, seconds=COOLDOWN_SECONDS):
    try:
        try:
            with open(cooldown_path) as f:
                cd = json.load(f)
        except (OSError, ValueError):
            cd = {}
        cd[leg] = time.time() + seconds
        d = os.path.dirname(cooldown_path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(cooldown_path, "w") as f:
            json.dump(cd, f)
    except OSError:
        pass  # cooldown is advisory, never a failure


def _leg_order(lead_with_winner, winners_log):
    """Order legs by proven median latency (fastest first); default: arxiv, alphaxiv."""
    if not lead_with_winner or not winners_log or not os.path.exists(winners_log):
        return [name for name, _ in LEGS]
    lat = {}
    try:
        with open(winners_log) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("ok"):
                    lat.setdefault(r["leg"], []).append(r["latency_ms"])
    except OSError:
        pass
    med = {k: sorted(v)[len(v) // 2] for k, v in lat.items() if v}
    names = [name for name, _ in LEGS]
    return sorted(names, key=lambda n: med.get(n, float("inf")))


def _log_winner(path, leg, query, latency_ms, ok, n):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps({"ts": time.time(), "leg": leg, "query": query,
                                "latency_ms": round(latency_ms, 1), "ok": ok,
                                "n": n}) + "\n")
    except OSError:
        pass  # winners log is a cache, never a failure


def search(query, maxn=8, winners_log=DEFAULT_WINNERS, lead_with_winner=False,
           timeout=LEG_TIMEOUT, cooldown_path=DEFAULT_COOLDOWN):
    """Race the legs for one query. Returns (papers, leg_stats).

    leg_stats: {leg: {"ok": bool, "latency_ms": float, "n": int, "error": str?}}
    papers: ranked, deduped list (see rank()).
    A leg that recently HTTP-429'd is skipped until its cooldown expires.
    """
    order = _leg_order(lead_with_winner, winners_log)
    legfn = dict(LEGS)
    papers, stats = [], {}
    todo = {}
    for name in order:
        why = _check_cooldown(cooldown_path, name) if cooldown_path else None
        if why:
            stats[name] = {"ok": False, "latency_ms": 0.0, "n": 0,
                           "error": "429 " + why}
        else:
            todo[name] = legfn[name]
    with cf.ThreadPoolExecutor(max_workers=max(1, len(todo))) as ex:
        futs = {ex.submit(_timed_leg, fn, query, maxn, timeout): name
                for name, fn in todo.items()}
        for f in cf.as_completed(futs, timeout=timeout + 5):
            name = futs[f]
            try:
                items, ms = f.result()
                stats[name] = {"ok": True, "latency_ms": round(ms, 1), "n": len(items)}
                for p in items:
                    p["query"] = query
                    papers.append(p)
            except Exception as e:  # leg died: logged, never blocks the other
                if isinstance(e, urllib.error.HTTPError) and e.code == 429:
                    _set_cooldown(cooldown_path, name)  # spare the leg, don't hammer it
                stats[name] = {"ok": False, "latency_ms": None, "n": 0,
                               "error": str(e)[:120]}
                papers.append({"query": query, "source": name,
                               "error": str(e)[:120]})
            s = stats[name]
            _log_winner(winners_log, name, query,
                        s["latency_ms"] or timeout * 1000, s["ok"], s["n"])
    return rank(papers), stats


def _timed_leg(fn, query, maxn, timeout):
    t0 = time.perf_counter()
    items = fn(query, maxn)
    return items, (time.perf_counter() - t0) * 1000


def rank(papers, topn=8):
    """Dedupe by arxiv_id; community votes first, then newest, then leg order."""
    seen, uniq = set(), []
    for p in papers:
        if "error" in p or not p.get("arxiv_id"):
            continue
        key = p["arxiv_id"].lower()
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    # stable sorts: published desc first, then votes desc (ties keep pub order)
    uniq.sort(key=lambda p: (p.get("published") or ""), reverse=True)
    uniq.sort(key=lambda p: (p.get("votes") or 0), reverse=True)
    return uniq[:topn]


def one_line(p, width=160):
    """Mechanical one-line relevance: topics + abstract snippet (no invention)."""
    bits = []
    topics = [t for t in (p.get("topics") or []) if t][:3]
    if topics:
        bits.append("[" + ", ".join(topics) + "]")
    summ = re.sub(r"\s+", " ", p.get("summary") or "").strip()
    if summ:
        if len(summ) > width:
            summ = summ[:width].rsplit(" ", 1)[0] + "…"
        bits.append(summ)
    return " ".join(bits) or "(no abstract)"


def main(argv=None):
    ap = argparse.ArgumentParser(description="race arXiv + alphaXiv legs for a query")
    ap.add_argument("query")
    ap.add_argument("--maxn", type=int, default=8)
    ap.add_argument("--jsonl", default=None)
    ap.add_argument("--winners", default=DEFAULT_WINNERS)
    ap.add_argument("--cooldown", default=DEFAULT_COOLDOWN)
    ap.add_argument("--lead-with-winner", action="store_true")
    a = ap.parse_args(argv)
    t0 = time.perf_counter()
    papers, stats = search(a.query, maxn=a.maxn, winners_log=a.winners,
                           lead_with_winner=a.lead_with_winner,
                           cooldown_path=a.cooldown)
    out = sys.stdout if not a.jsonl else open(a.jsonl, "w")
    for p in papers:
        out.write(json.dumps(p) + "\n")
    if a.jsonl:
        out.close()
    ok = [p for p in papers if "error" not in p]
    print("SUMMARY papers=%d query=%r legs=%s total=%.2fs"
          % (len(ok), a.query,
             ",".join("%s:%s" % (k, ("%.1fs" % (v["latency_ms"] / 1000))
                                 if v["ok"] else "FAIL")
                       for k, v in stats.items()),
             time.perf_counter() - t0), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
