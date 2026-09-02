#include "type_facts.h"

#include "clang/AST/Decl.h"
#include "llvm/Support/JSON.h"

#include <cstdint>
#include <optional>
#include <string>
#include <utility>

namespace ut_agent::extractor {
namespace {

std::string jsonText(llvm::StringRef Text) {
  return llvm::json::isUTF8(Text) ? Text.str() : llvm::json::fixUTF8(Text);
}

} // namespace

llvm::json::Object typeInfo(clang::QualType Type,
                            const clang::ASTContext *Context) {
  if (Type.isNull())
    return llvm::json::Object{
        {"canonical_type", ""},
        {"kind", "unknown"},
        {"bit_width", llvm::json::Value(nullptr)},
        {"signed", llvm::json::Value(nullptr)},
        {"min_value", llvm::json::Value(nullptr)},
        {"max_value", llvm::json::Value(nullptr)},
        {"enum_values", llvm::json::Object{}},
        {"pointer_depth", static_cast<int64_t>(0)},
        {"pointee_type", llvm::json::Value(nullptr)},
        {"pointee_info", llvm::json::Value(nullptr)},
        {"is_const", false},
        {"is_volatile", false}};
  clang::QualType Inspected = Type;
  unsigned PointerDepth = 0;
  while (!Inspected.isNull() && Inspected->isPointerType()) {
    ++PointerDepth;
    Inspected = Inspected->getPointeeType();
  }
  clang::QualType Canonical = Type.getCanonicalType();
  clang::QualType BaseCanonical = Inspected.getCanonicalType();
  std::string Kind = "other";
  std::optional<bool> Signed;
  if (PointerDepth > 0) {
    Kind = "pointer";
  } else if (Inspected->isBooleanType()) {
    Kind = "bool";
    Signed = false;
  } else if (Inspected->isEnumeralType()) {
    Kind = "enum";
    Signed = Inspected->isSignedIntegerType();
  } else if (Inspected->isIntegerType()) {
    Kind = "integer";
    Signed = Inspected->isSignedIntegerType();
  } else if (Inspected->isFloatingType()) {
    Kind = "float";
  } else if (Inspected->isRecordType()) {
    Kind = "record";
  }

  llvm::json::Object EnumValues;
  if (const auto *Enum = BaseCanonical->getAs<clang::EnumType>()) {
    for (const clang::EnumConstantDecl *Value : Enum->getDecl()->enumerators())
      EnumValues[jsonText(Value->getNameAsString())] =
          Value->getInitVal().getSExtValue();
  }

  llvm::json::Value BitWidth(nullptr);
  llvm::json::Value MinValue(nullptr);
  llvm::json::Value MaxValue(nullptr);
  if (Context && PointerDepth == 0 &&
      (Inspected->isIntegerType() || Inspected->isEnumeralType() ||
       Inspected->isFloatingType())) {
    const unsigned Width = Context->getTypeSize(Inspected);
    BitWidth = static_cast<int64_t>(Width);
    if (Inspected->isIntegerType() || Inspected->isEnumeralType()) {
      if (Width < 63) {
        if (Signed && *Signed) {
          MinValue = -(static_cast<int64_t>(1) << (Width - 1));
          MaxValue = (static_cast<int64_t>(1) << (Width - 1)) - 1;
        } else {
          MinValue = static_cast<int64_t>(0);
          MaxValue = (static_cast<int64_t>(1) << Width) - 1;
        }
      }
    }
  }

  return llvm::json::Object{
      {"canonical_type", jsonText(Canonical.getAsString())},
      {"kind", Kind},
      {"bit_width", std::move(BitWidth)},
      {"signed", Signed ? llvm::json::Value(*Signed)
                          : llvm::json::Value(nullptr)},
      {"min_value", std::move(MinValue)},
      {"max_value", std::move(MaxValue)},
      {"enum_values", std::move(EnumValues)},
      {"pointer_depth", static_cast<int64_t>(PointerDepth)},
      {"pointee_type", PointerDepth
                            ? llvm::json::Value(
                                  jsonText(Type->getPointeeType().getAsString()))
                            : llvm::json::Value(nullptr)},
      {"pointee_info", PointerDepth
                            ? llvm::json::Value(typeInfo(Inspected, Context))
                            : llvm::json::Value(nullptr)},
      {"is_const", Type.isConstQualified() ||
                       (PointerDepth && Inspected.isConstQualified())},
      {"is_volatile", Type.isVolatileQualified() ||
                         (PointerDepth && Inspected.isVolatileQualified())}};
}

} // namespace ut_agent::extractor
