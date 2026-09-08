from __future__ import annotations

import pytest


def test_extracts_text_link_metadata(pure_modules):
    models = pure_modules.models
    protocol = pure_modules.protocol
    reply = models.BotReply(
        message_id=42,
        text=("1. 沙丘2 (2024) 2160P REMUX 68.5GB\nhttps://115.com/s/swexample?password=a1b2\n祝观影愉快"),
    )

    resources = protocol.extract_resources([reply], keyword="沙丘2 2024")

    assert len(resources) == 1
    assert resources[0].title == "沙丘2 (2024) 2160P REMUX 68.5GB"
    assert resources[0].access_code == "a1b2"
    assert resources[0].quality == "4K"
    assert resources[0].size == int(68.5 * 1024**3)
    assert resources[0].message_id == 42


def test_extracts_url_button_and_deduplicates(pure_modules):
    models = pure_modules.models
    protocol = pure_modules.protocol
    url = "http://115.com/s/shared?password=z9y8"
    replies = [
        models.BotReply(
            message_id=1,
            text=f"资源链接：{url}",
            buttons=(models.ReplyButton(text="115链接", url=url),),
        )
    ]

    resources = protocol.extract_resources(replies, keyword="测试电影")

    assert len(resources) == 1
    assert resources[0].url.startswith("https://115.com/")


def test_uses_nearest_access_code_for_multiple_resources(pure_modules):
    models = pure_modules.models
    protocol = pure_modules.protocol
    reply = models.BotReply(
        message_id=2,
        text=(
            "电影甲 1080P 10GB\n"
            "https://115.com/s/first\n"
            "提取码：aaaa\n"
            "电影乙 2160P 20GB\n"
            "https://115.com/s/second\n"
            "提取码：bbbb"
        ),
    )

    resources = protocol.extract_resources([reply], keyword="电影")

    assert [item.access_code for item in resources] == ["aaaa", "bbbb"]
    assert [item.quality for item in resources] == ["1080P", "4K"]
    assert [item.size for item in resources] == [10 * 1024**3, 20 * 1024**3]


def test_rejects_lookalike_115_domain(pure_modules):
    models = pure_modules.models
    protocol = pure_modules.protocol
    reply = models.BotReply(message_id=3, text="https://evil115cdn.com/s/not-safe?password=abcd")

    assert protocol.extract_resources([reply], keyword="Movie") == []


def test_opaque_token_does_not_expose_share_url(pure_modules):
    models = pure_modules.models
    protocol = pure_modules.protocol
    resource = models.BotResource(
        title="Movie",
        url="https://115.com/s/swsecret?password=abcd",
        access_code="abcd",
    )
    identifier = protocol.resource_id(resource)

    token = protocol.encode_resource_token(identifier)

    assert protocol.decode_resource_token(token) == identifier
    assert "115.com" not in token
    assert "swsecret" not in token
    assert protocol.decode_resource_token("magnet:?xt=urn:btih:abc") is None


def test_template_rejects_unknown_or_missing_fields(pure_modules):
    render = pure_modules.protocol.render_template

    assert render("/search {keyword}", {"keyword": "Alien"}, required=("keyword",)) == "/search Alien"
    with pytest.raises(ValueError, match="未知占位符"):
        render("{unknown}", {"keyword": "Alien"})
    with pytest.raises(ValueError, match="缺少占位符"):
        render("/search", {"keyword": "Alien"}, required=("keyword",))
