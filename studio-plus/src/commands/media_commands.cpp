// Audio, video and animation (reskate_cli: audio, webm, video, animation-info, animation-export, animation-import, fbx-animation-info).
#include "commands/commands.h"
#include "core/engine.h"
#include "core/settings.h"
#include <algorithm>
#include <cctype>
#include <cwctype>
#include <filesystem>
#include <utility>

namespace studio {

namespace {
namespace fs = std::filesystem;

// EBX names in the game index are lowercase with forward slashes.
std::string asset_name(const Json& a, const std::string& param) {
    std::string name = a[param];
    for (auto& ch : name) {
        if (ch == '\\') ch = '/';
        else ch = static_cast<char>(std::tolower(static_cast<unsigned char>(ch)));
    }
    while (!name.empty() && name.front() == '/') name.erase(name.begin());
    if (name.empty()) throw Error("invalid_arguments", "--" + param + " must not be empty", {{"param", param}});
    return name;
}

// Paths from MCP arrive as given; make them absolute so the engine (whose working directory is
// the engine folder) and the result both see the full path.
fs::path path_arg(const Json& a, const std::string& param) {
    fs::path p(utf8_to_wide(a[param].get<std::string>()));
    std::error_code ec;
    fs::path abs = fs::absolute(p, ec);
    return ec ? p : abs.lexically_normal();
}

std::string lower_ext(const fs::path& p) {
    std::string ext = path_utf8(p.extension());
    for (auto& ch : ext) ch = static_cast<char>(std::tolower(static_cast<unsigned char>(ch)));
    return ext;
}

void require_file(const fs::path& p, const std::string& param) {
    std::error_code ec;
    if (!fs::is_regular_file(p, ec))
        throw Error("file_not_found", "--" + param + ": file not found: " + path_utf8(p), {{"param", param}, {"path", path_utf8(p)}});
}

Json file_size_or_null(const fs::path& p) {
    std::error_code ec;
    auto size = fs::file_size(p, ec);
    return ec ? Json(nullptr) : Json(static_cast<long long>(size));
}

// An engine error line we recognise: the substring to look for, our error code, and a hint
// appended to the engine's message so a user or an AI knows what to do next.
struct ErrorMap {
    std::string needle, code, hint;
};

// require_success, but turns the engine's known error lines into specific error codes.
void require_success_mapped(const EngineRun& run, const std::vector<ErrorMap>& codes) {
    try {
        require_success(run);
    } catch (Error& e) {
        std::string message = e.what();
        for (const auto& m : codes)
            if (message.find(m.needle) != std::string::npos) {
                Json details = e.details;
                details["engine_message"] = message;
                throw Error(m.code, m.hint.empty() ? message : message + ". " + m.hint, details);
            }
        throw;
    }
}

// Errors shared by the commands that read a clip from the game (anim info/export/import).
std::vector<ErrorMap> clip_errors(const std::string& clip) {
    return {
        {"Animation clip asset was not found", "clip_not_found",
         "No EBX named '" + clip + "'. Find clip names with 'asset find <part of the name>', e.g. 'asset find push_regular'"},
        {"Select a ClipControllerAsset", "not_a_clip",
         "'" + clip + "' is not a standalone ClipControllerAsset (blend and subt_ controllers, skeletons and rigs are refused)"},
        {"project format is unsupported", "project_invalid",
         "--project must be a Studio project (.fbproject); .fbmod files and other formats are not accepted"},
        {"Could not write FBX file", "output_not_writable", "Check that the output folder exists and the file is not open elsewhere"},
        {"Animation is too long", "clip_too_long", ""},
        {"FBX export failed", "fbx_export_failed", ""},
        {"Select an FBX or GLB mesh", "unsupported_file_type", "The file must be an .fbx or .glb"},
        {"Could not open mesh file", "fbx_unreadable", "The file could not be read as FBX/GLB"},
        {"invalid quaternion", "fbx_invalid", ""},
        {"Animation node is missing or ambiguous", "fbx_rig_mismatch",
         "Bone names in the FBX must match the game rig; start from an FBX made by 'anim export'"},
        {"Vertex and morph animation", "fbx_rig_mismatch", "Only skeletal (bone) animation can be imported"},
        {"Animation output must use", "invalid_arguments", ""},
    };
}

// The project file: must exist and be a .fbproject (the engine refuses .fbmod and anything else).
fs::path project_arg(const Json& a) {
    fs::path project = path_arg(a, "project");
    require_file(project, "project");
    if (lower_ext(project) != ".fbproject")
        throw Error("invalid_arguments", "--project must be a Studio project file ending in .fbproject: " + path_utf8(project),
                    {{"param", "project"}});
    return project;
}

// FBX/GLB inputs: the engine only opens these two (any letter case).
void require_mesh_file(const fs::path& p, const std::string& param) {
    require_file(p, param);
    std::string ext = lower_ext(p);
    if (ext != ".fbx" && ext != ".glb")
        throw Error("invalid_arguments", "--" + param + " must be an .fbx or .glb file: " + path_utf8(p), {{"param", param}});
}

bool path_inside(const fs::path& child, const fs::path& parent) {
    if (parent.empty()) return false;
    std::error_code ec;
    fs::path c = fs::weakly_canonical(child, ec);
    if (ec) c = child.lexically_normal();
    fs::path p = fs::weakly_canonical(parent, ec);
    if (ec) p = parent.lexically_normal();
    auto lower = [](const fs::path& x) {
        std::wstring w = x.native();
        for (auto& ch : w) ch = static_cast<wchar_t>(std::towlower(ch));
        while (w.size() > 3 && (w.back() == L'\\' || w.back() == L'/')) w.pop_back();
        return w;
    };
    std::wstring cw = lower(c), pw = lower(p);
    if (pw.empty() || cw.size() <= pw.size() || cw.compare(0, pw.size(), pw) != 0) return false;
    bool parent_ends_in_sep = pw.back() == L'\\' || pw.back() == L'/';
    return parent_ends_in_sep || cw[pw.size()] == L'\\' || cw[pw.size()] == L'/';
}

// These commands only write their own output file. Refuse an output inside the ReSkate folder (the one
// used for this run and the saved one) so they never change the game install.
void refuse_game_folder(const Context& c, const fs::path& game_root, const fs::path& output, const std::string& param) {
    for (const auto& root : {game_root, c.settings.game_root})
        if (path_inside(output, root))
            throw Error("output_in_game_folder",
                        "--" + param + " is inside the ReSkate folder (" + path_utf8(root) +
                            "). Write it somewhere else; this command never changes the game install",
                        {{"param", param}, {"path", path_utf8(output)}});
}

Json num(const Json& v) { return v.is_null() ? Json(nullptr) : v; }

Json field(const Json& kv, const char* key) {
    auto it = kv.find(key);
    return it == kv.end() ? Json(nullptr) : num(*it);
}

// "take=0 name=a/b duration=2.5 fps=60 tracks=75": key=value tokens, values may hold spaces
// (a take name), so a value runs until the next " key=".
Json parse_tokens(const std::string& line, const std::vector<std::string>& keys) {
    Json out = Json::object();
    std::vector<std::pair<size_t, std::string>> starts;
    for (const auto& k : keys) {
        std::string needle = k + "=";
        size_t pos = line.rfind(needle, 0) == 0 ? 0 : line.find(" " + needle);
        if (pos == std::string::npos) continue;
        starts.push_back({pos == 0 ? 0 : pos + 1, k});
    }
    std::sort(starts.begin(), starts.end());
    for (size_t i = 0; i < starts.size(); ++i) {
        size_t from = starts[i].first + starts[i].second.size() + 1;
        size_t to = i + 1 < starts.size() ? starts[i + 1].first - 1 : line.size();
        out[starts[i].second] = typed_value(std::string_view(line).substr(from, to > from ? to - from : 0));
    }
    return out;
}

// These engine commands print nothing on stderr except their final error line, which
// require_success turns into the command error. Forwarding it as progress too would show it twice.
void quiet_stderr(Context& c, std::string_view line) {
    if (line.size() >= 7 && (line.substr(0, 7) == "warning" || line.substr(0, 7) == "Warning")) c.log("warning", line);
}

Param project_param(const std::string& help) {
    return {"project", ParamType::Path, help, false, false, {}, std::nullopt};
}
}

void register_media_commands(Registry& r) {
    // ---------------------------------------------------------------- audio
    r.add({
        .group = "audio", .name = "info",
        .summary = "Decode a game sound and report its format",
        .description =
            "Looks up a NewWaveAsset or LocalizedWaveAsset in the game, decodes it with the bundled "
            "Native\\Audio\\vgmstream-cli.exe and reports type, codec, channels, sample rate, duration, "
            "segment count and the size of the decoded PCM16 WAV. This is a probe only: no WAV file is kept "
            "(the engine uses and cleans %TEMP%\\ReSkateStudio\\CliAudio) and nothing in the game is changed. "
            "Find sound names with 'asset find' or by type NewWaveAsset / LocalizedWaveAsset. Movie assets "
            "are rejected (use 'video info'). Decoding is cut off after two minutes.",
        .params = {
            {"asset", ParamType::String,
             "EBX path of a NewWaveAsset or LocalizedWaveAsset, e.g. audio/physicsprops/assets/dgo_wb_obj_metal_rail_impact_low",
             true, true},
            game_root_param(),
        },
        .examples = {"audio info audio/physicsprops/assets/dgo_wb_obj_metal_rail_impact_low",
                     "audio info audio/vo/text-to-speech_shippable/id_vo_statement_hi --json"},
        .run = [](Context& c, const Json& a) -> Json {
            std::string asset = asset_name(a, "asset");
            EngineRun run = run_engine(c, {"audio", path_utf8(c.game_root(a)), asset}, quiet_stderr);
            require_success_mapped(run, {
                {"audio asset was not found", "asset_not_found",
                 "No sound named '" + asset + "'. Find names with 'asset find' (NewWaveAsset / LocalizedWaveAsset)"},
                {"not a NewWaveAsset or LocalizedWaveAsset", "not_an_audio_asset",
                 "'" + asset + "' is not a sound; for videos use 'video info'"},
                {"bundled audio tool is missing", "audio_tool_missing",
                 "Native\\Audio\\vgmstream-cli.exe is missing from the engine folder; re-extract the ReSkate Studio zip"},
                {"exceeded two minutes", "decode_timeout", ""},
            });
            Json kv = parse_key_values(run.out_lines, false);
            Json out = {{"asset", asset}, {"type", field(kv, "type")}, {"codec", field(kv, "codec")},
                        {"channels", field(kv, "channels")}, {"sample_rate", field(kv, "sample-rate")},
                        {"duration_seconds", field(kv, "duration")}, {"segments", field(kv, "segments")},
                        {"wav_bytes", field(kv, "wave-bytes")}};
            if (auto ct = field(kv, "catalog-type"); ct.is_string() && !ct.get<std::string>().empty()) out["catalog_type"] = ct;
            return out;
        },
    });

    // ---------------------------------------------------------------- video
    r.add({
        .group = "video", .name = "info",
        .summary = "Check a game video's embedded WebM and report its size, fps and length",
        .description =
            "Locates a MovieTexture2Asset (in-game video), validates the embedded WebM chunk (EBML header "
            "declares WebM, segment present, chunk size matches the EBX metadata) and reports its byte size, "
            "resolution, frame rate and duration. Read only: it does not extract the .webm and writes nothing. "
            "Only 9 MovieTexture2Asset videos exist in the game; find them with 'asset find ui/videos'. "
            "To check the video really decodes, use 'video decode-test'.",
        .params = {
            {"asset", ParamType::String,
             "EBX path of a MovieTexture2Asset, e.g. ui/videos/splash/vid_skate_logo_lockup_withaudio_v2_1920x1080",
             true, true},
            game_root_param(),
        },
        .examples = {"video info ui/videos/splash/vid_skate_logo_lockup_withaudio_v2_1920x1080",
                     "video info ui/videos/s01_intro/dgo_video_season01_video_01 --json"},
        .run = [](Context& c, const Json& a) -> Json {
            std::string asset = asset_name(a, "asset");
            EngineRun run = run_engine(c, {"webm", path_utf8(c.game_root(a)), asset}, quiet_stderr);
            require_success_mapped(run, {
                {"movie asset was not found", "asset_not_found",
                 "No video named '" + asset + "'. The game's videos are under ui/videos/ ('asset find ui/videos')"},
                {"not a MovieTexture2Asset", "not_a_video_asset", "'" + asset + "' is not a video (MovieTexture2Asset)"},
                {"movie ", "invalid_webm", "The embedded WebM data of '" + asset + "' failed validation"},
            });
            Json kv = parse_key_values(run.out_lines, false);
            Json width = nullptr, height = nullptr;
            if (auto size = field(kv, "size"); size.is_string()) {
                std::string s = size;
                if (auto x = s.find('x'); x != std::string::npos) {
                    width = typed_value(std::string_view(s).substr(0, x));
                    height = typed_value(std::string_view(s).substr(x + 1));
                }
            }
            return {{"asset", asset}, {"valid", true}, {"webm_bytes", field(kv, "bytes")},
                    {"width", width}, {"height", height}, {"fps", field(kv, "fps")},
                    {"duration_seconds", field(kv, "duration")}};
        },
    });

    r.add({
        .group = "video", .name = "decode-test",
        .summary = "Decode a game video's first frame and audio as a playback test",
        .description =
            "Decodes a MovieTexture2Asset with Windows Media Foundation: the first BGRA video frame, a check "
            "that playback advances past it, and the audio track to PCM16. Reports frame size, timestamps and "
            "decoded byte counts. Use it to confirm a video will play on this machine; it needs Media "
            "Foundation with WebM/VP9 support. Nothing is kept: a temporary video-*.webm is written to "
            "%TEMP%\\ReSkateStudio\\MediaWork and removed. For the container stats only, use 'video info'.",
        .params = {
            {"asset", ParamType::String,
             "EBX path of a MovieTexture2Asset, e.g. ui/videos/s01_intro/dgo_video_season01_video_01", true, true},
            game_root_param(),
        },
        .examples = {"video decode-test ui/videos/splash/vid_skate_logo_lockup_withaudio_v2_1920x1080",
                     "video decode-test ui/videos/s01_intro/dgo_video_season01_video_01 --json"},
        .long_running = true,
        .run = [](Context& c, const Json& a) -> Json {
            std::string asset = asset_name(a, "asset");
            EngineRun run = run_engine(c, {"video", path_utf8(c.game_root(a)), asset}, quiet_stderr);
            require_success_mapped(run, {
                {"video EBX was not found", "asset_not_found",
                 "No video named '" + asset + "'. The game's videos are under ui/videos/ ('asset find ui/videos')"},
                {"not a MovieTexture2Asset", "not_a_video_asset", "'" + asset + "' is not a video (MovieTexture2Asset)"},
                {"timed out", "decode_timeout", ""},
                {"Media Foundation failed", "media_foundation_failed",
                 "Windows Media Foundation with WebM/VP9 support is needed to decode game videos"},
                {"video", "decode_failed", ""},
                {"WebM", "decode_failed", ""},
            });
            Json kv = parse_key_values(run.out_lines, false);
            return {{"asset", asset}, {"width", field(kv, "width")}, {"height", field(kv, "height")},
                    {"first_frame_seconds", field(kv, "timestamp")}, {"frame_bytes", field(kv, "frame-bytes")},
                    {"playback_seconds", field(kv, "playback-timestamp")}, {"audio_bytes", field(kv, "audio-bytes")}};
        },
    });

    // ---------------------------------------------------------------- anim
    r.add({
        .group = "anim", .name = "info",
        .summary = "Describe a game animation clip: rig, joints, fps, length, channels",
        .description =
            "Reads a standalone animation clip (ClipControllerAsset) and reports its rig, skeleton, joint and "
            "channel counts, frame rate, duration and every DOF channel (id, joint, kind: q rotation, t "
            "translation, s scale, onboard, speed). With --project the clip is read as modified by that "
            ".fbproject (the project file is only read; a .fbmod is refused): a clip replaced by 'anim import' "
            "reports its new channels, e.g. 217 instead of 87 because imported clips also carry scale. "
            "Read only. Gotcha: blend and subtractive controllers (names starting subt_, e.g. "
            "animation/dingo/subt_c_proto_onb_push_regular_medium_loop_02), skeletons and rigs are not "
            "standalone clips and are rejected with not_a_clip.",
        .params = {
            {"clip", ParamType::String,
             "EBX path of a ClipControllerAsset, e.g. animation/dingo/c_proto_onb_push_regular_medium_full_static",
             true, true},
            project_param("Studio project (.fbproject) whose overrides are applied before reading the clip; only read"),
            game_root_param(),
        },
        .examples = {"anim info animation/dingo/c_proto_onb_push_regular_medium_full_static",
                     "anim info animation/dingo/c_proto_onb_push_regular_medium_full_static --project C:\\mods\\scooter.fbproject --json"},
        .run = [](Context& c, const Json& a) -> Json {
            std::string clip = asset_name(a, "clip");
            std::vector<std::string> args = {"animation-info", path_utf8(c.game_root(a)), clip};
            if (a.contains("project")) args.insert(args.end(), {"--project", path_utf8(project_arg(a))});
            EngineRun run = run_engine(c, args, quiet_stderr);
            require_success_mapped(run, clip_errors(clip));
            Json channels = Json::array();
            std::vector<std::string> header;
            for (const auto& line : run.out_lines) {
                if (line.rfind("dof=", 0) != 0) { header.push_back(line); continue; }
                Json t = parse_tokens(line, {"dof", "name"});
                std::string name = t.value("name", std::string());
                Json ch = {{"dof", t.contains("dof") ? t["dof"] : Json(nullptr)}, {"name", name}};
                if (auto dot = name.rfind('.'); dot != std::string::npos) {
                    ch["joint"] = name.substr(0, dot);
                    ch["kind"] = name.substr(dot + 1);
                }
                channels.push_back(ch);
            }
            Json kv = parse_key_values(header, false);
            Json out = {{"clip", clip}, {"rig", field(kv, "rig")}, {"skeleton", field(kv, "skeleton")},
                        {"joints", field(kv, "joints")}, {"channel_count", field(kv, "channels")},
                        {"fps", field(kv, "fps")}, {"duration_seconds", field(kv, "duration")}};
            if (a.contains("project")) out["project"] = path_utf8(path_arg(a, "project"));
            out["channels"] = channels;
            return out;
        },
    });

    r.add({
        .group = "anim", .name = "export",
        .summary = "Bake a game animation clip to an FBX file",
        .description =
            "Bakes a game animation clip (ClipControllerAsset) to an FBX holding the skeleton and one take "
            "named after the clip, ready to edit in Blender or another DCC. With --project the clip is "
            "exported as modified by that .fbproject. Writes only the output FBX (about 2 MB; an existing "
            "file is overwritten); the game is only read, and an output inside the ReSkate folder is refused "
            "(output_in_game_folder). The output folder must already exist. Blend and subtractive "
            "controllers (subt_...) are rejected with not_a_clip. Check the result with 'anim fbx-info'.",
        .params = {
            {"clip", ParamType::String,
             "EBX path of a ClipControllerAsset, e.g. animation/dingo/c_proto_onb_push_regular_medium_full_static",
             true, true},
            {"output", ParamType::Path, "FBX file to write (.fbx); its folder must exist", true, true},
            project_param("Studio project (.fbproject) whose animation replacement is applied before exporting; only read"),
            game_root_param(),
        },
        .examples = {"anim export animation/dingo/c_proto_onb_push_regular_medium_full_static C:\\anim\\push.fbx",
                     "anim export animation/dingo/c_proto_onb_push_regular_medium_full_static C:\\anim\\push_mod.fbx --project C:\\mods\\scooter.fbproject --json"},
        .long_running = true,
        .run = [](Context& c, const Json& a) -> Json {
            std::string clip = asset_name(a, "clip");
            fs::path output = path_arg(a, "output");
            if (lower_ext(output) != ".fbx")
                throw Error("invalid_arguments", "--output must end in .fbx (the engine always writes FBX)", {{"param", "output"}});
            std::error_code ec;
            if (!fs::is_directory(output.parent_path(), ec))
                throw Error("output_folder_missing", "--output: folder does not exist: " + path_utf8(output.parent_path()),
                            {{"param", "output"}, {"path", path_utf8(output.parent_path())}});
            fs::path game_root = c.game_root(a);
            refuse_game_folder(c, game_root, output, "output");
            std::vector<std::string> args = {"animation-export", path_utf8(game_root), clip, path_utf8(output)};
            if (a.contains("project")) args.insert(args.end(), {"--project", path_utf8(project_arg(a))});
            c.progress(-1, "Baking " + clip);
            EngineRun run = run_engine(c, args, quiet_stderr);
            require_success_mapped(run, clip_errors(clip));
            Json kv = parse_key_values(run.out_lines, false);
            Json out = {{"clip", clip}, {"output", kv.contains("exported") ? kv["exported"] : Json(path_utf8(output))},
                        {"bytes", file_size_or_null(output)}};
            if (a.contains("project")) out["project"] = path_utf8(path_arg(a, "project"));
            return out;
        },
    });

    r.add({
        .group = "anim", .name = "import",
        .summary = "Replace a game animation clip with an FBX take, saved as .fbproject or .fbmod",
        .description =
            "Takes one animation take from an FBX whose bones match the game rig and saves it as a replacement "
            "for a game clip (ClipControllerAsset), either as a Studio project (.fbproject, chosen by the "
            "output extension) or a ready mod (.fbmod). With --project the replacement is layered onto a copy "
            "of that project and its resources are carried over (the base file is not modified). Writes only "
            "the output file: missing folders are created and an existing file is overwritten. Nothing is "
            "deployed and the game is only read; an output inside the ReSkate folder is refused "
            "(output_in_game_folder). The output extension must be lowercase (.FBMOD is refused by the "
            "engine). A plain import holds 3 resources (the clip EBX plus channel and key data). Use "
            "'anim fbx-info' to list the takes; a take index past the last one, or a GLB/FBX without takes, "
            "fails with take_not_found. Bone names must match the rig (fbx_rig_mismatch otherwise); an FBX "
            "made by 'anim export' always matches.",
        .params = {
            {"clip", ParamType::String,
             "EBX path of the ClipControllerAsset to replace, e.g. animation/dingo/c_proto_onb_push_regular_medium_full_static",
             true, true},
            {"input", ParamType::Path, "FBX (or GLB) file holding the animation take", true, true},
            {"output", ParamType::Path, "File to write, ending in .fbproject (Studio project) or .fbmod (ready mod), lowercase",
             true, true},
            {"take", ParamType::Integer, "Take index in the FBX, 0 for the first (list them with anim fbx-info)", false, false,
             {}, Json(0)},
            project_param("Base Studio project (.fbproject) to start from; it is copied, never modified"),
            game_root_param(),
        },
        .examples = {"anim import animation/dingo/c_proto_onb_push_regular_medium_full_static C:\\anim\\push.fbx C:\\mods\\push.fbmod",
                     "anim import animation/dingo/c_proto_onb_push_regular_medium_full_static C:\\anim\\push.fbx C:\\mods\\scooter2.fbproject --project C:\\mods\\scooter.fbproject --take 0 --json"},
        .long_running = true,
        .run = [](Context& c, const Json& a) -> Json {
            std::string clip = asset_name(a, "clip");
            fs::path input = path_arg(a, "input");
            fs::path output = path_arg(a, "output");
            require_mesh_file(input, "input");
            // The engine compares the extension case-sensitively, so .FBMOD fails late; check it here.
            std::string ext = path_utf8(output.extension());
            if (ext != ".fbproject" && ext != ".fbmod")
                throw Error("invalid_arguments", "--output must end in .fbproject or .fbmod (lowercase): " + path_utf8(output),
                            {{"param", "output"}});
            long long take = a["take"];
            if (take < 0) throw Error("invalid_arguments", "--take must be 0 or more", {{"param", "take"}});
            fs::path game_root = c.game_root(a);
            refuse_game_folder(c, game_root, output, "output");
            std::vector<std::string> args = {"animation-import", path_utf8(game_root), clip,
                                             path_utf8(input), path_utf8(output), "--take", std::to_string(take)};
            if (a.contains("project")) {
                fs::path project = project_arg(a);
                std::error_code ec;
                if (fs::equivalent(project, output, ec))
                    throw Error("invalid_arguments", "--output must differ from --project", {{"param", "output"}});
                args.insert(args.end(), {"--project", path_utf8(project)});
            }
            c.progress(-1, "Importing take " + std::to_string(take) + " onto " + clip);
            EngineRun run = run_engine(c, args, quiet_stderr);
            std::vector<ErrorMap> errors = clip_errors(clip);
            errors.insert(errors.begin(), {"Select an FBX animation take", "take_not_found",
                                           "Take " + std::to_string(take) + " does not exist in " + path_utf8(input) +
                                               "; list its takes with 'anim fbx-info' (a file without animation has none)"});
            require_success_mapped(run, errors);
            Json kv = parse_key_values(run.out_lines, false);
            Json out = {{"clip", clip}, {"input", path_utf8(input)}, {"take", take},
                        {"output", kv.contains("saved") ? kv["saved"] : Json(path_utf8(output))},
                        {"format", ext.substr(1)}, {"resources", field(kv, "resources")},
                        {"bytes", file_size_or_null(output)}};
            if (a.contains("project")) out["project"] = path_utf8(path_arg(a, "project"));
            return out;
        },
    });

    r.add({
        .group = "anim", .name = "fbx-info",
        .summary = "List the skeleton size and animation takes in an FBX",
        .description =
            "Reads an .fbx or .glb file (no other formats) and reports the skeleton joint count and every "
            "animation take with its index, name, duration, fps and track count. Does not need the game and "
            "writes nothing. A file without animation returns take_count 0 and an empty takes list rather "
            "than an error. Take indexes are what 'anim import --take' expects; a take exported by "
            "'anim export' is named after its game clip.",
        .params = {
            {"file", ParamType::Path, "FBX or GLB file to inspect", true, true},
        },
        .examples = {"anim fbx-info C:\\anim\\push_regular_medium.fbx", "anim fbx-info C:\\anim\\push.fbx --json"},
        .run = [](Context& c, const Json& a) -> Json {
            fs::path file = path_arg(a, "file");
            require_mesh_file(file, "file");
            EngineRun run = run_engine(c, {"fbx-animation-info", path_utf8(file)}, quiet_stderr);
            require_success_mapped(run, clip_errors(""));
            Json takes = Json::array();
            std::vector<std::string> header;
            for (const auto& line : run.out_lines) {
                if (line.rfind("take=", 0) != 0) { header.push_back(line); continue; }
                Json t = parse_tokens(line, {"take", "name", "duration", "fps", "tracks"});
                takes.push_back({{"index", field(t, "take")}, {"name", field(t, "name")},
                                 {"duration_seconds", field(t, "duration")}, {"fps", field(t, "fps")},
                                 {"tracks", field(t, "tracks")}});
            }
            Json kv = parse_key_values(header, false);
            return {{"file", path_utf8(file)}, {"joints", field(kv, "joints")}, {"take_count", field(kv, "takes")},
                    {"takes", takes}};
        },
    });
}
}
