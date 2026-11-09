"""Follow-up emails in the same thread (ADR-0021, slice 1): STANDARD campaigns on
SMTP and Gmail. Mocked repositories and recording providers only -- no database,
no network, no email. The SQL, grants and guard triggers are proven against a
real PostgreSQL in test_same_thread_real_db.py."""

# ruff: noqa: E501
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.api.deps import WorkspaceContext
from app.core.errors import AppError
from app.modules.campaigns.message_rendering import (
    compute_content_digest,
    compute_sequence_content_digest,
    render_step_content,
    reply_subject,
)
from app.modules.campaigns.reconcile import ReconcileService
from app.modules.campaigns.schemas import (
    SequenceStepCreateIn,
    SequenceStepReorderEntry,
    SequenceStepsReorderIn,
    SequenceStepUpdateIn,
)
from app.modules.campaigns.sequence_service import (
    SequenceService,
    describe_sequence_problem,
)
from app.modules.mailboxes.providers.base import (
    OutboundMessageEnvelope,
    ProviderSendResult,
)
from app.modules.mailboxes.providers.gmail import GmailProvider
from app.modules.mailboxes.providers.message_builder import build_rfc5322_message
from app.modules.mailboxes.providers.registry import ProviderRegistry
from tests.test_sending_service import _authorized_service, _ctx, _payload

WS = uuid.uuid4()
CAMPAIGN = uuid.uuid4()
SEQUENCE = uuid.uuid4()


@pytest.fixture(autouse=True)
def _reset_registry():
    yield
    ProviderRegistry.reset()


# ---------------------------------------------------------------------------
# Subject rule (D3)
# ---------------------------------------------------------------------------


class TestReplySubject:
    def test_prefixes_re(self) -> None:
        assert reply_subject("Quick idea for Acme") == "Re: Quick idea for Acme"

    @pytest.mark.parametrize("subject", ["Re: Hello", "RE: Hello", "re:Hello", "  Re: Hello  "])
    def test_does_not_double_an_existing_re(self, subject: str) -> None:
        assert reply_subject(subject) == subject.strip()

    def test_is_truncated_to_the_column_limit(self) -> None:
        out = reply_subject("x" * 500)
        assert len(out) == 500 and out.startswith("Re: ")

    def test_threaded_render_keeps_the_subject_and_digests_it(self) -> None:
        rendered = render_step_content(
            subject="",
            body_html="<p>Hi {{first_name}}</p>",
            frozen_variables={"first_name": "Ann"},
            renderer_version=1,
            thread_subject="Re: {{not a template}}",
        )
        assert rendered.subject == "Re: {{not a template}}"
        assert rendered.body_html == "<p>Hi Ann</p>"
        assert rendered.content_digest == compute_content_digest(
            "Re: {{not a template}}", "<p>Hi Ann</p>", 1
        )


# ---------------------------------------------------------------------------
# Sequence digest (D6): existing digests byte-identical
# ---------------------------------------------------------------------------


def _steps(*, flag: bool | None) -> list[dict]:
    second: dict = {
        "position": 3, "kind": "EMAIL", "email_subject": None,
        "email_body_html": "<p>Two</p>", "wait_duration_minutes": None,
    }
    if flag is not None:
        second["email_same_thread"] = flag
    return [
        {"position": 1, "kind": "EMAIL", "email_subject": "One",
         "email_body_html": "<p>One</p>", "wait_duration_minutes": None,
         "email_same_thread": False},
        {"position": 2, "kind": "WAIT", "email_subject": None,
         "email_body_html": None, "wait_duration_minutes": 60},
        second,
    ]


class TestSequenceDigest:
    def test_an_unthreaded_sequence_hashes_exactly_as_before(self) -> None:
        legacy = [
            {k: s[k] for k in ("position", "kind", "email_subject", "email_body_html", "wait_duration_minutes")}
            for s in _steps(flag=None)
        ]
        before = hashlib.sha256(
            json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        assert compute_sequence_content_digest(_steps(flag=None)) == before
        assert compute_sequence_content_digest(_steps(flag=False)) == before

    def test_the_flag_changes_the_digest_when_on(self) -> None:
        assert compute_sequence_content_digest(_steps(flag=True)) != compute_sequence_content_digest(
            _steps(flag=False)
        )


# ---------------------------------------------------------------------------
# Sequence editing (D1, D7, D9)
# ---------------------------------------------------------------------------


def _row(position: int, kind: str = "EMAIL", *, thread: bool = False, subject: str | None = "S") -> dict:
    return {
        "id": uuid.uuid4(), "sequence_id": SEQUENCE, "campaign_id": CAMPAIGN,
        "position": position, "kind": kind,
        "email_subject": subject if kind == "EMAIL" else None,
        "email_body_html": "<p>b</p>" if kind == "EMAIL" else None,
        "email_preheader": None, "email_variable_schema": {},
        "wait_duration_minutes": 60 if kind == "WAIT" else None,
        "source_template_version_id": None,
        "email_same_thread": thread, "version": 1,
        "created_at": datetime.now(UTC), "updated_at": datetime.now(UTC),
    }


def _context(role: str = "MANAGER") -> WorkspaceContext:
    return WorkspaceContext(workspace_id=WS, user_id=uuid.uuid4(), role_code=role)


def _sequence_service(
    steps_after: list[dict], *, campaign_type: str = "STANDARD", status: str = "DRAFT"
) -> SequenceService:
    service = SequenceService(MagicMock())
    repo = MagicMock()
    repo.get_campaign.return_value = {
        "id": CAMPAIGN, "status": status, "campaign_type": campaign_type,
        "activation_id": uuid.uuid4() if status != "DRAFT" else None,
    }
    repo.get_sequence.return_value = {"id": SEQUENCE, "campaign_id": CAMPAIGN, "status": "DRAFT"}
    repo.list_steps_ordered.return_value = steps_after
    repo.list_step_attachments.return_value = []
    repo.insert_step.side_effect = lambda **kw: {**_row(kw["position"], kw["kind"]), **kw}
    service.repo = repo
    return service


class TestStepValidation:
    def test_a_threaded_follow_up_needs_no_subject(self) -> None:
        service = _sequence_service([_row(1), _row(2, "WAIT"), _row(3, thread=True, subject=None)])
        service.add_step(
            _context(), CAMPAIGN,
            SequenceStepCreateIn(kind="EMAIL", position=3, email_body_html="<p>Bump</p>", email_same_thread=True),
        )
        kwargs = service.repo.insert_step.call_args.kwargs
        assert kwargs["email_same_thread"] is True
        assert kwargs["email_subject"] is None

    def test_an_unthreaded_email_still_needs_a_subject(self) -> None:
        service = _sequence_service([])
        with pytest.raises(AppError) as info:
            service.add_step(_context(), CAMPAIGN, SequenceStepCreateIn(kind="EMAIL", position=1, email_body_html="<p>x</p>"))
        assert info.value.status_code == 422

    def test_the_first_email_cannot_be_threaded(self) -> None:
        service = _sequence_service([_row(1, thread=True, subject=None)])
        with pytest.raises(AppError) as info:
            service.add_step(
                _context(), CAMPAIGN,
                SequenceStepCreateIn(kind="EMAIL", position=1, email_body_html="<p>x</p>", email_same_thread=True),
            )
        assert (info.value.code, info.value.status_code) == ("same_thread_requires_previous_email", 422)

    def test_a_wait_cannot_be_threaded(self) -> None:
        service = _sequence_service([_row(1)])
        with pytest.raises(AppError) as info:
            service.add_step(
                _context(), CAMPAIGN,
                SequenceStepCreateIn(kind="WAIT", position=2, wait_duration_minutes=60, email_same_thread=True),
            )
        assert info.value.status_code == 422

    def test_reorder_that_puts_a_threaded_step_first_is_refused(self) -> None:
        first, wait, threaded = _row(1), _row(2, "WAIT"), _row(3, thread=True, subject=None)
        service = _sequence_service([first, wait, threaded])
        service.repo.list_steps_ordered.side_effect = [
            [first, wait, threaded],
            [{**threaded, "position": 1}, wait, {**first, "position": 3}],
        ]
        with pytest.raises(AppError) as info:
            service.reorder_steps(
                _context(), CAMPAIGN,
                SequenceStepsReorderIn(steps=[
                    SequenceStepReorderEntry(step_id=threaded["id"], position=1),
                    SequenceStepReorderEntry(step_id=wait["id"], position=2),
                    SequenceStepReorderEntry(step_id=first["id"], position=3),
                ]),
            )
        assert info.value.code == "same_thread_requires_previous_email"

    def test_delete_that_leaves_a_threaded_step_first_is_refused(self) -> None:
        first, wait, threaded = _row(1), _row(2, "WAIT"), _row(3, thread=True, subject=None)
        service = _sequence_service([first, wait, threaded])
        service.repo.delete_step.return_value = True
        service.repo.list_steps_ordered.side_effect = [
            [first, wait, threaded],
            [{**threaded, "position": 1}],
        ]
        with pytest.raises(AppError) as info:
            service.delete_step(_context(), CAMPAIGN, first["id"], with_adjacent_wait=True)
        assert info.value.code == "same_thread_requires_previous_email"

    def test_turning_the_flag_off_needs_a_subject(self) -> None:
        threaded = _row(3, thread=True, subject=None)
        service = _sequence_service([_row(1), _row(2, "WAIT"), threaded])
        service.repo.get_step.return_value = threaded
        with pytest.raises(AppError) as info:
            service.update_step(
                _context(), CAMPAIGN, threaded["id"],
                SequenceStepUpdateIn(expected_version=1, email_same_thread=False),
            )
        assert info.value.status_code == 422
        service.repo.update_step.assert_not_called()

    def test_paused_toggle_on_a_step_leads_reached_is_locked(self) -> None:
        step = _row(3)
        service = _sequence_service([_row(1), _row(2, "WAIT"), step], status="PAUSED")
        service.repo.get_step.return_value = step
        service.repo.get_sequence_frontier.return_value = 3
        with pytest.raises(AppError) as info:
            service.update_step(
                _context(), CAMPAIGN, step["id"],
                SequenceStepUpdateIn(expected_version=1, email_same_thread=True),
            )
        assert info.value.code == "step_locked"
        service.repo.update_step.assert_not_called()

    def test_duplicate_step_copies_the_flag(self) -> None:
        source = _row(3, thread=True, subject=None)
        service = _sequence_service([_row(1), _row(2, "WAIT"), source])
        service.repo.get_step.return_value = source
        service.get_sequence = MagicMock()  # type: ignore[method-assign]
        service.duplicate_step(_context(), CAMPAIGN, source["id"])
        email_insert = service.repo.insert_step.call_args_list[-1].kwargs
        assert email_insert["kind"] == "EMAIL" and email_insert["email_same_thread"] is True

    def test_readiness_accepts_a_threaded_follow_up_without_subject(self) -> None:
        assert describe_sequence_problem([_row(1), _row(2, "WAIT"), _row(3, thread=True, subject=None)]) is None
        assert describe_sequence_problem([_row(1, subject=None)]) == "Every email needs a subject"


def test_campaign_duplicate_copies_the_flag() -> None:
    from app.modules.campaigns.schemas import CampaignDuplicateIn
    from app.modules.campaigns.service import CampaignService

    source_steps = [_row(1), _row(2, "WAIT"), _row(3, thread=True, subject=None)]
    service = CampaignService(MagicMock())
    repo = MagicMock()
    repo.get_campaign.return_value = {
        "id": CAMPAIGN, "name": "Src", "description": None, "status": "DRAFT",
        "campaign_type": "STANDARD", "current_settings_id": None,
    }
    repo.get_sequence.return_value = {"id": SEQUENCE, "personalization_config": None}
    repo.list_steps_ordered.return_value = source_steps
    repo.insert_step.side_effect = lambda **kw: {"id": uuid.uuid4(), **kw}
    repo.list_campaign_mailboxes.return_value = []
    repo.list_settings_versions.return_value = []
    repo.create_campaign.return_value = {"id": uuid.uuid4()}
    repo.create_sequence.return_value = {"id": uuid.uuid4()}
    service.repo = repo
    service.get_campaign = MagicMock()  # type: ignore[method-assign]
    service.duplicate_campaign(_context(), CAMPAIGN, CampaignDuplicateIn(name="Copy"))
    flags = [c.kwargs.get("email_same_thread") for c in repo.insert_step.call_args_list]
    assert flags == [False, False, True]


# ---------------------------------------------------------------------------
# Reconcile (D7): body-only re-render keeps the frozen "Re:" subject
# ---------------------------------------------------------------------------


def test_reconcile_rerenders_only_the_body_of_a_threaded_message() -> None:
    row = {
        "rfc_message_id": None, "dispatch_origin": None, "has_live_attempt": False,
        "renderer_version": 1, "email_same_thread": True, "email_subject": None,
        "email_body_html": "<p>New {{first_name}}</p>", "email_preheader": None,
        "frozen_variables": {"first_name": "Ann"},
        "content_subject": "Re: Quick idea", "content_digest": "0" * 64,
    }
    out = ReconcileService._changed_content(row)
    assert out == (
        "Re: Quick idea", "<p>New Ann</p>", compute_content_digest("Re: Quick idea", "<p>New Ann</p>", 1)
    )


# ---------------------------------------------------------------------------
# Send path (D4, D5)
# ---------------------------------------------------------------------------

THREAD = dict(
    reply_to_message_id=uuid.uuid4(),
    thread_in_reply_to="<two@acme.test>",
    thread_references=("<one@acme.test>", "<two@acme.test>"),
    thread_provider_id="gmail-thread-1",
)


def _send(ctx, result=None, provider: str = "GMAIL"):
    fake = MagicMock()
    fake.capabilities = frozenset()
    fake.send_message.return_value = result or ProviderSendResult(status="ACCEPTED", provider_message_id="p")
    ProviderRegistry.register(provider, fake)
    service = _authorized_service(ctx)
    with patch("app.modules.sending.service.decrypt_credentials", return_value={"access_token": "x"}):
        service.execute(_payload(workspace_id=ctx.workspace_id, message_id=ctx.message_id))
    audits = [c.kwargs for c in service.repository.record_audit_event.call_args_list]
    return fake.send_message.call_args.args[1], audits, service


def _thread_audits(audits):
    return [a["reason"] for a in audits if a["action"] == "message.same_thread_unavailable"]


class TestSendThreading:
    def test_gmail_gets_reply_headers_and_the_parent_thread(self) -> None:
        envelope, audits, _ = _send(_ctx(**THREAD))
        headers = dict(envelope.extra_headers)
        assert headers["In-Reply-To"] == "<two@acme.test>"
        assert headers["References"] == "<one@acme.test> <two@acme.test>"
        assert envelope.provider_thread_id == "gmail-thread-1"
        assert _thread_audits(audits) == []

    def test_smtp_gets_the_headers_and_no_thread_id(self) -> None:
        envelope, audits, _ = _send(_ctx(mailbox_provider="SMTP", **THREAD), provider="SMTP")
        assert dict(envelope.extra_headers)["In-Reply-To"] == "<two@acme.test>"
        assert envelope.provider_thread_id is None
        assert _thread_audits(audits) == []

    def test_an_unthreaded_message_carries_no_reply_headers(self) -> None:
        envelope, audits, _ = _send(_ctx())
        assert "In-Reply-To" not in dict(envelope.extra_headers)
        assert envelope.provider_thread_id is None
        assert _thread_audits(audits) == []

    def test_a_parent_without_message_id_sends_unthreaded_and_records_why(self) -> None:
        ctx = _ctx(**{**THREAD, "thread_in_reply_to": None, "thread_references": ()})
        envelope, audits, service = _send(ctx)
        assert "In-Reply-To" not in dict(envelope.extra_headers)
        assert envelope.provider_thread_id is None
        assert envelope.subject == ctx.content_subject  # frozen subject kept
        assert service.repository.finalize_attempt_result.call_args.kwargs["message_status"] == "SENT"
        assert _thread_audits(audits) == ["parent_message_id_missing"]

    def test_microsoft_is_given_the_parent_to_reply_to(self) -> None:
        envelope, audits, _ = _send(_ctx(mailbox_provider="MICROSOFT", **THREAD), provider="MICROSOFT")
        assert envelope.in_reply_to == "<two@acme.test>"  # Graph threads via createReply
        assert envelope.provider_thread_id is None  # a Gmail-only field
        assert _thread_audits(audits) == []

    def test_a_graph_parent_that_was_not_found_is_recorded(self) -> None:
        _, audits, _ = _send(
            _ctx(mailbox_provider="MICROSOFT", **THREAD),
            ProviderSendResult(status="ACCEPTED", provider_message_id="p", thread_dropped=True),
            provider="MICROSOFT",
        )
        assert _thread_audits(audits) == ["provider_thread_not_found"]

    def test_an_unthreaded_or_parentless_message_names_no_parent(self) -> None:
        plain, _, _ = _send(_ctx())
        assert plain.in_reply_to is None
        missing, _, _ = _send(_ctx(**{**THREAD, "thread_in_reply_to": None, "thread_references": ()}))
        assert missing.in_reply_to is None

    def test_a_gmail_thread_that_was_dropped_is_recorded(self) -> None:
        _, audits, _ = _send(
            _ctx(**THREAD),
            ProviderSendResult(status="ACCEPTED", provider_message_id="p", thread_dropped=True),
        )
        assert _thread_audits(audits) == ["provider_thread_not_found"]


def test_the_mime_message_carries_the_reply_headers() -> None:
    msg = build_rfc5322_message(
        OutboundMessageEnvelope(
            to_address="a@b.test", from_address="s@acme.test", subject="Re: Hi", body_html="<p>x</p>",
            extra_headers=(("In-Reply-To", "<two@acme.test>"), ("References", "<one@acme.test> <two@acme.test>")),
        )
    )
    assert msg["In-Reply-To"] == "<two@acme.test>"
    assert msg["References"] == "<one@acme.test> <two@acme.test>"


# ---------------------------------------------------------------------------
# Gmail adapter (D4): threadId, and one retry without it
# ---------------------------------------------------------------------------


def _gmail(handler) -> GmailProvider:
    return GmailProvider(client_id="id", client_secret="s", http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def _envelope(thread: str | None = "t-1") -> OutboundMessageEnvelope:
    return OutboundMessageEnvelope(
        to_address="lead@example.com", from_address="s@example.com", subject="Re: Hi",
        body_html="<p>x</p>", rfc_message_id="<m@example.com>", provider_thread_id=thread,
    )


class TestGmailThread:
    def test_the_thread_id_is_sent(self) -> None:
        posts: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(403)
            posts.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "m", "threadId": "t-1"})

        result = _gmail(handler).send_message({"access_token": "x"}, _envelope())
        assert result.status == "ACCEPTED" and not result.thread_dropped
        assert [p.get("threadId") for p in posts] == ["t-1"]

    def test_no_thread_id_key_when_unthreaded(self) -> None:
        posts: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(403)
            posts.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "m", "threadId": "t-9"})

        _gmail(handler).send_message({"access_token": "x"}, _envelope(thread=None))
        assert list(posts[0]) == ["raw"]

    @pytest.mark.parametrize(
        ("status", "message"),
        [(404, "Requested entity was not found."), (400, "Invalid thread_id value")],
    )
    def test_an_unknown_thread_is_retried_once_without_it(self, status: int, message: str) -> None:
        posts: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(403)
            body = json.loads(request.content)
            posts.append(body)
            if "threadId" in body:
                return httpx.Response(status, json={"error": {"code": status, "message": message}})
            return httpx.Response(200, json={"id": "m2", "threadId": "t-new"})

        result = _gmail(handler).send_message({"access_token": "x"}, _envelope())
        assert result.status == "ACCEPTED" and result.thread_dropped
        assert [("threadId" in p) for p in posts] == [True, False]
        assert posts[0]["raw"] == posts[1]["raw"]

    def test_other_rejections_are_not_retried(self) -> None:
        posts: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            posts.append(json.loads(request.content))
            return httpx.Response(400, json={"error": {"code": 400, "message": "Invalid To header"}})

        result = _gmail(handler).send_message({"access_token": "x"}, _envelope())
        assert result.status == "DEFINITIVELY_REJECTED"
        assert len(posts) == 1

    def test_a_retry_that_fails_too_is_reported_as_that_failure(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"error": {"code": 404, "message": "Requested entity was not found."}})

        result = _gmail(handler).send_message({"access_token": "x"}, _envelope())
        assert result.status == "DEFINITIVELY_REJECTED"


# ---------------------------------------------------------------------------
# Step test send (D8)
# ---------------------------------------------------------------------------


def test_step_test_send_of_a_threaded_step_uses_the_previous_subject(monkeypatch) -> None:
    from app.modules.campaigns import step_test_send_service as module

    first, wait, threaded = _row(1, subject="Idea for {{company|you}}"), _row(2, "WAIT"), _row(3, thread=True, subject=None)
    service = module.StepTestSendService(MagicMock(), storage_factory=MagicMock())
    repo = MagicMock()
    repo.get_campaign.return_value = {"id": CAMPAIGN}
    repo.get_step.return_value = threaded
    repo.list_steps_ordered.return_value = [first, wait, threaded]
    repo.list_campaign_mailboxes.return_value = [{"mailbox_id": MAILBOX}]
    repo.list_step_attachments.return_value = []
    service.repo = repo
    sent: dict = {}

    class FakeMailboxService:
        def __init__(self, *_a) -> None: ...

        def send_controlled_test_email(self, **kw):
            sent.update(kw)
            return MagicMock()

    monkeypatch.setattr(module, "MailboxService", FakeMailboxService)
    monkeypatch.setattr(module, "MailboxRepository", MagicMock())
    from app.modules.campaigns.schemas import StepTestSendIn

    service.send(
        _context(), CAMPAIGN, threaded["id"],
        StepTestSendIn(mailbox_id=MAILBOX, recipient_email="qa@example.com", confirm_recipient=True),
        "key-1",
    )
    assert sent["subject"] == "[Test] Re: Idea for Acme Corp"


MAILBOX = uuid.uuid4()
