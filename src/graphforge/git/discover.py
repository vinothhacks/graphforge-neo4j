"""Optional: discover repositories from a GitLab group via the REST API."""
from __future__ import annotations

import logging

log = logging.getLogger("graphforge.git.discover")


def gitlab_group_repos(server: str, group_id: str, token: str = "",
                       default_branch: str = "main", since_days: int = 0) -> list[dict]:
    """Return repo specs for every project in a GitLab group (incl. subgroups).

    If ``since_days`` > 0, only projects with activity in that window are
    returned (the "recent" incremental mode).
    """
    from datetime import datetime, timedelta, timezone

    import requests  # lazy; only needed for discovery

    server = server.rstrip("/")
    headers = {"PRIVATE-TOKEN": token} if token else {}
    params_base = {"include_subgroups": "true", "per_page": 100,
                   "archived": "false", "simple": "true",
                   "order_by": "last_activity_at", "sort": "desc"}
    if since_days and since_days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
        params_base["last_activity_after"] = cutoff.isoformat()
    specs: list[dict] = []
    page = 1
    while True:
        resp = requests.get(
            f"{server}/api/v4/groups/{group_id}/projects",
            params={**params_base, "page": page},
            headers=headers, timeout=60,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        for proj in batch:
            specs.append({
                "name": proj["path"],
                "url": proj["http_url_to_repo"],
                "branch": proj.get("default_branch") or default_branch,
            })
        page += 1
    log.info("Discovered %d repositories from GitLab group %s", len(specs), group_id)
    return specs
