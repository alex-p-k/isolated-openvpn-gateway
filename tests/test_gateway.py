import importlib.util
import contextlib
import io
import json
import os
import pathlib
import subprocess
import struct
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
def module(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'scripts'/(name+'.py'))
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);return mod
gateway=module('gateway')
vpn=module('vpn')
browser=module('browser')
import gateway_config
CONFIG=gateway_config.load_config(ROOT/'gateway.example.toml')

PROFILE='''client
dev tun
persist-tun
persist-key
auth-user-pass
remote vpn.example.com 443 tcp-client
http-proxy 192.0.2.80 443
ncp-ciphers AES-128-GCM
cipher AES-128-CBC
remote-cert-tls server
auth SHA256
key-direction 1
<ca>
OPAQUE CA TEST FIXTURE
</ca>
<tls-auth>
OPAQUE KEY TEST FIXTURE
</tls-auth>
'''

class Tests(unittest.TestCase):
    def setUp(self):
        self.config_patch=patch.object(gateway,'configuration',return_value=CONFIG)
        self.config_patch.start();self.addCleanup(self.config_patch.stop)

    def test_browser_resolver_does_not_blackhole_the_proxy_itself(self):
        flags=browser.browser_flags(CONFIG)
        rules=next(flag for flag in flags if flag.startswith('--host-resolver-rules='))
        self.assertIn('MAP * ~NOTFOUND',rules)
        self.assertIn('EXCLUDE 127.0.0.1',rules)
        self.assertIn('--proxy-bypass-list=<-loopback>',flags)
        self.assertIn('--proxy-server=socks5://127.0.0.1:1080',flags)
        self.assertFalse(any('direct://' in flag for flag in flags))

    def test_socks_dns_query_checks_real_payload_and_handles_partial_reads(self):
        question=b'\x08internal\x07example\x03com\x00'+struct.pack('!2H',1,1)
        answer=b'\xc0\x0c'+struct.pack('!HHIH',1,1,300,4)+bytes([198,51,100,42])
        reply=struct.pack('!6H',0x4356,0x8180,1,1,0,0)+question+answer
        class Stream:
            def __init__(self):
                self.buffer=b'\x05\x00\x05\x00\x00\x01'+bytes([203,0,113,20,40,40])+struct.pack('!H',len(reply))+reply
                self.sent=[]
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def settimeout(self,value):pass
            def sendall(self,data):self.sent.append(data)
            def recv(self,length):
                chunk,self.buffer=self.buffer[:1],self.buffer[1:]
                return chunk
        stream=Stream()
        with patch.object(gateway.socket,'create_connection',return_value=stream):
            self.assertTrue(gateway.socks_dns_query('192.0.2.53'))
        self.assertEqual(stream.sent[1],b'\x05\x01\x00\x01'+bytes([192,0,2,53])+struct.pack('!H',53))

    def test_socks_dns_query_refuses_failed_connection(self):
        with patch.object(gateway.socket,'create_connection',side_effect=ConnectionRefusedError):
            self.assertFalse(gateway.socks_dns_query('192.0.2.53'))

    def test_failure_test_requires_positive_control_before_stopping_vpn(self):
        with patch.object(gateway,'container_id',return_value='vpn'), patch.object(gateway,'ready',return_value=True), \
             patch.object(gateway,'read_json',return_value={'dns':['192.0.2.53']}), \
             patch.object(gateway,'socks_dns_query',return_value=False), patch.object(gateway,'docker') as docker:
            with self.assertRaises(gateway.GatewayError):
                gateway.failure_test()
            docker.assert_not_called()

    def test_derivation_preserves_security_and_proxy(self):
        result=gateway.derive_config(PROFILE,CONFIG['transports']['tcp'])
        self.assertIn('http-proxy 192.0.2.80 443',result)
        self.assertIn('data-ciphers AES-128-GCM\n',result)
        self.assertIn('data-ciphers-fallback AES-128-CBC\n',result)
        self.assertIn('remote-cert-tls server',result)
        self.assertIn('auth SHA256',result)
        self.assertIn('<tls-auth>\nOPAQUE KEY TEST FIXTURE\n</tls-auth>',result)
        self.assertNotIn('auth-user-pass',result)
        self.assertNotIn('persist-tun',result)

    def test_profile_endpoint_is_pinned_by_deployment(self):
        changed=PROFILE.replace('vpn.example.com 443','other.example.com 443')
        with self.assertRaises(gateway.GatewayError):
            gateway.derive_config(changed,CONFIG['transports']['tcp'])

    def test_empty_required_inline_block_is_rejected(self):
        changed=PROFILE.replace('OPAQUE CA TEST FIXTURE','')
        with self.assertRaises(gateway.GatewayError):
            gateway.derive_config(changed,CONFIG['transports']['tcp'])

    def test_direct_tcp_profile_selects_tcp_firewall_protocol(self):
        text=PROFILE.replace('http-proxy 192.0.2.80 443\n','')
        with patch.object(vpn.socket,'getaddrinfo',return_value=[
                (vpn.socket.AF_INET,vpn.socket.SOCK_STREAM,6,'',('192.0.2.44',0))]):
            _,endpoint,port,protocol=vpn.endpoint_config(text)
        self.assertEqual((endpoint,port,protocol),('192.0.2.44',443,'tcp'))

    def test_http_proxy_hostname_is_resolved_and_pinned_for_control_connection(self):
        text=PROFILE.replace('http-proxy 192.0.2.80 443','http-proxy proxy.example.com 443')
        with patch.object(vpn.socket,'getaddrinfo',return_value=[
                (vpn.socket.AF_INET,vpn.socket.SOCK_STREAM,6,'',('192.0.2.80',0))]):
            rendered,endpoint,port,protocol=vpn.endpoint_config(text)
        self.assertIn('http-proxy 192.0.2.80 443',rendered)
        self.assertEqual((endpoint,port,protocol),('192.0.2.80',443,'tcp'))

    def test_config_supports_custom_transport_name_and_socks_port(self):
        value=(ROOT/'gateway.example.toml').read_text().replace(
            'default_transport = "udp"','default_transport = "primary"').replace(
            'socks_port = 1080','socks_port = 2080').replace(
            '[transports.udp]','[transports.primary]').replace(
            '[transports.tcp]','[transports.fallback]')
        with tempfile.TemporaryDirectory() as tmp:
            path=pathlib.Path(tmp)/'gateway.toml';path.write_text(value)
            config=gateway_config.load_config(path)
        self.assertEqual(config['default_transport'],'primary')
        self.assertEqual(config['socks_port'],2080)
        self.assertEqual(list(config['transports']),['primary','fallback'])

    def test_rejects_profile_scripts(self):
        with self.assertRaises(gateway.GatewayError):
            gateway.derive_config(PROFILE+'up /malicious\n',CONFIG['transports']['tcp'])

    def test_dns_uses_push_only(self):
        self.assertEqual(vpn.pushed_dns({}),([],[]))
        dns,domains=vpn.pushed_dns({'foreign_option_1':'dhcp-option DNS 10.20.30.40',
          'foreign_option_2':'dhcp-option DOMAIN corp.test','foreign_option_3':'dhcp-option DNS 127.0.0.11',
          'foreign_option_4':'dhcp-option DOMAIN evil;command'})
        self.assertEqual(dns,['10.20.30.40']);self.assertEqual(domains,['corp.test'])

    def test_logs_drop_certificates_push_and_auth_details(self):
        self.assertIsNone(vpn.safe_line('VERIFY OK: CN=SensitiveName',[]))
        self.assertIsNone(vpn.safe_line("PUSH_REPLY,route secret",[]))
        self.assertEqual(vpn.safe_line('ERROR: secret-password', ['secret-password']),'ERROR: [REDACTED]')
        self.assertNotIn('alice',vpn.safe_line('AUTH_FAILED alice bad password',['alice']))

    def test_target_no_credentials_or_query(self):
        with self.assertRaises(gateway.GatewayError):gateway.safe_target('https://user:password@corp.test')
        with self.assertRaises(gateway.GatewayError):gateway.safe_target('https://corp.test/?token=secret')
        self.assertEqual(gateway.safe_target('corp.test').hostname,'corp.test')

    def test_windows_push_filters_preserve_dns_and_tls(self):
        result=gateway.derive_config(PROFILE,CONFIG['transports']['tcp'])
        self.assertIn('pull-filter ignore "block-outside-dns"',result)
        self.assertIn('pull-filter ignore "register-dns"',result)
        self.assertNotIn('route-nopull',result)
        self.assertNotIn('pull-filter ignore "dhcp-option',result)

    def test_network_commands_do_not_depend_on_path(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(vpn.subprocess,'run') as run:
            vpn.command('ip','route','show')
            self.assertEqual(run.call_args.args[0][0],'/sbin/ip')

    def test_hook_failure_is_reported_and_cannot_become_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            state=pathlib.Path(tmp)
            with patch.object(vpn,'STATE',state), patch.object(vpn,'STATUS',state/'status.json'), \
                 patch.dict(os.environ,{'script_type':'route-up','dev':'tun0'},clear=True), \
                 patch.object(vpn,'command',side_effect=FileNotFoundError('ip')):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(vpn.run_hook(),1)
                vpn.initialization_event('udp')
                result=vpn.read_state()
                self.assertFalse(result['ready'])
                self.assertFalse(result['routes_ready'])
                self.assertTrue(result['initialization_completed'])
                self.assertIn('FileNotFoundError',result['hook_error'])

@unittest.skipUnless(os.environ.get('GATEWAY_CONTAINER_TEST') == '1', 'requires isolated Linux test container')
class ContainerHookTests(unittest.TestCase):
    def test_hooks_called_by_real_openvpn_with_sanitized_environment(self):
        # --network none, tmpfs /run,/state,/tmp; NO corporate profiles or auth.
        # An ephemeral encrypted static test endpoint invokes actual OpenVPN
        # hooks without making any connection to a corporate/public server.
        self.assertFalse(pathlib.Path('/sys/class/net/eth0').exists())
        vpn.write_state({'ready':False})
        subprocess.run(['/usr/sbin/openvpn','--genkey','secret','/run/hook-test.key'],
                       check=True,capture_output=True)
        args=['/usr/sbin/openvpn','--dev','tun0','--ifconfig','203.0.113.2','203.0.113.1',
              '--remote','127.0.0.1','11194','--port','11195','--secret','/run/hook-test.key',
              '--cipher','AES-128-CBC','--auth','SHA256','--script-security','2','--route-noexec',
              '--route-up','/opt/gateway/vpn.py hook','--down','/opt/gateway/vpn.py hook',
              '--setenv','foreign_option_1','dhcp-option DNS 10.20.30.40','--verb','3']
        process=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
        output=''
        try:
            deadline=time.monotonic()+5
            while time.monotonic()<deadline and process.poll() is None:
                if vpn.read_state().get('routes_ready'):
                    break
                time.sleep(0.05)
            state=vpn.read_state()
            self.assertTrue(state.get('routes_ready'),str(state))
            self.assertEqual(state['dns'],['10.20.30.40'])
            self.assertIn('default dev tun0',subprocess.check_output(['/sbin/ip','route','show','table','100'],text=True))
        finally:
            if process.poll() is None:process.terminate()
            output,_=process.communicate(timeout=4)
            pathlib.Path('/run/hook-test.key').unlink(missing_ok=True)
        self.assertNotIn('FileNotFoundError',output)
        self.assertNotIn('Failed running command',output)
        self.assertFalse(vpn.read_state()['routes_ready'])
        self.assertIn('nameserver 127.0.0.1',pathlib.Path('/state/resolv.conf').read_text())
        self.assertNotEqual(subprocess.run(['/sbin/ip','link','show','tun0'],capture_output=True).returncode,0)

class TransportComparisonTests(unittest.TestCase):
    """Lifecycle mocks, not real VPN/network acceptance evidence."""
    evidence = {key: True for key in (
        'openvpn_terminated', 'tun0_absent', 'proxy_request_verified_before_stop',
        'socks_after_vpn_stop_blocked', 'firewall_forced_outer_blocked',
        'host_still_online', 'state_restored')}

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        root = pathlib.Path(stack.enter_context(tempfile.TemporaryDirectory()))
        stack.enter_context(patch.object(gateway, 'ROOT', root))
        stack.enter_context(patch.object(gateway, 'STATE', root/'state'))
        stack.enter_context(patch('builtins.print'))
        self.saved = {}
        def write(path, value):
            self.saved[path.name] = json.loads(value)
        defaults = {
            'backend_name': 'wsl', 'stop_internal': None, 'preflight': {},
            'prompt_credentials': None, 'start_transport': True, 'ready': True,
            'configuration': {'transports': {'udp': {}, 'tcp': {}}},
            'wsl_status_data': {'initialization_completed': True},
            'read_json': {'initialization_completed': True},
            'preferences': {'backend': 'docker', 'default_transport': 'tcp'},
            'save_preferences': None, 'validation': {'passed': True},
            'failure_test': self.evidence, 'network_snapshot': {},
            'compare_host': {'host_diagnostics_available': True, 'host_dns_preserved': True},
            'sanitized_events': {'available': True, 'text': 'synthetic event',
                                 'tail_lines': 200, 'scope': 'backend_history'},
        }
        self.calls = {name: stack.enter_context(patch.object(gateway, name, return_value=value))
                      for name, value in defaults.items()}
        stack.enter_context(patch.object(gateway, 'private_write', side_effect=write))
        # An accidentally unmocked command must not touch an installed backend.
        stack.enter_context(patch.object(gateway, 'run', side_effect=AssertionError('Unexpected real command')))

    def assert_aborted(self, phase):
        report = self.saved['comparison.json']
        self.assertFalse(report['completed'])
        self.assertFalse(report['session_ready'])
        self.assertTrue(report['cleanup_attempted'])
        self.assertEqual(report['failure']['phase'], phase)
        self.calls['stop_internal'].assert_called_with(backend='wsl')
        self.calls['save_preferences'].assert_not_called()
        return report

    def test_false_final_validation_does_not_select_or_leave_ready_session(self):
        self.calls['validation'].side_effect = [{'passed': True}, {'passed': True}, {'passed': False}]
        with self.assertRaises(gateway.GatewayError):
            gateway.compare_transports('wsl')
        report = self.assert_aborted('final_validation')
        self.assertFalse(report['final_validation']['passed'])

    def test_disappearing_final_session_cannot_pass(self):
        self.calls['ready'].return_value = False
        with self.assertRaises(gateway.GatewayError):
            gateway.compare_transports('wsl')
        self.assert_aborted('final_validation')

    def test_failed_repeat_start_cleans_up_without_selection(self):
        self.calls['start_transport'].side_effect = [True, True, False]
        with self.assertRaises(gateway.GatewayError):
            gateway.compare_transports('wsl')
        self.assert_aborted('final_start')

    def test_missing_negative_evidence_is_not_vacuously_successful(self):
        self.calls['failure_test'].return_value = {}
        with self.assertRaises(gateway.GatewayError):
            gateway.compare_transports('wsl')
        self.assert_aborted('selection')
        self.assertFalse(any(row['usable'] for row in self.saved['transports.json'].values()))
        self.assertEqual(self.calls['start_transport'].call_count, 2)

    def test_each_negative_control_requires_an_actual_boolean(self):
        for backend in ('docker', 'wsl'):
            required = {key: value for key, value in self.evidence.items()
                        if backend == 'wsl' or key != 'state_restored'}
            self.assertTrue(gateway.failure_evidence_passed(required, backend))
            self.assertFalse(gateway.failure_evidence_passed(None, backend))
            self.assertFalse(gateway.failure_evidence_passed({}, backend))
            for key in required:
                for invalid in (False, None, 1, 'true'):
                    with self.subTest(backend=backend, key=key, invalid=invalid):
                        self.assertFalse(gateway.failure_evidence_passed(dict(required, **{key: invalid}), backend))
                missing = dict(required)
                del missing[key]
                self.assertFalse(gateway.failure_evidence_passed(missing, backend))

    def test_negative_test_not_started_after_failed_positive_validation(self):
        self.calls['validation'].return_value = {'passed': False}
        with self.assertRaises(gateway.GatewayError):
            gateway.compare_transports('wsl')
        self.calls['failure_test'].assert_not_called()
        self.assert_aborted('selection')

    def test_initial_timeout_record_excludes_argv_and_output(self):
        secret = 'DO-NOT-LOG-SYNTHETIC-PRIVATE-DATA'
        self.calls['preflight'].side_effect = subprocess.TimeoutExpired(
            ['private-tool', secret], 35, output=secret, stderr=secret)
        with self.assertRaises(subprocess.TimeoutExpired):
            gateway.compare_transports('wsl')
        report = self.assert_aborted('initial_preflight')
        self.assertEqual(report['failure']['kind'], 'timeout')
        self.assertNotIn(secret, json.dumps(self.saved))
        self.calls['prompt_credentials'].assert_not_called()
        self.calls['start_transport'].assert_not_called()

    def test_next_preflight_failure_keeps_previous_result_and_failed_stage(self):
        self.calls['preflight'].side_effect = [{}, {}, gateway.GatewayError('SYNTHETIC-PRIVATE-ENDPOINT')]
        with self.assertRaises(gateway.GatewayError):
            gateway.compare_transports('wsl')
        report = self.assert_aborted('transport_preflight')
        self.assertEqual(report['transport'], 'tcp')
        self.assertTrue(self.saved['transports.json']['udp']['usable'])
        self.assertFalse(self.saved['transports.json']['tcp']['usable'])
        self.assertNotIn('SYNTHETIC-PRIVATE-ENDPOINT', json.dumps(self.saved))
        self.calls['start_transport'].assert_called_once_with('udp', backend='wsl')

    def test_final_preflight_failure_prevents_repeat_connection(self):
        self.calls['preflight'].side_effect = [{}, {}, {}, gateway.GatewayError('outer mismatch')]
        with self.assertRaises(gateway.GatewayError):
            gateway.compare_transports('wsl')
        self.assert_aborted('final_preflight')
        self.assertEqual(self.calls['start_transport'].call_count, 2)

    def test_cancelled_credentials_are_cleaned_up_and_recorded(self):
        self.calls['prompt_credentials'].side_effect = KeyboardInterrupt('SYNTHETIC-PRIVATE-INPUT')
        with self.assertRaises(KeyboardInterrupt):
            gateway.compare_transports('wsl')
        report = self.assert_aborted('credentials')
        self.assertEqual(report['failure']['kind'], 'cancelled')
        self.assertNotIn('SYNTHETIC-PRIVATE-INPUT', json.dumps(self.saved))
        self.calls['start_transport'].assert_not_called()

    def test_cleanup_failure_keeps_original_failure_evidence(self):
        self.calls['preflight'].side_effect = gateway.GatewayError('SYNTHETIC-PRIVATE-ERROR')
        self.calls['stop_internal'].side_effect = [None, OSError('SYNTHETIC-PRIVATE-PATH')]
        with self.assertRaises(OSError):
            gateway.compare_transports('wsl')
        report = self.assert_aborted('initial_preflight')
        self.assertEqual(report['failure']['kind'], 'gateway_check_failed')
        self.assertEqual(report['cleanup_failure'], {'phase': 'cleanup', 'kind': 'os_error'})
        self.assertNotIn('SYNTHETIC-PRIVATE', json.dumps(self.saved))

    def test_new_preflight_cannot_hide_host_change_across_comparison(self):
        self.calls['compare_host'].return_value = {'host_dns_preserved': False}
        with self.assertRaises(gateway.GatewayError):
            gateway.compare_transports('wsl')
        self.assert_aborted('host_preservation')

    def test_success_requires_complete_evidence_and_keeps_saved_backend(self):
        gateway.compare_transports('wsl')
        report = self.saved['comparison.json']
        self.assertTrue(report['completed'])
        self.assertTrue(report['session_ready'])
        self.assertFalse(report['cleanup_attempted'])
        self.assertEqual(report['phase'], 'complete')
        self.calls['save_preferences'].assert_called_once_with({'backend': 'docker', 'default_transport': 'udp'})
        self.assertEqual(self.calls['preflight'].call_count, 4)
        self.assertEqual(self.calls['validation'].call_count, 3)
        self.calls['stop_internal'].assert_called_with(keep_auth=True, backend='wsl')

    def test_docker_comparison_remains_wsl_independent_on_macos(self):
        self.calls['backend_name'].return_value = 'docker'
        self.calls['failure_test'].return_value = {key: value for key, value in self.evidence.items()
                                                   if key != 'state_restored'}
        with patch.object(gateway.host, 'IS_WINDOWS', False):
            gateway.compare_transports('docker')
        self.assertTrue(self.saved['comparison.json']['completed'])
        self.calls['wsl_status_data'].assert_not_called()
        self.calls['stop_internal'].assert_called_with(keep_auth=True, backend='docker')
        self.assertEqual(self.calls['sanitized_events'].call_count, 2)
        self.calls['sanitized_events'].assert_called_with('docker', lines=200)

    def test_wsl_comparison_records_selected_backend_history_scope(self):
        gateway.compare_transports('wsl')
        for row in self.saved['transports.json'].values():
            self.assertEqual(row['events'], 'synthetic event')
            self.assertEqual(row['event_log'], {'available': True, 'tail_lines': 200,
                                               'scope': 'backend_history'})
        self.calls['sanitized_events'].assert_called_with('wsl', lines=200)

    def test_missing_log_is_explicit_and_does_not_invent_transport_failure(self):
        self.calls['sanitized_events'].return_value = {
            'available': False, 'text': '', 'tail_lines': 200, 'scope': 'backend_history'}
        gateway.compare_transports('wsl')
        for row in self.saved['transports.json'].values():
            self.assertEqual(row['events'], '')
            self.assertFalse(row['event_log']['available'])
            self.assertTrue(row['usable'])

    def test_outer_path_change_during_credentials_blocks_first_transport(self):
        self.calls['preflight'].side_effect = [{}, gateway.GatewayError('outer mismatch')]
        with self.assertRaises(gateway.GatewayError):
            gateway.compare_transports('wsl')
        report = self.assert_aborted('transport_preflight')
        self.assertEqual(report['transport'], 'udp')
        self.calls['prompt_credentials'].assert_called_once_with('wsl')
        self.calls['start_transport'].assert_not_called()
        self.calls['failure_test'].assert_not_called()
        self.assertFalse(self.saved['transports.json']['udp']['connected'])

    def test_comparison_rechecks_after_prompt_and_before_first_start(self):
        events = []
        self.calls['preflight'].side_effect = lambda backend: events.append('preflight') or {}
        self.calls['prompt_credentials'].side_effect = lambda backend: events.append('credentials')
        self.calls['start_transport'].side_effect = lambda *args, **kwargs: events.append('start') or True
        gateway.compare_transports('wsl')
        self.assertEqual(events[:4], ['preflight', 'credentials', 'preflight', 'start'])


class SanitizedEventTests(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.root = pathlib.Path(stack.enter_context(tempfile.TemporaryDirectory()))
        stack.enter_context(patch.object(gateway, 'STATE', self.root))
        stack.enter_context(patch.object(gateway, 'backend_name', side_effect=lambda backend: backend))
        stack.enter_context(patch.object(gateway, 'run', side_effect=AssertionError('Unexpected real command')))

    def test_wsl_reads_only_managed_linux_log_even_if_docker_log_exists(self):
        (self.root/'safe.log').write_text('unrelated Docker event')
        output = subprocess.CompletedProcess([], 0, 'older\nfirst\nsecond\n', '')
        with patch.object(gateway, 'wsl', return_value=output) as wsl:
            result = gateway.sanitized_events('wsl', lines=2)
        wsl.assert_called_once_with('/usr/bin/tail', '-n', '2',
            '/var/lib/isolated-openvpn-gateway/state/safe.log', check=False, timeout=20)
        self.assertEqual(result, {'available': True, 'text': 'first\nsecond',
                                 'tail_lines': 2, 'scope': 'backend_history'})

    def test_docker_reads_current_start_without_wsl_on_macos(self):
        (self.root/'safe.log').write_text('older\nfirst\nsecond\n', encoding='utf-8')
        with patch.object(gateway.host, 'IS_WINDOWS', False), patch.object(gateway, 'wsl') as wsl:
            result = gateway.sanitized_events('docker', lines=2)
        wsl.assert_not_called()
        self.assertEqual(result, {'available': True, 'text': 'first\nsecond',
                                 'tail_lines': 2, 'scope': 'current_start'})

    def test_wsl_log_error_never_falls_back_to_docker_or_emits_stderr(self):
        (self.root/'safe.log').write_text('unrelated Docker event')
        output = subprocess.CompletedProcess([], 1, 'SYNTHETIC-PRIVATE', 'SYNTHETIC-PRIVATE')
        with patch.object(gateway, 'wsl', return_value=output):
            result = gateway.sanitized_events('wsl')
        self.assertFalse(result['available'])
        self.assertEqual(result['text'], '')
        self.assertNotIn('SYNTHETIC-PRIVATE', json.dumps(result))

    def test_log_timeout_does_not_save_command_or_exception_text(self):
        failure = subprocess.TimeoutExpired(['SYNTHETIC-PRIVATE'], 20,
                                            output='SYNTHETIC-PRIVATE', stderr='SYNTHETIC-PRIVATE')
        with patch.object(gateway, 'wsl', side_effect=failure):
            result = gateway.sanitized_events('wsl')
        self.assertFalse(result['available'])
        self.assertNotIn('SYNTHETIC-PRIVATE', json.dumps(result))

    def test_empty_local_log_is_distinct_from_unavailable(self):
        self.assertFalse(gateway.sanitized_events('docker')['available'])
        (self.root/'safe.log').write_text('')
        result = gateway.sanitized_events('docker')
        self.assertTrue(result['available'])
        self.assertEqual(result['text'], '')

    def test_tail_limit_rejects_unbounded_or_noninteger_requests(self):
        for value in (0, 201, -1, True, '45', 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                gateway.sanitized_events('wsl', lines=value)

    def test_logs_uses_the_same_selected_backend_reader(self):
        with patch.object(gateway, 'sanitized_events', return_value={
                'available': True, 'text': 'sanitized fixture'}) as read, patch('builtins.print') as output:
            gateway.logs('wsl')
        read.assert_called_once_with('wsl')
        output.assert_called_once_with('sanitized fixture')


class InteractiveStartPreflightTests(unittest.TestCase):
    """Synthetic credentials and lifecycle mocks; no network changes."""
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.root = pathlib.Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.auth = self.root/'auth'
        stack.enter_context(patch.object(gateway, 'ROOT', self.root))
        stack.enter_context(patch.object(gateway, 'RUNTIME', self.root/'runtime'))
        stack.enter_context(patch('builtins.print'))
        stack.enter_context(patch.object(gateway.host, 'installation_root_matches', return_value=True))
        stack.enter_context(patch.object(gateway.host, 'maybe_lifecycle_lock',
                                         side_effect=lambda *args: contextlib.nullcontext()))
        defaults = {
            'parse_cli': ('start', [], 'wsl'), 'read_json': {'project': gateway.PROJECT},
            'configuration': {'transports': {'udp': {}}, 'default_transport': 'udp'},
            'preferences': {}, 'backend_name': 'wsl', 'active_backend': 'wsl',
            'ready': False, 'preflight': {}, 'prompt_credentials': None,
            'start_transport': True, 'stop_internal': None, 'private_write': None,
        }
        self.calls = {name: stack.enter_context(patch.object(gateway, name, return_value=value))
                      for name, value in defaults.items()}
        stack.enter_context(patch.object(gateway, 'run', side_effect=AssertionError('Unexpected real command')))
        self.events = []
        def prompt(backend):
            self.events.append('credentials')
            self.auth.write_text('synthetic-test-user\nsynthetic-test-password\n')
        def stop(**kwargs):
            self.events.append('stop')
            self.auth.unlink(missing_ok=True)
        self.calls['prompt_credentials'].side_effect = prompt
        self.calls['stop_internal'].side_effect = stop
        self.calls['preflight'].side_effect = lambda backend: self.events.append('preflight') or {}
        self.calls['start_transport'].side_effect = lambda *args, **kwargs: self.events.append('start') or True

    def test_start_and_restart_recheck_both_backends_after_prompt(self):
        for action in ('start', 'restart'):
            for backend, windows in (('wsl', True), ('docker', True), ('docker', False)):
                with self.subTest(action=action, backend=backend, windows=windows):
                    self.events.clear()
                    self.calls['parse_cli'].return_value = (action, [], backend)
                    self.calls['backend_name'].return_value = backend
                    self.calls['active_backend'].return_value = backend
                    self.calls['ready'].side_effect = [False, True] if action == 'start' else [True]
                    with patch.object(gateway.host, 'IS_WINDOWS', windows):
                        gateway.main()
                    self.assertEqual(self.events, ['stop', 'preflight', 'credentials', 'preflight', 'start'])
                    self.calls['preflight'].assert_called_with(backend)
                    self.calls['start_transport'].assert_called_with('udp', backend=backend)

    def test_failed_initial_preflight_never_prompts_or_starts(self):
        self.calls['preflight'].side_effect = gateway.GatewayError('outer unavailable')
        with self.assertRaises(gateway.GatewayError):
            gateway.main()
        self.calls['prompt_credentials'].assert_not_called()
        self.calls['start_transport'].assert_not_called()
        self.assertFalse(self.auth.exists())

    def test_failed_post_prompt_preflight_removes_credentials_without_start(self):
        self.calls['preflight'].side_effect = [{}, gateway.GatewayError('outer mismatch')]
        with self.assertRaises(gateway.GatewayError):
            gateway.main()
        self.calls['prompt_credentials'].assert_called_once_with('wsl')
        self.calls['start_transport'].assert_not_called()
        self.assertEqual(self.calls['stop_internal'].call_count, 2)
        self.assertFalse(self.auth.exists())

    def test_cancelled_post_prompt_check_also_cleans_credentials(self):
        self.calls['preflight'].side_effect = [{}, KeyboardInterrupt()]
        with self.assertRaises(KeyboardInterrupt):
            gateway.main()
        self.calls['start_transport'].assert_not_called()
        self.assertFalse(self.auth.exists())

    def test_write_failure_cleans_up_even_if_started_session_is_ready(self):
        self.calls['ready'].side_effect = [False, True]
        def fail_active_backend(path, *_args, **_kwargs):
            if path.name == 'active-backend':
                raise OSError('synthetic state-write failure')
        self.calls['private_write'].side_effect = fail_active_backend
        with self.assertRaises(OSError):
            gateway.main()
        self.calls['start_transport'].assert_called_once()
        self.assertEqual(self.calls['stop_internal'].call_count, 2)
        self.assertFalse(self.auth.exists())

    def test_failed_final_readiness_check_is_not_success_and_cleans_credentials(self):
        for final_result in (False, OSError('synthetic status read failure')):
            with self.subTest(final_result=type(final_result).__name__):
                self.calls['ready'].side_effect = [False, final_result]
                with self.assertRaises((gateway.GatewayError, OSError)):
                    gateway.main()
                self.assertFalse(self.auth.exists())


if __name__ == '__main__': unittest.main()
