"""Seed a new persistent volume without replacing existing user data."""
import fcntl
import os
from pathlib import Path
import shutil
import sys


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


if __name__ == '__main__':
    initialize_data(Path(os.environ['TRACERT_DATA_DIR']), Path(__file__).parent / 'seed')
    os.execvp(sys.argv[1], sys.argv[1:])
