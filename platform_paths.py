"""Platform-neutral, local-only output paths for PG DDL Runner.

No database connections or DDL logic are handled here. Avoid platform-invalid
filename characters and preserve legacy Linux backup/report naming.
"""
import datetime
import os
from pathlib import Path

WINDOWS = os.name == 'nt'


def state_root():
    """State directory, configurable without affecting migration identity."""
    explicit = os.environ.get('PGDDL_STATE_HOME')
    if explicit:
        return Path(explicit).expanduser()
    if WINDOWS:
        local = os.environ.get('LOCALAPPDATA')
        return Path(local) / 'PGDDL' if local else Path.home() / 'AppData' / 'Local' / 'PGDDL'
    return Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local' / 'state'))) / 'pgddl'


def timestamp_part(when=None):
    """Date stamp for filenames, not for database identities or SQL."""
    when = when or datetime.datetime.now().astimezone()
    return when.strftime('%d-%m-%Y_%H-%M' if WINDOWS else '%d-%m-%Y_%H:%M')


def sync_directory(path):
    """Best-effort directory durability on POSIX; unsupported by Windows CRT.

    File contents are still fsynced separately. Do not hide POSIX sync errors.
    """
    if WINDOWS:
        return
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
