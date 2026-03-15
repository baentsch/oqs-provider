#!/usr/bin/env python3
"""
================================================================================
  GitHub Project Activity Analyzer
  Multi-Repo Edition · Contribution Indicators · Interactive HTML Report
================================================================================

Tracks comments, PRs, reviews, and discussions across one or more GitHub
repositories and produces a self-contained HTML visualization — no server
needed, everything runs in your browser.

────────────────────────────────────────────────────────────────────────────────
  QUICK START
────────────────────────────────────────────────────────────────────────────────

  1.  Install the only dependency:

        pip install requests

  2.  Get a GitHub Personal Access Token (see section below).

  3.  Run the script:

        python gh_activity.py owner/repo --token YOUR_TOKEN

  4.  Open the generated HTML file in any browser:

        open owner_repo_activity.html          # macOS
        xdg-open owner_repo_activity.html      # Linux
        start owner_repo_activity.html         # Windows

────────────────────────────────────────────────────────────────────────────────
  HOW TO GET A GITHUB PERSONAL ACCESS TOKEN (PAT)
────────────────────────────────────────────────────────────────────────────────

  Option A — Fine-grained token (recommended, least privilege):
  ─────────────────────────────────────────────────────────────
  1.  Go to: https://github.com/settings/tokens?type=beta
  2.  Click "Generate new token".
  3.  Give it a name (e.g. "gh-activity-analyzer").
  4.  Under "Repository access", choose:
        • "Public Repositories (read-only)"  — for public repos
        • or select specific repos           — for private repos
  5.  Under "Permissions → Repository permissions", enable:
        • Issues          → Read-only
        • Pull requests   → Read-only
        • Discussions     → Read-only   (if the repo uses Discussions)
  6.  Click "Generate token" and copy the token string.

  Option B — Classic token (simpler, broader scope):
  ───────────────────────────────────────────────────
  1.  Go to: https://github.com/settings/tokens
  2.  Click "Generate new token (classic)".
  3.  Give it a name and set an expiry (90 days recommended).
  4.  Tick the scope:
        • "public_repo"   — for public repositories only
        • "repo"          — required for private repositories
  5.  Click "Generate token" and copy the token string.

  Storing the token safely:
  ─────────────────────────
  Do NOT paste the token directly into shell history. Instead, set it as an
  environment variable and reference it via $GH_TOKEN:

    Linux / macOS (add to ~/.bashrc or ~/.zshrc for persistence):
      export GH_TOKEN="ghp_xxxxxxxxxxxxxxxxxxxx"

    Windows (PowerShell):
      $env:GH_TOKEN = "ghp_xxxxxxxxxxxxxxxxxxxx"

  The script reads GH_TOKEN automatically if --token is not provided.

────────────────────────────────────────────────────────────────────────────────
  USAGE EXAMPLES
────────────────────────────────────────────────────────────────────────────────

  # Single repository (token from environment variable)
  export GH_TOKEN="ghp_xxxxxxxxxxxxxxxxxxxx"
  python gh_activity.py open-quantum-safe/liboqs

  # Single repository with explicit token
  python gh_activity.py open-quantum-safe/liboqs --token $GH_TOKEN

  # Limit to data from a specific month onwards (faster + focused)
  python gh_activity.py open-quantum-safe/liboqs --token $GH_TOKEN --since 2022-01

  # Multiple repositories → one combined report with cross-repo comparison
  python gh_activity.py open-quantum-safe/liboqs open-quantum-safe/oqs-provider \\
      --token $GH_TOKEN --since 2022-01

  # Custom output filename
  python gh_activity.py facebook/react facebook/relay facebook/jest \\
      --token $GH_TOKEN --out react_ecosystem.html

  # Force re-fetch (ignore local cache)
  python gh_activity.py open-quantum-safe/liboqs --token $GH_TOKEN --no-cache

────────────────────────────────────────────────────────────────────────────────
  ALL OPTIONS
────────────────────────────────────────────────────────────────────────────────

  repos          One or more owner/repo strings (required, positional)
  --token TOKEN  GitHub PAT. Falls back to $GH_TOKEN environment variable.
  --since YYYY-MM  Only include data from this month onwards.
  --out FILE     Output HTML filename. Auto-generated if omitted.
  --no-cache     Re-fetch all data, ignoring any cached .json files.

────────────────────────────────────────────────────────────────────────────────
  CACHING & PERFORMANCE
────────────────────────────────────────────────────────────────────────────────

  After the first run, raw events are cached in:
    .gh_cache_<owner>_<repo>.json   (one file per repository)

  Subsequent runs load from cache instantly. The --since filter is applied
  to cached data too, so you can narrow the time window without re-fetching.

  Large repositories (thousands of PRs) can take 30–60 minutes on a first
  fetch due to GitHub API rate limits (5,000 req/hour for authenticated users).
  The script waits and retries automatically when a rate limit is hit.

  After upgrading from an older version that did not track PR/review events,
  run once with --no-cache to rebuild the cache with the full event set.

================================================================================
"""

import os
import sys
import re
import json
import argparse
import time
from datetime import datetime, timezone
from collections import defaultdict
from pathlib import Path

try:
    import requests
except ImportError:
    print("Missing dependency: pip install requests")
    sys.exit(1)


# ─── Event-type taxonomy ──────────────────────────────────────────────────────
#
# Each event is stored as one of these kind strings.
# They are grouped into categories for the UI filter toggles.
#
# COMMENT group   – discussion / issue / PR / commit text contributions
# PR group        – pull-request lifecycle events (opened, merged, closed)
# REVIEW group    – code-review verdicts and inline comments

CATEGORIES = {
    "comments": {
        "label": "Comments",
        "color": "#47d4ff",
        "kinds": [
            "issue_comment",
            "pr_review_comment",
            "discussion",
            "discussion_comment",
            "discussion_reply",
            "commit_comment",
        ],
    },
    "prs": {
        "label": "Pull Requests",
        "color": "#e8ff47",
        "kinds": [
            "pr_opened",
            "pr_merged",
            "pr_closed",      # closed without merge
        ],
    },
    "reviews": {
        "label": "Reviews",
        "color": "#a78bfa",
        "kinds": [
            "review_approved",
            "review_changes_requested",
            "review_commented",   # review submitted as COMMENT (no verdict)
        ],
    },
}

# Flat kind → category mapping for quick lookup
KIND_TO_CAT = {
    kind: cat
    for cat, meta in CATEGORIES.items()
    for kind in meta["kinds"]
}


# ─── GitHub API ───────────────────────────────────────────────────────────────

class GitHubClient:
    def __init__(self, token: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        self.base = "https://api.github.com"
        self._rate_remaining = 5000

    def _get(self, url, params=None):
        while True:
            r = self.session.get(url, params=params)
            if r.status_code == 403 and "rate limit" in r.text.lower():
                reset = int(r.headers.get("X-RateLimit-Reset", time.time() + 60))
                wait = max(reset - int(time.time()), 1)
                print(f"  ⏳ Rate limit — waiting {wait}s …")
                time.sleep(wait)
                continue
            r.raise_for_status()
            self._rate_remaining = int(r.headers.get("X-RateLimit-Remaining", 0))
            return r

    def paginate(self, url, params=None):
        params = dict(params or {})
        params.setdefault("per_page", 100)
        while url:
            r = self._get(url, params)
            data = r.json()
            yield from (data if isinstance(data, list) else data.get("items", []))
            url = r.links.get("next", {}).get("url")
            params = {}

    def graphql(self, query: str, variables: dict = None):
        r = self.session.post(
            "https://api.github.com/graphql",
            json={"query": query, "variables": variables or {}},
        )
        r.raise_for_status()
        result = r.json()
        if "errors" in result:
            raise RuntimeError(f"GraphQL errors: {result['errors']}")
        return result["data"]


# ─── Data Collection ──────────────────────────────────────────────────────────

def collect(owner: str, repo: str, client: GitHubClient, since: str = None):
    """
    Returns list of (author, month_YYYY-MM, kind, repo_full, ref, chars).

      ref   — short human-readable reference, e.g. "#1234", "PR#56",
               "D#7" (discussion), "c:abc1234" (commit sha prefix).
      chars — character count of the comment/review body text, or None for
               events that carry no prose body (pr_opened, pr_merged, pr_closed).
               Stripped of leading/trailing whitespace before counting.

    Comment body text is present in every GitHub API response that returns a
    comment object, so collecting it requires no additional API calls.
    """
    repo_full = f"{owner}/{repo}"
    events = []

    def body_len(text) -> int:
        """
        Return the character count of ORIGINAL new prose only.

        Strips before counting:
          - Block quotes  (lines starting with >)
          - Fenced code blocks  (``` ... ``` and ~~~ ... ~~~)
          - Inline images  ![alt](url)
          - HTML comments  <!-- ... --> (single- and multi-line)
          - <details>...</details> collapse blocks
          - Residual HTML tags

        Preserves:
          - Inline code  `foo`  (author chose to write it)
          - Checklists, @mentions, issue/PR references
          - All other original prose
        """
        if not text:
            return 0

        lines = text.splitlines()
        result = []
        in_code_block   = False
        in_html_comment = False
        in_details      = False

        for line in lines:
            stripped = line.strip()

            # ── Multi-line HTML comments ────────────────────────────────────
            if not in_html_comment and '<!--' in line:
                pre  = line[:line.index('<!--')]
                rest = line[line.index('<!--'):]
                if '-->' in rest:
                    # Inline comment on a single line — excise it
                    line     = re.sub(r'<!--.*?-->', '', line)
                    stripped = line.strip()
                    if not stripped:
                        continue
                else:
                    in_html_comment = True
                    pre_text = pre.strip()
                    if pre_text:
                        result.append(pre_text)
                    continue
            if in_html_comment:
                if '-->' in line:
                    in_html_comment = False
                    post = line[line.index('-->')+3:].strip()
                    if post:
                        result.append(post)
                continue

            # ── Fenced code blocks (``` or ~~~) ─────────────────────────────
            if re.match(r'^\s*(`{3,}|~{3,})', stripped):
                in_code_block = not in_code_block
                continue
            if in_code_block:
                continue

            # ── <details>…</details> collapse blocks ────────────────────────
            if re.match(r'^\s*<details', line, re.IGNORECASE):
                in_details = True
                continue
            if re.match(r'^\s*</details>', line, re.IGNORECASE):
                in_details = False
                continue
            if in_details:
                continue

            # ── Block quotes ────────────────────────────────────────────────
            if stripped.startswith('>'):
                continue

            # ── Inline images ───────────────────────────────────────────────
            line = re.sub(r'!\[.*?\]\(.*?\)', '', line)

            # ── Residual HTML tags (<summary>, <b>, etc.) ───────────────────
            line = re.sub(r'<[^>]+>', '', line)

            result.append(line)

        joined = '\n'.join(result)
        joined = re.sub(r'\n{3,}', '\n\n', joined)
        return len(joined.strip())

    def record(author, ts, kind, ref="", chars=None):
        if not author or author in ("ghost", ""):
            return
        if since and ts[:7] < since:
            return
        events.append((author, ts[:7], kind, repo_full, ref, chars))

    def issue_ref(url: str) -> str:
        """Extract '#<n>' from an issue_url like …/issues/123."""
        try:
            return "#" + url.rstrip("/").rsplit("/", 1)[-1]
        except Exception:
            return ""

    def pr_ref(n: int) -> str:
        return f"PR#{n}"

    # ── Comments on issues & PRs ──────────────────────────────────────────────
    print(f"  [{repo_full}] 📥 Issue/PR comments …")
    p = {"sort": "created", "direction": "asc"}
    if since:
        p["since"] = since + "-01T00:00:00Z"
    for c in client.paginate(f"{client.base}/repos/{owner}/{repo}/issues/comments", p):
        author = (c.get("user") or {}).get("login", "")
        ref = issue_ref(c.get("issue_url", ""))
        record(author, c["created_at"], "issue_comment", ref, body_len(c.get("body")))

    # ── Inline PR review comments ─────────────────────────────────────────────
    print(f"  [{repo_full}] 📥 PR review comments …")
    p = {"sort": "created", "direction": "asc"}
    if since:
        p["since"] = since + "-01T00:00:00Z"
    for c in client.paginate(f"{client.base}/repos/{owner}/{repo}/pulls/comments", p):
        author = (c.get("user") or {}).get("login", "")
        ref = issue_ref(c.get("pull_request_url", ""))
        record(author, c["created_at"], "pr_review_comment", ref, body_len(c.get("body")))

    # ── Commit comments ───────────────────────────────────────────────────────
    print(f"  [{repo_full}] 📥 Commit comments …")
    for c in client.paginate(f"{client.base}/repos/{owner}/{repo}/comments"):
        author = (c.get("user") or {}).get("login", "")
        sha = (c.get("commit_id") or "")[:7]
        ref = f"c:{sha}" if sha else ""
        record(author, c["created_at"], "commit_comment", ref, body_len(c.get("body")))

    # ── Pull Requests + Reviews ───────────────────────────────────────────────
    print(f"  [{repo_full}] 📥 Pull requests + reviews …")
    pr_params = {"state": "all", "sort": "created", "direction": "asc"}
    pr_count = 0
    review_state_map = {
        "APPROVED":           "review_approved",
        "CHANGES_REQUESTED":  "review_changes_requested",
        "COMMENTED":          "review_commented",
    }
    for pr in client.paginate(f"{client.base}/repos/{owner}/{repo}/pulls", pr_params):
        pr_author  = (pr.get("user") or {}).get("login", "")
        pr_created = pr["created_at"]
        pr_num     = pr["number"]
        ref        = pr_ref(pr_num)

        if since and pr_created[:7] < since and pr["state"] == "closed":
            continue

        # PR opened
        record(pr_author, pr_created, "pr_opened", ref)

        # PR merged / closed
        if pr.get("merged_at"):
            merged_by = (pr.get("merged_by") or {}).get("login", "") or pr_author
            record(merged_by, pr["merged_at"], "pr_merged", ref)
        elif pr["state"] == "closed" and pr.get("closed_at"):
            record(pr_author, pr["closed_at"], "pr_closed", ref)

        # Reviews on this PR — body is the top-level review comment (summary text)
        rev_url = f"{client.base}/repos/{owner}/{repo}/pulls/{pr_num}/reviews"
        for rev in client.paginate(rev_url):
            state  = rev.get("state", "")
            kind   = review_state_map.get(state)
            if kind:
                reviewer  = (rev.get("user") or {}).get("login", "")
                submitted = rev.get("submitted_at") or pr_created
                # body = the review summary text; may be empty for approve-only reviews
                record(reviewer, submitted, kind, ref, body_len(rev.get("body")))

        pr_count += 1
        if pr_count % 50 == 0:
            print(f"    … {pr_count} PRs (rate left: {client._rate_remaining})")

    # ── Discussions (GraphQL) ─────────────────────────────────────────────────
    print(f"  [{repo_full}] 📥 Discussions …")
    disc_q = """
    query($owner:String!, $repo:String!, $cursor:String) {
      repository(owner:$owner, name:$repo) {
        discussions(first:50, after:$cursor) {
          pageInfo { endCursor hasNextPage }
          nodes {
            number author { login } createdAt bodyText
            comments(first:100) {
              nodes {
                author { login } createdAt bodyText
                replies(first:50) {
                  nodes { author { login } createdAt bodyText }
                }
              }
            }
          }
        }
      }
    }
    """
    try:
        cursor = None
        while True:
            data = client.graphql(disc_q, {"owner": owner, "repo": repo, "cursor": cursor})
            dd = data["repository"]["discussions"]
            for d in dd["nodes"]:
                d_ref  = f"D#{d['number']}"
                author = (d.get("author") or {}).get("login", "")
                record(author, d["createdAt"], "discussion", d_ref,
                       body_len(d.get("bodyText")))
                for c in d["comments"]["nodes"]:
                    ca = (c.get("author") or {}).get("login", "")
                    record(ca, c["createdAt"], "discussion_comment", d_ref,
                           body_len(c.get("bodyText")))
                    for r in c["replies"]["nodes"]:
                        ra = (r.get("author") or {}).get("login", "")
                        record(ra, r["createdAt"], "discussion_reply", d_ref,
                               body_len(r.get("bodyText")))
            if not dd["pageInfo"]["hasNextPage"]:
                break
            cursor = dd["pageInfo"]["endCursor"]
    except Exception as e:
        print(f"    ⚠ Discussions not available: {e}")

    return events


# ─── Aggregation ─────────────────────────────────────────────────────────────

def aggregate_multi(all_events, repos):
    """
    Aggregate events into all structures needed by the HTML.

    Key addition vs. previous version:
      raw_events_by_repo  — {repo: [[author, month, kind], …]}
        Used by the HTML to recompute everything client-side when filters change.
    """
    by_contributor  = defaultdict(lambda: defaultdict(int))
    by_repo         = defaultdict(lambda: defaultdict(int))
    by_cr           = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    totals          = defaultdict(int)
    by_type         = defaultdict(lambda: defaultdict(int))
    months_set      = set()
    contrib_set     = set()

    # raw events per repo for client-side filtering
    raw_by_repo = defaultdict(list)

    for event in all_events:
        # Support old 4-tuple / 5-tuple cache entries and new 6-tuple format
        if len(event) == 6:
            author, month, kind, repo_full, ref, chars = event
        elif len(event) == 5:
            author, month, kind, repo_full, ref = event
            chars = None
        else:
            author, month, kind, repo_full = event
            ref = ""
            chars = None

        by_contributor[author][month]     += 1
        by_repo[repo_full][month]         += 1
        by_cr[author][repo_full][month]   += 1
        totals[month]                     += 1
        by_type[month][kind]              += 1
        months_set.add(month)
        contrib_set.add(author)
        # Store [author, month, kind, ref, chars] — repo is the dict key
        raw_by_repo[repo_full].append([author, month, kind, ref, chars])

    months = sorted(months_set)
    contributors = sorted(contrib_set,
        key=lambda c: sum(by_contributor[c].values()), reverse=True)

    per_repo_contributors = {
        r: sorted(
            {e[0] for e in all_events if e[3] == r},
            key=lambda c: sum(by_cr[c][r].values()), reverse=True
        )
        for r in repos
    }

    return {
        "months":       months,
        "contributors": contributors,
        "repos":        repos,
        "categories":   CATEGORIES,
        "kind_to_cat":  KIND_TO_CAT,
        # Pre-aggregated (used as baseline / fallback)
        "by_contributor": {c: dict(v) for c, v in by_contributor.items()},
        "by_repo":        {r: dict(v) for r, v in by_repo.items()},
        "by_contributor_repo": {
            c: {r: dict(mv) for r, mv in rv.items()}
            for c, rv in by_cr.items()
        },
        "totals":   dict(totals),
        "by_type":  {m: dict(v) for m, v in by_type.items()},
        "per_repo_contributors": per_repo_contributors,
        # Raw events for client-side filter recalculation
        "raw_by_repo": {r: v for r, v in raw_by_repo.items()},
    }


# ─── Cache helpers ────────────────────────────────────────────────────────────

def cache_path(owner, repo):
    return Path(f".gh_cache_{owner}_{repo}.json")

def load_cache(owner, repo):
    p = cache_path(owner, repo)
    if p.exists():
        with open(p) as f:
            return json.load(f)
    return None

def save_cache(owner, repo, events):
    with open(cache_path(owner, repo), "w") as f:
        json.dump(events, f)


# ─── HTML ─────────────────────────────────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{TITLE}} · Activity</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
@import url('https://fonts.googleapis.com/css2?family=Syne:wght@400;700;800&family=JetBrains+Mono:wght@400;600&display=swap');

:root {
  --bg:      #0c0d10;
  --surface: #14161b;
  --surf2:   #1c1f27;
  --border:  #262932;
  --accent:  #e8ff47;
  --text:    #e8eaf0;
  --muted:   #6b7280;
  --fh:      'Syne', sans-serif;
  --fm:      'JetBrains Mono', monospace;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--fh);min-height:100vh;overflow-x:hidden}

/* ── Header ── */
header{
  padding:2rem 3rem 1.2rem;border-bottom:1px solid var(--border);
  display:flex;align-items:flex-start;justify-content:space-between;gap:2rem;flex-wrap:wrap;
}
.h-title{font-size:clamp(1.2rem,2vw,1.9rem);font-weight:800;letter-spacing:-.04em;line-height:1.2}
.h-title .hl{color:var(--accent)}
.meta{font-family:var(--fm);font-size:.65rem;color:var(--muted);margin-top:.35rem;letter-spacing:.05em}
.stats-row{display:flex;gap:1rem;flex-wrap:wrap;align-items:flex-start}
.pill{
  background:var(--surface);border:1px solid var(--border);
  padding:.35rem .85rem;border-radius:999px;
  font-size:.68rem;font-family:var(--fm);display:flex;gap:.4rem;align-items:center;
}
.pill strong{color:var(--accent);font-size:.88rem}

/* ── Toolbar (repo tabs + type filters) ── */
.toolbar{
  display:flex;gap:0;flex-direction:column;
  border-bottom:1px solid var(--border);background:var(--surface);
}
.toolbar-row{
  display:flex;gap:.4rem;flex-wrap:wrap;
  padding:.7rem 3rem;align-items:center;
}
.toolbar-row + .toolbar-row{border-top:1px solid var(--border)}
.toolbar-label{
  font-family:var(--fm);font-size:.6rem;color:var(--muted);
  letter-spacing:.1em;text-transform:uppercase;margin-right:.4rem;white-space:nowrap;
}

/* repo tab */
.rtab{
  font-family:var(--fm);font-size:.65rem;letter-spacing:.06em;
  padding:.28rem .75rem;border-radius:6px;cursor:pointer;
  border:1px solid var(--border);color:var(--muted);background:transparent;transition:all .15s;
}
.rtab:hover{border-color:var(--accent);color:var(--accent)}
.rtab.active{border-color:var(--accent);color:var(--bg);background:var(--accent)}

/* type toggle */
.ttog{
  font-family:var(--fm);font-size:.62rem;letter-spacing:.05em;
  padding:.25rem .65rem;border-radius:6px;cursor:pointer;
  border:1px solid transparent;color:var(--muted);background:var(--surf2);
  transition:all .15s;display:flex;align-items:center;gap:.35rem;
}
.ttog .dot{width:7px;height:7px;border-radius:50%;flex-shrink:0;opacity:.5;transition:opacity .15s}
.ttog.on .dot{opacity:1}
.ttog.on{color:var(--text);border-color:var(--border)}
.ttog:hover{border-color:var(--muted)}

.cat-group{display:flex;gap:.35rem;align-items:center;flex-wrap:wrap}
.cat-label{
  font-family:var(--fm);font-size:.6rem;color:var(--muted);letter-spacing:.08em;
  padding:.2rem .5rem;border-radius:4px;background:rgba(255,255,255,.03);
  border:1px solid var(--border);white-space:nowrap;
}

/* select-all buttons */
.tog-all{
  font-family:var(--fm);font-size:.6rem;padding:.22rem .55rem;border-radius:5px;cursor:pointer;
  border:1px solid var(--border);color:var(--muted);background:transparent;transition:all .15s;
}
.tog-all:hover{color:var(--accent);border-color:var(--accent)}

/* ── Main layout ── */
main{max-width:1700px;margin:0 auto;padding:1.8rem 3rem 5rem;display:grid;gap:1.8rem}

/* ── Cards ── */
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;overflow:hidden}
.card-header{
  padding:.8rem 1.4rem;border-bottom:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;gap:1rem;flex-wrap:wrap;
}
.card-title{font-size:.64rem;font-family:var(--fm);letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}
.card-body{padding:1.3rem}
canvas{max-width:100%}

/* ── Generic controls ── */
.controls{display:flex;gap:.5rem;align-items:center;flex-wrap:wrap}
select,input[type=range]{
  background:var(--surf2);border:1px solid var(--border);color:var(--text);
  font-family:var(--fm);font-size:.65rem;padding:.26rem .52rem;border-radius:6px;cursor:pointer;outline:none;
}
select:focus{border-color:var(--accent)}
label{font-family:var(--fm);font-size:.64rem;color:var(--muted);letter-spacing:.05em}
.btn{
  background:transparent;border:1px solid var(--border);color:var(--muted);
  font-family:var(--fm);font-size:.64rem;padding:.26rem .6rem;
  border-radius:6px;cursor:pointer;letter-spacing:.05em;transition:all .15s;
}
.btn:hover,.btn.active{border-color:var(--accent);color:var(--accent);background:rgba(232,255,71,.05)}

/* ── Two-col ── */
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:1.8rem}
@media(max-width:900px){
  .two-col{grid-template-columns:1fr}
  main,header,.toolbar-row{padding-left:1rem;padding-right:1rem}
}

/* ── Leaderboard ── */
.contrib-grid{display:grid;gap:.4rem}
.contrib-row{
  display:grid;grid-template-columns:2rem 1fr auto auto auto;
  align-items:center;gap:.7rem;
  padding:.38rem .6rem;border-radius:8px;background:var(--surf2);
  border:1px solid transparent;cursor:pointer;transition:border-color .15s;
}
.contrib-row:hover{border-color:var(--border)}
.contrib-row.selected{border-color:var(--accent);background:rgba(232,255,71,.04)}
.contrib-rank{font-family:var(--fm);font-size:.58rem;color:var(--muted);text-align:right}
.contrib-name{font-size:.78rem;font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.contrib-bar-wrap{width:85px;background:var(--bg);height:3px;border-radius:2px;overflow:hidden}
.contrib-bar{height:100%;border-radius:2px;transition:width .35s ease}
.contrib-count{font-family:var(--fm);font-size:.66rem;color:var(--accent);min-width:3rem;text-align:right}
.lb-mute-btn{
  background:transparent;border:none;cursor:pointer;
  font-size:.78rem;color:var(--muted);opacity:0;transition:opacity .15s, color .15s;
  padding:.1rem .2rem;line-height:1;flex-shrink:0;
}
.contrib-row:hover .lb-mute-btn{opacity:1}
.lb-mute-btn:hover{color:#ff6b6b}
.repo-chips{display:flex;gap:.25rem;flex-wrap:wrap;margin-top:.18rem}
.repo-chip{font-family:var(--fm);font-size:.52rem;padding:.08rem .3rem;border-radius:4px}

/* ── Heatmap ── */
#heatmap-wrap{overflow-x:auto;padding-bottom:.5rem}
table.hm{border-collapse:separate;border-spacing:3px;font-family:var(--fm);font-size:.55rem}
table.hm th{color:var(--muted);font-weight:400;padding:.18rem .32rem;text-align:center}
table.hm td{
  width:24px;height:24px;border-radius:4px;text-align:center;vertical-align:middle;
  cursor:pointer;transition:transform .1s;font-size:.48rem;color:transparent;
}
table.hm td:hover{transform:scale(1.28);color:var(--bg);font-weight:700}

/* ── Tooltip ── */
#tt{
  position:fixed;background:var(--surf2);border:1px solid var(--accent);
  padding:.48rem .75rem;border-radius:8px;font-family:var(--fm);font-size:.62rem;
  pointer-events:none;z-index:999;opacity:0;transition:opacity .1s;max-width:270px;
}
#tt.show{opacity:1}
#tt .ttm{color:var(--accent);font-size:.74rem;font-weight:600;margin-bottom:.28rem}
#tt .ttr{display:flex;justify-content:space-between;gap:.9rem;color:var(--muted)}
#tt .ttr span:last-child{color:var(--text)}
#tt .ttd{border-top:1px solid var(--border);margin:.28rem 0 .18rem}

/* ── Repo legend ── */
.rl{display:flex;gap:.7rem;flex-wrap:wrap;margin-bottom:.7rem}
.rldot{display:inline-flex;align-items:center;gap:.3rem;font-family:var(--fm);font-size:.6rem;color:var(--muted)}
.rldot i{width:8px;height:8px;border-radius:50%;flex-shrink:0}

.pulse{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--accent);
  animation:pulse 2s infinite;margin-right:.3rem}
@keyframes pulse{0%,100%{opacity:1;box-shadow:0 0 0 0 rgba(232,255,71,.4)}
  50%{opacity:.7;box-shadow:0 0 0 5px rgba(232,255,71,0)}}
.empty{font-family:var(--fm);font-size:.68rem;color:var(--muted)}

/* ── Category legend in charts ── */
.cat-legend{display:flex;gap:1rem;flex-wrap:wrap;margin-bottom:.6rem}
.cat-item{display:flex;align-items:center;gap:.35rem;font-family:var(--fm);font-size:.6rem;color:var(--muted)}
.cat-item i{width:10px;height:10px;border-radius:3px;flex-shrink:0}

/* ── Help button ── */
.help-btn{
  width:32px;height:32px;border-radius:50%;
  background:transparent;border:1px solid var(--border);
  color:var(--muted);font-family:var(--fm);font-size:.85rem;font-weight:600;
  cursor:pointer;transition:all .2s;flex-shrink:0;
  display:flex;align-items:center;justify-content:center;
}
.help-btn:hover{border-color:var(--accent);color:var(--accent);background:rgba(232,255,71,.07)}

/* ── Help overlay ── */
#helpOverlay{
  position:fixed;inset:0;background:rgba(0,0,0,.72);
  z-index:1000;display:flex;align-items:flex-start;justify-content:flex-end;
  padding:1.5rem;opacity:0;pointer-events:none;
  transition:opacity .25s;
}
#helpOverlay.open{opacity:1;pointer-events:all}
#helpPanel{
  width:min(740px,96vw);height:calc(100vh - 3rem);
  background:var(--surface);border:1px solid var(--border);border-radius:14px;
  display:flex;flex-direction:column;overflow:hidden;
  transform:translateX(40px);transition:transform .25s;
  box-shadow:0 24px 80px rgba(0,0,0,.6);
}
#helpOverlay.open #helpPanel{transform:translateX(0)}

#helpHeader{
  padding:1.1rem 1.5rem;border-bottom:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;
  background:var(--surf2);flex-shrink:0;
}
#helpTitle{font-size:1rem;font-weight:800;letter-spacing:-.02em}
#helpClose{
  background:transparent;border:1px solid var(--border);color:var(--muted);
  font-family:var(--fm);font-size:.72rem;padding:.3rem .65rem;border-radius:6px;cursor:pointer;
  transition:all .15s;
}
#helpClose:hover{border-color:var(--accent);color:var(--accent)}

#helpTabs{
  display:flex;gap:0;border-bottom:1px solid var(--border);
  background:var(--surf2);flex-shrink:0;overflow-x:auto;
}
.htab{
  font-family:var(--fm);font-size:.65rem;letter-spacing:.06em;
  padding:.7rem 1.1rem;cursor:pointer;border:none;
  background:transparent;color:var(--muted);
  border-bottom:2px solid transparent;transition:all .15s;white-space:nowrap;
}
.htab:hover{color:var(--text)}
.htab.active{color:var(--accent);border-bottom-color:var(--accent)}

#helpBody{flex:1;overflow-y:auto;padding:1.8rem 2rem;scroll-behavior:smooth}
#helpBody::-webkit-scrollbar{width:5px}
#helpBody::-webkit-scrollbar-track{background:transparent}
#helpBody::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}

.hpage{display:none}
.hpage.active{display:block}

#helpBody h2{
  font-size:1.25rem;font-weight:800;letter-spacing:-.03em;
  color:var(--text);margin-bottom:1rem;
}
#helpBody h3{
  font-size:.72rem;font-family:var(--fm);letter-spacing:.1em;
  text-transform:uppercase;color:var(--accent);
  margin:1.6rem 0 .6rem;
}
#helpBody p{
  font-size:.85rem;line-height:1.7;color:rgba(232,234,240,.75);
  margin-bottom:.8rem;
}
#helpBody em{color:var(--text);font-style:normal;font-weight:700}
#helpBody ul{padding-left:1.2rem;margin-bottom:.8rem}
#helpBody li{
  font-size:.83rem;line-height:1.7;color:rgba(232,234,240,.75);margin-bottom:.3rem;
}
#helpBody strong{color:var(--text)}
#helpBody code{
  font-family:var(--fm);font-size:.78rem;
  background:var(--surf2);border:1px solid var(--border);
  padding:.1rem .4rem;border-radius:4px;color:var(--accent);
}
#helpBody pre{
  background:var(--surf2);border:1px solid var(--border);
  border-radius:8px;padding:1rem 1.2rem;overflow-x:auto;
  margin:.6rem 0 1rem;
}
#helpBody pre code{
  background:transparent;border:none;padding:0;
  font-size:.78rem;color:var(--text);line-height:1.65;
}
.htable{
  width:100%;border-collapse:collapse;font-size:.78rem;
  font-family:var(--fm);margin-bottom:1rem;
}
.htable th{
  text-align:left;padding:.5rem .75rem;
  border-bottom:1px solid var(--border);
  color:var(--muted);font-weight:600;font-size:.65rem;letter-spacing:.08em;
}
.htable td{
  padding:.45rem .75rem;border-bottom:1px solid rgba(38,41,50,.6);
  color:rgba(232,234,240,.8);vertical-align:top;line-height:1.5;
}
.htable tr:last-child td{border-bottom:none}
.htable td code{font-size:.72rem}
.cat-cell{font-weight:700;text-align:center;border-right:1px solid var(--border)}

.hgrid{
  display:grid;grid-template-columns:1fr 1fr;gap:.8rem;
  margin:1rem 0 1.2rem;
}
@media(max-width:560px){.hgrid{grid-template-columns:1fr}}
.hbox{
  display:flex;gap:.75rem;align-items:flex-start;
  background:var(--surf2);border:1px solid var(--border);
  border-radius:10px;padding:.9rem 1rem;
}
.hbox-icon{font-size:1.3rem;flex-shrink:0;margin-top:.05rem}
.hbox strong{display:block;font-size:.82rem;margin-bottom:.25rem;color:var(--text)}
.hbox p{font-size:.75rem;margin:0;color:var(--muted);line-height:1.55}

.hnote{
  background:rgba(232,255,71,.05);border:1px solid rgba(232,255,71,.2);
  border-radius:8px;padding:.8rem 1rem;margin:1rem 0;
  font-size:.78rem;line-height:1.6;color:rgba(232,234,240,.7);
}
.hnote strong{color:var(--accent)}

/* ── Contributor ref tooltip rows ── */
.ctref-cat{
  font-weight:700;font-size:.62rem;letter-spacing:.06em;text-transform:uppercase;
  margin:.5rem 0 .18rem;padding-bottom:.18rem;border-bottom:1px solid var(--border);
}
.ctref-cat:first-child{margin-top:.1rem}
.ctref-row{
  display:flex;gap:.5rem;align-items:baseline;
  margin:.1rem 0;line-height:1.45;
}
.ctref-kind{
  color:var(--muted);font-size:.6rem;white-space:nowrap;flex-shrink:0;min-width:7rem;
}
.ctref-refs{
  color:var(--text);font-size:.62rem;word-break:break-word;line-height:1.5;
  letter-spacing:.02em;
}
.ctref-stat{
  font-size:.58rem;color:#a78bfa;font-family:var(--fm);
  padding:.05rem 0 .2rem 0;letter-spacing:.03em;
  margin-left:7.4rem;  /* align under refs column */
}

/* ── Mute / what-if row ── */
.mute-search-wrap{position:relative;flex-shrink:0}
#muteSearch{
  width:200px;background:var(--surf2);border:1px solid var(--border);
  color:var(--text);font-family:var(--fm);font-size:.68rem;
  padding:.28rem .65rem;border-radius:6px;outline:none;transition:border-color .15s;
}
#muteSearch:focus{border-color:var(--accent)}
#muteSuggestions{
  position:absolute;top:calc(100% + 4px);left:0;z-index:200;
  background:var(--surface);border:1px solid var(--border);border-radius:8px;
  min-width:200px;max-height:220px;overflow-y:auto;
  box-shadow:0 8px 32px rgba(0,0,0,.5);display:none;
}
#muteSuggestions.open{display:block}
.msug{
  padding:.38rem .75rem;font-family:var(--fm);font-size:.68rem;cursor:pointer;
  color:var(--muted);transition:background .1s;white-space:nowrap;
}
.msug:hover,.msug.focused{background:var(--surf2);color:var(--text)}
.msug mark{background:transparent;color:var(--accent);font-weight:700}

#muteChips{display:flex;gap:.35rem;flex-wrap:wrap;align-items:center;flex:1;min-width:0}
.mchip{
  display:inline-flex;align-items:center;gap:.3rem;
  font-family:var(--fm);font-size:.62rem;letter-spacing:.04em;
  padding:.22rem .55rem .22rem .65rem;border-radius:999px;
  background:rgba(255,107,107,.12);border:1px solid rgba(255,107,107,.35);
  color:#ff9e9e;cursor:default;transition:background .15s;
}
.mchip:hover{background:rgba(255,107,107,.2)}
.mchip-x{
  cursor:pointer;font-size:.7rem;opacity:.7;margin-left:.1rem;
  padding:.05rem .1rem;border-radius:3px;
}
.mchip-x:hover{opacity:1;color:#ff6b6b}

.mute-hint{
  font-family:var(--fm);font-size:.6rem;color:var(--muted);
  letter-spacing:.04em;font-style:italic;white-space:nowrap;
}
</style>
</head>
<body>
<div id="tt"></div>

<!-- ── Header ── -->
<header>
  <div>
    <div class="h-title" id="mainTitle">Loading…</div>
    <div class="meta">ACTIVITY ANALYSIS · GENERATED {{GENERATED}}</div>
  </div>
  <div class="stats-row">
    <div class="pill"><strong id="s-total">–</strong>&nbsp;events</div>
    <div class="pill"><strong id="s-repos">–</strong>&nbsp;repos</div>
    <div class="pill"><strong id="s-contrib">–</strong>&nbsp;contributors</div>
    <div class="pill"><strong id="s-months">–</strong>&nbsp;months</div>
    <div class="pill">peak&nbsp;<strong id="s-peak">–</strong></div>
    <div class="pill" id="s-muted-pill" style="display:none;border-color:rgba(255,107,107,.4);background:rgba(255,107,107,.08)">
      <strong id="s-muted" style="color:#ff9e9e">0</strong>&nbsp;<span style="color:#ff9e9e">muted</span>
    </div>
    <button class="help-btn" onclick="toggleHelp()" title="Open documentation">?</button>
  </div>
</header>

<!-- ── Help overlay ── -->
<div id="helpOverlay" onclick="closeHelpIfBg(event)">
  <div id="helpPanel">
    <div id="helpHeader">
      <span id="helpTitle">Documentation</span>
      <button id="helpClose" onclick="toggleHelp()">✕</button>
    </div>

    <!-- Tab bar -->
    <div id="helpTabs">
      <button class="htab active" onclick="showTab('overview')">Overview</button>
      <button class="htab" onclick="showTab('filters')">Filters &amp; Toggles</button>
      <button class="htab" onclick="showTab('charts')">Charts</button>
      <button class="htab" onclick="showTab('events')">Event Types</button>
      <button class="htab" onclick="showTab('cli')">CLI Reference</button>
    </div>

    <div id="helpBody">

      <!-- ── Overview ── -->
      <div class="hpage active" id="tab-overview">
        <h2>GitHub Activity Analyzer</h2>
        <p>This report visualizes the <em>liveliness</em> of one or more GitHub projects over time. Every comment, pull request, review, and discussion contribution is tracked per contributor and per month, letting you spot activity trends, identify key contributors, and compare repositories side by side.</p>

        <div class="hgrid">
          <div class="hbox">
            <div class="hbox-icon">📊</div>
            <div>
              <strong>Timeline</strong>
              <p>Aggregated monthly activity — the project's heartbeat at a glance. Switch between bar, line, and area views. Enable <em>Split by Type</em> to see Comments / PRs / Reviews stacked separately.</p>
            </div>
          </div>
          <div class="hbox">
            <div class="hbox-icon">🏛️</div>
            <div>
              <strong>Repo Comparison</strong>
              <p>When analyzing multiple repositories, this chart shows relative activity per repo over time. Use Stacked to see the total, Grouped to compare heights, or Line for trend comparison.</p>
            </div>
          </div>
          <div class="hbox">
            <div class="hbox-icon">👥</div>
            <div>
              <strong>Contributor Breakdown</strong>
              <p>Stacked bar chart showing how the top N contributors distribute their activity each month. Click any bar segment to open that contributor's personal drilldown.</p>
            </div>
          </div>
          <div class="hbox">
            <div class="hbox-icon">🌡️</div>
            <div>
              <strong>Heatmap</strong>
              <p>Calendar-style month grid colored by intensity. Filter to a single contributor to see their personal rhythm. Click any cell to filter the Leaderboard to that month.</p>
            </div>
          </div>
          <div class="hbox">
            <div class="hbox-icon">🏆</div>
            <div>
              <strong>Leaderboard</strong>
              <p>Ranked list of contributors by event count. Filter to any single month or use All Time. Repo chips show per-repository breakdown. Click a row to open the contributor drilldown.</p>
            </div>
          </div>
          <div class="hbox">
            <div class="hbox-icon">🔍</div>
            <div>
              <strong>Contributor Drilldown</strong>
              <p>Opens when you click a contributor anywhere. Shows their individual activity timeline, broken down by repository when multiple repos are loaded.</p>
            </div>
          </div>
        </div>

        <div class="hnote">
          <strong>Tip:</strong> All filters (repo tab, event-type toggles) affect every chart simultaneously. The raw event data is embedded in this HTML file — no server needed, everything runs locally in your browser.
        </div>
      </div>

      <!-- ── Filters ── -->
      <div class="hpage" id="tab-filters">
        <h2>Filters &amp; Toggles</h2>

        <h3>Repo Tabs</h3>
        <p>The first toolbar row selects which repository's data is shown. <strong>ALL REPOS</strong> aggregates all loaded repositories; clicking a specific repo name narrows every chart to that repo only. The repo comparison chart is hidden when a single repo is selected.</p>

        <h3>Event-Type Toggles</h3>
        <p>The second toolbar row lets you include or exclude individual event types. Toggles are grouped into three categories:</p>
        <table class="htable">
          <tr><th>Category</th><th>Toggle</th><th>What it means</th></tr>
          <tr><td rowspan="6" class="cat-cell" style="color:#47d4ff">Comments</td>
              <td>issue comment</td><td>A comment posted on any Issue or Pull Request thread</td></tr>
          <tr><td>pr review comment</td><td>An inline code comment within a Pull Request diff</td></tr>
          <tr><td>discussion</td><td>A new Discussion thread opened in the repository</td></tr>
          <tr><td>discussion comment</td><td>A top-level reply inside a Discussion</td></tr>
          <tr><td>discussion reply</td><td>A nested reply to a discussion comment</td></tr>
          <tr><td>commit comment</td><td>A comment attached directly to a specific commit</td></tr>
          <tr><td rowspan="3" class="cat-cell" style="color:#e8ff47">Pull Requests</td>
              <td>pr opened</td><td>A new Pull Request was opened by this contributor</td></tr>
          <tr><td>pr merged</td><td>A Pull Request was merged — credited to the person who pressed Merge</td></tr>
          <tr><td>pr closed</td><td>A Pull Request was closed without merging</td></tr>
          <tr><td rowspan="3" class="cat-cell" style="color:#a78bfa">Reviews</td>
              <td>review approved</td><td>A review submitted with "Approve" verdict</td></tr>
          <tr><td>review changes requested</td><td>A review submitted with "Request Changes" verdict</td></tr>
          <tr><td>review commented</td><td>A review submitted with neutral "Comment" verdict (no verdict)</td></tr>
        </table>

        <h3>Shortcut Buttons</h3>
        <ul>
          <li><strong>ALL ON / ALL OFF</strong> — enable or disable every event type at once.</li>
          <li><strong>Comments ONLY / Pull Requests ONLY / Reviews ONLY</strong> — instantly isolate one category and switch off all others. Useful for answering questions like "who writes the most code reviews?" without comment noise.</li>
        </ul>

        <div class="hnote">
          <strong>Note:</strong> Toggling event types is instantaneous — the full dataset is stored in the browser. No network requests are made after the page loads.
        </div>
      </div>

      <!-- ── Charts ── -->
      <div class="hpage" id="tab-charts">
        <h2>Chart Controls</h2>

        <h3>Activity Timeline</h3>
        <table class="htable">
          <tr><th>Control</th><th>Effect</th></tr>
          <tr><td>BAR / LINE / AREA</td><td>Switch the chart rendering style. Area fills the region under the line for easier trend reading.</td></tr>
          <tr><td>SMOOTH slider</td><td>Adds Bézier curve tension to line/area charts (0 = straight segments, 0.5 = heavily smoothed).</td></tr>
          <tr><td>SPLIT BY TYPE</td><td>When ON, the chart shows three stacked datasets — one per event category (Comments, PRs, Reviews) — instead of a single total. Lets you see whether a spike was driven by discussion or code activity.</td></tr>
        </table>

        <h3>Repository Comparison</h3>
        <p>Only visible when multiple repos are loaded and <strong>ALL REPOS</strong> is selected.</p>
        <table class="htable">
          <tr><th>Mode</th><th>Best for</th></tr>
          <tr><td>STACKED</td><td>Reading combined project health; individual repos show proportional contribution</td></tr>
          <tr><td>GROUPED</td><td>Direct height comparison between repos in the same month</td></tr>
          <tr><td>LINE</td><td>Trend comparison — which repo is growing or declining relative to others</td></tr>
        </table>

        <h3>Contributor Breakdown</h3>
        <table class="htable">
          <tr><th>Control</th><th>Effect</th></tr>
          <tr><td>TOP selector</td><td>Limits the chart to the N most active contributors. "ALL" shows everyone, but may be slow for large projects.</td></tr>
          <tr><td>STACKED</td><td>Shows cumulative monthly total; each contributor's slice shows their share</td></tr>
          <tr><td>GROUPED</td><td>Places bars side by side for absolute comparisons between contributors</td></tr>
          <tr><td>Click a bar</td><td>Opens the Contributor Drilldown for that person</td></tr>
        </table>

        <h3>Activity Heatmap</h3>
        <table class="htable">
          <tr><th>Action</th><th>Effect</th></tr>
          <tr><td>CONTRIBUTOR dropdown</td><td>Switch between the aggregate view (ALL) and a single contributor's personal heatmap</td></tr>
          <tr><td>Hover a cell</td><td>Tooltip shows total count, per-repo breakdown, and per-event-type counts for that month</td></tr>
          <tr><td>Click a cell</td><td>Filters the Leaderboard to that specific month</td></tr>
        </table>

        <h3>Leaderboard</h3>
        <table class="htable">
          <tr><th>Action</th><th>Effect</th></tr>
          <tr><td>MONTH dropdown</td><td>Restrict ranking to a single month (or use ALL TIME)</td></tr>
          <tr><td>Repo chips</td><td>Small colored badges under each name show how their count breaks down per repo</td></tr>
          <tr><td>Click a row</td><td>Opens the Contributor Drilldown panel at the bottom of the page</td></tr>
        </table>
      </div>

      <!-- ── Event Types ── -->
      <div class="hpage" id="tab-events">
        <h2>Event Types &amp; Data Sources</h2>
        <p>The analyzer collects data from five GitHub API endpoints. Each event is attributed to the GitHub user who performed the action, tagged with the month it occurred, and stored with its event type.</p>

        <h3>What counts as an interaction?</h3>
        <p>The goal is to measure <em>visible community contribution</em> — actions that help other users, advance pull requests, or keep discussions moving. The following are deliberately <strong>not</strong> counted:</p>
        <ul>
          <li>Commits (tracked separately in the contributor graph)</li>
          <li>Reactions / emoji responses (no content contribution)</li>
          <li>Bot accounts and the deleted "ghost" user</li>
        </ul>

        <h3>Attribution rules</h3>
        <table class="htable">
          <tr><th>Event</th><th>Credited to</th></tr>
          <tr><td>pr_opened</td><td>The author of the Pull Request</td></tr>
          <tr><td>pr_merged</td><td>The person who clicked Merge (merged_by), falling back to the PR author</td></tr>
          <tr><td>pr_closed</td><td>The PR author (GitHub does not expose the closer via this endpoint)</td></tr>
          <tr><td>review_*</td><td>The reviewer who submitted the review</td></tr>
          <tr><td>issue_comment</td><td>The comment author</td></tr>
          <tr><td>pr_review_comment</td><td>The author of the inline diff comment</td></tr>
          <tr><td>discussion / comment / reply</td><td>The respective author at each level</td></tr>
          <tr><td>commit_comment</td><td>The comment author</td></tr>
        </table>

        <h3>Caching</h3>
        <p>After the first fetch, raw events are saved to <code>.gh_cache_&lt;owner&gt;_&lt;repo&gt;.json</code> next to the script. Subsequent runs load from cache instantly. Use <code>--no-cache</code> to force a full re-fetch (e.g. after new activity or after adding new event types).</p>

        <div class="hnote">
          <strong>Important:</strong> If you upgraded from an older version of this script that did not track PR and review events, you must run with <code>--no-cache</code> once to rebuild the cache with the full event set.
        </div>
      </div>

      <!-- ── CLI ── -->
      <div class="hpage" id="tab-cli">
        <h2>Command-Line Reference</h2>
        <p>The script requires Python 3.8+ and the <code>requests</code> library (<code>pip install requests</code>). A GitHub Personal Access Token (PAT) with at least <code>public_repo</code> scope is required.</p>

        <h3>Basic usage</h3>
        <pre><code>python gh_activity.py &lt;owner/repo&gt; [owner/repo …] [options]</code></pre>

        <h3>Arguments</h3>
        <table class="htable">
          <tr><th>Argument</th><th>Description</th></tr>
          <tr><td><code>repos</code></td><td>One or more <code>owner/repo</code> strings. Multiple repos produce a combined report with cross-repo comparison.</td></tr>
          <tr><td><code>--token TOKEN</code></td><td>GitHub Personal Access Token. Can also be set via the <code>GH_TOKEN</code> environment variable.</td></tr>
          <tr><td><code>--since YYYY-MM</code></td><td>Only include data from this month onwards. Applied both during fetch and to cached data.</td></tr>
          <tr><td><code>--out FILE</code></td><td>Output HTML filename. Defaults to <code>owner_repo_activity.html</code> (or a combined name for multiple repos).</td></tr>
          <tr><td><code>--no-cache</code></td><td>Ignore existing cache files and re-fetch all data from the GitHub API.</td></tr>
        </table>

        <h3>Examples</h3>
        <pre><code># Single repository
python gh_activity.py open-quantum-safe/liboqs \
    --token $GH_TOKEN

# Multiple repos → one combined report
python gh_activity.py open-quantum-safe/liboqs \
                      open-quantum-safe/oqs-provider \
    --token $GH_TOKEN --since 2022-01

# Force refresh + custom output name
python gh_activity.py facebook/react facebook/relay \
    --token $GH_TOKEN --no-cache --out react_ecosystem.html

# Quick overview, last 12 months only
python gh_activity.py torvalds/linux \
    --token $GH_TOKEN --since 2024-01</code></pre>

        <h3>Rate limits</h3>
        <p>The GitHub REST API allows 5,000 requests/hour for authenticated users. Large repositories with many PRs (thousands) may take 30–60 minutes on a first fetch. The script automatically pauses and retries when a rate limit is hit. Cache is saved incrementally per repo, so a partial run is safe to interrupt and resume (the interrupted repo will be re-fetched on the next run).</p>

        <h3>Token permissions</h3>
        <table class="htable">
          <tr><th>Scope</th><th>Required for</th></tr>
          <tr><td><code>public_repo</code></td><td>All public repository data</td></tr>
          <tr><td><code>repo</code></td><td>Private repositories (full scope needed)</td></tr>
          <tr><td><em>GraphQL</em></td><td>Discussions — same token, no extra scope needed for public repos</td></tr>
        </table>
      </div>

    </div><!-- /helpBody -->
  </div><!-- /helpPanel -->
</div><!-- /helpOverlay -->

<!-- ── Toolbar ── -->
<div class="toolbar">
  <!-- Row 1: repo tabs -->
  <div class="toolbar-row" id="repoTabs">
    <span class="toolbar-label">REPO</span>
  </div>
  <!-- Row 2: event-type toggles -->
  <div class="toolbar-row" id="typeToggles">
    <span class="toolbar-label">SHOW</span>
    <!-- filled by JS -->
  </div>
  <!-- Row 3: contributor mute / what-if -->
  <div class="toolbar-row" id="muteRow">
    <span class="toolbar-label">MUTE</span>
    <div class="mute-search-wrap">
      <input id="muteSearch" type="text" placeholder="type a contributor name…"
             autocomplete="off" spellcheck="false"
             oninput="updateMuteSearch(this.value)"
             onkeydown="muteSearchKey(event)">
      <div id="muteSuggestions"></div>
    </div>
    <div id="muteChips"></div>
    <button class="tog-all" id="muteUnmuteAll" onclick="unmuteAll()" style="display:none">UNMUTE ALL</button>
    <span class="mute-hint" id="muteHint">Mute contributors to see project activity without them</span>
  </div>
</div>

<main>
  <!-- 1 · Timeline -->
  <div class="card">
    <div class="card-header">
      <span class="card-title"><span class="pulse"></span>Activity Timeline</span>
      <div class="controls">
        <label>VIEW</label>
        <button class="btn active" id="aggBar"  onclick="setAggType('bar')">BAR</button>
        <button class="btn"        id="aggLine" onclick="setAggType('line')">LINE</button>
        <button class="btn"        id="aggArea" onclick="setAggType('area')">AREA</button>
        <label style="margin-left:.6rem">SMOOTH</label>
        <input type="range" id="smooth" min="0" max="5" value="0" style="width:60px"
               oninput="updateSmooth(this.value)">
        <label style="margin-left:.6rem">SPLIT BY TYPE</label>
        <button class="btn" id="aggSplit" onclick="toggleSplit()">OFF</button>
      </div>
    </div>
    <div class="card-body"><canvas id="chartAgg" height="85"></canvas></div>
  </div>

  <!-- 2 · Repo comparison -->
  <div class="card" id="repoCompareCard">
    <div class="card-header">
      <span class="card-title">Repository Comparison</span>
      <div class="controls">
        <button class="btn active" id="rcStacked" onclick="setRCMode('stacked')">STACKED</button>
        <button class="btn"        id="rcGrouped" onclick="setRCMode('grouped')">GROUPED</button>
        <button class="btn"        id="rcLine"    onclick="setRCMode('line')">LINE</button>
      </div>
    </div>
    <div class="card-body">
      <div class="rl" id="repoLegend"></div>
      <canvas id="chartRC" height="90"></canvas>
    </div>
  </div>

  <!-- 3 · Contributor breakdown -->
  <div class="card">
    <div class="card-header">
      <span class="card-title">Contributor Breakdown</span>
      <div class="controls">
        <label>TOP</label>
        <select id="topN" onchange="renderStacked()">
          <option value="10">10</option>
          <option value="15">15</option>
          <option value="20">20</option>
          <option value="999">ALL</option>
        </select>
        <button class="btn active" id="btnStacked" onclick="setStackedMode('stacked')">STACKED</button>
        <button class="btn"        id="btnGrouped" onclick="setStackedMode('grouped')">GROUPED</button>
      </div>
    </div>
    <div class="card-body"><canvas id="chartStacked" height="110"></canvas></div>
  </div>

  <!-- 4 · Heatmap + Leaderboard -->
  <div class="two-col">
    <div class="card">
      <div class="card-header">
        <span class="card-title">Activity Heatmap</span>
        <div class="controls">
          <label>CONTRIBUTOR</label>
          <select id="hmContrib" onchange="renderHeatmap()">
            <option value="__all__">ALL</option>
          </select>
        </div>
      </div>
      <div class="card-body"><div id="heatmap-wrap"></div></div>
    </div>
    <div class="card">
      <div class="card-header">
        <span class="card-title">Leaderboard</span>
        <div class="controls">
          <label>MONTH</label>
          <select id="lbMonth" onchange="renderLeaderboard()">
            <option value="__all__">ALL TIME</option>
          </select>
        </div>
      </div>
      <div class="card-body" style="max-height:440px;overflow-y:auto">
        <div class="contrib-grid" id="leaderboard"></div>
      </div>
    </div>
  </div>

  <!-- 5 · Drilldown -->
  <div class="card" id="drillCard" style="display:none">
    <div class="card-header">
      <span class="card-title" id="drillTitle">CONTRIBUTOR DRILLDOWN</span>
      <button class="btn" onclick="clearDrill()">✕ CLOSE</button>
    </div>
    <div class="card-body">
      <div class="rl" id="drillLegend"></div>
      <canvas id="chartDrill" height="80"></canvas>
    </div>
  </div>
</main>

<script>
// ── Injected data ─────────────────────────────────────────────────────────────
const DATA       = {{DATA_JSON}};
const allMonths  = DATA.months;
const allRepos   = DATA.repos;
const CATEGORIES = DATA.categories;   // {cat: {label, color, kinds[]}}
const KIND_TO_CAT= DATA.kind_to_cat;  // {kind: cat}
const rawByRepo  = DATA.raw_by_repo;  // {repo: [[author,month,kind], …]}
const perRepoCon = DATA.per_repo_contributors;

// ── Palette ───────────────────────────────────────────────────────────────────
const PAL = [
  '#e8ff47','#47d4ff','#ff6b6b','#a78bfa','#34d399',
  '#fb923c','#f472b6','#60a5fa','#facc15','#4ade80',
  '#c084fc','#38bdf8','#f87171','#a3e635','#fb7185',
  '#818cf8','#22d3ee','#fbbf24','#86efac','#c4b5fd',
];
const RCOL     = allRepos.map((_,i) => PAL[i % PAL.length]);
const repoCol  = r => RCOL[allRepos.indexOf(r)] || '#e8ff47';
const cc       = i => PAL[i % PAL.length];

const catKeys  = Object.keys(CATEGORIES);
const catColor = cat => CATEGORIES[cat]?.color || '#888';
const kindColor= k => catColor(KIND_TO_CAT[k]);

// ── Active state ──────────────────────────────────────────────────────────────
let activeRepo    = '__all__';
let activeKinds   = new Set(Object.values(KIND_TO_CAT).flatMap(() => []).concat(
  ...catKeys.map(c => CATEGORIES[c].kinds)
));  // all on by default
let mutedContribs = new Set();  // contributors excluded from all charts

// ── Derived / recomputed on every filter change ───────────────────────────────
let computed = {};   // populated by recompute()

function recompute() {
  // Gather raw events matching active repo + active kinds
  const repos = activeRepo === '__all__' ? allRepos : [activeRepo];
  const byC   = {};   // {login: {month: n}}
  const byR   = {};   // {repo:  {month: n}}
  const byCR  = {};   // {login: {repo: {month: n}}}
  const totals= {};   // {month: n}
  const byKind= {};   // {month: {kind: n}}
  const contribSet = new Set();
  const monthsSet  = new Set(allMonths);  // keep full month axis

  for (const repo of repos) {
    const evts = rawByRepo[repo] || [];
    for (const [author, month, kind, ref, chars] of evts) {  // chars may be null for old cache / lifecycle events
      if (!activeKinds.has(kind)) continue;
      if (mutedContribs.has(author)) continue;
      contribSet.add(author);
      // byC
      if (!byC[author]) byC[author] = {};
      byC[author][month] = (byC[author][month] || 0) + 1;
      // byR
      if (!byR[repo])  byR[repo]  = {};
      byR[repo][month] = (byR[repo][month] || 0) + 1;
      // byCR
      if (!byCR[author])       byCR[author] = {};
      if (!byCR[author][repo]) byCR[author][repo] = {};
      byCR[author][repo][month] = (byCR[author][repo][month] || 0) + 1;
      // totals
      totals[month] = (totals[month] || 0) + 1;
      // byKind
      if (!byKind[month]) byKind[month] = {};
      byKind[month][kind] = (byKind[month][kind] || 0) + 1;
    }
  }

  const contributors = [...contribSet].sort(
    (a,b) => Object.values(byC[b]||{}).reduce((s,n)=>s+n,0)
           - Object.values(byC[a]||{}).reduce((s,n)=>s+n,0)
  );

  // per-repo contributor rank within filtered kinds
  const prc = {};
  for (const repo of repos) {
    const seen = new Set();
    (rawByRepo[repo] || []).forEach(([a,,k]) => { if (activeKinds.has(k) && !mutedContribs.has(a)) seen.add(a); });
    prc[repo] = [...seen].sort(
      (a,b) => Object.values((byCR[b]||{})[repo]||{}).reduce((s,n)=>s+n,0)
             - Object.values((byCR[a]||{})[repo]||{}).reduce((s,n)=>s+n,0)
    );
  }

  computed = { contributors, byC, byR, byCR, totals, byKind, prc };
}

function valC(login, month) {
  return (computed.byC[login] || {})[month] || 0;
}
function totalM(month) { return computed.totals[month] || 0; }
function visibleContribs() {
  if (activeRepo === '__all__') return computed.contributors;
  return computed.prc[activeRepo] || [];
}

// ── Chart defaults ────────────────────────────────────────────────────────────
Chart.defaults.color       = '#6b7280';
Chart.defaults.borderColor = '#262932';
Chart.defaults.font.family = "'JetBrains Mono',monospace";
Chart.defaults.font.size   = 10;
const CS = {
  x: { grid:{color:'#1c1f27'}, ticks:{maxRotation:45} },
  y: { grid:{color:'#1c1f27'}, beginAtZero:true },
};

// ── Pills ─────────────────────────────────────────────────────────────────────
function updatePills() {
  const tot = Object.values(computed.totals).reduce((s,n)=>s+n,0);
  const months = allMonths;
  const peak   = [...months].sort((a,b) => totalM(b)-totalM(a))[0] || '–';
  document.getElementById('s-total').textContent   = tot.toLocaleString();
  document.getElementById('s-repos').textContent   = activeRepo==='__all__' ? allRepos.length : 1;
  document.getElementById('s-contrib').textContent = computed.contributors.length;
  document.getElementById('s-months').textContent  = months.length;
  document.getElementById('s-peak').textContent    = peak + (totalM(peak) ? ` (${totalM(peak)})` : '');
  const mutedPill = document.getElementById('s-muted-pill');
  if (mutedPill) {
    mutedPill.style.display = mutedContribs.size ? '' : 'none';
    const ms = document.getElementById('s-muted');
    if (ms) ms.textContent = mutedContribs.size;
  }

  if (activeRepo === '__all__') {
    document.getElementById('mainTitle').innerHTML =
      allRepos.map(r => `<span class="hl">${r.split('/')[1]}</span>`)
              .join(' <span style="color:var(--muted);font-weight:400">+</span> ');
  } else {
    const [o,r] = activeRepo.split('/');
    document.getElementById('mainTitle').innerHTML = `${o}/<span class="hl">${r}</span>`;
  }
}

// ── Repo tabs ─────────────────────────────────────────────────────────────────
function buildTabs() {
  const el = document.getElementById('repoTabs');
  let h = '<span class="toolbar-label">REPO</span>';
  h += `<button class="rtab active" onclick="setRepo('__all__')">ALL REPOS</button>`;
  allRepos.forEach((r,i) => {
    const dot = `<i style="width:7px;height:7px;border-radius:50%;background:${RCOL[i]};display:inline-block;margin-right:4px;vertical-align:middle"></i>`;
    h += `<button class="rtab" onclick="setRepo('${r}')">${dot}${r}</button>`;
  });
  el.innerHTML = h;
}

function setRepo(r) {
  activeRepo = r;
  document.querySelectorAll('.rtab').forEach((btn,i) =>
    btn.classList.toggle('active', i===0 ? r==='__all__' : allRepos[i-1]===r));
  document.getElementById('repoCompareCard').style.display =
    r==='__all__' && allRepos.length>1 ? '' : 'none';
  rebuildContribSelect();
  refreshAll();
}

// ── Type toggles ──────────────────────────────────────────────────────────────
function buildTypeToggles() {
  const el = document.getElementById('typeToggles');
  let h = '<span class="toolbar-label">SHOW</span>';

  catKeys.forEach(cat => {
    const meta = CATEGORIES[cat];
    h += `<div class="cat-group">
      <span class="cat-label" style="color:${meta.color}">${meta.label}</span>`;
    meta.kinds.forEach(kind => {
      const label = kind.replace(/_/g,' ');
      h += `<button class="ttog on" id="tog_${kind}" data-kind="${kind}"
              style="--kc:${meta.color}"
              onclick="toggleKind('${kind}')">
          <span class="dot" style="background:${meta.color}"></span>${label}
        </button>`;
    });
    h += `</div>`;
  });

  // select-all shortcuts
  h += `<div style="margin-left:auto;display:flex;gap:.35rem">
    <button class="tog-all" onclick="setAllKinds(true)">ALL ON</button>
    <button class="tog-all" onclick="setAllKinds(false)">ALL OFF</button>`;
  catKeys.forEach(cat => {
    const meta = CATEGORIES[cat];
    h += `<button class="tog-all" onclick="setCatKinds('${cat}',true)"
            style="color:${meta.color};border-color:${meta.color}22">${meta.label} ONLY</button>`;
  });
  h += `</div>`;

  el.innerHTML = h;
}

function toggleKind(kind) {
  if (activeKinds.has(kind)) activeKinds.delete(kind);
  else                        activeKinds.add(kind);
  document.getElementById(`tog_${kind}`)?.classList.toggle('on', activeKinds.has(kind));
  refreshAll();
}

function setAllKinds(on) {
  catKeys.forEach(cat => CATEGORIES[cat].kinds.forEach(k => {
    if (on) activeKinds.add(k); else activeKinds.delete(k);
  }));
  syncToggles();
  refreshAll();
}

function setCatKinds(targetCat, on) {
  // Turn target ON, all others OFF
  catKeys.forEach(cat => {
    const isTarget = cat === targetCat;
    CATEGORIES[cat].kinds.forEach(k => {
      if (isTarget) activeKinds.add(k);
      else          activeKinds.delete(k);
    });
  });
  syncToggles();
  refreshAll();
}

function syncToggles() {
  catKeys.forEach(cat => CATEGORIES[cat].kinds.forEach(k => {
    document.getElementById(`tog_${k}`)?.classList.toggle('on', activeKinds.has(k));
  }));
}

// ── Master refresh ────────────────────────────────────────────────────────────
function refreshAll() {
  recompute();
  updatePills();
  updateAgg();
  if (activeRepo==='__all__' && allRepos.length>1) updateRC();
  renderStacked();
  renderHeatmap();
  renderLeaderboard();
  if (selectedContrib) drillContributor(selectedContrib);
}

// ── 1 · Aggregated timeline ───────────────────────────────────────────────────
let chartAgg, aggType='bar', aggTension=0, aggSplit=false;

function aggDS() {
  const isArea = aggType==='area';
  const isLine = aggType==='line' || isArea;
  const months = allMonths;

  if (!aggSplit) {
    return [{
      label: 'Total',
      data:  months.map(m => totalM(m)),
      backgroundColor: isArea ? 'rgba(232,255,71,.1)' : '#e8ff47',
      borderColor: '#e8ff47', borderWidth:2,
      tension: aggTension, fill: isArea,
      pointRadius: isLine && !isArea ? 3 : 0,
      pointBackgroundColor:'#e8ff47',
    }];
  }

  // Split by active category
  return catKeys
    .filter(cat => CATEGORIES[cat].kinds.some(k => activeKinds.has(k)))
    .map(cat => {
      const meta = CATEGORIES[cat];
      const col  = meta.color;
      return {
        label: meta.label,
        data:  months.map(m =>
          meta.kinds.reduce((s,k) => s + ((computed.byKind[m]||{})[k]||0), 0)
        ),
        backgroundColor: isArea ? col+'22' : col+'cc',
        borderColor: col, borderWidth:2,
        tension: aggTension, fill: isArea,
        pointRadius: isLine && !isArea ? 2 : 0,
        stack: !isLine ? 'stack' : undefined,
      };
    });
}

function initAgg() {
  chartAgg = new Chart(document.getElementById('chartAgg').getContext('2d'), {
    type: aggType==='bar' ? 'bar' : 'line',
    data: { labels: allMonths, datasets: aggDS() },
    options:{
      responsive:true, animation:{duration:300},
      plugins:{
        legend:{display:aggSplit, position:'bottom', labels:{boxWidth:10,padding:8}},
        tooltip:{mode:'index', callbacks:{
          afterBody: items => {
            const m = allMonths[items[0].dataIndex];
            const rows = [];
            catKeys.forEach(cat => {
              CATEGORIES[cat].kinds.forEach(k => {
                const v = (computed.byKind[m]||{})[k]||0;
                if (v) rows.push(`  ${k.replace(/_/g,' ')}: ${v}`);
              });
            });
            return rows;
          }
        }}
      },
      scales:{
        x:{...CS.x, stacked: !aggSplit || aggType==='bar'},
        y:{...CS.y, stacked: !aggSplit || aggType==='bar'},
      },
    }
  });
}

function updateAgg() {
  chartAgg.config.type = aggType==='bar' ? 'bar' : 'line';
  chartAgg.data.datasets = aggDS();
  chartAgg.options.plugins.legend.display = aggSplit;
  chartAgg.options.scales.x.stacked = !aggSplit || aggType==='bar';
  chartAgg.options.scales.y.stacked = !aggSplit || aggType==='bar';
  chartAgg.update();
}

function setAggType(t) {
  aggType = t;
  ['aggBar','aggLine','aggArea'].forEach(id =>
    document.getElementById(id).classList.remove('active'));
  document.getElementById({bar:'aggBar',line:'aggLine',area:'aggArea'}[t]).classList.add('active');
  updateAgg();
}
function updateSmooth(v) { aggTension = v/10; updateAgg(); }
function toggleSplit() {
  aggSplit = !aggSplit;
  const btn = document.getElementById('aggSplit');
  btn.textContent = aggSplit ? 'ON' : 'OFF';
  btn.classList.toggle('active', aggSplit);
  updateAgg();
}

// ── 2 · Repo comparison ───────────────────────────────────────────────────────
let chartRC, rcMode='stacked';

function rcDS() {
  return allRepos.map((r,i) => ({
    label: r,
    data:  allMonths.map(m => (computed.byR[r]||{})[m]||0),
    backgroundColor: RCOL[i]+'cc', borderColor: RCOL[i], borderWidth:2,
    fill: false, tension: rcMode==='line' ? 0.35 : 0,
    stack: rcMode==='stacked' ? 'stack' : undefined,
    pointRadius: rcMode==='line' ? 2 : 0,
  }));
}

function initRC() {
  document.getElementById('repoLegend').innerHTML =
    allRepos.map((r,i) =>
      `<span class="rldot"><i style="background:${RCOL[i]}"></i>${r}</span>`
    ).join('');
  chartRC = new Chart(document.getElementById('chartRC').getContext('2d'), {
    type: rcMode==='line' ? 'line' : 'bar',
    data: { labels: allMonths, datasets: rcDS() },
    options:{
      responsive:true, animation:{duration:300},
      plugins:{ legend:{display:false}, tooltip:{mode:'index'} },
      scales:{
        x:{...CS.x, stacked:rcMode==='stacked'},
        y:{...CS.y, stacked:rcMode==='stacked'},
      }
    }
  });
}

function updateRC() {
  if (!chartRC) return;
  chartRC.data.datasets = rcDS();
  chartRC.update();
}

function setRCMode(m) {
  rcMode = m;
  ['rcStacked','rcGrouped','rcLine'].forEach(id =>
    document.getElementById(id).classList.remove('active'));
  document.getElementById({stacked:'rcStacked',grouped:'rcGrouped',line:'rcLine'}[m]).classList.add('active');
  chartRC.config.type = m==='line' ? 'line' : 'bar';
  chartRC.data.datasets = rcDS();
  chartRC.options.scales.x.stacked = m==='stacked';
  chartRC.options.scales.y.stacked = m==='stacked';
  chartRC.update();
}

// ── 3 · Contributor stacked ───────────────────────────────────────────────────
let chartStacked, stackedMode='stacked';

function renderStacked() {
  const N    = parseInt(document.getElementById('topN').value);
  const top  = visibleContribs().slice(0, N);
  const ds   = top.map((c,i) => ({
    label: c,
    data:  allMonths.map(m => valC(c,m)),
    backgroundColor: cc(i)+'cc', borderColor: cc(i), borderWidth:1,
    stack: stackedMode==='stacked' ? 'stack' : undefined,
  }));
  if (chartStacked) chartStacked.destroy();
  chartStacked = new Chart(document.getElementById('chartStacked').getContext('2d'), {
    type:'bar', data:{ labels:allMonths, datasets:ds },
    options:{
      responsive:true, animation:{duration:250},
      plugins:{
        legend:{position:'bottom', labels:{boxWidth:9,padding:6}},
        tooltip:{mode:'index'}
      },
      scales:{
        x:{...CS.x, stacked:stackedMode==='stacked'},
        y:{...CS.y, stacked:stackedMode==='stacked'},
      },
      onClick:(_,els) => {
        if (els.length) drillContributor(chartStacked.data.datasets[els[0].datasetIndex].label);
      }
    }
  });
}
function setStackedMode(m) {
  stackedMode = m;
  document.getElementById('btnStacked').classList.toggle('active', m==='stacked');
  document.getElementById('btnGrouped').classList.toggle('active', m==='grouped');
  renderStacked();
}

// ── 4a · Heatmap ──────────────────────────────────────────────────────────────
function renderHeatmap() {
  const contrib = document.getElementById('hmContrib').value;
  const vals    = allMonths.map(m =>
    contrib==='__all__' ? totalM(m) : valC(contrib,m)
  );
  const max = Math.max(...vals, 1);

  const byYear = {};
  allMonths.forEach((m,i) => {
    const [y,mo] = m.split('-');
    if (!byYear[y]) byYear[y] = {};
    byYear[y][parseInt(mo)] = vals[i];
  });

  const MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  let h = '<table class="hm"><thead><tr><th></th>';
  MON.forEach(n => h += `<th>${n}</th>`);
  h += '</tr></thead><tbody>';
  Object.keys(byYear).sort().forEach(yr => {
    h += `<tr><th>${yr}</th>`;
    for (let mo=1;mo<=12;mo++) {
      const v  = byYear[yr][mo]||0;
      const t  = v/max;
      const bg = v===0 ? '#1c1f27'
        : `rgba(${~~(232*t+20*(1-t))},${~~(255*t+25*(1-t))},${~~(71*t+39*(1-t))},${.12+.88*t})`;
      const ms = `${yr}-${String(mo).padStart(2,'0')}`;
      h += `<td style="background:${bg}"
        onmouseenter="showTT(event,'${ms}',${v},'${contrib}')"
        onmouseleave="hideTT()"
        onclick="filterByMonth('${ms}')">${v||''}</td>`;
    }
    h += '</tr>';
  });
  h += '</tbody></table>';
  document.getElementById('heatmap-wrap').innerHTML = h;
}

const tt = document.getElementById('tt');
function showTT(e, month, val, contrib) {
  let inner = `<div class="ttm">${month}</div>`;
  inner += `<div class="ttr"><span>${contrib==='__all__'?'TOTAL':contrib}</span><span>${val}</span></div>`;

  if (activeRepo==='__all__' && allRepos.length>1) {
    inner += '<div class="ttd"></div>';
    allRepos.forEach((r,i) => {
      const rv = (computed.byR[r]||{})[month]||0;
      if (rv) inner += `<div class="ttr"><span style="color:${RCOL[i]}">${r.split('/')[1]}</span><span>${rv}</span></div>`;
    });
  }

  const kinds = computed.byKind[month]||{};
  if (Object.keys(kinds).length) {
    inner += '<div class="ttd"></div>';
    catKeys.forEach(cat => {
      const meta = CATEGORIES[cat];
      meta.kinds.forEach(k => {
        const v = kinds[k]||0;
        if (v) inner += `<div class="ttr"><span style="color:${meta.color}">${k.replace(/_/g,' ')}</span><span>${v}</span></div>`;
      });
    });
  }
  tt.innerHTML = inner;
  tt.classList.add('show');
  moveTT(e);
}
function showContribTT(e, login, filterMonth) {
  const body = buildContribRefTooltip(login, filterMonth);
  if (!body) { hideTT(); return; }
  const total = allMonths.reduce((s,m) => s + valC(login, m), 0);
  const monthLabel = filterMonth ? ` · ${filterMonth}` : ' · all time';
  tt.innerHTML = `<div class="ttm">${login}<span style="color:var(--muted);font-size:.65rem;margin-left:.5rem">${monthLabel}</span></div>${body}`;
  tt.classList.add('show');
  moveTT(e);
}
function hideTT() { tt.classList.remove('show'); }
function moveTT(e) {
  tt.style.left = Math.min(e.clientX+14, window.innerWidth-280)+'px';
  tt.style.top  = (e.clientY-10)+'px';
}
document.addEventListener('mousemove', e => { if(tt.classList.contains('show')) moveTT(e); });

// ── Contributor ref lookup ───────────────────────────────────────────────────
// Returns grouped refs for a contributor, optionally filtered to one month.
// Result: { kind: Set<ref> }  — only refs that are active kinds, only non-empty refs.
function getContribRefs(login, filterMonth) {
  const repos = activeRepo === '__all__' ? allRepos : [activeRepo];
  const byKindRef   = {};  // {kind: Set<ref>}
  const byKindChars = {};  // {kind: number[]}  — chars per event for stats
  for (const repo of repos) {
    for (const [author, month, kind, ref, chars] of (rawByRepo[repo] || [])) {
      if (author !== login) continue;
      if (!activeKinds.has(kind)) continue;
      if (filterMonth && month !== filterMonth) continue;
      if (ref) {
        if (!byKindRef[kind]) byKindRef[kind] = new Set();
        byKindRef[kind].add(ref);
      }
      if (chars != null) {
        if (!byKindChars[kind]) byKindChars[kind] = [];
        byKindChars[kind].push(chars);
      }
    }
  }
  return { byKindRef, byKindChars };
}

// Compute median of a sorted (or unsorted) array
function median(arr) {
  if (!arr.length) return null;
  const s = [...arr].sort((a,b) => a-b);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : Math.round((s[m-1] + s[m]) / 2);
}

// Format chars stat: "med 142 chars · avg 188"
function fmtCharsStat(arr) {
  if (!arr || !arr.length) return '';
  const med = median(arr);
  const avg = Math.round(arr.reduce((a,b)=>a+b,0) / arr.length);
  const hasBody = arr.filter(x => x > 0).length;
  const pctBody = Math.round(hasBody / arr.length * 100);
  if (pctBody === 0) return 'no body text';
  return `med ${med}c · avg ${avg}c · ${pctBody}% non-empty`;
}

// Format a Set of refs into a compact comma-separated string, capped at maxShow.
function fmtRefs(refSet, maxShow = 12) {
  const arr = [...refSet].sort((a, b) => {
    // sort numerically by the number part
    const na = parseInt(a.replace(/\D/g, '')) || 0;
    const nb = parseInt(b.replace(/\D/g, '')) || 0;
    return na - nb;
  });
  if (arr.length <= maxShow) return arr.join(' ');
  return arr.slice(0, maxShow).join(' ') + ` +${arr.length - maxShow}`;
}

// Build the HTML for the contributor ref tooltip (used in leaderboard hover).
function buildContribRefTooltip(login, filterMonth) {
  const { byKindRef, byKindChars } = getContribRefs(login, filterMonth);
  const hasRefs  = Object.values(byKindRef).some(s => s.size > 0);
  const hasChars = Object.values(byKindChars).some(a => a.length > 0);
  if (!hasRefs && !hasChars) return '';

  let html = '';
  catKeys.forEach(cat => {
    const meta = CATEGORIES[cat];
    const activeInCat = meta.kinds.filter(k =>
      (byKindRef[k]?.size > 0) || (byKindChars[k]?.length > 0)
    );
    if (!activeInCat.length) return;
    html += `<div class="ctref-cat" style="color:${meta.color}">${meta.label}</div>`;
    activeInCat.forEach(k => {
      const label = k.replace(/_/g, '\u00a0');
      const refs  = byKindRef[k]?.size   ? fmtRefs(byKindRef[k]) : '';
      const stats = byKindChars[k]?.length ? fmtCharsStat(byKindChars[k]) : '';
      html += `<div class="ctref-row"><span class="ctref-kind">${label}</span><span class="ctref-refs">${refs}</span></div>`;
      if (stats) html += `<div class="ctref-stat">${stats}</div>`;
    });
  });
  return html;
}

// ── 4b · Leaderboard ─────────────────────────────────────────────────────────
let selectedContrib = null;

function renderLeaderboard() {
  const month = document.getElementById('lbMonth').value;
  const cs    = visibleContribs();
  const ranked = cs.map(c => {
    const count = month==='__all__'
      ? allMonths.reduce((s,m) => s+valC(c,m), 0)
      : valC(c,month);
    return { c, count };
  }).filter(x=>x.count>0).sort((a,b)=>b.count-a.count);

  const max = ranked[0]?.count||1;
  let html = '';
  ranked.forEach(({c,count},i) => {
    const col   = cc(i);
    const pct   = (count/max*100).toFixed(1);
    const active= c===selectedContrib ? 'selected':'';
    // Repo chips
    let chips = '';
    if (activeRepo==='__all__' && allRepos.length>1) {
      const crs = allRepos.map((r,ri)=>{
        const rv = month==='__all__'
          ? allMonths.reduce((s,m) => s+((computed.byCR[c]||{})[r]||{})[m]||0, 0)
          : ((computed.byCR[c]||{})[r]||{})[month]||0;
        return rv ? `<span class="repo-chip" style="background:${RCOL[ri]}22;color:${RCOL[ri]}">${r.split('/')[1]}&thinsp;${rv}</span>` : '';
      }).filter(Boolean).join('');
      if (crs) chips = `<div class="repo-chips">${crs}</div>`;
    }
    const filterMonthForHover = month === '__all__' ? null : month;
    html += `<div class="contrib-row ${active}"
      onclick="drillContributor('${c}')"
      onmouseenter="showContribTT(event,'${c}',${JSON.stringify(filterMonthForHover)})"
      onmouseleave="hideTT()">
      <span class="contrib-rank">#${i+1}</span>
      <span class="contrib-name" style="color:${col}">${c}${chips}</span>
      <div class="contrib-bar-wrap"><div class="contrib-bar" style="width:${pct}%;background:${col}"></div></div>
      <span class="contrib-count">${count}</span>
      <button class="lb-mute-btn" title="Mute this contributor"
        onclick="event.stopPropagation();muteContributor('${c}')">⊘</button>
    </div>`;
  });
  document.getElementById('leaderboard').innerHTML =
    html || '<span class="empty">No data for this selection.</span>';
}

function filterByMonth(m) {
  document.getElementById('lbMonth').value = m;
  renderLeaderboard();
}

// ── 5 · Drilldown ─────────────────────────────────────────────────────────────
let chartDrill;

function drillContributor(login) {
  selectedContrib = login;
  renderLeaderboard();
  const total = allMonths.reduce((s,m)=>s+valC(login,m),0);
  document.getElementById('drillTitle').textContent =
    `CONTRIBUTOR · ${login.toUpperCase()} · ${total.toLocaleString()} TOTAL`;
  document.getElementById('drillCard').style.display = '';

  const multiRepo = activeRepo==='__all__' && allRepos.length>1;
  let ds, legendHtml='';
  if (multiRepo) {
    ds = allRepos.map((r,i) => ({
      label: r,
      data:  allMonths.map(m => ((computed.byCR[login]||{})[r]||{})[m]||0),
      backgroundColor: RCOL[i]+'cc', borderColor: RCOL[i],
      borderWidth:1, borderRadius:3, stack:'stack',
    }));
    legendHtml = allRepos.map((r,i) =>
      `<span class="rldot"><i style="background:${RCOL[i]}"></i>${r}</span>`
    ).join('');
  } else {
    ds = [{
      label: login,
      data:  allMonths.map(m=>valC(login,m)),
      backgroundColor:'#47d4ffcc', borderColor:'#47d4ff',
      borderWidth:1, borderRadius:3,
    }];
  }
  document.getElementById('drillLegend').innerHTML = legendHtml;

  if (chartDrill) chartDrill.destroy();
  chartDrill = new Chart(document.getElementById('chartDrill').getContext('2d'), {
    type:'bar', data:{ labels:allMonths, datasets:ds },
    options:{
      responsive:true,
      plugins:{
        legend:{display:multiRepo, position:'bottom', labels:{boxWidth:9,padding:6}},
        tooltip:{mode:'index', callbacks:{
          afterBody: items => {
            const m = allMonths[items[0].dataIndex];
            const rows = [];
            catKeys.forEach(cat => {
              const meta = CATEGORIES[cat];
              meta.kinds.forEach(k => {
                if (!activeKinds.has(k)) return;
                const repoList = activeRepo==='__all__' ? allRepos : [activeRepo];
                const refs  = new Set();
                const chars = [];
                for (const r of repoList) {
                  (rawByRepo[r]||[]).forEach(([a,mo,ki,ref,ch]) => {
                    if (a!==login || mo!==m || ki!==k) return;
                    if (ref) refs.add(ref);
                    if (ch != null) chars.push(ch);
                  });
                }
                if (refs.size || chars.length) {
                  const refStr   = refs.size  ? fmtRefs(refs, 6) : '';
                  const statStr  = chars.length ? fmtCharsStat(chars) : '';
                  const kindLbl  = k.replace(/_/g,' ');
                  if (refStr)  rows.push(`  ${kindLbl}: ${refStr}`);
                  if (statStr) rows.push(`    └ ${statStr}`);
                }
              });
            });
            return rows;
          }
        }}
      },
      scales:{
        x:{...CS.x, stacked:multiRepo},
        y:{...CS.y, stacked:multiRepo},
      }
    }
  });
  document.getElementById('drillCard').scrollIntoView({behavior:'smooth',block:'nearest'});
}

function clearDrill() {
  selectedContrib = null;
  document.getElementById('drillCard').style.display = 'none';
  if (chartDrill) { chartDrill.destroy(); chartDrill=null; }
  renderLeaderboard();
}

// ── Selects ───────────────────────────────────────────────────────────────────
function rebuildContribSelect() {
  const sel = document.getElementById('hmContrib');
  const cur = sel.value;
  sel.innerHTML = '<option value="__all__">ALL</option>';
  visibleContribs().forEach(c => {
    const o = document.createElement('option');
    o.value=c; o.textContent=c;
    if (c===cur) o.selected=true;
    sel.appendChild(o);
  });
}
function buildMonthSelect() {
  const sel = document.getElementById('lbMonth');
  allMonths.forEach(m => {
    const o = document.createElement('option'); o.value=m; o.textContent=m;
    sel.appendChild(o);
  });
}

// ── Mute / what-if contributor filter ────────────────────────────────────────
let sugFocusIdx = -1;

function muteContributor(login) {
  if (!login || mutedContribs.has(login)) return;
  mutedContribs.add(login);
  document.getElementById('muteSearch').value = '';
  closeSuggestions();
  renderMuteChips();
  refreshAll();
}

function unmuteContributor(login) {
  mutedContribs.delete(login);
  renderMuteChips();
  refreshAll();
}

function unmuteAll() {
  mutedContribs.clear();
  renderMuteChips();
  refreshAll();
}

function renderMuteChips() {
  const wrap     = document.getElementById('muteChips');
  const hint     = document.getElementById('muteHint');
  const unmuteBtn= document.getElementById('muteUnmuteAll');
  const muted    = [...mutedContribs];

  if (!muted.length) {
    wrap.innerHTML = '';
    hint.style.display  = '';
    unmuteBtn.style.display = 'none';
    return;
  }

  hint.style.display  = 'none';
  unmuteBtn.style.display = '';

  wrap.innerHTML = muted.map(login =>
    `<span class="mchip">
      <span>${login}</span>
      <span class="mchip-x" onclick="unmuteContributor('${login}')" title="Unmute">✕</span>
    </span>`
  ).join('');
}

// Search / autocomplete
function updateMuteSearch(val) {
  sugFocusIdx = -1;
  if (!val.trim()) { closeSuggestions(); return; }
  const q     = val.toLowerCase();
  // Pool: all known contributors across all repos (from raw data, not filtered)
  const pool  = [...new Set(allRepos.flatMap(r =>
    (rawByRepo[r] || []).map(([a]) => a)
  ))].filter(a => a && !mutedContribs.has(a) && a.toLowerCase().includes(q));
  // Sort: exact prefix first, then alphabetical
  pool.sort((a, b) => {
    const aP = a.toLowerCase().startsWith(q);
    const bP = b.toLowerCase().startsWith(q);
    if (aP && !bP) return -1;
    if (bP && !aP) return  1;
    return a.localeCompare(b);
  });
  const top = pool.slice(0, 10);
  if (!top.length) { closeSuggestions(); return; }

  const el = document.getElementById('muteSuggestions');
  el.innerHTML = top.map((login, i) => {
    const esc   = login.replace(/</g, '&lt;');
    const highlighted = esc.replace(
      new RegExp('(' + q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'i'),
      '<mark>$1</mark>'
    );
    return `<div class="msug" data-login="${esc}" data-idx="${i}"
      onclick="muteContributor('${esc}')"
      onmouseenter="sugFocusIdx=${i};highlightSug()">${highlighted}</div>`;
  }).join('');
  el.classList.add('open');
}

function muteSearchKey(e) {
  const el   = document.getElementById('muteSuggestions');
  const items = el.querySelectorAll('.msug');
  if (e.key === 'ArrowDown')  { e.preventDefault(); sugFocusIdx = Math.min(sugFocusIdx+1, items.length-1); highlightSug(); }
  if (e.key === 'ArrowUp')    { e.preventDefault(); sugFocusIdx = Math.max(sugFocusIdx-1, 0); highlightSug(); }
  if (e.key === 'Enter' && sugFocusIdx >= 0) {
    const chosen = items[sugFocusIdx]?.dataset?.login;
    if (chosen) muteContributor(chosen);
  }
  if (e.key === 'Escape') closeSuggestions();
}

function highlightSug() {
  document.querySelectorAll('.msug').forEach((el, i) =>
    el.classList.toggle('focused', i === sugFocusIdx)
  );
}

function closeSuggestions() {
  document.getElementById('muteSuggestions').classList.remove('open');
  document.getElementById('muteSuggestions').innerHTML = '';
}

// Close suggestions when clicking outside
document.addEventListener('click', e => {
  if (!e.target.closest('.mute-search-wrap')) closeSuggestions();
});

// ── Help panel ────────────────────────────────────────────────────────────────
function toggleHelp() {
  document.getElementById('helpOverlay').classList.toggle('open');
}
function closeHelpIfBg(e) {
  if (e.target === document.getElementById('helpOverlay')) toggleHelp();
}
function showTab(name) {
  document.querySelectorAll('.hpage').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.htab').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  event.currentTarget.classList.add('active');
  document.getElementById('helpBody').scrollTop = 0;
}
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') document.getElementById('helpOverlay').classList.remove('open');
  if (e.key === '?' && !e.ctrlKey && !e.metaKey && !e.altKey) { e.preventDefault(); toggleHelp(); }
  if (e.key === 'F1') { e.preventDefault(); toggleHelp(); }
});

// ── Boot ──────────────────────────────────────────────────────────────────────
buildTabs();
buildTypeToggles();
buildMonthSelect();
recompute();
rebuildContribSelect();
updatePills();
initAgg();
if (allRepos.length>1) {
  initRC();
} else {
  document.getElementById('repoCompareCard').style.display='none';
}
renderStacked();
renderHeatmap();
renderLeaderboard();
</script>
</body>
</html>
"""


def build_html(data: dict, title: str):
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    html = HTML_TEMPLATE
    html = html.replace("{{TITLE}}",     title)
    html = html.replace("{{GENERATED}}", generated)
    html = html.replace("{{DATA_JSON}}", json.dumps(data, ensure_ascii=False))
    return html


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="GitHub activity visualizer — multi-repo + contribution indicators",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single repo
  python gh_activity.py open-quantum-safe/liboqs --token $GH_TOKEN

  # Multiple repos → combined report
  python gh_activity.py open-quantum-safe/liboqs open-quantum-safe/oqs-provider \\
      --token $GH_TOKEN --since 2022-01

  # Ecosystem comparison with custom output
  python gh_activity.py facebook/react facebook/relay facebook/jest \\
      --token $GH_TOKEN --out react_ecosystem.html

  # Re-fetch ignoring cache
  python gh_activity.py torvalds/linux --token $GH_TOKEN --no-cache
        """
    )
    parser.add_argument("repos", nargs="+",
                        help="One or more owner/repo strings")
    parser.add_argument("--token", default=os.environ.get("GH_TOKEN"),
                        help="GitHub PAT (or set GH_TOKEN env var)")
    parser.add_argument("--since", metavar="YYYY-MM",
                        help="Include only data from this month onwards")
    parser.add_argument("--out", default=None,
                        help="Output HTML filename (auto-generated if omitted)")
    parser.add_argument("--no-cache", action="store_true",
                        help="Ignore cached data and re-fetch everything")

    args = parser.parse_args()

    for r in args.repos:
        if "/" not in r:
            parser.error(f"'{r}' is not a valid owner/repo string")

    if not args.token:
        parser.error("GitHub token required: --token TOKEN or export GH_TOKEN=...")

    client = GitHubClient(args.token)
    since  = args.since

    all_events: list = []
    repo_list:  list = []

    for repo_str in args.repos:
        owner, repo = repo_str.split("/", 1)
        repo_full   = f"{owner}/{repo}"
        repo_list.append(repo_full)

        print(f"\n🔍  Analyzing {repo_full}" + (f"  (since {since})" if since else ""))

        events = None
        if not args.no_cache:
            cached = load_cache(owner, repo)
            if cached:
                def fix(e):
                    # Migrate old cache entries to current 6-tuple format:
                    # (author, month, kind, repo, ref, chars)
                    if len(e) == 6: return tuple(e)
                    if len(e) == 5: return (e[0], e[1], e[2], e[3], e[4], None)
                    if len(e) == 4: return (e[0], e[1], e[2], e[3], "", None)
                    return (e[0], e[1], e[2], repo_full, "", None)
                events = [fix(e) for e in cached]
                print(f"  📂 Loaded {len(events):,} events from cache  (--no-cache to refresh)")

        if events is None:
            print("  🌐 Fetching from GitHub API …")
            events = collect(owner, repo, client, since)
            save_cache(owner, repo, events)
            print(f"  ✅ Collected {len(events):,} interactions")

        if since:
            events = [e for e in events if e[1] >= since]

        all_events.extend(events)

    # ── Aggregate ──────────────────────────────────────────────────────────────
    print(f"\n📊  Aggregating {len(all_events):,} events across {len(repo_list)} repo(s) …")
    data = aggregate_multi(all_events, repo_list)

    months = data["months"]
    total  = sum(data["totals"].values())
    print(f"    Period:       {months[0] if months else '?'} → {months[-1] if months else '?'}")
    print(f"    Contributors: {len(data['contributors'])}")
    print(f"    Total events: {total:,}")

    # Print per-type summary
    all_types = defaultdict(int)
    for m_types in data["by_type"].values():
        for k, v in m_types.items():
            all_types[k] += v
    print("    Event types:")
    for cat, meta in CATEGORIES.items():
        cat_total = sum(all_types[k] for k in meta["kinds"])
        print(f"      {meta['label']}: {cat_total:,}")
        for k in meta["kinds"]:
            if all_types[k]:
                print(f"        {k}: {all_types[k]:,}")

    print("    Per-repo:")
    for r in repo_list:
        n = sum((data["by_repo"].get(r) or {}).values())
        print(f"      {r}: {n:,}")

    if data["contributors"]:
        print("    Top 5 contributors:")
        for c in data["contributors"][:5]:
            n = sum(data["by_contributor"][c].values())
            print(f"      {c}: {n:,}")

    # ── Build HTML ─────────────────────────────────────────────────────────────
    title    = repo_list[0] if len(repo_list)==1 else " + ".join(r.split("/")[1] for r in repo_list)
    out_file = args.out or ("_".join(r.replace("/","_") for r in repo_list) + "_activity.html")

    html = build_html(data, title)
    Path(out_file).write_text(html, encoding="utf-8")
    print(f"\n🎉  Report written → {out_file}")
    print(f"    Open in browser: file://{Path(out_file).resolve()}\n")


if __name__ == "__main__":
    main()
