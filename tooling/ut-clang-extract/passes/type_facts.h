#pragma once

#include "clang/AST/ASTContext.h"
#include "clang/AST/Type.h"
#include "llvm/Support/JSON.h"

namespace ut_agent::extractor {

// Type/domain facts are produced while QualType is still available.  The
// Python process receives this object through FunctionIR v3 and never
// reclassifies a C type from its spelling.
llvm::json::Object typeInfo(clang::QualType Type,
                            const clang::ASTContext *Context = nullptr);

} // namespace ut_agent::extractor
