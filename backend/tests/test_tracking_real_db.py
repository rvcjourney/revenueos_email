"""Open tracking, bounce tracking and reply sync against a REAL PostgreSQL.

Migration 0030, its grants/RLS, and the SQL the workers and API actually run,
exercised as the real database roles (app_api, app_worker_general,
app_worker_sync, app_scheduler, app_worker_send). Provider mailboxes are faked
(no email is sent or read, nothing touches production).

These cover what the SQLite unit tests cannot: a missing grant or policy fails
here exactly as it would in production.
"""

# ruff: noqa: E402, E501 -- imports follow importorskip; long lines are SQL.
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest

pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import WorkspaceContext, get_db
from app.core.config import Settings
from app.db.context import set_transaction_context
from app.main import app
from app.modules.analytics.activity import CampaignActivityService
from app.modules.analytics.repository import AnalyticsRepository
from app.modules.leads.activity import LeadActivityService
from app.modules.mailboxes.providers.base import (
    ProviderCapability,
    ProviderInboundMessage,
    SyncPageResult,
)
from app.modules.replies.service import ReplySyncService
from app.modules.tracking.pixel import TRANSPARENT_GIF
from app.modules.tracking.tokens import make_open_token
from tests.support.real_pg import throwaway_database
from tests.support.real_seed import World, _insert, seed_world

pytestmark = pytest.mark.filterwarnings("ignore")

SIGNING_KEY = "test-tracking-signing-key"
HUMAN_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
SCANNER_UA = "Mozilla/5.0 (compatible; Barracuda Sentinel)"


def open_token(w, message_id=None, *, sent_seconds_ago: int = 3600) -> str:
    """A current (v2) pixel token for an email sent ``sent_seconds_ago`` ago."""
    return make_open_token(
        w.ws,
        message_id or w.message1_id,
        SIGNING_KEY,
        sent_at=datetime.now(UTC) - timedelta(seconds=sent_seconds_ago),
    )
SENT_RFC_ID = "<sent1@acme.test>"


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def uri():
    with throwaway_database() as value:
        yield value


@pytest.fixture(scope="module")
def engine(uri):
    engine = create_engine("postgresql+psycopg://" + uri.split("://", 1)[1], future=True)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@pytest.fixture()
def su(uri):
    conn = psycopg.connect(uri, autocommit=True)
    yield conn
    conn.close()


def seed_sent_world(su, *, rfc_id: str = SENT_RFC_ID, thread_ref: str | None = None) -> World:
    """A tenant whose first email was SENT (with a known Message-ID), plus a
    PLANNED follow-up that a reply or hard bounce must cancel."""
    w = seed_world(su, running=True, members=1)
    now = datetime.now(UTC)
    su.execute("SET session_replication_role = replica")
    su.execute(
        """UPDATE public.messages SET status='SENT', accepted_at=%s, due_at=%s, anchor_at=%s,
                  rendered_at=%s, content_subject='Quick idea', content_body_html='<p>Hi</p>',
                  content_digest=%s, frozen_destination='lead0@target0.test',
                  frozen_sender_address='sender@acme.test', rfc_message_id=%s
           WHERE id=%s""",
        [now, now, now, now, "a" * 64, rfc_id, w.message1_id],
    )
    if thread_ref:
        su.execute(
            """INSERT INTO public.message_attempts
               (id, workspace_id, message_id, mailbox_id, ordinal, invocation_owner,
                credential_generation, authorization_deadline, dispatch_generation,
                evidence_state, started_at, completed_at, provider_request_id, provider_thread_ref)
               VALUES (%s,%s,%s,%s,1,'t',1,%s,1,'ACCEPTED',%s,%s,%s,%s)""",
            [uuid.uuid4(), w.ws, w.message1_id, w.mailbox_id, now + timedelta(minutes=5), now, now, "req-1", thread_ref],
        )
    w.follow_up_id = uuid.uuid4()  # type: ignore[attr-defined]
    _insert(
        su,
        "messages",
        id=w.follow_up_id,  # type: ignore[attr-defined]
        workspace_id=w.ws,
        purpose="CAMPAIGN",
        campaign_id=w.campaign_id,
        enrollment_id=w.enrollment_id,
        sequence_id=w.sequence_id,
        step_id=w.step2,
        mailbox_id=w.mailbox_id,
        address_id=w.address_ids[0],
        status="PLANNED",
    )
    su.execute("SET session_replication_role = origin")
    return w


def connect_mailbox(su, w: World, *, imap: bool = True) -> None:
    """Give the seeded SMTP mailbox a live connection (optionally with IMAP)."""
    config: dict[str, Any] = {
        "host": "smtp.acme.test",
        "port": 587,
        "security_mode": "STARTTLS",
        "username": "sender@acme.test",
    }
    if imap:
        config.update(imap_host="imap.acme.test", imap_port=993, imap_security_mode="IMPLICIT_TLS")
    su.execute("SET session_replication_role = replica")
    su.execute(
        """INSERT INTO public.mailbox_connections
           (id, workspace_id, mailbox_id, generation, credential_ciphertext, encryption_key_id,
            nonce, auth_mechanism, granted_scopes, protected_config)
           VALUES (%s,%s,%s,1,%s,'v1',%s,'SMTP_PASSWORD','[]'::jsonb,%s::jsonb)""",
        [uuid.uuid4(), w.ws, w.mailbox_id, b"x", b"123456789012", __import__("json").dumps(config)],
    )
    su.execute(
        """UPDATE public.mailboxes SET connection_state='CONNECTED', connected_generation=1,
                  current_connection_generation=1, health_state='HEALTHY',
                  provider_account_id=%s WHERE id=%s""",
        [str(w.mailbox_id), w.mailbox_id],
    )
    su.execute("SET session_replication_role = origin")


class FakeProvider:
    """Signature-enforcing fake mailbox: calling it the way the pre-fix service
    did (page_token=, max_results=) raises TypeError, as the real adapters do."""

    capabilities = frozenset({ProviderCapability.REPLY_SYNC})

    def __init__(self, pages: list[SyncPageResult]) -> None:
        self.pages = list(pages)
        self.cursors: list[str | None] = []

    def sync_inbound_messages(self, credential, cursor=None, page_size=50):  # type: ignore[no-untyped-def]
        self.cursors.append(cursor)
        if self.pages:
            return self.pages.pop(0)
        return SyncPageResult(messages=[], next_cursor=cursor)


def run_sync(session_factory, w: World, pages: list[SyncPageResult]) -> tuple[Any, FakeProvider]:
    provider = FakeProvider(pages)
    with session_factory() as session:
        service = ReplySyncService(session, providers={"SMTP": provider})  # type: ignore[dict-item]
        with patch(
            "app.modules.replies.service.decrypt_credentials", return_value={"password": "x"}
        ):
            result = service.sync_mailbox(workspace_id=w.ws, mailbox_id=w.mailbox_id)
    return result, provider


def reply_message(
    provider_id: str = "in-1",
    *,
    in_reply_to: str | None = SENT_RFC_ID,
    subject: str = "Re: Quick idea",
    sender: str = "lead0@target0.test",
    headers: dict[str, str] | None = None,
    thread_id: str | None = None,
    body: str = "Sounds interesting, let's talk.",
) -> ProviderInboundMessage:
    return ProviderInboundMessage(
        provider_message_id=provider_id,
        provider_thread_id=thread_id,
        rfc_message_id=f"<{provider_id}@target0.test>",
        in_reply_to=in_reply_to,
        references=[in_reply_to] if in_reply_to else [],
        from_address=sender,
        to_addresses=["sender@acme.test"],
        subject=subject,
        body_text=body,
        received_at=datetime.now(UTC),
        headers=headers or {},
    )


def dsn_message(
    provider_id: str,
    *,
    status: str = "5.1.1",
    action: str = "failed",
    original_id: str = SENT_RFC_ID,
) -> ProviderInboundMessage:
    return ProviderInboundMessage(
        provider_message_id=provider_id,
        rfc_message_id=f"<{provider_id}@mail.acme.test>",
        from_address="mailer-daemon@googlemail.com",
        to_addresses=["sender@acme.test"],
        subject="Delivery Status Notification (Failure)",
        body_text="Your message could not be delivered.",
        received_at=datetime.now(UTC),
        headers={"content-type": "multipart/report; report-type=delivery-status"},
        report_text=(
            "Final-Recipient: rfc822; lead0@target0.test\n"
            f"Action: {action}\nStatus: {status}\n"
            f"Diagnostic-Code: smtp; 550 {status} mailbox problem\n"
            f"Message-ID: {original_id}"
        ),
    )


def page(*messages: ProviderInboundMessage, cursor: str = "c1", more: bool = False) -> SyncPageResult:
    return SyncPageResult(messages=list(messages), next_cursor=cursor, has_more=more)


def scalar(su, sql: str, params: list[Any]) -> Any:
    return su.execute(sql, params).fetchone()[0]


def as_api(session: Session, w: World, *, user: uuid.UUID | None = None) -> None:
    """Run the rest of this transaction as an app_api request for the tenant."""
    session.execute(text("RESET ROLE; SET LOCAL ROLE app_api"))
    set_transaction_context(session, user_id=user or w.owner, workspace_id=w.ws)


@pytest.fixture()
def tracking_client(session_factory, monkeypatch):
    """The real app, with the DB dependency replaced by a real app_api session
    (as get_db does) and a configured signing key."""
    real_settings = Settings.current()
    configured = real_settings.model_copy(update={"tracking_signing_key": SIGNING_KEY})
    monkeypatch.setattr("app.api.v1.tracking.Settings.current", classmethod(lambda cls: configured))

    def override_get_db():
        with session_factory() as session:
            session.execute(text("SET LOCAL ROLE app_api"))
            yield session
            session.commit()

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# migration / grants
# ---------------------------------------------------------------------------


class TestMigrationGrants:
    def test_send_worker_can_record_the_message_id_once(self, uri, su):
        w = seed_world(su, running=True, members=1)
        with psycopg.connect(uri) as conn:
            conn.execute("SET LOCAL ROLE app_worker_send")
            conn.execute("SELECT set_config('app.workspace_id', %s, true)", [str(w.ws)])
            conn.execute(
                "UPDATE public.messages SET rfc_message_id = COALESCE(rfc_message_id, %s) WHERE id = %s",
                ["<one@acme.test>", w.message1_id],
            )
            # Idempotent re-record with a different value keeps the first one.
            conn.execute(
                "UPDATE public.messages SET rfc_message_id = COALESCE(rfc_message_id, %s) WHERE id = %s",
                ["<two@acme.test>", w.message1_id],
            )
            assert (
                conn.execute("SELECT rfc_message_id FROM public.messages WHERE id=%s", [w.message1_id]).fetchone()[0]
                == "<one@acme.test>"
            )
            # ...and it can never be rewritten.
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(
                    "UPDATE public.messages SET rfc_message_id = %s WHERE id = %s",
                    ["<three@acme.test>", w.message1_id],
                )

    def test_api_role_can_only_count_opens_not_rewrite_events(self, uri, su):
        w = seed_sent_world(su)
        su.execute(
            "INSERT INTO public.message_events (workspace_id, message_id, kind, source, first_occurred_at, last_occurred_at) "
            "VALUES (%s,%s,'OPENED','PIXEL',now(),now())",
            [w.ws, w.message1_id],
        )
        with psycopg.connect(uri) as conn:
            conn.execute("SET LOCAL ROLE app_api")
            conn.execute("SELECT set_config('app.workspace_id', %s, true)", [str(w.ws)])
            conn.execute(
                "UPDATE public.message_events SET occurrence_count = occurrence_count + 1, "
                "qualified_count = qualified_count + 1, automated_count = automated_count + 1, "
                "qualified_at = COALESCE(qualified_at, now()), last_user_agent = 'x' WHERE message_id=%s",
                [w.message1_id],
            )
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("UPDATE public.message_events SET kind='BOUNCED' WHERE message_id=%s", [w.message1_id])
        with psycopg.connect(uri) as conn:
            conn.execute("SET LOCAL ROLE app_api")
            conn.execute("SELECT set_config('app.workspace_id', %s, true)", [str(w.ws)])
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("DELETE FROM public.message_events WHERE message_id=%s", [w.message1_id])

    def test_another_tenants_workspace_context_cannot_touch_the_event(self, uri, su):
        w = seed_sent_world(su)
        other = seed_world(su, running=True, members=1)
        su.execute(
            "INSERT INTO public.message_events (workspace_id, message_id, kind, source, first_occurred_at, last_occurred_at) "
            "VALUES (%s,%s,'OPENED','PIXEL',now(),now())",
            [w.ws, w.message1_id],
        )
        with psycopg.connect(uri) as conn:
            conn.execute("SET LOCAL ROLE app_api")
            conn.execute("SELECT set_config('app.workspace_id', %s, true)", [str(other.ws)])
            rows = conn.execute(
                "UPDATE public.message_events SET occurrence_count = occurrence_count + 1 WHERE message_id=%s RETURNING 1",
                [w.message1_id],
            ).fetchall()
        assert rows == []


# ---------------------------------------------------------------------------
# open tracking (public pixel endpoint)
# ---------------------------------------------------------------------------


class TestOpenTracking:
    def pixel(
        self,
        client: TestClient,
        w: World,
        message_id: uuid.UUID | None = None,
        *,
        ua: str = HUMAN_UA,
        sent_seconds_ago: int = 3600,
    ):
        token = open_token(w, message_id, sent_seconds_ago=sent_seconds_ago)
        return client.get(f"/api/v1/t/o/{token}.gif", headers={"User-Agent": ua})

    def opened_stats(self, session_factory, w: World):
        with session_factory() as session:
            as_api(session, w)
            return AnalyticsRepository(session).get_campaign_analytics(w.ws, w.campaign_id)

    def test_pixel_serves_a_gif_and_counts_repeated_opens_once_per_email(self, su, tracking_client):
        w = seed_sent_world(su)
        for _ in range(5):
            response = self.pixel(tracking_client, w)
            assert response.status_code == 200
            assert response.headers["content-type"] == "image/gif"
            assert response.content == TRANSPARENT_GIF
            assert "no-store" in response.headers["cache-control"]
        rows = su.execute(
            "SELECT kind, occurrence_count, qualified_count, automated_count "
            "FROM public.message_events WHERE message_id=%s",
            [w.message1_id],
        ).fetchall()
        # 5 requests = ONE opened email, opened 5 times.
        assert rows == [("OPENED", 5, 5, 0)]

    def test_a_scanner_fetch_right_after_send_is_stored_but_is_not_an_open(
        self, su, tracking_client, session_factory
    ):
        w = seed_sent_world(su)
        connect_mailbox(su, w)
        assert self.pixel(tracking_client, w, ua=HUMAN_UA, sent_seconds_ago=2).status_code == 200
        row = su.execute(
            "SELECT occurrence_count, qualified_count, automated_count, qualified_at, last_user_agent "
            "FROM public.message_events WHERE message_id=%s",
            [w.message1_id],
        ).fetchone()
        assert row == (1, 0, 1, None, HUMAN_UA)  # evidence kept, never counted
        stats = self.opened_stats(session_factory, w)
        assert stats["opened"] == 0 and stats["total_opens"] == 0 and stats["open_rate"] == 0.0

    def test_a_person_opening_after_the_scanner_makes_it_opened_once(
        self, su, tracking_client, session_factory
    ):
        w = seed_sent_world(su)
        connect_mailbox(su, w)
        self.pixel(tracking_client, w, ua=SCANNER_UA, sent_seconds_ago=3)  # delivery scan
        self.pixel(tracking_client, w, ua=HUMAN_UA, sent_seconds_ago=3600)  # reader
        self.pixel(tracking_client, w, ua=HUMAN_UA, sent_seconds_ago=3700)  # re-read
        row = su.execute(
            "SELECT occurrence_count, qualified_count, automated_count, qualified_at IS NOT NULL "
            "FROM public.message_events WHERE message_id=%s",
            [w.message1_id],
        ).fetchone()
        assert row == (3, 2, 1, True)
        stats = self.opened_stats(session_factory, w)
        assert stats["opened"] == 1 and stats["total_opens"] == 2

    def test_old_tokens_without_a_send_time_and_bot_agents_never_count(
        self, su, tracking_client, session_factory
    ):
        w = seed_sent_world(su)
        connect_mailbox(su, w)
        v1 = make_open_token(w.ws, w.message1_id, SIGNING_KEY)
        assert tracking_client.get(f"/api/v1/t/o/{v1}.gif", headers={"User-Agent": HUMAN_UA}).status_code == 200
        self.pixel(tracking_client, w, ua="python-requests/2.31")
        self.pixel(tracking_client, w, ua="")
        assert scalar(su, "SELECT qualified_count FROM public.message_events WHERE message_id=%s", [w.message1_id]) == 0
        assert self.opened_stats(session_factory, w)["opened"] == 0

    def test_bulk_send_scanned_at_delivery_shows_no_opens(self, su, tracking_client, session_factory):
        """Every recipient's provider scans its email seconds after a bulk send."""
        w = seed_sent_world(su)
        connect_mailbox(su, w)
        for _ in range(20):
            self.pixel(tracking_client, w, ua=HUMAN_UA, sent_seconds_ago=1)
            self.pixel(tracking_client, w, ua=SCANNER_UA, sent_seconds_ago=1)
        assert scalar(su, "SELECT occurrence_count FROM public.message_events WHERE message_id=%s", [w.message1_id]) == 40
        stats = self.opened_stats(session_factory, w)
        assert stats["opened"] == 0 and stats["total_opens"] == 0 and stats["open_rate"] == 0.0

    def test_invalid_forged_and_unknown_tokens_return_the_same_gif_and_write_nothing(self, su, tracking_client):
        w = seed_sent_world(su)
        forged = make_open_token(w.ws, w.message1_id, "some-other-key")
        unknown = make_open_token(w.ws, uuid.uuid4(), SIGNING_KEY)
        for path in (f"{forged}.gif", f"{unknown}.gif", "not-a-token.gif", "x.gif"):
            response = tracking_client.get(f"/api/v1/t/o/{path}")
            assert response.status_code == 200
            assert response.content == TRANSPARENT_GIF
        assert scalar(su, "SELECT count(*) FROM public.message_events WHERE workspace_id=%s", [w.ws]) == 0

    def test_a_token_cannot_be_replayed_against_another_tenants_message(self, su, tracking_client):
        w = seed_sent_world(su)
        other = seed_sent_world(su)
        # Valid signature for workspace A but naming a message of workspace B.
        token = open_token(w, other.message1_id)
        response = tracking_client.get(f"/api/v1/t/o/{token}.gif", headers={"User-Agent": HUMAN_UA})
        assert response.status_code == 200
        assert scalar(su, "SELECT count(*) FROM public.message_events", []) >= 0
        assert (
            scalar(su, "SELECT count(*) FROM public.message_events WHERE message_id=%s", [other.message1_id]) == 0
        )

    def test_head_requests_are_not_opens(self, su, tracking_client):
        w = seed_sent_world(su)
        token = open_token(w)
        response = tracking_client.head(f"/api/v1/t/o/{token}.gif")
        assert response.status_code == 200
        assert scalar(su, "SELECT count(*) FROM public.message_events WHERE message_id=%s", [w.message1_id]) == 0


# ---------------------------------------------------------------------------
# reply sync (scheduler -> sync service -> matcher -> stop sequence)
# ---------------------------------------------------------------------------


class TestReplySync:
    def test_scheduler_creates_the_missing_sync_state_once(self, session_factory, su):
        """Root cause #1: no mailbox_sync_states row -> nothing was ever dispatched."""
        w = seed_sent_world(su)
        connect_mailbox(su, w)
        assert scalar(su, "SELECT count(*) FROM public.mailbox_sync_states WHERE mailbox_id=%s", [w.mailbox_id]) == 0
        with session_factory() as session:
            created = ReplySyncService(session).ensure_sync_states()
        assert created >= 1
        assert scalar(su, "SELECT count(*) FROM public.mailbox_sync_states WHERE mailbox_id=%s", [w.mailbox_id]) == 1
        with session_factory() as session:
            ReplySyncService(session).ensure_sync_states()
        assert scalar(su, "SELECT count(*) FROM public.mailbox_sync_states WHERE mailbox_id=%s", [w.mailbox_id]) == 1

    def test_reply_is_matched_stored_once_and_stops_the_sequence(self, session_factory, su):
        w = seed_sent_world(su)
        connect_mailbox(su, w)
        reply = reply_message()
        result, provider = run_sync(session_factory, w, [page(reply)])

        assert result.status == "CURRENT", result.error
        assert result.replies_matched == 1 and result.enrollments_stopped == 1
        inbound = su.execute(
            "SELECT association_status, classification, subject FROM public.inbound_messages WHERE mailbox_id=%s",
            [w.mailbox_id],
        ).fetchall()
        assert inbound == [("MATCHED", "HUMAN_REPLY", "Re: Quick idea")]
        # Attributed to the right email -> campaign, sequence and step.
        link = su.execute(
            "SELECT outbound_message_id, campaign_id, enrollment_id, status FROM public.inbound_outreach_links"
            " WHERE workspace_id=%s",
            [w.ws],
        ).fetchall()
        assert link == [(w.message1_id, w.campaign_id, w.enrollment_id, "CONFIRMED")]
        assert su.execute("SELECT state, stop_reason FROM public.campaign_enrollments WHERE id=%s", [w.enrollment_id]).fetchone() == ("STOPPED", "REPLIED")
        # The pending follow-up is cancelled, the sent email is untouched.
        assert su.execute("SELECT status FROM public.messages WHERE id=%s", [w.follow_up_id]).fetchone()[0] == "CANCELLED"
        assert su.execute("SELECT status FROM public.messages WHERE id=%s", [w.message1_id]).fetchone()[0] == "SENT"
        assert scalar(su, "SELECT count(*) FROM public.recipient_outcomes WHERE kind='REPLIED' AND enrollment_id=%s", [w.enrollment_id]) == 1

        # The provider saw a cursor-only call (no page_token/max_results).
        assert provider.cursors[0] is None

    def test_a_redelivered_page_does_not_store_the_reply_twice(self, session_factory, su):
        w = seed_sent_world(su)
        connect_mailbox(su, w)
        run_sync(session_factory, w, [page(reply_message())])
        result, _ = run_sync(session_factory, w, [page(reply_message())])
        assert result.messages_deduplicated == 1 and result.messages_persisted == 0
        assert scalar(su, "SELECT count(*) FROM public.inbound_messages WHERE mailbox_id=%s", [w.mailbox_id]) == 1
        assert scalar(su, "SELECT count(*) FROM public.recipient_outcomes WHERE kind='REPLIED' AND enrollment_id=%s", [w.enrollment_id]) == 1

    def test_reply_with_a_changed_subject_still_matches_by_message_id(self, session_factory, su):
        w = seed_sent_world(su)
        connect_mailbox(su, w)
        run_sync(session_factory, w, [page(reply_message(subject="totally different subject"))])
        assert su.execute("SELECT association_status FROM public.inbound_messages WHERE mailbox_id=%s", [w.mailbox_id]).fetchone()[0] == "MATCHED"

    def test_message_id_matching_ignores_brackets_and_case(self, session_factory, su):
        w = seed_sent_world(su, rfc_id="<Sent1@Acme.Test>")
        connect_mailbox(su, w)
        run_sync(session_factory, w, [page(reply_message(in_reply_to="sent1@acme.test"))])
        assert su.execute("SELECT association_status FROM public.inbound_messages WHERE mailbox_id=%s", [w.mailbox_id]).fetchone()[0] == "MATCHED"

    def test_reply_without_headers_matches_by_provider_thread(self, session_factory, su):
        """Gmail/Graph: no In-Reply-To, but the thread id recorded at send time."""
        w = seed_sent_world(su, thread_ref="thread-42")
        connect_mailbox(su, w)
        run_sync(session_factory, w, [page(reply_message(in_reply_to=None, thread_id="thread-42"))])
        row = su.execute(
            "SELECT im.association_status, l.evidence_type FROM public.inbound_messages im"
            " JOIN public.inbound_outreach_links l ON l.inbound_message_id = im.id WHERE im.mailbox_id=%s",
            [w.mailbox_id],
        ).fetchone()
        assert row == ("MATCHED", "PROVIDER_THREAD_CORROBORATED")

    def test_mail_from_a_stranger_with_no_identifiers_is_not_stored(self, session_factory, su):
        w = seed_sent_world(su)
        connect_mailbox(su, w)
        run_sync(session_factory, w, [page(reply_message(in_reply_to=None, sender="stranger@else.test"))])
        assert scalar(su, "SELECT count(*) FROM public.inbound_messages WHERE mailbox_id=%s", [w.mailbox_id]) == 0
        assert scalar(su, "SELECT count(*) FROM public.conversations WHERE mailbox_id=%s", [w.mailbox_id]) == 0
        assert su.execute("SELECT state FROM public.campaign_enrollments WHERE id=%s", [w.enrollment_id]).fetchone()[0] == "ACTIVE"

    def test_same_lead_in_two_campaigns_is_matched_to_the_replied_email_only(self, session_factory, su):
        w = seed_sent_world(su)
        other = seed_sent_world(su, rfc_id="<other@acme.test>")
        connect_mailbox(su, w)
        # Same lead address exists in another tenant's campaign: never crosses tenants.
        run_sync(session_factory, w, [page(reply_message(in_reply_to="<other@acme.test>"))])
        assert su.execute("SELECT association_status FROM public.inbound_messages WHERE mailbox_id=%s", [w.mailbox_id]).fetchone()[0] == "UNRESOLVED"
        assert su.execute("SELECT state FROM public.campaign_enrollments WHERE id=%s", [other.enrollment_id]).fetchone()[0] == "ACTIVE"

    def test_out_of_office_is_stored_but_does_not_stop_or_count_as_a_reply(self, session_factory, su):
        w = seed_sent_world(su)
        connect_mailbox(su, w)
        ooo = reply_message(subject="Automatic reply: Out of office", headers={"auto-submitted": "auto-replied"})
        run_sync(session_factory, w, [page(ooo)])
        assert su.execute("SELECT classification FROM public.inbound_messages WHERE mailbox_id=%s", [w.mailbox_id]).fetchone()[0] == "OUT_OF_OFFICE"
        assert su.execute("SELECT state FROM public.campaign_enrollments WHERE id=%s", [w.enrollment_id]).fetchone()[0] == "ACTIVE"
        assert scalar(su, "SELECT count(*) FROM public.recipient_outcomes WHERE kind='REPLIED' AND enrollment_id=%s", [w.enrollment_id]) == 0

    def test_a_second_reply_days_later_is_stored_and_the_lead_counts_once(self, session_factory, su):
        w = seed_sent_world(su)
        connect_mailbox(su, w)
        run_sync(session_factory, w, [page(reply_message("in-1"))])
        run_sync(session_factory, w, [page(reply_message("in-2", body="Following up on my note"))])
        assert scalar(su, "SELECT count(*) FROM public.inbound_messages WHERE mailbox_id=%s", [w.mailbox_id]) == 2
        with session_factory() as session:
            as_api(session, w)
            stats = AnalyticsRepository(session).get_campaign_analytics(w.ws, w.campaign_id)
        assert stats["replied"] == 1  # one lead replied, however many times

    def test_own_sent_copy_is_never_a_reply(self, session_factory, su):
        w = seed_sent_world(su)
        connect_mailbox(su, w)
        run_sync(session_factory, w, [page(reply_message(sender="sender@acme.test"))])
        assert scalar(su, "SELECT count(*) FROM public.inbound_messages WHERE mailbox_id=%s", [w.mailbox_id]) == 0

    def test_mailbox_without_imap_settings_is_parked_not_retried(self, session_factory, su):
        w = seed_sent_world(su)
        connect_mailbox(su, w, imap=False)
        result, provider = run_sync(session_factory, w, [page(reply_message())])
        assert result.status == "UNAVAILABLE" and result.error == "IMAP_NOT_CONFIGURED"
        assert provider.cursors == []  # the provider was never called
        assert su.execute("SELECT status FROM public.mailbox_sync_states WHERE mailbox_id=%s", [w.mailbox_id]).fetchone()[0] == "UNAVAILABLE"
        assert su.execute("SELECT sync_state FROM public.mailboxes WHERE id=%s", [w.mailbox_id]).fetchone()[0] == "UNAVAILABLE"

    def test_a_failing_provider_backs_off_and_does_not_advance_the_cursor(self, session_factory, su):
        w = seed_sent_world(su)
        connect_mailbox(su, w)

        class Boom(FakeProvider):
            def sync_inbound_messages(self, credential, cursor=None, page_size=50):  # type: ignore[no-untyped-def]
                raise RuntimeError("provider exploded")

        with session_factory() as session:
            service = ReplySyncService(session, providers={"SMTP": Boom([])})  # type: ignore[dict-item]
            with patch("app.modules.replies.service.decrypt_credentials", return_value={"password": "x"}):
                first = service.sync_mailbox(workspace_id=w.ws, mailbox_id=w.mailbox_id)
        assert first.status == "ERROR"
        state = su.execute(
            "SELECT failure_count, next_due_at > now() + interval '90 seconds', lease_owner, cursor_data"
            " FROM public.mailbox_sync_states WHERE mailbox_id=%s",
            [w.mailbox_id],
        ).fetchone()
        assert state[0] == 1 and state[1] is True and state[2] is None and state[3] is None


# ---------------------------------------------------------------------------
# bounces (delivery-status notifications found in the mailbox)
# ---------------------------------------------------------------------------


class TestBounceTracking:
    def test_hard_bounce_is_recorded_against_the_email_and_stops_the_sequence(self, session_factory, su):
        w = seed_sent_world(su)
        connect_mailbox(su, w)
        result, _ = run_sync(session_factory, w, [page(dsn_message("dsn-1"))])
        assert result.status == "CURRENT", result.error

        row = su.execute(
            "SELECT kind, bounce_type, bounce_code, source, occurrence_count FROM public.message_events WHERE message_id=%s",
            [w.message1_id],
        ).fetchone()
        assert row == ("BOUNCED", "HARD", "5.1.1", "DSN", 1)
        assert su.execute("SELECT state, stop_reason FROM public.campaign_enrollments WHERE id=%s", [w.enrollment_id]).fetchone() == ("STOPPED", "HARD_BOUNCE")
        assert su.execute("SELECT status FROM public.messages WHERE id=%s", [w.follow_up_id]).fetchone()[0] == "SKIPPED"
        assert scalar(su, "SELECT count(*) FROM public.suppressions WHERE workspace_id=%s AND reason='HARD_BOUNCE' AND status='ACTIVE'", [w.ws]) == 1
        # A bounce is not a reply: nothing stored as inbound, nothing counted.
        assert scalar(su, "SELECT count(*) FROM public.inbound_messages WHERE mailbox_id=%s", [w.mailbox_id]) == 0
        assert scalar(su, "SELECT count(*) FROM public.recipient_outcomes WHERE kind='REPLIED' AND enrollment_id=%s", [w.enrollment_id]) == 0

    def test_duplicate_and_late_notifications_count_the_email_once(self, session_factory, su):
        w = seed_sent_world(su)
        connect_mailbox(su, w)
        run_sync(session_factory, w, [page(dsn_message("dsn-1"))])
        run_sync(session_factory, w, [page(dsn_message("dsn-1"))])  # redelivered page
        run_sync(session_factory, w, [page(dsn_message("dsn-2"))])  # a second DSN, days later
        assert scalar(su, "SELECT count(*) FROM public.message_events WHERE message_id=%s AND kind='BOUNCED'", [w.message1_id]) == 1
        assert scalar(su, "SELECT count(*) FROM public.suppressions WHERE workspace_id=%s AND reason='HARD_BOUNCE'", [w.ws]) == 1
        with session_factory() as session:
            as_api(session, w)
            stats = AnalyticsRepository(session).get_campaign_analytics(w.ws, w.campaign_id)
        assert stats["bounced"] == 1 and stats["bounce_rate"] == 100.0

    def test_soft_bounce_is_evidence_only(self, session_factory, su):
        w = seed_sent_world(su)
        connect_mailbox(su, w)
        run_sync(session_factory, w, [page(dsn_message("dsn-1", status="4.2.2", action="delayed"))])
        row = su.execute("SELECT bounce_type FROM public.message_events WHERE message_id=%s", [w.message1_id]).fetchone()
        assert row == ("SOFT",)
        assert su.execute("SELECT state FROM public.campaign_enrollments WHERE id=%s", [w.enrollment_id]).fetchone()[0] == "ACTIVE"
        assert scalar(su, "SELECT count(*) FROM public.suppressions WHERE workspace_id=%s", [w.ws]) == 0

    def test_a_later_hard_bounce_upgrades_a_soft_one(self, session_factory, su):
        w = seed_sent_world(su)
        connect_mailbox(su, w)
        run_sync(session_factory, w, [page(dsn_message("dsn-1", status="4.2.2", action="delayed"))])
        run_sync(session_factory, w, [page(dsn_message("dsn-2"))])
        assert su.execute("SELECT bounce_type, occurrence_count FROM public.message_events WHERE message_id=%s", [w.message1_id]).fetchone() == ("HARD", 2)

    def test_a_forged_dsn_naming_an_unknown_email_cannot_suppress_anyone(self, session_factory, su):
        w = seed_sent_world(su)
        connect_mailbox(su, w)
        run_sync(session_factory, w, [page(dsn_message("dsn-x", original_id="<never-sent@evil.test>"))])
        assert scalar(su, "SELECT count(*) FROM public.suppressions WHERE workspace_id=%s", [w.ws]) == 0
        assert su.execute("SELECT state FROM public.campaign_enrollments WHERE id=%s", [w.enrollment_id]).fetchone()[0] == "ACTIVE"

    def test_bounce_after_the_enrollment_completed_is_still_recorded(self, session_factory, su):
        """The old code recorded no outcome once the enrollment was no longer ACTIVE."""
        w = seed_sent_world(su)
        connect_mailbox(su, w)
        su.execute("UPDATE public.campaign_enrollments SET state='COMPLETED', next_step_id=NULL WHERE id=%s", [w.enrollment_id])
        run_sync(session_factory, w, [page(dsn_message("dsn-late"))])
        assert scalar(su, "SELECT count(*) FROM public.recipient_outcomes WHERE kind='HARD_BOUNCE' AND enrollment_id=%s", [w.enrollment_id]) == 1
        assert scalar(su, "SELECT count(*) FROM public.message_events WHERE message_id=%s AND kind='BOUNCED'", [w.message1_id]) == 1


# ---------------------------------------------------------------------------
# analytics + lead timeline (the whole flow, read back as app_api)
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_send_open_reply_flows_into_analytics_and_the_lead_timeline(
        self, session_factory, su, tracking_client
    ):
        w = seed_sent_world(su)
        connect_mailbox(su, w)

        # recipient opens the email three times
        token = open_token(w)
        for _ in range(3):
            response = tracking_client.get(f"/api/v1/t/o/{token}.gif", headers={"User-Agent": HUMAN_UA})
            assert response.status_code == 200
        # recipient replies; Outly's mailbox sync picks it up
        result, _ = run_sync(session_factory, w, [page(reply_message())])
        assert result.status == "CURRENT", result.error

        with session_factory() as session:
            as_api(session, w)
            stats = AnalyticsRepository(session).get_campaign_analytics(w.ws, w.campaign_id)
            steps = AnalyticsRepository(session).get_sequence_analytics(w.ws, w.campaign_id)
        assert stats["sent"] == 1
        assert stats["opened"] == 1 and stats["total_opens"] == 3
        assert stats["delivered_estimated"] == 1 and stats["open_rate"] == 100.0
        assert stats["bounced"] == 0 and stats["bounce_rate"] == 0.0
        assert stats["replied"] == 1
        first_step = next(s for s in steps if s["position"] == 1)
        assert first_step["opened"] == 1 and first_step["replied"] == 1

        with session_factory() as session:
            as_api(session, w)
            ctx = WorkspaceContext(user_id=w.owner, workspace_id=w.ws, role_code="OWNER")
            activity = LeadActivityService(session).get_activity(ctx, w.lead_ids[0])
        kinds = [item.kind for item in activity.items]
        assert set(kinds) == {"EMAIL_SENT", "EMAIL_OPENED", "REPLY_RECEIVED"}
        reply = next(i for i in activity.items if i.kind == "REPLY_RECEIVED")
        assert reply.sender_email == "lead0@target0.test"
        assert reply.sequence_step_position == 1 and reply.campaign_name == "Outbound"
        assert reply.subject == "Re: Quick idea" and reply.conversation_id is not None
        opened = next(i for i in activity.items if i.kind == "EMAIL_OPENED")
        assert opened.occurrence_count == 3

    def test_lead_activity_is_invisible_across_tenants(self, session_factory, su):
        w = seed_sent_world(su)
        other = seed_sent_world(su)
        with session_factory() as session:
            as_api(session, other, user=other.owner)
            ctx = WorkspaceContext(user_id=other.owner, workspace_id=other.ws, role_code="OWNER")
            from app.core.errors import AppError

            with pytest.raises(AppError) as exc:
                LeadActivityService(session).get_activity(ctx, w.lead_ids[0])
            assert exc.value.status_code == 404

    def test_bounced_email_is_excluded_from_opens_and_delivered(self, session_factory, su):
        w = seed_sent_world(su)
        connect_mailbox(su, w)
        su.execute(
            "INSERT INTO public.message_events (workspace_id, message_id, kind, source, first_occurred_at, last_occurred_at, "
            "occurrence_count, qualified_at, qualified_count) "
            "VALUES (%s,%s,'OPENED','PIXEL',now(),now(),2,now(),2)",
            [w.ws, w.message1_id],
        )
        run_sync(session_factory, w, [page(dsn_message("dsn-1"))])
        with session_factory() as session:
            as_api(session, w)
            stats = AnalyticsRepository(session).get_campaign_analytics(w.ws, w.campaign_id)
        # A scanner "opening" a bounced email must not push open rate above 100%.
        assert stats["bounced"] == 1 and stats["delivered_estimated"] == 0
        assert stats["opened"] == 0 and stats["open_rate"] == 0.0


class TestWebhookBounceProcessing:
    def receipt_for(self, session_factory, w: World, payload: dict[str, Any], identity: str) -> uuid.UUID:
        """Store a verified webhook receipt exactly as the endpoint does (app_api)."""
        import hashlib
        import json

        from app.modules.events.repository import EventRepository
        from app.modules.events.schemas import ScopeKind

        with session_factory() as session:
            session.execute(text("SET LOCAL ROLE app_api"))
            set_transaction_context(session, workspace_id=w.ws)
            receipt_id, inserted = EventRepository(session).store_raw_receipt(
                workspace_id=w.ws,
                mailbox_id=w.mailbox_id,
                provider="SMTP",
                scope_kind=ScopeKind.MAILBOX,
                event_identity=identity,
                source_schema="provider.event.v1",
                payload_digest=hashlib.sha256(identity.encode()).hexdigest(),
                payload_ref=json.dumps(payload),
                verified_at=datetime.now(UTC),
            )
            session.commit()
        assert inserted
        return receipt_id

    def process(self, session_factory, w: World, receipt_id: uuid.UUID):
        from app.modules.events.processor import InboundEventProcessor

        with session_factory() as session:
            session.execute(text("SET LOCAL ROLE app_worker_general"))
            set_transaction_context(session, workspace_id=w.ws)
            return InboundEventProcessor(session).process_receipt(receipt_id)

    def test_hard_bounce_webhook_records_the_message_and_suppresses_with_receipt_provenance(
        self, session_factory, su
    ):
        w = seed_sent_world(su)
        payload = {
            "event_type": "BOUNCE",
            "bounce_classification": "HARD",
            "recipient_email": "lead0@target0.test",
            "provider_message_id": None,
            "reason": "550 5.1.1 user unknown",
        }
        receipt_id = self.receipt_for(session_factory, w, payload, "smtp:bounce:evt-1")
        result = self.process(session_factory, w, receipt_id)
        assert result.suppression_created is True
        assert su.execute("SELECT state, stop_reason FROM public.campaign_enrollments WHERE id=%s", [w.enrollment_id]).fetchone() == ("STOPPED", "HARD_BOUNCE")
        assert su.execute(
            "SELECT source_kind, source_key = provider_receipt_id::text FROM public.suppression_sources WHERE workspace_id=%s",
            [w.ws],
        ).fetchone() == ("PROVIDER_RECEIPT", True)
        # The recipient's only recent email is the one that bounced.
        assert su.execute("SELECT bounce_type, source FROM public.message_events WHERE message_id=%s", [w.message1_id]).fetchone() == ("HARD", "WEBHOOK_RECIPIENT")

        # The same notification processed again changes nothing.
        again = self.process(session_factory, w, receipt_id)
        assert again.status == "already_processed"
        assert scalar(su, "SELECT count(*) FROM public.suppressions WHERE workspace_id=%s", [w.ws]) == 1

    def test_soft_bounce_webhook_never_suppresses(self, session_factory, su):
        w = seed_sent_world(su)
        payload = {
            "event_type": "BOUNCE",
            "bounce_classification": "SOFT",
            "recipient_email": "lead0@target0.test",
            "reason": "452 4.2.2 mailbox full",
        }
        receipt_id = self.receipt_for(session_factory, w, payload, "smtp:bounce:evt-soft")
        assert self.process(session_factory, w, receipt_id).status == "processed_soft_bounce"
        assert scalar(su, "SELECT count(*) FROM public.suppressions WHERE workspace_id=%s", [w.ws]) == 0
        assert su.execute("SELECT bounce_type FROM public.message_events WHERE message_id=%s", [w.message1_id]).fetchone() == ("SOFT",)


class TestBounceAssociation:
    def test_dsn_without_any_identifier_falls_back_to_the_recipients_latest_email(self, session_factory, su):
        w = seed_sent_world(su)
        connect_mailbox(su, w)
        bare = dsn_message("dsn-bare")
        bare = ProviderInboundMessage(**{**bare.__dict__, "report_text": "Final-Recipient: rfc822; lead0@target0.test\nAction: failed\nStatus: 5.1.1"})
        run_sync(session_factory, w, [page(bare)])
        assert su.execute("SELECT bounce_type, source FROM public.message_events WHERE message_id=%s", [w.message1_id]).fetchone() == ("HARD", "DSN_RECIPIENT")


class TestCampaignActivityFeed:
    """The campaign Activity feed, read back as the real app_api role."""

    def test_feed_reads_every_source_under_rls_and_is_tenant_scoped(
        self, session_factory, su, tracking_client
    ):
        w = seed_sent_world(su)
        connect_mailbox(su, w)
        token = open_token(w)
        for _ in range(3):
            assert tracking_client.get(
                f"/api/v1/t/o/{token}.gif", headers={"User-Agent": HUMAN_UA}
            ).status_code == 200
        result, _ = run_sync(session_factory, w, [page(reply_message())])
        assert result.status == "CURRENT", result.error
        su.execute(
            "INSERT INTO public.recipient_outcomes (workspace_id, enrollment_id, kind, source_key, occurred_at) "
            "VALUES (%s,%s,'UNSUBSCRIBED','tok-1',now())",
            [w.ws, w.enrollment_id],
        )

        ctx = WorkspaceContext(user_id=w.owner, workspace_id=w.ws, role_code="OWNER")
        with session_factory() as session:
            as_api(session, w)
            feed = CampaignActivityService(session).get_activity(ctx, w.campaign_id, limit=100)
        by_type = {i.type.value: i for i in feed.items}
        assert {"SENT", "OPENED", "REPLIED", "UNSUBSCRIBED"} <= set(by_type)
        assert len([i for i in feed.items if i.type.value == "OPENED"]) == 1
        assert by_type["OPENED"].open_count == 3
        assert by_type["SENT"].recipient_email == "lead0@target0.test"
        assert by_type["REPLIED"].lead_id == w.lead_ids[0]
        assert feed.counts_by_type is not None and feed.counts_by_type["OPENED"] == 1
        times = [i.occurred_at for i in feed.items]
        assert times == sorted(times, reverse=True)

        # Filters, search and keyset paging run on Postgres too.
        with session_factory() as session:
            as_api(session, w)
            service = CampaignActivityService(session)
            first = service.get_activity(ctx, w.campaign_id, limit=2, q="LEAD0@")
            assert first.has_more and first.next_cursor
            rest = service.get_activity(ctx, w.campaign_id, limit=2, q="lead0@", cursor=first.next_cursor)
            ids = [i.id for i in first.items + rest.items]
            assert len(ids) == len(set(ids))
            assert service.get_activity(ctx, w.campaign_id, q="nobody@").items == []

    def test_other_tenants_campaign_is_not_found_and_not_listed(self, session_factory, su):
        w = seed_sent_world(su)
        other = seed_sent_world(su)
        from app.core.errors import AppError

        with session_factory() as session:
            as_api(session, other, user=other.owner)
            ctx = WorkspaceContext(user_id=other.owner, workspace_id=other.ws, role_code="OWNER")
            with pytest.raises(AppError) as exc:
                CampaignActivityService(session).get_activity(ctx, w.campaign_id)
            assert exc.value.status_code == 404
            own = CampaignActivityService(session).get_activity(ctx, other.campaign_id, limit=100)
        assert all(i.message_id != w.message1_id for i in own.items)
