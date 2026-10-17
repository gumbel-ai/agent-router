#!/bin/sh
# Codex runs hooks with a cleared environment; locate Router the way the Claude hook does.
# Observation must never disturb Codex, so every path ends by answering an empty object.
runtime_dir="${AGENT_ROUTER_STATE_DIR:-$HOME/.agent-router}"
if [ -r "$runtime_dir/python-path" ]; then
    IFS= read -r interpreter < "$runtime_dir/python-path"
    exec "$interpreter" -I -m router.observer_hook --source codex
fi
if command -v router-observe >/dev/null 2>&1; then
    exec router-observe --source codex
fi
cat >/dev/null
echo '{}'
