#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

DEPLOY_OPERATION="deploy"
DEPLOY_OPERATION_SET=false
KUBE_CONTEXT=""
KUBE_NAMESPACE="jobtracker"
HOST_JOBTRACKER_PORT="8086"
IMAGE_MODE="k3s-import"
IMAGE_REPOSITORY=""
IMAGE_TAG_SUFFIX="$(date +%Y%m%d%H%M%S)"
LOCAL_IMAGE_TAG="jobtracker:remote-${IMAGE_TAG_SUFFIX}"
DEPLOY_IMAGE_REF=""
SSH_HOST=""
SSH_USER=""
REMOTE_APP_DIR=""
HOST_DATA_DIR=""
FORWARDED_ARGS=()

usage() {
    cat <<'USAGE'
Usage: deployment/scripts/deploy-jobtracker-remote-k8s.sh [command] [options]

Commands:
  deploy             Build and deploy the private job tracker.
  status             Show job tracker Kubernetes resources and recent events.
  help               Show this help.

Options:
  --host <hostname>             SSH to the remote Ubuntu/k3s server and deploy there.
  --ssh-user <user>             SSH username for --host. Default: current user.
  --remote-dir <path>           Remote application directory. Default: /home/<ssh-user>/jobtracker.
  --data-dir <path>             Absolute node data directory. Default: existing deployment or /var/lib/jobtracker.
  --namespace <name>            Kubernetes namespace. Default: jobtracker.
  --jobtracker-port <port>      Host-facing job tracker port. Default: 8086.
  --context <name>              Kubernetes context to target.
  --image-mode <mode>           k3s-import or registry. Default: k3s-import.
  --image-repository <ref>      Required when --image-mode registry.
  -h, --help                    Show this help.

Examples:
  deployment/scripts/deploy-jobtracker-remote-k8s.sh deploy --host server.example.com --ssh-user deploy
  deployment/scripts/deploy-jobtracker-remote-k8s.sh status --host server.example.com --ssh-user deploy
USAGE
}

set_deploy_operation() {
    local operation="$1"
    if [[ "$DEPLOY_OPERATION_SET" == true ]]; then
        echo "Only one command can be supplied. Already selected '$DEPLOY_OPERATION', got '$operation'." >&2
        exit 1
    fi
    DEPLOY_OPERATION="$operation"
    DEPLOY_OPERATION_SET=true
    FORWARDED_ARGS+=("$operation")
}

require_option_value() {
    local option_name="$1"
    local option_value="${2:-}"
    if [[ -z "$option_value" || "$option_value" == --* ]]; then
        echo "$option_name requires a value." >&2
        usage >&2
        exit 1
    fi
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        deploy)
            set_deploy_operation "deploy"
            ;;
        status)
            set_deploy_operation "status"
            ;;
        help|-h|--help)
            usage
            exit 0
            ;;
        --host)
            require_option_value "$1" "${2:-}"
            SSH_HOST="${2:-}"
            shift
            ;;
        --ssh-user)
            require_option_value "$1" "${2:-}"
            SSH_USER="${2:-}"
            shift
            ;;
        --remote-dir)
            require_option_value "$1" "${2:-}"
            REMOTE_APP_DIR="${2:-}"
            shift
            ;;
        --namespace)
            require_option_value "$1" "${2:-}"
            KUBE_NAMESPACE="${2:-}"
            FORWARDED_ARGS+=("$1" "$KUBE_NAMESPACE")
            shift
            ;;
        --data-dir)
            require_option_value "$1" "${2:-}"
            HOST_DATA_DIR="$2"
            FORWARDED_ARGS+=("$1" "$HOST_DATA_DIR")
            shift
            ;;
        --jobtracker-port)
            require_option_value "$1" "${2:-}"
            HOST_JOBTRACKER_PORT="${2:-}"
            FORWARDED_ARGS+=("$1" "$HOST_JOBTRACKER_PORT")
            shift
            ;;
        --context)
            require_option_value "$1" "${2:-}"
            KUBE_CONTEXT="${2:-}"
            FORWARDED_ARGS+=("$1" "$KUBE_CONTEXT")
            shift
            ;;
        --image-mode)
            require_option_value "$1" "${2:-}"
            IMAGE_MODE="${2:-}"
            FORWARDED_ARGS+=("$1" "$IMAGE_MODE")
            shift
            ;;
        --image-repository)
            require_option_value "$1" "${2:-}"
            IMAGE_REPOSITORY="${2:-}"
            FORWARDED_ARGS+=("$1" "$IMAGE_REPOSITORY")
            shift
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
    shift
done

validate_data_dir() {
    if [[ ! "$1" =~ ^/[A-Za-z0-9_./-]+$ ]]; then
        echo "--data-dir must be an absolute path using letters, digits, dots, underscores, hyphens, and slashes." >&2
        exit 1
    fi
}
if [[ -n "$HOST_DATA_DIR" ]]; then
    validate_data_dir "$HOST_DATA_DIR"
fi

if [[ -n "$SSH_HOST" && -z "$SSH_USER" ]]; then
    SSH_USER="${USER:-$(id -un)}"
fi

if [[ -n "$SSH_HOST" && -z "$REMOTE_APP_DIR" ]]; then
    REMOTE_APP_DIR="/home/${SSH_USER}/jobtracker"
fi

if [[ "$IMAGE_MODE" != "k3s-import" && "$IMAGE_MODE" != "registry" ]]; then
    echo "Unsupported --image-mode: $IMAGE_MODE" >&2
    exit 1
fi

if [[ "$IMAGE_MODE" == "registry" && -z "$IMAGE_REPOSITORY" ]]; then
    echo "--image-repository is required when --image-mode registry is used." >&2
    exit 1
fi

if [[ ! "$HOST_JOBTRACKER_PORT" =~ ^[0-9]+$ || "$HOST_JOBTRACKER_PORT" -lt 1 || "$HOST_JOBTRACKER_PORT" -gt 65535 ]]; then
    echo "--jobtracker-port must be a TCP port from 1 to 65535. Got: $HOST_JOBTRACKER_PORT" >&2
    exit 1
fi

require_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "Missing required command: $1" >&2
        exit 1
    fi
}

shell_join() {
    local quoted=()
    local arg
    for arg in "$@"; do
        quoted+=("$(printf '%q' "$arg")")
    done
    printf '%s' "${quoted[*]}"
}

sync_project_to_remote() {
    local remote_target="${SSH_USER}@${SSH_HOST}"
    echo "Syncing job tracker to ${remote_target}:${REMOTE_APP_DIR}..."
    COPYFILE_DISABLE=1 COPY_EXTENDED_ATTRIBUTES_DISABLE=1 \
    tar \
        --no-mac-metadata \
        --exclude='.git' \
        --exclude='.DS_Store' \
        --exclude='._*' \
        --exclude='target' \
        --exclude='*/target' \
        --exclude='node_modules' \
        --exclude='*/node_modules' \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        -czf - Dockerfile .dockerignore nginx.conf src server deployment | ssh "$remote_target" "rm -rf $(printf '%q' "$REMOTE_APP_DIR") && mkdir -p $(printf '%q' "$REMOTE_APP_DIR") && tar --warning=no-unknown-keyword -xzf - -C $(printf '%q' "$REMOTE_APP_DIR")"
}

run_via_remote_ssh() {
    require_command ssh
    require_command tar
    local remote_target="${SSH_USER}@${SSH_HOST}"
    sync_project_to_remote
    local forwarded=""
    if (( ${#FORWARDED_ARGS[@]} > 0 )); then
        forwarded="$(shell_join "${FORWARDED_ARGS[@]}")"
    fi
    local remote_dir_quoted
    remote_dir_quoted="$(printf '%q' "$REMOTE_APP_DIR")"
    local remote_script="./deployment/scripts/deploy-jobtracker-remote-k8s.sh"
    local remote_command
    local htpasswd_env=""
    if [[ "$DEPLOY_OPERATION" != "status" && -n "${JOBTRACKER_HTPASSWD:-}" ]]; then
        htpasswd_env="export JOBTRACKER_HTPASSWD=$(printf '%q' "$JOBTRACKER_HTPASSWD") && "
    fi
    if [[ "$DEPLOY_OPERATION" == "status" ]]; then
        remote_command="export KUBECONFIG=\"\$HOME/.kube/config\" && cd ${remote_dir_quoted} && chmod +x ${remote_script} && JOBTRACKER_REMOTE_EXEC=1 ${remote_script}${forwarded:+ ${forwarded}}"
    else
        remote_command="sudo -v && export KUBECONFIG=\"\$HOME/.kube/config\" && ${htpasswd_env}cd ${remote_dir_quoted} && chmod +x ${remote_script} && JOBTRACKER_REMOTE_EXEC=1 ${remote_script}${forwarded:+ ${forwarded}}"
    fi
    echo "Running remote job tracker deploy on ${remote_target}..."
    ssh -tt "$remote_target" "bash -lc $(printf '%q' "$remote_command")"
}

if [[ -n "$SSH_HOST" && "${JOBTRACKER_REMOTE_EXEC:-0}" != "1" ]]; then
    run_via_remote_ssh
    exit 0
fi

require_command kubectl
if [[ "$DEPLOY_OPERATION" != "status" ]]; then
    require_command docker
fi

select_kubectl_command() {
    if [[ -n "${KUBECONFIG:-}" ]]; then
        KUBECTL_CMD=(kubectl)
        return
    fi

    if [[ -f "/etc/rancher/k3s/k3s.yaml" && ! -r "/etc/rancher/k3s/k3s.yaml" ]]; then
        require_command sudo
        require_command k3s
        KUBECTL_CMD=(sudo k3s kubectl)
        return
    fi

    KUBECTL_CMD=(kubectl)
}

select_kubectl_command
if [[ -n "$KUBE_CONTEXT" ]]; then
    KUBECTL_CMD+=(--context "$KUBE_CONTEXT")
fi

kubectl_ctx() {
    "${KUBECTL_CMD[@]}" "$@"
}

kubectl_ns() {
    "${KUBECTL_CMD[@]}" -n "$KUBE_NAMESPACE" "$@"
}

ensure_namespace() {
    if kubectl_ctx get namespace "$KUBE_NAMESPACE" >/dev/null 2>&1; then
        return
    fi
    echo "Creating namespace $KUBE_NAMESPACE..."
    kubectl_ctx create namespace "$KUBE_NAMESPACE"
}

ensure_jobtracker_secret() {
    if [[ -z "${JOBTRACKER_HTPASSWD:-}" ]]; then
        if kubectl_ns get secret jobtracker-basic-auth >/dev/null 2>&1; then
            echo "Using existing job tracker basic-auth secret."
            return
        fi

        cat >&2 <<'ERROR'
JOBTRACKER_HTPASSWD is required to deploy with basic authentication.

Create it with a userid and password only you know, for example in zsh:
  read -r -s "JOBTRACKER_PASSWORD?Job tracker password: "
  printf "\n"
  export JOBTRACKER_HTPASSWD="tracker:$(openssl passwd -apr1 "$JOBTRACKER_PASSWORD")"
  unset JOBTRACKER_PASSWORD

Then rerun the deploy command.
ERROR
        exit 1
    fi

    echo "Applying job tracker basic-auth secret..."
    kubectl_ns create secret generic jobtracker-basic-auth \
        --from-literal=htpasswd="$JOBTRACKER_HTPASSWD" \
        --dry-run=client \
        -o yaml | kubectl_ns apply -f -
}

show_status() {
    echo "Job tracker namespace: $KUBE_NAMESPACE"
    echo "Job tracker host port: $HOST_JOBTRACKER_PORT"
    echo
    kubectl_ns get deployment,pod,service -o wide || true
    echo
    kubectl_ns get endpoints || true
    echo
    kubectl_ns get events --sort-by=.lastTimestamp | tail -30 || true
}

build_and_publish_image() {
    echo "Building job tracker image as $LOCAL_IMAGE_TAG..."
    docker build -t "$LOCAL_IMAGE_TAG" .

    if [[ "$IMAGE_MODE" == "k3s-import" ]]; then
        require_command sudo
        require_command k3s
        if ! sudo -n true >/dev/null 2>&1; then
            echo "Passwordless sudo is required to import the job tracker image into k3s containerd." >&2
            echo "Run sudo -v on the remote server, then rerun this deploy while sudo is cached." >&2
            exit 1
        fi
        local image_archive="target/kubernetes/jobtracker-image.tar"
        mkdir -p "$(dirname "$image_archive")"
        docker save "$LOCAL_IMAGE_TAG" -o "$image_archive"
        sudo -n k3s ctr images import "$image_archive"
        rm -f "$image_archive"
        DEPLOY_IMAGE_REF="$LOCAL_IMAGE_TAG"
        return
    fi

    local remote_image="${IMAGE_REPOSITORY}:jobtracker-${IMAGE_TAG_SUFFIX}"
    docker tag "$LOCAL_IMAGE_TAG" "$remote_image"
    docker push "$remote_image"
    DEPLOY_IMAGE_REF="$remote_image"
}

apply_jobtracker_resources() {
    echo "Applying job tracker resources in namespace $KUBE_NAMESPACE..."
    if [[ -z "$HOST_DATA_DIR" ]]; then
        HOST_DATA_DIR="$(kubectl_ns get deployment jobtracker --ignore-not-found -o 'jsonpath={.spec.template.spec.volumes[?(@.name=="jobtracker-data")].hostPath.path}')"
        HOST_DATA_DIR="${HOST_DATA_DIR:-/var/lib/jobtracker}"
    fi
    validate_data_dir "$HOST_DATA_DIR"
    kubectl_ns delete pod -l app=jobtracker --ignore-not-found=true
    kubectl_ns delete service jobtracker --ignore-not-found=true
    sed \
        -e "s|image: jobtracker:latest|image: $DEPLOY_IMAGE_REF|" \
        -e "s|imagePullPolicy: Never|imagePullPolicy: IfNotPresent|" \
        -e "s|path: /var/lib/jobtracker|path: $HOST_DATA_DIR|" \
        deployment/k8s/jobtracker.yaml | kubectl_ns apply -f -
    kubectl_ns rollout status deployment/jobtracker --timeout=120s
    kubectl_ns patch service jobtracker --type json -p "[{\"op\":\"replace\",\"path\":\"/spec/type\",\"value\":\"LoadBalancer\"},{\"op\":\"replace\",\"path\":\"/spec/ports\",\"value\":[{\"name\":\"http\",\"port\":${HOST_JOBTRACKER_PORT},\"protocol\":\"TCP\",\"targetPort\":8080}]}]"
}

if [[ "$DEPLOY_OPERATION" == "status" ]]; then
    show_status
    exit 0
fi

ensure_namespace
ensure_jobtracker_secret
build_and_publish_image
apply_jobtracker_resources

echo "Job tracker is ready in namespace $KUBE_NAMESPACE."
echo "Host endpoint: http://<server>:${HOST_JOBTRACKER_PORT}"
