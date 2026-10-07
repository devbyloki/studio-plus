// The ANIMATIONS page, in four steps: find a clip, read it, export it to FBX, put an edited FBX take back.
// Every action is a registry command on the job system (asset find, anim info, anim export, anim fbx-info,
// anim import), so Activity keeps the studio-plus line for each; the page only holds what is on screen.
#include "gui/animations_page.h"

#include "core/settings.h"
#include "gui/app.h"
#include "gui/look.h"
#include "gui/page_kit.h"
#include "gui/widgets.h"

#include <shellapi.h>

#include <algorithm>
#include <filesystem>

namespace studio::gui {
namespace fs = std::filesystem;
namespace {

struct AnimState {
    std::string find, clip, export_path, fbx, output, base_project;
    bool export_edited = false, output_edited = false, show_all = false;
    int take = 0;
    std::shared_ptr<Job> search, info, exported, takes, imported, verify;
    std::string takes_for;   // the FBX `takes` was run on
    Json startup;
    bool startup_run = false, started = false;
    std::string show;  // startup option: a step to scroll to
    int show_frames = 0;
};
AnimState g_anim;

// A clip anim info accepts: a ClipControllerAsset, not a blend or subtractive controller, skeleton or rig.
bool clip_like(const std::string& name) {
    const std::string l = kit::leaf(kit::lower(name));
    return !(l.starts_with("subt_") || l.starts_with("blend") || l.find("skeleton") != std::string::npos ||
             l.ends_with("_rig") || l.starts_with("f_"));
}

void set_clip(const std::string& clip) {
    auto& s = g_anim;
    s.clip = clip;
    if (!s.export_edited) s.export_path = kit::data_path(L"Animations", kit::leaf(clip) + ".fbx");
    if (!s.output_edited) s.output = kit::data_path(L"Animations", kit::leaf(clip) + ".fbproject");
    if (s.fbx.empty()) s.fbx = s.export_path;
    s.info = kit::run("anim", "info", {{"clip", clip}});
}

void run_search() {
    auto& s = g_anim;
    if (!s.find.empty()) s.search = kit::run("asset", "find", {{"text", s.find}, {"limit", 500}});
}

void run_takes() {
    auto& s = g_anim;
    s.takes_for = s.fbx;
    s.take = 0;
    s.takes = kit::run("anim", "fbx-info", {{"file", s.fbx}});
}

void start() {
    auto& s = g_anim;
    if (s.started) return;
    s.started = true;
    const Json& a = s.startup;
    s.find = a.value("find", "");
    s.show = a.value("show", "");
    if (a.contains("fbx")) s.fbx = a.value("fbx", "");
    if (a.contains("output")) { s.output = a.value("output", ""); s.output_edited = true; }
    if (s.startup_run) {
        run_search();
        if (a.contains("clip")) set_clip(a.value("clip", ""));
        if (a.contains("fbx")) run_takes();  // only an FBX that was given, not the export path set_clip picks
    } else if (a.contains("clip")) {
        s.clip = a.value("clip", "");
    }
}

// ------------------------------------------------------------------ 1. find
void find_tile(App& app) {
    auto& s = g_anim;
    kit::scroll_here(s.show, s.show_frames, "find");
    begin_tile("##anim_find", 61);
    kit::step_title(1, "FIND A CLIP");
    kit::muted("Search the game's assets by name, then pick a clip. Clips live under animation/; blend and "
               "subtractive controllers (subt_...) are not clips on their own and are hidden unless you show them.");
    ImGui::SetNextItemWidth(ImGui::GetContentRegionAvail().x - S(260));
    const bool enter = input_text("##find", s.find, ImGuiInputTextFlags_EnterReturnsTrue, "Part of the name, e.g. push_regular");
    ImGui::SameLine();
    ImGui::BeginDisabled(s.find.empty() || kit::running(s.search));
    if (primary_button("FIND", ImVec2(S(110), 0)) || (enter && !s.find.empty())) run_search();
    ImGui::EndDisabled();
    ImGui::SameLine();
    ImGui::AlignTextToFramePadding();
    toggle("##show_all", &s.show_all);
    ImGui::SameLine();
    ImGui::TextDisabled("Show all");
    if (kit::running(s.search) || (s.search && !kit::succeeded(s.search))) kit::status(s.search, "Searched");
    const Json r = kit::result(s.search);
    if (r.is_object()) {
        std::vector<std::string> names;
        int hidden = 0;
        for (const auto& m : r.value("matches", Json::array())) {
            const std::string name = m.value("name", "");
            if (m.value("kind", "") != "ebx" || !kit::lower(name).starts_with("animation/")) continue;
            if (!s.show_all && !clip_like(name)) { ++hidden; continue; }
            names.push_back(name);
        }
        ImGui::TextDisabled("%zu clips%s", names.size(),
                            hidden ? (", " + std::to_string(hidden) + " controllers hidden").c_str() : "");
        if (kit::number(r, "count", 0) >= 500) {
            ImGui::SameLine();
            ImGui::TextDisabled("(first 500 matches: type more of the name)");
        }
        const float row = S(30);
        const float height = std::min(S(240), row * static_cast<float>(std::max<size_t>(names.size(), 1)) + S(4));
        ImGui::BeginChild("##clips", ImVec2(0, height), 0);
        const float width = ImGui::GetContentRegionAvail().x;
        ImGuiListClipper clipper;
        clipper.Begin(static_cast<int>(names.size()), row);
        while (clipper.Step())
            for (int i = clipper.DisplayStart; i < clipper.DisplayEnd; ++i) {
                const auto& name = names[static_cast<size_t>(i)];
                ImGui::PushID(i);
                const ImVec2 at = ImGui::GetCursorScreenPos();
                if (list_row("##clip", width, row, name == s.clip)) set_clip(name);
                ImGui::GetWindowDrawList()->AddText(ImVec2(at.x + S(12), at.y + (row - ImGui::GetTextLineHeight()) * 0.5f),
                    name == s.clip ? color::text : color::muted, name.c_str());
                ImGui::PopID();
            }
        ImGui::EndChild();
    }
    (void)app;
    end_tile();
}

// ------------------------------------------------------------------ 2. read
void info_tile() {
    auto& s = g_anim;
    kit::scroll_here(s.show, s.show_frames, "info");
    begin_tile("##anim_info", 62);
    kit::step_title(2, "READ THE CLIP");
    ImGui::SetNextItemWidth(ImGui::GetContentRegionAvail().x - S(130));
    input_text("##clip", s.clip, 0, "animation/dingo/...");
    ImGui::SameLine();
    ImGui::BeginDisabled(s.clip.empty() || kit::running(s.info));
    if (ImGui::Button("READ", ImVec2(S(120), 0))) set_clip(s.clip);
    ImGui::EndDisabled();
    kit::status(s.info, "Read");
    const Json r = kit::result(s.info);
    if (r.is_object()) {
        ImGui::Spacing();
        if (ImGui::BeginTable("##info", 4, ImGuiTableFlags_SizingStretchSame)) {
            ImGui::TableNextColumn(); field("LENGTH", std::to_string(kit::number(r, "duration_seconds", 0.0)).substr(0, 4) + " s");
            ImGui::TableNextColumn(); field("FRAME RATE", std::to_string(kit::number(r, "fps", 0)) + " fps");
            ImGui::TableNextColumn(); field("CHANNELS", std::to_string(kit::number(r, "channel_count", 0)));
            ImGui::TableNextColumn(); field("JOINTS", std::to_string(kit::number(r, "joints", 0)));
            ImGui::EndTable();
        }
        field("RIG", r.value("rig", ""));
        field("SKELETON", r.value("skeleton", ""));
    }
    end_tile();
}

// ------------------------------------------------------------------ 3. export
void export_tile(App& app) {
    auto& s = g_anim;
    kit::scroll_here(s.show, s.show_frames, "export");
    begin_tile("##anim_export", 63);
    kit::step_title(3, "EXPORT TO FBX");
    kit::muted("Bakes the clip on its rig into an FBX you can edit in Blender or any FBX tool.");
    if (path_field("##export", s.export_path, app.window, false, {{L"FBX (*.fbx)", L"*.fbx"}}, S(110))) s.export_edited = true;
    ImGui::BeginDisabled(s.clip.empty() || s.export_path.empty() || kit::running(s.exported));
    if (primary_button("EXPORT")) s.exported = kit::run("anim", "export", {{"clip", s.clip}, {"output", s.export_path}});
    ImGui::EndDisabled();
    if (s.clip.empty()) { ImGui::SameLine(); ImGui::AlignTextToFramePadding(); ImGui::TextDisabled("Pick a clip first"); }
    kit::status(s.exported, "Exported");
    const Json r = kit::result(s.exported);
    if (r.is_object()) {
        field("FBX", r.value("output", ""));
        if (ImGui::Button("OPEN FOLDER"))
            ShellExecuteW(nullptr, L"open", fs::path(utf8_to_wide(r.value("output", ""))).parent_path().c_str(), nullptr, nullptr, SW_SHOWNORMAL);
        ImGui::SameLine();
        if (ImGui::Button("USE FOR STEP 4")) { s.fbx = r.value("output", ""); run_takes(); }
    }
    end_tile();
}

// ------------------------------------------------------------------ 4. import
void import_tile(App& app) {
    auto& s = g_anim;
    kit::scroll_here(s.show, s.show_frames, "import");
    begin_tile("##anim_import", 64);
    kit::step_title(4, "PUT AN FBX TAKE BACK");
    kit::muted("Replaces the clip with a take from your FBX, baked at the clip's frame rate, keeping its loop, root "
               "motion and tags. The game is not changed: the result is a .fbproject or .fbmod for PROJECT & MODS.");
    kit::caption("FBX");
    path_field("##fbx", s.fbx, app.window, false, {{L"FBX (*.fbx)", L"*.fbx"}}, S(110));
    ImGui::BeginDisabled(s.fbx.empty() || kit::running(s.takes));
    if (ImGui::Button("LIST TAKES")) run_takes();
    ImGui::EndDisabled();
    if (!s.takes_for.empty() && s.takes_for != s.fbx) { ImGui::SameLine(); ImGui::AlignTextToFramePadding(); ImGui::TextDisabled("The FBX changed: list its takes again"); }
    if (kit::running(s.takes) || (s.takes && !kit::succeeded(s.takes))) kit::status(s.takes, "Read");
    const Json t = kit::result(s.takes);
    if (t.is_object()) {
        ImGui::TextDisabled("%d joints, %d takes", kit::number(t, "joints", 0), kit::number(t, "take_count", 0));
        const float width = ImGui::GetContentRegionAvail().x;
        int i = 0;
        for (const auto& take : t.value("takes", Json::array())) {
            ImGui::PushID(i);
            const ImVec2 at = ImGui::GetCursorScreenPos();
            if (list_row("##take", width, S(30), s.take == take.value("index", i))) s.take = take.value("index", i);
            char line[512];
            std::snprintf(line, sizeof(line), "Take %d   %s   %.2f s at %d fps, %d tracks", take.value("index", i),
                          take.value("name", "").c_str(), kit::number(take, "duration_seconds", 0.0), kit::number(take, "fps", 0), kit::number(take, "tracks", 0));
            ImGui::GetWindowDrawList()->AddText(ImVec2(at.x + S(12), at.y + (S(30) - ImGui::GetTextLineHeight()) * 0.5f), color::text, line);
            ImGui::PopID();
            ++i;
        }
    }
    ImGui::Spacing();
    kit::caption("SAVE AS (.fbproject or .fbmod)");
    if (path_field("##output", s.output, app.window, false, {{L"Project (*.fbproject)", L"*.fbproject"}, {L"Frosty mod (*.fbmod)", L"*.fbmod"}}, S(110)))
        s.output_edited = true;
    kit::caption("BUILD ON A PROJECT (optional: its other changes are kept)");
    path_field("##base", s.base_project, app.window, false, {{L"Project (*.fbproject)", L"*.fbproject"}}, S(110));
    const bool ready = !s.clip.empty() && !s.fbx.empty() && !s.output.empty();
    ImGui::BeginDisabled(!ready || kit::running(s.imported));
    if (primary_button("REPLACE CLIP"))
        s.imported = kit::run("anim", "import", {{"clip", s.clip}, {"input", s.fbx}, {"output", s.output}, {"take", s.take},
                                                 {"project", s.base_project}});
    ImGui::EndDisabled();
    ImGui::SameLine();
    ImGui::AlignTextToFramePadding();
    if (ready) ImGui::TextDisabled("Take %d onto %s", s.take, kit::leaf(s.clip).c_str());
    else ImGui::TextDisabled("Needs a clip (step 1), an FBX and a file to save");
    kit::status(s.imported, "Saved");
    const Json r = kit::result(s.imported);
    if (r.is_object()) {
        field("SAVED", r.value("output", "") + "  (" + kit::thousands(r.value("bytes", 0LL)) + " bytes, " +
                           std::to_string(kit::number(r, "resources", 0)) + " resources)");
        const bool project = kit::lower(r.value("output", "")).ends_with(".fbproject");
        ImGui::BeginDisabled(!project || kit::running(s.verify));
        if (ImGui::Button("CHECK THE NEW CLIP"))
            s.verify = kit::run("anim", "info", {{"clip", s.clip}, {"project", r.value("output", "")}});
        ImGui::EndDisabled();
        if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
            ImGui::SetTooltip(project ? "Reads the clip as the project changes it (anim info --project)" : "Only a .fbproject can be read back");
        ImGui::SameLine();
        if (ImGui::Button("OPEN PROJECT & MODS")) app.page = Page::project;
        kit::status(s.verify, "Read back");
        const Json v = kit::result(s.verify);
        if (v.is_object())
            kit::muted("The project's clip: " + std::to_string(kit::number(v, "channel_count", 0)) + " channels, " +
                       std::to_string(kit::number(v, "fps", 0)) + " fps, " + std::to_string(kit::number(v, "duration_seconds", 0.0)).substr(0, 4) + " s.");
    }
    end_tile();
}
} // namespace

void animations_startup(const Json& args, bool run) {
    g_anim.startup = args;
    g_anim.startup_run = run;
}

void animations_page(App& app) {
    start();
    kit::heading("ANIMATIONS");
    kit::muted("Swap one of the game's animation clips for your own: find it, export it to FBX, edit it, put it back. "
               "Each step is a studio-plus anim command; ACTIVITY keeps the command line for each run.");
    ImGui::Spacing();
    find_tile(app);
    ImGui::Spacing();
    info_tile();
    ImGui::Spacing();
    export_tile(app);
    ImGui::Spacing();
    import_tile(app);
}

} // namespace studio::gui
