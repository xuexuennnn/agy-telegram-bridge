# Antigravity Telegram Bridge (agy-telegram-bridge)

[![tests](https://github.com/xuexuennnn/agy-telegram-bridge/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/xuexuennnn/agy-telegram-bridge/actions/workflows/test.yml)


A Telegram control plane and Bubblewrap sandbox interface for running the Antigravity CLI (`agy`) directly from Telegram with a Hermes-like conversation UX.

The problem it solves: allows calling the Antigravity CLI from Telegram in a sandboxed, stateful conversation flow (similar to Hermes Agent), while keeping high-risk Linux host actions safely isolated under Bubblewrap.

It is a single-administrator tool. It is not multi-tenant, and it is deliberately not a
remote shell.

## Stack

Python 3.11+, asyncio, `python-telegram-bot`, Bubblewrap (`bwrap`), systemd user units,
`unittest`, GitHub Actions.

No database. State lives under `$HOME/.local/state/hermes-rescue-bot/`.

## How it works

```
Telegram update
  -> private-chat check + exact numeric user-ID allowlist
  -> command router (plain Python control plane)
  -> per-task Bubblewrap sandbox, built from an empty root
       - explicit read-only mounts for required binaries, loaders, DNS/TLS/NSS material
       - /var, /opt, /srv, home directories, and unrelated data roots are not mounted
       - isolated writable state; existing OAuth token mounted read-only
  -> output bounded, secrets redacted, response split for Telegram's UTF-16 limits
  -> new or modified files returned as native Telegram attachments
```

Risky host actions (restart, repair) require a button confirmation carrying a random
one-time nonce bound to the requesting user and chat, with a five-minute expiry.

The outer systemd user unit intentionally does **not** use `ProtectSystem`, `ProtectHome`,
or other filesystem namespace directives, because they prevent the unprivileged nested
Bubblewrap user namespace the sandbox depends on. The control plane's own host boundary
is therefore enforced by fixed paths, validation, and the single-user allowlist in code —
not by systemd. This trade-off is documented rather than hidden.

## Install

Requires Python 3.11+, `bwrap`, and a Telegram bot token.

```sh
git clone https://github.com/xuexuennnn/agy-telegram-bridge.git
cd agy-telegram-bridge
python -m venv .venv && .venv/bin/pip install -r requirements.txt

install -d "$HOME/.config/hermes-rescue-bot"
install -m 0600 .env.example "$HOME/.config/hermes-rescue-bot/rescue.env"
${EDITOR:-vi} "$HOME/.config/hermes-rescue-bot/rescue.env"
```

Fill in `RESCUE_BOT_TOKEN` and `RESCUE_ALLOWED_USER_ID` before starting. Then, to run it
as a service:

```sh
install -Dm644 systemd/agy-telegram-bridge.service \
  "$HOME/.config/systemd/user/agy-telegram-bridge.service"
systemctl --user daemon-reload
systemctl --user enable --now agy-telegram-bridge.service
```

## Tests

```sh
python -m unittest discover -s tests -q
```

Current result on a clean clone with Python 3.12:

```
Ran 130 tests in 2.45s

OK
```

The suite covers the security boundary (`tests/test_bot_security.py`, 782 lines), chat and
formatting behaviour (`tests/test_chat_ux.py`, 1616 lines), the core task runner, and the
public-release checks that assert no credentials or host-specific paths are present.

Full verification, matching what CI runs:

```sh
python -m unittest discover -s tests -q
python -m py_compile bot.py rescue_core.py tests/test_*.py
python -m pip check
scripts/verify-unit.sh
git diff --check
```

`scripts/verify-unit.sh` builds and removes a fake install tree under `/tmp`. It does not
write to a real home directory and does not start, stop, or reload any service.

CI runs on every push and pull request against Python 3.11 and 3.12. All GitHub Actions
are pinned to commit SHAs rather than tags.

## What is defended against

Malicious messages, prompt injection, hostile repository trees, symlink and mount escapes,
Git hook and config execution, secret disclosure in output, concurrent modification, and
runaway subprocesses.

Artifact return is the most defensive path. Before and after a task, the workspace is
walked using `O_DIRECTORY|O_NOFOLLOW` with relative directory file descriptors, comparing
device, inode, type and mode, owner, link count, size, mtime, and ctime. Only the exact
byte count declared by the snapshot is read. Verified content is copied to a `0600`
private file before the chat lock is released, and Telegram only ever reads that stable
copy — never a live workspace descriptor. Symlinks, hardlinks, cross-filesystem entries,
wrong ownership, group- or world-writable files, and out-of-bounds paths are rejected. If
the directory or entry budget is exhausted, the whole snapshot fails and returns nothing
rather than a partial result.

## Limitations

- Sandboxed tasks still share the host network. The diagnostic sandbox does not.
- A task can read the read-only OAuth token it is given. Untrusted repositories and
  prompts are not zero-risk.
- Snapshot verification proves file *identity and integrity*, not that content is benign.
  It is not antivirus and not a document parser.
- Import success does not imply that quota, refresh capability, or each account was
  validated.
- Telegram is not a good channel for credentials. The bot deletes the source message on a
  best-effort basis; if deletion fails, delete it manually.
- Kernel parameters, kernel modules, and cgroup protections must come from host policy.
  This is a user service.

## Security reporting

See `SECURITY.md`.

## License

MIT. See `LICENSE`.

Not affiliated with or endorsed by Telegram, Google, or the maintainers of any third-party
CLI referenced here. Third-party CLIs are not distributed with this project.
