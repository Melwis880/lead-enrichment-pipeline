"""robots.txt handling following RFC 9309 status-code semantics."""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.robotparser import RobotFileParser


@dataclass
class RobotsPolicy:
    """Answers "may we fetch this URL?" for one origin.

    RFC 9309 section 2.3.1:
    - 2xx: parse and obey the file.
    - 4xx ("unavailable", including 401/403): no rules, crawling allowed.
    - 5xx or network failure ("unreachable"): assume complete disallow.
    """

    user_agent: str
    mode: str  # "rules" | "allow_all" | "disallow_all"
    reason: str = ""
    _parser: RobotFileParser | None = field(default=None, repr=False)

    @classmethod
    def from_text(cls, text: str, user_agent: str) -> RobotsPolicy:
        parser = RobotFileParser()
        parser.parse(text.splitlines())
        return cls(user_agent=user_agent, mode="rules", reason="robots.txt rules", _parser=parser)

    @classmethod
    def from_status(cls, status: int | None, text: str, user_agent: str) -> RobotsPolicy:
        if status is None:
            return cls(user_agent, "disallow_all", "robots.txt unreachable (network error)")
        if 200 <= status < 300:
            return cls.from_text(text, user_agent)
        if 400 <= status < 500:
            return cls(user_agent, "allow_all", f"robots.txt unavailable (HTTP {status})")
        return cls(user_agent, "disallow_all", f"robots.txt unreachable (HTTP {status})")

    @property
    def _token(self) -> str:
        # Product token, e.g. "LeadEnrichBot" from "LeadEnrichBot/0.1 (+url)"
        return self.user_agent.split("/")[0].strip()

    def allowed(self, url: str) -> bool:
        if self.mode == "allow_all":
            return True
        if self.mode == "disallow_all" or self._parser is None:
            return False
        return self._parser.can_fetch(self._token, url)

    def crawl_delay(self) -> float | None:
        if self._parser is None:
            return None
        delay = self._parser.crawl_delay(self._token)
        return float(delay) if delay is not None else None
