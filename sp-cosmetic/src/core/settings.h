#pragma once
#include "core/registry.h"
#include <filesystem>
#include <string>
#include <string_view>

namespace studio {
std::wstring utf8_to_wide(std::string_view text);
std::string wide_to_utf8(std::wstring_view text);
std::string path_utf8(const std::filesystem::path& path);

// Saved in %LOCALAPPDATA%\ReSkateStudioPlus\settings.json.
struct Settings {
    std::filesystem::path game_root;   // Skate install folder
    std::filesystem::path blender;     // blender.exe
    std::filesystem::path engine_dir;  // folder holding reskate_cli.exe and Native\ (from the ReSkate Studio zip)

    static std::filesystem::path data_dir();
    static std::filesystem::path file();
    static Settings load();  // also fills empty fields by auto-detection
    void save() const;
    Json to_json() const;
    static std::vector<std::string> keys();  // game-root, blender, engine-dir
    void set(std::string_view key, const std::filesystem::path& value);  // throws Error("unknown_setting")

    // Where reskate_cli.exe lives: engine_dir, else RSSP_ENGINE, else <exe>\engine.
    std::filesystem::path resolved_engine_dir() const;
    std::filesystem::path reskate_cli() const;  // throws Error("engine_missing")
};

std::filesystem::path executable_dir();
std::filesystem::path detect_game_root();  // empty if not found
std::filesystem::path detect_blender();    // empty if not found
}
