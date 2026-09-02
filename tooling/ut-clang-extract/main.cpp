#include "clang/AST/ASTConsumer.h"
#include "clang/AST/Decl.h"
#include "clang/AST/Expr.h"
#include "clang/AST/ParentMapContext.h"
#include "clang/AST/RecursiveASTVisitor.h"
#include "clang/Basic/Diagnostic.h"
#include "clang/Basic/Version.h"
#include "clang/Frontend/CompilerInstance.h"
#include "clang/Frontend/FrontendActions.h"
#include "clang/Lex/Lexer.h"
#include "clang/Lex/PPCallbacks.h"
#include "clang/Lex/Preprocessor.h"
#include "clang/Tooling/CompilationDatabase.h"
#include "clang/Tooling/Tooling.h"
#include "llvm/Support/CommandLine.h"
#include "llvm/Support/Error.h"
#include "llvm/Support/JSON.h"
#include "llvm/Support/MemoryBuffer.h"
#include "llvm/Support/Path.h"
#include "llvm/Support/raw_ostream.h"
#include "passes/contract_validation.h"
#include "passes/type_facts.h"

#include <algorithm>
#include <cctype>
#include <cstdint>
#include <map>
#include <memory>
#include <limits>
#include <optional>
#include <set>
#include <sstream>
#include <string>
#include <system_error>
#include <tuple>
#include <utility>
#include <vector>

using namespace clang;

namespace {

constexpr llvm::StringLiteral ExtractorVersion = "0.3.0";
constexpr llvm::StringLiteral ExtractorName = "ut-clang-extract";

llvm::cl::opt<std::string> ContextPath(
    "context", llvm::cl::desc("CompileContext JSON path"));
llvm::cl::opt<std::string> OutputPath(
    "output", llvm::cl::desc("Output FunctionIR JSON path"),
    llvm::cl::init("-"));
llvm::cl::opt<std::string> FunctionName(
    "function", llvm::cl::desc("Extract one function by name"));
llvm::cl::opt<std::string> TargetsPath(
    "targets-file",
    llvm::cl::desc("Extract only absolute-file<TAB>function targets"));
llvm::cl::opt<bool> ShowVersion(
    "extractor-version", llvm::cl::desc("Print extractor and LLVM versions"),
    llvm::cl::init(false));

// Clang can read legacy source encodings as byte strings.  LLVM JSON requires
// every string value to be UTF-8, and its debug builds assert before applying
// their own repair.  Keep parsing on the original bytes, but make every value
// crossing the JSON boundary deterministic and valid UTF-8.
std::string jsonText(llvm::StringRef Text) {
  return llvm::json::isUTF8(Text) ? Text.str() : llvm::json::fixUTF8(Text);
}

struct FunctionFact {
  std::string File;
  unsigned Line = 0;
  std::string Name;
  llvm::json::Object Value;
};

struct DiagnosticFact {
  std::string Source;
  llvm::json::Value Value;
};

struct FunctionDefinitionFact {
  std::string File;
  unsigned Line = 0;
  std::string Name;
  std::map<unsigned, std::vector<std::string>> ParamFields;
  std::vector<std::string> ReturnFields;
};

struct FunctionPointerParameterFact {
  std::string Name;
  std::string Type;
  bool IsPointer = false;
  bool IsConst = false;
  llvm::json::Object TypeInfo;
};

struct FunctionPointerTargetFact {
  std::string Name;
  std::string ReturnType;
  std::vector<FunctionPointerParameterFact> Params;
};

struct RunState {
  std::vector<DiagnosticFact> Diagnostics;
  std::vector<FunctionFact> Functions;
  // Definitions from context sources are kept as call-site enrichment facts.
  // Clang creates one AST per source passed to ClangTool, so a declaration in
  // the target TU cannot see a callee body compiled from another source.
  std::vector<FunctionDefinitionFact> FunctionDefinitions;
  // Constant initializers are collected from every source in the supplied
  // CompileContext.  The target function may only have an extern declaration
  // in its own TU, so applying these facts is deliberately deferred until the
  // complete document is assembled.
  std::map<std::string, int64_t> GlobalInitializers;
  // Function-pointer table initializers are retained as AST facts.  A call
  // through a table can then be resolved to the configured Rte function
  // without consulting a reference CSV.
  std::map<std::string, std::vector<FunctionPointerTargetFact>>
      FunctionPointerTargets;
  std::set<std::string> TargetFiles;
  std::string ActiveSource;
  bool HasError = false;
  bool HasWarning = false;
};

using TargetSet = std::set<std::string>;

void addIssue(RunState &State, llvm::StringRef Code,
              llvm::StringRef Severity, llvm::StringRef Message);

std::string normalizedFileKey(llvm::StringRef File) {
  std::string Key = File.str();
  for (char &Value : Key) {
    if (Value == '\\')
      Value = '/';
    Value = static_cast<char>(
        std::tolower(static_cast<unsigned char>(Value)));
  }
  return Key;
}

std::string targetKey(llvm::StringRef File, llvm::StringRef Name) {
  std::string Key = normalizedFileKey(File);
  Key.push_back('\t');
  Key += Name.str();
  return Key;
}

bool loadTargets(RunState &State, TargetSet &Targets) {
  if (TargetsPath.empty())
    return true;
  auto Buffer = llvm::MemoryBuffer::getFile(TargetsPath);
  if (!Buffer) {
    addIssue(State, "INVALID_TARGETS_FILE", "error",
             (std::string("cannot open targets file: ") + TargetsPath).c_str());
    return false;
  }
  std::istringstream Lines((*Buffer)->getBuffer().str());
  std::string Line;
  unsigned LineNumber = 0;
  while (std::getline(Lines, Line)) {
    ++LineNumber;
    if (!Line.empty() && Line.back() == '\r')
      Line.pop_back();
    if (Line.empty())
      continue;
    const size_t Separator = Line.find('\t');
    if (Separator == std::string::npos || Separator == 0 ||
        Separator + 1 >= Line.size()) {
      addIssue(State, "INVALID_TARGETS_FILE", "error",
               (std::string("expected file<TAB>function at line ") +
                std::to_string(LineNumber))
                   .c_str());
      return false;
    }
    const llvm::StringRef File = llvm::StringRef(Line).substr(0, Separator);
    const llvm::StringRef Name = llvm::StringRef(Line).substr(Separator + 1);
    Targets.insert(targetKey(File, Name));
    State.TargetFiles.insert(normalizedFileKey(File));
  }
  if (Targets.empty()) {
    addIssue(State, "INVALID_TARGETS_FILE", "error",
             "targets file contains no targets");
    return false;
  }
  return true;
}

std::string compactText(llvm::StringRef Text) {
  std::string Result;
  Result.reserve(Text.size());
  for (char Value : Text)
    if (!llvm::isSpace(Value))
      Result.push_back(Value);
  return Result;
}

std::string normalizeIndexedPath(llvm::StringRef Path) {
  std::string Result;
  bool InIndex = false;
  for (char Value : Path) {
    if (Value == '[') {
      InIndex = true;
      continue;
    }
    if (Value == ']') {
      InIndex = false;
      continue;
    }
    if (!InIndex)
      Result.push_back(Value);
  }
  return Result;
}

const FunctionDecl *functionPointerTarget(const Expr *Expression) {
  if (!Expression)
    return nullptr;
  Expression = Expression->IgnoreParenImpCasts();
  if (const auto *Unary = dyn_cast<UnaryOperator>(Expression)) {
    if (Unary->getOpcode() == UO_AddrOf)
      return functionPointerTarget(Unary->getSubExpr());
  }
  if (const auto *Reference = dyn_cast<DeclRefExpr>(Expression))
    return dyn_cast<FunctionDecl>(Reference->getDecl());
  return nullptr;
}

llvm::json::Object issue(llvm::StringRef Code, llvm::StringRef Severity,
                         llvm::StringRef Message) {
  return llvm::json::Object{{"code", jsonText(Code)},
                            {"severity", jsonText(Severity)},
                            {"message", jsonText(Message)}};
}

void addIssue(RunState &State, llvm::StringRef Code, llvm::StringRef Severity,
              llvm::StringRef Message) {
  State.Diagnostics.push_back(
      DiagnosticFact{State.ActiveSource, issue(Code, Severity, Message)});
  if (Severity == "error")
    State.HasError = true;
  if (Severity == "warning")
    State.HasWarning = true;
}

std::string locationFile(const SourceManager &SM, SourceLocation Loc) {
  llvm::StringRef File = SM.getFilename(Loc);
  return File.empty() ? std::string("<unknown>") : jsonText(File);
}

llvm::json::Object location(const SourceManager &SM, SourceLocation Begin,
                            SourceLocation End) {
  if (Begin.isInvalid())
    return llvm::json::Object{{"file", "<unknown>"},
                              {"line", 1},
                              {"column", 1},
                              {"offset", 0},
                              {"end_offset", 0}};

  SourceLocation SpellingBegin = SM.getSpellingLoc(Begin);
  SourceLocation SpellingEnd = SM.getSpellingLoc(End.isValid() ? End : Begin);
  unsigned Offset = SM.getFileOffset(SpellingBegin);
  unsigned EndOffset = SM.getFileOffset(SpellingEnd);
  if (EndOffset < Offset)
    EndOffset = Offset;
  return llvm::json::Object{
      {"file", locationFile(SM, SpellingBegin)},
      {"line", static_cast<int64_t>(SM.getSpellingLineNumber(SpellingBegin))},
      {"column", static_cast<int64_t>(SM.getSpellingColumnNumber(SpellingBegin))},
      {"offset", static_cast<int64_t>(Offset)},
      {"end_offset", static_cast<int64_t>(EndOffset)}};
}

llvm::json::Object provenance(const SourceManager &SM, SourceRange Range,
                              llvm::StringRef ASTKind,
                              const LangOptions *LangOpts = nullptr) {
  SourceLocation Begin = Range.getBegin();
  SourceLocation End = Range.getEnd();
  llvm::json::Array MacroStack;
  if (LangOpts) {
    SourceLocation Cursor = Begin;
    for (unsigned Depth = 0; Cursor.isMacroID() && Depth < 32; ++Depth) {
      llvm::StringRef Name = Lexer::getImmediateMacroName(Cursor, SM, *LangOpts);
      if (!Name.empty())
        MacroStack.push_back(jsonText(Name));
      SourceLocation Caller = SM.getImmediateMacroCallerLoc(Cursor);
      if (Caller == Cursor)
        break;
      Cursor = Caller;
    }
  }
  return llvm::json::Object{
      {"spelling", location(SM, Begin, End)},
      {"expansion", location(SM, SM.getExpansionLoc(Begin),
                              SM.getExpansionLoc(End))},
      {"macro_stack", std::move(MacroStack)},
      {"ast_kind", jsonText(ASTKind)}};
}

llvm::json::Object emptyExtensions() { return llvm::json::Object{}; }

using ut_agent::extractor::typeInfo;

llvm::json::Object parameter(const ParmVarDecl *Param,
                             const ASTContext *Context = nullptr) {
  QualType Type = Param->getType();
  bool IsPointer = Type->isPointerType();
  bool IsConst = Type.isConstQualified();
  if (IsPointer)
    IsConst = IsConst || Type->getPointeeType().isConstQualified();
  return llvm::json::Object{
      {"name", jsonText(Param->getNameAsString())},
      {"type", jsonText(Type.getAsString())},
      {"is_ptr", IsPointer},
      {"is_const", IsConst},
      {"is_written", false},
      {"type_info", typeInfo(Type, Context)},
      {"access_paths", llvm::json::Array{}},
      {"write_effects", llvm::json::Array{}},
      {"write_status", "unknown"},
      {"extensions", emptyExtensions()}};
}

std::string sourceText(const SourceManager &SM, const LangOptions &LangOpts,
                       SourceRange Range, bool Spelling) {
  if (Range.isInvalid())
    return {};
  SourceLocation Begin = Spelling ? SM.getSpellingLoc(Range.getBegin())
                                  : SM.getExpansionLoc(Range.getBegin());
  SourceLocation End = Spelling ? SM.getSpellingLoc(Range.getEnd())
                                : SM.getExpansionLoc(Range.getEnd());
  if (Begin.isInvalid() || End.isInvalid())
    return {};
  bool Invalid = false;
  llvm::StringRef Text = Lexer::getSourceText(
      CharSourceRange::getTokenRange(SourceRange(Begin, End)), SM, LangOpts,
      &Invalid);
  return Invalid ? std::string() : jsonText(Text);
}

std::string prettyText(const Expr *Expression, const LangOptions &LangOpts) {
  if (!Expression)
    return {};
  std::string Text;
  llvm::raw_string_ostream Stream(Text);
  PrintingPolicy Policy(LangOpts);
  Expression->printPretty(Stream, nullptr, Policy);
  Stream.flush();
  return jsonText(Text);
}

std::optional<std::string> immediateMacro(const SourceManager &SM,
                                          const LangOptions &LangOpts,
                                          SourceLocation Loc) {
  if (Loc.isInvalid())
    return std::nullopt;
  llvm::StringRef Name = Lexer::getImmediateMacroName(Loc, SM, LangOpts);
  return Name.empty() ? std::nullopt
                      : std::optional<std::string>(jsonText(Name));
}

const VarDecl *referencedVar(const Expr *Expression) {
  if (!Expression)
    return nullptr;
  Expression = Expression->IgnoreParenImpCasts();
  if (const auto *Ref = dyn_cast<DeclRefExpr>(Expression))
    return dyn_cast<VarDecl>(Ref->getDecl());
  if (const auto *Member = dyn_cast<MemberExpr>(Expression))
    return referencedVar(Member->getBase());
  if (const auto *Subscript = dyn_cast<ArraySubscriptExpr>(Expression))
    return referencedVar(Subscript->getBase());
  if (const auto *Unary = dyn_cast<UnaryOperator>(Expression))
    return referencedVar(Unary->getSubExpr());
  // Macro accessors frequently wrap a global read in a cast, an opaque value,
  // or another expression node that does not preserve the simple lvalue
  // shape above after semantic analysis.  Walk the AST children as a final
  // structural fallback; this remains declaration-driven and does not infer
  // names from source text.
  for (const Stmt *Child : Expression->children()) {
    if (const auto *ChildExpr = dyn_cast_or_null<Expr>(Child)) {
      if (const VarDecl *Variable = referencedVar(ChildExpr))
        return Variable;
    }
  }
  return nullptr;
}

std::string memberPath(const Expr *Expression) {
  if (!Expression)
    return {};
  Expression = Expression->IgnoreParenImpCasts();
  if (const auto *Reference = dyn_cast<DeclRefExpr>(Expression))
    return jsonText(Reference->getNameInfo().getAsString());
  if (const auto *Member = dyn_cast<MemberExpr>(Expression)) {
    std::string Prefix = memberPath(Member->getBase());
    std::string Name = jsonText(Member->getMemberNameInfo().getAsString());
    if (Prefix.empty())
      return Name;
    if (Name.empty())
      return Prefix;
    return Prefix + "." + Name;
  }
  if (const auto *Subscript = dyn_cast<ArraySubscriptExpr>(Expression))
    return memberPath(Subscript->getBase());
  if (const auto *Unary = dyn_cast<UnaryOperator>(Expression))
    return memberPath(Unary->getSubExpr());
  return {};
}

class CallerFieldPathVisitor final
    : public RecursiveASTVisitor<CallerFieldPathVisitor> {
public:
  explicit CallerFieldPathVisitor(ASTContext &) {}

  bool VisitMemberExpr(MemberExpr *Expression) {
    const VarDecl *Root = referencedVar(Expression);
    if (!Root || Root->hasGlobalStorage())
      return true;
    const std::string FullPath = memberPath(Expression);
    const std::string Prefix = jsonText(Root->getNameAsString()) + ".";
    if (FullPath.rfind(Prefix, 0) != 0 || FullPath.size() <= Prefix.size())
      return true;
    auto &Paths = PathsByRoot[Root];
    if (std::find(Paths.begin(), Paths.end(), FullPath) == Paths.end())
      Paths.push_back(FullPath);
    return true;
  }

  bool VisitBinaryOperator(BinaryOperator *Operator) {
    if (!Operator->isAssignmentOp())
      return true;
    const VarDecl *Lhs = referencedVar(Operator->getLHS());
    const VarDecl *Rhs = referencedVar(Operator->getRHS());
    if (!Lhs || !Rhs)
      return true;
    const bool LhsObservable = isa<ParmVarDecl>(Lhs) ||
        (Lhs->hasGlobalStorage() && !Lhs->isLocalVarDecl());
    if (LhsObservable && !isa<ParmVarDecl>(Rhs) &&
        !(Rhs->hasGlobalStorage() && !Rhs->isLocalVarDecl()))
      ObservableRoots.insert(Rhs);
    return true;
  }

  bool VisitReturnStmt(ReturnStmt *Statement) {
    const VarDecl *Root = Statement->getRetValue()
                              ? referencedVar(Statement->getRetValue())
                              : nullptr;
    if (Root && !isa<ParmVarDecl>(Root) &&
        !(Root->hasGlobalStorage() && !Root->isLocalVarDecl()))
      ObservableRoots.insert(Root);
    return true;
  }

  const std::map<const VarDecl *, std::vector<std::string>> &paths() const {
    return PathsByRoot;
  }

  bool observableRoot(const VarDecl *Root) const {
    return ObservableRoots.count(Root) != 0;
  }

private:
  std::map<const VarDecl *, std::vector<std::string>> PathsByRoot;
  std::set<const VarDecl *> ObservableRoots;
};

// Preserve the actual lvalue path used by the function body.  ``memberPath``
// intentionally drops subscripts because it is used for type/field lookup;
// WinAMS output columns need the subscript and must distinguish ``p[26]``
// from the pointer base ``p``.
std::string accessPath(const Expr *Expression, const ASTContext &Context) {
  if (!Expression)
    return {};
  Expression = Expression->IgnoreParenImpCasts();
  if (const auto *Reference = dyn_cast<DeclRefExpr>(Expression))
    return jsonText(Reference->getNameInfo().getAsString());
  if (const auto *Subscript = dyn_cast<ArraySubscriptExpr>(Expression)) {
    std::string Prefix = accessPath(Subscript->getBase(), Context);
    if (Prefix.empty())
      return {};
    std::string Index;
    Expr::EvalResult Evaluated;
    if (Subscript->getIdx()->EvaluateAsInt(Evaluated, Context))
      Index = std::to_string(Evaluated.Val.getInt().getSExtValue());
    else
      Index = sourceText(Context.getSourceManager(), Context.getLangOpts(),
                          Subscript->getIdx()->getSourceRange(), true);
    if (Index.empty())
      return Prefix;
    return Prefix + "[" + jsonText(Index) + "]";
  }
  if (const auto *Member = dyn_cast<MemberExpr>(Expression)) {
    std::string Prefix = accessPath(Member->getBase(), Context);
    if (Prefix.empty())
      return jsonText(Member->getMemberNameInfo().getAsString());
    const char *Separator = Member->isArrow() ? "->" : ".";
    return Prefix + Separator +
           jsonText(Member->getMemberNameInfo().getAsString());
  }
  if (const auto *Unary = dyn_cast<UnaryOperator>(Expression)) {
    if (Unary->getOpcode() == UO_Deref) {
      std::string Prefix = accessPath(Unary->getSubExpr(), Context);
      return Prefix.empty() ? std::string() : "*" + Prefix;
    }
    return accessPath(Unary->getSubExpr(), Context);
  }
  return {};
}

void appendRecordLeafPaths(const QualType &Type, const ASTContext &Context,
                           llvm::StringRef Prefix,
                           std::vector<std::string> &Result) {
  if (const auto *Array = Context.getAsConstantArrayType(Type)) {
    const uint64_t Size = Array->getSize().getZExtValue();
    for (uint64_t Index = 0; Index < Size; ++Index) {
      std::string Indexed = Prefix.str() + "[" + std::to_string(Index) + "]";
      appendRecordLeafPaths(Array->getElementType(), Context, Indexed, Result);
    }
    return;
  }

  QualType Inspected = Type;
  const RecordType *Record = Inspected->getAs<RecordType>();
  if (!Record)
    Record = Inspected.getCanonicalType()->getAs<RecordType>();
  if (Record && Record->getDecl()->isCompleteDefinition()) {
    std::vector<const FieldDecl *> Fields;
    for (const FieldDecl *Field : Record->getDecl()->fields())
      Fields.push_back(Field);
    // WinAMS uses the source declaration order for structure/bit-field
    // columns.  Keep every union alternative in the IR as well: the body
    // access facts select the active alternative during CSV rendering.  A
    // type-only preference (for example always choosing ``st_frame``) loses
    // valid accesses such as ``u2_hword`` and makes unrelated union members
    // look observable.
    for (const FieldDecl *Field : Fields) {
      const std::string FieldName = jsonText(Field->getNameAsString());
      std::string FieldPrefix = Prefix.str();
      if (!FieldName.empty()) {
        if (!FieldPrefix.empty())
          FieldPrefix += ".";
        FieldPrefix += FieldName;
      }
      const size_t Before = Result.size();
      appendRecordLeafPaths(Field->getType(), Context, FieldPrefix, Result);
      if (Result.size() == Before && !FieldPrefix.empty())
        Result.push_back(FieldPrefix);
    }
    return;
  }

  if (!Prefix.empty())
    Result.push_back(Prefix.str());
}

std::vector<std::string> recordLeafPaths(QualType Type,
                                         const ASTContext &Context) {
  std::vector<std::string> Result;
  appendRecordLeafPaths(Type, Context, "", Result);
  std::vector<std::string> Unique;
  for (const std::string &Path : Result) {
    if (std::find(Unique.begin(), Unique.end(), Path) == Unique.end())
      Unique.push_back(Path);
  }
  return Unique;
}

class ParameterFieldVisitor final
    : public RecursiveASTVisitor<ParameterFieldVisitor> {
public:
  explicit ParameterFieldVisitor(
      const std::map<const ParmVarDecl *, unsigned> &Indexes)
      : Indexes(Indexes) {}

  bool VisitMemberExpr(MemberExpr *Expression) {
    const auto *Parameter = dyn_cast_or_null<ParmVarDecl>(
        referencedVar(Expression));
    if (!Parameter)
      return true;
    auto Index = Indexes.find(Parameter);
    if (Index == Indexes.end())
      return true;
    const std::string FullPath = memberPath(Expression);
    const std::string Prefix = jsonText(Parameter->getNameAsString()) + ".";
    if (FullPath.rfind(Prefix, 0) != 0 || FullPath.size() <= Prefix.size())
      return true;
    const std::string Relative = FullPath.substr(Prefix.size());
    auto &Paths = Fields[Index->second];
    if (std::find(Paths.begin(), Paths.end(), Relative) == Paths.end())
      Paths.push_back(Relative);
    return true;
  }

  std::vector<std::string> paths(unsigned Index) const {
    auto It = Fields.find(Index);
    return It == Fields.end() ? std::vector<std::string>{} : It->second;
  }

  llvm::json::Object json() const {
    llvm::json::Object Result;
    for (const auto &Entry : Fields) {
      llvm::json::Array Paths;
      for (const std::string &Path : Entry.second)
        Paths.push_back(jsonText(Path));
      Result[std::to_string(Entry.first)] = std::move(Paths);
    }
    return Result;
  }

private:
  const std::map<const ParmVarDecl *, unsigned> &Indexes;
  std::map<unsigned, std::vector<std::string>> Fields;
};

std::optional<int64_t> constantInteger(const Expr *Expression,
                                        const ASTContext &Context) {
  if (!Expression)
    return std::nullopt;
  Expr::EvalResult Result;
  if (!Expression->EvaluateAsInt(Result, Context))
    return std::nullopt;
  return Result.Val.getInt().getSExtValue();
}

std::optional<int64_t> nestedConstantInteger(const Expr *Expression,
                                              const ASTContext &Context) {
  if (!Expression)
    return std::nullopt;
  if (auto Value = constantInteger(Expression, Context))
    return Value;
  for (const Stmt *Child : Expression->children()) {
    if (const auto *ChildExpr = dyn_cast_or_null<Expr>(Child)) {
      if (auto Value = nestedConstantInteger(ChildExpr, Context))
        return Value;
    }
  }
  return std::nullopt;
}

std::string comparisonOp(BinaryOperatorKind Opcode) {
  if (Opcode == BO_EQ || Opcode == BO_NE || Opcode == BO_LT ||
      Opcode == BO_LE || Opcode == BO_GT || Opcode == BO_GE)
    return BinaryOperator::getOpcodeStr(Opcode).str();
  return {};
}

class ParameterWriteVisitor final
    : public RecursiveASTVisitor<ParameterWriteVisitor> {
public:
  explicit ParameterWriteVisitor(const ParmVarDecl *Target) : Target(Target) {}

  bool VisitBinaryOperator(BinaryOperator *Operator) {
    if (Operator->isAssignmentOp() &&
        referencedVar(Operator->getLHS()) == Target)
      Written = true;
    return true;
  }

  bool VisitUnaryOperator(UnaryOperator *Operator) {
    if (Operator->isIncrementDecrementOp() &&
        referencedVar(Operator->getSubExpr()) == Target)
      Written = true;
    return true;
  }

  bool written() const { return Written; }

private:
  const ParmVarDecl *Target;
  bool Written = false;
};

class FunctionBodyVisitor final
    : public RecursiveASTVisitor<FunctionBodyVisitor> {
  struct MemoryFact {
    std::string Name;
    int64_t Address = 0;
    unsigned Width = 1;
    bool Read = false;
    bool Write = false;
    bool Conditional = false;
    std::optional<int64_t> InputValue;
    std::optional<int64_t> ExpectedValue;
    SourceRange Range;
  };

  // A control expression may name an automatic variable even though the
  // testcase controls the value which produced it.  Keep this provenance in
  // the AST extractor; the Python rules layer must not guess it from names.
  struct ValueOrigin {
    std::string Kind;
    std::string Expression;
    std::string Driver;
    const VarDecl *DriverDecl = nullptr;
    std::string Callee;
    int64_t SourceOffset = -1;
    int64_t CallOffset = -1;
    std::string Base;
    std::string Index;
    std::string Field;
  };

  struct ControlFact {
    std::string Name;
    std::string Var;
    std::string Source;
    std::string Type;
    std::string CanonicalVar;
    std::string ASTKind = "BinaryOperator";
    const VarDecl *Variable = nullptr;
    std::optional<ValueOrigin> Origin;
    std::set<std::string> BranchIds;
    SourceRange Range;
  };

  struct GlobalFieldAccess {
    bool Read = false;
    bool Write = false;
    bool CopiedFromLocal = false;
    unsigned Line = 1;
    unsigned Offset = 0;
    unsigned ReadLine = 0;
    unsigned ReadOffset = 0;
    unsigned WriteLine = 0;
    unsigned WriteOffset = 0;
  };

  struct GlobalFact {
    std::string Name;
    bool Read = false;
    bool Write = false;
    bool IsConst = false;
    bool IsVolatile = false;
    bool IsUnion = false;
    std::string SourceFile;
    std::vector<uint64_t> ArraySizes;
    std::vector<std::string> FieldPaths;
    std::map<std::string, GlobalFieldAccess> FieldAccesses;
    unsigned ReadLine = 0;
    unsigned ReadOffset = 0;
    unsigned WriteLine = 0;
    unsigned WriteOffset = 0;
    SourceRange Range;
  };

  struct ParamAccess {
    std::string Path;
    bool Read = false;
    bool Write = false;
    unsigned Offset = 0;
  };

  struct ParamWriteEffect {
    std::string Path;
    std::string Value;
    std::optional<int64_t> ConstantValue;
    std::vector<std::pair<std::string, bool>> Guards;
    int64_t Order = 0;
  };

  struct ReturnEffect {
    std::string Value;
    std::optional<int64_t> ConstantValue;
    int64_t SourceOffset = -1;
    std::optional<ValueOrigin> Origin;
    std::vector<std::pair<std::string, bool>> Guards;
    int64_t Order = 0;
  };

  struct LocalValueEffect {
    ValueOrigin Origin;
    std::optional<int64_t> ConstantValue;
    int64_t SourceOffset = -1;
    std::string Path;
    std::string Operation = "=";
    std::vector<std::pair<std::string, bool>> Guards;
    int64_t Order = 0;
  };

  struct GlobalWriteEffect {
    std::string Path;
    std::string Value;
    std::optional<int64_t> ConstantValue;
    std::optional<ValueOrigin> Origin;
    int64_t SourceOffset = -1;
    std::vector<std::pair<std::string, bool>> Guards;
    int64_t Order = 0;
  };

public:
  FunctionBodyVisitor(ASTContext &Context, const FunctionDecl *Decl,
                      const std::map<std::string, std::string> &Macros,
                      const std::map<std::string,
                                     std::map<std::string, int64_t>> &Enums)
      : Context(Context), Decl(Decl), SM(Context.getSourceManager()),
        LangOpts(Context.getLangOpts()), Macros(Macros), Enums(Enums) {}

  void build() {
    if (Stmt *Body = Decl->getBody()) {
      // Calls are visited before later statements in source order.  Collect
      // caller-side member uses in a read-only prepass so a stub field read
      // after the call is available when the call fact is built.
      CallerFieldPathVisitor Collector(Context);
      Collector.TraverseStmt(Body);
      LocalFieldPaths = Collector.paths();
      for (const auto &Entry : LocalFieldPaths)
        if (Collector.observableRoot(Entry.first))
          ObservableRoots.insert(Entry.first);
      TraverseStmt(Body);
    }
  }

  llvm::json::Array parameters() const {
    llvm::json::Array Result;
    for (const ParmVarDecl *Param : Decl->parameters()) {
      llvm::json::Object Value = parameter(Param, &Context);
      Value["is_written"] = WrittenParams.count(Param) != 0;
      llvm::json::Array Accesses;
      auto It = ParameterAccesses.find(Param);
      if (It != ParameterAccesses.end()) {
        for (const ParamAccess &Access : It->second) {
          Accesses.push_back(llvm::json::Object{
              {"path", jsonText(Access.Path)},
              {"read", Access.Read},
              {"write", Access.Write},
              {"offset", static_cast<int64_t>(Access.Offset)}});
        }
      }
      Value["access_paths"] = std::move(Accesses);
      llvm::json::Array Writes;
      auto Effects = ParamWriteEffects.find(Param);
      if (Effects != ParamWriteEffects.end()) {
        for (const ParamWriteEffect &Effect : Effects->second) {
          llvm::json::Array Guards;
          for (const auto &Guard : Effect.Guards)
            Guards.push_back(llvm::json::Object{
                {"bid", Guard.first}, {"then", Guard.second}});
          Writes.push_back(llvm::json::Object{
              {"path", jsonText(Effect.Path)},
              {"value", jsonText(Effect.Value)},
              {"constant_value", Effect.ConstantValue
                                    ? llvm::json::Value(*Effect.ConstantValue)
                                    : llvm::json::Value(nullptr)},
              {"source_offset", static_cast<int64_t>(-1)},
              {"order", Effect.Order},
              {"guards", std::move(Guards)},
              {"origin", llvm::json::Value(nullptr)},
              {"name", llvm::json::Value(nullptr)},
              {"operator", "="}});
        }
      }
      Value["write_effects"] = std::move(Writes);
      Value["write_status"] = Param->getType()->isPointerType()
                                   ? (ParamWriteEffects.count(Param) != 0
                                          ? llvm::json::Value("known")
                                          : llvm::json::Value("unknown"))
                                   : llvm::json::Value("unknown");
      Result.push_back(llvm::json::Value(std::move(Value)));
    }
    return Result;
  }

  llvm::json::Array branches() { return std::move(Branches); }
  llvm::json::Array calls() { return std::move(Calls); }

  llvm::json::Array names(const std::set<std::string> &Values) const {
    llvm::json::Array Result;
    for (const std::string &Value : Values)
      Result.push_back(jsonText(Value));
    return Result;
  }

  llvm::json::Array parameterWriteEffects() const {
    llvm::json::Array Result;
    for (const auto &Entry : ParamWriteEffects) {
      for (const ParamWriteEffect &Effect : Entry.second) {
        llvm::json::Array Guards;
        for (const auto &Guard : Effect.Guards)
          Guards.push_back(llvm::json::Object{
              {"bid", Guard.first}, {"then", Guard.second}});
        Result.push_back(llvm::json::Object{
            {"name", jsonText(Entry.first->getNameAsString())},
            {"path", jsonText(Effect.Path)},
            {"value", jsonText(Effect.Value)},
            {"constant_value", Effect.ConstantValue
                                  ? llvm::json::Value(*Effect.ConstantValue)
                                  : llvm::json::Value(nullptr)},
            {"source_offset", static_cast<int64_t>(-1)},
            {"order", Effect.Order},
            {"guards", std::move(Guards)},
            {"origin", llvm::json::Value(nullptr)},
            {"operator", "="}});
      }
    }
    return Result;
  }

  llvm::json::Array globalWrites() const { return names(GlobalWrites); }
  llvm::json::Array globalsUsed() const { return names(GlobalsUsed); }
  llvm::json::Array locals() const { return names(Locals); }
  llvm::json::Array localOrigins() const {
    llvm::json::Array Result;
    for (const auto &Entry : LocalOrigins)
      Result.push_back(jsonText(Entry.first->getNameAsString()));
    return Result;
  }

  llvm::json::Object origin(const ValueOrigin &Value) const {
    llvm::json::Object Result{
        {"kind", jsonText(Value.Kind)},
        {"expression", jsonText(Value.Expression)}};
    if (!Value.Driver.empty())
      Result["driver"] = jsonText(Value.Driver);
    if (!Value.Callee.empty())
      Result["callee"] = jsonText(Value.Callee);
    if (Value.CallOffset >= 0) {
      Result["call_offset"] = Value.CallOffset;
      const int64_t Order = callOrder(Value.CallOffset);
      if (Order >= 0) {
        Result["call_order"] = Order;
        Result["call_id"] = "call_" + std::to_string(Order);
      }
    }
    if (!Value.Base.empty())
      Result["base"] = jsonText(Value.Base);
    if (!Value.Index.empty())
      Result["index"] = jsonText(Value.Index);
    if (!Value.Field.empty())
      Result["field"] = jsonText(Value.Field);
    return Result;
  }

  llvm::json::Array localValueEffects() const {
    llvm::json::Array Result;
    for (const auto &Entry : LocalValueEffects) {
      const std::string Name = Entry.first->getNameAsString();
      for (const LocalValueEffect &Effect : Entry.second) {
        llvm::json::Array Guards;
        for (const auto &Guard : Effect.Guards)
          Guards.push_back(llvm::json::Object{
              {"bid", Guard.first}, {"then", Guard.second}});
        Result.push_back(llvm::json::Object{
            {"name", jsonText(Name)},
            {"path", jsonText(Effect.Path)},
            {"value", jsonText(Effect.Origin.Expression)},
            {"constant_value", Effect.ConstantValue
                                  ? llvm::json::Value(*Effect.ConstantValue)
                                  : llvm::json::Value(nullptr)},
            {"source_offset", Effect.SourceOffset},
            {"order", Effect.Order},
            {"operator", jsonText(Effect.Operation)},
            {"guards", std::move(Guards)},
            {"origin", origin(Effect.Origin)}});
      }
    }
    return Result;
  }

  llvm::json::Array returnEffects() const {
    llvm::json::Array Result;
    for (const ReturnEffect &Effect : ReturnEffects) {
      llvm::json::Array Guards;
      for (const auto &Guard : Effect.Guards)
        Guards.push_back(llvm::json::Object{
            {"bid", Guard.first}, {"then", Guard.second}});
      Result.push_back(llvm::json::Object{
          {"value", jsonText(Effect.Value)},
          {"constant_value", Effect.ConstantValue
                                ? llvm::json::Value(*Effect.ConstantValue)
                                : llvm::json::Value(nullptr)},
          {"source_offset", Effect.SourceOffset},
          {"order", Effect.Order},
          {"guards", std::move(Guards)},
          {"origin", Effect.Origin ? llvm::json::Value(origin(*Effect.Origin))
                                    : llvm::json::Value(nullptr)}});
    }
    return Result;
  }

  llvm::json::Array globalWriteEffects() const {
    llvm::json::Array Result;
    for (const GlobalWriteEffect &Effect : GlobalWriteEffects) {
      llvm::json::Array Guards;
      for (const auto &Guard : Effect.Guards)
        Guards.push_back(llvm::json::Object{
            {"bid", Guard.first}, {"then", Guard.second}});
      Result.push_back(llvm::json::Object{
          {"path", jsonText(Effect.Path)},
          {"value", jsonText(Effect.Value)},
          {"constant_value", Effect.ConstantValue
                                ? llvm::json::Value(*Effect.ConstantValue)
                                : llvm::json::Value(nullptr)},
          {"source_offset", Effect.SourceOffset},
          {"order", Effect.Order},
          {"guards", std::move(Guards)},
          {"origin", Effect.Origin ? llvm::json::Value(origin(*Effect.Origin))
                                    : llvm::json::Value(nullptr)}});
    }
    return Result;
  }

  llvm::json::Object enums() const {
    llvm::json::Object Result;
    for (const auto &Enum : Enums) {
      llvm::json::Object Members;
      for (const auto &Member : Enum.second)
        Members[jsonText(Member.first)] = Member.second;
      Result[jsonText(Enum.first)] = std::move(Members);
    }
    return Result;
  }

  llvm::json::Array controlVariables() const {
    llvm::json::Array Result;
    for (const auto &Entry : Controls) {
      ControlFact Fact = Entry.second;
      Fact.Origin = resolveOrigin(Fact.Variable, Fact.Range.getBegin());
      if (Fact.Origin) {
        if (Fact.Origin->Kind == "stub_return")
          Fact.Source = "stub";
        else if (Fact.Origin->Kind == "const_table_field" ||
                 Fact.Origin->Kind == "derived")
          Fact.Source = "derived";
        else if (Fact.Origin->Kind == "local_from_global")
          Fact.Source = "local_from_global";
      }
      llvm::json::Array BranchIds;
      for (const std::string &BranchId : Fact.BranchIds)
        BranchIds.push_back(BranchId);
      llvm::json::Value ValueOriginValue(nullptr);
      llvm::json::Object Extensions{
          {"canonical_var", jsonText(Fact.CanonicalVar)}};
      if (Fact.Origin) {
        llvm::json::Object Origin{
            {"kind", jsonText(Fact.Origin->Kind)},
             {"expression", jsonText(Fact.Origin->Expression)}};
        if (!Fact.Origin->Driver.empty())
          Origin["driver"] = jsonText(Fact.Origin->Driver);
        if (!Fact.Origin->Callee.empty())
          Origin["callee"] = jsonText(Fact.Origin->Callee);
        if (Fact.Origin->CallOffset >= 0) {
          Origin["call_offset"] = Fact.Origin->CallOffset;
          const int64_t Order = callOrder(Fact.Origin->CallOffset);
          if (Order >= 0)
            Origin["call_order"] = Order;
        }
        if (!Fact.Origin->Base.empty())
          Origin["base"] = jsonText(Fact.Origin->Base);
        if (!Fact.Origin->Index.empty())
          Origin["index"] = jsonText(Fact.Origin->Index);
        if (!Fact.Origin->Field.empty())
          Origin["field"] = jsonText(Fact.Origin->Field);
        ValueOriginValue = std::move(Origin);
      }
      Result.push_back(llvm::json::Object{
          {"name", jsonText(Fact.Name)},
          {"var", jsonText(Fact.Var)},
          {"source", Fact.Source},
          {"set_via", Fact.Origin && !Fact.Origin->Expression.empty()
                           ? llvm::json::Value(jsonText(Fact.Origin->Expression))
                           : llvm::json::Value(nullptr)},
          {"var_type", llvm::json::Value(jsonText(Fact.Type))},
          {"constant_value", llvm::json::Value(nullptr)},
          {"constant_reason", llvm::json::Value(nullptr)},
          {"branch_ids", std::move(BranchIds)},
          {"type_info", typeInfo(Fact.Variable->getType(), &Context)},
          {"value_origin", std::move(ValueOriginValue)},
          {"provenance", provenance(SM, Fact.Range, Fact.ASTKind,
                                     &LangOpts)},
          {"extensions", std::move(Extensions)}});
    }
    return Result;
  }

  llvm::json::Array memoryVariables() const {
    llvm::json::Array Result;
    for (const auto &Entry : Memories) {
      const MemoryFact &Fact = Entry.second;
      llvm::json::Object Extensions{
          {"address_source", "constant_pointer_macro"}};
      Result.push_back(llvm::json::Object{
          {"name", jsonText(Fact.Name)},
          {"address", Fact.Address},
          {"width", static_cast<int64_t>(Fact.Width)},
          {"read", Fact.Read},
          {"write", Fact.Write},
          {"conditional", Fact.Conditional},
          {"input_value", Fact.InputValue
                               ? llvm::json::Value(*Fact.InputValue)
                               : llvm::json::Value(nullptr)},
          {"expected_value", Fact.ExpectedValue
                                 ? llvm::json::Value(*Fact.ExpectedValue)
                                 : llvm::json::Value(nullptr)},
          {"provenance", provenance(SM, Fact.Range, "UnaryOperator",
                                     &LangOpts)},
          {"extensions", std::move(Extensions)}});
    }
    return Result;
  }

  llvm::json::Array globalObjects() const {
    llvm::json::Array Result;
    for (const auto &Entry : GlobalObjects) {
      const GlobalFact &Fact = Entry.second;
      llvm::json::Array ArraySizes;
      for (uint64_t Size : Fact.ArraySizes)
        ArraySizes.push_back(static_cast<int64_t>(Size));
      llvm::json::Array FieldPaths;
      for (const std::string &Path : Fact.FieldPaths)
        FieldPaths.push_back(jsonText(Path));
      llvm::json::Array FieldAccesses;
      const auto Recorded = GlobalFieldAccesses.find(Fact.Name);
      const auto &AccessMap = Recorded == GlobalFieldAccesses.end()
                                  ? Fact.FieldAccesses
                                  : Recorded->second;
      std::vector<std::pair<std::string, GlobalFieldAccess>> OrderedAccesses(
          AccessMap.begin(), AccessMap.end());
      std::sort(OrderedAccesses.begin(), OrderedAccesses.end(),
                [](const auto &Left, const auto &Right) {
                  if (Left.second.Line != Right.second.Line)
                    return Left.second.Line < Right.second.Line;
                  if (Left.second.Offset != Right.second.Offset)
                    return Left.second.Offset < Right.second.Offset;
                  return Left.first < Right.first;
                });
      for (const auto &Access : OrderedAccesses) {
        FieldAccesses.push_back(llvm::json::Object{
            {"path", jsonText(Access.first)},
            {"read", Access.second.Read},
            {"write", Access.second.Write},
            {"copied_from_local", Access.second.CopiedFromLocal},
            {"line", static_cast<int64_t>(Access.second.Line)},
            {"offset", static_cast<int64_t>(Access.second.Offset)},
            {"read_line", static_cast<int64_t>(Access.second.ReadLine)},
            {"read_offset", static_cast<int64_t>(Access.second.ReadOffset)},
            {"write_line", static_cast<int64_t>(Access.second.WriteLine)},
            {"write_offset", static_cast<int64_t>(Access.second.WriteOffset)}});
      }
      Result.push_back(llvm::json::Object{
          {"name", jsonText(Fact.Name)},
          {"read", Fact.Read},
          {"write", Fact.Write},
          {"is_const", Fact.IsConst},
          {"is_volatile", Fact.IsVolatile},
          {"is_union", Fact.IsUnion},
          {"source_file", jsonText(Fact.SourceFile)},
          {"array_sizes", std::move(ArraySizes)},
          {"field_paths", std::move(FieldPaths)},
          {"field_accesses", std::move(FieldAccesses)},
          {"read_line", static_cast<int64_t>(Fact.ReadLine)},
          {"read_offset", static_cast<int64_t>(Fact.ReadOffset)},
          {"write_line", static_cast<int64_t>(Fact.WriteLine)},
          {"write_offset", static_cast<int64_t>(Fact.WriteOffset)},
          {"provenance", provenance(SM, Fact.Range, "DeclRefExpr",
                                     &LangOpts)},
          {"extensions", emptyExtensions()}});
    }
    return Result;
  }

  llvm::json::Object config() const {
    llvm::json::Object Result;
    for (const auto &Entry : Macros)
      Result[jsonText(Entry.first)] = jsonText(Entry.second);
    return Result;
  }

  bool hasUnsupported() const { return HasUnsupported; }

  llvm::json::Array diagnostics() {
    llvm::json::Array Result;
    for (const auto &Diagnostic : Diagnostics)
      Result.push_back(Diagnostic);
    return Result;
  }

private:
  struct ComparisonView {
    const Expr *Variable = nullptr;
    const Expr *Boundary = nullptr;
    std::string Op;
  };

  struct LoopCounter {
    const VarDecl *Variable = nullptr;
    int64_t Start = 0;
    int64_t Step = 0;
  };

  bool sourceRangeContains(SourceRange Outer, SourceRange Inner) const {
    if (Outer.isInvalid() || Inner.isInvalid())
      return false;
    // Macro operands in embedded C commonly spell in a header while their
    // statement belongs to the target function.  Containment is a control
    // flow fact, so compare expansion locations; spelling locations would
    // incorrectly discard guards for assignments such as ``x = U1G_OFF``.
    const SourceLocation OuterBegin = SM.getExpansionLoc(Outer.getBegin());
    const SourceLocation OuterEnd = SM.getExpansionLoc(Outer.getEnd());
    const SourceLocation InnerBegin = SM.getExpansionLoc(Inner.getBegin());
    const SourceLocation InnerEnd = SM.getExpansionLoc(Inner.getEnd());
    if (OuterBegin.isInvalid() || OuterEnd.isInvalid() ||
        InnerBegin.isInvalid() || InnerEnd.isInvalid() ||
        SM.getFileID(OuterBegin) != SM.getFileID(InnerBegin) ||
        SM.getFileID(OuterEnd) != SM.getFileID(InnerEnd))
      return false;
    return SM.getFileOffset(OuterBegin) <= SM.getFileOffset(InnerBegin) &&
           SM.getFileOffset(InnerEnd) <= SM.getFileOffset(OuterEnd);
  }

  llvm::json::Array callGuards(const CallExpr *Call) const {
    llvm::json::Array Result;
    const Stmt *Current = Call;
    for (unsigned Depth = 0; Depth < 64; ++Depth) {
      auto Parents = Context.getParents(*Current);
      if (Parents.empty())
        break;
      const DynTypedNode &Parent = Parents[0];
      if (const auto *If = Parent.get<IfStmt>()) {
        const auto *Comparison = dyn_cast_or_null<BinaryOperator>(
            If->getCond()->IgnoreParenImpCasts());
        if (Comparison) {
          const ComparisonView View = comparisonView(Comparison);
          const auto *Member = dyn_cast_or_null<MemberExpr>(
              View.Variable ? View.Variable->IgnoreParenImpCasts() : nullptr);
          const auto *Subscript = Member
                                      ? dyn_cast<ArraySubscriptExpr>(
                                            Member->getBase()
                                                ->IgnoreParenImpCasts())
                                      : nullptr;
          const VarDecl *Global = Subscript
                                      ? referencedVar(Subscript)
                                      : nullptr;
          const VarDecl *Index = Subscript
                                     ? referencedVar(Subscript->getIdx())
                                     : nullptr;
          const auto Boundary = constantInteger(View.Boundary, Context);
          const std::string FullPath = Member ? memberPath(Member) : "";
          const std::string Prefix =
              Global ? jsonText(Global->getNameAsString()) + "." : "";
          const bool InThen = If->getThen() && sourceRangeContains(
              If->getThen()->getSourceRange(), Call->getSourceRange());
          const bool InElse = If->getElse() && sourceRangeContains(
              If->getElse()->getSourceRange(), Call->getSourceRange());
          if (Global && Index && isExternalGlobal(Global) && Boundary &&
              !View.Op.empty() && !Prefix.empty() &&
              FullPath.rfind(Prefix, 0) == 0 && (InThen || InElse)) {
            Result.push_back(llvm::json::Object{
                {"global", jsonText(Global->getNameAsString())},
                {"index_var", jsonText(Index->getNameAsString())},
                {"field", jsonText(FullPath.substr(Prefix.size()))},
                {"op", View.Op},
                {"boundary", *Boundary},
                {"then", InThen}});
          }
        }
      }
      if (const auto *ParentStmt = Parent.get<Stmt>()) {
        Current = ParentStmt;
        continue;
      }
      if (const auto *ParentExpr = Parent.get<Expr>()) {
        Current = ParentExpr;
        continue;
      }
      break;
    }
    return Result;
  }

  std::optional<LoopCounter> loopCounter(const ForStmt *Loop) const {
    const VarDecl *Variable = nullptr;
    std::optional<int64_t> Start;
    if (const Stmt *Init = Loop->getInit()) {
      if (const auto *Decls = dyn_cast<DeclStmt>(Init)) {
        for (auto It = Decls->decl_begin(); It != Decls->decl_end(); ++It) {
          if (const auto *Candidate = dyn_cast<VarDecl>(*It)) {
            if (Candidate->hasInit()) {
              Variable = Candidate;
              Start = constantInteger(Candidate->getInit(), Context);
              break;
            }
          }
        }
      } else if (const auto *Assignment = dyn_cast<BinaryOperator>(Init)) {
        if (Assignment->isAssignmentOp()) {
          Variable = referencedVar(Assignment->getLHS());
          Start = constantInteger(Assignment->getRHS(), Context);
        }
      }
    }
    if (!Variable || !Start || !Loop->getCond() || !Loop->getInc())
      return std::nullopt;

    int64_t Step = 0;
    const Expr *Increment = Loop->getInc()->IgnoreParenImpCasts();
    if (const auto *Unary = dyn_cast<UnaryOperator>(Increment)) {
      if (Unary->isIncrementDecrementOp() &&
          referencedVar(Unary->getSubExpr()) == Variable)
        Step = Unary->getOpcode() == UO_PreInc ||
                       Unary->getOpcode() == UO_PostInc
                   ? 1
                   : -1;
    } else if (const auto *Assignment = dyn_cast<BinaryOperator>(Increment)) {
      if (Assignment->isAssignmentOp() &&
          referencedVar(Assignment->getLHS()) == Variable) {
        if (Assignment->getOpcode() == BO_AddAssign)
          Step = constantInteger(Assignment->getRHS(), Context).value_or(0);
        else if (Assignment->getOpcode() == BO_SubAssign)
          Step = -constantInteger(Assignment->getRHS(), Context).value_or(0);
      }
    }
    if (Step == 0)
      return std::nullopt;
    return LoopCounter{Variable, *Start, Step};
  }

  std::optional<int64_t> forTripCount(const ForStmt *Loop) const {
    auto Counter = loopCounter(Loop);
    if (!Counter)
      return std::nullopt;
    const auto *Condition =
        dyn_cast<BinaryOperator>(Loop->getCond()->IgnoreParenImpCasts());
    if (!Condition)
      return std::nullopt;
    const VarDecl *Variable = Counter->Variable;
    const Expr *BoundExpr = nullptr;
    std::string Op;
    if (referencedVar(Condition->getLHS()) == Variable) {
      BoundExpr = Condition->getRHS();
      Op = comparisonOp(Condition->getOpcode());
    } else if (referencedVar(Condition->getRHS()) == Variable) {
      BoundExpr = Condition->getLHS();
      Op = comparisonOp(Condition->getOpcode());
      static const std::map<std::string, std::string> Inverted{
          {"<", ">"}, {"<=", ">="}, {">", "<"}, {">=", "<="}};
      auto It = Inverted.find(Op);
      if (It != Inverted.end())
        Op = It->second;
    }
    auto Bound = constantInteger(BoundExpr, Context);
    if (!Bound || Op.empty())
      return std::nullopt;

    const bool Positive = Counter->Step > 0;
    const bool DirectionMatches =
        Positive ? (Op == "<" || Op == "<=") : (Op == ">" || Op == ">=");
    if (!DirectionMatches)
      return std::nullopt;
    const int64_t Distance = Positive
                                 ? *Bound - Counter->Start
                                 : Counter->Start - *Bound;
    if (Distance < 0)
      return int64_t{0};
    const int64_t Magnitude = Positive ? Counter->Step : -Counter->Step;
    const int64_t Quotient = Distance / Magnitude;
    const bool Remainder = Distance % Magnitude != 0;
    if (Op == "<" || Op == ">")
      return Quotient + (Remainder ? 1 : 0);
    return Quotient + 1;
  }

  int64_t callCapacity(const CallExpr *Call) const {
    int64_t Capacity = 1;
    const Stmt *Current = Call;
    for (unsigned Depth = 0; Depth < 64; ++Depth) {
      auto Parents = Context.getParents(*Current);
      if (Parents.empty())
        break;
      const DynTypedNode &Parent = Parents[0];
      if (const auto *Loop = Parent.get<ForStmt>()) {
        if (auto Count = forTripCount(Loop))
          Capacity *= std::max<int64_t>(1, *Count);
      }
      if (const auto *ParentStmt = Parent.get<Stmt>()) {
        Current = ParentStmt;
        continue;
      }
      if (const auto *ParentExpr = Parent.get<Expr>()) {
        Current = ParentExpr;
        continue;
      }
      break;
    }
    return Capacity;
  }

  bool returnValueUsed(const CallExpr *Call) const {
    const Stmt *Current = Call;
    for (unsigned Depth = 0; Depth < 32; ++Depth) {
      auto Parents = Context.getParents(*Current);
      if (Parents.empty())
        return true;
      const DynTypedNode &Parent = Parents[0];
      if (const auto *Cast = Parent.get<CastExpr>()) {
        if (Cast->getType()->isVoidType())
          return false;
        Current = Cast;
        continue;
      }
      if (Parent.get<ParenExpr>() || Parent.get<ExprWithCleanups>() ||
          Parent.get<MaterializeTemporaryExpr>() ||
          Parent.get<CXXBindTemporaryExpr>() || Parent.get<ConstantExpr>()) {
        Current = Parent.get<Stmt>();
        if (!Current)
          Current = Parent.get<Expr>();
        if (!Current)
          return true;
        continue;
      }
      if (Parent.get<CompoundStmt>() || Parent.get<NullStmt>())
        return false;
      return true;
    }
    return true;
  }

  std::vector<std::string> argumentFieldPaths(const Expr *Argument) const {
    const VarDecl *Root = referencedVar(Argument);
    if (!Root)
      return {};
    auto It = LocalFieldPaths.find(Root);
    if (It == LocalFieldPaths.end())
      return {};
    std::vector<std::string> Candidates;
    const std::string Prefix = jsonText(Root->getNameAsString()) + ".";
    for (const std::string &Access : It->second) {
      // The caller-side field uses are the observable stub shape.  Do not
      // restrict them by source offset: a field consumed after the call is
      // exactly the common pointer-output case, and a local can be reused by
      // more than one call site.
      if (Access.rfind(Prefix, 0) != 0)
        continue;
      const std::string Relative = Access.substr(Prefix.size());
      if (std::find(Candidates.begin(), Candidates.end(), Relative) ==
          Candidates.end())
        Candidates.push_back(Relative);
    }
    std::vector<std::string> Leaves;
    for (const std::string &Candidate : Candidates) {
      const std::string PrefixCandidate = Candidate + ".";
      const bool HasChild = std::any_of(
          Candidates.begin(), Candidates.end(), [&](const std::string &Other) {
            return Other.rfind(PrefixCandidate, 0) == 0;
          });
      if (!HasChild)
        Leaves.push_back(Candidate);
    }
    return Leaves;
  }

  const Expr *simpleVariableExpr(const Expr *Expression) const {
    if (!Expression)
      return nullptr;
    // Explicit C-style casts are common around bit-field assignments in the
    // embedded sources.  Strip them too, otherwise a casted const-table
    // member is mistaken for an unresolvable expression while an otherwise
    // identical uncast member is tracked correctly.
    Expression = Expression->IgnoreParenCasts();
    if (const auto *And = dyn_cast<BinaryOperator>(Expression)) {
      if (And->getOpcode() == BO_And && referencedVar(And->getLHS()))
        return And->getLHS();
    }
    return referencedVar(Expression) ? Expression : nullptr;
  }

  std::string variableText(const Expr *Expression) const {
    if (!Expression)
      return {};
    if (const auto *And = dyn_cast<BinaryOperator>(
            Expression->IgnoreParenImpCasts())) {
      if (And->getOpcode() == BO_And)
        Expression = And->getLHS();
    }
    std::string Text = text(Expression->getSourceRange(), true);
    if (!Text.empty())
      return Text;
    if (const VarDecl *Variable = referencedVar(Expression))
      return jsonText(Variable->getNameAsString());
    return {};
  }

  ComparisonView comparisonView(const BinaryOperator *Comparison) const {
    const Expr *Left = Comparison->getLHS();
    const Expr *Right = Comparison->getRHS();
    const Expr *LeftVariable = simpleVariableExpr(Left);
    const Expr *RightVariable = simpleVariableExpr(Right);
    const bool LeftConstant = constantInteger(Left, Context).has_value();
    const bool RightConstant = constantInteger(Right, Context).has_value();

    ComparisonView View;
    if (LeftVariable && !LeftConstant) {
      View.Variable = LeftVariable;
      View.Boundary = Right;
    } else if (RightVariable) {
      View.Variable = RightVariable;
      View.Boundary = Left;
      View.Op = comparisonOp(Comparison->getOpcode());
      if (LeftConstant && !RightConstant) {
        static const std::map<std::string, std::string> Inverted{
            {"<", ">"}, {"<=", ">="}, {">", "<"}, {">=", "<="}};
        auto It = Inverted.find(View.Op);
        if (It != Inverted.end())
          View.Op = It->second;
      }
      return View;
    } else if (LeftVariable) {
      View.Variable = LeftVariable;
      View.Boundary = Right;
    }
    View.Op = comparisonOp(Comparison->getOpcode());
    return View;
  }

  std::string text(SourceRange Range, bool Spelling) const {
    return sourceText(SM, LangOpts, Range, Spelling);
  }

  bool parameterIsWritten(const ParmVarDecl *Parameter,
                          const Stmt *Body) const {
    if (!Parameter || !Body)
      return false;
    ParameterWriteVisitor Visitor(Parameter);
    Visitor.TraverseStmt(const_cast<Stmt *>(Body));
    return Visitor.written();
  }

  llvm::json::Object atom(const BinaryOperator *Comparison) const {
    const Expr *Left = Comparison->getLHS();
    const Expr *Right = Comparison->getRHS();
    const ComparisonView View = comparisonView(Comparison);
    const VarDecl *Variable = referencedVar(View.Variable);
    const Expr *BoundaryExpr = View.Boundary;
    std::optional<int64_t> Boundary = constantInteger(BoundaryExpr, Context);
    std::optional<int64_t> Mask;
    for (const Expr *Candidate : {Left, Right}) {
      const Expr *Masked = Candidate ? Candidate->IgnoreParenImpCasts() : nullptr;
      if (!Masked)
        continue;
      if (const auto *And = dyn_cast<BinaryOperator>(Masked->IgnoreParenCasts())) {
        if (And->getOpcode() == BO_And && referencedVar(And->getLHS())) {
          Mask = constantInteger(And->getRHS(), Context);
          if (Mask)
            break;
        }
      }
    }
    std::vector<std::string> Qualifiers;
    std::string Type;
    if (Variable) {
      QualType VarType = Variable->getType();
      Type = jsonText(VarType.getAsString());
      if (VarType.isConstQualified())
        Qualifiers.push_back("const");
      if (VarType.isVolatileQualified())
        Qualifiers.push_back("volatile");
    }
    llvm::json::Array QualifierArray;
    for (const std::string &Qualifier : Qualifiers)
      QualifierArray.push_back(Qualifier);
    std::string Op = View.Op;
    std::string VariableSpelling = variableText(View.Variable);
    std::string BoundarySpelling = BoundaryExpr
                                       ? text(BoundaryExpr->getSourceRange(), true)
                                       : std::string();
    llvm::json::Object Extensions;
    Extensions["canonical_var"] =
        View.Variable ? accessPath(View.Variable, Context) : std::string();
    llvm::json::Object Result{
        {"var", Variable ? jsonText(VariableSpelling)
                          : text(Left->getSourceRange(), true)},
        {"var_type", Variable ? llvm::json::Value(Type) : llvm::json::Value(nullptr)},
        {"op", Op},
        {"right", Boundary ? llvm::json::Value(nullptr)
                            : llvm::json::Value(
                                  Right && referencedVar(Right)
                                      ? accessPath(Right, Context)
                                      : (Right ? text(Right->getSourceRange(), true)
                                               : std::string()))},
        {"boundary", Boundary ? llvm::json::Value(*Boundary)
                                : llvm::json::Value(nullptr)},
        {"boundary_name", llvm::json::Value(nullptr)},
        {"text", prettyText(Comparison, LangOpts)},
        {"mask", Mask ? llvm::json::Value(*Mask) : llvm::json::Value(nullptr)},
        {"cond_text_spelling", text(Comparison->getSourceRange(), true)},
        {"cond_text_expanded", prettyText(Comparison, LangOpts)},
        {"type_spelling", Variable ? llvm::json::Value(Type)
                                     : llvm::json::Value(nullptr)},
          {"canonical_type", Variable ? llvm::json::Value(
                                            jsonText(Variable->getType()
                                                         .getCanonicalType()
                                                         .getAsString()))
                                     : llvm::json::Value(nullptr)},
          {"qualifiers", std::move(QualifierArray)},
          {"type_info", typeInfo(Variable ? Variable->getType() : QualType(),
                                  &Context)},
          {"provenance", provenance(SM, Comparison->getSourceRange(),
                                   "BinaryOperator", &LangOpts)},
        {"extensions", std::move(Extensions)}};
    if (BoundaryExpr) {
      if (const auto *Ref = dyn_cast<DeclRefExpr>(
              BoundaryExpr->IgnoreParenImpCasts())) {
        if (const auto *Enum = dyn_cast<EnumConstantDecl>(Ref->getDecl())) {
          Result["boundary_name"] = jsonText(Enum->getNameAsString());
        }
      }
    }
    if (!BoundarySpelling.empty()) {
      auto It = Macros.find(BoundarySpelling);
      if (It != Macros.end())
        Result["boundary_name"] = jsonText(BoundarySpelling);
    }
    if (const auto Macro = immediateMacro(SM, LangOpts,
                                          BoundaryExpr
                                              ? BoundaryExpr->getBeginLoc()
                                              : SourceLocation())) {
      if (Macros.find(*Macro) != Macros.end())
        Result["boundary_name"] = jsonText(*Macro);
    }
    return Result;
  }

  const Expr *comparisonVariable(const BinaryOperator *Comparison) const {
    return comparisonView(Comparison).Variable;
  }

  bool isExternalGlobal(const VarDecl *Variable) const {
    // ``hasGlobalStorage`` is also true for a function-scope static. Such a
    // declaration remains implementation-local and must not become a
    // WinAMS input/output column. Only namespace/file-scope storage is an
    // external global fact for the testcase contract.
    return Variable && Variable->hasGlobalStorage() &&
           !Variable->isLocalVarDecl();
  }

  bool isConstObject(QualType Type) const {
    while (const auto *Array = Context.getAsConstantArrayType(Type))
      Type = Array->getElementType();
    return Type.isConstQualified();
  }

  std::optional<ValueOrigin> expressionOrigin(const Expr *Expression) const {
    if (!Expression)
      return std::nullopt;
    // Explicit C-style casts are common around bit-field assignments in the
    // embedded sources.  Strip them too, otherwise a casted const-table
    // member is mistaken for an unresolvable expression while an otherwise
    // identical uncast member is tracked correctly.
    Expression = Expression->IgnoreParenCasts();
    ValueOrigin Origin;
    Origin.Expression = text(Expression->getSourceRange(), true);

    if (const auto *Call = dyn_cast<CallExpr>(Expression)) {
      if (const FunctionDecl *Direct = Call->getDirectCallee()) {
        Origin.Kind = "stub_return";
        Origin.Callee = jsonText(Direct->getNameAsString());
        Origin.CallOffset = static_cast<int64_t>(
            SM.getFileOffset(SM.getSpellingLoc(Call->getExprLoc())));
        return Origin;
      }
      return std::nullopt;
    }

    if (const auto *Member = dyn_cast<MemberExpr>(Expression)) {
      const Expr *Base = Member->getBase()->IgnoreParenImpCasts();
      if (const auto *Subscript = dyn_cast<ArraySubscriptExpr>(Base)) {
        const VarDecl *Table = referencedVar(Subscript);
        if (Table && isExternalGlobal(Table) && isConstObject(Table->getType())) {
          Origin.Kind = "const_table_field";
          Origin.Base = jsonText(Table->getNameAsString());
          Origin.Index = text(Subscript->getIdx()->getSourceRange(), true);
          Origin.Field = memberPath(Member);
          const std::string Prefix = Origin.Base + ".";
          if (Origin.Field.rfind(Prefix, 0) == 0)
            Origin.Field = Origin.Field.substr(Prefix.size());
          Origin.DriverDecl = referencedVar(Subscript->getIdx());
          if (Origin.DriverDecl)
            Origin.Driver = jsonText(Origin.DriverDecl->getNameAsString());
          return Origin;
        }
      }
      // Cast-heavy embedded C expressions can hide the array subscript from
      // the direct-base test above.  The AST still retains the table symbol
      // and complete member spelling; recover the same const-table origin
      // from that semantic path instead of losing the local field value.
      const VarDecl *Table = referencedVar(Member);
      if (Table && isExternalGlobal(Table) &&
          isConstObject(Table->getType())) {
        const std::string FullPath = memberPath(Member);
        const std::string Prefix = Table->getNameAsString() + "[";
        if (FullPath.rfind(Prefix, 0) == 0) {
          const size_t Close = FullPath.find(']', Prefix.size());
          if (Close != std::string::npos && Close + 2 <= FullPath.size() &&
              FullPath[Close + 1] == '.') {
            Origin.Kind = "const_table_field";
            Origin.Base = jsonText(Table->getNameAsString());
            Origin.Index = FullPath.substr(Prefix.size(),
                                           Close - Prefix.size());
            Origin.Field = FullPath.substr(Close + 2);
            Origin.DriverDecl = referencedVar(
                Member->getBase()->IgnoreParenImpCasts());
            if (const auto *Subscript = dyn_cast<ArraySubscriptExpr>(Base))
              Origin.DriverDecl = referencedVar(Subscript->getIdx());
            if (Origin.DriverDecl)
              Origin.Driver = jsonText(Origin.DriverDecl->getNameAsString());
            return Origin;
          }
        }
      }
    }

    const VarDecl *Variable = referencedVar(Expression);
    if (!Variable)
      return std::nullopt;
    Origin.DriverDecl = Variable;
    Origin.Driver = jsonText(Variable->getNameAsString());
    if (isa<ParmVarDecl>(Variable))
      Origin.Kind = "param";
    else if (isExternalGlobal(Variable))
      Origin.Kind = "global";
    else
      Origin.Kind = "local";
    return Origin;
  }

  void recordLocalOrigin(const VarDecl *Variable, const Expr *Expression,
                         SourceLocation Location,
                         const Stmt *Statement = nullptr,
                         std::string Path = {},
                         std::string Operation = "=") {
    if (!Variable || isa<ParmVarDecl>(Variable) ||
        !Variable->isLocalVarDecl())
      return;
    auto Origin = expressionOrigin(Expression);
    const std::optional<int64_t> Constant =
        constantInteger(Expression, Context);
    if (!Origin && !Constant)
      return;
    if (!Origin) {
      ValueOrigin ConstantOrigin;
      ConstantOrigin.Kind = "constant";
      ConstantOrigin.Expression = text(Expression->getSourceRange(), true);
      Origin = std::move(ConstantOrigin);
    }
    if (Origin->Expression.empty())
      Origin->Expression = prettyText(Expression, LangOpts);
    if (Origin->Kind == "global" && Origin->DriverDecl)
      ensureGlobalObjectFact(Origin->DriverDecl, false,
                             Expression->getSourceRange(),
                             Expression->getBeginLoc());
    Origin->SourceOffset = static_cast<int64_t>(
        SM.getFileOffset(SM.getExpansionLoc(Location)));
    Origin->CallOffset = Origin->CallOffset >= 0
                             ? Origin->CallOffset
                             : Origin->SourceOffset;
    std::vector<std::pair<std::string, bool>> Guards =
        Statement ? activeGuards(Statement)
                  : std::vector<std::pair<std::string, bool>>{};
    LocalValueEffects[Variable].push_back(LocalValueEffect{
        *Origin, Constant, Origin->SourceOffset, std::move(Path),
        std::move(Operation),
        std::move(Guards), NextEffectOrder++});
    LocalOrigins[Variable].push_back(std::move(*Origin));
  }

  void recordLocalStubOutputOrigin(const VarDecl *Variable,
                                   const CallExpr *Call, unsigned Index,
                                   llvm::StringRef CalleeName) {
    if (!Variable || !Call || !Variable->isLocalVarDecl())
      return;
    ValueOrigin Origin;
    // Indirect calls are resolved after the AST visitor has emitted the
    // function JSON.  Keep a local pending marker so the final target pass
    // can promote only resolved Rte_Read calls to a real stub parameter.
    Origin.Kind = CalleeName.empty() ? "indirect_param" : "stub_param";
    Origin.Expression = prettyText(Call, LangOpts);
    if (!CalleeName.empty())
      Origin.Callee = jsonText(CalleeName.str());
    Origin.Index = std::to_string(Index);
    // ``callOrder`` is keyed by the call's spelling location in the call
    // table, while source sequencing uses expansion locations.  Preserve
    // both roles so the local origin can resolve the correct PTROUT slot.
    Origin.CallOffset = static_cast<int64_t>(SM.getFileOffset(
        SM.getSpellingLoc(Call->getExprLoc())));
    Origin.SourceOffset = static_cast<int64_t>(SM.getFileOffset(
        SM.getExpansionLoc(Call->getExprLoc())));
    LocalValueEffects[Variable].push_back(LocalValueEffect{
        Origin, std::nullopt, Origin.SourceOffset, "", "=",
        activeGuards(Call), NextEffectOrder++});
    LocalOrigins[Variable].push_back(std::move(Origin));
  }

  std::optional<ValueOrigin> originAt(const VarDecl *Variable,
                                      SourceLocation UseLocation) const {
    if (!Variable)
      return std::nullopt;
    if (isa<ParmVarDecl>(Variable)) {
      ValueOrigin Origin;
      Origin.Kind = "param";
      Origin.Driver = jsonText(Variable->getNameAsString());
      Origin.DriverDecl = Variable;
      return Origin;
    }
    if (isExternalGlobal(Variable)) {
      ValueOrigin Origin;
      Origin.Kind = "global";
      Origin.Driver = jsonText(Variable->getNameAsString());
      Origin.DriverDecl = Variable;
      return Origin;
    }
    auto It = LocalOrigins.find(Variable);
    if (It == LocalOrigins.end() || It->second.empty())
      return std::nullopt;
    const int64_t UseOffset = static_cast<int64_t>(
        SM.getFileOffset(SM.getExpansionLoc(UseLocation)));
    const ValueOrigin *Selected = nullptr;
    for (const ValueOrigin &Candidate : It->second) {
      const int64_t CandidateOffset = Candidate.SourceOffset >= 0
                                          ? Candidate.SourceOffset
                                          : Candidate.CallOffset;
      const int64_t SelectedOffset =
          Selected ? (Selected->SourceOffset >= 0 ? Selected->SourceOffset
                                                  : Selected->CallOffset)
                   : -1;
      if (CandidateOffset <= UseOffset &&
          (!Selected || CandidateOffset > SelectedOffset))
        Selected = &Candidate;
    }
    return Selected ? std::optional<ValueOrigin>(*Selected) : std::nullopt;
  }

  std::optional<ValueOrigin> resolveOrigin(
      const VarDecl *Variable, SourceLocation UseLocation,
      std::set<const VarDecl *> Seen = {}) const {
    if (!Variable || !Seen.insert(Variable).second)
      return std::nullopt;
    auto Origin = originAt(Variable, UseLocation);
    if (!Origin || !Origin->DriverDecl || Origin->DriverDecl == Variable)
      return Origin;
    if (Origin->Kind == "local") {
      auto Upstream = resolveOrigin(Origin->DriverDecl, UseLocation, Seen);
      if (Upstream)
        return Upstream;
    }
    if (Origin->Kind == "global")
      Origin->Kind = "local_from_global";
    else if (Origin->Kind == "param")
      Origin->Kind = "derived";
    return Origin;
  }

  int64_t callOrder(int64_t Offset) const {
    for (size_t Index = 0; Index < Calls.size(); ++Index) {
      const auto *Call = Calls[Index].getAsObject();
      if (!Call)
        continue;
      const auto *Provenance = Call->getObject("provenance");
      const auto *Spelling = Provenance
                                 ? Provenance->getObject("spelling")
                                 : nullptr;
      const auto CallOffset = Spelling ? Spelling->getInteger("offset")
                                       : std::optional<int64_t>();
      if (CallOffset && *CallOffset == Offset)
        return static_cast<int64_t>(Index);
    }
    return -1;
  }

  std::vector<std::pair<std::string, bool>> activeGuards(
      const Stmt *Statement) const {
    std::vector<std::pair<std::string, bool>> Result;
    const Stmt *Current = Statement;
    for (unsigned Depth = 0; Depth < 64 && Current; ++Depth) {
      auto Parents = Context.getParents(*Current);
      if (Parents.empty())
        break;
      const DynTypedNode &Parent = Parents[0];
      if (const auto *If = Parent.get<IfStmt>()) {
        auto Branch = BranchIds.find(If);
        if (Branch != BranchIds.end()) {
          const bool InThen = If->getThen() && sourceRangeContains(
              If->getThen()->getSourceRange(), Statement->getSourceRange());
          const bool InElse = If->getElse() && sourceRangeContains(
              If->getElse()->getSourceRange(), Statement->getSourceRange());
          if (InThen || InElse)
            Result.emplace_back(Branch->second, InThen);
        }
      }
      if (const auto *ParentStmt = Parent.get<Stmt>()) {
        Current = ParentStmt;
        continue;
      }
      if (const auto *ParentExpr = Parent.get<Expr>()) {
        Current = ParentExpr;
        continue;
      }
      break;
    }
    std::reverse(Result.begin(), Result.end());
    return Result;
  }

  void recordParameterWriteEffect(const ParmVarDecl *Parameter,
                                  const Expr *Lhs, const Expr *Rhs) {
    if (!Parameter || !Lhs || !Rhs)
      return;
    const std::string Path = accessPath(Lhs, Context);
    if (Path.empty() || Path == jsonText(Parameter->getNameAsString()))
      return;
    ParamWriteEffects[Parameter].push_back(ParamWriteEffect{
        Path, text(Rhs->getSourceRange(), true), constantInteger(Rhs, Context),
        activeGuards(Lhs), NextEffectOrder++});
  }

  void recordGlobalWriteEffect(const Expr *Lhs, const Expr *Rhs,
                               const BinaryOperator *Operator) {
    if (!Lhs || !Rhs || !Operator)
      return;
    const std::string Path = accessPath(Lhs, Context);
    if (Path.empty())
      return;
    GlobalWriteEffect Effect;
    Effect.Path = Path;
    Effect.Value = text(Rhs->getSourceRange(), true);
    if (Effect.Value.empty())
      Effect.Value = prettyText(Rhs, LangOpts);
    Effect.ConstantValue = constantInteger(Rhs, Context);
    Effect.Origin = expressionOrigin(Rhs);
    Effect.SourceOffset = static_cast<int64_t>(SM.getFileOffset(
        SM.getExpansionLoc(Lhs->getBeginLoc())));
    Effect.Guards = activeGuards(Operator);
    if (Effect.Guards.empty())
      Effect.Guards = activeGuards(Lhs);
    Effect.Order = NextEffectOrder++;
    GlobalWriteEffects.push_back(std::move(Effect));
  }

  void recordParameterAccess(const Expr *Expression) {
    const auto *Parameter = dyn_cast_or_null<ParmVarDecl>(
        referencedVar(Expression));
    if (!Parameter)
      return;
    std::string Path = accessPath(Expression, Context);
    if (Path.empty())
      return;
    const bool Write = isWriteLValue(Expression);
    auto &Accesses = ParameterAccesses[Parameter];
    auto It = std::find_if(
        Accesses.begin(), Accesses.end(),
        [&](const ParamAccess &Access) { return Access.Path == Path; });
    if (It == Accesses.end()) {
      Accesses.push_back(ParamAccess{
          Path, !Write, Write,
          SM.getFileOffset(SM.getSpellingLoc(Expression->getBeginLoc()))});
    } else {
      It->Read = It->Read || !Write;
      It->Write = It->Write || Write;
    }
  }

  void ensureGlobalObjectFact(const VarDecl *Variable, bool Written,
                              SourceRange Range, SourceLocation Location) {
    if (!isExternalGlobal(Variable))
      return;
    const std::string Name = jsonText(Variable->getNameAsString());
    GlobalsUsed.insert(Name);
    if (Written)
      GlobalWrites.insert(Name);
    auto It = GlobalObjects.find(Name);
    if (It == GlobalObjects.end()) {
      GlobalFact Fact;
      Fact.Name = Name;
      Fact.Read = !Written;
      Fact.Write = Written;
      Fact.Range = Range;
      QualType Type = Variable->getType();
      while (const auto *Array = Context.getAsConstantArrayType(Type)) {
        Fact.ArraySizes.push_back(Array->getSize().getZExtValue());
        Type = Array->getElementType();
      }
      Fact.FieldPaths = recordLeafPaths(Type, Context);
      if (const auto *Record = Type->getAs<RecordType>())
        Fact.IsUnion = Record->getDecl()->isUnion();
      Fact.IsConst = Variable->getType().isConstQualified();
      Fact.IsVolatile = Variable->getType().isVolatileQualified();
      Fact.SourceFile = locationFile(SM, SM.getSpellingLoc(
          Variable->getLocation()));
      GlobalObjects.emplace(Name, std::move(Fact));
      It = GlobalObjects.find(Name);
    } else {
      It->second.Read = It->second.Read || !Written;
      It->second.Write = It->second.Write || Written;
    }
    SourceLocation AccessLocation = SM.getExpansionLoc(Location);
    const unsigned Line = SM.getSpellingLineNumber(AccessLocation);
    const unsigned Offset = SM.getFileOffset(AccessLocation);
    GlobalFact &Fact = It->second;
    if (Written) {
      if (Fact.WriteLine == 0 || Offset < Fact.WriteOffset) {
        Fact.WriteLine = Line;
        Fact.WriteOffset = Offset;
      }
    } else if (Fact.ReadLine == 0 || Offset < Fact.ReadOffset) {
      Fact.ReadLine = Line;
      Fact.ReadOffset = Offset;
    }
  }

  void recordGlobalFieldAccess(const Expr *Expression) {
    const VarDecl *Variable = referencedVar(Expression);
    if (!isExternalGlobal(Variable))
      return;
    const bool Write = isWriteLValue(Expression);
    ensureGlobalObjectFact(Variable, Write, Expression->getSourceRange(),
                           Expression->getBeginLoc());
    const std::string Name = jsonText(Variable->getNameAsString());
    const std::string FullPath = memberPath(Expression);
    const std::string Prefix = Name + ".";
    std::string Relative;
    if (FullPath == Name)
      Relative = "";
    else if (FullPath.rfind(Prefix, 0) == 0)
      Relative = FullPath.substr(Prefix.size());
    else
      return;
    recordGlobalFieldAccessPath(
        Variable, Relative, Write, Expression->getSourceRange(),
        Expression->getBeginLoc());
  }

  void recordGlobalFieldAccessPath(const VarDecl *Variable,
                                   const std::string &Relative,
                                   bool Write, SourceRange Range,
                                   SourceLocation Location,
                                   bool CopiedFromLocal = false,
                                   unsigned Sequence = 0) {
    if (!isExternalGlobal(Variable))
      return;
    const std::string Name = jsonText(Variable->getNameAsString());
    ensureGlobalObjectFact(Variable, Write, Range, Location);
    auto &Access = GlobalFieldAccesses[Name][Relative];
    Access.Read = Access.Read || !Write;
    Access.Write = Access.Write || Write;
    Access.CopiedFromLocal = Access.CopiedFromLocal || CopiedFromLocal;
    SourceLocation MemberLocation = Location;
    // A PAL/EXP accessor is normally a macro defined in an export header.
    // Its spelling location is useful provenance, but it is not the order in
    // which the tested function observes the global.  For event ordering use
    // the expansion location, which points back to the call in the target
    // function.  Direct source accesses are unchanged by this conversion.
    MemberLocation = SM.getExpansionLoc(MemberLocation);
    if (Access.Line == 1 && Access.Offset == 0) {
      Access.Line = SM.getSpellingLineNumber(MemberLocation);
      Access.Offset = SM.getFileOffset(MemberLocation);
      if (CopiedFromLocal)
        Access.Offset += Sequence;
    }
    const unsigned Line = SM.getSpellingLineNumber(MemberLocation);
    const unsigned Offset = SM.getFileOffset(MemberLocation);
    if (Write) {
      if (Access.WriteLine == 0 || Offset < Access.WriteOffset) {
        Access.WriteLine = Line;
        Access.WriteOffset = Offset;
      }
    } else if (Access.ReadLine == 0 || Offset < Access.ReadOffset) {
      Access.ReadLine = Line;
      Access.ReadOffset = Offset;
    }
  }

  void registerControlVariable(const Expr *Expression,
                               llvm::StringRef BranchId, SourceRange Range,
                               llvm::StringRef ASTKind = "BinaryOperator") {
    const VarDecl *Variable = referencedVar(Expression);
    if (!Variable)
      return;
    std::string Path = variableText(Expression);
    if (Path.empty())
      return;
    std::string Key = compactText(Path);
    auto It = Controls.find(Key);
    if (It == Controls.end()) {
      ControlFact Fact;
      const size_t Dot = Path.rfind('.');
      Fact.Name = jsonText(Dot == std::string::npos
                                ? Path
                                : Path.substr(Dot + 1));
      Fact.Var = jsonText(Path);
      Fact.Source = isa<ParmVarDecl>(Variable)
                        ? "param"
                        : (isExternalGlobal(Variable) ? "global" : "local");
      Fact.Type = jsonText(Expression->getType().getAsString());
      Fact.CanonicalVar = accessPath(Expression, Context);
      Fact.ASTKind = ASTKind.str();
      Fact.Variable = Variable;
      Fact.Range = Range;
      Fact.BranchIds.insert(BranchId.str());
      Controls.emplace(Key, std::move(Fact));
    } else {
      It->second.BranchIds.insert(BranchId.str());
    }
  }

  void collectControlVariables(const Expr *Expression,
                               llvm::StringRef BranchId) {
    if (!Expression)
      return;
    Expression = Expression->IgnoreParenImpCasts();
    if (const auto *Binary = dyn_cast<BinaryOperator>(Expression)) {
      if (Binary->isLogicalOp()) {
        collectControlVariables(Binary->getLHS(), BranchId);
        collectControlVariables(Binary->getRHS(), BranchId);
        return;
      }
      if (!comparisonOp(Binary->getOpcode()).empty()) {
        registerControlVariable(comparisonVariable(Binary), BranchId,
                                Binary->getSourceRange());
        return;
      }
    }
    if (const auto *Reference = dyn_cast<DeclRefExpr>(Expression)) {
      registerControlVariable(Expression,
                              BranchId, Expression->getSourceRange(),
                              "DeclRefExpr");
      return;
    }
    for (const Stmt *Child : Expression->children())
      if (const auto *ChildExpr = dyn_cast_or_null<Expr>(Child))
        collectControlVariables(ChildExpr, BranchId);
  }

  void collectAtoms(const Expr *Expression, llvm::json::Array &Atoms) const {
    if (!Expression)
      return;
    Expression = Expression->IgnoreParenImpCasts();
    if (const auto *Binary = dyn_cast<BinaryOperator>(Expression)) {
      if (Binary->isLogicalOp()) {
        collectAtoms(Binary->getLHS(), Atoms);
        collectAtoms(Binary->getRHS(), Atoms);
        return;
      }
      if (!comparisonOp(Binary->getOpcode()).empty()) {
        Atoms.push_back(llvm::json::Value(atom(Binary)));
        return;
      }
    }
    for (const Stmt *Child : Expression->children())
      if (const auto *ChildExpr = dyn_cast_or_null<Expr>(Child))
        collectAtoms(ChildExpr, Atoms);
  }

  llvm::json::Value conditionTree(const Expr *Expression,
                                  size_t &AtomIndex) const {
    if (!Expression)
      return llvm::json::Object{{"kind", "unknown"}};
    Expression = Expression->IgnoreParenImpCasts();
    if (const auto *Unary = dyn_cast<UnaryOperator>(Expression)) {
      if (Unary->getOpcode() == UO_LNot) {
        return llvm::json::Object{
            {"kind", "not"},
            {"child", conditionTree(Unary->getSubExpr(), AtomIndex)},
        };
      }
    }
    if (const auto *Binary = dyn_cast<BinaryOperator>(Expression)) {
      if (Binary->isLogicalOp()) {
        llvm::json::Array Children;
        Children.push_back(conditionTree(Binary->getLHS(), AtomIndex));
        Children.push_back(conditionTree(Binary->getRHS(), AtomIndex));
        return llvm::json::Object{
            {"kind", "logical"},
            {"op", BinaryOperator::getOpcodeStr(Binary->getOpcode()).str()},
            {"children", std::move(Children)},
        };
      }
      if (!comparisonOp(Binary->getOpcode()).empty()) {
        return llvm::json::Object{
            {"kind", "atom"},
            {"index", static_cast<int64_t>(AtomIndex++)},
        };
      }
    }
    for (const Stmt *Child : Expression->children())
      if (const auto *ChildExpr = dyn_cast_or_null<Expr>(Child))
        return conditionTree(ChildExpr, AtomIndex);
    return llvm::json::Object{{"kind", "unknown"}};
  }

  std::optional<std::string> parentBranch(const IfStmt *Statement,
                                          unsigned &ChainIndex) const {
    const IfStmt *Cursor = Statement;
    std::optional<std::string> Parent;
    while (true) {
      auto Parents = Context.getParents(*Cursor);
      const IfStmt *MaybeParent = nullptr;
      for (const DynTypedNode &Node : Parents) {
        MaybeParent = Node.get<IfStmt>();
        if (MaybeParent)
          break;
      }
      if (!MaybeParent || MaybeParent->getElse() != Cursor)
        break;
      ++ChainIndex;
      auto Found = BranchIds.find(MaybeParent);
      if (Found != BranchIds.end())
        Parent = Found->second;
      Cursor = MaybeParent;
    }
    return Parent;
  }

  llvm::json::Array switchCases(const SwitchStmt *Statement) const {
    struct CaseValue {
      unsigned Line;
      llvm::json::Object Value;
    };
    std::vector<CaseValue> Values;
    for (const SwitchCase *Case = Statement->getSwitchCaseList(); Case;
         Case = Case->getNextSwitchCase()) {
      SourceLocation Loc = Case->getBeginLoc();
      llvm::json::Object Value{
          {"label", "default"},
          {"value", llvm::json::Value(nullptr)},
          {"is_default", isa<DefaultStmt>(Case)},
          {"value_proof", llvm::json::Value(nullptr)},
          {"provenance", provenance(SM, Case->getSourceRange(),
                                     isa<DefaultStmt>(Case) ? "DefaultStmt"
                                                            : "CaseStmt",
                                     &LangOpts)},
          {"extensions", emptyExtensions()}};
      if (const auto *CaseStmtNode = dyn_cast<CaseStmt>(Case)) {
        Value["label"] = text(CaseStmtNode->getLHS()->getSourceRange(), true);
        if (auto Integer = constantInteger(CaseStmtNode->getLHS(), Context))
          Value["value"] = *Integer;
        if (Value.getInteger("value"))
          Value["value_proof"] = "Clang constant evaluation";
      }
      Values.push_back(CaseValue{SM.getSpellingLineNumber(Loc), std::move(Value)});
    }
    std::sort(Values.begin(), Values.end(),
              [](const CaseValue &Left, const CaseValue &Right) {
                return Left.Line < Right.Line;
              });
    llvm::json::Array Result;
    for (CaseValue &Value : Values)
      Result.push_back(llvm::json::Value(std::move(Value.Value)));
    return Result;
  }

  void addBranch(llvm::StringRef Kind, const Stmt *Statement,
                 const Expr *Condition, unsigned ChainIndex = 0,
                 std::optional<std::string> Parent = std::nullopt,
                 llvm::json::Array Cases = llvm::json::Array{}) {
    std::string Bid = "b" + std::to_string(Branches.size());
    llvm::json::Array Atoms;
    collectAtoms(Condition, Atoms);
    collectControlVariables(Condition, Bid);
    std::string Spelling = Condition ? text(Condition->getSourceRange(), true) : "";
    if (Spelling.empty() && Condition)
      Spelling = text(Condition->getSourceRange(), false);
    std::string Expanded = Condition ? prettyText(Condition, LangOpts) : "";
    if (Expanded.empty() && Condition)
      Expanded = text(Condition->getSourceRange(), false);
    std::optional<bool> Constant;
    if (Condition) {
      bool Value = false;
      if (Condition->EvaluateAsBooleanCondition(Value, Context))
        Constant = Value;
    }
    llvm::json::Value Connective(nullptr);
    if (const auto *Logical = Condition
                                  ? dyn_cast<BinaryOperator>(
                                        Condition->IgnoreParenImpCasts())
                                  : nullptr) {
      if (Logical->isLogicalOp())
        Connective = jsonText(BinaryOperator::getOpcodeStr(
            Logical->getOpcode()));
    }
    llvm::json::Object Extensions = emptyExtensions();
    llvm::json::Value ConditionTree(nullptr);
    if (Condition) {
      size_t AtomIndex = 0;
      ConditionTree = conditionTree(Condition, AtomIndex);
    }
    llvm::json::Value Selector(nullptr);
    if (Kind == "switch" && Condition) {
      if (const auto Origin = expressionOrigin(Condition))
        Selector = origin(*Origin);
      else
        Selector = llvm::json::Object{
            {"kind", "selector"},
            {"expression", variableText(Condition)}};
    }
    llvm::json::Object Branch{
        {"bid", Bid},
          {"kind", jsonText(Kind)},
        {"line", static_cast<int64_t>(SM.getSpellingLineNumber(
                    Statement->getBeginLoc()))},
        {"file", locationFile(SM, SM.getSpellingLoc(Statement->getBeginLoc()))},
        {"cond_text", Spelling.empty() ? Expanded : Spelling},
        {"cond_text_spelling", Spelling},
        {"cond_text_expanded", Expanded},
        {"atoms", std::move(Atoms)},
        {"cases", std::move(Cases)},
        {"from_macro", Condition ? immediateMacro(SM, LangOpts,
                                                    Condition->getBeginLoc())
                                   : std::nullopt},
        {"chain_index", static_cast<int64_t>(ChainIndex)},
        {"connective", std::move(Connective)},
        {"reach_min", llvm::json::Value(nullptr)},
        {"reach_max", llvm::json::Value(nullptr)},
        {"constant_value", Constant ? llvm::json::Value(*Constant)
                                     : llvm::json::Value(nullptr)},
        {"constant_reason", Constant ? llvm::json::Value("Clang constant evaluation")
                                      : llvm::json::Value(nullptr)},
        {"parent_bid", Parent ? llvm::json::Value(*Parent)
                               : llvm::json::Value(nullptr)},
        {"condition_tree", std::move(ConditionTree)},
        {"selector", std::move(Selector)},
        {"provenance", provenance(SM, Statement->getSourceRange(), Kind,
                                   &LangOpts)},
        {"extensions", std::move(Extensions)}};
    if (const auto *If = dyn_cast<IfStmt>(Statement))
      BranchIds[If] = Bid;
    Branches.push_back(llvm::json::Value(std::move(Branch)));
  }

public:
  bool VisitIfStmt(IfStmt *Statement) {
    unsigned ChainIndex = 0;
    std::optional<std::string> Parent = parentBranch(Statement, ChainIndex);
    addBranch(ChainIndex == 0 ? "if" : "elseif", Statement,
              Statement->getCond(), ChainIndex, Parent);
    return true;
  }

  bool VisitWhileStmt(WhileStmt *Statement) {
    addBranch("while", Statement, Statement->getCond());
    return true;
  }

  bool VisitDoStmt(DoStmt *Statement) {
    addBranch("dowhile", Statement, Statement->getCond());
    return true;
  }

  bool VisitForStmt(ForStmt *Statement) {
    addBranch("for", Statement, Statement->getCond());
    return true;
  }

  bool VisitConditionalOperator(ConditionalOperator *Statement) {
    addBranch("ternary", Statement, Statement->getCond());
    return true;
  }

  bool VisitSwitchStmt(SwitchStmt *Statement) {
    addBranch("switch", Statement, Statement->getCond(), 0, std::nullopt,
              switchCases(Statement));
    return true;
  }

  bool VisitCallExpr(CallExpr *Call) {
    const FunctionDecl *Direct = Call->getDirectCallee();
    const std::string DirectName = Direct
                                       ? Direct->getNameAsString()
                                       : std::string();
    std::string Callee = Direct ? jsonText(Direct->getNameAsString())
                                : text(Call->getCallee()->getSourceRange(), true);
    if (Callee.empty())
      Callee = "<indirect>";
    llvm::json::Array ArgTypes;
    llvm::json::Array ArgTypeInfos;
    for (const Expr *Arg : Call->arguments())
      ArgTypes.push_back(jsonText(Arg->getType().getAsString()));
    for (const Expr *Arg : Call->arguments())
      ArgTypeInfos.push_back(typeInfo(Arg->getType(), &Context));
    llvm::json::Array Params;
    if (Direct) {
      const FunctionDecl *Definition = Direct->getDefinition();
      if (!Definition)
        Definition = Direct;
      for (unsigned Index = 0; Index < Direct->parameters().size(); ++Index) {
        const ParmVarDecl *Param = Direct->parameters()[Index];
        llvm::json::Object ParamValue = parameter(Param, &Context);
        if (Param->getType()->isPointerType() && Definition->getBody()) {
          const ParmVarDecl *DefinitionParam = Definition->getParamDecl(Index);
          const bool Written = DefinitionParam &&
              parameterIsWritten(DefinitionParam, Definition->getBody());
          ParamValue["is_written"] = Written;
          ParamValue["write_status"] = "known";
        }
        Params.push_back(std::move(ParamValue));
      }
    }
    recordMemoryHelper(Call, Direct);
    llvm::json::Value TableBase(nullptr);
    llvm::json::Value TableMember(nullptr);
    std::string TablePath;
    if (!Direct) {
      TablePath = normalizeIndexedPath(
          memberPath(Call->getCallee()));
      const size_t Dot = TablePath.rfind('.');
      if (Dot != std::string::npos && Dot > 0 && Dot + 1 < TablePath.size()) {
        TableBase = TablePath.substr(0, Dot);
        TableMember = TablePath.substr(Dot + 1);
      }
    }
    llvm::json::Object Extensions{
        {"call_capacity", callCapacity(Call)},
        {"return_used", returnValueUsed(Call)}};
    if (llvm::json::Array Guards = callGuards(Call); !Guards.empty())
      Extensions["guards"] = std::move(Guards);
    llvm::json::Object PointerArguments;
    for (unsigned Index = 0; Index < Call->getNumArgs(); ++Index) {
      const Expr *Argument = Call->getArg(Index);
      if (!Argument || !Argument->getType()->isPointerType())
        continue;
      const Expr *Ignored = Argument->IgnoreParenImpCasts();
      const bool IsAddress =
          isa<UnaryOperator>(Ignored) &&
          cast<UnaryOperator>(Ignored)->getOpcode() == UO_AddrOf;
      const bool IsNull = Argument->isNullPointerConstant(
          Context, Expr::NPC_ValueDependentIsNull);
      PointerArguments[std::to_string(Index)] = llvm::json::Object{
          {"is_address", IsAddress}, {"is_null", IsNull}};
      if (IsAddress &&
          ((Direct && DirectName.rfind("Rte_Read_", 0) == 0) ||
           (!Direct && !TablePath.empty()))) {
        const VarDecl *Root = referencedVar(Argument);
        if (Root && Root->isLocalVarDecl())
          recordLocalStubOutputOrigin(
              Root, Call, Index,
              Direct && DirectName.rfind("Rte_Read_", 0) == 0
                  ? llvm::StringRef(DirectName)
                  : llvm::StringRef());
      }
    }
    if (!PointerArguments.empty())
      Extensions["pointer_arguments"] = std::move(PointerArguments);
    llvm::json::Object CallerFields;
    for (unsigned Index = 0; Index < Call->getNumArgs(); ++Index) {
      if (!Call->getArg(Index)->getType()->isPointerType())
        continue;
      std::vector<std::string> Paths = argumentFieldPaths(Call->getArg(Index));
      if (Paths.empty())
        continue;
      llvm::json::Array Fields;
      for (const std::string &Path : Paths)
        Fields.push_back(jsonText(Path));
      CallerFields[std::to_string(Index)] = std::move(Fields);
    }
    if (!CallerFields.empty())
      Extensions["caller_param_fields"] = std::move(CallerFields);
    llvm::json::Object CallerOutputs;
    for (unsigned Index = 0; Index < Call->getNumArgs(); ++Index) {
      if (!Call->getArg(Index)->getType()->isPointerType())
        continue;
      if (Direct && argumentFieldPaths(Call->getArg(Index)).empty())
        continue;
      const VarDecl *Root = referencedVar(Call->getArg(Index));
      if (!Root)
        continue;
      const bool Observable = isa<ParmVarDecl>(Root) ||
          (Root->hasGlobalStorage() && !Root->isLocalVarDecl()) ||
          ObservableRoots.count(Root) != 0;
      CallerOutputs[std::to_string(Index)] = Observable;
    }
    if (!CallerOutputs.empty())
      Extensions["caller_param_output"] = std::move(CallerOutputs);
    if (Direct) {
      std::vector<std::string> ReturnFields =
          recordLeafPaths(Direct->getReturnType(), Context);
      if (!ReturnFields.empty()) {
        llvm::json::Array Fields;
        for (const std::string &Field : ReturnFields)
          Fields.push_back(jsonText(Field));
        Extensions["return_fields"] = std::move(Fields);
      }

      const FunctionDecl *Definition = Direct->getDefinition();
      if (!Definition)
        Definition = Direct;
      std::map<const ParmVarDecl *, unsigned> ParameterIndexes;
      for (unsigned Index = 0; Index < Definition->parameters().size(); ++Index)
        ParameterIndexes[Definition->parameters()[Index]] = Index;
      if (Stmt *Body = Definition->getBody()) {
        ParameterFieldVisitor FieldVisitor(ParameterIndexes);
        FieldVisitor.TraverseStmt(Body);
        llvm::json::Object ParamFields;
        for (unsigned Index = 0;
             Index < Direct->parameters().size() && Index < Call->getNumArgs();
             ++Index) {
          const ParmVarDecl *Param = Direct->parameters()[Index];
          if (!Param->getType()->isPointerType())
            continue;
          std::vector<std::string> Paths = argumentFieldPaths(
              Call->getArg(Index));
          if (Paths.empty())
            Paths = FieldVisitor.paths(Index);
          if (Paths.empty())
            continue;
          llvm::json::Array Fields;
          for (const std::string &Field : Paths)
            Fields.push_back(jsonText(Field));
          ParamFields[std::to_string(Index)] = std::move(Fields);
        }
        if (!ParamFields.empty())
          Extensions["param_fields"] = std::move(ParamFields);
      }
    }
    auto takeExtension = [&Extensions](llvm::StringRef Key,
                                       llvm::json::Value Default) {
      if (llvm::json::Value *Value = Extensions.get(Key))
        return std::move(*Value);
      return Default;
    };
    llvm::json::Object Value{
        {"order", static_cast<int64_t>(Calls.size())},
        {"call_id", "call_" + std::to_string(Calls.size())},
        {"callee", Callee},
        // A macro-generated callee (for example an Rte wrapper) has a
        // spelling location in its declaration/header.  The renderer needs
        // the call-site line for deterministic source ordering, so use the
        // expansion location here while provenance still retains spelling
        // and macro information.
        {"line", static_cast<int64_t>(SM.getExpansionLineNumber(
                    SM.getExpansionLoc(Call->getExprLoc())))},
        {"via_macro", immediateMacro(SM, LangOpts, Call->getExprLoc())},
        {"ptr_call", Direct == nullptr},
        {"is_static", Direct && Direct->getStorageClass() == SC_Static},
        {"table_base", std::move(TableBase)},
        {"table_member", std::move(TableMember)},
        {"arg_types", std::move(ArgTypes)},
        {"arg_type_infos", std::move(ArgTypeInfos)},
        {"params", std::move(Params)},
        {"ret_type", jsonText(Call->getType().getAsString())},
        {"callee_kind", Direct
                            ? ((DirectName.find("orread_reg") != std::string::npos ||
                                DirectName.find("orwrite_reg") != std::string::npos)
                                   ? "memory_helper"
                                   : "direct")
                            : "indirect"},
        {"max_occurrences", takeExtension(
                                 "call_capacity", llvm::json::Value(static_cast<int64_t>(1)))},
        {"return_used", takeExtension("return_used", llvm::json::Value(false))},
        {"pointer_arguments", takeExtension(
                                   "pointer_arguments", llvm::json::Value(
                                       llvm::json::Object{}))},
        {"caller_param_fields", takeExtension(
                                      "caller_param_fields", llvm::json::Value(
                                          llvm::json::Object{}))},
        {"caller_param_output", takeExtension(
                                      "caller_param_output", llvm::json::Value(
                                          llvm::json::Object{}))},
        {"param_fields", takeExtension("param_fields", llvm::json::Value(
                                             llvm::json::Object{}))},
        {"return_fields", takeExtension("return_fields", llvm::json::Value(
                                              llvm::json::Array{}))},
        {"guards", takeExtension("guards", llvm::json::Value(
                                      llvm::json::Array{}))},
        {"provenance", provenance(SM, Call->getSourceRange(), "CallExpr",
                                   &LangOpts)},
        {"extensions", std::move(Extensions)}};
    if (!Direct) {
      HasUnsupported = true;
      llvm::json::Object Diagnostic = issue(
          "INDIRECT_CALL_UNRESOLVED", "warning",
          "callee declaration is unavailable; indirect call retained");
      Diagnostic["provenance"] = provenance(SM, Call->getSourceRange(),
                                             "CallExpr", &LangOpts);
      Diagnostics.push_back(llvm::json::Value(std::move(Diagnostic)));
    }
    Calls.push_back(llvm::json::Value(std::move(Value)));
    return true;
  }

  bool VisitReturnStmt(ReturnStmt *Statement) {
    if (!Statement)
      return true;
    const Expr *Expression = Statement->getRetValue();
    ReturnEffect Effect;
    if (Expression) {
      Effect.Value = text(Expression->getSourceRange(), true);
      if (Effect.Value.empty())
        Effect.Value = prettyText(Expression, LangOpts);
      Effect.ConstantValue = constantInteger(Expression, Context);
      Effect.Origin = expressionOrigin(Expression);
      Effect.SourceOffset = static_cast<int64_t>(SM.getFileOffset(
          SM.getExpansionLoc(Expression->getBeginLoc())));
    }
    Effect.Guards = activeGuards(Statement);
    Effect.Order = NextEffectOrder++;
    ReturnEffects.push_back(std::move(Effect));
    return true;
  }

  bool VisitVarDecl(VarDecl *Variable) {
    if (Variable->isLocalVarDecl() && !isa<ParmVarDecl>(Variable))
      Locals.insert(jsonText(Variable->getNameAsString()));
    if (Variable->isLocalVarDecl() && !isa<ParmVarDecl>(Variable) &&
        Variable->hasInit())
      recordLocalOrigin(Variable, Variable->getInit(), Variable->getLocation(),
                        Variable->getInit());
    return true;
  }

  bool VisitDeclRefExpr(DeclRefExpr *Reference) {
    if (const auto *Parameter = dyn_cast<ParmVarDecl>(Reference->getDecl()))
      recordParameterAccess(Reference);
    if (const auto *Variable = dyn_cast<VarDecl>(Reference->getDecl())) {
      if (isExternalGlobal(Variable)) {
        const bool Written = isWrittenReference(Reference);
        // Passing an array/pointer object to a helper reads its address, not
        // the object contents.  WinAMS does not expose such implementation
        // buffers as testcase IO; their pointee/element is recorded only when
        // the AST contains an actual dereference or member value access.
        if (!Written && isAddressOnlyReference(Reference))
          return true;
        ensureGlobalObjectFact(Variable, Written, Reference->getSourceRange(),
                               Reference->getBeginLoc());
      }
    }
    return true;
  }

  bool VisitMemberExpr(MemberExpr *Expression) {
    recordParameterAccess(Expression);
    recordGlobalFieldAccess(Expression);
    const VarDecl *Root = referencedVar(Expression);
    if (!Root)
      return true;
    const std::string FullPath = memberPath(Expression);
    const std::string Prefix = jsonText(Root->getNameAsString()) + ".";
    if (FullPath.rfind(Prefix, 0) != 0 || FullPath.size() <= Prefix.size())
      return true;
    auto &Paths = LocalFieldPaths[Root];
    if (std::find(Paths.begin(), Paths.end(), FullPath) == Paths.end())
      Paths.push_back(FullPath);
    return true;
  }

  bool VisitArraySubscriptExpr(ArraySubscriptExpr *Expression) {
    recordParameterAccess(Expression);
    if (const VarDecl *Variable = referencedVar(Expression);
        isExternalGlobal(Variable) && !Expression->getType()->isRecordType())
      recordGlobalFieldAccess(Expression);
    return true;
  }

  bool VisitBinaryOperator(BinaryOperator *Operator) {
    if (!Operator->isAssignmentOp())
      return true;
    const VarDecl *Variable = referencedVar(Operator->getLHS());
    if (!Variable)
      return true;
    std::string LocalPath = accessPath(Operator->getLHS(), Context);
    const std::string LocalPrefix =
        jsonText(Variable->getNameAsString()) + ".";
    if (LocalPath.rfind(LocalPrefix, 0) == 0)
      LocalPath = LocalPath.substr(LocalPrefix.size());
    else if (LocalPath == jsonText(Variable->getNameAsString()))
      LocalPath.clear();
    recordLocalOrigin(Variable, Operator->getRHS(),
                      Operator->getLHS()->getBeginLoc(), Operator,
                      std::move(LocalPath),
                      BinaryOperator::getOpcodeStr(Operator->getOpcode()).str());
    if (const auto *Param = dyn_cast<ParmVarDecl>(Variable)) {
      const std::string Path = accessPath(Operator->getLHS(), Context);
      if (Param->getType()->isPointerType() &&
          Path != jsonText(Param->getNameAsString())) {
        WrittenParams.insert(Param);
        recordParameterWriteEffect(Param, Operator->getLHS(),
                                   Operator->getRHS());
      }
    }
    if (isExternalGlobal(Variable)) {
      GlobalWrites.insert(jsonText(Variable->getNameAsString()));
      if (Operator->getOpcode() == BO_Assign)
        recordGlobalWriteEffect(Operator->getLHS(), Operator->getRHS(),
                                Operator);
    }
    // A whole-record assignment does not contain a MemberExpr rooted at the
    // global.  Preserve the active fields copied from a local record so a
    // union-valued global can select those fields instead of exposing every
    // representation alias declared in the type.
    if (isExternalGlobal(Variable)) {
      const VarDecl *Source = referencedVar(Operator->getRHS());
      auto Paths = LocalFieldPaths.find(Source);
      if (Source && !isExternalGlobal(Source) && Paths != LocalFieldPaths.end()) {
        const std::string SourcePrefix =
            jsonText(Source->getNameAsString()) + ".";
        std::string TargetPath = memberPath(Operator->getLHS());
        const std::string GlobalName =
            jsonText(Variable->getNameAsString());
        std::string TargetRelative;
        const std::string GlobalPrefix = GlobalName + ".";
        if (TargetPath.rfind(GlobalPrefix, 0) == 0)
          TargetRelative = TargetPath.substr(GlobalPrefix.size());
        for (size_t Sequence = 0; Sequence < Paths->second.size(); ++Sequence) {
          const std::string &Path = Paths->second[Sequence];
          if (Path.rfind(SourcePrefix, 0) != 0 ||
              Path.size() <= SourcePrefix.size())
            continue;
          std::string Relative = Path.substr(SourcePrefix.size());
          if (!TargetRelative.empty())
            Relative = TargetRelative + "." + Relative;
          recordGlobalFieldAccessPath(
              Variable, Relative, true, Operator->getLHS()->getSourceRange(),
              Operator->getLHS()->getBeginLoc(), true,
              static_cast<unsigned>(Sequence));
        }
      }
    }
    return true;
  }

  bool VisitUnaryOperator(UnaryOperator *Operator) {
    recordParameterAccess(Operator);
    recordMemory(Operator);
    if (!Operator->isIncrementDecrementOp())
      return true;
    if (const VarDecl *Variable = referencedVar(Operator->getSubExpr())) {
      if (isExternalGlobal(Variable))
        GlobalWrites.insert(jsonText(Variable->getNameAsString()));
      if (const auto *Param = dyn_cast<ParmVarDecl>(Variable)) {
        const std::string Path = accessPath(Operator, Context);
        if (Param->getType()->isPointerType() &&
            Path != jsonText(Param->getNameAsString()))
          WrittenParams.insert(Param);
      }
    }
    return true;
  }

public:
  llvm::json::Object makeFunction() {
    SourceManager &Source = Context.getSourceManager();
    SourceLocation Begin = Decl->getBeginLoc();
    std::string File = locationFile(Source, Source.getSpellingLoc(Begin));
    llvm::json::Object Function{
        {"name", jsonText(Decl->getNameAsString())},
        {"file", File},
        {"line", static_cast<int64_t>(Source.getSpellingLineNumber(Begin))},
        {"line_end", static_cast<int64_t>(Source.getSpellingLineNumber(Decl->getEndLoc()))},
        {"ret_type", jsonText(Decl->getReturnType().getAsString())},
        {"is_static", Decl->getStorageClass() == SC_Static},
        {"params", parameters()},
        {"globals_used", globalsUsed()},
        {"locals", locals()},
        {"calls", calls()},
        {"branches", branches()},
        {"config", config()},
        {"notes", llvm::json::Array{}},
        {"enums", enums()},
        {"global_writes", globalWrites()},
        {"parameter_write_effects", parameterWriteEffects()},
        {"global_write_effects", globalWriteEffects()},
        {"local_value_effects", localValueEffects()},
        {"return_effects", returnEffects()},
        {"global_objects", globalObjects()},
        {"control_vars", controlVariables()},
        {"config_ptrs", llvm::json::Array{}},
        {"memory_vars", memoryVariables()},
        {"status", hasUnsupported() ? "UNSUPPORTED" : "OK"},
        {"provenance", provenance(Source, Decl->getSourceRange(), "FunctionDecl")},
        {"diagnostics", diagnostics()},
        {"extensions", llvm::json::Object{
            {"local_origins", localOrigins()}}}};
    return Function;
  }

private:
  void recordMemoryHelper(const CallExpr *Call,
                          const FunctionDecl *Direct) {
    if (!Call || !Direct || Call->getNumArgs() < 1)
      return;
    const std::string Callee = Direct->getNameAsString();
    const bool Read = Callee.find("orread_reg") != std::string::npos;
    const bool Write = Callee.find("orwrite_reg") != std::string::npos;
    if (!Read && !Write)
      return;

    const Expr *AddressExpr = Call->getArg(0);
    const auto Address = nestedConstantInteger(AddressExpr, Context);
    if (!Address)
      return;
    const int64_t AddressValue = *Address < 0
                                     ? static_cast<int64_t>(
                                           static_cast<uint32_t>(*Address))
                                     : *Address;
    std::string Name = immediateMacro(SM, LangOpts,
                                      AddressExpr->getBeginLoc())
                           .value_or(text(AddressExpr->getSourceRange(), true));
    if (Name.empty())
      Name = "<constant-memory>";

    unsigned Bits = 0;
    const size_t Reg = Callee.find("reg");
    if (Reg != std::string::npos) {
      for (size_t Index = Reg + 3; Index < Callee.size(); ++Index) {
        const char Value = Callee[Index];
        if (Value < '0' || Value > '9')
          break;
        Bits = Bits * 10 + static_cast<unsigned>(Value - '0');
      }
    }
    const unsigned Width = Bits >= 8 ? std::max(1U, Bits / 8) : 1U;
    // WinAMS names a register by the width of the accessor, even when the
    // address macro itself was declared with a wider integer type (for
    // example U4L_DMA_REG_* passed to an *reg16 helper).
    if (Name.size() > 3 && Name[0] == 'U') {
      const size_t Length = Name.find('L', 1);
      if (Length != std::string::npos && Name[Length + 1] == '_')
        Name = "U" + std::to_string(Width) + Name.substr(Length);
    }
    const auto Key = std::make_pair(Name, AddressValue);
    auto It = Memories.find(Key);
    if (It == Memories.end()) {
      MemoryFact Fact;
      Fact.Name = Name;
      Fact.Address = AddressValue;
      Fact.Width = Width;
      Fact.Read = Read;
      Fact.Write = Write;
      Fact.Conditional = isConditionalAccess(Call);
      Fact.InputValue = Write ? std::optional<int64_t>(0) : std::nullopt;
      if (Write && Call->getNumArgs() >= 2)
        Fact.ExpectedValue = constantInteger(Call->getArg(1), Context);
      Fact.Range = Call->getSourceRange();
      Memories.emplace(Key, std::move(Fact));
      return;
    }
    It->second.Read = It->second.Read || Read;
    It->second.Write = It->second.Write || Write;
    It->second.Conditional = It->second.Conditional ||
                             isConditionalAccess(Call);
    if (Write) {
      It->second.InputValue = 0;
      if (Call->getNumArgs() >= 2)
        It->second.ExpectedValue = constantInteger(Call->getArg(1), Context);
    }
  }

  void recordMemory(const UnaryOperator *Operator) {
    if (Operator->getOpcode() != UO_Deref)
      return;
    QualType AccessType = Operator->getType();
    if (!AccessType.isVolatileQualified())
      return;
    auto Address = nestedConstantInteger(Operator->getSubExpr(), Context);
    if (!Address)
      return;
    if (*Address < 0)
      *Address = static_cast<int64_t>(static_cast<uint32_t>(*Address));
    std::string Name = immediateMacro(SM, LangOpts, Operator->getBeginLoc())
                           .value_or(text(Operator->getSourceRange(), true));
    if (Name.empty())
      Name = "<constant-memory>";
    uint64_t Bits = Context.getTypeSize(AccessType);
    unsigned Width = static_cast<unsigned>((Bits + 7) / 8);
    if (Width == 0)
      Width = 1;
    const bool Write = isWriteLValue(Operator);
    const auto Key = std::make_pair(Name, *Address);
    auto It = Memories.find(Key);
    if (It == Memories.end()) {
      MemoryFact Fact;
      Fact.Name = Name;
      Fact.Address = *Address;
      Fact.Width = Width;
      Fact.Read = !Write;
      Fact.Write = Write;
      Fact.Conditional = isConditionalAccess(Operator);
      Fact.Range = Operator->getSourceRange();
      Memories.emplace(Key, std::move(Fact));
      return;
    }
    It->second.Read = It->second.Read || !Write;
    It->second.Write = It->second.Write || Write;
    It->second.Conditional =
        It->second.Conditional || isConditionalAccess(Operator);
  }

  bool isWriteLValue(const Expr *Expression) const {
    const Expr *Current = Expression;
    for (unsigned Depth = 0; Depth < 32; ++Depth) {
      auto Parents = Context.getParents(*Current);
      if (Parents.empty())
        return false;
      const DynTypedNode &Parent = Parents[0];
      if (const auto *Operator = Parent.get<BinaryOperator>()) {
        const Expr *LHS = Operator->getLHS()->IgnoreParenImpCasts();
        const Expr *Target = Current->IgnoreParenImpCasts();
        if (LHS == Target && Operator->isAssignmentOp())
          return true;
        return false;
      }
      if (const auto *Unary = Parent.get<UnaryOperator>()) {
        if (Unary->isIncrementDecrementOp())
          return true;
        Current = Unary;
        continue;
      }
      const auto *ParentExpr = Parent.get<Expr>();
      if (!ParentExpr)
        return false;
      Current = ParentExpr;
    }
    return false;
  }

  bool isConditionalAccess(const Stmt *Statement) const {
    std::vector<const Stmt *> Pending{Statement};
    std::set<const Stmt *> Seen;
    while (!Pending.empty()) {
      const Stmt *Current = Pending.back();
      Pending.pop_back();
      if (!Seen.insert(Current).second)
        continue;
      for (const DynTypedNode &Parent : Context.getParents(*Current)) {
        if (Parent.get<IfStmt>() || Parent.get<WhileStmt>() ||
            Parent.get<DoStmt>() || Parent.get<ForStmt>() ||
            Parent.get<SwitchStmt>() || Parent.get<ConditionalOperator>())
          return true;
        if (const auto *ParentStmt = Parent.get<Stmt>())
          Pending.push_back(ParentStmt);
      }
    }
    return false;
  }

  bool isWrittenReference(const DeclRefExpr *Reference) const {
    return isWriteLValue(Reference);
  }

  bool isAddressOnlyReference(const DeclRefExpr *Reference) const {
    const VarDecl *Variable = dyn_cast<VarDecl>(Reference->getDecl());
    if (!Variable ||
        (!Variable->getType()->isArrayType() &&
         !Variable->getType()->isPointerType()))
      return false;

    const Expr *Current = Reference;
    for (unsigned Depth = 0; Depth < 32; ++Depth) {
      auto Parents = Context.getParents(*Current);
      if (Parents.empty())
        return false;
      const DynTypedNode &Parent = Parents[0];
      if (Parent.get<CallExpr>())
        return true;
      if (Parent.get<CompoundStmt>() || Parent.get<DeclStmt>() ||
          Parent.get<ReturnStmt>() || Parent.get<BinaryOperator>() ||
          Parent.get<UnaryOperator>() || Parent.get<MemberExpr>() ||
          Parent.get<ArraySubscriptExpr>()) {
        if (Parent.get<BinaryOperator>() || Parent.get<UnaryOperator>() ||
            Parent.get<MemberExpr>() || Parent.get<ArraySubscriptExpr>()) {
          Current = Parent.get<Expr>();
          if (!Current)
            return false;
          continue;
        }
        return false;
      }
      if (const auto *ParentExpr = Parent.get<Expr>()) {
        Current = ParentExpr;
        continue;
      }
      return false;
    }
    return false;
  }

  ASTContext &Context;
  const FunctionDecl *Decl;
  SourceManager &SM;
  const LangOptions &LangOpts;
  const std::map<std::string, std::string> &Macros;
  const std::map<std::string, std::map<std::string, int64_t>> &Enums;
  llvm::json::Array Branches;
  llvm::json::Array Calls;
  std::set<std::string> GlobalsUsed;
  std::set<std::string> GlobalWrites;
  std::set<std::string> Locals;
  std::set<const ParmVarDecl *> WrittenParams;
  std::map<const IfStmt *, std::string> BranchIds;
  std::map<std::string, ControlFact> Controls;
  std::map<std::string, GlobalFact> GlobalObjects;
  std::map<std::string, std::map<std::string, GlobalFieldAccess>>
      GlobalFieldAccesses;
  std::map<const VarDecl *, std::vector<ValueOrigin>> LocalOrigins;
  std::map<const VarDecl *, std::vector<LocalValueEffect>>
      LocalValueEffects;
  std::map<const VarDecl *, std::vector<std::string>> LocalFieldPaths;
  std::set<const VarDecl *> ObservableRoots;
  std::map<const ParmVarDecl *, std::vector<ParamAccess>> ParameterAccesses;
  std::map<const ParmVarDecl *, std::vector<ParamWriteEffect>>
      ParamWriteEffects;
  std::vector<ReturnEffect> ReturnEffects;
  std::vector<GlobalWriteEffect> GlobalWriteEffects;
  std::map<std::pair<std::string, int64_t>, MemoryFact> Memories;
  std::vector<llvm::json::Value> Diagnostics;
  bool HasUnsupported = false;
  int64_t NextEffectOrder = 0;
};

class FunctionVisitor final
    : public RecursiveASTVisitor<FunctionVisitor> {
public:
  FunctionVisitor(ASTContext &Context, RunState &State, llvm::StringRef Filter,
                  const TargetSet &Targets,
                  const std::map<std::string, std::string> &Macros)
      : Context(Context), State(State), Filter(Filter.str()), Targets(Targets),
        Macros(Macros) {}

  bool VisitEnumDecl(EnumDecl *Decl) {
    if (!Decl->isCompleteDefinition())
      return true;
    std::string Name = Decl->getNameAsString();
    if (Name.empty()) {
      if (const TypedefNameDecl *Typedef = Decl->getTypedefNameForAnonDecl())
        Name = Typedef->getNameAsString();
    }
    if (Name.empty())
      return true;
    SourceManager &SM = Context.getSourceManager();
    SourceLocation Begin = Decl->getBeginLoc();
    if (Begin.isInvalid() ||
        SM.isInSystemHeader(SM.getExpansionLoc(Begin)))
      return true;
    auto &Members = Enums[Name];
    for (const EnumConstantDecl *Constant : Decl->enumerators())
      Members[Constant->getNameAsString()] =
          Constant->getInitVal().getSExtValue();
    return true;
  }

  bool VisitVarDecl(VarDecl *Decl) {
    if (!Decl->hasGlobalStorage() || !Decl->hasInit() ||
        !Decl->isThisDeclarationADefinition())
      return true;
    SourceManager &SM = Context.getSourceManager();
    SourceLocation Begin = Decl->getBeginLoc();
    if (Begin.isInvalid() || SM.isInSystemHeader(SM.getSpellingLoc(Begin)))
      return true;
    collectInitializer(Decl->getNameAsString(), Decl->getType(),
                       Decl->getInit());
    collectFunctionPointerInitializer(Decl->getNameAsString(),
                                      Decl->getType(), Decl->getInit());
    return true;
  }

  bool VisitFunctionDecl(FunctionDecl *Decl) {
    if (!Decl->isThisDeclarationADefinition())
      return true;
    SourceManager &SM = Context.getSourceManager();
    SourceLocation Begin = Decl->getBeginLoc();
    if (Begin.isInvalid() || !SM.isWrittenInMainFile(SM.getExpansionLoc(Begin)))
      return true;
    const std::string Name = Decl->getNameAsString();
    const std::string File = locationFile(SM, SM.getSpellingLoc(Begin));
    const bool Selected = !Targets.empty()
                              ? Targets.count(targetKey(File, Name)) != 0
                              : Filter.empty() || Name == Filter;
    // In bulk mode, only target bodies are expensive to materialize.  Global
    // initializers and function-pointer tables are still visited in every
    // translation unit, so cross-TU facts remain available to the target.
    if (Targets.empty() || Selected)
      recordDefinition(Decl);
    if (!Selected)
      return true;
    FunctionBodyVisitor Body(Context, Decl, Macros, Enums);
    Body.build();
    if (Body.hasUnsupported())
      addIssue(State, "UNSUPPORTED_FUNCTION_FACTS", "warning",
               (std::string("unsupported facts in function: ") +
                Decl->getNameAsString())
                   .c_str());
    State.Functions.push_back(FunctionFact{
        File, SM.getSpellingLineNumber(Begin), Name,
        Body.makeFunction()});
    // A filtered extraction stops after the requested definition.  Without
    // a filter, keep traversing the main file so ``extract_all`` receives
    // every source-defined function instead of only the first one.
    return !Targets.empty() || Filter.empty();
  }

private:
  void recordDefinition(const FunctionDecl *Decl) {
    FunctionDefinitionFact Fact;
    SourceManager &SM = Context.getSourceManager();
    SourceLocation Begin = Decl->getBeginLoc();
    Fact.File = locationFile(SM, SM.getSpellingLoc(Begin));
    Fact.Line = SM.getSpellingLineNumber(Begin);
    Fact.Name = Decl->getNameAsString();

    std::map<const ParmVarDecl *, unsigned> Indexes;
    for (unsigned Index = 0; Index < Decl->parameters().size(); ++Index)
      Indexes[Decl->parameters()[Index]] = Index;
    if (Stmt *Body = Decl->getBody()) {
      ParameterFieldVisitor Visitor(Indexes);
      Visitor.TraverseStmt(Body);
      for (const auto &Entry : Indexes) {
        std::vector<std::string> Paths = Visitor.paths(Entry.second);
        if (!Paths.empty())
          Fact.ParamFields[Entry.second] = std::move(Paths);
      }
    }
    Fact.ReturnFields = recordLeafPaths(Decl->getReturnType(), Context);
    State.FunctionDefinitions.push_back(std::move(Fact));
  }

  void collectFunctionPointerInitializer(llvm::StringRef Path, QualType Type,
                                         const Expr *Initializer) {
    if (!Initializer || Path.empty())
      return;
    Initializer = Initializer->IgnoreParenImpCasts();
    if (Type->isFunctionPointerType()) {
      const FunctionDecl *Target = functionPointerTarget(Initializer);
      if (!Target)
        return;
      FunctionPointerTargetFact Fact;
      Fact.Name = Target->getNameAsString();
      Fact.ReturnType = Target->getReturnType().getAsString();
      for (const ParmVarDecl *Param : Target->parameters()) {
        QualType ParamType = Param->getType();
        bool IsConst = ParamType.isConstQualified();
        if (ParamType->isPointerType())
          IsConst = IsConst || ParamType->getPointeeType().isConstQualified();
        Fact.Params.push_back(FunctionPointerParameterFact{
            Param->getNameAsString(), ParamType.getAsString(),
            ParamType->isPointerType(), IsConst,
            typeInfo(ParamType, &Context)});
      }
      const std::string Key = normalizeIndexedPath(Path);
      auto &Targets = State.FunctionPointerTargets[Key];
      const auto Existing = std::find_if(
          Targets.begin(), Targets.end(), [&](const auto &Candidate) {
            return Candidate.Name == Fact.Name;
          });
      if (Existing == Targets.end())
        Targets.push_back(std::move(Fact));
      return;
    }

    const auto *List = dyn_cast<InitListExpr>(Initializer);
    if (!List)
      return;

    if (const auto *Array = Context.getAsConstantArrayType(Type)) {
      QualType ElementType = Array->getElementType();
      for (unsigned Index = 0; Index < List->getNumInits(); ++Index) {
        std::string ElementPath = Path.str() + "[" +
                                  std::to_string(Index) + "]";
        collectFunctionPointerInitializer(ElementPath, ElementType,
                                          List->getInit(Index));
      }
      return;
    }

    if (const auto *Record = Type->getAs<RecordType>()) {
      unsigned Index = 0;
      for (const FieldDecl *Field : Record->getDecl()->fields()) {
        if (Index >= List->getNumInits())
          break;
        std::string FieldPath = Path.str() + "." + Field->getNameAsString();
        collectFunctionPointerInitializer(FieldPath, Field->getType(),
                                          List->getInit(Index));
        ++Index;
      }
      return;
    }

    if (List->getNumInits() == 1)
      collectFunctionPointerInitializer(Path, Type, List->getInit(0));
  }

  void collectInitializer(llvm::StringRef Path, QualType Type,
                          const Expr *Initializer) {
    if (!Initializer || Path.empty())
      return;
    Initializer = Initializer->IgnoreParenImpCasts();
    if (auto Value = constantInteger(Initializer, Context)) {
      int64_t Normalized = *Value;
      if (Type->isUnsignedIntegerType()) {
        const unsigned Bits = Context.getTypeSize(Type);
        if (Bits > 0 && Bits < 64) {
          const uint64_t Mask = (uint64_t{1} << Bits) - 1;
          Normalized = static_cast<int64_t>(
              static_cast<uint64_t>(Normalized) & Mask);
        }
      }
      State.GlobalInitializers[compactText(Path)] = Normalized;
      return;
    }
    const auto *List = dyn_cast<InitListExpr>(Initializer);
    if (!List)
      return;

    if (const auto *Array = Context.getAsConstantArrayType(Type)) {
      QualType ElementType = Array->getElementType();
      for (unsigned Index = 0; Index < List->getNumInits(); ++Index) {
        std::string ElementPath = Path.str() + "[" +
                                  std::to_string(Index) + "]";
        collectInitializer(ElementPath, ElementType, List->getInit(Index));
      }
      return;
    }

    if (const auto *Record = Type->getAs<RecordType>()) {
      unsigned Index = 0;
      for (const FieldDecl *Field : Record->getDecl()->fields()) {
        if (Index >= List->getNumInits())
          break;
        std::string FieldPath = Path.str() + "." + Field->getNameAsString();
        collectInitializer(FieldPath, Field->getType(), List->getInit(Index));
        ++Index;
      }
      return;
    }

    if (List->getNumInits() == 1)
      collectInitializer(Path, Type, List->getInit(0));
  }

  ASTContext &Context;
  RunState &State;
  std::string Filter;
  const TargetSet &Targets;
  const std::map<std::string, std::string> &Macros;
  std::map<std::string, std::map<std::string, int64_t>> Enums;
};

class FunctionConsumer final : public ASTConsumer {
public:
  FunctionConsumer(ASTContext &Context, RunState &State, llvm::StringRef Filter,
                   const TargetSet &Targets,
                   const std::map<std::string, std::string> &Macros)
      : Visitor(Context, State, Filter, Targets, Macros) {}

  void HandleTranslationUnit(ASTContext &Context) override {
    Visitor.TraverseDecl(Context.getTranslationUnitDecl());
  }

private:
  FunctionVisitor Visitor;
};

class CapturingDiagnosticConsumer final : public DiagnosticConsumer {
public:
  explicit CapturingDiagnosticConsumer(RunState &State) : State(State) {}

  void HandleDiagnostic(DiagnosticsEngine::Level Level,
                        const Diagnostic &Info) override {
    llvm::SmallString<256> Message;
    Info.FormatDiagnostic(Message);
    llvm::StringRef Severity = "info";
    if (Level == DiagnosticsEngine::Warning ||
        Level == DiagnosticsEngine::Remark)
      Severity = "warning";
    if (Level == DiagnosticsEngine::Error ||
        Level == DiagnosticsEngine::Fatal)
      Severity = "error";
    addIssue(State, "CLANG_DIAGNOSTIC", Severity, Message);
  }

private:
  RunState &State;
};

class MacroTraceCallbacks final : public PPCallbacks {
public:
  MacroTraceCallbacks(Preprocessor &Preprocessor,
                      std::map<std::string, std::string> &Definitions)
      : Preprocessor(Preprocessor), Definitions(Definitions) {}

  void MacroDefined(const Token &MacroNameTok,
                    const MacroDirective *Directive) override {
    if (!Directive || !MacroNameTok.getIdentifierInfo())
      return;
    const MacroInfo *Info = Directive->getMacroInfo();
    SourceLocation Definition = Info->getDefinitionLoc();
    llvm::StringRef File = Preprocessor.getSourceManager().getFilename(Definition);
    if (File.empty() || File.starts_with("<") ||
        Preprocessor.getSourceManager().isInSystemHeader(Definition))
      return;
    std::string Replacement;
    for (const Token &TokenValue : Info->tokens()) {
      if (!Replacement.empty())
        Replacement.push_back(' ');
      bool Invalid = false;
      Replacement += Preprocessor.getSpelling(TokenValue, &Invalid);
      if (Invalid)
        return;
    }
    Definitions[jsonText(MacroNameTok.getIdentifierInfo()->getName())] =
        jsonText(Replacement);
  }

private:
  Preprocessor &Preprocessor;
  std::map<std::string, std::string> &Definitions;
};

class ExtractAction final : public ASTFrontendAction {
public:
  ExtractAction(RunState &State, llvm::StringRef Filter,
                const TargetSet &Targets)
      : State(State), Filter(Filter.str()), Targets(Targets) {}

  bool BeginSourceFileAction(CompilerInstance &Compiler) override {
    SourceManager &Source = Compiler.getSourceManager();
    State.ActiveSource = jsonText(
        Source.getFilename(Source.getLocForStartOfFile(Source.getMainFileID())));
    Compiler.getDiagnostics().setClient(
        new CapturingDiagnosticConsumer(State), true);
    Compiler.getPreprocessor().addPPCallbacks(
        std::make_unique<MacroTraceCallbacks>(Compiler.getPreprocessor(),
                                              MacroDefinitions));
    return true;
  }

  std::unique_ptr<ASTConsumer>
  CreateASTConsumer(CompilerInstance &Compiler, llvm::StringRef) override {
    return std::make_unique<FunctionConsumer>(Compiler.getASTContext(), State,
                                               Filter, Targets,
                                               MacroDefinitions);
  }

private:
  RunState &State;
  std::string Filter;
  const TargetSet &Targets;
  std::map<std::string, std::string> MacroDefinitions;
};

class ExtractActionFactory final : public tooling::FrontendActionFactory {
public:
  ExtractActionFactory(RunState &State, llvm::StringRef Filter,
                       const TargetSet &Targets)
      : State(State), Filter(Filter.str()), Targets(Targets) {}

  std::unique_ptr<FrontendAction> create() override {
    return std::make_unique<ExtractAction>(State, Filter, Targets);
  }

private:
  RunState &State;
  std::string Filter;
  const TargetSet &Targets;
};

struct CompileContext {
  llvm::json::Object Raw{
      {"schema_version", 1},
      {"language", "c"},
      {"standard", "c11"},
      {"source_files", llvm::json::Array{}},
      {"include_dirs", llvm::json::Array{}},
      {"defines", llvm::json::Object{}},
      {"force_includes", llvm::json::Array{}},
      {"target_triple", nullptr},
      {"cpu", nullptr},
      {"abi", nullptr},
      {"sysroot", nullptr},
      {"resource_dir", nullptr},
      {"extra_args", llvm::json::Array{}}};
  std::vector<std::string> Sources;
  std::vector<std::string> Arguments;
};

std::optional<std::string> requiredString(const llvm::json::Object &Object,
                                          llvm::StringRef Key,
                                          RunState &State) {
  std::optional<llvm::StringRef> Value = Object.getString(Key);
  if (!Value) {
    addIssue(State, "INVALID_COMPILE_CONTEXT", "error",
             (std::string("missing string field: ") + Key.str()).c_str());
    return std::nullopt;
  }
  return Value->str();
}

bool appendStringArray(const llvm::json::Object &Object, llvm::StringRef Key,
                       std::vector<std::string> &Output, RunState &State) {
  const llvm::json::Array *Values = Object.getArray(Key);
  if (!Values) {
    addIssue(State, "INVALID_COMPILE_CONTEXT", "error",
             (std::string("missing array field: ") + Key.str()).c_str());
    return false;
  }
  for (const llvm::json::Value &Value : *Values) {
    std::optional<llvm::StringRef> Text = Value.getAsString();
    if (!Text) {
      addIssue(State, "INVALID_COMPILE_CONTEXT", "error",
               (std::string("array field contains non-string: ") + Key.str()).c_str());
      return false;
    }
    Output.push_back(Text->str());
  }
  return true;
}

bool loadCompileContext(RunState &State, CompileContext &Context) {
  auto Buffer = llvm::MemoryBuffer::getFile(ContextPath);
  if (!Buffer) {
    addIssue(State, "CONTEXT_READ_ERROR", "error",
             (std::string("cannot read context: ") + ContextPath).c_str());
    return false;
  }
  auto Parsed = llvm::json::parse(Buffer.get()->getBuffer());
  if (!Parsed) {
    std::string Error = llvm::toString(Parsed.takeError());
    addIssue(State, "CONTEXT_JSON_ERROR", "error", Error);
    return false;
  }
  const llvm::json::Object *Object = Parsed->getAsObject();
  if (!Object) {
    addIssue(State, "INVALID_COMPILE_CONTEXT", "error",
             "context root must be an object");
    return false;
  }
  std::optional<int64_t> Version = Object->getInteger("schema_version");
  std::optional<llvm::StringRef> Language = Object->getString("language");
  if (!Version || *Version != 1 || !Language || *Language != "c") {
    addIssue(State, "INVALID_COMPILE_CONTEXT", "error",
             "schema_version must be 1 and language must be c");
    return false;
  }
  std::optional<std::string> Standard = requiredString(*Object, "standard", State);
  if (!Standard || !appendStringArray(*Object, "source_files", Context.Sources, State))
    return false;
  if (Context.Sources.empty()) {
    addIssue(State, "INVALID_COMPILE_CONTEXT", "error",
             "source_files must not be empty");
    return false;
  }

  Context.Arguments = {"-fsyntax-only", "-x", "c", "-std=" + *Standard};
  std::vector<std::string> Includes;
  if (!appendStringArray(*Object, "include_dirs", Includes, State))
    return false;
  for (const std::string &Include : Includes)
    Context.Arguments.insert(Context.Arguments.end(), {"-I", Include});

  const llvm::json::Object *Defines = Object->getObject("defines");
  if (!Defines) {
    addIssue(State, "INVALID_COMPILE_CONTEXT", "error",
             "missing object field: defines");
    return false;
  }
  std::vector<std::pair<std::string, std::string>> SortedDefines;
  for (const auto &Entry : *Defines) {
    std::optional<llvm::StringRef> Value = Entry.second.getAsString();
    if (!Value) {
      addIssue(State, "INVALID_COMPILE_CONTEXT", "error",
               "defines values must be strings");
      return false;
    }
    SortedDefines.emplace_back(Entry.first.str(), Value->str());
  }
  std::sort(SortedDefines.begin(), SortedDefines.end());
  for (const auto &Define : SortedDefines)
    Context.Arguments.push_back("-D" + Define.first + "=" + Define.second);

  std::vector<std::string> ForceIncludes;
  if (!appendStringArray(*Object, "force_includes", ForceIncludes, State))
    return false;
  for (const std::string &Include : ForceIncludes)
    Context.Arguments.insert(Context.Arguments.end(), {"-include", Include});

  auto appendOptionalArg = [&](llvm::StringRef Key, llvm::StringRef Prefix) {
    std::optional<llvm::StringRef> Value = Object->getString(Key);
    if (Value && !Value->empty())
      Context.Arguments.push_back(Prefix.str() + Value->str());
  };
  appendOptionalArg("target_triple", "--target=");
  appendOptionalArg("cpu", "-mcpu=");
  appendOptionalArg("abi", "-mabi=");
  appendOptionalArg("sysroot", "--sysroot=");
  appendOptionalArg("resource_dir", "-resource-dir=");

  std::vector<std::string> ExtraArgs;
  if (!appendStringArray(*Object, "extra_args", ExtraArgs, State))
    return false;
  Context.Arguments.insert(Context.Arguments.end(), ExtraArgs.begin(), ExtraArgs.end());
  Context.Raw = *Object;
  return true;
}

std::set<std::string> scopedSources(const RunState &State) {
  std::set<std::string> Sources;
  if (FunctionName.empty()) {
    Sources = State.TargetFiles;
    return Sources;
  }
  for (const FunctionFact &Function : State.Functions)
    if (Function.Name == FunctionName)
      Sources.insert(normalizedFileKey(Function.File));
  return Sources;
}

bool diagnosticInScope(const DiagnosticFact &Diagnostic,
                       const std::set<std::string> &Sources) {
  if (Sources.empty())
    return FunctionName.empty() || Diagnostic.Source.empty();
  return Diagnostic.Source.empty() ||
         Sources.find(normalizedFileKey(Diagnostic.Source)) != Sources.end();
}

std::optional<int64_t>
initializerValue(const std::map<std::string, int64_t> &Initializers,
               llvm::StringRef Expression) {
  auto It = Initializers.find(compactText(Expression));
  if (It == Initializers.end())
    return std::nullopt;
  return It->second;
}

std::optional<bool> evaluateGuard(const llvm::json::Object &Guard,
                                  int64_t Actual) {
  const auto Op = Guard.getString("op");
  const auto Boundary = Guard.getInteger("boundary");
  if (!Op || !Boundary)
    return std::nullopt;
  if (*Op == "==")
    return Actual == *Boundary;
  if (*Op == "!=")
    return Actual != *Boundary;
  if (*Op == "<")
    return Actual < *Boundary;
  if (*Op == "<=")
    return Actual <= *Boundary;
  if (*Op == ">")
    return Actual > *Boundary;
  if (*Op == ">=")
    return Actual >= *Boundary;
  return std::nullopt;
}

std::optional<int64_t> guardedCallCapacity(
    const llvm::json::Object &Call,
    const std::map<std::string, int64_t> &Initializers) {
  const auto Capacity = Call.getInteger("max_occurrences");
  const llvm::json::Array *Guards = Call.getArray("guards");
  if (!Capacity || !Guards || Guards->empty() || *Capacity <= 0)
    return std::nullopt;

  int64_t Matches = 0;
  for (int64_t Index = 0; Index < *Capacity; ++Index) {
    bool Satisfied = true;
    for (const llvm::json::Value &RawGuard : *Guards) {
      const llvm::json::Object *Guard = RawGuard.getAsObject();
      if (!Guard)
        return std::nullopt;
      const auto Global = Guard->getString("global");
      const auto Field = Guard->getString("field");
      const auto Then = Guard->getBoolean("then");
      if (!Global || !Field || !Then)
        return std::nullopt;
      const std::string Path = Global->str() + "[" +
                               std::to_string(Index) + "]." + Field->str();
      const auto Value = initializerValue(Initializers, Path);
      if (!Value)
        return std::nullopt;
      const auto Result = evaluateGuard(*Guard, *Value);
      if (!Result)
        return std::nullopt;
      if (*Result != *Then) {
        Satisfied = false;
        break;
      }
    }
    if (Satisfied)
      ++Matches;
  }
  return Matches;
}

void applyGuardedCallCapacities(llvm::json::Object &Function,
                                const RunState &State) {
  auto *Calls = Function.getArray("calls");
  if (!Calls)
    return;
  for (llvm::json::Value &RawCall : *Calls) {
    llvm::json::Object *Call = RawCall.getAsObject();
    if (!Call)
      continue;
    if (const auto Capacity = guardedCallCapacity(*Call,
                                                  State.GlobalInitializers))
      if (*Capacity > 0)
        (*Call)["max_occurrences"] = *Capacity;
  }
}

void applyGlobalInitializers(llvm::json::Object &Function,
                             const RunState &State) {
  auto *Controls = Function.getArray("control_vars");
  if (Controls) {
    for (llvm::json::Value &Raw : *Controls) {
      llvm::json::Object *Control = Raw.getAsObject();
      if (!Control)
        continue;
      auto Var = Control->getString("var");
      if (!Var)
        continue;
      auto Value = initializerValue(State.GlobalInitializers, *Var);
      if (!Value) {
        if (auto Name = Control->getString("name"))
          Value = initializerValue(State.GlobalInitializers, *Name);
      }
      if (!Value) {
        if (const auto *Extensions = Control->getObject("extensions"))
          if (auto Canonical = Extensions->getString("canonical_var"))
            if (!Canonical->empty())
              Value = initializerValue(State.GlobalInitializers, *Canonical);
      }
      if (!Value)
        continue;
      (*Control)["constant_value"] = *Value;
      (*Control)["constant_reason"] =
          "Clang global/static initializer propagation";
    }
  }

  applyGuardedCallCapacities(Function, State);

  auto *Branches = Function.getArray("branches");
  if (!Branches)
    return;
  for (llvm::json::Value &Raw : *Branches) {
    llvm::json::Object *Branch = Raw.getAsObject();
    if (!Branch)
      continue;
    auto Kind = Branch->getString("kind");
    if (!Kind || *Kind == "switch")
      continue;
    auto *Atoms = Branch->getArray("atoms");
    if (!Atoms || Atoms->empty())
      continue;
    bool Known = true;
    std::vector<bool> Values;
    for (llvm::json::Value &AtomRaw : *Atoms) {
      llvm::json::Object *Atom = AtomRaw.getAsObject();
      if (!Atom) {
        Known = false;
        break;
      }
      auto Var = Atom->getString("var");
      auto Boundary = Atom->getInteger("boundary");
      auto Op = Atom->getString("op");
      if (!Var || !Boundary || !Op) {
        Known = false;
        break;
      }
      auto Left = initializerValue(State.GlobalInitializers, *Var);
      if (!Left) {
        if (const auto *Extensions = Atom->getObject("extensions"))
          if (auto Canonical = Extensions->getString("canonical_var"))
            if (!Canonical->empty())
              Left = initializerValue(State.GlobalInitializers, *Canonical);
      }
      if (!Left) {
        Known = false;
        break;
      }
      bool Value = false;
      if (*Op == "==")
        Value = *Left == *Boundary;
      else if (*Op == "!=")
        Value = *Left != *Boundary;
      else if (*Op == "<")
        Value = *Left < *Boundary;
      else if (*Op == "<=")
        Value = *Left <= *Boundary;
      else if (*Op == ">")
        Value = *Left > *Boundary;
      else if (*Op == ">=")
        Value = *Left >= *Boundary;
      else {
        Known = false;
        break;
      }
      Values.push_back(Value);
    }
    if (!Known || Values.empty())
      continue;
    bool Result = Values.front();
    if (auto Connective = Branch->getString("connective")) {
      for (size_t Index = 1; Index < Values.size(); ++Index) {
        if (*Connective == "&&")
          Result = Result && Values[Index];
        else if (*Connective == "||")
          Result = Result || Values[Index];
        else {
          Known = false;
          break;
        }
      }
    } else if (Values.size() != 1) {
      Known = false;
    }
    if (Known) {
      (*Branch)["constant_value"] = Result;
      std::string Reason = "Clang global/static initializer propagation";
      Reason += "; AST values: ";
      const auto Connective = Branch->getString("connective");
      for (size_t Index = 0; Index < Values.size(); ++Index) {
        if (Index != 0)
          Reason += (Connective && *Connective == "||") ? " || " : " && ";
        llvm::json::Object *Atom = (*Atoms)[Index].getAsObject();
        if (!Atom)
          continue;
        const auto Left = Atom->getInteger("boundary");
        const auto Op = Atom->getString("op");
        const auto Var = Atom->getString("var");
        auto Initial = Var ? initializerValue(State.GlobalInitializers, *Var)
                           : std::optional<int64_t>();
        if (!Initial) {
          if (const auto *Extensions = Atom->getObject("extensions"))
            if (auto Canonical = Extensions->getString("canonical_var"))
              Initial = initializerValue(State.GlobalInitializers, *Canonical);
        }
        Reason += Initial ? std::to_string(*Initial) : "?";
        Reason += " ";
        Reason += Op ? Op->str() : "?";
        Reason += " ";
        Reason += Left ? std::to_string(*Left) : "?";
      }
      (*Branch)["constant_reason"] = Reason;
    }
  }
}

void applyDerivedControlFacts(llvm::json::Object &Function,
                              const RunState &State) {
  auto *Controls = Function.getArray("control_vars");
  if (Controls) for (llvm::json::Value &Raw : *Controls) {
    llvm::json::Object *Control = Raw.getAsObject();
    if (!Control)
      continue;
    llvm::json::Object *Origin = Control->getObject("value_origin");
    if (!Origin)
      continue;
    auto Kind = Origin->getString("kind");
    auto Base = Origin->getString("base");
    auto Field = Origin->getString("field");
    if (!Kind || *Kind != "const_table_field" || !Base || !Field)
      continue;
    const std::string Prefix = Base->str() + "[";
    const std::string Suffix = "]." + Field->str();
    llvm::json::Object Values;
    for (const auto &Entry : State.GlobalInitializers) {
      const std::string &Path = Entry.first;
      if (Path.rfind(Prefix, 0) != 0 || Path.size() <= Prefix.size() ||
          Path.size() < Suffix.size() ||
          Path.substr(Path.size() - Suffix.size()) != Suffix)
        continue;
      const size_t Begin = Prefix.size();
      const size_t End = Path.size() - Suffix.size();
      if (End <= Begin)
        continue;
      const std::string Index = Path.substr(Begin, End - Begin);
      if (Index.find_first_not_of("0123456789") != std::string::npos)
        continue;
      Values[Index] = Entry.second;
    }
    if (!Values.empty())
      (*Origin)["table_values"] = std::move(Values);
  }

  // The same const-table provenance is needed for local field assignments
  // and their later whole-record copies.  Control variables are not the only
  // consumers of a table member, so annotate all effect origins here while
  // the translation-unit initializer map is available.
  auto annotateOrigin = [&](llvm::json::Object *Origin) {
    if (!Origin)
      return;
    auto Kind = Origin->getString("kind");
    auto Base = Origin->getString("base");
    auto Field = Origin->getString("field");
    if (!Kind || *Kind != "const_table_field" || !Base || !Field)
      return;
    const std::string Prefix = Base->str() + "[";
    const std::string Suffix = "]." + Field->str();
    llvm::json::Object Values;
    for (const auto &Entry : State.GlobalInitializers) {
      const std::string &Path = Entry.first;
      if (Path.rfind(Prefix, 0) != 0 || Path.size() <= Prefix.size() ||
          Path.size() < Suffix.size() ||
          Path.substr(Path.size() - Suffix.size()) != Suffix)
        continue;
      const size_t Begin = Prefix.size();
      const size_t End = Path.size() - Suffix.size();
      if (End <= Begin)
        continue;
      const std::string Index = Path.substr(Begin, End - Begin);
      if (Index.find_first_not_of("0123456789") != std::string::npos)
        continue;
      Values[Index] = Entry.second;
    }
    if (!Values.empty())
      (*Origin)["table_values"] = std::move(Values);
  };
  for (llvm::StringRef Key : {"local_value_effects", "global_write_effects",
                              "return_effects"}) {
    auto *Effects = Function.getArray(Key);
    if (!Effects)
      continue;
    for (llvm::json::Value &Raw : *Effects) {
      auto *Effect = Raw.getAsObject();
      if (!Effect)
        continue;
      annotateOrigin(Effect->getObject("origin"));
    }
  }
}

struct JsonSourceSpan {
  int64_t Start = 0;
  int64_t End = 0;
};

std::optional<JsonSourceSpan> spellingSpan(const llvm::json::Object &Value) {
  const llvm::json::Object *Provenance = Value.getObject("provenance");
  if (!Provenance)
    return std::nullopt;
  const llvm::json::Object *Spelling = Provenance->getObject("spelling");
  if (!Spelling)
    return std::nullopt;
  const std::optional<int64_t> Start = Spelling->getInteger("offset");
  const std::optional<int64_t> End = Spelling->getInteger("end_offset");
  if (!Start || !End || *Start <= 0 || *End <= *Start)
    return std::nullopt;
  return JsonSourceSpan{*Start, *End};
}

void applyBranchNesting(llvm::json::Object &Function) {
  auto *Branches = Function.getArray("branches");
  if (!Branches)
    return;

  for (size_t ChildIndex = 0; ChildIndex < Branches->size(); ++ChildIndex) {
    llvm::json::Object *Child = (*Branches)[ChildIndex].getAsObject();
    if (!Child || Child->getString("parent_bid"))
      continue;
    const std::optional<JsonSourceSpan> ChildSpan = spellingSpan(*Child);
    if (!ChildSpan)
      continue;

    std::optional<std::tuple<int64_t, size_t, std::string>> Best;
    for (size_t ParentIndex = 0; ParentIndex < ChildIndex; ++ParentIndex) {
      llvm::json::Object *Parent = (*Branches)[ParentIndex].getAsObject();
      if (!Parent)
        continue;
      const std::optional<JsonSourceSpan> ParentSpan = spellingSpan(*Parent);
      if (!ParentSpan || ParentSpan->Start > ChildSpan->Start ||
          ChildSpan->End > ParentSpan->End ||
          (ParentSpan->Start == ChildSpan->Start &&
           ParentSpan->End == ChildSpan->End))
        continue;
      const auto ParentFile = Parent->getString("file");
      const auto ChildFile = Child->getString("file");
      if (ParentFile && ChildFile && *ParentFile != *ChildFile)
        continue;
      const auto ParentId = Parent->getString("bid");
      if (!ParentId)
        continue;
      const int64_t Width = ParentSpan->End - ParentSpan->Start;
      const auto Candidate = std::make_tuple(Width, ParentIndex,
                                             ParentId->str());
      if (!Best || Candidate < *Best)
        Best = Candidate;
    }
    if (Best)
      (*Child)["parent_bid"] = std::get<2>(*Best);
  }
}

void applyFunctionPointerTargets(llvm::json::Object &Function,
                                 const RunState &State) {
  auto *Calls = Function.getArray("calls");
  if (!Calls)
    return;

  bool HasUnresolvedIndirect = false;
  for (llvm::json::Value &RawCall : *Calls) {
    llvm::json::Object *Call = RawCall.getAsObject();
    if (!Call)
      continue;
    const auto IsPointerCall = Call->getBoolean("ptr_call");
    if (!IsPointerCall || !*IsPointerCall)
      continue;
    const auto TableBase = Call->getString("table_base");
    const auto TableMember = Call->getString("table_member");
    if (!TableBase || !TableMember) {
      HasUnresolvedIndirect = true;
      continue;
    }
    const std::string Key = TableBase->str() + "." + TableMember->str();
    const auto It = State.FunctionPointerTargets.find(Key);
    if (It == State.FunctionPointerTargets.end() || It->second.size() != 1) {
      HasUnresolvedIndirect = true;
      continue;
    }

    const FunctionPointerTargetFact &Target = It->second.front();
    (*Call)["callee"] = jsonText(Target.Name);
    (*Call)["ptr_call"] = false;
    (*Call)["callee_kind"] = "direct";
    (*Call)["is_static"] = false;
    (*Call)["ret_type"] = jsonText(Target.ReturnType);
    llvm::json::Array Params;
    for (const FunctionPointerParameterFact &Param : Target.Params) {
      Params.push_back(llvm::json::Object{
          {"name", jsonText(Param.Name)},
          {"type", jsonText(Param.Type)},
          {"is_ptr", Param.IsPointer},
          {"is_const", Param.IsConst},
          {"is_written", false},
          {"type_info", llvm::json::Object(Param.TypeInfo)},
          {"access_paths", llvm::json::Array{}},
          {"write_effects", llvm::json::Array{}},
          {"write_status", "unknown"},
          {"extensions", emptyExtensions()}});
    }
    (*Call)["params"] = std::move(Params);
    if (llvm::json::Object *Extensions = Call->getObject("extensions"))
      (*Extensions)["resolved_via"] = "function_pointer_initializer";

    // Local variables passed by address are recorded while visiting the
    // indirect call, before this pass has resolved its table target.  Once a
    // unique target is known, promote only Rte_Read targets to the same
    // ``stub_param`` provenance used for direct calls.  This keeps local
    // temporaries out of the CSV while still allowing the corresponding
    // PTROUT value to drive later branch conditions.
    if (Target.Name.rfind("Rte_Read_", 0) == 0) {
      std::optional<int64_t> CallOffset;
      if (const llvm::json::Object *Provenance =
              Call->getObject("provenance")) {
        if (const llvm::json::Object *Spelling =
                Provenance->getObject("spelling"))
          CallOffset = Spelling->getInteger("offset");
      }
      if (CallOffset) {
        if (llvm::json::Array *Effects =
                Function.getArray("local_value_effects")) {
          for (llvm::json::Value &RawEffect : *Effects) {
            llvm::json::Object *Effect = RawEffect.getAsObject();
            if (!Effect)
              continue;
            llvm::json::Object *Origin = Effect->getObject("origin");
            if (!Origin)
              continue;
            const auto Kind = Origin->getString("kind");
            const auto OriginCallOffset = Origin->getInteger("call_offset");
            if (!Kind || *Kind != "indirect_param" ||
                !OriginCallOffset || *OriginCallOffset != *CallOffset)
              continue;
            (*Origin)["kind"] = "stub_param";
            (*Origin)["callee"] = jsonText(Target.Name);
          }
        }
      }
    }
  }

  if (!HasUnresolvedIndirect) {
    if (auto Status = Function.getString("status"))
      if (*Status == "UNSUPPORTED")
        Function["status"] = "OK";
    if (auto *Diagnostics = Function.getArray("diagnostics")) {
      llvm::json::Array Remaining;
      for (llvm::json::Value &RawDiagnostic : *Diagnostics) {
        const llvm::json::Object *Diagnostic = RawDiagnostic.getAsObject();
        const auto Code = Diagnostic ? Diagnostic->getString("code")
                                     : std::optional<llvm::StringRef>();
        if (Code && *Code == "INDIRECT_CALL_UNRESOLVED")
          continue;
        Remaining.push_back(std::move(RawDiagnostic));
      }
      *Diagnostics = std::move(Remaining);
    }
  }
}

using ut_agent::extractor::missingTypedField;

std::string documentStatus(const RunState &State) {
  if (FunctionName.empty() && State.TargetFiles.empty())
    return State.HasError ? "ERROR" : (State.HasWarning ? "PARTIAL" : "OK");

  const std::set<std::string> Sources = scopedSources(State);
  std::string ResolvedFunctionFile;
  bool ResolvedFunction = false;
  for (const FunctionFact &Function : State.Functions) {
    if (Function.Name != FunctionName)
      continue;
    ResolvedFunctionFile = Function.File;
    if (const auto Status = Function.Value.getString("status"))
      ResolvedFunction = *Status != "UNSUPPORTED";
    break;
  }
  bool HasScopedError = false;
  bool HasScopedWarning = false;
  for (const DiagnosticFact &Diagnostic : State.Diagnostics) {
    if (!diagnosticInScope(Diagnostic, Sources))
      continue;
    const llvm::json::Object *Issue = Diagnostic.Value.getAsObject();
    if (!Issue)
      continue;
    const auto Code = Issue->getString("code");
    if (ResolvedFunction &&
        normalizedFileKey(Diagnostic.Source) ==
            normalizedFileKey(ResolvedFunctionFile) &&
        Code && *Code == "UNSUPPORTED_FUNCTION_FACTS")
      continue;
    const std::optional<llvm::StringRef> Severity = Issue->getString("severity");
    HasScopedError = HasScopedError || (Severity && *Severity == "error");
    HasScopedWarning = HasScopedWarning || (Severity && *Severity == "warning");
  }
  if (State.Functions.empty() || HasScopedError)
    return "ERROR";

  for (const FunctionFact &Function : State.Functions) {
    if (Function.Name != FunctionName)
      continue;
    if (const std::optional<llvm::StringRef> FunctionStatus =
            Function.Value.getString("status"))
      if (*FunctionStatus == "UNSUPPORTED")
        return "UNSUPPORTED";
  }
  return HasScopedWarning ? "PARTIAL" : "OK";
}

llvm::json::Object makeDocument(const CompileContext &Context,
                                RunState &State) {
  std::sort(State.Functions.begin(), State.Functions.end(),
            [](const FunctionFact &Left, const FunctionFact &Right) {
              return std::tie(Left.File, Left.Line, Left.Name) <
                     std::tie(Right.File, Right.Line, Right.Name);
            });
  llvm::json::Array Functions;
  for (FunctionFact &Fact : State.Functions) {
    llvm::json::Object Function = Fact.Value;
    // Apply pointer-field/return-field facts discovered in any explicitly
    // supplied context source before the JSON document is emitted.  This is
    // still AST-only: no WinAMS CSV or reference project participates.
    for (const FunctionDefinitionFact &Definition : State.FunctionDefinitions) {
      const bool TargetRH = Fact.File.find("DOOR_RH") != std::string::npos;
      const bool TargetLH = Fact.File.find("DOOR_LH") != std::string::npos;
      const bool DefinitionRH =
          Definition.File.find("DOOR_RH") != std::string::npos;
      const bool DefinitionLH =
          Definition.File.find("DOOR_LH") != std::string::npos;
      // Some common modules (history_memory is one example) do not encode
      // the LH/RH side in the target path.  In that case either side's
      // generated accessor body is a valid structural field fact; retain the
      // deterministic first matching definition instead of discarding both.
      if ((TargetRH || TargetLH) &&
          ((TargetRH != DefinitionRH) || (TargetLH != DefinitionLH)))
        continue;
      if (llvm::json::Array *Calls = Function.getArray("calls")) {
        for (llvm::json::Value &CallValue : *Calls) {
          llvm::json::Object *Call = CallValue.getAsObject();
          if (!Call)
            continue;
          const auto Callee = Call->getString("callee");
          if (!Callee || *Callee != Definition.Name)
            continue;
          if (!Definition.ParamFields.empty() &&
              !Call->get("param_fields")) {
            llvm::json::Object ParamFields;
            for (const auto &Entry : Definition.ParamFields) {
              llvm::json::Array Fields;
              for (const std::string &Field : Entry.second)
                Fields.push_back(jsonText(Field));
              ParamFields[std::to_string(Entry.first)] = std::move(Fields);
            }
            (*Call)["param_fields"] = std::move(ParamFields);
          }
          if (!Definition.ReturnFields.empty() &&
              !Call->get("return_fields")) {
            llvm::json::Array Fields;
            for (const std::string &Field : Definition.ReturnFields)
              Fields.push_back(jsonText(Field));
            (*Call)["return_fields"] = std::move(Fields);
          }
        }
      }
    }
    applyFunctionPointerTargets(Function, State);
    applyGlobalInitializers(Function, State);
    applyDerivedControlFacts(Function, State);
    applyBranchNesting(Function);
    if (const std::string Missing = missingTypedField(Function);
        !Missing.empty()) {
      addIssue(State, "FUNCTIONIR_CONTRACT_MISSING", "error",
               (std::string("function ") + Fact.Name +
                " is missing typed field: " + Missing).c_str());
      Function["status"] = "UNSUPPORTED";
    }
    if (const auto Status = Function.getString("status"))
      Fact.Value["status"] = Status->str();
    Functions.push_back(llvm::json::Value(std::move(Function)));
  }
  llvm::json::Array Diagnostics;
  const std::set<std::string> ScopedSources = scopedSources(State);
  for (const DiagnosticFact &Diagnostic : State.Diagnostics) {
    if (!diagnosticInScope(Diagnostic, ScopedSources))
      continue;
    Diagnostics.push_back(Diagnostic.Value);
  }
  const std::string Status = documentStatus(State);
  llvm::json::Object CompileContextValue = Context.Raw;
  return llvm::json::Object{
      {"schema_version", 3},
      {"extractor", llvm::json::Object{{"name", ExtractorName.str()},
                                        {"version", ExtractorVersion.str()},
                                        {"clang_version", CLANG_VERSION_STRING}}},
      {"status", Status},
      {"compile_context", llvm::json::Value(std::move(CompileContextValue))},
      {"diagnostics", std::move(Diagnostics)},
      {"functions", std::move(Functions)}};
}

bool writeDocument(llvm::json::Object Document, RunState &State) {
  llvm::raw_ostream *Output = &llvm::outs();
  std::unique_ptr<llvm::raw_fd_ostream> FileOutput;
  if (OutputPath != "-") {
    std::error_code Error;
    FileOutput = std::make_unique<llvm::raw_fd_ostream>(OutputPath, Error);
    if (Error) {
      addIssue(State, "OUTPUT_WRITE_ERROR", "error",
               (std::string("cannot open output: ") + OutputPath).c_str());
      return false;
    }
    Output = FileOutput.get();
  }
  llvm::json::OStream JSON(*Output, 2);
  JSON.value(llvm::json::Value(std::move(Document)));
  *Output << "\n";
  Output->flush();
  return true;
}

} // namespace

int main(int argc, const char **argv) {
  llvm::cl::ParseCommandLineOptions(argc, argv, "deterministic FunctionIR extractor\n");
  if (ShowVersion) {
    llvm::outs() << ExtractorName << " " << ExtractorVersion << "\n"
                 << "LLVM " << CLANG_VERSION_STRING << "\n";
    return 0;
  }
  RunState State;
  TargetSet Targets;
  CompileContext Context;
  if (ContextPath.empty()) {
    addIssue(State, "MISSING_CONTEXT", "error", "--context is required");
  } else if (loadCompileContext(State, Context) && loadTargets(State, Targets)) {
    tooling::FixedCompilationDatabase Database(".", Context.Arguments);
    tooling::ClangTool Tool(Database, Context.Sources);
    ExtractActionFactory Factory(State, FunctionName, Targets);
    int Result = Tool.run(&Factory);
    State.ActiveSource.clear();
    // A bulk context may contain legacy translation units with unrelated
    // target-specific diagnostics.  Their AST facts are still useful (for
    // example function-pointer initializers); target presence and scoped
    // diagnostics decide the bulk result below.
    if (Result != 0 && Targets.empty())
      addIssue(State, "CLANG_TOOL_ERROR", "error", "ClangTool failed");
    if (!FunctionName.empty() && State.Functions.empty())
      addIssue(State, "FUNCTION_NOT_FOUND", "error",
               (std::string("function definition not found: ") + FunctionName).c_str());
    if (!Targets.empty()) {
      TargetSet Found;
      for (const FunctionFact &Function : State.Functions)
        Found.insert(targetKey(Function.File, Function.Name));
      if (Found.size() != Targets.size())
        addIssue(State, "TARGET_NOT_FOUND", "error",
                 "one or more requested function targets were not found");
    }
  }

  llvm::json::Object Document = makeDocument(Context, State);
  bool Written = writeDocument(std::move(Document), State);
  return Written && documentStatus(State) != "ERROR" ? 0 : 1;
}
