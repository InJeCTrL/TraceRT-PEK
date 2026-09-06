#!/usr/bin/env python3
"""Start, stop, restart, and inspect the TraceRT-PEK web service."""

import argparse
import os
import signal
import shlex
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PID_FILE = ROOT / '.server.pid'
LOG_FILE = ROOT / 'logs' / 'server-service.log'
SERVER = ROOT / 'server.py'
SCHEDULER = ROOT / 'update_scheduler.py'
SCHEDULER_PID_FILE = ROOT / '.update-scheduler.pid'
HEALTH_URL = 'http://127.0.0.1:8765/api/health'


def read_pid():
    try:
        return int(PID_FILE.read_text(encoding='ascii').strip())
    except (FileNotFoundError, ValueError):
        return None


def running(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True


def healthy():
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=1) as response:
            return response.status == 200
    except Exception:
        return False


def read_scheduler_pid():
    try:
        return int(SCHEDULER_PID_FILE.read_text(encoding='ascii').strip())
    except (FileNotFoundError, ValueError):
        return None


def scheduler_running():
    return running(read_scheduler_pid())


def scheduler_command(command):
    result = subprocess.run(
        [sys.executable, str(SCHEDULER), command],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
    )
    output = (result.stdout or result.stderr).strip()
    if output:
        print(output, file=sys.stderr if result.returncode else sys.stdout)
    return result.returncode


def start():
    env_file = ROOT / '.env'
    if env_file.exists():
        for line in env_file.read_text(encoding='utf-8').splitlines():
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            key, separator, value = line.partition('=')
            if separator and key.strip() in (
                    'AMAP_API_KEY', 'AMAP_JS_KEY', 'AMAP_JS_SECURITY_CODE',
                    'ADMIN_USERNAME', 'ADMIN_PASSWORD'):
                parts = shlex.split(value, comments=True)
                os.environ.setdefault(key.strip(), ' '.join(parts))
    pid = read_pid()
    if running(pid) and healthy():
        print(f'服务已运行，PID {pid}')
        return scheduler_command('start')
    if healthy():
        print('端口 8765 已有可用服务运行；未重复启动')
        return scheduler_command('start')
    PID_FILE.unlink(missing_ok=True)
    LOG_FILE.parent.mkdir(exist_ok=True)
    with LOG_FILE.open('ab', buffering=0) as output:
        process = subprocess.Popen(
            [sys.executable, '-u', str(SERVER)],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    PID_FILE.write_text(f'{process.pid}\n', encoding='ascii')
    for _ in range(100):
        if process.poll() is not None:
            PID_FILE.unlink(missing_ok=True)
            print(f'服务启动失败，查看日志：{LOG_FILE}', file=sys.stderr)
            return process.returncode or 1
        if healthy():
            print(f'服务已启动，PID {process.pid}；日志：{LOG_FILE}')
            return scheduler_command('start')
        time.sleep(.1)
    os.killpg(process.pid, signal.SIGTERM)
    PID_FILE.unlink(missing_ok=True)
    print(f'服务启动超时，查看日志：{LOG_FILE}', file=sys.stderr)
    return 1


def stop():
    scheduler_result = scheduler_command('stop')
    pid = read_pid()
    if not running(pid):
        PID_FILE.unlink(missing_ok=True)
        print('服务未运行')
        return scheduler_result
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    for _ in range(100):
        if not running(pid):
            PID_FILE.unlink(missing_ok=True)
            print('服务已停止')
            return scheduler_result
        time.sleep(.1)
    os.killpg(pid, signal.SIGKILL)
    PID_FILE.unlink(missing_ok=True)
    print('服务已强制停止')
    return scheduler_result


def status():
    pid = read_pid()
    if running(pid) and healthy():
        print(f'服务运行中，PID {pid}；{HEALTH_URL}')
        server_ok = True
    elif healthy():
        print('服务运行中，但不是由 service.py 启动')
        server_ok = True
    else:
        print('服务未运行')
        server_ok = False
    scheduler_pid = read_scheduler_pid()
    if scheduler_running():
        print(f'摄像头定期更新运行中，PID {scheduler_pid}；每 6 小时检查一次')
        scheduler_ok = True
    else:
        print('摄像头定期更新未运行')
        scheduler_ok = False
    return 0 if server_ok and scheduler_ok else 1


def restart():
    result = stop()
    return result or start()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('start', 'stop', 'restart', 'status'))
    command = parser.parse_args().command
    return {'start': start, 'stop': stop, 'restart': restart, 'status': status}[command]()


if __name__ == '__main__':
    raise SystemExit(main())
