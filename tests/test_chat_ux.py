import asyncio
import io
import os
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import bot
from bot import (
    ACTIVE_PROCS,
    JOB_CANCEL_REQUESTED,
    _terminate_process,
    active_job,
    agy_result_succeeded,
    artifact_snapshot,
    bot_commands,
    build_chat_args,
    clean_agy_output,
    chat_session_active,
    chat_session_marker,
    deliver_result,
    document_size_allowed,
    format_project_status,
    format_status_summary,
    keep_typing,
    mark_chat_session,
    new_artifacts,
    reset_chat_session,
    run,
    safe_text,
    send_artifacts,
    set_pending_confirmation,
    should_retry_fresh,
    split_message,
    telegram_html,
    telegram_html_chunks,
    utf16_plain_chunks,
    stop_active_job,
    take_pending_confirmation,
)


class CredentialUploadGuardTests(unittest.TestCase):
    def test_unknown_or_oversized_documents_are_rejected_before_download(self):
        self.assertFalse(document_size_allowed(None))
        self.assertTrue(document_size_allowed(1024 * 1024))
        self.assertFalse(document_size_allowed(1024 * 1024 + 1))


class TelegramCommandMenuTests(unittest.TestCase):
    def test_menu_exposes_friendly_unique_commands_with_descriptions(self):
        commands = bot_commands()
        names = [item.command for item in commands]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("ask", names)
        self.assertIn("new", names)
        self.assertIn("stop", names)
        self.assertIn("status", names)
        self.assertIn("project_status", names)
        self.assertIn("project_repair", names)
        self.assertTrue(all(item.description.strip() for item in commands))
        self.assertTrue(all(len(item.command) <= 32 for item in commands))
        self.assertTrue(all(len(item.description) <= 256 for item in commands))


class TelegramResultFormattingTests(unittest.TestCase):
    def _validate_html_payload(self, payload):
        import html as html_module
        token = re.compile(r"</?(?:b|code)>|&(?:amp|lt|gt);|.", re.S)
        self.assertEqual("".join(token.findall(payload)), payload)
        stack = []
        for item in token.findall(payload):
            if item in ("<b>", "<code>"):
                stack.append(item[1:-1])
            elif item.startswith("</"):
                self.assertTrue(stack)
                self.assertEqual(stack.pop(), item[2:-1])
        self.assertFalse(stack)
        self.assertLessEqual(len(payload.encode("utf-16-le")) // 2, 3500)
        return html_module.unescape(re.sub(r"</?(?:b|code)>", "", payload))

    def test_final_html_chunking_handles_entities_emoji_and_boundaries(self):
        for text in (
            "&" * 3800,
            "😀" * 3800,
            "<>&",
            "x" * 9000,
            "**" + ("a" * 3490) + "** tail",
            "### heading `code`\n" + ("- **item** & 😀\n" * 500),
        ):
            with self.subTest(prefix=text[:12]):
                chunks = telegram_html_chunks(text)
                self.assertGreaterEqual(len(chunks), 1)
                visible = "".join(self._validate_html_payload(chunk) for chunk in chunks)
                expected = re.sub(r"(?m)^#{1,3}\s+", "", text)
                expected = re.sub(r"(?m)^[-*]\s+", "• ", expected)
                expected = expected.replace("**", "").replace("`", "")
                self.assertEqual(visible, expected)

    def test_narration_filter_keeps_separate_and_concatenated_answers(self):
        separate = clean_agy_output(
            "I will inspect services.\nLet me check logs.\n### 结论\n- 服务正常"
        )
        self.assertNotIn("I will", separate)
        self.assertNotIn("Let me", separate)
        self.assertIn("### 结论", separate)
        concatenated = clean_agy_output(
            "Next, I will inspect.Now I will verify.我已完成检查。\n- 结果正常"
        )
        self.assertTrue(concatenated.startswith("我已完成检查"))
        self.assertNotIn("I will", concatenated)

    def test_narration_only_has_clear_chinese_fallback(self):
        self.assertEqual(
            clean_agy_output("I am going to inspect.\nI'm going to verify."),
            "反重力已完成检查，但没有生成可展示的结论。",
        )

    def test_telegram_html_escapes_raw_html_then_formats_subset(self):
        rendered = telegram_html("### <诊断>\n- **Gateway**：`active`")
        self.assertIn("<b>&lt;诊断&gt;</b>", rendered)
        self.assertIn("• <b>Gateway</b>：<code>active</code>", rendered)
        self.assertNotIn("<诊断>", rendered)

    def test_markdown_tokenizer_prevents_overlap_and_preserves_malformed_literals(self):
        self.assertEqual(telegram_html("`**x**`"), "<code>**x**</code>")
        self.assertEqual(
            telegram_html("### result `**literal**`"),
            "<b>result <code>**literal**</code></b>",
        )
        self.assertEqual(telegram_html("<b>raw</b>"), "&lt;b&gt;raw&lt;/b&gt;")
        self.assertEqual(telegram_html("unclosed **bold"), "unclosed **bold")
        self.assertEqual(
            telegram_html("**bold `code`**"),
            "<b>bold <code>code</code></b>",
        )

    def test_narration_filter_preserves_user_facing_results_and_recommendations(self):
        cases = {
            "I will inspect. Final answer: service is DOWN": "Final answer: service is DOWN",
            "I'll recommend disabling the broken service.": "I'll recommend disabling the broken service.",
            "Let me be clear: the conclusion is failure": "the conclusion is failure",
            "I will inspect. 最终答案：服务 DOWN": "最终答案：服务 DOWN",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertIn(expected, clean_agy_output(source))
        self.assertEqual(
            clean_agy_output("I will inspect.\nLet me check.\nI will verify."),
            "反重力已完成检查，但没有生成可展示的结论。",
        )

    def test_long_results_are_split_without_losing_the_beginning(self):
        text = "BEGIN\n" + ("一段较长的反重力结果\n" * 30) + "END"
        chunks = split_message(text, limit=120)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(chunks[0].startswith("BEGIN"))
        self.assertTrue(chunks[-1].endswith("END"))
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(chunk) <= 120 for chunk in chunks))

    def test_redacted_output_keeps_diagnosis_and_final_summary(self):
        text = "IMPORTANT DIAGNOSIS\n" + ("detail\n" * 100) + "FINAL SUMMARY"
        result = safe_text(text, limit=120)
        self.assertTrue(result.startswith("IMPORTANT DIAGNOSIS"))
        self.assertIn("输出过长", result)
        self.assertTrue(result.endswith("FINAL SUMMARY"))

    def test_default_output_budget_can_span_multiple_telegram_messages(self):
        text = "A" * 5000
        result = safe_text(text)
        self.assertEqual(result, text)
        self.assertGreater(len(split_message(result)), 1)

    def test_plain_chunks_bound_utf16_and_preserve_emoji(self):
        text = "😀" * 4000
        chunks = utf16_plain_chunks(text)
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(chunk.encode("utf-16-le")) // 2 <= 3500 for chunk in chunks))

    def test_plain_chunks_preserve_mixed_long_unbroken_text(self):
        text = ("BMP😀𐐷" * 1200) + ("x" * 5000)
        chunks = utf16_plain_chunks(text)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(chunk.encode("utf-16-le")) // 2 <= 3500 for chunk in chunks))


class TelegramResultDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_delivery_edits_notice_then_sends_remaining_chunks(self):
        class FakeBot:
            def __init__(self):
                self.sent = []

            async def send_message(self, *, chat_id, text, **kwargs):
                self.sent.append((chat_id, text, kwargs))

        class FakeNotice:
            chat_id = 42

            def __init__(self, bot):
                self.bot = bot
                self.edited = None
                self.edit_kwargs = None

            async def edit_text(self, text, **kwargs):
                self.edited = text
                self.edit_kwargs = kwargs

            def get_bot(self):
                return self.bot

        text = "BEGIN\n" + ("result line\n" * 500) + "END"
        bot = FakeBot()
        notice = FakeNotice(bot)
        await deliver_result(notice, text)
        delivered = notice.edited + "".join(chunk for _, chunk, _ in bot.sent)
        self.assertEqual(delivered, text)
        self.assertTrue(all(chat_id == 42 for chat_id, _, _ in bot.sent))
        self.assertTrue(notice.edit_kwargs.get("link_preview_options").is_disabled)
        self.assertTrue(
            all(kwargs.get("link_preview_options").is_disabled for _, _, kwargs in bot.sent)
        )

    async def test_delivery_uses_html_for_every_chunk(self):
        class FakeBot:
            def __init__(self): self.sent = []
            async def send_message(self, **kwargs): self.sent.append(kwargs)
        class FakeNotice:
            chat_id = 42
            def __init__(self): self.bot, self.kwargs = FakeBot(), {}
            async def edit_text(self, text, **kwargs): self.text, self.kwargs = text, kwargs
            def get_bot(self): return self.bot
        notice = FakeNotice()
        await deliver_result(notice, "### 开始\n" + ("- **项目** `正常`\n" * 500) + "结束")
        self.assertEqual(notice.kwargs["parse_mode"], "HTML")
        self.assertTrue(notice.kwargs["link_preview_options"].is_disabled)
        self.assertGreater(len(notice.bot.sent), 0)
        self.assertTrue(all(item["parse_mode"] == "HTML" for item in notice.bot.sent))
        self.assertTrue(all(item["link_preview_options"].is_disabled for item in notice.bot.sent))


class AgyPromptRulesTests(unittest.TestCase):
    def test_chat_prompt_contains_generic_mobile_and_artifact_rules(self):
        prompt = build_chat_args("create a report", continue_session=False)[-1]
        for phrase in (
            "concise Simplified Chinese", "Lead with the conclusion",
            "Do not narrate tool usage", "file://", "valid .docx",
            "Telegram Bot will detect and upload",
        ):
            self.assertIn(phrase, prompt)
        self.assertNotIn("/home/" + "ubuntu", prompt)
        self.assertNotIn("Noda" + "velle", prompt)


class ArtifactDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_cancellation_closes_all_artifacts_and_propagates(self):
        artifacts = [
            bot.PreparedArtifact(io.BytesIO(b"one"), "one.pdf"),
            bot.PreparedArtifact(io.BytesIO(b"two"), "two.pdf"),
        ]
        sender = mock.AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await bot.upload_prepared_artifacts(
                SimpleNamespace(send_document=sender), chat_id=7, artifacts=artifacts,
            )
        self.assertTrue(all(item.handle.closed for item in artifacts))

    async def test_upload_cancellation_between_items_closes_remaining_artifacts(self):
        artifacts = [
            bot.PreparedArtifact(io.BytesIO(str(index).encode()), f"{index}.pdf")
            for index in range(3)
        ]
        sender = mock.AsyncMock(side_effect=[None, asyncio.CancelledError()])
        with self.assertRaises(asyncio.CancelledError):
            await bot.upload_prepared_artifacts(
                SimpleNamespace(send_document=sender), chat_id=7, artifacts=artifacts,
            )
        self.assertEqual(sender.await_count, 2)
        self.assertTrue(all(item.handle.closed for item in artifacts))

    async def test_upload_success_and_failure_close_every_artifact(self):
        for side_effect, expected in ((None, (2, [])), ([RuntimeError("no"), None], (1, None))):
            artifacts = [
                bot.PreparedArtifact(io.BytesIO(b"x"), "one.pdf"),
                bot.PreparedArtifact(io.BytesIO(b"y"), "two.pdf"),
            ]
            sender = mock.AsyncMock(side_effect=side_effect)
            result = await bot.upload_prepared_artifacts(
                SimpleNamespace(send_document=sender), chat_id=7, artifacts=artifacts,
            )
            self.assertEqual(result[0], expected[0])
            if expected[1] == []:
                self.assertEqual(result[1], [])
            else:
                self.assertTrue(result[1])
            self.assertTrue(all(item.handle.closed for item in artifacts))

    def _file(self, root, name, data=b"x", mode=0o600):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)
        return path

    def test_snapshot_diff_supports_formats_and_caps_at_ten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); root.chmod(0o700)
            before = artifact_snapshot(root)
            for index in range(12):
                self._file(root, f"report-{index}.txt", str(index).encode())
            self._file(root, "ignored.py")
            after = artifact_snapshot(root)
            self.assertNotIn(Path("ignored.py"), after)
            self.assertEqual(len(new_artifacts(before, after)), 10)

    def test_supported_suffix_allowlist_and_45_mib_limit(self):
        expected = {
            ".doc", ".docx", ".rtf", ".odt", ".xls", ".xlsx", ".csv",
            ".ppt", ".pptx", ".pdf", ".txt", ".md", ".json", ".yaml",
            ".yml", ".xml", ".png", ".jpg", ".jpeg", ".webp", ".gif",
            ".mp3", ".m4a", ".ogg", ".wav", ".mp4", ".mov", ".webm",
            ".zip", ".tar", ".gz", ".7z",
        }
        self.assertEqual(bot.ARTIFACT_SUFFIXES, expected)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); root.chmod(0o700)
            oversized = self._file(root, "large.pdf")
            with oversized.open("r+b") as handle:
                handle.truncate(bot.MAX_ARTIFACT_BYTES + 1)
            self.assertNotIn(Path("large.pdf"), artifact_snapshot(root))

    def test_snapshot_filters_size_empty_links_permissions_and_nested_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); root.chmod(0o700)
            self._file(root, "ok.pdf", b"pdf")
            self._file(root, "empty.pdf", b"")
            self._file(root, "world.pdf", b"x", 0o666)
            real = self._file(root, "real.txt")
            os.link(real, root / "hard.txt")
            (root / "link.pdf").symlink_to(root / "ok.pdf")
            outside = Path(directory).parent / "artifact-outside-fixture"
            outside.mkdir(exist_ok=True)
            (root / "nested").symlink_to(outside, target_is_directory=True)
            try:
                snap = artifact_snapshot(root)
            finally:
                outside.rmdir()
            self.assertEqual(set(snap), {Path("ok.pdf")})

    def test_snapshot_rejects_cross_device_and_wrong_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); root.chmod(0o700)
            item = self._file(root, "item.pdf")
            real_lstat = Path.lstat
            def fake_lstat(path):
                info = real_lstat(path)
                if Path(path) == item:
                    values = list(info); values[2] += 1
                    return os.stat_result(values)
                return info
            with mock.patch.object(Path, "lstat", fake_lstat):
                self.assertEqual(artifact_snapshot(root), {})
            def wrong_owner(path):
                info = real_lstat(path)
                if Path(path) == item:
                    values = list(info); values[4] += 1
                    return os.stat_result(values)
                return info
            with mock.patch.object(Path, "lstat", wrong_owner):
                self.assertEqual(artifact_snapshot(root), {})

    def test_snapshot_budget_exhaustion_fails_closed_without_partial_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); root.chmod(0o700)
            self._file(root, "first.pdf")
            self._file(root, "second.pdf")
            with mock.patch.object(bot, "MAX_ARTIFACT_SCAN_ENTRIES", 1):
                with self.assertRaises(bot.ArtifactScanError):
                    artifact_snapshot(root)

    def test_snapshot_file_lstat_permission_error_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); root.chmod(0o700)
            item = self._file(root, "item.pdf")
            real_lstat = Path.lstat

            def denied(path):
                if Path(path) == item:
                    raise PermissionError("denied")
                return real_lstat(path)

            with mock.patch.object(Path, "lstat", denied):
                with self.assertRaises(bot.ArtifactScanError):
                    artifact_snapshot(root)

    def test_snapshot_child_directory_lstat_permission_error_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); root.chmod(0o700)
            child = root / "child"; child.mkdir(mode=0o700)
            real_lstat = Path.lstat

            def denied(path):
                if Path(path) == child:
                    raise PermissionError("denied")
                return real_lstat(path)

            with mock.patch.object(Path, "lstat", denied):
                with self.assertRaises(bot.ArtifactScanError):
                    artifact_snapshot(root)

    def test_snapshot_walk_error_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); root.chmod(0o700)

            def broken_walk(*args, **kwargs):
                kwargs["onerror"](PermissionError("scandir denied"))
                return iter(())

            with mock.patch("bot.os.walk", side_effect=broken_walk):
                with self.assertRaises(bot.ArtifactScanError):
                    artifact_snapshot(root)

    def test_snapshot_disappearance_races_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); root.chmod(0o700)
            item = self._file(root, "item.pdf")
            real_lstat = Path.lstat

            def vanished(path):
                if Path(path) == item:
                    raise FileNotFoundError("vanished")
                return real_lstat(path)

            with mock.patch.object(Path, "lstat", vanished):
                self.assertEqual(artifact_snapshot(root), {})

    async def test_native_upload_uses_open_descriptor_and_rejects_replacement(self):
        class FakeBot:
            def __init__(self): self.documents = []
            async def send_document(self, **kwargs):
                self.documents.append((kwargs["filename"], kwargs["caption"], kwargs["document"].read()))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); root.chmod(0o700)
            item = self._file(root, "report.pdf", b"trusted")
            fake = FakeBot()
            expected = artifact_snapshot(root)
            with mock.patch.object(bot, "CHAT_DIR", root), mock.patch.object(
                bot, "ARTIFACT_STAGING_DIR", root.parent / "bot-staging"
            ):
                sent, warnings = await send_artifacts(
                    fake, chat_id=7, paths=[Path("report.pdf")], expected=expected
                )
            self.assertEqual((sent, warnings), (1, []))
            self.assertEqual(fake.documents, [("report.pdf", "📎 report.pdf", b"trusted")])

            original_open = os.open
            def replacing_open(path, flags, *args, **kwargs):
                replacement = root / "replacement.tmp"
                replacement.write_bytes(b"replaced"); replacement.chmod(0o600)
                os.replace(replacement, item)
                return original_open(path, flags, *args, **kwargs)
            with mock.patch.object(bot, "CHAT_DIR", root), mock.patch.object(
                bot, "ARTIFACT_STAGING_DIR", root.parent / "bot-staging"
            ), mock.patch("bot.os.open", replacing_open):
                sent, warnings = await send_artifacts(
                    FakeBot(), chat_id=7, paths=[Path("report.pdf")], expected=expected
                )
            self.assertEqual(sent, 0)
            self.assertTrue(warnings)

    async def test_upload_uses_stable_copy_when_workspace_file_is_truncated(self):
        started = asyncio.Event()
        continue_reading = asyncio.Event()

        class DelayedBot:
            def __init__(self):
                self.payload = None

            async def send_document(self, **kwargs):
                started.set()
                await continue_reading.wait()
                self.payload = kwargs["document"].read()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); root.chmod(0o700)
            item = self._file(root, "report.pdf", b"verified-original")
            expected = artifact_snapshot(root)
            fake = DelayedBot()
            with mock.patch.object(bot, "CHAT_DIR", root), mock.patch.object(
                bot, "ARTIFACT_STAGING_DIR", root.parent / "bot-staging"
            ):
                upload = asyncio.create_task(send_artifacts(
                    fake, chat_id=7, paths=[Path("report.pdf")], expected=expected
                ))
                await started.wait()
                item.write_bytes(b"")
                continue_reading.set()
                sent, warnings = await upload

            self.assertEqual((sent, warnings), (1, []))
            self.assertEqual(fake.payload, b"verified-original")

    async def test_upload_uses_stable_copy_after_equal_length_workspace_overwrite(self):
        started = asyncio.Event()
        continue_reading = asyncio.Event()

        class DelayedBot:
            async def send_document(self, **kwargs):
                started.set()
                await continue_reading.wait()
                self.payload = kwargs["document"].read()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); root.chmod(0o700)
            item = self._file(root, "report.pdf", b"original-bytes")
            expected = artifact_snapshot(root)
            fake = DelayedBot()
            with mock.patch.object(bot, "CHAT_DIR", root), mock.patch.object(
                bot, "ARTIFACT_STAGING_DIR", root.parent / "bot-staging"
            ):
                upload = asyncio.create_task(send_artifacts(
                    fake, chat_id=7, paths=[Path("report.pdf")], expected=expected
                ))
                await started.wait()
                item.write_bytes(b"changed!-bytes")
                continue_reading.set()
                sent, warnings = await upload
            self.assertEqual((sent, warnings), (1, []))
            self.assertEqual(fake.payload, b"original-bytes")

    async def test_parent_rename_and_symlink_replacement_is_rejected(self):
        class FakeBot:
            def __init__(self): self.payloads = []
            async def send_document(self, **kwargs):
                self.payloads.append(kwargs["document"].read())

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "chat"; root.mkdir(mode=0o700)
            nested = root / "nested"; nested.mkdir(mode=0o700)
            item = self._file(nested, "report.pdf", b"internal")
            outside = base / "outside"; outside.mkdir(mode=0o700)
            self._file(outside, "report.pdf", b"external")
            expected = artifact_snapshot(root)
            original_open = os.open
            raced = False

            def racing_open(path, flags, *args, **kwargs):
                nonlocal raced
                fd = original_open(path, flags, *args, **kwargs)
                if path == "nested" and kwargs.get("dir_fd") is not None and not raced:
                    raced = True
                    nested.rename(root / "old-nested")
                    nested.symlink_to(outside, target_is_directory=True)
                return fd

            fake = FakeBot()
            with mock.patch.object(bot, "CHAT_DIR", root), mock.patch.object(
                bot, "ARTIFACT_STAGING_DIR", base / "bot-staging"
            ), mock.patch("bot.os.open", racing_open):
                sent, warnings = await send_artifacts(
                    fake, chat_id=7, paths=[Path("nested/report.pdf")], expected=expected
                )
            self.assertEqual(sent, 0)
            self.assertTrue(warnings)
            self.assertNotIn(b"external", fake.payloads)

    async def test_agent_job_sends_artifacts_only_after_success(self):
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=123),
            effective_chat=SimpleNamespace(id=42, send_action=mock.AsyncMock()),
            get_bot=mock.Mock(return_value=mock.AsyncMock()),
        )
        notice = mock.AsyncMock()
        for code, output, expected in ((0, "完成", 1), (1, "失败", 0)):
            with (
                mock.patch("bot.run", new=mock.AsyncMock(return_value=(code, output))),
                mock.patch("bot.artifact_snapshot", side_effect=[{}, {Path("result.pdf"): (1,)}] if code == 0 else [{}]),
                mock.patch("bot.prepare_artifacts", return_value=(
                    [bot.PreparedArtifact(io.BytesIO(b"x"), "result.pdf")], []
                )),
                mock.patch("bot.upload_prepared_artifacts", new=mock.AsyncMock(return_value=(1, []))) as sender,
                mock.patch("bot.deliver_result", new=mock.AsyncMock()),
            ):
                await bot.run_agent_job(
                    update, notice, ["agy"], timeout=1, cwd=bot.CHAT_DIR,
                    label="任务", artifact_workspace=True,
                )
            self.assertEqual(sender.await_count, expected)

    async def test_agent_job_keeps_chat_lock_until_artifact_copy_is_prepared(self):
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=123),
            effective_chat=SimpleNamespace(id=42, send_action=mock.AsyncMock()),
            get_bot=mock.Mock(return_value=mock.AsyncMock()),
        )
        notice = mock.AsyncMock()
        entered = threading.Event()
        release = threading.Event()

        def paused_prepare(paths, expected):
            entered.set()
            release.wait(timeout=2)
            return [], []

        with (
            mock.patch("bot.run", new=mock.AsyncMock(return_value=(0, "完成"))),
            mock.patch("bot.artifact_snapshot", side_effect=[{}, {Path("result.pdf"): (1,)}]),
            mock.patch("bot.prepare_artifacts", side_effect=paused_prepare),
            mock.patch("bot.deliver_result", new=mock.AsyncMock()),
            mock.patch.object(bot, "CHAT_LOCK", asyncio.Lock()),
        ):
            job = asyncio.create_task(bot.run_agent_job(
                update, notice, ["agy"], timeout=1, cwd=bot.CHAT_DIR,
                label="任务", artifact_workspace=True,
            ))
            await asyncio.to_thread(entered.wait, 2)
            waiter = asyncio.create_task(bot.CHAT_LOCK.acquire())
            await asyncio.sleep(0)
            self.assertFalse(waiter.done())
            release.set()
            await job
            await asyncio.wait_for(waiter, 1)
            bot.CHAT_LOCK.release()

    async def test_agent_job_cancellation_keeps_both_locks_until_freeze_cleanup(self):
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=123),
            effective_chat=SimpleNamespace(id=42, send_action=mock.AsyncMock()),
            get_bot=mock.Mock(return_value=mock.AsyncMock()),
        )
        entered = threading.Event(); release = threading.Event()
        artifacts = [bot.PreparedArtifact(io.BytesIO(b"x"), "x.pdf")]

        def paused_prepare(paths, expected):
            entered.set(); release.wait(timeout=2)
            return artifacts, []

        with (
            mock.patch("bot.run", new=mock.AsyncMock(return_value=(0, "完成"))),
            mock.patch("bot.artifact_snapshot", side_effect=[{}, {Path("x.pdf"): (1,)}]),
            mock.patch("bot.prepare_artifacts", side_effect=paused_prepare),
            mock.patch.object(bot, "CHAT_LOCK", asyncio.Lock()),
            mock.patch.object(bot, "MUTATION_LOCK", asyncio.Lock()),
        ):
            job = asyncio.create_task(bot.run_agent_job(
                update, mock.AsyncMock(), ["agy"], timeout=1, cwd=bot.CHAT_DIR,
                label="任务", mutation=True, artifact_workspace=True,
            ))
            await asyncio.to_thread(entered.wait, 2)
            job.cancel()
            chat_waiter = asyncio.create_task(bot.CHAT_LOCK.acquire())
            mutation_waiter = asyncio.create_task(bot.MUTATION_LOCK.acquire())
            await asyncio.sleep(0)
            self.assertFalse(chat_waiter.done())
            self.assertFalse(mutation_waiter.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await job
            self.assertTrue(artifacts[0].handle.closed)
            await asyncio.wait_for(asyncio.gather(chat_waiter, mutation_waiter), 1)
            bot.CHAT_LOCK.release(); bot.MUTATION_LOCK.release()

    async def test_chat_cancellation_keeps_lock_until_freeze_cleanup(self):
        entered = threading.Event(); release = threading.Event()
        artifacts = [bot.PreparedArtifact(io.BytesIO(b"x"), "x.pdf")]
        notice = mock.AsyncMock()
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=9124),
            effective_chat=SimpleNamespace(id=42, send_action=mock.AsyncMock()),
            effective_message=SimpleNamespace(reply_text=mock.AsyncMock(return_value=notice)),
            get_bot=mock.Mock(return_value=mock.AsyncMock()),
        )

        def paused_prepare(paths, expected):
            entered.set(); release.wait(timeout=2)
            return artifacts, []

        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.object(bot, "CHAT_DIR", Path(directory) / "chat"),
                mock.patch.object(bot, "CHAT_LOCK", asyncio.Lock()),
                mock.patch("bot.allowed", return_value=True),
                mock.patch("bot.chat_session_active", return_value=False),
                mock.patch("bot.run", new=mock.AsyncMock(return_value=(0, "完成"))),
                mock.patch("bot.artifact_snapshot", side_effect=[{}, {Path("x.pdf"): (1,)}]),
                mock.patch("bot.prepare_artifacts", side_effect=paused_prepare),
            ):
                job = asyncio.create_task(bot.run_chat_prompt(update, "fixture"))
                await asyncio.to_thread(entered.wait, 2)
                job.cancel()
                waiter = asyncio.create_task(bot.CHAT_LOCK.acquire())
                await asyncio.sleep(0)
                self.assertFalse(waiter.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await job
                self.assertTrue(artifacts[0].handle.closed)
                await asyncio.wait_for(waiter, 1)
                bot.CHAT_LOCK.release()

    async def test_html_delivery_failure_does_not_block_frozen_artifact_upload(self):
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=123),
            effective_chat=SimpleNamespace(id=42, send_action=mock.AsyncMock()),
            get_bot=mock.Mock(return_value=mock.AsyncMock()),
        )
        notice = mock.AsyncMock()
        notice.get_bot = mock.Mock(return_value=update.get_bot())
        notice.chat_id = 42
        frozen = bot.PreparedArtifact(io.BytesIO(b"frozen"), "result.pdf")
        with (
            mock.patch("bot.run", new=mock.AsyncMock(return_value=(0, "完成"))),
            mock.patch("bot.artifact_snapshot", side_effect=[{}, {Path("result.pdf"): (1,)}]),
            mock.patch("bot.prepare_artifacts", return_value=([frozen], [])),
            mock.patch("bot.deliver_result", new=mock.AsyncMock(side_effect=RuntimeError("HTML rejected"))),
            mock.patch("bot.upload_prepared_artifacts", new=mock.AsyncMock(return_value=(1, []))) as uploader,
        ):
            await bot.run_agent_job(
                update, notice, ["agy"], timeout=1, cwd=bot.CHAT_DIR,
                label="任务", artifact_workspace=True,
            )
        uploader.assert_awaited_once()

    async def test_agent_job_cancellation_during_delivery_closes_prepared_artifacts(self):
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=123),
            effective_chat=SimpleNamespace(id=42, send_action=mock.AsyncMock()),
            get_bot=mock.Mock(return_value=mock.AsyncMock()),
        )
        artifacts = [bot.PreparedArtifact(io.BytesIO(b"x"), "x.pdf"),
                     bot.PreparedArtifact(io.BytesIO(b"y"), "y.pdf")]
        with (
            mock.patch("bot.run", new=mock.AsyncMock(return_value=(0, "完成"))),
            mock.patch("bot.artifact_snapshot", side_effect=[{}, {Path("x.pdf"): (1,)}]),
            mock.patch("bot.prepare_artifacts", return_value=(artifacts, [])),
            mock.patch("bot.deliver_result", new=mock.AsyncMock(side_effect=asyncio.CancelledError())),
            mock.patch("bot.upload_prepared_artifacts", new=mock.AsyncMock()) as uploader,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await bot.run_agent_job(
                    update, mock.AsyncMock(), ["agy"], timeout=1, cwd=bot.CHAT_DIR,
                    label="任务", artifact_workspace=True,
                )
        uploader.assert_not_awaited()
        self.assertTrue(all(item.handle.closed for item in artifacts))

    async def test_html_rejection_uses_bounded_complete_plain_fallback(self):
        output = "😀" * 4000
        telegram_bot = mock.AsyncMock()
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=123),
            effective_chat=SimpleNamespace(id=42, send_action=mock.AsyncMock()),
            get_bot=mock.Mock(return_value=telegram_bot),
        )
        notice = mock.AsyncMock()
        notice.get_bot = mock.Mock(return_value=telegram_bot)
        notice.chat_id = 42
        with (
            mock.patch("bot.run", new=mock.AsyncMock(return_value=(0, output))),
            mock.patch("bot.artifact_snapshot", side_effect=[{}, {}]),
            mock.patch("bot.deliver_result", new=mock.AsyncMock(side_effect=RuntimeError("HTML rejected"))),
        ):
            await bot.run_agent_job(
                update, notice, ["agy"], timeout=1, cwd=bot.CHAT_DIR, label="任务",
                artifact_workspace=True,
            )
        payloads = [notice.edit_text.await_args.args[0]] + [
            call.kwargs["text"] for call in telegram_bot.send_message.await_args_list
        ]
        self.assertEqual("".join(payloads), f"✅ 任务完成\n\n{output}")
        self.assertTrue(all(len(item.encode("utf-16-le")) // 2 <= 3500 for item in payloads))
        self.assertTrue(all("parse_mode" not in call.kwargs for call in telegram_bot.send_message.await_args_list))

    async def test_plain_fallback_failure_still_uploads_artifacts(self):
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=123),
            effective_chat=SimpleNamespace(id=42, send_action=mock.AsyncMock()),
            get_bot=mock.Mock(return_value=mock.AsyncMock()),
        )
        artifact = bot.PreparedArtifact(io.BytesIO(b"x"), "x.pdf")
        notice = mock.AsyncMock()
        notice.edit_text.side_effect = RuntimeError("plain rejected")
        with (
            mock.patch("bot.run", new=mock.AsyncMock(return_value=(0, "完成"))),
            mock.patch("bot.artifact_snapshot", side_effect=[{}, {Path("x.pdf"): (1,)}]),
            mock.patch("bot.prepare_artifacts", return_value=([artifact], [])),
            mock.patch("bot.deliver_result", new=mock.AsyncMock(side_effect=RuntimeError("HTML rejected"))),
            mock.patch("bot.upload_prepared_artifacts", new=mock.AsyncMock(return_value=(1, []))) as uploader,
        ):
            await bot.run_agent_job(
                update, notice, ["agy"], timeout=1, cwd=bot.CHAT_DIR,
                label="任务", artifact_workspace=True,
            )
        uploader.assert_awaited_once()
        self.assertTrue(artifact.handle.closed)

    async def test_agent_followup_plain_chunk_failure_still_uploads_artifacts(self):
        telegram_bot = mock.AsyncMock()
        telegram_bot.send_message.side_effect = RuntimeError("later chunk rejected")
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=123),
            effective_chat=SimpleNamespace(id=42, send_action=mock.AsyncMock()),
            get_bot=mock.Mock(return_value=telegram_bot),
        )
        notice = mock.AsyncMock(); notice.get_bot = mock.Mock(return_value=telegram_bot); notice.chat_id = 42
        artifact = bot.PreparedArtifact(io.BytesIO(b"x"), "x.pdf")
        with (
            mock.patch("bot.run", new=mock.AsyncMock(return_value=(0, "😀" * 4000))),
            mock.patch("bot.artifact_snapshot", side_effect=[{}, {Path("x.pdf"): (1,)}]),
            mock.patch("bot.prepare_artifacts", return_value=([artifact], [])),
            mock.patch("bot.deliver_result", new=mock.AsyncMock(side_effect=RuntimeError("HTML rejected"))),
            mock.patch("bot.upload_prepared_artifacts", new=mock.AsyncMock(return_value=(1, []))) as uploader,
        ):
            await bot.run_agent_job(
                update, notice, ["agy"], timeout=1, cwd=bot.CHAT_DIR,
                label="任务", artifact_workspace=True,
            )
        uploader.assert_awaited_once()
        self.assertTrue(artifact.handle.closed)

    async def test_agent_followup_plain_chunk_cancellation_propagates_and_closes(self):
        telegram_bot = mock.AsyncMock()
        telegram_bot.send_message.side_effect = asyncio.CancelledError()
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=123),
            effective_chat=SimpleNamespace(id=42, send_action=mock.AsyncMock()),
            get_bot=mock.Mock(return_value=telegram_bot),
        )
        notice = mock.AsyncMock(); notice.get_bot = mock.Mock(return_value=telegram_bot); notice.chat_id = 42
        artifact = bot.PreparedArtifact(io.BytesIO(b"x"), "x.pdf")
        with (
            mock.patch("bot.run", new=mock.AsyncMock(return_value=(0, "😀" * 4000))),
            mock.patch("bot.artifact_snapshot", side_effect=[{}, {Path("x.pdf"): (1,)}]),
            mock.patch("bot.prepare_artifacts", return_value=([artifact], [])),
            mock.patch("bot.deliver_result", new=mock.AsyncMock(side_effect=RuntimeError("HTML rejected"))),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await bot.run_agent_job(
                    update, notice, ["agy"], timeout=1, cwd=bot.CHAT_DIR,
                    label="任务", artifact_workspace=True,
                )
        self.assertTrue(artifact.handle.closed)

    async def test_agent_scan_error_skips_freeze_and_upload(self):
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=123),
            effective_chat=SimpleNamespace(id=42, send_action=mock.AsyncMock()),
            get_bot=mock.Mock(return_value=mock.AsyncMock()),
        )
        with (
            mock.patch("bot.run", new=mock.AsyncMock(return_value=(0, "完成"))),
            mock.patch("bot.artifact_snapshot", side_effect=[{}, bot.ArtifactScanError("denied")]),
            mock.patch("bot.prepare_artifacts") as prepare,
            mock.patch("bot.upload_prepared_artifacts", new=mock.AsyncMock(return_value=(0, []))) as uploader,
            mock.patch("bot.deliver_result", new=mock.AsyncMock()),
        ):
            await bot.run_agent_job(
                update, mock.AsyncMock(), ["agy"], timeout=1, cwd=bot.CHAT_DIR,
                label="任务", artifact_workspace=True,
            )
        prepare.assert_not_called()
        uploader.assert_not_awaited()

    async def test_chat_cancellation_during_delivery_closes_prepared_artifacts(self):
        artifacts = [bot.PreparedArtifact(io.BytesIO(b"x"), "x.pdf"),
                     bot.PreparedArtifact(io.BytesIO(b"y"), "y.pdf")]
        notice = mock.AsyncMock()
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=9123),
            effective_chat=SimpleNamespace(id=42, send_action=mock.AsyncMock()),
            effective_message=SimpleNamespace(reply_text=mock.AsyncMock(return_value=notice)),
            get_bot=mock.Mock(return_value=mock.AsyncMock()),
        )
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.object(bot, "CHAT_DIR", Path(directory) / "chat"),
                mock.patch("bot.allowed", return_value=True),
                mock.patch("bot.chat_session_active", return_value=False),
                mock.patch("bot.run", new=mock.AsyncMock(return_value=(0, "完成"))),
                mock.patch("bot.artifact_snapshot", side_effect=[{}, {Path("x.pdf"): (1,)}]),
                mock.patch("bot.prepare_artifacts", return_value=(artifacts, [])),
                mock.patch("bot.deliver_result", new=mock.AsyncMock(side_effect=asyncio.CancelledError())),
                mock.patch("bot.upload_prepared_artifacts", new=mock.AsyncMock()) as uploader,
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await bot.run_chat_prompt(update, "fixture")
        uploader.assert_not_awaited()
        self.assertTrue(all(item.handle.closed for item in artifacts))

    async def test_chat_followup_plain_chunk_failure_still_uploads_artifacts(self):
        telegram_bot = mock.AsyncMock()
        telegram_bot.send_message.side_effect = RuntimeError("later chunk rejected")
        notice = mock.AsyncMock(); notice.get_bot = mock.Mock(return_value=telegram_bot); notice.chat_id = 42
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=9130),
            effective_chat=SimpleNamespace(id=42, send_action=mock.AsyncMock()),
            effective_message=SimpleNamespace(reply_text=mock.AsyncMock(return_value=notice)),
            get_bot=mock.Mock(return_value=telegram_bot),
        )
        artifact = bot.PreparedArtifact(io.BytesIO(b"x"), "x.pdf")
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.object(bot, "CHAT_DIR", Path(directory) / "chat"),
                mock.patch("bot.allowed", return_value=True),
                mock.patch("bot.chat_session_active", return_value=False),
                mock.patch("bot.run", new=mock.AsyncMock(return_value=(0, "😀" * 4000))),
                mock.patch("bot.artifact_snapshot", side_effect=[{}, {Path("x.pdf"): (1,)}]),
                mock.patch("bot.prepare_artifacts", return_value=([artifact], [])),
                mock.patch("bot.deliver_result", new=mock.AsyncMock(side_effect=RuntimeError("HTML rejected"))),
                mock.patch("bot.upload_prepared_artifacts", new=mock.AsyncMock(return_value=(1, []))) as uploader,
            ):
                await bot.run_chat_prompt(update, "fixture")
        uploader.assert_awaited_once()
        self.assertTrue(artifact.handle.closed)

    async def test_chat_followup_plain_chunk_cancellation_propagates_and_closes(self):
        telegram_bot = mock.AsyncMock()
        telegram_bot.send_message.side_effect = asyncio.CancelledError()
        notice = mock.AsyncMock(); notice.get_bot = mock.Mock(return_value=telegram_bot); notice.chat_id = 42
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=9131),
            effective_chat=SimpleNamespace(id=42, send_action=mock.AsyncMock()),
            effective_message=SimpleNamespace(reply_text=mock.AsyncMock(return_value=notice)),
            get_bot=mock.Mock(return_value=telegram_bot),
        )
        artifact = bot.PreparedArtifact(io.BytesIO(b"x"), "x.pdf")
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.object(bot, "CHAT_DIR", Path(directory) / "chat"),
                mock.patch("bot.allowed", return_value=True),
                mock.patch("bot.chat_session_active", return_value=False),
                mock.patch("bot.run", new=mock.AsyncMock(return_value=(0, "😀" * 4000))),
                mock.patch("bot.artifact_snapshot", side_effect=[{}, {Path("x.pdf"): (1,)}]),
                mock.patch("bot.prepare_artifacts", return_value=([artifact], [])),
                mock.patch("bot.deliver_result", new=mock.AsyncMock(side_effect=RuntimeError("HTML rejected"))),
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await bot.run_chat_prompt(update, "fixture")
        self.assertTrue(artifact.handle.closed)

    async def test_chat_scan_error_skips_freeze_and_upload(self):
        notice = mock.AsyncMock()
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=9132),
            effective_chat=SimpleNamespace(id=42, send_action=mock.AsyncMock()),
            effective_message=SimpleNamespace(reply_text=mock.AsyncMock(return_value=notice)),
            get_bot=mock.Mock(return_value=mock.AsyncMock()),
        )
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.object(bot, "CHAT_DIR", Path(directory) / "chat"),
                mock.patch("bot.allowed", return_value=True),
                mock.patch("bot.chat_session_active", return_value=False),
                mock.patch("bot.run", new=mock.AsyncMock(return_value=(0, "完成"))),
                mock.patch("bot.artifact_snapshot", side_effect=[{}, bot.ArtifactScanError("denied")]),
                mock.patch("bot.prepare_artifacts") as prepare,
                mock.patch("bot.upload_prepared_artifacts", new=mock.AsyncMock(return_value=(0, []))) as uploader,
                mock.patch("bot.deliver_result", new=mock.AsyncMock()),
            ):
                await bot.run_chat_prompt(update, "fixture")
        prepare.assert_not_called()
        uploader.assert_not_awaited()


class ProgressFeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_typing_indicator_repeats_until_cancelled(self):
        class FakeChat:
            def __init__(self):
                self.actions = []

            async def send_action(self, action):
                self.actions.append(action)

        chat = FakeChat()
        task = asyncio.create_task(keep_typing(chat, interval=0.01))
        await asyncio.sleep(0.035)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertGreaterEqual(len(chat.actions), 2)


class FriendlyStatusTests(unittest.TestCase):
    def test_status_is_mobile_friendly_and_actionable(self):
        summary = format_status_summary(
            gateway=(0, "active"),
            cpa=(0, "active"),
            agy=(0, "1.1.3"),
            account_count=6,
            model_count=12,
            session_active=True,
        )
        self.assertIn("✅ Hermes Gateway：运行中", summary)
        self.assertIn("✅ 救援 CPA：运行中", summary)
        self.assertIn("✅ Antigravity：1.1.3", summary)
        self.assertIn("6 个账号 / 12 个模型", summary)
        self.assertIn("当前对话：已建立", summary)
        self.assertNotIn("$ ", summary)

    def test_failed_gateway_status_suggests_the_recovery_action(self):
        summary = format_status_summary(
            gateway=(3, "inactive"),
            cpa=(0, "active"),
            agy=(0, "1.1.3"),
            account_count=0,
            model_count=None,
            session_active=False,
        )
        self.assertIn("❌ Hermes Gateway：未运行", summary)
        self.assertIn("/restart", summary)

    def test_status_bounds_and_redacts_untrusted_command_output(self):
        token = "123456789:" + "C" * 35
        summary = format_status_summary(
            gateway=(0, "active"),
            cpa=(0, "active"),
            agy=(0, "v1 " + token + ("x" * 5000)),
            account_count=1,
            model_count=1,
            session_active=False,
        )
        self.assertNotIn(token, summary)
        self.assertLess(len(summary), 1200)

    def test_project_status_bounds_untrusted_command_output(self):
        token = "123456789:" + "D" * 35
        summary = format_project_status(
            service=(0, "active"),
            version=(0, token + ("z" * 5000)),
            account_count=2,
            model_count=4,
            repo_changes=1,
        )
        self.assertNotIn(token, summary)
        self.assertLess(len(summary), 800)
        self.assertIn("accounts=2", summary)


class ConfirmationFlowTests(unittest.TestCase):
    def test_pending_confirmation_is_bound_to_nonce_user_chat_and_expiry(self):
        user_id = 24680
        nonce = set_pending_confirmation(
            user_id,
            "restart",
            "",
            chat_id=99,
            nonce="test-nonce",
            now=100.0,
            ttl=10.0,
        )
        self.assertEqual(nonce, "test-nonce")
        self.assertIsNone(
            take_pending_confirmation(nonce, user_id=111, chat_id=99, now=105.0)
        )
        self.assertEqual(
            take_pending_confirmation(nonce, user_id=user_id, chat_id=99, now=105.0),
            ("restart", ""),
        )
        self.assertIsNone(
            take_pending_confirmation(nonce, user_id=user_id, chat_id=99, now=106.0)
        )

        expired = set_pending_confirmation(
            user_id,
            "project_repair",
            "修复服务",
            chat_id=99,
            nonce="expired-nonce",
            now=200.0,
            ttl=10.0,
        )
        self.assertIsNone(
            take_pending_confirmation(expired, user_id=user_id, chat_id=99, now=211.0)
        )


class BackgroundLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        bot.ACTIVE_PROCS.clear()
        bot.JOB_STARTING.clear()
        bot.JOB_CANCEL_REQUESTED.clear()
        bot.SPAWN_REAPERS.clear()
        bot.SPAWN_TASKS.clear()
        bot.CREDENTIAL_IMPORT_TASKS.clear()

    async def test_shutdown_terminates_processes_and_waits_for_reapers(self):
        proc = mock.Mock()
        bot.ACTIVE_PROCS[1] = proc
        bot.JOB_STARTING.add(3)
        bot.JOB_CANCEL_REQUESTED.add(4)
        reaper_finished = asyncio.Event()

        async def reaper():
            await asyncio.sleep(0.02)
            reaper_finished.set()

        reaper_task = asyncio.create_task(reaper())
        bot.SPAWN_REAPERS.add(reaper_task)
        with mock.patch("bot._terminate_process", new=mock.AsyncMock()) as terminate_proc:
            await bot.shutdown_bot(None)

        terminate_proc.assert_awaited_once_with(proc)
        self.assertTrue(reaper_finished.is_set())
        self.assertFalse(bot.ACTIVE_PROCS)
        self.assertFalse(bot.JOB_STARTING)
        self.assertFalse(bot.JOB_CANCEL_REQUESTED)
        self.assertFalse(bot.SPAWN_REAPERS)

    async def test_shutdown_reaps_pending_spawn_and_clears_all_registries(self):
        proc = mock.Mock()
        spawn = asyncio.get_running_loop().create_future()
        bot.SPAWN_TASKS.add(spawn)
        bot.CREDENTIAL_IMPORT_TASKS.add(asyncio.create_task(asyncio.sleep(0)))
        spawn.set_result(proc)
        with mock.patch("bot._terminate_process", new=mock.AsyncMock()) as terminate:
            await asyncio.wait_for(bot.shutdown_bot(None), timeout=2)
        terminate.assert_awaited_once_with(proc)
        self.assertFalse(bot.SPAWN_TASKS)
        self.assertFalse(bot.SPAWN_REAPERS)
        self.assertFalse(bot.CREDENTIAL_IMPORT_TASKS)

    async def test_reaper_done_callback_consumes_exception(self):
        class FakeTask:
            def __init__(self):
                self.exception_calls = 0

            def exception(self):
                self.exception_calls += 1
                return RuntimeError("reaper failed")

        task = FakeTask()
        bot.SPAWN_REAPERS.add(task)
        with self.assertLogs(bot.log, level="ERROR"):
            bot._finish_spawn_reaper(task)
        self.assertEqual(task.exception_calls, 1)
        self.assertNotIn(task, bot.SPAWN_REAPERS)


class ActiveJobControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        bot.ACTIVE_PROCS.clear()
        bot.JOB_STARTING.clear()
        bot.JOB_CANCEL_REQUESTED.clear()
        bot.SPAWN_TASKS.clear()
        bot.SPAWN_REAPERS.clear()

    async def test_repeated_cancellation_keeps_ownership_until_termination_finishes(self):
        user_id = 12352
        entered = asyncio.Event()
        release = asyncio.Event()

        async def gated_terminate(proc):
            entered.set()
            await release.wait()

        task = asyncio.create_task(
            run([sys.executable, "-c", "import time; time.sleep(30)"], owner_id=user_id)
        )
        for _ in range(100):
            if user_id in ACTIVE_PROCS:
                break
            await asyncio.sleep(0)
        proc = ACTIVE_PROCS[user_id]
        with mock.patch("bot._terminate_process", new=gated_terminate):
            task.cancel()
            await asyncio.wait_for(entered.wait(), timeout=2)
            task.cancel()
            await asyncio.sleep(0)
            self.assertIs(ACTIVE_PROCS.get(user_id), proc)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=2)
        await _terminate_process(proc)
        self.assertNotIn(user_id, ACTIVE_PROCS)

    async def test_successful_leader_with_devnull_child_is_cleaned_before_return(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "child.pid"
            leader = (
                "import pathlib,subprocess,sys\n"
                "d=open('/dev/null','wb')\n"
                "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],"
                "stdin=subprocess.DEVNULL,stdout=d,stderr=d)\n"
                f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid))\n"
            )
            code, _ = await asyncio.wait_for(
                run([sys.executable, "-c", leader], timeout=15), timeout=20
            )
            self.assertEqual(code, 0)
            child_pid = int(pid_file.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)
    async def test_running_job_can_be_stopped_without_waiting_for_timeout(self):
        user_id = 12345
        task = asyncio.create_task(
            run(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                timeout=60,
                owner_id=user_id,
            )
        )
        for _ in range(50):
            if active_job(user_id):
                break
            await asyncio.sleep(0.02)
        self.assertTrue(active_job(user_id))
        self.assertTrue(await stop_active_job(user_id))
        code, output = await asyncio.wait_for(task, timeout=5)
        self.assertEqual(code, 130)
        self.assertIn("停止", output)
        self.assertFalse(active_job(user_id))

    async def test_stop_kills_descendants_after_process_group_leader_exits(self):
        user_id = 12351
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "child.pid"
            child_code = "import time; time.sleep(30)"
            leader_code = (
                "import pathlib,subprocess,sys\n"
                f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}])\n"
                f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid))\n"
            )
            task = asyncio.create_task(
                run([sys.executable, "-c", leader_code], timeout=60, owner_id=user_id)
            )
            for _ in range(200):
                proc = ACTIVE_PROCS.get(user_id)
                if pid_file.exists() and proc is not None and proc.returncode is not None:
                    break
                await asyncio.sleep(0.01)
            proc = ACTIVE_PROCS.get(user_id)
            self.assertIsNotNone(proc)
            self.assertIsNotNone(proc.returncode)
            child_pid = int(pid_file.read_text())
            self.assertTrue(await stop_active_job(user_id))
            code, output = await asyncio.wait_for(task, timeout=5)
            self.assertEqual(code, 130)
            self.assertIn("停止", output)
            for _ in range(100):
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.01)
            else:
                self.fail("descendant process survived /stop")

    async def test_graceful_sigterm_exit_zero_is_still_reported_as_stopped(self):
        user_id = 12350
        script = (
            "import signal,time,sys\n"
            "def stop(signum, frame):\n"
            " print('Error: failed to send message: no active conversation', flush=True)\n"
            " raise SystemExit(0)\n"
            "signal.signal(signal.SIGTERM, stop)\n"
            "print('ready', flush=True)\n"
            "time.sleep(30)\n"
        )
        task = asyncio.create_task(
            run([sys.executable, "-c", script], timeout=60, owner_id=user_id)
        )
        for _ in range(50):
            if user_id in ACTIVE_PROCS:
                break
            await asyncio.sleep(0.02)
        self.assertIn(user_id, ACTIVE_PROCS)
        await asyncio.sleep(0.05)
        self.assertTrue(await stop_active_job(user_id))
        code, output = await asyncio.wait_for(task, timeout=5)
        self.assertEqual(code, 130)
        self.assertIn("停止", output)
        self.assertFalse(active_job(user_id))

    async def test_cancelling_run_terminates_child_and_clears_registry(self):
        user_id = 12346
        task = asyncio.create_task(
            run(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                timeout=60,
                owner_id=user_id,
            )
        )
        for _ in range(50):
            if user_id in ACTIVE_PROCS:
                break
            await asyncio.sleep(0.02)
        self.assertIn(user_id, ACTIVE_PROCS)
        proc = ACTIVE_PROCS[user_id]
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(proc.wait(), timeout=5)
        self.assertIsNotNone(proc.returncode)
        self.assertFalse(active_job(user_id))

    async def test_external_cancel_after_spawn_before_handle_registration_reaps_child(self):
        user_id = 12349
        original_spawn = asyncio.create_subprocess_exec
        spawned = asyncio.Event()
        release = asyncio.Event()
        holder = {}

        async def spawn_then_block(*args, **kwargs):
            proc = await original_spawn(*args, **kwargs)
            holder["proc"] = proc
            spawned.set()
            await release.wait()
            return proc

        with mock.patch("bot.asyncio.create_subprocess_exec", new=spawn_then_block):
            task = asyncio.create_task(
                run(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    timeout=60,
                    owner_id=user_id,
                )
            )
            await asyncio.wait_for(spawned.wait(), timeout=2)
            proc = holder["proc"]
            try:
                task.cancel()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                await asyncio.wait_for(proc.wait(), timeout=2)
                self.assertIsNotNone(proc.returncode)
                self.assertFalse(active_job(user_id))
            finally:
                if proc.returncode is None:
                    await _terminate_process(proc)

    async def test_stop_during_spawn_cancels_job_before_it_runs(self):
        user_id = 12347
        original_spawn = asyncio.create_subprocess_exec
        entered = asyncio.Event()
        release = asyncio.Event()

        async def delayed_spawn(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original_spawn(*args, **kwargs)

        with mock.patch("bot.asyncio.create_subprocess_exec", new=delayed_spawn):
            task = asyncio.create_task(
                run(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    timeout=60,
                    owner_id=user_id,
                )
            )
            await asyncio.wait_for(entered.wait(), timeout=2)
            self.assertTrue(active_job(user_id))
            self.assertTrue(await stop_active_job(user_id))
            release.set()
            code, output = await asyncio.wait_for(task, timeout=5)
        self.assertEqual(code, 130)
        self.assertIn("停止", output)
        self.assertFalse(active_job(user_id))

    async def test_stop_during_failed_spawn_does_not_leave_cancel_state(self):
        user_id = 12348
        entered = asyncio.Event()
        release = asyncio.Event()

        async def failed_spawn(*args, **kwargs):
            entered.set()
            await release.wait()
            raise OSError("spawn failed")

        with mock.patch("bot.asyncio.create_subprocess_exec", new=failed_spawn):
            task = asyncio.create_task(
                run(["missing-command"], timeout=10, owner_id=user_id)
            )
            await asyncio.wait_for(entered.wait(), timeout=2)
            self.assertTrue(await stop_active_job(user_id))
            release.set()
            code, output = await asyncio.wait_for(task, timeout=2)
        self.assertEqual(code, 130)
        self.assertIn("停止", output)
        self.assertNotIn(user_id, JOB_CANCEL_REQUESTED)
        self.assertFalse(active_job(user_id))

    async def test_subprocess_output_is_bounded_but_keeps_head_and_tail(self):
        code, output = await run(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('HEAD\\n'+'x'*2000000+'\\nTAIL')",
            ],
            timeout=30,
        )
        self.assertEqual(code, 0)
        self.assertLessEqual(len(output), 14000)
        self.assertTrue(output.startswith("HEAD"))
        self.assertTrue(output.endswith("TAIL"))

    async def test_failure_diagnostic_in_omitted_middle_is_preserved_for_classification(self):
        code, output = await run(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('H'*1000+'\\nError: timed out waiting for response\\n'+'T'*1000)",
            ],
            timeout=30,
        )
        self.assertEqual(code, 0)
        self.assertFalse(agy_result_succeeded(code, output))

    async def test_generic_fatal_in_omitted_middle_is_preserved_for_classification(self):
        code, output = await run(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('H'*1000+'\\nFatal: internal failure\\n'+'T'*1000)",
            ],
            timeout=30,
        )
        self.assertEqual(code, 0)
        self.assertFalse(agy_result_succeeded(code, output))

    async def test_terminate_allows_graceful_cleanup_before_sigkill(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "cleaned"
            script = (
                "import pathlib,signal,time,sys\n"
                "def cleanup(signum, frame):\n"
                "    time.sleep(0.4)\n"
                "    pathlib.Path(sys.argv[1]).write_text('done')\n"
                "    raise SystemExit(0)\n"
                "signal.signal(signal.SIGTERM, cleanup)\n"
                "print('READY', flush=True)\n"
                "time.sleep(30)\n"
            )
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                script,
                str(marker),
                stdout=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            self.assertIsNotNone(proc.stdout)
            stdout = proc.stdout
            assert stdout is not None
            self.assertEqual(
                await asyncio.wait_for(stdout.readline(), timeout=2),
                b"READY\n",
            )
            await _terminate_process(proc)
            await asyncio.wait_for(proc.wait(), timeout=5)
            self.assertEqual(marker.read_text(), "done")


class NewChatConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        if bot.CHAT_LOCK.locked():
            bot.CHAT_LOCK.release()

    async def test_new_without_prompt_waits_for_old_chat_before_reset(self):
        message = SimpleNamespace(text="/new", reply_text=mock.AsyncMock())
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=7001),
            effective_message=message,
        )
        await bot.CHAT_LOCK.acquire()

        with (
            mock.patch("bot.allowed", return_value=True),
            mock.patch("bot.stop_active_job", new=mock.AsyncMock(return_value=True)),
            mock.patch("bot.reset_chat_session") as reset,
        ):
            task = asyncio.create_task(bot.new_chat(update, SimpleNamespace(args=[])))
            await asyncio.sleep(0)
            reset.assert_not_called()
            bot.CHAT_LOCK.release()
            await asyncio.wait_for(task, timeout=2)

        reset.assert_called_once_with(7001)
        message.reply_text.assert_awaited_once()

    async def test_stop_during_progress_reply_prevents_old_chat_spawn(self):
        user_id = 7003
        entered = asyncio.Event()
        release = asyncio.Event()
        notice = SimpleNamespace(edit_text=mock.AsyncMock())

        async def gated_reply(*args, **kwargs):
            entered.set()
            await release.wait()
            return notice

        message = SimpleNamespace(reply_text=mock.AsyncMock(side_effect=gated_reply))
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=user_id),
            effective_message=message,
            effective_chat=SimpleNamespace(),
        )
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch("bot.CHAT_DIR", Path(directory) / "chat"),
                mock.patch("bot.allowed", return_value=True),
                mock.patch("bot.chat_session_active", return_value=False),
                mock.patch("bot.run", new=mock.AsyncMock()) as run_mock,
            ):
                task = asyncio.create_task(bot.run_chat_prompt(update, "fixture"))
                await asyncio.wait_for(entered.wait(), timeout=2)
                self.assertTrue(await bot.stop_active_job(user_id))
                release.set()
                await asyncio.wait_for(task, timeout=2)
        run_mock.assert_not_awaited()
        notice.edit_text.assert_awaited_once()
        self.assertNotIn(user_id, bot.CHAT_PRESPAWN)

    async def test_new_with_prompt_delegates_atomic_reset_to_waiting_chat_path(self):
        message = SimpleNamespace(text="/new 请检查状态", reply_text=mock.AsyncMock())
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=7002),
            effective_message=message,
        )

        with (
            mock.patch("bot.allowed", return_value=True),
            mock.patch("bot.stop_active_job", new=mock.AsyncMock(return_value=True)),
            mock.patch("bot.run_chat_prompt", new=mock.AsyncMock()) as run_prompt,
        ):
            await bot.new_chat(update, SimpleNamespace(args=["请检查状态"]))

        run_prompt.assert_awaited_once_with(
            update,
            "请检查状态",
            force_new=True,
            reset_session=True,
            wait_for_lock=True,
        )


class AntigravityChatArgsTests(unittest.TestCase):
    def test_new_chat_does_not_continue_old_conversation(self):
        args = build_chat_args("你好", continue_session=False)
        self.assertNotIn("--continue", args)
        self.assertEqual(args[-2], "-p")
        self.assertTrue(args[-1].startswith("你好\n"))

    def test_follow_up_chat_continues_recent_conversation(self):
        args = build_chat_args("继续", continue_session=True)
        self.assertIn("--continue", args)
        self.assertEqual(args[-2], "-p")
        self.assertTrue(args[-1].startswith("继续\n"))

    def test_session_marker_can_be_created_and_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            user_id = 42
            self.assertFalse(chat_session_active(user_id, root=root))
            mark_chat_session(user_id, root=root)
            self.assertTrue(chat_session_active(user_id, root=root))
            reset_chat_session(user_id, root=root)
            self.assertFalse(chat_session_active(user_id, root=root))

    def test_session_marker_symlink_is_replaced_without_touching_target(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "markers"
            root.mkdir()
            target = base / "victim"
            target.write_text("unchanged", encoding="utf-8")
            marker = chat_session_marker(42, root=root)
            marker.symlink_to(target)

            mark_chat_session(42, root=root)

            self.assertEqual(target.read_text(encoding="utf-8"), "unchanged")
            self.assertFalse(marker.is_symlink())
            self.assertEqual(marker.read_text(encoding="utf-8"), "active\n")

    def test_exit_zero_known_failure_output_is_not_success(self):
        for output in (
            "",
            "Error: timed out waiting for response",
            "Error: failed to send message: no active conversation",
            'Warning: conversation "missing" not found.',
            'jetski: no output produced — tool permission was auto-denied.',
        ):
            self.assertFalse(agy_result_succeeded(0, output), output)
        self.assertTrue(agy_result_succeeded(0, "正常回答"))
        self.assertFalse(agy_result_succeeded(1, "正常回答"))

    def test_only_broken_continuation_retries_as_a_fresh_chat(self):
        self.assertTrue(should_retry_fresh(1, "Conversation not found; cannot continue"))
        self.assertFalse(
            should_retry_fresh(0, 'Warning: conversation "missing" not found.')
        )
        self.assertFalse(
            should_retry_fresh(0, "Error: failed to send message: no active conversation")
        )
        self.assertFalse(should_retry_fresh(1, "401 invalid authentication token"))
        self.assertFalse(
            should_retry_fresh(1, "401 invalid authentication token; cannot continue")
        )
        self.assertFalse(should_retry_fresh(429, "quota exhausted"))
        self.assertFalse(
            should_retry_fresh(429, "quota exhausted; unable to continue")
        )
        self.assertFalse(should_retry_fresh(130, "任务已停止"))


if __name__ == "__main__":
    unittest.main()
