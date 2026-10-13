import json
import shlex
import unittest
from concurrent.futures import ThreadPoolExecutor

from router.dispatch import literal_payload


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


if __name__ == '__main__':
    unittest.main()
