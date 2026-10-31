import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import resource_guard as g


def sample():
    psi = {'some': {'avg10': 0.0}, 'full': {'avg10': 0.0}}
    guest = {'identity': [1, 20], 'current': 4*g.GIB, 'high': 12*g.GIB, 'max': 18*g.GIB,
             'events': {'oom_kill': 100}, 'memory_psi': copy.deepcopy(psi), 'io_psi': copy.deepcopy(psi)}
    return {'time': 100, 'boot': 'boot-a', 'errors': [], 'host': {
        'available': 12*g.GIB, 'total': 32*g.GIB, 'oom_kill': 200,
        'memory_psi': copy.deepcopy(psi), 'io_psi': copy.deepcopy(psi)},
        'guests': {'lxc.payload.guest': guest}}


def item():
    return {'id': 'a'*64, 'created': '2026-10-31T00:00:00Z', 'labels': {
        g.LABEL+'oom-recover': 'true', g.LABEL+'recovery-key': 'api',
        g.LABEL+'restart-memory-mib': '512', 'coolify.applicationId': '9',
        'com.docker.compose.service': 'api'}, 'state': {
            'Status': 'exited', 'ExitCode': 137, 'OOMKilled': True,
            'Running': False, 'Restarting': False,
            'FinishedAt': (dt.datetime.now(dt.timezone.utc)-dt.timedelta(minutes=1)).isoformat()}}


class PressureTests(unittest.TestCase):
    def test_private_docker_cgroup_namespace_maps_guest(self):
        for path in ('0::/lxc.payload.guest/init.scope', '0::/../../lxc.payload.project_guest/init.scope'):
            expected = 'lxc.payload.project_guest' if 'project_' in path else 'lxc.payload.guest'
            self.assertEqual(g.guest_cgroup(path), expected)
        for invalid in ('0::/system.slice/docker.scope', '1:memory:/guest'):
            with self.assertRaises(ValueError):
                g.guest_cgroup(invalid)

    def test_psi_validation(self):
        self.assertEqual(g.pressure('some avg10=12.5 total=10\nfull avg10=1 total=2')['some']['avg10'], 12.5)
        for text in ('some avg10=0', 'some avg10=NaN\nfull avg10=0', 'some avg10=-1\nfull avg10=0'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                g.pressure(text)

    def test_guest_pressure_with_plenty_of_host_ram(self):
        s = sample()
        s['guests']['lxc.payload.guest']['memory_psi']['some']['avg10'] = 80
        self.assertIn('guest-memory-pressure', g.reasons(s, guest=s['guests']['lxc.payload.guest']))
        self.assertEqual(g.reasons(s), [])

    def test_io_stall_and_restart_budget(self):
        s = sample()
        s['host']['io_psi']['full']['avg10'] = 60
        self.assertIn('host-io-stall', g.reasons(s))
        self.assertIn('host-memory-reserve', g.reasons(s, required=9*g.GIB))

    def test_soft_limit_matters_before_hard_limit(self):
        s = sample()
        guest = s['guests']['lxc.payload.guest']
        guest['current'] = 11*g.GIB
        self.assertIn('guest-memory-headroom', g.reasons(s, guest=guest))

    def test_missing_telemetry_blocks(self):
        s = sample()
        s['errors'] = [{'cgroup': 'guest'}]
        self.assertIn('incomplete-telemetry', g.reasons(s))

    def test_counter_baselines_and_identity_reset(self):
        old, new = sample(), sample()
        self.assertEqual(g.changes(None, new), [])
        new['host']['oom_kill'] += 1
        new['guests']['lxc.payload.guest']['events']['oom_kill'] += 1
        self.assertEqual(len(g.changes(old, new)), 2)
        new['guests']['lxc.payload.guest']['identity'] = [1, 21]
        self.assertEqual(len(g.changes(old, new)), 1)
        new['boot'] = 'boot-b'
        self.assertEqual(g.changes(old, new), [])

    def test_counter_decrease_is_not_new_oom(self):
        old, new = sample(), sample()
        new['host']['oom_kill'] = 0
        new['guests']['lxc.payload.guest']['events']['oom_kill'] = 0
        self.assertEqual(g.changes(old, new), [])

    def test_forecast_resets_on_recreation(self):
        old, new = sample(), sample()
        new['time'] = 105
        new['guests']['lxc.payload.guest']['current'] = 17*g.GIB
        self.assertEqual(len(g.forecasts(old, new)), 1)
        new['guests']['lxc.payload.guest']['identity'] = [1, 21]
        self.assertEqual(g.forecasts(old, new), [])


class CandidateTests(unittest.TestCase):
    def test_old_future_and_invalid_incident_times_block(self):
        for value in ('2020-01-01T00:00:00Z', '2099-01-01T00:00:00Z', 'bad', '2026-10-31T01:00:00'):
            i = item()
            i['state']['FinishedAt'] = value
            self.assertIsNone(g.candidate(i, [i]))
    def test_opt_in_and_confirmed_oom_required(self):
        i = item()
        self.assertEqual(g.candidate(i, [i]), ('api', 512*g.MIB))
        for key, value in (('Running', True), ('OOMKilled', False), ('ExitCode', 0),
                           ('Status', 'dead'), ('Restarting', True)):
            changed = copy.deepcopy(i)
            changed['state'][key] = value
            with self.subTest(key=key):
                self.assertIsNone(g.candidate(changed, [changed]))

    def test_invalid_restart_budgets(self):
        for value in ('', '0', '-1', 'nan', '63', '10000000000'):
            i = item()
            i['labels'][g.LABEL+'restart-memory-mib'] = value
            self.assertIsNone(g.candidate(i, [i]))

    def test_no_opt_in_and_invalid_key(self):
        for key, value in (('oom-recover', 'false'), ('recovery-key', '../../api')):
            i = item()
            i['labels'][g.LABEL+key] = value
            self.assertIsNone(g.candidate(i, [i]))

    def test_newer_opted_out_deployment_blocks_old(self):
        old = item()
        new = copy.deepcopy(old)
        new['id'] = 'b'*64
        new['created'] = '2026-10-31T02:00:00Z'
        new['labels'] = {'coolify.applicationId': '9', 'com.docker.compose.service': 'api'}
        self.assertIsNone(g.candidate(old, [old, new]))

    def test_running_replica_blocks_duplicate(self):
        old = item()
        running = copy.deepcopy(old)
        running['id'] = 'b'*64
        running['created'] = '2026-10-30T02:00:00Z'
        running['state']['Running'] = True
        self.assertIsNone(g.candidate(old, [old, running]))


class StoreTests(unittest.TestCase):
    def test_persistent_cooldown_and_same_incident(self):
        with tempfile.TemporaryDirectory() as directory:
            store = g.Store(Path(directory))
            self.assertTrue(store.reserve('host:api', 'incident1', 10000))
            self.assertFalse(store.reserve('host:api', 'incident2', 10001))
            store.lock.close()
            store = g.Store(Path(directory))
            self.assertFalse(store.reserve('host:api', 'incident1', 15000))
            self.assertTrue(store.reserve('host:api', 'incident2', 15000))
            store.lock.close()

    def test_global_and_daily_attempt_budgets(self):
        with tempfile.TemporaryDirectory() as directory:
            store = g.Store(Path(directory))
            for x in range(3):
                self.assertTrue(store.reserve(str(x), str(x), 10000))
            self.assertFalse(store.reserve('fourth', '4', 10001))
            self.assertTrue(store.reserve('0', 'new1', 15000))
            self.assertTrue(store.reserve('0', 'new2', 20000))
            self.assertFalse(store.reserve('0', 'new3', 25000))
            store.lock.close()

    def test_corrupt_state_never_resets_silently(self):
        for content in ('{"api": [{}]}', '[]', '{"api": [{"time": "bad", "incident": "x"}]}'):
            with tempfile.TemporaryDirectory() as directory:
                (Path(directory)/'recovery.json').write_text(content)
                with self.assertRaises(ValueError):
                    g.Store(Path(directory))

    def test_second_writer_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = g.Store(Path(directory))
            with self.assertRaises(BlockingIOError):
                g.Store(Path(directory))
            store.lock.close()


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = g.Store(Path(temp.name))
        self.addCleanup(self.store.lock.close)
        self.scope = {'name': 'host', 'cgroup': None}

    def invoke(self, apply=True, current=None):
        g.recover(self.scope, current or sample(), self.store, apply, Path('/proc'), Path('/sys/fs/cgroup'), 4*g.GIB)

    @patch.object(g, 'emit')
    @patch.object(g, 'docker')
    @patch.object(g, 'inventory', return_value=[item()])
    def test_observe_never_starts(self, inventory, docker, emit):
        self.invoke(apply=False)
        docker.assert_not_called()

    @patch.object(g, 'emit')
    @patch.object(g, 'docker')
    @patch.object(g, 'inventory', return_value=[item()])
    def test_pressure_blocks_opted_in_recovery(self, inventory, docker, emit):
        current = sample()
        current['host']['available'] = g.GIB
        self.invoke(current=current)
        docker.assert_not_called()

    @patch.object(g, 'emit')
    @patch.object(g, 'docker')
    @patch.object(g, 'snapshot', return_value=sample())
    @patch.object(g, 'inventory', return_value=[item()])
    def test_start_once_persisted_before_action(self, inventory, snapshot, docker, emit):
        def start(*args):
            self.assertTrue(json.loads(self.store.path.read_text()))
        docker.side_effect = start
        self.invoke()
        self.invoke()
        docker.assert_called_once_with(self.scope, 'start', 'a'*64)

    @patch.object(g, 'emit')
    @patch.object(g, 'docker', side_effect=RuntimeError('daemon failure'))
    @patch.object(g, 'snapshot', return_value=sample())
    @patch.object(g, 'inventory', return_value=[item()])
    def test_failed_start_keeps_budget(self, inventory, snapshot, docker, emit):
        with self.assertRaises(RuntimeError):
            self.invoke()
        self.invoke()
        self.assertEqual(docker.call_count, 1)

    @patch.object(g, 'emit')
    @patch.object(g, 'docker')
    @patch.object(g, 'inventory', side_effect=[[item()], []])
    def test_deployment_removed_during_check(self, inventory, docker, emit):
        self.invoke()
        docker.assert_not_called()

    @patch.object(g, 'emit')
    @patch.object(g, 'docker')
    @patch.object(g, 'snapshot')
    @patch.object(g, 'inventory', return_value=[item()])
    def test_pressure_returns_before_start(self, inventory, snapshot, docker, emit):
        current = sample()
        current['host']['io_psi']['full']['avg10'] = 80
        snapshot.return_value = current
        self.invoke()
        docker.assert_not_called()

    @patch.object(g, 'emit')
    @patch.object(g, 'docker')
    @patch.object(g, 'inventory', return_value=[item()])
    def test_missing_guest_is_not_healthy(self, inventory, docker, emit):
        self.scope = {'name': 'default/missing', 'cgroup': 'lxc.payload.missing'}
        self.invoke()
        docker.assert_not_called()


if __name__ == '__main__':
    unittest.main()
