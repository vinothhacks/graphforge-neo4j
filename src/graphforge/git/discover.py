"""Optional: discover repositories from a GitLab group via the REST API."""
from __future__ import annotations

import logging

log = logging.getLogger("graphforge.git.discover")

#: Hard ceiling on pagination. 100 projects a page, so this is 50,000 repositories
#: -- far past any real group, and a bound where there was none.
MAX_PAGES = 500


def gitlab_group_repos(server: str, group_id: str, token: str = "",
                       default_branch: str = "main", since_days: int = 0) -> list[dict]:
    """Return repo specs for every project in a GitLab group (incl. subgroups).

    If ``since_days`` > 0, only projects with activity in that window are
    returned (the "recent" incremental mode).
    """
    from datetime import datetime, timedelta, timezone

    try:
        import requests  # lazy: GitLab discovery is the only thing that needs it
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "GitLab discovery is not installed. Run: "
            "pip install 'graphforge-neo4j[gitlab]'"
        ) from exc

    server = server.rstrip("/")
    headers = {"PRIVATE-TOKEN": token} if token else {}
    params_base = {"include_subgroups": "true", "per_page": 100,
                   "archived": "false", "simple": "true",
                   "order_by": "last_activity_at", "sort": "desc"}
    if since_days and since_days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
        params_base["last_activity_after"] = cutoff.isoformat()
    specs: list[dict] = []
    seen: set[str] = set()
    for page in range(1, MAX_PAGES + 1):
        resp = requests.get(
            f"{server}/api/v4/groups/{group_id}/projects",
            params={**params_base, "page": page},
            headers=headers, timeout=60,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        fresh = 0
        for proj in batch:
            url = proj["http_url_to_repo"]
            # A proxy or a misbehaving API that keeps returning the same page
            # would otherwise spin forever: the old loop was `while True` with
            # no bound and no check that a page contained anything new.
            if url in seen:
                continue
            seen.add(url)
            fresh += 1
            specs.append({
                "name": proj["path"],
                "url": url,
                "branch": proj.get("default_branch") or default_branch,
            })
        if not fresh:
            log.warning("GitLab returned no new projects on page %d; stopping", page)
            break
    else:
        log.warning("stopped after %d pages of GitLab results (MAX_PAGES)", MAX_PAGES)
    log.info("Discovered %d repositories from GitLab group %s", len(specs), group_id)
    return specs
