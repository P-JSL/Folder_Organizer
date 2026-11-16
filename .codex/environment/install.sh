#!/usr/bin/env bash
set -euo pipefail

project_dir=/workspace/Folder_Organizer
tools_dir=/workspace/.folder-organizer-tools
test -f "$project_dir/pyproject.toml"
test -f "$project_dir/rust-app/Cargo.lock"
mkdir -p "$tools_dir/downloads"
export UV_CACHE_DIR="$tools_dir/uv-cache"
export PIP_CACHE_DIR="$tools_dir/pip-cache"
export RUSTUP_HOME="$tools_dir/rustup"
export CARGO_HOME="$tools_dir/cargo"
export CARGO_BUILD_JOBS=4
if [ -n "${SSL_CERT_FILE:-}" ]; then
    export CARGO_HTTP_CAINFO="${CARGO_HTTP_CAINFO:-$SSL_CERT_FILE}"
fi

for command_name in uv python3 curl sha256sum cc make pkg-config; do
    command -v "$command_name" >/dev/null
done
pkg-config --exists openssl

uv --system-certs venv --allow-existing --python python3 "$project_dir/.venv"
uv --system-certs pip install \
    --python "$project_dir/.venv/bin/python" \
    --index-url https://pypi.org/simple \
    -e "$project_dir[dev,bundle]" \
    'setuptools==80.9.0' 'wheel==0.45.1' 'pip==26.3'

if [ ! -x "$CARGO_HOME/bin/rustup" ]; then
    curl --fail --location --silent --show-error --max-time 120 \
        --output "$tools_dir/downloads/rustup-init" \
        https://static.rust-lang.org/rustup/dist/x86_64-unknown-linux-gnu/rustup-init
    curl --fail --location --silent --show-error --max-time 30 \
        --output "$tools_dir/downloads/rustup-init.sha256" \
        https://static.rust-lang.org/rustup/dist/x86_64-unknown-linux-gnu/rustup-init.sha256
    (
        cd "$tools_dir/downloads"
        sha256sum --check rustup-init.sha256
        chmod u+x rustup-init
        ./rustup-init -y --no-modify-path --profile minimal \
            --default-toolchain 1.99.1 --component rustfmt --component clippy
    )
else
    "$CARGO_HOME/bin/rustup" toolchain install 1.99.1 --profile minimal \
        --component rustfmt --component clippy
    "$CARGO_HOME/bin/rustup" default 1.99.1
fi

cat > "$tools_dir/activate.sh" <<'ACTIVATE'
# Source this file to enable the Folder Organizer development tools.
export UV_CACHE_DIR=/workspace/.folder-organizer-tools/uv-cache
export PIP_CACHE_DIR=/workspace/.folder-organizer-tools/pip-cache
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-/workspace/.folder-organizer-tools/cache}"
export RUSTUP_HOME=/workspace/.folder-organizer-tools/rustup
export CARGO_HOME=/workspace/.folder-organizer-tools/cargo
export CARGO_BUILD_JOBS=4
if [ -n "${SSL_CERT_FILE:-}" ]; then
    export CARGO_HTTP_CAINFO="${CARGO_HTTP_CAINFO:-$SSL_CERT_FILE}"
fi
. /workspace/Folder_Organizer/.venv/bin/activate
case ":$PATH:" in
    *":$CARGO_HOME/bin:"*) ;;
    *) export PATH="$CARGO_HOME/bin:$PATH" ;;
esac
if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
    export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}"
fi
ACTIVATE

. "$tools_dir/activate.sh"
cd "$project_dir"
cargo +1.99.1 fetch --manifest-path rust-app/Cargo.toml --locked \
    --target x86_64-unknown-linux-gnu --quiet
python -m pip check
python -m pytest --version
python -m build --version
pyinstaller --version
rustc +1.99.1 --version
cargo +1.99.1 --version
rustfmt +1.99.1 --version
cargo +1.99.1 clippy --version
QT_QPA_PLATFORM=offscreen python -B - <<'PY'
from PySide6.QtCore import qVersion
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QTableView
from PIL import Image
from pypdf import PdfReader

app = QApplication([])
table = QTableView()
image = QImage(2, 2, QImage.Format.Format_RGB32)
assert image.width() == 2
assert table.model() is None
print('Qt toolkit initialization: OK', qVersion(), 'platform=' + app.platformName())
print('Pillow and pypdf import: OK')
table.deleteLater()
app.processEvents()
app.quit()
PY
python -B - <<'PY'
import json
import subprocess

result = subprocess.run([
    'cargo', '+1.99.1', 'metadata', '--manifest-path', 'rust-app/Cargo.toml',
    '--locked', '--offline', '--features', 'gui',
    '--filter-platform', 'x86_64-unknown-linux-gnu', '--format-version', '1',
], check=True, capture_output=True, text=True)
graph = json.loads(result.stdout)
print('Resolved Linux GUI dependency graph: OK; packages:', len(graph['resolve']['nodes']))
PY

# Local Xorg/XFCE desktop and Windows RDP client.
# Existing application source, manifests, lockfiles, and network policy are preserved.
umask 077
mkdir -p /workspace/.folder-organizer-tools/desktop/apt /workspace/.folder-organizer-tools/windows
cat > '/workspace/.folder-organizer-tools/desktop/desktop.py' <<'FOLDER_ORGANIZER_SETUP_FILE_0_END'
#!/usr/bin/env python3
"""Manage a local authenticated Xorg/XFCE session; no remote ports are opened."""
import json
import os
from pathlib import Path
import secrets
import shlex
import signal
import subprocess
import sys
import time

TOOLS = Path('/workspace/.folder-organizer-tools')
BASE = TOOLS / 'desktop'
RUN = Path(f'/tmp/folder-organizer-desktop-{os.getuid()}')
STATE = RUN / 'processes.json'
DISPLAY = ':99'


def birth(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
    except (FileNotFoundError, ProcessLookupError):
        return None


def owned(record):
    return bool(record and record.get('birth') is not None and birth(record['pid']) == record['birth'])


def state():
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def environment():
    env = os.environ.copy()
    env.update({
        'DISPLAY': DISPLAY, 'XAUTHORITY': str(RUN / 'Xauthority'),
        'ICEAUTHORITY': str(RUN / 'ICEauthority'),
        'XDG_RUNTIME_DIR': str(RUN), 'XDG_SESSION_TYPE': 'x11',
        'XDG_CURRENT_DESKTOP': 'XFCE', 'XDG_SESSION_DESKTOP': 'xfce',
        'XDG_CONFIG_HOME': str(BASE / 'config'),
        'XDG_CACHE_HOME': str(BASE / 'cache'),
        'XDG_DATA_HOME': str(BASE / 'data'),
        'XDG_STATE_HOME': str(BASE / 'state'),
        'QT_QPA_PLATFORM': 'xcb',
        'XDG_DATA_DIRS': str(BASE / 'native/usr/share') + ':/usr/local/share:/usr/share',
        'XDG_DESKTOP_PORTAL_DIR': str(BASE / 'native/usr/share/xdg-desktop-portal/portals'),
    })
    if (RUN / 'dbus-address').exists():
        env['DBUS_SESSION_BUS_ADDRESS'] = (RUN / 'dbus-address').read_text().strip()
    else:
        env.pop('DBUS_SESSION_BUS_ADDRESS', None)
    return env


def probe(env, wm=False):
    command = ['xprop', '-root', '_NET_SUPPORTING_WM_CHECK'] if wm else ['xdpyinfo']
    try:
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=2)
        return result.returncode == 0 and (not wm or 'window id #' in result.stdout)
    except (subprocess.TimeoutExpired, OSError):
        return False


def spawn(command, env, log_name):
    with (RUN / log_name).open('ab') as log:
        proc = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    return proc, {'pid': proc.pid, 'birth': birth(proc.pid)}


def wait_ready(check, proc, label):
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f'{label} exited with code {proc.returncode}; logs: {RUN}')
        if check():
            return
        time.sleep(0.2)
    raise RuntimeError(f'{label} did not become ready; logs: {RUN}')


def stop():
    records = state()
    for name in ['session', 'xorg']:
        record = records.get(name)
        if owned(record):
            try:
                os.killpg(record['pid'], signal.SIGTERM)
            except ProcessLookupError:
                pass
    for filename in ['processes.json', 'environment.sh', 'dbus-address']:
        (RUN / filename).unlink(missing_ok=True)
    deadline = time.monotonic() + 5
    while Path('/tmp/.X11-unix/X99').exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    print('Managed desktop stop requested.')


def start():
    RUN.mkdir(mode=0o700, exist_ok=True)
    RUN.chmod(0o700)
    for name in ['config', 'cache', 'data', 'state']:
        (BASE / name).mkdir(parents=True, exist_ok=True)
    records = state()
    env = environment()
    if all(owned(records.get(k)) for k in ['xorg', 'session']) and probe(env, wm=True):
        print(f'Desktop already running: {DISPLAY}')
        return
    if any(owned(records.get(k)) for k in ['xorg', 'session']):
        raise RuntimeError('Managed desktop is partially running; inspect logs, then use stop before restarting.')
    if Path('/tmp/.X11-unix/X99').exists() or Path('/tmp/.X99-lock').exists():
        raise RuntimeError('Display :99 is occupied; existing display was left unchanged.')
    (RUN / 'dbus-address').unlink(missing_ok=True)
    auth = RUN / 'Xauthority'
    auth.touch(mode=0o600, exist_ok=True)
    auth.chmod(0o600)
    subprocess.run(['xauth', '-f', str(auth), 'add', DISPLAY,
                    'MIT-MAGIC-COOKIE-1', secrets.token_hex(16)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    env = environment()
    xorg, records['xorg'] = spawn([
        '/usr/lib/xorg/Xorg', DISPLAY, '-config', '/etc/X11/xorg.conf',
        '-logfile', str(RUN / 'Xorg.log'), '-auth', str(auth),
        '-nolisten', 'tcp', '-noreset',
    ], env, 'xorg-console.log')
    STATE.write_text(json.dumps(records))
    try:
        wait_ready(lambda: probe(env), xorg, 'Xorg')
        session, records['session'] = spawn([
            'dbus-run-session', '--', 'bash', str(BASE / 'session.sh'),
        ], env, 'xfce-session.log')
        STATE.write_text(json.dumps(records))
        wait_ready(lambda: probe(environment(), wm=True) and (RUN / 'dbus-address').exists(),
                   session, 'XFCE window manager')
        selected = ['DISPLAY', 'XAUTHORITY', 'ICEAUTHORITY', 'XDG_RUNTIME_DIR',
                    'XDG_SESSION_TYPE', 'XDG_CURRENT_DESKTOP', 'XDG_SESSION_DESKTOP',
                    'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_DATA_HOME',
                    'XDG_STATE_HOME', 'QT_QPA_PLATFORM', 'DBUS_SESSION_BUS_ADDRESS',
                    'XDG_DATA_DIRS', 'XDG_DESKTOP_PORTAL_DIR']
        env = environment()
        (RUN / 'environment.sh').write_text(''.join(
            f'export {key}={shlex.quote(env[key])}\n' for key in selected))
        print(f'Authenticated Xorg/XFCE desktop ready: {DISPLAY}')
        print('Activate: . /workspace/.folder-organizer-tools/desktop/activate.sh')
    except Exception:
        stop()
        raise


def status():
    env = environment()
    records = state()
    print(json.dumps({
        'display': DISPLAY,
        'xorg_alive': bool(owned(records.get('xorg'))),
        'session_alive': bool(owned(records.get('session'))),
        'x11_connection': probe(env), 'window_manager': probe(env, wm=True),
        'remote_viewer': 'not configured',
    }, indent=2))


if __name__ == '__main__':
    commands = {'start': start, 'stop': stop, 'status': status}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        raise SystemExit('Usage: desktop.py start|stop|status')
    commands[sys.argv[1]]()
FOLDER_ORGANIZER_SETUP_FILE_0_END
cat > '/workspace/.folder-organizer-tools/desktop/session.sh' <<'FOLDER_ORGANIZER_SETUP_FILE_1_END'
#!/usr/bin/env bash
set -euo pipefail
umask 077
printf '%s\n' "$DBUS_SESSION_BUS_ADDRESS" > "$XDG_RUNTIME_DIR/dbus-address"
exec xfce4-session
FOLDER_ORGANIZER_SETUP_FILE_1_END
cat > '/workspace/.folder-organizer-tools/desktop/activate.sh' <<'FOLDER_ORGANIZER_SETUP_FILE_2_END'
# Start desktop.py first, then source this file in a development shell.
desktop_runtime="/tmp/folder-organizer-desktop-$(id -u)"
if [ ! -f "$desktop_runtime/environment.sh" ]; then
    printf '%s\n' 'Desktop is not started. Run: python3 /workspace/.folder-organizer-tools/desktop/desktop.py start' >&2
    return 1
fi
. /workspace/.folder-organizer-tools/activate.sh
. "$desktop_runtime/environment.sh"
unset desktop_runtime
FOLDER_ORGANIZER_SETUP_FILE_2_END
cat > '/workspace/.folder-organizer-tools/desktop/install.sh' <<'FOLDER_ORGANIZER_SETUP_FILE_3_END'
#!/usr/bin/env bash
set -euo pipefail
desktop_dir=/workspace/.folder-organizer-tools/desktop
mkdir -p "$desktop_dir/apt/lists/partial" "$desktop_dir/apt/archives/partial" \
    "$desktop_dir/native" "$desktop_dir/config/xdg-desktop-portal"
for tool_name in Xorg xfce4-session xfwm4 dbus-run-session xauth xdpyinfo xprop dpkg-deb; do
    command -v "$tool_name" >/dev/null
done
test -f /usr/lib/xorg/modules/drivers/dummy_drv.so
apt_options=(-c "$desktop_dir/apt/apt.conf")
if [ -n "${HTTP_PROXY:-}" ]; then
    apt_options+=(-o "Acquire::http::Proxy=$HTTP_PROXY")
fi
if [ -n "${HTTPS_PROXY:-}" ]; then
    apt_options+=(-o "Acquire::https::Proxy=$HTTPS_PROXY")
fi
/usr/bin/apt-get "${apt_options[@]}" update
/usr/bin/apt-get "${apt_options[@]}" --download-only --no-install-recommends --yes \
    install 'xdg-desktop-portal=1.20.3+ds-1' 'xdg-desktop-portal-gtk=1.15.3-1'
for package_file in "$desktop_dir"/apt/archives/*.deb; do
    dpkg-deb --extract "$package_file" "$desktop_dir/native"
done
python3 -B - <<'PY'
from pathlib import Path
root = Path('/workspace/.folder-organizer-tools/desktop/native')
for path in (root/'usr/share/dbus-1/services').glob('*.service'):
    content = path.read_text()
    for line in content.splitlines():
        if line.startswith('Exec=/usr/libexec/'):
            command = line.removeprefix('Exec=').split()[0]
            candidate = root / command.lstrip('/')
            if candidate.is_file():
                content = content.replace('Exec=' + command, 'Exec=' + str(candidate))
    path.write_text(content)
PY
cat > "$desktop_dir/config/xdg-desktop-portal/portals.conf" <<'PORTALS'
[preferred]
default=gtk
PORTALS
"$desktop_dir/native/usr/libexec/xdg-desktop-portal" --version
test -x "$desktop_dir/native/usr/libexec/xdg-desktop-portal-gtk"
FOLDER_ORGANIZER_SETUP_FILE_3_END
cat > '/workspace/.folder-organizer-tools/desktop/apt/apt.conf' <<'FOLDER_ORGANIZER_SETUP_FILE_4_END'
Dir::State "/workspace/.folder-organizer-tools/desktop/apt";
Dir::State::status "/var/lib/dpkg/status";
Dir::State::lists "/workspace/.folder-organizer-tools/desktop/apt/lists";
Dir::Cache "/workspace/.folder-organizer-tools/desktop/apt";
Dir::Cache::archives "/workspace/.folder-organizer-tools/desktop/apt/archives";
Dir::Etc::sourcelist "/workspace/.folder-organizer-tools/desktop/apt/sources.list";
Dir::Etc::sourceparts "-";
Debug::NoLocking "true";
APT::Get::List-Cleanup "false";
Acquire::Languages "none";
#clear APT::Update::Post-Invoke;
#clear APT::Update::Post-Invoke-Success;
FOLDER_ORGANIZER_SETUP_FILE_4_END
cat > '/workspace/.folder-organizer-tools/desktop/apt/sources.list' <<'FOLDER_ORGANIZER_SETUP_FILE_5_END'
deb [signed-by=/usr/share/keyrings/debian-archive-keyring.pgp] https://deb.debian.org/debian trixie main
FOLDER_ORGANIZER_SETUP_FILE_5_END
cat > '/workspace/.folder-organizer-tools/windows/install.sh' <<'FOLDER_ORGANIZER_SETUP_FILE_6_END'
#!/usr/bin/env bash
set -euo pipefail
windows_dir=/workspace/.folder-organizer-tools/windows
desktop_dir=/workspace/.folder-organizer-tools/desktop
mkdir -p "$windows_dir/archives/partial" "$windows_dir/native"
apt_options=(-c "$desktop_dir/apt/apt.conf"
    -o "Dir::Cache::archives=$windows_dir/archives")
if [ -n "${HTTP_PROXY:-}" ]; then
    apt_options+=(-o "Acquire::http::Proxy=$HTTP_PROXY")
fi
if [ -n "${HTTPS_PROXY:-}" ]; then
    apt_options+=(-o "Acquire::https::Proxy=$HTTPS_PROXY")
fi
/usr/bin/apt-get "${apt_options[@]}" --download-only --no-install-recommends --yes \
    install 'freerdp3-x11=3.15.0+dfsg-2.1+deb13u3'
for package_file in "$windows_dir"/archives/*.deb; do
    dpkg-deb --extract "$package_file" "$windows_dir/native"
done
LD_LIBRARY_PATH="$windows_dir/native/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
    "$windows_dir/native/usr/bin/xfreerdp3" /version
FOLDER_ORGANIZER_SETUP_FILE_6_END
cat > '/workspace/.folder-organizer-tools/windows/connect.py' <<'FOLDER_ORGANIZER_SETUP_FILE_7_END'
#!/usr/bin/env python3
"""Check the supported RDP route, or connect with an interactive password prompt."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

BASE = Path('/workspace/.folder-organizer-tools/windows')
CLIENT = BASE / 'native/usr/bin/xfreerdp3'


def target_allowed(host, grants):
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        host = host.encode('idna').decode('ascii').rstrip('.').lower()
        for domain in grants.get('domains', []):
            if not isinstance(domain, str):
                continue
            domain = domain.rstrip('.').lower()
            if domain == host or (domain.startswith('*.') and host.endswith(domain[1:])):
                return True
        return False
    if address.version != 4:
        return False
    for cidr in grants.get('ip_ranges', []):
        try:
            if address in ipaddress.ip_network(cidr, strict=False):
                return True
        except (ValueError, TypeError):
            continue
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', help='Windows hostname or IPv4 address')
    parser.add_argument('--username', help='Windows account name; no password')
    parser.add_argument('--domain', help='Optional Windows account domain')
    parser.add_argument('--port', type=int, default=3389)
    parser.add_argument('--connect', action='store_true',
                        help='Open RDP using a real interactive terminal for credentials')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('Port must be between 1 and 65535.')
    for name in ['host', 'username', 'domain']:
        value = getattr(args, name)
        if value and (any(ord(c) < 32 for c in value) or value.startswith('/')):
            parser.error(f'Invalid {name}.')
    if args.host and any(c in args.host for c in '/:@* '):
        parser.error('Use a plain hostname or IPv4 address for --host.')

    policy = json.loads(Path('/etc/codex/network-policy.json').read_text())
    grants = policy.get('tcp_network_access') or {}
    proxy = policy.get('tcp_connect_proxy')
    problems = []
    if not CLIENT.is_file():
        problems.append('FreeRDP client is not installed.')
    if not args.host:
        problems.append('Windows hostname or IPv4 address is required.')
    if not args.username:
        problems.append('Windows account name is required.')
    if policy.get('vpn_configured') is not True:
        problems.append('The managed environment VPN is not configured.')
    if args.host and not target_allowed(args.host, grants):
        problems.append('The Windows target has no matching raw TCP domain/CIDR grant.')
    elif not args.host and not (grants.get('domains') or grants.get('ip_ranges')):
        problems.append('The raw TCP destination grant list is empty.')
    if not proxy:
        problems.append('The supported TCP CONNECT proxy is unavailable.')
    else:
        parsed = urlsplit(proxy)
        if parsed.scheme != 'http' or not parsed.hostname or parsed.username or parsed.password:
            problems.append('The TCP proxy configuration is unsupported by this helper.')
    if args.connect and not os.environ.get('DISPLAY'):
        problems.append('Activate the desktop environment before connecting.')
    if args.connect and not sys.stdin.isatty():
        problems.append('Use a secure interactive terminal for password entry; do not pass it in chat or command arguments.')

    print(json.dumps({
        'client_installed': CLIENT.is_file(),
        'vpn_configured': policy.get('vpn_configured') is True,
        'has_tcp_grants': bool(grants.get('domains') or grants.get('ip_ranges')),
        'windows_host_supplied': bool(args.host),
        'windows_account_supplied': bool(args.username),
        'connection_attempted': bool(args.connect and not problems),
        'remaining_requirements': problems,
    }, indent=2), flush=True)
    if not args.connect:
        return 0
    if problems:
        return 2
    env = os.environ.copy()
    local_lib = str(BASE / 'native/usr/lib/x86_64-linux-gnu')
    env['LD_LIBRARY_PATH'] = local_lib + (':' + env['LD_LIBRARY_PATH'] if env.get('LD_LIBRARY_PATH') else '')
    command = [str(CLIENT), f'/v:{args.host}:{args.port}', f'/u:{args.username}',
               f'/proxy:{proxy}', '/dynamic-resolution', '/from-stdin:force']
    if args.domain:
        command.append(f'/d:{args.domain}')
    os.execvpe(str(CLIENT), command, env)


if __name__ == '__main__':
    raise SystemExit(main())
FOLDER_ORGANIZER_SETUP_FILE_7_END
bash /workspace/.folder-organizer-tools/desktop/install.sh
bash /workspace/.folder-organizer-tools/windows/install.sh
# Desktop processes start on demand using the saved start_skill.

# Cloud-local Windows VM helpers; preserve any retained installed disk.
mkdir -p /workspace/.folder-organizer-tools/windows-vm
cat > '/workspace/.folder-organizer-tools/windows-vm/install-tools.sh' <<'FOLDER_ORGANIZER_VM_STATIC_0_END'
#!/usr/bin/env bash
set -euo pipefail
vm_dir=/workspace/.folder-organizer-tools/windows-vm
desktop_dir=/workspace/.folder-organizer-tools/desktop
mkdir -p "$vm_dir/archives/partial" "$vm_dir/native"
apt_options=(-c "$desktop_dir/apt/apt.conf" -o "Dir::Cache::archives=$vm_dir/archives")
if [ -n "${HTTP_PROXY:-}" ]; then
    apt_options+=(-o "Acquire::http::Proxy=$HTTP_PROXY")
fi
if [ -n "${HTTPS_PROXY:-}" ]; then
    apt_options+=(-o "Acquire::https::Proxy=$HTTPS_PROXY")
fi
/usr/bin/apt-get "${apt_options[@]}" --download-only --no-install-recommends --yes install \
    'qemu-system-x86=1:10.0.13+ds-0+deb13u1' 'qemu-utils=1:10.0.13+ds-0+deb13u1' \
    'ovmf=2025.02-8+deb13u1' 'swtpm=0.7.1-1.5' 'swtpm-tools=0.7.1-1.5' \
    genisoimage mtools wimtools
for package_file in "$vm_dir"/archives/*.deb; do
    dpkg-deb --extract "$package_file" "$vm_dir/native"
done
export LD_LIBRARY_PATH="$vm_dir/native/usr/lib/x86_64-linux-gnu:$vm_dir/native/usr/lib/x86_64-linux-gnu/swtpm${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
"$vm_dir/native/usr/bin/qemu-system-x86_64" --version
"$vm_dir/native/usr/bin/qemu-img" --version
"$vm_dir/native/usr/bin/swtpm" --version
FOLDER_ORGANIZER_VM_STATIC_0_END
cat > '/workspace/.folder-organizer-tools/windows-vm/install-vm.sh' <<'FOLDER_ORGANIZER_VM_STATIC_1_END'
#!/usr/bin/env bash
set -euo pipefail
umask 077
vm_setup_dir=/workspace/.folder-organizer-tools/windows-vm
native_ready=true
for vm_binary in qemu-system-x86_64 qemu-img swtpm genisoimage mtype mformat mcopy; do
    if [ ! -x "$vm_setup_dir/native/usr/bin/$vm_binary" ]; then native_ready=false; fi
done
if [ "$native_ready" != true ]; then
    bash "$vm_setup_dir/install-tools.sh"
fi
if [ -f "$vm_setup_dir/state/windows.qcow2" ]; then
    test -f "$vm_setup_dir/state/vm.json"
    test -f "$vm_setup_dir/state/credentials.json"
    test -f "$vm_setup_dir/state/nvram.fd"
    printf '%s\n' 'Existing Windows installation retained. No disk preparation or unattended installation was replayed.'
else
    python3 "$vm_setup_dir/download-iso.py"
    python3 "$vm_setup_dir/prepare.py"
fi
printf '%s\n' 'VM setup files are ready. Start Windows only when requested with vm.py start.'
FOLDER_ORGANIZER_VM_STATIC_1_END
cat > '/workspace/.folder-organizer-tools/windows-vm/download-iso.py' <<'FOLDER_ORGANIZER_VM_STATIC_2_END'
#!/usr/bin/env python3
"""Fetch fixed Microsoft ISO byte ranges and verify the published SHA-256."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess

BASE = Path('/workspace/.folder-organizer-tools/windows-vm/media')
ISO = BASE / 'windows11-enterprise-eval-ko-kr.iso'
PREFIX = ISO.with_suffix('.iso.partial')
PARTS = BASE / 'parts'
URL = 'https://software-static.download.prss.microsoft.com/dbazure/26300.9457.260913-1737.26h2_ge_release_svc_refresh_CLIENTENTERPRISEEVAL_OEMRET_x64FRE_ko-kr.iso'
SIZE = 8225404928
SHA256 = 'b7ec51f0d332ac2aa4f7faeffbefac961ce6eb7204b5876b48fa8d9f1cf0a9a6'
BLOCK = 512 * 1024 * 1024


def download(interval):
    start, end = interval
    path = PARTS / f'{start:012d}-{end:012d}.bin'
    if path.exists() and path.stat().st_size == end - start + 1:
        return path
    subprocess.run(['curl', '--fail', '--location', '--silent', '--show-error',
                    '--max-time', '1200', '--range', f'{start}-{end}',
                    '--output', str(path), URL], check=True)
    if path.stat().st_size != end - start + 1:
        raise RuntimeError('Microsoft server did not return the requested byte range.')
    return path


def main():
    BASE.mkdir(parents=True, exist_ok=True)
    PARTS.mkdir(exist_ok=True)
    if ISO.exists():
        digest = hashlib.sha256()
        with ISO.open('rb') as stream:
            while block := stream.read(8 * 1024 * 1024):
                digest.update(block)
        if ISO.stat().st_size != SIZE or digest.hexdigest() != SHA256:
            raise RuntimeError('Existing ISO does not match the Microsoft SHA-256; it was left unchanged.')
        (BASE / 'verified-iso.json').write_text(json.dumps({
            'source': URL, 'sha256': SHA256, 'bytes': SIZE,
            'verification_source': 'https://go.microsoft.com/fwlink/?LinkId=2382606',
            'edition': 'Windows 11 Enterprise Evaluation 26H2', 'language': 'ko-KR',
        }, indent=2))
        print('Existing Microsoft ISO SHA-256: VERIFIED; no download was repeated.')
        return
    offset = PREFIX.stat().st_size if PREFIX.exists() else 0
    if offset > SIZE:
        raise RuntimeError('Existing prefix is larger than the expected official image.')
    ranges = [(p, min(p + BLOCK, SIZE) - 1) for p in range(offset, SIZE, BLOCK)]
    print(f'Reusing prefix: {offset} bytes; remaining ranges: {len(ranges)}', flush=True)
    with ThreadPoolExecutor(max_workers=6) as workers:
        paths = list(workers.map(download, ranges))
    building = ISO.with_suffix('.iso.building')
    digest = hashlib.sha256()
    sources = ([PREFIX] if offset else []) + paths
    with building.open('wb') as output:
        for source in sources:
            with source.open('rb') as stream:
                while data := stream.read(8 * 1024 * 1024):
                    digest.update(data)
                    output.write(data)
    if building.stat().st_size != SIZE or digest.hexdigest() != SHA256:
        raise RuntimeError('ISO failed the Microsoft SHA-256 check. No unverified ISO will be used.')
    building.replace(ISO)
    (BASE / 'verified-iso.json').write_text(json.dumps({
        'source': URL, 'sha256': SHA256, 'bytes': SIZE,
        'verification_source': 'https://go.microsoft.com/fwlink/?LinkId=2382606',
        'edition': 'Windows 11 Enterprise Evaluation 26H2', 'language': 'ko-KR',
    }, indent=2))
    for source in sources:
        source.unlink()
    print('Microsoft official ISO SHA-256: VERIFIED', flush=True)


if __name__ == '__main__':
    main()
FOLDER_ORGANIZER_VM_STATIC_2_END
cat > '/workspace/.folder-organizer-tools/windows-vm/prepare.py' <<'FOLDER_ORGANIZER_VM_STATIC_3_END'
#!/usr/bin/env python3
"""Prepare a fresh Windows evaluation VM without changing the project checkout."""
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import ssl
import subprocess
import xml.etree.ElementTree as ET

BASE = Path('/workspace/.folder-organizer-tools/windows-vm')
NATIVE = BASE / 'native'
STATE = BASE / 'state'
MEDIA = BASE / 'media'
SEED = STATE / 'seed'
WCM = 'http://schemas.microsoft.com/WMIConfig/2002/State'
ET.register_namespace('wcm', WCM)


def node(parent, tag, value=None, **attributes):
    result = ET.SubElement(parent, tag, attributes)
    if value is not None:
        result.text = str(value)
    return result


def add(parent, name):
    return node(parent, name, **{'{' + WCM + '}action': 'add'})


def component(settings, name):
    return node(settings, 'component', name=name, processorArchitecture='amd64',
                publicKeyToken='31bf3856ad364e35', language='neutral', versionScope='nonSxS')


def main():
    os.umask(0o077)
    if (STATE / 'windows.qcow2').exists():
        raise SystemExit('An existing VM disk was found and left unchanged.')
    iso = MEDIA / 'windows11-enterprise-eval-ko-kr.iso'
    receipt = json.loads((MEDIA / 'verified-iso.json').read_text())
    if not iso.is_file() or iso.stat().st_size != receipt['bytes']:
        raise SystemExit('The verified Microsoft ISO is missing or changed.')
    STATE.mkdir(parents=True, exist_ok=True)
    STATE.chmod(0o700)
    SEED.mkdir(exist_ok=True)
    (SEED / 'certs').mkdir(exist_ok=True)
    credentials_path = STATE / 'credentials.json'
    if credentials_path.exists():
        credentials = json.loads(credentials_path.read_text())
    else:
        credentials = {'username': 'Developer', 'password': secrets.token_urlsafe(24) + 'Aa9!'}
        credentials_path.write_text(json.dumps(credentials))
        credentials_path.chmod(0o600)
    password = credentials['password']
    root = ET.Element('unattend', {'xmlns': 'urn:schemas-microsoft-com:unattend'})
    pe = node(root, 'settings', **{'pass': 'windowsPE'})
    locale = component(pe, 'Microsoft-Windows-International-Core-WinPE')
    node(node(locale, 'SetupUILanguage'), 'UILanguage', 'ko-KR')
    for key, value in {'InputLocale': '0412:00000412', 'SystemLocale': 'ko-KR',
                       'UILanguage': 'ko-KR', 'UserLocale': 'ko-KR'}.items():
        node(locale, key, value)
    setup = component(pe, 'Microsoft-Windows-Setup')
    disk_configuration = node(setup, 'DiskConfiguration')
    disk = add(disk_configuration, 'Disk')
    node(disk, 'DiskID', 0)
    node(disk, 'WillWipeDisk', 'true')
    partitions = node(disk, 'CreatePartitions')
    for order, kind, size in [(1, 'EFI', 260), (2, 'MSR', 16), (3, 'Primary', None)]:
        partition = add(partitions, 'CreatePartition')
        node(partition, 'Order', order)
        node(partition, 'Type', kind)
        node(partition, 'Size' if size else 'Extend', size if size else 'true')
    modifications = node(disk, 'ModifyPartitions')
    for order, partition_id, filesystem, label in [(1, 1, 'FAT32', 'System'), (2, 3, 'NTFS', 'Windows')]:
        modification = add(modifications, 'ModifyPartition')
        node(modification, 'Order', order)
        node(modification, 'PartitionID', partition_id)
        node(modification, 'Format', filesystem)
        node(modification, 'Label', label)
        if partition_id == 3:
            node(modification, 'Letter', 'C')
    node(disk_configuration, 'WillShowUI', 'OnError')
    os_image = node(node(setup, 'ImageInstall'), 'OSImage')
    metadata = add(node(os_image, 'InstallFrom'), 'MetaData')
    node(metadata, 'Key', '/IMAGE/INDEX')
    node(metadata, 'Value', 1)
    target = node(os_image, 'InstallTo')
    node(target, 'DiskID', 0)
    node(target, 'PartitionID', 3)
    node(os_image, 'WillShowUI', 'OnError')
    user_data = node(setup, 'UserData')
    node(user_data, 'AcceptEula', 'true')
    node(user_data, 'FullName', 'Developer')
    node(user_data, 'Organization', 'Cloud development')

    specialize = node(root, 'settings', **{'pass': 'specialize'})
    shell = component(specialize, 'Microsoft-Windows-Shell-Setup')
    node(shell, 'ComputerName', 'FO-WIN11')
    node(shell, 'TimeZone', 'Korea Standard Time')

    oobe = node(root, 'settings', **{'pass': 'oobeSystem'})
    locale = component(oobe, 'Microsoft-Windows-International-Core')
    for key, value in {'InputLocale': '0412:00000412', 'SystemLocale': 'ko-KR',
                       'UILanguage': 'ko-KR', 'UserLocale': 'ko-KR'}.items():
        node(locale, key, value)
    shell = component(oobe, 'Microsoft-Windows-Shell-Setup')
    options = node(shell, 'OOBE')
    for key in ['HideEULAPage', 'HideOnlineAccountScreens', 'HideWirelessSetupInOOBE', 'HideOEMRegistrationScreen']:
        node(options, key, 'true')
    node(options, 'ProtectYourPC', 3)
    account = add(node(node(shell, 'UserAccounts'), 'LocalAccounts'), 'LocalAccount')
    node(account, 'Name', credentials['username'])
    node(account, 'DisplayName', 'Developer')
    node(account, 'Group', 'Administrators')
    account_password = node(account, 'Password')
    node(account_password, 'Value', password)
    node(account_password, 'PlainText', 'true')
    logon = node(shell, 'AutoLogon')
    node(logon, 'Username', credentials['username'])
    node(logon, 'Domain', 'FO-WIN11')
    node(logon, 'Enabled', 'true')
    node(logon, 'LogonCount', 1)
    logon_password = node(logon, 'Password')
    node(logon_password, 'Value', password)
    node(logon_password, 'PlainText', 'true')
    first = add(node(shell, 'FirstLogonCommands'), 'SynchronousCommand')
    node(first, 'Order', 1)
    node(first, 'Description', 'Prepare the local development desktop')
    node(first, 'CommandLine', "powershell.exe -NoProfile -ExecutionPolicy Bypass -Command \"$drive = [System.IO.DriveInfo]::GetDrives() | Where-Object { $_.IsReady -and $_.VolumeLabel -eq 'FOSETUP' } | Select-Object -First 1; & (Join-Path $drive.RootDirectory.FullName 'Bootstrap.ps1')\"")
    ET.ElementTree(root).write(SEED / 'Autounattend.xml', encoding='utf-8', xml_declaration=True)
    shutil.copyfile(BASE / 'guest-bootstrap.ps1', SEED / 'Bootstrap.ps1')
    shutil.copyfile(BASE / 'guest-report.ps1', SEED / 'Report.ps1')

    certificate_file = next((Path(os.environ[key]) for key in ['CODEX_PROXY_CERT', 'SSL_CERT_FILE']
                             if os.environ.get(key) and Path(os.environ[key]).is_file()), None)
    if certificate_file is None:
        raise SystemExit('The managed proxy CA file was not available.')
    certificates = re.findall(r'-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----',
                              certificate_file.read_text(), re.S)
    if not certificates:
        raise SystemExit('The managed proxy CA file contained no PEM certificate.')
    for index, certificate in enumerate(certificates):
        (SEED / 'certs' / f'proxy-ca-{index}.cer').write_bytes(ssl.PEM_cert_to_DER_cert(certificate))
    env = os.environ.copy()
    env['LD_LIBRARY_PATH'] = str(NATIVE / 'usr/lib/x86_64-linux-gnu')
    subprocess.run([str(NATIVE / 'usr/bin/genisoimage'), '-quiet', '-J', '-r', '-V', 'FOSETUP',
                    '-o', str(STATE / 'setup.iso'), str(SEED)], env=env, check=True)
    status_disk = STATE / 'diagnostics.img'
    with status_disk.open('wb') as output:
        output.truncate(64 * 1024 * 1024)
    subprocess.run([str(NATIVE / 'usr/bin/mformat'), '-i', str(status_disk), '-F',
                    '-v', 'FOLOGS', '::'], env=env, check=True)
    status_id = STATE / 'FOLOGS.ID'
    status_id.write_text('Public guest readiness metadata only.\n')
    subprocess.run([str(NATIVE / 'usr/bin/mcopy'), '-i', str(status_disk),
                    str(status_id), '::/FOLOGS.ID'], env=env, check=True)
    shutil.copyfile(NATIVE / 'usr/share/OVMF/OVMF_VARS_4M.ms.fd', STATE / 'nvram.fd')
    subprocess.run([str(NATIVE / 'usr/bin/qemu-img'), 'create', '-f', 'qcow2',
                    str(STATE / 'windows.qcow2'), '64G'], env=env, check=True, stdout=subprocess.DEVNULL)
    (STATE / 'vm.json').write_text(json.dumps({
        'name': 'FO-WIN11', 'cpus': 2, 'memory_mib': 8192, 'disk_gib': 64,
        'cpu_model': 'Haswell-v2,pmu=off,vmx=off',
        'acceleration': 'tcg', 'rdp_host': '127.0.0.1', 'rdp_port': 13389,
        'edition': receipt['edition'], 'language': receipt['language'], 'installed': False,
        'install_media_boot_consumed': False,
    }, indent=2))
    print('Fresh Windows VM prepared: 2 vCPU, 8 GiB RAM, 64 GiB virtual disk.')
    print('A strong local account password was generated in a private owner-only state file; no password was printed.')


if __name__ == '__main__':
    main()
FOLDER_ORGANIZER_VM_STATIC_3_END
cat > '/workspace/.folder-organizer-tools/windows-vm/guest-bootstrap.ps1' <<'FOLDER_ORGANIZER_VM_STATIC_4_END'
$ErrorActionPreference = 'Stop'
$OutputDirectory = 'C:\CloudVM'
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
function Stage([string]$Name) { Set-Content -Path "$OutputDirectory\bootstrap-stage.txt" -Value $Name -Encoding UTF8 }
function Find-Drive([string]$Label) {
    [System.IO.DriveInfo]::GetDrives() | Where-Object { $_.IsReady -and $_.VolumeLabel -eq $Label } | Select-Object -First 1
}
try {
    Stage 'find-local-media'
    $SeedDrive = Find-Drive 'FOSETUP'
    if (-not $SeedDrive) { throw 'Bootstrap media was not found.' }
    $Seed = $SeedDrive.RootDirectory.FullName
    Stage 'import-managed-proxy-certificates'
    foreach ($Certificate in Get-ChildItem (Join-Path $Seed 'certs\*.cer')) {
        Import-Certificate -FilePath $Certificate.FullName -CertStoreLocation Cert:\LocalMachine\Root | Out-Null
    }
    & netsh.exe winhttp set proxy '10.0.2.2:18080' | Out-Null
    $InternetSettings = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings'
    $MachineInternet = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Internet Settings'
    $ProxyPolicy = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\CurrentVersion\Internet Settings'
    foreach ($Location in @($InternetSettings,$MachineInternet,$ProxyPolicy)) {
        if (-not (Test-Path $Location)) { New-Item -Path $Location -Force | Out-Null }
    }
    Set-ItemProperty $ProxyPolicy ProxySettingsPerUser 0
    Set-ItemProperty $MachineInternet ProxyEnable 1
    Set-ItemProperty $MachineInternet ProxyServer 'http=10.0.2.2:18080;https=10.0.2.2:18080'
    Set-ItemProperty $MachineInternet ProxyOverride '<local>'
    Set-ItemProperty $InternetSettings ProxyEnable 1
    Set-ItemProperty $InternetSettings ProxyServer 'http=10.0.2.2:18080;https=10.0.2.2:18080'
    Set-ItemProperty $InternetSettings ProxyOverride '<local>'
    Stage 'configure-rdp'
    Set-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server' fDenyTSConnections 0
    Set-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp' UserAuthentication 1
    & netsh.exe advfirewall firewall set rule 'name=CloudVM local RDP' new enable=yes | Out-Null
    if ($LASTEXITCODE -ne 0) {
        & netsh.exe advfirewall firewall add rule 'name=CloudVM local RDP' dir=in action=allow protocol=TCP localport=3389 profile=any | Out-Null
    }
    Set-Service TermService -StartupType Automatic
    Start-Service TermService
    Set-LocalUser -Name Developer -PasswordNeverExpires $true
    Set-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power' HiberbootEnabled 0
    & powercfg.exe /hibernate off | Out-Null
    $Winlogon = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
    Set-ItemProperty $Winlogon AutoAdminLogon '0'
    Remove-ItemProperty $Winlogon DefaultPassword -ErrorAction SilentlyContinue
    Remove-ItemProperty $Winlogon AutoLogonCount -ErrorAction SilentlyContinue
    Stage 'read-rdp-certificate'
    $RdpCertificate = $null
    for ($Attempt = 0; $Attempt -lt 40 -and -not $RdpCertificate; $Attempt++) {
        $RdpCertificate = Get-ChildItem 'Cert:\LocalMachine\Remote Desktop' -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $RdpCertificate) { Start-Sleep -Milliseconds 500 }
    }
    if (-not $RdpCertificate) { throw 'RDP certificate was not created.' }
    $Hasher = [System.Security.Cryptography.SHA256]::Create()
    $RdpSha256 = ([BitConverter]::ToString($Hasher.ComputeHash($RdpCertificate.RawData))).Replace('-','').ToLowerInvariant()
    $Hasher.Dispose()
    $OS = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
    $Addresses = @([System.Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces() | ForEach-Object {
        $_.GetIPProperties().UnicastAddresses | Where-Object { $_.Address.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork -and -not [System.Net.IPAddress]::IsLoopback($_.Address) } | ForEach-Object { $_.Address.ToString() }
    })
    $Ready = [ordered]@{
        status = 'ready'; computer = $env:COMPUTERNAME; account = 'Developer'
        edition = $OS.ProductName; display_version = $OS.DisplayVersion
        version = [System.Environment]::OSVersion.Version.ToString(); build = $OS.CurrentBuildNumber
        rdp_port = 3389; rdp_cert_sha256 = $RdpSha256
        timezone = [System.TimeZoneInfo]::Local.Id; proxy = '10.0.2.2:18080'; ipv4 = $Addresses
    }
    $Json = $Ready | ConvertTo-Json -Compress
    Set-Content -Path "$OutputDirectory\ready.json" -Value $Json -Encoding UTF8
    $StatusDrive = Find-Drive 'FOLOGS'
    if ($StatusDrive) {
        $Bytes = [System.Text.Encoding]::UTF8.GetBytes($Json)
        $File = [System.IO.File]::Open((Join-Path $StatusDrive.RootDirectory.FullName 'ready.json'), [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write, [System.IO.FileShare]::Read)
        try { $File.Write($Bytes,0,$Bytes.Length); $File.Flush($true) } finally { $File.Dispose() }
    }
    Stage 'ready'
    Start-Process -FilePath cscript.exe -ArgumentList '//B',"$env:SystemRoot\System32\slmgr.vbs",'/ato' -WindowStyle Hidden
} catch {
    Set-Content -Path "$OutputDirectory\bootstrap-error.txt" -Value $_.Exception.Message -Encoding UTF8
    exit 1
}
FOLDER_ORGANIZER_VM_STATIC_4_END
cat > '/workspace/.folder-organizer-tools/windows-vm/guest-report.ps1' <<'FOLDER_ORGANIZER_VM_STATIC_5_END'
$ErrorActionPreference = 'Stop'
$OutputDirectory = 'C:\CloudVM'
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$Products = @(Get-CimInstance SoftwareLicensingProduct -Filter "ApplicationID='55c92734-d682-4d71-983e-d6ec3f16059f'" | Where-Object PartialProductKey | ForEach-Object {
    [ordered]@{
        name = $_.Name
        description = $_.Description
        license_status = $_.LicenseStatus
        status_reason = $_.LicenseStatusReason
        grace_minutes = $_.GracePeriodRemaining
        evaluation_end = $(if ($_.EvaluationEndDate) { $_.EvaluationEndDate.ToString('o') } else { $null })
    }
})
$Report = [ordered]@{
    computer = $env:COMPUTERNAME
    account = $env:USERNAME
    build = (Get-CimInstance Win32_OperatingSystem).BuildNumber
    term_service = (Get-Service TermService).Status.ToString()
    products = $Products
}
$Json = $Report | ConvertTo-Json -Depth 5 -Compress
Set-Content -Path "$OutputDirectory\license.json" -Value $Json -Encoding UTF8
$StatusDrive = (Get-CimInstance Win32_LogicalDisk | Where-Object VolumeName -eq 'FOLOGS' | Select-Object -First 1).DeviceID
if ($StatusDrive) {
    $Bytes = [System.Text.Encoding]::UTF8.GetBytes($Json)
    $File = [System.IO.File]::Open("$StatusDrive\license.json", [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write, [System.IO.FileShare]::Read)
    try { $File.Write($Bytes, 0, $Bytes.Length); $File.Flush($true) } finally { $File.Dispose() }
}
FOLDER_ORGANIZER_VM_STATIC_5_END
cat > '/workspace/.folder-organizer-tools/windows-vm/proxy-relay.py' <<'FOLDER_ORGANIZER_VM_STATIC_6_END'
#!/usr/bin/env python3
"""Relay guest HTTP proxy traffic only to the environment's configured proxy."""
import os
import select
import socket
import socketserver
from urllib.parse import urlsplit

proxy = urlsplit(os.environ.get('HTTPS_PROXY') or os.environ['HTTP_PROXY'])
if proxy.scheme != 'http' or not proxy.hostname or proxy.username or proxy.password:
    raise SystemExit('Unsupported managed proxy configuration.')


class Relay(socketserver.BaseRequestHandler):
    def handle(self):
        with socket.create_connection((proxy.hostname, proxy.port or 8080), timeout=15) as upstream:
            upstream.settimeout(None)
            peers = [self.request, upstream]
            while True:
                ready, _, _ = select.select(peers, [], [], 120)
                if not ready:
                    return
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        return
                    (upstream if source is self.request else self.request).sendall(data)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


with Server(('127.0.0.1', 18080), Relay) as server:
    server.serve_forever()
FOLDER_ORGANIZER_VM_STATIC_6_END
cat > '/workspace/.folder-organizer-tools/windows-vm/vm.py' <<'FOLDER_ORGANIZER_VM_STATIC_7_END'
#!/usr/bin/env python3
"""Manage the local Windows VM. Credentials never appear in process arguments."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import socket
import subprocess
import time

BASE = Path('/workspace/.folder-organizer-tools/windows-vm')
NATIVE = BASE / 'native'
STATE = BASE / 'state'
RUN = Path(f'/tmp/folder-organizer-windows-vm-{os.getuid()}')
PROCESSES = RUN / 'processes.json'


def birth(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return fields[19] if fields[0] != 'Z' else None
    except OSError:
        return None


def alive(record):
    return bool(record and record.get('birth') and birth(record['pid']) == record['birth'])


def records():
    return json.loads(PROCESSES.read_text()) if PROCESSES.exists() else {}


def qmp(command, arguments=None):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(str(RUN / 'qmp.sock'))
        stream = client.makefile('rwb')
        json.loads(stream.readline())
        for request in [{'execute': 'qmp_capabilities'}, {'execute': command, 'arguments': arguments or {}}]:
            stream.write((json.dumps(request) + '\n').encode())
            stream.flush()
            while True:
                response = json.loads(stream.readline())
                if 'event' not in response:
                    break
            if 'error' in response:
                raise RuntimeError(response['error'])
        return response['return']


def guest_ready():
    candidates = []
    path = STATE / 'serial.log'
    if path.exists():
        for line in reversed(path.read_text(errors='replace').splitlines()):
            if 'FO_VM_READY ' in line:
                candidates.append(line.split('FO_VM_READY ', 1)[1])
                break
    # The status disk carries public readiness/certificate metadata only. Some
    # Windows images do not enumerate the legacy serial device.
    status_disk = STATE / 'diagnostics.img'
    mtype = NATIVE / 'usr/bin/mtype'
    if status_disk.exists() and mtype.exists():
        env = os.environ.copy()
        env['LD_LIBRARY_PATH'] = str(NATIVE / 'usr/lib/x86_64-linux-gnu')
        try:
            response = subprocess.run([str(mtype), '-i', str(status_disk), '::/ready.json'],
                                      env=env, capture_output=True, timeout=3)
            if response.returncode == 0 and len(response.stdout) <= 16384:
                candidates.insert(0, response.stdout.decode('utf-8-sig'))
        except (OSError, subprocess.TimeoutExpired, UnicodeError):
            pass
    saved = STATE / 'guest-ready.json'
    if saved.exists():
        candidates.append(saved.read_text())
    for candidate in candidates:
        try:
            result = json.loads(candidate)
            if (not isinstance(result, dict) or result.get('computer') != 'FO-WIN11'
                    or not re.fullmatch('[0-9a-f]{64}', result.get('rdp_cert_sha256', ''))):
                continue
            config = json.loads((STATE / 'vm.json').read_text())
            if not config.get('installed'):
                config['installed'] = True
                config['os_installation_complete'] = True
                (STATE / 'vm.json').write_text(json.dumps(config, indent=2))
            saved.write_text(json.dumps(result, ensure_ascii=False, indent=2))
            return result
        except (ValueError, TypeError):
            pass
    return None


def rdp_service_ready():
    try:
        with socket.create_connection(('127.0.0.1', 13389), timeout=2) as connection:
            connection.settimeout(8)
            connection.sendall(bytes.fromhex('030000130ee000000000000100080003000000'))
            response = connection.recv(64)
            return len(response) >= 11 and response[0] == 3 and response[5] == 0xd0
    except OSError:
        return False


def spawn(command, env, logfile):
    with (STATE / logfile).open('ab') as output:
        proc = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                                stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
    return proc, {'pid': proc.pid, 'birth': birth(proc.pid)}


def start():
    os.umask(0o077)
    RUN.mkdir(mode=0o700, exist_ok=True)
    data = records()
    if alive(data.get('qemu')):
        print('The managed Windows VM is already running.')
        return
    if any(alive(record) for record in data.values()):
        raise SystemExit('A partial managed VM session remains; inspect it before restarting.')
    config = json.loads((STATE / 'vm.json').read_text())
    first_install_boot = not config.get('installed') and not config.get('install_media_boot_consumed')
    maintenance_boot = bool(config.get('maintenance_boot_once'))
    firmware_variables = STATE / 'nvram.fd'
    if maintenance_boot:
        # Existing Windows Boot Manager entries can override firmware boot order.
        # Recovery uses separate variables and leaves normal UEFI state intact.
        firmware_variables = STATE / 'recovery-nvram.fd'
        shutil.copyfile(NATIVE / 'usr/share/OVMF/OVMF_VARS_4M.ms.fd', firmware_variables)
    for port in [13389, 18080]:
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', port))
    for name in ['qmp.sock', 'tpm.sock', 'qga.sock']:
        (RUN / name).unlink(missing_ok=True)
    (STATE / 'tpm').mkdir(exist_ok=True)
    env = os.environ.copy()
    env['LD_LIBRARY_PATH'] = ':'.join([str(NATIVE / 'usr/lib/x86_64-linux-gnu'),
                                     str(NATIVE / 'usr/lib/x86_64-linux-gnu/swtpm')])
    env['QEMU_MODULE_DIR'] = str(NATIVE / 'usr/lib/x86_64-linux-gnu/qemu')
    env['TZ'] = 'Asia/Seoul'
    _, data['proxy'] = spawn(['python3', str(BASE / 'proxy-relay.py')], env, 'proxy.log')
    tpm, data['tpm'] = spawn([str(NATIVE / 'usr/bin/swtpm'), 'socket', '--tpm2',
                             '--tpmstate', f'dir={STATE / "tpm"}',
                             '--ctrl', f'type=unixio,path={RUN / "tpm.sock"}'], env, 'tpm.log')
    PROCESSES.write_text(json.dumps(data))
    deadline = time.monotonic() + 10
    while not (RUN / 'tpm.sock').exists():
        if tpm.poll() is not None or time.monotonic() > deadline:
            raise RuntimeError('TPM emulator did not initialize; see state/tpm.log.')
        time.sleep(.1)
    command = [
        str(NATIVE / 'usr/bin/qemu-system-x86_64'),
        '-name', 'FO-WIN11', '-machine', 'q35,smm=on',
        # TCG cannot provide the hardware PMU. Advertising it made Windows
        # execute RDPMC in the kernel and crash with 0x3B / 0xC0000096.
        '-accel', 'tcg,thread=multi', '-cpu', config.get('cpu_model', 'Haswell-v2,pmu=off,vmx=off'),
        '-smp', str(config['cpus']), '-m', str(config['memory_mib']),
        '-L', str(NATIVE / 'usr/share/qemu'),
        '-global', 'driver=cfi.pflash01,property=secure,value=on',
        '-drive', f'if=pflash,format=raw,unit=0,readonly=on,file={NATIVE / "usr/share/OVMF/OVMF_CODE_4M.ms.fd"}',
        '-drive', f'if=pflash,format=raw,unit=1,file={firmware_variables}',
        '-drive', f'file={STATE / "windows.qcow2"},format=qcow2,if=ide,index=0,cache=writeback,discard=unmap',
        '-chardev', f'socket,id=chrtpm,path={RUN / "tpm.sock"}',
        '-tpmdev', 'emulator,id=tpm0,chardev=chrtpm', '-device', 'tpm-tis,tpmdev=tpm0',
        '-netdev', 'user,id=net0,hostfwd=tcp:127.0.0.1:13389-:3389',
        '-device', 'e1000e,netdev=net0,romfile=',
        '-vga', 'none', '-device', f'VGA,romfile={NATIVE / "usr/share/seabios/vgabios-stdvga.bin"}',
        '-device', 'qemu-xhci,id=xhci', '-device', 'usb-tablet',
        '-display', 'none',
        '-qmp', f'unix:{RUN / "qmp.sock"},server=on,wait=off',
        '-rtc', 'base=localtime,clock=host', '-boot', 'order=c,once=d,menu=off' if first_install_boot or maintenance_boot else 'order=c,menu=off',
    ]
    if config.get('guest_agent') and not maintenance_boot:
        command += ['-chardev', f'socket,id=guestagent,path={RUN / "qga.sock"},server=on,wait=off',
                    '-serial', 'chardev:guestagent']
    else:
        command += ['-serial', f'file:{STATE / "serial.log"}']
    if not config.get('installed') or maintenance_boot:
        command += ['-drive', f'file={BASE / "media/windows11-enterprise-eval-ko-kr.iso"},media=cdrom,if=ide,index=2,readonly=on']
        # Recovery must never see the unattended disk-wiping installation media.
        seed_media = STATE / ('recovery.iso' if maintenance_boot else 'setup.iso')
        command += ['-drive', f'file={seed_media},media=cdrom,if=ide,index=3,readonly=on']
    if (STATE / 'diagnostics.img').exists():
        command += ['-drive', f'file={STATE / "diagnostics.img"},format=raw,if=none,id=gueststatus',
                    '-device', 'usb-storage,drive=gueststatus,bus=xhci.0']
    vm, data['qemu'] = spawn(command, env, 'qemu.log')
    PROCESSES.write_text(json.dumps(data))
    deadline = time.monotonic() + 15
    while not (RUN / 'qmp.sock').exists():
        if vm.poll() is not None or time.monotonic() > deadline:
            raise RuntimeError('QEMU did not initialize; see state/qemu.log.')
        time.sleep(.1)
    if first_install_boot or maintenance_boot:
        for _ in range(15):
            if vm.poll() is not None:
                raise RuntimeError('QEMU exited; see state/qemu.log.')
            qmp('send-key', {'keys': [{'type': 'qcode', 'data': 'spc'}], 'hold-time': 100})
            time.sleep(1)
        if first_install_boot:
            config['install_media_boot_consumed'] = True
        if maintenance_boot:
            config['maintenance_boot_once'] = False
        (STATE / 'vm.json').write_text(json.dumps(config, indent=2))
    print('Windows VM started. RDP is bound only to 127.0.0.1:13389.')
    print('Guest proxy traffic uses only the managed HTTP/HTTPS proxy.')


def status():
    data = records()
    config = json.loads((STATE / 'vm.json').read_text())
    ready = guest_ready()
    serial = (STATE / 'serial.log').read_text(errors='replace') if (STATE / 'serial.log').exists() else ''
    allocated = (STATE / 'windows.qcow2').stat().st_blocks * 512 / 1024**3
    service_ready = rdp_service_ready() if alive(data.get('qemu')) else False
    phase = ('configured_rdp_responding' if ready and service_ready else 'configured_waiting_for_rdp' if ready else
             'initializing_windows' if config.get('os_installation_complete') else
             'installing_windows' if allocated > .1 else 'firmware_or_installer')
    if not alive(data.get('qemu')):
        phase = 'stopped'
    if 'FO_VM_ERROR' in serial and not ready:
        phase = 'bootstrap_failed'
    result = {'processes': {key: alive(record) for key, record in data.items()},
              'guest_phase': phase, 'rdp_service_responding': service_ready,
              'disk_allocated_gib': round(allocated, 2),
              'guest': ready}
    if alive(data.get('qemu')):
        result['emulator'] = qmp('query-status')
        result['block_statistics'] = [
            {'device': item.get('device'),
             'read_mib': round(item.get('stats', {}).get('rd_bytes', 0) / 1024**2),
             'written_mib': round(item.get('stats', {}).get('wr_bytes', 0) / 1024**2)}
            for item in qmp('query-blockstats')]
    print(json.dumps(result, indent=2, ensure_ascii=False))


def stop():
    data = records()
    if alive(data.get('qemu')):
        qmp('system_powerdown')
        deadline = time.monotonic() + 50
        while alive(data.get('qemu')) and time.monotonic() < deadline:
            time.sleep(.5)
        if alive(data.get('qemu')):
            raise SystemExit('Guest shutdown is still pending. No force-stop or disk reset was performed.')
    for name in ['proxy', 'tpm']:
        record = data.get(name)
        if alive(record):
            os.killpg(record['pid'], signal.SIGTERM)
    deadline = time.monotonic() + 5
    while any(alive(data.get(name)) for name in ['proxy', 'tpm']) and time.monotonic() < deadline:
        time.sleep(.1)
    if any(alive(data.get(name)) for name in ['proxy', 'tpm']):
        raise SystemExit('Guest is stopped, but auxiliary processes are still exiting; retry stop before start.')
    PROCESSES.unlink(missing_ok=True)
    print('Windows VM stopped after guest shutdown.')


def rdp(auth_only=False, bootstrap=False):
    ready = guest_ready()
    if not ready and not bootstrap:
        raise SystemExit('Windows bootstrap has not reported readiness.')
    credentials = json.loads((STATE / 'credentials.json').read_text())
    env = os.environ.copy()
    desktop_env = Path(f'/tmp/folder-organizer-desktop-{os.getuid()}/environment.sh')
    if desktop_env.exists():
        for line in desktop_env.read_text().splitlines():
            key, value = line.removeprefix('export ').split('=', 1)
            env[key] = shlex.split(value)[0]
    client_dir = Path('/workspace/.folder-organizer-tools/windows/native')
    env['LD_LIBRARY_PATH'] = str(client_dir / 'usr/lib/x86_64-linux-gnu')
    # First-use trust is limited to this task-owned loopback VM. FreeRDP stores
    # and checks its certificate on later connections; verification stays on.
    certificate = f'/cert:fingerprint:sha256:{ready["rdp_cert_sha256"]}' if ready else '/cert:tofu'
    options = ['/v:127.0.0.1:13389', f'/u:{credentials["username"]}', '/d:FO-WIN11',
               f'/p:{credentials["password"]}', certificate,
               '/dynamic-resolution', '/timeout:120000', '/log-level:WARN']
    if auth_only:
        options.append('+auth-only')
    reader, writer = os.pipe()
    with (STATE / ('rdp-auth.log' if auth_only else 'rdp-desktop.log')).open('ab') as output:
        proc = subprocess.Popen([str(client_dir / 'usr/bin/xfreerdp3'), f'/args-from:fd:{reader}'],
                                env=env, pass_fds=(reader,), stdin=subprocess.DEVNULL,
                                stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
    os.close(reader)
    with os.fdopen(writer, 'wb') as stream:
        stream.write(('\n'.join(options) + '\n').encode())
    if auth_only:
        try:
            code = proc.wait(timeout=150)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            raise SystemExit('RDP connection timed out; Windows desktop readiness is unconfirmed.')
        print('Windows RDP authentication exit:', code)
        if code:
            raise SystemExit(code)
    else:
        time.sleep(2)
        if proc.poll() is not None:
            raise SystemExit(f'RDP desktop exited with code {proc.returncode}; see the private RDP log.')
        print('FreeRDP client launched in local Linux display; inspect it to confirm the Windows desktop.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['start', 'status', 'stop', 'auth-check', 'desktop',
                                         'bootstrap-auth-check', 'bootstrap-desktop'])
    action = parser.parse_args().action
    {'start': start, 'status': status, 'stop': stop,
     'auth-check': lambda: rdp(True), 'desktop': rdp,
     'bootstrap-auth-check': lambda: rdp(True, True),
     'bootstrap-desktop': lambda: rdp(False, True)}[action]()
FOLDER_ORGANIZER_VM_STATIC_7_END
cat > '/workspace/.folder-organizer-tools/windows-vm/keyboard.py' <<'FOLDER_ORGANIZER_VM_STATIC_8_END'
#!/usr/bin/env python3
"""Send installation-console keys to the task-owned VM only."""
import runpy
import json
from pathlib import Path
import sys
import time

qmp = runpy.run_path('/workspace/.folder-organizer-tools/windows-vm/vm.py')['qmp']


def tap(*keys):
    qmp('input-send-event', {'events': [{'type': 'key', 'data': {'down': True, 'key': {'type': 'qcode', 'data': key}}} for key in keys]})
    try:
        time.sleep(.12)
    finally:
        qmp('input-send-event', {'events': [{'type': 'key', 'data': {'down': False, 'key': {'type': 'qcode', 'data': key}}} for key in reversed(keys)]})
    time.sleep(.12)


def text(value):
    plain = {' ': 'spc', '.': 'dot', ',': 'comma', '/': 'slash', '\\': 'backslash',
             '-': 'minus', '=': 'equal', ';': 'semicolon', "'": 'apostrophe',
             '[': 'bracket_left', ']': 'bracket_right', '`': 'grave_accent'}
    shifted = {':': 'semicolon', '>': 'dot', '<': 'comma', '|': 'backslash',
               '_': 'minus', '+': 'equal', '"': 'apostrophe', '?': 'slash',
               '&': '7', '(': '9', ')': '0', '!': '1', '$': '4', '%': '5',
               '^': '6', '*': '8', '{': 'bracket_left', '}': 'bracket_right',
               '@': '2', '#': '3', '~': 'grave_accent'}
    for character in value:
        if character.isascii() and character.isalpha():
            tap(*(['shift', character.lower()] if character.isupper() else [character]))
        elif character.isdigit():
            tap(character)
        elif character in plain:
            tap(plain[character])
        elif character in shifted:
            tap('shift', shifted[character])
        else:
            raise ValueError('Unsupported installation-console character.')


if __name__ == '__main__':
    if sys.argv[1] == 'command-prompt':
        tap('shift', 'f10')
    elif sys.argv[1] == 'type':
        text(sys.argv[2])
        tap('ret')
    elif sys.argv[1] == 'sign-in':
        # Call only after confirming Developer's password field has focus.
        credentials = json.loads(Path('/workspace/.folder-organizer-tools/windows-vm/state/credentials.json').read_text())
        tap('ctrl', 'a')
        text(credentials['password'])
        tap('ret')
        print('Private Developer credential submitted; no credential printed.')
    else:
        raise SystemExit('Usage: keyboard.py command-prompt | type TEXT | sign-in')
FOLDER_ORGANIZER_VM_STATIC_8_END
cat > '/workspace/.folder-organizer-tools/windows-vm/monitor.py' <<'FOLDER_ORGANIZER_VM_STATIC_9_END'
#!/usr/bin/env python3
"""Report installation progress without running application tests."""
import json
from pathlib import Path
import runpy
import time

vm = runpy.run_path('/workspace/.folder-organizer-tools/windows-vm/vm.py')
state = Path('/workspace/.folder-organizer-tools/windows-vm/state')
deadline = time.monotonic() + 3600
service_seen_at = None
while time.monotonic() < deadline:
    records = vm['records']()
    if not vm['alive'](records.get('qemu')):
        raise SystemExit('The VM process stopped before readiness.')
    ready = vm['guest_ready']()
    service = vm['rdp_service_ready']()
    if service and service_seen_at is None:
        service_seen_at = time.monotonic()
    allocated = (state/'windows.qcow2').stat().st_blocks * 512 / 1024**3
    report = {'disk_allocated_gib':round(allocated,2), 'guest_ready':bool(ready),
              'rdp_service_responding':service}
    if ready:
        report['windows_build'] = ready.get('build')
    serial = (state/'serial.log').read_text(errors='replace')
    if 'FO_VM_ERROR ' in serial and not ready:
        password = json.loads((state/'credentials.json').read_text())['password']
        message = serial.split('FO_VM_ERROR ')[-1].splitlines()[0]
        report['bootstrap_error'] = message.replace(password, '[REDACTED]')
        print(json.dumps(report), flush=True)
        raise SystemExit(2)
    print(json.dumps(report), flush=True)
    if ready and service:
        raise SystemExit(0)
    if service and not ready and time.monotonic() - service_seen_at > 90:
        raise SystemExit('RDP is responding, but guest metadata must be recovered from C:\\CloudVM\\ready.json.')
    time.sleep(30)
raise SystemExit('Installation monitoring deadline reached; VM state was preserved.')
FOLDER_ORGANIZER_VM_STATIC_9_END
cat > '/workspace/.folder-organizer-tools/windows-vm/README.md' <<'FOLDER_ORGANIZER_VM_STATIC_10_END'
# Windows inside this cloud environment

The existing Windows 11 Enterprise Evaluation installation runs inside the Linux
cloud environment using QEMU TCG: two virtual CPUs, 8 GiB RAM, a sparse 64 GiB
disk, UEFI Secure Boot, and TPM 2.0. Hardware acceleration (/dev/kvm) is not
available, so boot and first-user setup are slow. No external Windows PC, cloud
provider account, or VPN is used for this local VM.

The Korean evaluation ISO was downloaded from Microsoft and checked against its
published SHA-256 before installation:

`b7ec51f0d332ac2aa4f7faeffbefac961ce6eb7204b5876b48fa8d9f1cf0a9a6`

Sources: https://www.microsoft.com/en-us/evalcenter/download-windows-11-enterprise
and https://go.microsoft.com/fwlink/?LinkId=2382606 . The download receipt is
media/verified-iso.json. This is an evaluation license; normal Microsoft
activation and the evaluation term apply. No purchased product key was supplied.
Activation must be checked before claiming the evaluation is usable for its term.

## Run the retained VM

```sh
python3 /workspace/.folder-organizer-tools/windows-vm/vm.py start
python3 /workspace/.folder-organizer-tools/windows-vm/vm.py status
python3 /workspace/.folder-organizer-tools/windows-vm/vm.py auth-check
python3 /workspace/.folder-organizer-tools/windows-vm/vm.py desktop
python3 /workspace/.folder-organizer-tools/windows-vm/vm.py stop
```

Use the execution tool's normal approved host context for VM and display sockets.
Start the local display when a graphical RDP session is needed:

```sh
python3 /workspace/.folder-organizer-tools/desktop/desktop.py start
. /workspace/.folder-organizer-tools/desktop/activate.sh
```

`desktop` opens FreeRDP inside that authenticated Xorg/XFCE display (:99). The
provider declares a shared desktop on demand, but the current task does not expose
its desktop control/open-viewer tools. A local window alone does not establish a
user-facing shared viewer. No public viewer URL or public RDP endpoint is created.

RDP forwards only 127.0.0.1:13389 to this VM's port 3389. The helper passes the
private Developer credential through an inherited file descriptor and pins the
guest certificate. It neither prints the password nor includes it in process
arguments. Guest HTTP/HTTPS uses the configured managed proxy through the loopback
relay and imported proxy CA. Preserve inherited proxy and certificate settings.

Credentials, unattended setup media, and private state stay owner-only under
state/. Never print credentials.json, copy passwords into configuration fields,
dump unattended XML, or commit these files. For manual initial sign-in only,
keyboard.py sign-in reads the stored credential internally; first confirm that
the Developer password field is actually focused. Slower key timings are needed
under software emulation.

## Preserve the installed disk

Windows installation completed successfully. It originally auto-logged the
OOBE-only defaultuser0 account, leaving the Developer bootstrap unexecuted. On
2026-11-16, Microsoft Windows PE repaired the login settings using native reg.exe.
The original installed disk was preserved before repair. **Both of these are
required by the current disk chain:**

- state/windows.qcow2: active copy-on-write layer.
- state/windows.pre-logon-repair.qcow2: retained installed backing disk.

Do not delete, move, overwrite, or flatten either file as routine setup. Unused
diagnostic disk copies were removed; they are not rollback dependencies. Normal starts
use state/nvram.fd; recovery used separate recovery-nvram.fd and recovery.iso.
Recovery must never attach unattended disk-wiping setup.iso while booting the
Windows installer. Do not reinstall to fix missing readiness metadata.

`installed=false` means bootstrap has not reported readiness; it does not prove
Windows is uninstalled. Readiness can arrive through serial, the small FOLOGS FAT
status disk, or retained public metadata. Check real RDP negotiation and account
authentication before declaring connectivity.

Use vm.py stop for a clean guest shutdown. A slow guest can outlast the helper's
first 50-second wait; inspect it and allow shutdown to finish. Do not force-stop
the normal installed VM just because that wait expires. Never publish a running
VM disk. Processes and /tmp sockets must be recreated after environment restore.

## Setup and verification status

The existing-disk path of install-vm.sh has been run successfully and preserved the
baseline disk, private account state, and VM settings. It does not replay disk
preparation or unattended installation. A fresh installation path exists only for
an absent disk and is not independently verified as a new-task restoration.

As of 2026-11-16, the Windows native OOBE log reports successful completion
(exit 0, IMAGE_STATE_COMPLETE). Bootstrap completed, and public ready.json
reports build 26300, IP 10.0.2.15 and the RDP certificate fingerprint. WinHTTP
and full-machine WinINET use 10.0.2.2:18080, and the environment proxy root CA
is installed. Native HTTPS returned HTTP 200 with curl's per-request
--ssl-revoke-best-effort option because the managed certificate has no revocation
method; chain and hostname validation remain enabled.

An earlier RDP negotiation and Developer NLA login succeeded, but the latest
pinned connection timed out during RDP session activation. The client now allows
120 seconds for connection setup. This does not establish a usable desktop:
Explorer.EXE logged an APPCRASH in twinui.pcshell.dll (0x80270233), and the Windows
console still displays a waiting screen. The local Linux desktop is running.

Normal Microsoft activation failed: Security-SPP logged 0x8004FE93 and an HTTP
503 response. The same activation endpoint also returned 503 from the host, with the managed
proxy reporting an upstream connection failure.
Activation is incomplete. No activation workaround, rearm or clock change was
used. The current chat has no tool to open a user-facing shared viewer.

The CPU must remain Haswell-v2,pmu=off,vmx=off: exposing the previous max CPU's
hardware performance counters caused a confirmed RDPMC kernel crash under TCG.
The experimental COM1 guest agent is disabled and is not a working control path.
Use guest-native reads and the FAT metadata disk for current provisioning state;
raw inspection of the live encrypted Windows volume can be stale.

Application source, tests, manifests, and lockfiles remain unchanged. No
application regression suite, build, or publication is part of this environment
setup. See START.md for the complete retained startup instructions.

Normal ACPI shutdown completed after the persistent shell failure. The retained
Windows disk was started again without force-stop or reinstall; the guest is
booting, and desktop recovery is not yet verified. The stop helper now waits for
its proxy/TPM processes to exit before removing records, avoiding an observed
loopback-port release race during immediate restart.
FOLDER_ORGANIZER_VM_STATIC_10_END
cat > '/workspace/.folder-organizer-tools/windows-vm/START.md' <<'FOLDER_ORGANIZER_VM_STATIC_11_END'
Use the existing /workspace/Folder_Organizer checkout. Cloud tasks are already isolated; do not make another checkout/worktree unless requested. Preserve application source, tests, dependency declarations, lockfiles, existing files, and local commits during environment setup. Use polite Korean with this user. Do not publish without a new explicit user instruction.

Before project development commands:
    . /workspace/.folder-organizer-tools/activate.sh
    cd /workspace/Folder_Organizer

The retained toolchain includes Python 3.12, the pinned runtime/dev/bundle extras, setuptools 80.9.0, wheel 0.45.1, pip 26.3, and user-local Rust 1.99.1 with Cargo/rustfmt/Clippy and a four-job limit. Linux Cargo.lock dependencies including optional GUI dependencies are cached. The .venv, writable caches, and inherited proxy/TLS trust are configured. Use cargo +1.99.1 with the subcommand before --manifest-path rust-app/Cargo.toml and --locked. GUI checks and Windows packaging run only for a requested task.

Do not automatically launch the repository application, run builds/tests, take application screenshots, or publish merely to start the environment. Follow docs/RELEASE_POLICY.md: skip passed and unchanged-feature suites, run only relevant new/changed/failed cases, and capture application screens only when the user requests them. Necessary VM installation/login diagnostics are separate environment troubleshooting; do not turn them into release gates. Previously passed dependency checks are not an application regression suite. A fresh-task restoration is not independently verified.

Preserve inherited proxy and CA settings; inspect actual environment policy/readiness before network work. HTTP/HTTPS networking is unrestricted in the selected published environment. Do not disable TLS/checksum validation, rewrite policy JSON, or install a separate VPN. No external Windows host or VPN is required for the local Windows VM.

Cloud-local Windows is the user's chosen Windows workflow, superseding the earlier external-PC connection. Its files are /workspace/.folder-organizer-tools/windows-vm. It uses QEMU TCG (no /dev/kvm), 2 vCPU, 8 GiB RAM, a sparse 64 GiB disk, UEFI Secure Boot, and TPM 2.0. Boot/login can be slow under software emulation. The media is official Microsoft Korean Windows 11 Enterprise Evaluation with a verified Microsoft SHA-256; it is an evaluation license, not a purchased Windows license. Use normal Microsoft activation and respect its term. Never bypass activation or change the clock to extend it.

Start/resume the existing installation:
    python3 /workspace/.folder-organizer-tools/windows-vm/vm.py start
    python3 /workspace/.folder-organizer-tools/windows-vm/vm.py status

Never run prepare.py on an existing disk or replay Autounattend installation to refresh tools. installed=false can mean Bootstrap is incomplete; it does not prove Windows needs reinstalling. Readiness is reported through optional COM1 serial, the small FOLOGS FAT metadata disk, and retained guest metadata; confirm a real RDP negotiation/authentication before claiming connectivity. RDP is forwarded ONLY from host 127.0.0.1:13389 to this VM's 3389. Guest HTTP/HTTPS uses the existing managed proxy via a loopback relay. This is not an external-host RDP connection and needs no user-supplied Windows IP or managed VPN grant.

The Developer administrator password is generated privately in state/credentials.json. NEVER print it, put it in process arguments, copy it into configuration, or dump private unattended media/hives. The RDP helper uses an inherited FD and a pinned guest certificate. keyboard.py sign-in may submit the private credential only after checking the Developer password field is focused. Its slower key timings were needed for reliable software-emulated input. Do not guess/reset credentials merely because a key transmission failed.

Desktop use:
1. Prefer the managed shared desktop if tools are actually available. The latest environment declares remote_desktop enabled/on demand, but setup-time tool discovery exposed no desktop js/open_in_codex tool and cloud-agent-desktop reported unavailable. Declared capability is not proof a viewer works. Use the cloud-desktop skill's supported tools when offered; do not invent a viewer URL. The local :99 session below does not automatically appear in a separate platform desktop.
2. To use the retained authenticated Xorg/XFCE display:
       python3 /workspace/.folder-organizer-tools/desktop/desktop.py start
       . /workspace/.folder-organizer-tools/desktop/activate.sh
   It uses owner-only Xauthority under /tmp and binds no external X11 TCP port. Qt XCB, XFCE, session D-Bus, and GTK file-chooser portal were verified previously. Use the execution tool's normal approved host context for desktop/QMP sockets; sandbox reachability failure does not prove the host session is down.
3. Once Windows metadata and service are ready:
       python3 /workspace/.folder-organizer-tools/windows-vm/vm.py auth-check
       python3 /workspace/.folder-organizer-tools/windows-vm/vm.py desktop
   desktop opens FreeRDP inside the prepared local Linux desktop. A working local window is not proof of a connected user-facing shared viewer. If a valid platform display is available, use its supported session rather than promising a public endpoint.
4. To stop Windows, use vm.py stop and allow normal guest shutdown to finish. The first 50-second wait can expire on slow software emulation; inspect current state and continue waiting. Do not force-stop the normal installed guest merely because that wait elapsed. Stop the managed Linux session separately with desktop.py stop if needed. Running processes and /tmp sockets do not survive environment snapshots.

Important retained recovery state: current state/windows.qcow2 has state/windows.pre-logon-repair.qcow2 as a required backing file. KEEP BOTH. The original installed disk was preserved while Windows PE repaired a defaultuser0 automatic-logon loop. Do not delete, move, flatten, replace, or reinstall either disk as routine setup. Windows PE recovery used separate recovery-nvram.fd and recovery.iso; maintenance boot must NEVER attach setup.iso containing unattended disk-wiping instructions. Normal starts use the retained normal nvram.fd and boot the existing installation.

The combined repeatable setup is saved in install_script and /workspace/.folder-organizer-tools/install-with-windows-vm.sh. Run it only when installation/refresh is needed. It retains an existing Windows disk and prepares a fresh disk only when none exists; it does not launch Windows or application suites. Do not claim a new task was independently restored merely because the configuration saved successfully.

Before any user-authorized environment publication/snapshot, shut Windows down cleanly. Do not publish a running VM disk. Saving setup instructions as a draft does not publish or apply that draft; preserve unrelated configuration and repositories when updating it.

CPU compatibility: use Haswell-v2,pmu=off,vmx=off for this TCG VM. The previous max CPU exposed unsupported RDPMC reads and caused confirmed Windows kernel crashes. PMU/VMX/SVM are off. Preserve this CPU setting. The experimental COM1 guest agent is disabled and is not a working management route.

Current confirmed Windows state (2026-11-16): OOBE finished successfully (native log exit0, IMAGE_STATE_COMPLETE at18:14:56 UTC); Bootstrap completed and FAT ready.json reports build26300, IP10.0.2.15 and the guest SHA256 RDP certificate. WinHTTP and full-machine WinINET proxy10.0.2.2:18080 and the managed root CA are configured. Native HTTPS returned200 with per-request curl --ssl-revoke-best-effort because the managed certificate has no revocation method; chain and hostname verification remain enabled. Do not globally weaken TLS/revocation checks.

Desktop readiness is NOT confirmed. Earlier Developer NLA/RDP authentication passed, but the latest pinned auth failed at RDP session activation (exit131); a longer-timeout graphical client connected to a black screen and was then disconnected by Windows. Explorer crashed in twinui.pcshell.dll with0x80270233. The console shows a waiting screen. vm.py now allows120s connection setup and bounds auth checks at150s; status reports configured_waiting_for_rdp rather than assuming boot. Inspect the actual current screen and logs before declaring a working desktop. No application was launched.

Normal evaluation activation FAILED: Security-SPP reported0x8004FE93 and HTTP503 from activation-v2.sls.microsoft.com, and a host request to the same endpoint returned503. No license workaround, rearm or clock change was used. Do not claim the evaluation is activated. Preserve this blocker until a normal Microsoft activation succeeds.

Viewer limitation: this environment settings chat exposes no desktop js/CUA/open_in_codex/open_cloud_preview tool, and the user's shown panel has no Desktop/+ menu. Do not repeat earlier unsupported menu guidance, invent a viewer URL, or treat local Xorg/FreeRDP windows as user-visible. User-facing shared viewing is blocked independently of Windows initialization.

Use native Windows reads and FAT metadata for provisioning state; raw reads of the live encrypted NTFS volume can be stale. Never print private Cached-unattend.xml or credentials. Unused diagnostic disk copies were deleted after verifying the active chain; retain BOTH active windows.qcow2 and required windows.pre-logon-repair.qcow2. No publication occurred; save only as a draft. Fresh-task restoration remains unverified.

Latest runtime action: normal ACPI shutdown completed and the retained Windows disk was restarted without force-stop or reinstall. The guest is booting; usable desktop recovery remains unverified. vm.py stop now waits for its proxy/TPM processes to exit before removing records, preventing an observed immediate-restart port release race. Check actual QMP/process and Windows UI state before claiming readiness. The activation HTTP503 body says upstream connection failure at the managed proxy; do not bypass that proxy.
FOLDER_ORGANIZER_VM_STATIC_11_END
bash /workspace/.folder-organizer-tools/windows-vm/install-vm.sh
