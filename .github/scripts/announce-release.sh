#!/usr/bin/env bash
# Announces a published Spela release in Discord (.github/workflows/release-announce.yml runs it).
#   announce-release.sh <tag>              check the release, then post it to $DISCORD_WEBHOOK_URL once
#   announce-release.sh <tag> --dry-run    print the Discord message instead; changes nothing, needs no webhook
# Needs gh (GH_TOKEN) and jq. The release must be stable (vX.Y.Z, not a draft or prerelease) and carry the files
# release.ps1 uploads. A marker in its notes (MARKER, invisible on GitHub) records the announcement, so a rerun skips it.
set -euo pipefail

tag=${1:?usage: announce-release.sh <tag> [--dry-run]}
dry=${2:-}
repo=${GITHUB_REPOSITORY:-ensamvarg360/spela}
MARKER="<!-- spela:discord-announced -->"
ASSET_TRIES=${ASSET_TRIES:-10}  # 30 s apart: gh normally uploads before it publishes, this covers a slow upload

skip() { echo "::notice::Not announcing $tag: $1"; exit 0; }
fail() { echo "::error::Discord announcement for $tag failed: $1 The release itself is fine."; exit 1; }

[[ $tag =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || skip "not a stable version tag."
version=${tag#v}
release=$(gh api "repos/$repo/releases/tags/$tag") || fail "couldn't read the release from GitHub."
[[ $(jq -r .draft <<<"$release") == false ]] || skip "it's a draft."
[[ $(jq -r .prerelease <<<"$release") == false ]] || skip "it's a prerelease."
body=$(jq -r '.body // ""' <<<"$release")
[[ $body != *"$MARKER"* || $dry == --dry-run ]] || skip "it was announced already."

# The downloads release.ps1 uploads; announce only once GitHub has all of them
wanted=("Spela-$version.zip" "SHA256SUMS.txt")
for try in $(seq "$ASSET_TRIES"); do
  have=$(gh api "repos/$repo/releases/tags/$tag" --jq '[.assets[] | select(.state == "uploaded") | .name]') ||
    fail "couldn't read the release's files from GitHub."
  missing=$(jq -r --args '$ARGS.positional - . | join(", ")' "${wanted[@]}" <<<"$have")
  [[ -z $missing ]] && break
  ((try < ASSET_TRIES)) || fail "its downloads never all appeared (missing: $missing)."
  echo "Waiting for $missing ($try/$ASSET_TRIES)"
  sleep 30
done
zip_url=$(gh api "repos/$repo/releases/tags/$tag" --jq ".assets[] | select(.name == \"Spela-$version.zip\") | .browser_download_url")
page=$(jq -r .html_url <<<"$release")

# The notes without release.ps1's Downloads table (Discord shows no tables), headings in bold, cut to fit an embed
notes=$(sed '/^### Downloads/,$d' <<<"$body" | sed -E 's/^#{1,6} +(.+)$/**\1**/')
links="[Download Spela-$version.zip]($zip_url) · [Release page]($page)"
limit=$((4096 - ${#links} - 40))  # an embed description holds 4096 characters
if ((${#notes} > limit)); then notes="${notes:0:limit}…"$'\n\n'"[All the notes]($page)"; fi
payload=$(jq -n --arg title "Spela $tag is out" --arg url "$page" --arg notes "$notes" --arg links "$links" '{
  username: "Spela Releases",
  allowed_mentions: {parse: []},
  embeds: [{title: $title, url: $url, color: 7506394, description: (($notes | rtrimstr("\n")) + "\n\n" + $links)}]
}')

if [[ $dry == --dry-run ]]; then
  echo "$payload"
  exit 0
fi
[[ -n ${DISCORD_WEBHOOK_URL:-} ]] || fail "the repository secret DISCORD_WEBHOOK_URL isn't set."

# Claim it first, so a second run can't post it again; give the claim back if Discord refuses
release_id=$(jq -r .id <<<"$release")
gh api -X PATCH "repos/$repo/releases/$release_id" -f body="$body"$'\n\n'"$MARKER" >/dev/null ||
  fail "couldn't mark the release as announced, so nothing was posted."
# the URL stays out of the log: no -v, and curl's own errors name only the host
if ! status=$(curl -sS -o /tmp/discord-response -w '%{http_code}' -H "Content-Type: application/json" \
    --data-binary "$payload" "$DISCORD_WEBHOOK_URL?wait=true") || [[ $status != 2* ]]; then
  gh api -X PATCH "repos/$repo/releases/$release_id" -f body="$body" >/dev/null ||
    echo "::warning::Couldn't remove the announced marker from the release notes. Delete '$MARKER' from them before rerunning."
  fail "Discord answered ${status:-nothing}: $(head -c 300 /tmp/discord-response 2>/dev/null)"
fi
echo "Announced $tag in Discord."
