#!/usr/bin/env python3
"""Conservative duplicate detector for Twitter morning research.

Goal: avoid re-posting the same link/topic when the URL differs (HN item vs GitHub vs blog, tracking params, etc.).

Rules (conservative):
- DUPLICATE if canonicalized URL exactly matches any recentPosts[].link canonical.
- Else DUPLICATE if >=2 keyword tokens match between candidate (url+title) and any recent post (text+link),
  AND at least one matched token is "strong" (not generic).

Exit codes:
- 0 => duplicate (prints reason)
- 1 => not duplicate
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))
from db import get_conn, get_recent_posts, kv_get_json

KV_RECENT_POSTS = "twitter:recent_posts"

STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "built",
    "by",
    "can",
    "for",
    "from",
    "get",
    "how",
    "i",
    "if",
    "in",
    "is",
    "it",
    "its",
    "ma",
    "make",
    "made",
    "no",
    "of",
    "on",
    "or",
    "our",
    "show",
    "showhn",
    "hn",
    "that",
    "the",
    "this",
    "to",
    "up",
    "using",
    "via",
    "we",
    "what",
    "when",
    "with",
    "you",
    "your",
}

# Tokens that are too broad to count as the only shared signal.
GENERIC = {
    "cloud",
    "pricing",
    "price",
    "cost",
    "costs",
    "security",
    "open",
    "source",
    "opensource",
    "ai",
    "agent",
    "agents",
    "browser",
    "tool",
    "tools",
    "python",
    "rust",
    "kubernetes",
    "k8s",
    "aws",
    "gcp",
    "azure",
    "gpu",
    "mcp",
    "api",
}

SHORT_ALLOW = {"aws", "gcp", "h3", "mcp", "gpu", "api", "os"}


def canonicalize_url(url: str) -> str:
    url = url.strip()
    if not url:
        return ""
    # strip query + fragment
    url = re.sub(r"[?#].*$", "", url)
    # strip trailing slash
    url = re.sub(r"/$", "", url)

    # normalize GitHub URLs to org/repo
    try:
        u = urlparse(url)
    except Exception:
        return url

    host = (u.netloc or "").lower()
    if host.endswith("github.com"):
        parts = [p for p in u.path.split("/") if p]
        if len(parts) >= 2:
            return f"https://github.com/{parts[0]}/{parts[1]}"

    return url


_token_re = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> set[str]:
    text = (text or "").lower()
    toks = set(_token_re.findall(text))
    out: set[str] = set()
    for t in toks:
        if t in STOPWORDS:
            continue
        if len(t) >= 4 or t in SHORT_ALLOW:
            out.add(t)
    return out


def url_tokens(url: str) -> set[str]:
    url = canonicalize_url(url)
    if not url:
        return set()
    try:
        u = urlparse(url)
    except Exception:
        return tokenize(url)

    host = (u.netloc or "").lower()
    parts = [p for p in u.path.split("/") if p]

    # GitHub: org/repo tokens are very strong
    if host.endswith("github.com") and len(parts) >= 2:
        org, repo = parts[0].lower(), parts[1].lower()
        return {org, repo} | tokenize(repo) | tokenize(org)

    # Otherwise: use path segments + domain tokens
    host_bits = [b for b in re.split(r"[.\-]", host) if b and b not in {"www", "com", "org", "io", "net", "app"}]
    out = set(host_bits)
    for p in parts[-4:]:
        out |= tokenize(p)
    return out


@dataclass
class RecentPost:
    date: str
    type: str
    text: str
    link: str | None

    @property
    def canonical_link(self) -> str:
        return canonicalize_url(self.link or "")

    @property
    def tokens(self) -> set[str]:
        return tokenize(self.text or "") | url_tokens(self.link or "")


def is_duplicate(candidate_url: str, candidate_title: str, recent: list[RecentPost]) -> tuple[bool, str]:
    c_url_can = canonicalize_url(candidate_url)

    # 1) Exact canonical URL match
    for rp in recent:
        if rp.canonical_link and c_url_can and rp.canonical_link == c_url_can:
            return True, f"canonical link match: {c_url_can} (recent {rp.date})"

    # 2) Conservative keyword match
    c_tokens = url_tokens(candidate_url) | tokenize(candidate_title)
    if not c_tokens:
        return False, "no candidate tokens"

    for rp in recent:
        inter = c_tokens & rp.tokens
        if len(inter) < 2:
            continue

        # require at least one strong token (not generic)
        strong = [t for t in inter if t not in GENERIC]
        if strong:
            return True, f"keyword match ({sorted(inter)[:6]}) vs recent {rp.date}"

    return False, "no match"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--title", default="")
    ap.add_argument("--max", type=int, default=200, help="max recentPosts entries to consider")
    args = ap.parse_args()
    with get_conn() as conn:
        kv_recent = kv_get_json(conn, KV_RECENT_POSTS, [])
        if not isinstance(kv_recent, list):
            kv_recent = []
        rows = get_recent_posts(conn, days=90, limit=args.max)
    rp_raw = list(kv_recent)[-args.max :]
    for row in rows:
        rp_raw.append(
            {
                "date": str(row.get("posted_at", ""))[:10],
                "type": row.get("type", ""),
                "text": row.get("text", ""),
                "link": row.get("url"),
            }
        )
    if len(rp_raw) > args.max:
        rp_raw = rp_raw[-args.max :]

    recent: list[RecentPost] = []
    for r in rp_raw:
        recent.append(
            RecentPost(
                date=str(r.get("date", "")),
                type=str(r.get("type", "")),
                text=str(r.get("text", "")),
                link=r.get("link"),
            )
        )

    dup, reason = is_duplicate(args.url, args.title, recent)
    if dup:
        print(f"DUPLICATE: {reason}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
