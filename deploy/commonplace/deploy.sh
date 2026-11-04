#!/usr/bin/env bash
set -Eeuo pipefail

die() {
    printf '[deploy] ERROR: %s: %s\n' "$1" "$2" >&2
    exit 1
}

fresh_enough() {
    local start=$1 snap=$2 now=$3 max_age=$4
    (( snap >= start && snap <= now + 60 && now - snap <= max_age ))
}

validate_literal_path() {
    [[ $2 != *[[:space:]\"\'\$\`\\#]* ]] || die config "$1 must be a literal path without whitespace, quotes, expansions or comments"
}

run_command() {
    local -a command
    read -r -a command <<< "$1"
    (( ${#command[@]} > 0 )) || return 2
    set -f
    "${command[@]}"
}

main() {
    local owner mode
    local key value line config start before after result latest timestamp snap now rendered health execution
    [[ $(id -u) == 0 ]] || die preflight 'must run as root'
    [[ $# == 0 && ! -v SKIP_BACKUP ]] || die usage 'deploy.sh accepts no arguments'
    COMMONPLACE_ROOT=${COMMONPLACE_ROOT:-/srv/commonplace}
    config=${COMMONPLACE_DEPLOY_ENV:-$COMMONPLACE_ROOT/deploy.env}
    if [[ -f $config ]]; then
        read -r owner mode < <(stat -c '%u %a' -- "$config") || die config 'cannot inspect deploy.env permissions'
        [[ $owner == 0 && $mode =~ ^[0-7]{3,4}$ ]] || die config 'deploy.env must be owned by root and not group/world-writable'
        (( (8#$mode & 8#022) == 0 )) || die config 'deploy.env must be owned by root and not group/world-writable'
        while IFS= read -r line || [[ -n $line ]]; do
            [[ $line =~ ^[A-Z_][A-Z0-9_]*= ]] || continue
            key=${line%%=*}
            case $key in
                DEPLOY_BACKUP_UNIT|DEPLOY_BACKUP_TIMEOUT|DEPLOY_BACKUP_CHECK_CMD|DEPLOY_BACKUP_LATEST_CMD|DEPLOY_BACKUP_MAX_AGE|APP_PORT|COMPOSE_FILE|COMPOSE_ENV_FILE|COMMONPLACE_SOURCE_DIR|COMMONPLACE_ENV_FILE|COMMONPLACE_DATA_DIR) ;;
                *) continue ;;
            esac
            value=${line#*=}
            if [[ ! -v $key ]]; then
                case $key in
                    COMMONPLACE_SOURCE_DIR|COMMONPLACE_ENV_FILE|COMMONPLACE_DATA_DIR) validate_literal_path "$key" "$value" ;;
                esac
                export "$key=$value"
            fi
        done < "$config"
    fi
    [[ ${DEPLOY_BACKUP_UNIT:-} =~ ^[A-Za-z0-9@:_.-]+\.service$ ]] || die config 'invalid DEPLOY_BACKUP_UNIT'
    DEPLOY_BACKUP_TIMEOUT=${DEPLOY_BACKUP_TIMEOUT:-3600}
    [[ $DEPLOY_BACKUP_TIMEOUT =~ ^[1-9][0-9]{0,5}$ ]] || die config 'invalid DEPLOY_BACKUP_TIMEOUT'
    [[ -n ${DEPLOY_BACKUP_CHECK_CMD:-} ]] || die config 'DEPLOY_BACKUP_CHECK_CMD is required'
    [[ -n ${DEPLOY_BACKUP_LATEST_CMD:-} ]] || die config 'DEPLOY_BACKUP_LATEST_CMD is required'
    DEPLOY_BACKUP_MAX_AGE=${DEPLOY_BACKUP_MAX_AGE:-7200}
    APP_PORT=${APP_PORT:-8765}
    [[ $DEPLOY_BACKUP_MAX_AGE =~ ^[0-9]{1,5}$ ]] || die config 'invalid DEPLOY_BACKUP_MAX_AGE'
    (( 10#$DEPLOY_BACKUP_MAX_AGE >= 60 && 10#$DEPLOY_BACKUP_MAX_AGE <= 86400 )) || die config 'invalid DEPLOY_BACKUP_MAX_AGE'
    [[ $APP_PORT =~ ^[0-9]{1,5}$ ]] || die config 'invalid APP_PORT'
    (( 10#$APP_PORT >= 1 && 10#$APP_PORT <= 65535 )) || die config 'invalid APP_PORT'
    APP_PORT=$((10#$APP_PORT))
    COMPOSE_FILE=${COMPOSE_FILE:-$COMMONPLACE_ROOT/compose.yml}
    COMPOSE_ENV_FILE=${COMPOSE_ENV_FILE:-$COMMONPLACE_ROOT/.env}
    for key in jq flock curl systemctl docker date timeout; do
        command -v "$key" >/dev/null || die preflight "missing $key"
    done
    [[ -f $COMPOSE_FILE && -f $COMPOSE_ENV_FILE ]] || die config 'compose and environment files must exist'
    { exec 9>"$COMMONPLACE_ROOT/.deploy.lock"; } 2>/dev/null || die lock 'cannot open deploy lock'
    flock -n 9 || die lock 'another deploy is running'
    # Read the Compose input as literal data too; exported settings win.
    while IFS= read -r line || [[ -n $line ]]; do
        [[ $line =~ ^COMMONPLACE_(SOURCE_DIR|ENV_FILE|DATA_DIR)= ]] || continue
        key=${line%%=*}
        value=${line#*=}
        if [[ ! -v $key ]]; then
            validate_literal_path "$key" "$value"
            export "$key=$value"
        fi
    done < "$COMPOSE_ENV_FILE"
    for key in COMMONPLACE_ROOT COMPOSE_FILE COMPOSE_ENV_FILE COMMONPLACE_SOURCE_DIR COMMONPLACE_ENV_FILE COMMONPLACE_DATA_DIR; do
        [[ ${!key:-} == /* ]] || die config "$key must be an absolute path"
    done
    [[ $(systemctl show -p LoadState --value "$DEPLOY_BACKUP_UNIT") == loaded ]] || die config 'unit is not loaded'
    [[ $(systemctl show -p Type --value "$DEPLOY_BACKUP_UNIT") == oneshot ]] || die config 'unit must be oneshot'
    [[ $(systemctl show -p RemainAfterExit --value "$DEPLOY_BACKUP_UNIT") == no ]] || die config 'unit must have RemainAfterExit=no'
    start=$(date -u +%s) || die preflight 'cannot read clock'

    before=$(systemctl show -p InvocationID --value "$DEPLOY_BACKUP_UNIT" 2>/dev/null) || die backup 'cannot read invocation'
    timeout "$DEPLOY_BACKUP_TIMEOUT" systemctl start --wait "$DEPLOY_BACKUP_UNIT" 2>/dev/null || die backup 'unit start failed'
    after=$(systemctl show -p InvocationID --value "$DEPLOY_BACKUP_UNIT" 2>/dev/null) || die backup 'cannot read invocation'
    [[ -n $after ]] || die backup 'backup unit was unloaded; keep it referenced'
    [[ $after != "$before" ]] || die backup 'unit did not run a new invocation'
    result=$(systemctl show -p Result --value "$DEPLOY_BACKUP_UNIT" 2>/dev/null) || die backup 'cannot read result'
    [[ $result == success ]] || die backup 'unit did not succeed'

    execution=$(systemctl show -p ExecMainStartTimestamp --value "$DEPLOY_BACKUP_UNIT") || die backup 'cannot read execution time'
    [[ -n $execution ]] || die backup 'missing execution time'
    execution=$(date -d "$execution" +%s) || die backup 'invalid execution time'
    (( execution >= start )) || die backup 'unit execution predates deploy'

    latest=$(run_command "$DEPLOY_BACKUP_LATEST_CMD" 2>/dev/null) || die latest 'command failed'
    [[ $latest != *$'\n'* ]] || die latest 'more than one line'
    timestamp=$(jq -er 'select(type == "object") | select(.id | type == "string" and test("^[0-9a-f]{64}$")) | .time | select(type == "string")' <<< "$latest" 2>/dev/null) || die latest 'invalid snapshot JSON'
    [[ $timestamp =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})$ ]] || die latest 'invalid snapshot time'
    snap=$(date -d "$timestamp" +%s 2>/dev/null) || die latest 'invalid snapshot time'
    now=$(date -u +%s) || die latest 'cannot read clock'
    fresh_enough "$start" "$snap" "$now" "$((10#$DEPLOY_BACKUP_MAX_AGE))" || die latest 'snapshot is not fresh'
    run_command "$DEPLOY_BACKUP_CHECK_CMD" >/dev/null 2>&1 || die check 'backup verification failed'

    if systemctl is-active --quiet "$DEPLOY_BACKUP_UNIT"; then
        die backup 'unit is active; not deploying'
    fi
    local -a compose=(docker compose -f "$COMPOSE_FILE" --env-file "$COMPOSE_ENV_FILE")
    rendered=$("${compose[@]}" config --format json 2>/dev/null) || die deploy 'compose config failed'
    jq --arg port "$APP_PORT" -e 'all(.services[] | .ports[]? | select(.published != null); .host_ip == "127.0.0.1" and (.published | tostring) == $port)' <<< "$rendered" >/dev/null 2>&1 || die deploy 'published ports must use loopback'
    "${compose[@]}" up -d --build || die deploy 'compose up failed'
    "${compose[@]}" ps || die deploy 'compose ps failed'
    health=$(curl -s --max-time 10 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$APP_PORT/healthz") || health=unavailable
    printf '[deploy] healthz: %s\n' "$health"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
