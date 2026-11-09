"""Terminal-style 'thinking' animation for Telegram.

Telegram can't run real animations in text, but it can edit a message in place.
We cycle a rotating spark glyph + changing status phrase + elapsed timer, the same
feel as a CLI spinner, until the first real token arrives.
"""
import asyncio
import contextlib
import time

from telegram.error import BadRequest, RetryAfter, TelegramError

# The spark "rotates" by cycling through glyphs of growing / shrinking size.
SPINNER = ["✦", "✧", "✶", "✷", "✸", "✹", "✺", "✹", "✸", "✷", "✶", "✧"]

THINK_PHRASES = [
    "Thinking",
    "Reasoning",
    "Connecting ideas",
    "Orbiting the question",
    "Energizing the core",
    "Weighing options",
    "Composing",
]
IMAGE_PHRASES = ["Mixing colors", "Sketching", "Painting pixels", "Adding light", "Polishing"]
LISTEN_PHRASES = ["Listening", "Transcribing", "Understanding"]
LOOK_PHRASES = ["Looking closely", "Scanning details", "Reading the scene"]
DOC_PHRASES = ["Reading", "Skimming sections", "Extracting key points"]

FRAME_INTERVAL = 0.9  # seconds; Telegram tolerates ~1 edit/sec/chat reliably


def frame_text(i: int, phrases: list[str], started: float) -> str:
    glyph = SPINNER[i % len(SPINNER)]
    phrase = phrases[(i // 3) % len(phrases)]
    dots = "." * (i % 3 + 1)
    elapsed = int(time.monotonic() - started)
    suffix = f"  ({elapsed}s)" if elapsed >= 3 else ""
    return f"{glyph} {phrase}{dots}{suffix}"


async def safe_edit(msg, text: str, **kwargs) -> bool:
    """Edit a message, swallowing 'not modified' and honouring flood waits."""
    try:
        await msg.edit_text(text, **kwargs)
        return True
    except RetryAfter as e:
        await asyncio.sleep(min(e.retry_after, 5) + 0.2)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise
    except TelegramError:
        pass
    return False


class Thinking:
    """Async context manager: animates `msg` until stopped / first token arrives."""

    def __init__(self, msg, phrases: list[str] | None = None, header: str = ""):
        self.msg = msg
        self.header = header
        self.phrases = phrases or THINK_PHRASES
        self._task: asyncio.Task | None = None

    async def _run(self) -> None:
        started = time.monotonic()
        i = 0
        while True:
            frame = frame_text(i, self.phrases, started)
            await safe_edit(self.msg, f"{self.header}\n\n{frame}" if self.header else frame)
            i += 1
            await asyncio.sleep(FRAME_INTERVAL)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    async def __aenter__(self) -> "Thinking":
        self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.stop()
