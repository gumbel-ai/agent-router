import errno
import io
import json
import os
import shlex
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from router.client import request as router_request
from router.dispatch import canonical
from router.fleet import FleetConfig, FleetHost


class FleetCompletionTests(unittest.TestCase):
    def setUp(self):
        self.config = FleetConfig((FleetHost('worker-a', 'local'), FleetHost('worker-b', 'ssh', 'example-host')),
                                  ssh_timeout_seconds=15)
        self.event = {'status': 'completed', 'consumer': 'batch-job', 'host_id': 'host-b', 'subscription_id': 'subscription-b',
                      'event': {'event_id': 'completion-1', 'host_id': 'producing-host',
                                'managed_id': 'managed-job', 'status': 'exited', 'exit_code': 0,
                                'group_outlived': False, 'session_id': 'session  a', 'agent_id': 'worker  a',
                                'work_item_id': 'batch  α'}}

    def test_ack_error_requires_failed_transport_and_exact_structured_binding(self):
        from router.fleet import SSHQueryError
        from router.fleet_completions import acknowledge_completion

        binding = {'consumer': 'batch-job', 'event_id': 'completion-1', 'host_id': 'host-b',
                   'event_host_id': 'producing-host', 'subscription_id': 'subscription-b'}
        envelope = {'schema_version': 1, **binding, 'error': {'code': 'database_busy'}}
        rows = [(envelope, 1, 'database_busy'),
                ({**envelope, 'error': {'code': 'acknowledgment_refused'}}, 1, 'acknowledgment_refused'),
                ({**envelope, 'error': {'code': 'fixture-private-value'}}, 1, 'exit_failed'),
                ({**envelope, 'schema_version': True}, 1, 'exit_failed'),
                ({**envelope, 'acknowledged': True}, 1, 'exit_failed'),
                ({**binding, 'acknowledged': True}, 1, 'exit_failed'),
                (envelope, 2, 'exit_failed'), (envelope, 255, 'exit_failed'),
                ({**envelope, 'extra': 'fixture-private-value'}, 1, 'exit_failed'),
                ('malformed', 1, 'exit_failed'), (' ' * (16 * 1024 + 1), 1, 'exit_failed'),
                ('[' * 2000 + ']' * 2000, 1, 'exit_failed')]
        rows.extend(({**envelope, key: 'mismatched'}, 1, 'exit_failed') for key in binding)
        for value, exit_code, expected in rows:
            error = SSHQueryError('exit_failed', 'fixture-private-value', exit_code=exit_code)
            error.stdout = value.encode() if isinstance(value, str) else json.dumps(value).encode()
            with self.subTest(value=value, exit_code=exit_code), \
                    patch('router.fleet_completions.ssh_stdout', side_effect=error):
                result = acknowledge_completion(self.config, 'worker-b', **binding)
                self.assertFalse(result['ok'])
                self.assertIsNone(result['host_id'])
                self.assertEqual(result['error'], {'code': expected, 'exit_code': exit_code,
                                                   'acknowledgment': 'unknown'})
                self.assertNotIn('fixture-private-value', canonical(result))
        with patch('router.fleet_completions.ssh_stdout', return_value=json.dumps(envelope).encode()):
            result = acknowledge_completion(self.config, 'worker-b', **binding)
        self.assertEqual(result['error']['code'], 'invalid_response')

    def test_wait_preserves_identities_and_projects_remote_payloads(self):
        from router.fleet_completions import query_completions
        raw = {**self.event, 'prompt': 'fixture-private-value',
               'event': {**self.event['event'], 'output': 'fixture-private-value'}}
        with patch('router.fleet_completions.wait', return_value={'status': 'deadline', 'consumer': 'batch-job', 'host_id': 'host-a', 'subscription_id': 'subscription-a'}), \
                patch('router.fleet_completions.ssh_stdout', return_value=json.dumps(raw).encode()):
            result = query_completions(self.config, 'batch-job', 15)
        self.assertFalse(result['partial'])
        self.assertEqual([host['name'] for host in result['hosts']], ['worker-a', 'worker-b'])
        self.assertEqual(result['hosts'][0]['host_id'], 'host-a')
        remote = result['hosts'][1]
        self.assertEqual(remote['host_id'], 'host-b')
        self.assertEqual(remote['result'], self.event)
        self.assertNotIn('fixture-private-value', canonical(result))

    def test_bad_or_unavailable_hosts_are_explicit_and_never_empty_success(self):
        from router.fleet_completions import query_completions
        variants = [b'', b'[]', b'not-json', json.dumps({**self.event, 'consumer': 'other'}).encode(),
                    json.dumps({**self.event, 'event': {**self.event['event'], 'event_id': 'completion-01'}}).encode(),
                    json.dumps({**self.event, 'event': {**self.event['event'], 'exit_code': True}}).encode(),
                    json.dumps({**self.event, 'status': 'failed'}).encode(),
                    json.dumps({**self.event, 'event': {**self.event['event'], 'group_outlived': True}}).encode(),
                    json.dumps({**self.event, 'event': {**self.event['event'], 'exit_code': 7}}).encode(),
                    json.dumps({**self.event, 'event': {**self.event['event'], 'exit_code': None}}).encode(),
                    json.dumps({**self.event, 'event': {**self.event['event'], 'signal': 15}}).encode()]
        for raw in variants:
            with self.subTest(raw=raw), patch('router.fleet_completions.ssh_stdout', return_value=raw):
                result = query_completions(self.config, 'batch-job', 15, only='worker-b')
            self.assertTrue(result['partial'])
            self.assertEqual(result['hosts'][0]['error']['code'], 'invalid_response')
        unavailable = {'status': 'unavailable', 'consumer': 'batch-job'}
        with patch('router.fleet_completions.ssh_stdout', return_value=json.dumps(unavailable).encode()):
            result = query_completions(self.config, 'batch-job', 15, only='worker-b')
        self.assertEqual(result['hosts'][0]['error']['code'], 'unavailable')
        self.assertFalse(result['hosts'][0]['ok'])

    def test_optional_metadata_degrades_without_hiding_durable_event_identity(self):
        from router.fleet_completions import query_completions
        for field, value in (('session_id', 'bad\n'), ('agent_id', []), ('managed_id', 'bad\n'), ('work_item_id', 'bad\n'),
                             ('attribution', 'unsupported'), ('finished_at', True), ('uncertain_at', float('nan'))):
            raw = {**self.event, 'event': {**self.event['event'], field: value}}
            with self.subTest(field=field), patch('router.fleet_completions.ssh_stdout',
                                                  return_value=json.dumps(raw).encode()):
                result = query_completions(self.config, 'batch-job', 15, only='worker-b')
            host = result['hosts'][0]
            self.assertTrue(host['ok'])
            self.assertTrue(result['partial'])
            self.assertEqual(host['warning'], {'code': 'invalid_metadata', 'fields': [field]})
            self.assertNotIn(field, host['result']['event'])
            self.assertEqual(host['result']['event']['event_id'], 'completion-1')

    def test_cli_wait_and_host_bound_ack_have_explicit_exit_contracts(self):
        from router.cli import main
        from router.client import CompletionBusy, CompletionRefused
        from router.fleet_completions import query_completions
        cases = [(['fleet', 'wait', '--consumer', 'batch-job', '--timeout', '15'], False, 0),
                 (['fleet', 'wait', '--consumer', 'batch-job', '--timeout', '15'], True, 1)]
        for args, partial, expected in cases:
            result = {'schema_version': 1, 'consumer': 'batch-job', 'hosts': [], 'partial': partial}
            with self.subTest(partial=partial), patch('sys.argv', ['router', *args]), \
                    patch('router.fleet.load_fleet_config', return_value=self.config), \
                    patch('router.fleet_completions.query_completions', return_value=result), \
                    redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()):
                try:
                    main()
                    code = 0
                except SystemExit as error:
                    code = error.code
            self.assertEqual(code, expected)
            self.assertEqual(json.loads(output.getvalue()), result)
        with patch('router.fleet_completions.ssh_stdout') as transport, self.assertRaises(ValueError):
            query_completions(self.config, 'batch-job', float('nan'))
        self.assertEqual(transport.call_count, 0)

        for args, expected in ((['fleet', 'wait', '--consumer', 'batch-job', '--timeout', 'nan'], 2),
                               (['fleet', 'ack', '--host', 'worker-b', '--host-id', 'host-b',
                                 '--event-host-id', 'host-b', '--subscription-id', 'subscription-b', '--consumer', 'batch-job', 'completion-01'], 2)):
            with self.subTest(args=args), patch('sys.argv', ['router', *args]), \
                    patch('router.fleet.load_fleet_config', return_value=self.config), \
                    redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as caught:
                main()
            self.assertEqual(caught.exception.code, expected)
            self.assertEqual(output.getvalue(), '')
        ack_args = ['fleet', 'ack', '--host', 'worker-b', '--host-id', 'host-b', '--event-host-id', 'host-b', '--subscription-id', 'subscription-b',
                    '--consumer', 'batch-job', 'completion-1']
        for ok in (True, False):
            result = {'name': 'worker-b', 'host_id': 'host-b', 'ok': ok}
            with self.subTest(ok=ok), patch('sys.argv', ['router', *ack_args]), \
                    patch('router.fleet.load_fleet_config', return_value=self.config), \
                    patch('router.fleet_completions.acknowledge_completion', return_value=result), \
                    redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()):
                try:
                    main()
                    code = 0
                except SystemExit as error:
                    code = error.code
            self.assertEqual(code, 0 if ok else 1)
            self.assertEqual(json.loads(output.getvalue()), result)
        for args, function in ((ack_args, 'acknowledge_completion'),
                               (['fleet', 'wait', '--consumer', 'batch-job', '--timeout', '15'], 'query_completions')):
            with self.subTest(function=function), patch('sys.argv', ['router', *args]), \
                    patch('router.fleet.load_fleet_config', return_value=self.config), \
                    patch('router.fleet_completions.' + function, side_effect=KeyboardInterrupt), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors, \
                    self.assertRaises(SystemExit) as caught:
                main()
            self.assertEqual(caught.exception.code, 130)
            self.assertIn('unknown', errors.getvalue())
        local_args = ['ack', '--consumer=batch-job', '--host-id=host-b', '--event-host-id=producing-host',
                      '--subscription-id=subscription-b', 'completion-1']
        for error, error_code in ((CompletionBusy('fixture-private-value'), 'database_busy'),
                                  (CompletionRefused('fixture-private-value'), 'acknowledgment_refused'),
                                  (ValueError('fixture-private-value'), None)):
            for flags in ([], ['--json-errors']):
                with self.subTest(error_code=error_code, flags=flags), \
                        patch('sys.argv', ['router', *local_args, *flags]), \
                        patch('router.cli.request', side_effect=error), \
                        redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()), \
                        self.assertRaises(SystemExit) as caught:
                    main()
                self.assertEqual(caught.exception.code, 1)
                if flags and error_code is not None:
                    self.assertEqual(json.loads(output.getvalue()), {
                        'schema_version': 1, 'consumer': 'batch-job', 'host_id': 'host-b',
                        'event_host_id': 'producing-host', 'subscription_id': 'subscription-b',
                        'event_id': 'completion-1', 'error': {'code': error_code}})
                else:
                    self.assertEqual(output.getvalue(), '')

    def test_duplicate_hosts_deadlines_and_invalid_arguments_remain_explicit(self):
        from router.fleet_completions import acknowledge_completion, query_completions
        for status, raw_status, code, signum, group in (
                ('failed', 'failed', 1, None, False), ('failed', 'signaled', None, 15, True),
                ('uncertain', 'unknown', None, None, None), ('uncertain', 'exited', 0, None, True)):
            event = {**self.event, 'status': status,
                     'event': {**self.event['event'], 'status': raw_status, 'exit_code': code,
                               'signal': signum, 'group_outlived': group}}
            with self.subTest(status=status, raw_status=raw_status), patch('router.fleet_completions.ssh_stdout',
                                                                         return_value=json.dumps(event).encode()):
                result = query_completions(self.config, 'batch-job', 15, only='worker-b')
            self.assertFalse(result['partial'])
            self.assertEqual(result['hosts'][0]['result']['status'], status)
        with patch('router.fleet_completions.wait', return_value=self.event), \
                patch('router.fleet_completions.ssh_stdout', return_value=json.dumps(self.event).encode()):
            result = query_completions(self.config, 'batch-job', 15)
        self.assertTrue(result['partial'])
        self.assertEqual([host['error']['code'] for host in result['hosts']], ['duplicate_host_id'] * 2)
        self.assertTrue(all('result' not in host for host in result['hosts']))
        for options in ({'consumer': ''}, {'timeout': -1}, {'timeout': 3601}, {'only': 'missing'}):
            arguments = {'consumer': 'batch-job', 'timeout': 15}
            arguments.update(options)
            with self.subTest(options=options), patch('router.fleet_completions.ssh_stdout') as transport, \
                    self.assertRaises(ValueError):
                query_completions(self.config, **arguments)
            self.assertEqual(transport.call_count, 0)
        with self.assertRaises(ValueError):
            acknowledge_completion(self.config, None, 'batch-job', 'completion-1', 'host-b', 'host-b', 'subscription-b')

    def test_separate_hosts_replay_after_lost_replies_restart_and_route_changes(self):
        from router.fleet_completions import acknowledge_completion, query_completions
        popen = subprocess.Popen
        with tempfile.TemporaryDirectory(prefix="ar-fleet-completions-") as temporary:
            root = Path(temporary)
            project = root / "example-project"
            project.mkdir()
            (project / ".agent-router.json").write_text('{"observe": true}\n')
            processes, readers, environments, endpoints = [], [], [], []
            launcher = """import os,sys,router.host
from pathlib import Path
from router.daemon import Controller
identity=sys.argv[1]
router.host.stable_host_id=lambda directory: identity
original=Controller.handle
def handle(self, operation, payload):
    result=original(self, operation, payload)
    if operation == 'completion_next' and result.get('status') == 'pending':
        Path(os.environ['FIXTURE_PENDING']).touch()
    return result
Controller.handle=handle
from router.cli import main
sys.argv=['router','daemon','start']
main()
"""
            def stop(process, endpoint):
                if process.poll() is None:
                    try:
                        router_request("daemon_stop", {}, endpoint, timeout=15, deadline=True)
                        process.wait(timeout=15)
                    except (ValueError, OSError, subprocess.TimeoutExpired):
                        process.terminate()
                        try:
                            process.wait(timeout=15)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=15)
                process.communicate()
            def start(number):
                endpoint = root / f"host-{number}.sock"
                environment = {key: value for key, value in os.environ.items()
                               if key in {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SHELL", "USER", "LOGNAME"}}
                environment.update(AGENT_ROUTER_STATE_DIR=str(root / f"state-{number}"),
                                   AGENT_ROUTER_SOCKET=str(endpoint), AGENT_ROUTER_ENV_FILE=str(root / "missing.env"),
                                   PYTHONPATH=str(Path(__file__).resolve().parents[1]),
                                   FIXTURE_PENDING=str(root / f"pending-{number}"))
                home = root / f'home-{number}'
                home.mkdir(exist_ok=True)
                environment['HOME'] = str(home)
                process = popen([sys.executable, "-c", launcher, f"--emulated-host-{number}"], env=environment,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
                processes.append((process, endpoint))
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        _, error = process.communicate()
                        self.fail("Simulated daemon exited: " + error.decode(errors="replace")[-1000:])
                    try:
                        router_request("health", {}, endpoint, timeout=.5, deadline=True)
                        return environment, endpoint
                    except (ValueError, OSError):
                        time.sleep(.02)
                self.fail("Simulated daemon did not start within 15 seconds")
            def cli(number, *arguments):
                completed = subprocess.run([sys.executable, "-m", "router.cli", *arguments],
                                           env=environments[number], cwd=project, capture_output=True, timeout=15, check=False)
                self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))
                return completed.stdout
            drop, fail_after_reply, route = [False], [False], [1]
            def remote(command, **options):
                arguments = shlex.split(" ".join(command[command.index("--") + 2:]))
                self.assertEqual(arguments[0], "router")
                if drop[0]:
                    drop[0] = False
                    source = "import io,sys;sys.stdout=io.StringIO();from router.cli import main;main()"
                    argv = [sys.executable, "-c", source, *arguments[1:]]
                elif fail_after_reply[0]:
                    fail_after_reply[0] = False
                    argv = [sys.executable, '-c', 'from router.cli import main;main();raise SystemExit(1)',
                            *arguments[1:]]
                else:
                    argv = [sys.executable, "-m", "router.cli", *arguments[1:]]
                process = popen(argv, env=environments[route[0]], **options)
                readers.append(process)
                return process
            def local(operation, payload, **options):
                return router_request(operation, payload, endpoints[0], **options)
            try:
                for number in range(2):
                    environment, endpoint = start(number)
                    environments.append(environment)
                    endpoints.append(endpoint)
                    script = ("from pathlib import Path; p=Path('executions-" + str(number) + "'); "
                              "p.write_text(p.read_text()+'x' if p.exists() else 'x')")
                    for _ in range(2):
                        cli(number, "run", "--", sys.executable, "-c", script)
                with patch('router.fleet.subprocess.Popen', side_effect=remote), \
                        patch('router.managed.request', side_effect=local), \
                        patch('router.fleet_completions.request', side_effect=local):
                    first = query_completions(self.config, 'batch-job', 15)
                    self.assertFalse(first['partial'], first)
                    self.assertEqual([host['host_id'] for host in first['hosts']],
                                     ['--emulated-host-0', '--emulated-host-1'])
                    leading = query_completions(self.config, '--help', 15, only='worker-b')
                    self.assertFalse(leading['partial'], leading)
                    self.assertEqual(leading['hosts'][0]['result']['consumer'], '--help')
                    remote_event = first['hosts'][1]['result']
                    event_id = remote_event['event']['event_id']
                    def ack():
                        return acknowledge_completion(self.config, 'worker-b', 'batch-job', event_id,
                                                      '--emulated-host-1', '--emulated-host-1', remote_event['subscription_id'])
                    connection = sqlite3.connect(root / 'state-1' / 'router.sqlite3')
                    try:
                        connection.execute('BEGIN IMMEDIATE')
                        busy = ack()
                        self.assertEqual(busy['error'], {'code': 'database_busy', 'exit_code': 1,
                                                         'acknowledgment': 'unknown'})
                        self.assertFalse(busy['ok'])
                        self.assertIsNone(busy['host_id'])
                        arguments = ['ack', '--consumer=batch-job', '--host-id=--emulated-host-1',
                                     '--event-host-id=--emulated-host-1',
                                     '--subscription-id=' + remote_event['subscription_id'], event_id]
                        for flags in ([], ['--json-errors']):
                            with popen([sys.executable, '-m', 'router.cli', *arguments, *flags],
                                       env=environments[1], cwd=project,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE) as direct:
                                try:
                                    output, diagnostic = direct.communicate(timeout=15)
                                except subprocess.TimeoutExpired:
                                    direct.kill()
                                    direct.communicate()
                                    raise
                            self.assertEqual(direct.returncode, 1, diagnostic)
                            self.assertIn(b'Completion database is busy', diagnostic)
                            if flags:
                                self.assertEqual(json.loads(output)['error']['code'], 'database_busy')
                            else:
                                self.assertEqual(output, b'')
                    finally:
                        connection.rollback()
                        connection.close()
                    refusal = {'consumer': 'batch-job', 'event_id': event_id, 'host_id': '--emulated-host-1',
                               'event_host_id': '--emulated-host-1', 'subscription_id': remote_event['subscription_id']}
                    for field, value in (('consumer', 'unknown-consumer'), ('event_id', 'completion-999999'),
                                         ('event_id', 'completion-2'), ('host_id', 'wrong-host'),
                                         ('event_host_id', 'wrong-producer'), ('subscription_id', 'wrong-subscription')):
                        with self.subTest(refused_field=field, value=value):
                            refused = acknowledge_completion(self.config, 'worker-b', **{**refusal, field: value})
                            self.assertEqual(refused['error'], {'code': 'acknowledgment_refused', 'exit_code': 1,
                                                                'acknowledgment': 'unknown'})
                            self.assertFalse(refused['ok'])
                            self.assertIsNone(refused['host_id'])
                    self.assertEqual(query_completions(self.config, 'batch-job', 15, only='worker-b')
                                     ['hosts'][0]['result'], remote_event)
                    drop[0] = True
                    self.assertTrue(query_completions(self.config, 'batch-job', 15, only='worker-b')['partial'])
                    stop(*processes[1])
                    environments[1], _ = start(1)
                    replay = query_completions(self.config, 'batch-job', 15, only='worker-b')
                    self.assertEqual(replay['hosts'][0]['result'], remote_event)
                    route[0] = 0
                    refused = ack()
                    self.assertFalse(refused['ok'])
                    self.assertEqual(refused['error']['code'], 'acknowledgment_refused')
                    self.assertEqual(refused['error']['exit_code'], 1)
                    local_replay = query_completions(self.config, 'batch-job', 15, only='worker-a')
                    self.assertEqual(local_replay['hosts'][0]['result'], first['hosts'][0]['result'])
                    self.assertEqual(refused['error']['acknowledgment'], 'unknown')
                    route[0] = 1
                    self.assertEqual(query_completions(self.config, 'batch-job', 15, only='worker-b')
                                     ['hosts'][0]['result'], remote_event)
                    drop[0] = True
                    self.assertFalse(ack()['ok'])
                    self.assertTrue(ack()['ok'])
                    self.assertTrue(ack()['ok'])
                    fail_after_reply[0] = True
                    failed_reply = ack()
                    self.assertFalse(failed_reply['ok'])
                    self.assertEqual(failed_reply['error'], {'code': 'exit_failed', 'exit_code': 1,
                                                            'acknowledgment': 'unknown'})
                    second = query_completions(self.config, 'batch-job', 15, only='worker-b')['hosts'][0]['result']
                    self.assertNotEqual(second['event']['event_id'], event_id)
                    self.assertTrue(acknowledge_completion(self.config, 'worker-b', 'batch-job',
                                    second['event']['event_id'], '--emulated-host-1', '--emulated-host-1', remote_event['subscription_id'])['ok'])
                    pending, release = root / 'pending-1', root / 'release-job'
                    pending.unlink(missing_ok=True)
                    source = ("import time;from pathlib import Path; "
                              f"release=Path({str(release)!r}); "
                              "deadline=time.monotonic()+15\n"
                              "while not release.exists() and time.monotonic()<deadline: time.sleep(.02)\n"
                              "assert release.exists()")
                    job = popen([sys.executable, '-m', 'router.cli', 'run', '--', sys.executable, '-c', source],
                                env=environments[1], cwd=project, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    synchronized = []
                    def finish_during_wait():
                        end = time.monotonic() + 15
                        while not pending.exists() and time.monotonic() < end:
                            time.sleep(.02)
                        synchronized.append(pending.exists())
                        release.touch()
                    finisher = threading.Thread(target=finish_during_wait)
                    finisher.start()
                    try:
                        during = query_completions(self.config, 'batch-job', 15, only='worker-b')
                        self.assertFalse(during['partial'], during)
                        self.assertEqual(during['hosts'][0]['result']['status'], 'completed')
                        self.assertTrue(acknowledge_completion(self.config, 'worker-b', 'batch-job',
                                        during['hosts'][0]['result']['event']['event_id'],
                                        '--emulated-host-1', '--emulated-host-1', remote_event['subscription_id'])['ok'])
                    finally:
                        release.touch()
                        finisher.join(timeout=15)
                        job.communicate(timeout=15)
                    self.assertEqual(synchronized, [True])
                    self.assertEqual(job.returncode, 0)
                    deadline = query_completions(self.config, 'batch-job', 1, only='worker-b')
                    self.assertFalse(deadline['partial'], deadline)
                    self.assertEqual(deadline['hosts'][0]['result']['status'], 'deadline')
                    stop(*processes[-1])
                    failed = query_completions(self.config, 'batch-job', 15)
                    self.assertTrue(failed['partial'])
                    self.assertTrue(failed['hosts'][0]['ok'])
                    self.assertEqual(failed['hosts'][1]['error']['code'], 'unavailable')
                self.assertEqual([(project / f'executions-{number}').read_text() for number in range(2)], ['xx', 'xx'])
                self.assertTrue(all(process.poll() is not None for process in readers))
            finally:
                for process, endpoint in reversed(processes):
                    stop(process, endpoint)

    def test_ssh_errors_bounds_quoting_and_interrupted_reader_cleanup(self):
        from router.fleet import SSHQueryError, ssh_stdout
        from router.fleet_completions import query_completions
        config = FleetConfig((self.config.hosts[1],), ssh_timeout_seconds=15)
        popen, processes, commands = subprocess.Popen, [], []
        def launch(source):
            def start(command, **options):
                commands.append(command)
                process = popen([sys.executable, '-c', source], **options)
                processes.append(process)
                return process
            return start
        rejected = ["import sys;sys.stderr.write('fixture-private-value');sys.exit(255)"]
        for reply in (self.event, {'status': 'deadline', 'consumer': 'batch-job', 'host_id': 'host-b',
                                   'subscription_id': 'subscription-b'}):
            rejected.append(f"import sys;sys.stdout.write({json.dumps(reply)!r});"
                            "sys.stderr.write('\\x1b[0m');sys.exit(255)")
        for source, code in [(source, 'exit_failed') for source in rejected] + [
                ("import os;os.write(1,b'x'*(1024*1024+1))", 'output_limit')]:
            with self.subTest(code=code), patch('router.fleet.subprocess.Popen', side_effect=launch(source)):
                result = query_completions(config, 'batch-job', 15)
            self.assertFalse(result['hosts'][0]['ok'])
            self.assertEqual(result['hosts'][0]['error']['code'], code)
            self.assertNotIn('fixture-private-value', canonical(result))
            self.assertIsNotNone(processes[-1].poll())
            if code == 'exit_failed':
                with patch('router.fleet.subprocess.Popen', side_effect=launch(source)), \
                        self.assertRaises(SSHQueryError) as caught:
                    ssh_stdout(config.hosts[0], 15, ['wait'])
                self.assertTrue(str(caught.exception))
        with patch('router.fleet.subprocess.Popen', side_effect=FileNotFoundError(2, 'fixture-private-value')):
            result = query_completions(config, 'batch-job', 15)
        self.assertEqual(result['hosts'][0]['error'], {'code': 'spawn_failed', 'errno': 2})
        with patch('router.fleet.subprocess.Popen', side_effect=launch('import time;time.sleep(30)')), \
                patch('router.fleet.os.set_blocking', side_effect=OSError('')), \
                self.assertRaises(SSHQueryError) as caught:
            ssh_stdout(config.hosts[0], 15, ['wait'])
        self.assertEqual(caught.exception.code, 'transport_failed')
        self.assertTrue(str(caught.exception))
        self.assertIsNone(caught.exception.errno)
        self.assertIsNotNone(processes[-1].poll())
        read = os.read
        reader_fds = set()
        def read_failure(fd, size):
            if fd in reader_fds:
                raise OSError(errno.EIO, 'fixture-private-value')
            return read(fd, size)
        for boundary, failure, expected_errno in [
                ('set_blocking', OSError(errno.EBADF, 'fixture-private-value'), errno.EBADF),
                ('read', read_failure, errno.EIO)]:
            reader_fds.clear()
            start = launch('import os,time;os.write(1,b"x");time.sleep(30)')
            def start_reader(command, start=start, **options):
                process = start(command, **options)
                reader_fds.update((process.stdout.fileno(), process.stderr.fileno()))
                return process
            with self.subTest(boundary=boundary, errno=expected_errno), \
                    patch('router.fleet.subprocess.Popen', side_effect=start_reader), \
                    patch('router.fleet.os.' + boundary, side_effect=failure):
                result = query_completions(config, 'batch-job', 15)
                expected = {'code': 'transport_failed'}
                if expected_errno is not None:
                    expected['errno'] = expected_errno
                self.assertEqual(result['hosts'][0]['error'], expected)
                self.assertFalse(result['hosts'][0]['ok'])
                self.assertNotIn('fixture-private-value', canonical(result))
                self.assertIsNotNone(processes[-1].poll())
        with patch('router.fleet.subprocess.Popen', side_effect=launch('import time;time.sleep(30)')), \
                patch('router.fleet.time.monotonic', side_effect=[0, 16]), \
                patch('router.fleet._kill_remaining_group'), self.assertRaises(SSHQueryError) as caught:
            # The fixture owns only the sleeping leader; its clock cannot drive group cleanup.
            ssh_stdout(config.hosts[0], 15, ['wait'])
        self.assertEqual(caught.exception.code, 'deadline')
        self.assertIsNotNone(processes[-1].poll())
        with tempfile.TemporaryDirectory(prefix='ar-ssh-descendant-') as temporary:
            source = ("import subprocess,sys;from pathlib import Path; "
                      "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
                      "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
                      f"Path({str(Path(temporary) / 'child')!r}).write_text(str(child.pid));"
                      "sys.stderr.write('\\x1b[0m');sys.exit(255)")
            with patch('router.fleet.subprocess.Popen', side_effect=launch(source)):
                result = query_completions(config, 'batch-job', 15)
            child = int((Path(temporary) / 'child').read_text())
            try:
                end = time.monotonic() + 15
                while time.monotonic() < end:
                    try:
                        os.kill(child, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(.02)
                else:
                    self.fail('Failed SSH reader left an owned descendant alive')
                self.assertEqual(result['hosts'][0]['error'], {'code': 'exit_failed', 'exit_code': 255})
            finally:
                try:
                    os.kill(child, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        consumer = "worker; $(false) ' quoted"
        with patch('router.fleet.subprocess.Popen', side_effect=launch("print('{}')")):
            ssh_stdout(config.hosts[0], 15, ['wait', '--consumer', consumer, '--timeout', '15'])
        arguments = shlex.split(' '.join(commands[-1][commands[-1].index('--') + 2:]))
        self.assertEqual(arguments, ['router', 'wait', '--consumer', consumer, '--timeout', '15'])
        cancelled = threading.Event()
        cancelled.set()
        with patch('router.fleet.subprocess.Popen', side_effect=launch('import time;time.sleep(30)')), \
                self.assertRaises(SSHQueryError) as caught:
            ssh_stdout(config.hosts[0], 15, ['wait'], cancelled=cancelled)
        self.assertEqual(caught.exception.code, 'interrupted')
        self.assertIsNotNone(processes[-1].poll())
