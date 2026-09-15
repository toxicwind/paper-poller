#!/usr/bin/env python3
"""Minimal stdlib-only arXiv + alphaXiv racer. Runs on awrawr-pc via the
bridge (cell egress is down). Borrowed endpoint shapes from
emergent-enrich route.py; fail-fast per-leg timeouts (HFT-like)."""
import concurrent.futures as cf
import json
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

UA = {"User-Agent": "shingle-paper-race/1.0"}
LEG_TIMEOUT = 12

QUERIES = [
    "hedged requests tail latency redundant distributed systems",
    "LLM inference serving benchmark latency throughput",
    "multi-agent systems coordination consensus protocols",
]


def fetch(url, timeout=LEG_TIMEOUT):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def arxiv_leg(query, maxn=8):
    q = "all:" + "+AND+".join(urllib.parse.quote(w) for w in query.split()[:6])
    url = ("https://export.arxiv.org/api/query?search_query=%s"
           "&start=0&max_results=%d&sortBy=relevance&sortOrder=descending" % (q, maxn))
    data = fetch(url)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out = []
    for e in ET.fromstring(data).findall("a:entry", ns):
        aid = (e.findtext("a:id", default="", namespaces=ns) or "").rsplit("/abs/", 1)[-1]
        out.append({
            "source": "arxiv",
            "title": re.sub(r"\s+", " ", e.findtext("a:title", default="", namespaces=ns) or "").strip(),
            "arxiv_id": aid,
            "url": "https://arxiv.org/abs/" + aid,
            "published": (e.findtext("a:published", default="", namespaces=ns) or "")[:10],
        })
    return out


def alphaxiv_leg(query, maxn=8):
    url = "https://api.alphaxiv.org/v1/search/paper?q=" + urllib.parse.quote(query)
    data = json.loads(fetch(url).decode("utf-8"))
    items = data if isinstance(data, list) else []
    out = []
    for r in items[:maxn]:
        aid = re.sub(r"v\d+$", "", r.get("universal_paper_id") or r.get("canonical_id") or "")
        m = r.get("metrics") or {}
        out.append({
            "source": "alphaxiv",
            "title": r.get("title"),
            "arxiv_id": aid or None,
            "url": "https://arxiv.org/abs/" + aid if aid else None,
            "published": (r.get("publication_date") or "")[:10],
            "votes": m.get("total_votes"),
            "topics": r.get("topics") or [],
        })
    return out


def one_query(query):
    papers = []
    with cf.ThreadPoolExecutor(max_workers=2) as ex:
        futs = {ex.submit(arxiv_leg, query): "arxiv",
                ex.submit(alphaxiv_leg, query): "alphaxiv"}
        for f in cf.as_completed(futs, timeout=LEG_TIMEOUT + 5):
            try:
                for p in f.result():
                    p["query"] = query
                    papers.append(p)
            except Exception as e:
                papers.append({"query": query, "source": futs[f],
                               "error": str(e)[:120]})
    return papers


def main():
    all_papers = []
    with cf.ThreadPoolExecutor(max_workers=3) as ex:
        for papers in ex.map(one_query, QUERIES):
            all_papers.extend(papers)
    for p in all_papers:
        print(json.dumps(p), flush=True)
    ok = [p for p in all_papers if "error" not in p]
    print("SUMMARY papers=%d queries=%d" % (len(ok), len(QUERIES)), file=sys.stderr)


if __name__ == "__main__":
    main()
