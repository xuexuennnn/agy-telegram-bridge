import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_LOCAL_RELEASE_FILES = {
    "CODEX_RESULT.md", "CONTINUE_TASK.md", "FIXUP.md", "FIXUP2.md",
    "FIXUP3.md", "FIXUP4.md", "REVIEW_TASK.md", "PORT_UX_ARTIFACTS.md",
    "FIX_REVIEW_UX_ARTIFACTS.md", "FIX_REVIEW2.md", "FIX_REVIEW3.md",
}
REQUIRED = {
    "README.md", "LICENSE", "NOTICE", ".env.example", "requirements.txt",
    ".gitignore", "SECURITY.md", "CONTRIBUTING.md",
    ".github/workflows/test.yml", "systemd/hermes-rescue-bot.service",
    "scripts/verify-unit.sh",
}


def public_files():
    output = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        text=True,
    )
    for name in output.splitlines():
        path = ROOT / name
        if path.is_file() and name not in FORBIDDEN_LOCAL_RELEASE_FILES:
            yield name, path


class PublicReleaseTests(unittest.TestCase):
    def test_required_materials_exist(self):
        missing = sorted(name for name in REQUIRED if not (ROOT / name).is_file())
        self.assertEqual(missing, [])

    def test_local_review_and_task_files_are_not_public_release_content(self):
        names = {name for name, _ in public_files()}
        self.assertFalse(
            names.intersection(FORBIDDEN_LOCAL_RELEASE_FILES)
        )

    def test_public_candidate_command_excludes_every_local_review_file(self):
        output = subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=ROOT,
            text=True,
        )
        self.assertEqual(set(output.splitlines()).intersection(FORBIDDEN_LOCAL_RELEASE_FILES), set())

    def test_actions_are_immutably_pinned_and_dependencies_are_checked(self):
        workflow = (ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8")
        self.assertIn(
            "actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5 # v4",
            workflow,
        )
        self.assertIn(
            "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065 # v5",
            workflow,
        )
        self.assertIn("python -m pip check", workflow)
        action_refs = re.findall(r"uses:\s*[^@\s]+@([^\s#]+)", workflow)
        self.assertTrue(action_refs)
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in action_refs))

    def test_public_files_contain_no_private_identity_paths_or_secrets(self):
        private_names = ["noda" + "velle", "agy" + "bot"]
        old_runtime_names = [
            ".model" + "pass", "agy-" + "runtime", "chat-session-" + "state",
            "cpa/" + "auths", "back" + "ups/",
        ]
        secret_patterns = [
            re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),
            re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
            re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
            re.compile(r"\bAIza[A-Za-z0-9_-]{30,}\b"),
            re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
            re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
            re.compile(r"[A-Za-z0-9._%+-]+@(?!example\.(?:com|org)\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
        ]
        failures = []
        for name, path in public_files():
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                failures.append(f"{name}: binary public artifact")
                continue
            lowered = text.lower()
            for value in private_names:
                if value in lowered:
                    failures.append(f"{name}: private identity")
            if "/home/" + "ubuntu" in text:
                failures.append(f"{name}: fixed home path")
            if re.search(r"[a-z0-9-]+\.apps\.googleusercontent\.com", lowered):
                failures.append(f"{name}: embedded client id")
            if name != ".gitignore":
                for value in old_runtime_names:
                    if value in lowered:
                        failures.append(f"{name}: old runtime name {value}")
            for pattern in secret_patterns:
                if pattern.search(text):
                    failures.append(f"{name}: secret-like fixture")
        self.assertEqual(failures, [])

    def test_systemd_unit_is_portable_and_bubblewrap_compatible(self):
        unit = (ROOT / "systemd/hermes-rescue-bot.service").read_text(encoding="utf-8")
        self.assertIn("%h", unit)
        required = {
            "NoNewPrivileges=yes", "RestrictSUIDSGID=yes",
            "LockPersonality=yes",
            "RestrictRealtime=yes",
            "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
            "KeyringMode=private",
            "SystemCallFilter=~@clock @cpu-emulation @debug @module @obsolete @raw-io @reboot @swap",
            "SystemCallArchitectures=native", "Restart=on-failure",
            "RestartSec=5", "TimeoutStartSec=60", "TimeoutStopSec=30",
            "UMask=0077", "StandardOutput=journal", "StandardError=journal",
        }
        self.assertEqual(required - set(unit.splitlines()), set())
        for unsupported in (
            "ProtectSystem=", "ProtectHome=", "ReadWritePaths=", "ReadOnlyPaths=",
            "InaccessiblePaths=", "BindPaths=", "BindReadOnlyPaths=",
            "TemporaryFileSystem=", "RootDirectory=", "RootImage=", "PrivateTmp=",
            "PrivateUsers=", "MemoryDenyWriteExecute=",
            "ProtectKernelTunables=", "ProtectKernelModules=", "ProtectControlGroups=",
        ):
            self.assertNotIn(unsupported, unit)
        address_line = next(
            line for line in unit.splitlines() if line.startswith("RestrictAddressFamilies=")
        )
        self.assertEqual(address_line, "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6")

if __name__ == "__main__":
    unittest.main()
