"""Pure parsing and opaque-token helpers for the TG 115 protocol."""

from __future__ import annotations

import hashlib
import re
import string
from collections.abc import Mapping
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from .models import BotReply, BotResource

TOKEN_PARAM = "x.tg115"
RESOURCE_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SHARE_URL_PATTERN = re.compile(
    r"https?://(?:[a-z0-9-]+\.)?(?:115\.com|115cdn\.com)/[^\s<>\[\]{}\"']+",
    re.IGNORECASE,
)
ACCESS_CODE_PATTERN = re.compile(
    r"(?:提取码|访问码|提取密码|密码|receive[_ -]?code|code)\s*[:：=]?\s*([a-z0-9]{4,12})",
    re.IGNORECASE,
)
SIZE_PATTERN = re.compile(r"(?<![a-z0-9])(\d+(?:\.\d+)?)\s*(tb|gb|mb|kb)(?![a-z0-9])", re.IGNORECASE)
QUALITY_PATTERN = re.compile(
    r"(?<![a-z0-9])(8k|4320p|4k|2160p|1440p|1080[pi]?|720p|remux|bluray|blu-ray|web-?dl)(?![a-z0-9])",
    re.IGNORECASE,
)
TRAILING_URL_PUNCTUATION = ".,;:!?，。；：！？、）)]}》】>"
GENERIC_TITLE_PATTERN = re.compile(
    r"^(?:资源|链接|下载|115|转存|提取码|访问码|密码|点击|获取|详情|网盘)(?:地址|链接|资源|详情)?\s*[:：-]?$",
    re.IGNORECASE,
)


def normalize_keyword(value: str) -> str:
    """Collapse whitespace while preserving the user's title spelling."""

    return " ".join(str(value or "").strip().split())


def render_template(template: str, values: Mapping[str, object], *, required: tuple[str, ...] = ()) -> str:
    """Render a controlled text template and reject unknown placeholders."""

    template = str(template or "").strip()
    if not template:
        raise ValueError("模板不能为空")
    formatter = string.Formatter()
    fields = {
        field_name for _, field_name, _, _ in formatter.parse(template) if field_name is not None and field_name != ""
    }
    unknown = fields - set(values)
    if unknown:
        raise ValueError(f"模板包含未知占位符: {', '.join(sorted(unknown))}")
    missing = set(required) - fields
    if missing:
        raise ValueError(f"模板缺少占位符: {', '.join(sorted(missing))}")
    return template.format_map({key: str(value or "") for key, value in values.items()}).strip()


def normalize_share_url(value: str) -> str:
    """Normalize a 115 URL for de-duplication without dropping its access code."""

    value = str(value or "").strip().rstrip(TRAILING_URL_PUNCTUATION)
    parsed = urlparse(value)
    if parsed.scheme.lower() not in {"http", "https"}:
        return ""
    hostname = (parsed.hostname or "").lower()
    valid_host = (
        hostname == "115.com"
        or hostname.endswith(".115.com")
        or hostname == "115cdn.com"
        or hostname.endswith(".115cdn.com")
    )
    if not valid_host:
        return ""
    netloc = hostname
    if parsed.port:
        netloc = f"{hostname}:{parsed.port}"
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    return urlunparse(("https", netloc, path, "", parsed.query, ""))


def _nearest_match(
    pattern: re.Pattern[str],
    text: str,
    start: int,
    end: int,
    *,
    prefer_after: bool = False,
) -> re.Match[str] | None:
    candidates = pattern.finditer(text[max(0, start - 240) : min(len(text), end + 240)])
    window_start = max(0, start - 240)

    def score(match: re.Match[str]) -> tuple[int, int]:
        absolute_start = window_start + match.start()
        absolute_end = window_start + match.end()
        if absolute_end <= start:
            return start - absolute_end, 1 if prefer_after else 0
        if absolute_start >= end:
            return absolute_start - end, 0
        return 0, 0

    return min(candidates, key=score, default=None)


def _access_code(url: str, text: str, start: int, end: int) -> str:
    query = parse_qs(urlparse(url).query)
    for key in ("password", "receive_code", "pwd", "code"):
        values = query.get(key)
        if values and str(values[0]).strip():
            return str(values[0]).strip()
    match = _nearest_match(ACCESS_CODE_PATTERN, text, start, end, prefer_after=True)
    return match.group(1) if match else ""


def _size_bytes(text: str, start: int, end: int) -> int:
    match = _nearest_match(SIZE_PATTERN, text, start, end)
    if not match:
        return 0
    multipliers = {"kb": 1024, "mb": 1024**2, "gb": 1024**3, "tb": 1024**4}
    return int(float(match.group(1)) * multipliers[match.group(2).lower()])


def _quality(text: str, start: int, end: int) -> str:
    del end
    line_start = text.rfind("\n", 0, start) + 1
    candidate_line = text[line_start:start]
    if not candidate_line.strip():
        previous_text = text[:line_start].rstrip("\n")
        previous_start = previous_text.rfind("\n") + 1
        candidate_line = previous_text[previous_start:]
    matches = list(QUALITY_PATTERN.finditer(candidate_line))
    if not matches:
        nearest = _nearest_match(QUALITY_PATTERN, text, start, start)
        matches = [nearest] if nearest else []
    if not matches:
        return ""
    rank = {
        "8K": 100,
        "4320P": 100,
        "4K": 90,
        "2160P": 90,
        "1440P": 80,
        "1080P": 70,
        "1080I": 65,
        "1080": 65,
        "720P": 60,
        "REMUX": 50,
        "BLURAY": 40,
        "WEB-DL": 30,
        "WEBDL": 30,
    }
    values = [match.group(1).upper().replace("BLU-RAY", "BLURAY") for match in matches]
    value = max(values, key=lambda item: rank.get(item, 0))
    return "4K" if value in {"2160P", "4K"} else value


def _clean_title(value: str) -> str:
    value = SHARE_URL_PATTERN.sub("", value)
    value = ACCESS_CODE_PATTERN.sub("", value)
    value = re.sub(r"^[\s#>*_`|\-–—\d.、:：()（）\[\]【】]+", "", value)
    value = re.sub(r"\s+", " ", value).strip(" -–—:：|#")
    if not value or GENERIC_TITLE_PATTERN.fullmatch(value):
        return ""
    return value[:180]


def _title_near_url(text: str, start: int, end: int, fallback: str) -> str:
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    if line_end < 0:
        line_end = len(text)
    same_line = _clean_title(text[line_start:start] + " " + text[end:line_end])
    if same_line:
        return same_line

    previous_lines = text[:line_start].splitlines()
    for line in reversed(previous_lines[-4:]):
        candidate = _clean_title(line)
        if candidate:
            return candidate
    return normalize_keyword(fallback) or "TG 115资源"


def extract_resources(replies: tuple[BotReply, ...] | list[BotReply], *, keyword: str) -> list[BotResource]:
    """Extract and de-duplicate 115 shares from reply text and URL buttons."""

    resources: list[BotResource] = []
    seen: set[str] = set()
    for reply in replies:
        sources = [reply.text or ""]
        sources.extend(f"{button.text}\n{button.url}" for button in reply.buttons if button.url)
        for text in sources:
            for match in SHARE_URL_PATTERN.finditer(text):
                url = normalize_share_url(match.group(0))
                if not url or url in seen:
                    continue
                seen.add(url)
                resources.append(
                    BotResource(
                        title=_title_near_url(text, match.start(), match.end(), keyword),
                        url=url,
                        access_code=_access_code(url, text, match.start(), match.end()),
                        size=_size_bytes(text, match.start(), match.end()),
                        quality=_quality(text, match.start(), match.end()),
                        message_id=reply.message_id,
                    )
                )
    return resources


def resource_id(resource: BotResource, *, context: str = "") -> str:
    """Create a stable opaque ID for one share URL and access code."""

    material = (
        f"{normalize_share_url(resource.url)}\n{resource.access_code.strip()}\n{context.strip().casefold()}".encode()
    )
    return hashlib.sha256(material).hexdigest()


def encode_resource_token(identifier: str) -> str:
    """Encode only an opaque local ID in a magnet-shaped MoviePilot resource token."""

    if not RESOURCE_ID_PATTERN.fullmatch(identifier):
        raise ValueError("无效的资源 ID")
    btih = hashlib.sha1(identifier.encode(), usedforsecurity=False).hexdigest()
    return f"magnet:?{urlencode({'xt': f'urn:btih:{btih}', TOKEN_PARAM: identifier})}"


def decode_resource_token(content: object) -> str | None:
    """Return the local resource ID when content belongs to this plugin."""

    if not isinstance(content, str) or not content.startswith("magnet:"):
        return None
    values = parse_qs(urlparse(content).query).get(TOKEN_PARAM) or []
    identifier = str(values[0]) if values else ""
    return identifier if RESOURCE_ID_PATTERN.fullmatch(identifier) else None
