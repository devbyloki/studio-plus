// The ASSETS page. The left panel is the asset index as a folder tree (asset tree, one folder at a
// time as it is opened) or, while there is search text, the matches (asset search); both are drawn
// with a list clipper, so only the rows on screen cost anything. The right panel inspects the asset
// picked: asset info first, then ebx get, a texture export for the preview, or asset lua-source,
// depending on what the asset is. Every one of those is a registry command on the job system, so
// Activity keeps the studio-plus line for it; the page only holds what is on screen.
#include "gui/assets_page.h"

#include "core/settings.h"
#include "gui/app.h"
#include "gui/jobs.h"
#include "gui/look.h"
#include "gui/renderer.h"
#include "gui/widgets.h"

#include <shellapi.h>
#include <shobjidl.h>
#include <wincodec.h>
#include <wrl/client.h>

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <filesystem>
#include <map>

namespace studio::gui {
namespace fs = std::filesystem;
using Microsoft::WRL::ComPtr;
namespace {

// ---------------------------------------------------------------- small helpers

void heading(const char* text) {
    ImGui::PushFont(g_fonts.tile);
    ImGui::TextUnformatted(text);
    ImGui::PopFont();
}

void wrapped_muted(const std::string& text) {
    ImGui::PushTextWrapPos(0);
    ImGui::TextDisabled("%s", text.c_str());
    ImGui::PopTextWrapPos();
}

void wrapped_colour(const std::string& text, ImU32 colour) {
    ImGui::PushStyleColor(ImGuiCol_Text, colour);
    ImGui::PushTextWrapPos(0);
    ImGui::TextUnformatted(text.c_str());
    ImGui::PopTextWrapPos();
    ImGui::PopStyleColor();
}

void caption(const std::string& text) {
    ImGui::PushFont(g_fonts.caption);
    ImGui::TextDisabled("%s", text.c_str());
    ImGui::PopFont();
}

std::string grouped(long long value) {
    std::string digits = std::to_string(value);
    for (int at = static_cast<int>(digits.size()) - 3; at > (digits[0] == '-' ? 1 : 0); at -= 3)
        digits.insert(static_cast<size_t>(at), ",");
    return digits;
}

std::string size_text(double bytes) {
    const char* units[]{"B", "KB", "MB", "GB"};
    int unit = 0;
    while (bytes >= 1024 && unit < 3) { bytes /= 1024; ++unit; }
    char buffer[32];
    std::snprintf(buffer, sizeof(buffer), unit == 0 ? "%.0f %s" : "%.1f %s", bytes, units[unit]);
    return buffer;
}

std::string upper(std::string text) {
    for (auto& c : text) c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
    return text;
}

std::string last_part(const std::string& name) {
    const auto slash = name.rfind('/');
    return slash == std::string::npos ? name : name.substr(slash + 1);
}

std::string folder_part(const std::string& name) {
    const auto slash = name.rfind('/');
    return slash == std::string::npos ? std::string() : name.substr(0, slash);
}

double number_of(const Json& object, const char* key) {
    if (!object.is_object() || !object.contains(key) || !object[key].is_number()) return 0;
    return object[key].get<double>();
}

std::shared_ptr<Job> run(const char* group, const char* name, Json args) {
    const Command* c = Registry::instance().find(group, name);
    return c ? g_jobs.start(*c, std::move(args)) : nullptr;
}

bool running(const std::shared_ptr<Job>& job) { return job && !job->done.load(); }

// Takes a finished job's outcome once: true and `out` filled, and the job let go.
bool take(std::shared_ptr<Job>& job, Json& out) {
    if (!job || !job->done.load()) return false;
    {
        std::lock_guard lock(job->mutex);
        out = job->outcome;
    }
    job.reset();
    return true;
}

std::pair<double, std::string> progress_of(const std::shared_ptr<Job>& job) {
    if (!job) return {-1, ""};
    std::lock_guard lock(job->mutex);
    return {job->fraction, job->message};
}

std::string error_text(const Json& outcome) {
    if (outcome.contains("error")) return outcome["error"].value("message", "The command failed");
    return "The command failed";
}

// A panel with the tiles' rough edge, at a fixed size, for content that scrolls inside it.
void begin_panel(const char* id, unsigned seed, ImVec2 size) {
    const ImVec2 parent_min = ImGui::GetWindowDrawList()->GetClipRectMin();
    const ImVec2 parent_max = ImGui::GetWindowDrawList()->GetClipRectMax();
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(S(18), S(14)));
    ImGui::BeginChild(id, size, ImGuiChildFlags_AlwaysUseWindowPadding, ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
    ImGui::PopStyleVar();
    const ImVec2 a = ImGui::GetWindowPos();
    const ImVec2 b(a.x + ImGui::GetWindowSize().x, a.y + ImGui::GetWindowSize().y);
    auto* draw = ImGui::GetWindowDrawList();
    draw->PushClipRect(ImVec2(std::max(parent_min.x, a.x - S(3)), std::max(parent_min.y, a.y - S(3))),
        ImVec2(std::min(parent_max.x, b.x + S(3)), std::min(parent_max.y, b.y + S(3))));
    rough_rect(draw, a, b, color::tile, seed);
    draw->PopClipRect();
}

void end_panel() { ImGui::EndChild(); }

// A progress bar the width of the line, with the message under it.
void job_progress(const std::shared_ptr<Job>& job, const char* fallback) {
    const auto [fraction, message] = progress_of(job);
    auto* draw = ImGui::GetWindowDrawList();
    const ImVec2 a = ImGui::GetCursorScreenPos();
    const float width = ImGui::GetContentRegionAvail().x;
    // Unknown progress sweeps instead of sitting at zero.
    const float time = static_cast<float>(ImGui::GetTime());
    const float fill = fraction < 0 ? std::fmod(time * 0.5f, 1.0f) : static_cast<float>(fraction);
    progress_bar(draw, a, ImVec2(a.x + width, a.y + S(10)), fill, time);
    ImGui::Dummy(ImVec2(width, S(12)));
    wrapped_muted(message.empty() ? fallback : message);
}

// IFileSaveDialog with one file type. Empty when cancelled.
std::wstring pick_save(HWND owner, const std::wstring& name, const wchar_t* type_name, const wchar_t* pattern, const wchar_t* ext) {
    ComPtr<IFileSaveDialog> dialog;
    if (FAILED(CoCreateInstance(CLSID_FileSaveDialog, nullptr, CLSCTX_INPROC_SERVER, IID_PPV_ARGS(&dialog)))) return {};
    COMDLG_FILTERSPEC spec[]{{type_name, pattern}, {L"All files", L"*.*"}};
    dialog->SetFileTypes(2, spec);
    dialog->SetDefaultExtension(ext);
    dialog->SetFileName(name.c_str());
    FILEOPENDIALOGOPTIONS options{};
    dialog->GetOptions(&options);
    dialog->SetOptions(options | FOS_FORCEFILESYSTEM | FOS_OVERWRITEPROMPT);
    if (FAILED(dialog->Show(owner))) return {};
    ComPtr<IShellItem> result;
    PWSTR path = nullptr;
    if (FAILED(dialog->GetResult(&result)) || FAILED(result->GetDisplayName(SIGDN_FILESYSPATH, &path))) return {};
    std::wstring out = path;
    CoTaskMemFree(path);
    return out;
}

// A PNG (or any image WIC reads) as straight-alpha RGBA.
bool load_image(const fs::path& path, std::vector<unsigned char>& rgba, UINT& width, UINT& height) {
    ComPtr<IWICImagingFactory> factory;
    if (FAILED(CoCreateInstance(CLSID_WICImagingFactory, nullptr, CLSCTX_INPROC_SERVER, IID_PPV_ARGS(&factory)))) return false;
    ComPtr<IWICBitmapDecoder> decoder;
    if (FAILED(factory->CreateDecoderFromFilename(path.c_str(), nullptr, GENERIC_READ, WICDecodeMetadataCacheOnDemand, &decoder)))
        return false;
    ComPtr<IWICBitmapFrameDecode> frame;
    ComPtr<IWICFormatConverter> converter;
    if (FAILED(decoder->GetFrame(0, &frame)) || FAILED(factory->CreateFormatConverter(&converter)) ||
        FAILED(converter->Initialize(frame.Get(), GUID_WICPixelFormat32bppRGBA, WICBitmapDitherTypeNone, nullptr, 0,
                                     WICBitmapPaletteTypeCustom)) ||
        FAILED(converter->GetSize(&width, &height)) || !width || !height)
        return false;
    rgba.resize(static_cast<size_t>(width) * height * 4);
    return SUCCEEDED(converter->CopyPixels(nullptr, width * 4, static_cast<UINT>(rgba.size()), rgba.data()));
}

std::string file_safe(const std::string& name) {
    std::string out;
    for (char c : name) out.push_back(std::isalnum(static_cast<unsigned char>(c)) || c == '_' || c == '-' ? c : '_');
    return out;
}

// ---------------------------------------------------------------- what the page remembers

const char* const kinds[]{"all", "ebx", "res", "chunk"};
const char* const kind_labels[]{"All kinds", "EBX assets", "Resources", "Chunks"};
const char* const categories[]{"", "texture", "mesh", "lua", "level", "shader", "animation", "audio", "video", "blueprint", "other"};
const char* const category_labels[]{"All groups", "Textures", "Meshes", "Lua scripts", "Levels", "Shaders", "Animations",
                                    "Audio", "Video", "Blueprints", "Other"};
enum class Tab { properties, texture, lua, info };
const char* const tab_labels[]{"PROPERTIES", "TEXTURE", "LUA", "INFO"};
const char* const tab_views[]{"properties", "texture", "lua", "info"};

struct Folder {
    std::shared_ptr<Job> job;
    bool loaded = false, open = false;
    std::string error;
    std::vector<Json> folders, assets;
    long long asset_total = 0;
};

struct Row {
    int depth = 0;
    bool folder = false, more = false, loading = false;
    std::string path;    // folder path, or the asset's name
    std::string label;
    long long count = 0;
    const Json* asset = nullptr;
};

struct Pick {
    std::string name, kind;
};

struct AssetsState {
    bool started = false;
    Json startup = Json::object();
    bool startup_applied = false;

    std::shared_ptr<Job> index;
    Json index_result;
    bool ready = false;
    std::string index_error, index_error_code;

    std::string query, searched;
    double edited_at = 0;
    int kind = 1, category = 0;
    std::string type, type_filter;
    std::shared_ptr<Job> types_job;
    std::vector<std::pair<std::string, long long>> types;

    std::map<std::string, Folder> folders;   // by path; "" is the top
    std::vector<Row> rows;

    std::shared_ptr<Job> search;
    std::vector<Json> results;
    long long result_total = 0;
    bool appending = false;
    std::string search_error;

    Pick pick;
    std::vector<Pick> back;
    std::shared_ptr<Job> info_job, ebx_job, texture_job, lua_job, export_job;
    Json info, ebx, texture, lua;
    std::string ebx_error, texture_error, lua_error, lua_text;
    Tab tab = Tab::info;
    Json views = Json::array();
    int mip = -1;  // -1: the largest that fits the preview
    ImTextureID preview{};
    UINT preview_width = 0, preview_height = 0;
    std::string export_message;
    std::map<std::string, std::string> edits;  // field path -> new value, saved with ebx set
    std::string editing, edit_text;            // the path whose value is being typed
    std::shared_ptr<Job> draft_job;
    std::string draft_message;
    bool draft_ok = false;
    bool export_ok = false;
    std::string export_path;
    bool scroll_to_pick = false;
};
AssetsState g;

std::string kind_arg() { return kinds[g.kind]; }

Json filter_args() {
    Json a = {{"kind", kind_arg()}};
    if (g.category) a["category"] = categories[g.category];
    if (!g.type.empty()) a["type"] = g.type;
    return a;
}

void load_folder(const std::string& path, long long offset = 0) {
    auto& f = g.folders[path];
    Json a = filter_args();
    a["path"] = path;
    if (offset) a["offset"] = offset;
    a["limit"] = 2000;
    f.job = run("asset", "tree", a);
}

void reset_tree() {
    g.folders.clear();
    load_folder("");
    g.folders[""].open = true;
}

void start_search(bool append) {
    if (g.query.empty()) return;
    Json a = filter_args();
    a["text"] = g.query;
    a["limit"] = 300;
    if (append) a["offset"] = static_cast<long long>(g.results.size());
    g.search = run("asset", "search", a);
    g.searched = g.query;
    g.appending = append;
    g.search_error.clear();
}

void filters_changed() {
    reset_tree();
    if (!g.query.empty()) start_search(false);
}

bool has_view(const char* view) {
    for (const auto& v : g.views) if (v.is_string() && v.get<std::string>() == view) return true;
    return false;
}

void release_preview(App& app) {
    if (g.preview && app.renderer) app.renderer->release_texture(g.preview);
    g.preview = {};
    g.preview_width = g.preview_height = 0;
}

fs::path preview_file() {
    return Settings::data_dir() / L"asset-previews" /
           fs::path(utf8_to_wide(file_safe(g.pick.name.size() > 120 ? g.pick.name.substr(g.pick.name.size() - 120) : g.pick.name) +
                                 "_" + std::to_string(g.mip) + ".png"));
}

void start_view(Tab tab) {
    g.tab = tab;
    const Json name = g.pick.name;
    if (tab == Tab::properties && g.ebx.is_null() && !g.ebx_job && g.ebx_error.empty())
        g.ebx_job = run("ebx", "get", {{"name", name}, {"limit", 300}, {"max-array", 256}});
    if (tab == Tab::texture && g.texture.is_null() && !g.texture_job && g.texture_error.empty()) {
        Json a = {{"name", name}, {"kind", g.pick.kind}, {"output", path_utf8(preview_file())}};
        if (g.mip >= 0) a["mip"] = g.mip;
        else a["max-size"] = 2048;
        g.texture_job = run("texture", "export", a);
    }
    if (tab == Tab::lua && g.lua.is_null() && !g.lua_job && g.lua_error.empty())
        g.lua_job = run("asset", "lua-source", {{"name", name}, {"kind", g.pick.kind}});
}

void select(App& app, const std::string& name, const std::string& kind, bool remember = true) {
    if (name == g.pick.name && kind == g.pick.kind) return;
    if (remember && !g.pick.name.empty()) g.back.push_back(g.pick);
    if (g.back.size() > 50) g.back.erase(g.back.begin());
    g.pick = {name, kind};
    g.info = g.ebx = g.texture = g.lua = Json();
    g.ebx_error.clear(); g.texture_error.clear(); g.lua_error.clear(); g.lua_text.clear();
    g.ebx_job.reset(); g.texture_job.reset(); g.lua_job.reset();
    g.views = Json::array();
    g.mip = -1;
    g.export_message.clear();
    g.edits.clear();
    g.editing.clear();
    g.draft_message.clear();
    release_preview(app);
    g.info_job = run("asset", "info", {{"name", name}, {"kind", kind}});
}

// ---------------------------------------------------------------- polling

void apply_startup(App& app) {
    g.startup_applied = true;
    const Json& s = g.startup;
    auto text = [&](const char* key) { return s.contains(key) && s[key].is_string() ? s[key].get<std::string>() : std::string(); };
    for (int i = 0; i < 4; ++i) if (text("kind") == kinds[i]) g.kind = i;
    for (int i = 1; i < 11; ++i) if (text("category") == categories[i]) g.category = i;
    if (!text("type").empty()) g.type = text("type");
    if (!text("kind").empty() || !text("category").empty() || !text("type").empty()) reset_tree();
    std::string open = text("open");
    while (!open.empty()) {
        const auto cut = open.find(';');
        std::string path = open.substr(0, cut);
        while (!path.empty() && path.back() == '/') path.pop_back();
        // Every folder on the way down opens too.
        for (size_t at = 0; at != std::string::npos;) {
            at = path.find('/', at + 1);
            const std::string part = path.substr(0, at);
            auto& f = g.folders[part];
            f.open = true;
            if (!f.loaded && !f.job) load_folder(part);
        }
        open = cut == std::string::npos ? "" : open.substr(cut + 1);
    }
    if (!text("search").empty()) {
        g.query = text("search");
        start_search(false);
    }
    if (!text("select").empty()) {
        select(app, text("select"), text("select-kind").empty() ? "ebx" : text("select-kind"), false);
        g.scroll_to_pick = true;
        // Draft changes, Path=Value, given once or more.
        Json edits = s.value("edit", Json());
        if (edits.is_string()) edits = Json::array({edits});
        if (edits.is_array())
            for (const auto& edit : edits) {
                const std::string pair = edit.is_string() ? edit.get<std::string>() : "";
                const auto eq = pair.find('=');
                if (eq != std::string::npos && eq > 0) g.edits[pair.substr(0, eq)] = pair.substr(eq + 1);
            }
    }
}

void poll(App& app) {
    Json out;
    if (take(g.index, out)) {
        if (out.value("ok", false)) {
            g.ready = true;
            g.index_result = out["result"];
            g.index_error.clear();
            reset_tree();
            g.types_job = run("asset", "types", Json::object());
        } else {
            g.index_error = error_text(out);
            g.index_error_code = out.contains("error") ? out["error"].value("code", "") : "";
        }
    }
    if (g.ready && !g.startup_applied) apply_startup(app);
    if (take(g.types_job, out) && out.value("ok", false)) {
        g.types.clear();
        for (const auto& t : out["result"]["types"]) g.types.emplace_back(t.value("type", ""), t.value("count", 0ll));
    }
    for (auto& [path, f] : g.folders) {
        if (!take(f.job, out)) continue;
        if (!out.value("ok", false)) {
            f.error = error_text(out);
            continue;
        }
        const Json& r = out["result"];
        const bool more = r.value("offset", 0ll) > 0;
        if (!more) {
            f.folders.assign(r["folders"].begin(), r["folders"].end());
            f.assets.clear();
        }
        for (const auto& a : r["assets"]) f.assets.push_back(a);
        f.asset_total = r.value("asset_total", 0ll);
        f.loaded = true;
        f.error.clear();
    }
    if (take(g.search, out)) {
        if (out.value("ok", false)) {
            if (!g.appending) g.results.clear();
            for (const auto& m : out["result"]["matches"]) g.results.push_back(m);
            g.result_total = out["result"].value("total", 0ll);
        } else {
            g.search_error = error_text(out);
        }
    }
    if (take(g.info_job, out)) {
        if (out.value("ok", false)) {
            g.info = out["result"];
            g.views = g.info.value("views", Json::array());
            const auto& startup_tab = g.startup.value("tab", "");
            Tab tab = has_view("texture") ? Tab::texture : has_view("lua") ? Tab::lua : has_view("properties") ? Tab::properties : Tab::info;
            for (int i = 0; i < 4; ++i)
                if (startup_tab == tab_views[i] && (i == 3 || has_view(tab_views[i]))) tab = static_cast<Tab>(i);
            g.startup.erase("tab");
            start_view(tab);
        } else {
            g.info = {{"error", error_text(out)}};
        }
    }
    if (take(g.ebx_job, out)) {
        if (out.value("ok", false)) g.ebx = out["result"];
        else g.ebx_error = error_text(out);
    }
    if (take(g.lua_job, out)) {
        if (out.value("ok", false)) {
            g.lua = out["result"];
            g.lua_text = g.lua.value("source", "");
        } else {
            g.lua_error = error_text(out);
        }
    }
    if (take(g.texture_job, out)) {
        if (out.value("ok", false)) {
            g.texture = out["result"];
            std::vector<unsigned char> rgba;
            UINT w = 0, h = 0;
            release_preview(app);
            if (load_image(fs::path(utf8_to_wide(g.texture.value("output", ""))), rgba, w, h) && app.renderer) {
                g.preview = app.renderer->upload_texture(rgba, w, h);
                g.preview_width = w;
                g.preview_height = h;
            }
            if (!g.preview) g.texture_error = "The preview could not be shown (no free texture slot, or the PNG did not load).";
        } else {
            g.texture_error = error_text(out);
        }
    }
    if (take(g.draft_job, out)) {
        g.draft_ok = out.value("ok", false);
        if (g.draft_ok) {
            const auto& r = out["result"];
            g.draft_message = "Saved " + r.value("output", "") + " with " + std::to_string(r["changes"].size()) + " change(s)." +
                              (r.value("roundtrip_identical", false) ? "" : " Note: this asset does not write back byte for byte "
                               "even unchanged, so check it before use.");
            g.edits.clear();
        } else {
            g.draft_message = error_text(out);
        }
    }
    if (take(g.export_job, out)) {
        g.export_ok = out.value("ok", false);
        g.export_message = g.export_ok ? "Saved " + out["result"].value("output", "") : error_text(out);
        if (g.export_ok) g.export_path = out["result"].value("output", "");
    }
    // Search a moment after typing stops, so each keystroke is not a command of its own.
    if (g.ready && g.query != g.searched && ImGui::GetTime() - g.edited_at > 0.3) {
        if (g.query.empty()) {
            g.searched.clear();
            g.results.clear();
            g.search.reset();
        } else {
            start_search(false);
        }
    }
}

// ---------------------------------------------------------------- the left panel

void flatten(const std::string& path, int depth) {
    auto it = g.folders.find(path);
    if (it == g.folders.end()) return;
    const Folder& f = it->second;
    if (!f.loaded) {
        Row r;
        r.depth = depth;
        r.loading = true;
        r.label = f.error.empty() ? "Loading..." : f.error;
        g.rows.push_back(std::move(r));
        return;
    }
    for (const auto& sub : f.folders) {
        Row r;
        r.depth = depth;
        r.folder = true;
        r.path = sub.value("path", "");
        r.label = sub.value("name", "");
        r.count = sub.value("count", 0ll);
        g.rows.push_back(r);
        auto child = g.folders.find(r.path);
        if (child != g.folders.end() && child->second.open) flatten(r.path, depth + 1);
    }
    for (const auto& a : f.assets) {
        Row r;
        r.depth = depth;
        r.path = a.value("name", "");
        r.label = a.value("label", "");
        r.asset = &a;
        g.rows.push_back(std::move(r));
    }
    if (static_cast<long long>(f.assets.size()) < f.asset_total) {
        Row r;
        r.depth = depth;
        r.more = true;
        r.path = path;
        r.count = f.asset_total - static_cast<long long>(f.assets.size());
        r.loading = static_cast<bool>(f.job);
        g.rows.push_back(std::move(r));
    }
}

// A row's name from `at`, and its type right-aligned at `right` when both fit. The name always wins:
// the type is dropped, then the name clipped, so the part of a name that tells assets apart stays.
void name_and_type(ImDrawList* draw, ImFont* font, ImVec2 at, float right, const std::string& name, const std::string& type,
                   float type_y, ImU32 ink) {
    const float name_w = font->CalcTextSizeA(font->FontSize, FLT_MAX, 0, name.c_str()).x;
    const float type_w = g_fonts.caption->CalcTextSizeA(g_fonts.caption->FontSize, FLT_MAX, 0, type.c_str()).x;
    const bool both = at.x + name_w + S(14) + type_w <= right;
    const ImVec4 clip(at.x, at.y - S(4), right, at.y + font->FontSize + S(4));
    draw->AddText(font, font->FontSize, at, ink, name.c_str(), nullptr, 0, &clip);
    if (both) draw->AddText(g_fonts.caption, g_fonts.caption->FontSize, ImVec2(right - type_w, type_y), color::muted, type.c_str());
}

void kind_mark(ImDrawList* draw, ImVec2 at, const std::string& kind) {
    if (kind == "ebx") return;
    badge(draw, at, kind == "res" ? "RES" : "CHUNK", kind == "res" ? color::tile_grey : color::avatar, color::text);
}

// A folder's open/closed arrow, centred on `centre`.
void arrow(ImDrawList* draw, ImVec2 centre, bool open) {
    const float r = S(4.5f);
    if (open)
        draw->AddTriangleFilled(ImVec2(centre.x - r, centre.y - r * 0.5f), ImVec2(centre.x + r, centre.y - r * 0.5f),
                                ImVec2(centre.x, centre.y + r * 0.7f), color::muted);
    else
        draw->AddTriangleFilled(ImVec2(centre.x - r * 0.5f, centre.y - r), ImVec2(centre.x + r * 0.7f, centre.y),
                                ImVec2(centre.x - r * 0.5f, centre.y + r), color::muted);
}

void tree_rows(App& app, float width) {
    g.rows.clear();
    flatten("", 0);
    const float height = S(30), indent = S(16);
    auto* draw = ImGui::GetWindowDrawList();
    ImGuiListClipper clipper;
    clipper.Begin(static_cast<int>(g.rows.size()), height);
    int pick_row = -1;
    if (g.scroll_to_pick)
        for (size_t i = 0; i < g.rows.size(); ++i)
            if (!g.rows[i].folder && g.rows[i].asset && g.rows[i].path == g.pick.name) pick_row = static_cast<int>(i);
    if (pick_row >= 0) clipper.IncludeItemByIndex(pick_row);
    while (clipper.Step())
        for (int n = clipper.DisplayStart; n < clipper.DisplayEnd; ++n) {
            const Row& r = g.rows[static_cast<size_t>(n)];
            ImGui::PushID(n);
            const ImVec2 at = ImGui::GetCursorScreenPos();
            const float x = at.x + S(10) + indent * static_cast<float>(r.depth);
            const float text_y = at.y + (height - g_fonts.body->FontSize) * 0.5f;
            if (r.loading && !r.more) {
                ImGui::Dummy(ImVec2(width, height));
                draw->AddText(ImVec2(x + S(16), text_y), color::muted, r.label.c_str());
            } else if (r.more) {
                if (list_row("##more", width, height, false) && !r.loading) load_folder(r.path, static_cast<long long>(g.folders[r.path].assets.size()));
                const std::string label = r.loading ? "Loading..." : "Show " + grouped(r.count) + " more";
                draw->AddText(ImVec2(x + S(16), text_y), color::blue, label.c_str());
            } else if (r.folder) {
                auto& f = g.folders[r.path];
                if (list_row("##folder", width, height, false)) {
                    f.open = !f.open;
                    if (f.open && !f.loaded && !f.job) load_folder(r.path);
                }
                arrow(draw, ImVec2(x + S(5), at.y + height * 0.5f), f.open);
                const std::string count = grouped(r.count);
                const float count_w = g_fonts.caption->CalcTextSizeA(g_fonts.caption->FontSize, FLT_MAX, 0, count.c_str()).x;
                const ImVec4 clip(at.x, at.y, at.x + width - count_w - S(20), at.y + height);
                draw->AddText(g_fonts.bold, g_fonts.bold->FontSize, ImVec2(x + S(16), text_y - S(1)), color::text, r.label.c_str(),
                              nullptr, 0, &clip);
                draw->AddText(g_fonts.caption, g_fonts.caption->FontSize,
                              ImVec2(at.x + width - count_w - S(10), at.y + (height - g_fonts.caption->FontSize) * 0.5f), color::muted,
                              count.c_str());
            } else {
                const std::string kind = r.asset->value("kind", "ebx");
                const bool picked = r.path == g.pick.name && kind == g.pick.kind;
                if (list_row("##asset", width, height, picked)) select(app, r.path, kind);
                if (picked && g.scroll_to_pick && n == pick_row) {
                    ImGui::SetScrollHereY(0.4f);
                    g.scroll_to_pick = false;
                }
                if (ImGui::IsItemHovered() && !ImGui::IsItemActive())
                    ImGui::SetTooltip("%s\n%s", r.path.c_str(), r.asset->value("type", "").c_str());
                float left = x + S(16);
                if (kind != "ebx") {
                    kind_mark(draw, ImVec2(left, at.y + S(7)), kind);
                    left += badge_width(kind == "res" ? "RES" : "CHUNK") + S(6);
                }
                name_and_type(draw, g_fonts.body, ImVec2(left, text_y), at.x + width - S(10), r.label,
                              r.asset->value("type", ""), at.y + (height - g_fonts.caption->FontSize) * 0.5f,
                              picked ? color::text : rgba(205, 210, 218));
            }
            ImGui::PopID();
        }
}

void result_rows(App& app, float width) {
    const float height = S(46);
    auto* draw = ImGui::GetWindowDrawList();
    ImGuiListClipper clipper;
    clipper.Begin(static_cast<int>(g.results.size()), height);
    while (clipper.Step())
        for (int n = clipper.DisplayStart; n < clipper.DisplayEnd; ++n) {
            const Json& m = g.results[static_cast<size_t>(n)];
            const std::string name = m.value("name", ""), kind = m.value("kind", "ebx"), type = m.value("type", "");
            ImGui::PushID(n);
            const ImVec2 at = ImGui::GetCursorScreenPos();
            const bool picked = name == g.pick.name && kind == g.pick.kind;
            if (list_row("##match", width, height, picked)) select(app, name, kind);
            if (ImGui::IsItemHovered() && !ImGui::IsItemActive()) ImGui::SetTooltip("%s\n%s", name.c_str(), type.c_str());
            float left = at.x + S(14);
            if (kind != "ebx") {
                kind_mark(draw, ImVec2(left, at.y + S(7)), kind);
                left += badge_width(kind == "res" ? "RES" : "CHUNK") + S(6);
            }
            name_and_type(draw, g_fonts.bold, ImVec2(left, at.y + S(6)), at.x + width - S(10), last_part(name), type, at.y + S(9),
                          color::text);
            const ImVec4 clip2(at.x, at.y, at.x + width - S(10), at.y + height);
            draw->AddText(g_fonts.caption, g_fonts.caption->FontSize, ImVec2(at.x + S(14), at.y + S(27)), color::muted,
                          folder_part(name).c_str(), nullptr, 0, &clip2);
            ImGui::PopID();
        }
    if (static_cast<long long>(g.results.size()) < g.result_total) {
        ImGui::Dummy(ImVec2(0, S(4)));
        ImGui::BeginDisabled(running(g.search));
        if (ImGui::Button(running(g.search) ? "LOADING..." : ("SHOW " + grouped(std::min<long long>(300, g.result_total - static_cast<long long>(g.results.size()))) + " MORE").c_str()))
            start_search(true);
        ImGui::EndDisabled();
    }
}

void type_combo(float width) {
    ImGui::SetNextItemWidth(width);
    const std::string shown = g.type.empty() ? "All types" : g.type;
    if (!ImGui::BeginCombo("##type", shown.c_str(), ImGuiComboFlags_HeightLarge)) return;
    if (ImGui::IsWindowAppearing()) ImGui::SetKeyboardFocusHere();
    ImGui::SetNextItemWidth(-1);
    input_text("##type_filter", g.type_filter, 0, "Filter types");
    std::string filter = g.type_filter;
    for (auto& c : filter) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    if (ImGui::Selectable("All types", g.type.empty())) {
        g.type.clear();
        filters_changed();
    }
    for (const auto& [type, count] : g.types) {
        std::string low = type;
        for (auto& c : low) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
        if (!filter.empty() && low.find(filter) == std::string::npos) continue;
        const std::string label = type + "  (" + grouped(count) + ")";
        if (ImGui::Selectable(label.c_str(), g.type == type)) {
            g.type = type;
            filters_changed();
        }
    }
    ImGui::EndCombo();
}

void browser(App& app, ImVec2 size) {
    begin_panel("##assets_browser", 501, size);
    if (!g.ready) {
        ImGui::PushFont(g_fonts.heading);
        ImGui::TextUnformatted("ASSET INDEX");
        ImGui::PopFont();
        if (running(g.index)) {
            wrapped_muted("Reading the game's TOCs, bundles and EBX types. The first time takes a few seconds; after that it "
                          "loads from the cache.");
            ImGui::Spacing();
            job_progress(g.index, "Starting...");
            ImGui::Spacing();
            ImGui::BeginDisabled(g.index->cancel.load());
            if (ImGui::Button("CANCEL")) g.index->cancel = true;
            ImGui::EndDisabled();
        } else {
            wrapped_colour(g.index_error.empty() ? "The asset index is not loaded." : g.index_error, color::danger);
            ImGui::Spacing();
            if (primary_button("BUILD INDEX")) g.index = run("asset", "index", Json::object());
            if (g.index_error_code == "game_root_missing" || g.index_error_code == "not_a_game_root" ||
                g.index_error_code == "game_data_missing") {
                ImGui::SameLine();
                if (ImGui::Button("SETTINGS")) app.page = Page::settings;
            }
        }
        end_panel();
        return;
    }
    // Search and filters.
    ImGui::SetNextItemWidth(-1);
    if (input_text("##search", g.query, 0, "Search names, or *type (e.g. popsicle *texture)")) g.edited_at = ImGui::GetTime();
    if (ImGui::IsItemDeactivatedAfterEdit() && ImGui::IsKeyPressed(ImGuiKey_Enter)) g.edited_at = 0;
    const float gap = ImGui::GetStyle().ItemSpacing.x;
    const float third = (ImGui::GetContentRegionAvail().x - gap * 2) / 3;
    ImGui::SetNextItemWidth(third);
    if (ImGui::BeginCombo("##kind", kind_labels[g.kind])) {
        for (int i = 0; i < 4; ++i)
            if (ImGui::Selectable(kind_labels[i], g.kind == i) && g.kind != i) { g.kind = i; filters_changed(); }
        ImGui::EndCombo();
    }
    ImGui::SameLine();
    ImGui::SetNextItemWidth(third);
    if (ImGui::BeginCombo("##category", category_labels[g.category], ImGuiComboFlags_HeightLarge)) {
        for (int i = 0; i < 11; ++i)
            if (ImGui::Selectable(category_labels[i], g.category == i) && g.category != i) { g.category = i; filters_changed(); }
        ImGui::EndCombo();
    }
    ImGui::SameLine();
    type_combo(ImGui::GetContentRegionAvail().x);

    // What is listed.
    const bool searching = !g.query.empty();
    std::string count;
    if (searching) {
        count = running(g.search) && !g.appending ? "Searching..." : grouped(g.result_total) + (g.result_total == 1 ? " match" : " matches");
    } else {
        long long total = 0;
        auto top = g.folders.find("");
        if (top != g.folders.end()) {
            for (const auto& f : top->second.folders) total += f.value("count", 0ll);
            total += top->second.asset_total;
        }
        count = grouped(total) + " " + (g.kind == 1 ? "EBX assets" : g.kind == 2 ? "resources" : g.kind == 3 ? "chunks" : "assets");
    }
    ImGui::PushFont(g_fonts.caption);
    ImGui::TextDisabled("%s", count.c_str());
    if (!g.search_error.empty() && searching) {
        ImGui::SameLine();
        ImGui::TextColored(ImGui::ColorConvertU32ToFloat4(color::danger), "%s", g.search_error.c_str());
    }
    ImGui::PopFont();

    const float footer = ImGui::GetFrameHeight() + S(8);
    ImGui::BeginChild("##asset_rows", ImVec2(0, ImGui::GetContentRegionAvail().y - footer), 0);
    const float width = ImGui::GetContentRegionAvail().x;
    // Rows touch, like a list; the rule under each one separates them.
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(ImGui::GetStyle().ItemSpacing.x, 0));
    if (searching) result_rows(app, width);
    else tree_rows(app, width);
    ImGui::PopStyleVar();
    ImGui::EndChild();

    // The index itself.
    ImGui::Dummy(ImVec2(0, S(2)));
    const Json& r = g.index_result;
    ImGui::AlignTextToFramePadding();
    caption(grouped(r.value("assets", 0ll)) + " in the index, " + (r.value("from_cache", false) ? "loaded from the cache" : "built just now"));
    ImGui::SameLine();
    const float button = S(150);
    ImGui::SetCursorPosX(ImGui::GetCursorPosX() + std::max(0.0f, ImGui::GetContentRegionAvail().x - button));
    if (ImGui::Button("REBUILD INDEX", ImVec2(button, 0))) {
        g.ready = false;
        g.index = run("asset", "index", {{"refresh", true}});
    }
    if (ImGui::IsItemHovered()) ImGui::SetTooltip("Read the game's files again (asset index --refresh)");
    end_panel();
}

// ---------------------------------------------------------------- the inspector

void link(const std::string& label, const std::string& name, const std::string& kind, App& app) {
    ImGui::PushStyleColor(ImGuiCol_Text, color::blue);
    ImGui::TextUnformatted(label.c_str());
    ImGui::PopStyleColor();
    if (ImGui::IsItemHovered()) {
        ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
        ImGui::SetTooltip("Open %s (%s)", name.c_str(), kind.c_str());
    }
    if (ImGui::IsItemClicked()) {
        select(app, name, kind);
        g.scroll_to_pick = true;
    }
}

bool colour_like(const std::string& key, const Json& v) {
    if (!v.is_object() || !v.contains("x") || !v.contains("y") || !v.contains("z")) return false;
    std::string k = key + v.value("$type", "");
    for (auto& c : k) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    return k.find("color") != std::string::npos || k.find("colour") != std::string::npos || k.find("tint") != std::string::npos;
}

std::string scalar_text(const Json& v) {
    if (v.is_string()) return v.get<std::string>();
    if (v.is_null()) return "null";
    if (v.is_number_float()) {
        char text[40];
        std::snprintf(text, sizeof(text), "%.6g", v.get<double>());
        return text;
    }
    return v.dump();
}

// A value that can be changed in a draft: a boolean, number or string (enums arrive as their member name).
void editable_value(const std::string& path, const Json& v) {
    const auto edited = g.edits.find(path);
    const bool changed = edited != g.edits.end();
    const std::string shown = changed ? edited->second : v.is_boolean() ? (v.get<bool>() ? "true" : "false") : scalar_text(v);
    if (g.editing == path) {
        ImGui::SetNextItemWidth(-1);
        if (ImGui::IsWindowAppearing() || !ImGui::IsAnyItemActive()) ImGui::SetKeyboardFocusHere();
        if (input_text("##edit", g.edit_text, ImGuiInputTextFlags_EnterReturnsTrue | ImGuiInputTextFlags_AutoSelectAll)) {
            const std::string original = v.is_boolean() ? (v.get<bool>() ? "true" : "false") : scalar_text(v);
            if (g.edit_text == original) g.edits.erase(path);
            else g.edits[path] = g.edit_text;
            g.editing.clear();
        } else if (ImGui::IsKeyPressed(ImGuiKey_Escape) || (!ImGui::IsItemActive() && ImGui::IsMouseClicked(0) && !ImGui::IsItemHovered())) {
            g.editing.clear();
        }
        return;
    }
    ImU32 ink = color::text;
    if (changed) ink = color::warning;
    else if (v.is_boolean()) ink = v.get<bool>() ? color::good : color::muted;
    ImGui::PushStyleColor(ImGuiCol_Text, ink);
    ImGui::TextUnformatted(shown.c_str());
    ImGui::PopStyleColor();
    if (ImGui::IsItemHovered()) {
        ImGui::SetTooltip(changed ? "Changed in this draft (was %s). Double-click to change it again." : "%s\nDouble-click to change it",
                          changed ? scalar_text(v).c_str() : path.c_str());
        if (ImGui::IsMouseDoubleClicked(0)) {
            g.editing = path;
            g.edit_text = shown;
        }
    }
    if (ImGui::BeginPopupContextItem("##value")) {
        if (ImGui::MenuItem("Copy value")) copy_text(shown);
        if (ImGui::MenuItem("Change...")) {
            g.editing = path;
            g.edit_text = shown;
        }
        if (changed && ImGui::MenuItem("Undo the change")) g.edits.erase(path);
        ImGui::EndPopup();
    }
}

void property(App& app, const std::string& key, const Json& v, int id, const std::string& path) {
    ImGui::TableNextRow();
    ImGui::TableNextColumn();
    ImGui::PushID(id);
    const bool object = v.is_object() && !v.contains("$ref") && !v.contains("$import") && !v.contains("$resource") &&
                        !v.contains("$boxed") && !(v.contains("$type") && v.size() == 1);
    const bool array = v.is_array();
    if (object || array) {
        const bool empty = array ? v.empty() : v.size() <= 1;
        const bool open = ImGui::TreeNodeEx(key.c_str(), ImGuiTreeNodeFlags_SpanAllColumns |
                                            (empty ? ImGuiTreeNodeFlags_Leaf | ImGuiTreeNodeFlags_NoTreePushOnOpen : 0));
        ImGui::TableNextColumn();
        if (array) {
            caption(std::to_string(v.size()) + (v.size() == 1 ? " item" : " items"));
        } else if (colour_like(key, v)) {
            const ImVec4 c(static_cast<float>(number_of(v, "x")), static_cast<float>(number_of(v, "y")),
                           static_cast<float>(number_of(v, "z")), v.contains("w") ? static_cast<float>(number_of(v, "w")) : 1.0f);
            ImGui::ColorButton("##swatch", c, ImGuiColorEditFlags_AlphaPreviewHalf, ImVec2(S(34), ImGui::GetTextLineHeight()));
            ImGui::SameLine();
            caption(scalar_text(v["x"]) + ", " + scalar_text(v["y"]) + ", " + scalar_text(v["z"]) +
                    (v.contains("w") ? ", " + scalar_text(v["w"]) : ""));
        } else {
            caption(v.value("$type", ""));
        }
        if (open && !empty) {
            int child = 0;
            if (array) {
                for (const auto& item : v) {
                    if (item.is_object() && item.contains("$more")) {
                        ImGui::TableNextRow();
                        ImGui::TableNextColumn();
                        ImGui::TextDisabled("...");
                        ImGui::TableNextColumn();
                        caption(grouped(item.value("$more", 0ll)) + " more (ebx get --max-array)");
                        continue;
                    }
                    property(app, "[" + std::to_string(child) + "]", item, child, path + "[" + std::to_string(child) + "]");
                    ++child;
                }
            } else {
                for (const auto& [k, item] : v.items()) {
                    if (k == "$type") continue;
                    property(app, k, item, child++, path + "." + k);
                }
            }
            ImGui::TreePop();
        }
    } else {
        ImGui::TreeNodeEx(key.c_str(), ImGuiTreeNodeFlags_Leaf | ImGuiTreeNodeFlags_NoTreePushOnOpen | ImGuiTreeNodeFlags_SpanAllColumns);
        ImGui::TableNextColumn();
        if (v.is_object() && v.contains("$ref")) {
            ImGui::TextDisabled("-> instance [%lld] %s", v.value("$ref", 0ll), v.value("type", "").c_str());
        } else if (v.is_object() && v.contains("$import")) {
            if (v.contains("asset")) link(v.value("asset", ""), v.value("asset", ""), "ebx", app);
            else ImGui::TextDisabled("import %lld (%s)", v.value("$import", 0ll), v.value("file_guid", "").c_str());
        } else if (v.is_object() && v.contains("$resource")) {
            if (v.contains("name")) link(v.value("name", "") + "  (res)", v.value("name", ""), "res", app);
            else ImGui::TextDisabled("resource %s", v.value("$resource", "").c_str());
        } else if (v.is_object() && v.contains("$type")) {
            ImGui::TextDisabled("type %s", v.value("$type", "").c_str());
        } else if (v.is_object() && v.contains("$boxed")) {
            ImGui::TextDisabled("boxed value");
        } else if (v.is_null()) {
            ImGui::TextDisabled("null");
        } else {
            editable_value(path, v);
        }
    }
    ImGui::PopID();
}

// The draft: what has been changed, saved as an edited .ebx with ebx set. The game is never written.
void draft_bar(App& app) {
    if (g.edits.empty() && g.draft_message.empty() && !running(g.draft_job)) {
        caption("Double-click a value to change it in a draft.");
        return;
    }
    if (!g.edits.empty()) {
        ImGui::AlignTextToFramePadding();
        ImGui::PushStyleColor(ImGuiCol_Text, color::warning);
        ImGui::Text("%zu %s in this draft", g.edits.size(), g.edits.size() == 1 ? "change" : "changes");
        ImGui::PopStyleColor();
        ImGui::SameLine();
        ImGui::BeginDisabled(running(g.draft_job));
        if (primary_button("SAVE AS .EBX")) {
            const auto path = pick_save(app.window, utf8_to_wide(last_part(g.pick.name)) + L".ebx", L"EBX asset", L"*.ebx", L"ebx");
            if (!path.empty()) {
                Json sets = Json::array();
                for (const auto& [field, value] : g.edits) sets.push_back(field + "=" + value);
                g.draft_job = run("ebx", "set", {{"name", g.pick.name}, {"set", sets}, {"output", wide_to_utf8(path)}});
                g.draft_message.clear();
            }
        }
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("Write this asset with the changes to a .ebx file (ebx set)");
        ImGui::SameLine();
        if (ImGui::Button("DISCARD")) {
            g.edits.clear();
            g.draft_message.clear();
        }
        ImGui::EndDisabled();
    }
    if (running(g.draft_job)) {
        ImGui::TextDisabled("Saving...");
    } else if (!g.draft_message.empty()) {
        wrapped_colour(g.draft_message, g.draft_ok ? color::good : color::danger);
    }
}

void properties_view(App& app) {
    if (running(g.ebx_job)) {
        job_progress(g.ebx_job, "Reading the EBX...");
        return;
    }
    if (!g.ebx_error.empty()) {
        wrapped_colour(g.ebx_error, color::danger);
        return;
    }
    if (g.ebx.is_null()) return;
    const auto& e = g.ebx;
    caption(e.value("root_type", "") + "  |  " + grouped(e.value("instance_count", 0ll)) + " objects  |  " +
            size_text(number_of(e, "bytes")) + "  |  " + e.value("guid", ""));
    if (e.contains("instances_truncated"))
        caption("Showing the first " + std::to_string(e["instances"].size()) + "; ebx get --limit shows more.");
    draft_bar(app);
    ImGui::BeginChild("##props", ImVec2(0, 0), 0);
    const auto& instances = e["instances"];
    for (size_t i = 0; i < instances.size(); ++i) {
        const auto& inst = instances[i];
        ImGui::PushID(static_cast<int>(i));
        const std::string title = "[" + std::to_string(inst.value("index", 0)) + "]  " + inst.value("type", "") +
                                  (inst.value("exported", false) ? "" : "   (internal)");
        ImGui::PushFont(g_fonts.bold);
        const bool open = ImGui::CollapsingHeader(title.c_str(), i == 0 ? ImGuiTreeNodeFlags_DefaultOpen : 0);
        ImGui::PopFont();
        if (open) {
            if (inst.contains("guid")) caption("guid " + inst.value("guid", ""));
            if (ImGui::BeginTable("##fields", 2, ImGuiTableFlags_RowBg | ImGuiTableFlags_BordersInnerH | ImGuiTableFlags_Resizable |
                                                     ImGuiTableFlags_SizingStretchProp)) {
                ImGui::TableSetupColumn("Field", ImGuiTableColumnFlags_WidthStretch, 0.42f);
                ImGui::TableSetupColumn("Value", ImGuiTableColumnFlags_WidthStretch, 0.58f);
                int n = 0;
                const std::string base = "[" + std::to_string(inst.value("index", 0)) + "]";
                for (const auto& [k, v] : inst["fields"].items()) property(app, k, v, n++, base + "." + k);
                ImGui::EndTable();
            }
        }
        ImGui::PopID();
    }
    const auto& imports = e["imports"];
    if (!imports.empty()) {
        ImGui::PushFont(g_fonts.bold);
        const bool open = ImGui::CollapsingHeader(("Imports (" + std::to_string(imports.size()) + ")").c_str());
        ImGui::PopFont();
        if (open)
            for (const auto& imp : imports) {
                ImGui::PushID(imp.value("index", 0));
                if (imp.contains("asset")) {
                    link(imp.value("asset", ""), imp.value("asset", ""), "ebx", app);
                    ImGui::SameLine();
                    caption(imp.value("type", ""));
                } else {
                    ImGui::TextDisabled("%s (not in the index)", imp.value("file_guid", "").c_str());
                }
                ImGui::PopID();
            }
    }
    ImGui::EndChild();
}

void texture_view() {
    if (running(g.texture_job)) {
        job_progress(g.texture_job, "Decoding the texture...");
        return;
    }
    if (!g.texture_error.empty()) {
        wrapped_colour(g.texture_error, color::danger);
        if (g.mip >= 0 && ImGui::Button("LARGEST MIP THAT FITS")) {
            g.mip = -1;
            g.texture_error.clear();
            start_view(Tab::texture);
        }
        return;
    }
    if (g.texture.is_null()) return;
    const auto& t = g.texture;
    const int mips = t.value("mips", 1);
    const int shown_mip = t.value("mip", 0);
    caption(std::to_string(t.value("width", 0)) + " x " + std::to_string(t.value("height", 0)) + "  |  " + t.value("format", "") +
            "  |  " + std::to_string(mips) + (mips == 1 ? " mip" : " mips") + "  |  " + t.value("texture_type", "") +
            (t.value("slices", 1) > 1 ? " (" + std::to_string(t.value("slices", 1)) + " slices)" : ""));
    ImGui::SetNextItemWidth(S(220));
    const std::string mip_label = "Mip " + std::to_string(shown_mip) + "  (" + std::to_string(t.value("mip_width", 0)) + " x " +
                                  std::to_string(t.value("mip_height", 0)) + ")";
    if (ImGui::BeginCombo("##mip", mip_label.c_str())) {
        for (int m = 0; m < mips; ++m) {
            const int w = std::max(1, t.value("width", 1) >> m), h = std::max(1, t.value("height", 1) >> m);
            if (ImGui::Selectable(("Mip " + std::to_string(m) + "  (" + std::to_string(w) + " x " + std::to_string(h) + ")").c_str(),
                                  m == shown_mip) && m != shown_mip) {
                g.mip = m;
                g.texture = Json();
                start_view(Tab::texture);
            }
        }
        ImGui::EndCombo();
    }
    if (!g.texture.is_null() && g.preview) {
        const ImVec2 avail = ImGui::GetContentRegionAvail();
        const float scale = std::min({avail.x / static_cast<float>(g.preview_width), (avail.y - S(6)) / static_cast<float>(g.preview_height),
                                      std::max(1.0f, g_scale)});
        const ImVec2 size(static_cast<float>(g.preview_width) * scale, static_cast<float>(g.preview_height) * scale);
        const ImVec2 at = ImGui::GetCursorScreenPos();
        auto* draw = ImGui::GetWindowDrawList();
        // A checkerboard under it, so transparency reads as transparency.
        const float cell = S(12);
        for (float y = 0; y < size.y; y += cell)
            for (float x = 0; x < size.x; x += cell) {
                const bool dark = (static_cast<int>(x / cell) + static_cast<int>(y / cell)) % 2 == 0;
                draw->AddRectFilled(ImVec2(at.x + x, at.y + y), ImVec2(at.x + std::min(size.x, x + cell), at.y + std::min(size.y, y + cell)),
                                    dark ? rgba(44, 44, 48) : rgba(62, 62, 66));
            }
        ImGui::Image(g.preview, size);
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("Mip %d shown at %.0f%%", shown_mip, scale * 100);
    }
}

void lua_view() {
    if (running(g.lua_job)) {
        job_progress(g.lua_job, "Reading the script...");
        return;
    }
    if (!g.lua_error.empty()) {
        wrapped_colour(g.lua_error, color::danger);
        return;
    }
    if (g.lua.is_null()) return;
    caption(g.lua.value("file", "") + "  |  " + grouped(g.lua.value("lines", 0ll)) + " lines  |  " + size_text(number_of(g.lua, "bytes")));
    ImGui::PushStyleColor(ImGuiCol_FrameBg, ImGui::ColorConvertU32ToFloat4(rgba(18, 19, 22)));
    ImGui::InputTextMultiline("##lua", g.lua_text.data(), g.lua_text.size() + 1, ImVec2(-1, -1), ImGuiInputTextFlags_ReadOnly);
    ImGui::PopStyleColor();
}

void info_row(const char* name, const std::string& value) {
    if (value.empty()) return;
    ImGui::TableNextRow();
    ImGui::TableNextColumn();
    ImGui::PushFont(g_fonts.caption);
    ImGui::TextDisabled("%s", name);
    ImGui::PopFont();
    ImGui::TableNextColumn();
    ImGui::PushTextWrapPos(0);
    ImGui::TextUnformatted(value.c_str());
    ImGui::PopTextWrapPos();
    if (ImGui::BeginPopupContextItem(name)) {
        if (ImGui::MenuItem("Copy")) copy_text(value);
        ImGui::EndPopup();
    }
}

void info_view(App& app) {
    const auto& i = g.info;
    ImGui::BeginChild("##info", ImVec2(0, 0), 0);
    if (ImGui::BeginTable("##info_rows", 2, ImGuiTableFlags_SizingStretchProp | ImGuiTableFlags_BordersInnerH)) {
        ImGui::TableSetupColumn("name", ImGuiTableColumnFlags_WidthFixed, S(130));
        ImGui::TableSetupColumn("value", ImGuiTableColumnFlags_WidthStretch);
        info_row("NAME", i.value("name", ""));
        info_row("KIND", i.value("kind", ""));
        info_row("TYPE", i.value("type", ""));
        info_row("GROUP", i.value("category", ""));
        info_row("SIZE", size_text(number_of(i, "size")) + "  (" + grouped(static_cast<long long>(number_of(i, "size"))) + " bytes)");
        info_row("STORED", size_text(number_of(i, "stored")) + " in the archive");
        info_row("FIRST BUNDLE", i.value("bundle", ""));
        info_row("SUPERBUNDLE", i.value("superbundle", ""));
        info_row("BUNDLES", grouped(i.value("bundle_count", 0ll)));
        info_row("LOCATION", i.value("location", ""));
        info_row("GUID", i.value("guid", ""));
        info_row("RESOURCE TYPE", i.value("resource_type", ""));
        info_row("RESOURCE ID", i.value("resource_id", ""));
        info_row("RESOURCE META", i.value("resource_meta", ""));
        info_row("NOTE", i.value("type_error", ""));
        ImGui::EndTable();
    }
    const auto& related = i.value("related", Json::array());
    if (!related.empty()) {
        ImGui::Spacing();
        ImGui::PushFont(g_fonts.bold);
        ImGui::TextUnformatted("SAME NAME, OTHER KIND");
        ImGui::PopFont();
        for (const auto& r : related) {
            ImGui::PushID(r.value("kind", "").c_str());
            link(r.value("kind", "") + "  " + r.value("type", ""), i.value("name", ""), r.value("kind", ""), app);
            ImGui::PopID();
        }
    }
    ImGui::EndChild();
}

void export_buttons(App& app) {
    const std::string name = g.pick.name;
    const std::wstring base = utf8_to_wide(last_part(name));
    auto start = [&](const char* group, const char* command, Json args) {
        g.export_message.clear();
        g.export_job = run(group, command, std::move(args));
    };
    ImGui::BeginDisabled(running(g.export_job));
    if (g.tab == Tab::texture) {
        if (ImGui::Button("EXPORT PNG")) {
            const auto path = pick_save(app.window, base + L".png", L"PNG image", L"*.png", L"png");
            if (!path.empty()) {
                Json a = {{"name", name}, {"kind", g.pick.kind}, {"output", wide_to_utf8(path)}, {"format", "png"}};
                if (g.mip >= 0) a["mip"] = g.mip;
                start("texture", "export", a);
            }
        }
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("The mip shown, decoded to RGBA (texture export)");
        ImGui::SameLine();
        if (ImGui::Button("EXPORT DDS")) {
            const auto path = pick_save(app.window, base + L".dds", L"DirectDraw Surface", L"*.dds", L"dds");
            if (!path.empty())
                start("texture", "export", {{"name", name}, {"kind", g.pick.kind}, {"output", wide_to_utf8(path)}, {"format", "dds"}});
        }
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("Every mip in its own format (texture export)");
        ImGui::SameLine();
    }
    if (g.tab == Tab::lua) {
        if (ImGui::Button("SAVE .LUA")) {
            const auto path = pick_save(app.window, base + L".lua", L"Lua source", L"*.lua", L"lua");
            if (!path.empty()) start("asset", "lua-source", {{"name", name}, {"kind", g.pick.kind}, {"output", wide_to_utf8(path)}});
        }
        ImGui::SameLine();
    }
    const std::string ext = g.pick.kind == "ebx" ? "ebx" : g.pick.kind == "res" ? "res" : "chunk";
    if (ImGui::Button("EXPORT RAW")) {
        const auto path = pick_save(app.window, base + L"." + utf8_to_wide(ext), L"Raw payload", (L"*." + utf8_to_wide(ext)).c_str(),
                                    utf8_to_wide(ext).c_str());
        if (!path.empty()) start("asset", "export-raw", {{"name", name}, {"kind", g.pick.kind}, {"output", wide_to_utf8(path)}});
    }
    if (ImGui::IsItemHovered()) ImGui::SetTooltip("The payload as the game reads it (asset export-raw)");
    ImGui::EndDisabled();
    if (running(g.export_job)) {
        ImGui::SameLine();
        ImGui::AlignTextToFramePadding();
        ImGui::TextDisabled("Exporting...");
    } else if (!g.export_message.empty()) {
        ImGui::SameLine();
        ImGui::AlignTextToFramePadding();
        ImGui::PushStyleColor(ImGuiCol_Text, g.export_ok ? color::good : color::danger);
        ImGui::TextUnformatted(g.export_ok ? "Saved" : "Failed");
        ImGui::PopStyleColor();
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", g.export_message.c_str());
        if (g.export_ok) {
            ImGui::SameLine();
            if (ImGui::SmallButton("SHOW")) {
                const std::wstring arg = L"/select,\"" + utf8_to_wide(g.export_path) + L"\"";
                ShellExecuteW(nullptr, L"open", L"explorer.exe", arg.c_str(), nullptr, SW_SHOWNORMAL);
            }
        } else {
            wrapped_colour(g.export_message, color::danger);
        }
    }
}

void inspector(App& app, ImVec2 size) {
    begin_panel("##assets_inspector", 502, size);
    if (g.pick.name.empty()) {
        ImGui::PushFont(g_fonts.heading);
        ImGui::TextUnformatted("INSPECTOR");
        ImGui::PopFont();
        wrapped_muted("Pick an asset on the left to look inside it: EBX properties, texture previews and Lua source, with "
                      "exports to PNG, DDS, .lua and the raw payload.");
        if (g.ready) {
            const Json& r = g.index_result;
            ImGui::Spacing();
            field("EBX ASSETS", grouped(r.value("ebx", 0ll)));
            field("RESOURCES", grouped(r.value("res", 0ll)));
            field("CHUNKS", grouped(r.value("chunks", 0ll)));
            field("BUNDLES", grouped(r.value("bundles", 0ll)) + " in " + grouped(r.value("superbundles", 0ll)) + " superbundles");
            field("CACHE", r.value("cache", ""));
        }
        end_panel();
        return;
    }
    // Title: the last part of the name, the rest under it.
    auto* draw = ImGui::GetWindowDrawList();
    if (!g.back.empty()) {
        if (ImGui::Button("<", ImVec2(S(32), 0))) {
            const Pick to = g.back.back();
            g.back.pop_back();
            select(app, to.name, to.kind, false);
            g.scroll_to_pick = true;
        }
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("Back to %s", g.back.back().name.c_str());
        ImGui::SameLine();
    }
    ImGui::PushFont(g_fonts.heading);
    const ImVec2 title_at = ImGui::GetCursorScreenPos();
    const float title_room = ImGui::GetContentRegionAvail().x;
    const ImVec4 clip(title_at.x, title_at.y, title_at.x + title_room, title_at.y + S(40));
    draw->AddText(g_fonts.heading, g_fonts.heading->FontSize, title_at, color::text, upper(last_part(g.pick.name)).c_str(), nullptr, 0, &clip);
    ImGui::Dummy(ImVec2(title_room, g_fonts.heading->FontSize));
    ImGui::PopFont();
    ImGui::PushFont(g_fonts.caption);
    ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + ImGui::GetContentRegionAvail().x - S(60));
    ImGui::TextDisabled("%s", g.pick.name.c_str());
    ImGui::PopTextWrapPos();
    ImGui::PopFont();
    ImGui::SameLine();
    copy_button("COPY##name", g.pick.name);

    const Json& i = g.info;
    if (running(g.info_job) || i.is_null()) {
        ImGui::TextDisabled("Looking it up...");
        end_panel();
        return;
    }
    if (i.contains("error")) {
        wrapped_colour(i.value("error", ""), color::danger);
        end_panel();
        return;
    }
    {
        const ImVec2 at = ImGui::GetCursorScreenPos();
        float x = at.x;
        const std::string kind = upper(i.value("kind", ""));
        badge(draw, ImVec2(x, at.y), kind, color::blue, color::ink);
        x += badge_width(kind) + S(6);
        const std::string type = i.value("type", "");
        badge(draw, ImVec2(x, at.y), type, color::tile_grey, color::text);
        x += badge_width(type) + S(10);
        const std::string facts = size_text(number_of(i, "size")) + "  |  in " + grouped(i.value("bundle_count", 0ll)) +
                                  (i.value("bundle_count", 0ll) == 1 ? " bundle" : " bundles");
        draw->AddText(g_fonts.caption, g_fonts.caption->FontSize, ImVec2(x, at.y + S(3)), color::muted, facts.c_str());
        ImGui::Dummy(ImVec2(0, g_fonts.caption->FontSize + S(8)));
    }

    // Tabs: only the views this asset has, then INFO.
    for (int t = 0; t < 4; ++t) {
        if (t != 3 && !has_view(tab_views[t])) continue;
        const bool on = static_cast<int>(g.tab) == t;
        if (on ? primary_button(tab_labels[t]) : ImGui::Button(tab_labels[t])) start_view(static_cast<Tab>(t));
        ImGui::SameLine();
    }
    ImGui::NewLine();
    export_buttons(app);
    ImGui::Dummy(ImVec2(0, S(2)));
    ImGui::BeginChild("##view", ImVec2(0, 0), 0, g.tab == Tab::lua || g.tab == Tab::properties ? ImGuiWindowFlags_NoScrollbar : 0);
    if (g.tab == Tab::properties) properties_view(app);
    else if (g.tab == Tab::texture) texture_view();
    else if (g.tab == Tab::lua) lua_view();
    else info_view(app);
    ImGui::EndChild();
    end_panel();
}

} // namespace

void assets_startup(const Json& args) { g.startup = args.is_object() ? args : Json::object(); }

void assets_page(App& app) {
    if (!g.started) {
        g.started = true;
        g.index = run("asset", "index", Json::object());
    }
    poll(app);

    heading("ASSETS");
    wrapped_muted("Everything in the game's data. Search by name or type, open folders, look inside. Nothing here changes the game.");
    ImGui::Spacing();
    const ImVec2 avail = ImGui::GetContentRegionAvail();
    // The frame adds a little space under every page; leave room for it so the page itself never scrolls.
    const float height = std::max(S(300), avail.y - S(8) - ImGui::GetStyle().ItemSpacing.y * 2);
    const float gap = S(14);
    const float left = std::clamp(avail.x * 0.42f, S(340), S(560));
    browser(app, ImVec2(left, height));
    ImGui::SameLine(0, gap);
    inspector(app, ImVec2(avail.x - left - gap, height));
}

} // namespace studio::gui
