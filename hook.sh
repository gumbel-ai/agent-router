#!/bin/sh
# Dependencies are installed only by explicit router setup, never by hooks.
runtime_dir="${AGENT_ROUTER_STATE_DIR:-$HOME/.agent-router}"
if [ -r "$runtime_dir/python-path" ]; then
    IFS= read -r interpreter < "$runtime_dir/python-path"
    if [ "$1" = "observe" ]; then
        exec "$interpreter" -I -m router.observer_hook
    fi
    exec "$interpreter" -I -m router.hooks "$1"
fi
if [ "$1" = "observe" ]; then
    exec router-observe
fi
exec router-host-hook "$1"
