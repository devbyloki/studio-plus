from __future__ import annotations

import argparse
from array import array
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
import functools
import hashlib
import importlib.util
import json
import math
from operator import itemgetter
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import threading
import traceback
import uuid
import zlib
from dataclasses import dataclass
from pathlib import Path

import bpy
from mathutils import Matrix, Vector
import numpy as np

_RUNTIME_ROOT = Path(__file__).resolve().parent
if str(_RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(_RUNTIME_ROOT))
# A missing or broken sibling file is reported as a coded failure by main(), not as a bare
# traceback with Blender's exit code 0.
_STARTUP_ERRORS = []
try:
    import studio_glbwrite as glbwrite
except Exception as _ex:  # noqa: BLE001
    glbwrite = None
    _STARTUP_ERRORS.append(("runtime_incomplete",
                            f"studio_glbwrite.py could not be loaded from {_RUNTIME_ROOT}: {_ex}"))


VERSION = "2.12.0"

# Exit codes. 0 and 1 are what reskate_cli has always seen for a finished conversion; usage
# errors used to share 2 with runtime failures.
EXIT_OK = 0
EXIT_COMPLETED_WITH_ERRORS = 1
EXIT_FAILED = 2
EXIT_USAGE = 3
EXIT_UNSUPPORTED_BLENDER = 4
EXIT_CODES = {
    EXIT_OK: "ok: the map folder (or the inspect report) was written",
    EXIT_COMPLETED_WITH_ERRORS: "completed_with_errors: written, but some objects or materials failed (see errors)",
    EXIT_FAILED: "failed: nothing was published (see error_code)",
    EXIT_USAGE: "usage: the arguments are wrong (see error_code usage_error)",
    EXIT_UNSUPPORTED_BLENDER: "this Blender version cannot run the converter",
}

# Every error_code a failed run can report.
ERROR_CODES = {
    "usage_error": "The arguments are wrong or missing",
    "blender_unsupported": "Blender is older than the converter supports",
    "runtime_incomplete": "A file the converter needs next to it is missing or broken",
    "input_not_found": "--input does not exist",
    "input_unsupported": "--input is not a .blend or .fbx file",
    "input_load_failed": "Blender could not open or import --input",
    "invalid_option": "A numeric option is out of range",
    "output_not_directory": "--output exists and is a file",
    "output_not_owned": "--output is not empty and was not made by this converter",
    "output_unsafe": "--output is a drive root, the home folder, a link, or holds the input",
    "output_write_failed": "The map folder could not be written",
    "internal_error": "An unexpected error; see traceback",
}

MIN_BLENDER = (3, 6, 0)
TESTED_BLENDER = ((4, 2, 0), (5, 3, 0))   # from, up to but not including


class MapImportError(Exception):
    """A failure the caller can act on: a stable snake_case code and a plain message."""

    def __init__(self, code, message, exit_code=EXIT_FAILED, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.details = details
IDENTITY = [1.0, 0.0, 0.0,
            0.0, 1.0, 0.0,
            0.0, 0.0, 1.0,
            0.0, 0.0, 0.0]
SUPPORTED_INPUTS = {".blend", ".fbx"}
PASSTHROUGH_IMAGES = {".png", ".dds"}
PLAYER_SIZE = [0.50, 1.85, 0.40]


RENDER_CELL_SIZE = 64.0
MAX_RENDER_LOCAL_REACH = RENDER_CELL_SIZE * 0.5


MAX_TRIANGLE_SPATIAL_CELLS = 4096


NATIVE_SECTION_MAX_VERTICES = (1 << 16) - 1


COLLISION_MODE_IDS = ("triangle_mesh", "convex_parts", "hull", "none", "water")
COLLISION_MODES = frozenset(COLLISION_MODE_IDS)


PLACEMENT_MODE_IDS = ("authored_mesh", "retail_blueprint")
PLACEMENT_MODES = frozenset(PLACEMENT_MODE_IDS)


NATIVE_COLLISION_MATERIAL_SLOT_COUNT = 116
NATIVE_COLLISION_MATERIAL_FLAG = 0x20
DEFAULT_COLLISION_MATERIAL_PACKED = 0x20
WATER_COLLISION_MATERIAL_PACKED = 0x00F007A0
MATERIAL_SURFACE_IDS = (
    "default", "concrete", "asphalt", "wood", "metal", "metal_rail",
    "metal_ledge", "concrete_ledge", "brick", "tile", "glass", "plastic",
    "grass", "dirt", "sand", "water", "carpet",
)
MATERIAL_SURFACES = frozenset(MATERIAL_SURFACE_IDS)


def _load_collision_surface_catalog() -> dict:
    path = Path(__file__).with_name("collision_surface_catalog.json")
    with path.open("r", encoding="utf-8") as stream:
        catalog = json.load(stream)
    if catalog.get("format") != 1:
        raise ValueError(f"unsupported collision surface catalog: {path}")
    source = catalog.get("source") or {}
    if source.get("distribution") != "Steam":
        raise ValueError("collision surface catalog is not sourced from Steam")
    rows = catalog.get("materialPropertyRows")
    sounds = catalog.get("materialSoundIds")
    network = catalog.get("networkIdToPackedMaterial")
    if (not isinstance(rows, list) or len(rows) != NATIVE_COLLISION_MATERIAL_SLOT_COUNT or
            not isinstance(sounds, list) or len(sounds) != len(rows) or
            not isinstance(network, list) or len(network) != 279):
        raise ValueError("collision surface catalog has invalid table dimensions")
    values = []
    for raw in network:
        if (isinstance(raw, bool) or not isinstance(raw, int) or raw < 0 or
                raw & 0x3f or ((raw >> 6) & 0x1fff) >= len(rows) or
                ((raw >> 19) & 0x1fff) >= len(rows)):
            raise ValueError("collision surface catalog has an invalid declaration")
        values.append(raw | NATIVE_COLLISION_MATERIAL_FLAG)
    if len(set(values)) != len(values) or DEFAULT_COLLISION_MATERIAL_PACKED not in values:
        raise ValueError("collision surface catalog declarations are not unique and complete")
    catalog["physicsPackedMaterials"] = tuple(values)
    return catalog


try:
    COLLISION_SURFACE_CATALOG = _load_collision_surface_catalog()
except Exception as _ex:  # noqa: BLE001
    _STARTUP_ERRORS.append(("runtime_incomplete",
                            f"collision_surface_catalog.json is missing or invalid: {_ex}"))
    COLLISION_SURFACE_CATALOG = {"physicsPackedMaterials": (DEFAULT_COLLISION_MATERIAL_PACKED,)}
NATIVE_COLLISION_MATERIAL_PACKED_VALUES = frozenset(
    COLLISION_SURFACE_CATALOG["physicsPackedMaterials"])

# Round rails: the game only builds grind edges on creases of about 30 degrees or more, so a
# smooth tube (16 sides and up) is not grindable on an ordinary surface. The shipped round
# rails carry the "IncludeInSurfaceAnalysis" property row (slot 71) on their tube, which
# makes the skater grind the surface itself; with it every side count works. This maps a
# material slot to the shipped declaration that adds the property to it.
SURFACE_ANALYSIS_PROPERTY_SLOT = 71
SMOOTH_GRIND_PACKED_BY_MATERIAL_SLOT = {
    (packed >> 6) & 0x1fff: packed
    for packed in COLLISION_SURFACE_CATALOG["physicsPackedMaterials"]
    if ((packed >> 19) & 0x1fff) == SURFACE_ANALYSIS_PROPERTY_SLOT
}
METAL_RAIL_MATERIAL_SLOT = 46


def _smooth_grind_variant(packed: int) -> int:
    """The declaration that grinds as a smooth surface for this material, or the metal rail's."""
    slot = (packed >> 6) & 0x1fff
    if ((packed >> 19) & 0x1fff) == SURFACE_ANALYSIS_PROPERTY_SLOT:
        return packed
    if slot == 0:   # an untouched default surface: a rail is metal
        slot = METAL_RAIL_MATERIAL_SLOT
    return SMOOTH_GRIND_PACKED_BY_MATERIAL_SLOT.get(
        slot, SMOOTH_GRIND_PACKED_BY_MATERIAL_SLOT[METAL_RAIL_MATERIAL_SLOT])


GLB_WRITE_WORKERS = max(1, min(8, os.cpu_count() or 1))


_last_progress = {"phase": None, "time": 0.0}


_OUTPUT_LOCK = threading.Lock()


def _emit_studio_progress(payload):
    # Studio's live output parser recognises this prefix; raw print is not a UI log.
    with _OUTPUT_LOCK:
        print("RESKATE_MAP_PROGRESS=" + json.dumps(payload, separators=(",", ":")), flush=True)


class ConversionPerformance:
    # Timings live in <output>\performance.json: inside the folder the run owns, never beside it.
    # The heartbeat also prints a "Still in <phase>" progress note, so a long Cycles bake or FBX
    # import is never silent for more than `interval` seconds.
    def __init__(self, path, interval=5.0):
        self.path = Path(path)
        self.interval = interval
        self.started = time.perf_counter()
        self.lock = threading.RLock()
        self.write_lock = threading.Lock()
        self.stop = threading.Event()
        self.stack = []
        self.totals = {}
        self.progress = {}
        self.status = 'running'
        self.thread = None
        self.warning_printed = False
        self.material_graphs = {}
        self.last_publish = -math.inf
        self.last_note = -math.inf

    def start(self):
        self.publish(force=True)
        self.thread = threading.Thread(target=self._heartbeat, name='reskate-performance', daemon=True)
        self.thread.start()

    @contextmanager
    def phase(self, name):
        frame = [name, time.perf_counter(), 0.0]
        with self.lock:
            self.stack.append(frame)
        try:
            yield
        finally:
            elapsed = time.perf_counter() - frame[1]
            with self.lock:
                self.stack.pop()
                row = self.totals.setdefault(name, {'calls': 0, 'inclusive_seconds': 0.0, 'self_seconds': 0.0})
                row['calls'] += 1
                row['inclusive_seconds'] += elapsed
                row['self_seconds'] += max(0.0, elapsed - frame[2])
                if self.stack:
                    self.stack[-1][2] += elapsed
            if elapsed >= 2.0:
                self.note(f'Finished {name} in {elapsed:.1f}s')

    def note(self, message, *, force=False):
        with self.lock:
            now = time.perf_counter()
            if not force and now - self.last_note < 1.0:
                return
            self.last_note = now
            payload = dict(self.progress)
        context = payload.get('message', '')
        payload.setdefault('progress', 0.0)
        payload.setdefault('phase', 'inspect')
        payload['message'] = message + ((' | ' + context) if context else '')
        _emit_studio_progress(payload)

    def update_progress(self, payload):
        with self.lock:
            changed = payload.get('phase') != self.progress.get('phase')
            self.progress = dict(payload)
        if changed:
            self.publish()

    def snapshot(self):
        now = time.perf_counter()
        with self.lock:
            return {'patch': 'map-import-' + VERSION, 'status': self.status,
                    'elapsed_seconds': round(now - self.started, 3),
                    'progress': dict(self.progress),
                    'material_graphs': dict(self.material_graphs),
                    'active': [{'phase': f[0], 'elapsed_seconds': round(now-f[1], 3)} for f in self.stack],
                    'completed_calls': {name: {key: round(value, 6) if isinstance(value, float) else value
                                               for key, value in row.items()} for name, row in self.totals.items()},
                    'timing_note': 'Inclusive timings nest; do not sum them. Active calls are not yet in completed_calls.'}

    def publish(self, *, force=False):
        # One writer, atomic replacement; diagnosis must never abort conversion.
        with self.write_lock:
            now = time.perf_counter()
            if not force and now - self.last_publish < 5.0:
                return None
            self.last_publish = now
            snapshot = self.snapshot()
            temporary = self.path.with_name(self.path.name + '.tmp')
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary.write_text(json.dumps(snapshot, indent=2) + '\n', encoding='utf-8')
                os.replace(temporary, self.path)
            except OSError as error:
                if not self.warning_printed:
                    print('[ReSkate performance] Could not write timing file: ' + str(error), flush=True)
                    self.warning_printed = True
            return snapshot

    def _heartbeat(self):
        while not self.stop.wait(self.interval):
            snapshot = self.publish(force=True)
            active = snapshot['active'][-1] if snapshot['active'] else snapshot['progress']
            phase = active.get('phase', 'between phases')
            duration = active.get('elapsed_seconds', 0.0)
            self.note(f'Still in {phase}: {duration:.1f}s; total {snapshot["elapsed_seconds"]:.1f}s', force=True)

    def finish(self, status):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=2.0)
        with self.lock:
            self.status = status
        self.publish(force=True)


_PERF = None


def _perf_phase(name):
    return _PERF.phase(name) if _PERF is not None else nullcontext()


def _timed(name, function):
    @functools.wraps(function)
    def call(*args, **kwargs):
        with _perf_phase(name):
            return function(*args, **kwargs)
    return call


def _profile_conversion(function):
    @functools.wraps(function)
    def call(options, stage, destination):
        global _PERF
        performance = ConversionPerformance(destination / PERFORMANCE_FILE)
        _PERF = performance
        _last_progress.update(phase=None, time=0.0)
        performance.start()
        performance.note('Conversion timings: ' + str(performance.path), force=True)
        status = 'failed'
        try:
            result = function(options, stage, destination)
            status = result.get('status', 'ok')
            return result
        finally:
            performance.finish(status)
            _PERF = None
    return call


def _progress_note(message: str):
    # A message for the step in progress, without moving the bar: before a long call that
    # prints nothing itself (opening a big .blend, FBX import, a Cycles bake).
    if _PERF is not None:
        _PERF.note(message, force=True)
        return
    payload = dict(_last_progress.get("payload") or {"progress": 0.0, "phase": "inspect"})
    payload["message"] = message
    _emit_studio_progress(payload)


def _progress(progress: float, phase: str, message: str, **extra):
    # Per-object updates are throttled; phase changes and the end of a phase always print.
    now = time.perf_counter()
    current, total = extra.get("current"), extra.get("total")
    final = current is not None and total is not None and current >= total
    if (phase == _last_progress["phase"] and not final and
            now - _last_progress["time"] < 0.1):
        return
    _last_progress.update(phase=phase, time=now)
    payload = {"progress": round(min(1.0, max(0.0, float(progress))), 4),
               "phase": phase, "message": message}
    payload.update(extra)
    _last_progress["payload"] = payload
    if _PERF is not None:
        _PERF.update_progress(payload)
    _emit_studio_progress(payload)


def _safe_name(value: str, fallback: str = "item", limit: int = 80) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", (value or "").strip())
    value = value.strip("._-")
    return (value or fallback)[:limit]


def _map_id(value: str) -> str:
    value = re.sub(r"[^a-z0-9_]+", "_", (value or "").strip().lower())
    value = re.sub(r"_+", "_", value).strip("_") or "custom_map"
    if value[0].isdigit():
        value = "map_" + value
    return value[:64]


def _round_list(values, digits=6):
    return [round(float(v), digits) for v in values]


def _blender_to_game(v):

    return (float(v[0]), float(v[2]), -float(v[1]))


def _game_transform_rows(matrix: Matrix, scale: float) -> list[float]:

    bx = _blender_to_game((matrix[0][0], matrix[1][0], matrix[2][0]))
    by = _blender_to_game((matrix[0][1], matrix[1][1], matrix[2][1]))
    bz = _blender_to_game((matrix[0][2], matrix[1][2], matrix[2][2]))
    translation = _blender_to_game(matrix.translation)
    right, up, forward = bx, bz, (-by[0], -by[1], -by[2])
    return [
        right[0], right[1], right[2],
        up[0], up[1], up[2],
        forward[0], forward[1], forward[2],
        translation[0] * scale, translation[1] * scale,
        translation[2] * scale,
    ]


class Diagnostics:
    def __init__(self):
        self.warnings: list[dict] = []
        self.errors: list[dict] = []

    @staticmethod
    def _entry(code, message, context):
        out = {"code": str(code), "message": str(message)}
        out.update({k: v for k, v in context.items() if v is not None})
        return out

    def warn(self, code, message, **context):
        self.warnings.append(self._entry(code, message, context))

    def error(self, code, message, **context):
        self.errors.append(self._entry(code, message, context))


class NameAllocator:
    def __init__(self):
        self._used: set[str] = set()

    def get(self, wanted: str, fallback="item") -> str:
        stem = _safe_name(wanted, fallback)
        candidate = stem
        suffix = 2
        while candidate.casefold() in self._used:
            tail = f"_{suffix}"
            candidate = stem[:max(1, 80 - len(tail))] + tail
            suffix += 1
        self._used.add(candidate.casefold())
        return candidate


class GlbWriteQueue:


    def __init__(self, workers=GLB_WRITE_WORKERS):
        self.workers = max(1, int(workers))
        self.limit = max(2, self.workers * 2)
        self.executor = ThreadPoolExecutor(
            max_workers=self.workers, thread_name_prefix="reskate-glb")
        self.pending = deque()
        self.submitted = 0

    @staticmethod
    def _vectors(values):
        # Copies the stream so later edits by the caller cannot race the writer thread.
        try:
            frozen = np.array(values, dtype=np.float64)
            if frozen.ndim == 2:
                return frozen
        except (TypeError, ValueError):
            pass
        return tuple(tuple(float(component) for component in row)
                     for row in values)

    def submit(self, destination, positions, *, normals=None, uvs=None,
               indices=None, name="mesh", success=None, failure=None):
        frozen_positions = self._vectors(positions)
        frozen_normals = None if normals is None else self._vectors(normals)
        frozen_uvs = None if uvs is None else self._vectors(uvs)
        frozen_indices = None if indices is None else np.array(indices, dtype=np.int64)
        future = self.executor.submit(
            glbwrite.write_blender_mesh, destination, frozen_positions,
            normals=frozen_normals, uvs=frozen_uvs, indices=frozen_indices,
            name=str(name))
        self.pending.append((future, str(destination), success, failure))
        self.submitted += 1
        if len(self.pending) >= self.limit:
            self._complete_one()

    def _complete_one(self):
        future, destination, success, failure = self.pending.popleft()
        try:
            future.result()
        except Exception as error:
            wrapped = RuntimeError(
                f"GLB serialization failed for '{destination}': {error}")
            if failure is None:
                raise wrapped from error
            failure(wrapped)
        else:
            if success is not None:
                success()

    def drain(self):
        while self.pending:
            self._complete_one()

    def close(self):
        try:
            self.drain()
        finally:
            self.executor.shutdown(wait=True)

    def abort(self):
        for future, _destination, _success, _failure in self.pending:
            future.cancel()
        self.pending.clear()
        try:
            self.executor.shutdown(wait=True, cancel_futures=True)
        except TypeError:
            self.executor.shutdown(wait=True)

    def __enter__(self):
        return self

    def __exit__(self, exception_type, _exception, _traceback):
        if exception_type is None:
            self.close()
        else:
            self.abort()
        return False


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (struct.pack(">I", len(payload)) + kind + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))


def _rgba_png(width: int, height: int, rgba: bytes | bytearray) -> bytes:

    width, height = int(width), int(height)
    if width <= 0 or height <= 0 or len(rgba) != width * height * 4:
        raise ValueError("RGBA payload does not match its PNG dimensions")
    stride = width * 4
    rows = np.frombuffer(bytes(rgba), dtype=np.uint8).reshape(height, stride)
    raw = np.concatenate((np.zeros((height, 1), dtype=np.uint8), rows), axis=1).tobytes()
    # Materials often regenerate identical images; level-9 deflate is the expensive part.
    key = (width, height, hashlib.blake2b(raw, digest_size=20).digest())
    cached = _PNG_CACHE.get(key)
    if cached is not None:
        return cached
    png = (b"\x89PNG\r\n\x1a\n"
           + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height,
                                               8, 6, 0, 0, 0))
           + _png_chunk(b"IDAT", zlib.compress(raw, 9))
           + _png_chunk(b"IEND", b""))
    _PNG_CACHE[key] = png
    return png


_PNG_CACHE: dict[tuple, bytes] = {}


def _unit_bytes(values) -> np.ndarray:
    """Vectorised TextureStore.opacity component(): clamp to 0..1, non-finite as 1, round half-even."""
    values = np.asarray(values, dtype=np.float32).astype(np.float64)
    values = np.where(np.isfinite(values), values, 1.0)
    return np.rint(np.clip(values, 0.0, 1.0) * 255.0).astype(np.uint8)


def _solid_png(rgba, size=4, source_is_linear=False) -> bytes:

    def srgb(v):
        v = min(1.0, max(0.0, float(v)))
        if not source_is_linear:
            return v
        return 12.92 * v if v <= 0.0031308 else 1.055 * v ** (1.0 / 2.4) - 0.055

    px = bytes((round(srgb(rgba[0]) * 255),
                round(srgb(rgba[1]) * 255),
                round(srgb(rgba[2]) * 255),
                round(min(1.0, max(0.0, float(rgba[3]))) * 255)))
    return _rgba_png(size, size, px * (size * size))


def _source_file(image) -> Path | None:
    try:
        value = image.filepath_from_user()
        path = Path(bpy.path.abspath(value)) if value else None
        if path and path.is_file():
            return path.resolve()
    except Exception:
        pass
    for value in (getattr(image, "filepath_raw", ""), getattr(image, "filepath", "")):
        try:
            path = Path(bpy.path.abspath(value)) if value else None
            if path and path.is_file():
                return path.resolve()
        except Exception:
            continue
    return None


class TextureStore:


    def __init__(self, root: Path, diagnostics: Diagnostics, stats: dict):
        self.root = root
        self.diag = diagnostics
        self.stats = stats
        self.by_digest: dict[str, str] = {}
        self.image_cache: dict[int, str] = {}
        self.alpha_cache: dict[int, str] = {}
        self.opacity_cache: dict[tuple, tuple[str, dict]] = {}
        self.tinted_cache: dict[tuple, str] = {}
        self.dependencies: list[dict] = []
        self._dependency_keys: set[tuple] = set()
        root.mkdir(parents=True, exist_ok=True)

    def alpha_kind(self, image, material=None) -> str:


        if image is None:
            return "opaque"
        key = int(image.as_pointer())
        cached = self.alpha_cache.get(key)
        if cached is not None:
            self.stats["alpha_classification_cache_hits"] = (
                self.stats.get("alpha_classification_cache_hits", 0) + 1)
            return cached

        self.stats["alpha_images_scanned"] = (
            self.stats.get("alpha_images_scanned", 0) + 1)
        try:
            channels = int(getattr(image, "channels", 0))
            width, height = (int(value) for value in image.size[:2])
            alpha_channel = 3 if channels >= 4 else (1 if channels == 2 else None)
            if alpha_channel is None or width <= 0 or height <= 0:
                kind = "opaque"
            else:
                values = np.empty(width * height * channels, dtype=np.float32)
                image.pixels.foreach_get(values)


                epsilon = 0.5 / 255.0
                alpha = values[alpha_channel::channels].astype(np.float64)
                if np.any((alpha > epsilon) & (alpha < 1.0 - epsilon)):
                    kind = "blend"
                elif np.any(alpha <= epsilon):
                    kind = "mask"
                else:
                    kind = "opaque"
        except Exception as ex:


            kind = "blend"
            self.stats["alpha_classification_failures"] = (
                self.stats.get("alpha_classification_failures", 0) + 1)
            self.diag.warn(
                "alpha_classification_failed", str(ex), material=material,
                image=getattr(image, "name", None))
        self.alpha_cache[key] = kind
        return kind

    def add_bytes(self, data: bytes, stem: str, ext: str = ".png",
                  source: str | None = None, role: str | None = None) -> str:
        digest = hashlib.sha256(data).hexdigest()
        self.stats["texture_references"] += 1
        if digest in self.by_digest:
            self.stats["textures_deduplicated"] += 1
            rel = self.by_digest[digest]
        else:
            ext = ext.lower() if ext.lower() in PASSTHROUGH_IMAGES else ".png"
            name = f"{_safe_name(stem, 'texture', 58)}-{digest[:12]}{ext}"
            path = self.root / name
            path.write_bytes(data)
            rel = "textures/" + name
            self.by_digest[digest] = rel
            self.stats["textures_written"] += 1
        if source:
            dep = {"source": source, "output": rel}
            if role:
                dep["role"] = role
            dependency_key = tuple(sorted(dep.items()))
            if dependency_key not in self._dependency_keys:
                self._dependency_keys.add(dependency_key)
                self.dependencies.append(dep)
        return rel

    def solid(self, rgba, stem, role, source_is_linear=False) -> str:
        return self.add_bytes(_solid_png(rgba, source_is_linear=source_is_linear),
                              stem, ".png", source=f"generated:{role}", role=role)

    def opacity(self, alpha_image, stem, material=None, *, fallback_alpha=1.0,
                surface_mask_image=None, surface_mask_constant=None,
                channel="Alpha") -> tuple[str, dict]:


        def pointer(value):
            try:
                return int(value.as_pointer())
            except Exception:
                return 0

        constant = tuple(surface_mask_constant or ())
        cache_key = (pointer(alpha_image), pointer(surface_mask_image), constant,
                     round(float(fallback_alpha), 8), channel)
        cached = self.opacity_cache.get(cache_key)
        if cached is not None:
            self.stats["texture_references"] += 1
            self.stats["textures_deduplicated"] += 1
            return cached[0], dict(cached[1])

        def component(value, fallback=1.0):
            try:
                value = float(value)
            except Exception:
                value = fallback
            if not math.isfinite(value):
                value = fallback
            return round(min(1.0, max(0.0, value)) * 255.0)

        def pixels(image):
            channels = int(getattr(image, "channels", 0))
            width, height = (int(value) for value in image.size[:2])
            if channels <= 0 or width <= 0 or height <= 0:
                raise ValueError("image has no readable pixels")
            values = np.empty(width * height * channels, dtype=np.float32)
            image.pixels.foreach_get(values)
            return width, height, channels, values

        alpha_from_image = False
        try:
            if alpha_image is None:
                raise ValueError("Principled Alpha has no direct image source")
            width, height, channels, alpha_values = pixels(alpha_image)
            if channel == "Alpha":
                alpha_channel = 3 if channels >= 4 else (1 if channels == 2 else None)
            else:
                alpha_channel = {"Red": 0, "Green": 1, "Blue": 2}.get(channel)
                if alpha_channel is not None and alpha_channel >= channels:
                    alpha_channel = None
            if alpha_channel is None:
                raise ValueError(f"alpha source image has no {channel} channel")
            red = _unit_bytes(alpha_values[alpha_channel::channels])
            alpha_from_image = True
        except Exception as ex:
            width = height = 4
            red = np.full(width * height, component(fallback_alpha), dtype=np.uint8)
            self.diag.warn(
                "mask_opacity_source_fallback",
                f"Could not read authored mask coverage; using the Principled Alpha factor: {ex}",
                material=material,
                image=getattr(alpha_image, "name", None))

        green = blue = None
        channels_source = "neutral"
        if surface_mask_image is not None:
            try:
                mw, mh, mc, mask_values = pixels(surface_mask_image)
                if (mw, mh) == (width, height) and mc >= 3:
                    green = _unit_bytes(mask_values[1::mc])
                    blue = _unit_bytes(mask_values[2::mc])
                    channels_source = "surface_mask_image"
            except Exception as ex:
                self.diag.warn(
                    "opacity_surface_mask_fallback",
                    f"Could not reuse surface-mask G/B channels: {ex}",
                    material=material,
                    image=getattr(surface_mask_image, "name", None))
        elif len(constant) == 4:
            mw, mh, mask_green, mask_blue = constant
            if (int(mw), int(mh)) == (width, height):
                green = np.full(width * height, component(mask_green), dtype=np.uint8)
                blue = np.full(width * height, component(mask_blue), dtype=np.uint8)
                channels_source = "generated_surface_mask"

        if green is None or blue is None:
            green = np.full(width * height, 128, dtype=np.uint8)
            blue = np.full(width * height, 255, dtype=np.uint8)
            channels_source = "neutral"

        rgba = np.stack((red, green, blue, np.full(width * height, 255, dtype=np.uint8)),
                        axis=1)
        # Blender stores rows bottom-up; PNG wants them top-down.
        png_rgba = rgba.reshape(height, width * 4)[::-1].tobytes()
        source = (f"blender:{getattr(alpha_image, 'name', 'alpha')}:{channel.casefold()}"
                  if alpha_from_image else "generated:opacity_fallback")
        rel = self.add_bytes(_rgba_png(width, height, png_rgba), stem, ".png",
                             source=source, role="opacity")
        info = {
            "alpha_from_image": alpha_from_image,
            "surface_channels": channels_source,
            "size": [width, height],
        }
        self.opacity_cache[cache_key] = (rel, info)
        return rel, dict(info)

    def tinted(self, image, tint, stem, material=None) -> str | None:
        """The image multiplied by a constant linear colour, as a Multiply Mix node computes
        it (colour-managed images are decoded first); written once per image and tint.
        Alpha passes through unchanged."""
        if all(abs(value - 1.0) < 1e-6 for value in tint):
            return self.image(image, stem, "base_color", material)
        key = (int(image.as_pointer()), tuple(round(float(value), 6) for value in tint))
        cached = self.tinted_cache.get(key)
        if cached is not None:
            self.stats["texture_references"] += 1
            self.stats["textures_deduplicated"] += 1
            return cached
        try:
            width, height = (int(value) for value in image.size[:2])
            channels = int(getattr(image, "channels", 0))
            if width <= 0 or height <= 0 or channels <= 0:
                raise RuntimeError("image has no pixels")
            pixels = np.empty(width * height * channels, dtype=np.float32)
            image.pixels.foreach_get(pixels)
            pixels = pixels.reshape(height, width, channels).astype(np.float64)
            rgb = pixels[..., :3] if channels >= 3 else np.repeat(pixels[..., :1], 3, axis=2)
            alpha = pixels[..., 3] if channels >= 4 else np.ones((height, width))
            colourspace = str(getattr(image.colorspace_settings, "name", "")).casefold()
            if not getattr(image, "is_float", False) and colourspace == "srgb":
                rgb = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
            rgb = np.clip(rgb * np.asarray(tint, dtype=np.float64), 0.0, 1.0)
            rgb = np.where(rgb <= 0.0031308, rgb * 12.92, 1.055 * rgb ** (1.0 / 2.4) - 0.055)
            # Blender stores rows bottom-up; PNG rows run top-down.
            rgba = np.concatenate((rgb, alpha[..., None]), axis=2)[::-1]
            data = _rgba_png(width, height, _unit_bytes(rgba).tobytes())
            source = f"tinted:{image.name}:" + ",".join(f"{value:.4f}" for value in tint)
            rel = self.add_bytes(data, stem, ".png", source, "base_color")
            self.tinted_cache[key] = rel
            self.stats["tinted_base_textures"] = self.stats.get("tinted_base_textures", 0) + 1
            return rel
        except Exception as ex:
            self.diag.warn("tinted_image_failed", str(ex), material=material,
                           image=getattr(image, "name", None))
            return None

    def image(self, image, stem, role, material=None) -> str | None:
        self.stats["texture_references"] += 0
        if image is None:
            return None
        key = int(image.as_pointer())
        if key in self.image_cache:

            self.stats["texture_references"] += 1
            self.stats["textures_deduplicated"] += 1
            return self.image_cache[key]

        source_kind = str(getattr(image, "source", ""))
        if source_kind in {"TILED", "SEQUENCE", "MOVIE"}:
            self.diag.warn(
                "unsupported_image_source",
                f"Image '{image.name}' uses {source_kind}; only its current frame/tile can be exported",
                material=material, image=image.name, role=role)

        source = _source_file(image)
        if source and source.suffix.lower() in PASSTHROUGH_IMAGES:
            try:
                rel = self.add_bytes(source.read_bytes(), stem,
                                     source.suffix.lower(), str(source), role)
                self.image_cache[key] = rel
                return rel
            except Exception as ex:
                self.diag.warn("image_copy_failed", str(ex), material=material,
                               image=image.name, source=str(source), role=role)

        packed = getattr(image, "packed_file", None)
        if packed is not None:
            try:
                data = bytes(packed.data)
                head = data[:8]
                ext = ".dds" if head[:4] == b"DDS " else (
                    ".png" if head == b"\x89PNG\r\n\x1a\n" else None)
                if ext:
                    rel = self.add_bytes(data, stem, ext,
                                         f"packed:{image.name}", role)
                    self.image_cache[key] = rel
                    return rel
            except Exception as ex:
                self.diag.warn("packed_image_read_failed", str(ex),
                               material=material, image=image.name, role=role)


        temp_path = None
        copy = None
        try:
            fd, raw_path = tempfile.mkstemp(prefix="reskate-image-", suffix=".png")
            os.close(fd)
            temp_path = Path(raw_path)
            temp_path.unlink(missing_ok=True)
            copy = image.copy()
            copy.file_format = "PNG"
            copy.filepath_raw = str(temp_path)
            try:
                copy.alpha_mode = "STRAIGHT"
            except Exception:
                pass
            try:
                copy.save(filepath=str(temp_path))
            except TypeError:
                copy.save()
            if not temp_path.is_file():
                raise RuntimeError("Blender produced no PNG")
            rel = self.add_bytes(temp_path.read_bytes(), stem, ".png",
                                 str(source) if source else f"blender:{image.name}", role)
            self.image_cache[key] = rel
            return rel
        except Exception as ex:
            self.diag.warn("image_conversion_failed", str(ex), material=material,
                           image=image.name, source=str(source) if source else None,
                           role=role)
            return None
        finally:
            if copy is not None:
                try:
                    bpy.data.images.remove(copy)
                except Exception:
                    pass
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)


def _uses_nodes(material) -> bool:
    # Blender 5 materials always use nodes; Material.use_nodes is deprecated there and goes in 6.0.
    if material is None:
        return False
    if bpy.app.version >= (5, 0, 0):
        return material.node_tree is not None
    return bool(getattr(material, "use_nodes", False))


def _enable_nodes(material):
    if bpy.app.version < (5, 0, 0) or material.node_tree is None:
        material.use_nodes = True


def _active_principled(material):
    if not _uses_nodes(material):
        return None
    nodes = material.node_tree.nodes
    outputs = sorted((n for n in nodes if n.type == "OUTPUT_MATERIAL"),
                     key=lambda n: (not getattr(n, "is_active_output", False), n.name))
    seen = set()
    stack = []
    for output in outputs:
        surface = output.inputs.get("Surface")
        if surface and surface.is_linked:
            stack.extend(link.from_node for link in surface.links)
    while stack:
        node = stack.pop(0)
        if int(node.as_pointer()) in seen:
            continue
        seen.add(int(node.as_pointer()))
        if node.type == "BSDF_PRINCIPLED":
            return node
        for socket in node.inputs:
            stack.extend(link.from_node for link in socket.links)
    return next(iter(sorted((n for n in nodes if n.type == "BSDF_PRINCIPLED"),
                            key=lambda n: n.name)), None)


def _default(socket, fallback):
    if socket is None:
        return fallback
    try:
        value = socket.default_value
        if isinstance(value, (int, float)):
            return float(value)
        return tuple(float(x) for x in value)
    except Exception:
        return fallback


def _upstream(socket):
    if socket is None or not socket.is_linked:
        return None, None
    link = sorted(socket.links, key=lambda x: (x.from_node.name, x.from_socket.name))[0]
    return link.from_node, link.from_socket


def _unwrap_reroutes(node, socket):
    seen = set()
    while node is not None and node.type == "REROUTE":
        key = int(node.as_pointer())
        if key in seen:
            break
        seen.add(key)
        node, socket = _upstream(node.inputs[0])
    return node, socket


def _mapping_is_identity(node) -> bool:
    try:
        loc = tuple(node.inputs["Location"].default_value)
        rot = tuple(node.inputs["Rotation"].default_value)
        scale = tuple(node.inputs["Scale"].default_value)
        return (max(abs(v) for v in loc) < 1e-7
                and max(abs(v) for v in rot) < 1e-7
                and max(abs(v - 1.0) for v in scale) < 1e-7)
    except Exception:
        return False


def _simple_uv(image_node) -> tuple[bool, str | None]:
    vector = image_node.inputs.get("Vector")
    if vector is None or not vector.is_linked:
        return True, None
    node, socket = _upstream(vector)
    node, socket = _unwrap_reroutes(node, socket)
    while node is not None and node.type == "MAPPING" and _mapping_is_identity(node):
        node, socket = _upstream(node.inputs.get("Vector"))
        node, socket = _unwrap_reroutes(node, socket)
    if node is None:
        return True, None
    if node.type == "UVMAP":
        return True, None
    if node.type == "TEX_COORD" and socket is not None and socket.name == "UV":
        return True, None
    return False, f"image vectors come from {node.type}:{getattr(socket, 'name', '')}"


def _simple_base_image(socket):

    node, output = _upstream(socket)
    node, output = _unwrap_reroutes(node, output)
    if node is None:
        return None, None, "unlinked"
    if node.type != "TEX_IMAGE" or getattr(node, "image", None) is None:
        return None, None, f"base colour is computed by {node.type}"
    if output is not None and output.name != "Color":
        return None, None, f"base colour uses image output {output.name}"
    simple, reason = _simple_uv(node)
    return (node.image, node, None) if simple else (None, node, reason)


def _trace_image(socket):

    node, output = _upstream(socket)
    channel = getattr(output, "name", None)
    chain = []
    seen = set()
    while node is not None and int(node.as_pointer()) not in seen:
        seen.add(int(node.as_pointer()))
        chain.append(node.type)
        if node.type == "TEX_IMAGE":
            image = getattr(node, "image", None)
            if image is not None:
                return image, channel or "Color", chain


            return None, None, []
        if node.type == "REROUTE":
            node, output = _upstream(node.inputs[0])
        elif node.type == "NORMAL_MAP":
            colour_input = node.inputs.get("Color")
            if colour_input is None or not colour_input.is_linked:


                return None, None, []
            node, output = _upstream(colour_input)
            channel = "Color"
        elif node.type in {"SEPRGB", "SEPARATE_COLOR", "SEPXYZ"}:

            channel = getattr(output, "name", channel)
            node, output = _upstream(node.inputs[0])
        else:
            linked = [s for s in node.inputs if s.is_linked]
            if len(linked) != 1:
                break
            node, output = _upstream(linked[0])
    return None, channel, chain


def _direct_source(socket, wanted_node, wanted_output: str) -> bool:
    node, output = _upstream(socket)
    node, output = _unwrap_reroutes(node, output)


    return (node is not None and wanted_node is not None and
            int(node.as_pointer()) == int(wanted_node.as_pointer()) and
            output is not None and
            output.name == wanted_output)


def _blank_generated_image(image) -> bool:

    if (image is None or str(getattr(image, "source", "")) != "GENERATED" or
            getattr(image, "packed_file", None) is not None or
            bool(getattr(image, "is_dirty", False)) or
            str(getattr(image, "generated_type", "")) != "BLANK"):
        return False
    try:
        colour = tuple(float(value) for value in image.generated_color)
        return len(colour) >= 3 and max(abs(value) for value in colour[:3]) < 1e-7
    except Exception:
        return False


def _br_imported_shader_info(material, principled=None) -> dict | None:


    if not _uses_nodes(material):
        return None
    nodes = material.node_tree.nodes

    def named(name, node_type):
        node = nodes.get(name)
        return node if node is not None and node.type == node_type else None

    bsdf = named("BR_BSDF", "BSDF_PRINCIPLED")
    diffuse = named("BR_DiffuseTex", "TEX_IMAGE")
    diffuse_uv = named("BR_DiffuseUV", "UVMAP")
    normal = named("BR_NormalTex", "TEX_IMAGE")
    normal_uv = named("BR_NormalUV", "UVMAP")
    normal_map = named("BR_NormalMap", "NORMAL_MAP")
    specular = named("BR_SpecularTex", "TEX_IMAGE")
    specular_uv = named("BR_SpecularUV", "UVMAP")
    specular_extract = named("Separate Color", "SEPARATE_COLOR")
    lightmap = named("BR_LightmapTex", "TEX_IMAGE")
    lightmap_uv = named("BR_LightmapUV", "UVMAP")
    emission = named("BR_Emission", "EMISSION")
    mix = named("BR_MixShader", "MIX_SHADER")
    output = named("Material Output", "OUTPUT_MATERIAL")
    required = (bsdf, diffuse, diffuse_uv, normal, normal_uv, normal_map,
                specular, specular_uv, specular_extract, lightmap, lightmap_uv,
                emission, mix, output)
    if (any(node is None for node in required) or
            (principled is not None and
             int(principled.as_pointer()) != int(bsdf.as_pointer())) or
            not _direct_source(bsdf.inputs.get("Base Color"), diffuse, "Color") or
            not _direct_source(diffuse.inputs.get("Vector"), diffuse_uv, "UV") or
            not _direct_source(normal_map.inputs.get("Color"), normal, "Color") or
            not _direct_source(bsdf.inputs.get("Normal"), normal_map, "Normal") or
            not _direct_source(normal.inputs.get("Vector"), normal_uv, "UV") or
            not _direct_source(specular_extract.inputs.get("Color"), specular,
                               "Color") or
            not _direct_source(bsdf.inputs.get("Specular IOR Level"),
                               specular_extract, "Red") or
            not _direct_source(specular.inputs.get("Vector"), specular_uv, "UV") or
            not _direct_source(lightmap.inputs.get("Vector"), lightmap_uv, "UV") or
            not _direct_source(emission.inputs.get("Color"), lightmap, "Color") or
            not _direct_source(mix.inputs[1], bsdf, "BSDF") or
            not _direct_source(mix.inputs[2], emission, "Emission") or
            not _direct_source(output.inputs.get("Surface"), mix, "Shader")):
        return None

    alpha_socket = bsdf.inputs.get("Alpha")
    alpha_from_diffuse = _direct_source(alpha_socket, diffuse, "Alpha")
    lightmap_image = getattr(lightmap, "image", None)
    if lightmap_image is None:
        lightmap_status = "empty_texture_slot"
    elif _blank_generated_image(lightmap_image):
        lightmap_status = "blank_generated_placeholder"
    else:
        lightmap_status = "source_image_not_compiled"
    return {
        "family": "br_imported_principled_lightmap_v1",
        "diffuse_image": (getattr(getattr(diffuse, "image", None), "name", None)),
        "diffuse_uv": str(getattr(diffuse_uv, "uv_map", "")),
        "alpha_source": ("diffuse_texture_alpha" if alpha_from_diffuse
                         else "principled_factor"),
        "normal_source": ("empty_texture_slot" if getattr(normal, "image", None) is None
                          else "source_image"),
        "normal_uv": str(getattr(normal_uv, "uv_map", "")),
        "specular_source": ("empty_texture_slot" if getattr(specular, "image", None) is None
                            else "source_image"),
        "specular_uv": str(getattr(specular_uv, "uv_map", "")),
        "lightmap_source": lightmap_status,
        "lightmap_image": getattr(lightmap_image, "name", None),
        "lightmap_uv": str(getattr(lightmap_uv, "uv_map", "")),
    }


def _surface_for(material) -> str:
    def normalise(value):
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, int):
            return (MATERIAL_SURFACE_IDS[value]
                    if 0 <= value < len(MATERIAL_SURFACE_IDS) else None)
        value = str(value).strip().lower()
        return value if value in MATERIAL_SURFACES else None

    if material is not None:
        try:
            nested_raw = material.get("sk8_material")
        except Exception:
            nested_raw = None
        if nested_raw is not None:
            try:
                value = normalise(nested_raw.get("surface"))
            except Exception:
                value = None
            if value is not None:
                return value

        try:
            nested_rna = getattr(material, "sk8_material", None)
            explicitly_set = (nested_rna is not None and
                              (not hasattr(nested_rna, "is_property_set") or
                               nested_rna.is_property_set("surface")))
            value = normalise(getattr(nested_rna, "surface", None)) \
                if explicitly_set else None
        except Exception:
            value = None
        if value is not None:
            return value

        for key in ("sk8_surface", "surface", "physics_surface"):
            try:
                value = normalise(material.get(key))
            except Exception:
                value = None
            if value is not None:
                return value
    name = (material.name if material else "").lower()
    for token, surface in (("rail", "metal_rail"), ("ledge", "concrete_ledge"),
                           ("concrete", "concrete"), ("asphalt", "asphalt"),
                           ("brick", "brick"), ("wood", "wood"),
                           ("metal", "metal"), ("glass", "glass"),
                           ("grass", "grass"), ("dirt", "dirt"),
                           ("sand", "sand"), ("tile", "tile"),
                           ("plastic", "plastic")):
        if token in name:
            return surface
    return "default"


def _explicit_native_mask_image(material):
    explicit = _profile_raw_value(material, "mask_image", None)
    if isinstance(explicit, bpy.types.Image):
        return explicit
    if not _uses_nodes(material):
        return None
    found = []
    for node in material.node_tree.nodes:
        image = getattr(node, "image", None) if node.type == "TEX_IMAGE" else None
        if image is None:
            continue
        source = _source_file(image)
        haystack = " ".join((node.name, getattr(node, "label", ""), image.name,
                             source.stem if source else "")).lower()
        if re.search(r"(?:^|[_\-\s])msk(?:$|[_\-\s.])", haystack):
            found.append((node.name, image))
    return sorted(found, key=lambda item: item[0])[0][1] if found else None


def _multiply_add_tint(socket):
    """Recognise only image * constant + exact zero, as seen in the supplied USD graphs."""
    node, output = _unwrap_reroutes(*_upstream(socket))
    if (node is None or node.type != "VECT_MATH" or
            getattr(node, "operation", "") != "MULTIPLY_ADD" or
            output is None or output.name != "Vector" or len(node.inputs) < 3):
        return None, None, None
    left, right, add = node.inputs[:3]
    if add.is_linked or bool(left.is_linked) == bool(right.is_linked):
        return None, None, None
    constant = right if left.is_linked else left
    source = left if left.is_linked else right
    try:
        offset = tuple(float(add.default_value[i]) for i in range(3))
        tint = tuple(float(constant.default_value[i]) for i in range(3))
    except (AttributeError, TypeError, ValueError, IndexError):
        return None, None, None
    # No epsilon: even a small intentional offset must keep the original bake path.
    # Both darkening and brightening constants use the existing tint exporter.
    # That exporter clamps RGB to the PNG range; it retains the source alpha.
    if any(value != 0.0 for value in offset) or not all(
            math.isfinite(value) and value >= 0.0 for value in tint):
        return None, None, None
    image, image_node, _reason = _simple_base_image(source)
    if image is None:
        return None, None, None
    return image, image_node, tint


def _tinted_base_image(socket):
    """A plain image multiplied by a constant RGB tint.

    Supports the existing RGBA Mix/Multiply and Vector Math/Multiply paths, plus
    the Vector Math/Multiply Add graphs captured from this map when their Add
    input is exactly zero. The latter route avoids per-object scene baking.

    Returns ``(image, image_node, tint)`` where *tint* is a linear RGB multiplier, or
    ``(None, None, None)`` when the graph is not one of these simple forms.
    """
    image, image_node, tint = _multiply_add_tint(socket)
    if image is not None:
        return image, image_node, tint
    node, _output = _upstream(socket)
    node, _output = _unwrap_reroutes(node, _output)
    if node is None:
        return None, None, None

    # USD Preview Surface: ``inputs:scale = (r, g, b, a)`` is imported by Blender as
    # Image Texture -> Vector Math (Multiply) -> Principled Base Color.  The old importer
    # did not recognise this and classified the material as procedural, causing the same
    # material to be baked with Cycles once for every map object that used it.
    if node.type == "VECT_MATH" and getattr(node, "operation", "") == "MULTIPLY":
        if _output is None or _output.name != "Vector":
            return None, None, None
        inputs = [value for value in node.inputs if value.enabled]
        linked = [value for value in inputs if value.is_linked]
        constant = [value for value in inputs if not value.is_linked]
        if len(linked) == 1 and len(constant) >= 1:
            image, image_node, _reason = _simple_base_image(linked[0])
            if image is not None:
                try:
                    value = constant[0].default_value
                    tint = tuple(float(value[i]) for i in range(3))
                    if all(math.isfinite(value) and value >= 0.0 for value in tint):
                        return image, image_node, tint
                except (TypeError, ValueError, IndexError):
                    pass

    # Native Blender equivalent: Color Mix in Multiply mode with one image input and one
    # constant colour input.
    if node.type != "MIX" or getattr(node, "data_type", "") != "RGBA" or \
            getattr(node, "blend_type", "") != "MULTIPLY":
        return None, None, None
    inputs = [value for value in node.inputs if value.enabled]
    factor = next((value for value in inputs if value.name == "Factor"), None)
    colours = [value for value in inputs if value.name in ("A", "B")]
    if factor is None or factor.is_linked or len(colours) != 2:
        return None, None, None
    linked = [value for value in colours if value.is_linked]
    constant = [value for value in colours if not value.is_linked]
    if len(linked) != 1 or len(constant) != 1:
        return None, None, None
    image, image_node, _reason = _simple_base_image(linked[0])
    if image is None:
        return None, None, None
    amount = float(factor.default_value)
    if getattr(node, "clamp_factor", True):
        amount = min(1.0, max(0.0, amount))
    # mix(a, a * b, f) = a * (1 - f + f * b), per channel.
    tint = tuple(1.0 - amount + amount * float(value) for value in constant[0].default_value[:3])
    return image, image_node, tint


def _mix_inputs(node):
    """The (factor, below, top) sockets of a plain Mix node of either generation, or None."""
    if node.type == "MIX_RGB":
        if getattr(node, "blend_type", "") != "MIX":
            return None
        return node.inputs.get("Fac"), node.inputs.get("Color1"), node.inputs.get("Color2")
    if node.type == "MIX" and getattr(node, "data_type", "") == "RGBA" and \
            getattr(node, "blend_type", "") == "MIX":
        enabled = [value for value in node.inputs if value.enabled]
        factor = next((value for value in enabled if value.name == "Factor"), None)
        colours = [value for value in enabled if value.name in ("A", "B")]
        if factor is None or len(colours) != 2:
            return None
        return factor, colours[0], colours[1]
    return None


_CHANNEL_NAMES = {"alpha": "Alpha", "a": "Alpha", "red": "Red", "r": "Red",
                  "green": "Green", "g": "Green", "blue": "Blue", "b": "Blue",
                  "color": "Red"}
_CHANNEL_INDEX = {"Red": 0, "Green": 1, "Blue": 2, "Alpha": 3}


def _decal_factor(socket, notes: list):
    """The coverage driving one decal layer, reduced to an image channel times an optional
    vertex-colour channel: ("image", image, channel, vertex) or ("vertex", vertex) with vertex
    = (colour attribute name, channel index) or None. Skate rips drive it through Multiply,
    Subtract/Divide remaps and Separate Color nodes; anything beyond the two terms is dropped
    with a note. None when no image or vertex-colour term is found."""
    images = []
    vertices = []
    simplified = []

    def visit(node, output, depth):
        if node is None or depth > 8:
            return
        if node.type == "TEX_IMAGE":
            if getattr(node, "image", None) is not None:
                channel = _CHANNEL_NAMES.get(str(getattr(output, "name", "")).casefold(), "Red")
                images.append((node.image, channel))
            return
        if node.type in {"VERTEX_COLOR", "ATTRIBUTE"}:
            name = getattr(node, "layer_name", None) or getattr(node, "attribute_name", None) or ""
            channel = _CHANNEL_NAMES.get(str(getattr(output, "name", "")).casefold(), "Red")
            vertices.append((name, _CHANNEL_INDEX.get(channel, 0)))
            return
        if node.type in {"SEPARATE_COLOR", "SEPRGB"}:
            channel = _CHANNEL_NAMES.get(str(getattr(output, "name", "")).casefold(), "Red")
            inner, inner_output = _unwrap_reroutes(*_upstream(node.inputs[0]))
            if inner is None:
                return
            if inner.type == "TEX_IMAGE":
                if getattr(inner, "image", None) is not None:
                    images.append((inner.image, channel))
            elif inner.type in {"VERTEX_COLOR", "ATTRIBUTE"}:
                name = getattr(inner, "layer_name", None) or getattr(inner, "attribute_name", None) or ""
                vertices.append((name, _CHANNEL_INDEX.get(channel, 0)))
            else:
                visit(inner, inner_output, depth + 1)
            return
        if node.type == "MATH":
            if getattr(node, "operation", "") != "MULTIPLY":
                simplified.append(node.operation)
            for value in node.inputs:
                if value.enabled and value.is_linked:
                    inner, inner_output = _unwrap_reroutes(*_upstream(value))
                    visit(inner, inner_output, depth + 1)
                elif value.enabled and node.operation == "MULTIPLY":
                    try:
                        if abs(float(value.default_value) - 1.0) > 1e-6:
                            simplified.append("MULTIPLY_CONSTANT")
                    except TypeError:
                        pass
            return
        if node.type in {"MAP_RANGE", "CLAMP", "INVERT", "GAMMA", "BRIGHTCONTRAST", "RGBTOBW", "MIX", "MIX_RGB"}:
            simplified.append(node.type)
            for value in node.inputs:
                if value.enabled and value.is_linked:
                    inner, inner_output = _unwrap_reroutes(*_upstream(value))
                    visit(inner, inner_output, depth + 1)
            return
        simplified.append(node.type)

    node, output = _unwrap_reroutes(*_upstream(socket))
    visit(node, output, 0)
    if simplified:
        notes.append("decal coverage math was simplified to its texture and vertex-colour terms "
                     f"({', '.join(sorted(set(simplified)))})")
    if len(images) > 1:
        notes.append("only the first texture term of a decal coverage was kept")
    if len(vertices) > 1:
        notes.append("only the first vertex-colour term of a decal coverage was kept")
    vertex = vertices[0] if vertices else None
    if images:
        image, channel = images[0]
        return "image", image, channel, vertex
    if vertex is not None:
        return "vertex", vertex
    return None


def _image_uv_layer(image_node) -> str | None:
    """The UV map an image node samples, or None for the active one."""
    vector = image_node.inputs.get("Vector")
    if vector is None or not vector.is_linked:
        return None
    node, socket = _unwrap_reroutes(*_upstream(vector))
    while node is not None and node.type == "MAPPING" and _mapping_is_identity(node):
        node, socket = _unwrap_reroutes(*_upstream(node.inputs.get("Vector")))
    if node is not None and node.type == "UVMAP":
        return node.uv_map or None
    return None


def _base_image_uv_layer(material) -> str | None:
    principled = _active_principled(material)
    if principled is None:
        return None
    socket = principled.inputs.get("Base Color")
    image, node, _reason = _simple_base_image(socket)
    if image is None:
        image, node, _tint = _tinted_base_image(socket)
    return _image_uv_layer(node) if image is not None else None


def _decal_layers(material):
    """A base texture with decal textures mixed over it - a chain of Mix nodes on the Base
    Color whose top inputs are images - as (base, layers, notes): base = (image, node, tint),
    layers bottom to top of (image, node, factor, blend) with blend "mix" (also Add, which is
    approximated) or "multiply" (a darkening decal). None when the graph is anything else,
    which is then baked."""
    principled = _active_principled(material)
    if principled is None:
        return None
    socket = principled.inputs.get("Base Color")
    if socket is None or not socket.is_linked:
        return None
    layers = []
    notes = []
    for _ in range(6):
        image, node, _reason = _simple_base_image(socket)
        if image is not None:
            base = (image, node, None)
            break
        image, node, tint = _tinted_base_image(socket)
        if image is not None:
            base = (image, node, tint)
            break
        node, _output = _unwrap_reroutes(*_upstream(socket))
        if node is None:
            return None
        blend_type = getattr(node, "blend_type", "")
        if node.type == "MIX_RGB":
            factor_socket, below, top = (node.inputs.get("Fac"), node.inputs.get("Color1"),
                                         node.inputs.get("Color2"))
        elif node.type == "MIX" and getattr(node, "data_type", "") == "RGBA":
            enabled = [value for value in node.inputs if value.enabled]
            factor_socket = next((value for value in enabled if value.name == "Factor"), None)
            colours = [value for value in enabled if value.name in ("A", "B")]
            if factor_socket is None or len(colours) != 2:
                return None
            below, top = colours
        else:
            return None
        if blend_type == "MIX":
            blend = "mix"
        elif blend_type == "ADD":
            blend = "mix"
            notes.append("an additive decal layer is drawn as an alpha-blended decal")
        elif blend_type == "MULTIPLY":
            blend = "multiply"
        else:
            return None
        image, image_node, _reason = _simple_base_image(top)
        if image is None:
            return None
        if factor_socket.is_linked:
            factor = _decal_factor(factor_socket, notes)
            if factor is None:
                return None
            layers.append((image, image_node, factor, blend))
        else:
            amount = float(factor_socket.default_value)
            if amount >= 1.0 - 1e-6 and blend == "mix":
                return None
            if amount > 1e-6:
                layers.append((image, image_node, ("constant", amount), blend))
        socket = below
    else:
        return None
    if not layers:
        return None
    layers.reverse()
    return base, layers, notes


_darkening_images: dict = {}


def _darkening_image(image):
    """A generated image standing for a Multiply decal: black, with alpha = 1 - luminance
    (times the source alpha), so alpha blending darkens the surface the way the multiply
    did for grey decals."""
    key = int(image.as_pointer())
    cached = _darkening_images.get(key)
    if cached is not None:
        return cached
    channels = int(getattr(image, "channels", 0))
    width, height = (int(value) for value in image.size[:2])
    if channels <= 0 or width <= 0 or height <= 0:
        return None
    values = np.empty(width * height * channels, dtype=np.float32)
    image.pixels.foreach_get(values)
    values = values.reshape(-1, channels)
    rgb = values[:, :3] if channels >= 3 else np.repeat(values[:, :1], 3, axis=1)
    linear = np.clip(rgb, 0.0, 1.0) ** 2.2
    coverage = 1.0 - linear.mean(axis=1)
    if channels in (2, 4):
        coverage *= np.clip(values[:, channels - 1], 0.0, 1.0)
    out = np.zeros((width * height, 4), dtype=np.float32)
    out[:, 3] = coverage
    generated = bpy.data.images.new(f"ReSkateDarken_{key:x}", width=width, height=height,
                                    alpha=True, float_buffer=False)
    generated.pixels.foreach_set(out.ravel())
    try:
        generated.alpha_mode = "STRAIGHT"
    except Exception:
        pass
    _darkening_images[key] = generated
    return generated


def _decal_material(source, layer_index: int, image, image_node, factor, blend):
    """A temporary Blender material standing for one decal layer, so the ordinary material
    record path (alpha classification, opacity mask, native mask) applies to it."""
    mat = bpy.data.materials.new(f"{source.name}__decal{layer_index}")
    _enable_nodes(mat)
    tree = mat.node_tree
    principled = next((n for n in tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
    if principled is None:
        principled = tree.nodes.new("ShaderNodeBsdfPrincipled")
        output = next((n for n in tree.nodes if n.type == "OUTPUT_MATERIAL"), None) or \
            tree.nodes.new("ShaderNodeOutputMaterial")
        tree.links.new(principled.outputs["BSDF"], output.inputs["Surface"])
    colour_image = image
    if blend == "multiply":
        colour_image = _darkening_image(image) or image
    tex = tree.nodes.new("ShaderNodeTexImage")
    tex.image = colour_image
    for attribute in ("extension", "interpolation", "projection"):
        try:
            setattr(tex, attribute, getattr(image_node, attribute))
        except Exception:
            pass
    tree.links.new(tex.outputs["Color"], principled.inputs["Base Color"])
    source_principled = _active_principled(source)
    if source_principled is not None:
        for name in ("Metallic", "Roughness", "Specular IOR Level"):
            src = source_principled.inputs.get(name)
            dst = principled.inputs.get(name)
            if src is not None and dst is not None and not src.is_linked:
                dst.default_value = src.default_value
    if blend == "multiply":
        # The darkening image carries its coverage in its own alpha.
        tree.links.new(tex.outputs["Alpha"], principled.inputs["Alpha"])
    elif factor[0] == "constant":
        principled.inputs["Alpha"].default_value = float(factor[1])
    elif factor[0] == "vertex":
        principled.inputs["Alpha"].default_value = 1.0
    else:
        _kind, factor_image, channel, _vertex = factor
        source_node = tex if factor_image == colour_image else None
        if source_node is None:
            source_node = tree.nodes.new("ShaderNodeTexImage")
            source_node.image = factor_image
        if channel == "Alpha":
            tree.links.new(source_node.outputs["Alpha"], principled.inputs["Alpha"])
        else:
            separate = tree.nodes.new("ShaderNodeSeparateColor")
            tree.links.new(source_node.outputs["Color"], separate.inputs["Color"])
            tree.links.new(separate.outputs[channel], principled.inputs["Alpha"])
    for attribute, value in (("surface_render_method", "BLENDED"), ("blend_method", "BLEND")):
        try:
            setattr(mat, attribute, value)
        except Exception:
            pass
    try:
        mat.use_backface_culling = source.use_backface_culling
    except Exception:
        pass
    try:
        settings = source.get("sk8_material")
        if settings is not None:
            mat["sk8_material"] = settings.to_dict() if hasattr(settings, "to_dict") else dict(settings)
    except Exception:
        pass
    return mat


def _decal_vertex_mask(factor):
    """The (colour attribute name, channel) a decal layer's coverage is gated by, or None."""
    if factor[0] == "vertex":
        return factor[1]
    if factor[0] == "image":
        return factor[3]
    return None


def _base_mode(material):
    principled = _active_principled(material)
    if principled is None:
        return "constant", None
    socket = principled.inputs.get("Base Color")
    if socket is None or not socket.is_linked:
        return "constant", None
    image, node, reason = _simple_base_image(socket)
    if image is None:
        image, _node, _tint = _tinted_base_image(socket)
    return ("image", image) if image is not None else ("procedural", reason)


def _object_has_uv(obj) -> bool:
    try:
        return bool(obj.data.uv_layers and len(obj.data.uv_layers.active.data))
    except Exception:
        return False


def _record_bake_material(material):
    if _PERF is None or material is None:
        return
    name = str(material.name)
    key = str(int(material.as_pointer()))
    with _PERF.lock:
        known = key in _PERF.material_graphs
    if known:
        return
    _PERF.note('Procedural bake material: ' + name)
    with _PERF.lock:
        if len(_PERF.material_graphs) >= 32:
            return  # Bound diagnostic size; first 32 distinct baked materials suffice.
    try:
        nodes = []
        tree = getattr(material, 'node_tree', None)
        for node in tree.nodes if tree else ():
            row = {'name': node.name, 'type': node.type}
            for attribute in ('operation', 'blend_type', 'data_type', 'uv_map', 'attribute_name',
                              'extension', 'projection', 'interpolation', 'vector_type'):
                if hasattr(node, attribute):
                    row[attribute] = str(getattr(node, attribute))
            image = getattr(node, 'image', None)
            if image is not None:
                row['image'] = str(image.name)
                row['image_size'] = list(image.size[:2])
            inputs = []
            for socket in node.inputs:
                value = {'name': socket.name, 'linked': bool(socket.is_linked)}
                if socket.is_linked:
                    value['links'] = [{'node': link.from_node.name, 'socket': link.from_socket.name}
                                      for link in socket.links]
                elif hasattr(socket, 'default_value'):
                    raw = socket.default_value
                    if isinstance(raw, (str, bool, int, float)):
                        value['default'] = raw
                    else:
                        try:
                            value['default'] = [float(v) for v in raw]
                        except (TypeError, ValueError):
                            value['default'] = str(raw)
                inputs.append(value)
            row['inputs'] = inputs
            nodes.append(row)
        mode, reason = _base_mode(material)
        with _PERF.lock:
            _PERF.material_graphs[key] = {'name': name, 'mode': mode,
                                          'reason': str(reason), 'nodes': nodes}
        _PERF.publish()
    except Exception as error:
        _PERF.note('Could not describe material ' + name + ': ' + str(error))


def _bake_base_colour(obj, material, size: int, textures: TextureStore,
                      stem: str, diagnostics: Diagnostics, stats: dict) -> str | None:
    _record_bake_material(material)
    if isinstance(obj, _LazyBakeObject):
        obj = obj.get()
    if not _object_has_uv(obj):
        diagnostics.warn("procedural_bake_no_uv",
                         "Procedural base colour cannot be baked because this mesh has no UV map",
                         material=material.name, object=obj.name)
        return None

    scene = bpy.context.scene
    old_engine = scene.render.engine
    old_active = bpy.context.view_layer.objects.active
    old_selected = list(bpy.context.selected_objects)
    old_materials = list(obj.data.materials)
    bake_material = None
    image = None
    temp_path = None
    hidden_for_bake = []
    try:
        bake_material = material.copy()
        _enable_nodes(bake_material)
        image = bpy.data.images.new(
            name=f"ReSkateBake_{uuid.uuid4().hex}", width=size, height=size,
            alpha=True, float_buffer=False)
        image.colorspace_settings.name = "sRGB"
        target = bake_material.node_tree.nodes.new("ShaderNodeTexImage")
        target.name = "ReSkate_Bake_Target"
        target.image = image
        bake_material.node_tree.nodes.active = target

        obj.data.materials.clear()
        obj.data.materials.append(bake_material)
        # Cycles syncs every render-visible object per bake; a city's worth of geometry
        # made each one seconds long.
        with _perf_phase("bake_scene_hide"):
            for other in scene.objects:
                if other is not obj and not other.hide_render:
                    other.hide_render = True
                    hidden_for_bake.append(other)
        if bpy.context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        bpy.ops.object.select_all(action="DESELECT")
        obj.hide_set(False)
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        scene.render.engine = "CYCLES"
        try:
            scene.cycles.device = "CPU"
            scene.cycles.samples = 1
        except Exception:
            pass
        with _perf_phase("bake_dependency_update"):
            bpy.context.view_layer.update()
        _progress_note(f"Baking the base colour of {material.name} with Cycles ({size}x{size})")
        with _perf_phase("cycles_bake"):
            bpy.ops.object.bake(type="DIFFUSE", pass_filter={"COLOR"},
                                use_clear=True, margin=2)

        fd, raw_path = tempfile.mkstemp(prefix="reskate-bake-", suffix=".png")
        os.close(fd)
        temp_path = Path(raw_path)
        temp_path.unlink(missing_ok=True)
        image.file_format = "PNG"
        image.filepath_raw = str(temp_path)
        with _perf_phase("bake_image_save"):
            image.save()
        if not temp_path.is_file():
            raise RuntimeError("Blender produced no baked PNG")
        stats["procedural_bakes"] += 1
        return textures.add_bytes(temp_path.read_bytes(), stem, ".png",
                                  source=f"baked:{material.name}", role="base_color")
    except Exception as ex:
        diagnostics.warn("procedural_bake_failed", str(ex),
                         material=material.name, object=obj.name)
        return None
    finally:
        with _perf_phase("bake_restore"):
            for other in hidden_for_bake:
                other.hide_render = False
            try:
                obj.data.materials.clear()
                for old in old_materials:
                    obj.data.materials.append(old)
            except Exception:
                pass
            scene.render.engine = old_engine
            try:
                bpy.ops.object.select_all(action="DESELECT")
                for old in old_selected:
                    old.select_set(True)
                bpy.context.view_layer.objects.active = old_active
            except Exception:
                pass
            if bake_material is not None:
                try:
                    bpy.data.materials.remove(bake_material)
                except Exception:
                    pass
            if image is not None:
                try:
                    bpy.data.images.remove(image)
                except Exception:
                    pass
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)


def _material_invisible(material) -> bool:
    """The material's Invisible (Collision Only) switch (Material > Skate Map): its faces are
    not drawn in game and keep only their collision."""
    return material is not None and _profile_bool(_profile_raw_value(material, "invisible", False))


def _material_enum(material, attribute: str) -> str | None:

    if material is None:
        return None
    try:
        value = getattr(material, attribute)
    except Exception:
        return None
    value = str(value).strip().upper()
    return value or None


def _material_alpha_cutoff(material) -> float:

    try:
        value = float(getattr(material, "alpha_threshold"))
    except Exception:
        value = 0.5
    if not math.isfinite(value):
        value = 0.5
    return round(min(1.0, max(0.0, value)), 5)


def _material_render_metadata(material) -> dict:

    if material is None:
        return {}
    override = _profile_raw_value(material, "domain", "surface")
    if isinstance(override, int) and not isinstance(override, bool) and 0 <= override < 7:
        override = ("surface", "decal", "foliage", "ocean", "ocean", "glass_opaque")[override]
    override = str(override).casefold()
    if override == "water":
        override = "ocean"
    elif override == "glass":
        override = "glass_opaque"
    if override not in {"surface", "decal", "foliage", "ocean", "glass_opaque"}:
        raise ValueError(f"{material.name}: unsupported shader override {override}")
    result = {"domain": override, "shader_override": override}
    try:


        result["double_sided"] = override == "foliage" or not bool(material.use_backface_culling)
    except Exception:
        pass
    try:
        result["transparent_shadow"] = bool(material.use_transparent_shadow)
    except Exception:
        pass
    if override == "decal":
        result["alpha"] = "blend"
    elif override == "foliage":
        result["alpha"] = "mask"
    elif override in {"ocean", "glass_opaque"}:
        result["alpha"] = "opaque"
    return result


def _material_alpha_metadata(material, alpha_socket, textures: TextureStore,
                             diagnostics: Diagnostics,
                             base_color_image=None) -> dict:


    if alpha_socket is None or not alpha_socket.is_linked:
        return {
            "alpha": "opaque",
            "source_alpha_kind": "opaque",
            "alpha_mode_source": ("no_principled_alpha" if alpha_socket is None
                                  else "principled_alpha_unlinked"),
            "source_alpha_mode": "OPAQUE",
        }

    surface_mode = _material_enum(material, "surface_render_method")
    legacy_mode = _material_enum(material, "blend_method")

    alpha_image, alpha_channel, alpha_chain = _trace_image(alpha_socket)
    try:
        shared_base_alpha = (
            base_color_image is not None and alpha_image is not None and
            str(alpha_channel).casefold() == "alpha" and
            int(base_color_image.as_pointer()) == int(alpha_image.as_pointer()))
    except Exception:
        shared_base_alpha = base_color_image is not None and \
            base_color_image is alpha_image and \
            str(alpha_channel).casefold() == "alpha"


    if shared_base_alpha and (surface_mode == "BLENDED" or
                              legacy_mode == "BLEND"):
        kind, source, source_mode = (
            "blend", "base_color_texture_alpha",
            surface_mode if surface_mode == "BLENDED" else legacy_mode)


    elif surface_mode == "BLENDED":
        kind, source, source_mode = (
            "blend", "blender.surface_render_method", surface_mode)
    elif legacy_mode == "BLEND":
        kind, source, source_mode = (
            "blend", "blender.blend_method", legacy_mode)
    elif surface_mode == "DITHERED":
        kind, source, source_mode = (
            "mask", "blender.surface_render_method", surface_mode)
    elif legacy_mode in {"HASHED", "CLIP"}:
        kind, source, source_mode = (
            "mask", "blender.blend_method", legacy_mode)
    else:
        if alpha_image is not None and str(alpha_channel).casefold() == "alpha":
            kind = textures.alpha_kind(
                alpha_image, material=getattr(material, "name", None))
            source, source_mode = "image_pixels", "AUTO"
        else:


            kind, source, source_mode = "blend", "linked_expression", "AUTO"
            diagnostics.warn(
                "alpha_input_not_classified",
                "Linked alpha input has no authored Blender mode and could not be reduced to an image Alpha channel; preserving blend precision",
                material=getattr(material, "name", None),
                channel=alpha_channel, nodes=alpha_chain)

    result = {
        "alpha": kind,


        "source_alpha_kind": kind,
        "alpha_mode_source": source,
        "source_alpha_mode": source_mode,
    }
    if kind == "mask":
        result["alpha_cutoff"] = _material_alpha_cutoff(material)
    return result


def _material_record(material, bake_obj, key, options, textures: TextureStore,
                     diagnostics: Diagnostics, stats: dict,
                     surface_profiles: dict, base_override=None) -> dict:
    surface = _surface_for(material)
    collision_material_packed = _collision_material(None, material)
    record = {"surface": surface, "srgb": True,
              "source_material": material.name if material else "<unassigned>",
              "collision_material": _collision_material_identifier(
                  collision_material_packed),
              "collision_material_packed": collision_material_packed}
    surface_profile_id = _register_surface_profile(material, surface_profiles)
    if surface_profile_id:
        record["surface_profile"] = surface_profile_id
    render_metadata = _material_render_metadata(material)
    if render_metadata.get("shader_override") == "ocean":
        record.update(render_metadata)
        return record
    principled = _active_principled(material)
    imported_shader = _br_imported_shader_info(material, principled)
    if imported_shader is not None:
        record["source_shader_family"] = imported_shader["family"]
        record["source_shader"] = imported_shader
        stats["imported_shader_materials"] = (
            stats.get("imported_shader_materials", 0) + 1)
        lightmap_source = imported_shader["lightmap_source"]
        if lightmap_source == "blank_generated_placeholder":
            stats["blank_source_lightmaps_ignored"] = (
                stats.get("blank_source_lightmaps_ignored", 0) + 1)
        elif lightmap_source == "source_image_not_compiled":
            diagnostics.warn(
                "source_lightmap_not_exported",
                "The imported BR shader has a nonblank source lightmap, but the native material path has no proven independent lightmap slot",
                material=material.name,
                image=imported_shader.get("lightmap_image"),
                uv_map=imported_shader.get("lightmap_uv"))
    if principled is None:
        colour = tuple(getattr(material, "diffuse_color", (0.8, 0.8, 0.8, 1.0))) \
            if material else (0.8, 0.8, 0.8, 1.0)
        colour = tuple(colour[:4]) if len(colour) >= 4 else tuple(colour[:3]) + (1.0,)
        base = textures.solid(colour, key + "_c", "base_color", source_is_linear=True)
        record.update({"texture": base, "base_color_texture": base,
                       "base_color_srgb": True,
                       "base_color_factor": _round_list(colour, 5),
                       "base_color_source": "material_diffuse_color",
                       "metallic_factor": 0.0, "roughness_factor": 0.5,
                       "alpha_factor": float(colour[3])})
        normal = textures.solid((0.5, 0.5, 1.0, 1.0), key + "_ny",
                                "flat_normal", source_is_linear=False)
        mask = textures.solid((0.0, 0.5, 1.0, colour[3]), key + "_msk",
                              "native_mask", source_is_linear=False)
        record.update({"normal_texture": normal, "normal_srgb": False,
                       "normal_encoding": "tangent-space-rgb",
                       "normal_generated": True,
                       "mask_texture": mask, "mask_srgb": False,
                       "mask_channels": {"r": "metallic", "g": "smoothness",
                                         "b": "ambient_occlusion", "a": "opacity"},
                       "mask_generated": True})
    else:
        base_socket = principled.inputs.get("Base Color")
        colour = _default(base_socket, (0.8, 0.8, 0.8, 1.0))
        if len(colour) < 4:
            colour = tuple(colour) + (1.0,)
        base = None
        base_source_image = None
        source = "constant"
        explicit_base = _profile_raw_value(material, "base_color_image", None)
        if isinstance(explicit_base, bpy.types.Image):
            base = textures.image(explicit_base, key + "_c", "base_color", material=material.name)
            base_source_image = explicit_base
            source = "explicit_image"
        elif base_socket is not None and base_socket.is_linked:
            if base_override is not None:
                # The bottom texture of a decal-layered material; its layers export as
                # separate decal parts.
                image, image_node, tint = base_override
                reason = None
            else:
                image, image_node, reason = _simple_base_image(base_socket)
                tint = None
                if image is None:
                    tinted, tinted_node, tint = _tinted_base_image(base_socket)
                    if tinted is not None:
                        image, image_node = tinted, tinted_node
            if image is not None:
                base = (textures.tinted(image, tint, key + "_c", material=material.name)
                        if tint is not None else
                        textures.image(image, key + "_c", "base_color",
                                       material=material.name))
                base_source_image = image
                source = "image" if tint is None else "tinted_image"
                if tint is not None and _multiply_add_tint(base_socket)[0] is not None:
                    stats["multiply_add_tint_materials"] = stats.get("multiply_add_tint_materials", 0) + 1
                    if _PERF is not None:
                        _PERF.note("Direct image tint (Multiply Add): " + material.name)
                # How UVs outside 0..1 sample: REPEAT tiles, EXTEND clamps to the
                # edge texels, CLIP is transparent, MIRROR flips every other tile.
                extension = str(getattr(image_node, "extension", "REPEAT") or "REPEAT")
                record["base_color_extension"] = extension.casefold()
            else:
                source = "procedural_bake"
                if options.bake_procedural:
                    base = _bake_base_colour(bake_obj, material, options.bake_size,
                                             textures, key + "_c", diagnostics, stats)
                if base is None:
                    source = "procedural_fallback"
                    diagnostics.warn(
                        "procedural_base_color_fallback",
                        f"{reason or 'procedural graph'} was not preserved; using the Base Color socket fallback",
                        material=material.name, object=bake_obj.name)
        if base is None:
            base = textures.solid(colour, key + "_c", "base_color",
                                  source_is_linear=True)
            stats["constant_base_color_textures"] += 1
        metallic_socket = principled.inputs.get("Metallic")
        roughness_socket = principled.inputs.get("Roughness")
        alpha_socket = principled.inputs.get("Alpha")
        metallic = float(_default(metallic_socket, 0.0))
        roughness = float(_default(roughness_socket, 0.5))
        alpha = float(_default(alpha_socket, colour[3]))
        record.update({"texture": base, "base_color_texture": base,
                       "base_color_srgb": True,
                       "base_color_factor": _round_list(colour, 5),
                       "base_color_source": source,
                       "metallic_factor": round(metallic, 5),
                       "roughness_factor": round(roughness, 5),
                       "alpha_factor": round(alpha, 5)})

        scalar_paths = {}
        for role, socket in (("metallic", metallic_socket),
                             ("roughness", roughness_socket)):
            if socket is not None and socket.is_linked and _explicit_native_mask_image(material) is None:
                try:
                    image, channel = _map_export_addon().scalar_texture_source(socket)
                    if image is None:
                        raise ValueError("no image is connected")
                    path = textures.image(image, key + "_" + role, role,
                                          material=material.name)
                    if not path:
                        raise ValueError("the connected image could not be exported")
                    record[role + "_texture"] = path
                    record[role + "_channel"] = channel
                    record[role + "_srgb"] = image.colorspace_settings.name.casefold() == "srgb"
                    scalar_paths[role] = (path, channel)
                except (ValueError, RuntimeError) as ex:
                    diagnostics.warn("pbr_input_not_exported",
                                     f"{role}: {ex}; using the socket value",
                                     material=material.name)

        normal_socket = principled.inputs.get("Normal")
        normal_path = None
        if normal_socket is not None and normal_socket.is_linked:
            image, channel, chain = _trace_image(normal_socket)
            if image is not None:
                normal_path = textures.image(image, key + "_ny", "normal",
                                             material=material.name)
                if normal_path:
                    record["normal_texture"] = normal_path
                    record["normal_srgb"] = False
                    record["normal_source_channel"] = channel
                    record["normal_encoding"] = "tangent-space-rgb"
            elif chain:
                diagnostics.warn("normal_input_not_exported",
                                 "Linked normal input has no traceable image dependency",
                                 material=material.name, nodes=chain)
        if normal_path is None:
            normal_path = textures.solid((0.5, 0.5, 1.0, 1.0), key + "_ny",
                                         "flat_normal", source_is_linear=False)
            record["normal_texture"] = normal_path
            record["normal_srgb"] = False
            record["normal_encoding"] = "tangent-space-rgb"
            record["normal_generated"] = True


        surface_mask_image = None
        surface_mask_constant = None
        explicit_mask = _explicit_native_mask_image(material)
        if explicit_mask is not None:
            mask = textures.image(explicit_mask, key + "_msk", "native_mask",
                                  material=material.name)
            if mask:
                record["mask_texture"] = mask
                record["mask_srgb"] = False
                record["mask_channels"] = {"r": "metallic", "g": "smoothness",
                                           "b": "explicit_source", "a": "opacity"}
                record["mask_source"] = "explicit_msk_image"
                surface_mask_image = explicit_mask
        else:
            mask = textures.solid((metallic, 1.0 - roughness, 1.0, alpha),
                                  key + "_msk", "native_mask",
                                  source_is_linear=False)
            record["mask_texture"] = mask
            record["mask_srgb"] = False
            record["mask_channels"] = {"r": "metallic",
                                       "g": "smoothness",
                                       "b": "ambient_occlusion",
                                       "a": "opacity"}
            record["mask_generated"] = True


            surface_mask_constant = (4, 4, 1.0 - roughness, 1.0)
            if scalar_paths:
                record["mask_source"] = "native_scalar_packing"
            else:
                record["mask_source"] = "principled_factors"

        alpha_metadata = _material_alpha_metadata(
            material, alpha_socket, textures, diagnostics,
            base_color_image=base_source_image)
        record.update(alpha_metadata)
        if alpha_metadata.get("alpha_mode_source") == \
                "base_color_texture_alpha":
            stats["shared_base_color_alpha_materials"] = \
                stats.get("shared_base_color_alpha_materials", 0) + 1
        if alpha_metadata["alpha"] in {"mask", "blend"}:
            alpha_image, alpha_channel, _alpha_chain = _trace_image(alpha_socket)
            opacity_channel = _CHANNEL_NAMES.get(str(alpha_channel).casefold())
            if opacity_channel is None:
                alpha_image = None
            opacity_source = "principled_alpha_image"


            if alpha_image is None and base_source_image is not None and \
                    textures.alpha_kind(
                        base_source_image,
                        material=getattr(material, "name", None)) != "opaque":
                alpha_image = base_source_image
                opacity_source = "base_color_texture_alpha_fallback"
                stats["base_color_alpha_fallback_materials"] = (
                    stats.get("base_color_alpha_fallback_materials", 0) + 1)
            opacity, opacity_info = textures.opacity(
                alpha_image, key + "_osk", material=material.name,
                fallback_alpha=alpha,
                surface_mask_image=surface_mask_image,
                surface_mask_constant=surface_mask_constant,
                channel=(opacity_channel or "Alpha") if alpha_image is not None else "Alpha")
            surface_channels = opacity_info["surface_channels"]
            preserved = surface_channels != "neutral"
            record.update({
                "opacity_texture": opacity,
                "opacity_srgb": False,
                "opacity_packing": "osk_r_opacity",
                "opacity_channels": {
                    "r": (opacity_source if opacity_info["alpha_from_image"] else
                          "principled_alpha_factor_fallback"),
                    "g": ("surface_mask_g" if preserved else
                          "neutral_smoothness_0.5"),
                    "b": ("surface_mask_b" if preserved else
                          "neutral_ambient_occlusion_1.0"),
                    "a": "opaque",
                },
                "opacity_surface_channels_source": surface_channels,
            })

    if principled is None:
        record.update(_material_alpha_metadata(
            material, None, textures, diagnostics))
    record.update(render_metadata)
    for role, suffix, kind in (("base_color", "_c", "base_color"),
                               ("normal", "_ny", "normal"),
                               ("mask", "_msk", "native_mask"),
                               ("opacity", "_opacity", "opacity")):
        image = _profile_raw_value(material, role + "_image", None)
        if not isinstance(image, bpy.types.Image):
            continue
        path = textures.image(image, key + suffix, kind,
                              material=material.name)
        if not path:
            raise ValueError(f"{material.name}: could not export assigned {role} texture")
        record[role + "_texture"] = path
        record[role + "_srgb"] = role == "base_color" and bool(_profile_raw_value(material, "srgb", True))
        record.pop(role + "_generated", None)
        if role == "base_color":
            record["texture"] = path
            record["base_color_source"] = "explicit_image"
            record["srgb"] = record["base_color_srgb"]
        elif role == "normal":
            record["normal_encoding"] = "tangent-space-rgb"
        elif role == "mask":
            record["mask_source"] = "explicit_msk_image"
        elif role == "opacity":
            record["alpha_source"] = "opacity_texture"
            record.pop("opacity_packing", None)
    if _explicit_native_mask_image(material) is None:
        for role in ("roughness", "metallic"):
            image = _profile_raw_value(material, role + "_image", None)
            if not isinstance(image, bpy.types.Image):
                continue
            path = textures.image(image, key + "_" + role, role, material=material.name)
            if not path:
                raise ValueError(f"{material.name}: could not export assigned {role} texture")
            channel = _profile_raw_value(material, role + "_channel", "Color")
            if isinstance(channel, int):
                channel = ("Color", "Red", "Green", "Blue", "Alpha")[channel]
            record[role + "_texture"] = path
            record[role + "_channel"] = channel
            record[role + "_srgb"] = image.colorspace_settings.name.casefold() == "srgb"
    alpha_mode = _profile_raw_value(material, "alpha", "auto")
    if isinstance(alpha_mode, int):
        alpha_mode = ("auto", "opaque", "mask", "blend")[alpha_mode]
    if alpha_mode != "auto" and render_metadata.get("domain", "surface") == "surface":
        record["alpha"] = alpha_mode
    alpha_source = _profile_raw_value(material, "alpha_source", "auto")
    if isinstance(alpha_source, int):
        alpha_source = ("auto", "base_color_texture", "opacity_texture", "constant")[alpha_source]
    if alpha_source != "auto":
        record["alpha_source"] = alpha_source

    return record


@dataclass
class EvaluatedInstance:
    obj: object
    original: object
    matrix_world: Matrix
    persistent_id: tuple
    is_instance: bool
    # The scene object that draws a collection instance (None for a plain object).
    instancer: object = None


def _matrix_sort_key(matrix):
    return tuple(round(float(matrix[r][c]), 9) for r in range(4) for c in range(4))


def _evaluated_mesh_instances(depsgraph, diagnostics: Diagnostics):
    out = []
    missing_sources = set()
    for item in depsgraph.object_instances:
        obj = item.object
        if obj is None or obj.type != "MESH":
            continue
        original = getattr(obj, "original", None) or obj
        if getattr(original.data, "is_missing", False):
            # to_mesh() can crash Blender on missing linked-library placeholders,
            # especially particle scatters with tens of thousands of copies.
            key = int(original.data.as_pointer())
            if key not in missing_sources:
                missing_sources.add(key)
                diagnostics.warn("missing_linked_mesh",
                                 "Linked mesh library is missing; its placements were skipped",
                                 object=original.name,
                                 library=original.data.library.filepath if original.data.library else None)
            continue
        if original.get("sk8_npc_enabled", False):
            continue
        if original.get("sk8_guide", False):  # authoring aids, like a bus stop's wire shelter
            continue
        if getattr(original, "hide_render", False):
            diagnostics.warn("hidden_render_object_skipped",
                             "Object is disabled for rendering and was not exported",
                             object=original.name)
            continue
        if not item.is_instance and original.particle_systems and not original.show_instancer_for_render:
            continue
        persistent = tuple(int(v) for v in getattr(item, "persistent_id", ()))
        instancer = getattr(item, "parent", None) if item.is_instance else None
        if instancer is not None:
            instancer = getattr(instancer, "original", None) or instancer
            if instancer.hide_render:
                continue
        if item.is_instance:
            # Instance objects are temporaries that Blender frees once iteration moves on
            # keeping them crashes later reads (material_slots, to_mesh)
            # Collection and linked-duplicate instances share the sources evaluated data, so the
            # stable evaluated source is equivalent.
            stable = original.evaluated_get(depsgraph)
            if stable.data is not None and obj.data is not None and \
                    stable.data.as_pointer() == obj.data.as_pointer():
                obj = stable
        out.append(EvaluatedInstance(obj, original, Matrix(item.matrix_world),
                                     persistent, bool(item.is_instance), instancer))
    out.sort(key=lambda x: (x.original.name.casefold(), x.original.name,
                            x.persistent_id, _matrix_sort_key(x.matrix_world)))
    return out


def _source_path(obj):
    names = []
    seen = set()
    current = obj
    while current is not None and int(current.as_pointer()) not in seen:
        seen.add(int(current.as_pointer()))
        names.append(current.name)
        current = current.parent
    return "/".join(reversed(names))


def _shared_mesh_placements(instances, surface_profiles):
    """Group evaluated copies, not just names: modifiers and material overrides matter.

    Collection/particle/Geometry Nodes instances share an evaluated object. Linked
    duplicates without modifiers can also share their original mesh datablock. A
    procedural bake may depend on world position or object identity, so those retain
    the existing per-placement bake path instead of silently changing appearance.
    """
    groups = {}
    for index, instance in enumerate(instances):
        original = instance.original
        if _placement_mode(original) != "authored_mesh":
            continue
        determinant = instance.matrix_world.to_3x3().determinant()
        if not math.isfinite(determinant) or abs(determinant) < 1e-12:
            continue
        materials = [slot.material for slot in instance.obj.material_slots]
        if any(_base_mode(mat)[0] == "procedural" and _decal_layers(mat) is None
               for mat in materials):
            continue
        geometry = ("evaluated", int(instance.obj.as_pointer()))
        if not original.modifiers and not getattr(original.data, "shape_keys", None):
            geometry = ("mesh", int(original.data.as_pointer()))
        contracts = tuple((int(mat.as_pointer()) if mat else 0,
                           _collision_material(original, mat),
                           _register_surface_profile(mat, surface_profiles, original))
                          for mat in (materials or [None]))
        key = (geometry, contracts, _collision_mode(original), determinant < 0.0)
        groups.setdefault(key, []).append(index)
    shared = {}
    skipped = set()
    for indices in groups.values():
        if len(indices) > 1:
            shared[indices[0]] = [instances[index] for index in indices]
            skipped.update(indices[1:])
    return shared, skipped


def _marker_record(obj, matrix: Matrix, marker_name: str, scale: float,
                   diagnostics: Diagnostics, is_instance=False,
                   persistent_id=()):
    position = _blender_to_game(matrix.translation)
    position = [v * scale for v in position]


    facing = _blender_to_game(matrix.to_3x3() @ Vector((0.0, 1.0, 0.0)))
    length = math.hypot(facing[0], facing[2])
    yaw = math.degrees(math.atan2(facing[0], facing[2])) if length > 1e-8 else 0.0
    if length <= 1e-8:
        diagnostics.warn("marker_facing_vertical",
                         f"{marker_name} local +Y is vertical; yaw defaulted to zero",
                         object=obj.name, marker=marker_name)
    record = {"name": obj.name, "source_name": obj.name,
              "source_path": _source_path(obj),
              "source_collections": sorted(c.name for c in obj.users_collection),
              "position": _round_list(position, 4), "yaw": round(yaw, 3)}
    if is_instance:


        record["source_instance"] = [int(v) for v in persistent_id
                                     if int(v) != 2147483647]
    return record


def _empty_marker_records(scene, marker_name: str, scale: float,
                          diagnostics: Diagnostics, missing_warning=False):
    markers = sorted((obj for obj in scene.objects
                      if obj.type == "EMPTY"
                      and obj.name.casefold() == marker_name.casefold()),
                     key=lambda obj: (_source_path(obj).casefold(), _source_path(obj)))
    records = []
    for obj in markers:
        records.append(_marker_record(obj, Matrix(obj.matrix_world), marker_name,
                                      scale, diagnostics))
    if missing_warning and not records:
        diagnostics.warn("spawn_missing",
                         "No EMPTY named exactly 'spawn' (case-insensitive) was found")
    return records


def _evaluated_empty_marker_records(depsgraph, marker_name: str, scale: float,
                                    diagnostics: Diagnostics, numbered=False):
    # numbered: every Empty whose name starts with the marker ("TravelPoint.001") counts.


    candidates = []
    for item in depsgraph.object_instances:
        obj = item.object
        if obj is None or obj.type != "EMPTY":
            continue
        original = getattr(obj, "original", None) or obj
        folded = original.name.casefold()
        if folded != marker_name.casefold() and not (numbered and folded.startswith(marker_name.casefold())):
            continue
        persistent = tuple(int(v) for v in getattr(item, "persistent_id", ()))
        matrix = Matrix(item.matrix_world)
        candidates.append((original, matrix, bool(item.is_instance), persistent))
    candidates.sort(key=lambda x: (_source_path(x[0]).casefold(),
                                   _source_path(x[0]), x[3],
                                   _matrix_sort_key(x[1])))
    return [_marker_record(obj, matrix, marker_name, scale, diagnostics,
                           is_instance=is_instance, persistent_id=persistent)
            for obj, matrix, is_instance, persistent in candidates]


_HANDOFF_ADDON = None


def _map_export_addon():
    global _HANDOFF_ADDON
    if _HANDOFF_ADDON is None:
        path = _RUNTIME_ROOT / "sk8_map_export.py"
        spec = importlib.util.spec_from_file_location("_studio_map_handoff_addon", path)
        _HANDOFF_ADDON = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_HANDOFF_ADDON)
    return _HANDOFF_ADDON


# The record builders are pure functions of the depsgraph, so one loaded copy of the add-on
# serves them all (it used to be executed again for each record type).
def _evaluated_light_records(depsgraph, scale: float):
    return _map_export_addon().light_records(depsgraph, scale)


def _evaluated_audio_records(depsgraph, scale: float):
    return _map_export_addon().audio_records(depsgraph, scale)


def _evaluated_npc_routes(depsgraph, scale: float):
    return _map_export_addon().npc_route_records(depsgraph, scale)


def _evaluated_interaction_prefabs(depsgraph, scale: float):
    return _map_export_addon().interaction_prefab_records(depsgraph, scale)


def _evaluated_reflection_probe_records(depsgraph, scale: float):
    # EEVEE reflection cubemaps (Blender < 4.1) and sphere probes (4.1+) become Frostbite
    # box reflection volumes, which the game captures at runtime from the probe's origin.
    candidates = []
    for item in depsgraph.object_instances:
        obj = item.object
        if obj is None or obj.type != "LIGHT_PROBE":
            continue
        probe = obj.data
        if str(getattr(probe, "type", "")) not in {"CUBEMAP", "SPHERE"}:
            continue
        original = getattr(obj, "original", None) or obj
        if getattr(original, "hide_render", False):
            continue
        persistent = tuple(int(value) for value in
                           getattr(item, "persistent_id", ()))
        candidates.append((original, probe, Matrix(item.matrix_world),
                           persistent))
    candidates.sort(key=lambda row: (_source_path(row[0]).casefold(),
                                     _source_path(row[0]), row[3],
                                     _matrix_sort_key(row[2])))
    output = []
    for original, probe, matrix, _ in candidates:
        distance = float(getattr(probe, "influence_distance", 2.5))
        rows = _game_transform_rows(matrix, scale)
        # The influence is the probe's unit shape scaled by distance and the object's
        # scale; a Frostbite volume is a unit cube, so the axes carry the full size.
        size = 2.0 * distance * scale
        transform = [value * size for value in rows[:9]] + rows[9:]
        output.append({
            "name": original.name,
            "source_path": _source_path(original),
            "influence": str(getattr(probe, "influence_type", "ELIPSOID")).lower(),
            "transform": _round_list(transform, 6),
            "falloff": round(float(getattr(probe, "falloff", 0.2)), 6),
            "clip_end": round(float(getattr(probe, "clip_end", 40.0)) * scale, 6),
            "intensity": round(float(getattr(probe, "intensity", 1.0)), 6),
        })
    return output


def _spawn_records(scene, scale, diagnostics: Diagnostics):
    records = _empty_marker_records(scene, "spawn", scale, diagnostics,
                                    missing_warning=True)
    if len(records) > 1:
        diagnostics.warn("multiple_spawn_markers",
                         "A map has one native spawn; the first deterministic marker was selected",
                         count=len(records))
        return records[:1]
    return records


@dataclass
class PartArrays:
    world_positions: list
    world_normals: list
    local_positions: list
    local_normals: list
    uvs: list
    triangle_count: int


@dataclass(frozen=True)
class CollisionArrays:
    positions: tuple
    indices: tuple
    triangle_count: int


@dataclass(frozen=True)
class CollisionWriteJob:
    destination: Path
    arrays: CollisionArrays
    name: str
    mode: str


@dataclass
class SpatialPart:
    cell: tuple
    arrays: PartArrays


    origin: tuple | None = None


@dataclass
class _Corner:
    world_position: tuple
    world_normal: tuple
    local_position: tuple
    local_normal: tuple
    uv: tuple


def _normalised(values, fallback):
    length2 = sum(float(value) * float(value) for value in values)
    if length2 <= 1e-24:
        return tuple(float(value) for value in fallback)
    inverse = 1.0 / math.sqrt(length2)
    return tuple(float(value) * inverse for value in values)


def _interpolate_corner(a: _Corner, b: _Corner, amount: float,
                        axis=None, boundary=None) -> _Corner:
    t = min(1.0, max(0.0, float(amount)))

    def lerp(left, right):
        return tuple(float(x) + (float(y) - float(x)) * t
                     for x, y in zip(left, right))

    world_position = list(lerp(a.world_position, b.world_position))
    if axis is not None:


        world_position[axis] = float(boundary)
    world_normal = lerp(a.world_normal, b.world_normal)
    local_normal = lerp(a.local_normal, b.local_normal)
    return _Corner(
        tuple(world_position),
        _normalised(world_normal, a.world_normal),
        lerp(a.local_position, b.local_position),
        _normalised(local_normal, a.local_normal),
        lerp(a.uv, b.uv),
    )


def _clip_polygon(poly, axis: int, boundary: float, keep_above: bool):
    if not poly:
        return []

    def inside(corner):
        value = corner.world_position[axis]
        return value >= boundary if keep_above else value <= boundary

    output = []
    previous = poly[-1]
    previous_inside = inside(previous)
    for current in poly:
        current_inside = inside(current)
        if current_inside != previous_inside:
            a = previous.world_position[axis]
            b = current.world_position[axis]
            denominator = b - a
            amount = ((boundary - a) / denominator
                      if abs(denominator) > 1e-30 else 0.0)
            output.append(_interpolate_corner(previous, current, amount,
                                              axis, boundary))
        if current_inside:
            output.append(current)
        previous = current
        previous_inside = current_inside


    compact = []
    for corner in output:
        if compact and all(abs(corner.world_position[i] -
                               compact[-1].world_position[i]) <= 1e-10
                           for i in range(3)):
            continue
        compact.append(corner)
    if len(compact) > 1 and all(
            abs(compact[0].world_position[i] - compact[-1].world_position[i])
            <= 1e-10 for i in range(3)):
        compact.pop()
    return compact


def _triangle_area_squared(a: _Corner, b: _Corner, c: _Corner):
    ab = tuple(b.world_position[i] - a.world_position[i] for i in range(3))
    ac = tuple(c.world_position[i] - a.world_position[i] for i in range(3))
    cross = (ab[1] * ac[2] - ab[2] * ac[1],
             ab[2] * ac[0] - ab[0] * ac[2],
             ab[0] * ac[1] - ab[1] * ac[0])
    return sum(value * value for value in cross)


def _local_reach(positions) -> float:
    if not positions:
        return 0.0
    lo = [min(point[axis] for point in positions) for axis in range(3)]
    hi = [max(point[axis] for point in positions) for axis in range(3)]
    return max((hi[axis] - lo[axis]) * 0.5 for axis in range(3))


def _cell_origin(cell, cell_size: float) -> tuple:
    return tuple((int(index) + 0.5) * float(cell_size) for index in cell)


def _origin_reach(positions, origin) -> float:
    if len(positions) == 0:
        return 0.0
    points = np.asarray(positions, dtype=np.float64)
    return float(np.max(np.abs(points - np.asarray(origin, dtype=np.float64))))


def _native_render_origin(part: SpatialPart) -> list:

    if part.origin is None:
        raise ValueError("spatial render part has no shared origin")
    return [float(v) for v in _blender_to_game(part.origin)]


def _filter_native_degenerate_triangles(
        part: SpatialPart) -> tuple[SpatialPart | None, int]:


    if part.origin is None:
        raise ValueError("spatial render part has no shared origin")
    arrays = part.arrays
    lengths = {len(arrays.world_positions), len(arrays.world_normals),
               len(arrays.local_positions), len(arrays.local_normals),
               len(arrays.uvs)}
    if len(lengths) != 1 or len(arrays.world_positions) % 3:
        raise ValueError("render corner streams are not aligned triangles")

    # Match the GLB's float32 positions and the compiler's float32 local subtraction.
    positions = np.asarray(arrays.world_positions, dtype=np.float64).reshape(-1, 3)
    single = positions.astype(np.float32)
    if np.any(np.isinf(single) & np.isfinite(positions)):
        raise OverflowError("float too large to pack with f format")
    local = single - np.asarray(part.origin, dtype=np.float32)
    points = local.astype(np.float64).reshape(-1, 3, 3)
    u = points[:, 1] - points[:, 0]
    v = points[:, 2] - points[:, 0]
    degenerate = ((u[:, 1] * v[:, 2] - u[:, 2] * v[:, 1] == 0.0) &
                  (u[:, 2] * v[:, 0] - u[:, 0] * v[:, 2] == 0.0) &
                  (u[:, 0] * v[:, 1] - u[:, 1] * v[:, 0] == 0.0))
    dropped = int(np.count_nonzero(degenerate))
    if not dropped:
        return part, 0
    keep = np.nonzero(~degenerate)[0]
    if not len(keep):
        return None, dropped
    pick = _corner_picker(keep)
    filtered = PartArrays(
        pick(arrays.world_positions), pick(arrays.world_normals),
        pick(arrays.local_positions), pick(arrays.local_normals),
        pick(arrays.uvs), len(keep))
    return SpatialPart(part.cell, filtered, part.origin), dropped


def _corner_picker(triangles):
    """Returns a function selecting the three corners of each listed triangle, in order."""
    corners = (np.asarray(triangles, dtype=np.int64)[:, None] * 3 +
               np.arange(3)).ravel().tolist()
    getter = itemgetter(*corners)
    return lambda values: list(getter(values))


def _split_part_spatially(arrays: PartArrays,
                          cell_size: float = RENDER_CELL_SIZE) -> list[SpatialPart]:


    if not arrays.world_positions:
        return [SpatialPart((0, 0, 0), arrays,
                            _cell_origin((0, 0, 0), cell_size))]

    positions = np.asarray(arrays.world_positions, dtype=np.float64).reshape(-1, 3)
    bounds_min = [float(value) for value in positions.min(axis=0)]
    bounds_max = [float(value) for value in positions.max(axis=0)]
    epsilon = max(1e-9, cell_size * 1e-9)

    bounds_cells = []
    for axis in range(3):
        if bounds_max[axis] - bounds_min[axis] <= epsilon:
            first = last = int(math.floor(
                ((bounds_min[axis] + bounds_max[axis]) * 0.5) / cell_size))
        else:
            first = int(math.floor(bounds_min[axis] / cell_size))


            last = int(math.floor((bounds_max[axis] - epsilon) / cell_size))
        bounds_cells.append((first, max(first, last)))
    if all(first == last for first, last in bounds_cells):
        cell = tuple(first for first, _last in bounds_cells)
        origin = _cell_origin(cell, cell_size)
        if _origin_reach(positions, origin) <= \
                cell_size * 0.5 + 1e-5:
            return [SpatialPart(cell, arrays, origin)]

    # Triangles lying wholly inside one cell come through clipping unchanged (or are
    # dropped whole), so they are classified in bulk; only straddling ones are clipped.
    triangles = positions.reshape(-1, 3, 3)
    low = triangles.min(axis=1)
    high = triangles.max(axis=1)
    flat = (high - low) <= epsilon
    first = np.floor(low / cell_size)
    last = np.maximum(first, np.floor((high - epsilon) / cell_size))
    cells = np.where(flat, np.floor(((low + high) * 0.5) / cell_size), first)
    lower = cells * cell_size
    upper = lower + cell_size
    inside = ((flat | (first == last)).all(axis=1) &
              ((triangles >= lower[:, None, :]) &
               (triangles <= upper[:, None, :])).all(axis=(1, 2)))

    def near(a, b):
        return (np.abs(triangles[:, a] - triangles[:, b]) <= 1e-10).all(axis=1)

    ab = triangles[:, 1] - triangles[:, 0]
    ac = triangles[:, 2] - triangles[:, 0]
    cross_x = ab[:, 1] * ac[:, 2] - ab[:, 2] * ac[:, 1]
    cross_y = ab[:, 2] * ac[:, 0] - ab[:, 0] * ac[:, 2]
    cross_z = ab[:, 0] * ac[:, 1] - ab[:, 1] * ac[:, 0]
    area = (cross_x * cross_x + cross_y * cross_y) + cross_z * cross_z
    keep = inside & ~(near(1, 0) | near(2, 1) | near(0, 2)) & (area > 1e-24)

    if inside.all() and (not keep.any() or np.abs(cells[keep]).max() < 2.0 ** 52):
        kept = np.nonzero(keep)[0]
        output = []
        if len(kept):
            unique_cells, inverse = np.unique(cells[kept].astype(np.int64), axis=0,
                                              return_inverse=True)
            inverse = inverse.reshape(-1)
            order = np.argsort(inverse, kind="stable")
            bounds = np.searchsorted(inverse[order], np.arange(len(unique_cells) + 1))
            for group, cell_row in enumerate(unique_cells):
                members = kept[order[bounds[group]:bounds[group + 1]]]
                pick = _corner_picker(members)
                part = PartArrays(pick(arrays.world_positions),
                                  pick(arrays.world_normals),
                                  pick(arrays.local_positions),
                                  pick(arrays.local_normals),
                                  pick(arrays.uvs), len(members))
                cell = tuple(int(value) for value in cell_row)
                origin = _cell_origin(cell, cell_size)
                if _origin_reach(part.world_positions, origin) > \
                        MAX_RENDER_LOCAL_REACH + 1e-5:
                    raise RuntimeError(
                        f"spatial partition {cell} exceeds the {MAX_RENDER_LOCAL_REACH:g} m "
                        "native local-coordinate bound")
                output.append(SpatialPart(cell, part, origin))
        if not output:
            raise RuntimeError("spatial partitioning removed every source triangle")
        return output

    buckets = {}

    for offset in range(0, len(arrays.world_positions), 3):
        triangle_index = offset // 3
        if inside[triangle_index]:
            if keep[triangle_index]:
                target = buckets.setdefault(
                    tuple(int(value) for value in cells[triangle_index]),
                    [[], [], [], [], []])
                target[0].extend(arrays.world_positions[offset:offset + 3])
                target[1].extend(arrays.world_normals[offset:offset + 3])
                target[2].extend(arrays.local_positions[offset:offset + 3])
                target[3].extend(arrays.local_normals[offset:offset + 3])
                target[4].extend(arrays.uvs[offset:offset + 3])
            continue
        polygon = [
            _Corner(arrays.world_positions[offset + index],
                    arrays.world_normals[offset + index],
                    arrays.local_positions[offset + index],
                    arrays.local_normals[offset + index],
                    arrays.uvs[offset + index])
            for index in range(3)
        ]
        fragments = [((), polygon)]
        for axis in range(3):
            next_fragments = []
            for partial_cell, fragment in fragments:
                low_value = min(c.world_position[axis] for c in fragment)
                high_value = max(c.world_position[axis] for c in fragment)
                if high_value - low_value <= epsilon:
                    cell_index = int(math.floor(
                        ((low_value + high_value) * 0.5) / cell_size))
                    first = last = cell_index
                else:
                    first = int(math.floor(low_value / cell_size))


                    last = int(math.floor((high_value - epsilon) / cell_size))
                    last = max(first, last)
                for cell_index in range(first, last + 1):
                    lower = cell_index * cell_size
                    upper = lower + cell_size
                    clipped = _clip_polygon(fragment, axis, lower, True)
                    clipped = _clip_polygon(clipped, axis, upper, False)
                    if len(clipped) >= 3:
                        next_fragments.append(
                            (partial_cell + (cell_index,), clipped))
            fragments = next_fragments
            if not fragments:
                break

        for cell, polygon in fragments:
            target = buckets.setdefault(cell, [[], [], [], [], []])
            for index in range(1, len(polygon) - 1):
                triangle = (polygon[0], polygon[index], polygon[index + 1])
                if _triangle_area_squared(*triangle) <= 1e-24:
                    continue
                for corner in triangle:
                    target[0].append(corner.world_position)
                    target[1].append(corner.world_normal)
                    target[2].append(corner.local_position)
                    target[3].append(corner.local_normal)
                    target[4].append(corner.uv)

    output = []
    for cell in sorted(buckets):
        values = buckets[cell]
        if not values[0]:
            continue
        part = PartArrays(values[0], values[1], values[2], values[3],
                          values[4], len(values[0]) // 3)
        origin = _cell_origin(cell, cell_size)
        if _origin_reach(part.world_positions, origin) > \
                MAX_RENDER_LOCAL_REACH + 1e-5:
            raise RuntimeError(
                f"spatial partition {cell} exceeds the {MAX_RENDER_LOCAL_REACH:g} m "
                "native local-coordinate bound")
        output.append(SpatialPart(cell, part, origin))
    if not output:
        raise RuntimeError("spatial partitioning removed every source triangle")
    return output


def _triangle_spatial_cell_count(points,
                                 cell_size: float = RENDER_CELL_SIZE) -> int | None:

    if len(points) != 3 or not math.isfinite(cell_size) or cell_size <= 0.0:
        return None
    epsilon = max(1e-9, cell_size * 1e-9)
    total = 1
    for axis in range(3):
        values = [float(point[axis]) for point in points]
        if not all(math.isfinite(value) for value in values):
            return None
        low_value, high_value = min(values), max(values)
        if high_value - low_value <= epsilon:
            cells = 1
        else:
            first = int(math.floor(low_value / cell_size))
            last = int(math.floor((high_value - epsilon) / cell_size))
            cells = max(1, last - first + 1)
        total *= cells
        if total > MAX_TRIANGLE_SPATIAL_CELLS:
            return total
    return total


def _source_triangle_spatial_cell_count(mesh, triangle, world: Matrix,
                                        scale: float) -> int | None:
    points = []
    for source_index in triangle.vertices:
        point = world @ mesh.vertices[source_index].co
        points.append(tuple(float(component) * scale for component in point))
    return _triangle_spatial_cell_count(points)


def _split_part_for_native_index_width(
        part: SpatialPart,
        max_vertices: int = NATIVE_SECTION_MAX_VERTICES) -> list[SpatialPart]:


    arrays = part.arrays
    lengths = {len(arrays.world_positions), len(arrays.world_normals),
               len(arrays.local_positions), len(arrays.local_normals),
               len(arrays.uvs)}
    if len(lengths) != 1:
        raise ValueError("render corner attribute arrays have different lengths")
    vertex_count = len(arrays.world_positions)
    if vertex_count % 3:
        raise ValueError(
            f"render corner stream has {vertex_count} vertices, not whole triangles")
    if arrays.triangle_count != vertex_count // 3:
        raise ValueError(
            "render triangle count does not match the corner attribute streams")
    if max_vertices < 3:
        raise ValueError("native section vertex limit must fit at least one triangle")


    vertices_per_part = max_vertices - max_vertices % 3
    if vertex_count <= vertices_per_part:
        return [part]

    output = []
    for start in range(0, vertex_count, vertices_per_part):
        stop = min(vertex_count, start + vertices_per_part)
        split = PartArrays(
            arrays.world_positions[start:stop],
            arrays.world_normals[start:stop],
            arrays.local_positions[start:stop],
            arrays.local_normals[start:stop],
            arrays.uvs[start:stop],
            (stop - start) // 3,
        )
        output.append(SpatialPart(part.cell, split, part.origin))
    return output


def _corner_normal(mesh, loop_index):
    try:
        return Vector(mesh.corner_normals[loop_index].vector)
    except Exception:
        try:
            return Vector(mesh.loops[loop_index].normal)
        except Exception:
            return Vector((0.0, 0.0, 1.0))


def _repair_normal_streams(positions, world_normals, local_normals):
    """Operate before conversion to Python rows; bounded temporary working arrays."""
    repaired = 0
    for start in range(0, len(positions), 3 * 32768):
        end = min(len(positions), start + 3 * 32768)
        points = positions[start:end].reshape(-1, 3, 3)
        u, v = points[:, 1] - points[:, 0], points[:, 2] - points[:, 0]
        face = np.stack((u[:, 1] * v[:, 2] - u[:, 2] * v[:, 1],
                         u[:, 2] * v[:, 0] - u[:, 0] * v[:, 2],
                         u[:, 0] * v[:, 1] - u[:, 1] * v[:, 0]), axis=1)
        # Existing tuple rows contain Python floats. Promote float32 normals before
        # multiplication, preserving the original float64 dot-product order.
        normals = world_normals[start:end].reshape(-1, 3, 3)
        dot = np.zeros(normals.shape[:2], dtype=np.float64)
        magnitude = np.zeros_like(dot)
        for axis in range(3):
            term = face[:, None, axis] * normals[:, :, axis].astype(np.float64)
            dot = dot + term
            magnitude = magnitude + np.abs(term)
        nondegenerate = np.any(face != 0.0, axis=1)[:, None]
        flip = (dot < 0.0) & nondegenerate
        # Python 3.12+ uses compensated float sum. Resolve cancellation/nonfinite
        # cases with the running interpreter's scalar sum, matching the old code
        # even across Blender's different bundled Python versions.
        uncertain = nondegenerate & ((np.abs(dot) <= magnitude * (8 * np.finfo(np.float64).eps)) |
                                     ~np.isfinite(dot) | ~np.isfinite(magnitude))
        for triangle, corner in np.argwhere(uncertain):
            flip[triangle, corner] = sum(float(face[triangle, axis]) *
                                         float(normals[triangle, corner, axis])
                                         for axis in range(3)) < 0.0
        flip = flip.ravel()
        indices = np.flatnonzero(flip) + start
        world_normals[indices] *= -1
        local_normals[indices] *= -1
        repaired += len(indices)
    return repaired


def _repair_opposed_corner_normals(arrays: PartArrays) -> int:


    prepared = getattr(arrays, "_prepared_normal_repairs", None)
    if prepared is not None:
        del arrays._prepared_normal_repairs
        return prepared
    repaired = 0
    positions = arrays.world_positions
    for start in range(0, len(positions), 3):
        a, b, c = positions[start:start + 3]
        ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
        face = (uy * vz - uz * vy,
                uz * vx - ux * vz,
                ux * vy - uy * vx)
        if face == (0.0, 0.0, 0.0):
            continue
        for index in range(start, start + 3):
            normal = arrays.world_normals[index]
            if sum(face[axis] * normal[axis] for axis in range(3)) < 0.0:
                arrays.world_normals[index] = tuple(-value for value in normal)
                arrays.local_normals[index] = tuple(
                    -value for value in arrays.local_normals[index])
                repaired += 1
    return repaired


def _foreach(collection, attribute, count, dtype):
    values = np.empty(count, dtype=dtype)
    collection.foreach_get(attribute, values)
    return values


def _mathutils_transform(matrix, points):
    """Matrix @ Vector for many points, reproducing mathutils bit for bit:
    float32 products accumulated in double, rounded once to float32."""
    rows = np.array(matrix, dtype=np.float32)
    result = np.empty((len(points), 3), dtype=np.float32)
    for row in range(3):
        total = 0.0 + (rows[row, 0] * points[:, 0]).astype(np.float64)
        total = total + (rows[row, 1] * points[:, 1]).astype(np.float64)
        total = total + (rows[row, 2] * points[:, 2]).astype(np.float64)
        if rows.shape[1] == 4:
            total = total + np.float64(rows[row, 3])
        result[:, row] = total.astype(np.float32)
    return result


def _mathutils_world_normals(normal_matrix, normals):
    """normal_matrix @ n, then Vector.normalize() when length_squared > 1e-20, else +Z,
    matching mathutils' double-precision length and float32 scaling exactly."""
    vectors = _mathutils_transform(normal_matrix, normals)
    length_squared = np.zeros(len(vectors), dtype=np.float64)
    for axis in (2, 1, 0):
        component = vectors[:, axis].astype(np.float64)
        length_squared = length_squared + component * component
    usable = length_squared > 1e-20
    with np.errstate(divide="ignore", invalid="ignore"):
        scale = np.float32(1.0) / np.sqrt(length_squared).astype(np.float32)
        result = (vectors * scale[:, None]).astype(np.float32)
    result[~usable] = np.array((0.0, 0.0, 1.0), dtype=np.float32)
    return result


def _rows(values) -> list:
    return list(map(tuple, values.tolist()))


class _MeshArrays:
    """Bulk arrays of one evaluated mesh, read once instead of per corner."""

    def __init__(self, mesh, world: Matrix, scale: float):
        triangles = mesh.loop_triangles
        count = len(triangles)
        self.triangle_loops = _foreach(triangles, "loops", count * 3, np.int32).reshape(-1, 3)
        self.triangle_vertices = _foreach(triangles, "vertices", count * 3, np.int32).reshape(-1, 3)
        self.triangle_materials = _foreach(triangles, "material_index", count, np.int32)
        self.local_positions = _foreach(mesh.vertices, "co", len(mesh.vertices) * 3,
                                        np.float32).reshape(-1, 3)
        self.world_positions = _mathutils_transform(
            world, self.local_positions).astype(np.float64) * scale
        self.loop_vertices = _foreach(mesh.loops, "vertex_index", len(mesh.loops), np.int32)
        self.normal_matrix = world.to_3x3().inverted_safe().transposed()
        try:
            self.corner_normals = _foreach(mesh.corner_normals, "vector",
                                           len(mesh.loops) * 3, np.float32).reshape(-1, 3)
        except Exception:
            self.corner_normals = None
        layer = mesh.uv_layers.active
        self.uvs = (_foreach(layer.data, "uv", len(layer.data) * 2, np.float32).reshape(-1, 2)
                    if layer is not None else None)
        self._colour_cache = {}
        self.mesh = mesh
        # Decal layers sample their own UV maps.
        self.uv_layers = {}
        for other in mesh.uv_layers:
            if other == layer:
                self.uv_layers[other.name] = self.uvs
            else:
                self.uv_layers[other.name] = _foreach(other.data, "uv", len(other.data) * 2,
                                                      np.float32).reshape(-1, 2)


def _corner_colour_channel(data: _MeshArrays, name: str, channel: int):
    """One channel of a colour attribute per face corner (0..1), or None when absent."""
    key = (name, channel)
    if key in data._colour_cache:
        return data._colour_cache[key]
    mesh = data.mesh
    attribute = None
    try:
        attributes = mesh.color_attributes
        attribute = attributes.get(name) if name else (attributes.active_color or
                                                       (attributes[0] if len(attributes) else None))
    except Exception:
        attribute = None
    result = None
    if attribute is not None:
        try:
            count = len(attribute.data)
            values = _foreach(attribute.data, "color", count * 4, np.float32).reshape(-1, 4)
            values = values[:, min(max(channel, 0), 3)]
            if attribute.domain == "POINT":
                values = values[data.loop_vertices]
            result = values
        except Exception:
            result = None
    data._colour_cache[key] = result
    return result


def _decal_triangles(data: _MeshArrays, triangles, vertex_mask):
    """The triangles of `triangles` a decal layer can show on: those whose corners average at
    least 0.4 in the vertex-colour channel gating it (a soft vertex blend becomes a hard cut
    near its midpoint; the native decal has no per-vertex weight); every triangle when there
    is no gate."""
    if vertex_mask is None or data is None or data.corner_normals is None:
        return triangles
    values = _corner_colour_channel(data, vertex_mask[0], vertex_mask[1])
    if values is None:
        return triangles
    index = np.asarray(triangles, dtype=np.int64)
    corners = values[data.triangle_loops[index]]
    keep = corners.mean(axis=1) >= 0.4
    return index[keep]


def _triangle_cell_counts(data: _MeshArrays, cell_size: float = RENDER_CELL_SIZE):
    """_triangle_spatial_cell_count for every loop triangle; None is returned as -1."""
    points = data.world_positions[data.triangle_vertices]
    result = np.full(len(points), -1.0)
    if not len(points):
        return result
    epsilon = max(1e-9, cell_size * 1e-9)
    with np.errstate(invalid="ignore", over="ignore"):
        low = points.min(axis=1)
        high = points.max(axis=1)
        first = np.floor(low / cell_size)
        last = np.floor((high - epsilon) / cell_size)
        cells = np.where(high - low <= epsilon, 1.0, np.maximum(1.0, last - first + 1.0))
    finite = np.isfinite(points).all(axis=1)
    open_ = np.ones(len(points), dtype=bool)
    total = np.ones(len(points))
    for axis in range(3):
        result[open_ & ~finite[:, axis]] = -1.0
        open_ &= finite[:, axis]
        total = np.where(open_, total * cells[:, axis], total)
        exceeded = open_ & (total > MAX_TRIANGLE_SPATIAL_CELLS)
        result[exceeded] = total[exceeded]
        open_ &= ~exceeded
    result[open_] = total[open_]
    # Beyond 2^40 m the float arithmetic above could round; use the exact path there.
    extreme = np.nonzero(np.isfinite(points).all(axis=(1, 2)) &
                         (np.abs(points).max(axis=(1, 2)) > 2.0 ** 40))[0]
    for index in extreme:
        count = _triangle_spatial_cell_count([tuple(point) for point in points[index].tolist()])
        result[index] = -1.0 if count is None else float(count)
    return result


def _extract_part(mesh, triangles, world: Matrix, scale: float,
                  reverse_winding: bool, data: _MeshArrays | None = None,
                  uv_layer: str | None = None,
                  repair_normals: bool = False) -> PartArrays:
    if data is None or data.corner_normals is None:
        return _extract_part_slow(mesh, [mesh.loop_triangles[int(index)] for index in triangles],
                                  world, scale, reverse_winding, uv_layer=uv_layer)
    loops = data.triangle_loops[np.asarray(triangles, dtype=np.int64)]
    if reverse_winding:
        loops = loops[:, [0, 2, 1]]
    loops = loops.ravel()
    vertices = data.loop_vertices[loops]
    local_normals = data.corner_normals[loops]
    source_uvs = data.uv_layers.get(uv_layer) if uv_layer else None
    if source_uvs is None:
        source_uvs = data.uvs
    uvs = (source_uvs[loops] if source_uvs is not None
           else np.zeros((len(loops), 2), dtype=np.float32))
    world_positions = data.world_positions[vertices]
    world_normals = _mathutils_world_normals(data.normal_matrix, local_normals)
    if repair_normals:
        with _perf_phase("normal_repair_array_kernel"):
            repaired = _repair_normal_streams(world_positions, world_normals, local_normals)
    part = PartArrays(
        _rows(world_positions), _rows(world_normals),
        _rows(data.local_positions[vertices]), _rows(local_normals),
        _rows(uvs), len(loops) // 3)
    if repair_normals:
        part._prepared_normal_repairs = repaired
    return part


# Decal parts float this far above the surface they dress, one step per layer, so
# their triangles never z-fight the base (the city's own decal copies sit about 1 cm up).
DECAL_LIFT = 0.008


def _lift_part(arrays: PartArrays, distance: float) -> PartArrays:
    positions = np.asarray(arrays.world_positions, dtype=np.float64)
    normals = np.asarray(arrays.world_normals, dtype=np.float64)
    if len(positions) == 0:
        return arrays
    return PartArrays(_rows(positions + normals * distance), arrays.world_normals,
                      arrays.local_positions, arrays.local_normals, arrays.uvs,
                      arrays.triangle_count)


def _extract_part_slow(mesh, triangles, world: Matrix, scale: float,
                       reverse_winding: bool, uv_layer: str | None = None) -> PartArrays:
    normal_matrix = world.to_3x3().inverted_safe().transposed()
    source_uv = mesh.uv_layers.get(uv_layer) if uv_layer else None
    source_uv = source_uv if source_uv is not None else mesh.uv_layers.active
    uv_data = source_uv.data if source_uv is not None else None
    wp, wn, lp, ln, uvs = [], [], [], [], []
    for tri in triangles:
        loops = list(tri.loops)
        if reverse_winding:
            loops[1], loops[2] = loops[2], loops[1]
        for loop_index in loops:
            vertex = mesh.vertices[mesh.loops[loop_index].vertex_index]
            local_position = Vector(vertex.co)
            local_normal = _corner_normal(mesh, loop_index)
            world_position = world @ local_position
            world_normal = normal_matrix @ local_normal
            if world_normal.length_squared > 1e-20:
                world_normal.normalize()
            else:
                world_normal = Vector((0.0, 0.0, 1.0))
            wp.append(tuple(float(c) * scale for c in world_position))
            wn.append(tuple(float(c) for c in world_normal))
            lp.append(tuple(float(c) for c in local_position))
            ln.append(tuple(float(c) for c in local_normal))
            uv = uv_data[loop_index].uv if uv_data is not None else (0.0, 0.0)
            uvs.append((float(uv[0]), float(uv[1])))
    return PartArrays(wp, wn, lp, ln, uvs, len(triangles))


def _mesh_from_arrays(name: str, positions, normals, uvs):
    mesh = bpy.data.meshes.new(name)
    count = len(positions)
    faces = [(i, i + 1, i + 2) for i in range(0, count, 3)]
    mesh.from_pydata(positions, [], faces)
    mesh.update(calc_edges=False)
    layer = mesh.uv_layers.new(name="UVMap")
    if len(uvs):
        layer.data.foreach_set("uv", np.asarray(uvs, dtype=np.float32).ravel())
    mesh.polygons.foreach_set("use_smooth", np.ones(len(mesh.polygons), dtype=bool))
    try:
        mesh.normals_split_custom_set(normals)
    except Exception:
        try:
            mesh.normals_split_custom_set_from_vertices(normals)
        except Exception:
            pass
    return mesh


def _collision_arrays_from_source(mesh, world: Matrix, scale: float,
                                  reverse_winding: bool,
                                  triangles=None, data: _MeshArrays | None = None) -> CollisionArrays:
    if data is not None:
        selected = (np.arange(len(data.triangle_vertices)) if triangles is None
                    else np.asarray(triangles, dtype=np.int64))
        source = data.triangle_vertices[selected]
        if reverse_winding:
            source = source[:, [0, 2, 1]]
        flat = source.ravel()
        if not len(flat):
            return CollisionArrays((), (), 0)
        # Vertices are numbered in order of first use, as the per-corner loop did.
        unique, first_use, inverse = np.unique(flat, return_index=True, return_inverse=True)
        order = np.argsort(first_use, kind="stable")
        rank = np.empty(len(order), dtype=np.int64)
        rank[order] = np.arange(len(order))
        return CollisionArrays(tuple(_rows(data.world_positions[unique[order]])),
                               tuple(rank[inverse.reshape(-1)].tolist()),
                               len(flat) // 3)
    if triangles is not None:
        triangles = [mesh.loop_triangles[int(index)] for index in triangles]

    used = {}
    positions = []
    indices = []
    for triangle in mesh.loop_triangles if triangles is None else triangles:
        source_indices = list(triangle.vertices)
        if reverse_winding:
            source_indices[1], source_indices[2] = source_indices[2], source_indices[1]
        for source_index in source_indices:
            if source_index not in used:
                point = world @ mesh.vertices[source_index].co
                used[source_index] = len(positions)
                positions.append(tuple(float(c) * scale for c in point))
            indices.append(used[source_index])
    return CollisionArrays(tuple(positions), tuple(indices),
                           len(indices) // 3)


def _collision_topology_signature(arrays: CollisionArrays) -> tuple:

    used = sorted(set(int(index) for index in arrays.indices))
    if not used:
        return 0, 0, 0
    parent = {index: index for index in used}

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left, right):
        left, right = find(left), find(right)
        if left != right:
            parent[right] = left

    edges = {}
    for offset in range(0, len(arrays.indices), 3):
        triangle = tuple(int(index) for index in arrays.indices[offset:offset + 3])
        for left, right in ((triangle[0], triangle[1]),
                            (triangle[1], triangle[2]),
                            (triangle[2], triangle[0])):
            union(left, right)
            edge = (left, right) if left < right else (right, left)
            edges[edge] = edges.get(edge, 0) + 1
    components = len({find(index) for index in used})

    boundary_edges = [edge for edge, count in edges.items() if count == 1]
    boundary_parent = {index: index for edge in boundary_edges for index in edge}

    def boundary_find(index):
        while boundary_parent[index] != index:
            boundary_parent[index] = boundary_parent[boundary_parent[index]]
            index = boundary_parent[index]
        return index

    for left, right in boundary_edges:
        left_root, right_root = boundary_find(left), boundary_find(right)
        if left_root != right_root:
            boundary_parent[right_root] = left_root
    boundary_components = len({boundary_find(index) for index in boundary_parent})
    euler_characteristic = len(used) - len(edges) + arrays.triangle_count
    return components, boundary_components, euler_characteristic


def _initialize_collision_stats(stats):

    for key in ("collision_meshes", "collision_triangles",
                "collision_source_triangles", "collision_mesh_source_triangles",
                "collision_lod_meshes", "collision_lod_topology_rejections",
                "collision_lod_triangles_removed",
                "collision_mesh_output_triangles"):
        stats.setdefault(key, 0)
    stats.setdefault("collision_lod_triangle_budget", None)


def _submit_collision_job(writes: GlbWriteQueue, job, stats):


    _initialize_collision_stats(stats)
    arrays = job.arrays
    stats["collision_source_triangles"] += arrays.triangle_count
    if job.mode in {"triangle_mesh", "water"}:
        stats["collision_mesh_source_triangles"] += arrays.triangle_count
    stats["collision_mesh_output_triangles"] = (
        stats["collision_mesh_source_triangles"] -
        stats["collision_lod_triangles_removed"])

    def collision_written(count=arrays.triangle_count):
        stats["collision_meshes"] += 1
        stats["collision_triangles"] += count

    writes.submit(job.destination, arrays.positions, indices=arrays.indices,
                  name=job.name, success=collision_written)


def _submit_collision_jobs(writes: GlbWriteQueue, jobs, stats, diagnostics):

    _initialize_collision_stats(stats)
    for job in jobs:
        _submit_collision_job(writes, job, stats)


class _LazyBakeObject:
    """Material inspection needs a name, but only baking needs a Blender mesh."""
    def __init__(self, name, mesh_name, positions, normals, uvs, matrix=None):
        self._name = name
        self.mesh_name = mesh_name
        self.positions, self.normals, self.uvs = positions, normals, uvs
        self.matrix = matrix
        self.obj = None

    @property
    def name(self):
        return self.obj.name if self.obj is not None else self._name

    def get(self):
        if self.obj is None:
            mesh = _mesh_from_arrays(self.mesh_name, self.positions, self.normals, self.uvs)
            self.obj = _temporary_object(self._name, mesh, self.matrix)
        return self.obj

    def close(self):
        if self.obj is not None:
            _remove_temp_object(self.obj)
            self.obj = None


def _temporary_object(name: str, mesh, matrix=None):
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    if matrix is not None:
        obj.matrix_world = matrix
    return obj


_TEMP_TRASH: list = []
_TEMP_TRASH_BATCH = 256


def _remove_temp_object(obj):
    if isinstance(obj, _LazyBakeObject):
        obj.close()
        return
    # Deleting an ID scans every ID in the file, so each temp object is unlinked and
    # renamed out of the way now (the scene and later temp names stay as before)
    # and deleted in batches.
    mesh = getattr(obj, "data", None)
    for collection in list(obj.users_collection):
        collection.objects.unlink(obj)
    serial = len(_TEMP_TRASH)
    obj.name = f"ReSkateTrashObject{serial}"
    _TEMP_TRASH.append(obj)
    if mesh is not None:
        mesh.name = f"ReSkateTrashMesh{serial}"
        _TEMP_TRASH.append(mesh)
    if len(_TEMP_TRASH) >= _TEMP_TRASH_BATCH:
        _flush_temp_objects()


def _flush_temp_objects():
    if not _TEMP_TRASH:
        return
    trash = list(_TEMP_TRASH)
    _TEMP_TRASH.clear()
    try:
        bpy.data.batch_remove(trash)
    except Exception:
        for item in trash:
            try:
                if isinstance(item, bpy.types.Object):
                    bpy.data.objects.remove(item, do_unlink=True)
                else:
                    bpy.data.meshes.remove(item)
            except Exception:
                pass


def _material_at(mesh, index, original=None):
    # to_mesh() carries datablock materials, not OBJECT-linked slot overrides.
    if original is not None and index < len(original.material_slots):
        slot = original.material_slots[index]
        if slot.link == 'OBJECT':
            return slot.material
    try:
        return mesh.materials[index]
    except Exception:
        return None


def _normalise_collision_mode(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return (COLLISION_MODE_IDS[value]
                if 0 <= value < len(COLLISION_MODE_IDS) else None)
    mode = str(value).strip().lower()
    if mode == "mesh":
        mode = "triangle_mesh"
    return mode if mode in COLLISION_MODES else None


def _normalise_placement_mode(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return (PLACEMENT_MODE_IDS[value]
                if 0 <= value < len(PLACEMENT_MODE_IDS) else None)
    mode = str(value).strip().lower()
    return mode if mode in PLACEMENT_MODES else None


def _object_setting(original, property_name: str, flat_name: str):

    try:
        nested_raw = original.get("sk8_object")
    except Exception:
        nested_raw = None
    if nested_raw is not None:
        try:
            value = nested_raw.get(property_name)
        except Exception:
            value = None
        if value is not None:
            return value
    try:
        nested_rna = getattr(original, "sk8_object", None)
        explicitly_set = (nested_rna is not None and
                          (not hasattr(nested_rna, "is_property_set") or
                           nested_rna.is_property_set(property_name)))
        if explicitly_set:
            return getattr(nested_rna, property_name, None)
    except Exception:
        pass
    try:
        return original.get(flat_name)
    except Exception:
        return None


def _placement_mode(original) -> str:
    return (_normalise_placement_mode(
        _object_setting(original, "placement_mode", "sk8_placement_mode")) or
        "authored_mesh")


def _retail_blueprint(original) -> str:
    value = _object_setting(
        original, "retail_blueprint", "sk8_retail_blueprint")
    return str(value).strip() if value is not None else ""


def _normalise_collision_material(value, *, integer_is_slot=False) -> int | None:


    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        if integer_is_slot:
            candidate = (value << 6) | NATIVE_COLLISION_MATERIAL_FLAG
            return (candidate if 0 <= value < NATIVE_COLLISION_MATERIAL_SLOT_COUNT
                    and candidate in NATIVE_COLLISION_MATERIAL_PACKED_VALUES else None)
        return value if value in NATIVE_COLLISION_MATERIAL_PACKED_VALUES else None

    text = str(value).strip().lower()
    if not text:
        return None
    is_identifier = text.startswith("material_")
    if is_identifier:
        text = text[len("material_"):]
    try:
        packed = int(text, 10) if re.fullmatch(r"[+-]?\d+", text) else int(text, 0)
    except ValueError:
        packed = None
    if packed is not None:
        if integer_is_slot and not is_identifier:
            candidate = (packed << 6) | NATIVE_COLLISION_MATERIAL_FLAG
            return (candidate
                    if 0 <= packed < NATIVE_COLLISION_MATERIAL_SLOT_COUNT and
                    candidate in NATIVE_COLLISION_MATERIAL_PACKED_VALUES else None)
        return packed if packed in NATIVE_COLLISION_MATERIAL_PACKED_VALUES else None
    return None


def _collision_material_identifier(packed: int) -> str:
    return f"material_{packed:04d}"


def _saved_collision_material(owner, nested_property: str) -> int | None:

    if owner is None:
        return None
    try:
        nested_raw = owner.get(nested_property)
    except Exception:
        nested_raw = None
    if nested_raw is not None:
        try:
            packed = _normalise_collision_material(
                nested_raw.get("collision_material"))
        except Exception:
            packed = None
        if packed is not None:
            return packed

    try:
        nested_rna = getattr(owner, nested_property, None)
        explicitly_set = (nested_rna is not None and
                          (not hasattr(nested_rna, "is_property_set") or
                           nested_rna.is_property_set("collision_material")))
        packed = (_normalise_collision_material(
            getattr(nested_rna, "collision_material", None))
                  if explicitly_set else None)
    except Exception:
        packed = None
    if packed is not None:
        return packed

    for key in ("sk8_collision_material_packed", "collision_material_packed"):
        try:
            packed = _normalise_collision_material(owner.get(key))
        except Exception:
            packed = None
        if packed is not None:
            return packed
    return None


def _collision_material(original, material=None) -> int:
    packed = _base_collision_material(original, material)
    if _profile_bool(_profile_raw_value(original, "round_rail", False)):
        return _smooth_grind_variant(packed)
    return packed


def _base_collision_material(original, material=None) -> int:


    packed = _saved_collision_material(material, "sk8_material")
    if packed is not None:
        return packed
    packed = _saved_collision_material(original, "sk8_object")
    if packed is not None:
        return packed


    for key in ("sk8_collision_material", "collision_material"):
        try:
            packed = (_normalise_collision_material(
                original.get(key), integer_is_slot=True)
                      if original is not None else None)
        except Exception:
            packed = None
        if packed is not None:
            return packed
    return DEFAULT_COLLISION_MATERIAL_PACKED


def _profile_raw_value(material, key, default):

    if material is None:
        return default
    try:
        group = "sk8_object" if isinstance(material, bpy.types.Object) else "sk8_material"
        nested = material.get(group)
        if nested is not None and key in nested:
            return nested.get(key)
    except Exception:
        pass
    try:
        settings = getattr(material, group, None)
        if settings is not None:
            if key in settings:
                return settings.get(key)
            return getattr(settings, key)
    except Exception:
        pass
    return default


def _profile_bool(value, default=False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    return default


def _profile_float(material, key, default) -> float:
    value = _profile_raw_value(material, key, default)
    try:
        value = float(value)
    except (TypeError, ValueError):
        return float(default)
    return value if math.isfinite(value) else float(default)


def _surface_profile_from_material(material) -> dict | None:

    if material is None:
        return None
    try:
        raw_json = material.get("sk8_surface_profile_json")
    except Exception:
        raw_json = None
    if raw_json:
        try:
            parsed = json.loads(str(raw_json))
        except (TypeError, ValueError) as ex:
            raise ValueError(
                f"material {material.name!r} has invalid sk8_surface_profile_json: {ex}")
        if not isinstance(parsed, dict):
            raise ValueError(
                f"material {material.name!r} surface profile must be an object")
        profile = dict(parsed)
        profile.pop("name", None)
        profile["format"] = 1
        profile["donor_material_packed"] = _collision_material(None, material)
        return profile

    custom_audio = _profile_bool(
        _profile_raw_value(material, "custom_audio", False))
    custom_friction = _profile_bool(
        _profile_raw_value(material, "custom_friction", False))
    custom_behavior = _profile_bool(
        _profile_raw_value(material, "custom_behavior", False))
    if not any((custom_audio, custom_friction, custom_behavior)):
        return None

    profile = {
        "format": 1,
        "donor_material_packed": _collision_material(None, material),
    }
    if custom_audio:
        profile["audio"] = {
            "softness": _profile_float(material, "audio_softness", 0.2),
            "smoothness": _profile_float(material, "audio_smoothness", 0.25),
            "min_impact_force": _profile_float(
                material, "audio_min_impact_force", 0.1),
            "impact_cooldown_release": _profile_float(
                material, "audio_impact_cooldown_release", 0.25),
            "ignore_player_collisions": _profile_bool(_profile_raw_value(
                material, "audio_ignore_player_collisions", False)),
        }
    if custom_friction:
        profile["friction"] = {
            part: {
                "dynamic": _profile_float(
                    material, f"{part}_dynamic_friction", dynamic),
                "static": _profile_float(
                    material, f"{part}_static_friction", static),
                "restitution": _profile_float(
                    material, f"{part}_restitution", 0.0),
            }
            for part, dynamic, static in (
                ("deck", 0.5, 0.5),
                ("truck", 0.7, 0.8),
                ("wheel", 0.7, 0.8),
            )
        }
    if custom_behavior:
        profile["behavior"] = {
            key: _profile_bool(_profile_raw_value(material, key, False))
            for key in (
                "exclude_from_edge_generation", "include_in_surface_analysis",
                "exclude_from_grinding", "jump_pad", "boost_pad", "wipeout",
                "slide", "do_not_align", "stairs", "camera_occluder")
        }
    return profile


def _surface_profile_id(profile: dict) -> str:
    value = {key: profile[key] for key in sorted(profile) if key != "name"}
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True).encode("ascii")
    return "surface_" + hashlib.sha256(payload).hexdigest()[:20]


def _register_surface_profile(material, surface_profiles: dict, original=None) -> str | None:
    profile = _surface_profile_from_material(material)
    if original is not None:
        profile = _map_export_addon()._combine_surface_profiles(original, material,
            _surface_profile_from_material(original), profile)
    if profile is None:
        return None
    profile_id = _surface_profile_id(profile)
    if profile_id not in surface_profiles:
        stored = dict(profile)
        stored["name"] = original.name if original is not None else material.name
        surface_profiles[profile_id] = stored
    return profile_id


def _collision_groups_by_surface(mesh, grouped, original, surface_profiles):


    contract_by_material = {}
    triangles_by_contract = {}
    for material_index in sorted(grouped):
        material = _material_at(mesh, material_index, original)
        if _collision_mode(original) == "water":
            packed = WATER_COLLISION_MATERIAL_PACKED
            profile_id = None
        else:
            packed = _collision_material(original, material)
            profile_id = _register_surface_profile(material, surface_profiles, original)
        contract = (profile_id or "", packed)
        contract_by_material[material_index] = contract
        triangles_by_contract.setdefault(contract, []).extend(
            grouped[material_index])
    return contract_by_material, triangles_by_contract


def _collision_mode(original) -> str:


    try:
        nested_raw = original.get("sk8_object")
    except Exception:
        nested_raw = None
    if nested_raw is not None:
        try:
            mode = _normalise_collision_mode(nested_raw.get("collision_mode"))
        except Exception:
            mode = None
        if mode is not None:
            return mode

    try:
        nested_rna = getattr(original, "sk8_object", None)
        explicitly_set = (nested_rna is not None and
                          (not hasattr(nested_rna, "is_property_set") or
                           nested_rna.is_property_set("collision_mode")))
        mode = (_normalise_collision_mode(
            getattr(nested_rna, "collision_mode", None))
                if explicitly_set else None)
    except Exception:
        mode = None
    if mode is not None:
        return mode

    for key in ("sk8_collision_mode", "collision_mode"):
        try:
            mode = _normalise_collision_mode(original.get(key))
        except Exception:
            mode = None
        if mode is not None:
            return mode
    try:
        if original.get("sk8_no_collision"):
            return "none"
    except Exception:
        pass
    return "triangle_mesh"


def _load_input(path: Path):
    suffix = path.suffix.lower()
    if suffix == ".blend":
        with _perf_phase("open_blend"):
            bpy.ops.wm.open_mainfile(filepath=str(path), load_ui=False)
    elif suffix == ".fbx":
        bpy.ops.wm.read_factory_settings(use_empty=True)
        with _perf_phase("fbx_import"):
            bpy.ops.import_scene.fbx(filepath=str(path), use_image_search=True)
    else:
        raise ValueError(f"unsupported input '{path.suffix}'; expected .blend or .fbx")
    if not hasattr(bpy.types.Material, "sk8_material"):
        spec = importlib.util.spec_from_file_location("_studio_material_handoff", _RUNTIME_ROOT / "sk8_map_export.py")
        addon = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(addon)
        bpy.utils.register_class(addon.Sk8MaterialSettings)
        bpy.types.Material.sk8_material = bpy.props.PointerProperty(type=addon.Sk8MaterialSettings)


IMPORT_MARKER = ".reskate-studio-map-import"
IMPORT_MARKER_TEXT = "ReSkate Studio map import v1\n"


def _owned_import(directory: Path):
    try:
        marker = (directory / IMPORT_MARKER).read_bytes()
        expected = IMPORT_MARKER_TEXT.encode("utf-8")
        return marker in (expected, expected.replace(b"\n", b"\r\n"))
    except OSError:
        return False


# Everything a published import holds. An owned folder only ever loses these entries; anything
# else in it (a note, a file another tool put there) is left alone. Nothing is ever written
# outside --output: the build happens in <output>\.building-*, and the timings go to
# <output>\performance.json.
PERFORMANCE_FILE = "performance.json"
STAGE_PREFIX = ".building-"
OWNED_ENTRIES = ("map.json", "conversion-report.json", PERFORMANCE_FILE, "meshes", "collision",
                 "textures", "ui")


def _is_link(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(is_junction and is_junction())


def _check_output(destination: Path, source: Path | None = None) -> Path:
    # Refuses an --output that could make the publish step delete something it does not own.
    raw = destination.expanduser()
    if raw.exists() and _is_link(raw):
        raise MapImportError("output_unsafe", f"--output is a link or junction, not a real folder: {raw}",
                             path=str(raw))
    destination = raw.resolve()
    home = Path.home().resolve()
    if destination == Path(destination.anchor) or destination in (home, home.parent):
        raise MapImportError("output_unsafe", f"--output must be a folder of its own, not {destination}",
                             path=str(destination))
    if source is not None and (source == destination or destination in source.parents):
        raise MapImportError("output_unsafe", f"--output must not contain the input scene: {destination}",
                             path=str(destination))
    if destination.exists():
        if not destination.is_dir():
            raise MapImportError("output_not_directory", f"--output exists and is a file: {destination}",
                                 path=str(destination))
        if any(destination.iterdir()) and not _owned_import(destination):
            raise MapImportError(
                "output_not_owned",
                f"--output is not empty and was not made by this converter (no {IMPORT_MARKER} marker): "
                f"{destination}. Use an empty or new folder.", path=str(destination))
    return destination


def _fresh_stage(destination: Path):
    # `destination` has passed _check_output. It is claimed (marker written) before anything
    # else goes in, so every later run can prove the folder is ours.
    try:
        destination.mkdir(parents=True, exist_ok=True)
        if not _owned_import(destination):
            (destination / IMPORT_MARKER).write_bytes(IMPORT_MARKER_TEXT.encode("utf-8"))
        # A stage left by a run that was killed: ours only if it carries the marker too.
        for leftover in destination.glob(STAGE_PREFIX + "*"):
            if leftover.is_dir() and not _is_link(leftover) and _owned_import(leftover):
                shutil.rmtree(leftover, ignore_errors=True)
        # Not tempfile.mkdtemp: since Python 3.13 (Blender 5.x) it gives the folder an owner-only
        # ACL on Windows, and the published map then cannot be read by another account or by an
        # unelevated process when the converter ran elevated. A plain mkdir inherits --output's.
        while True:
            stage = destination / (STAGE_PREFIX + uuid.uuid4().hex[:12])
            try:
                stage.mkdir()
                break
            except FileExistsError:
                continue
    except OSError as ex:
        raise MapImportError("output_write_failed", f"Cannot write to --output {destination}: {ex}",
                             path=str(destination)) from ex
    try:
        (stage / IMPORT_MARKER).write_bytes(IMPORT_MARKER_TEXT.encode("utf-8"))
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return stage, destination


def _publish(stage: Path, destination: Path):
    # Swaps the previous result for the new one, entry by entry, touching only OWNED_ENTRIES.
    if not _owned_import(destination):
        raise MapImportError("output_not_owned", f"--output lost its {IMPORT_MARKER} marker during the run: "
                             f"{destination}", path=str(destination))
    for name in OWNED_ENTRIES:
        if name == PERFORMANCE_FILE:
            continue   # written live by this run
        target = destination / name
        if _is_link(target):
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    for child in stage.iterdir():
        if child.name != IMPORT_MARKER:
            os.replace(child, destination / child.name)
    (stage / IMPORT_MARKER).unlink(missing_ok=True)
    stage.rmdir()


def _foreign_entries(destination: Path) -> list:
    return sorted(child.name for child in destination.iterdir()
                  if child.name not in OWNED_ENTRIES and not child.name.startswith("."))


# The pause-menu map is a straight-down 16:9 view of the level. A scene without its own
# assets/<scene>/map.* gets one rendered here; the framed rectangle is written beside it so the
# compiler places the map camera over exactly what was rendered.
PAUSE_MAP_IMAGE_TYPES = (".png", ".jpg", ".jpeg", ".dds")
PAUSE_MAP_SIZE = (3840, 2160)
PAUSE_MAP_PADDING = 1.1
PAUSE_MAP_MIN_HALF_HEIGHT = 25.0
# Framing ignores objects no map can contain (beyond 100 km) and, in a scene with enough
# objects to judge by, ones more than eight times further from the middle than 95% of them.
# The map's cloud cover is a square mask over the map's width, centred on it, read from its
# alpha: opaque is clear, transparent is cloud. One is made from what the render covers: clear
# over the map and a margin around it, fading into cloud.
PAUSE_MAP_CLOUD_SIZE = 1024
PAUSE_MAP_CLOUD_GRID = 256
PAUSE_MAP_CLOUD_MARGIN = 0.035   # of the map's width, kept clear around what was rendered
PAUSE_MAP_CLOUD_FADE = 0.05      # of the map's width, over which the clouds come in
PAUSE_MAP_WORLD_LIMIT = 100000.0
PAUSE_MAP_MIN_CROWD = 20
PAUSE_MAP_STRAY_FACTOR = 8.0
# The "3d" render's shadows fall as they do in game in the afternoon: BAM's 16:00 visual
# environment (lighting/ve/tod/bam/ve_bam_high_1600) sets SunRotationX 241 (azimuth) and
# SunRotationY 28 (elevation), with no separate shadow sun. Toward the sun in game axes is
# (sin X cos Y, sin Y, cos X cos Y). Workbench takes its light vector in that same layout,
# (x, up, -y) pointing at the light, so under the straight-down camera it is used unchanged.
PAUSE_MAP_SUN_AZIMUTH = 241.0
PAUSE_MAP_SUN_ELEVATION = 28.0


def _pause_map_sun_direction():
    azimuth = math.radians(PAUSE_MAP_SUN_AZIMUTH)
    elevation = math.radians(PAUSE_MAP_SUN_ELEVATION)
    return (math.sin(azimuth) * math.cos(elevation), math.sin(elevation),
            math.cos(azimuth) * math.cos(elevation))


# A decal overlay is a copy of the surface it lies on: "<object>_decal0" beside "<object>"
# (or "<object>_decal0.025" beside "<object>.025").
_PAUSE_MAP_DECAL_NAME = re.compile(r"_decal\d*(\.\d+)?$", re.IGNORECASE)


def _pause_map_left_out(original, names) -> bool:
    # What the pause map's picture leaves out: decals, and water.
    #
    # Decals are overlays (stains, markings, edge wear) drawn over the surfaces that matter on
    # a map. Names alone do not say so: whole scenes are imported from a mesh called
    # "..._decal0", surfaces mention their decal layer ("sur_Rock_..._decal_Other_...") and
    # blended materials are mostly foliage. So a name counts only beside the object it copies,
    # and a material only when it is set to the decal domain.
    #
    # The ocean shader is a sheet of water over (and far beyond) the map: it would hide what
    # is under it and stretch the frame out to its edges.
    match = _PAUSE_MAP_DECAL_NAME.search(original.name)
    if match:
        base = original.name[:match.start()]
        if base in names or (match.group(1) and base + match.group(1) in names):
            return True
    materials = [slot.material for slot in original.material_slots if slot.material is not None]
    if not materials:
        return False
    if all(_material_invisible(material) for material in materials):
        return True   # collision only: nothing of it is drawn in game
    try:
        domains = {_material_render_metadata(material).get("domain") for material in materials}
        return domains <= {"decal"} or domains <= {"ocean"}
    except Exception:
        return False


def _pause_map_hidden(original) -> bool:
    # Hidden from the map overview by hand (the object's "Hide From Map Overview" setting).
    try:
        return bool(_object_setting(original, "hide_from_pause_map", "sk8_hide_from_pause_map"))
    except Exception:
        return False


def _pause_map_base_node(socket):
    # The image node nearest the Base Color socket, through colour inputs before values (a
    # mix's factor is often a mask image) and never through vectors (UV warps).
    if socket is None or not socket.is_linked:
        return None
    queue = [link.from_node for link in socket.links]
    seen = set()
    while queue:
        node = queue.pop(0)
        if node is None or int(node.as_pointer()) in seen:
            continue
        seen.add(int(node.as_pointer()))
        if node.type == "TEX_IMAGE" and getattr(node, "image", None) is not None:
            return node
        inputs = [value for value in node.inputs
                  if getattr(value, "enabled", True) and value.is_linked and value.type in ("RGBA", "VALUE")]
        inputs.sort(key=lambda value: value.type != "RGBA")
        queue.extend(link.from_node for value in inputs for link in value.links)
    return None


def _show_base_colours(materials) -> list:
    # Workbench's texture colour draws each material's active image node: whichever was last
    # clicked in the shader editor (often the normal map), not the one giving Base Color. For
    # the render, every material shows its base colour image; one without shows its Base
    # Color value rather than some other image. Returns what _restore_base_colours undoes.
    undo = []
    for material in materials:
        try:
            if not _uses_nodes(material):
                continue  # Workbench and the export both use its viewport colour
            nodes = material.node_tree.nodes
            principled = _active_principled(material)
            socket = principled.inputs.get("Base Color") if principled is not None else None
            base = _pause_map_base_node(socket)
            others = [] if base is not None else [
                node for node in nodes if node.type == "TEX_IMAGE" and getattr(node, "image", None) is not None]
            colour = None
            if base is None and socket is not None and not socket.is_linked:
                colour = tuple(socket.default_value)[:4]
            if base is None and not others and colour is None:
                continue
            undo.append((material, nodes.active, tuple(material.diffuse_color),
                         [(node, node.image) for node in others]))
            if base is not None:
                nodes.active = base
            for node in others:
                node.image = None
            if colour is not None:
                material.diffuse_color = colour + (1.0,) * (4 - len(colour))
        except (AttributeError, ReferenceError, TypeError, ValueError):
            continue
    return undo


def _restore_base_colours(undo):
    for material, active, colour, images in reversed(undo):
        try:
            for node, image in images:
                node.image = image
            material.diffuse_color = colour
            if active is not None:
                material.node_tree.nodes.active = active
        except (AttributeError, ReferenceError, TypeError, ValueError):
            continue


def _box_blur(values: np.ndarray, radius: int) -> np.ndarray:
    if radius <= 0:
        return values
    for axis in (0, 1):
        padded = np.pad(values, [(radius, radius) if a == axis else (0, 0) for a in (0, 1)], mode="edge")
        total = np.cumsum(padded, axis=axis, dtype=np.float64)
        total = np.insert(total, 0, 0.0, axis=axis)
        window = 2 * radius + 1
        upper = np.take(total, np.arange(window, total.shape[axis]), axis=axis)
        lower = np.take(total, np.arange(0, total.shape[axis] - window), axis=axis)
        values = ((upper - lower) / window).astype(np.float32)
    return values


def _finish_pause_map(picture: Path, clouds: Path):
    # The render is transparent wherever there is no map, as the game's own map pictures are.
    # That coverage also becomes the cloud mask.
    image = bpy.data.images.load(str(picture), check_existing=False)
    try:
        width, height = image.size
        pixels = np.empty(width * height * 4, dtype=np.float32)
        image.pixels.foreach_get(pixels)
    finally:
        bpy.data.images.remove(image)
    pixels = pixels.reshape(height, width, 4)[::-1]  # Blender stores rows bottom-up

    # Coverage on a coarse grid over the square the mask spans: the map's width, centred.
    grid = PAUSE_MAP_CLOUD_GRID
    rows = max(1, round(grid * height / width))
    ys = (np.arange(rows) * height // rows)
    xs = (np.arange(grid) * width // grid)
    step_y, step_x = max(1, height // rows), max(1, width // grid)
    covered = np.zeros((rows, grid), dtype=np.float32)
    for dy in range(0, step_y, max(1, step_y // 4)):
        for dx in range(0, step_x, max(1, step_x // 4)):
            sample = pixels[np.minimum(ys + dy, height - 1)][:, np.minimum(xs + dx, width - 1), 3]
            covered = np.maximum(covered, sample)
    square = np.zeros((grid, grid), dtype=np.float32)
    top = (grid - rows) // 2
    square[top:top + rows] = (covered > 0.5).astype(np.float32)
    # Grow the clear area by the margin (a blur of a 0/1 field, thresholded low, dilates it),
    # then soften its edge.
    margin = max(1, round(grid * PAUSE_MAP_CLOUD_MARGIN))
    clear = (_box_blur(_box_blur(square, margin), margin) > 0.02).astype(np.float32)
    fade = max(1, round(grid * PAUSE_MAP_CLOUD_FADE / 2))
    clear = _box_blur(_box_blur(clear, fade), fade)
    size = PAUSE_MAP_CLOUD_SIZE
    index = np.linspace(0.0, grid - 1.0, size)
    low = np.floor(index).astype(np.int64)
    high = np.minimum(low + 1, grid - 1)
    weight = (index - low).astype(np.float32)
    rows_mix = clear[low] * (1.0 - weight)[:, None] + clear[high] * weight[:, None]
    full = rows_mix[:, low] * (1.0 - weight)[None, :] + rows_mix[:, high] * weight[None, :]
    mask = np.zeros((size, size, 4), dtype=np.float32)
    mask[:, :, 3] = np.clip(full, 0.0, 1.0)
    clouds.write_bytes(_rgba_png(size, size, _unit_bytes(mask).tobytes()))


def _write_rgba_png(path: Path, pixels: np.ndarray):
    # pixels: (height, width, 4) uint8, top row first. Level 6: a 4K picture at level 9 is slow.
    height, width = pixels.shape[:2]
    raw = np.concatenate((np.zeros((height, 1), dtype=np.uint8),
                          pixels.reshape(height, width * 4)), axis=1).tobytes()
    path.write_bytes(b"\x89PNG\r\n\x1a\n"
                     + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
                     + _png_chunk(b"IDAT", zlib.compress(raw, 6))
                     + _png_chunk(b"IEND", b""))


def _read_render_png(path: Path) -> np.ndarray:
    # Returns (height, width, 4) uint8, top row first. An 8-bit PNG's pixels come back from
    # Blender as stored (no colour management), so the round trip is exact.
    image = bpy.data.images.load(str(path), check_existing=False)
    try:
        width, height = image.size
        values = np.empty(width * height * 4, dtype=np.float32)
        image.pixels.foreach_get(values)
    finally:
        bpy.data.images.remove(image)
    return _unit_bytes(values).reshape(height, width, 4)[::-1]


PAUSE_MAP_TILE_OVERLAP = 32   # pixels rendered past each tile edge and cropped: cavity and AA


def _render_pause_map_tiled(scene, camera, centre_x, centre_y, half_width, half_height,
                            size, tiles: int, target: Path, label: str):
    # Renders the frame as tiles x tiles separate Workbench renders, each a small orthographic
    # view of its part of the map, and stitches them. Each GPU frame then covers 1/tiles^2 of
    # the pixels and only the geometry in (or shadowing into) that part: one 4K frame of the
    # whole city with shadow volumes can run past Windows' GPU watchdog (TDR) or out of video
    # memory, and either kills Blender outright.
    width, height = size
    metres_per_pixel = (half_width * 2.0) / width
    render = scene.render
    picture = np.zeros((height, width, 4), dtype=np.uint8)
    xs = [round(i * width / tiles) for i in range(tiles + 1)]
    ys = [round(i * height / tiles) for i in range(tiles + 1)]
    overlap = PAUSE_MAP_TILE_OVERLAP
    scratch = target.parent / ".pause_map_tiles"
    scratch.mkdir(parents=True, exist_ok=True)
    left = centre_x - half_width
    top = centre_y + half_height
    count = tiles * tiles
    try:
        for row in range(tiles):
            for column in range(tiles):
                index = row * tiles + column + 1
                _emit_studio_progress({"progress": round(0.87 + 0.10 * (index - 1) / count, 4), "phase": "pause_map",
                                       "message": f"Rendering pause-map tile {index}/{count} ({label})"})
                x0, x1 = xs[column], xs[column + 1]
                y0, y1 = ys[row], ys[row + 1]
                # The tile plus its overlap, clamped to nothing: the overlap past the map's
                # edge is simply cropped away like the rest.
                rx0, rx1 = x0 - overlap, x1 + overlap
                ry0, ry1 = y0 - overlap, y1 + overlap
                tile_w, tile_h = rx1 - rx0, ry1 - ry0
                render.resolution_x, render.resolution_y = tile_w, tile_h
                camera.data.sensor_fit = "AUTO"
                camera.data.ortho_scale = max(tile_w, tile_h) * metres_per_pixel
                camera.location.x = left + (rx0 + rx1) * 0.5 * metres_per_pixel
                camera.location.y = top - (ry0 + ry1) * 0.5 * metres_per_pixel
                tile_path = scratch / f"tile_{row:02d}_{column:02d}.png"
                render.filepath = str(tile_path)
                bpy.ops.render.render(write_still=True)
                if not tile_path.is_file():
                    raise RuntimeError(f"pause-map tile {index}/{count} was not written")
                pixels = _read_render_png(tile_path)
                picture[y0:y1, x0:x1] = pixels[overlap:overlap + (y1 - y0), overlap:overlap + (x1 - x0)]
                tile_path.unlink(missing_ok=True)
        _write_rgba_png(target, picture)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _render_pause_map(scene, instances, source: Path, stage: Path, scale: float,
                      style: str, diagnostics: Diagnostics, size=PAUSE_MAP_SIZE,
                      textured: bool = True, tiles: int = 4,
                      texture_limit: str = "CLAMP_512", samples: str = "8") -> bool:
    authored = source.parent / "assets" / source.stem
    if _pause_map_authored(source):
        return False
    low = [math.inf] * 3
    high = [-math.inf] * 3
    names = {obj.name for obj in scene.objects}
    # Objects hidden by hand are left out of the picture and of its frame whatever the rest
    # of the scene is. That covers anything the render draws (curves, text, the instancer of
    # a collection instance), not only the meshes that export.
    excluded = [obj for obj in scene.objects if _pause_map_hidden(obj)]
    for instance in instances:
        try:
            if instance.original not in excluded and _pause_map_hidden(instance.original):
                excluded.append(instance.original)  # a mesh inside an instanced collection
        except (AttributeError, ReferenceError):
            continue

    def hidden_by_hand(instance) -> bool:
        try:
            return instance.original in excluded or (
                instance.instancer is not None and instance.instancer in excluded)
        except (AttributeError, ReferenceError):
            return False

    instances = [instance for instance in instances if not hidden_by_hand(instance)]
    decals = []  # and water: everything left out of the picture and of its frame
    for instance in instances:
        try:
            if _pause_map_left_out(instance.original, names) and instance.original not in decals:
                decals.append(instance.original)
        except (AttributeError, ReferenceError):
            continue
    if len(decals) >= len({id(instance.original) for instance in instances}):
        decals = []  # nothing but decals and water: the scene is not using them as overlays
    boxes = []
    for instance in instances:
        try:
            if instance.original in decals:
                continue
            corners = [instance.matrix_world @ Vector(corner) for corner in instance.original.bound_box]
        except (AttributeError, ReferenceError):
            continue
        box_low = [min(corner[axis] for corner in corners) for axis in range(3)]
        box_high = [max(corner[axis] for corner in corners) for axis in range(3)]
        boxes.append((instance.original.name, box_low, box_high))

    # One stray object frames the whole map out of sight: a broken import can sit trillions of
    # metres away. Anything beyond the engine's reach, or far outside where the rest of the map
    # is, is left out of the frame (it still exports) and named so it can be found.
    strays = []
    def finite(box):
        return all(math.isfinite(value) and abs(value) <= PAUSE_MAP_WORLD_LIMIT for value in box[1] + box[2])
    strays += [box[0] for box in boxes if not finite(box)]
    boxes = [box for box in boxes if finite(box)]
    if len(boxes) >= PAUSE_MAP_MIN_CROWD:
        centres = [((box[1][0] + box[2][0]) * 0.5, (box[1][1] + box[2][1]) * 0.5) for box in boxes]
        middle = (sorted(centre[0] for centre in centres)[len(centres) // 2],
                  sorted(centre[1] for centre in centres)[len(centres) // 2])
        distances = [math.hypot(centre[0] - middle[0], centre[1] - middle[1]) for centre in centres]
        reach = sorted(distances)[int(len(distances) * 0.95)] * PAUSE_MAP_STRAY_FACTOR + PAUSE_MAP_MIN_HALF_HEIGHT
        strays += [box[0] for box, distance in zip(boxes, distances) if distance > reach]
        boxes = [box for box, distance in zip(boxes, distances) if distance <= reach]
    if strays:
        diagnostics.warn("pause_map_strays",
                         "Left out of the pause map's frame, far from the rest of the map: " +
                         ", ".join(sorted(strays)[:8]) + (" ..." if len(strays) > 8 else ""),
                         count=len(strays))
    for _, box_low, box_high in boxes:
        for axis in range(3):
            low[axis] = min(low[axis], box_low[axis])
            high[axis] = max(high[axis], box_high[axis])
    if not all(math.isfinite(value) for value in low + high):
        return False

    aspect = size[0] / size[1]
    centre_x = (low[0] + high[0]) * 0.5
    centre_y = (low[1] + high[1]) * 0.5
    half_height = max((high[1] - low[1]) * 0.5, (high[0] - low[0]) * 0.5 / aspect,
                      PAUSE_MAP_MIN_HALF_HEIGHT / scale) * PAUSE_MAP_PADDING
    half_width = half_height * aspect

    render = scene.render
    camera_data = bpy.data.cameras.new("ReSkatePauseMapCamera")
    camera_data.type = "ORTHO"
    camera_data.ortho_scale = half_width * 2.0
    camera_data.clip_start = 0.1
    camera_data.clip_end = (high[2] - low[2]) + 200.0
    camera = bpy.data.objects.new("ReSkatePauseMapCamera", camera_data)
    camera.location = (centre_x, centre_y, high[2] + 100.0)
    camera.rotation_euler = (0.0, 0.0, 0.0)  # looks down -Z with +Y up: Blender's top view
    scene.collection.objects.link(camera)
    scene.camera = camera

    target = stage / "ui" / "map_image.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    render.resolution_x, render.resolution_y = size
    render.resolution_percentage = 100
    render.film_transparent = True  # nothing but the map: the rest stays transparent
    render.use_border = False
    render.filepath = str(target)
    render.image_settings.file_format = "PNG"
    render.image_settings.color_mode = "RGBA"
    try:
        scene.view_settings.view_transform = "Standard"
    except TypeError:
        pass
    # Unlit: Workbench's flat lighting draws each surface's texture (or its viewport colour)
    # as it is, with no lights or highlights. The lit engines are not a substitute, so there
    # is no fallback to them. "3d" keeps those colours and adds depth; "2d" is the flat sheet.
    rendered = False
    hidden = [original for original in decals + excluded if not original.hide_render]
    materials = {slot.material
                 for obj in list(scene.objects) + [instance.original for instance in instances]
                 for slot in getattr(obj, "material_slots", ()) if slot.material is not None}
    shown = []
    system = bpy.context.preferences.system
    texture_limit_before = system.gl_texture_limit
    try:
        for original in hidden:
            original.hide_render = True
        # A map pixel covers about a metre; full-size textures add nothing to it but video
        # memory. The limit applies to every image Workbench uploads.
        if textured:
            system.gl_texture_limit = texture_limit
            shown = _show_base_colours(materials)
        render.engine = "BLENDER_WORKBENCH"
        shading = scene.display.shading
        shading.light = "FLAT"
        # Untextured is the last resort: no image goes to the GPU at all.
        shading.color_type = "TEXTURE" if textured else "MATERIAL"
        # Depth without lighting the colours: shadows cast by the afternoon sun, and darkened
        # creases with lightened ridges along edges.
        depth = style == "3d"
        shading.show_shadows = depth
        shading.shadow_intensity = 0.55
        scene.display.light_direction = _pause_map_sun_direction()
        scene.display.shadow_shift = 0.1
        scene.display.shadow_focus = 0.6
        shading.show_cavity = depth
        shading.cavity_type = "BOTH"
        shading.cavity_ridge_factor = 1.0
        shading.cavity_valley_factor = 1.5
        shading.curvature_ridge_factor = 1.0
        shading.curvature_valley_factor = 1.5
        shading.show_object_outline = False
        shading.show_specular_highlight = False
        shading.show_backface_culling = False
        scene.display.render_aa = samples
        target.unlink(missing_ok=True)
        label = f"{style}" + ("" if textured else ", untextured")
        _render_pause_map_tiled(scene, camera, centre_x, centre_y, half_width, half_height,
                                size, max(1, int(tiles)), target, label)
        rendered = target.is_file()
    except (TypeError, RuntimeError, AttributeError, ValueError) as ex:
        diagnostics.warn("pause_map_render_engine", f"Workbench could not render the pause map: {ex}")
    finally:
        try:
            system.gl_texture_limit = texture_limit_before
        except (TypeError, AttributeError):
            pass
        _restore_base_colours(shown)
        for original in hidden:
            original.hide_render = False
    bpy.data.objects.remove(camera, do_unlink=True)
    bpy.data.cameras.remove(camera_data)
    if not rendered:
        diagnostics.warn("pause_map_render_failed", "The pause-menu map could not be rendered; San Van's is kept")
        return False
    try:
        _finish_pause_map(target, stage / "ui" / "map_clouds.png")
    except Exception as ex:
        diagnostics.warn("pause_map_clouds_failed", f"The pause map's cloud mask could not be made: {ex}")
    # Copies beside the scene, to look at or to touch up and promote to assets/<scene>/map.png
    # and map_clouds.png. A mask left over from an earlier export would not match this picture.
    try:
        exported = authored / "export"
        exported.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(target, exported / "map.png")
        clouds = stage / "ui" / "map_clouds.png"
        if clouds.is_file():
            shutil.copyfile(clouds, exported / "map_clouds.png")
        else:
            (exported / "map_clouds.png").unlink(missing_ok=True)
    except OSError as ex:
        diagnostics.warn("pause_map_copy_failed", f"The rendered pause map was not copied beside the scene: {ex}")
    # Game axes: +X is Blender +X, +Z is Blender -Y; the picture's top edge is Blender +Y.
    (stage / "ui" / "map_image.json").write_text(json.dumps({
        "min_x": (centre_x - half_width) * scale, "max_x": (centre_x + half_width) * scale,
        "min_z": -(centre_y + half_height) * scale, "max_z": -(centre_y - half_height) * scale,
    }, indent=2) + "\n", encoding="utf-8")
    return True


# The pause map renders in a separate Blender process. Workbench draws the whole scene on
# the GPU in one go (every texture uploaded, plus shadows and cavity at 4K); on a scene this
# size that can exhaust video memory, and a driver or GPU failure ends the process without
# a Python exception. Done in-process, that threw away the whole conversion. In a child, a
# crash costs only the pause map: each attempt is lighter than the last, and if none works
# the map keeps San Van's pause map and the conversion still finishes.
PAUSE_MAP_RESULT_PREFIX = "RESKATE_PAUSE_MAP_RESULT="
PAUSE_MAP_CHILD_TIMEOUT = 5 * 60
PAUSE_MAP_TOTAL_TIMEOUT = 20 * 60
CONVERSION_TIMEOUT = 2 * 60 * 60 - 60  # finish before Studio's outer two-hour limit
PAUSE_MAP_ATTEMPTS = (
    # (style, tiles per side, texture limit, anti-aliasing samples, textured). Every attempt
    # renders the full 3840x2160 picture; later ones ask less of the GPU per frame.
    (None, 4, "CLAMP_512", "8", True),      # as requested ("3d" by default), 16 tiles
    (None, 8, "CLAMP_256", "5", True),      # the same look in 64 smaller frames
    ("2d", 8, "CLAMP_256", "5", True),      # flat textures: no shadows or edge shading
    ("2d", 8, "CLAMP_128", "FXAA", False),  # last resort: viewport colours, no textures
)


def _pause_map_authored(source: Path) -> bool:
    authored = source.parent / "assets" / source.stem
    return any((authored / ("map" + suffix)).is_file() for suffix in PAUSE_MAP_IMAGE_TYPES)


def _run_pause_map_child(command, timeout: float, creation: int):
    # Runs the child Blender, passing its tile progress straight through to Studio, and
    # keeps the rest of its output for the report. Returns (exit code, output, timed out).
    child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, creationflags=creation)
    timed_out = threading.Event()

    def expire():
        if child.poll() is None:
            timed_out.set()
            try:
                child.kill()
            except OSError:
                pass  # it may have exited between poll() and kill()

    watchdog = threading.Timer(timeout, expire)
    watchdog.daemon = True
    watchdog.start()
    kept = deque(maxlen=200)
    try:
        for raw in child.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if line.startswith("RESKATE_MAP_PROGRESS="):
                with _OUTPUT_LOCK:
                    print(line, flush=True)
            else:
                kept.append(line)
        code = child.wait()
    finally:
        watchdog.cancel()
        watchdog.join()
        if child.poll() is None:
            child.kill()
            child.wait()
        child.stdout.close()
    return code, "\n".join(kept), timed_out.is_set()


def _render_pause_map_isolated(source: Path, stage: Path, scale: float, style: str,
                               diagnostics: Diagnostics, *, deadline=None) -> bool:
    if _pause_map_authored(source):
        return False
    blender = bpy.app.binary_path
    if not blender:
        diagnostics.warn("pause_map_render_failed",
                         "Blender's executable path is unknown; San Van's pause map is kept")
        return False
    flags = [flag for flag in ("--factory-startup", "--disable-autoexec") if flag in sys.argv]
    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    render_deadline = time.perf_counter() + PAUSE_MAP_TOTAL_TIMEOUT
    if deadline is not None:
        render_deadline = min(render_deadline, deadline - 30.0)
    tried = set()
    for attempt, (attempt_style, tiles, texture_limit, samples, textured) in enumerate(PAUSE_MAP_ATTEMPTS, start=1):
        attempt_style = attempt_style or style
        key = (attempt_style, tiles, textured)
        if key in tried:
            continue   # "2d" requested: the 3d-only steps collapse into the same attempt
        tried.add(key)
        remaining = render_deadline - time.perf_counter()
        if remaining <= 0.0:
            diagnostics.warn("pause_map_timeout", "Pause map time budget exhausted; San Van's pause map is kept")
            break
        label = f"{attempt_style}, {tiles * tiles} tiles" + ("" if textured else ", untextured")
        _progress(0.87, "pause_map", f"Rendering the pause-menu map ({label})")
        for leftover in ("map_image.png", "map_clouds.png", "map_image.json"):
            (stage / "ui" / leftover).unlink(missing_ok=True)
        command = [blender, "--background", *flags, "--python", str(Path(__file__).resolve()), "--",
                   "--pause-map-only", "--input", str(source), "--output", str(stage),
                   "--scale", repr(scale), "--pause-map", attempt_style,
                   "--pause-map-tiles", str(tiles), "--pause-map-texture-limit", texture_limit,
                   "--pause-map-samples", samples]
        if not textured:
            command.append("--pause-map-untextured")
        timeout = min(PAUSE_MAP_CHILD_TIMEOUT, remaining)
        try:
            code, output, timed_out = _run_pause_map_child(command, timeout, creation)
        except OSError as ex:
            diagnostics.warn("pause_map_attempt_failed",
                             f"Pause map attempt {attempt} ({label}) could not start Blender: {ex}")
            break
        if timed_out:
            diagnostics.warn("pause_map_attempt_failed",
                             f"Pause map attempt {attempt} ({label}) timed out after {timeout}s")
            continue
        result = None
        for line in output.splitlines():
            if line.startswith(PAUSE_MAP_RESULT_PREFIX):
                try:
                    result = json.loads(line[len(PAUSE_MAP_RESULT_PREFIX):])
                except ValueError:
                    pass
        if result is None:
            tail = " | ".join(line.strip() for line in output.splitlines()[-6:] if line.strip())
            diagnostics.warn("pause_map_attempt_failed",
                             f"Pause map attempt {attempt} ({label}) ended without a result "
                             f"(Blender exit code {code}; likely a GPU/driver crash or out of "
                             f"memory). Last output: {tail[-600:]}")
            continue
        if code != 0:
            diagnostics.warn("pause_map_attempt_failed",
                             f"Pause map attempt {attempt} ({label}) exited with code {code}")
            continue
        for warning in result.get("warnings", []):
            if str(warning.get("code", "")).startswith("pause_map"):
                diagnostics.warnings.append(warning)
        if result.get("error"):
            diagnostics.warn("pause_map_attempt_failed",
                             f"Pause map attempt {attempt} ({label}) failed: {result['error']}")
            continue
        if result.get("rendered") and (stage / "ui" / "map_image.png").is_file():
            if attempt > 1:
                diagnostics.warn("pause_map_reduced",
                                 f"The pause map was rendered with reduced settings ({label}) "
                                 "after the full render failed")
            return True
        if not result.get("rendered"):
            return False   # nothing to frame, or authored map: not a failure to retry
    diagnostics.warn("pause_map_render_failed",
                     "Every pause map attempt failed; San Van's pause map is kept")
    return False


def _pause_map_only(options) -> int:
    # Child entry point for _render_pause_map_isolated: renders into an existing stage folder.
    diagnostics = Diagnostics()
    result = {"rendered": False}
    try:
        source = Path(options.input).expanduser().resolve()
        stage = Path(options.output).expanduser().resolve()
        _load_input(source)
        scene = bpy.context.scene
        depsgraph = bpy.context.evaluated_depsgraph_get()
        instances = _evaluated_mesh_instances(depsgraph, Diagnostics())
        result["rendered"] = bool(_render_pause_map(
            scene, instances, source, stage, options.scale, options.pause_map, diagnostics,
            textured=not options.pause_map_untextured, tiles=options.pause_map_tiles,
            texture_limit=options.pause_map_texture_limit, samples=options.pause_map_samples))
        if not result["rendered"] and any(warning["code"] in ("pause_map_render_engine", "pause_map_render_failed")
                                          for warning in diagnostics.warnings):
            result["error"] = "Workbench could not render the pause map"   # worth a lighter retry
    except Exception as ex:
        result["error"] = f"{type(ex).__name__}: {ex}"
        result["traceback"] = traceback.format_exc()
    result["warnings"] = diagnostics.warnings
    print(PAUSE_MAP_RESULT_PREFIX + json.dumps(result, separators=(",", ":")), flush=True)
    return 0 if "error" not in result else 1


def _checked_input(options) -> Path:
    source = Path(options.input).expanduser().resolve()
    if not source.exists():
        raise MapImportError("input_not_found", f"Input scene not found: {source}", path=str(source))
    if not source.is_file() or source.suffix.lower() not in SUPPORTED_INPUTS:
        raise MapImportError("input_unsupported", f"--input must be a .blend or .fbx file: {source}",
                             path=str(source))
    if not math.isfinite(options.scale) or options.scale <= 0.0:
        raise MapImportError("invalid_option", "--scale must be a finite positive number", option="scale")
    if options.bake_size < 1:
        raise MapImportError("invalid_option", "--bake-size must be a positive integer", option="bake-size")
    if not math.isfinite(options.timeout_seconds) or options.timeout_seconds <= 0.0:
        raise MapImportError("invalid_option", "--timeout-seconds must be a finite positive number",
                             option="timeout-seconds")
    return source


def _open_input(source: Path):
    try:
        _load_input(source)
    except MapImportError:
        raise
    except Exception as ex:
        raise MapImportError("input_load_failed", f"Blender could not open {source.name}: {ex}",
                             path=str(source)) from ex


def _pause_map_skipped(options) -> bool:
    return bool(options.no_pause_map or os.environ.get("RESKATE_MAP_NO_PAUSE_MAP", "").strip() in ("1", "true", "yes"))


def convert(options):
    source = _checked_input(options)
    destination = _check_output(Path(options.output), source)
    stage, destination = _fresh_stage(destination)
    return _convert(options, stage, destination)


@_profile_conversion
def _convert(options, stage, destination):
    started = time.perf_counter()
    source = Path(options.input).expanduser().resolve()
    diagnostics = Diagnostics()
    for code, message in getattr(options, "version_warnings", ()):
        diagnostics.warn(code, message)
    stats = {
        "performance_patch": "map-import-" + VERSION,
        "scene_objects": 0, "evaluated_mesh_instances": 0,
        "source_meshes": 0, "mesh_parts": 0, "triangles": 0,
        "corner_vertices": 0, "materials": 0, "material_splits": 0,
        "negative_winding_repairs": 0, "opposed_corner_normals_repaired": 0,
        "singular_transforms": 0,
        "textures_written": 0, "texture_references": 0,
        "textures_deduplicated": 0, "constant_base_color_textures": 0,
        "alpha_images_scanned": 0, "alpha_classification_cache_hits": 0,
        "alpha_classification_failures": 0,
        "shared_base_color_alpha_materials": 0,
        "imported_shader_materials": 0, "blank_source_lightmaps_ignored": 0,
        "procedural_bakes": 0, "spawn_points": 0, "travel_points": 0,
        "light_entities": 0,
        "retail_blueprint_instances": 0,
        "shared_mesh_sources": 0, "shared_mesh_placements": 0,
        "unique_render_meshes": 0,
        "collision_meshes": 0, "collision_triangles": 0,
        "spatial_split_sources": 0, "spatial_split_parts": 0,
        "spatial_triangles_added": 0, "max_render_local_reach": 0.0,
        "native_quantization_triangles_dropped": 0,
        "native_section_split_sources": 0, "native_section_split_parts": 0,
        "max_render_section_vertices": 0,
        "unreasonable_source_triangles_dropped": 0,
        "glb_write_workers": GLB_WRITE_WORKERS, "glb_write_jobs": 0,
    }
    writes = None
    collision_job_count = 0
    try:
        (stage / "meshes").mkdir()
        (stage / "collision").mkdir()
        textures = TextureStore(stage / "textures", diagnostics, stats)
        _progress(0.01, "load", f"Loading {source.name}")
        _open_input(source)
        scene = bpy.context.scene
        stats["scene_objects"] = len(scene.objects)
        spawns = _spawn_records(scene, options.scale, diagnostics)
        stats["spawn_points"] = len(spawns)

        with _perf_phase("dependency_graph"):
            depsgraph = bpy.context.evaluated_depsgraph_get()
        travel_points = _evaluated_empty_marker_records(
            depsgraph, "TravelPoint", options.scale, diagnostics, numbered=True)
        for index, record in enumerate(travel_points):
            marker = bpy.data.objects.get(record["source_name"])
            stop_name = str(marker.get("bus_stop_name", "")).strip() if marker is not None else ""
            record["stop_name"] = stop_name or f"Bus Stop {index + 1}"
            record["shelter"] = bool(marker.get("bus_stop_shelter", True)) if marker is not None else True
        stats["travel_points"] = len(travel_points)
        lights = _evaluated_light_records(depsgraph, options.scale)
        audio_volumes, audio_emitters = _evaluated_audio_records(depsgraph, options.scale)
        interaction_prefabs = _evaluated_interaction_prefabs(depsgraph, options.scale)
        stats["interaction_prefabs"] = len(interaction_prefabs)
        trigger_effects, vfx_prefabs = _map_export_addon().native_behavior_records(depsgraph, options.scale)
        stats["trigger_effects"] = len(trigger_effects)
        stats["vfx_prefabs"] = len(vfx_prefabs)
        npc_routes = _evaluated_npc_routes(depsgraph, options.scale)
        stats["npc_routes"] = len(npc_routes)
        grind_curves, grind_notes = _map_export_addon().grind_curve_records(depsgraph, options.scale)
        for note in grind_notes:
            diagnostics.warn("grind_curve", note)
        stats["audio_volumes"] = len(audio_volumes)
        stats["audio_emitters"] = len(audio_emitters)
        stats["light_entities"] = len(lights)
        reflection_probes = _evaluated_reflection_probe_records(depsgraph,
                                                                options.scale)
        stats["reflection_probes"] = len(reflection_probes)
        instances = _evaluated_mesh_instances(depsgraph, diagnostics)
        stats["evaluated_mesh_instances"] = len(instances)
        stats["source_meshes"] = len({x.original.name_full for x in instances})

        object_names = NameAllocator()
        material_names = NameAllocator()
        material_keys: dict[int, str] = {}
        decal_keys: dict[tuple, str] = {}
        materials: dict[str, dict] = {}
        surface_profiles: dict[str, dict] = {}
        objects = []
        render_sequence = 0
        writes = GlbWriteQueue()
        shared_placements, shared_copies = _shared_mesh_placements(
            instances, surface_profiles)
        stats["shared_mesh_sources"] = len(shared_placements)
        stats["shared_mesh_placements"] = sum(map(len, shared_placements.values()))

        _progress(0.06, "inspect",
                  f"Found {len(instances)} evaluated mesh instances",
                  current=0, total=len(instances))

        for instance_index, instance in enumerate(instances):
            if instance_index in shared_copies:
                continue
            _progress(0.08 + 0.76 * instance_index / max(1, len(instances)),
                      "geometry", f"Converting {instance.original.name}",
                      current=instance_index + 1, total=len(instances))
            evaluated = instance.obj
            if _placement_mode(instance.original) == "retail_blueprint":
                retail_blueprint = _retail_blueprint(instance.original)
                if not retail_blueprint:
                    diagnostics.error(
                        "retail_blueprint_missing",
                        "Retail Blueprint Instance has no ObjectBlueprint EBX path",
                        object=instance.original.name)
                    continue
                base_label = instance.original.name
                if instance.is_instance:
                    base_label += f"__instance_{instance_index:04d}"
                object_name = object_names.get(base_label, "retail_blueprint")
                entry = {
                    "name": object_name,
                    "placement_mode": "retail_blueprint",
                    "retail_blueprint": retail_blueprint,
                    "retail_collision_policy": "native",
                    "transform": _game_transform_rows(
                        instance.matrix_world, options.scale),
                    "static": True,
                    "collision_mode": "none",
                    "source_object": instance.original.name,
                    "source_path": _source_path(instance.original),
                }
                if instance.is_instance:
                    entry["source_instance"] = [
                        int(value) for value in instance.persistent_id
                        if int(value) != 2147483647]
                objects.append(entry)
                stats["retail_blueprint_instances"] += 1
                continue
            placements = shared_placements.get(instance_index)
            # Meshes and collision stay in prototype space; native placements carry
            # the world matrix. Mirrors use a reflected prototype plus a positive-
            # handed placement, so the native cull mode and tangent winding agree.
            geometry_matrix = instance.matrix_world
            if placements:
                geometry_matrix = Matrix.Identity(4)
                if instance.matrix_world.to_3x3().determinant() < 0.0:
                    geometry_matrix[0][0] = -1.0
            with _perf_phase("mesh_evaluation"):
                try:
                    mesh = evaluated.to_mesh(preserve_all_data_layers=True,
                                             depsgraph=depsgraph)
                except TypeError:
                    mesh = evaluated.to_mesh()
            if mesh is None:
                diagnostics.warn("evaluated_mesh_missing",
                                 "Blender returned no evaluated mesh",
                                 object=instance.original.name)
                continue
            try:
                with _perf_phase("triangulation"):
                    mesh.calc_loop_triangles()
                mesh_arrays = _MeshArrays(mesh, geometry_matrix, options.scale)
                cell_counts = _triangle_cell_counts(mesh_arrays)
                reasonable = ((cell_counts >= 0.0) &
                              (cell_counts <= MAX_TRIANGLE_SPATIAL_CELLS))
                unreasonable_triangles = int(np.count_nonzero(~reasonable))
                oversized = cell_counts[cell_counts > MAX_TRIANGLE_SPATIAL_CELLS]
                maximum_triangle_cells = int(oversized.max()) if len(oversized) else 0
                kept_triangles = np.nonzero(reasonable)[0]
                kept_materials = mesh_arrays.triangle_materials[kept_triangles]
                grouped: dict[int, np.ndarray] = {}
                for material_index in dict.fromkeys(kept_materials.tolist()):
                    grouped[material_index] = kept_triangles[kept_materials == material_index]
                if unreasonable_triangles:
                    stats["unreasonable_source_triangles_dropped"] += \
                        unreasonable_triangles
                    diagnostics.warn(
                        "unreasonable_source_triangles_dropped",
                        "Triangles spanning an unsafe number of 64 m render cells were skipped",
                        object=instance.original.name,
                        triangles=unreasonable_triangles,
                        cell_limit=MAX_TRIANGLE_SPATIAL_CELLS,
                        maximum_cell_count=(maximum_triangle_cells or
                                            "non-finite"))
                if not grouped:
                    diagnostics.warn("mesh_has_no_triangles",
                                     "Evaluated mesh contains no exportable triangles",
                                     object=instance.original.name)
                    continue
                if len(grouped) > 1:
                    stats["material_splits"] += len(grouped) - 1

                determinant = float(geometry_matrix.to_3x3().determinant())
                reverse = determinant < 0.0
                if reverse:
                    stats["negative_winding_repairs"] += len(grouped)
                if abs(determinant) < 1e-12:
                    stats["singular_transforms"] += 1
                    diagnostics.warn("singular_world_transform",
                                     "World transform is singular; normal transform used Blender's safe inverse",
                                     object=instance.original.name)

                collision_mode = _collision_mode(instance.original)
                collision_materials, collision_groups = \
                    _collision_groups_by_surface(
                        mesh, grouped, instance.original, surface_profiles)
                # Invisible (collision only) materials are drawn as nothing: their faces keep
                # only their collision.
                invisible = {index for index in grouped
                             if _material_invisible(_material_at(mesh, index, instance.original))}
                visible_contracts = {collision_materials[index] for index in grouped
                                     if index not in invisible}
                collision_paths = {}
                if collision_mode != "none":
                    for surface_contract in sorted(collision_groups):
                        surface_profile_id, collision_material_packed = \
                            surface_contract
                        collision_suffix = ""
                        if len(collision_groups) > 1:
                            collision_suffix = (
                                f"__{surface_profile_id}"
                                if surface_profile_id else
                                f"__surface_{collision_material_packed:08d}")
                        collision_name = (
                            f"{instance_index:04d}_"
                            f"{_safe_name(instance.original.name, 'collision')}"
                            f"{collision_suffix}.glb")
                        collision_rel = "collision/" + collision_name
                        collision_paths[surface_contract] = collision_rel
                        collision_arrays = _collision_arrays_from_source(
                            mesh, geometry_matrix, options.scale, reverse,
                            triangles=collision_groups[surface_contract],
                            data=mesh_arrays)

                        _submit_collision_job(writes, CollisionWriteJob(
                            destination=stage / collision_rel,
                            arrays=collision_arrays,
                            name=(instance.original.name + collision_suffix +
                                  "_collision"),
                            mode=collision_mode), stats)
                        collision_job_count += 1
                        if surface_contract in visible_contracts:
                            continue
                        # Only invisible materials use this surface: collision with nothing drawn.
                        entry = {
                            "name": object_names.get(
                                instance.original.name + collision_suffix + "__collision_only",
                                "collision"),
                            "placement_mode": "authored_mesh",
                            "render": False,
                            "collision_mesh": collision_rel,
                            "transform": list(IDENTITY),
                            "static": True,
                            "collision_mode": collision_mode,
                            "collision_material": _collision_material_identifier(
                                collision_material_packed),
                            "collision_material_packed": collision_material_packed,
                            "source_object": instance.original.name,
                            "world_transform_baked": not bool(placements),
                        }
                        if surface_profile_id:
                            entry["surface_profile"] = surface_profile_id
                        rows = [entry]
                        if placements:
                            rows = [dict(entry,
                                         name=object_names.get(
                                             f"{entry['name']}__placed_{copy_index:04d}", "collision"),
                                         transform=_game_transform_rows(
                                             placed.matrix_world @ geometry_matrix, options.scale),
                                         source_object=placed.original.name)
                                    for copy_index, placed in enumerate(placements)]
                        objects.extend(rows)
                        stats["invisible_collision_objects"] = \
                            stats.get("invisible_collision_objects", 0) + len(rows)

                if invisible:
                    stats["invisible_material_parts"] = \
                        stats.get("invisible_material_parts", 0) + len(invisible)
                for material_index in sorted(grouped):
                    if material_index in invisible:
                        continue
                    triangles = grouped[material_index]
                    material = _material_at(mesh, material_index, instance.original)
                    surface_contract = collision_materials[material_index]
                    surface_profile_id, collision_material_packed = surface_contract
                    collision_rel = collision_paths.get(surface_contract)
                    base_label = instance.original.name
                    if len(grouped) > 1:
                        base_label += "__" + (material.name if material else f"slot_{material_index}")
                    if instance.is_instance:
                        base_label += f"__instance_{instance_index:04d}"
                    base_object_name = object_names.get(base_label, "mesh")
                    # All direct image/tint paths sample the image node's authored UV map.
                    base_uv_layer = _base_image_uv_layer(material)
                    arrays = _extract_part(mesh, triangles, geometry_matrix,
                                           options.scale, reverse, mesh_arrays,
                                           uv_layer=base_uv_layer, repair_normals=True)
                    repaired_normals = _repair_opposed_corner_normals(arrays)
                    if repaired_normals:
                        stats["opposed_corner_normals_repaired"] += repaired_normals
                        diagnostics.warn(
                            "opposed_corner_normals_repaired",
                            "Corner normals facing through their geometric triangle were flipped",
                            object=base_object_name,
                            material=material.name if material else None,
                            corners=repaired_normals)


                    bake_obj = None
                    material_obj = None
                    decal_variants = []
                    try:
                        mode, _ = _base_mode(material)
                        pointer = int(material.as_pointer()) if material else 0
                        # Decal-layered graphs export their base texture plus one native
                        # decal part per layer instead of baking a texture per object.
                        layered = _decal_layers(material) if mode == "procedural" else None
                        if layered is not None:
                            mode = "layered"
                        if mode == "procedural":


                            material_key = material_names.get(
                                (material.name if material else "default") + "__" +
                                base_object_name,
                                "material")
                        elif pointer not in material_keys:
                            material_keys[pointer] = material_names.get(
                                material.name if material else "default", "material")
                            material_key = material_keys[pointer]
                        else:
                            material_key = material_keys[pointer]

                        if material_key not in materials:
                            if mode == "procedural":
                                bake_obj = _LazyBakeObject(
                                    base_object_name + "_bake", base_object_name + "_bake_mesh",
                                    arrays.local_positions, arrays.local_normals, arrays.uvs,
                                    instance.matrix_world)
                            else:
                                material_obj = _LazyBakeObject(
                                    base_object_name + "_material", base_object_name + "_material_mesh",
                                    arrays.world_positions, arrays.world_normals, arrays.uvs)
                                bake_obj = material_obj
                            material_record = _material_record(
                                material, bake_obj, material_key, options, textures,
                                diagnostics, stats, surface_profiles,
                                base_override=layered[0] if layered is not None else None)
                            materials[material_key] = material_record
                            stats["materials"] += 1
                            if layered is not None:
                                stats["decal_layered_materials"] = \
                                    stats.get("decal_layered_materials", 0) + 1
                                for note in sorted(set(layered[2])):
                                    diagnostics.warn("decal_layer_simplified", note,
                                                     material=material.name)
                        if layered is not None:
                            for layer_index, (layer_image, layer_node, layer_factor, layer_blend) in \
                                    enumerate(layered[1]):
                                layer_triangles = _decal_triangles(
                                    mesh_arrays, triangles, _decal_vertex_mask(layer_factor))
                                if len(layer_triangles) == 0:
                                    stats["decal_layers_masked_out"] = \
                                        stats.get("decal_layers_masked_out", 0) + 1
                                    continue
                                decal_cache_key = (pointer, layer_index)
                                decal_key = decal_keys.get(decal_cache_key)
                                if decal_key is None:
                                    decal_key = material_names.get(
                                        f"{material.name}__decal{layer_index}", "material")
                                    decal_keys[decal_cache_key] = decal_key
                                    decal_material = _decal_material(
                                        material, layer_index, layer_image, layer_node,
                                        layer_factor, layer_blend)
                                    try:
                                        if bake_obj is None:
                                            material_obj = _LazyBakeObject(
                                                base_object_name + "_material", base_object_name + "_material_mesh",
                                                arrays.world_positions, arrays.world_normals, arrays.uvs)
                                            bake_obj = material_obj
                                        materials[decal_key] = _material_record(
                                            decal_material, bake_obj, decal_key, options,
                                            textures, diagnostics, stats, surface_profiles)
                                        materials[decal_key]["decal_layer"] = layer_index
                                        materials[decal_key]["decal_source_material"] = material.name
                                        stats["materials"] += 1
                                        stats["decal_layers"] = stats.get("decal_layers", 0) + 1
                                    finally:
                                        bpy.data.materials.remove(decal_material)
                                decal_variants.append(
                                    (_lift_part(
                                        _extract_part(mesh, layer_triangles, geometry_matrix,
                                                      options.scale, reverse, mesh_arrays,
                                                      uv_layer=_image_uv_layer(layer_node)),
                                        DECAL_LIFT * (layer_index + 1)),
                                     decal_key, f"__decal{layer_index}"))
                    except Exception as ex:
                        diagnostics.error("mesh_material_export_failed", str(ex),
                                          object=base_object_name,
                                          material=material.name if material else None)
                        continue
                    finally:
                        if bake_obj is not None and bake_obj is not material_obj:
                            _remove_temp_object(bake_obj)
                        if material_obj is not None:
                            _remove_temp_object(material_obj)

                    root_object_name = base_object_name
                    variants = [(arrays, material_key, "")] + decal_variants
                    for arrays, material_key, variant_suffix in variants:
                        base_object_name = root_object_name + variant_suffix
                        part_collision_mode = "none" if variant_suffix else collision_mode
                        part_collision_rel = None if variant_suffix else collision_rel
                        if placements and _local_reach(arrays.world_positions) <= MAX_RENDER_LOCAL_REACH:
                            points = np.asarray(arrays.world_positions, dtype=np.float64)
                            origin = tuple((points.min(axis=0) + points.max(axis=0)) * 0.5)
                            raw_spatial_parts = [SpatialPart((0, 0, 0), arrays, origin)]
                        else:
                            raw_spatial_parts = _split_part_spatially(arrays)
                        spatial_parts = []
                        native_dropped = 0
                        for raw_spatial_part in raw_spatial_parts:
                            filtered_part, dropped = \
                                _filter_native_degenerate_triangles(raw_spatial_part)
                            native_dropped += dropped
                            if filtered_part is not None:
                                spatial_parts.append(filtered_part)
                        if native_dropped:
                            stats["native_quantization_triangles_dropped"] += \
                                native_dropped
                            diagnostics.warn(
                                "render_triangles_dropped_after_native_quantization",
                                "Render triangles that are degenerate at float32 precision were removed",
                                object=base_object_name,
                                material=material.name if material else None,
                                triangles=native_dropped,
                                empty_spatial_parts=(len(raw_spatial_parts) -
                                                     len(spatial_parts)))
                        if not spatial_parts:
                            diagnostics.warn(
                                "render_material_part_not_representable",
                                "The material part has no nondegenerate float32 triangles and was skipped",
                                object=base_object_name,
                                material=material.name if material else None)
                            continue
                        if len(raw_spatial_parts) > 1:
                            stats["spatial_split_sources"] += 1
                            stats["spatial_split_parts"] += len(spatial_parts)
                            stats["spatial_triangles_added"] += (
                                sum(part.arrays.triangle_count for part in spatial_parts) -
                                arrays.triangle_count)
                        for spatial_part in spatial_parts:
                            stats["max_render_local_reach"] = max(
                                stats["max_render_local_reach"],
                                _origin_reach(spatial_part.arrays.world_positions,
                                              spatial_part.origin))

                        render_parts = []
                        native_sections_for_source = 0
                        for spatial_index, spatial_part in enumerate(spatial_parts):
                            section_parts = _split_part_for_native_index_width(
                                spatial_part)
                            if len(section_parts) > 1:
                                native_sections_for_source += len(section_parts)
                                diagnostics.warn(
                                    "mesh_split_for_native_index_width",
                                    "Dense render geometry was automatically partitioned into native 16-bit index sections",
                                    object=base_object_name,
                                    spatial_part=spatial_index,
                                    source_vertices=len(
                                        spatial_part.arrays.world_positions),
                                    emitted_sections=len(section_parts),
                                    vertices_per_section=
                                        NATIVE_SECTION_MAX_VERTICES)
                            for section_index, section_part in enumerate(section_parts):
                                render_parts.append(
                                    (spatial_index, section_index,
                                     len(section_parts), section_part))
                        if native_sections_for_source:
                            stats["native_section_split_sources"] += 1
                            stats["native_section_split_parts"] += \
                                native_sections_for_source

                        if not mesh.uv_layers.active:
                            diagnostics.warn("mesh_has_no_uv",
                                             "Mesh has no active UV map; zero UVs were exported",
                                             object=base_object_name)

                        for spatial_index, section_index, section_count, spatial_part \
                                in render_parts:
                            if len(render_parts) == 1:
                                object_name = base_object_name
                            elif len(spatial_parts) == 1:
                                object_name = object_names.get(
                                    f"{base_object_name}__section_{section_index:03d}",
                                    "mesh")
                            elif section_count > 1:
                                object_name = object_names.get(
                                    f"{base_object_name}__tile_{spatial_index:03d}"
                                    f"__section_{section_index:03d}", "mesh")
                            else:
                                object_name = object_names.get(
                                    f"{base_object_name}__tile_{spatial_index:03d}",
                                    "mesh")
                            part_arrays = spatial_part.arrays
                            rel = (f"meshes/{render_sequence:04d}_"
                                   f"{_safe_name(object_name)}.glb")
                            render_sequence += 1
                            entry = {
                                "name": object_name,
                                "placement_mode": "authored_mesh",
                                "mesh": rel,
                                "material": material_key,
                                "transform": list(IDENTITY),
                                "static": True,
                                "collision_mode": part_collision_mode,
                                "collision_material": _collision_material_identifier(
                                    collision_material_packed),
                                "collision_material_packed": collision_material_packed,
                                "source_object": instance.original.name,
                                "source_material_slot": material_index,
                                "world_transform_baked": not bool(placements),
                                "shared_geometry": bool(placements),
                                "native_render_origin": _native_render_origin(
                                    spatial_part),
                                "source_spatial_cell": list(spatial_part.cell),
                                "max_local_reach": round(
                                    _origin_reach(part_arrays.world_positions,
                                                  spatial_part.origin), 6),
                            }
                            if surface_profile_id:
                                entry["surface_profile"] = surface_profile_id
                            if len(spatial_parts) > 1:
                                entry.update({
                                    "source_spatial_part": spatial_index,
                                    "source_spatial_parts": len(spatial_parts),
                                })
                            if section_count > 1:
                                entry.update({
                                    "source_native_section_part": section_index,
                                    "source_native_section_parts": section_count,
                                    "native_section_vertex_limit":
                                        NATIVE_SECTION_MAX_VERTICES,
                                })
                            if part_collision_rel is not None:
                                entry["collision_mesh"] = part_collision_rel
                                entry["collision_world_transform_baked"] = not bool(placements)

                            placement_entries = [entry]
                            if placements:
                                placement_entries = []
                                for copy_index, placed in enumerate(placements):
                                    row = dict(entry)
                                    row["name"] = object_names.get(
                                        f"{object_name}__placed_{copy_index:04d}", "mesh")
                                    row["transform"] = _game_transform_rows(
                                        placed.matrix_world @ geometry_matrix, options.scale)
                                    row["source_object"] = placed.original.name
                                    row["source_path"] = _source_path(placed.original)
                                    if placed.is_instance:
                                        row["source_instance"] = [int(v) for v in placed.persistent_id
                                                                  if int(v) != 2147483647]
                                    placement_entries.append(row)

                            triangle_count = part_arrays.triangle_count
                            vertex_count = len(part_arrays.world_positions)
                            material_label = material.name if material else None

                            def render_written(rows=placement_entries, triangles=triangle_count,
                                               vertices=vertex_count):
                                objects.extend(rows)
                                stats["unique_render_meshes"] += 1
                                stats["mesh_parts"] += len(rows)
                                stats["triangles"] += triangles * len(rows)
                                stats["corner_vertices"] += vertices * len(rows)
                                stats["max_render_section_vertices"] = max(
                                    stats["max_render_section_vertices"],
                                    vertices)

                            def render_failed(error, target=object_name,
                                              source_material=material_label):
                                diagnostics.error(
                                    "mesh_part_export_failed", str(error),
                                    object=target, material=source_material)

                            writes.submit(
                                stage / rel, part_arrays.world_positions,
                                normals=part_arrays.world_normals,
                                uvs=part_arrays.uvs, name=object_name,
                                success=render_written, failure=render_failed)
            finally:
                try:
                    evaluated.to_mesh_clear()
                except Exception:
                    pass

        _flush_temp_objects()
        # Grind curves: invisible sharp collision prisms, each a collision piece of its own
        # so the game's edge analysis never lumps them with dense ripped geometry.
        stats.update(grind_curves=0, grind_curve_pieces=0, grind_curve_triangles=0,
                     grind_curve_length=0.0)
        for record in grind_curves:
            grind_name = object_names.get(record["name"] + "__grind", "grind")
            grind_rel = f"collision/grind_{_safe_name(grind_name, 'grind')}.glb"
            grind_entry = {
                "name": grind_name,
                "placement_mode": "authored_mesh",
                "render": False,
                "collision_mesh": grind_rel,
                "transform": list(IDENTITY),
                "static": True,
                "collision_mode": "triangle_mesh",
                "collision_material": _collision_material_identifier(record["packed"]),
                "collision_material_packed": record["packed"],
                "world_transform_baked": True,
                "source_object": record["source_object"],
                "grind_curve": True,
            }

            def grind_written(entry=grind_entry, record=record):
                objects.append(entry)
                stats["grind_curves"] += 1
                stats["grind_curve_pieces"] += record["pieces"]
                stats["grind_curve_triangles"] += len(record["indices"]) // 3
                stats["grind_curve_length"] = round(stats["grind_curve_length"] + record["length"], 3)

            def grind_failed(error, target=record["name"]):
                diagnostics.error("grind_curve_export_failed", str(error), object=target)

            writes.submit(stage / grind_rel, record["positions"], indices=record["indices"],
                          name=grind_name, success=grind_written, failure=grind_failed)
        _progress(0.85, "collision",
                  "Finishing streamed full-resolution collision surfaces",
                  current=collision_job_count, total=collision_job_count)
        _progress(0.86, "geometry", "Finishing parallel geometry writes",
                  current=writes.submitted, total=writes.submitted)
        try:
            writes.close()
        finally:
            stats["glb_write_jobs"] = writes.submitted
            writes = None

        if not objects and not lights and not audio_volumes and not audio_emitters and not npc_routes and not interaction_prefabs and not trigger_effects and not vfx_prefabs:
            diagnostics.error("no_geometry", "No mesh parts were exported")

        stats["max_render_local_reach"] = round(
            float(stats["max_render_local_reach"]), 6)
        stats["surface_profiles"] = len(surface_profiles)
        display_name = options.map_name or source.stem
        map_name = options.map_id or _map_id(display_name)
        document = {
            "format": 1,
            "name": map_name,
            "display_name": display_name,
            "units": "meters",
            "up": "y",
            "forward": "-z",
            "generator": f"ReSkate Studio map import {VERSION} / Blender {bpy.app.version_string}",
            "source": {
                "file": source.name,
                "type": source.suffix.lower()[1:],
                "world_transforms_baked": not bool(shared_placements),
                "scale": options.scale,
                "render_spatial_partition": {
                    "strategy": "triangle_clip_axis_aligned_cells",
                    "cell_size": RENDER_CELL_SIZE,
                    "max_local_reach": MAX_RENDER_LOCAL_REACH,
                },
                "render_section_partition": {
                    "strategy": "triangle_boundary_u16_index_sections",
                    "max_vertices": NATIVE_SECTION_MAX_VERTICES,
                },
            },
            "materials": materials,
            "surface_profiles": surface_profiles,
            "objects": objects,
            "travel_points": travel_points,
            "lights": lights,
            "audio_volumes": audio_volumes,
            "audio_emitters": audio_emitters,
            "interaction_prefabs": interaction_prefabs,
            "trigger_effects": trigger_effects,
            "vfx_prefabs": vfx_prefabs,
            "npc_routes": npc_routes,
            "reflection_probes": reflection_probes,
            "lighting": {
                "strategy": ("native_frostbite_light_entities" if lights else
                             "inherit_bam_environment"),
                "blender_light_count": len(lights),
                "native_light_entities_authored": False,
                "custom_light_compiler_requested": bool(lights),
            },
            "player_size": PLAYER_SIZE,
            "stats": stats,


            "warnings": [entry["message"] for entry in diagnostics.warnings],
        }
        if spawns:
            document["spawn"] = spawns[0]
            document["spawn_points"] = spawns

        _progress(0.87, "pause_map", "Skipping the pause-menu map" if _pause_map_skipped(options)
                  else "Rendering the pause-menu map")
        try:
            # Everything this process still needs from the scene is already in `document`.
            # Emptying it frees the memory the child Blender needs to load the scene again.
            del instances, depsgraph, scene
            bpy.ops.wm.read_factory_settings(use_empty=True)
        except Exception as ex:
            diagnostics.warn("pause_map_memory", f"The scene could not be unloaded before the pause map: {ex}")
        if _pause_map_skipped(options):
            stats["pause_map_rendered"] = False
            if not _pause_map_authored(source):
                diagnostics.warn("pause_map_skipped",
                                 "The pause-menu map was not rendered (--no-pause-map); the map uses San Van's")
        else:
            try:
                stats["pause_map_rendered"] = _render_pause_map_isolated(
                    source, stage, options.scale, options.pause_map, diagnostics,
                    deadline=started + options.timeout_seconds)
            except Exception as ex:  # the map still builds with San Van's pause map
                stats["pause_map_rendered"] = False
                diagnostics.warn("pause_map_render_failed", f"The pause-menu map could not be rendered: {ex}")

        foreign = _foreign_entries(destination)
        if foreign:
            diagnostics.warn("output_has_other_files",
                             "The output folder holds files this converter did not write; they were left alone",
                             files=foreign[:20])
        _progress(0.98, "manifest", "Writing map manifest")
        (stage / "map.json").write_text(
            json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        report = {
            "schema": 1,
            "status": "ok" if not diagnostics.errors else "completed_with_errors",
            "converter_version": VERSION,
            "blender_version": bpy.app.version_string,
            "input": str(source),
            "output": str(destination),
            "map_name": map_name,
            "stats": stats,
            "warnings": diagnostics.warnings,
            "errors": diagnostics.errors,
            "image_dependencies": textures.dependencies,
            "duration_seconds": round(time.perf_counter() - started, 3),
            "performance": _PERF.snapshot() if _PERF is not None else {},
        }
        report["performance"]["status"] = report["status"]
        report["mode"] = "convert"
        report["exit_code"] = EXIT_OK if report["status"] == "ok" else EXIT_COMPLETED_WITH_ERRORS
        (stage / "conversion-report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        try:
            _publish(stage, destination)
        except OSError as ex:
            raise MapImportError("output_write_failed", f"Could not publish the map into {destination}: {ex}",
                                 path=str(destination)) from ex
        _progress(1.0, "complete", "Conversion complete",
                  current=len(objects), total=len(objects))
        return report
    except Exception:
        if writes is not None:
            writes.abort()
        shutil.rmtree(stage, ignore_errors=True)
        raise


INSPECT_OBJECT_LIMIT = 2000


def _inspect(options):
    # --inspect / --dry-run: what the scene holds and what a conversion would make of it,
    # read with the same rules as convert, without writing a single file.
    started = time.perf_counter()
    source = _checked_input(options)
    diagnostics = Diagnostics()
    for code, message in getattr(options, "version_warnings", ()):
        diagnostics.warn(code, message)
    _progress(0.05, "load", f"Loading {source.name}")
    _open_input(source)
    scene = bpy.context.scene
    _progress(0.4, "inspect", "Reading objects")
    depsgraph = bpy.context.evaluated_depsgraph_get()
    spawn_markers = _empty_marker_records(scene, "spawn", options.scale, diagnostics,
                                          missing_warning=True)
    if len(spawn_markers) > 1:
        diagnostics.warn("multiple_spawn_markers",
                         "A map has one native spawn; the first deterministic marker is used",
                         count=len(spawn_markers))
    travel_points = _evaluated_empty_marker_records(depsgraph, "TravelPoint", options.scale,
                                                    diagnostics, numbered=True)
    instances = _evaluated_mesh_instances(depsgraph, diagnostics)

    def counted(label, function):
        try:
            return len(function(depsgraph, options.scale))
        except Exception as ex:  # a record type the scene cannot describe is a warning here
            diagnostics.warn("inspect_records_failed", f"Could not read {label}: {ex}")
            return None

    lights = counted("lights", _evaluated_light_records)
    try:
        volumes, emitters = _evaluated_audio_records(depsgraph, options.scale)
        audio = {"volumes": len(volumes), "emitters": len(emitters)}
    except Exception as ex:
        diagnostics.warn("inspect_records_failed", f"Could not read audio: {ex}")
        audio = None
    npc_routes = counted("NPC routes", _evaluated_npc_routes)
    prefabs = counted("interaction prefabs", _evaluated_interaction_prefabs)
    reflection_probes = counted("reflection probes", _evaluated_reflection_probe_records)
    try:
        grind_curves = len(_map_export_addon().grind_curve_records(depsgraph, options.scale)[0])
    except Exception as ex:
        diagnostics.warn("inspect_records_failed", f"Could not read grind curves: {ex}")
        grind_curves = None

    triangles_by_mesh = {}
    objects = []
    materials = {}
    collision_modes = {}
    placement_modes = {}
    low = [math.inf] * 3
    high = [-math.inf] * 3
    total_triangles = 0
    for index, instance in enumerate(instances):
        if index % 50 == 0:
            _progress(0.4 + 0.5 * index / max(1, len(instances)), "inspect",
                      f"Reading {instance.original.name}", current=index, total=len(instances))
        original = instance.original
        data = instance.obj.data
        key = int(data.as_pointer()) if data is not None else 0
        if key not in triangles_by_mesh:
            triangles_by_mesh[key] = sum(len(p.vertices) - 2 for p in data.polygons) if data is not None else 0
        triangles = triangles_by_mesh[key]
        placement = _placement_mode(original)
        mode = "none" if placement == "retail_blueprint" else _collision_mode(original)
        collision_modes[mode] = collision_modes.get(mode, 0) + 1
        placement_modes[placement] = placement_modes.get(placement, 0) + 1
        slot_names = []
        for slot in getattr(instance.obj, "material_slots", ()):
            material = slot.material
            if material is None:
                continue
            slot_names.append(material.name)
            row = materials.get(material.name)
            if row is None:
                try:
                    base, _ = _base_mode(material)
                except Exception:
                    base = "unknown"
                row = materials[material.name] = {
                    "name": material.name, "surface": _surface_for(material),
                    "base_color": base, "invisible": _material_invisible(material), "objects": 0}
            row["objects"] += 1
        if placement != "retail_blueprint":
            total_triangles += triangles
            for corner in getattr(instance.obj, "bound_box", ()):
                point = _blender_to_game(instance.matrix_world @ Vector(corner))
                for axis in range(3):
                    low[axis] = min(low[axis], point[axis] * options.scale)
                    high[axis] = max(high[axis], point[axis] * options.scale)
        if len(objects) < INSPECT_OBJECT_LIMIT:
            entry = {"name": original.name, "placement_mode": placement, "collision_mode": mode,
                     "triangles": triangles, "materials": slot_names,
                     "instance": bool(instance.is_instance)}
            if placement == "retail_blueprint":
                entry["retail_blueprint"] = _retail_blueprint(original)
                if not entry["retail_blueprint"]:
                    diagnostics.error("retail_blueprint_missing",
                                      "Retail Blueprint Instance has no ObjectBlueprint EBX path",
                                      object=original.name)
            if _pause_map_hidden(original):
                entry["hidden_from_pause_map"] = True
            objects.append(entry)
    entities = [lights, npc_routes, prefabs, audio and (audio["volumes"] or audio["emitters"])]
    if not instances and not any(entities):
        diagnostics.error("no_geometry", "The scene has no mesh objects a map can be built from")
    display_name = options.map_name or source.stem
    bounds = None
    if all(math.isfinite(value) for value in low + high):
        bounds = {"min": _round_list(low, 3), "max": _round_list(high, 3),
                  "size": _round_list([high[axis] - low[axis] for axis in range(3)], 3)}
    _progress(1.0, "complete", "Inspection complete")
    status = "ok" if not diagnostics.errors else "completed_with_errors"
    return {
        "schema": 1,
        "mode": "inspect",
        "status": status,
        "exit_code": EXIT_OK if status == "ok" else EXIT_COMPLETED_WITH_ERRORS,
        "converter_version": VERSION,
        "blender_version": bpy.app.version_string,
        "input": str(source),
        "map_name": options.map_id or _map_id(display_name),
        "display_name": display_name,
        "summary": {
            "scene_objects": len(scene.objects),
            "mesh_instances": len(instances),
            "unique_meshes": len(triangles_by_mesh),
            "triangles": total_triangles,
            "materials": len(materials),
            "spawn_points": len(spawn_markers),
            "travel_points": len(travel_points),
            "lights": lights,
            "audio": audio,
            "npc_routes": npc_routes,
            "interaction_prefabs": prefabs,
            "reflection_probes": reflection_probes,
            "grind_curves": grind_curves,
            "collision_modes": collision_modes,
            "placement_modes": placement_modes,
        },
        "bounds": bounds,
        "spawn": spawn_markers[0] if spawn_markers else None,
        "spawn_points": spawn_markers,
        "travel_points": travel_points,
        "pause_map": ("authored" if _pause_map_authored(source) else
                      "skipped" if _pause_map_skipped(options) else "rendered_" + options.pause_map),
        "objects": objects,
        "objects_truncated": len(instances) > len(objects),
        "materials": sorted(materials.values(), key=lambda row: row["name"].casefold()),
        "warnings": diagnostics.warnings,
        "errors": diagnostics.errors,
        "duration_seconds": round(time.perf_counter() - started, 3),
    }


def _describe():
    # --describe: the converter's contract as JSON, for a driver or an AI that wants to check
    # what this copy supports before calling it.
    return {
        "schema": 1, "mode": "describe", "status": "ok", "exit_code": EXIT_OK,
        "converter_version": VERSION, "blender_version": bpy.app.version_string,
        "minimum_blender": ".".join(map(str, MIN_BLENDER)),
        "tested_blender": [".".join(map(str, TESTED_BLENDER[0])), ".".join(map(str, TESTED_BLENDER[1]))],
        "modes": {"convert": "--input X --output DIR", "inspect": "--input X --inspect (or --dry-run)",
                  "describe": "--describe"},
        "exit_codes": {str(code): text for code, text in EXIT_CODES.items()},
        "error_codes": ERROR_CODES,
        "output_entries": list(OWNED_ENTRIES),
        "progress_line": "RESKATE_MAP_PROGRESS={progress 0..1, phase, message, current?, total?}",
        "result_line": "RESKATE_MAP_RESULT={schema, mode, status, exit_code, ...}",
        "environment": {"RESKATE_MAP_NO_PAUSE_MAP": "1 skips the pause-map render, like --no-pause-map"},
    }


class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    # Usage errors become a coded result instead of argparse's own exit 2.
    def error(self, message):
        raise _UsageError(message)


def _arguments(argv):
    parser = _Parser(
        description="Convert .blend/.fbx scene geometry into a ReSkate map handshake")
    parser.add_argument("--input", help="Source .blend or .fbx (required except with --describe)")
    parser.add_argument("--output",
                        help="Output folder for map.json/meshes/textures: new, empty, or made by this "
                             "converter before (required to convert)")
    parser.add_argument("--inspect", "--dry-run", dest="inspect", action="store_true",
                        help="Report objects, materials, collision modes and spawn points as JSON; "
                             "writes nothing and needs no --output")
    parser.add_argument("--describe", action="store_true",
                        help="Print the converter's exit codes, error codes and modes as JSON")
    parser.add_argument("--no-pause-map", action="store_true",
                        help="Skip the pause-menu map render (the slowest step); the map keeps San Van's")
    parser.add_argument("--result", default=None,
                        help="Also write the RESKATE_MAP_RESULT JSON to this file, on success and failure")
    parser.add_argument("--map-name", default=None,
                        help="Display name; a native-safe identifier is derived from it")
    parser.add_argument("--map-id", default=None,
                        help="Native-safe identifier selected by Studio")
    parser.add_argument("--scale", type=float, default=1.0,
                        help="Positive world scale; Blender metres are 1:1 at 1.0")
    parser.add_argument("--bake-size", type=int, default=1024,
                        help="Square resolution for procedural Base Color baking")
    parser.add_argument("--no-procedural-bake", dest="bake_procedural",
                        action="store_false",
                        help="Diagnose procedural Base Color and use a constant fallback")
    parser.add_argument("--pause-map", choices=("2d", "3d"), default="3d",
                        help="Rendered pause-menu map: flat textures, or with shadows and edge shading")
    # Internal: used by the child Blender that renders the pause map in isolation.
    parser.add_argument("--pause-map-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--pause-map-tiles", type=int, default=4, help=argparse.SUPPRESS)
    parser.add_argument("--pause-map-texture-limit", default="CLAMP_512", help=argparse.SUPPRESS)
    parser.add_argument("--pause-map-samples", default="8", help=argparse.SUPPRESS)
    parser.add_argument("--pause-map-untextured", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--timeout-seconds", type=float, default=CONVERSION_TIMEOUT,
                        help=argparse.SUPPRESS)
    parser.set_defaults(bake_procedural=True)
    return parser.parse_args(argv)


def _version_warnings():
    # Raises for a Blender the converter cannot run on; returns warnings for untested ones.
    version = tuple(bpy.app.version)
    if version < MIN_BLENDER:
        raise MapImportError(
            "blender_unsupported",
            f"Blender {bpy.app.version_string} is too old for the map converter: it needs Blender "
            f"{'.'.join(map(str, MIN_BLENDER))} or newer (tested on 4.2 to 5.2). Install a current "
            "Blender LTS and point the tool at its blender.exe.",
            exit_code=EXIT_UNSUPPORTED_BLENDER, blender_version=bpy.app.version_string)
    low, high = TESTED_BLENDER
    if version < low or version >= high:
        return [("blender_untested",
                 f"Blender {bpy.app.version_string} has not been tested with converter {VERSION} "
                 f"(tested on {'.'.join(map(str, low))} up to {high[0]}.{high[1] - 1}); check the map in game")]
    return []


def _emit_result(result, options_result_file=None):
    # The one result line, always printed; also written to --result when given.
    line = json.dumps(result, separators=(",", ":"), ensure_ascii=False)
    with _OUTPUT_LOCK:
        print("RESKATE_MAP_RESULT=" + line, flush=True)
    if options_result_file:
        try:
            target = Path(options_result_file).expanduser()
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".tmp")
            temporary.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            os.replace(temporary, target)
        except OSError as ex:
            print(f"[ReSkate map import] Could not write --result {options_result_file}: {ex}", flush=True)


def _failure(code, message, exit_code, mode, details=None, trace=None):
    error = {"code": code, "message": message}
    error.update(details or {})
    failure = {"schema": 1, "mode": mode, "status": "failed", "exit_code": exit_code,
               "converter_version": VERSION, "blender_version": bpy.app.version_string,
               # "error" stays the plain message string older drivers read.
               "error": message, "error_code": code, "errors": [error]}
    if trace:
        failure["traceback"] = trace
    return failure


def _result_file_from_argv(argv):
    # --result is honoured even when the rest of the arguments do not parse.
    for index, value in enumerate(argv):
        if value == "--result" and index + 1 < len(argv):
            return argv[index + 1]
        if value.startswith("--result="):
            return value.split("=", 1)[1]
    return None


def main():
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    result_file = _result_file_from_argv(argv)
    mode = "convert"
    try:
        options = _arguments(argv)
        if options.pause_map_only:
            return _pause_map_only(options)
        mode = "describe" if options.describe else "inspect" if options.inspect else "convert"
        if mode == "describe":
            result = _describe()
            _emit_result(result, options.result)
            return EXIT_OK
        if not options.input:
            raise _UsageError("--input is required")
        if mode == "convert" and not options.output:
            raise _UsageError("--output is required to convert (or pass --inspect)")
        for code, message in _STARTUP_ERRORS:
            raise MapImportError(code, message)
        options.version_warnings = _version_warnings()
        result = _inspect(options) if mode == "inspect" else convert(options)
        _emit_result(result, options.result)
        return result["exit_code"]
    except SystemExit:
        raise
    except _UsageError as ex:
        _emit_result(_failure("usage_error", str(ex), EXIT_USAGE, mode), result_file)
        print(f"studio_map_import.py: error: {ex}", file=sys.stderr, flush=True)
        return EXIT_USAGE
    except MapImportError as ex:
        _emit_result(_failure(ex.code, ex.message, ex.exit_code, mode, ex.details), result_file)
        return ex.exit_code
    except Exception as ex:
        _emit_result(_failure("internal_error", f"{type(ex).__name__}: {ex}", EXIT_FAILED, mode,
                              trace=traceback.format_exc()), result_file)
        return EXIT_FAILED


# Instrument only main-thread entry points. Writer work is measured as queue wait.
for _name in (
    "_load_input", "_evaluated_mesh_instances", "_shared_mesh_placements",
    "_triangle_cell_counts", "_extract_part", "_repair_opposed_corner_normals",
    "_collision_arrays_from_source", "_material_record", "_bake_base_colour",
    "_split_part_spatially", "_filter_native_degenerate_triangles",
    "_split_part_for_native_index_width", "_render_pause_map", "_render_pause_map_isolated",
    "_mesh_from_arrays",
):
    globals()[_name] = _timed(_name.lstrip("_"), globals()[_name])
_MeshArrays.__init__ = _timed("mesh_array_extraction", _MeshArrays.__init__)
GlbWriteQueue._complete_one = _timed("glb_queue_wait_and_callback", GlbWriteQueue._complete_one)
for _name in ("image", "alpha_kind", "opacity", "tinted"):
    setattr(TextureStore, _name, _timed("texture_" + _name, getattr(TextureStore, _name)))


if __name__ == "__main__":
    raise SystemExit(main())
