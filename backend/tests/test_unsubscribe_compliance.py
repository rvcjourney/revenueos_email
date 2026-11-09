# ruff: noqa: E501 -- long test names and HTML literals
from __future__ import annotations

from uuid import uuid4

import pytest

from app.core.config import Settings
from app.core.errors import AppError
from app.modules.mailboxes.providers.base import OutboundMessageEnvelope
from app.modules.mailboxes.providers.message_builder import build_rfc5322_message
from app.modules.unsubscribe.compliance import (
    DEFAULT_FOOTER_TEXT,
    ComplianceInfo,
    build_footer_html,
    build_footer_text,
    html_to_text,
    inject_footer,
    read_compliance,
    unsubscribe_headers,
    unsubscribe_ready,
    unsubscribe_url,
)
from app.modules.unsubscribe.tokens import (
    TOKEN_PREFIX,
    is_stateless_token,
    make_unsubscribe_token,
    parse_unsubscribe_token,
)

KEY = "k" * 32


class TestToken:
    def test_round_trip_names_the_message(self) -> None:
        ws, mid = uuid4(), uuid4()
        token = make_unsubscribe_token(ws, mid, KEY)

        assert token.startswith(TOKEN_PREFIX) and is_stateless_token(token)
        parsed = parse_unsubscribe_token(token, KEY)
        assert parsed is not None
        assert (parsed.workspace_id, parsed.message_id) == (ws, mid)

    def test_it_is_url_safe_and_short(self) -> None:
        token = make_unsubscribe_token(uuid4(), uuid4(), KEY)
        assert len(token) < 80
        assert all(c.isalnum() or c in "-_." for c in token)

    def test_a_different_key_cannot_verify_it(self) -> None:
        token = make_unsubscribe_token(uuid4(), uuid4(), KEY)
        assert parse_unsubscribe_token(token, "z" * 32) is None

    def test_a_tampered_token_is_rejected(self) -> None:
        token = make_unsubscribe_token(uuid4(), uuid4(), KEY)
        flipped = token[:-2] + ("AA" if not token.endswith("AA") else "BB")
        assert parse_unsubscribe_token(flipped, KEY) is None

    def test_swapping_in_another_message_id_breaks_the_signature(self) -> None:
        ws = uuid4()
        a = make_unsubscribe_token(ws, uuid4(), KEY)
        b = make_unsubscribe_token(ws, uuid4(), KEY)
        # Same length, different payload: splicing halves must not verify.
        spliced = a[: len(a) // 2] + b[len(b) // 2 :]
        assert parse_unsubscribe_token(spliced, KEY) is None

    @pytest.mark.parametrize(
        "bad", ["", "u1.", "u1.!!!", "plain", "u1." + "A" * 500, "x1.abc"]
    )
    def test_malformed_input_is_rejected_not_raised(self, bad: str) -> None:
        assert parse_unsubscribe_token(bad, KEY) is None

    def test_an_open_tracking_token_is_not_an_unsubscribe_token(self) -> None:
        """Domain separation: the same key must not make one kind pass as the other."""
        from app.modules.tracking.tokens import make_open_token

        open_token = make_open_token(uuid4(), uuid4(), KEY)
        assert parse_unsubscribe_token(TOKEN_PREFIX + open_token, KEY) is None

    def test_no_key_means_no_token(self) -> None:
        with pytest.raises(ValueError):
            make_unsubscribe_token(uuid4(), uuid4(), "")
        assert parse_unsubscribe_token("u1.abc", "") is None


class TestConfiguration:
    def _settings(self, **kw: str) -> Settings:
        return Settings.current().model_copy(update=kw)

    def test_ready_only_with_both_values_and_a_real_origin(self) -> None:
        ok = self._settings(
            unsubscribe_base_url="https://app.example.com", unsubscribe_signing_key=KEY
        )
        assert unsubscribe_ready(ok)
        for bad in (
            {"unsubscribe_base_url": "", "unsubscribe_signing_key": KEY},
            {"unsubscribe_base_url": "https://app.example.com", "unsubscribe_signing_key": ""},
            {"unsubscribe_base_url": "app.example.com", "unsubscribe_signing_key": KEY},
            {"unsubscribe_base_url": "ftp://app.example.com", "unsubscribe_signing_key": KEY},
        ):
            assert not unsubscribe_ready(self._settings(**bad)), bad

    def test_the_url_points_at_the_public_endpoint(self) -> None:
        settings = self._settings(
            unsubscribe_base_url="https://app.example.com/", unsubscribe_signing_key=KEY
        )
        ws, mid = uuid4(), uuid4()
        url = unsubscribe_url(settings, ws, mid)
        assert url.startswith("https://app.example.com/api/v1/unsubscribe/u1.")
        token = url.rsplit("/", 1)[1]
        parsed = parse_unsubscribe_token(token, KEY)
        assert parsed is not None and parsed.message_id == mid

    def test_headers_are_the_rfc_8058_pair(self) -> None:
        headers = dict(unsubscribe_headers("https://x.test/u"))
        assert headers == {
            "List-Unsubscribe": "<https://x.test/u>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        }


class TestComplianceSettings:
    def test_reads_the_workspace_section(self) -> None:
        info = read_compliance(
            {"compliance": {"postal_address": " 1 Main St\nMumbai ", "footer_text": "Bye"}}
        )
        assert info == ComplianceInfo(postal_address="1 Main St\nMumbai", footer_text="Bye")

    @pytest.mark.parametrize(
        "defaults",
        [None, {}, {"compliance": None}, {"compliance": "x"}, {"compliance": {"postal_address": 5}}, "text", []],
    )
    def test_anything_malformed_reads_as_not_set(self, defaults: object) -> None:
        assert read_compliance(defaults) == ComplianceInfo()

    def test_values_are_length_capped(self) -> None:
        info = read_compliance(
            {"compliance": {"postal_address": "a" * 5000, "footer_text": "b" * 5000}}
        )
        assert len(info.postal_address) == 500 and len(info.footer_text) == 300


class TestFooter:
    URL = "https://app.example.com/api/v1/unsubscribe/u1.abc"

    def test_html_has_the_link_the_address_and_the_default_line(self) -> None:
        html = build_footer_html(self.URL, ComplianceInfo(postal_address="1 Main St\nMumbai"))
        assert f'href="{self.URL}"' in html
        assert "1 Main St<br>Mumbai" in html
        assert DEFAULT_FOOTER_TEXT in html

    def test_workspace_text_is_escaped(self) -> None:
        html = build_footer_html(
            self.URL,
            ComplianceInfo(postal_address="<script>x</script>", footer_text='"><img src=x>'),
        )
        assert "<script>" not in html and "<img" not in html
        assert "&lt;script&gt;" in html

    def test_text_footer_has_the_plain_url(self) -> None:
        text = build_footer_text(self.URL, ComplianceInfo(postal_address="1 Main St"))
        assert f"Unsubscribe: {self.URL}" in text and "1 Main St" in text

    def test_footer_goes_before_the_closing_body_tag(self) -> None:
        out = inject_footer("<html><body><p>Hi</p></body></html>", "<div>F</div>")
        assert out == "<html><body><p>Hi</p><div>F</div></body></html>"

    def test_footer_is_appended_to_a_fragment(self) -> None:
        assert inject_footer("<p>Hi</p>", "<div>F</div>") == "<p>Hi</p><div>F</div>"


class TestHtmlToText:
    def test_paragraphs_breaks_lists_and_links(self) -> None:
        text = html_to_text(
            "<p>Hi Sam,</p><p>See <a href='https://x.test/a'>our site</a>.<br>Thanks</p>"
            "<ul><li>One</li><li>Two</li></ul>"
        )
        assert text.splitlines()[0] == "Hi Sam,"
        assert "our site (https://x.test/a)" in text
        assert "Thanks" in text and "- One" in text and "- Two" in text

    def test_scripts_styles_and_head_are_dropped(self) -> None:
        text = html_to_text(
            "<html><head><title>T</title><style>p{}</style></head>"
            "<body><script>alert(1)</script><p>Hello &amp; welcome</p></body></html>"
        )
        assert text == "Hello & welcome"

    def test_a_link_whose_text_is_its_url_is_not_repeated(self) -> None:
        assert html_to_text("<a href='https://x.test'>https://x.test</a>") == "https://x.test"

    def test_output_never_has_runs_of_blank_lines(self) -> None:
        assert "\n\n\n" not in html_to_text("<p>a</p><p></p><p></p><p>b</p>")


class TestMimeAssembly:
    BASE = dict(
        to_address="lead@target.test",
        from_address="me@acme.test",
        subject="Hello",
        body_html="<p>Hi</p>",
    )

    def test_the_headers_and_a_real_text_part_are_in_the_message(self) -> None:
        envelope = OutboundMessageEnvelope(
            **self.BASE,
            body_text="Hi\n\n--\nUnsubscribe: https://x.test/u",
            extra_headers=unsubscribe_headers("https://x.test/u"),
        )
        msg = build_rfc5322_message(envelope)

        assert msg["List-Unsubscribe"] == "<https://x.test/u>"
        assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
        text = msg.get_body(preferencelist=("plain",)).get_content()
        assert "Unsubscribe: https://x.test/u" in text
        assert "HTML-capable" not in text

    def test_no_extra_headers_means_none_are_added(self) -> None:
        msg = build_rfc5322_message(OutboundMessageEnvelope(**self.BASE))
        assert msg["List-Unsubscribe"] is None

    @pytest.mark.parametrize("bad", ["<https://x.test/u>\r\nBcc: evil@x.test", "a\nb"])
    def test_a_header_value_with_a_newline_is_refused(self, bad: str) -> None:
        envelope = OutboundMessageEnvelope(
            **self.BASE, extra_headers=(("List-Unsubscribe", bad),)
        )
        with pytest.raises(AppError):
            build_rfc5322_message(envelope)


class TestCampaignPreflight:
    """Activation is blocked until a signed unsubscribe link can be built."""

    @staticmethod
    def _errors(defaults: dict, settings: Settings | None = None) -> list[str]:
        from unittest.mock import MagicMock, patch

        from app.api.deps import WorkspaceContext
        from app.modules.campaigns.preflight import PreflightService

        service = PreflightService.__new__(PreflightService)
        service.repo = MagicMock()
        service.repo.get_workspace_defaults.return_value = defaults
        context = WorkspaceContext(workspace_id=uuid4(), user_id=uuid4(), role_code="OWNER")
        errors: list = []
        with patch(
            "app.modules.campaigns.preflight.Settings.current",
            return_value=settings or Settings.current(),
        ):
            service._check_compliance(context, errors)
        return [e.code for e in errors]

    def test_ready_with_an_address_and_unsubscribe_configured(self) -> None:
        assert self._errors({"compliance": {"postal_address": "1 Main St"}}) == []

    def test_the_footer_settings_are_optional(self) -> None:
        assert self._errors({}) == []
        assert self._errors({"compliance": {"postal_address": "   "}}) == []

    def test_unconfigured_unsubscribe_blocks_activation(self) -> None:
        bare = Settings.current().model_copy(
            update={"unsubscribe_base_url": "", "unsubscribe_signing_key": ""}
        )
        codes = self._errors({"compliance": {"postal_address": "1 Main St"}}, bare)
        assert codes == ["unsubscribe_not_configured"]

    def test_unconfigured_unsubscribe_blocks_even_without_footer_settings(self) -> None:
        bare = Settings.current().model_copy(
            update={"unsubscribe_base_url": "", "unsubscribe_signing_key": ""}
        )
        assert self._errors({}, bare) == ["unsubscribe_not_configured"]


class TestSavingComplianceSettings:
    """Reading is forgiving, saving is strict."""

    def test_valid_values_are_trimmed_and_both_keys_returned(self) -> None:
        from app.modules.unsubscribe.compliance import validate_compliance_section

        assert validate_compliance_section({"postal_address": "  1 Main St  "}) == {
            "postal_address": "1 Main St",
            "footer_text": "",
        }

    @pytest.mark.parametrize(
        "bad",
        [
            "text",
            ["a"],
            None,
            {"postal_address": 5},
            {"postal_address": "a" * 501},
            {"footer_text": "b" * 301},
            {"postal_address": "ok", "logo_url": "https://x.test"},
        ],
    )
    def test_anything_else_is_a_422(self, bad: object) -> None:
        from app.modules.unsubscribe.compliance import validate_compliance_section

        with pytest.raises(AppError) as exc:
            validate_compliance_section(bad)
        assert exc.value.status_code == 422


class TestProductionStartupGuard:
    PROD = dict(
        app_env="production",
        database_url="postgresql://u:p@h/db",
        redis_url="redis://r:6379/0",
        supabase_url="https://p.supabase.co",
        backend_cors_origins="https://app.example.com",
    )

    def test_sending_in_production_without_unsubscribe_settings_does_not_start(self) -> None:
        # The test environment supplies both values, so "missing" is explicit.
        with pytest.raises(ValueError, match="UNSUBSCRIBE_BASE_URL"):
            Settings(  # type: ignore[arg-type]
                **self.PROD,
                sending_worker_enabled=True,
                unsubscribe_base_url="",
                unsubscribe_signing_key="",
            )
        with pytest.raises(ValueError, match="UNSUBSCRIBE_SIGNING_KEY"):
            Settings(  # type: ignore[arg-type]
                **self.PROD,
                sending_worker_enabled=True,
                unsubscribe_base_url="https://app.example.com",
                unsubscribe_signing_key="",
            )

    def test_it_starts_when_configured_or_when_not_sending(self) -> None:
        Settings(  # type: ignore[arg-type]
            **self.PROD,
            sending_worker_enabled=True,
            unsubscribe_base_url="https://app.example.com",
            unsubscribe_signing_key=KEY,
            platform_operator_emails="founder@corp.io",
        )
        Settings(**self.PROD, sending_worker_enabled=False)  # type: ignore[arg-type]
