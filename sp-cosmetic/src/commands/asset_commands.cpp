// Game asset search and EBX inspection (reskate_cli: assets, read, ebx, find-name, find-type,
// ebx-values, ebx-roundtrip, ebx-author, schema, cas, toc-stock, donors).
#include "commands/commands.h"
#include "core/engine.h"
#include "core/settings.h"

namespace studio {

void register_asset_commands(Registry& r) {
    r.add({
        .group = "asset", .name = "find",
        .summary = "Search game assets by name",
        .description = "Substring search over every EBX asset and resource name in the game. Names in the index are "
                       "lowercase, so the search text is lowercased for you. Returns each match with its kind "
                       "(ebx or res).",
        .params = {
            {"text", ParamType::String, "Part of the asset name, e.g. baker_popsicle", true, true},
            {"limit", ParamType::Integer, "Most matches to return", false, false, {}, Json(100)},
            game_root_param(),
        },
        .examples = {"asset find baker_popsicle", "asset find deckgraphic --limit 10 --json"},
        .run = [](Context& c, const Json& a) -> Json {
            std::string text = a["text"];
            for (auto& ch : text) ch = static_cast<char>(std::tolower(static_cast<unsigned char>(ch)));
            long long limit = a["limit"];
            if (limit < 1) throw Error("invalid_arguments", "--limit must be at least 1", {{"param", "limit"}});
            EngineRun run = run_engine(c, {"find-name", path_utf8(c.game_root(a)), text, std::to_string(limit)});
            require_success(run);
            Json matches = Json::array();
            long long count = 0;
            for (const auto& line : run.out_lines) {
                if (line.rfind("matches=", 0) == 0) { count = std::stoll(line.substr(8)); continue; }
                auto space = line.find(' ');
                if (space == std::string::npos) continue;
                matches.push_back({{"kind", line.substr(0, space)}, {"name", line.substr(space + 1)}});
            }
            return {{"count", count}, {"matches", matches}};
        },
    });
}
}
