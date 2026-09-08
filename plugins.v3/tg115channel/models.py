"""Small transport-neutral models used by the Telegram and MoviePilot adapters."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ReplyButton:
    """Serializable projection of one Telegram inline button."""

    text: str
    url: str = ""
    row: int = -1
    column: int = -1
    callback: bool = False


@dataclass(frozen=True, slots=True)
class BotReply:
    """Serializable subset of a Telegram bot reply."""

    message_id: int | None
    text: str
    buttons: tuple[ReplyButton, ...] = ()


@dataclass(frozen=True, slots=True)
class BotResource:
    """A 115 share discovered in one Telegram reply."""

    title: str
    url: str
    access_code: str = ""
    size: int = 0
    quality: str = ""
    message_id: int | None = None

    def to_record(self, *, media_type: str, keyword: str, discovered_at: float) -> dict[str, Any]:
        record = asdict(self)
        record.update(
            {
                "media_type": media_type,
                "keyword": keyword,
                "discovered_at": float(discovered_at),
            }
        )
        return record

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> BotResource:
        return cls(
            title=str(record.get("title") or ""),
            url=str(record.get("url") or ""),
            access_code=str(record.get("access_code") or ""),
            size=max(0, int(record.get("size") or 0)),
            quality=str(record.get("quality") or ""),
            message_id=(int(record["message_id"]) if record.get("message_id") is not None else None),
        )


@dataclass(frozen=True, slots=True)
class RequestResult:
    """Replies collected for a single serialized request to the resource bot."""

    replies: tuple[BotReply, ...]

    @property
    def text(self) -> str:
        return "\n".join(reply.text for reply in self.replies if reply.text)
