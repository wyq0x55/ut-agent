#include "contract_validation.h"

#include <utility>

namespace ut_agent::extractor {

namespace {

std::string missingObjectField(const llvm::json::Object &Function,
                               llvm::StringRef ArrayKey,
                               llvm::StringRef Field) {
  const llvm::json::Array *Values = Function.getArray(ArrayKey);
  if (!Values)
    return ArrayKey.str();
  for (const llvm::json::Value &Raw : *Values) {
    const llvm::json::Object *Value = Raw.getAsObject();
    if (!Value)
      return ArrayKey.str() + "[]";
    if (!Value->getObject(Field))
      return ArrayKey.str() + "[]." + Field.str();
  }
  return {};
}

std::string missingNestedObjectField(const llvm::json::Object &Function,
                                     llvm::StringRef OuterKey,
                                     llvm::StringRef InnerKey,
                                     llvm::StringRef Field) {
  const llvm::json::Array *Outer = Function.getArray(OuterKey);
  if (!Outer)
    return OuterKey.str();
  for (const llvm::json::Value &RawOuter : *Outer) {
    const llvm::json::Object *OuterValue = RawOuter.getAsObject();
    if (!OuterValue)
      return OuterKey.str() + "[]";
    const llvm::json::Array *Inner = OuterValue->getArray(InnerKey);
    if (!Inner)
      return OuterKey.str() + "[]." + InnerKey.str();
    for (const llvm::json::Value &RawInner : *Inner) {
      const llvm::json::Object *InnerValue = RawInner.getAsObject();
      if (!InnerValue)
        return OuterKey.str() + "[]." + InnerKey.str() + "[]";
      if (!InnerValue->getObject(Field))
        return OuterKey.str() + "[]." + InnerKey.str() + "[]." + Field.str();
    }
  }
  return {};
}

} // namespace

std::string missingTypedField(const llvm::json::Object &Function) {
  constexpr const char *Required[] = {
      "is_static", "parameter_write_effects", "global_write_effects",
      "local_value_effects", "return_effects", "global_objects",
      "control_vars", "calls", "branches", "params"};
  for (const char *Key : Required)
    if (!Function.get(Key))
      return Key;
  for (const auto &Requirement : {
           std::pair<llvm::StringRef, llvm::StringRef>{"params", "type_info"},
           {"branches", "provenance"},
           {"calls", "provenance"},
           {"control_vars", "type_info"},
           {"control_vars", "provenance"},
           {"memory_vars", "provenance"},
           {"global_objects", "provenance"}}) {
    if (std::string Missing = missingObjectField(
            Function, Requirement.first, Requirement.second);
        !Missing.empty())
      return Missing;
  }
  if (std::string Missing = missingNestedObjectField(
          Function, "branches", "atoms", "type_info");
      !Missing.empty())
    return Missing;
  if (std::string Missing = missingNestedObjectField(
          Function, "branches", "cases", "provenance");
      !Missing.empty())
    return Missing;
  if (std::string Missing = missingNestedObjectField(
          Function, "calls", "params", "type_info");
      !Missing.empty())
    return Missing;
  if (const llvm::json::Array *Calls = Function.getArray("calls")) {
    for (const llvm::json::Value &Raw : *Calls) {
      const llvm::json::Object *Call = Raw.getAsObject();
      if (!Call)
        continue;
      const llvm::json::Array *ArgTypes = Call->getArray("arg_type_infos");
      if (!ArgTypes)
        return "calls[].arg_type_infos";
      for (const llvm::json::Value &ArgType : *ArgTypes)
        if (!ArgType.getAsObject())
          return "calls[].arg_type_infos[]";
    }
  }
  if (!Function.getObject("provenance"))
    return "provenance";
  return {};
}

} // namespace ut_agent::extractor
