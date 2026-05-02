import base64
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from googleapiclient.errors import HttpError

import main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _resolve_auth_files
# ---------------------------------------------------------------------------

def test_resolve_auth_files_uses_local_when_present(tmp_path, monkeypatch):
    local_creds = tmp_path / "credentials.json"
    local_creds.write_text("{}")
    local_token = tmp_path / "token.json"
    monkeypatch.setattr(main, "_LOCAL_CREDENTIALS_FILE", local_creds)
    monkeypatch.setattr(main, "_LOCAL_TOKEN_FILE", local_token)

    creds_file, token_file = main._resolve_auth_files()

    assert creds_file == local_creds
    assert token_file == local_token


def test_resolve_auth_files_falls_back_to_default_when_no_local(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "_LOCAL_CREDENTIALS_FILE", tmp_path / "credentials.json")
    default_creds = tmp_path / "default_credentials.json"
    default_token = tmp_path / "token-gmail.json"
    monkeypatch.setattr(main, "_DEFAULT_CREDENTIALS_FILE", default_creds)
    monkeypatch.setattr(main, "_DEFAULT_TOKEN_FILE", default_token)

    creds_file, token_file = main._resolve_auth_files()

    assert creds_file == default_creds
    assert token_file == default_token


def test_resolve_auth_files_local_takes_priority_over_default(tmp_path, monkeypatch):
    local_creds = tmp_path / "credentials.json"
    local_creds.write_text("{}")
    monkeypatch.setattr(main, "_LOCAL_CREDENTIALS_FILE", local_creds)
    monkeypatch.setattr(main, "_LOCAL_TOKEN_FILE", tmp_path / "token.json")
    monkeypatch.setattr(main, "_DEFAULT_CREDENTIALS_FILE", tmp_path / "default_credentials.json")
    monkeypatch.setattr(main, "_DEFAULT_TOKEN_FILE", tmp_path / "token-gmail.json")

    creds_file, _ = main._resolve_auth_files()

    assert creds_file == local_creds

def _encode(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


class _FakeResp:
    def __init__(self, status: int):
        self.status = status
        self.reason = "test"


def _http_error(status: int) -> HttpError:
    return HttpError(_FakeResp(status), b"error body")


# ---------------------------------------------------------------------------
# mime_to_ext
# ---------------------------------------------------------------------------

def test_mime_to_ext_known_types():
    assert main.mime_to_ext("image/jpeg") == ".jpg"
    assert main.mime_to_ext("image/png") == ".png"
    assert main.mime_to_ext("image/gif") == ".gif"
    assert main.mime_to_ext("image/webp") == ".webp"


def test_mime_to_ext_strips_charset():
    assert main.mime_to_ext("image/jpeg; charset=utf-8") == ".jpg"


def test_mime_to_ext_unknown_returns_bin():
    assert main.mime_to_ext("image/xyz-unknown-format") == ".bin"


# ---------------------------------------------------------------------------
# safe_filename
# ---------------------------------------------------------------------------

def test_safe_filename_basic():
    assert main.safe_filename("Hello World") == "Hello_World"


def test_safe_filename_strips_special_chars():
    result = main.safe_filename("Hello, World! (2024)")
    assert "," not in result
    assert "!" not in result
    assert "(" not in result


def test_safe_filename_empty_returns_fallback():
    assert main.safe_filename("") == "no_subject"


def test_safe_filename_truncates():
    assert len(main.safe_filename("a" * 200)) == 60


# ---------------------------------------------------------------------------
# header_value
# ---------------------------------------------------------------------------

def test_header_value_found():
    headers = [{"name": "Subject", "value": "Re: Test"}, {"name": "From", "value": "a@b.com"}]
    assert main.header_value(headers, "Subject") == "Re: Test"


def test_header_value_case_insensitive():
    headers = [{"name": "SUBJECT", "value": "Test"}]
    assert main.header_value(headers, "subject") == "Test"


def test_header_value_missing_returns_empty():
    assert main.header_value([], "Subject") == ""
    assert main.header_value([{"name": "From", "value": "x"}], "Subject") == ""


# ---------------------------------------------------------------------------
# decode_body
# ---------------------------------------------------------------------------

def test_decode_body_returns_text():
    part = {"body": {"data": _encode("Hello, World!")}}
    assert main.decode_body(part) == "Hello, World!"


def test_decode_body_missing_data_returns_empty():
    assert main.decode_body({"body": {}}) == ""
    assert main.decode_body({}) == ""


# ---------------------------------------------------------------------------
# extract_body
# ---------------------------------------------------------------------------

def test_extract_body_text_plain():
    payload = {"mimeType": "text/plain", "body": {"data": _encode("plain text")}}
    result = main.extract_body(payload)
    assert result["text"] == "plain text"
    assert result["html"] == ""


def test_extract_body_text_html():
    payload = {"mimeType": "text/html", "body": {"data": _encode("<p>html</p>")}}
    result = main.extract_body(payload)
    assert result["html"] == "<p>html</p>"
    assert result["text"] == ""


def test_extract_body_multipart_alternative():
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _encode("plain")}, "parts": []},
            {"mimeType": "text/html", "body": {"data": _encode("<p>html</p>")}, "parts": []},
        ],
    }
    result = main.extract_body(payload)
    assert result["text"] == "plain"
    assert result["html"] == "<p>html</p>"


def test_extract_body_empty_payload():
    result = main.extract_body({})
    assert result == {"text": "", "html": ""}


# ---------------------------------------------------------------------------
# needs_fetch
# ---------------------------------------------------------------------------

def test_needs_fetch_both_present_returns_false():
    entry = {"json": Path("x.json"), "folder": Path("x/")}
    assert not main.needs_fetch(entry, save_json=True, save_html=True, msg_id="x")


def test_needs_fetch_json_only_mode_done():
    entry = {"json": Path("x.json")}
    assert not main.needs_fetch(entry, save_json=True, save_html=False, msg_id="x")


def test_needs_fetch_html_only_mode_done():
    entry = {"folder": Path("x/")}
    assert not main.needs_fetch(entry, save_json=False, save_html=True, msg_id="x")


def test_needs_fetch_missing_json_returns_true():
    assert main.needs_fetch({}, save_json=True, save_html=False, msg_id="x")


def test_needs_fetch_text_only_email_html_not_needed(tmp_path):
    json_file = tmp_path / "x.json"
    json_file.write_text(json.dumps({"body_html": ""}))
    entry = {"json": json_file}
    assert not main.needs_fetch(entry, save_json=True, save_html=True, msg_id="x")


def test_needs_fetch_html_body_present_folder_missing(tmp_path):
    json_file = tmp_path / "x.json"
    json_file.write_text(json.dumps({"body_html": "<p>content</p>"}))
    entry = {"json": json_file}
    assert main.needs_fetch(entry, save_json=True, save_html=True, msg_id="x")


# ---------------------------------------------------------------------------
# build_saved_index
# ---------------------------------------------------------------------------

def test_build_saved_index_nonexistent_dir(tmp_path):
    result = main.build_saved_index(tmp_path / "ghost")
    assert result == {}


def test_build_saved_index_json_file(tmp_path):
    (tmp_path / "abc123_subject.json").write_text("{}")
    result = main.build_saved_index(tmp_path)
    assert "abc123" in result
    assert "json" in result["abc123"]


def test_build_saved_index_folder_with_html(tmp_path):
    folder = tmp_path / "abc123_subject"
    folder.mkdir()
    (folder / "abc123_subject.html").write_text("<html/>")
    result = main.build_saved_index(tmp_path)
    assert "abc123" in result
    assert "folder" in result["abc123"]


def test_build_saved_index_folder_without_html_ignored(tmp_path):
    folder = tmp_path / "abc123_subject"
    folder.mkdir()
    result = main.build_saved_index(tmp_path)
    assert "abc123" not in result


def test_build_saved_index_both_present(tmp_path):
    (tmp_path / "abc123_subject.json").write_text("{}")
    folder = tmp_path / "abc123_subject"
    folder.mkdir()
    (folder / "abc123_subject.html").write_text("<html/>")
    result = main.build_saved_index(tmp_path)
    assert "json" in result["abc123"]
    assert "folder" in result["abc123"]


# ---------------------------------------------------------------------------
# list_messages
# ---------------------------------------------------------------------------

def test_list_messages_single_page():
    svc = MagicMock()
    svc.users().messages().list().execute.return_value = {
        "messages": [{"id": "1"}, {"id": "2"}]
    }
    result = main.list_messages(svc, "from:a@b.com", max_results=None)
    assert [m["id"] for m in result] == ["1", "2"]


def test_list_messages_pagination():
    svc = MagicMock()
    list_exec = svc.users().messages().list().execute
    list_exec.side_effect = [
        {"messages": [{"id": "1"}], "nextPageToken": "tok1"},
        {"messages": [{"id": "2"}]},
    ]
    result = main.list_messages(svc, "from:a@b.com", max_results=None)
    assert [m["id"] for m in result] == ["1", "2"]


def test_list_messages_max_results_truncates():
    svc = MagicMock()
    # API might return up to page_size items; result must be capped at max_results
    svc.users().messages().list().execute.return_value = {
        "messages": [{"id": str(i)} for i in range(10)]
    }
    result = main.list_messages(svc, "from:a@b.com", max_results=3)
    assert len(result) == 3
    assert [m["id"] for m in result] == ["0", "1", "2"]


def test_list_messages_empty_result():
    svc = MagicMock()
    svc.users().messages().list().execute.return_value = {"messages": []}
    result = main.list_messages(svc, "from:a@b.com", max_results=None)
    assert result == []


# ---------------------------------------------------------------------------
# _execute_with_retry
# ---------------------------------------------------------------------------

def test_execute_with_retry_success_first_attempt():
    req = MagicMock()
    req.execute.return_value = {"ok": True}
    result = main._execute_with_retry(req)
    assert result == {"ok": True}
    req.execute.assert_called_once()


def test_execute_with_retry_retries_on_429():
    req = MagicMock()
    req.execute.side_effect = [_http_error(429), {"ok": True}]
    with patch("main.time.sleep"):
        result = main._execute_with_retry(req, max_retries=3)
    assert result == {"ok": True}
    assert req.execute.call_count == 2


def test_execute_with_retry_retries_on_503():
    req = MagicMock()
    req.execute.side_effect = [_http_error(503), _http_error(503), {"ok": True}]
    with patch("main.time.sleep"):
        result = main._execute_with_retry(req, max_retries=5)
    assert result == {"ok": True}
    assert req.execute.call_count == 3


def test_execute_with_retry_raises_after_max_retries():
    req = MagicMock()
    req.execute.side_effect = _http_error(429)
    with patch("main.time.sleep"):
        with pytest.raises(HttpError):
            main._execute_with_retry(req, max_retries=3)
    assert req.execute.call_count == 3


def test_execute_with_retry_raises_immediately_on_non_retryable():
    req = MagicMock()
    req.execute.side_effect = _http_error(403)
    with pytest.raises(HttpError):
        main._execute_with_retry(req)
    req.execute.assert_called_once()


def test_execute_with_retry_raises_immediately_on_404():
    req = MagicMock()
    req.execute.side_effect = _http_error(404)
    with pytest.raises(HttpError):
        main._execute_with_retry(req)
    req.execute.assert_called_once()
