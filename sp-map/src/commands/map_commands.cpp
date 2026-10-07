// Custom maps, levels and layout (reskate_cli: compile-map, map, level-preview, levels, layout, layout-cas, layout-roundtrip, shadow-profiles).
#include "commands/commands.h"
#include "core/engine.h"
#include "core/settings.h"
#include <windows.h>
#include <tlhelp32.h>
#include <algorithm>
#include <cctype>
#include <cmath>
#include <cwctype>
#include <filesystem>
#include <fstream>

namespace fs = std::filesystem;

namespace studio {

namespace {

std::string snake(std::string key) {
    std::replace(key.begin(), key.end(), '-', '_');
    return key;
}

// key=value stdout lines as an object with snake_case keys, numbers as numbers.
Json kv_object(const std::vector<std::string>& lines) {
    Json raw = parse_key_values(lines, false);
    Json out = Json::object();
    for (auto it = raw.begin(); it != raw.end(); ++it) out[snake(it.key())] = *it;
    return out;
}

bool starts_with(std::string_view s, std::string_view prefix) { return s.substr(0, prefix.size()) == prefix; }

std::string lower(std::string s) {
    for (auto& ch : s) ch = static_cast<char>(std::tolower(static_cast<unsigned char>(ch)));
    return s;
}

// Relative paths are resolved against the current folder, because the engine runs in its own folder.
fs::path absolute_path(const std::string& text) {
    fs::path p(utf8_to_wide(text));
    std::error_code ec;
    fs::path abs = fs::absolute(p, ec);
    return ec ? p : abs.lexically_normal();
}

// True when `child` is `parent` or lies inside it (case-insensitive, as Windows paths are).
bool is_inside(const fs::path& child, const fs::path& parent) {
    auto norm = [](const fs::path& p) {
        std::error_code ec;
        fs::path w = fs::weakly_canonical(p, ec);
        std::wstring s = (ec ? p : w).lexically_normal().wstring();
        for (auto& ch : s) ch = static_cast<wchar_t>(std::towlower(ch));
        while (s.size() > 3 && (s.back() == L'\\' || s.back() == L'/')) s.pop_back();
        return s;
    };
    std::wstring c = norm(child), p = norm(parent);
    if (p.empty() || c.size() < p.size() || c.compare(0, p.size(), p) != 0) return false;
    return c.size() == p.size() || c[p.size()] == L'\\' || c[p.size()] == L'/' || p.back() == L'\\';
}

[[noreturn]] void bad_arg(const std::string& param, const std::string& message) {
    throw Error("invalid_arguments", "--" + param + ": " + message, {{"param", param}});
}

bool parse_double(std::string_view text, double& out) {
    std::string s(text);
    while (!s.empty() && s.front() == ' ') s.erase(s.begin());
    while (!s.empty() && s.back() == ' ') s.pop_back();
    if (s.empty()) return false;
    try {
        size_t used = 0;
        out = std::stod(s, &used);
        return used == s.size() && std::isfinite(out);
    } catch (...) {
        return false;
    }
}

// A List param whose items may themselves be comma-separated ("0.5,0.25" or ["0.5","0.25"]).
std::vector<double> number_list(const Json& a, const std::string& param) {
    std::vector<double> out;
    if (!a.contains(param) || a[param].is_null()) return out;
    for (const auto& item : a[param]) {
        std::string s = item.get<std::string>();
        size_t start = 0;
        while (start <= s.size()) {
            size_t comma = s.find(',', start);
            std::string part = s.substr(start, comma == std::string::npos ? std::string::npos : comma - start);
            double d = 0;
            if (!parse_double(part, d)) bad_arg(param, "takes comma-separated numbers, got '" + s + "'");
            if (d < 0) bad_arg(param, "values must not be negative");
            out.push_back(d);
            if (comma == std::string::npos) break;
            start = comma + 1;
        }
    }
    if (out.empty()) bad_arg(param, "needs at least one number");
    return out;
}

std::string join_numbers(const std::vector<double>& values) {
    std::string out;
    for (double v : values) out += (out.empty() ? "" : ",") + Json(v).dump();
    return out;
}

bool skate_running() {
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snap == INVALID_HANDLE_VALUE) return false;
    PROCESSENTRY32W entry{};
    entry.dwSize = sizeof(entry);
    bool found = false;
    for (BOOL ok = Process32FirstW(snap, &entry); ok; ok = Process32NextW(snap, &entry))
        if (_wcsicmp(entry.szExeFile, L"Skate.exe") == 0) { found = true; break; }
    CloseHandle(snap);
    return found;
}

// Runs the engine and turns a failure into Error(code), or Error(not_found_code) when the
// engine's message says something was not found.
EngineRun run_checked(Context& c, const std::vector<std::string>& args, const std::string& code,
                      const std::string& not_found_code = {}, LineHandler on_err = nullptr) {
    EngineRun run = run_engine(c, args, std::move(on_err));
    try {
        require_success(run, code);
    } catch (Error& e) {
        std::string message = lower(e.what());
        if (!not_found_code.empty() &&
            (message.find("not found") != std::string::npos || message.find("could not open") != std::string::npos ||
             message.find("does not exist") != std::string::npos))
            throw Error(not_found_code, e.what(), e.details);
        throw;
    }
    return run;
}

// The engine's layout dump is an indented tree: "key (N)" opens a container of N entries,
// "key = value" is a field, a bare "key" is an empty marker. Arrays of records are printed
// flat, so a container whose first key comes back again is split into one object per record.
struct TreeLine {
    size_t indent;
    std::string key;
    bool container = false;
    long long count = 0;
    bool has_value = false;
    std::string value;
};

TreeLine parse_tree_line(const std::string& raw) {
    TreeLine t;
    t.indent = raw.find_first_not_of(' ');
    if (t.indent == std::string::npos) t.indent = 0;
    std::string body = raw.substr(t.indent);
    if (auto eq = body.find(" = "); eq != std::string::npos) {
        t.key = body.substr(0, eq);
        t.has_value = true;
        t.value = body.substr(eq + 3);
    } else if (body.size() >= 2 && body.back() == '=' && body[body.size() - 2] == ' ') {
        t.key = body.substr(0, body.size() - 2);
        t.has_value = true;
    } else if (auto open = body.rfind(" ("); open != std::string::npos && body.back() == ')') {
        t.key = body.substr(0, open);
        t.container = true;
        try { t.count = std::stoll(body.substr(open + 2, body.size() - open - 3)); } catch (...) { t.container = false; t.key = body; }
    } else {
        t.key = body;
    }
    return t;
}

void add_field(Json& obj, const std::string& key, Json value, bool repeated) {
    if (!repeated) { obj[key] = std::move(value); return; }
    if (!obj.contains(key)) obj[key] = Json::array();
    obj[key].push_back(std::move(value));
}

// Keys that occur more than once in `entries` (they become arrays).
std::vector<std::string> repeated_keys(const std::vector<std::pair<std::string, Json>>& entries, size_t from, size_t to) {
    std::vector<std::string> out;
    for (size_t a = from; a < to; ++a)
        for (size_t b = a + 1; b < to; ++b)
            if (entries[a].first == entries[b].first &&
                std::find(out.begin(), out.end(), entries[a].first) == out.end()) out.push_back(entries[a].first);
    return out;
}

Json to_object(std::vector<std::pair<std::string, Json>>& entries, size_t from, size_t to) {
    auto rep = repeated_keys(entries, from, to);
    Json obj = Json::object();
    for (size_t k = from; k < to; ++k)
        add_field(obj, entries[k].first, std::move(entries[k].second),
                  std::find(rep.begin(), rep.end(), entries[k].first) != rep.end());
    return obj;
}

Json build_entries(const std::vector<TreeLine>& lines, size_t& i, size_t parent_indent, bool top) {
    // Collect direct children first so we can tell flat records apart from plain objects.
    std::vector<std::pair<std::string, Json>> entries;
    while (i < lines.size() && (top || lines[i].indent > parent_indent)) {
        const TreeLine& t = lines[i];
        size_t my_indent = t.indent;
        ++i;
        Json value;
        if (t.has_value) {
            value = typed_value(t.value);
        } else {
            Json children = Json::object();
            if (i < lines.size() && lines[i].indent > my_indent) children = build_entries(lines, i, my_indent, false);
            if (t.container) {
                Json node = Json::object();
                node["count"] = t.count;
                if (children.is_array()) node["items"] = children;
                else for (auto it = children.begin(); it != children.end(); ++it) node[it.key()] = *it;
                value = node;
            } else {
                value = children.empty() ? Json(nullptr) : children;
            }
        }
        entries.emplace_back(t.key, std::move(value));
    }
    bool records = false;
    if (!top && entries.size() > 1)
        for (size_t k = 1; k < entries.size(); ++k)
            if (entries[k].first == entries[0].first) { records = true; break; }
    if (!records) return to_object(entries, 0, entries.size());
    Json arr = Json::array();
    size_t start = 0;
    for (size_t k = 1; k <= entries.size(); ++k)
        if (k == entries.size() || entries[k].first == entries[0].first) {
            arr.push_back(to_object(entries, start, k));
            start = k;
        }
    // A list of single-field records reads better as a list of values.
    bool single = std::all_of(arr.begin(), arr.end(), [](const Json& r) { return r.size() == 1; });
    if (single) {
        Json values = Json::array();
        for (const auto& r : arr) values.push_back(r.begin().value());
        return values;
    }
    return arr;
}

Json parse_layout_tree(const std::vector<std::string>& raw_lines) {
    std::vector<TreeLine> lines;
    for (const auto& l : raw_lines) lines.push_back(parse_tree_line(l));
    size_t i = 0;
    return build_entries(lines, i, 0, true);
}

// Finds a layout.toc from a file path or a folder (folder\layout.toc, then folder\Data\layout.toc).
fs::path resolve_layout_toc(const fs::path& given, const std::string& param) {
    std::error_code ec;
    if (fs::is_regular_file(given, ec)) return given;
    if (fs::is_directory(given, ec)) {
        for (const fs::path& candidate : {given / L"layout.toc", given / L"Data" / L"layout.toc"})
            if (fs::is_regular_file(candidate, ec)) return candidate;
        throw Error("layout_not_found", "No layout.toc in " + path_utf8(given) + " or its Data folder",
                    {{"param", param}, {"path", path_utf8(given)}});
    }
    throw Error("layout_not_found", "layout.toc not found: " + path_utf8(given), {{"param", param}, {"path", path_utf8(given)}});
}

fs::path layout_input(Context& c, const Json& a, const std::string& param) {
    std::string given = arg_string(a, param);
    if (!given.empty()) return resolve_layout_toc(absolute_path(given), param);
    return resolve_layout_toc(c.game_root(a) / L"Data" / L"layout.toc", "game-root");
}

// The game folder for commands that index the game (the engine hashes Skate.exe first).
fs::path require_game(Context& c, const Json& a) {
    fs::path game = c.game_root(a);
    std::error_code ec;
    if (!fs::is_regular_file(game / L"Skate.exe", ec))
        throw Error("not_a_game_root", "No Skate.exe in " + path_utf8(game) + ". Pass the Skate install folder as --game-root.",
                    {{"path", path_utf8(game)}});
    return game;
}

const std::vector<std::string> k_times_of_day = {"morning", "noon", "afternoon", "evening", "night"};

// Progress for compile-map. The engine reports Blender's "[export NN%] ..." lines (sometimes
// several glued on one line), then timestamped steps and "Slicing pause-map tile i of N".
// Everything is mapped onto one rising 0..1 bar so GUI and MCP progress never go backwards.
struct CompileProgress {
    bool scene_input = false;
    double fraction = 0;
    Json warnings = Json::array();
    std::string blender;

    void report(Context& c, double f, const std::string& message) {
        fraction = std::max(fraction, std::min(f, 1.0));
        c.progress(fraction, message);
    }

    void segment(Context& c, std::string s) {
        while (!s.empty() && s.back() == ' ') s.pop_back();
        if (s.empty()) return;
        if (starts_with(lower(s), "warning")) {
            std::string text = s.find(':') != std::string::npos ? s.substr(s.find(':') + 1) : s;
            while (!text.empty() && text.front() == ' ') text.erase(text.begin());
            warnings.push_back(text);
            c.log("warning", text);
            return;
        }
        if (starts_with(s, "blender=")) blender = s.substr(8);
        if (starts_with(s, "[export ")) {
            size_t pct = s.find('%');
            double d = 0;
            if (pct != std::string::npos && parse_double(std::string_view(s).substr(8, pct - 8), d)) {
                std::string msg = s.find("] ") != std::string::npos ? s.substr(s.find("] ") + 2) : "";
                report(c, 0.40 * d / 100.0, "Blender export: " + msg);
                return;
            }
        }
        if (starts_with(s, "[export] finished")) { report(c, 0.40, s); return; }
        if (auto at = s.find("Slicing pause-map tile "); at != std::string::npos) {
            long long n = 0, total = 0;
            if (sscanf_s(s.c_str() + at, "Slicing pause-map tile %lld of %lld", &n, &total) == 2 && total > 0) {
                double base = scene_input ? 0.45 : 0.15;
                report(c, base + (0.85 - base) * static_cast<double>(n) / static_cast<double>(total), s);
                return;
            }
        }
        if (s.find("s] done") != std::string::npos) { report(c, 1.0, s); return; }
        report(c, fraction, s);
    }

    void line(Context& c, std::string_view line) {
        std::string s(line);
        // Split glued "[export NN%]" pieces into separate messages.
        size_t pos = 0;
        while (true) {
            size_t next = s.find("[export", pos + 1);
            if (next == std::string::npos) { segment(c, s.substr(pos)); break; }
            segment(c, s.substr(pos, next - pos));
            pos = next;
        }
    }
};

}  // namespace

void register_map_commands(Registry& r) {
    // ---------------------------------------------------------------- map compile
    r.add({
        .group = "map", .name = "compile",
        .summary = "Compile a Blender/FBX scene or normalized map into a Skate level Patch (optionally deploy it)",
        .description =
            "Builds a custom Skate map into a Frostbite Patch staging folder. Input is a .blend or .fbx scene "
            "(Blender runs headless with ReSkate Studio's studio_map_import.py to make a normalized export first) "
            "or an already normalized map folder holding map.json. The map ID comes from the scene file name: "
            "discmap.blend becomes level dingolevel_reskate_discmap. Extra .fbmod files can be merged into the "
            "same Patch.\n\n"
            "Writes: <staging-root>\\.reskate-studio-staging (ownership marker) and <staging-root>\\Patch\\ "
            "(initfs_win32, layout.toc, Win32\\...toc, cas_01.cas, reskate-build.json, reskate-levels.json; about "
            "20 MB for a tiny map). Also uses %TEMP%\\ReSkateStudio\\MapExport and Cache. Scene input also leaves "
            "assets\\<id>\\export\\map.png in the engine's working folder.\n\n"
            "With deploy=true it ALSO WRITES INTO THE GAME FOLDER: the Patch is installed into "
            "<game>\\Mods\\<mod-folder>. The engine then checks that Skate.exe matches the build (SHA-256), that "
            "staging and game are separate trees, and refuses a Mods folder Studio does not manage. Skate must "
            "not be running.\n\n"
            "Gotchas: a non-empty staging folder without Studio's marker is refused; the staging folder may not "
            "be inside the game folder. cell-size must be 200, 100 or 50 (checked here, the engine only fails "
            "after the whole export). Mismatched lod-ratios/lod-distances counts only warn and the first N of "
            "each are used. Including BAM lighting (the default) ships BAM's Lighting_Global without Enlighten "
            "for every level while installed; in the tested engine build that warning still appears with "
            "--no-bam-lighting. enlighten is experimental. Takes about 10 s for a trivial map, "
            "much longer for real ones. Returns staged file paths, the built level(s) and every count the "
            "engine reports (triangles, cells, LODs, lights, collision, GI, merged mods, conflicts).",
        .params = {
            {"map", ParamType::Path, "A .blend or .fbx scene, or a normalized map folder containing map.json", true, true},
            {"staging-root", ParamType::Path, "Output staging folder, created if missing. A non-empty folder must already be a Studio staging folder", true, true},
            {"mods", ParamType::List, "Extra .fbmod files to merge into the same Patch", false, true},
            {"deploy", ParamType::Boolean, "Install the result into <game>\\Mods (writes into the game folder)", false, false, {}, Json(false)},
            {"mod-folder", ParamType::String, "Mods subfolder name for deploy: 1-64 letters, digits, spaces, _ - or ., not starting with '.'. Ignored without deploy"},
            {"blender", ParamType::Path, "blender.exe for scene input. Defaults to the saved Blender setting, then auto-detection"},
            {"pause-map", ParamType::Enum, "Pause-menu map render style made by the Blender converter (scene input only)", false, false, {"2d", "3d"}, Json("3d")},
            {"time-of-day", ParamType::Enum, "Starting or fixed time of day. Omit for the engine default", false, false, k_times_of_day},
            {"stream", ParamType::Boolean, "--stream streams render meshes through WorldPartition cells, --no-stream keeps them resident. Omit for the engine default (resident for a tiny map)"},
            {"cell-size", ParamType::Integer, "Streaming cell size in metres: 200, 100 or 50"},
            {"cell-load-distance", ParamType::Number, "Streaming cell load distance in metres"},
            {"vista-ratio", ParamType::Number, "Vista proxy simplification ratio"},
            {"vista-error", ParamType::Number, "Vista proxy error in metres"},
            {"lods", ParamType::Boolean, "Generate mesh LODs; --no-lods turns it off", false, false, {}, Json(true)},
            {"lod-ratios", ParamType::List, "LOD triangle ratios, e.g. 0.5,0.25 (comma-separated or repeated)"},
            {"lod-distances", ParamType::List, "LOD switch distances in metres, e.g. 40,120 (same count as lod-ratios)"},
            {"lod-min-triangles", ParamType::Integer, "Minimum triangles for a mesh to get LODs"},
            {"gi", ParamType::Boolean, "Bake GI probes; --no-gi turns it off", false, false, {}, Json(true)},
            {"enlighten", ParamType::Boolean, "Build Enlighten probes and input systems (experimental)", false, false, {}, Json(false)},
            {"enlighten-open-sky", ParamType::Boolean, "Enlighten open-sky mode", false, false, {}, Json(false)},
            {"gi-enlighten-spacing", ParamType::Number, "Enlighten probe spacing in metres (4 gives a 10x2x10 grid on a 40 m map)"},
            {"gi-enlighten-gain", ParamType::Number, "Enlighten gain factor"},
            {"auto-reflection", ParamType::Boolean, "Auto-place reflection probes; --no-auto-reflection turns it off", false, false, {}, Json(true)},
            {"bam-lighting", ParamType::Boolean, "Include BAM's Lighting_Global for live time-of-day swaps; --no-bam-lighting leaves it out", false, false, {}, Json(true)},
            {"live-tod", ParamType::Boolean, "Live time-of-day swapping; --no-live-tod turns it off", false, false, {}, Json(true)},
            {"gi-intensity", ParamType::Number, "GI bake intensity factor"},
            {"gi-rays", ParamType::Integer, "GI bake ray count"},
            {"gi-lods", ParamType::Integer, "GI LOD count, 1-4"},
            {"gi-sky", ParamType::Number, "GI bake sky light in lux"},
            {"gi-sun", ParamType::Number, "GI bake sun light in lux"},
            {"gi-bounce", ParamType::Number, "GI bake bounce factor"},
            {"gi-sun-rotation", ParamType::String, "Sun rotation for the GI bake as x,y degrees, e.g. 45,30"},
            game_root_param(),
        },
        .examples = {
            "map compile C:\\maps\\discmap.blend C:\\maps\\discmap_pkg",
            "map compile C:\\maps\\norm\\discmap C:\\maps\\pkg --time-of-day night --no-gi --stream --cell-size 100 --json",
            "map compile C:\\maps\\discmap.blend C:\\maps\\pkg C:\\mods\\extra.fbmod --deploy --mod-folder \"Disc Map\"",
        },
        .writes_game = true,
        .long_running = true,
        .run = [](Context& c, const Json& a) -> Json {
            std::error_code ec;
            fs::path game = require_game(c, a);
            fs::path map = absolute_path(a["map"].get<std::string>());
            fs::path staging = absolute_path(a["staging-root"].get<std::string>());
            bool deploy = a["deploy"].get<bool>();

            // Input checks the engine only makes late, or not at all.
            bool scene_input = false;
            if (!fs::exists(map, ec))
                throw Error("map_not_found", "Map scene or folder not found: " + path_utf8(map), {{"path", path_utf8(map)}});
            if (fs::is_directory(map, ec)) {
                if (!fs::is_regular_file(map / L"map.json", ec))
                    throw Error("map_invalid", "Folder has no map.json, so it is not a normalized map export: " + path_utf8(map),
                                {{"path", path_utf8(map)}});
            } else {
                std::string ext = lower(path_utf8(map.extension()));
                if (ext != ".blend" && ext != ".fbx") bad_arg("map", "must be a .blend or .fbx scene or a normalized map folder");
                scene_input = true;
            }
            if (fs::exists(staging, ec)) {
                if (!fs::is_directory(staging, ec)) bad_arg("staging-root", "is a file, not a folder");
                bool empty = fs::directory_iterator(staging, ec) == fs::directory_iterator();
                if (!empty && !fs::exists(staging / L".reskate-studio-staging", ec))
                    throw Error("staging_not_owned",
                                "Staging folder is not empty and is not a ReSkate Studio staging folder: " + path_utf8(staging) +
                                ". Use an empty or new folder.", {{"path", path_utf8(staging)}});
            }
            if (is_inside(staging, game) || is_inside(game, staging))
                bad_arg("staging-root", "must be outside the Skate folder (and must not contain it)");
            if (is_inside(map, game)) bad_arg("map", "must not be inside the Skate folder");

            std::vector<std::string> mods;
            if (a.contains("mods"))
                for (const auto& m : a["mods"]) {
                    fs::path p = absolute_path(m.get<std::string>());
                    if (lower(path_utf8(p.extension())) != ".fbmod") bad_arg("mods", "not an .fbmod file: " + path_utf8(p));
                    if (!fs::is_regular_file(p, ec))
                        throw Error("mod_not_found", "Mod file not found: " + path_utf8(p), {{"path", path_utf8(p)}});
                    mods.push_back(path_utf8(p));
                }

            std::string mod_folder = arg_string(a, "mod-folder");
            if (a.contains("mod-folder")) {
                bool ok = !mod_folder.empty() && mod_folder.size() <= 64 && mod_folder.front() != '.';
                for (char ch : mod_folder)
                    if (!(std::isalnum(static_cast<unsigned char>(ch)) || ch == ' ' || ch == '_' || ch == '-' || ch == '.')) ok = false;
                if (!ok) bad_arg("mod-folder", "use 1-64 letters, digits, spaces, _ - or ., not starting with '.'");
                if (!deploy) c.log("warning", "--mod-folder is ignored without --deploy");
            }

            std::string blender = arg_string(a, "blender");
            if (!blender.empty() && !fs::is_regular_file(absolute_path(blender), ec))
                throw Error("blender_not_found", "blender.exe not found: " + blender, {{"path", blender}});

            if (a.contains("cell-size")) {
                long long cs = a["cell-size"];
                if (cs != 200 && cs != 100 && cs != 50) bad_arg("cell-size", "must be 200, 100 or 50");
            }
            for (const char* p : {"cell-load-distance", "vista-ratio", "vista-error", "gi-enlighten-spacing", "gi-enlighten-gain",
                                  "gi-intensity", "gi-sky", "gi-sun", "gi-bounce"})
                if (a.contains(p) && a[p].get<double>() < 0) bad_arg(p, "must not be negative");
            if (a.contains("gi-enlighten-spacing") && a["gi-enlighten-spacing"].get<double>() <= 0)
                bad_arg("gi-enlighten-spacing", "must be more than 0");
            if (a.contains("lod-min-triangles") && a["lod-min-triangles"].get<long long>() < 0)
                bad_arg("lod-min-triangles", "must not be negative");
            if (a.contains("gi-rays") && a["gi-rays"].get<long long>() < 1) bad_arg("gi-rays", "must be at least 1");
            if (a.contains("gi-lods")) {
                long long n = a["gi-lods"];
                if (n < 1 || n > 4) bad_arg("gi-lods", "must be 1 to 4");
            }
            std::string sun = arg_string(a, "gi-sun-rotation");
            if (a.contains("gi-sun-rotation")) {
                auto comma = sun.find(',');
                double x = 0, y = 0;
                if (comma == std::string::npos || !parse_double(sun.substr(0, comma), x) || !parse_double(sun.substr(comma + 1), y))
                    bad_arg("gi-sun-rotation", "takes x,y degrees, e.g. 45,30");
            }
            std::vector<double> lod_ratios = number_list(a, "lod-ratios");
            std::vector<double> lod_distances = number_list(a, "lod-distances");
            if (!lod_ratios.empty() && !lod_distances.empty() && lod_ratios.size() != lod_distances.size())
                c.log("warning", "lod-ratios has " + std::to_string(lod_ratios.size()) + " values and lod-distances has " +
                                 std::to_string(lod_distances.size()) + "; the engine uses the first " +
                                 std::to_string(std::min(lod_ratios.size(), lod_distances.size())) + " of each");
            if (!a["lods"].get<bool>() && (!lod_ratios.empty() || !lod_distances.empty()))
                c.log("warning", "LOD settings are ignored because lods is false");

            if (deploy) {
                if (skate_running())
                    throw Error("game_running", "Skate is running. Close the game before deploying a map.");
            }

            std::vector<std::string> args = {"compile-map", path_utf8(game), path_utf8(map), path_utf8(staging)};
            if (deploy) args.push_back("--deploy");
            if (!mod_folder.empty()) { args.push_back("--mod-folder"); args.push_back(mod_folder); }
            if (!blender.empty()) { args.push_back("--blender"); args.push_back(path_utf8(absolute_path(blender))); }
            if (scene_input) add_option(args, a, "pause-map", "--pause-map");
            add_option(args, a, "time-of-day", "--time-of-day");
            if (a.contains("stream")) args.push_back(a["stream"].get<bool>() ? "--stream" : "--no-stream");
            add_option(args, a, "cell-size", "--cell-size");
            add_option(args, a, "cell-load-distance", "--cell-load-distance");
            add_option(args, a, "vista-ratio", "--vista-ratio");
            add_option(args, a, "vista-error", "--vista-error");
            if (!a["lods"].get<bool>()) args.push_back("--no-lods");
            if (!lod_ratios.empty()) { args.push_back("--lod-ratios"); args.push_back(join_numbers(lod_ratios)); }
            if (!lod_distances.empty()) { args.push_back("--lod-distances"); args.push_back(join_numbers(lod_distances)); }
            add_option(args, a, "lod-min-triangles", "--lod-min-triangles");
            if (!a["gi"].get<bool>()) args.push_back("--no-gi");
            add_flag(args, a, "enlighten", "--enlighten");
            add_flag(args, a, "enlighten-open-sky", "--enlighten-open-sky");
            add_option(args, a, "gi-enlighten-spacing", "--gi-enlighten-spacing");
            add_option(args, a, "gi-enlighten-gain", "--gi-enlighten-gain");
            if (!a["auto-reflection"].get<bool>()) args.push_back("--no-auto-reflection");
            if (!a["bam-lighting"].get<bool>()) args.push_back("--no-bam-lighting");
            if (!a["live-tod"].get<bool>()) args.push_back("--no-live-tod");
            add_option(args, a, "gi-intensity", "--gi-intensity");
            add_option(args, a, "gi-rays", "--gi-rays");
            add_option(args, a, "gi-lods", "--gi-lods");
            add_option(args, a, "gi-sky", "--gi-sky");
            add_option(args, a, "gi-sun", "--gi-sun");
            add_option(args, a, "gi-bounce", "--gi-bounce");
            if (!sun.empty()) { args.push_back("--gi-sun-rotation"); args.push_back(sun); }
            for (const auto& m : mods) args.push_back(m);

            CompileProgress progress;
            progress.scene_input = scene_input;
            c.progress(0, scene_input ? "Exporting the scene with Blender" : "Reading the normalized map");
            EngineRun run = run_checked(c, args, "compile_failed", {},
                                        [&](Context& ctx, std::string_view line) { progress.line(ctx, line); });

            Json kv = kv_object(run.out_lines);
            Json files = Json::object();
            Json stats = Json::object();
            Json result = Json::object();
            result["staging_root"] = path_utf8(staging);
            result["patch_dir"] = path_utf8(staging / L"Patch");
            for (auto it = kv.begin(); it != kv.end(); ++it) {
                const std::string& k = it.key();
                if (k == "toc" || k == "cas" || k == "layout") {
                    // The engine mixes / and \ in these paths.
                    std::string path = it->is_string() ? it->get<std::string>() : it->dump();
                    std::replace(path.begin(), path.end(), '/', '\\');
                    files[k] = path;
                }
                else if (k == "deployed") continue;
                else if (it->is_number()) stats[k] = *it;
                else result[k] = *it;
            }
            // The level(s) the Patch defines, from the manifest the engine writes.
            Json levels = Json::array();
            fs::path manifest = staging / L"Patch" / L"reskate-levels.json";
            if (fs::is_regular_file(manifest, ec)) {
                std::ifstream in(manifest);
                Json parsed = Json::parse(in, nullptr, false);
                if (!parsed.is_discarded()) levels = parsed.is_object() && parsed.contains("levels") ? parsed["levels"] : parsed;
            }
            if (levels.empty() || !levels.is_array()) {
                Json found = Json::array();
                for (const auto& e : fs::directory_iterator(staging / L"Patch" / L"Win32" / L"levels" / L"game", ec)) {
                    std::string name = path_utf8(e.path().filename());
                    if (e.is_directory() && starts_with(name, "dingolevel_reskate_")) found.push_back(name);
                }
                if (!found.empty() || levels.empty()) levels = found;
            }
            result["levels"] = levels;
            result["files"] = files;
            result["stats"] = stats;
            result["deployed"] = deploy;
            if (kv.contains("deployed")) result["deploy_target"] = kv["deployed"];
            if (!progress.blender.empty()) result["blender"] = progress.blender;
            result["warnings"] = progress.warnings;
            result["seconds"] = std::round(run.seconds * 10) / 10;
            return result;
        },
    });

    // ---------------------------------------------------------------- map inspect
    r.add({
        .group = "map", .name = "inspect",
        .summary = "Validate and summarise a normalized map export folder",
        .description =
            "Checks a normalized map export (the folder with map.json, meshes\\, collision\\, textures\\ and ui\\ "
            "that the Blender converter makes) and returns its name and counts: objects, materials, glb files, "
            "vertices, triangles and collision data. A quick pre-flight before map compile with a normalized "
            "folder. Read-only, no game needed. Not for .blend files and not for the game folder. An invalid "
            "map.json fails with the engine's reason (e.g. 'map.json must use Y-up coordinates').",
        .params = {
            {"folder", ParamType::Path, "Normalized map folder (or its map.json)", true, true},
        },
        .examples = {"map inspect C:\\maps\\norm\\discmap", "map inspect C:\\maps\\norm\\discmap\\map.json --json"},
        .run = [](Context& c, const Json& a) -> Json {
            std::error_code ec;
            fs::path folder = absolute_path(a["folder"].get<std::string>());
            if (fs::is_regular_file(folder, ec) && lower(path_utf8(folder.filename())) == "map.json") folder = folder.parent_path();
            if (!fs::is_directory(folder, ec))
                throw Error("map_not_found", "Normalized map folder not found: " + path_utf8(folder), {{"path", path_utf8(folder)}});
            if (!fs::is_regular_file(folder / L"map.json", ec))
                throw Error("map_invalid", "No map.json in " + path_utf8(folder) + ". Point at a normalized map export folder.",
                            {{"path", path_utf8(folder)}});
            EngineRun run = run_checked(c, {"map", path_utf8(folder)}, "map_invalid");
            Json result = {{"folder", path_utf8(folder)}};
            Json kv = kv_object(run.out_lines);
            for (auto it = kv.begin(); it != kv.end(); ++it) result[it.key()] = *it;
            return result;
        },
    });

    // ---------------------------------------------------------------- level list
    r.add({
        .group = "level", .name = "list",
        .summary = "List the game's level containers",
        .description =
            "Indexes the game and lists level containers (level, sublevel, layer and schematic EBX assets) in "
            "index order (not sorted), plus the total count (about 6000). Use the names with level preview. "
            "limit 0 returns only the count. Read-only, takes a few seconds for indexing.",
        .params = {
            {"limit", ParamType::Integer, "Most names to return; 0 returns only the count", false, false, {}, Json(20)},
            game_root_param(),
        },
        .examples = {"level list", "level list --limit 100000 --json", "level list --limit 0"},
        .long_running = true,
        .run = [](Context& c, const Json& a) -> Json {
            long long limit = a["limit"];
            if (limit < 0) bad_arg("limit", "must not be negative");
            EngineRun run = run_checked(c, {"levels", path_utf8(require_game(c, a)), std::to_string(limit)}, "engine_failed");
            Json levels = Json::array();
            long long total = 0;
            for (const auto& line : run.out_lines) {
                if (starts_with(line, "level-containers=")) { total = std::stoll(line.substr(17)); continue; }
                levels.push_back(line);
            }
            return {{"total", total}, {"count", levels.size()}, {"levels", levels}};
        },
    });

    // ---------------------------------------------------------------- level preview
    r.add({
        .group = "level", .name = "preview",
        .summary = "Count what a game level contains (placements, meshes, triangles, materials)",
        .description =
            "Loads one level EBX from the game and reports what a preview of it would hold: placements, unique "
            "and expanded meshes, vertices and triangles, material variants and warnings, visited objects and "
            "containers. Statistics only, no image. Read-only; about 3 s, mostly indexing. Get level names "
            "from level list.",
        .params = {
            {"level", ParamType::String, "Level EBX asset name, e.g. levels/game/bam_levelroot/commlot_piers_02_streetpark_06", true, true},
            game_root_param(),
        },
        .examples = {"level preview levels/game/bam_levelroot/commlot_piers_02_streetpark_06",
                     "level preview levels/game/bam_levelroot/commlot_piers_02_streetpark_06 --json"},
        .long_running = true,
        .run = [](Context& c, const Json& a) -> Json {
            std::string level = a["level"];
            std::replace(level.begin(), level.end(), '\\', '/');
            EngineRun run = run_checked(c, {"level-preview", path_utf8(require_game(c, a)), level}, "engine_failed", "level_not_found");
            Json result = {{"level", level}};
            Json kv = kv_object(run.out_lines);
            for (auto it = kv.begin(); it != kv.end(); ++it) result[it.key()] = *it;
            return result;
        },
    });

    // ---------------------------------------------------------------- level shadow-profiles
    r.add({
        .group = "level", .name = "shadow-profiles",
        .summary = "Count the game's native shadow-profile data that map compile reuses",
        .description =
            "Indexes the game and counts the native shadow-profile bundles, assets and components that map "
            "compile reuses for custom maps (its shadow_profile_* stats). Read-only, about 3 s.",
        .params = {game_root_param()},
        .examples = {"level shadow-profiles", "level shadow-profiles --json"},
        .long_running = true,
        .run = [](Context& c, const Json& a) -> Json {
            EngineRun run = run_checked(c, {"shadow-profiles", path_utf8(require_game(c, a))}, "engine_failed");
            return kv_object(run.out_lines);
        },
    });

    // ---------------------------------------------------------------- layout info
    r.add({
        .group = "layout", .name = "info",
        .summary = "Show the game's Data\\layout.toc as a tree",
        .description =
            "Reads <game-root>\\Data\\layout.toc (the Frostbite layout) and returns it as a tree: superBundles, "
            "fs, installManifest (installChunks with id, name, installBundle, flags, sizes, language; "
            "installGroups; settings) and meta ownables. Containers show their entry count; repeated records "
            "become arrays. Read-only. game-root can be any folder holding Data\\layout.toc, e.g. a staged "
            "package arranged that way; it always reads Data\\layout.toc, never Patch\\layout.toc.",
        .params = {game_root_param()},
        .examples = {"layout info", "layout info --game-root C:\\dev\\fakeroot --json"},
        .run = [](Context& c, const Json& a) -> Json {
            std::error_code ec;
            fs::path root = c.game_root(a);
            fs::path toc = root / L"Data" / L"layout.toc";
            if (!fs::is_regular_file(toc, ec))
                throw Error("layout_not_found", "No Data\\layout.toc in " + path_utf8(root), {{"path", path_utf8(toc)}});
            EngineRun run = run_checked(c, {"layout", path_utf8(root)}, "layout_invalid");
            return {{"file", path_utf8(toc)}, {"layout", parse_layout_tree(run.out_lines)}};
        },
    });

    // ---------------------------------------------------------------- layout cas
    r.add({
        .group = "layout", .name = "cas",
        .summary = "List the CAS archive tables inside a layout.toc",
        .description =
            "Decodes the layeredInstallChunkFiles and unlayeredInstallChunkFiles tables of a layout.toc: each "
            "record's archive number, catalog and padding. Useful to check that a staged Patch\\layout.toc "
            "references the expected cas archives. Takes a layout.toc file or a folder holding one (folder\\"
            "layout.toc, then folder\\Data\\layout.toc); without one it reads the game's Data\\layout.toc. Read-only.",
        .params = {
            {"toc", ParamType::Path, "layout.toc file, or a folder such as a staged Patch folder. Default: <game-root>\\Data\\layout.toc", false, true},
            game_root_param(),
        },
        .examples = {"layout cas", "layout cas C:\\maps\\pkg\\Patch --json"},
        .run = [](Context& c, const Json& a) -> Json {
            fs::path toc = layout_input(c, a, "toc");
            EngineRun run = run_checked(c, {"layout-cas", path_utf8(toc)}, "layout_invalid");
            Json tables = Json::object();
            Json* current = nullptr;
            for (const auto& line : run.out_lines) {
                if (line.empty()) continue;
                if (line[0] != ' ') {
                    auto space = line.find(' ');
                    std::string name = line.substr(0, space);
                    Json table = {{"bytes", nullptr}, {"records", Json::array()}};
                    if (space != std::string::npos) {
                        Json kv = kv_object({line.substr(space + 1)});
                        if (kv.contains("bytes")) table["bytes"] = kv["bytes"];
                    }
                    tables[name] = table;
                    current = &tables[name];
                    continue;
                }
                if (!current) continue;
                Json record = Json::object();
                std::string rest = line;
                size_t pos = 0;
                while (pos < rest.size()) {
                    pos = rest.find_first_not_of(' ', pos);
                    if (pos == std::string::npos) break;
                    size_t end = rest.find(' ', pos);
                    std::string tok = rest.substr(pos, end == std::string::npos ? std::string::npos : end - pos);
                    if (auto eq = tok.find('='); eq != std::string::npos) record[tok.substr(0, eq)] = typed_value(tok.substr(eq + 1));
                    pos = end == std::string::npos ? rest.size() : end;
                }
                (*current)["records"].push_back(record);
            }
            for (auto& t : tables) t["count"] = t["records"].size();
            return {{"file", path_utf8(toc)}, {"tables", tables}};
        },
    });

    // ---------------------------------------------------------------- layout roundtrip
    r.add({
        .group = "layout", .name = "roundtrip",
        .summary = "Check that a layout.toc re-encodes byte for byte (optionally write the result)",
        .description =
            "Parses a layout.toc and encodes it again to check the encoder is lossless; reports source and "
            "encoded sizes and whether the bodies are identical. With output it writes the re-encoded file "
            "there (parent folders are created). The input is never changed. The output may not be inside the "
            "game folder or be the input itself. Takes a layout.toc file or a folder holding one; without one it "
            "reads the game's Data\\layout.toc.",
        .params = {
            {"toc", ParamType::Path, "layout.toc file or a folder holding one. Default: <game-root>\\Data\\layout.toc", false, true},
            {"output", ParamType::Path, "Where to write the re-encoded layout.toc. Omit to only compare", false, true},
            game_root_param(),
        },
        .examples = {"layout roundtrip", "layout roundtrip C:\\maps\\pkg\\Patch\\layout.toc C:\\temp\\rt.toc --json"},
        .run = [](Context& c, const Json& a) -> Json {
            std::error_code ec;
            fs::path toc = layout_input(c, a, "toc");
            std::vector<std::string> args = {"layout-roundtrip", path_utf8(toc)};
            std::string out_text = arg_string(a, "output");
            fs::path output;
            if (!out_text.empty()) {
                output = absolute_path(out_text);
                if (fs::is_directory(output, ec)) bad_arg("output", "is a folder; give a file path such as rt.toc");
                if (is_inside(output, toc) || is_inside(toc, output)) bad_arg("output", "must not be the input file");
                fs::path game = c.settings.game_root;
                if (a.contains("game-root")) game = absolute_path(a["game-root"].get<std::string>());
                if (!game.empty() && is_inside(output, game)) bad_arg("output", "must be outside the Skate folder");
                fs::create_directories(output.parent_path(), ec);
                args.push_back(path_utf8(output));
            }
            EngineRun run = run_checked(c, args, "layout_invalid");
            Json kv = kv_object(run.out_lines);
            Json result = {{"file", path_utf8(toc)}};
            for (auto it = kv.begin(); it != kv.end(); ++it) result[it.key()] = *it;
            result["output"] = output.empty() ? Json(nullptr) : Json(path_utf8(output));
            if (result.contains("body_identical") && result["body_identical"] == false)
                c.log("warning", "the re-encoded layout differs from the source");
            return result;
        },
    });
}
}
