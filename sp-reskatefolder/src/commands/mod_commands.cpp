// Projects and mods (reskate_cli: compile-mod, deploy-mod, project-info, fbmod-info, validate, patch-audit,
// initfs-info, lua), plus "mod list", which reads <game>\Mods directly without the engine.
#include "commands/commands.h"
#include "core/engine.h"
#include "core/settings.h"
#include <windows.h>
#include <bcrypt.h>
#include <tlhelp32.h>
#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <cwctype>
#include <format>
#include <iterator>
#include <optional>
#include <vector>
#include <filesystem>
#include <fstream>
#include <initializer_list>
#include <sstream>
#include <utility>

#pragma comment(lib, "bcrypt.lib")

namespace fs = std::filesystem;

namespace studio {

namespace {
// ---- small helpers ---------------------------------------------------------------------------

// The engine runs with its own folder as the working directory, so every path handed to it must be
// absolute. The CLI makes Path params absolute, but not List items, and MCP clients may send relative paths.
fs::path absolute_path(const fs::path& p) {
    if (p.empty()) return p;
    std::error_code ec;
    fs::path out = fs::absolute(p, ec);
    return ec ? p : out.lexically_normal();
}

fs::path to_path(const Json& args, const std::string& param) { return absolute_path(fs::path(utf8_to_wide(arg_string(args, param)))); }

std::wstring lower(std::wstring s) {
    for (auto& ch : s) ch = static_cast<wchar_t>(std::towlower(static_cast<wint_t>(ch)));
    return s;
}

std::string lower_ascii(std::string s) {
    for (auto& ch : s) ch = static_cast<char>(std::tolower(static_cast<unsigned char>(ch)));
    return s;
}

// "decoded-assets" -> "decoded_assets", "gameRoot" -> "game_root".
std::string snake(std::string_view key) {
    std::string out;
    for (char ch : key) {
        if (ch == '-' || ch == ' ') { out.push_back('_'); continue; }
        if (ch >= 'A' && ch <= 'Z') {
            if (!out.empty() && out.back() != '_') out.push_back('_');
            out.push_back(static_cast<char>(ch - 'A' + 'a'));
            continue;
        }
        out.push_back(ch);
    }
    return out;
}

// Splits "key=value" (value may contain spaces and '='). Returns false for other lines.
bool split_kv(std::string_view line, std::string& key, std::string& value) {
    auto eq = line.find('=');
    if (eq == std::string_view::npos || eq == 0) return false;
    std::string_view k = line.substr(0, eq);
    if (k.find(' ') != std::string_view::npos) return false;
    key.assign(k);
    value.assign(line.substr(eq + 1));
    return true;
}

fs::path normalised(const fs::path& p) {
    std::error_code ec;
    fs::path out = fs::weakly_canonical(fs::absolute(p, ec), ec);
    if (ec) out = fs::absolute(p, ec);
    return out.lexically_normal();
}

// True when `child` is `parent` or lies inside it (case-insensitive, after resolving).
bool is_within(const fs::path& child, const fs::path& parent) {
    std::wstring c = lower(normalised(child).native()), p = lower(normalised(parent).native());
    while (p.size() > 3 && (p.back() == L'\\' || p.back() == L'/')) p.pop_back();
    while (c.size() > 3 && (c.back() == L'\\' || c.back() == L'/')) c.pop_back();
    if (c == p) return true;
    if (c.size() <= p.size() || c.compare(0, p.size(), p) != 0) return false;
    return c[p.size()] == L'\\' || c[p.size()] == L'/' || p.back() == L'\\';
}

bool path_exists(const fs::path& p) { std::error_code ec; return fs::exists(p, ec); }
bool dir_exists(const fs::path& p) { std::error_code ec; return fs::is_directory(p, ec); }
bool file_exists(const fs::path& p) { std::error_code ec; return fs::is_regular_file(p, ec); }

std::string read_text(const fs::path& p) {
    std::ifstream f(p, std::ios::binary);
    std::ostringstream ss;
    ss << f.rdbuf();
    return ss.str();
}

void require_file(const fs::path& p, const std::string& param) {
    if (!path_exists(p)) throw Error("path_not_found", "File does not exist: " + path_utf8(p), {{"param", param}, {"path", path_utf8(p)}});
    if (!file_exists(p)) throw Error("not_a_file", "Not a file: " + path_utf8(p), {{"param", param}, {"path", path_utf8(p)}});
}

// Checks a Skate folder before the engine spends 3 s loading it and fails with a vaguer message.
fs::path checked_game_root(const Context& c, const Json& a, bool need_exe = true, bool need_data = true) {
    fs::path root = absolute_path(c.game_root(a));
    std::string s = path_utf8(root);
    if (!dir_exists(root)) throw Error("game_root_not_found", "Skate folder does not exist: " + s, {{"game_root", s}});
    if (need_exe && !file_exists(root / L"Skate.exe"))
        throw Error("not_a_game_root", "No Skate.exe in " + s + " (pass --game-root or run: studio-plus studio set game-root <folder>)", {{"game_root", s}});
    if (need_data && !dir_exists(root / L"Data"))
        throw Error("not_a_game_root", "No Data folder in " + s, {{"game_root", s}});
    return root;
}

// require_success, but with a stable error code for engine messages we know.
void require_ok(const EngineRun& run, std::initializer_list<std::pair<const char*, const char*>> known) {
    try {
        require_success(run);
    } catch (Error& e) {
        std::string msg = e.what();
        if (msg.find("Skate Oodle codec was not found") != std::string::npos) e.code = "oodle_missing";
        else if (msg.find("for SHA-256") != std::string::npos) e.code = "not_a_game_root";
        else if (msg.find("could not open binary input") != std::string::npos) e.code = "file_unreadable";
        for (const auto& [needle, code] : known)
            if (msg.find(needle) != std::string::npos) { e.code = code; break; }
        throw;
    }
}

// Engine key=value output with snake_case keys and numbers as numbers. Other lines go to `other`.
Json kv_object(const std::vector<std::string>& lines, Json* other = nullptr) {
    Json out = Json::object();
    for (const auto& line : lines) {
        std::string k, v;
        if (split_kv(line, k, v)) out[snake(k)] = typed_value(v);
        else if (other) other->push_back(line);
    }
    return out;
}

Json path_or_null(const std::string& v) { return v.empty() ? Json(nullptr) : Json(v); }

// ---- SHA-256 of a file (Windows CNG) -----------------------------------------------------------

std::string sha256_file(const fs::path& p) {
    BCRYPT_ALG_HANDLE alg = nullptr;
    BCRYPT_HASH_HANDLE hash = nullptr;
    if (!BCRYPT_SUCCESS(BCryptOpenAlgorithmProvider(&alg, BCRYPT_SHA256_ALGORITHM, nullptr, 0)))
        throw Error("hash_failed", "SHA-256 is not available");
    std::string hex;
    bool ok = BCRYPT_SUCCESS(BCryptCreateHash(alg, &hash, nullptr, 0, nullptr, 0, 0));
    std::ifstream f(p, std::ios::binary);
    ok = ok && f.is_open();
    std::vector<char> buf(1 << 20);
    while (ok && f) {
        f.read(buf.data(), static_cast<std::streamsize>(buf.size()));
        auto n = f.gcount();
        if (n > 0) ok = BCRYPT_SUCCESS(BCryptHashData(hash, reinterpret_cast<PUCHAR>(buf.data()), static_cast<ULONG>(n), 0));
    }
    unsigned char digest[32] = {};
    ok = ok && BCRYPT_SUCCESS(BCryptFinishHash(hash, digest, sizeof digest, 0));
    if (hash) BCryptDestroyHash(hash);
    BCryptCloseAlgorithmProvider(alg, 0);
    if (!ok) throw Error("hash_failed", "Could not hash " + path_utf8(p), {{"path", path_utf8(p)}});
    static const char* digits = "0123456789abcdef";
    for (unsigned char b : digest) { hex.push_back(digits[b >> 4]); hex.push_back(digits[b & 15]); }
    return hex;
}

// ---- Is Skate running from this folder? --------------------------------------------------------

bool skate_running_from(const fs::path& game_root) {
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snap == INVALID_HANDLE_VALUE) return false;
    PROCESSENTRY32W pe{};
    pe.dwSize = sizeof pe;
    bool found = false;
    for (BOOL more = Process32FirstW(snap, &pe); more && !found; more = Process32NextW(snap, &pe)) {
        if (lower(pe.szExeFile) != L"skate.exe") continue;
        HANDLE proc = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pe.th32ProcessID);
        if (!proc) { found = true; break; }  // cannot tell where it runs from: assume this install
        wchar_t image[MAX_PATH * 4];
        DWORD size = static_cast<DWORD>(std::size(image));
        if (QueryFullProcessImageNameW(proc, 0, image, &size)) found = is_within(fs::path(image), game_root);
        else found = true;
        CloseHandle(proc);
    }
    CloseHandle(snap);
    return found;
}

// ---- Staging and deployed-mod markers ------------------------------------------------------------

// Reads "skate_sha256=<hex>" from a .reskate-studio-patch marker. "" if absent.
std::string marker_sha(const fs::path& marker) {
    if (!file_exists(marker)) return {};
    std::istringstream in(read_text(marker));
    std::string line;
    while (std::getline(in, line)) {
        if (!line.empty() && line.back() == '\r') line.pop_back();
        if (line.rfind("skate_sha256=", 0) == 0) return line.substr(13);
    }
    return {};
}

std::optional<Json> read_json_file(const fs::path& p, std::string* error = nullptr) {
    if (!file_exists(p)) return std::nullopt;
    try {
        return Json::parse(read_text(p));
    } catch (const std::exception& e) {
        if (error) *error = e.what();
        return std::nullopt;
    }
}

// ---- Frostbite DbObject (the .ReSkateStudio-last-deploy.db receipt) -----------------------------

struct DbReader {
    const std::string& d;
    size_t i = 0;
    bool ok = true;
    uint64_t varint() {
        uint64_t v = 0;
        for (int shift = 0; i < d.size() && shift < 64; shift += 7) {
            auto b = static_cast<unsigned char>(d[i++]);
            v |= static_cast<uint64_t>(b & 0x7f) << shift;
            if (!(b & 0x80)) return v;
        }
        ok = false;
        return 0;
    }
    std::string cstring() {
        auto end = d.find('\0', i);
        if (end == std::string::npos) { ok = false; return {}; }
        std::string s = d.substr(i, end - i);
        i = end + 1;
        return s;
    }
    std::string bytes(size_t n) {
        if (i + n > d.size()) { ok = false; return {}; }
        std::string s = d.substr(i, n);
        i += n;
        return s;
    }
    static std::string hex(const std::string& b) {
        static const char* digits = "0123456789abcdef";
        std::string out;
        for (char ch : b) { auto u = static_cast<unsigned char>(ch); out.push_back(digits[u >> 4]); out.push_back(digits[u & 15]); }
        return out;
    }
    // Reads the value of an entry whose type byte was already consumed.
    Json value(unsigned char type) {
        switch (type & 0x1f) {
        case 0x01: case 0x02: {  // list / object
            size_t size = static_cast<size_t>(varint());
            size_t end = i + size;
            if (!ok || end > d.size()) { ok = false; return nullptr; }
            Json out = (type & 0x1f) == 0x02 ? Json::object() : Json::array();
            while (ok && i < end) {
                auto t = static_cast<unsigned char>(d[i++]);
                if (t == 0) break;
                std::string name = (t & 0x80) ? std::string() : cstring();
                Json v = value(t);
                if (out.is_object()) out[snake(name)] = v; else out.push_back(v);
            }
            i = end;
            return out;
        }
        case 0x06: { auto b = bytes(1); return ok ? Json(b[0] != 0) : Json(nullptr); }
        case 0x07: {
            std::string s = bytes(static_cast<size_t>(varint()));
            if (!s.empty() && s.back() == '\0') s.pop_back();
            return s;
        }
        case 0x08: { auto b = bytes(4); if (!ok) return nullptr; int32_t v = 0; memcpy(&v, b.data(), 4); return v; }
        case 0x09: { auto b = bytes(8); if (!ok) return nullptr; int64_t v = 0; memcpy(&v, b.data(), 8); return v; }
        case 0x0b: { auto b = bytes(4); if (!ok) return nullptr; float v = 0; memcpy(&v, b.data(), 4); return v; }
        case 0x0c: { auto b = bytes(8); if (!ok) return nullptr; double v = 0; memcpy(&v, b.data(), 8); return v; }
        case 0x0f: return hex(bytes(16));
        case 0x10: return hex(bytes(20));
        case 0x13: return hex(bytes(static_cast<size_t>(varint())));
        default: ok = false; return nullptr;
        }
    }
};

Json read_receipt(const fs::path& p) {
    if (!file_exists(p)) return nullptr;
    std::string data = read_text(p);
    if (data.empty()) return nullptr;
    DbReader r{data};
    auto type = static_cast<unsigned char>(data[r.i++]);
    Json v = r.value(type);
    if (!r.ok || !v.is_object()) return {{"path", path_utf8(p)}, {"error", "receipt format not recognised"}};
    v["path"] = path_utf8(p);
    for (const char* key : {"game_root", "mod_root", "backup_root", "retired_patch", "displaced_root"})
        if (v.contains(key) && v[key].is_string()) {
            std::string s = v[key];
            if (s.empty()) v[key] = nullptr;
            else { for (auto& ch : s) if (ch == '/') ch = '\\'; v[key] = s; }
        }
    return v;
}

// ---- mods.json: match entries to folders without assuming one fixed layout -----------------------

std::vector<std::pair<std::string, Json>> mods_json_entries(const Json& j) {
    std::vector<std::pair<std::string, Json>> out;  // (key name if the entry came from an object map, entry)
    const Json* list = &j;
    if (j.is_object()) {
        for (const char* k : {"mods", "Mods", "installed", "entries"})
            if (j.contains(k) && (j[k].is_array() || j[k].is_object())) { list = &j[k]; break; }
    }
    if (list->is_array()) for (const auto& e : *list) out.emplace_back("", e);
    else if (list->is_object()) for (auto it = list->begin(); it != list->end(); ++it) out.emplace_back(it.key(), it.value());
    return out;
}

bool entry_names_folder(const std::string& key, const Json& entry, const std::string& folder_lower) {
    auto matches = [&](std::string s) {
        for (auto& ch : s) if (ch == '/') ch = '\\';
        auto slash = s.find_last_of('\\');
        if (slash != std::string::npos && slash + 1 < s.size()) s = s.substr(slash + 1);
        else if (slash != std::string::npos) { s.pop_back(); auto s2 = s.find_last_of('\\'); if (s2 != std::string::npos) s = s.substr(s2 + 1); }
        return lower_ascii(s) == folder_lower;
    };
    if (!key.empty() && matches(key)) return true;
    if (entry.is_string()) return matches(entry.get<std::string>());
    if (entry.is_object())
        for (const char* k : {"name", "folder", "dir", "directory", "path", "id", "mod", "modFolder", "folderName"})
            if (entry.contains(k) && entry[k].is_string() && matches(entry[k].get<std::string>())) return true;
    return false;
}

Json describe_mod_folder(const fs::path& dir) {
    Json m;
    m["name"] = path_utf8(dir.filename());
    m["path"] = path_utf8(dir);
    fs::path marker = dir / L".reskate-studio-patch";
    bool managed = file_exists(marker);
    bool layout = file_exists(dir / L"layout.toc");
    m["managed_by_reskate"] = managed;
    m["kind"] = managed ? "reskate_native_patch" : layout ? "native_patch" : "unknown";
    m["has_layout_toc"] = layout;
    m["has_initfs"] = file_exists(dir / L"initfs_win32");
    std::string sha = marker_sha(marker);
    m["skate_sha256"] = path_or_null(sha);
    long long files = 0;
    unsigned long long bytes = 0;
    std::error_code ec;
    for (fs::recursive_directory_iterator it(dir, fs::directory_options::skip_permission_denied, ec), end; !ec && it != end; it.increment(ec)) {
        std::error_code e2;
        if (it->is_regular_file(e2)) { ++files; bytes += it->file_size(e2); }
    }
    m["files"] = files;
    m["bytes"] = bytes;
    std::error_code tec;
    auto t = fs::last_write_time(dir, tec);
    if (!tec) {
        auto sys = std::chrono::clock_cast<std::chrono::system_clock>(t);
        m["modified"] = std::format("{:%Y-%m-%dT%H:%M:%SZ}", std::chrono::floor<std::chrono::seconds>(sys));
    }
    if (auto build = read_json_file(dir / L"reskate-build.json")) m["build"] = *build;
    if (auto levels = read_json_file(dir / L"reskate-levels.json")) m["levels"] = levels->contains("levels") ? (*levels)["levels"] : *levels;
    return m;
}

}  // namespace

void register_mod_commands(Registry& r) {
    // ---- mod compile ------------------------------------------------------------------------------
    r.add({
        .group = "mod", .name = "compile",
        .summary = "Build .fbmod files into a staging folder ready to install",
        .description =
            "Compiles one or more binary .fbmod files against the installed game into a native Frostbite Patch "
            "staging folder (reskate_cli compile-mod). The fbmods are applied in the order given. Reads the game, "
            "writes only into the staging folder: .reskate-studio-staging, and Patch\\ with layout.toc, initfs_win32, "
            ".reskate-studio-patch (records the Skate.exe SHA-256), Win32\\...\\cas_01.cas archives and level TOCs "
            "(about 60 MB for a small animation mod). Install the result with 'mod deploy'.\n\n"
            "Gotchas: the staging folder must be outside the Skate folder, and if it exists it must be empty or a "
            "previous staging folder (which is wiped and rebuilt), and the fbmods must not sit inside it. A .fbproject is not accepted: export it as .fbmod "
            "in ReSkate Studio first. The build is tied to the exact Skate.exe; after a game update, compile again. "
            "Not every fbmod compiles (a skin-tone fbmod failed with 'Cosmetic shader parameter dependency is "
            "unavailable'); a failure after loading leaves a staging folder holding only the marker. Takes 3-4 s "
            "for a small mod (2.7 s is the game index load).",
        .params = {
            {"staging-dir", ParamType::Path, "Output staging folder, outside the Skate folder. Created if missing", true, true},
            {"fbmod", ParamType::List, "One or more binary .fbmod files, applied in order", true, true},
            game_root_param(),
        },
        .examples = {"mod compile C:\\mods\\stage C:\\mods\\anim.fbmod",
                     "mod compile C:\\mods\\stage base.fbmod tweaks.fbmod --json"},
        .long_running = true,
        .run = [](Context& c, const Json& a) -> Json {
            fs::path game = checked_game_root(c, a);
            fs::path staging = to_path(a, "staging-dir");
            std::string staging_s = path_utf8(staging);
            if (is_within(staging, game) || is_within(game, staging))
                throw Error("staging_inside_game", "The staging folder must be outside the Skate folder: " + staging_s,
                            {{"param", "staging-dir"}, {"path", staging_s}});
            if (path_exists(staging)) {
                if (!dir_exists(staging)) throw Error("not_a_folder", "Staging path is a file: " + staging_s, {{"param", "staging-dir"}});
                std::error_code ec;
                bool empty = fs::is_empty(staging, ec);
                if (!empty && !file_exists(staging / L".reskate-studio-staging"))
                    throw Error("staging_not_owned",
                                "Staging folder is not empty and is not a ReSkate Studio staging folder (it would be wiped): " + staging_s,
                                {{"param", "staging-dir"}, {"path", staging_s}});
            }
            std::vector<std::string> args = {"compile-mod", path_utf8(game), staging_s};
            Json inputs = Json::array();
            for (const auto& item : a["fbmod"]) {
                fs::path p = absolute_path(fs::path(utf8_to_wide(item.get<std::string>())));
                require_file(p, "fbmod");
                if (is_within(p, staging))
                    throw Error("fbmod_inside_staging",
                                path_utf8(p) + " is inside the staging folder, which is wiped before the build. Move the fbmod out first.",
                                {{"param", "fbmod"}, {"path", path_utf8(p)}});
                std::wstring ext = lower(p.extension().native());
                if (ext == L".fbproject")
                    throw Error("not_an_fbmod", path_utf8(p) + " is a project, not an fbmod. Export it as .fbmod in ReSkate Studio first.",
                                {{"param", "fbmod"}, {"path", path_utf8(p)}});
                args.push_back(path_utf8(p));
                inputs.push_back(path_utf8(p));
            }
            c.progress(-1, "Compiling " + std::to_string(inputs.size()) + " fbmod(s) against the game");
            EngineRun run = run_engine(c, args);
            try {
                require_ok(run, {{"file is not a binary FBMOD", "not_an_fbmod"},
                                 {"staging is not empty", "staging_not_owned"},
                                 {"dedicated folder outside the game", "staging_inside_game"},
                                 {"dependency is unavailable", "mod_not_compilable"}});
            } catch (Error& e) {
                // The engine does not say which input it means.
                if (e.code != "not_an_fbmod" && e.code != "file_unreadable") throw;
                std::string which;
                for (const auto& in : inputs) which += (which.empty() ? "" : ", ") + in.get<std::string>();
                if (inputs.size() > 1) which = "one of " + which;
                throw Error(e.code, std::string(e.what()) + ": " + which, {{"param", "fbmod"}, {"fbmods", inputs}});
            }
            Json counts = kv_object(run.out_lines);
            Json out = {{"staging_dir", staging_s}, {"patch_dir", path_utf8(staging / L"Patch")}, {"fbmods", inputs}};
            for (const char* k : {"resources", "bundles", "tocs"}) out[k] = counts.contains(k) ? counts[k] : Json(nullptr);
            out["skate_sha256"] = path_or_null(marker_sha(staging / L"Patch" / L".reskate-studio-patch"));
            out["seconds"] = run.seconds;
            return out;
        },
    });

    // ---- mod deploy ------------------------------------------------------------------------------
    r.add({
        .group = "mod", .name = "deploy",
        .summary = "Install a compiled staging folder into the game's Mods folder",
        .description =
            "WRITES TO THE GAME FOLDER. Installs a 'mod compile' staging build into <game>\\Mods\\<mod-folder> as a "
            "transaction (reskate_cli deploy-mod): copies the staging Patch folder, verifies the copy, moves any "
            "previous version to <game>\\.ReSkateStudio-Mod-backup\\<mod-folder>, and writes the receipt "
            "<game>\\.ReSkateStudio-last-deploy.db. It may retire an old-style root Patch folder marked "
            ".frosty-managed-skate-patch. Skate must be closed (checked before starting).\n\n"
            "Gotchas: the build must have been compiled against the current Skate.exe (same SHA-256), otherwise "
            "compile again. The mod folder is a name, not a path: 1-64 letters, digits, spaces, '_', '-' or '.', not "
            "starting with '.', not ending with a space or '.', and not a Windows device name such as CON or NUL. An existing Mods folder that ReSkate Studio did not create is refused. Use the same "
            "name again to update a mod. See installed mods with 'mod list'.",
        .params = {
            {"staging-dir", ParamType::Path, "Staging folder made by 'mod compile' (holds Patch\\layout.toc and Patch\\initfs_win32)", true, true},
            {"mod-folder", ParamType::String, "Folder name under <game>\\Mods, e.g. ScooterPush", true, true},
            game_root_param(),
        },
        .examples = {"mod deploy C:\\mods\\stage ScooterPush",
                     "mod deploy C:\\mods\\stage ScooterPush --game-root D:\\TestSkate --json"},
        .writes_game = true,
        .run = [](Context& c, const Json& a) -> Json {
            std::string name = a["mod-folder"];
            bool name_ok = !name.empty() && name.size() <= 64 && name[0] != '.';
            for (char ch : name)
                if (!(std::isalnum(static_cast<unsigned char>(ch)) || ch == ' ' || ch == '_' || ch == '-' || ch == '.')) name_ok = false;
            if (!name_ok)
                throw Error("invalid_mod_folder",
                            "Mod folder name \"" + name + "\" is not usable: 1-64 letters, digits, spaces, '_', '-' or '.', not starting with '.'",
                            {{"param", "mod-folder"}});
            {
                // Windows device names cannot be folder names, with or without an extension ("nul.txt").
                std::string base = lower_ascii(name.substr(0, name.find('.')));
                while (!base.empty() && base.back() == ' ') base.pop_back();
                bool device = base == "con" || base == "prn" || base == "aux" || base == "nul" ||
                              (base.size() == 4 && (base.rfind("com", 0) == 0 || base.rfind("lpt", 0) == 0) && base[3] >= '0' && base[3] <= '9');
                if (device)
                    throw Error("invalid_mod_folder", "Mod folder name \"" + name + "\" is a reserved Windows device name; pick another name",
                                {{"param", "mod-folder"}});
            }
            if (name.back() == ' ' || name.back() == '.')
                throw Error("invalid_mod_folder",
                            "Mod folder name \"" + name + "\" must not end with a space or '.' (Windows drops them from folder names)",
                            {{"param", "mod-folder"}});
            fs::path game = checked_game_root(c, a);
            fs::path staging = to_path(a, "staging-dir");
            std::string staging_s = path_utf8(staging);
            if (!dir_exists(staging)) throw Error("path_not_found", "Staging folder does not exist: " + staging_s, {{"param", "staging-dir"}});
            if (!file_exists(staging / L"Patch" / L"layout.toc") || !file_exists(staging / L"Patch" / L"initfs_win32"))
                throw Error("staging_incomplete", "Not a compiled staging folder (needs Patch\\layout.toc and Patch\\initfs_win32): " + staging_s,
                            {{"param", "staging-dir"}});
            if (is_within(staging, game) || is_within(game, staging))
                throw Error("staging_inside_game", "Staging and Skate folders must be separate folder trees", {{"param", "staging-dir"}});
            if (skate_running_from(game))
                throw Error("game_running", "Skate is running. Close it before installing a mod.");
            EngineRun run = run_engine(c, {"deploy-mod", path_utf8(game), staging_s, name});
            require_ok(run, {{"Skate.exe differs from this staged build", "game_version_mismatch"},
                             {"has no Skate.exe SHA-256", "game_version_mismatch"},
                             {"is not usable", "invalid_mod_folder"},
                             {"is not managed by ReSkate Studio", "mod_folder_not_managed"},
                             {"refusing to replace", "mod_folder_not_managed"},
                             {"already running", "deploy_busy"},
                             {"symbolic links or junctions", "links_not_allowed"},
                             {"does not contain the Skate Data", "not_a_game_root"}});
            Json out = {{"mod_folder", name}};
            Json notes = Json::array();
            for (const auto& line : run.out_lines) {
                std::string k, v;
                if (line.rfind("note:", 0) == 0) {
                    auto start = line.find_first_not_of(' ', 5);
                    notes.push_back(start == std::string::npos ? std::string() : line.substr(start));
                } else if (split_kv(line, k, v)) {
                    if (k == "mod") out["mod_path"] = v;
                    else if (k == "backup" || k == "retired-patch") out[snake(k)] = path_or_null(v);
                    else out[snake(k)] = typed_value(v);
                } else notes.push_back(line);
            }
            out["notes"] = notes;
            return out;
        },
    });

    // ---- mod list --------------------------------------------------------------------------------
    r.add({
        .group = "mod", .name = "list",
        .summary = "List the mods installed in the game's Mods folder",
        .description =
            "Read only. Lists every folder in <game>\\Mods with its size and file count, whether ReSkate Studio "
            "manages it (.reskate-studio-patch marker), the Skate.exe SHA-256 it was built for, and for maps the "
            "reskate-build.json and reskate-levels.json details. Also reads Mods\\mods.json when present (shown as "
            "found, and matched to folders by name), the backups in <game>\\.ReSkateStudio-Mod-backup, and the "
            "last deploy receipt <game>\\.ReSkateStudio-last-deploy.db.\n\n"
            "With --check-hash it also hashes Skate.exe (about 145 MB) and reports for each mod whether it was built "
            "for the installed game version; a mod built for another version needs to be compiled again.",
        .params = {
            {"check-hash", ParamType::Boolean, "Hash Skate.exe and report which mods match the installed game version", false, false, {}, Json(false)},
            game_root_param(),
        },
        .examples = {"mod list", "mod list --check-hash --json"},
        .run = [](Context& c, const Json& a) -> Json {
            fs::path game = checked_game_root(c, a, true, false);
            fs::path mods_dir = game / L"Mods";
            Json out = {{"game_root", path_utf8(game)}, {"mods_dir", path_utf8(mods_dir)}, {"exists", dir_exists(mods_dir)}};
            std::string game_sha;
            if (a["check-hash"].get<bool>()) {
                c.progress(-1, "Hashing Skate.exe");
                game_sha = sha256_file(game / L"Skate.exe");
                out["skate_sha256"] = game_sha;
            }

            Json mods_json = nullptr;
            std::vector<std::pair<std::string, Json>> entries;
            fs::path mods_json_path = mods_dir / L"mods.json";
            if (file_exists(mods_json_path)) {
                std::string err;
                auto parsed = read_json_file(mods_json_path, &err);
                mods_json = {{"path", path_utf8(mods_json_path)}, {"valid", parsed.has_value()}};
                if (parsed) { mods_json["content"] = *parsed; entries = mods_json_entries(*parsed); }
                else mods_json["error"] = err;
            }
            std::vector<bool> entry_matched(entries.size(), false);

            Json mods = Json::array();
            Json other_files = Json::array();
            if (dir_exists(mods_dir)) {
                std::vector<fs::path> dirs;
                std::error_code ec;
                for (fs::directory_iterator it(mods_dir, ec), end; !ec && it != end; it.increment(ec)) {
                    std::error_code e2;
                    if (it->is_directory(e2)) dirs.push_back(it->path());
                    else if (lower(it->path().filename().native()) != L"mods.json") other_files.push_back(path_utf8(it->path().filename()));
                }
                std::sort(dirs.begin(), dirs.end(), [](const fs::path& x, const fs::path& y) {
                    return lower(x.filename().native()) < lower(y.filename().native());
                });
                for (const auto& dir : dirs) {
                    if (c.cancelled()) throw Error("cancelled", "Cancelled");
                    Json m = describe_mod_folder(dir);
                    if (!game_sha.empty())
                        m["matches_game"] = m["skate_sha256"].is_string() ? Json(m["skate_sha256"].get<std::string>() == game_sha) : Json(nullptr);
                    if (!mods_json.is_null()) {
                        std::string folder_lower = lower_ascii(m["name"].get<std::string>());
                        Json entry = nullptr;
                        for (size_t i = 0; i < entries.size(); ++i)
                            if (entry_names_folder(entries[i].first, entries[i].second, folder_lower)) {
                                entry = entries[i].second;
                                entry_matched[i] = true;
                                break;
                            }
                        m["in_mods_json"] = !entry.is_null();
                        if (!entry.is_null()) m["mods_json_entry"] = entry;
                    }
                    mods.push_back(m);
                }
            }
            out["count"] = mods.size();
            out["mods"] = mods;
            if (!other_files.empty()) out["other_files"] = other_files;
            if (!mods_json.is_null()) {
                Json unmatched = Json::array();
                for (size_t i = 0; i < entries.size(); ++i)
                    if (!entry_matched[i]) {
                        if (entries[i].first.empty()) unmatched.push_back(entries[i].second);
                        else unmatched.push_back({{"key", entries[i].first}, {"entry", entries[i].second}});
                    }
                mods_json["entries_without_folder"] = unmatched;
            }
            out["mods_json"] = mods_json;

            Json backups = Json::array();
            fs::path backup_dir = game / L".ReSkateStudio-Mod-backup";
            if (dir_exists(backup_dir)) {
                std::error_code ec;
                for (fs::directory_iterator it(backup_dir, ec), end; !ec && it != end; it.increment(ec))
                    if (std::error_code e2; it->is_directory(e2)) backups.push_back({{"name", path_utf8(it->path().filename())}, {"path", path_utf8(it->path())}});
            }
            out["backups"] = backups;
            out["last_deploy"] = read_receipt(game / L".ReSkateStudio-last-deploy.db");
            return out;
        },
    });

    // ---- mod info --------------------------------------------------------------------------------
    r.add({
        .group = "mod", .name = "info",
        .summary = "Show the header of a binary .fbmod file",
        .description =
            "Read only. Prints the metadata of a binary .fbmod (reskate_cli fbmod-info): title, author, profile, "
            "head (the game data build it was made against), format version and resource count. Needs no game "
            "folder.\n\nGotchas: the resource count can be higher than the count the producing command reported "
            "(it seems to include metadata slots). A .fbproject is rejected; use 'project info' for those. "
            "--verbose is meant to add native-costume recipe detail; for animation and skin-tone mods it adds nothing.",
        .params = {
            {"file", ParamType::Path, "Binary .fbmod file", true, true},
            {"verbose", ParamType::Boolean, "Ask for extra detail (costume recipe and witness data for costume mods)", false, false, {}, Json(false)},
        },
        .examples = {"mod info C:\\mods\\anim.fbmod", "mod info C:\\mods\\costume.fbmod --verbose --json"},
        .run = [](Context& c, const Json& a) -> Json {
            fs::path file = to_path(a, "file");
            require_file(file, "file");
            if (lower(file.extension().native()) == L".fbproject")
                throw Error("not_an_fbmod", path_utf8(file) + " is a project file; use: studio-plus project info", {{"param", "file"}});
            std::vector<std::string> args = {"fbmod-info", path_utf8(file)};
            add_flag(args, a, "verbose", "--verbose");
            EngineRun run = run_engine(c, args);
            try {
                require_ok(run, {{"file is not a binary FBMOD", "not_an_fbmod"},
                                 {"FBMOD format version is unsupported", "fbmod_version_unsupported"},
                                 {"FBMOD payload exceeds bounds", "fbmod_corrupt"}});
            } catch (Error& e) {
                if (e.code == "not_an_fbmod" || e.code == "file_unreadable")
                    throw Error(e.code, std::string(e.what()) + ": " + path_utf8(file), {{"param", "file"}, {"path", path_utf8(file)}});
                throw;
            }
            // Header keys go to the top level. Anything else (the --verbose costume recipe lines such as
            // "costume=..." and " marker=...", which can repeat) is kept in order under "details".
            static const char* header[] = {"title", "author", "profile", "head", "version", "resources"};
            Json other = Json::array();
            Json out = {{"file", path_utf8(file)}};
            for (const auto& line : run.out_lines) {
                std::string k, v;
                bool is_header = split_kv(line, k, v) && !out.contains(k) &&
                                 std::any_of(std::begin(header), std::end(header), [&](const char* h) { return k == h; });
                if (!is_header) { other.push_back(line); continue; }
                if (k == "resources") out["resource_count"] = typed_value(v);
                else out[k] = k == "author" && v.empty() ? Json(nullptr) : typed_value(v);
            }
            if (!other.empty()) out["details"] = other;
            return out;
        },
    });

    // ---- project info ----------------------------------------------------------------------------
    r.add({
        .group = "project", .name = "info",
        .summary = "Show the metadata and resource list of a .fbproject",
        .description =
            "Read only. Reads a ReSkate Studio .fbproject (reskate_cli project-info) and returns its title, author, "
            "profile, head (game data build) and every resource it holds: kind (ebx asset, res resource or chunk "
            "GUID), name, size in bytes and whether it is a new asset ('added') rather than a replacement. Needs no "
            "game folder.\n\nGotchas: the ebx/res/chunk kind is read from the engine's numeric kind (1, 2, 3); the "
            "mapping is inferred from the names. There is no command that turns a .fbproject into an .fbmod; "
            "use 'Export .fbmod' in ReSkate Studio, then 'mod compile'.",
        .params = {
            {"file", ParamType::Path, ".fbproject file", true, true},
        },
        .examples = {"project info C:\\mods\\Untitled.fbproject", "project info scooter.fbproject --json"},
        .run = [](Context& c, const Json& a) -> Json {
            fs::path file = to_path(a, "file");
            require_file(file, "file");
            std::wstring ext = lower(file.extension().native());
            if (ext == L".fbmod")
                throw Error("not_a_project", path_utf8(file) + " is an fbmod; use: studio-plus mod info", {{"param", "file"}});
            EngineRun run = run_engine(c, {"project-info", path_utf8(file)});
            try {
                require_ok(run, {{"project format is unsupported", "not_a_project"}});
            } catch (Error& e) {
                if (e.code == "not_a_project" || e.code == "file_unreadable")
                    throw Error(e.code, std::string(e.what()) + " (expected a ReSkate Studio .fbproject): " + path_utf8(file),
                                {{"param", "file"}, {"path", path_utf8(file)}});
                throw;
            }
            Json out = {{"file", path_utf8(file)}};
            Json resources = Json::array();
            Json other = Json::array();
            for (const auto& line : run.out_lines) {
                std::string k, v;
                auto first_space = line.find(' ');
                auto bytes_at = line.rfind(" bytes=");
                if (!line.empty() && std::isdigit(static_cast<unsigned char>(line[0])) && first_space != std::string::npos &&
                    bytes_at != std::string::npos && bytes_at > first_space) {
                    // "<kind> <name-or-guid> bytes=<n>[ added]" (the name is everything between kind and bytes=)
                    int kind = std::atoi(line.substr(0, first_space).c_str());
                    std::string name = line.substr(first_space + 1, bytes_at - first_space - 1);
                    std::istringstream in(line.substr(bytes_at + 1));
                    std::string size_tok, flag;
                    in >> size_tok >> flag;
                    if (!name.empty()) {
                        Json res = {{"kind", kind == 1 ? "ebx" : kind == 2 ? "res" : kind == 3 ? "chunk" : "unknown"},
                                    {"kind_code", kind}, {"name", name}, {"bytes", typed_value(size_tok.substr(6))},
                                    {"added", flag == "added"}};
                        resources.push_back(res);
                        continue;
                    }
                }
                if (split_kv(line, k, v)) {
                    if (k == "resources") out["resource_count"] = typed_value(v);
                    else out[snake(k)] = k == "author" && v.empty() ? Json(nullptr) : typed_value(v);
                } else other.push_back(line);
            }
            out["resources"] = resources;
            if (!other.empty()) out["lines"] = other;
            return out;
        },
    });

    // ---- game validate ---------------------------------------------------------------------------
    r.add({
        .group = "game", .name = "validate",
        .summary = "Check that the game install can be read and decoded",
        .description =
            "Read only. Locates and decodes the first <count> assets of the game through the CAS and Oodle pipeline "
            "(reskate_cli validate) and reports how many decoded and how many bytes. A quick 'is this Skate folder "
            "usable' check. Needs Skate.exe, Data and oo2core_9_win64.dll in the folder.\n\nGotchas: about 2.7 s at "
            "the default 100 or at 500. A count above the number of assets fails with count_too_large, but only "
            "after decoding everything (about 54 s).",
        .params = {
            {"count", ParamType::Integer, "Number of assets to decode, at least 1", false, true, {}, Json(100)},
            game_root_param(),
        },
        .examples = {"game validate", "game validate 500 --json"},
        .long_running = true,
        .run = [](Context& c, const Json& a) -> Json {
            long long count = a["count"];
            if (count < 1) throw Error("invalid_arguments", "--count must be at least 1", {{"param", "count"}});
            fs::path game = checked_game_root(c, a);
            if (!file_exists(game / L"oo2core_9_win64.dll"))
                throw Error("oodle_missing", "No oo2core_9_win64.dll in " + path_utf8(game), {{"game_root", path_utf8(game)}});
            c.progress(-1, "Decoding " + std::to_string(count) + " assets");
            EngineRun run = run_engine(c, {"validate", path_utf8(game), std::to_string(count)});
            require_ok(run, {{"not enough located assets", "count_too_large"},
                             {"not enough EBX assets", "count_too_large"}});
            Json other = Json::array();
            Json out = kv_object(run.out_lines, &other);
            out["requested"] = count;
            out["ok"] = out.contains("decoded_assets") && out["decoded_assets"] == count;
            out["game_root"] = path_utf8(game);
            out["seconds"] = run.seconds;
            if (!other.empty()) out["lines"] = other;
            return out;
        },
    });

    // ---- game patch-audit ------------------------------------------------------------------------
    r.add({
        .group = "game", .name = "patch-audit",
        .summary = "Audit the native Patch folder in the game root",
        .description =
            "Read only. Audits <game>\\Patch (reskate_cli patch-audit): counts patched assets, EBX, resources and "
            "chunks, mesh/texture/physics resources, ownership headers, added assets and stock replacements, checks "
            "ownership headers and encoded SHA-1 against the bundle manifests, and lists each replaced stock asset. "
            "Without a Patch folder every count is 0.\n\nGotchas: it does not look at Mods\\<name> installs or "
            "staging folders, only <game>\\Patch (use 'mod list' for Mods). Integrity problems fail the command "
            "(patch_integrity). Needs oo2core_9_win64.dll. Takes 5-6 s.",
        .params = {game_root_param()},
        .examples = {"game patch-audit", "game patch-audit --json"},
        .long_running = true,
        .run = [](Context& c, const Json& a) -> Json {
            fs::path game = checked_game_root(c, a);
            if (!file_exists(game / L"oo2core_9_win64.dll"))
                throw Error("oodle_missing", "No oo2core_9_win64.dll in " + path_utf8(game), {{"game_root", path_utf8(game)}});
            c.progress(-1, "Auditing " + path_utf8(game / L"Patch"));
            EngineRun run = run_engine(c, {"patch-audit", path_utf8(game)});
            require_ok(run, {{"does not match the bundle manifest", "patch_integrity"},
                             {"non-stock ownership header", "patch_integrity"}});
            Json counts = Json::object();
            Json stock = Json::array();
            Json other = Json::array();
            for (const auto& line : run.out_lines) {
                std::string k, v;
                if (!split_kv(line, k, v)) { other.push_back(line); continue; }
                if (k == "stock-replacement") stock.push_back(v);
                else counts[snake(k)] = typed_value(v);
            }
            Json out = {{"game_root", path_utf8(game)}, {"patch_dir", path_utf8(game / L"Patch")},
                        {"patch_present", dir_exists(game / L"Patch")}, {"counts", counts}, {"stock_replacements", stock}};
            if (!other.empty()) out["lines"] = other;
            return out;
        },
    });

    // ---- game initfs -----------------------------------------------------------------------------
    r.add({
        .group = "game", .name = "initfs",
        .summary = "Summarise the game's initfs_win32 archive",
        .description =
            "Read only. Reports the file count and total bytes of the game's initfs_win32, the initial file system "
            "archive (reskate_cli initfs-info). Unlike the other game commands it needs only the Data folder, not "
            "Skate.exe. Pass the Skate folder, not the initfs file or a staging folder.",
        .params = {game_root_param()},
        .examples = {"game initfs", "game initfs --json"},
        .run = [](Context& c, const Json& a) -> Json {
            fs::path game = checked_game_root(c, a, false, true);
            EngineRun run = run_engine(c, {"initfs-info", path_utf8(game)});
            require_ok(run, {});
            Json other = Json::array();
            Json out = kv_object(run.out_lines, &other);
            out["game_root"] = path_utf8(game);
            if (!other.empty()) out["lines"] = other;
            return out;
        },
    });

    // ---- asset lua -------------------------------------------------------------------------------
    r.add({
        .group = "asset", .name = "lua",
        .summary = "Show the source file, size and line count of a Lua asset",
        .description =
            "Read only. Finds a LuaAsset by its exact EBX name (reskate_cli lua) and reports the embedded source "
            "file name, its size in bytes and its line count. The Lua source text itself is not printed.\n\n"
            "Gotchas: the name is an exact match (lowercased for you), not a search; find names with "
            "'asset find characters/lua/'. A non-Lua asset gives not_a_lua_asset. Takes about 2.7 s (game index load).",
        .params = {
            {"name", ParamType::String, "Full EBX asset name, e.g. characters/lua/skaterlualoaderasset", true, true},
            game_root_param(),
        },
        .examples = {"asset lua characters/lua/skaterlualoaderasset", "asset lua characters/lua/luatattoocompositor --json"},
        .run = [](Context& c, const Json& a) -> Json {
            std::string name = lower_ascii(a["name"].get<std::string>());
            for (auto& ch : name) if (ch == '\\') ch = '/';
            fs::path game = checked_game_root(c, a);
            EngineRun run = run_engine(c, {"lua", path_utf8(game), name});
            try {
                require_ok(run, {{"Lua asset was not found", "asset_not_found"},
                                 {"is not a LuaAsset", "not_a_lua_asset"},
                                 {"source text is unavailable", "lua_compiled"}});
            } catch (const Error& e) {
                if (e.code != "asset_not_found") throw;
                throw Error(e.code, "No Lua asset named " + name + " (exact name needed; search with: studio-plus asset find characters/lua/)",
                            e.details);
            }
            Json other = Json::array();
            Json kv = kv_object(run.out_lines, &other);
            Json out = Json::object();
            for (auto it = kv.begin(); it != kv.end(); ++it) {
                if (it.key() == "resource") out["name"] = it.value();
                else if (it.key() == "lines") out["line_count"] = it.value();
                else out[it.key()] = it.value();
            }
            if (!other.empty()) out["other_lines"] = other;
            return out;
        },
    });
}
}
