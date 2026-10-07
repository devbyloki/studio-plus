// Studio+ itself: settings, health check, version and a raw pass-through to reskate_cli.
#include "commands/commands.h"
#include "core/engine.h"
#include "core/settings.h"
#include "version.h"
#include <filesystem>

namespace fs = std::filesystem;

namespace studio {

void register_studio_commands(Registry& r) {
    r.add({
        .group = "studio", .name = "version",
        .summary = "Show the Studio+ version",
        .description = "Prints the Studio+ version and where its files and settings live.",
        .examples = {"studio version"},
        .run = [](Context& c, const Json&) -> Json {
            return {{"version", STUDIO_PLUS_VERSION}, {"executable_dir", path_utf8(executable_dir())},
                    {"settings_file", path_utf8(Settings::file())},
                    {"engine_dir", path_utf8(c.settings.resolved_engine_dir())}};
        },
    });

    r.add({
        .group = "studio", .name = "settings",
        .summary = "Show the saved settings",
        .description = "Shows the Skate folder, Blender and engine folder Studio+ will use. Empty values are "
                       "auto-detected on every start (Steam libraries, Program Files, RESKATE_BLENDER).",
        .examples = {"studio settings", "studio settings --json"},
        .run = [](Context& c, const Json&) -> Json {
            Json j = c.settings.to_json();
            j["engine-dir (resolved)"] = path_utf8(c.settings.resolved_engine_dir());
            j["file"] = path_utf8(Settings::file());
            return j;
        },
    });

    r.add({
        .group = "studio", .name = "set",
        .summary = "Change a setting",
        .description = "Saves one setting. Keys: game-root (Skate install folder), blender (blender.exe), "
                       "engine-dir (folder with reskate_cli.exe). An empty value clears it, so it is auto-detected again.",
        .params = {
            {"key", ParamType::Enum, "Which setting", true, true, {"game-root", "blender", "engine-dir"}},
            {"value", ParamType::Path, "New value; empty clears it", false, true},
        },
        .examples = {"studio set game-root \"D:\\SteamLibrary\\steamapps\\common\\Skate\"",
                     "studio set blender \"C:\\Program Files\\Blender Foundation\\Blender 5.2\\blender.exe\""},
        .run = [](Context& c, const Json& a) -> Json {
            std::string key = a["key"];
            std::string value = arg_string(a, "value");
            fs::path path = utf8_to_wide(value);
            std::error_code ec;
            if (!value.empty() && !fs::exists(path, ec))
                throw Error("path_not_found", "Path does not exist: " + value, {{"path", value}});
            if (key == "game-root" && !value.empty() && !fs::exists(path / L"Skate.exe", ec))
                throw Error("not_a_game_root", "No Skate.exe in " + value, {{"path", value}});
            c.settings.set(key, path);
            c.settings.save();
            return c.settings.to_json();
        },
    });

    r.add({
        .group = "studio", .name = "doctor",
        .summary = "Check that everything Studio+ needs is in place",
        .description = "Checks the Skate folder, Blender, and the engine (reskate_cli.exe and its Native folder), "
                       "and says what to fix for anything missing.",
        .examples = {"studio doctor", "studio doctor --json"},
        .run = [](Context& c, const Json&) -> Json {
            std::error_code ec;
            Json checks = Json::array();
            auto check = [&](std::string name, bool ok, std::string detail, std::string fix) {
                Json j = {{"check", name}, {"ok", ok}, {"detail", detail}};
                if (!ok) j["fix"] = fix;
                checks.push_back(j);
                return ok;
            };
            fs::path game = c.settings.game_root;
            check("game-root", !game.empty() && fs::exists(game / L"Skate.exe", ec),
                  game.empty() ? "not found" : path_utf8(game),
                  "studio-plus studio set game-root <Skate folder>");
            fs::path engine = c.settings.resolved_engine_dir();
            bool cli = fs::exists(engine / L"reskate_cli.exe", ec);
            check("engine", cli, path_utf8(engine / L"reskate_cli.exe"),
                  "studio-plus studio set engine-dir <folder with reskate_cli.exe>");
            check("engine-native", fs::exists(engine / L"Native" / L"Blender" / L"studio_map_import.py", ec),
                  path_utf8(engine / L"Native"), "The engine folder needs the Native folder from the ReSkate Studio zip");
            fs::path blender = c.settings.blender;
            check("blender", !blender.empty() && fs::exists(blender, ec),
                  blender.empty() ? "not found (only needed for .blend and .fbx maps)" : path_utf8(blender),
                  "studio-plus studio set blender <path to blender.exe>");
            bool all_ok = true;
            for (const auto& j : checks) all_ok = all_ok && j["ok"].get<bool>();
            return {{"ok", all_ok}, {"checks", checks}};
        },
    });

    r.add({
        .group = "studio", .name = "raw",
        .summary = "Run reskate_cli directly with your own arguments",
        .description = "Escape hatch: passes the arguments straight to reskate_cli.exe and returns its output. "
                       "Prefer the named commands, which check arguments and return structured results.",
        .params = {{"args", ParamType::List, "Arguments for reskate_cli, in order", true, true}},
        .examples = {"studio raw find-name \"C:\\...\\Skate\" baker_popsicle 5"},
        .long_running = true,
        .run = [](Context& c, const Json& a) -> Json {
            std::vector<std::string> args;
            for (const auto& v : a["args"]) args.push_back(v.get<std::string>());
            EngineRun run = run_engine(c, args);
            return {{"exit_code", run.exit_code}, {"stdout", run.out_lines}, {"stderr", run.err_lines},
                    {"seconds", run.seconds}};
        },
    });
}
}
