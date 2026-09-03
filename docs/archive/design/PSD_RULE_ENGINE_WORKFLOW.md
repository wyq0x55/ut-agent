> Historical document. Not normative. Current architecture: ../../architecture.md
# PSD Semantic Rule Engine Workflow

> Version: 1.0  
> Status: Draft  
> Owner: Rule Mining Team  
> Goal: Build a cross-project PSD Semantic Rule Engine rather than a historical CSV replay engine.

---

# 1. Vision

The final target is:

```text
Source Code
+ Include Paths
+ Compile Macros
+ Approved PSD Rule Pack

↓

WinAMS TestCsv
Stub Source
DefineVar.dat
```

without reading:

```text
Project Golden TestCsv
Project Historical CSV
Project-specific Scenario Database
```

during generation.

---

# 2. Key Definitions

## Historical Replay Engine

Behavior:

```text
Read Historical CSV

↓

Learn Function Scenarios

↓

Generate Similar CSV
```

Characteristics:

- High replay accuracy
- Weak generalization
- Project dependent
- Not scalable

---

## PSD Semantic Rule Engine

Behavior:

```text
Read Source AST

↓

Match Semantic Rules

↓

Generate Test Cases

↓

Render WinAMS CSV
```

Characteristics:

- Cross-project reuse
- Rule-driven
- Scalable
- Maintainable

---

# 3. Core Principles

## Principle 1

Historical TestCsv is training material.

Historical TestCsv is NOT generation input.

---

## Principle 2

Function Scenario != Semantic Rule

Example:

Function Scenario:

```text
FunctionA
  -> Case1
  -> Case2

FunctionB
  -> Case1
  -> Case2
```

Semantic Rule:

```text
IF_CONST_EQUALITY

Pattern:
    variable == constant

Cases:
    MIN
    CONST-1
    CONST
    CONST+1
    MAX
```

---

## Principle 3

Only source code semantics can drive generation.

Generation must rely on:

```text
AST
CFG
Type Information
Rule Pack
```

Must not rely on:

```text
Function Name
Project Name
Historical CSV
```

---

## Principle 4

Not every pattern becomes a rule.

Project-specific behavior should remain:

```text
PROJECT_SPECIFIC
```

unless it appears repeatedly across multiple projects.

---

# 4. High Level Lifecycle

```text
Project

↓

Corpus Extraction

↓

Rule Compression

↓

Rule Pack

↓

New Project Validation

↓

Difference Analysis

↓

Rule Evolution

↓

Regression Validation

↓

Next Project
```

---

# 5. Phase 1 - Corpus Extraction

## Input

```text
Source Code
TestCsv
Stub
Project Profile
```

## Output

```text
Corpus
```

Example:

```text
if_const_equal
if_const_compare
if_var_equal
if_var_compare
switch
and_condition
or_condition
nested_if
function_call
pointer_writeback
loop
register_access
```

## Objective

Capture facts only.

Do NOT generate rules yet.

---

# 6. Phase 2 - Rule Compression

## Input

```text
Corpus
```

## Output

```text
PSD Rule Pack
```

Example:

```json
{
  "rule_id": "PSD_IF_CONST_EQUAL",

  "ast_pattern":
      "variable == constant",

  "generator":
      [
        "MIN",
        "CONST-1",
        "CONST",
        "CONST+1",
        "MAX"
      ]
}
```

---

## Compression Targets

### Condition Rules

```text
var == const
var != const

var < const
var <= const

var > const
var >= const

var == var
var != var
```

---

### Compound Rules

```text
AND
OR
Nested IF
Else-IF Priority
```

---

### Switch Rules

```text
switch
case
default
case ±1
MIN/MAX
```

---

### Loop Rules

```text
for
while
do-while
```

---

### Stub Rules

```text
Call Count
Return Value
Argument Capture
Pointer Output
```

---

# 7. Phase 3 - Local Validation

Validate:

```text
Rule Pack
VS
Project Golden
```

---

## Important

Do NOT require 100% match.

Forcing 100% early generally produces:

```text
Project-Specific Rules
```

instead of:

```text
Cross-Project Rules
```

---

## Classification

### VALIDATED

Rule successfully explains historical behavior.

---

### PROJECT_SPECIFIC

Only appears in this project.

Do not immediately promote.

---

### UNKNOWN_PATTERN

Cannot be explained.

Candidate for future investigation.

---

### BUG

Incorrect implementation.

Must be fixed.

---

## Promotion Threshold

Recommended:

```text
VALIDATED >= 80%
```

before moving to the next project.

---

# 8. Phase 4 - New Project Validation

## Input

```text
New Project
+
Current Rule Pack
```

## Restriction

Generation must NOT read:

```text
Golden TestCsv
```

---

## Flow

```text
Source

↓

AST

↓

Rule Matching

↓

Generate TestCsv

↓

Compare With Golden

↓

Difference Analysis
```

---

# 9. Difference Categories

## RULE_MISS

Rule exists.

Rule failed to trigger.

---

## NEW_PATTERN

Pattern not covered by current Rule Pack.

Potential new rule candidate.

---

## PROJECT_SPECIFIC

Only useful for the current project.

Keep local.

Do not promote.

---

## BUG

Implementation defect.

Must be fixed.

---

# 10. Rule Promotion Strategy

A pattern becomes a PSD rule only when:

```text
Appears in multiple functions

AND

Appears in multiple projects
```

---

## Example

Project A:

```c
if(a == b)
```

Project B:

```c
if(temp == target)
```

Project C:

```c
if(speed == ref)
```

Promote:

```text
PSD_VAR_EQUALITY
```

---

## Example NOT To Promote

Only appears once:

```c
if(reg == 0x55AA)
```

Classification:

```text
PROJECT_SPECIFIC
```

Do NOT add to Rule Pack.

---

# 11. Leave-One-Project-Out Validation

Mandatory validation stage.

---

## Example

Projects:

```text
A
B
C
D
```

Validation:

```text
Train:
A+B+C

Validate:
D
```

---

```text
Train:
A+B+D

Validate:
C
```

---

```text
Train:
A+C+D

Validate:
B
```

---

```text
Train:
B+C+D

Validate:
A
```

---

## Purpose

Verify:

```text
Rule Generalization
```

rather than:

```text
Project Memorization
```

---

# 12. Regression Validation

Whenever Rule Pack changes:

```text
v0.1
↓
v0.2
↓
v0.3
```

all historical projects must be re-evaluated.

---

## Validation Mode

Use only:

```text
Source
Include
Macros
Rule Pack
```

Generation must run with:

```text
No Golden Input
```

---

# 13. Metrics

## Rule Coverage

```text
Matched AST Nodes
/
Total AST Nodes
```

Target:

```text
>95%
```

---

## Semantic Match Rate

```text
Generated Cases
VS
Golden Cases
```

Target:

```text
>90%
```

---

## Project Specific Ratio

Target:

```text
<5%
```

---

## Unknown Pattern Ratio

Target:

```text
<2%
```

---

# 14. Recommended Project Sequence

## Project 1

```text
N-O2504-PHD-020

↓

Corpus

↓

Rule Pack v0.1
```

---

## Project 2

```text
N-O2505-PSD-036

↓

No Golden Generation

↓

Difference Analysis

↓

Rule Pack v0.2
```

---

## Project 3

```text
N-O2508-PSD-101

↓

No Golden Generation

↓

Difference Analysis

↓

Rule Pack v0.3
```

---

## Project 4

```text
N-O2602-PSD-228

↓

No Golden Generation

↓

Difference Analysis

↓

Rule Pack v0.4
```

---

# 15. Recommended Success Strategy

## Option A (Not Recommended)

```text
Project A

↓

100% Replay

↓

Project B

↓

100% Replay

↓

Project C
```

Problem:

```text
Rule Pack becomes
Historical CSV Compression
```

---

## Option B (Recommended)

```text
Project A

↓

Rule Extraction

↓

Project B

↓

Rule Evolution

↓

Project C

↓

Rule Evolution

↓

Project D

↓

Rule Evolution

↓

Final Regression
```

Advantages:

- Better generalization
- Less project pollution
- More stable Rule Pack
- Faster long-term progress

---

# 16. V1 Exit Criteria

The completion condition is NOT:

```text
All historical CSVs
100% identical
```

---

The completion condition IS:

```text
Choose a completely new PSD project

Without reading Golden TestCsv

Use only:

Source
Include
Macros
Rule Pack

Generate:

TestCsv
Stub

And achieve:

Rule Coverage >95%
Semantic Match >90%
```

---

# 17. Definition of Success

Bad:

```text
Historical CSV Replay Engine
```

Good:

```text
PSD Semantic Rule Engine
```

Final lifecycle:

```text
Project
↓
Corpus
↓
Rule Compression
↓
Rule Pack
↓
New Project
↓
Difference Analysis
↓
Rule Evolution
↓
Regression
↓
Rule Pack
```

Continuous iteration produces a reusable, cross-project PSD semantic knowledge base.

---

# 18. Repository Execution Contract

The following command sequence is the executable form of this workflow:

```text
rules collect (historical project)
        ↓
rules compress (one or more project corpora)
        ↓
rules approve (only reviewed cross-function/cross-project candidates)
        ↓
gen (new project; no --reference-csv)
        ↓
external Golden comparison and difference classification
```

The compressed report can be approved directly (the command handles its
`candidate_pack` wrapper):

```text
ut-agent rules approve psd-cross-project-v1.json \
  --id semantic.<family-id> --authority <record> --reason <evidence> \
  -o psd-cross-project-v1-approved.json
```

`rules compress` classifies each normalized branch family explicitly as
`PROJECT_SPECIFIC`, `CROSS_FUNCTION`, or `CROSS_PROJECT`. A rule with
`scope=function:*` is not considered reusable unless its evidence satisfies the
configured function/project thresholds.

Once a `CROSS_PROJECT` family is reviewed and approved, it is a formal
executable rule: the next project's generation loads it, matches the current
AST against its normalized family signature, and records the rule ID and
evidence in every synthesized intent that uses it. Approval therefore enables
execution immediately; it does not mean that the new project's coverage or
oracle is already proven. A miss becomes a versioned difference/evidence item
for the next compression cycle rather than silently mutating the approved
pack.

For the current PSD sample, N-O2504-PHD-020 has four function-level scenario
matrices, two within-project semantic families, and four profile strategies. It
provides the first semantic evidence for the PSD profile. After merging
N-O2605-ELT-034, two families satisfy the cross-project gate and may be
promoted to the approved pack. N-O2605-ELT-034 remains the blind-validation
project; its TestCsv may be read only after generation for difference analysis.

The generated intent manifest is an audit artifact, not a generation input.
Historical scenario rows must never be copied into an approved cross-project
rule pack.
