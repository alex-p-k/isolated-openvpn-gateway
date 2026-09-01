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

if __name__ == '__main__': unittest.main()
