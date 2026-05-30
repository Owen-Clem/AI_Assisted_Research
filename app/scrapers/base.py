from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class RawArticle:
    url: str
    title: str
    published_at: str  # ISO8601 string
    content_text: str  # HTML-stripped plain text
    excerpt: str       # first ~500 chars of content_text


class BaseScraper(ABC):
    def __init__(self, source_config: dict):
        self.name = source_config["name"]
        self.url = source_config["url"]
        self.reputation = source_config["reputation"]

    @abstractmethod
    async def fetch(self, lookback_days: int) -> list[RawArticle]:
        """Return articles published within the last lookback_days."""
        ...
