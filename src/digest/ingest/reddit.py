"""Reddit ingestor — pulls top-of-day posts from configured subreddits via PRAW."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import praw
import yaml

from digest.config import settings
from digest.ingest.base import IngestedItem, IngestorBase

logger = logging.getLogger(__name__)

SUBREDDITS_CONFIG = Path(__file__).resolve().parents[3] / "config" / "subreddits.yaml"


class RedditIngestor(IngestorBase):
    name = "reddit"

    def __init__(self) -> None:
        if not settings.reddit_client_id:
            raise RuntimeError("REDDIT_CLIENT_ID not set")
        self.reddit = praw.Reddit(
            client_id=settings.reddit_client_id,
            client_secret=settings.reddit_client_secret,
            user_agent=settings.reddit_user_agent,
        )
        self.reddit.read_only = True
        self.config = yaml.safe_load(SUBREDDITS_CONFIG.read_text())

    def fetch(self) -> list[IngestedItem]:
        items: list[IngestedItem] = []
        for group in self.config["groups"]:
            group_name = group["name"]
            min_score = group.get("min_score", 50)
            min_comments = group.get("min_comments", 10)
            limit = group.get("limit", 10)
            time_filter = group.get("time_filter", "day")
            for sub_name in group["subreddits"]:
                try:
                    sub = self.reddit.subreddit(sub_name)
                    for post in sub.top(time_filter=time_filter, limit=limit):
                        if post.score < min_score or post.num_comments < min_comments:
                            continue
                        if post.stickied:
                            continue
                        items.append(
                            IngestedItem(
                                source=self.name,
                                source_id=post.id,
                                title=post.title,
                                url=f"https://reddit.com{post.permalink}",
                                author=str(post.author) if post.author else None,
                                content=post.selftext or "",
                                published_at=datetime.fromtimestamp(
                                    post.created_utc, tz=timezone.utc
                                ),
                                metadata={
                                    "subreddit": sub_name,
                                    "group": group_name,
                                    "score": post.score,
                                    "num_comments": post.num_comments,
                                    "external_url": post.url if not post.is_self else None,
                                },
                            )
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("reddit: failed on r/%s: %s", sub_name, exc)
        return items
