#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
tmp=$(mktemp -d /tmp/hermes-rescue-unit.XXXXXX)
trap 'rm -rf "$tmp"' EXIT HUP INT TERM

home=$tmp/home
install=$home/.local/share/hermes-rescue-bot
mkdir -p "$install/.venv/bin" "$home/.config/hermes-rescue-bot" \
    "$home/.local/state/hermes-rescue-bot" "$tmp/unit"
cp "$root/bot.py" "$install/bot.py"
ln -s "$(command -v python3)" "$install/.venv/bin/python"
: > "$home/.config/hermes-rescue-bot/rescue.env"
sed "s|%h|$home|g" "$root/systemd/hermes-rescue-bot.service" \
    > "$tmp/unit/hermes-rescue-bot.service"

systemd-analyze --user verify "$tmp/unit/hermes-rescue-bot.service"
