"""Script entry point for the Claude PreToolUse hook."""
from router.hooks import main

if __name__ == "__main__":
    import sys
    sys.argv = [sys.argv[0], "authorize"]
    main()
