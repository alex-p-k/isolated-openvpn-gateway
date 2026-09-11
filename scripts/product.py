"""Small, plain-text CLI primitives. Never render arbitrary child output."""
import json
import sys


class ProductError(Exception):
    def __init__(self, code, message, next_action='vpn-gateway doctor'):
        super().__init__(message)
        self.code, self.next_action = code, next_action


def ask(prompt, default=None):
    if not sys.stdin.isatty():
        raise ProductError('INPUT_REQUIRED', 'Interactive input is required; run in a terminal or supply the missing option.',
                           'vpn-gateway setup --help')
    suffix = ' [' + str(default) + ']' if default is not None else ''
    value = input(prompt + suffix + ': ').strip()
    return value or default or ''


def confirm(prompt, default=False):
    answer = ask(prompt + ' (yes/no)', 'no' if not default else 'yes').casefold()
    if answer not in ('yes', 'y', 'no', 'n'):
        raise ProductError('INPUT_INVALID', 'Enter yes or no; nothing further was changed.', 'vpn-gateway setup')
    return answer in ('yes', 'y')


def read_object(path):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    except (ValueError, UnicodeError):
        raise ProductError('STATE_INVALID', 'A local settings file is invalid; it was not overwritten.') from None
    if not isinstance(data, dict):
        raise ProductError('STATE_INVALID', 'Expected an object in the local settings file.')
    return data


def platform_instruction(text):
    if sys.platform == 'darwin':
        return text.replace('.\\setup.cmd', 'python3 install.py --wizard')
    return text


def error_text(exc):
    if isinstance(exc, ProductError):
        return platform_instruction(f'ERROR [{exc.code}]: {exc}\nNext: {exc.next_action}')
    # OSError/TimeoutExpired and legacy subprocess errors may contain private
    # URLs, arguments, stdout or stderr. Do not pass their text to the terminal.
    reason = str(exc).lower()
    if 'outer' in reason or 'egress' in reason:
        return ('ERROR [OUTER_VPN]: The required outer VPN route/egress could not be verified.\n'
                'Next: connect the configured outer VPN, then run vpn-gateway doctor.')
    if 'lock' in reason or isinstance(exc, BlockingIOError):
        return 'ERROR [BUSY]: Another gateway operation is running.\nNext: vpn-gateway status'
    if 'wsl' in reason:
        return ('ERROR [WSL_UNAVAILABLE]: The managed WSL operation could not complete.\n'
                'Next: vpn-gateway doctor; for installation recovery run vpn-gateway setup.')
    if 'docker' in reason:
        return 'ERROR [DOCKER_UNAVAILABLE]: Docker Linux backend is unavailable.\nNext: vpn-gateway doctor'
    if isinstance(exc, PermissionError) or 'acl' in reason:
        return 'ERROR [ACCESS_DENIED]: Required private-file permissions are unavailable.\nNext: vpn-gateway doctor'
    return ('ERROR [OPERATION_FAILED]: The operation did not complete; private command output was withheld.\n'
            'Next: vpn-gateway doctor; vpn-gateway logs for sanitized VPN events.')
