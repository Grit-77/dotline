#!/usr/bin/env bash
# bash 3.2+ and curl 7.55+: keep the bearer out of curl's process arguments.
set -eu
set +x
umask 077

die() {
    printf '%s' "dotline: $1" >&2
    if [[ ${send_attempted:-0} == 1 ]]; then
        printf '%s' "; the message may have arrived. Resend with the same client_id, never a new one: $client_id" >&2
    fi
    printf '\n' >&2
    exit 1
}
work=$(mktemp -d "${TMPDIR:-/tmp}/dotline.XXXXXXXX")
trap 'rm -rf "$work"' EXIT
trap 'exit 130' INT TERM

home_dir=${DOTLINE_HOME:-${XDG_CONFIG_HOME:-$HOME/.config}/dotline}
if [[ ${OS:-} == Windows_NT && -z ${DOTLINE_HOME:-} ]]; then
    home_dir=${APPDATA:?APPDATA is required}/dotline
fi
url=${DOTLINE_URL:-}
if [[ -z $url && -f $home_dir/config.toml ]]; then
    while IFS= read -r line; do
        if [[ $line =~ ^[[:space:]]*url[[:space:]]*=[[:space:]]*\"([^\"]*)\" ]]; then
            url=${BASH_REMATCH[1]}
        elif [[ $line =~ ^[[:space:]]*url[[:space:]]*=[[:space:]]*\'([^\']*)\' ]]; then
            url=${BASH_REMATCH[1]}
        fi
    done < "$home_dir/config.toml"
fi
url=${url:-http://127.0.0.1:8790}
url=${url%/}
[[ $url != *'@'* && $url != *'?'* && $url != *'#'* ]] || die 'URL must not contain credentials or a query'
case "$url" in
    https://*) ;;
    http://127.0.0.1|http://127.0.0.1:*|http://localhost|http://localhost:*) ;;
    *) die 'remote URLs must use HTTPS' ;;
esac
authority=${url#*://}
[[ $authority != */* ]] || die 'URL must be an origin without a path'

make_header() {
    local token_file=${DOTLINE_TOKEN_FILE:-$home_dir/token} secret
    [[ -r $token_file ]] || die 'token file is unavailable'
    secret=$(< "$token_file")
    secret=${secret%$'\r'}
    [[ -n $secret && $secret != *$'\n'* && $secret != *$'\r'* ]] || die 'token file is invalid'
    printf 'Authorization: Bearer %s\n' "$secret" > "$work/header"
    unset secret
}

request() {
    local path=$1 method=${2:-GET} timeout=${3:-15}
    if [[ $path == /v1/health ]]; then
        curl --silent --show-error --fail --max-time "$timeout" --noproxy '*' \
            --tlsv1.2 "$url$path" > "$work/response" || die 'HTTP request failed'
    elif [[ $method == POST ]]; then
        curl --silent --show-error --fail --max-time "$timeout" --noproxy '*' \
            --tlsv1.2 --header "@$work/header" --header 'Content-Type: application/json; charset=utf-8' \
            --data-binary "@$work/body" "$url$path" > "$work/response" || die 'HTTP request failed'
    else
        curl --silent --show-error --fail --max-time "$timeout" --noproxy '*' \
            --tlsv1.2 --header "@$work/header" "$url$path" > "$work/response" || die 'HTTP request failed'
    fi
    response=$(< "$work/response")
}

# A fresh random (version 4) UUID for every send. Only od and tr are needed.
new_client_id() {
    local hex variant
    hex=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
    [[ ${#hex} -eq 32 ]] || die 'cannot generate a client_id'
    printf -v variant '%x' $(( (0x${hex:16:1} & 3) | 8 ))
    client_id=${hex:0:8}-${hex:8:4}-4${hex:13:3}-$variant${hex:17:3}-${hex:20:12}
}

# Send the prepared body; after a network error send it once more with the same client_id.
# curl exits 22 when the server answered with an HTTP error: that is final, never retried.
post_message() {
    local attempt rc
    for attempt in 1 2; do
        rc=0
        curl --silent --show-error --fail --max-time 15 --noproxy '*' \
            --tlsv1.2 --header "@$work/header" --header 'Content-Type: application/json; charset=utf-8' \
            --data-binary "@$work/body" "$url/v1/messages" > "$work/response" || rc=$?
        ((rc != 0)) || break
        ((rc != 22)) || die 'HTTP request failed'
        ((attempt == 1)) || die 'HTTP request failed'
        sleep 1
    done
    response=$(< "$work/response")
}

json_string() {
    local value=$1 char i escaped
    printf '"'
    for ((i=0; i<${#value}; i++)); do
        char=${value:i:1}
        case "$char" in
            '"') printf '\\"' ;;
            '\') printf '\\\\' ;;
            $'\n') printf '\\n' ;;
            $'\r') printf '\\r' ;;
            $'\t') printf '\\t' ;;
            *)
                if [[ $char < ' ' ]]; then
                    printf -v escaped '\\u%04x' "'$char"
                    printf '%s' "$escaped"
                else
                    printf '%s' "$char"
                fi ;;
        esac
    done
    printf '"'
}

# A tokenizer for the API's flat reply records. Strings are consumed whole,
# so braces or apparent JSON inside message text cannot change the selection.
take_string() {
    local char escape digits
    parsed=''
    cursor=$((cursor + 1))
    while ((cursor < ${#response})); do
        char=${response:cursor:1}; cursor=$((cursor + 1))
        [[ $char != '"' ]] || return 0
        [[ $char > $'\x1f' ]] || die 'invalid server JSON'
        if [[ $char == '\' ]]; then
            escape=${response:cursor:1}; cursor=$((cursor + 1))
            case "$escape" in
                '"'|'\'|'/') char=$escape ;;
                n) char=$'\n' ;; r) char=$'\r' ;; t) char=$'\t' ;;
                b) char=$'\b' ;; f) char=$'\f' ;;
                u)
                    digits=${response:cursor:4}; cursor=$((cursor + 4))
                    [[ $digits =~ ^[0-9a-fA-F]{4}$ ]] || die 'invalid server JSON'
                    [[ ${strict_send_json:-0} != 1 || $digits != 0000 ]] || die 'invalid server JSON'
                    printf -v char '%b' "\\u$digits" ;;
                *) die 'invalid server JSON' ;;
            esac
        fi
        parsed=$parsed$char
    done
    die 'invalid server JSON'
}

find_reply() {
    local cursor=0 parsed char key='' value to='' text='' depth=0
    found=0
    while ((cursor < ${#response})); do
        char=${response:cursor:1}
        case "$char" in
            '{') depth=$((depth + 1)); to=''; text=''; cursor=$((cursor + 1)) ;;
            '}')
                if [[ $to == "$message_id" ]]; then
                    found=1; reply_text=$text; return 0
                fi
                depth=$((depth - 1)); cursor=$((cursor + 1)) ;;
            '"')
                take_string; key=$parsed
                while [[ ${response:cursor:1} == ' ' ]]; do cursor=$((cursor + 1)); done
                if [[ ${response:cursor:1} == ':' ]]; then
                    cursor=$((cursor + 1))
                    while [[ ${response:cursor:1} == ' ' ]]; do cursor=$((cursor + 1)); done
                    if [[ ${response:cursor:1} == '"' ]]; then
                        take_string; value=$parsed
                    else
                        value=''
                        while [[ ${response:cursor:1} =~ [0-9] ]]; do
                            value=$value${response:cursor:1}; cursor=$((cursor + 1))
                        done
                    fi
                    [[ $key != to ]] || to=$value
                    [[ $key != text ]] || text=$value
                fi ;;
            *) cursor=$((cursor + 1)) ;;
        esac
    done
}

skip_space() {
    while [[ ${response:cursor:1} == [[:space:]] ]]; do cursor=$((cursor + 1)); done
}

# Send responses are flat JSON objects. Consume the whole object before trusting its id.
read_send_response() {
    local cursor=0 parsed key value kind char strict_send_json=1
    message_number=''; duplicate=0
    skip_space
    [[ ${response:cursor:1} == '{' ]] || die 'invalid server response'
    cursor=$((cursor + 1)); skip_space
    while [[ ${response:cursor:1} != '}' ]]; do
        [[ ${response:cursor:1} == '"' ]] || die 'invalid server response'
        take_string; key=$parsed; skip_space
        [[ ${response:cursor:1} == ':' ]] || die 'invalid server response'
        cursor=$((cursor + 1)); skip_space
        if [[ ${response:cursor:1} == '"' ]]; then
            take_string; value=$parsed; kind=string
        else
            value=''; kind=scalar
            while ((cursor < ${#response})); do
                char=${response:cursor:1}
                [[ $char != ',' && $char != '}' && $char != [[:space:]] ]] || break
                value=$value$char; cursor=$((cursor + 1))
            done
            [[ $value =~ ^(0|[1-9][0-9]*|true|false|null)$ ]] || die 'invalid server response'
        fi
        case "$key" in
            id)
                [[ $kind == scalar && $value =~ ^[1-9][0-9]*$ ]] || die 'invalid server response'
                message_number=$value ;;
            duplicate)
                [[ $kind == scalar && ( $value == true || $value == false ) ]] || die 'invalid server response'
                duplicate=0; [[ $value != true ]] || duplicate=1 ;;
        esac
        skip_space
        [[ ${response:cursor:1} != '}' ]] || break
        [[ ${response:cursor:1} == ',' ]] || die 'invalid server response'
        cursor=$((cursor + 1)); skip_space
        [[ ${response:cursor:1} != '}' ]] || die 'invalid server response'
    done
    cursor=$((cursor + 1)); skip_space
    ((cursor == ${#response})) && [[ -n $message_number ]] || die 'invalid server response'
}

command=${1:-}; [[ -n $command ]] || die 'usage: dotline.sh send|wait|replies|health [arguments]'
shift
topic='' file='' minutes=10 after=0 text='' message_id='' client_id='' client_id_set=0
while (($#)); do
    case "$1" in
        --topic|--file|--minutes|--after|--client-id)
            (($# >= 2)) || die 'option needs a value'
            case "$1" in
                --topic) topic=$2 ;; --file) file=$2 ;; --minutes) minutes=$2 ;; --after) after=$2 ;;
                --client-id) client_id=$2; client_id_set=1 ;;
            esac
            shift 2 ;;
        *)
            if [[ $command == wait && -z $message_id ]]; then message_id=$1
            elif [[ -z $text ]]; then text=$1
            else text="$text $1"; fi
            shift ;;
    esac
done
[[ $command == health ]] || make_header
case "$command" in
    send)
        if [[ -n $file ]]; then
            [[ -z $text && -r $file ]] || die 'use either text or a readable --file'
            IFS= read -r -d '' text < "$file" || true
        fi
        [[ -n $text ]] || die 'message text is required'
        if ((client_id_set)); then
            ((${#client_id} >= 1 && ${#client_id} <= 64)) || die 'client_id must contain 1 to 64 characters'
        else
            new_client_id
        fi
        {
            printf '{"text":'; json_string "$text"
            printf ',"topic":'; json_string "$topic"
            printf ',"client_id":'; json_string "$client_id"
            printf '}'
        } > "$work/body"
        send_attempted=1
        post_message
        read_send_response
        if ((duplicate)); then
            printf 'already delivered: message %s\n' "$message_number"
        else
            printf '%s\n' "$message_number"
        fi ;;
    wait)
        [[ $message_id =~ ^[1-9][0-9]*$ && $minutes =~ ^[1-9][0-9]*$ ]] || die 'wait requires a positive id and whole minutes'
        deadline=$((SECONDS + minutes * 60))
        while :; do
            remaining=$((deadline - SECONDS))
            ((remaining > 0)) || die 'timed out waiting for a reply'
            timeout=15
            ((remaining >= timeout)) || timeout=$remaining
            request /v1/replies?after=0 GET "$timeout"
            find_reply
            if ((found)); then printf '%s\n' "$reply_text"; break; fi
            ((SECONDS < deadline)) || die 'timed out waiting for a reply'
            remaining=$((deadline - SECONDS)); delay=20
            ((remaining >= delay)) || delay=$remaining
            sleep "$delay"
        done ;;
    replies)
        [[ $after =~ ^[0-9]+$ ]] || die 'after must be a nonnegative integer'
        request "/v1/replies?after=$after"; printf '%s\n' "$response" ;;
    health) request /v1/health; printf '%s\n' "$response" ;;
    *) die 'unknown command' ;;
esac
