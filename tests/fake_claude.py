"""A stand-in for the Anthropic client that returns scripted answers and records the requests."""
import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeBlock:
    text: str
    type: str = "text"


@dataclass
class FakeResponse:
    content: list[FakeBlock]
    stop_reason: str = "end_turn"


@dataclass
class FakeMessages:
    replies: list[Any]
    requests: list[dict[str, Any]] = field(default_factory=list)


    def create(self, **kwargs: Any) -> FakeResponse:
        self.requests.append(json.loads(json.dumps({k: v for k, v in kwargs.items()}, default=str)))
        reply = self.replies.pop(0)
        if isinstance(reply, FakeResponse):
            return reply
        return FakeResponse([FakeBlock(json.dumps(reply))])


class FakeClaude:
    def __init__(self, replies: list[Any]) -> None:
        self.messages = FakeMessages(replies)


def question(message: str) -> dict[str, Any]:
    return {"status": "question", "message": message, "summary": [], "parties": [], "code": ""}
