"""Unit tests for email step content: allow-list sanitizer, pre-header
rendering/digests, and the "explicit content wins over template" rule."""

from __future__ import annotations

import hashlib
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from app.core.errors import AppError
from app.modules.campaigns.message_rendering import (
    compute_sequence_content_digest,
    render_step_content,
)
from app.modules.campaigns.preflight import _has_visible_content
from app.modules.campaigns.schemas import SequenceStepCreateIn, SequenceStepUpdateIn
from app.modules.campaigns.sequence_service import SequenceService
from app.modules.templates.sanitizer import sanitize_email_html
from app.modules.templates.variables import validate_template_content
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# Sanitizer
# ---------------------------------------------------------------------------


class TestEmailSanitizer:
    def test_keeps_editor_vocabulary_and_variables(self) -> None:
        html_in = (
            '<p style="text-align: center; color: #ff0000">Hi '
            "{{first_name|there}},</p><ul><li><strong>a</strong></li></ul>"
        )
        out = sanitize_email_html(html_in)
        assert "{{first_name|there}}" in out
        assert 'style="text-align: center; color: #ff0000"' in out
        assert "<ul><li><strong>a</strong></li></ul>" in out

    def test_drops_script_style_and_their_content(self) -> None:
        out = sanitize_email_html(
            "<p>ok</p><script>alert(1)</script><style>p{x:y}</style><p>after</p>"
        )
        assert "alert" not in out
        assert "p{x:y}" not in out
        assert "<p>ok</p>" in out and "<p>after</p>" in out

    def test_self_closed_script_does_not_swallow_the_rest(self) -> None:
        assert "after" in sanitize_email_html("<script/><p>after</p>")

    def test_void_tags_that_never_close_do_not_swallow_the_rest(self) -> None:
        out = sanitize_email_html('<meta charset="x"><input><p>kept</p>')
        assert "<p>kept</p>" in out

    def test_strips_event_handlers_and_unknown_attributes(self) -> None:
        out = sanitize_email_html('<p onclick="x()" class="c" id="i" data-x="1">t</p>')
        assert out == "<p>t</p>"

    @pytest.mark.parametrize(
        "href",
        [
            "javascript:alert(1)",
            " JaVaScRiPt:alert(1)",
            "data:text/html;base64,AAAA",
            "vbscript:x",
            "{{first_name}}",  # a variable must not be able to supply the scheme
            "/relative",
        ],
    )
    def test_unsafe_href_is_dropped(self, href: str) -> None:
        out = sanitize_email_html(f'<a href="{href}">x</a>')
        assert "href" not in out
        assert ">x</a>" in out

    def test_safe_hrefs_kept_and_variable_allowed_after_scheme(self) -> None:
        out = sanitize_email_html(
            '<a href="https://e.com/?n={{first_name|x}}">a</a>'
            '<a href="mailto:a@b.co">b</a>'
        )
        assert 'href="https://e.com/?n={{first_name|x}}"' in out
        assert 'href="mailto:a@b.co"' in out

    def test_img_requires_https_or_cid_source(self) -> None:
        assert "<img" not in sanitize_email_html('<img src="data:image/png;base64,AA">')
        assert "<img" not in sanitize_email_html('<img src="http://e.com/a.png">')
        assert 'src="cid:abc-123"' in sanitize_email_html('<img src="cid:abc-123">')
        assert 'src="https://e.com/a.png"' in sanitize_email_html(
            '<img src="https://e.com/a.png" alt="a" width="10" onerror="x()">'
        )

    def test_img_keeps_size_and_wrap_attributes_but_not_arbitrary_ones(self) -> None:
        out = sanitize_email_html(
            '<img src="cid:abc-123" width="240" align="left" hspace="8" '
            'vspace="999" onclick="x()">'
        )
        assert 'width="240"' in out and 'align="left"' in out and 'hspace="8"' in out
        assert "vspace" not in out and "onclick" not in out

    def test_style_property_allow_list(self) -> None:
        out = sanitize_email_html(
            '<span style="color:red;position:absolute;'
            "background:url(javascript:x);font-size:14px;"
            'font-family:Arial, sans-serif">t</span>'
        )
        assert "position" not in out and "url(" not in out
        assert "color: red" in out and "font-size: 14px" in out
        assert "font-family: Arial, sans-serif" in out

    def test_text_is_escaped_and_entities_not_double_escaped(self) -> None:
        html_in = "<p>a &amp; b &lt; c</p>"
        assert sanitize_email_html(html_in) == html_in

    def test_empty(self) -> None:
        assert sanitize_email_html("") == ""


# ---------------------------------------------------------------------------
# Pre-header rendering + digests
# ---------------------------------------------------------------------------


def _legacy_digest(subject: str, body: str, renderer_version: int = 1) -> str:
    return hashlib.sha256(
        f"{subject}\x00{body}\x00{renderer_version}".encode()
    ).hexdigest()


class TestPreheaderRendering:
    def test_no_preheader_is_byte_identical_to_legacy_render(self) -> None:
        rendered = render_step_content(
            subject="Hi {{first_name}}",
            body_html="<p>Yo {{first_name}}</p>",
            frozen_variables={"first_name": "Ada"},
            renderer_version=1,
        )
        assert rendered.body_html == "<p>Yo Ada</p>"
        assert rendered.content_digest == _legacy_digest("Hi Ada", "<p>Yo Ada</p>")

    def test_preheader_injected_hidden_first_and_digest_covers_it(self) -> None:
        plain = render_step_content(
            subject="s",
            body_html="<p>b</p>",
            frozen_variables={},
            renderer_version=1,
        )
        with_pre = render_step_content(
            subject="s",
            body_html="<p>b</p>",
            frozen_variables={},
            renderer_version=1,
            preheader="Quick note",
        )
        assert with_pre.body_html.startswith('<div style="display:none')
        assert "Quick note" in with_pre.body_html
        assert with_pre.body_html.endswith("<p>b</p>")
        assert with_pre.content_digest != plain.content_digest
        # The send gate recomputes the digest from the stored body, so the
        # pre-header is covered without any gate change.
        assert with_pre.content_digest == _legacy_digest("s", with_pre.body_html)

    def test_preheader_variables_resolved_and_html_escaped(self) -> None:
        rendered = render_step_content(
            subject="s",
            body_html="<p>b</p>",
            frozen_variables={"first_name": "<b>Ada</b>"},
            renderer_version=1,
            preheader="For {{first_name}} & co",
        )
        assert "&lt;b&gt;Ada&lt;/b&gt; &amp; co" in rendered.body_html
        assert "<b>Ada</b>" not in rendered.body_html

    def test_blank_preheader_is_ignored(self) -> None:
        rendered = render_step_content(
            subject="s",
            body_html="<p>b</p>",
            frozen_variables={},
            renderer_version=1,
            preheader="   ",
        )
        assert rendered.body_html == "<p>b</p>"


class TestSequenceDigest:
    STEPS = [
        {
            "position": 1,
            "kind": "EMAIL",
            "email_subject": "s",
            "email_body_html": "<p>b</p>",
            "wait_duration_minutes": None,
        },
        {
            "position": 2,
            "kind": "WAIT",
            "email_subject": None,
            "email_body_html": None,
            "wait_duration_minutes": 1440,
        },
    ]

    def test_missing_or_empty_preheader_hashes_like_legacy(self) -> None:
        legacy = compute_sequence_content_digest(self.STEPS)
        with_none = [{**self.STEPS[0], "email_preheader": None}, self.STEPS[1]]
        with_empty = [{**self.STEPS[0], "email_preheader": ""}, self.STEPS[1]]
        assert compute_sequence_content_digest(with_none) == legacy
        assert compute_sequence_content_digest(with_empty) == legacy

    def test_preheader_changes_digest(self) -> None:
        legacy = compute_sequence_content_digest(self.STEPS)
        changed = [{**self.STEPS[0], "email_preheader": "hello"}, self.STEPS[1]]
        assert compute_sequence_content_digest(changed) != legacy


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestPreheaderValidation:
    def test_unknown_variable_in_preheader_rejected(self) -> None:
        with pytest.raises(AppError) as exc:
            validate_template_content("s", "b", "Hi {{not_a_field}}")
        assert exc.value.status_code == 422

    def test_preheader_variables_join_schema(self) -> None:
        schema = validate_template_content("s", "b", "{{company}}")
        assert schema["standard"] == ["company"]

    def test_control_characters_and_length_rejected(self) -> None:
        with pytest.raises(AppError):
            validate_template_content("s", "b", "a\nb")
        with pytest.raises(AppError):
            validate_template_content("s", "b", "x" * 256)

    def test_wait_bounds_on_schemas(self) -> None:
        SequenceStepCreateIn(kind="WAIT", position=1, wait_duration_minutes=525_600)
        with pytest.raises(ValidationError):
            SequenceStepCreateIn(kind="WAIT", position=1, wait_duration_minutes=525_601)
        with pytest.raises(ValidationError):
            SequenceStepUpdateIn(expected_version=1, wait_duration_minutes=0)
        with pytest.raises(ValidationError):
            SequenceStepCreateIn(
                kind="EMAIL", position=1, email_subject="s", leading_wait_minutes=0
            )


class TestVisibleContent:
    @pytest.mark.parametrize(
        "body", [None, "", "   ", "<p></p>", "<p>&nbsp;</p>", "<p><br></p>"]
    )
    def test_empty_bodies(self, body: str | None) -> None:
        assert _has_visible_content(body) is False

    @pytest.mark.parametrize(
        "body", ["<p>hi</p>", "hi", '<img src="cid:a">', "<p>{{first_name}}</p>"]
    )
    def test_non_empty_bodies(self, body: str) -> None:
        assert _has_visible_content(body) is True


# ---------------------------------------------------------------------------
# Explicit content wins over template defaults
# ---------------------------------------------------------------------------


def _service_with_template(version: dict | None) -> SequenceService:
    service = SequenceService(MagicMock())
    service._get_template_version = MagicMock(return_value=version)  # type: ignore[method-assign]
    return service


TEMPLATE = {
    "id": uuid4(),
    "subject": "Template subject",
    "body_html": "<p>Template body</p>",
    "preheader": "Template pre",
    "variable_schema": {},
}


class TestResolveEmailContent:
    def test_template_supplies_defaults(self) -> None:
        service = _service_with_template(TEMPLATE)
        subject, body, pre, _ = service._resolve_email_content(
            MagicMock(),
            source_template_version_id=TEMPLATE["id"],
            email_subject=None,
            email_body_html=None,
        )
        assert (subject, body, pre) == (
            "Template subject",
            "<p>Template body</p>",
            "Template pre",
        )

    def test_explicit_fields_win_over_template(self) -> None:
        service = _service_with_template(TEMPLATE)
        subject, body, pre, _ = service._resolve_email_content(
            MagicMock(),
            source_template_version_id=TEMPLATE["id"],
            email_subject="Edited",
            email_body_html="<p>Edited body</p>",
            email_preheader="",
        )
        assert subject == "Edited"
        assert body == "<p>Edited body</p>"
        assert pre is None  # "" clears even when the template has one

    def test_missing_template_version_is_422(self) -> None:
        service = _service_with_template(None)
        with pytest.raises(AppError) as exc:
            service._resolve_email_content(
                MagicMock(),
                source_template_version_id=uuid4(),
                email_subject=None,
                email_body_html=None,
            )
        assert exc.value.status_code == 422

    def test_subject_required_without_template(self) -> None:
        service = _service_with_template(None)
        with pytest.raises(AppError):
            service._resolve_email_content(
                MagicMock(),
                source_template_version_id=None,
                email_subject="  ",
                email_body_html="<p>x</p>",
            )

    def test_body_is_sanitized_and_variables_validated(self) -> None:
        service = _service_with_template(None)
        _, body, _, schema = service._resolve_email_content(
            MagicMock(),
            source_template_version_id=None,
            email_subject="s",
            email_body_html=(
                '<p onclick="x()">{{first_name|there}}</p><script>x</script>'
            ),
        )
        assert body == "<p>{{first_name|there}}</p>"
        assert schema["standard"] == ["first_name"]
        with pytest.raises(AppError):
            service._resolve_email_content(
                MagicMock(),
                source_template_version_id=None,
                email_subject="s",
                email_body_html="<p>{{bogus}}</p>",
            )

    def test_legacy_body_kept_verbatim_when_not_sanitizing(self) -> None:
        service = _service_with_template(None)
        legacy = '<style>p{}</style><p class="x">hi</p>'
        _, body, _, _ = service._resolve_email_content(
            MagicMock(),
            source_template_version_id=None,
            email_subject="s",
            email_body_html=legacy,
            sanitize_body=False,
        )
        assert body == legacy
