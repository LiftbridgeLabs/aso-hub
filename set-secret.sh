#!/bin/bash
# Save one ASO Hub secret without it showing on screen or in shell history.
# Usage: ./set-secret.sh asobeast-token (or alerts-lumen: an app's #alerts webhook)
# Paste the value at the prompt (input is hidden) and press Return.
set -euo pipefail
name="${1:?usage: ./set-secret.sh <name>, for example asobeast-token}"
[[ "$name" =~ ^[a-z0-9-]+$ ]] || { echo "name: lowercase letters, digits and dashes only" >&2; exit 2; }
# On the server the hub's secrets folder; on the Mac ~/.config/aso-hub.
server_dir=/opt/appdata/aso-hub/secrets
if [[ -n "${ASO_HUB_SECRETS:-}" ]]; then dir="$ASO_HUB_SECRETS"
elif [[ -d "$server_dir" ]]; then dir="$server_dir"
else dir="$HOME/.config/aso-hub"; fi
mkdir -p "$dir" && chmod 700 "$dir"
IFS= read -r -s -p "Paste $name: " value
echo
[[ -n "$value" ]] || { echo "nothing pasted, nothing saved" >&2; exit 1; }
if [[ "$name" == alerts-* || "$name" == discord-webhook ]] && [[ "$value" != https://discord.com/api/webhooks/* ]]; then
  echo "that isn't a Discord webhook address (it starts https://discord.com/api/webhooks/); nothing saved" >&2; exit 1
fi
if [[ "$name" == hub-password && ${#value} -lt 12 ]]; then
  echo "use at least 12 characters; nothing saved" >&2; exit 1
fi
umask 077
tmp=$(mktemp "$dir/.$name.XXXXXX")
trap 'rm -f "$tmp"' EXIT
printf '%s\n' "$value" > "$tmp"
mv "$tmp" "$dir/$name"
echo "Saved to $dir/$name (only you can read it)."
