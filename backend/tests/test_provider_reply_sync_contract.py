"""Gmail, Microsoft Graph and IMAP inbound sync: cursors, paging, filtering and
failure behavior (the parts whose bugs silently lost replies)."""

# ruff: noqa: E501 -- test data (raw MIME, SQL, header values) reads better unwrapped.

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from app.core.errors import PROVIDER_AUTH_REJECTED_STATUS, AppError
from app.modules.mailboxes.providers.gmail import GMAIL_DEFAULT_SCOPES, GmailProvider
from app.modules.mailboxes.providers.imap_sync import (
    FetchedImapMessage,
    ImapLibSession,
    imap_configured,
    imap_credential,
    parse_imap_message,
    sync_imap_page,
)
from app.modules.mailboxes.providers.microsoft import (
    MICROSOFT_DEFAULT_SCOPES,
    MicrosoftGraphProvider,
)
from app.modules.mailboxes.providers.smtp import SmtpProvider
from app.modules.mailboxes.reply_capability import ReplySyncStatus, reply_sync_status

CRED = {"access_token": "tok"}


def gmail(handler) -> GmailProvider:
    return GmailProvider(
        client_id="id",
        client_secret="secret",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def gmail_message(mid: str, *, labels: tuple[str, ...] = ("INBOX",), frm: str = "lead@x.test") -> dict[str, Any]:
    return {
        "id": mid,
        "threadId": f"t-{mid}",
        "labelIds": list(labels),
        "internalDate": "1790000000000",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": frm},
                {"name": "Message-ID", "value": f"<{mid}@x.test>"},
                {"name": "In-Reply-To", "value": "<sent-1@acme.test>"},
                {"name": "Subject", "value": "Re: hi"},
            ],
            "body": {"data": base64.urlsafe_b64encode(b"hello").decode()},
        },
    }


class GmailServer:
    """A tiny scripted Gmail API. Records every request for assertions."""

    def __init__(self, *, message_status: int = 200, history_status: int = 200) -> None:
        self.requests: list[httpx.Request] = []
        self.message_status = message_status
        self.history_status = history_status
        self.messages: dict[str, dict[str, Any]] = {}
        self.list_pages: list[dict[str, Any]] = []
        self.history_pages: list[dict[str, Any]] = []
        self.raw: str | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, params = request.url.path, dict(request.url.params)
        if path.endswith("/profile"):
            return httpx.Response(200, json={"historyId": "1000"})
        if path.endswith("/history"):
            if self.history_status != 200:
                return httpx.Response(self.history_status, json={})
            return httpx.Response(200, json=self.history_pages.pop(0))
        if path.endswith("/messages"):
            return httpx.Response(200, json=self.list_pages.pop(0))
        mid = path.rsplit("/", 1)[1]
        if params.get("format") == "raw":
            return httpx.Response(200, json={"raw": self.raw})
        if self.message_status != 200:
            return httpx.Response(self.message_status, json={})
        return httpx.Response(200, json=self.messages[mid])

    def paths(self) -> list[str]:
        return [r.url.path.rsplit("/", 1)[-1] for r in self.requests]


class TestGmailScopes:
    def test_read_scope_is_requested(self) -> None:
        assert "https://www.googleapis.com/auth/gmail.readonly" in GMAIL_DEFAULT_SCOPES
        assert "https://www.googleapis.com/auth/gmail.send" in GMAIL_DEFAULT_SCOPES

    def test_authorization_url_asks_for_the_read_scope(self) -> None:
        url = GmailProvider(client_id="id", client_secret="s").get_authorization_url("st", "http://cb")
        assert "gmail.readonly" in url


class TestGmailFullSyncAndPaging:
    def test_first_sync_is_bounded_to_the_inbox_and_the_horizon(self) -> None:
        server = GmailServer()
        server.list_pages = [{"messages": [{"id": "m1"}]}]
        server.messages["m1"] = gmail_message("m1")
        result = gmail(server).sync_inbound_messages(CRED, cursor=None, page_size=50)
        listing = next(r for r in server.requests if r.url.path.endswith("/messages"))
        assert listing.url.params["labelIds"] == "INBOX"
        assert listing.url.params["q"] == "newer_than:30d"
        assert [m.provider_message_id for m in result.messages] == ["m1"]
        # Done: the cursor is now the mailbox position captured BEFORE the scan.
        assert result.next_cursor == "1000" and result.has_more is False

    def test_paging_the_first_scan_keeps_the_position_and_uses_messages_list_tokens(self) -> None:
        """Regression: page 2 used a messages.list token on history.list -> 400."""
        server = GmailServer()
        server.list_pages = [
            {"messages": [{"id": "m1"}], "nextPageToken": "TOK"},
            {"messages": [{"id": "m2"}]},
        ]
        server.messages.update(m1=gmail_message("m1"), m2=gmail_message("m2"))
        provider = gmail(server)
        first = provider.sync_inbound_messages(CRED, cursor=None)
        assert first.has_more is True
        assert json.loads(first.next_cursor or "") == {"m": "full", "h": "1000", "t": "TOK"}

        second = provider.sync_inbound_messages(CRED, cursor=first.next_cursor)
        assert "history" not in server.paths()  # never sent to history.list
        assert server.paths().count("profile") == 1  # position is not re-read mid-scan
        listing = [r for r in server.requests if r.url.path.endswith("/messages")][-1]
        assert listing.url.params["pageToken"] == "TOK"
        assert second.next_cursor == "1000" and second.has_more is False

    def test_sent_and_draft_copies_are_skipped(self) -> None:
        server = GmailServer()
        server.list_pages = [{"messages": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}]
        server.messages.update(
            a=gmail_message("a"),
            b=gmail_message("b", labels=("INBOX", "SENT")),
            c=gmail_message("c", labels=("DRAFT",)),
        )
        result = gmail(server).sync_inbound_messages(CRED, cursor=None)
        assert [m.provider_message_id for m in result.messages] == ["a"]


class TestGmailHistorySync:
    def test_incremental_sync_filters_to_the_inbox(self) -> None:
        server = GmailServer()
        server.history_pages = [
            {"historyId": "1100", "history": [{"messagesAdded": [{"message": {"id": "m1", "threadId": "t"}}]}]}
        ]
        server.messages["m1"] = gmail_message("m1")
        result = gmail(server).sync_inbound_messages(CRED, cursor="1000")
        hist = next(r for r in server.requests if r.url.path.endswith("/history"))
        assert hist.url.params["labelId"] == "INBOX"
        assert hist.url.params["startHistoryId"] == "1000"
        assert result.next_cursor == "1100"

    def test_traversal_in_flight_keeps_the_original_start_position(self) -> None:
        server = GmailServer()
        server.history_pages = [{"historyId": "1100", "history": [], "nextPageToken": "H2"}]
        result = gmail(server).sync_inbound_messages(CRED, cursor="1000")
        assert json.loads(result.next_cursor or "") == {"m": "hist", "h": "1000", "t": "H2"}
        server.history_pages = [{"historyId": "1100", "history": []}]
        done = gmail(server).sync_inbound_messages(CRED, cursor=result.next_cursor)
        hist = [r for r in server.requests if r.url.path.endswith("/history")][-1]
        assert hist.url.params["startHistoryId"] == "1000" and hist.url.params["pageToken"] == "H2"
        assert done.next_cursor == "1100"

    def test_expired_history_requests_a_resync(self) -> None:
        result = gmail(GmailServer(history_status=404)).sync_inbound_messages(CRED, cursor="1")
        assert result.resync_required is True

    def test_a_failed_message_fetch_aborts_the_page_instead_of_losing_the_reply(self) -> None:
        """Regression: fetch errors were skipped while the cursor still advanced."""
        server = GmailServer(message_status=500)
        server.history_pages = [
            {"historyId": "1100", "history": [{"messagesAdded": [{"message": {"id": "m1"}}]}]}
        ]
        with pytest.raises(AppError):
            gmail(server).sync_inbound_messages(CRED, cursor="1000")

    def test_a_message_deleted_before_it_was_read_is_skipped(self) -> None:
        server = GmailServer(message_status=404)
        server.history_pages = [
            {"historyId": "1100", "history": [{"messagesAdded": [{"message": {"id": "gone"}}]}]}
        ]
        result = gmail(server).sync_inbound_messages(CRED, cursor="1000")
        assert result.messages == [] and result.next_cursor == "1100"

    def test_rate_limit_backs_off_without_advancing(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"Retry-After": "45"}, json={})

        result = gmail(handler).sync_inbound_messages(CRED, cursor="1000")
        assert result.retry_after_seconds == 45.0 and result.next_cursor is None

    def test_a_missing_read_scope_surfaces_as_an_error_not_silence(self) -> None:
        with pytest.raises(AppError):
            gmail(GmailServer(history_status=403)).sync_inbound_messages(CRED, cursor="1000")


class TestGmailDeliveryReports:
    RAW = (
        b"From: MAILER-DAEMON@googlemail.com\r\nSubject: Delivery Status Notification (Failure)\r\n"
        b"MIME-Version: 1.0\r\nContent-Type: multipart/report; report-type=delivery-status; boundary=B\r\n\r\n"
        b"--B\r\nContent-Type: text/plain\r\n\r\nNot delivered\r\n"
        b"--B\r\nContent-Type: message/delivery-status\r\n\r\nFinal-Recipient: rfc822; jane@t.test\r\n"
        b"Action: failed\r\nStatus: 5.1.1\r\n\r\n"
        b"--B\r\nContent-Type: message/rfc822\r\n\r\nMessage-ID: <sent-1@acme.test>\r\n\r\nbody\r\n--B--\r\n"
    )

    def test_the_raw_message_is_parsed_for_the_report(self) -> None:
        server = GmailServer()
        server.history_pages = [
            {"historyId": "1100", "history": [{"messagesAdded": [{"message": {"id": "d1"}}]}]}
        ]
        dsn = gmail_message("d1", frm="mailer-daemon@googlemail.com")
        dsn["payload"]["headers"].append(
            {"name": "Content-Type", "value": "multipart/report; report-type=delivery-status"}
        )
        server.messages["d1"] = dsn
        server.raw = base64.urlsafe_b64encode(self.RAW).decode().rstrip("=")
        result = gmail(server).sync_inbound_messages(CRED, cursor="1000")
        report = result.messages[0].report_text or ""
        assert "Final-Recipient: rfc822; jane@t.test" in report and "<sent-1@acme.test>" in report


# ------------------------------------------------------------------- Microsoft


def graph(handler) -> MicrosoftGraphProvider:
    return MicrosoftGraphProvider(
        client_id="id",
        client_secret="secret",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def graph_message(mid: str = "g1") -> dict[str, Any]:
    return {
        "id": mid,
        "conversationId": "conv-1",
        "internetMessageId": f"<{mid}@outlook.com>",
        "subject": "Re: hi",
        "from": {"emailAddress": {"address": "lead@x.test", "name": "Lead"}},
        "toRecipients": [{"emailAddress": {"address": "sender@acme.test"}}],
        "receivedDateTime": "2026-09-20T10:00:00Z",
        "body": {"contentType": "text", "content": "hello"},
    }


class TestGraphSync:
    def test_scopes_cover_reading_and_the_draft_used_to_learn_message_ids(self) -> None:
        assert "https://graph.microsoft.com/Mail.ReadWrite" in MICROSOFT_DEFAULT_SCOPES
        assert "https://graph.microsoft.com/Mail.Send" in MICROSOFT_DEFAULT_SCOPES

    def test_first_sync_is_bounded_and_headers_are_read_per_message(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path.endswith("/delta"):
                return httpx.Response(
                    200,
                    json={"value": [graph_message()], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/delta?token=D"},
                )
            return httpx.Response(
                200,
                json={"internetMessageHeaders": [{"name": "In-Reply-To", "value": "<sent-1@acme.test>"}]},
            )

        result = graph(handler).sync_inbound_messages(CRED, cursor=None)
        delta = seen[0]
        assert "receivedDateTime ge" in delta.url.params["$filter"]
        assert delta.headers.get("Prefer", "").startswith("odata.maxpagesize")
        # header lookup is a separate, plain GET (no paging Prefer)
        assert "Prefer" not in seen[1].headers
        assert result.messages[0].in_reply_to == "<sent-1@acme.test>"
        assert result.messages[0].provider_thread_id == "conv-1"
        assert result.next_cursor and "token=D" in result.next_cursor

    def test_falls_back_to_plain_delta_when_graph_rejects_the_filter(self) -> None:
        calls: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/delta"):
                calls.append(dict(request.url.params))
                if "$filter" in request.url.params:
                    return httpx.Response(400, json={"error": {"code": "BadRequest"}})
                return httpx.Response(200, json={"value": [], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/x"})
            return httpx.Response(200, json={})

        result = graph(handler).sync_inbound_messages(CRED, cursor=None)
        assert len(calls) == 2 and "$filter" not in calls[1]
        assert result.next_cursor

    def test_expired_delta_requests_a_resync(self) -> None:
        result = graph(lambda r: httpx.Response(410, json={})).sync_inbound_messages(
            CRED, cursor="https://graph.microsoft.com/v1.0/me/delta?token=old"
        )
        assert result.resync_required is True

    def test_untrusted_continuation_urls_are_refused(self) -> None:
        with pytest.raises(AppError):
            graph(lambda r: httpx.Response(200, json={})).sync_inbound_messages(
                CRED, cursor="https://evil.example.com/v1.0/me/delta"
            )

    def test_missing_permission_is_an_error(self) -> None:
        with pytest.raises(AppError):
            graph(lambda r: httpx.Response(403, json={})).sync_inbound_messages(CRED, cursor=None)

    def test_throttling_backs_off(self) -> None:
        result = graph(lambda r: httpx.Response(429, headers={"Retry-After": "12"}, json={})).sync_inbound_messages(
            CRED, cursor=None
        )
        assert result.retry_after_seconds == 12.0


# ------------------------------------------------------------------------ IMAP


class FakeImap:
    """In-memory INBOX: UID -> raw RFC 822."""

    def __init__(self, uidvalidity: int = 7, mail: dict[int, bytes] | None = None) -> None:
        self.uidvalidity = uidvalidity
        self.mail = mail or {}
        self.searches: list[tuple[int, Any]] = []
        self.closed = False

    def select_inbox(self) -> int:
        return self.uidvalidity

    def search_uids(self, *, min_uid: int, since):  # type: ignore[no-untyped-def]
        self.searches.append((min_uid, since))
        return sorted(u for u in self.mail if u >= min_uid)

    def fetch(self, uids: list[int]) -> list[FetchedImapMessage]:
        return [FetchedImapMessage(u, self.mail[u], datetime(2026, 9, 20, tzinfo=UTC)) for u in uids]

    def close(self) -> None:
        self.closed = True


def mime(n: int, *, extra: str = "", frm: str = "lead@x.test") -> bytes:
    return (
        f"From: Lead <{frm}>\r\nTo: sender@acme.test\r\nSubject: Re: hi {n}\r\n"
        f"Message-ID: <in-{n}@x.test>\r\nIn-Reply-To: <sent-1@acme.test>\r\n"
        f"References: <sent-1@acme.test>\r\n{extra}\r\nhello {n}\r\n"
    ).encode()


class TestImapSync:
    NOW = datetime(2026, 9, 26, tzinfo=UTC)

    def sync(self, session, cursor=None, page_size=50):  # type: ignore[no-untyped-def]
        return sync_imap_page(session, cursor, page_size=page_size, horizon_days=30, now=self.NOW)

    def test_first_sync_searches_only_the_horizon(self) -> None:
        session = FakeImap(mail={1: mime(1)})
        result = self.sync(session)
        assert session.searches == [(1, (self.NOW - timedelta(days=30)).date())]
        assert json.loads(result.next_cursor or "") == {"v": 7, "u": 1}
        message = result.messages[0]
        # Named by Message-ID, so a UIDVALIDITY reset cannot store it twice.
        assert message.provider_message_id == "in-1@x.test"
        assert message.in_reply_to == "<sent-1@acme.test>" and message.from_address == "lead@x.test"

    def test_incremental_sync_reads_only_newer_uids_without_the_horizon(self) -> None:
        session = FakeImap(mail={1: mime(1), 2: mime(2), 3: mime(3)})
        result = self.sync(session, cursor=json.dumps({"v": 7, "u": 2}))
        assert session.searches == [(3, None)]
        assert [m.provider_message_id for m in result.messages] == ["in-3@x.test"]
        assert json.loads(result.next_cursor or "")["u"] == 3

    def test_pages_are_bounded_and_progress_is_recorded(self) -> None:
        session = FakeImap(mail={i: mime(i) for i in range(1, 6)})
        first = self.sync(session, page_size=2)
        assert len(first.messages) == 2 and first.has_more is True
        second = self.sync(session, cursor=first.next_cursor, page_size=2)
        assert [m.provider_message_id for m in second.messages] == ["in-3@x.test", "in-4@x.test"]
        last = self.sync(session, cursor=second.next_cursor, page_size=2)
        assert len(last.messages) == 1 and last.has_more is False

    def test_uidvalidity_change_invalidates_the_cursor(self) -> None:
        session = FakeImap(uidvalidity=99, mail={1: mime(1)})
        result = self.sync(session, cursor=json.dumps({"v": 7, "u": 500}))
        assert result.resync_required is True and result.messages == []

    def test_nothing_new_keeps_the_epoch_recorded(self) -> None:
        result = self.sync(FakeImap(mail={}), cursor=json.dumps({"v": 7, "u": 4}))
        assert json.loads(result.next_cursor or "") == {"v": 7, "u": 4} and result.messages == []

    def test_a_bounce_carries_its_machine_readable_report(self) -> None:
        raw = (
            b"From: MAILER-DAEMON@x.test\r\nSubject: Undelivered Mail Returned to Sender\r\n"
            b"Message-ID: <dsn-1@x.test>\r\nMIME-Version: 1.0\r\n"
            b"Content-Type: multipart/report; report-type=delivery-status; boundary=B\r\n\r\n"
            b"--B\r\nContent-Type: text/plain\r\n\r\nfailed\r\n"
            b"--B\r\nContent-Type: message/delivery-status\r\n\r\nFinal-Recipient: rfc822; j@t.test\r\n"
            b"Action: failed\r\nStatus: 5.1.1\r\n\r\n--B--\r\n"
        )
        message = parse_imap_message(FetchedImapMessage(9, raw), uidvalidity=7)
        assert "Status: 5.1.1" in (message.report_text or "")

    def test_a_message_without_a_message_id_falls_back_to_uid_identity(self) -> None:
        message = parse_imap_message(FetchedImapMessage(4, b"From: a@b.co\r\nSubject: x\r\n\r\nhi"), uidvalidity=7)
        assert message.provider_message_id == "imap:7:4"

    def test_auto_replies_are_flagged(self) -> None:
        message = parse_imap_message(
            FetchedImapMessage(1, mime(1, extra="Auto-Submitted: auto-replied\r\n")), uidvalidity=1
        )
        assert message.is_automated is True


class FakeConn:
    def __init__(self, response: bytes) -> None:
        self.response = response

    def uid(self, command: str, *args: str) -> tuple[str, list[Any]]:
        self.last = (command, args)
        return "OK", [self.response]


class TestImapLibSession:
    def session(self, response: bytes) -> tuple[ImapLibSession, FakeConn]:
        session = ImapLibSession.__new__(ImapLibSession)
        conn = FakeConn(response)
        session._conn = conn  # type: ignore[attr-defined]
        return session, conn

    def test_search_never_returns_a_uid_below_the_cursor(self) -> None:
        # "5:*" also returns the highest existing UID (2) when it is below 5.
        session, conn = self.session(b"2")
        assert session.search_uids(min_uid=5, since=None) == []
        assert conn.last == ("SEARCH", ("UID", "5:*"))

    def test_horizon_becomes_an_imap_since_criterion(self) -> None:
        session, conn = self.session(b"7 8")
        assert session.search_uids(min_uid=1, since=datetime(2026, 9, 1).date()) == [7, 8]
        assert conn.last[1][:2] == ("SINCE", "01-Sep-2026")


class TestSmtpImapWiring:
    CRED = {
        "host": "smtp.x.test", "port": 587, "security_mode": "STARTTLS",
        "username": "u", "password": "pw",
        "imap_host": "imap.x.test", "imap_port": 993, "imap_security_mode": "IMPLICIT_TLS",
    }  # fmt: skip

    def test_imap_login_defaults_to_the_smtp_login(self) -> None:
        assert imap_configured(self.CRED) is True
        login = imap_credential(self.CRED)
        assert (login["username"], login["password"], login["host"]) == ("u", "pw", "imap.x.test")
        dedicated = imap_credential({**self.CRED, "imap_username": "iu", "imap_password": "ipw"})
        assert (dedicated["username"], dedicated["password"]) == ("iu", "ipw")

    def test_sync_uses_the_injected_session_and_always_closes_it(self) -> None:
        fake = FakeImap(mail={1: mime(1)})
        provider = SmtpProvider(imap_connector=lambda cred: fake)
        result = provider.sync_inbound_messages(self.CRED, cursor=None)
        assert len(result.messages) == 1 and fake.closed is True

    def test_validate_imap_rejects_a_bad_login_with_a_clear_error(self) -> None:
        import imaplib

        def refuse(cred: Any) -> Any:
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")

        with pytest.raises(AppError) as exc:
            SmtpProvider(imap_connector=refuse).validate_imap(self.CRED)
        assert exc.value.status_code == PROVIDER_AUTH_REJECTED_STATUS

    def test_validate_imap_reports_an_unreachable_server(self) -> None:
        def unreachable(cred: Any) -> Any:
            raise OSError("connection refused")

        with pytest.raises(AppError) as exc:
            SmtpProvider(imap_connector=unreachable).validate_imap(self.CRED)
        assert exc.value.status_code == 502

    def test_without_imap_settings_smtp_cannot_sync(self) -> None:
        from app.modules.mailboxes.providers.base import UnsupportedCapabilityError

        with pytest.raises(UnsupportedCapabilityError):
            SmtpProvider().sync_inbound_messages({"host": "h", "port": 587}, cursor=None)


# ------------------------------------------------------------------ eligibility


class TestReplySyncStatus:
    GMAIL_SEND_ONLY = ["openid", "https://www.googleapis.com/auth/gmail.send"]

    @pytest.mark.parametrize(
        ("provider", "scopes", "config", "expected"),
        [
            ("GMAIL", GMAIL_SEND_ONLY, None, ReplySyncStatus.RECONNECT_REQUIRED),
            ("GMAIL", GMAIL_SEND_ONLY + ["https://www.googleapis.com/auth/gmail.readonly"], None, ReplySyncStatus.ENABLED),
            ("GMAIL", ["https://www.googleapis.com/auth/gmail.modify"], None, ReplySyncStatus.ENABLED),
            ("GMAIL", ["https://mail.google.com/"], None, ReplySyncStatus.ENABLED),
            ("GMAIL", [], None, ReplySyncStatus.RECONNECT_REQUIRED),
            ("MICROSOFT", ["Mail.Send", "offline_access"], None, ReplySyncStatus.RECONNECT_REQUIRED),
            ("MICROSOFT", ["https://graph.microsoft.com/Mail.ReadWrite"], None, ReplySyncStatus.ENABLED),
            ("MICROSOFT", ["mail.read"], None, ReplySyncStatus.ENABLED),
            ("SMTP", [], {"host": "h", "port": 587}, ReplySyncStatus.IMAP_NOT_CONFIGURED),
            ("SMTP", [], {"imap_host": "h", "imap_port": 993}, ReplySyncStatus.ENABLED),
            ("SMTP", [], None, ReplySyncStatus.IMAP_NOT_CONFIGURED),
            ("SENDGRID", [], None, ReplySyncStatus.UNSUPPORTED),
        ],
    )
    def test_status(self, provider: str, scopes: list[str], config: Any, expected: ReplySyncStatus) -> None:
        assert reply_sync_status(provider, scopes, config) == expected

    def test_scopes_may_arrive_as_json_or_a_space_separated_string(self) -> None:
        assert reply_sync_status("MICROSOFT", json.dumps(["Mail.ReadWrite"]), None) == ReplySyncStatus.ENABLED
        assert reply_sync_status("MICROSOFT", "Mail.Send Mail.ReadWrite", None) == ReplySyncStatus.ENABLED
