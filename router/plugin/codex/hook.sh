#!/bin/sh
# Codex runs hooks with a cleared environment; locate Router the way the Claude hook does.
# Unavailable hooks never block native work or grant dispatch; the daemon enforces authorization.
case "${1:-observe}" in
    observe) purpose=observation; module=router.observer_hook; entrypoint=router-observe; set -- --source codex ;;
    submit) purpose=delegation; module=router.hooks; entrypoint=router-host-hook; set -- codex-submit ;;
    authorize) purpose=delegation; module=router.hooks; entrypoint=router-host-hook; set -- codex-authorize ;;
    *) echo 'Agent Router hook: unsupported Codex operation' >&2; exit 2 ;;
esac
run_hook() {
    if response=$("$@"); then
        printf '%s\n' "$response"
    else
        hook_exit=$?
        cat >/dev/null
        echo "Agent Router $purpose unavailable: selected integration exited $hook_exit; no automatic retry" >&2
        echo '{}'
    fi
}
runtime_dir="${AGENT_ROUTER_STATE_DIR:-$HOME/.agent-router}"
if [ -r "$runtime_dir/python-path" ]; then
    IFS= read -r interpreter < "$runtime_dir/python-path"
    if [ -x "$interpreter" ]; then
        run_hook "$interpreter" -I -m "$module" "$@"
    else
        cat >/dev/null
        echo "Agent Router $purpose unavailable: selected interpreter is not executable" >&2
        echo '{}'
    fi
    exit 0
fi
if command -v "$entrypoint" >/dev/null 2>&1; then
    run_hook "$entrypoint" "$@"
    exit 0
fi
cat >/dev/null
echo "Agent Router $purpose unavailable: Router hook integration is missing" >&2
echo '{}'
