from __future__ import annotations

import pytest


class FakeClient:
    def __init__(self, receive_response=None, directory_id=12):
        self.receive_response = receive_response or {"state": True}
        self.directory_id = directory_id
        self.received = []

    def fs_dir_getid(self, path):
        return {"id": self.directory_id, "path": path}

    def fs_makedirs_app(self, path, pid=0):
        return {"cid": 99, "path": path, "pid": pid}

    def share_receive(self, payload):
        self.received.append(payload)
        return self.receive_response


def test_transfer_success_uses_validated_destination(pure_modules):
    p115 = pure_modules.p115
    client = FakeClient()
    service = p115.P115TransferService(
        "UID=1; CID=2; SEID=3",
        client_factory=lambda _cookie: client,
    )

    result = service.transfer(
        url="https://115.com/s/swexample?password=a1b2",
        destination="影视//电影/",
    )

    assert result.ok is True
    assert result.destination == "/影视/电影"
    assert client.received == [
        {
            "share_code": "swexample",
            "receive_code": "a1b2",
            "file_id": 0,
            "cid": 12,
            "is_check": 0,
        }
    ]


def test_already_saved_is_idempotent_success(pure_modules):
    p115 = pure_modules.p115
    client = FakeClient(receive_response={"state": False, "message": "该资源已经转存"})
    service = p115.P115TransferService(
        "UID=1; CID=2; SEID=3",
        client_factory=lambda _cookie: client,
    )

    result = service.transfer(
        url="https://115.com/s/swexample?password=a1b2",
        destination="/影视/电影",
    )

    assert result.ok is True
    assert result.data["already_saved"] is True


def test_cookie_and_path_validation(pure_modules):
    service_type = pure_modules.p115.P115TransferService

    assert "SEID" in service_type.cookie_error("UID=1; CID=2")
    assert service_type.normalize_pan_path("/影视/电视剧/") == "/影视/电视剧"
    with pytest.raises(ValueError, match=r"不允许包含 \.\."):
        service_type.normalize_pan_path("/影视/../秘密")


def test_missing_share_code_is_rejected_before_network(pure_modules):
    p115 = pure_modules.p115
    client = FakeClient()
    service = p115.P115TransferService(
        "UID=1; CID=2; SEID=3",
        client_factory=lambda _cookie: client,
    )

    result = service.transfer(url="https://115.com/s/swexample", destination="/影视/电影")

    assert result.ok is False
    assert "提取码" in result.message
    assert client.received == []
