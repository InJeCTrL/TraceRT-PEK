#!/usr/bin/env python3
"""Refresh GCJ-02 camera data from one long-lived Python process."""

import argparse
import fcntl
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from threading import Event
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / 'logs'
LOG_FILE = LOG_DIR / 'update-scheduler.log'
PID_FILE = Path(os.environ.get('TRACERT_RUNTIME_DIR', ROOT)) / '.update-scheduler.pid'
LOCK_FILE = Path(os.environ.get('TRACERT_DATA_DIR', ROOT)) / '.update-scheduler.lock'
UPDATE_SCRIPT = ROOT / 'update_avoid_points.py'
TIMEZONE = ZoneInfo('Asia/Shanghai')

CAMERA_INTERVAL = int(os.environ.get('TRACERT_CAMERA_UPDATE_INTERVAL', 6 * 60 * 60))
RETRY_INTERVAL = int(os.environ.get('TRACERT_UPDATE_RETRY_INTERVAL', 5 * 60))
TASKS = (('cameras', CAMERA_INTERVAL),)


def log(message):
    timestamp = datetime.now(TIMEZONE).isoformat(timespec='seconds')
    print(f'[{timestamp}] {message}', flush=True)


def pid_is_running(pid):
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True
    return True


def read_pid():
    try:
        return int(PID_FILE.read_text(encoding='ascii').strip())
    except (FileNotFoundError, ValueError):
        return None


def run_update(kind):
    started = time.monotonic()
    log(f'{kind} update started')
    result = subprocess.run(
        [sys.executable, str(UPDATE_SCRIPT)],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
    )
    elapsed = time.monotonic() - started
    if result.returncode == 0:
        log(f'{kind} update completed in {elapsed:.1f}s')
        return True
    log(f'{kind} update failed with exit code {result.returncode} after {elapsed:.1f}s')
    return False


def run_scheduler():
    LOCK_FILE.touch(exist_ok=True)
    with LOCK_FILE.open('r+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('update scheduler is already running', file=sys.stderr)
            return 1

        PID_FILE.write_text(f'{os.getpid()}\n', encoding='ascii')
        stopped = Event()

        def stop_scheduler(signum, _frame):
            log(f'received signal {signum}; stopping after the active update')
            stopped.set()

        signal.signal(signal.SIGTERM, stop_scheduler)
        signal.signal(signal.SIGINT, stop_scheduler)
        next_run = {kind: time.monotonic() for kind, _ in TASKS}
        intervals = dict(TASKS)
        log(f'scheduler started; camera interval={CAMERA_INTERVAL}s, retry interval={RETRY_INTERVAL}s')
        try:
            while not stopped.is_set():
                now = time.monotonic()
                due = [kind for kind, _ in TASKS if next_run[kind] <= now]
                if not due:
                    stopped.wait(max(1, min(next_run.values()) - now))
                    continue
                for kind in due:
                    if stopped.is_set():
                        break
                    succeeded = run_update(kind)
                    delay = intervals[kind] if succeeded else RETRY_INTERVAL
                    next_run[kind] = time.monotonic() + delay
        finally:
            PID_FILE.unlink(missing_ok=True)
            log('scheduler stopped')
    return 0


def start_scheduler():
    pid = read_pid()
    if pid and pid_is_running(pid):
        print(f'update scheduler is already running (pid {pid})')
        return 0
    PID_FILE.unlink(missing_ok=True)
    LOG_DIR.mkdir(exist_ok=True)
    with LOG_FILE.open('ab', buffering=0) as output:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), 'run'],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    for _ in range(30):
        if process.poll() is not None:
            print(f'update scheduler failed to start; see {LOG_FILE}', file=sys.stderr)
            return process.returncode or 1
        pid = read_pid()
        if pid == process.pid:
            print(f'update scheduler started (pid {pid}); log: {LOG_FILE}')
            return 0
        time.sleep(.1)
    process.terminate()
    print(f'update scheduler did not become ready; see {LOG_FILE}', file=sys.stderr)
    return 1


def stop_scheduler():
    pid = read_pid()
    if not pid or not pid_is_running(pid):
        PID_FILE.unlink(missing_ok=True)
        print('update scheduler is not running')
        return 0
    os.kill(pid, signal.SIGTERM)
    for _ in range(100):
        if not pid_is_running(pid):
            print('update scheduler stopped')
            return 0
        time.sleep(.1)
    print(f'update scheduler is still stopping (pid {pid})')
    return 1


def scheduler_status():
    pid = read_pid()
    if pid and pid_is_running(pid):
        print(f'update scheduler is running (pid {pid}); log: {LOG_FILE}')
        return 0
    print('update scheduler is not running')
    return 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('start', 'run', 'stop', 'status'))
    args = parser.parse_args()
    commands = {
        'start': start_scheduler,
        'run': run_scheduler,
        'stop': stop_scheduler,
        'status': scheduler_status,
    }
    return commands[args.command]()


if __name__ == '__main__':
    raise SystemExit(main())
