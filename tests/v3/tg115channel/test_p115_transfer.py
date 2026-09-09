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


def test_directory_browser_paginates_and_ignores_files(pure_modules):
    class ListingClient:
        def __init__(self):
            self.offsets = []

        def fs_files(self, payload, **_kwargs):
            self.offsets.append(payload["offset"])
            rows = (
                [{"cid": "2", "n": "电影"}, {"fid": "10", "n": "文件"}]
                if payload["offset"] == 0
                else [{"cid": "3", "n": "电视剧"}]
            )
            return {"state": True, "path": [{"cid": "0", "name": "根目录"}], "count": 3, "data": rows}

    client = ListingClient()
    service = pure_modules.p115.P115TransferService("UID=1; CID=2; SEID=3", client_factory=lambda _: client)
    result = service.list_directory("0")
    assert client.offsets == [0, 2]
    assert [item["value"] for item in result["children"]] == ["2", "3"]
    assert result["path"] == "/"
    with pytest.raises(RuntimeError, match="不匹配"):
        service.list_directory("99")


def test_selected_directory_id_avoids_ambiguous_path_lookup(pure_modules):
    client = FakeClient(directory_id=12)
    service = pure_modules.p115.P115TransferService("UID=1; CID=2; SEID=3", client_factory=lambda _: client)
    result = service.transfer(url="https://115.com/s/example?password=abcd", destination="/电影", directory_id="88")
    assert result.ok
    assert client.received[0]["cid"] == 88
