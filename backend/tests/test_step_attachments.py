"""Attachments and inline images: upload validation, the attachment service
against a fake storage, digests, MIME/Graph payloads, and how the send worker
handles missing or corrupt files. No network, no database, no email."""

from __future__ import annotations

import base64
import hashlib
import io
import uuid
import zipfile
from datetime import UTC, datetime
from email import message_from_bytes, policy
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from app.api.deps import WorkspaceContext
from app.core.errors import AppError
from app.modules.campaigns.attachment_service import (
    SIGNED_URL_TTL_SECONDS,
    StepAttachmentService,
)
from app.modules.campaigns.attachments import (
    MAX_ATTACHMENTS,
    MAX_FILE_BYTES,
    MAX_INLINE_IMAGES,
    extract_cid_references,
    sanitize_filename,
    storage_safe_name,
    validate_upload,
)
from app.modules.campaigns.message_rendering import compute_sequence_content_digest
from app.modules.imports.storage import (
    StorageBucketMissingError,
    StorageError,
    StorageObjectMissingError,
    StorageUnavailableError,
    SupabaseStorageClient,
)
from app.modules.mailboxes.providers.base import (
    EnvelopeAttachment,
    OutboundMessageEnvelope,
)
from app.modules.mailboxes.providers.message_builder import build_rfc5322_message
from app.modules.mailboxes.providers.microsoft import MicrosoftGraphProvider
from app.modules.sending.service import SendingService, _CredentialFailure

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 32
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 16
PDF = b"%PDF-1.7\n%test\n"


def _ooxml(prefix: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(f"{prefix}document.xml", "<x/>")
    return buffer.getvalue()


class TestValidateUpload:
    @pytest.mark.parametrize(
        ("name", "data", "content_type"),
        [
            ("a.png", PNG, "image/png"),
            ("a.JPG", JPEG, "image/jpeg"),
            ("a.jpeg", JPEG, "image/jpeg"),
            ("a.gif", GIF, "image/gif"),
            ("a.webp", WEBP, "image/webp"),
            ("a.pdf", PDF, "application/pdf"),
            ("notes.txt", "héllo".encode(), "text/plain"),
            ("data.csv", b"a,b\n1,2\n", "text/csv"),
            (
                "d.docx",
                _ooxml("word/"),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
            (
                "s.xlsx",
                _ooxml("xl/"),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
            (
                "p.pptx",
                _ooxml("ppt/"),
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ),
        ],
    )
    def test_accepts_allowed_types_and_sets_the_type_from_the_extension(
        self, name: str, data: bytes, content_type: str
    ) -> None:
        result = validate_upload(filename=name, data=data, disposition="ATTACHMENT")
        assert result.content_type == content_type
        assert result.sha256 == hashlib.sha256(data).hexdigest()
        assert result.size_bytes == len(data)

    @pytest.mark.parametrize(
        "name",
        ["x.svg", "x.html", "x.exe", "x.zip", "x.js", "x.docm", "x", "x.png.exe", ""],
    )
    def test_rejects_types_outside_the_allow_list(self, name: str) -> None:
        with pytest.raises(AppError) as exc:
            validate_upload(filename=name, data=PNG, disposition="ATTACHMENT")
        assert exc.value.code == "attachment_type_not_allowed"

    @pytest.mark.parametrize(
        ("name", "data"),
        [
            ("fake.png", b"<script>alert(1)</script>"),
            ("fake.jpg", PNG),
            ("fake.pdf", PNG),
            ("fake.txt", b"has\x00nul"),
            ("fake.txt", b"\xff\xfe\xfa"),
            ("fake.docx", b"PK\x03\x04garbage"),
            ("fake.docx", _ooxml("xl/")),  # a spreadsheet renamed to .docx
            ("fake.xlsx", PDF),
        ],
    )
    def test_rejects_contents_that_do_not_match_the_extension(
        self, name: str, data: bytes
    ) -> None:
        with pytest.raises(AppError) as exc:
            validate_upload(filename=name, data=data, disposition="ATTACHMENT")
        assert exc.value.code == "attachment_type_mismatch"

    def test_size_limits(self) -> None:
        with pytest.raises(AppError) as exc:
            validate_upload(filename="a.pdf", data=b"", disposition="ATTACHMENT")
        assert exc.value.status_code == 422
        big = PDF + b"0" * MAX_FILE_BYTES
        with pytest.raises(AppError) as exc:
            validate_upload(filename="a.pdf", data=big, disposition="ATTACHMENT")
        assert exc.value.code == "attachment_too_large"
        ok = PDF + b"0" * (MAX_FILE_BYTES - len(PDF))
        assert (
            validate_upload(
                filename="a.pdf", data=ok, disposition="ATTACHMENT"
            ).size_bytes
            == MAX_FILE_BYTES
        )

    def test_only_images_can_be_inline(self) -> None:
        assert validate_upload(
            filename="a.png", data=PNG, disposition="INLINE"
        ).is_image
        with pytest.raises(AppError):
            validate_upload(filename="a.pdf", data=PDF, disposition="INLINE")


class TestFilenames:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("report.pdf", "report.pdf"),
            ("../../etc/passwd", "passwd"),
            ("C:\\Users\\me\\a.pdf", "a.pdf"),
            ("a\r\nb.pdf", "a__b.pdf"),
            ("  .hidden.pdf ", "hidden.pdf"),
            (None, "attachment"),
            ("///", "attachment"),
        ],
    )
    def test_sanitize_filename(self, raw: str | None, expected: str) -> None:
        assert sanitize_filename(raw) == expected

    def test_long_names_keep_their_extension(self) -> None:
        name = sanitize_filename("a" * 400 + ".pdf")
        assert len(name) == 255 and name.endswith(".pdf")

    def test_storage_key_fragment_is_ascii_and_path_free(self) -> None:
        fragment = storage_safe_name("Résumé final/../v2 (1).pdf")
        assert "/" not in fragment and ".." not in fragment
        assert fragment.isascii() and len(fragment) <= 80


class TestCidReferences:
    def test_extracts_image_content_ids(self) -> None:
        body = '<p><img src="cid:abc123XY" alt=""><img src=\'cid:zzz-1\'></p>'
        assert extract_cid_references(body) == {"abc123XY", "zzz-1"}

    def test_ignores_other_sources(self) -> None:
        assert extract_cid_references('<img src="https://e.com/a.png">') == set()
        assert extract_cid_references(None) == set()


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []
        self.fail_upload: Exception | None = None

    def upload_object(self, key: str, *, content_type: str, data: bytes):
        if self.fail_upload:
            raise self.fail_upload
        self.objects[key] = data

    def download_object(self, key: str) -> bytes:
        if key not in self.objects:
            raise StorageObjectMissingError("missing")
        return self.objects[key]

    def delete_object(self, key: str) -> None:
        self.deleted.append(key)
        self.objects.pop(key, None)

    def create_signed_url(self, key: str, *, expires_in: int = 300) -> str:
        return f"https://storage.example/sign/{key}?exp={expires_in}"


WS, USER, CAMPAIGN, STEP, SEQUENCE = (uuid.uuid4() for _ in range(5))


def _context() -> WorkspaceContext:
    ctx = MagicMock(spec=WorkspaceContext)
    ctx.workspace_id, ctx.user_id = WS, USER
    return ctx


def _row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": uuid.uuid4(),
        "step_id": STEP,
        "campaign_id": CAMPAIGN,
        "sequence_id": SEQUENCE,
        "filename": "a.pdf",
        "content_type": "application/pdf",
        "size_bytes": len(PDF),
        "sha256": hashlib.sha256(PDF).hexdigest(),
        "disposition": "ATTACHMENT",
        "content_id": "tok" + uuid.uuid4().hex[:8],
        "storage_key": f"{WS}/{CAMPAIGN}/{STEP}/k-a.pdf",
        "created_at": datetime.now(UTC),
    }
    row.update(overrides)
    return row


def _service(
    *,
    status: str = "DRAFT",
    existing: list[dict] | None = None,
    step_kind: str = "EMAIL",
) -> tuple[StepAttachmentService, FakeStorage, MagicMock]:
    storage = FakeStorage()
    service = StepAttachmentService(MagicMock(), storage_factory=lambda: storage)
    repo = MagicMock()
    repo.get_campaign.return_value = {"id": CAMPAIGN, "status": status}
    repo.get_step.return_value = {
        "id": STEP,
        "campaign_id": CAMPAIGN,
        "sequence_id": SEQUENCE,
        "kind": step_kind,
    }
    repo.list_step_attachments.return_value = existing or []
    repo.insert_attachment.side_effect = lambda **kw: _row(
        **{
            k: v
            for k, v in kw.items()
            if k
            in {
                "filename",
                "content_type",
                "size_bytes",
                "sha256",
                "disposition",
                "content_id",
                "storage_key",
            }
        }
    )
    repo.count_storage_key_references.return_value = 0
    service.repo = repo
    return service, storage, repo


class TestUploadService:
    def _upload(self, service, *, name="a.pdf", data=PDF, disposition="ATTACHMENT"):
        return service.upload(
            _context(),
            CAMPAIGN,
            STEP,
            filename=name,
            data=data,
            disposition=disposition,
        )

    def test_stores_the_file_privately_and_records_its_digest(self) -> None:
        service, storage, repo = _service()
        attachment, created = self._upload(service)
        assert created is True
        assert attachment.filename == "a.pdf"
        (key,) = storage.objects
        assert key.startswith(f"{WS}/{CAMPAIGN}/{STEP}/")
        kwargs = repo.insert_attachment.call_args.kwargs
        assert kwargs["workspace_id"] == WS and kwargs["step_id"] == STEP
        assert kwargs["sha256"] == hashlib.sha256(PDF).hexdigest()
        assert kwargs["created_by"] == USER
        assert len(kwargs["content_id"]) >= 8

    def test_content_ids_are_unique_per_upload(self) -> None:
        service, _, repo = _service()
        self._upload(service)
        self._upload(service, data=PDF + b"more")
        ids = [c.kwargs["content_id"] for c in repo.insert_attachment.call_args_list]
        assert len(set(ids)) == 2

    def test_same_file_twice_is_a_no_op(self) -> None:
        existing = _row()
        service, storage, repo = _service(existing=[existing])
        attachment, created = self._upload(service)
        assert created is False and attachment.id == existing["id"]
        assert storage.objects == {}
        repo.insert_attachment.assert_not_called()

    def test_lost_race_keeps_the_winner_and_removes_our_object(self) -> None:
        winner = _row()
        service, storage, repo = _service()
        repo.insert_attachment.side_effect = None
        repo.insert_attachment.return_value = None
        repo.find_attachment_by_content.return_value = winner
        attachment, created = self._upload(service)
        assert created is False and attachment.id == winner["id"]
        assert storage.objects == {} and len(storage.deleted) == 1

    def test_only_a_draft_campaign_can_change(self) -> None:
        service, storage, _ = _service(status="RUNNING")
        with pytest.raises(AppError) as exc:
            self._upload(service)
        assert exc.value.code == "state_conflict" and exc.value.status_code == 409
        assert storage.objects == {}

    def test_unknown_campaign_or_foreign_step_is_404(self) -> None:
        service, _, repo = _service()
        repo.get_campaign.return_value = None
        with pytest.raises(AppError) as exc:
            self._upload(service)
        assert exc.value.status_code == 404
        service, _, repo = _service()
        repo.get_step.return_value = {
            "id": STEP,
            "campaign_id": uuid.uuid4(),
            "sequence_id": SEQUENCE,
            "kind": "EMAIL",
        }
        with pytest.raises(AppError) as exc:
            self._upload(service)
        assert exc.value.status_code == 404

    def test_wait_steps_cannot_have_files(self) -> None:
        service, _, _ = _service(step_kind="WAIT")
        with pytest.raises(AppError) as exc:
            self._upload(service)
        assert exc.value.status_code == 422

    def test_invalid_files_never_reach_storage(self) -> None:
        service, storage, _ = _service()
        with pytest.raises(AppError):
            self._upload(service, name="x.exe", data=b"MZ")
        with pytest.raises(AppError):
            self._upload(service, name="x.png", data=b"not a png")
        assert storage.objects == {}

    def test_attachment_count_limit(self) -> None:
        existing = [
            _row(sha256=f"{i:064x}", content_id=f"cid{i:08d}")
            for i in range(MAX_ATTACHMENTS)
        ]
        service, storage, _ = _service(existing=existing)
        with pytest.raises(AppError) as exc:
            self._upload(service)
        assert exc.value.code == "attachment_limit"
        assert storage.objects == {}

    def test_inline_image_count_limit(self) -> None:
        existing = [
            _row(
                disposition="INLINE",
                content_type="image/png",
                filename="i.png",
                sha256=f"{i:064x}",
                content_id=f"img{i:08d}",
                size_bytes=10,
            )
            for i in range(MAX_INLINE_IMAGES)
        ]
        service, _, _ = _service(existing=existing)
        with pytest.raises(AppError) as exc:
            self._upload(service, name="n.png", data=PNG, disposition="INLINE")
        assert exc.value.code == "attachment_limit"

    def test_total_size_limit(self) -> None:
        existing = [_row(size_bytes=MAX_FILE_BYTES - 5, sha256="e" * 64)]
        service, storage, _ = _service(existing=existing)
        with pytest.raises(AppError) as exc:
            self._upload(service, data=PDF + b"x" * 100)
        assert exc.value.code == "attachment_limit"
        assert storage.objects == {}

    def test_storage_outage_is_a_clean_503(self) -> None:
        service, storage, repo = _service()
        storage.fail_upload = StorageUnavailableError("down")
        with pytest.raises(AppError) as exc:
            self._upload(service)
        assert exc.value.status_code == 503
        repo.insert_attachment.assert_not_called()

    def test_missing_bucket_says_so_and_is_logged(self, caplog) -> None:
        service, storage, repo = _service()
        storage.fail_upload = StorageBucketMissingError(
            "bucket not found (secret-detail)"
        )
        with caplog.at_level("WARNING"):
            with pytest.raises(AppError) as exc:
                self._upload(service)
        assert exc.value.code == "attachment_storage_not_configured"
        assert "secret-detail" not in exc.value.message
        assert "secret-detail" in caplog.text
        repo.insert_attachment.assert_not_called()

    def test_supabase_bucket_not_found_response_is_classified(self) -> None:
        # Supabase answers an upload to a missing bucket with HTTP 400, not 404.
        response = httpx.Response(
            400,
            json={
                "statusCode": "404",
                "error": "Bucket not found",
                "message": "Bucket not found",
            },
        )
        with pytest.raises(StorageBucketMissingError):
            SupabaseStorageClient(MagicMock(), bucket="x")._raise_for_status(
                response, action="upload"
            )

    def test_other_storage_rejection_keeps_the_body_for_the_log(self) -> None:
        response = httpx.Response(400, text="mime type image/png is not supported")
        with pytest.raises(StorageError, match="mime type image/png"):
            SupabaseStorageClient(MagicMock(), bucket="x")._raise_for_status(
                response, action="upload"
            )

    def test_invalid_disposition_is_rejected(self) -> None:
        service, _, _ = _service()
        with pytest.raises(AppError):
            self._upload(service, disposition="EVIL")


class TestDeleteAndPreview:
    def test_delete_removes_the_row_and_the_unreferenced_object(self) -> None:
        row = _row()
        service, storage, repo = _service()
        repo.get_attachment.return_value = row
        storage.objects[row["storage_key"]] = PDF
        service.delete(_context(), CAMPAIGN, STEP, row["id"])
        repo.delete_attachment.assert_called_once_with(
            workspace_id=WS, attachment_id=row["id"]
        )
        assert storage.deleted == [row["storage_key"]]

    def test_delete_keeps_an_object_still_used_by_a_copy(self) -> None:
        row = _row()
        service, storage, repo = _service()
        repo.get_attachment.return_value = row
        repo.count_storage_key_references.return_value = 1
        service.delete(_context(), CAMPAIGN, STEP, row["id"])
        assert storage.deleted == []

    def test_delete_unknown_attachment_is_404(self) -> None:
        service, _, repo = _service()
        repo.get_attachment.return_value = None
        with pytest.raises(AppError) as exc:
            service.delete(_context(), CAMPAIGN, STEP, uuid.uuid4())
        assert exc.value.status_code == 404

    def test_delete_is_blocked_once_the_campaign_is_active(self) -> None:
        service, _, repo = _service(status="RUNNING")
        with pytest.raises(AppError) as exc:
            service.delete(_context(), CAMPAIGN, STEP, uuid.uuid4())
        assert exc.value.status_code == 409
        repo.delete_attachment.assert_not_called()

    def test_signed_url_is_short_lived(self) -> None:
        row = _row()
        service, _, repo = _service()
        repo.get_attachment.return_value = row
        out = service.signed_url(_context(), CAMPAIGN, STEP, row["id"])
        assert out.expires_in == SIGNED_URL_TTL_SECONDS

    def test_signed_url_is_scoped_to_this_step(self) -> None:
        service, _, repo = _service()
        repo.get_attachment.return_value = None
        with pytest.raises(AppError) as exc:
            service.signed_url(_context(), CAMPAIGN, STEP, uuid.uuid4())
        assert exc.value.status_code == 404
        repo.get_attachment.assert_called_once()
        assert repo.get_attachment.call_args.kwargs["step_id"] == STEP


class TestSequenceDigest:
    STEPS = [
        {
            "position": 1,
            "kind": "EMAIL",
            "email_subject": "s",
            "email_body_html": "<p>b</p>",
            "wait_duration_minutes": None,
        }
    ]

    def test_no_attachments_hashes_like_before(self) -> None:
        base = compute_sequence_content_digest(self.STEPS)
        assert (
            compute_sequence_content_digest([{**self.STEPS[0], "attachments": []}])
            == base
        )
        assert (
            compute_sequence_content_digest([{**self.STEPS[0], "attachments": None}])
            == base
        )

    def test_files_are_part_of_the_frozen_identity(self) -> None:
        base = compute_sequence_content_digest(self.STEPS)
        a = {"content_id": "aaaaaaaa", "sha256": "1" * 64, "disposition": "ATTACHMENT"}
        b = {"content_id": "bbbbbbbb", "sha256": "2" * 64, "disposition": "INLINE"}
        with_a = compute_sequence_content_digest(
            [{**self.STEPS[0], "attachments": [a]}]
        )
        assert with_a != base
        both = compute_sequence_content_digest(
            [{**self.STEPS[0], "attachments": [a, b]}]
        )
        assert both != with_a
        # Order of the rows must not matter.
        assert both == compute_sequence_content_digest(
            [{**self.STEPS[0], "attachments": [b, a]}]
        )
        swapped = {**a, "sha256": "3" * 64}
        assert (
            compute_sequence_content_digest(
                [{**self.STEPS[0], "attachments": [swapped]}]
            )
            != with_a
        )


def _parse(env: OutboundMessageEnvelope):
    return message_from_bytes(
        build_rfc5322_message(env).as_bytes(), policy=policy.default
    )


class TestMime:
    def _env(self, *attachments: EnvelopeAttachment, html: str = "<p>Hi</p>"):
        return OutboundMessageEnvelope(
            to_address="a@b.co",
            from_address="s@x.co",
            subject="S",
            body_html=html,
            rfc_message_id="m1@x.co",
            attachments=attachments,
        )

    def test_no_attachments_is_unchanged(self) -> None:
        msg = _parse(self._env())
        assert msg.get_content_type() == "multipart/alternative"

    def test_attachment_is_multipart_mixed(self) -> None:
        msg = _parse(self._env(EnvelopeAttachment("doc.pdf", "application/pdf", PDF)))
        assert msg.get_content_type() == "multipart/mixed"
        (attachment,) = list(msg.iter_attachments())
        assert attachment.get_filename() == "doc.pdf"
        assert attachment.get_content_type() == "application/pdf"
        assert attachment.get_content() == PDF

    def test_inline_image_is_related_to_the_html_with_a_content_id(self) -> None:
        html = '<p>Hi <img src="cid:abc12345"></p>'
        msg = _parse(
            self._env(
                EnvelopeAttachment("logo.png", "image/png", PNG, "abc12345"), html=html
            )
        )
        related = next(
            p for p in msg.walk() if p.get_content_type() == "multipart/related"
        )
        parts = list(related.iter_parts())
        assert parts[0].get_content_type() == "text/html"
        assert parts[1]["Content-ID"] == "<abc12345>"
        assert parts[1].get_content_disposition() == "inline"
        assert parts[1].get_content() == PNG

    def test_inline_and_regular_together(self) -> None:
        msg = _parse(
            self._env(
                EnvelopeAttachment("logo.png", "image/png", PNG, "abc12345"),
                EnvelopeAttachment("doc.pdf", "application/pdf", PDF),
            )
        )
        assert msg.get_content_type() == "multipart/mixed"
        assert [p.get_filename() for p in msg.iter_attachments()] == ["doc.pdf"]
        assert any(p.get_content_type() == "multipart/related" for p in msg.walk())


class TestGraphPayload:
    def test_attachments_are_sent_as_file_attachments(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            if request.url.path.endswith("/send"):
                return httpx.Response(202)
            # The draft creation call carries the message itself.
            captured["body"] = {"message": json.loads(request.content)}
            return httpx.Response(201, json={"id": "d-1"})

        provider = MicrosoftGraphProvider()
        provider._get_client = lambda: httpx.Client(  # type: ignore[method-assign]
            transport=httpx.MockTransport(handler)
        )
        result = provider.send_message(
            {"access_token": "t"},
            OutboundMessageEnvelope(
                to_address="a@b.co",
                from_address="s@x.co",
                subject="S",
                body_html='<img src="cid:abc12345">',
                attachments=(
                    EnvelopeAttachment("logo.png", "image/png", PNG, "abc12345"),
                    EnvelopeAttachment("doc.pdf", "application/pdf", PDF),
                ),
            ),
        )
        assert result.status == "ACCEPTED"
        attachments = captured["body"]["message"]["attachments"]
        assert attachments[0]["isInline"] is True
        assert attachments[0]["contentId"] == "abc12345"
        assert base64.b64decode(attachments[0]["contentBytes"]) == PNG
        assert attachments[1]["isInline"] is False and "contentId" not in attachments[1]
        assert attachments[1]["name"] == "doc.pdf"

    def test_no_attachments_key_when_there_are_none(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            if request.url.path.endswith("/send"):
                return httpx.Response(202)
            captured["body"] = {"message": json.loads(request.content)}
            return httpx.Response(201, json={"id": "d-1"})

        provider = MicrosoftGraphProvider()
        provider._get_client = lambda: httpx.Client(  # type: ignore[method-assign]
            transport=httpx.MockTransport(handler)
        )
        provider.send_message(
            {"access_token": "t"},
            OutboundMessageEnvelope(
                to_address="a@b.co", from_address="s@x.co", subject="S"
            ),
        )
        assert "attachments" not in captured["body"]["message"]

    def test_header_injection_in_a_filename_is_refused(self) -> None:
        provider = MicrosoftGraphProvider()
        with pytest.raises(AppError):
            provider.send_message(
                {"access_token": "t"},
                OutboundMessageEnvelope(
                    to_address="a@b.co",
                    from_address="s@x.co",
                    subject="S",
                    attachments=(
                        EnvelopeAttachment("a\r\nBcc: x@y.co", "text/plain", b"x"),
                    ),
                ),
            )


def _sending_service(storage: FakeStorage | None, rows: list[dict]) -> SendingService:
    service = SendingService(
        MagicMock(),
        settings=MagicMock(),
        rate_limiter=MagicMock(),
        storage_factory=(lambda: storage) if storage is not None else None,
    )
    service.repository = MagicMock()
    service.repository.list_step_attachments.return_value = rows
    return service


def _ctx(**raw: Any) -> MagicMock:
    ctx = MagicMock()
    ctx.purpose = raw.pop("purpose", "CAMPAIGN")
    ctx.workspace_id = WS
    ctx.raw = {"step_id": STEP, **raw}
    return ctx


class TestSendWorkerAttachments:
    def test_downloads_verifies_and_builds_envelope_attachments(self) -> None:
        pdf = _row()
        img = _row(
            disposition="INLINE",
            content_type="image/png",
            filename="i.png",
            size_bytes=len(PNG),
            sha256=hashlib.sha256(PNG).hexdigest(),
            content_id="cidtoken1",
            storage_key="k/i.png",
        )
        storage = FakeStorage()
        storage.objects[pdf["storage_key"]] = PDF
        storage.objects["k/i.png"] = PNG
        loaded = _sending_service(storage, [pdf, img])._load_attachments(_ctx())
        assert [(a.filename, a.content_id) for a in loaded] == [
            ("a.pdf", None),
            ("i.png", "cidtoken1"),
        ]
        assert loaded[0].data == PDF and loaded[1].data == PNG

    def test_no_step_or_test_message_means_no_attachments(self) -> None:
        service = _sending_service(FakeStorage(), [_row()])
        assert service._load_attachments(_ctx(purpose="CONTROLLED_TEST")) == ()
        ctx = _ctx()
        ctx.raw = {}
        assert service._load_attachments(ctx) == ()

    def test_no_rows_needs_no_storage_at_all(self) -> None:
        def boom():
            raise AssertionError("storage must not be touched")

        service = SendingService(
            MagicMock(),
            settings=MagicMock(),
            rate_limiter=MagicMock(),
            storage_factory=boom,
        )
        service.repository = MagicMock()
        service.repository.list_step_attachments.return_value = []
        assert service._load_attachments(_ctx()) == ()

    def test_missing_object_fails_the_message_without_sending(self) -> None:
        with pytest.raises(_CredentialFailure) as exc:
            _sending_service(FakeStorage(), [_row()])._load_attachments(_ctx())
        assert exc.value.error_code == "attachment_missing"
        assert exc.value.message_status == "FAILED"

    def test_tampered_object_fails_the_message(self) -> None:
        row = _row()
        storage = FakeStorage()
        storage.objects[row["storage_key"]] = PDF + b"tampered"
        with pytest.raises(_CredentialFailure) as exc:
            _sending_service(storage, [row])._load_attachments(_ctx())
        assert exc.value.error_code == "attachment_corrupt"
        assert exc.value.message_status == "FAILED"

    def test_same_size_but_different_bytes_is_still_corrupt(self) -> None:
        row = _row()
        storage = FakeStorage()
        storage.objects[row["storage_key"]] = b"%PDF-1.7\n%TEST\n"  # same length
        assert len(storage.objects[row["storage_key"]]) == row["size_bytes"]
        with pytest.raises(_CredentialFailure) as exc:
            _sending_service(storage, [row])._load_attachments(_ctx())
        assert exc.value.error_code == "attachment_corrupt"

    def test_storage_outage_retries_instead_of_failing(self) -> None:
        class Down(FakeStorage):
            def download_object(self, key: str) -> bytes:
                raise StorageUnavailableError("503")

        with pytest.raises(_CredentialFailure) as exc:
            _sending_service(Down(), [_row()])._load_attachments(_ctx())
        assert exc.value.message_status == "RETRY_SCHEDULED"
        assert exc.value.hold_reason == "attachment_storage_unavailable"

    def test_the_read_transaction_is_closed_before_downloading(self) -> None:
        row = _row()
        storage = FakeStorage()
        storage.objects[row["storage_key"]] = PDF
        service = _sending_service(storage, [row])
        order: list[str] = []
        service.session.commit.side_effect = lambda: order.append("commit")
        original = storage.download_object
        storage.download_object = lambda k: (order.append("download"), original(k))[1]  # type: ignore[method-assign]
        service._load_attachments(_ctx())
        assert order == ["commit", "download"]
