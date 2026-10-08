"""project export: builds a ReSkate Studio .fbproject (format in docs/discovery/mesh-replace.md), exports it
with `studio-plus project export`, and decodes the .fbmod it writes to check every resource came through.

    python tests/project_export_test.py <path to studio-plus.exe> [--wine]

Needs no game folder (payloads are stored raw without one). --wine runs the exe under Wine (Linux).
"""
import hashlib, json, os, random, struct, subprocess, sys, tempfile

EXE = os.path.abspath(sys.argv[1])
WINE = '--wine' in sys.argv[2:]
T = tempfile.mkdtemp(prefix='sp-export-')
rnd = random.Random(7)


def native(p):
    return 'Z:' + p.replace('/', '\\') if WINE else p


def sp(*args):
    env = dict(os.environ, RSSP_DATA_DIR=native(os.path.join(T, 'data')))
    os.makedirs(os.path.join(T, 'data'), exist_ok=True)
    wine = ['/usr/lib/wine/wine64' if os.path.exists('/usr/lib/wine/wine64') else 'wine64'] if WINE else []
    if WINE: env['WINEDEBUG'] = '-all'
    cmd = wine + [EXE, *args, '--json']
    p = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=300)
    return p.returncode, json.loads(p.stdout) if p.stdout.strip() else None, p.stderr


def s32(t): b = t.encode(); return struct.pack('<I', len(b)) + b
def guid(): return bytes(rnd.getrandbits(8) for _ in range(16))
def frosty_hash(t):
    h = 5381
    for c in t.encode(): h = ((h * 33) ^ c) & 0xFFFFFFFF
    return h

res = []
res.append(dict(kind=1, added=True, name='items/board_bottomart/own_scooter', user='', bundles=['win32/items/a'], sb=[], links=['x_ap'],
                data=bytes(rnd.getrandbits(8) for _ in range(3534)), id=guid()))
res.append(dict(kind=2, added=False, name='characters/skateboard/static/static_skateboard_mesh', user='reskate-cosmetic-original-mesh:v1|donor=x',
                type=0x49B156D4, rid=0x1122334455667788, meta=bytes(range(16)), bundles=['b1', 'b2'], sb=[], links=['6a76d5b0-f8e8-556c-454e-c038e7d63973'],
                data=bytes(rnd.getrandbits(8) for _ in range(2992))))
cid = guid()
res.append(dict(kind=3, added=True, name='6a76d5b0-f8e8-556c-454e-c038e7d63973', user='', bundles=['b1'], sb=['win32/items'], links=[],
                data=bytes(rnd.getrandbits(8) for _ in range(200000)), id=cid, rs=16, re=4096, lo=0, ls=0, mip=2))
res.append(dict(kind=3, added=False, name='dup', user='', bundles=[], sb=[], links=[], data=res[2]['data'], id=guid(), rs=0, re=0, lo=0, ls=0, mip=-1))  # same payload

out = b'RSPROJT1' + struct.pack('<I', 2) + s32('Skate') + struct.pack('<I', 625200)
for t in ['Razor Scooter', 'loki', 'Cosmetics', '2.0', 'A scooter', 'https://x']: out += s32(t)
out += bytes(40) + struct.pack('<I', len(res))
for r in res:
    out += struct.pack('<I', r['kind'] | (0x100 if r['added'] else 0)) + s32(r['name']) + s32(r['user'])
    if r['kind'] == 2: out += struct.pack('<IQQ', r['type'], r['rid'], len(r['meta'])) + r['meta']
    else: out += struct.pack('<IQQ', 0, 0, 0)
    out += (bytes(16) if r['kind'] == 2 else r['id']) + (r['id'] if r['kind'] == 3 else bytes(16))
    if r['kind'] == 3: out += struct.pack('<IIIQi', r['rs'], r['re'], r['lo'], len(r['data']), r['mip'])
    else: out += struct.pack('<IIIQi', 0, 0, 0, 0, -1)
    for lst in (r['bundles'], r['sb'], r['links']):
        out += struct.pack('<I', len(lst)) + b''.join(s32(x) for x in lst)
    out += b'\0' + struct.pack('<Q', len(r['data'])) + r['data']
proj = os.path.join(T, 'Test Project.fbproject')
open(proj, 'wb').write(out)


# 1. default output name, title override
code, j, err = sp('project', 'export', native(proj), '--author', 'Studio+ test')
assert code == 0 and j['ok'], (code, j, err[-500:])
r = j['result']; print('export result:', json.dumps(r))
assert r['resources'] == 4 and r['ebx'] == 1 and r['res'] == 1 and r['chunks'] == 2 and r['added'] == 2
assert r['title'] == 'Razor Scooter' and r['author'] == 'Studio+ test' and r['version'] == '2.0'
mod = open(os.path.join(T, 'Test Project.fbmod'), 'rb').read()

# 2. parse the fbmod
class R:
    def __init__(s, b): s.b, s.a = b, 0
    def u(s, f): v = struct.unpack_from('<' + f, s.b, s.a); s.a += struct.calcsize('<' + f); return v[0] if len(v) == 1 else v
    def cstr(s): e = s.b.index(b'\0', s.a); t = s.b[s.a:e].decode(); s.a = e + 1; return t
    def dotnet(s):
        n = shift = 0
        while True:
            c = s.b[s.a]; s.a += 1; n |= (c & 0x7F) << shift; shift += 7
            if c < 0x80: break
        t = s.b[s.a:s.a + n].decode(); s.a += n; return t
m = R(mod)
assert m.u('Q') == 0x01005954534F5246 and m.u('I') == 6
data_offset, data_count = m.u('q'), m.u('i')
assert m.dotnet() == 'Skate' and m.u('I') == 625200
meta = [m.cstr() for _ in range(6)]; assert meta == ['Razor Scooter', 'Studio+ test', 'Cosmetics', '2.0', 'A scooter', 'https://x'], meta
count = m.u('i'); assert count == 4 + 5
entries = []
for i in range(count):
    t, idx, name = m.u('B'), m.u('i'), m.cstr()
    e = dict(type=t, idx=idx, name=name)
    if idx != -1:
        e['sha1'] = m.b[m.a:m.a + 20]; m.a += 20
        e['size'], e['flags'], e['handler'], e['user'] = m.u('q'), m.u('B'), m.u('i'), m.cstr()
    e['bundles'] = [m.u('I') for _ in range(m.u('i'))]
    if t == 2: e['rtype'], e['rid'] = m.u('I'), m.u('Q'); n = m.u('i'); e['meta'] = m.b[m.a:m.a + n]; m.a += n
    if t == 3:
        e['rs'], e['re'], e['lo'], e['ls'], e['h32'], e['mip'] = m.u('I'), m.u('I'), m.u('I'), m.u('I'), m.u('i'), m.u('i')
        e['sb'] = [m.u('I') for _ in range(m.u('i'))]
    entries.append(e)
assert m.a == data_offset, (m.a, data_offset)
table = [(m.u('q'), m.u('q')) for _ in range(data_count)]
payload_base = m.a
def decode(blob):
    out, a = b'', 0
    while a < len(blob):
        size = struct.unpack_from('>I', blob, a)[0]; typ = blob[a + 4]; packed = ((blob[a + 5] & 0x0F) << 16) | struct.unpack_from('>H', blob, a + 6)[0]; assert blob[a + 5] >> 4 == 7
        assert typ == 0, 'expected raw blocks without a game folder'
        out += blob[a + 8:a + 8 + packed]; a += 8 + packed
    return out
assert [e['name'] for e in entries[:5]] == ['Icon', 'Screenshot0', 'Screenshot1', 'Screenshot2', 'Screenshot3']
for e, r in zip(entries[5:], res):
    assert e['type'] == r['kind'] and e['name'] == r['name'] and e['user'] == r['user'], e
    assert e['flags'] == (8 if r['added'] else 0)
    assert e['bundles'] == [frosty_hash(b) for b in r['bundles']]
    off, size = table[e['idx']]
    blob = mod[payload_base + off: payload_base + off + size]
    assert hashlib.sha1(blob).digest() == e['sha1']
    assert decode(blob) == r['data'] and e['size'] == len(r['data'])
    if r['kind'] == 2: assert (e['rtype'], e['rid'], e['meta']) == (r['type'], r['rid'], r['meta'])
    if r['kind'] == 3:
        assert (e['rs'], e['re'], e['lo'], e['ls'], e['mip']) == (r['rs'], r['re'], r['lo'], len(r['data']), r['mip']), e
        assert e['sb'] == [frosty_hash(x) for x in r['sb']]
assert entries[7]['idx'] == entries[8]['idx'], 'identical payloads share one entry'
assert data_count == 3
print('fbmod checks passed: 4 resources, payloads decode, metadata, bundles, chunk fields, dedup')

# 3. error paths
code, j, _ = sp('project', 'export', native(os.path.join(T, 'Test Project.fbmod')))
assert code != 0 and j['error']['code'] == 'not_a_project', j
bad = os.path.join(T, 'bad.fbproject'); open(bad, 'wb').write(b'NOTAPROJ' + bytes(20))
code, j, _ = sp('project', 'export', native(bad))
assert code != 0 and j['error']['code'] == 'not_a_project' and 'RSPROJT1' in j['error']['message'], j
cut = os.path.join(T, 'cut.fbproject'); open(cut, 'wb').write(out[:len(out) // 2])
code, j, _ = sp('project', 'export', native(cut))
assert code != 0 and 'ends early' in j['error']['message'] or 'past the end' in j['error']['message'], j
code, j, _ = sp('project', 'export', native(proj), native(os.path.join(T, 'x.zip')))
assert code != 0 and j['error']['code'] == 'bad_output', j
print('error paths passed:', 'not_a_project (fbmod, bad magic, cut short), bad_output')
print('project export: all checks passed')
