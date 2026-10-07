#pragma once
#include "core/registry.h"

namespace studio {
// One function per file in src/commands. register_all_commands() in all.cpp calls each of them.
void register_studio_commands(Registry& r);
void register_asset_commands(Registry& r);     // asset_commands.cpp
void register_map_commands(Registry& r);       // map_commands.cpp
void register_mod_commands(Registry& r);       // mod_commands.cpp
void register_cosmetic_commands(Registry& r);  // cosmetic_commands.cpp
void register_media_commands(Registry& r);     // media_commands.cpp
void register_browse_commands(Registry& r);    // browse_commands.cpp
}
