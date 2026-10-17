import json
import shlex
import unittest
from concurrent.futures import ThreadPoolExecutor

from router.dispatch import literal_payload, mutation_operation


class DispatchTests(unittest.TestCase):
    def test_literal_shell_contract(self):
        payloads = [
            {"task": "don't change the API"},
            {"task": "hello"},
            {"task": "$(printf changed)"},
        ]
        cases = [(shlex.join(["router", "start", "--json", json.dumps(p)]), p) for p in payloads]
        cases += [(command, None) for command in [
            'router start --json "{\\"task\\":\\"$(printf changed)\\"}"',
            'router start --json "{\\"task\\":\\"$ROUTER_PROBE\\"}"',
            "router start --json '{}' ; printf extra",
            "router start --json '{}' | tee output",
            "router start --json '{invalid}'",
            "router start --json '[]'",
        ]]

        def check(case):
            command, expected = case
            if expected is None:
                with self.assertRaises(ValueError):
                    literal_payload(command)
            else:
                self.assertEqual(literal_payload(command), expected)

        # Frozen, read-only inputs: the project's five-worker rule applies.
        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(check, cases))

    def test_hook_finds_router_mutations_however_the_shell_reaches_them(self):
        cases = {
            "router start --json '{}'": "start",
            "/usr/local/bin/router review --json '{}'": "review",
            "cd repo && router cancel --json '{}'": "cancel",
            "x=1;router close --json '{}'": "close",
            "(router start --json '{}')": "start",
            "echo $(router start --json '{}')": "start",
            'echo "$(router start --json {})"': "start",
            "echo `router close --json {}`": "close",
            "bash -c 'router start --json {}'": "start",
            'eval "router review --json {}"': "review",
            "git status": None,
            "router status --json": None,
            "my-router start": "start",  # Conservative, as the previous matcher was.
            "router_tool start": None,
            "echo $'it\\'s'; eval 'router start --json {}'": "start",
            "unset c; ${c:-router start} --json '{}'": "start",
            "router \\\nstart --json '{}'": "start",
            "echo 'unbalanced router start": "start",  # Unparseable text falls back to plain words.
            "cat <<'EOF'\nit's\nEOF\nrouter start --json '{}'": "start",
            "echo $'it\\'s'; router start --json '{}'": "start",
            "curl http://h/p#x && router start --json '{}'": "start",
            "echo $'it\\'s';router start --json '{}'": "start",
            "cat <<'EOF'\nit's\nEOF\ntrue;router start --json '{}'": "start",
            "router start$(printf '') --json '{}'": "start",
            "printf '%s\\n' value#; router start --json '{}'": "start",
            "router start --json '{}'\nprintf '%s\\n' '": "start",  # Runs before the later parse error.
        }
        nested = "router start --json {}"
        for _ in range(8):
            nested = shlex.join(["eval", nested])
        cases[nested] = "start"
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(mutation_operation(command), expected)
        with self.assertRaises(TypeError):
            mutation_operation(None)
        # A found mutation is allowed only as the standalone canonical form.
        self.assertEqual(literal_payload("router review --json '{}'", "review"), {})
        for command in ("cd repo && router start --json '{}'", "bash -c 'router start --json {}'",
                        "router  start --json '{}'"):
            with self.subTest(command=command), self.assertRaises(ValueError):
                literal_payload(command, mutation_operation(command))


if __name__ == '__main__':
    unittest.main()
