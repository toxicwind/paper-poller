#!/usr/bin/env python3
"""paper-poller leg racer: arXiv + alphaXiv, stdlib-only, HFT-like.

Evolved from /tmp/kite-paper-race/race.py (seed demo, 2026-09-14 18:26 MDT).
Endpoint shapes borrowed from emergent-enrich route.py (public, no key):
  arXiv:    https://export.arxiv.org/api/query  (Atom feed)
  alphaXiv: https://api.alphaxiv.org/v1/search/paper  (public path; alphaXiv
            403s KEYED requests on public paths, so no auth header is sent)

HFT rules enforced:
  - legs race concurrently per query (ThreadPoolExecutor); a dead leg never
    blocks the other
  - fail-fast per-leg timeout (LEG_TIMEOUT); slow is a kind of wrong
  - the race ends on SUFFICIENCY, not exhaustion: once maxn papers are in hand
    stragglers get SETTLE_SECONDS and then are dropped, so a throttled arXiv
    leg cannot stretch a 5s answer into 22s while contributing nothing
  - per-leg latency measured with perf_counter and appended to the winners
    JSONL (keep the fast path hot: --lead-with-winner orders legs by it)
  - first VALID result wins per leg; losers are logged, not retried

One-shot CLI:
  race_papers.py "query words" [--maxn 8] [--jsonl out.jsonl]
Prints one JSON object per paper to stdout, SUMMARY to stderr.
"""
import argparse
import json
import os
import queue
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import threading

UA = {"User-Agent": "paper-poller/1.0 (mailto:toxicwind@gmail.com)"}
LEG_TIMEOUT = 6              # a leg that has not answered in 6s will not answer
RACE_DEADLINE_SECONDS = 10   # absolute wall for the whole race
SETTLE_SECONDS = 1.5         # grace for stragglers once the result set is full
ARXIV_BASE = "https://export.arxiv.org/api/query"
ALPHAXIV_SEARCH = "https://api.alphaxiv.org/v1/search/paper"
HF_SEARCH = "https://huggingface.co/api/papers/search"
DEFAULT_WINNERS = os.path.expanduser("~/.cache/paper-poller/paper_leg_winners.jsonl")
DEFAULT_COOLDOWN = os.path.join(os.path.dirname(DEFAULT_WINNERS), "cooldown.json")
# Per-leg request policy, applied to the first attempt AND every retry.
#
# arXiv's numbers are the ones its own reference client ships with
# (arxiv 4.0.1: `Client(page_size=100, delay_seconds=3.0, num_retries=3)`), and
# it measures the delay from the moment the previous response came back, so a
# slow response still costs a full interval. Semantic Scholar is the same
# story without published numbers: it clears a shared 429 only after several
# seconds, and from this host it took six attempts. Both are shared community
# infrastructure, so neither is burst.
LEG_MIN_INTERVAL = {"arxiv": 3.0, "s2": 1.0}
LEG_MAX_RETRIES = {"arxiv": 3, "s2": 5}
MAX_RETRIES = 1               # a leg with no published policy re-tries once
# arXiv ANDs its terms, so one long query is already narrow there. The query
# decomposition exists for the legs that cannot AND — so arXiv gets one
# request, which also keeps it inside its own pacing.
LEG_MAX_SUBQUERIES = {"arxiv": 1}
COOLDOWN_BASE_SECONDS = 60
COOLDOWN_BACKOFF = 4          # 60s, 4m, 16m, 64m, 256m …
COOLDOWN_MAX_SECONDS = 6 * 3600   # the six-hour ban is the case that matters
_LEG_NEXT_OK = {}
_LEG_NEXT_LOCK = threading.Lock()
_COOLDOWN_LOCK = threading.Lock()
_ARXIV_VER_RE = re.compile(r"v\d+$")
OPENALEX_WORKS = "https://api.openalex.org/works"
S2_SEARCH = "https://api.semanticscholar.org/graph/v1/paper/search"
CONTACT_MAIL = "toxicwind@gmail.com"
MAX_QUERY_TOKENS = 6   # a longer query is split; see plan_queries
MAX_SUBQUERIES = 4
_STOPWORDS = frozenset("""a an and are as at be been by for from has have how in is
it its of on or that the their there this to was were what when where which who
why with""".split())


def _strip_ver(aid):
    return _ARXIV_VER_RE.sub("", aid or "")


def fetch(url, timeout=LEG_TIMEOUT):
    """One GET. Retry and pacing belong to the caller (_timed_leg), so that a
    policy stated per leg governs the first attempt and every retry alike
    rather than the two drifting apart."""
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


def _lexical_hit(haystack, terms):
    """True if any query term of 3+ chars appears in the text."""
    low = haystack.lower()
    return any(t in low for t in terms)


def hf_papers_leg(query, maxn=8):
    """HuggingFace's arXiv paper index — the leg that survives arXiv throttling.

    export.arxiv.org answers this host with a bare 429 (no Retry-After) and then
    starts hanging; HF is a separate service and keeps answering 200. Its
    ranking is semantic rather than keyword-exact, so results are lexically
    filtered before they count: a fallback that returns off-topic papers is
    worse than no fallback at all. Declared last so the median-latency ordering
    in _leg_order keeps it behind the exact legs whenever they work.
    """
    data = json.loads(fetch(HF_SEARCH + "?q=" + urllib.parse.quote(query)).decode("utf-8"))
    items = data if isinstance(data, list) else []
    terms = [w.lower() for w in query.split() if len(w) >= 3]
    out = []
    for r in items:
        p = r.get("paper") or {}
        aid = _strip_ver(p.get("id") or "")
        title = re.sub(r"\s+", " ", r.get("title") or "").strip()
        summary = re.sub(r"\s+", " ", r.get("summary") or "").strip()
        if terms and not _lexical_hit(title + " " + summary, terms):
            continue
        out.append({
            "source": "hf",
            "title": title,
            "arxiv_id": aid or None,
            "url": "https://arxiv.org/abs/" + aid if aid else None,
            "published": (r.get("publishedAt") or "")[:10],
            "summary": summary[:600],
            "votes": None,
            "topics": [],
        })
        if len(out) >= maxn:
            break
    return out


# Filler that appears in half the corpus and so discriminates nothing. Excluded
# from the relevance floor, never from the query itself.
_GENERIC = frozenset("""software paper papers method methods approach approaches
system systems model models using used based via novel new framework study
studies analysis towards toward real improved state art survey review
application applications results result""".split())


def _query_terms(query):
    """Query terms worth requiring: 3+ chars, not a stopword, not filler."""
    return [w.lower() for w in re.findall(r"[A-Za-z0-9][\w.+-]*", query)
            if len(w) >= 3 and w.lower() not in _STOPWORDS
            and w.lower() not in _GENERIC]


def _title_hit(title, terms):
    """How many distinct query terms appear in a paper's TITLE.

    The relevance floor for the semantic legs. OpenAlex, Semantic Scholar and
    HuggingFace all rank semantically, so "three-way merge software" matches
    every paper that merely says "software" and hands back the most-cited few
    (QUANTUM ESPRESSO, SExtractor) as though they were answers. Requiring a
    query term in the title is the cheap discriminator: an off-topic result has
    none, and a genuinely relevant paper nearly always has one. The count is
    also the strongest relevance signal the ranker has, ahead of citations.
    """
    low = " " + re.sub(r"[^a-z0-9]+", " ", (title or "").lower()) + " "
    return len({t for t in terms if (" " + t + " ") in low})


def _openalex_abstract(inv):
    """OpenAlex ships abstracts as an inverted index; rebuild reading order."""
    if not inv:
        return ""
    words = sorted((pos, w) for w, poss in inv.items() for pos in (poss or []))
    return re.sub(r"\s+", " ", " ".join(w for _, w in words)).strip()


def openalex_leg(query, maxn=8):
    """OpenAlex. Deepest index of the four — citations, topics and reconstructed
    abstracts — and it answers a polite User-Agent without throttling.

    Deliberately over-fetches: the results are relevance-filtered after the
    fact, so asking for exactly maxn would spend the whole budget on off-topic
    work the filter then discards.
    """
    url = (OPENALEX_WORKS + "?search=" + urllib.parse.quote(query)
           + "&per-page=%d&mailto=%s" % (min(maxn * 4, 50), CONTACT_MAIL))
    data = json.loads(fetch(url).decode("utf-8"))
    terms = _query_terms(query)
    out = []
    for r in (data.get("results") or []):
        title = re.sub(r"\s+", " ", r.get("title") or "").strip()
        hits = _title_hit(title, terms)
        if not hits:
            continue
        loc = r.get("primary_location") or {}
        doi = (r.get("doi") or "").replace("https://doi.org/", "") or None
        out.append({
            "source": "openalex",
            "title": title,
            # OpenAlex indexes preprints but almost never carries the arXiv id,
            # so cross-leg merging falls back to DOI and then to title.
            "arxiv_id": None,
            "doi": doi,
            "url": loc.get("landing_page_url") or r.get("doi") or r.get("id"),
            "published": str(r.get("publication_year") or ""),
            "summary": _openalex_abstract(r.get("abstract_inverted_index"))[:600],
            "votes": r.get("cited_by_count"),
            "topics": [t.get("display_name") for t in (r.get("topics") or [])][:3],
            "title_hits": hits,
        })
        if len(out) >= maxn:
            break
    return out


def s2_leg(query, maxn=8):
    """Semantic Scholar. The only leg that reports an ArXiv id for a paper first
    published at a non-preprint venue, so it bridges the bibliographic index
    and the preprint index.

    Over-fetches for the same reason as OpenAlex: relevance is filtered after
    the fact, and S2 caps a page at 100.
    """
    url = (S2_SEARCH + "?query=" + urllib.parse.quote(query)
           + "&limit=%d&fields=title,abstract,externalIds,year,citationCount"
           % min(maxn * 4, 100))
    data = json.loads(fetch(url).decode("utf-8"))
    terms = _query_terms(query)
    out = []
    for r in (data.get("data") or []):
        title = re.sub(r"\s+", " ", r.get("title") or "").strip()
        hits = _title_hit(title, terms)
        if not hits:
            continue
        ids = r.get("externalIds") or {}
        doi = (ids.get("DOI") or "").lower() or None
        out.append({
            "source": "s2",
            "title": title,
            "arxiv_id": _strip_ver(ids.get("ArXiv") or "") or None,
            "doi": doi,
            "url": ("https://doi.org/" + doi) if doi else r.get("paperId"),
            "published": str(r.get("year") or ""),
            "summary": re.sub(r"\s+", " ", r.get("abstract") or "").strip()[:600],
            "votes": r.get("citationCount"),
            "topics": [],
            "title_hits": hits,
        })
        if len(out) >= maxn:
            break
    return out


def plan_queries(query, max_tokens=MAX_QUERY_TOKENS, max_sub=MAX_SUBQUERIES):
    """Split an over-long query into a deterministic set of mixed-token queries.

    A long natural-language query dilutes every leg: the exact ones have to
    quote all of it (arXiv ANDs up to six terms and then returns almost
    nothing), and the semantic ones drift to the nearest topic rather than to
    the conjunction the user actually asked about. So terms are deduped and
    stopworded out, and only then — when more than max_tokens survive — is the
    query recombined.

    Recombined as PAIRWISE subsets, not contiguous windows: the terms that
    matter in a long question are rarely adjacent ("how does A interact with B
    under C" wants {A,B}, {A,C}, {B,C}). The full conjunction is always kept as
    one sub-query so precision is not traded away for recall.

    Fully deterministic — same query, same plan — so the leg cache, the
    cooldown state and the winner log all stay meaningful across runs.
    """
    terms, seen = [], set()
    for w in re.findall(r"[A-Za-z0-9][\w.+-]*", query):
        lw = w.lower()
        if len(lw) < 3 or lw in _STOPWORDS or lw in seen:
            continue
        seen.add(lw)
        terms.append(lw)
    if not terms:  # query was all stopwords or punctuation: nothing to mix
        return [query]
    if len(terms) <= max_tokens:
        return [" ".join(terms)]
    # Longer tokens are the rarer, more specific ones — a cheap deterministic
    # stand-in for IDF that needs no corpus to calibrate.
    head = sorted(terms, key=lambda t: (-len(t), terms.index(t)))[:2 * max_sub]
    subs = [" ".join(terms)]
    for i in range(len(head)):
        for j in range(i + 1, len(head)):
            subs.append(head[i] + " " + head[j])
            if len(subs) > max_sub:
                return subs
    return subs


LEGS = (("arxiv", arxiv_leg), ("alphaxiv", alphaxiv_leg), ("s2", s2_leg),
        ("openalex", openalex_leg), ("hf", hf_papers_leg))


def _read_cooldown(path):
    try:
        with open(path) as f:
            cd = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(cd, dict):
        return {}
    # The original format stored a bare epoch per leg; read that as one strike.
    return {k: (v if isinstance(v, dict) else {"until": v, "strikes": 1})
            for k, v in cd.items()}


def _write_cooldown(path, cd):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w") as f:
        json.dump(cd, f)


def _check_cooldown(cooldown_path, leg):
    """Return a reason string if the leg is cooling down, else None."""
    try:
        e = _read_cooldown(cooldown_path).get(leg) or {}
        until = e.get("until") or 0
        if time.time() < until:
            return "cooling until %s (strike %d)" % (
                time.strftime("%H:%M:%S", time.localtime(until)),
                e.get("strikes", 1))
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def _pace(leg, interval):
    """Wait until this leg's minimum interval has elapsed since its previous
    request completed, then let it issue one.

    Shared infrastructure is what earns an hours-long ban, so the pacing is
    enforced here rather than trusted to the caller. The slot is claimed under
    the lock and slept on outside it, so concurrent sub-queries queue up
    instead of all sleeping the same amount.
    """
    if not interval:
        return
    with _LEG_NEXT_LOCK:
        now = time.monotonic()
        wait = _LEG_NEXT_OK.get(leg, 0.0) - now
        if wait > 0:
            _LEG_NEXT_OK[leg] += interval
        else:
            _LEG_NEXT_OK[leg] = now + interval
    if wait > 0:
        time.sleep(wait)


def _pace_done(leg):
    """Record that this leg's request finished, starting its next interval.

    Measured from completion rather than from dispatch, so a slow response
    still costs a full interval — the reference client's accounting, and the
    stricter of the two readings of "one request every N seconds".
    """
    with _LEG_NEXT_LOCK:
        interval = LEG_MIN_INTERVAL.get(leg, 0.0)
        if interval:
            _LEG_NEXT_OK[leg] = time.monotonic() + interval


def _is_throttle(e):
    """Whether another attempt could plausibly fix this."""
    if isinstance(e, urllib.error.HTTPError):
        return e.code in (429, 500, 502, 503, 504)
    return isinstance(e, (TimeoutError, urllib.error.URLError,
                          ConnectionError, OSError))


def _set_cooldown(cooldown_path, leg):
    """Back a leg off, escalating on repeat offences.

    A flat five-minute wait is wrong for a host that bans for hours: it walks
    back into a live ban every five minutes and keeps it alive. Each 429
    multiplies the wait, so a host that has banned us is asked ever less often,
    up to a six-hour ceiling — and a single success clears the record.

    Called from worker threads, so the read-modify-write is under a lock: two
    legs throttling at once would otherwise lose one strike between them.
    """
    if not cooldown_path:
        return
    with _COOLDOWN_LOCK:
        try:
            cd = _read_cooldown(cooldown_path)
            strikes = int((cd.get(leg) or {}).get("strikes", 0)) + 1
            delay = min(COOLDOWN_BASE_SECONDS * COOLDOWN_BACKOFF ** (strikes - 1),
                        COOLDOWN_MAX_SECONDS)
            cd[leg] = {"until": time.time() + delay, "strikes": strikes}
            _write_cooldown(cooldown_path, cd)
        except (OSError, ValueError, TypeError):
            pass  # cooldown is advisory, never a failure


def _clear_cooldown(cooldown_path, leg):
    """A leg that answered has paid its debt; the next 429 starts from the floor."""
    if not cooldown_path:
        return
    with _COOLDOWN_LOCK:
        try:
            cd = _read_cooldown(cooldown_path)
            if cd.pop(leg, None) is not None:
                _write_cooldown(cooldown_path, cd)
        except (OSError, ValueError):
            pass


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
    papers: ranked, deduped list of at most maxn (see rank()).
    A leg that recently HTTP-429'd is skipped until its cooldown expires.
    """
    order = _leg_order(lead_with_winner, winners_log)
    legfn = dict(LEGS)
    subs = plan_queries(query)
    papers, stats = [], {}
    todo = {}
    for name in order:
        why = _check_cooldown(cooldown_path, name) if cooldown_path else None
        if why:
            stats[name] = {"ok": False, "latency_ms": 0.0, "n": 0,
                           "error": "429 " + why}
        else:
            todo[name] = legfn[name]
    jobs = [((name, sub),
             lambda n=name, s=sub: _timed_leg(n, todo[n], s, maxn, timeout,
                                              cooldown_path))
            for name in todo
            for sub in subs[:LEG_MAX_SUBQUERIES.get(name, len(subs))]]
    # Sufficiency, not exhaustion, decides the race: see _race_legs.
    deadline = time.monotonic() + RACE_DEADLINE_SECONDS
    for (name, sub), items, ms, err in _race_legs(jobs, deadline, maxn):
        prev = stats.get(name)
        if err is None:
            for p in items:
                p["query"] = query
                p["subquery"] = sub
                papers.append(p)
            # A leg is as fast as its best sub-query and as big as their union.
            seen_ok = bool(prev and prev.get("ok"))
            stats[name] = {"ok": True,
                           "latency_ms": round(min(prev["latency_ms"], ms)
                                               if seen_ok else ms, 1),
                           "n": (prev["n"] if seen_ok else 0) + len(items)}
        else:
            if not prev:  # a leg is only reported dead before anything worked
                stats[name] = {"ok": False, "latency_ms": round(ms, 1), "n": 0,
                               "error": str(err)[:120]}
                papers.append({"query": query, "source": name, "error": str(err)[:120]})
        s = stats[name]
        _log_winner(winners_log, name, query, s["latency_ms"], s["ok"], s["n"])
    # maxn is the caller's budget, not a suggestion: rank() defaulted to its own
    # 8 and quietly over-delivered, so a --maxn 6 run printed eight papers.
    return rank(papers, topn=maxn), stats


def _race_legs(jobs, deadline, maxn):
    """Run legs concurrently on DAEMON threads, returning (key, items, ms, err)
    for every leg that finished in time.

    ThreadPoolExecutor is the wrong tool for abandoning a leg. Its workers are
    non-daemon and an interpreter-exit hook joins them, so breaking out of the
    wait loop still blocks until the slowest leg finishes: a throttled arXiv leg
    spending 3s of pacing plus a failed fetch, three times, stretched a 2s
    answer into 29s no matter how early the loop stopped waiting. A race exists
    so that a leg which cannot answer costs nothing, and that requires being
    able to leave it behind — only a daemon thread can be left behind.

    Two windows, both absolute: the deadline governs until maxn papers are in
    hand, after which SETTLE_SECONDS is all a straggler gets before it is
    abandoned. The 429 ladder is unaffected because _timed_leg writes its own
    record on the first throttle, while the thread is provably still alive.
    """
    out = queue.Queue()

    def wrap(key, job):
        try:
            items, ms, err = job()
        except BaseException as exc:  # noqa: BLE001
            items, ms, err = None, 0.0, exc
        out.put((key, items, ms, err))

    for key, job in jobs:
        threading.Thread(target=wrap, args=(key, job), daemon=True,
                         name="leg:%s" % (key[0],)).start()
    results, found, settle_at = [], 0, None
    while True:
        # Both windows are absolute instants, latched once. Recomputing the
        # settle budget from the clock on every pass would push it forward with
        # itself and the loop would never expire — which is exactly what it did,
        # hanging the whole query on a leg that could not answer.
        if found >= maxn:
            if settle_at is None:
                settle_at = time.monotonic() + SETTLE_SECONDS
            budget = settle_at
        else:
            settle_at = None
            budget = deadline
        left = budget - time.monotonic()
        if left <= 0:
            return results
        try:
            results.append(out.get(timeout=min(0.5, left)))
        except queue.Empty:
            continue
        found += len(results[-1][1] or ())


def _timed_leg(leg, fn, query, maxn, timeout, cooldown_path=None):
    """Run one leg under its own rate-limit policy, always returning the real
    elapsed time — including when it fails.

    Recording a failure at the full timeout budget (rather than the time it
    actually took) is what made a 0.36s throttle read as a 10s timeout, which
    sent the diagnosis down the wrong path entirely.

    Retry and pacing live here rather than inside each leg, so the policy is
    stated once per leg and every attempt — the first and every retry — passes
    through the same limiter. That is the shape of arXiv's own reference
    client (arxiv 4.0.1 `Client._parse_feed`): the delay is enforced between
    attempts, not merely between queries, which is what makes retrying
    compliant at all rather than a way to earn a ban.

    A 429 is written to disk HERE, on the first one, while this thread is
    demonstrably still alive. The race ends on sufficiency, so a throttled leg
    is routinely still retrying when the main thread returns and the one-shot
    CLI exits — a record written after the race would never reach the disk,
    and an unrecorded 429 is how a 60-second cooldown turns back into a
    six-hour ban. One invocation counts as one offence, so the strike is
    recorded once and the retries are still allowed to succeed.
    """
    t0 = time.perf_counter()
    interval = LEG_MIN_INTERVAL.get(leg, 0.0)
    attempts = LEG_MAX_RETRIES.get(leg, MAX_RETRIES) + 1
    last = None
    logged = False
    for attempt in range(attempts):
        _pace(leg, interval)
        try:
            items = fn(query, maxn)
        except Exception as e:
            last = e
            _pace_done(leg)
            if not logged and isinstance(e, urllib.error.HTTPError) and e.code == 429:
                _set_cooldown(cooldown_path, leg)
                logged = True
            if attempt == attempts - 1 or not _is_throttle(e):
                return None, (time.perf_counter() - t0) * 1000, e
        else:
            _pace_done(leg)
            if not logged and cooldown_path:
                _clear_cooldown(cooldown_path, leg)
            return items, (time.perf_counter() - t0) * 1000, None
    return None, (time.perf_counter() - t0) * 1000, last


def _dedupe_key(p):
    """Cross-leg identity. Only the preprint-backed legs guarantee an arXiv id,
    so a bibliographic paper dedupes on its DOI and, failing that, its title."""
    if p.get("arxiv_id"):
        return "arx:" + p["arxiv_id"].lower()
    if p.get("doi"):
        return "doi:" + p["doi"].lower()
    t = re.sub(r"[^a-z0-9]+", " ", (p.get("title") or "").lower()).strip()
    return "ttl:" + t if t else ""


def rank(papers, topn=8):
    """Dedupe by cross-leg identity, then rank by relevance before popularity.

    Sort order is least-significant first: recency, then community weight, then
    how many query terms the title actually matched. Citations are a measure of
    fame, not of fit, and letting them lead is what put a 1995 astro-physics
    paper at the top of a query about merge algorithms.
    """
    seen, uniq = set(), []
    for p in papers:
        if "error" in p:
            continue
        key = _dedupe_key(p)
        if not key or key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    uniq.sort(key=lambda p: (p.get("published") or ""), reverse=True)
    uniq.sort(key=lambda p: (p.get("votes") or 0), reverse=True)
    uniq.sort(key=lambda p: (p.get("title_hits") or 0), reverse=True)
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
