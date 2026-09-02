cmake_minimum_required(VERSION 3.24)

project(ut_clang_extract LANGUAGES CXX)

if(NOT LLVM_DIR)
  message(FATAL_ERROR "Pass -DLLVM_DIR=<LLVM prefix>/lib/cmake/llvm")
endif()

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

find_package(LLVM CONFIG REQUIRED)

# The prebuilt Windows LLVM archive records the DIA SDK path from its build
# machine.  Resolve that imported link item to the installed VS Build Tools
# copy so this project remains relocatable across supported Windows hosts.
find_file(UT_AGENT_DIA_GUIDS diaguids.lib
  PATHS
    "C:/Program Files/Microsoft Visual Studio/2022/Enterprise/DIA SDK/lib/amd64"
    "C:/Program Files/Microsoft Visual Studio/2022/Professional/DIA SDK/lib/amd64"
    "C:/Program Files/Microsoft Visual Studio/2022/Community/DIA SDK/lib/amd64"
    "C:/Program Files/Microsoft Visual Studio/2022/BuildTools/DIA SDK/lib/amd64"
    "C:/Program Files (x86)/Microsoft Visual Studio/18/BuildTools/DIA SDK/lib/amd64"
    "C:/Program Files (x86)/Microsoft Visual Studio/18/BuildTools/VC/Tools/MSVC/14.51.36231/lib/onecore/x64"
  NO_DEFAULT_PATH
)
if(UT_AGENT_DIA_GUIDS AND TARGET LLVMDebugInfoPDB)
  get_target_property(UT_AGENT_PDB_LINKS LLVMDebugInfoPDB INTERFACE_LINK_LIBRARIES)
  string(REPLACE
    "C:/Program Files/Microsoft Visual Studio/2022/Enterprise/DIA SDK/lib/amd64/diaguids.lib"
    "${UT_AGENT_DIA_GUIDS}"
    UT_AGENT_PDB_LINKS
    "${UT_AGENT_PDB_LINKS}"
  )
  set_property(TARGET LLVMDebugInfoPDB PROPERTY INTERFACE_LINK_LIBRARIES
    "${UT_AGENT_PDB_LINKS}"
  )
endif()

if(NOT Clang_DIR)
  set(Clang_DIR "${LLVM_DIR}/../clang" CACHE PATH "Clang CMake package directory")
endif()
find_package(Clang CONFIG REQUIRED)

message(STATUS "LLVM ${LLVM_PACKAGE_VERSION}")
message(STATUS "LLVM include directory: ${LLVM_INCLUDE_DIRS}")
message(STATUS "Clang include directory: ${CLANG_INCLUDE_DIRS}")

add_subdirectory(ut-clang-extract)
