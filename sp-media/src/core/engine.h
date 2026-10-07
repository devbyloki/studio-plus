#pragma once
// Calls into reskate_cli.exe, the engine shipped in the ReSkate Studio zip.
#include "core/process.h"
#include "core/registry.h"
#include <functional>
#include <string>
#include <vector>

namespace studio {
struct EngineRun {
    int exit_code = -1;
    std::string out;                     // stdout as text
    std::vector<std::string> out_lines;  // stdout split into non-empty lines
    std::vector<std::string> err_lines;  // stderr split into non-empty lines
    double seconds = 0;
};

using LineHandler = std::function<void(Context&, std::string_view line)>;

// Runs reskate_cli with `args`. stderr lines go to `on_err_line` (default: forwarded to
// context.progress as unknown-fraction messages, "warning:" lines to context.log).
// Throws Error("cancelled") if the context was cancelled.
EngineRun run_engine(Context& context, const std::vector<std::string>& args,
                     LineHandler on_err_line = nullptr, LineHandler on_out_line = nullptr);

// Throws Error(code) with the most useful stderr line when exit_code != 0.
void require_success(const EngineRun& run, const std::string& code = "engine_failed");

// key=value lines become object fields (numbers and true/false converted). Lines that are not
// key=value are collected under "lines" when keep_other_lines is true.
Json parse_key_values(const std::vector<std::string>& lines, bool keep_other_lines = true);

// Converts a JSON value to a number when it looks like one, else returns it unchanged.
Json typed_value(std::string_view text);

// Adds "--flag value" or "--flag" pairs for optional args present in `args`.
void add_option(std::vector<std::string>& out, const Json& args, const std::string& param, const std::string& flag);
void add_flag(std::vector<std::string>& out, const Json& args, const std::string& param, const std::string& flag);
std::string arg_string(const Json& args, const std::string& param);  // "" if absent
}
