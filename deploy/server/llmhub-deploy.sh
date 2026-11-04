#!/usr/bin/env bash
# llmhub server steps (T-371 plan §6): llmhub-deploy.sh <step> check|apply|rollback
#
# Steps: s0 preflight snapshot | s1 artifacts | s2 owners+dirs | s3 images | s4 secrets+keys |
#        s5 config | s6 api+reaper+backup timer | s7 ufw API | s8a weights cert |
#        s8b weights config | s8c validator+nginx+logrotate | s9 ufw weights |
#        s10x remove the e2e client | s11 post-check vs s0 | s14 full rollback
# Rules (tested by tests/test_t371_docker_files.py and test_t371_deploy_script.py):
# - docker rm/restart/start/stop only through `dctl`, docker exec only through `dexec`,
#   names `llmhub-*` only; `docker run` only via `drun_once` (--rm, no name), `drun_named`
#   (llmhub-*) and the `nginx -t` dry run (--rm);
# - dockerd, daemon.json, llama-server, foreign containers and foreign ufw rules untouched;
# - secrets are created in files and never printed; only sha256 of keys leave the host;
# - apply is idempotent (a second apply changes nothing), rollback removes what apply made.
# Environment (defaults = the server; the F5 test overrides them):
#   LLMHUB_DIST (dir of this script)  LLMHUB_UFW (ufw)  LLMHUB_SYSTEMCTL (systemctl)
#   LLMHUB_LOGROTATE (logrotate)  LLMHUB_API_HOST (10.8.0.1)  LLMHUB_PUBLIC_IP (65.109.95.35)
#   LLMHUB_VPN_IF (tun0)  LLMHUB_PUBLIC_IF (enp9s0)  LLMHUB_LLAMA_GID (998)
#   LLMHUB_RENDER_SET ("k=v;k=v" extra render --set)  LLMHUB_WEIGHTS_MODEL/_DRAFT (rel paths)
#   LLMHUB_BACKUP_DIR (/root)  LLMHUB_HEALTH_WAIT_S (120)
set -euo pipefail
umask 027

DIST=${LLMHUB_DIST:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}
UFW=${LLMHUB_UFW:-ufw}
SYSTEMCTL=${LLMHUB_SYSTEMCTL:-systemctl}
LOGROTATE=${LLMHUB_LOGROTATE:-logrotate}
API_HOST=${LLMHUB_API_HOST:-10.8.0.1}
API_PORT=8095
AUTH_PORT=8096
PUBLIC_IP=${LLMHUB_PUBLIC_IP:-65.109.95.35}
WEIGHTS_PORT=9443
VPN_IF=${LLMHUB_VPN_IF:-tun0}
VPN_NET=10.8.0.0/24
PUBLIC_IF=${LLMHUB_PUBLIC_IF:-enp9s0}
LLAMA_GID=${LLMHUB_LLAMA_GID:-998}
MODELS=/opt/models
SUMS=$MODELS/sha256-T365.txt
RATE=80m
MAX_DL=1
BACKUP_DIR=${LLMHUB_BACKUP_DIR:-/root}
HEALTH_WAIT_S=${LLMHUB_HEALTH_WAIT_S:-120}

ETC=/etc/llmhub
DATA=/var/lib/llmhub
WAUTH=/var/lib/llmhub-weights-auth
LOGS=/var/log/llmhub
MARK=/var/lib/llmhub-deploy
UNIT_DIR=/etc/systemd/system
LOGROTATE_FILE=/etc/logrotate.d/llmhub-acct

UID_API=64100
UID_NGINX=64101
UID_WAUTH=64102
CONTAINERS=(llmhub-api llmhub-reaper llmhub-weights-auth llmhub-weights-nginx)
CLIENTS=(voicebot gamedev agents e2e)
RULE_API=(in on "$VPN_IF" from "$VPN_NET" to "$API_HOST" port "$API_PORT" proto tcp)
RULE_WEIGHTS=(in on "$PUBLIC_IF" to "$PUBLIC_IP" port "$WEIGHTS_PORT" proto tcp)

log() { printf '%s\n' "llmhub-deploy: $*" >&2; }
die() { log "ERROR: $*"; exit 1; }
fail() { log "check failed: $*"; return 1; }

# ------------------------------------------------------------------ docker wrappers
is_ours() {
    case "$1" in llmhub-api|llmhub-reaper|llmhub-weights-auth|llmhub-weights-nginx) return 0 ;; esac
    return 1
}

dctl() {  # dctl rm|restart|start|stop NAME
    local action=$1 name=$2
    is_ours "$name" || die "refusing docker $action on $name (not llmhub-*)"
    case "$action" in
        rm) docker rm -f "$name" >/dev/null ;;
        restart|start|stop) docker "$action" "$name" >/dev/null ;;
        *) die "dctl: unknown action $action" ;;
    esac
}

dexec() {  # dexec [-i] NAME cmd...
    local opt=()
    if [ "$1" = "-i" ]; then opt=(-i); shift; fi
    local name=$1
    shift
    is_ours "$name" || die "refusing docker exec on $name"
    docker exec "${opt[@]}" "$name" "$@"
}

drun_once() {  # throw-away container from our image: no name, --rm
    docker run --rm --read-only --cap-drop ALL --security-opt no-new-privileges:true \
        --label org.llmhub.tool=1 "$@"
}

image_id() { tr -d '[:space:]' <"$DIST/image.id"; }
nginx_id() { tr -d '[:space:]' <"$DIST/nginx.id"; }
release() { tr -d '[:space:]' <"$DIST/VERSION"; }

exists() { docker inspect -f '{{.Id}}' "$1" >/dev/null 2>&1; }
running() { [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = "true" ]; }
health() {
    docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$1" \
        2>/dev/null || echo missing
}

args_of() {  # args_of FILE -> one argument per line on stdout, placeholders filled
    local file=$1 line
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            '' | '#'*) continue ;;
            '@include '*) args_of "$DIST/run-args/${line#@include }"; continue ;;
        esac
        line=${line//@IMAGE@/$(image_id)}
        line=${line//@NGINX_IMAGE@/$(nginx_id)}
        line=${line//@RELEASE@/$(release)}
        line=${line//@API_HOST@/$API_HOST}
        line=${line//@API_PORT@/$API_PORT}
        line=${line//@LLAMA_GID@/$LLAMA_GID}
        line=${line//@PUBLIC_IP@/$PUBLIC_IP}
        printf '%s\n' "$line"
    done <"$file"
}

mount_sources() {  # host paths of -v in an args file
    local prev="" a
    while IFS= read -r a; do
        if [ "$prev" = "-v" ]; then printf '%s\n' "${a%%:*}"; fi
        prev=$a
    done < <(args_of "$1")
}

drun_named() {  # drun_named NAME: docker run -d from run-args/NAME.args (idempotent)
    local name=$1 file="$DIST/run-args/$1.args" src want img
    is_ours "$name" || die "refusing to run $name"
    while IFS= read -r src; do
        [ -e "$src" ] || die "$name: mount source $src is missing (docker would create a dir)"
    done < <(mount_sources "$file")
    want=$(image_id)
    [ "$name" = llmhub-weights-nginx ] && want=$(nginx_id)
    if exists "$name"; then
        img=$(docker inspect -f '{{.Image}}' "$name")
        [ "$img" = "$want" ] || die "$name runs another image; roll it back first"
        running "$name" || dctl start "$name"
        return 0
    fi
    local args=()
    mapfile -t args < <(args_of "$file")
    docker run -d "${args[@]}" >/dev/null
    log "started $name"
}

wait_healthy() {
    local name t=0
    for name in "$@"; do
        while [ "$(health "$name")" != healthy ]; do
            [ "$t" -lt "$HEALTH_WAIT_S" ] || { fail "$name is $(health "$name")"; return 1; }
            sleep 2
            t=$((t + 2))
        done
    done
}

listening() {  # listening ADDR PORT
    if command -v ss >/dev/null 2>&1; then
        ss -Hltn "( sport = :$2 )" | awk '{print $4}' | grep -qx "$1:$2"
    else
        (exec 3<>"/dev/tcp/$1/$2") 2>/dev/null
    fi
}

no_wildcard() {  # no 0.0.0.0 / [::] listener on PORT
    command -v ss >/dev/null 2>&1 || return 0
    ! ss -Hltn "( sport = :$1 )" | awk '{print $4}' | grep -Eq "^(0\.0\.0\.0|\*|\[::\]):$1$"
}

env_has_no_secret() {  # docker inspect Env must not contain the content of any secret file
    local name=$1 env f
    env=$(docker inspect -f '{{json .Config.Env}}' "$name")
    for f in "$ETC/secrets/label" "$ETC/secrets/instance" "$ETC/secrets/weights"; do
        [ -s "$f" ] || continue
        if grep -qF -- "$(head -n1 "$f")" <<<"$env"; then fail "$name: Env contains a secret"; return 1; fi
    done
    if grep -Eq 'LLMHUB_[A-Z_]*(SECRET|KEY|TOKEN)=' <<<"$env"; then fail "$name: secret-like Env value"; return 1; fi
}

stat_is() {  # stat_is PATH "uid:gid mode"
    local got
    got=$(stat -c '%u:%g %a' "$1" 2>/dev/null) || { fail "$1 missing"; return 1; }
    [ "$got" = "$2" ] || { fail "$1 is $got, want $2"; return 1; }
}

marker() { install -d -m 0700 "$MARK"; [ -f "$MARK/$1.done" ] || date -u +%FT%TZ >"$MARK/$1.done"; }
has_marker() { [ -f "$MARK/$1.done" ]; }
record() { install -d -m 0700 "$MARK"; printf '%s\n' "$2" >>"$MARK/$1.created"; }

# ------------------------------------------------------------------ S0 / S11 snapshots
snapshot() {  # snapshot DIR
    local d=$1 c
    install -d -m 0700 "$d"
    for c in llama-server llama-server-27b llama-server-small; do
        "$SYSTEMCTL" show "$c" -p ActiveState,SubState,ExecMainPID,NRestarts,ExecMainStartTimestamp \
            2>/dev/null || true
    done >"$d/llama.txt"
    {
        find /etc/systemd/system -maxdepth 2 -name 'llama*' -type f -print0 2>/dev/null \
            | sort -z | xargs -0r sha256sum
        find /etc/llama -type f -print0 2>/dev/null | sort -z | xargs -0r sha256sum
    } >"$d/llama-files.txt"
    docker ps -aq --no-trunc | xargs -r docker inspect \
        -f '{{.Name}} {{.Id}} {{.State.StartedAt}} {{.RestartCount}}' \
        | { grep -v '^/llmhub-' || true; } | sort >"$d/containers.txt"
    "$SYSTEMCTL" show docker -p ExecMainPID,ActiveEnterTimestamp >"$d/dockerd.txt" 2>/dev/null || true
    if command -v ss >/dev/null 2>&1; then ss -Hltn | awk '{print $4}' | sort -u; fi >"$d/listen.txt"
    { "$UFW" status numbered 2>/dev/null || true; } | sed 's/^\[ *[0-9]*\] *//' \
        | { grep -v 'llmhub' || true; } | sort >"$d/ufw.txt"
    awk '/^MemAvailable:/ {print $2}' /proc/meminfo >"$d/mem.txt"
}

check_s0() { [ -f "$MARK/preflight/baseline/containers.txt" ] || fail "no baseline snapshot"; }
apply_s0() {
    if [ -f "$MARK/preflight/baseline/containers.txt" ]; then log "baseline exists (kept)"; return 0; fi
    listening "$API_HOST" "$API_PORT" && die "port $API_PORT is busy"
    listening 127.0.0.1 "$AUTH_PORT" && die "port $AUTH_PORT is busy"
    listening "$PUBLIC_IP" "$WEIGHTS_PORT" && die "port $WEIGHTS_PORT is busy"
    local id
    for id in $UID_API $UID_NGINX $UID_WAUTH; do
        if getent passwd "$id" >/dev/null && ! getent passwd "$id" | grep -q '^llmhub'; then
            die "uid $id is taken"
        fi
    done
    install -d -m 0700 "$MARK" "$MARK/preflight"
    snapshot "$MARK/preflight/baseline"
    log "baseline snapshot: $MARK/preflight/baseline"
}
rollback_s0() { log "s0 is read-only"; }

check_s11() {
    check_s0 || return 1
    local now="$MARK/preflight/now" f ok=0
    rm -rf "$now"
    snapshot "$now"
    for f in llama.txt llama-files.txt containers.txt dockerd.txt ufw.txt; do
        if ! cmp -s "$MARK/preflight/baseline/$f" "$now/$f"; then
            fail "$f differs from the baseline" || true
            diff "$MARK/preflight/baseline/$f" "$now/$f" >&2 || true
            ok=1
        fi
    done
    diff "$MARK/preflight/baseline/listen.txt" "$now/listen.txt" >&2 || true  # informational
    return "$ok"
}
apply_s11() { check_s11; }
rollback_s11() { log "s11 is read-only"; }

# ------------------------------------------------------------------ S1 artifacts
check_s1() { (cd "$DIST" && sha256sum --quiet -c SHA256SUMS) || fail "SHA256SUMS"; }
apply_s1() { check_s1; }
rollback_s1() { log "s1: remove $DIST by hand after s14 (the script lives there)"; }

# ------------------------------------------------------------------ S2 owners and dirs
ensure_account() {  # ensure_account ID NAME
    local id=$1 name=$2
    if getent group "$id" >/dev/null; then
        [ "$(getent group "$id" | cut -d: -f1)" = "$name" ] || die "gid $id belongs to another group"
    else
        groupadd -g "$id" "$name"
        record s2 "group $name"
    fi
    if getent passwd "$id" >/dev/null; then
        [ "$(getent passwd "$id" | cut -d: -f1)" = "$name" ] || die "uid $id belongs to another user"
    else
        useradd --system -u "$id" -g "$id" -M -d /nonexistent -s /usr/sbin/nologin "$name"
        record s2 "user $name"
    fi
}

DIRS=(
    "$ETC 0755 0 0"
    "$ETC/secrets 0700 0 0"
    "$ETC/keys 0700 0 0"
    "$ETC/weights 0750 0 $UID_NGINX"
    "$ETC/weights-pub 0755 0 0"
    "$DATA 0750 $UID_API $UID_API"
    "$DATA/backup 0750 $UID_API $UID_API"
    "$WAUTH 0700 $UID_WAUTH $UID_WAUTH"
    "$LOGS 0750 0 $UID_API"
)

check_s2() {
    local pair id name d path mode u g
    for pair in "$UID_API:llmhub" "$UID_NGINX:llmhub-wnginx" "$UID_WAUTH:llmhub-wauth"; do
        id=${pair%%:*}
        name=${pair#*:}
        [ "$(getent passwd "$id" | cut -d: -f1)" = "$name" ] || { fail "user $name"; return 1; }
    done
    for d in "${DIRS[@]}"; do
        read -r path mode u g <<<"$d"
        stat_is "$path" "$u:$g ${mode#0}" || return 1
    done
}
apply_s2() {
    ensure_account $UID_API llmhub
    ensure_account $UID_NGINX llmhub-wnginx
    ensure_account $UID_WAUTH llmhub-wauth
    local d path mode u g
    for d in "${DIRS[@]}"; do
        read -r path mode u g <<<"$d"
        install -d -m "$mode" -o "$u" -g "$g" "$path"
    done
    marker s2
    check_s2
}
rollback_s2() {
    has_marker s2 || { log "s2 marker missing: nothing to undo"; return 0; }
    local c i path kind name
    for c in "${CONTAINERS[@]}"; do exists "$c" && die "container $c exists; roll back s6/s8c first"; done
    for ((i = ${#DIRS[@]} - 1; i >= 0; i--)); do
        read -r path _ <<<"${DIRS[$i]}"
        if [ -d "$path" ]; then rmdir "$path" 2>/dev/null || log "kept non-empty $path"; fi
    done
    if [ -f "$MARK/s2.created" ]; then
        while read -r kind name; do
            if [ "$kind" = user ] && getent passwd "$name" >/dev/null; then userdel "$name"; fi
        done <"$MARK/s2.created"
        while read -r kind name; do
            if [ "$kind" = group ] && getent group "$name" >/dev/null; then groupdel "$name"; fi
        done <"$MARK/s2.created"
        rm -f "$MARK/s2.created"
    fi
    rm -f "$MARK/s2.done"
}

# ------------------------------------------------------------------ S3 images
check_s3() {
    [ "$(docker image inspect -f '{{.Id}}' "$(image_id)" 2>/dev/null)" = "$(image_id)" ] \
        || { fail "llmhub image"; return 1; }
    [ "$(docker image inspect -f '{{.Id}}' "$(nginx_id)" 2>/dev/null)" = "$(nginx_id)" ] \
        || { fail "nginx image"; return 1; }
    local v
    v=$(drun_once --network none --user $UID_API "$(image_id)" \
        python -c 'import llmhub; print(llmhub.__version__)')
    [ "$v" = "$(release)" ] || fail "image version $v != $(release)"
}
apply_s3() {
    if ! docker image inspect "$(image_id)" >/dev/null 2>&1; then
        gzip -dc "$DIST/llmhub-$(release).tar.gz" | docker load >/dev/null
    fi
    if ! docker image inspect "$(nginx_id)" >/dev/null 2>&1; then
        gzip -dc "$DIST/nginx-weights-$(release).tar.gz" | docker load >/dev/null
    fi
    check_s3
}
rollback_s3() {
    local img
    for img in "$(image_id)" "$(nginx_id)"; do
        if [ -n "$(docker ps -aq --filter "ancestor=$img")" ]; then die "image $img is in use"; fi
        docker image rm "$img" >/dev/null 2>&1 || true
    done
}

# ------------------------------------------------------------------ S4 secrets and keys
new_secret() {  # new_secret NAME GID: 48 random bytes, base64, straight into the file
    local f="$ETC/secrets/$1"
    [ -s "$f" ] && return 0
    (umask 077 && head -c 48 /dev/urandom | base64 -w0 >"$f.tmp")
    chown "0:$2" "$f.tmp"
    chmod 0640 "$f.tmp"
    mv "$f.tmp" "$f"
    record s4 "$f"
}

key_hash() { head -n1 "$1" | tr -d '\n' | sha256sum | cut -d' ' -f1; }

new_key() {  # new_key NAME DEST (the key goes into a file; stdout has only its sha256)
    local name=$1 dest=$2
    [ -s "$dest" ] && return 0
    rm -f "$ETC/keys/$name.key.new"
    drun_once --network none --user 0 -v "$ETC/keys:/out" "$(image_id)" \
        python -m llmhub keys new --name "$name" --out "/out/$name.key.new" >/dev/null
    chown 0:0 "$ETC/keys/$name.key.new"
    chmod 0600 "$ETC/keys/$name.key.new"
    mv "$ETC/keys/$name.key.new" "$dest"
    record s4 "$dest"
}

write_hashes() {
    local out="$MARK/hashes.txt" n
    {
        printf 'admin %s\n' "$(key_hash "$ETC/admin.key")"
        for n in "${CLIENTS[@]}"; do
            if [ -s "$ETC/keys/$n.key" ]; then printf '%s %s\n' "$n" "$(key_hash "$ETC/keys/$n.key")"; fi
        done
    } >"$out.tmp"
    chmod 0644 "$out.tmp"
    if cmp -s "$out.tmp" "$out"; then rm -f "$out.tmp"; else mv "$out.tmp" "$out"; fi
}

check_s4() {
    stat_is "$ETC/secrets/label" "0:$UID_API 640" || return 1
    stat_is "$ETC/secrets/instance" "0:$UID_API 640" || return 1
    stat_is "$ETC/secrets/weights" "0:$UID_WAUTH 640" || return 1
    stat_is "$ETC/secrets/providers" "0:0 600" || return 1
    stat_is "$ETC/admin.key" "0:0 600" || return 1
    local a b
    for a in label instance weights; do
        [ "$(head -n1 "$ETC/secrets/$a" | tr -d '\n' | wc -c)" -ge 32 ] || { fail "$a too short"; return 1; }
        for b in label instance weights; do
            [ "$a" = "$b" ] && continue
            if cmp -s "$ETC/secrets/$a" "$ETC/secrets/$b"; then fail "$a equals $b"; return 1; fi
        done
    done
    [ -s "$MARK/hashes.txt" ] || fail "hashes.txt"
}
apply_s4() {
    new_secret label $UID_API
    new_secret instance $UID_API
    new_secret weights $UID_WAUTH
    if [ ! -e "$ETC/secrets/providers" ]; then
        install -m 0600 -o 0 -g 0 /dev/null "$ETC/secrets/providers"
        record s4 "$ETC/secrets/providers"
    fi
    new_key admin "$ETC/admin.key"
    local n
    for n in "${CLIENTS[@]}"; do
        if [ "$n" = e2e ] && has_marker s10x; then continue; fi
        new_key "$n" "$ETC/keys/$n.key"
    done
    write_hashes
    marker s4
    check_s4
}
rollback_s4() {
    [ -f "$MARK/s4.created" ] || { log "s4: nothing created"; return 0; }
    local f
    while read -r f; do if [ -e "$f" ]; then shred -u "$f"; fi; done <"$MARK/s4.created"
    rm -f "$MARK/s4.created" "$MARK/s4.done" "$MARK/hashes.txt"
}

# ------------------------------------------------------------------ S5 / S8b config
render_config() {  # -> $ETC/config.yaml.new
    local sets=() item extra=() weights=()
    IFS=';' read -r -a sets <<<"${LLMHUB_RENDER_SET:-}"
    for item in "${sets[@]}"; do if [ -n "$item" ]; then extra+=(--set "$item"); fi; done
    if has_marker s10x; then extra+=(--drop-client e2e); fi
    if has_marker s8b; then
        weights=(--weights --sums "$SUMS" --models-dir "$MODELS" --public-ip "$PUBLIC_IP"
            --port "$WEIGHTS_PORT" --ca "$ETC/weights-pub/weights-cert.pem"
            --model "$(cat "$MARK/weights.model")")
        if [ -s "$MARK/weights.draft" ]; then weights+=(--draft "$(cat "$MARK/weights.draft")"); fi
    fi
    drun_once --network none --user 0 -v "$ETC:/out" -v "$MARK/hashes.txt:/in/hashes.txt:ro" \
        -v "$MODELS:$MODELS:ro" "$(image_id)" python /opt/llmhub/deploy/render.py config \
        --template /opt/llmhub/deploy/config.example.yaml --out /out/config.yaml.new \
        --hashes /in/hashes.txt "${extra[@]}" "${weights[@]}"
}

install_config() {  # returns 0 when config.yaml changed, 1 when it is the same
    local new="$ETC/config.yaml.new" cur="$ETC/config.yaml"
    if [ -f "$cur" ] && cmp -s "$new" "$cur"; then rm -f "$new"; return 1; fi
    if [ -f "$cur" ]; then cp -p "$cur" "$cur.bak-$(date -u +%Y%m%dT%H%M%S)"; fi
    chown "0:$UID_API" "$new"
    chmod 0640 "$new"
    mv "$new" "$cur"
}

config_check() {
    local out
    out=$(drun_once --network none --user "$UID_API:$UID_API" \
        -v "$ETC/config.yaml:$ETC/config.yaml:ro" \
        -v "$ETC/secrets/label:/run/s/label:ro" -v "$ETC/secrets/instance:/run/s/instance:ro" \
        -e LLMHUB_LABEL_SECRET_FILE=/run/s/label -e LLMHUB_INSTANCE_SECRET_FILE=/run/s/instance \
        "$(image_id)" python -m llmhub config-check --config "$ETC/config.yaml") \
        || { fail "config-check"; return 1; }
    [ "$out" = ok ] || fail "config-check: $out"
}

check_s5() { stat_is "$ETC/config.yaml" "0:$UID_API 640" && config_check; }
apply_s5() {
    render_config
    install_config || log "config unchanged"
    marker s5
    check_s5
}
rollback_s5() {
    local last
    last=$(find "$ETC" -maxdepth 1 -name 'config.yaml.bak-*' | sort | tail -n1)
    if [ -n "$last" ]; then mv "$last" "$ETC/config.yaml"; else rm -f "$ETC/config.yaml"; fi
    rm -f "$MARK/s5.done"
}

# ------------------------------------------------------------------ S6 api + reaper + backup
admin_call() {  # admin_call CMD... (admin key via stdin, never in argv/env)
    dexec -i llmhub-api python -m llmhub "$@" --admin-key-file /dev/stdin <"$ETC/admin.key"
}

check_s6() {
    wait_healthy llmhub-api llmhub-reaper || return 1
    listening "$API_HOST" "$API_PORT" || { fail "API not on $API_HOST:$API_PORT"; return 1; }
    no_wildcard "$API_PORT" || { fail "wildcard listener on $API_PORT"; return 1; }
    dexec llmhub-api python -m llmhub healthcheck --url "http://$API_HOST:$API_PORT/healthz" \
        || { fail "healthz"; return 1; }
    admin_call status >/dev/null || { fail "admin status"; return 1; }
    env_has_no_secret llmhub-api || return 1
    env_has_no_secret llmhub-reaper || return 1
    stat_is "$DATA/state.db" "$UID_API:$UID_API 640" || return 1
    [ -f "$UNIT_DIR/llmhub-backup.timer" ] || fail "backup timer not installed"
}
apply_s6() {
    drun_named llmhub-api
    drun_named llmhub-reaper
    local u changed=0
    for u in llmhub-backup.service llmhub-backup.timer; do
        if cmp -s "$DIST/systemd/$u" "$UNIT_DIR/$u"; then continue; fi
        install -m 0644 -o 0 -g 0 "$DIST/systemd/$u" "$UNIT_DIR/$u"
        changed=1
    done
    if [ "$changed" = 1 ]; then "$SYSTEMCTL" daemon-reload; fi
    "$SYSTEMCTL" enable --now llmhub-backup.timer >/dev/null 2>&1
    marker s6
    check_s6
}
rollback_s6() {
    local c
    for c in llmhub-reaper llmhub-api; do if exists "$c"; then dctl rm "$c"; fi; done
    if [ -f "$UNIT_DIR/llmhub-backup.timer" ]; then
        "$SYSTEMCTL" disable --now llmhub-backup.timer >/dev/null 2>&1 || true
        rm -f "$UNIT_DIR/llmhub-backup.timer" "$UNIT_DIR/llmhub-backup.service"
        "$SYSTEMCTL" daemon-reload
    fi
    rm -f "$MARK/s6.done"
}

# ------------------------------------------------------------------ S7 / S9 ufw
ufw_has() { "$UFW" status 2>/dev/null | grep -qF "# $1"; }
check_s7() { ufw_has "llmhub api" || fail "ufw rule llmhub api"; }
apply_s7() {
    ufw_has "llmhub api" || "$UFW" allow "${RULE_API[@]}" comment 'llmhub api' >/dev/null
    check_s7
}
rollback_s7() { if ufw_has "llmhub api"; then "$UFW" delete allow "${RULE_API[@]}" >/dev/null; fi; }

weights_get() {  # weights_get PATH [tls12] -> HTTP status or "tls_error" (pinned cert, no token)
    drun_once -i --network host --user "$UID_API:$UID_API" \
        -v "$ETC/weights-pub:$ETC/weights-pub:ro" "$(image_id)" \
        python - "$PUBLIC_IP" "$WEIGHTS_PORT" "$1" "${2:-}" <<'PY'
import http.client, ssl, sys
ip, port, path, mode = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
ctx = ssl.create_default_context(cafile="/etc/llmhub/weights-pub/weights-cert.pem")
if mode == "tls12":
    ctx.minimum_version = ctx.maximum_version = ssl.TLSVersion.TLSv1_2
try:
    c = http.client.HTTPSConnection(ip, port, context=ctx, timeout=10)
    c.request("GET", path)
    print(c.getresponse().status)
except (ssl.SSLError, OSError):
    print("tls_error")
PY
}

check_s9() {
    ufw_has "llmhub weights" || { fail "ufw rule llmhub weights"; return 1; }
    local s
    # F9: the path matches the location regex (12 hex op, dir/file.gguf): no token -> 401
    s=$(weights_get /w/000000000000/qwen/x.gguf)
    [ "$s" = 401 ] || { fail "no token: $s, want 401"; return 1; }
    s=$(weights_get /w/000000000000/bench/run.py)
    [ "$s" = 404 ] || [ "$s" = 403 ] || { fail "non-gguf path: $s"; return 1; }
    s=$(weights_get /w/000000000000/qwen/x.gguf tls12)
    [ "$s" = tls_error ] || { fail "TLS 1.2 accepted"; return 1; }
    if "$UFW" status 2>/dev/null | grep -F "# llmhub weights" | grep -q '(v6)'; then
        fail "IPv6 rule present"
        return 1
    fi
}
apply_s9() {
    ufw_has "llmhub weights" || "$UFW" allow "${RULE_WEIGHTS[@]}" comment 'llmhub weights' >/dev/null
    check_s9
}
rollback_s9() {
    if ufw_has "llmhub weights"; then "$UFW" delete allow "${RULE_WEIGHTS[@]}" >/dev/null; fi
}

# ------------------------------------------------------------------ S8 weights
check_s8a() {
    stat_is "$ETC/weights/weights-key.pem" "$UID_NGINX:$UID_NGINX 600" || return 1
    stat_is "$ETC/weights-pub/weights-cert.pem" "0:0 644" || return 1
    local fp
    fp=$(drun_once --network none --user $UID_API -v "$ETC/weights-pub:/c:ro" "$(image_id)" \
        python -c 'import hashlib, ssl; print(hashlib.sha256(ssl.PEM_cert_to_DER_cert(open("/c/weights-cert.pem").read())).hexdigest())')
    [ "$fp" = "$(cat "$MARK/weights.fp")" ] || fail "certificate fingerprint changed"
}
apply_s8a() {
    if [ ! -s "$ETC/weights/weights-key.pem" ]; then
        drun_once --network none --user 0 -v "$ETC/weights:/out" "$(image_id)" \
            python /opt/llmhub/deploy/gen_weights_cert.py "$PUBLIC_IP" /out \
            | tail -n1 >"$MARK/weights.fp"
        chown "$UID_NGINX:$UID_NGINX" "$ETC/weights/weights-key.pem"
        chmod 0600 "$ETC/weights/weights-key.pem"
        chmod 0644 "$ETC/weights/weights-cert.pem"
        install -m 0644 -o 0 -g 0 "$ETC/weights/weights-cert.pem" "$ETC/weights-pub/weights-cert.pem"
        log "weights cert sha256 $(cat "$MARK/weights.fp")"
    fi
    marker s8a
    check_s8a
}
rollback_s8a() {
    rm -f "$ETC/weights/weights-key.pem" "$ETC/weights/weights-cert.pem" \
        "$ETC/weights-pub/weights-cert.pem" "$MARK/weights.fp" "$MARK/s8a.done"
}

check_s8b() {
    grep -q 'kind: self_hosted' "$ETC/config.yaml" || { fail "no weights source in config"; return 1; }
    config_check || return 1
    wait_healthy llmhub-api
}
apply_s8b() {
    if [ -n "${LLMHUB_WEIGHTS_MODEL:-}" ]; then printf '%s\n' "$LLMHUB_WEIGHTS_MODEL" >"$MARK/weights.model"; fi
    if [ -n "${LLMHUB_WEIGHTS_DRAFT:-}" ]; then printf '%s\n' "$LLMHUB_WEIGHTS_DRAFT" >"$MARK/weights.draft"; fi
    [ -s "$MARK/weights.model" ] || die "LLMHUB_WEIGHTS_MODEL is required (path under $MODELS)"
    marker s8b
    render_config
    if install_config; then dctl restart llmhub-api; fi
    check_s8b
}
rollback_s8b() {
    rm -f "$MARK/s8b.done" "$MARK/weights.model" "$MARK/weights.draft"
    render_config
    if install_config && exists llmhub-api; then dctl restart llmhub-api; fi
}

render_nginx_conf() {
    drun_once --network none --user 0 -v "$ETC:/out" "$(image_id)" \
        python /opt/llmhub/deploy/render.py nginx \
        --template /opt/llmhub/deploy/nginx-weights.conf --out /out/nginx-weights.conf.new \
        --var "PUBLIC_IP=$PUBLIC_IP" --var "PORT=$WEIGHTS_PORT" --var "RATE=$RATE" \
        --var SEGMENTS=1 --var "MAX_DL=$MAX_DL" --var "MODELS=$MODELS" \
        --var "AUTH=127.0.0.1:$AUTH_PORT" --var "LOGDIR=$LOGS" \
        --var "CERT=$ETC/weights/weights-cert.pem" --var "KEY=$ETC/weights/weights-key.pem" \
        --var PID=/tmp/nginx.pid --var TMP=/tmp
    chmod 0644 "$ETC/nginx-weights.conf.new"
    if cmp -s "$ETC/nginx-weights.conf.new" "$ETC/nginx-weights.conf"; then
        rm -f "$ETC/nginx-weights.conf.new"
    else
        mv "$ETC/nginx-weights.conf.new" "$ETC/nginx-weights.conf"
    fi
}

check_s8c() {
    wait_healthy llmhub-weights-auth llmhub-weights-nginx || return 1
    listening 127.0.0.1 "$AUTH_PORT" || { fail "validator not on 127.0.0.1:$AUTH_PORT"; return 1; }
    listening "$PUBLIC_IP" "$WEIGHTS_PORT" || { fail "nginx not on $PUBLIC_IP:$WEIGHTS_PORT"; return 1; }
    no_wildcard "$AUTH_PORT" || { fail "wildcard listener on $AUTH_PORT"; return 1; }
    no_wildcard "$WEIGHTS_PORT" || { fail "wildcard listener on $WEIGHTS_PORT"; return 1; }
    stat_is "$LOGS/weights-acct.log" "$UID_NGINX:$UID_API 640" || return 1
    env_has_no_secret llmhub-weights-auth || return 1
    env_has_no_secret llmhub-weights-nginx || return 1
    [ -f "$LOGROTATE_FILE" ] || { fail "logrotate file"; return 1; }
    "$LOGROTATE" -d "$LOGROTATE_FILE" >/dev/null 2>&1 || fail "logrotate -d"
}
apply_s8c() {
    render_nginx_conf
    local f
    for f in weights-acct.log weights-access.log weights-error.log; do
        [ -e "$LOGS/$f" ] || install -m 0640 -o $UID_NGINX -g $UID_API /dev/null "$LOGS/$f"
    done
    docker run --rm --read-only --cap-drop ALL --network none --user "$UID_NGINX:$UID_NGINX" \
        --group-add $UID_API --tmpfs /tmp --entrypoint nginx \
        -v "$ETC/nginx-weights.conf:/etc/nginx-weights/nginx.conf:ro" \
        -v "$ETC/weights:$ETC/weights:ro" -v "$MODELS:$MODELS:ro" -v "$LOGS:$LOGS" \
        "$(nginx_id)" -t -q -e stderr -c /etc/nginx-weights/nginx.conf || die "nginx -t failed"
    drun_named llmhub-weights-auth
    drun_named llmhub-weights-nginx
    if ! cmp -s "$DIST/logrotate-llmhub-acct" "$LOGROTATE_FILE"; then
        install -m 0644 -o 0 -g 0 "$DIST/logrotate-llmhub-acct" "$LOGROTATE_FILE"
    fi
    marker s8c
    check_s8c
}
rollback_s8c() {
    local c
    for c in llmhub-weights-nginx llmhub-weights-auth; do if exists "$c"; then dctl rm "$c"; fi; done
    rm -f "$LOGROTATE_FILE" "$ETC/nginx-weights.conf" "$MARK/s8c.done"
}

# ------------------------------------------------------------------ S10x: drop the e2e client
check_s10x() {
    if grep -q '^  e2e:' "$ETC/config.yaml"; then fail "e2e still in config"; return 1; fi
    [ ! -e "$ETC/keys/e2e.key" ] || fail "e2e key still on disk"
}
apply_s10x() {
    marker s10x
    render_config
    install_config || true
    admin_call reload-clients >/dev/null
    if [ -e "$ETC/keys/e2e.key" ]; then shred -u "$ETC/keys/e2e.key"; fi
    write_hashes
    check_s10x
}
rollback_s10x() {
    log "s10x: to re-add e2e: rm $MARK/s10x.done; s4 apply; s5 apply; reload-clients"
}

# ------------------------------------------------------------------ S14 full rollback
check_s14() {
    local c
    for c in "${CONTAINERS[@]}"; do if exists "$c"; then fail "$c exists"; return 1; fi; done
    for c in "$ETC" "$DATA" "$WAUTH" "$LOGS"; do if [ -e "$c" ]; then fail "$c exists"; return 1; fi; done
    if getent passwd $UID_API >/dev/null; then fail "user llmhub exists"; return 1; fi
    if ufw_has "llmhub api" || ufw_has "llmhub weights"; then fail "ufw rule left"; return 1; fi
    check_s11
}
apply_s14() {
    # F13: archive, then remove dirs and users. A reinstall recreates the fixed ids
    # 64100-64102, so files restored from the archive keep valid owners.
    local c ts d dirs=() name
    for c in llmhub-weights-nginx llmhub-weights-auth llmhub-reaper llmhub-api; do
        if exists "$c"; then dctl rm "$c"; fi
    done
    rollback_s9
    rollback_s7
    if [ -f "$UNIT_DIR/llmhub-backup.timer" ]; then
        "$SYSTEMCTL" disable --now llmhub-backup.timer >/dev/null 2>&1 || true
        rm -f "$UNIT_DIR/llmhub-backup.timer" "$UNIT_DIR/llmhub-backup.service"
        "$SYSTEMCTL" daemon-reload
    fi
    rm -f "$LOGROTATE_FILE"
    ts=$(date -u +%Y%m%dT%H%M%S)
    for d in "$ETC" "$DATA" "$WAUTH" "$LOGS"; do if [ -e "$d" ]; then dirs+=("$d"); fi; done
    if [ "${#dirs[@]}" -gt 0 ]; then
        (umask 077 && tar czf "$BACKUP_DIR/llmhub-rollback-$ts.tgz" "${dirs[@]}" 2>/dev/null)
        chmod 0600 "$BACKUP_DIR/llmhub-rollback-$ts.tgz"
        log "archive: $BACKUP_DIR/llmhub-rollback-$ts.tgz"
        rm -rf "${dirs[@]}"
    fi
    rollback_s3
    for name in llmhub-wauth llmhub-wnginx llmhub; do
        if getent passwd "$name" >/dev/null; then userdel "$name"; fi
        if getent group "$name" >/dev/null; then groupdel "$name"; fi
    done
    find "$MARK" -maxdepth 1 -type f -delete
    check_s14
}
rollback_s14() { log "s14: restore = tar xzf the archive (fixed uids), then s2..s9 apply"; }

# ------------------------------------------------------------------ main
STEPS="s0 s1 s2 s3 s4 s5 s6 s7 s8a s8b s8c s9 s10x s11 s14"
usage() { echo "usage: $0 <${STEPS// /|}> check|apply|rollback" >&2; exit 2; }
[ $# -eq 2 ] || usage
case " $STEPS " in *" $1 "*) ;; *) usage ;; esac
case "$2" in check | apply | rollback) ;; *) usage ;; esac
[ "$(id -u)" -eq 0 ] || die "run as root"
"$2_$1"
