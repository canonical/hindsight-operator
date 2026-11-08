# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
# Shared helpers for the spread tasks. Source this file; it defines functions
# only, so tasks read as a story: `as alice POST /path '{"json": "body"}'`.

# The tenants of the demo: who holds which key, and which banks each key may use.
ADMIN_KEY="spread-admin-key"
declare -gA KEYS=(
    [alice]="spread-key-alice"
    [bob]="spread-key-bob"
    [auditor]="spread-key-auditor"
)
BANK_ACCESS='
alice:
  banks: [alice-*]
  read-banks: [shared]
bob:
  banks: [bob-*]
  read-banks: [shared]
auditor:
  read-banks: ["*"]
'
STATE_FILE=/root/hindsight-multitenancy.env

bootstrap_juju_on_microk8s() {
    if juju show-controller "${CONTROLLER}" >/dev/null 2>&1; then
        return
    fi
    snap install concierge --classic
    # The same configuration CI uses for the integration tests.
    concierge prepare -c "${PROJECT_PATH}/.github/actions/setup-k8s/concierge.yaml"
    juju show-controller "${CONTROLLER}" >/dev/null
}

deploy_hindsight_with_tenants() {
    test -n "${CRAFT_ARTIFACT}" || { echo "CRAFT_ARTIFACT must name the packed charm"; exit 1; }
    image=$(sed -n 's/^ *upstream-source: *//p' "${PROJECT_PATH}/charmcraft.yaml")
    juju add-model -c "${CONTROLLER}" "${MODEL#*:}"
    # Juju schedules pods on amd64 nodes unless told otherwise.
    juju set-model-constraints -m "${MODEL}" arch="$(dpkg --print-architecture)"
    # The Juju snap can always read its own directory.
    mkdir -p ~/snap/juju/common
    cp "${CRAFT_ARTIFACT}" ~/snap/juju/common/hindsight.charm
    juju deploy -m "${MODEL}" ~/snap/juju/common/hindsight.charm hindsight \
        --resource hindsight-image="${image}" --storage data=1G

    env_secret=$(juju add-secret -m "${MODEL}" hindsight-env tenant-api-key="${ADMIN_KEY}")
    key_args=()
    for name in "${!KEYS[@]}"; do key_args+=("${name}=${KEYS[$name]}"); done
    keys_secret=$(juju add-secret -m "${MODEL}" hindsight-access-keys "${key_args[@]}")
    juju grant-secret -m "${MODEL}" hindsight-env hindsight
    juju grant-secret -m "${MODEL}" hindsight-access-keys hindsight
    juju config -m "${MODEL}" hindsight env-secret="${env_secret}" \
        access-keys="${keys_secret}" bank-access="${BANK_ACCESS}"

    juju wait-for unit -m "${MODEL}" hindsight/0 \
        --query='workload-status=="active" && agent-status=="idle"' --timeout 30m
    address=$(juju status -m "${MODEL}" --format json | jq -r '.applications.hindsight.units["hindsight/0"].address')
    echo "HINDSIGHT=http://${address}:8888" > "${STATE_FILE}"

    # Give each tenant something to remember, as the admin.
    load_state
    for bank in alice-notes bob-notes shared private; do
        expect 200 as admin PUT "/v1/default/banks/${bank}" '{}'
    done
}

load_state() {
    # shellcheck disable=SC1090
    . "${STATE_FILE}"
}

# as <who> <METHOD> <path> [json-body]: prints the response body, sets STATUS.
as() {
    local who=$1 method=$2 path=$3 body=${4:-}
    local key
    case "${who}" in
        admin) key="${ADMIN_KEY}" ;;
        nobody) key="" ;;
        *) key="${KEYS[$who]}" ;;
    esac
    local args=(-sS --path-as-is -o /tmp/hindsight-body -w '%{http_code}' -X "${method}"
        -H 'Content-Type: application/json'
        -H 'Accept: application/json, text/event-stream')
    [ -n "${key}" ] && args+=(-H "Authorization: Bearer ${key}")
    [ -n "${body}" ] && args+=(--data "${body}")
    STATUS=$(curl "${args[@]}" "${HINDSIGHT}${path}")
    BODY=$(cat /tmp/hindsight-body)
}

# expect <status> as <who> ...: run the request and fail the task if the status differs.
expect() {
    local want=$1
    shift
    "$@"
    if [ "${STATUS}" != "${want}" ]; then
        echo "FAIL: '$*' returned ${STATUS}, expected ${want}: ${BODY}"
        exit 1
    fi
    echo "ok: ${*:2} -> ${STATUS}"
}

# banks_of <who>: the bank ids a key sees, sorted and space-separated.
banks_of() {
    as "$1" GET /v1/default/banks
    echo "${BODY}" | jq -r '[.banks[].bank_id] | sort | join(" ")'
}

retain() {  # retain <who> <bank> <text>
    as "$1" POST "/v1/default/banks/$2/memories" \
        "$(jq -nc --arg t "$3" '{items: [{content: $t}], async: false}')"
}

recall() {  # recall <who> <bank> <query>
    as "$1" POST "/v1/default/banks/$2/memories/recall" "$(jq -nc --arg q "$3" '{query: $q}')"
}
