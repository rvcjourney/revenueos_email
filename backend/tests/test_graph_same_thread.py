"""Microsoft Graph same-thread follow-ups (ADR-0021 D4, slice 3) against a fake
Graph (httpx.MockTransport). No network, no real mailbox, no email."""

# ruff: noqa: E501
from __future__ import annotations

import base64
import json
from collections.abc import Callable
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.modules.mailboxes.providers.base import (
    EnvelopeAttachment,
    ErrorCategory,
    OutboundMessageEnvelope,
)
from app.modules.mailboxes.providers.microsoft import MicrosoftGraphProvider

PARENT_RFC = "<parent-1@outlook.com>"
HTML = "<p>Bumping this, Ada?</p>"
FILE = EnvelopeAttachment(filename="deck.pdf", content_type="application/pdf", data=b"%PDF-1")
IMAGE = EnvelopeAttachment(filename="logo.png", content_type="image/png", data=b"\x89PNG", content_id="logo-1")


def _envelope(*, in_reply_to: str | None = PARENT_RFC, attachments=()) -> OutboundMessageEnvelope:
    return OutboundMessageEnvelope(
        to_address="lead@example.com",
        from_address="sales@company.com",
        from_name="Sales Team",
        subject="Re: Quick idea",
        body_html=HTML,
        rfc_message_id="<ours@company.com>",
        attachments=attachments,
        in_reply_to=in_reply_to,
    )


class FakeGraph:
    """Records every call; `overrides[(method, path_suffix)]` replaces a reply."""

    def __init__(self, *, lookup: list[dict] | None = None) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.requests: list[httpx.Request] = []
        self.lookup = [{"id": "parent-id", "conversationId": "conv-parent", "isDraft": False}] if lookup is None else lookup
        self.overrides: dict[tuple[str, str], Callable[[], httpx.Response]] = {}

    def _body(self, request: httpx.Request) -> dict | None:
        return json.loads(request.content) if request.content else None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path, self._body(request)))
        self.requests.append(request)
        for (method, suffix), reply in self.overrides.items():
            if request.method == method and path.endswith(suffix):
                return reply()
        if request.method == "GET" and path == "/v1.0/me/messages":
            return httpx.Response(200, json={"value": self.lookup})
        if request.method == "POST" and path == "/v1.0/me/messages/parent-id/createReply":
            return httpx.Response(201, json={
                "id": "reply-1", "conversationId": "conv-parent",
                "internetMessageId": "<reply-1@outlook.com>",
            })
        if request.method == "PATCH" and path == "/v1.0/me/messages/reply-1":
            return httpx.Response(200, json={"id": "reply-1"})
        if request.method == "POST" and path == "/v1.0/me/messages/reply-1/attachments":
            return httpx.Response(201, json={"id": "att"})
        if request.method == "POST" and path == "/v1.0/me/messages":
            return httpx.Response(201, json={
                "id": "draft-1", "conversationId": "conv-new", "internetMessageId": "<graph-1@outlook.com>",
            })
        if request.method == "POST" and path.endswith("/send"):
            return httpx.Response(202)
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(500)

    def methods(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p, _ in self.calls]


def _send(graph: FakeGraph, envelope: OutboundMessageEnvelope | None = None):
    provider = MicrosoftGraphProvider(
        client_id="id", client_secret="s", http_client=httpx.Client(transport=httpx.MockTransport(graph))
    )
    return provider.send_message({"access_token": "t"}, envelope or _envelope())


def _raise(exc: Exception) -> Callable[[], httpx.Response]:
    def reply() -> httpx.Response:
        raise exc

    return reply


def _status(code: int, **headers: str) -> Callable[[], httpx.Response]:
    return lambda: httpx.Response(code, json={"error": {"code": str(code), "message": "x"}}, headers=headers)


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_a_threaded_send_replies_to_the_parent_and_matches_the_snapshot() -> None:
    graph = FakeGraph()
    result = _send(graph, _envelope(attachments=(FILE, IMAGE)))

    assert graph.methods() == [
        ("GET", "/v1.0/me/messages"),
        ("POST", "/v1.0/me/messages/parent-id/createReply"),
        ("PATCH", "/v1.0/me/messages/reply-1"),
        ("POST", "/v1.0/me/messages/reply-1/attachments"),
        ("POST", "/v1.0/me/messages/reply-1/attachments"),
        ("POST", "/v1.0/me/messages/reply-1/send"),
    ]
    patch = graph.calls[2][2]
    assert patch == {
        "subject": "Re: Quick idea",
        "body": {"contentType": "HTML", "content": HTML},
        "toRecipients": [{"emailAddress": {"address": "lead@example.com"}}],
        "ccRecipients": [],
        "bccRecipients": [],
        "from": {"emailAddress": {"address": "sales@company.com", "name": "Sales Team"}},
    }
    assert graph.calls[3][2] == {
        "@odata.type": "#microsoft.graph.fileAttachment", "name": "deck.pdf",
        "contentType": "application/pdf", "contentBytes": base64.b64encode(b"%PDF-1").decode(), "isInline": False,
    }
    assert graph.calls[4][2] == {
        "@odata.type": "#microsoft.graph.fileAttachment", "name": "logo.png", "contentType": "image/png",
        "contentBytes": base64.b64encode(b"\x89PNG").decode(), "isInline": True, "contentId": "logo-1",
    }
    assert result.status == "ACCEPTED" and not result.thread_dropped
    assert result.rfc_message_id == "<reply-1@outlook.com>"
    assert result.provider_thread_id == "conv-parent"
    assert result.provider_message_id == "reply-1"


def test_the_lookup_searches_the_whole_mailbox_with_an_encoded_filter() -> None:
    graph = FakeGraph()
    _send(graph, _envelope(in_reply_to="<a+b/c'd@outlook.com>"))
    request = graph.requests[0]
    assert request.url.path == "/v1.0/me/messages"  # every folder, not Sent Items only
    raw = urlsplit(str(request.url)).query
    assert "%3C" in raw and "+" not in raw.split("filter=", 1)[1].split("&", 1)[0]
    query = parse_qs(raw)
    assert query["$filter"] == ["internetMessageId eq '<a+b/c''d@outlook.com>'"]  # OData doubles quotes


def test_a_bare_message_id_is_bracketed_for_the_lookup() -> None:
    graph = FakeGraph()
    _send(graph, _envelope(in_reply_to="parent-1@outlook.com"))
    assert parse_qs(urlsplit(str(graph.requests[0].url)).query)["$filter"] == [
        "internetMessageId eq '<parent-1@outlook.com>'"
    ]


# ---------------------------------------------------------------------------
# parent not found -> unthreaded
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lookup", [[], [{"id": "d", "conversationId": "c", "isDraft": True}]])
def test_no_sent_parent_sends_unthreaded(lookup) -> None:
    graph = FakeGraph(lookup=lookup)
    result = _send(graph)
    assert graph.methods() == [
        ("GET", "/v1.0/me/messages"),
        ("POST", "/v1.0/me/messages"),
        ("POST", "/v1.0/me/messages/draft-1/send"),
    ]
    assert result.status == "ACCEPTED" and result.thread_dropped
    assert result.rfc_message_id == "<graph-1@outlook.com>"


def test_a_parent_deleted_before_create_reply_sends_unthreaded() -> None:
    graph = FakeGraph()
    graph.overrides[("POST", "/createReply")] = _status(404)
    result = _send(graph)
    assert ("POST", "/v1.0/me/messages/draft-1/send") in graph.methods()
    assert result.status == "ACCEPTED" and result.thread_dropped


# ---------------------------------------------------------------------------
# failures before /send: retryable, nothing sent, reply draft removed
# ---------------------------------------------------------------------------


def _no_send(graph: FakeGraph) -> bool:
    return not any(p.endswith("/send") for _, p in graph.methods())


def test_throttled_lookup_is_retryable_and_sends_nothing() -> None:
    graph = FakeGraph()
    graph.overrides[("GET", "/me/messages")] = _status(429, **{"Retry-After": "7"})
    result = _send(graph)
    assert result.status == "DEFINITIVELY_REJECTED"
    assert result.error_category == ErrorCategory.RATE_LIMIT and result.retry_after_seconds == 7
    assert graph.methods() == [("GET", "/v1.0/me/messages")]


def test_throttled_create_reply_is_retryable_and_sends_nothing() -> None:
    graph = FakeGraph()
    graph.overrides[("POST", "/createReply")] = _status(429)
    result = _send(graph)
    assert result.status == "DEFINITIVELY_REJECTED" and result.error_category == ErrorCategory.RATE_LIMIT
    assert _no_send(graph) and not result.thread_dropped
    assert ("POST", "/v1.0/me/messages") not in graph.methods()  # not sent unthreaded either


@pytest.mark.parametrize(
    "failure", [_raise(httpx.ReadTimeout("slow")), _raise(httpx.ConnectError("down")), _status(503)]
)
def test_a_lookup_that_times_out_or_fails_is_a_safe_non_send(failure) -> None:
    graph = FakeGraph()
    graph.overrides[("GET", "/me/messages")] = failure
    result = _send(graph)
    assert result.status == "DEFINITIVELY_REJECTED"
    assert result.error_category in (ErrorCategory.NETWORK_ERROR, ErrorCategory.TEMPORARY_PROVIDER_ERROR)
    assert _no_send(graph)


def test_missing_scope_is_the_existing_scope_failure() -> None:
    graph = FakeGraph()
    graph.overrides[("GET", "/me/messages")] = _status(403)
    result = _send(graph)
    assert result.error_category == ErrorCategory.INSUFFICIENT_SCOPE and _no_send(graph)


@pytest.mark.parametrize("cleanup", [_status(500), _raise(httpx.ConnectError("gone"))])
def test_a_failed_patch_never_sends_and_cleanup_failure_is_ignored(cleanup) -> None:
    graph = FakeGraph()
    graph.overrides[("PATCH", "/reply-1")] = _status(503)
    graph.overrides[("DELETE", "/reply-1")] = cleanup
    result = _send(graph)
    assert result.status == "DEFINITIVELY_REJECTED" and result.error_category == ErrorCategory.TEMPORARY_PROVIDER_ERROR
    assert _no_send(graph)
    assert ("DELETE", "/v1.0/me/messages/reply-1") in graph.methods()


def test_a_failed_attachment_upload_never_sends_and_removes_the_reply() -> None:
    graph = FakeGraph()
    graph.overrides[("POST", "/reply-1/attachments")] = _raise(httpx.WriteTimeout("slow"))
    result = _send(graph, _envelope(attachments=(FILE,)))
    assert result.status == "DEFINITIVELY_REJECTED" and _no_send(graph)
    assert ("DELETE", "/v1.0/me/messages/reply-1") in graph.methods()


def test_a_reply_without_an_id_is_a_safe_non_send() -> None:
    graph = FakeGraph()
    graph.overrides[("POST", "/createReply")] = lambda: httpx.Response(201, json={"conversationId": "c"})
    result = _send(graph)
    assert result.status == "DEFINITIVELY_REJECTED" and _no_send(graph)


def test_a_send_timeout_after_create_reply_is_unknown_with_the_reply_ids() -> None:
    graph = FakeGraph()
    graph.overrides[("POST", "/reply-1/send")] = _raise(httpx.ReadTimeout("slow"))
    result = _send(graph)
    assert result.status == "UNKNOWN"
    assert result.rfc_message_id == "<reply-1@outlook.com>" and result.provider_thread_id == "conv-parent"
    assert ("DELETE", "/v1.0/me/messages/reply-1") not in graph.methods()  # may have been sent


# ---------------------------------------------------------------------------
# unthreaded sends are byte-for-byte what they were
# ---------------------------------------------------------------------------


def test_an_unthreaded_send_is_unchanged() -> None:
    graph = FakeGraph()
    # Built without the new field so this same test also passes on the old code.
    envelope = OutboundMessageEnvelope(
        to_address="lead@example.com", from_address="sales@company.com", from_name="Sales Team",
        subject="Re: Quick idea", body_html=HTML, rfc_message_id="<ours@company.com>", attachments=(FILE,),
    )
    result = _send(graph, envelope)
    assert graph.methods() == [("POST", "/v1.0/me/messages"), ("POST", "/v1.0/me/messages/draft-1/send")]
    assert graph.requests[0].content == json.dumps({
        "subject": "Re: Quick idea",
        "body": {"contentType": "HTML", "content": HTML},
        "toRecipients": [{"emailAddress": {"address": "lead@example.com"}}],
        "from": {"emailAddress": {"address": "sales@company.com", "name": "Sales Team"}},
        "attachments": [{
            "@odata.type": "#microsoft.graph.fileAttachment", "name": "deck.pdf",
            "contentType": "application/pdf", "contentBytes": base64.b64encode(b"%PDF-1").decode(),
            "isInline": False,
        }],
    }).encode()  # httpx's own json= serialization
    assert graph.requests[1].content == b""
    assert result.status == "ACCEPTED" and not result.thread_dropped


# ---------------------------------------------------------------------------
# send-time budget (issue I-01 follow-up): never start /send near the deadline
# ---------------------------------------------------------------------------


def _send_by(graph: FakeGraph, envelope: OutboundMessageEnvelope, deadline):
    provider = MicrosoftGraphProvider(
        client_id="id", client_secret="s", http_client=httpx.Client(transport=httpx.MockTransport(graph))
    )
    return provider.send_message({"access_token": "t"}, envelope, authorization_deadline=deadline)


def _in(seconds: float):
    from datetime import UTC, datetime, timedelta

    return datetime.now(UTC) + timedelta(seconds=seconds)


@pytest.mark.parametrize(("threaded", "draft"), [(False, "draft-1"), (True, "reply-1")])
def test_too_little_time_left_sends_nothing_and_removes_the_draft(threaded, draft) -> None:
    graph = FakeGraph()
    result = _send_by(graph, _envelope(in_reply_to=PARENT_RFC if threaded else None), _in(5))
    assert result.status == "DEFINITIVELY_REJECTED"
    assert result.error_category == ErrorCategory.TEMPORARY_PROVIDER_ERROR
    assert result.error_code == "send_deadline_budget"
    assert _no_send(graph)
    assert ("DELETE", f"/v1.0/me/messages/{draft}") in graph.methods()


def test_a_deadline_already_passed_sends_nothing() -> None:
    graph = FakeGraph()
    result = _send_by(graph, _envelope(in_reply_to=None), _in(-1))
    assert result.status == "DEFINITIVELY_REJECTED" and _no_send(graph)


@pytest.mark.parametrize("deadline", [None, "far"])
def test_enough_time_left_changes_no_request(deadline) -> None:
    plain, timed = FakeGraph(), FakeGraph()
    _send(plain, _envelope(attachments=(FILE,)))
    result = _send_by(timed, _envelope(attachments=(FILE,)), None if deadline is None else _in(60))
    assert timed.methods() == plain.methods()
    assert [r.content for r in timed.requests] == [r.content for r in plain.requests]
    assert result.status == "ACCEPTED"
