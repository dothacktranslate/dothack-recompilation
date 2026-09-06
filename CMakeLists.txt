cmake_minimum_required(VERSION 3.21)

project(
    dothack_infection_recomp
    VERSION 0.1.0
    LANGUAGES C CXX
)

set(CMAKE_C_STANDARD 17)
set(CMAKE_C_STANDARD_REQUIRED ON)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

set(CMAKE_EXPORT_COMPILE_COMMANDS ON)


# ============================================================
# Linux/x86 compiler requirements
# ============================================================

# PS2Recomp's runtime uses SSE4.1 intrinsics.
if(
    CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang"
    AND CMAKE_SYSTEM_PROCESSOR MATCHES "x86_64|amd64|AMD64"
)
    add_compile_options(-msse4.1)
endif()


# ============================================================
# PS2Recomp configuration
# ============================================================

# Our normal application build only needs the runtime.
# The analyzer/recompiler have their own separate developer build.
set(PS2X_BUILD_RECOMP OFF CACHE BOOL "" FORCE)
set(PS2X_BUILD_ANALYZER OFF CACHE BOOL "" FORCE)
set(PS2X_BUILD_TEST OFF CACHE BOOL "" FORCE)
set(PS2X_BUILD_STUDIO OFF CACHE BOOL "" FORCE)
set(PS2X_BUILD_RUNTIME ON CACHE BOOL "" FORCE)

# Useful during early game bring-up.
set(PS2X_ENABLE_RUNTIME_LOGS ON CACHE BOOL "" FORCE)
set(PS2X_ENABLE_AGRESSIVE_LOGS OFF CACHE BOOL "" FORCE)

# Generated recompilation code should be compiled as individual
# translation units rather than CMake unity batches.
set(PS2X_ENABLE_RUNNER_UNITY_BUILD OFF CACHE BOOL "" FORCE)

add_subdirectory(extern/PS2Recomp)

# PS2Recomp ships a placeholder register_functions.cpp.
# Our generated recompilation provides the real implementation,
# so disable compilation of the placeholder.
set_source_files_properties(
    "${CMAKE_CURRENT_SOURCE_DIR}/extern/PS2Recomp/ps2xRuntime/src/runner/register_functions.cpp"
    TARGET_DIRECTORY ps2EntryRunner
    PROPERTIES HEADER_FILE_ONLY ON
)

if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
    target_compile_options(
        ps2EntryRunner
        PRIVATE
        "-include${CMAKE_CURRENT_SOURCE_DIR}/include/dothack/ps2recomp_gcc_compat.h"
    )
endif()

# ============================================================
# .hack//INFECTION generated recompilation
# ============================================================

set(
    DOTHACK_GENERATED_DIR
    "${CMAKE_CURRENT_SOURCE_DIR}/generated/SLUS_202.67"
)

if(NOT EXISTS "${DOTHACK_GENERATED_DIR}/register_functions.cpp")
    message(
        FATAL_ERROR
        "Generated .hack//INFECTION code was not found.\n"
        "Run ps2_recomp with config/recomp/SLUS_202.67.toml first."
    )
endif()

file(
    GLOB_RECURSE DOTHACK_RECOMPILED_SOURCES
    CONFIGURE_DEPENDS
    "${DOTHACK_GENERATED_DIR}/*.cpp"
)

target_sources(
    ps2EntryRunner
    PRIVATE
    ${DOTHACK_RECOMPILED_SOURCES}
)

target_include_directories(
    ps2EntryRunner
    PRIVATE
    "${DOTHACK_GENERATED_DIR}"
)


# ============================================================
# Project-specific code
# ============================================================

file(
    GLOB_RECURSE DOTHACK_PROJECT_SOURCES
    CONFIGURE_DEPENDS
    "${CMAKE_CURRENT_SOURCE_DIR}/src/*.cpp"
)

target_sources(
    ps2EntryRunner
    PRIVATE
    ${DOTHACK_PROJECT_SOURCES}
)

target_include_directories(
    ps2EntryRunner
    PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/include"
)


# ============================================================
# Output
# ============================================================

set_target_properties(
    ps2EntryRunner
    PROPERTIES
    OUTPUT_NAME "dothack-infection-recomp"
)

message(STATUS "Configuring .hack//INFECTION Recompilation")
