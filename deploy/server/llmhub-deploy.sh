#!/usr/bin/env bash
# llmhub server steps (T-371 plan §6): llmhub-deploy.sh <step> check|apply|rollback
#
# Steps: s0 preflight snapshot | s1 artifacts | s2 owners+dirs | s3 images | s4 secrets+keys |
#        s5 config | s6 api+reaper+backup timer | s6k RunPod key (T-372) | s7 ufw API | s8a weights cert |
#        s8b weights config | s8c validator+nginx+logrotate | s9 ufw weights |
#        s10x remove the e2e client | s11 post-check vs s0 | s14 full rollback
# Rules (tested by tests/test_t371_docker_files.py and test_t371_deploy_script.py):
# - docker rm/restart/start/stop only through `dctl`, docker exec only through `dexec`,
#   names `llmhub-*` only; `docker run` only via `drun_once` (--rm, no name), `drun_named`
#   (llmhub-*) and the `nginx -t` dry run (--rm);
# - dockerd, daemon.json, llama-server, foreign containers and foreign ufw rules untouched;
# - secrets are created in files and never printed; only sha256 of keys leave the host;
# - apply is idempotent (a second apply changes nothing), rollback removes what apply made;
# - the config lives in the DIRECTORY $ETC/conf (mounted as a dir: a rename inside it is seen by
#   running containers, SEC-D2); a changed config restarts api/reaper/validator;
# - renders run as $UID_API into a private staging dir, never as root over $ETC (L6);
# - ufw rules and accounts are removed only when this script recorded creating them (L1, L2);
# - T-372: the RunPod key ($ETC/secrets/runpod, one line, 0400 64100:0) is delivered by the
#   operator over ssh stdin, never by this script; it is mounted ro ONLY into api and reaper,
#   and only while the file is non-empty (`@include-if-secret`); s6k applies/rotates/removes it.
# Environment (defaults = the server; the F5 test overrides them):
#   LLMHUB_DIST (dir of this script)  LLMHUB_UFW (ufw)  LLMHUB_SYSTEMCTL (systemctl)
#   LLMHUB_LOGROTATE (logrotate)  LLMHUB_API_HOST (10.8.0.1)  LLMHUB_PUBLIC_IP (65.109.95.35)
#   LLMHUB_VPN_IF (tun0)  LLMHUB_PUBLIC_IF (enp9s0)  LLMHUB_LLAMA_GID (998)
#   LLMHUB_RENDER_SET ("k=v;k=v" extra render --set)  LLMHUB_WEIGHTS_MODEL/_DRAFT (rel paths)
#   LLMHUB_BACKUP_DIR (/root)  LLMHUB_HEALTH_WAIT_S (120)
#   T-372: LLMHUB_CONFIG_TEMPLATE (config.example.yaml | config.t372.example.yaml)
#   LLMHUB_WEIGHTS_CHAIN (e.g. hf or self_hosted,hf)  LLMHUB_HF_BASE_URL (hf source base_url)
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
CONF=$ETC/conf
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
CONFIG_READERS=(llmhub-api llmhub-reaper llmhub-weights-auth)
STAGE=$MARK/stage
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

# R3-5: dist has the index digest (<kind>.id: containerd image store) and the config digest
# (<kind>.config-id: classic graphdriver store); s3 records the Id the local dockerd reports
release() { tr -d '[:space:]' <"$DIST/VERSION"; }
local_id_file() { printf '%s/%s-%s.local-id' "$MARK" "$1" "$(release)"; }  # KIND = image|nginx
dist_ids() {  # dist_ids KIND -> the accepted Ids, one per line
    tr -d '[:space:]' <"$DIST/$1.id"
    printf '\n'
    if [ -f "$DIST/$1.config-id" ]; then tr -d '[:space:]' <"$DIST/$1.config-id"; printf '\n'; fi
}
is_dist_id() { [ -n "$2" ] && grep -qxF -- "$2" < <(dist_ids "$1"); }  # is_dist_id KIND ID
our_id() {  # our_id KIND: the Id recorded by s3 (verified against dist), else dist <kind>.id
    local f got
    f=$(local_id_file "$1")
    if [ -s "$f" ]; then
        got=$(tr -d '[:space:]' <"$f")
        is_dist_id "$1" "$got" || die "$f: $got is not an Id of dist $1"
        printf '%s' "$got"
        return 0
    fi
    tr -d '[:space:]' <"$DIST/$1.id"
}
image_id() { our_id image; }
nginx_id() { our_id nginx; }
# SEC-D5: our images are addressed by OUR tags (the nginx Id equals upstream nginx:1.27-alpine)
image_tag() { printf 'llmhub:%s' "$(release)"; }
nginx_tag() { printf 'llmhub-nginx:%s' "$(release)"; }
tag_id() { docker image inspect -f '{{.Id}}' "$1" 2>/dev/null || true; }

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
            '@include-if-secret '*)  # @include-if-secret NAME FILE: only for a non-empty secret
                local rest=${line#@include-if-secret } sname sfile
                sname=${rest%% *}
                sfile=${rest#* }
                if [ -s "$ETC/secrets/$sname" ]; then args_of "$DIST/run-args/$sfile"; fi
                continue ;;
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

is_wildcard() { grep -Eq '^(0\.0\.0\.0|\*|\[::\]|\[::ffff:0\.0\.0\.0\]):[0-9]+$' <<<"$1"; }

stage_dir() {  # fresh private staging dir for a render (owned by $UID_API, 0700)
    rm -rf "$STAGE"
    install -d -m 0700 "$MARK"
    install -d -m 0700 -o "$UID_API" -g "$UID_API" "$STAGE"
}

restart_config_readers() {  # SEC-D2: settings other than clients are read at start only
    local c restarted=()
    for c in "${CONFIG_READERS[@]}"; do
        if exists "$c"; then dctl restart "$c"; restarted+=("$c"); fi
    done
    if [ "${#restarted[@]}" -gt 0 ]; then
        log "config changed: restarted ${restarted[*]}"
        wait_healthy "${restarted[@]}"
    fi
}

daemon_reload() {  # I1: never activate pending edits of llama units as a side effect
    local out
    out=$("$SYSTEMCTL" show llama-server llama-server-27b llama-server-small -p NeedDaemonReload \
        2>/dev/null || true)
    if grep -q '=yes' <<<"$out"; then die "a llama unit has NeedDaemonReload=yes; reload by hand first"; fi
    "$SYSTEMCTL" daemon-reload
}

env_has_no_secret() {  # docker inspect Env must not contain the content of any secret file
    local name=$1 env f
    env=$(docker inspect -f '{{json .Config.Env}}' "$name")
    for f in "$ETC/secrets/label" "$ETC/secrets/instance" "$ETC/secrets/weights" \
        "$ETC/secrets/runpod"; do
        [ -s "$f" ] || continue
        # SEC-D1: the secret is the pattern FILE, never an argument (ps, /proc/*/cmdline)
        if grep -qF -f "$f" <<<"$env"; then fail "$name: Env contains a secret"; return 1; fi
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
    local id p
    for p in "$API_PORT" "$AUTH_PORT" "$WEIGHTS_PORT"; do  # L4
        no_wildcard "$p" || die "a wildcard listener holds port $p"
        ufw_foreign "$p" && die "ufw already has a foreign rule for port $p"
    done
    for id in $UID_API $UID_NGINX $UID_WAUTH; do
        if getent passwd "$id" >/dev/null && ! getent passwd "$id" | grep -q '^llmhub'; then
            die "uid $id is taken"
        fi
    done
    # R3-4: renders run as $UID_API and read $SUMS + stat the GGUF (s8b); window 1 does not
    # need it, so only warn (o+x on the dirs, o+r on the file)
    for p in /opt "$MODELS" "$SUMS"; do
        local mode need=1 what=o+x
        [ "$p" = "$SUMS" ] && { need=4; what=o+r; }
        if ! mode=$(stat -c %a "$p" 2>/dev/null); then log "WARN: $p is missing (needed by s8b)"; continue; fi
        [ $((8#$mode & need)) -ne 0 ] || log "WARN: $p lacks $what (render as uid $UID_API fails in s8b)"
    done
    install -d -m 0700 "$MARK" "$MARK/preflight"
    snapshot "$MARK/preflight/baseline"
    # R3-5: record the image store (graphdriver vs containerd snapshotter) next to the baseline
    docker info -f '{{.Driver}} {{json .DriverStatus}}' >"$MARK/preflight/baseline/docker-store.txt" 2>&1 || true
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
    new_listeners "$MARK/preflight/baseline/listen.txt" "$now/listen.txt" || ok=1
    return "$ok"
}

new_listeners() {  # L3: new wildcard listeners or a wrong address on our ports -> fail
    local base=$1 now=$2 a port bad=0
    while IFS= read -r a; do
        [ -n "$a" ] || continue
        port=${a##*:}
        if is_wildcard "$a"; then
            fail "new wildcard listener $a" || true
            bad=1
            continue
        fi
        case "$port" in
            "$API_PORT") [ "$a" = "$API_HOST:$API_PORT" ] || { fail "unexpected listener $a" || true; bad=1; } ;;
            "$AUTH_PORT") [ "$a" = "127.0.0.1:$AUTH_PORT" ] || { fail "unexpected listener $a" || true; bad=1; } ;;
            "$WEIGHTS_PORT") [ "$a" = "$PUBLIC_IP:$WEIGHTS_PORT" ] || { fail "unexpected listener $a" || true; bad=1; } ;;
            *) log "new listener (not ours, informational): $a" ;;
        esac
    done < <(comm -13 <(sort -u "$base") <(sort -u "$now"))
    return "$bad"
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
    "$CONF 0750 0 $UID_API"
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
    remove_recorded_accounts
    rm -f "$MARK/s2.done"
}
remove_recorded_accounts() {  # users/groups listed in s2.created (and only llmhub* names)
    local kind name
    [ -f "$MARK/s2.created" ] || { log "no s2.created: accounts kept"; return 0; }
    while read -r kind name; do
        case "$name" in llmhub|llmhub-wnginx|llmhub-wauth) ;; *) continue ;; esac
        if [ "$kind" = user ] && getent passwd "$name" >/dev/null; then userdel "$name"; fi
    done <"$MARK/s2.created"
    while read -r kind name; do
        case "$name" in llmhub|llmhub-wnginx|llmhub-wauth) ;; *) continue ;; esac
        if [ "$kind" = group ] && getent group "$name" >/dev/null; then groupdel "$name"; fi
    done <"$MARK/s2.created"
    rm -f "$MARK/s2.created"
}

# ------------------------------------------------------------------ S3 images
check_s3() {
    [ "$(tag_id "$(image_tag)")" = "$(image_id)" ] || { fail "$(image_tag) != image.id"; return 1; }
    [ "$(tag_id "$(nginx_tag)")" = "$(nginx_id)" ] || { fail "$(nginx_tag) != nginx.id"; return 1; }
    local v
    v=$(drun_once --network none --user $UID_API "$(image_id)" \
        python -c 'import llmhub; print(llmhub.__version__)')
    [ "$v" = "$(release)" ] || fail "image version $v != $(release)"
}
load_tagged() {  # load_tagged KIND TAG TARBALL: load unless TAG exists; record its local Id
    local kind=$1 tag=$2 tar=$3 got
    got=$(tag_id "$tag")
    if [ -z "$got" ]; then
        gzip -dc "$tar" | docker load >/dev/null
        got=$(tag_id "$tag")
    fi
    # R3-5: index digest (containerd store) or config digest (graphdriver store) of dist
    is_dist_id "$kind" "$got" || die "$tag is $got, want one of: $(dist_ids "$kind" | tr '\n' ' ')(foreign image under our tag?)"
    install -d -m 0700 "$MARK"
    printf '%s\n' "$got" >"$(local_id_file "$kind")"
}
apply_s3() {
    load_tagged image "$(image_tag)" "$DIST/llmhub-$(release).tar.gz"
    load_tagged nginx "$(nginx_tag)" "$DIST/nginx-weights-$(release).tar.gz"
    check_s3
}
rollback_s3() {
    # only our tags: an image that also carries a foreign tag (or backs a foreign container)
    # stays; docker refuses to remove the last tag of an image a container uses (no -f)
    local c kind tag got
    for c in "${CONTAINERS[@]}"; do exists "$c" && die "container $c exists; roll back s6/s8c first"; done
    for kind in image nginx; do
        tag="llmhub:$(release)"
        [ "$kind" = nginx ] && tag="llmhub-nginx:$(release)"
        got=$(tag_id "$tag")
        if [ -z "$got" ]; then :
        elif ! is_dist_id "$kind" "$got"; then
            log "kept $tag ($got is not our image)"  # R3-2: never untag a foreign image
        else
            docker image rm "$tag" >/dev/null 2>&1 || log "kept $tag (in use by a foreign container)"
        fi
        rm -f "$(local_id_file "$kind")"
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
    stage_dir  # L6: the generator sees only an empty private dir, never the other keys
    drun_once --network none --user "$UID_API:$UID_API" -v "$STAGE:/out" "$(image_id)" \
        python -m llmhub keys new --name "$name" --out "/out/$name.key" >/dev/null
    install -m 0600 -o 0 -g 0 "$STAGE/$name.key" "$ETC/keys/$name.key.new"
    rm -rf "$STAGE"
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
    if [ -e "$ETC/secrets/runpod" ]; then stat_is "$ETC/secrets/runpod" "$UID_API:0 400" || return 1; fi
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
config_template() {  # T-372: LLMHUB_CONFIG_TEMPLATE picks one of the shipped templates
    case "${LLMHUB_CONFIG_TEMPLATE:-config.example.yaml}" in
        config.example.yaml | config.t372.example.yaml) printf '%s' "${LLMHUB_CONFIG_TEMPLATE:-config.example.yaml}" ;;
        *) die "LLMHUB_CONFIG_TEMPLATE must be config.example.yaml or config.t372.example.yaml" ;;
    esac
}

render_config() {  # -> $CONF/config.yaml.new (rendered by $UID_API in $STAGE, L6)
    local sets=() item extra=() weights=() tpl
    tpl=$(config_template)
    if [ -n "${LLMHUB_WEIGHTS_CHAIN:-}" ]; then
        [[ "$LLMHUB_WEIGHTS_CHAIN" =~ ^[a-z_]+(,[a-z_]+)*$ ]] || die "bad LLMHUB_WEIGHTS_CHAIN"
        extra+=(--chain "$LLMHUB_WEIGHTS_CHAIN")
    fi
    if [ -n "${LLMHUB_HF_BASE_URL:-}" ]; then extra+=(--hf-base-url "$LLMHUB_HF_BASE_URL"); fi
    IFS=';' read -r -a sets <<<"${LLMHUB_RENDER_SET:-}"
    for item in "${sets[@]}"; do if [ -n "$item" ]; then extra+=(--set "$item"); fi; done
    if has_marker s10x; then extra+=(--drop-client e2e); fi
    if has_marker s8b; then
        weights=(--weights --sums "$SUMS" --models-dir "$MODELS" --public-ip "$PUBLIC_IP"
            --port "$WEIGHTS_PORT" --ca "$ETC/weights-pub/weights-cert.pem"
            --model "$(cat "$MARK/weights.model")")
        if [ -s "$MARK/weights.draft" ]; then weights+=(--draft "$(cat "$MARK/weights.draft")"); fi
    fi
    stage_dir
    local pub=()
    if [ -d "$ETC/weights-pub" ]; then pub=(-v "$ETC/weights-pub:$ETC/weights-pub:ro"); fi
    drun_once --network none --user "$UID_API:$UID_API" -v "$STAGE:/out" \
        -v "$MARK/hashes.txt:/in/hashes.txt:ro" -v "$MODELS:$MODELS:ro" "${pub[@]}" "$(image_id)" \
        python /opt/llmhub/deploy/render.py config \
        --template "/opt/llmhub/deploy/$tpl" --out /out/config.yaml \
        --hashes /in/hashes.txt "${extra[@]}" "${weights[@]}"
    cp "$STAGE/config.yaml" "$CONF/config.yaml.new"
    rm -rf "$STAGE"
}

install_config() {  # returns 0 when config.yaml changed, 1 when it is the same
    # rename INSIDE the mounted directory: running containers see the new file (SEC-D2)
    local new="$ETC/conf/config.yaml.new" cur="$ETC/conf/config.yaml"
    if [ -f "$cur" ] && cmp -s "$new" "$cur"; then rm -f "$new"; return 1; fi
    if [ -f "$cur" ]; then cp -p "$cur" "$ETC/config.yaml.bak-$(date -u +%Y%m%dT%H%M%S)"; fi
    chown "0:$UID_API" "$new"
    chmod 0640 "$new"
    mv "$new" "$cur"
}

install_checked_config() {  # R3-1: config-check the rendered .new BEFORE it becomes live
    local new="$CONF/config.yaml.new"
    if ! cmp -s "$new" "$CONF/config.yaml"; then
        chown "0:$UID_API" "$new"  # readable by the checker ($UID_API), like the live file
        chmod 0640 "$new"
        config_check "$new" || { rm -f "$new"; die "rendered config fails config-check; live config kept"; }
    fi
    install_config
}

config_check() {  # config_check [PATH under $CONF] (default: the live config.yaml)
    local out path=${1:-$CONF/config.yaml} rp=()
    if [ -s "$ETC/secrets/runpod" ]; then  # T-372: config-check reads the key file (never prints it)
        rp=(-v "$ETC/secrets/runpod:/run/s/runpod:ro" -e LLMHUB_RUNPOD_API_KEY_FILE=/run/s/runpod)
    fi
    out=$(drun_once --network none --user "$UID_API:$UID_API" \
        -v "$CONF:$CONF:ro" \
        -v "$ETC/secrets/label:/run/s/label:ro" -v "$ETC/secrets/instance:/run/s/instance:ro" \
        -e LLMHUB_LABEL_SECRET_FILE=/run/s/label -e LLMHUB_INSTANCE_SECRET_FILE=/run/s/instance \
        "${rp[@]}" "$(image_id)" python -m llmhub config-check --config "$path") \
        || { fail "config-check"; return 1; }
    [ "$out" = ok ] || fail "config-check: $out"
}

check_s5() { stat_is "$CONF/config.yaml" "0:$UID_API 640" && config_check; }
apply_s5() {
    render_config
    if install_checked_config; then restart_config_readers; else log "config unchanged"; fi
    marker s5
    check_s5
}
rollback_s5() {
    local last
    last=$(find "$ETC" -maxdepth 1 -name 'config.yaml.bak-*' | sort | tail -n1)
    if [ -n "$last" ]; then
        cp -p "$last" "$CONF/config.yaml.new"
        mv "$CONF/config.yaml.new" "$CONF/config.yaml"  # rename inside the mounted dir
        rm -f "$last"
        restart_config_readers
    else
        rm -f "$CONF/config.yaml"
    fi
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
    if [ "$changed" = 1 ]; then daemon_reload; fi
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
        daemon_reload
    fi
    rm -f "$MARK/s6.done"
}

# ------------------------------------------------------------------ S6k RunPod key (T-372)
RUNPOD_KEY_DEST=/run/llmhub-secrets/runpod
has_runpod_mount() {  # has_runpod_mount NAME
    docker inspect -f '{{range .Mounts}}{{.Destination}}{{"\n"}}{{end}}' "$1" 2>/dev/null \
        | grep -qxF "$RUNPOD_KEY_DEST"
}
runpod_mounts_match() {  # the key is mounted in api+reaper iff the file is non-empty
    local c want=0
    if [ -s "$ETC/secrets/runpod" ]; then want=1; fi
    for c in llmhub-api llmhub-reaper; do
        exists "$c" || { fail "$c missing"; return 1; }
        if has_runpod_mount "$c"; then
            [ "$want" = 1 ] || { fail "$c mounts the RunPod key but the file is gone"; return 1; }
        else
            [ "$want" = 0 ] || { fail "$c lacks the RunPod key mount (s6k apply)"; return 1; }
        fi
    done
    for c in llmhub-weights-auth llmhub-weights-nginx; do
        if exists "$c" && has_runpod_mount "$c"; then fail "$c mounts the RunPod key"; return 1; fi
    done
}
recreate_api_reaper() {  # new containers pick up the (changed) mount list from run-args
    local c
    for c in llmhub-reaper llmhub-api; do if exists "$c"; then dctl rm "$c"; fi; done
    drun_named llmhub-api
    drun_named llmhub-reaper
    wait_healthy llmhub-api llmhub-reaper
}
check_s6k() {
    if [ -e "$ETC/secrets/runpod" ]; then
        stat_is "$ETC/secrets/runpod" "$UID_API:0 400" || return 1
        [ "$(head -n1 "$ETC/secrets/runpod" | tr -d '\n' | wc -c)" -ge 16 ] || { fail "runpod key too short"; return 1; }
    fi
    runpod_mounts_match || return 1
    env_has_no_secret llmhub-api || return 1
    env_has_no_secret llmhub-reaper
}
apply_s6k() {
    # the operator delivers the key first (value never in argv/env/logs):
    #   ssh root@HOST 'install -m 0400 -o 64100 -g 0 /dev/stdin /etc/llmhub/secrets/runpod' < KEYFILE
    [ -s "$ETC/secrets/runpod" ] || die "no $ETC/secrets/runpod; deliver the key over ssh stdin first"
    stat_is "$ETC/secrets/runpod" "$UID_API:0 400" || die "fix owner/mode of the RunPod key"
    # a replaced file is a new inode: a bind mount keeps the old one, so always recreate
    recreate_api_reaper
    check_s6k
}
rollback_s6k() {
    if [ -e "$ETC/secrets/runpod" ]; then shred -u "$ETC/secrets/runpod"; fi
    recreate_api_reaper
    check_s6k
}

# ------------------------------------------------------------------ S7 / S9 ufw
ufw_has() { "$UFW" status 2>/dev/null | grep -qF "# $1"; }
ufw_foreign() {  # a rule for PORT without our comment (allow would merge into it, L1)
    "$UFW" status 2>/dev/null | grep -v '# llmhub' | grep -Eq "(^|[^0-9])$1([^0-9]|$)"
}
ufw_add() {  # ufw_add TAG COMMENT PORT SPEC...: add once, record that WE added it
    local tag=$1 comment=$2 port=$3
    shift 3
    ufw_has "$comment" && return 0
    ufw_foreign "$port" && die "ufw has a foreign rule for port $port; resolve it by hand"
    "$UFW" allow "$@" comment "$comment" >/dev/null
    install -d -m 0700 "$MARK"
    : >"$MARK/ufw-$tag.added"
}
ufw_remove() {  # ufw_remove TAG COMMENT SPEC...: delete only a rule this script added
    local tag=$1 comment=$2
    shift 2
    if [ -f "$MARK/ufw-$tag.added" ] && ufw_has "$comment"; then
        "$UFW" delete allow "$@" >/dev/null
    elif ufw_has "$comment"; then
        log "ufw rule '$comment' was not added by this script: kept"
    fi
    rm -f "$MARK/ufw-$tag.added"
}
check_s7() { ufw_has "llmhub api" || fail "ufw rule llmhub api"; }
apply_s7() {
    ufw_add api 'llmhub api' "$API_PORT" "${RULE_API[@]}"
    check_s7
}
rollback_s7() { ufw_remove api 'llmhub api' "${RULE_API[@]}"; }

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
    ufw_add weights 'llmhub weights' "$WEIGHTS_PORT" "${RULE_WEIGHTS[@]}"
    check_s9
    # L7: this check runs on the host itself (lo); the ufw rule on the public interface is
    # proven only from OUTSIDE - docs/runbook.md section 6 ("S9 from an external host")
    log "S9: now repeat the probe from an external host (docs/runbook.md section 6)"
}
rollback_s9() { ufw_remove weights 'llmhub weights' "${RULE_WEIGHTS[@]}"; }

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
    grep -q 'kind: self_hosted' "$CONF/config.yaml" || { fail "no weights source in config"; return 1; }
    config_check || return 1
    wait_healthy llmhub-api
}
apply_s8b() {
    if [ -n "${LLMHUB_WEIGHTS_MODEL:-}" ]; then printf '%s\n' "$LLMHUB_WEIGHTS_MODEL" >"$MARK/weights.model"; fi
    if [ -n "${LLMHUB_WEIGHTS_DRAFT:-}" ]; then printf '%s\n' "$LLMHUB_WEIGHTS_DRAFT" >"$MARK/weights.draft"; fi
    [ -s "$MARK/weights.model" ] || die "LLMHUB_WEIGHTS_MODEL is required (path under $MODELS)"
    marker s8b
    render_config
    if install_checked_config; then restart_config_readers; fi
    check_s8b
}
rollback_s8b() {
    rm -f "$MARK/s8b.done" "$MARK/weights.model" "$MARK/weights.draft"
    render_config
    if install_checked_config; then restart_config_readers; fi
}

render_nginx_conf() {  # -> $ETC/nginx-weights.conf.new; 0 when it differs from the live conf
    stage_dir
    drun_once --network none --user "$UID_API:$UID_API" -v "$STAGE:/out" "$(image_id)" \
        python /opt/llmhub/deploy/render.py nginx \
        --template /opt/llmhub/deploy/nginx-weights.conf --out /out/nginx-weights.conf \
        --var "PUBLIC_IP=$PUBLIC_IP" --var "PORT=$WEIGHTS_PORT" --var "RATE=$RATE" \
        --var SEGMENTS=1 --var "MAX_DL=$MAX_DL" --var "MODELS=$MODELS" \
        --var "AUTH=127.0.0.1:$AUTH_PORT" --var "LOGDIR=$LOGS" \
        --var "CERT=$ETC/weights/weights-cert.pem" --var "KEY=$ETC/weights/weights-key.pem" \
        --var PID=/tmp/nginx.pid --var TMP=/tmp
    install -m 0644 -o 0 -g 0 "$STAGE/nginx-weights.conf" "$ETC/nginx-weights.conf.new"
    rm -rf "$STAGE"
    if cmp -s "$ETC/nginx-weights.conf.new" "$ETC/nginx-weights.conf"; then
        rm -f "$ETC/nginx-weights.conf.new"
        return 1
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
    local changed=0 conf="$ETC/nginx-weights.conf"
    # R3-3: a changed conf is dry-run as .new and replaces the live file only after `nginx -t`
    if render_nginx_conf; then changed=1; conf="$ETC/nginx-weights.conf.new"; fi
    local f
    for f in weights-acct.log weights-access.log weights-error.log; do
        [ -e "$LOGS/$f" ] || install -m 0640 -o $UID_NGINX -g $UID_API /dev/null "$LOGS/$f"
    done
    # L5: dry run hardened like the service; logs go to a throw-away tmpfs, never $LOGS.
    # `nginx -t` binds its listen sockets: with --network none the public IP does not exist
    # (and with host network the live nginx already holds the port), so the dry-run copy
    # listens on the container's own loopback instead. Only the listen address differs.
    local dry rc=0
    dry=$(mktemp "$MARK/nginx-dry.XXXXXX")
    sed "s/listen $PUBLIC_IP:$WEIGHTS_PORT /listen 127.0.0.1:$WEIGHTS_PORT /" "$conf" >"$dry"
    chmod 0644 "$dry"
    docker run --rm --read-only --cap-drop ALL --security-opt no-new-privileges:true \
        --pids-limit 64 --network none --user "$UID_NGINX:$UID_NGINX" \
        --group-add $UID_API --tmpfs /tmp --tmpfs "$LOGS:rw,noexec,nosuid,size=1m,mode=1777" \
        --entrypoint nginx \
        -v "$dry:/etc/nginx-weights/nginx.conf:ro" \
        -v "$ETC/weights:$ETC/weights:ro" -v "$MODELS:$MODELS:ro" \
        "$(nginx_id)" -t -q -e stderr -c /etc/nginx-weights/nginx.conf || rc=1
    rm -f "$dry"
    [ $rc = 0 ] || { rm -f "$ETC/nginx-weights.conf.new"; die "nginx -t failed (live conf kept)"; }
    if [ "$changed" = 1 ]; then mv "$ETC/nginx-weights.conf.new" "$ETC/nginx-weights.conf"; fi
    # a single-file mount keeps the old inode: a changed conf needs a restart (SEC-D2)
    if [ "$changed" = 1 ] && exists llmhub-weights-nginx; then dctl restart llmhub-weights-nginx; fi
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
key_status() {  # key_status KEYFILE -> HTTP status of GET /v1/models with that key (stdin)
    dexec -i llmhub-api python -c "import sys, httpx; k = sys.stdin.readline().strip(); print(
httpx.get('http://$API_HOST:$API_PORT/v1/models', headers={'Authorization': 'Bearer ' + k},
          timeout=10).status_code)" <"$1"
}

check_s10x() {
    if grep -q '^  e2e:' "$CONF/config.yaml"; then fail "e2e still in config"; return 1; fi
    [ ! -e "$ETC/keys/e2e.key" ] || { fail "e2e key still on disk"; return 1; }
    if grep -q '^e2e ' "$MARK/hashes.txt"; then fail "e2e still in hashes.txt"; return 1; fi
    # SEC-D2: the RUNNING API must not know the client any more (not only the file on disk)
    local list
    list=$(admin_call client list) || { fail "admin client list"; return 1; }
    if grep -q '"name": *"e2e"' <<<"$list"; then fail "running API still has client e2e"; return 1; fi
}
apply_s10x() {
    marker s10x
    render_config
    if install_checked_config; then restart_config_readers; fi
    admin_call reload-clients >/dev/null
    if [ -e "$ETC/keys/e2e.key" ]; then
        local st
        st=$(key_status "$ETC/keys/e2e.key")
        [ "$st" = 401 ] || die "the running API still accepts the e2e key (HTTP $st)"
        shred -u "$ETC/keys/e2e.key"
    fi
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
    local c ts d dirs=()
    for c in llmhub-weights-nginx llmhub-weights-auth llmhub-reaper llmhub-api; do
        if exists "$c"; then dctl rm "$c"; fi
    done
    rollback_s9
    rollback_s7
    if [ -f "$UNIT_DIR/llmhub-backup.timer" ]; then
        "$SYSTEMCTL" disable --now llmhub-backup.timer >/dev/null 2>&1 || true
        rm -f "$UNIT_DIR/llmhub-backup.timer" "$UNIT_DIR/llmhub-backup.service"
        daemon_reload
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
    remove_recorded_accounts  # L2: only accounts s2 recorded as created
    find "$MARK" -maxdepth 1 -type f -delete
    check_s14
}
rollback_s14() { log "s14: restore = tar xzf the archive (fixed uids), then s2..s9 apply"; }

# ------------------------------------------------------------------ main
STEPS="s0 s1 s2 s3 s4 s5 s6 s6k s7 s8a s8b s8c s9 s10x s11 s14"
usage() { echo "usage: $0 <${STEPS// /|}> check|apply|rollback" >&2; exit 2; }
[ $# -eq 2 ] || usage
case " $STEPS " in *" $1 "*) ;; *) usage ;; esac
case "$2" in check | apply | rollback) ;; *) usage ;; esac
[ "$(id -u)" -eq 0 ] || die "run as root"
"$2_$1"
