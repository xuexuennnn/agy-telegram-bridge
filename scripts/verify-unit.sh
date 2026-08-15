#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
tmp=$(mktemp -d /tmp/hermes-rescue-unit.XXXXXX)
trap 'rm -rf "$tmp"' EXIT HUP INT TERM

home=$tmp/home
install=$home/agy-telegram-bridge
mkdir -p "$install/.venv/bin" "$home/.config/agy-telegram-bridge" \
    "$home/.local/state/agy-telegram-bridge" "$tmp/unit"
cp "$root/bot.py" "$install/bot.py"
ln -s "$(command -v python3)" "$install/.venv/bin/python3"
: > "$home/.config/agy-telegram-bridge/rescue.env"
sed "s|%h|$home|g" "$root/systemd/agy-telegram-bridge.service" \
    > "$tmp/unit/agy-telegram-bridge.service"

systemd-analyze --user verify "$tmp/unit/agy-telegram-bridge.service"
