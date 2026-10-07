#include "core/settings.h"
#include <windows.h>
#include <shlobj.h>
#include <algorithm>
#include <fstream>
#include <regex>
#include <sstream>

namespace fs = std::filesystem;

namespace studio {

std::wstring utf8_to_wide(std::string_view text) {
    if (text.empty()) return {};
    int n = MultiByteToWideChar(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), nullptr, 0);
    std::wstring out(static_cast<size_t>(n), L'\0');
    MultiByteToWideChar(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), out.data(), n);
    return out;
}

std::string wide_to_utf8(std::wstring_view text) {
    if (text.empty()) return {};
    int n = WideCharToMultiByte(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), nullptr, 0, nullptr, nullptr);
    std::string out(static_cast<size_t>(n), '\0');
    WideCharToMultiByte(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), out.data(), n, nullptr, nullptr);
    return out;
}

std::string path_utf8(const fs::path& path) { return wide_to_utf8(path.native()); }

fs::path executable_dir() {
    std::wstring buffer(MAX_PATH, L'\0');
    for (;;) {
        DWORD n = GetModuleFileNameW(nullptr, buffer.data(), static_cast<DWORD>(buffer.size()));
        if (n < buffer.size()) { buffer.resize(n); break; }
        buffer.resize(buffer.size() * 2);
    }
    return fs::path(buffer).parent_path();
}

fs::path Settings::data_dir() {
    PWSTR local = nullptr;
    fs::path base;
    if (SUCCEEDED(SHGetKnownFolderPath(FOLDERID_LocalAppData, 0, nullptr, &local))) base = local;
    CoTaskMemFree(local);
    return base / L"ReSkateStudioPlus";
}

fs::path Settings::file() { return data_dir() / L"settings.json"; }

namespace {
std::wstring registry_string(HKEY root, const wchar_t* key, const wchar_t* value) {
    wchar_t buffer[1024];
    DWORD size = sizeof(buffer);
    if (RegGetValueW(root, key, value, RRF_RT_REG_SZ, nullptr, buffer, &size) != ERROR_SUCCESS) return {};
    return buffer;
}

std::vector<fs::path> steam_libraries() {
    std::vector<fs::path> out;
    fs::path steam = registry_string(HKEY_CURRENT_USER, L"Software\\Valve\\Steam", L"SteamPath");
    if (steam.empty()) steam = L"C:\\Program Files (x86)\\Steam";
    steam = steam.lexically_normal().make_preferred();
    out.push_back(steam);
    std::ifstream vdf(steam / L"steamapps" / L"libraryfolders.vdf");
    std::string line;
    static const std::regex path_line("\"path\"\\s+\"([^\"]+)\"");
    while (std::getline(vdf, line)) {
        std::smatch m;
        if (std::regex_search(line, m, path_line)) {
            std::string p = m[1];
            std::string unescaped;
            for (size_t i = 0; i < p.size(); ++i) {
                if (p[i] == '\\' && i + 1 < p.size() && p[i + 1] == '\\') ++i;
                unescaped += p[i];
            }
            out.emplace_back(utf8_to_wide(unescaped));
        }
    }
    return out;
}
}

fs::path detect_game_root() {
    std::error_code ec;
    for (const auto& library : steam_libraries()) {
        fs::path candidate = library / L"steamapps" / L"common" / L"Skate";
        if (fs::exists(candidate / L"Skate.exe", ec)) return candidate.lexically_normal().make_preferred();
    }
    return {};
}

fs::path detect_blender() {
    std::error_code ec;
    if (wchar_t env[MAX_PATH]; GetEnvironmentVariableW(L"RESKATE_BLENDER", env, MAX_PATH) > 0 && fs::exists(env, ec))
        return env;
    std::vector<fs::path> found;
    for (const wchar_t* base : {L"C:\\Program Files\\Blender Foundation", L"C:\\Program Files (x86)\\Blender Foundation"}) {
        if (!fs::exists(base, ec)) continue;
        for (const auto& entry : fs::directory_iterator(base, ec))
            if (fs::exists(entry.path() / L"blender.exe", ec)) found.push_back(entry.path() / L"blender.exe");
    }
    for (const auto& library : steam_libraries()) {
        fs::path steam_blender = library / L"steamapps" / L"common" / L"Blender" / L"blender.exe";
        if (fs::exists(steam_blender, ec)) found.push_back(steam_blender);
    }
    if (found.empty()) return {};
    // Folder names are "Blender 4.2", "Blender 5.2": the highest sorts last.
    std::sort(found.begin(), found.end());
    return found.back();
}

Settings Settings::load() {
    Settings s;
    std::ifstream in(file());
    if (in) {
        try {
            Json j = Json::parse(in);
            if (j.contains("game-root")) s.game_root = utf8_to_wide(j["game-root"].get<std::string>());
            if (j.contains("blender")) s.blender = utf8_to_wide(j["blender"].get<std::string>());
            if (j.contains("engine-dir")) s.engine_dir = utf8_to_wide(j["engine-dir"].get<std::string>());
        } catch (...) {
            // A damaged settings file falls back to detection; it is rewritten on the next save.
        }
    }
    if (s.game_root.empty()) s.game_root = detect_game_root();
    if (s.blender.empty()) s.blender = detect_blender();
    return s;
}

Json Settings::to_json() const {
    return {{"game-root", path_utf8(game_root)}, {"blender", path_utf8(blender)},
            {"engine-dir", path_utf8(engine_dir)}};
}

void Settings::save() const {
    std::error_code ec;
    fs::create_directories(data_dir(), ec);
    fs::path tmp = file();
    tmp += L".tmp";
    {
        std::ofstream out(tmp, std::ios::binary | std::ios::trunc);
        out << to_json().dump(2) << "\n";
        if (!out) throw Error("settings_write_failed", "Could not write " + path_utf8(tmp));
    }
    fs::rename(tmp, file(), ec);
    if (ec) throw Error("settings_write_failed", "Could not save " + path_utf8(file()) + ": " + ec.message());
}

std::vector<std::string> Settings::keys() { return {"game-root", "blender", "engine-dir"}; }

void Settings::set(std::string_view key, const fs::path& value) {
    if (key == "game-root") game_root = value;
    else if (key == "blender") blender = value;
    else if (key == "engine-dir") engine_dir = value;
    else throw Error("unknown_setting", "Unknown setting '" + std::string(key) + "'. Settings: game-root, blender, engine-dir");
}

fs::path Settings::resolved_engine_dir() const {
    if (!engine_dir.empty()) return engine_dir;
    if (wchar_t env[MAX_PATH]; GetEnvironmentVariableW(L"RSSP_ENGINE", env, MAX_PATH) > 0) return env;
    return executable_dir() / L"engine";
}

fs::path Settings::reskate_cli() const {
    fs::path cli = resolved_engine_dir() / L"reskate_cli.exe";
    std::error_code ec;
    if (!fs::exists(cli, ec))
        throw Error("engine_missing",
            "reskate_cli.exe was not found at " + path_utf8(cli) +
            ". Put the ReSkate Studio files in an 'engine' folder beside Studio+, or run: studio-plus studio set engine-dir <folder>",
            {{"expected", path_utf8(cli)}});
    return cli;
}
}
