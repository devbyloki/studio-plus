cmake_minimum_required(VERSION 3.24)
project(ReSkateStudioPlus VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")

configure_file(src/version.h.in ${CMAKE_BINARY_DIR}/generated/version.h @ONLY)

# Third-party headers are included as system headers so /W4 /WX applies to our code only.
add_library(studio_third_party INTERFACE)
target_include_directories(studio_third_party SYSTEM INTERFACE third_party)

add_library(studio_core STATIC
    src/core/registry.cpp
    src/core/settings.cpp
    src/core/process.cpp
    src/core/engine.cpp
    src/commands/all.cpp
    src/commands/studio.cpp
    src/commands/asset_commands.cpp
    src/commands/map_commands.cpp
    src/commands/mod_commands.cpp
    src/commands/cosmetic_commands.cpp
    src/commands/media_commands.cpp
    src/mcp/server.cpp
)
target_include_directories(studio_core PUBLIC src ${CMAKE_BINARY_DIR}/generated)
target_link_libraries(studio_core PUBLIC studio_third_party shell32 advapi32 ole32)
target_compile_options(studio_core PUBLIC /W4 /WX /utf-8 /permissive- /EHsc /Zc:__cplusplus /external:W0)
target_compile_definitions(studio_core PUBLIC UNICODE _UNICODE NOMINMAX WIN32_LEAN_AND_MEAN)

add_executable(studio-plus-cli src/cli/main.cpp)
target_link_libraries(studio-plus-cli PRIVATE studio_core)
target_link_options(studio-plus-cli PRIVATE /ENTRY:wmainCRTStartup)
set_target_properties(studio-plus-cli PROPERTIES OUTPUT_NAME studio-plus)
