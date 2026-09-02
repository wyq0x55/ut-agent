add_executable(ut-clang-extract
  main.cpp
  passes/contract_validation.cpp
  passes/type_facts.cpp
)

target_include_directories(ut-clang-extract PRIVATE
  ${LLVM_INCLUDE_DIRS}
  ${CLANG_INCLUDE_DIRS}
)

target_compile_definitions(ut-clang-extract PRIVATE CLANG_BUILD_STATIC)
target_link_libraries(ut-clang-extract PRIVATE clangTooling)

if(MSVC)
  # The LLVM/Clang static libraries in the pinned archive are built without RTTI.
  target_compile_options(ut-clang-extract PRIVATE /GR- /bigobj)
endif()

set_target_properties(ut-clang-extract PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin"
)
