"""Run the actual PowerShell TCP probe against temporary loopback sockets."""
import json
import os
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == 'nt', 'requires real Windows PowerShell and TCP stack')
class WindowsListenerProbeTests(unittest.TestCase):
    def test_numeric_ipv4_and_ipv6_positive_and_negative_controls(self):
        script = r'''
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    (Join-Path (Get-Location) 'tools/windows_acceptance.ps1'), [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'Acceptance script parse failed.' }
$functions = @($ast.FindAll({ param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -in @('Test-Tcp','Test-TcpControl','Get-SocksListenerEvidence')
}, $false))
if ($functions.Count -ne 3) { throw 'Probe functions not found.' }
foreach ($function in $functions) { . ([scriptblock]::Create($function.Extent.Text)) }
$result = @{}
foreach ($address in @('127.0.0.1','::1')) {
    $ip = [Net.IPAddress]::Parse($address)
    $listener = [Net.Sockets.TcpListener]::new($ip, 0)
    try {
        $listener.Start()
        $port = $listener.LocalEndpoint.Port
        $positive = Test-Tcp $address $port
    } finally { $listener.Stop() }
    $result[$address] = @{
        Positive = $positive
        Negative = -not (Test-Tcp $address $port)
        Control = Test-TcpControl $address
    }
}
$result.HostnameRejected = $false
try { Test-Tcp 'example.invalid' 1080 | Out-Null }
catch { $result.HostnameRejected = $true }
$result.NonloopbackControlRejected = $false
try { Test-TcpControl '192.0.2.1' | Out-Null }
catch { $result.NonloopbackControlRejected = $true }
# Broken diagnostics cannot pass a negative acceptance check. No real network
# interfaces are enumerated in this fixture; the controls above used real TCP.
function Get-NetTCPConnection { param($State,$LocalPort,$ErrorAction)
    [pscustomobject]@{LocalAddress='127.0.0.1'}
}
function Get-NetIPAddress { @() }
function Test-TcpControl([string]$Address) { return $Address -eq '127.0.0.1' }
function Test-Tcp([string]$Address, [int]$Port) { return $Address -eq '127.0.0.1' }
$result.BrokenIpv6ProbeRejected = -not (Get-SocksListenerEvidence).Passed
$result | ConvertTo-Json -Depth 4 -Compress
'''
        powershell = Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
        result = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-Command', script],
                                cwd=ROOT, capture_output=True, text=True, timeout=25)
        self.assertEqual(result.returncode, 0, result.stderr)
        evidence = json.loads(result.stdout)
        self.assertTrue(evidence['HostnameRejected'])
        self.assertTrue(evidence['NonloopbackControlRejected'])
        self.assertTrue(evidence['BrokenIpv6ProbeRejected'])
        for address in ('127.0.0.1', '::1'):
            with self.subTest(address=address):
                self.assertEqual(evidence[address], {'Positive':True,'Negative':True,'Control':True})


if __name__ == '__main__':
    unittest.main()
