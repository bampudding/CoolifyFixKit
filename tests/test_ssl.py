import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'check_and_heal.sh'


class SslTests(unittest.TestCase):
    def test_only_https_sites_and_bare_domains_are_monitored(self):
        code = '''
docker_cmd() {
    if [[ "$1" == ps ]]; then echo abc; else
        echo '{"caddy":"bare.example.com,https://tls.example.com http://plain.example.com","caddy_1":"https://two.example.com/path","caddy.reverse_proxy":"{{upstreams 80}}"}'
    fi
}
get_domains
'''
        _, _, output = self.execute(code)
        self.assertEqual(output.splitlines(), ['bare.example.com', 'tls.example.com', 'two.example.com'])

    def execute(self, code, *, apply='true', critical='true', pressure=0, route=2, restart=0):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = {**os.environ, 'GUARD_STATE': directory, 'SSL_APPLY': apply,
                   'SSL_CRITICAL_RESTART': critical,
                   'CALLS': str(root/'calls'), 'PRESSURE': str(pressure),
                   'ROUTE': str(route), 'RESTART': str(restart), 'SCRIPT': str(SCRIPT)}
            setup = '''
source "$SCRIPT"
timeout() { shift; "$@"; }
python3() { return "$PRESSURE"; }
has_caddy_route() { return "$ROUTE"; }
docker() { echo "$*" >> "$CALLS"; return "$RESTART"; }
'''
            result = subprocess.run(['bash', '-c', setup + code], env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = (root/'calls').read_text() if (root/'calls').exists() else ''
            stamp = (root/'ssl-restart-at').read_text() if (root/'ssl-restart-at').exists() else ''
            return calls, stamp, result.stdout

    def test_observation_never_restarts(self):
        self.assertEqual(self.execute('heal_domain example.com', apply='false')[0], '')

    def test_pressure_blocks_proxy_restart(self):
        self.assertEqual(self.execute('heal_domain example.com', pressure=1)[0], '')

    def test_critical_proxy_requires_explicit_last_resort_override(self):
        self.assertEqual(self.execute('heal_domain example.com', critical='false')[0], '')

    def test_live_route_never_triggers_certificate_repair(self):
        self.assertEqual(self.execute('heal_domain example.com', route=0)[0], '')

    def test_missing_route_requires_coolify_reconciliation(self):
        self.assertEqual(self.execute('heal_domain example.com', route=1)[0], '')

    def test_global_cooldown_covers_other_domains(self):
        calls, stamp, _ = self.execute('heal_domain a.example.com; heal_domain b.example.com')
        self.assertEqual(calls.count('restart --time 10 coolify-proxy'), 1)
        self.assertTrue(stamp.strip().isdigit())

    def test_failed_restart_keeps_cooldown(self):
        calls, stamp, _ = self.execute('heal_domain a.example.com; heal_domain b.example.com', restart=1)
        self.assertEqual(calls.count('restart'), 1)
        self.assertTrue(stamp)

    def test_corrupt_state_blocks_action(self):
        calls, _, _ = self.execute('echo bad > "$STATE_DIR/ssl-restart-at"; heal_domain example.com')
        self.assertEqual(calls, '')


if __name__ == '__main__':
    unittest.main()
