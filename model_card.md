# BugHound Mini Model Card (Reflection)

Completed after running BugHound in both Heuristic and Gemini modes.

---

## 1) What is this system?

**Name:** BugHound

**Purpose:** BugHound is a small agentic code-review tool. Given a Python snippet, it runs a five-step
loop — plan, analyze, act, test, reflect — to detect potential issues, propose a fix, score how risky
that fix is, and decide whether the fix is safe enough to auto-apply or should instead be routed to a
human for review. It is explicitly *not* meant to be a fully autonomous code-fixer; the point of the
exercise is the reliability scaffolding around the LLM (or heuristic) call, not the fix quality alone.

**Intended users:** Students learning agentic workflows and AI reliability concepts — specifically, how
to reason about trusting model output, designing fallback paths, and building guardrails that gate
automatic action.

---

## 2) How does it work?

1. **PLAN** — a fixed, one-line log entry announcing the workflow; no branching happens here.
2. **ANALYZE** (`BugHoundAgent.analyze`, [bughound_agent.py](bughound_agent.py)) — detects issues.
   - If no client is attached (`client=None`), or the client's response can't be trusted, this step uses
     `_heuristic_analyze`: three plain-text pattern checks for `print(`, a bare `except:`, and `TODO`
     comments (each mapped to a fixed type/severity/message).
   - If a client is attached, it sends the code to the LLM (Gemini) with a system prompt demanding
     "ONLY valid JSON," then validates the response in two layers: (a) does it parse as a JSON array at
     all, and (b) does every item in that array have a non-empty `msg` and a recognized severity
     (`low`/`medium`/`high`)? If parsing fails, or every item fails content validation, the agent falls
     back to `_heuristic_analyze` and logs why.
3. **ACT** (`propose_fix`) — proposes a fix for detected issues.
   - No issues → returns the original code unchanged (no-op fix).
   - No client, or the LLM call raises/returns empty output → `_heuristic_fix`: targeted regex
     substitutions (bare `except:` → `except Exception as e:` with a comment; `print(` → `logging.info(`,
     with an `import logging` inserted).
   - Otherwise, the LLM is asked to rewrite the whole file to address the listed issues, "preserving
     behavior" and making "the smallest changes needed" (per `prompts/fixer_system.txt`).
4. **TEST** (`assess_risk`, [reliability/risk_assessor.py](reliability/risk_assessor.py)) — always
   local, deterministic, non-LLM. Starts at a score of 100 and deducts points for issue severity and for
   structural red flags in the diff (see Section 4), producing a score, a `low`/`medium`/`high` level,
   and a `should_autofix` boolean.
5. **REFLECT** — just reads `should_autofix` and logs one of two fixed messages; no LLM call, no new
   logic.

**Heuristics vs. Gemini, summarized:** heuristics only ever run ANALYZE/ACT locally with zero network
calls, and are also the mandatory fallback whenever the LLM path is unavailable or untrustworthy.
Gemini, when configured, replaces both the ANALYZE and ACT steps with model calls — but TEST and
REFLECT are never delegated to the model; they always run locally against whatever code and issues
came out of steps 2–3, regardless of which path produced them.

---

## 3) Inputs and outputs

**Inputs tested** (all short, single-function Python snippets, 5–13 lines):

| Snippet | Shape | Source |
|---|---|---|
| `sample_code/cleanish.py` | one function, already using `logging`, no obvious issues | provided sample |
| `sample_code/mixed_issues.py` | one function with a `# TODO` comment, a `print(...)`, and a bare `try/except:` | provided sample |
| `sample_code/flaky_try_except.py` | a file-loading function wrapping `open()`/`read()` in a bare `except:` | provided sample, opened while testing the JSON-fallback path |
| hand-crafted "docstring trap" snippet | a function with correct real error handling (`if b == 0: return None`), but whose **docstring** contains an illustrative `try/except:` code example | written this session specifically to probe string-literal vs. live-code confusion |

**Outputs observed:**

- **Issue types:** heuristics only ever produce three fixed categories — `Code Quality` (print),
  `Reliability` (bare except), `Maintainability` (TODO). Gemini produced a much wider and more varied
  set of freeform categories across runs — `Configuration`, `Robustness`, `Documentation`, `design`,
  `error-handling`, `logic` — with severities that don't always match how a human would grade the same
  problem (see Section 6).
- **Fixes proposed:** heuristic fixes are minimal, mechanical, line-local substitutions. Gemini's fixes
  ranged from equally-scoped stylistic changes up to a full rewrite that changed the function's
  error-handling *behavior* (see Section 5, item 2).
- **Risk reports:** scores observed ranged from 0 (no fix produced) to 100 (no issues, no changes) to
  30/10 (multiple medium/high severity issues) to 55–70 (a single high or a couple of medium issues).
  `should_autofix` was `True` only for the two truly clean cases (`cleanish.py` and the docstring-trap
  case, heuristic mode) and `False` in every run that had a real Medium- or High-severity issue —
  consistent with the severity gate described in Section 4.

---

## 4) Reliability and safety rules

Two rules from `assess_risk` in [reliability/risk_assessor.py](reliability/risk_assessor.py):

### Rule 1 — Severity-based score deduction + severity gate on `should_autofix`

- **What it checks:** subtracts 40/20/5 points per High/Medium/Low-severity issue (lines 39–47), and —
  after a guardrail added this session — `should_autofix` is forced to `False` outright if *any* issue
  has Medium or High severity, independent of the final score.
- **Why it matters:** ties eligibility for automatic application to how dangerous the flagged problem
  is, not just an aggregate number. Before the gate existed, a single Medium-severity issue only cost 20
  points (100 → 80), which still cleared the `>= 75` "low risk" autofix threshold — so a fix for
  something the agent itself called "Medium" severity could be silently auto-applied.
- **False positive it could cause:** if an analyzer over-labels a trivial stylistic nit as "Medium" (this
  session's Gemini run labeled a missing docstring as "Low" but a missing type check as "Medium" on a
  function that was otherwise fine) — legitimate, low-risk fixes get needlessly routed to human review.
  Annoying friction, not dangerous.
- **False negative it could miss:** the whole rule is only as good as the upstream issue detector. If the
  analyzer fails to notice a real problem at all (as heuristics did on `cleanish.py`, reporting zero
  issues where Gemini found three legitimate ones), there's nothing for this rule to deduct against —
  score stays at 100 and `should_autofix` is `True` for a fix a domain expert might still want to review.

### Rule 2 — Structural "much shorter than original" check

- **What it checks:** `len(fixed_lines) < len(original_lines) * 0.5` (lines 52–54) — flags when the
  proposed fix is less than half the line count of the original, as a proxy for "did the model quietly
  delete logic."
- **Why it matters:** catches a real LLM failure mode — a lazy or lossy rewrite that drops functionality
  instead of making a targeted fix — without needing to understand what the code actually does.
- **False positive it could cause:** legitimately simplifying verbose or redundant code (e.g. collapsing
  several near-duplicate `print()` calls into a loop, as in `sample_code/print_spam.py`) would trip this
  check even though the fix is correct and desirable.
- **False negative it could miss:** the check is one-directional — it only looks for *shrinkage*, never
  growth. This session's live Gemini run against `mixed_issues.py` produced a fix that *grew* from 8 to
  17 lines while changing the function's actual error-handling behavior (returning `0` on error became
  raising `ValueError`/`TypeError`) — a substantive behavior change this rule cannot see at all. In that
  run the fix was still correctly blocked from autofix, but only because the severity and
  return/except-related checks happened to catch it — not this one.

---

## 5) Observed failure modes

**1. A time BugHound missed an issue it should have caught:**
Heuristic mode on `sample_code/cleanish.py` reported **zero issues** and marked the (unchanged) code
`should_autofix: True` at a perfect score of 100. Running the identical snippet through Gemini surfaced
three real, legitimate concerns the pattern-matching heuristics have no way to see: missing
`logging.basicConfig()` (so the `logging.info` call may not even be visible), no type validation on
`add(a, b)`, and a missing docstring. Heuristics are keyword-only, so anything outside their three fixed
patterns (print/except/TODO) is invisible to them by construction — a real reliability gap whenever the
Gemini path is unavailable.

**2. A time BugHound suggested a fix that felt risky and went beyond what was needed:**
Gemini's fix for `mixed_issues.py` didn't just replace the bare `except:` with a specific exception type
(the minimal, expected fix) — it restructured the function to **raise** `ValueError`/`TypeError` instead
of returning `0` on error, reasoning that "returning 0 on error is ambiguous." That's a legitimate
observation, but it silently changes the function's public contract (any caller relying on the old
fallback-to-`0` behavior would now get an unhandled exception) despite the fixer's own system prompt
explicitly saying "Preserve behavior whenever possible... make the smallest changes needed." The risk
report correctly flagged this as high-risk (score 10, `should_autofix: False`), so the guardrail worked —
but the *fix itself* is a clear instance of the model not honoring its own instructions, and is exactly
the kind of change that must never be auto-applied.

**(Bonus, already remediated this session, kept for the record):** before a guardrail was added,
heuristic ANALYZE treated a bare `except:` that appeared only inside a **docstring's illustrative
example** — not real code — as a real `Reliability | High` issue, and the heuristic fixer then corrupted
the docstring text trying to "fix" it. This was a false positive plus over-editing in one; it's fixed
now by masking triple-quoted string contents before running the regex checks (see `_mask_string_literals`
in `bughound_agent.py`).

---

## 6) Heuristic vs Gemini comparison

- **What Gemini detected that heuristics did not:** anything semantic — missing docstrings, missing
  input validation, ambiguous return values, missing logging configuration, and (on the docstring-trap
  snippet) a genuine critique of the function's design ("returning `None` for invalid input is less
  explicit than raising `ValueError`") that required actually understanding what the code and its
  documentation meant, not just pattern-matching text.
- **What heuristics caught consistently:** the three patterns they're built for (print/bare-except/TODO)
  were caught every single time they were genuinely present in live code, with zero false negatives in
  testing — and, after the docstring-masking fix, zero false positives from string-literal content
  either. Heuristics are narrow but dependable within that narrow scope.
- **How the proposed fixes differed:** heuristic fixes are minimal, line-local, mechanical substitutions
  that never touch surrounding code. Gemini's fixes were consistently more thorough (adding docstrings,
  type hints, `logging.basicConfig`) but also consistently *larger* than strictly necessary, and in one
  case changed actual error-handling behavior rather than just its style (Section 5, item 2).
- **Did the risk scorer agree with your intuition?** Mostly yes — every run with a real Medium/High issue
  correctly landed on `should_autofix: False`, matching the intuition that these fixes need a human
  look. One notable discrepancy: **the configured default model (`gemma-3-27b-it`) returned a 404 "model
  not found" error on every single request this session** — `GeminiClient.complete()` swallows that
  exception internally and returns `""`, which the agent then logs as `"LLM output was not parseable
  JSON. Falling back to heuristics."` That's a misleading diagnosis: the real cause was an API
  configuration error, not the model producing bad JSON, and the log gives no way to tell the two apart.
  Switching to `gemini-2.5-flash` (a currently valid model per `client.models.list()`) produced the real
  Gemini output quoted throughout this document.

---

## 7) Human-in-the-loop decision

**Scenario:** the agent proposes a fix whose control-flow *outcome* for an error path differs from the
original — e.g. the original function returns a sentinel value (`0`, `None`) on failure, and the fixed
code instead raises an exception (or vice versa), as happened with Gemini's `mixed_issues.py` fix in
Section 5. Even when the reasoning behind the change is sound, this is a breaking change to the
function's calling contract, and no automated heuristic can know whether existing callers depend on the
old behavior.

- **Trigger:** compare, for each `try/except` block present in both the original and fixed code, whether
  the set of terminal actions in the corresponding error path (`return <value>` vs. `raise <Exception>`)
  changed. If it did, treat this the same way the severity gate already does for Medium/High issues:
  force `should_autofix = False` regardless of score.
- **Where to implement:** `risk_assessor.py`, as a new structural check alongside the existing
  return/except line-52-64 checks — it's a property of the diff itself, not of the analyzer's own issue
  labels, so it belongs in the same "structural change checks" section, not the agent workflow or the UI.
- **Message to show the user:** something like *"This fix changes how errors are handled — the original
  code returned a value on failure, but the proposed fix raises an exception instead. Auto-apply is
  disabled; please review whether calling code expects the old behavior before applying this fix."*

---

## 8) Improvement idea

**Stop swallowing the real error inside `GeminiClient.complete()`.** Right now
([llm_client.py](llm_client.py):49-62), any exception from the Gemini API — auth failure, invalid model
name, rate limit, network error — is caught and silently converted to an empty string. That empty string
then flows into `_parse_json_array_of_issues("")`, which fails to parse, so `bughound_agent.py` logs
*"LLM output was not parseable JSON. Falling back to heuristics"* — a message that is true in a narrow
technical sense but actively misleading about the actual cause, as this session's testing showed (a
misconfigured default model name masqueraded as a JSON-formatting problem for every single request).

The fix is small: let `GeminiClient.complete()` re-raise the exception instead of catching it (or catch
only the genuinely expected "response was blocked/empty" case and re-raise everything else). The calling
code in `bughound_agent.py` already has an `except Exception as e: self._log("ANALYZE", f"API Error:
{str(e)}...")` handler built for exactly this — it's just never exercised today because `GeminiClient`
never lets a real error reach it. No new guardrail logic is needed, just removing the layer that hides
the signal that already exists. This would measurably improve reliability by making failure logs
actually diagnostic (a developer or student could immediately tell "the model name is wrong" apart from
"the model responded with garbage"), without adding any new complexity to the system.
