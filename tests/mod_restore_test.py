"""mod uninstall / mod restore against a fake game folder (no real game, no engine needed).

    python tests/mod_restore_test.py <path to studio-plus.exe> [--wine]
"""
import json, os, subprocess, sys, tempfile

EXE = os.path.abspath(sys.argv[1])
WINE = '--wine' in sys.argv[2:]
T = tempfile.mkdtemp(prefix='sp-restore-')
GAME = os.path.join(T, 'Skate')
MODS = os.path.join(GAME, 'Mods')
BACKUPS = os.path.join(GAME, '.ReSkateStudio-Mod-backup')


def native(p):
    return 'Z:' + p.replace('/', '\\') if WINE else p


def sp(*args):
    env = dict(os.environ, RSSP_DATA_DIR=native(os.path.join(T, 'data')))
    os.makedirs(os.path.join(T, 'data'), exist_ok=True)
    wine = ['/usr/lib/wine/wine64' if os.path.exists('/usr/lib/wine/wine64') else 'wine64'] if WINE else []
    if WINE: env['WINEDEBUG'] = '-all'
    p = subprocess.run(wine + [EXE, *args, '--game-root', native(GAME), '--json'], capture_output=True, text=True, env=env, timeout=300)
    return json.loads(p.stdout)


def mod(folder, name, version, studio=True):
    d = os.path.join(folder, name)
    os.makedirs(d)
    open(os.path.join(d, 'layout.toc'), 'w').write(version)
    if studio: open(os.path.join(d, '.reskate-studio-patch'), 'w').write('stamp\nskate_sha256=' + '0' * 64 + '\n')


def version(path): return open(os.path.join(path, 'layout.toc')).read()


os.makedirs(os.path.join(GAME, 'Data'))
open(os.path.join(GAME, 'Skate.exe'), 'w').write('not really')
mod(MODS, 'Scooter', 'v2')
mod(MODS, 'Other', 'x', studio=False)
mod(BACKUPS, 'Scooter', 'v1')  # what deploy keeps when it replaces v1 with v2

r = sp('mod', 'uninstall', 'Other')
assert not r['ok'] and r['error']['code'] == 'mod_folder_not_managed', r
assert os.path.isdir(os.path.join(MODS, 'Other'))
r = sp('mod', 'uninstall', 'Missing')
assert not r['ok'] and r['error']['code'] == 'mod_not_found', r
r = sp('mod', 'restore', '..\\Skate.exe')
assert not r['ok'] and r['error']['code'] == 'invalid_arguments', r

# Undo the last deploy: v1 comes back, v2 is kept as a backup.
r = sp('mod', 'restore', 'Scooter')
assert r['ok'], r
kept = r['result']['previous_kept_as']
assert kept.startswith('Scooter (replaced ') and version(os.path.join(MODS, 'Scooter')) == 'v1'
assert version(os.path.join(BACKUPS, kept)) == 'v2' and not os.path.exists(os.path.join(BACKUPS, 'Scooter'))
print('restore swaps the installed version with the backup:', kept)

# Uninstall, then restore it from the backup uninstall made.
r = sp('mod', 'uninstall', 'Scooter')
assert r['ok'], r
removed = r['result']['backup']
assert removed.startswith('Scooter (removed ') and not os.path.exists(os.path.join(MODS, 'Scooter'))
assert version(os.path.join(BACKUPS, removed)) == 'v1'
listed = sp('mod', 'list')['result']
assert removed in [b['name'] for b in listed['backups']] and 'Scooter' not in [m['name'] for m in listed['mods']], listed['backups']
r = sp('mod', 'restore', removed)
assert r['ok'] and r['result']['mod_folder'] == 'Scooter' and 'previous_kept_as' not in r['result'], r
assert version(os.path.join(MODS, 'Scooter')) == 'v1' and not os.path.exists(os.path.join(BACKUPS, removed))
print('uninstall moves it to the backups and restore puts it back:', removed)
print('mod uninstall / restore: all checks passed')
