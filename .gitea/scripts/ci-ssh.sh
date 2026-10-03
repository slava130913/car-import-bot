#!/usr/bin/env bash
# SSH helper for Gitea Actions jobs. Plain OpenSSH + tar: no third-party actions to break or trust.
#
#   ci-ssh.sh deploy   atomic release to every server in DEPLOY_TARGETS, health check, auto-rollback
#   ci-ssh.sh run      run REMOTE_COMMAND on every server in DEPLOY_TARGETS
#
# Inputs (environment):
#   SSH_PRIVATE_KEY     secret: private key of the CI deploy identity (no passphrase), "base64:..." form
#   SSH_KNOWN_HOSTS     variable: known_hosts lines of the servers; unknown host keys are rejected
#   DEPLOY_TARGETS      user@host[:port] list, separated by spaces, commas or newlines
#   DEPLOY_PATH         remote base dir (default /srv/<repo>): releases/, shared/, current -> releases/<id>
#   DEPLOY_SOURCE       local dir to upload (default .)
#   DEPLOY_EXCLUDES     extra tar --exclude patterns, space separated
#   DEPLOY_BUILD        remote command run in the new release before it goes live (e.g. npm ci)
#   DEPLOY_ACTIVATE     remote command run in `current` after the switch (e.g. docker compose up -d --build)
#   DEPLOY_HEALTHCHECK  remote command that must succeed after activation; retried
#   DEPLOY_HEALTH_RETRIES / DEPLOY_HEALTH_DELAY   default 10 tries, 3 s apart
#   DEPLOY_KEEP         releases kept per server (default 5)
#   DEPLOY_ENV_FILE     secret, optional, "base64:..." form: written to shared/.env (0600), linked into releases
#   DEPLOY_PUBLIC_URL   optional URL checked from the runner after all servers are live
#   REMOTE_COMMAND      bash script for `run`
set -euo pipefail

mode=${1:?usage: ci-ssh.sh deploy|run}
: "${SSH_PRIVATE_KEY:?secret SSH_DEPLOY_KEY is empty}"
: "${SSH_KNOWN_HOSTS:?variable SSH_KNOWN_HOSTS is empty: register the server host keys first}"
: "${DEPLOY_TARGETS:?no target servers: set DEPLOY_TARGETS}"

# act_runner masks a secret only as one whole line; the step's env dump shows multi-line values
# unmasked. So file-like secrets are stored as one line "base64:<data>" (scripts/ci.py does it).
secret_value() {
    case $1 in
        base64:*) printf '%s' "${1#base64:}" | base64 -d ;;
        *) printf '%s\n' "$1" ;;
    esac
}
for name in SSH_PRIVATE_KEY DEPLOY_ENV_FILE; do
    if [[ ${!name:-} == *$'\n'* ]]; then
        echo "::warning::$name is a multi-line secret and is NOT masked in logs. Re-save it with: python scripts/ci.py set-secret ... --file"
    fi
done

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
install -m 600 /dev/null "$work/key"
secret_value "$SSH_PRIVATE_KEY" | tr -d '\r' > "$work/key"
printf '%s\n' "$SSH_KNOWN_HOSTS" | tr -d '\r' > "$work/known_hosts"
ssh-keygen -y -f "$work/key" >/dev/null || { echo 'SSH_DEPLOY_KEY is not a valid unencrypted private key' >&2; exit 1; }

read -r -a targets <<< "$(printf '%s' "$DEPLOY_TARGETS" | tr ',\r\n' '   ')"
[ "${#targets[@]}" -gt 0 ] || { echo 'DEPLOY_TARGETS has no servers' >&2; exit 1; }

remote() {  # remote TARGET COMMAND...  (stdin is forwarded)
    local target=$1 userhost port=22
    shift
    userhost=${target%:*}
    if [[ $target == *:* ]]; then port=${target##*:}; fi
    ssh -i "$work/key" -p "$port" -o BatchMode=yes -o IdentitiesOnly=yes \
        -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$work/known_hosts" \
        -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=4 \
        "$userhost" "$@"
}

# Script from stdin -> temp file on the server -> run with stdin=/dev/null, so commands that
# read stdin (docker, npm, ssh...) cannot swallow the rest of the script.
remote_script() {
    remote "$1" 'f=$(mktemp) && cat > "$f" && bash "$f" < /dev/null; rc=$?; rm -f "$f"; exit $rc'
}

run_all() {
    for target in "${targets[@]}"; do
        echo "::group::$target"
        printf 'set -euo pipefail\n%s\n' "${REMOTE_COMMAND:?REMOTE_COMMAND is empty}" | remote_script "$target"
        echo '::endgroup::'
    done
}

# Runs on the server under a lock; the preamble with quoted values is prepended by deploy_all.
activate_script() {
    cat <<'REMOTE'
set -euo pipefail
cd "$BASE"
exec 9>"$BASE/.deploy.lock"
if command -v flock >/dev/null; then flock -w 600 9 || { echo 'Another deploy holds the lock' >&2; exit 1; }; fi
rel="releases/$ID"
if [ -f shared/.env ]; then ln -sfn ../../shared/.env "$rel/.env"; fi
printf 'sha=%s\nref=%s\nreleased=%s\n' "$SHA" "$REF" "$(date -u +%FT%TZ)" > "$rel/.release"
if [ -n "$BUILD" ] && ! (cd "$rel" && bash -eo pipefail -c "$BUILD") 9>&-; then
    rm -rf -- "$rel"
    echo 'Build failed on the server; nothing was switched' >&2
    exit 1
fi
prev=$(readlink current 2>/dev/null || true)
# User commands run without fd 9: a daemon they start must not inherit (and hold) the deploy lock.
user_cmd() { (cd "$1" && bash -eo pipefail -c "$2") 9>&-; }
switch() { ln -sfn "$1" .current.tmp && { mv -Tf .current.tmp current 2>/dev/null || { rm -f current && mv .current.tmp current; }; }; }
activate() { [ -z "$ACTIVATE" ] || user_cmd current "$ACTIVATE"; }
healthy() {
    [ -z "$HEALTH" ] && return 0
    for _ in $(seq "$RETRIES"); do
        if user_cmd current "$HEALTH" >/dev/null 2>&1; then return 0; fi
        sleep "$DELAY"
    done
    user_cmd current "$HEALTH" || return 1
}
switch "$rel"
if activate && healthy; then
    echo "Live: $rel"
    ls -1dt releases/*/ | sed 's#/$##' | tail -n +"$((KEEP + 1))" | while read -r old; do
        if [ "$old" != "$rel" ] && [ "$old" != "$prev" ]; then rm -rf -- "$old"; fi
    done
    exit 0
fi
echo "Release $rel failed activation or health check" >&2
if [ -n "$prev" ] && [ -d "$prev" ]; then
    switch "$prev"
    if activate && healthy; then echo "Rolled back to $prev (healthy)" >&2; else echo "Rolled back to $prev, but it is not healthy either" >&2; fi
else
    echo 'No previous release to roll back to' >&2
    rm -f current
fi
mv -- "$rel" "releases/failed-$ID"
exit 1
REMOTE
}

deploy_all() {
    local base=${DEPLOY_PATH:-/srv/${GITHUB_REPOSITORY##*/}}
    local source=${DEPLOY_SOURCE:-.}
    local id sha
    sha=${GITHUB_SHA:-manual}
    id="$(date -u +%Y%m%d%H%M%S)-${sha:0:8}"
    local excludes=(--exclude=./.git --exclude=./.gitea)
    for pattern in ${DEPLOY_EXCLUDES:-}; do excludes+=("--exclude=$pattern"); done
    tar -C "$source" "${excludes[@]}" -czf "$work/release.tgz" .
    echo "Release $id ($(du -h "$work/release.tgz" | cut -f1)) -> ${#targets[@]} server(s)"

    for target in "${targets[@]}"; do
        echo "::group::$target"
        remote "$target" "set -e; mkdir -p $(printf '%q' "$base/releases/$id") $(printf '%q' "$base/shared") && tar -xzf - -C $(printf '%q' "$base/releases/$id")" < "$work/release.tgz"
        if [ -n "${DEPLOY_ENV_FILE:-}" ]; then
            secret_value "$DEPLOY_ENV_FILE" | tr -d '\r' | remote "$target" "umask 077; cat > $(printf '%q' "$base/shared/.env.tmp") && mv $(printf '%q' "$base/shared/.env.tmp") $(printf '%q' "$base/shared/.env")"
        fi
        {
            printf 'BASE=%q ID=%q SHA=%q REF=%q\n' "$base" "$id" "$sha" "${GITHUB_REF_NAME:-}"
            printf 'BUILD=%q ACTIVATE=%q HEALTH=%q\n' "${DEPLOY_BUILD:-}" "${DEPLOY_ACTIVATE:-}" "${DEPLOY_HEALTHCHECK:-}"
            printf 'RETRIES=%q DELAY=%q KEEP=%q\n' "${DEPLOY_HEALTH_RETRIES:-10}" "${DEPLOY_HEALTH_DELAY:-3}" "${DEPLOY_KEEP:-5}"
            activate_script
        } | remote_script "$target"
        echo '::endgroup::'
    done

    if [ -n "${DEPLOY_PUBLIC_URL:-}" ]; then
        curl -fsS --retry 10 --retry-delay 3 --retry-all-errors -o /dev/null "$DEPLOY_PUBLIC_URL"
        echo "Public check OK: $DEPLOY_PUBLIC_URL"
    fi
}

case $mode in
    deploy) deploy_all ;;
    run) run_all ;;
    *) echo "unknown mode: $mode" >&2; exit 2 ;;
esac
