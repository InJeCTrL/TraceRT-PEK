"""Seed a new persistent volume without replacing existing user data."""
import fcntl
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time


def initialize_data(data, seed):
    data.mkdir(parents=True, exist_ok=True)
    with (data / '.seed.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for name in ('camera_points.json', 'data_update_status.json'):
            target = data / name
            if target.exists():
                continue
            temporary = target.with_suffix('.seed.tmp')
            shutil.copyfile(seed / name, temporary)
            temporary.replace(target)


def supervise(commands, grace_seconds=10):
    children = []
    stopping = False

    def request_stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    def signal_children(signum):
        for child in children:
            try:
                os.killpg(child.pid, signum)
            except ProcessLookupError:
                pass

    previous = {sig: signal.signal(sig, request_stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        for command in commands:
            if stopping:
                return 0
            children.append(subprocess.Popen(command, start_new_session=True))
        while not stopping:
            for child in children:
                code = child.poll()
                if code is not None:
                    print(f'Child {child.pid} exited unexpectedly ({code}); restarting container', flush=True)
                    return 1
            time.sleep(.2)
        return 0
    finally:
        # Signal process groups so an in-flight update cannot outlive its scheduler.
        signal_children(signal.SIGTERM)
        deadline = time.monotonic() + grace_seconds
        while any(child.poll() is None for child in children) and time.monotonic() < deadline:
            time.sleep(.1)
        signal_children(signal.SIGKILL)
        for child in children:
            child.wait()
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == '__main__':
    initialize_data(Path(os.environ['TRACERT_DATA_DIR']), Path(__file__).parent / 'seed')
    if sys.argv[1:] == ['serve']:
        raise SystemExit(supervise([
            [sys.executable, '-u', str(Path(__file__).parent / 'server.py')],
            [sys.executable, '-u', str(Path(__file__).parent / 'update_scheduler.py'), 'run'],
        ]))
    os.execvp(sys.argv[1], sys.argv[1:])
