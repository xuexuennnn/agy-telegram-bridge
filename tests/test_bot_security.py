import asyncio
import inspect
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import bot
from bot import (
    build_agy_sandbox_args,
    build_diagnostic_sandbox_args,
    build_safe_git_status_args,
    confirmation_callback,
    document,
    project,
    project_guard_prompt,
    safe_text,
    sanitized_child_env,
)


class OutputRedactionTests(unittest.TestCase):
    def test_redacts_complete_quoted_secret_containing_spaces(self):
        text = 'client_secret="harmless fixture words only" trailing=visible'
        redacted = safe_text(text)
        self.assertNotIn("harmless fixture words only", redacted)
        self.assertIn("trailing=visible", redacted)

    def test_model_ids_and_global_errors_cannot_emit_control_or_quoted_secret(self):
        with self.assertRaises(ValueError):
            bot.parse_cpa_model_payload(b'{"data":[{"id":"model\\nsecret"}]}')

class GlobalErrorLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_exception_log_omits_control_and_multiword_fixture(self):
        fixture = '"harmless multi word fixture"\nforged-log-line'
        context = SimpleNamespace(error=RuntimeError(fixture))
        with self.assertLogs(bot.log, level="ERROR") as captured:
            await bot.error_handler(object(), context)
        rendered = "\n".join(captured.output)
        self.assertNotIn("harmless multi word fixture", rendered)
        self.assertNotIn("forged-log-line", rendered)
        self.assertIn("RuntimeError", rendered)


class SandboxSecurityTests(unittest.TestCase):
    @staticmethod
    def _bind_destinations(args):
        destinations = []
        for index, value in enumerate(args[:-2]):
            if value in {"--bind", "--ro-bind"}:
                destinations.append(args[index + 2])
        return destinations

    def _agy_args(self, mode, root, *, with_venv=False):
        project = root / "managed-project"
        project.mkdir(parents=True)
        if with_venv:
            (project / ".venv").mkdir()
        home = root / "operator"
        agy = home / "tools" / "agy"
        token = home / ".gemini" / "antigravity-cli" / "token"
        trusted_uv = home / ".local" / "share" / "uv" / "python"
        state = root / "state"
        config = root / "config"
        for path in (agy.parent, token.parent, trusted_uv, state, config):
            path.mkdir(parents=True, exist_ok=True)
        agy.write_text("fixture", encoding="utf-8")
        token.write_text("fixture", encoding="utf-8")
        patches = (
            mock.patch.object(bot.Path, "home", return_value=home),
            mock.patch.object(bot, "AGY", str(agy)),
            mock.patch.object(bot, "AGY_TOKEN", token),
            mock.patch.object(bot, "AGY_STATE_DIR", token.parent),
            mock.patch.object(bot, "TRUSTED_UV_PYTHON_ROOT", trusted_uv),
            mock.patch.object(bot, "PROJECT_REPO", project),
            mock.patch("bot.prepare_agy_sandbox_storage"),
            mock.patch("bot.validate_agy_mount_sources"),
            mock.patch("bot.validate_project_repair_tree"),
            mock.patch("bot._validate_repair_overrides"),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9]:
            overrides = {"state_dir_override": state, "config_dir_override": config} if mode == "project-repair" else {}
            return build_agy_sandbox_args([str(agy)], mode=mode, **overrides)

    def test_bot_owned_writable_state_stays_beneath_state_root(self):
        writable_paths = (
            bot.CPA_AUTH_DIR,
            bot.CPA_CONFIG,
            bot.AGY_SANDBOX_ROOT,
            bot.AGY_SANDBOX_STATE_DIR,
            bot.AGY_TASK_STATE_DIR,
            bot.AGY_PROJECT_READ_STATE_DIR,
            bot.AGY_SANDBOX_CONFIG_DIR,
            bot.AGY_TASK_CONFIG_DIR,
            bot.AGY_PROJECT_READ_CONFIG_DIR,
            bot.CHAT_DIR,
            bot.CHAT_SESSION_DIR,
        )
        for path in writable_paths:
            self.assertTrue(path.is_relative_to(bot.STATE_ROOT), path)
            self.assertFalse(path.is_relative_to(bot.ROOT), path)

    def test_chat_and_task_sandboxes_do_not_mount_artifact_staging(self):
        self.assertFalse(bot.ARTIFACT_STAGING_DIR.is_relative_to(bot.CHAT_DIR))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for mode in ("chat", "task"):
                args = self._agy_args(mode, root / mode)
                self.assertNotIn(str(bot.ARTIFACT_STAGING_DIR), self._bind_destinations(args))

    def test_child_environment_is_allowlisted(self):
        env = sanitized_child_env({"HOME": "/tmp/fixture-home", "PATH": "/usr/bin", "RESCUE_BOT_TOKEN": "fixture-secret"})
        self.assertEqual(env["HOME"], "/tmp/fixture-home")
        self.assertNotIn("RESCUE_BOT_TOKEN", env)

    def test_project_is_fail_closed_without_absolute_configuration(self):
        with mock.patch.object(bot, "PROJECT_REPO", None):
            with self.assertRaisesRegex(RuntimeError, "not configured"):
                bot._validate_project_repository()
        with mock.patch.object(bot, "PROJECT_REPO", Path("relative")):
            with self.assertRaisesRegex(RuntimeError, "not configured"):
                bot._validate_project_repository()

    def test_repair_tree_rejects_hardlinks_and_external_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            regular = root / "regular"
            regular.write_text("data", encoding="utf-8")
            linked = root / "linked"
            os.link(regular, linked)
            with self.assertRaisesRegex(RuntimeError, "hard link"):
                bot.validate_project_repair_tree(root)
            linked.unlink()
            outside = root.parent / (root.name + "-outside")
            outside.write_text("data", encoding="utf-8")
            escape = root / "escape"
            escape.symlink_to(outside)
            try:
                with self.assertRaisesRegex(RuntimeError, "outside repository"):
                    bot.validate_project_repair_tree(root)
            finally:
                outside.unlink(missing_ok=True)

    def test_sandbox_source_keeps_read_only_token_and_host_controls_blocked(self):
        source = inspect.getsource(build_agy_sandbox_args)
        self.assertIn("--ro-bind", source)
        self.assertIn("AGY_TOKEN", source)
        self.assertIn("/usr/bin/systemctl", source)
        self.assertNotIn("mode == \"oauth\"", source)

    def test_sandbox_builders_never_emit_whole_host_root_bind(self):
        diagnostic = build_diagnostic_sandbox_args(["/usr/bin/true"])
        self.assertNotIn(["--ro-bind", "/", "/"], [diagnostic[i:i + 3] for i in range(len(diagnostic) - 2)])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for mode in ("chat", "task", "project-read", "project-repair"):
                with self.subTest(mode=mode):
                    args = self._agy_args(mode, root / mode)
                    self.assertNotIn(["--ro-bind", "/", "/"], [args[i:i + 3] for i in range(len(args) - 2)])

    def test_all_sandbox_modes_omit_host_data_roots(self):
        forbidden = {"/etc/machine-id", "/var", "/opt", "/srv", "/mnt", "/media", "/root", "/home"}
        diagnostic = build_diagnostic_sandbox_args(["/usr/bin/true"])
        self.assertTrue(forbidden.isdisjoint(self._bind_destinations(diagnostic)))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for mode in ("chat", "task", "project-read", "project-repair"):
                with self.subTest(mode=mode):
                    args = self._agy_args(mode, root / mode)
                    destinations = set(self._bind_destinations(args))
                    self.assertNotIn("/etc/machine-id", destinations)
                    self.assertTrue({"/var", "/opt", "/srv", "/mnt", "/media", "/root", "/home"}.isdisjoint(destinations))

    def test_chat_and_task_share_only_dedicated_artifact_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for mode in ("chat", "task"):
                args = self._agy_args(mode, root / mode)
                workspace = str(bot.CHAT_DIR)
                self.assertIn(["--bind", workspace, workspace], [args[i:i + 3] for i in range(len(args) - 2)])
                self.assertIn(["--chdir", workspace], [args[i:i + 2] for i in range(len(args) - 1)])
                self.assertNotIn(str(bot.PROJECT_REPO), args)
                self.assertNotIn(str(bot.CPA_AUTH_DIR), args)

    def test_project_modes_do_not_enable_artifact_auto_upload(self):
        async def exercise():
            message = SimpleNamespace(reply_text=mock.AsyncMock(return_value=mock.AsyncMock()))
            update = SimpleNamespace(
                effective_user=SimpleNamespace(id=1),
                effective_chat=SimpleNamespace(id=2, type="private"),
                effective_message=message,
            )
            context = SimpleNamespace(args=["inspect"])
            runner = mock.AsyncMock()
            with tempfile.TemporaryDirectory() as directory:
                with (
                    mock.patch("bot.allowed", return_value=True),
                    mock.patch.object(bot, "PROJECT_REPO", Path(directory)),
                    mock.patch("bot.build_agy_sandbox_args", return_value=["project-sandbox"]),
                    mock.patch("bot.run_agent_job", new=runner),
                ):
                    await project(update, context)
            args, kwargs = runner.await_args
            self.assertFalse(kwargs.get("artifact_workspace", False))
            self.assertNotIn(str(bot.CHAT_DIR), args[2])

            query = SimpleNamespace(
                data="confirm:abcdefgh", answer=mock.AsyncMock(),
                edit_message_reply_markup=mock.AsyncMock(),
                edit_message_text=mock.AsyncMock(), message=mock.AsyncMock(),
            )
            callback_update = SimpleNamespace(
                callback_query=query,
                effective_user=SimpleNamespace(id=1),
                effective_chat=SimpleNamespace(id=2, type="private"),
            )
            runner.reset_mock()
            storage = mock.MagicMock()
            storage.__enter__.return_value = (Path("/state"), Path("/config"))
            with tempfile.TemporaryDirectory() as directory:
                with (
                    mock.patch("bot.allowed", return_value=True),
                    mock.patch("bot.take_pending_confirmation", return_value=("project_repair", "fix")),
                    mock.patch.object(bot, "PROJECT_REPAIR_ENABLED", True),
                    mock.patch.object(bot, "PROJECT_REPO", Path(directory)),
                    mock.patch("bot.ephemeral_agy_repair_storage", return_value=storage),
                    mock.patch("bot.build_agy_sandbox_args", return_value=["repair-sandbox"]),
                    mock.patch("bot.run_agent_job", new=runner),
                ):
                    await confirmation_callback(callback_update, SimpleNamespace())
            args, kwargs = runner.await_args
            self.assertFalse(kwargs.get("artifact_workspace", False))
            self.assertNotIn(str(bot.CHAT_DIR), args[2])

        asyncio.run(exercise())

    def test_runtime_symlinks_bind_resolved_source_to_configured_destination(self):
        configured = Path("/etc/resolv.conf")
        if not configured.is_symlink():
            self.skipTest("resolver configuration is not a symlink on this host")
        args = build_diagnostic_sandbox_args(["/usr/bin/true"])
        triples = [args[index:index + 3] for index in range(len(args) - 2)]
        self.assertIn(
            ["--ro-bind", str(configured.resolve(strict=True)), str(configured)],
            triples,
        )

    def test_project_repair_mounts_optional_venv_only_when_present(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            absent = self._agy_args("project-repair", root / "absent")
            self.assertFalse(any(value.endswith("/.venv") for value in self._bind_destinations(absent)))
            self.assertFalse(any("/share/uv/python" in value for value in self._bind_destinations(absent)))
            present = self._agy_args("project-repair", root / "present", with_venv=True)
            self.assertTrue(any(value.endswith("/.venv") for value in self._bind_destinations(present)))
            self.assertTrue(any("/share/uv/python" in value for value in self._bind_destinations(present)))

    def test_diagnostic_sandbox_executes_without_host_var_visibility(self):
        if not shutil.which("bwrap"):
            self.skipTest("Bubblewrap is unavailable")
        python = Path("/usr/bin/python3")
        if not python.exists():
            self.skipTest("system Python is unavailable")
        sentinel_root = Path("/var/tmp")
        if not os.access(sentinel_root, os.W_OK):
            self.skipTest("/var/tmp is not safely writable")
        sentinel = sentinel_root / f"hermes-rescue-sentinel-{uuid.uuid4().hex}"
        sentinel.touch(mode=0o600, exist_ok=False)
        try:
            code = "import pathlib,ssl,sys; sys.exit(pathlib.Path(sys.argv[1]).exists())"
            args = build_diagnostic_sandbox_args([str(python), "-c", code, str(sentinel)])
            completed = subprocess.run(args, capture_output=True, timeout=15)
            if completed.returncode != 0 and b"No permissions to create new namespace" in completed.stderr:
                self.skipTest("unprivileged user namespaces are unavailable")
            self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", "replace"))
        finally:
            sentinel.unlink(missing_ok=True)

    def test_project_prompt_enforces_private_secret_safe_scope(self):
        prompt = project_guard_prompt("inspect health", repair=True)
        self.assertIn("private code", prompt)
        self.assertIn("Never push", prompt)
        self.assertIn("Never print or return access tokens", prompt)
        self.assertIn("127.0.0.1", prompt)

    def test_invalid_credential_never_auto_escalates(self):
        source = inspect.getsource(document)
        self.assertNotIn("agy_import_credential", source)
        self.assertNotIn("dangerously-skip-permissions", source)

    def test_project_handlers_use_mandatory_sandbox(self):
        self.assertIn("build_agy_sandbox_args", inspect.getsource(project))
        repair_source = inspect.getsource(confirmation_callback)
        self.assertIn("build_agy_sandbox_args", repair_source)
        self.assertIn("mutation=True", repair_source)

class ConfirmationCallbackValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_or_unknown_callback_never_consumes_or_executes_pending_action(self):
        class Query:
            def __init__(self, data):
                self.data = data
                self.answers = []
                self.message = SimpleNamespace()

            async def answer(self, text=None, show_alert=False):
                self.answers.append((text, show_alert))

            async def edit_message_reply_markup(self, **kwargs):
                raise AssertionError("invalid callback must not edit or consume pending action")

            async def edit_message_text(self, text):
                raise AssertionError("invalid callback must not execute pending action")

        user_id = bot.ALLOWED_USER_ID
        chat_id = 77
        for index, data in enumerate(("malformed", "approve:valid_nonce_12", "confirm:bad nonce")):
            nonce = f"valid_nonce_{index:02d}"
            bot.set_pending_confirmation(
                user_id,
                "restart",
                "",
                chat_id=chat_id,
                nonce=nonce,
            )
            query = Query(data.replace("valid_nonce_12", nonce))
            update = SimpleNamespace(
                effective_user=SimpleNamespace(id=user_id),
                effective_chat=SimpleNamespace(id=chat_id, type="private"),
                callback_query=query,
            )
            with mock.patch("bot.run", new=mock.AsyncMock()) as runner:
                await confirmation_callback(update, SimpleNamespace())
            self.assertIn(nonce, bot.PENDING_CONFIRMATIONS)
            runner.assert_not_awaited()
            self.assertTrue(query.answers)
            bot.PENDING_CONFIRMATIONS.pop(nonce, None)


class RepairFeatureGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_repair_command_and_stale_callback_fail_closed_when_disabled(self):
        replies = []

        class Message:
            async def reply_text(self, text, **kwargs):
                replies.append(text)

        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=bot.ALLOWED_USER_ID),
            effective_chat=SimpleNamespace(id=77, type="private"),
            effective_message=Message(),
        )
        with (
            mock.patch.object(bot, "PROJECT_REPAIR_ENABLED", False),
            mock.patch.object(bot, "PROJECT_REPO", Path("/tmp/project-fixture")),
        ):
            await bot.project_repair(update, SimpleNamespace(args=["fix", "it"]))
        self.assertIn("默认关闭", replies[-1])

        nonce = bot.set_pending_confirmation(
            bot.ALLOWED_USER_ID,
            "project_repair",
            "fix it",
            chat_id=77,
            nonce="repair_nonce_12",
        )

        class Query:
            data = f"confirm:{nonce}"
            message = SimpleNamespace()

            def __init__(self):
                self.edited = ""

            async def answer(self, *args, **kwargs):
                return None

            async def edit_message_reply_markup(self, **kwargs):
                return None

            async def edit_message_text(self, text):
                self.edited = text

        query = Query()
        callback_update = SimpleNamespace(
            effective_user=SimpleNamespace(id=bot.ALLOWED_USER_ID),
            effective_chat=SimpleNamespace(id=77, type="private"),
            callback_query=query,
        )
        with (
            mock.patch.object(bot, "PROJECT_REPAIR_ENABLED", False),
            mock.patch("bot.run_agent_job", new=mock.AsyncMock()) as runner,
        ):
            await confirmation_callback(callback_update, SimpleNamespace())
        runner.assert_not_awaited()
        self.assertIn("已关闭", query.edited)


class BotConfigurationTests(unittest.IsolatedAsyncioTestCase):
    def test_service_names_reject_option_like_traversal_malformed_and_long_values(self):
        invalid = (
            "-evil.service", ".hidden.service", "bad..name.service",
            "bad/service.service", "plain", "x" * 248 + ".service",
        )
        for value in invalid:
            with self.subTest(value=value[:40]):
                with mock.patch.dict(os.environ, {"TEST_SERVICE": value}):
                    with self.assertRaisesRegex(RuntimeError, "safe systemd service unit name"):
                        bot._service_name("TEST_SERVICE", "safe.service")

    def test_explicit_paths_reject_relative_and_control_characters(self):
        for key in ("RESCUE_PROJECT_REPO", "CPA_AUTH_DIR", "CPA_CONFIG", "AGY_TOKEN_PATH"):
            for value in ("relative/path", "/tmp/path\ncontrol"):
                with self.subTest(key=key, value=repr(value)):
                    with mock.patch.dict(os.environ, {key: value}):
                        with self.assertRaises(RuntimeError):
                            bot._configured_path(key)

        with mock.patch("bot.os.environ.get", return_value="/tmp/bad\x00path"):
            with self.assertRaisesRegex(RuntimeError, "control characters"):
                bot._configured_path("CPA_CONFIG")

    def test_empty_project_configuration_is_disabled_without_probes(self):
        with mock.patch.dict(os.environ, {"RESCUE_PROJECT_REPO": ""}):
            self.assertIsNone(bot._configured_path("RESCUE_PROJECT_REPO"))

    async def test_unconfigured_project_status_does_not_probe_service_or_filesystem(self):
        message = SimpleNamespace(reply_text=mock.AsyncMock())
        update = SimpleNamespace(effective_message=message)
        with (
            mock.patch("bot.allowed", return_value=True),
            mock.patch.object(bot, "PROJECT_REPO", None),
            mock.patch("bot.run", new=mock.AsyncMock()) as runner,
            mock.patch.object(
                bot,
                "CPA_AUTH_DIR",
                SimpleNamespace(glob=mock.Mock(side_effect=AssertionError("filesystem probe"))),
            ),
        ):
            await bot.project_status(update, SimpleNamespace())
        runner.assert_not_awaited()
        message.reply_text.assert_awaited_once()

    async def test_missing_allowed_user_id_fails_closed_before_side_effects(self):
        application = SimpleNamespace(bot=SimpleNamespace())
        with mock.patch.object(bot, "ALLOWED_USER_ID", 0):
            with self.assertRaisesRegex(RuntimeError, "RESCUE_ALLOWED_USER_ID"):
                await bot.configure_bot(application)


class CpaHttpBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_cpa_models_enforces_response_limit_and_uses_strict_parser(self):
        oversized = bot.httpx.MockTransport(
            lambda request: bot.httpx.Response(
                200,
                content=b"x" * (bot.MAX_HTTP_RESPONSE + 1),
                request=request,
            )
        )
        async with bot.httpx.AsyncClient(transport=oversized) as client:
            with self.assertRaises(ValueError):
                await bot.cpa_models(client=client, api_key="test-key")

        valid = bot.httpx.MockTransport(
            lambda request: bot.httpx.Response(
                200,
                json={"data": [{"id": "model-z"}, {"id": "model-a"}]},
                request=request,
            )
        )
        async with bot.httpx.AsyncClient(transport=valid) as client:
            self.assertEqual(
                await bot.cpa_models(client=client, api_key="test-key"),
                ["model-a", "model-z"],
            )


class CredentialMessageLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        bot.CREDENTIAL_WRITES_QUARANTINED = False
        bot.CREDENTIAL_IMPORT_TASKS.clear()

    def make_update(self, events):
        class Message:
            document = SimpleNamespace(file_name="account.json", file_size=100, file_id="f1")

            async def delete(self):
                events.append("delete")

        return SimpleNamespace(
            effective_user=SimpleNamespace(id=bot.ALLOWED_USER_ID),
            effective_chat=SimpleNamespace(id=77, type="private"),
            effective_message=Message(),
        )

    async def test_sensitive_message_is_deleted_before_progress_or_download(self):
        events = []

        class Notice:
            def __init__(self):
                self.edited = ""

            async def edit_text(self, text):
                self.edited = text

        notice = Notice()

        class FakeBot:
            async def send_message(self, **kwargs):
                events.append("send")
                return notice

            async def get_file(self, file_id):
                events.append("download")
                raise RuntimeError("download failed")

        update = self.make_update(events)
        with self.assertLogs("hermes-rescue-bot", level="ERROR"):
            await document(update, SimpleNamespace(bot=FakeBot()))
        self.assertEqual(events, ["delete", "send", "download"])
        self.assertIn("下载失败", notice.edited)

    async def test_repeated_cancellation_holds_lock_until_recovery_is_classified(self):
        started = asyncio.Event()
        release = asyncio.Event()
        events = []

        class Notice:
            async def edit_text(self, text): pass
        class TelegramFile:
            async def download_as_bytearray(self): return bytearray(b"{}")
        class FakeBot:
            async def send_message(self, **kwargs):
                events.append("send")
                return Notice()
            async def get_file(self, file_id):
                events.append("download")
                return TelegramFile()
        async def fake_to_thread(function, *args):
            started.set()
            await release.wait()
            raise bot.CredentialRecoveryError("fixture")

        with mock.patch("bot.asyncio.to_thread", new=fake_to_thread):
            task = asyncio.create_task(document(self.make_update(events), SimpleNamespace(bot=FakeBot())))
            await asyncio.wait_for(started.wait(), timeout=1)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.assertTrue(bot.MUTATION_LOCK.locked())
            self.assertEqual(len(bot.CREDENTIAL_IMPORT_TASKS), 1)

            before_second = list(events)
            await document(self.make_update(events), SimpleNamespace(bot=FakeBot()))
            self.assertEqual(events[len(before_second):], ["delete", "delete", "send"])
            self.assertFalse(task.done())
            self.assertTrue(bot.MUTATION_LOCK.locked())

            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(bot.CREDENTIAL_WRITES_QUARANTINED)
        self.assertFalse(bot.MUTATION_LOCK.locked())
        self.assertEqual(bot.CREDENTIAL_IMPORT_TASKS, set())
        events.clear()
        await document(self.make_update(events), SimpleNamespace(bot=FakeBot()))
        self.assertEqual(events, ["delete", "send"])

    async def test_allowlisted_invalid_inputs_delete_early_but_nonallowlisted_do_not(self):
        events = []
        update = self.make_update(events)
        update.effective_message.document.file_name = "not-json.txt"
        await document(update, SimpleNamespace(bot=mock.AsyncMock()))
        self.assertEqual(events[0], "delete")

        class ForeignMessage:
            document = SimpleNamespace(file_name="account.json", file_size=1, file_id="x")
            async def delete(self): events.append("foreign-delete")
            async def reply_text(self, text): events.append("deny")
        foreign = SimpleNamespace(
            effective_user=SimpleNamespace(id=bot.ALLOWED_USER_ID + 1),
            effective_chat=SimpleNamespace(id=2, type="private"),
            effective_message=ForeignMessage(),
        )
        await document(foreign, SimpleNamespace(bot=mock.AsyncMock()))
        self.assertNotIn("foreign-delete", events)

    async def test_progress_send_failure_still_deletes_sensitive_message_and_releases_lock(self):
        events = []

        class FakeBot:
            async def send_message(self, **kwargs):
                events.append("send")
                raise RuntimeError("telegram send failed")

        update = self.make_update(events)
        with self.assertRaises(RuntimeError):
            await document(update, SimpleNamespace(bot=FakeBot()))
        self.assertEqual(events, ["delete", "send"])
        self.assertFalse(bot.MUTATION_LOCK.locked())

    async def _document_error_text(self, error):
        events = []

        class Notice:
            def __init__(self):
                self.edited = ""

            async def edit_text(self, text):
                self.edited = text

        notice = Notice()

        class TelegramFile:
            async def download_as_bytearray(self):
                return bytearray(b"{}")

        class FakeBot:
            async def send_message(self, **kwargs):
                return notice

            async def get_file(self, file_id):
                return TelegramFile()

        async def direct_to_thread(function, *args):
            return function(*args)

        with (
            mock.patch("bot.import_cpa_bundle", side_effect=error),
            mock.patch("bot.asyncio.to_thread", new=direct_to_thread),
        ):
            await document(self.make_update(events), SimpleNamespace(bot=FakeBot()))
        return notice.edited

    async def test_incomplete_credential_rollback_is_reported_as_critical(self):
        with self.assertLogs("hermes-rescue-bot", level="CRITICAL"):
            text = await self._document_error_text(
                bot.CredentialRecoveryError("recovery incomplete")
            )
        self.assertIn("回滚不完整", text)
        self.assertIn("停止使用 CPA", text)
        self.assertIn("人工检查", text)
        self.assertNotIn("格式不受支持", text)
        self.assertTrue(bot.CREDENTIAL_WRITES_QUARANTINED)

    async def test_recovered_commit_failure_is_not_reported_as_format_error(self):
        text = await self._document_error_text(
            bot.CredentialCommitError("commit failed and restored")
        )
        self.assertIn("完整恢复", text)
        self.assertNotIn("格式不受支持", text)

    async def test_cancellation_waits_for_import_thread_before_releasing_mutation_lock(self):
        events = []
        started = asyncio.Event()
        release = asyncio.Event()

        class Notice:
            async def edit_text(self, text):
                return None

        class TelegramFile:
            async def download_as_bytearray(self):
                return bytearray(b"{}")

        class FakeBot:
            async def send_message(self, **kwargs):
                return Notice()

            async def get_file(self, file_id):
                return TelegramFile()

        async def fake_to_thread(function, raw, auth_dir):
            started.set()
            await release.wait()
            return {
                "format": "codex",
                "imported": 1,
                "created": 1,
                "updated": 0,
                "unchanged": 0,
            }

        update = self.make_update(events)
        async def direct_to_thread(function, *args):
            return function(*args)
        with (
            mock.patch("bot.asyncio.to_thread", new=fake_to_thread),
            mock.patch("bot.cpa_models", new=mock.AsyncMock(return_value=["model-a"])),
        ):
            task = asyncio.create_task(document(update, SimpleNamespace(bot=FakeBot())))
            await asyncio.wait_for(started.wait(), timeout=2)
            task.cancel()
            await asyncio.sleep(0)
            try:
                self.assertTrue(bot.MUTATION_LOCK.locked())
                self.assertFalse(task.done())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(bot.MUTATION_LOCK.locked())

    async def test_document_success_uses_bundled_importer_and_avoids_false_account_claim(self):
        events = []

        class Notice:
            def __init__(self):
                self.edited = ""

            async def edit_text(self, text):
                self.edited = text

        notice = Notice()

        class TelegramFile:
            async def download_as_bytearray(self):
                events.append("download")
                return bytearray(b"{}")

        class FakeBot:
            async def send_message(self, **kwargs):
                events.append("send")
                return notice

            async def get_file(self, file_id):
                events.append("get_file")
                return TelegramFile()

        update = self.make_update(events)
        async def direct_to_thread(function, *args):
            return function(*args)
        with (
            mock.patch(
                "bot.import_cpa_bundle",
                return_value={
                    "format": "codex",
                    "imported": 1,
                    "created": 1,
                    "updated": 0,
                    "unchanged": 0,
                },
            ) as importer,
            mock.patch("bot.asyncio.to_thread", new=direct_to_thread),
            mock.patch(
                "bot.cpa_models",
                new=mock.AsyncMock(return_value=["model-a"]),
            ),
            mock.patch("bot.asyncio.sleep", new=mock.AsyncMock()),
        ):
            await document(update, SimpleNamespace(bot=FakeBot()))

        importer.assert_called_once_with(b"{}", bot.CPA_AUTH_DIR)
        self.assertEqual(events, ["delete", "send", "get_file", "download"])
        self.assertIn("凭证文件已导入", notice.edited)
        self.assertIn("新增账号：1", notice.edited)
        self.assertIn("更新账号：0", notice.edited)
        self.assertIn("未变化：0", notice.edited)
        self.assertIn("不代表已逐账号验证", notice.edited)
        self.assertNotIn("凭证导入成功", notice.edited)
        self.assertFalse(bot.MUTATION_LOCK.locked())


if __name__ == "__main__":
    unittest.main()
