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
# C++ only: the GUI target also compiles a .rc, and rc.exe does not take these.
target_compile_options(studio_core PUBLIC $<$<COMPILE_LANGUAGE:CXX>:/W4 /WX /utf-8 /permissive- /EHsc /Zc:__cplusplus /external:W0>)
target_compile_definitions(studio_core PUBLIC UNICODE _UNICODE NOMINMAX WIN32_LEAN_AND_MEAN)

add_executable(studio-plus-cli src/cli/main.cpp)
target_link_libraries(studio-plus-cli PRIVATE studio_core)
target_link_options(studio-plus-cli PRIVATE /ENTRY:wmainCRTStartup)
set_target_properties(studio-plus-cli PROPERTIES OUTPUT_NAME studio-plus)

# ---------------------------------------------------------------- the window: ReSkate Studio+.exe
enable_language(RC)

# Dear ImGui with ReSkate's patched DX12 backend. Third-party, so it builds outside /W4 /WX.
add_library(studio_imgui STATIC
    third_party/imgui/imgui.cpp
    third_party/imgui/imgui_draw.cpp
    third_party/imgui/imgui_tables.cpp
    third_party/imgui/imgui_widgets.cpp
    third_party/imgui/backends/imgui_impl_dx12.cpp
    third_party/imgui/backends/imgui_impl_win32.cpp
)
target_include_directories(studio_imgui SYSTEM PUBLIC third_party/imgui)
target_compile_definitions(studio_imgui PUBLIC UNICODE _UNICODE NOMINMAX WIN32_LEAN_AND_MEAN)
target_link_libraries(studio_imgui PUBLIC d3d12 dxgi d3dcompiler dwmapi)

set(STUDIO_ICON ${CMAKE_SOURCE_DIR}/assets/studio-plus.ico)
set(STUDIO_FONTS ${CMAKE_SOURCE_DIR}/third_party/fonts)
configure_file(src/gui/resources.rc.in ${CMAKE_BINARY_DIR}/generated/studio_gui.rc @ONLY)

add_executable(studio-plus-gui WIN32
    src/gui/main.cpp
    src/gui/app.cpp
    src/gui/pages.cpp
    src/gui/runner.cpp
    src/gui/jobs.cpp
    src/gui/widgets.cpp
    src/gui/look.cpp
    src/gui/renderer.cpp
    ${CMAKE_BINARY_DIR}/generated/studio_gui.rc
)
target_link_libraries(studio-plus-gui PRIVATE studio_core studio_imgui windowscodecs uuid)
set_target_properties(studio-plus-gui PROPERTIES OUTPUT_NAME "ReSkate Studio+")
# The .rc embeds these, so a changed font or icon rebuilds it.
set_source_files_properties(${CMAKE_BINARY_DIR}/generated/studio_gui.rc PROPERTIES OBJECT_DEPENDS
    "${STUDIO_ICON};${STUDIO_FONTS}/Montserrat-SemiBold.ttf;${STUDIO_FONTS}/Montserrat-ExtraBold.ttf;${STUDIO_FONTS}/PermanentMarker-Regular.ttf")
