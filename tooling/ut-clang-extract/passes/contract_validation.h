#pragma once

#include <string>

#include "llvm/Support/JSON.h"

namespace ut_agent::extractor {

// Validate the stable fields required at the C++ -> FunctionIR boundary.
// Missing fields are an extractor defect, not a Python-side defaulting case.
std::string missingTypedField(const llvm::json::Object &Function);

} // namespace ut_agent::extractor
