"""Markdown (as produced by Gemini) -> Telegram-safe HTML, plus message chunking."""
import html
import re

LIMIT = 3900  # Telegram max is 4096; leave room for HTML tags

_FENCE = re.compile(r"```([\w+#-]*)\n?(.*?)```", re.S)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_ITALIC = re.compile(r"(?<![\*\w])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\*\w])")
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*$", re.M)
_BULLET = re.compile(r"^(\s*)[*\-]\s+", re.M)
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")


def _inline(text: str) -> str:
    text = html.escape(text, quote=False)
    codes: list[str] = []

    def stash(m):
        codes.append(f"<code>{m.group(1)}</code>")
        return f"\x00{len(codes) - 1}\x00"

    text = _INLINE_CODE.sub(stash, text)  # protect code from further markup
    text = _HEADING.sub(r"<b>\1</b>", text)
    text = _BULLET.sub(r"\1• ", text)
    text = _BOLD.sub(r"<b>\1</b>", text)
    text = _ITALIC.sub(r"<i>\1</i>", text)
    text = _LINK.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', text)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], text)


def md_to_html(text: str) -> str:
    out, pos = [], 0
    for m in _FENCE.finditer(text):
        out.append(_inline(text[pos:m.start()]))
        lang, code = m.group(1), html.escape(m.group(2).strip("\n"), quote=False)
        cls = f' class="language-{lang}"' if lang else ""
        out.append(f"<pre><code{cls}>{code}</code></pre>")
        pos = m.end()
    out.append(_inline(text[pos:]))
    return "".join(out)


def chunk_markdown(text: str, limit: int = LIMIT) -> list[str]:
    """Split on line boundaries; if a split lands inside a code fence, close and reopen it."""
    chunks, cur, size = [], [], 0
    in_fence, fence_lang = False, ""
    for line in text.split("\n"):
        # a single over-long line: hard-split it
        pieces = [line[i:i + limit] for i in range(0, len(line), limit)] or [""]
        for piece in pieces:
            if size + len(piece) + 1 > limit and cur:
                if in_fence:
                    cur.append("```")
                chunks.append("\n".join(cur))
                cur = [f"```{fence_lang}"] if in_fence else []
                size = sum(len(c) + 1 for c in cur)
            cur.append(piece)
            size += len(piece) + 1
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            fence_lang = stripped[3:].strip() if in_fence else ""
    if cur:
        chunks.append("\n".join(cur))
    return chunks or [""]
