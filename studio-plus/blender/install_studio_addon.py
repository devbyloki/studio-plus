from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import sys
import traceback
import uuid

import addon_utils
import bpy


EXPECTED_MODULE = "sk8_map_export"


def _progress(amount: float, message: str) -> None:
    print(f"CUSTOMMAP_PROGRESS\t{amount:.3f}\t{message}", flush=True)


def _write_atomic(path: str, value: dict) -> None:
    destination = os.path.abspath(path)
    parent = os.path.dirname(destination)
    if not parent:
        raise ValueError("The installer result path has no parent directory")
    os.makedirs(parent, exist_ok=True)
    temporary = destination + "." + uuid.uuid4().hex + ".tmp"
    try:
        with open(temporary, "x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def _replace_atomic(path: str, contents: bytes) -> None:
    temporary = path + "." + uuid.uuid4().hex + ".tmp"
    try:
        with open(temporary, "xb") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def _forget_module(module: str) -> None:
    doomed = [name for name in sys.modules
              if name == module or name.startswith(module + ".")]
    for name in doomed:
        sys.modules.pop(name, None)
    importlib.invalidate_caches()


def _disable(module: str, default_set: bool) -> None:
    try:
        addon_utils.disable(module, default_set=default_set)
    except Exception:


        pass
    _forget_module(module)


def _version_text(module_object) -> str:
    info = getattr(module_object, "bl_info", None)
    version = info.get("version") if isinstance(info, dict) else None
    if isinstance(version, (tuple, list)) and version:
        return ".".join(str(int(part)) for part in version)
    return "unknown"


def _parse_args() -> argparse.Namespace:
    arguments = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--addon", required=True)
    parser.add_argument("--module", default=EXPECTED_MODULE)
    parser.add_argument("--result", required=True)
    parser.add_argument("--vfx-catalog")
    return parser.parse_args(arguments)


def _install(args: argparse.Namespace) -> dict:
    if args.module != EXPECTED_MODULE:
        raise ValueError("Refusing to install an unexpected Blender module")
    source = os.path.realpath(os.path.abspath(args.addon))
    if os.path.basename(source).lower() != EXPECTED_MODULE + ".py":
        raise ValueError("The bundled add-on must be named sk8_map_export.py")
    if not os.path.isfile(source):
        raise FileNotFoundError("The bundled Blender add-on is missing: " + source)
    with open(source, "rb") as stream:
        contents = stream.read()
    if not contents:
        raise ValueError("The bundled Blender add-on is empty")


    compile(contents, source, "exec")
    source_hash = hashlib.sha256(contents).hexdigest()
    addon_directory = bpy.utils.user_resource("SCRIPTS", path="addons", create=True)
    if not addon_directory or not os.path.isdir(addon_directory):
        raise RuntimeError("Blender did not provide a writable user add-on directory")
    addon_directory = os.path.realpath(addon_directory)


    sys.path[:] = [path for path in sys.path if not path or os.path.realpath(path) != addon_directory]
    sys.path.insert(0, addon_directory)
    destination = os.path.realpath(os.path.join(addon_directory, EXPECTED_MODULE + ".py"))
    if os.path.dirname(destination) != addon_directory:
        raise RuntimeError("Resolved add-on destination escaped Blender's user add-on directory")
    package_conflict = os.path.join(addon_directory, EXPECTED_MODULE)
    if os.path.isdir(package_conflict):
        raise RuntimeError(
            "A folder-based sk8_map_export add-on conflicts with Studio's single-file add-on. "
            "Remove that older folder in Blender Preferences, then install again.")

    previous = None
    if os.path.isfile(destination):
        with open(destination, "rb") as stream:
            previous = stream.read()
    was_enabled = EXPECTED_MODULE in bpy.context.preferences.addons
    catalog_destination = os.path.join(bpy.utils.user_resource("CONFIG", create=True), "sk8_vfx_catalog.json")
    catalog_contents = None
    previous_catalog = None
    if args.vfx_catalog:
        with open(args.vfx_catalog, "rb") as stream:
            catalog_contents = stream.read()
        if os.path.isfile(catalog_destination):
            with open(catalog_destination, "rb") as stream:
                previous_catalog = stream.read()
    _progress(0.25, "installing bundled Skate Map authoring add-on")

    try:
        if was_enabled:
            _disable(EXPECTED_MODULE, default_set=False)
        _replace_atomic(destination, contents)
        _forget_module(EXPECTED_MODULE)
        enable_errors = []
        module_object = addon_utils.enable(
            EXPECTED_MODULE, default_set=True, persistent=True,
            handle_error=enable_errors.append)
        if module_object is None or EXPECTED_MODULE not in bpy.context.preferences.addons:
            if enable_errors:
                raise RuntimeError("Could not enable sk8_map_export: " +
                                   str(enable_errors[-1])) from enable_errors[-1]
            raise RuntimeError("Blender did not enable sk8_map_export")
        loaded = os.path.realpath(os.path.abspath(getattr(module_object, "__file__", "")))
        if loaded != destination:
            raise RuntimeError("Blender loaded a different sk8_map_export copy: " + loaded)
        catalog_count = 0
        if catalog_contents is not None:
            catalog_count = len(module_object._read_vfx_catalog(args.vfx_catalog))
            _replace_atomic(catalog_destination, catalog_contents)
            module_object._load_vfx_catalog()
        result = bpy.ops.wm.save_userpref()
        if "FINISHED" not in result:
            raise RuntimeError("Blender could not save the enabled add-on preference")
        _progress(0.90, "Skate Map add-on enabled; Blender preferences saved")
        return {
            "ok": True,
            "module": EXPECTED_MODULE,
            "version": _version_text(module_object),
            "sourceSha256": source_hash,
            "installedFile": destination,
            "blenderVersion": bpy.app.version_string,
            "vfxEntries": catalog_count,
            "vfxCatalogFile": catalog_destination if catalog_contents is not None else "",
            "vfxCatalogSha256": hashlib.sha256(catalog_contents).hexdigest() if catalog_contents is not None else "",
        }
    except Exception:


        if catalog_contents is not None:
            if previous_catalog is not None:
                _replace_atomic(catalog_destination, previous_catalog)
            elif os.path.isfile(catalog_destination):
                os.remove(catalog_destination)
        _disable(EXPECTED_MODULE, default_set=True)
        if previous is None:
            if os.path.isfile(destination):
                os.remove(destination)
        else:
            _replace_atomic(destination, previous)
            if was_enabled:
                _forget_module(EXPECTED_MODULE)
                addon_utils.enable(EXPECTED_MODULE, default_set=True, persistent=True)
        try:
            bpy.ops.wm.save_userpref()
        except Exception:
            pass
        raise


def main() -> int:
    args = _parse_args()
    try:
        report = _install(args)
        _write_atomic(args.result, report)
        print("RESKATE_ADDON_RESULT=" + json.dumps(report, sort_keys=True), flush=True)
        _progress(1.0, "Blender add-on installation complete")
        return 0
    except Exception as error:
        report = {
            "ok": False,
            "module": EXPECTED_MODULE,
            "error": str(error) or error.__class__.__name__,
            "details": traceback.format_exc(),
        }
        try:
            _write_atomic(args.result, report)
        except Exception as report_error:
            print("Could not write installer result: " + str(report_error), file=sys.stderr)
        print("RESKATE_ADDON_RESULT=" + json.dumps(report, sort_keys=True), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

