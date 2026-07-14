# Manual Session Learning Analysis

`ob learnings analyze` separates durable learnings from concrete work before any
clustering or promotion decision. It is intended for interactive review of
session-close backlogs, normally in batches of 50 summaries.

## Why the Classification Happens First

A session summary mixes several different kinds of information:

- what happened in one session;
- unfinished repository or operational work;
- context-specific decisions;
- durable cause-and-effect knowledge;
- rules that already exist in standards or skills;
- generic or unsupported synthesis.

Treating all of those as learnings produces a noisy review queue and encourages
repository maintenance work to masquerade as durable knowledge. The command
therefore assigns every extracted claim to one of seven routes before it
clusters anything.

| Kind | Meaning | Next step |
|---|---|---|
| `learning` | Evidence-backed, generalizable cause/effect claim that changes future behavior | Cluster and apply the review gate |
| `todo` | Concrete unfinished change to a repository, deployment, configuration, or operation | Keep in the work-item queue |
| `decision` | Context-specific choice and rationale | Review as a decision, not a universal rule |
| `standard_candidate` | Evidence-backed rule that may deserve normative enforcement | Review separately before editing a standard |
| `skill_candidate` | Evidence-backed reusable procedure with judgment or branching | Review separately before editing a skill |
| `duplicate_doctrine` | Rule explicitly tied to an existing standard or skill artifact | Count as confirmation; do not create duplicate knowledge |
| `noise` | Status narration, generic advice, incomplete causal claims, or unsupported synthesis | Discard from knowledge review |

## Deterministic Gates

A claim remains a `learning` only when it includes all of the following:

- an observed behavior or outcome;
- a cause or mechanism;
- a future behavior change;
- concrete source evidence;
- a generalizability signal.

Imperative concrete changes such as "fix", "update", "add", "implement",
"ensure", or "configure" route to `todo`, as do explicit pending statements
using terms such as "must", "should", or "not yet". A descriptive statement
about completed work does not become a TODO merely because the model emitted an
action field. Standard and skill candidates must also satisfy the causal
evidence contract. A `duplicate_doctrine` candidate must name the existing
artifact it duplicates.

In multi-summary batches, learning, standard, and skill candidates require
evidence. TODO and decision candidates may omit it. Whenever evidence is
present, every item must be a verbatim excerpt of the title, content, or
narrative of the specific source summary named by `source_memory_id`, and at
least one excerpt must be unique within the input batch. Candidates with
missing required evidence, shared boilerplate only, paraphrased evidence, or
cross-summary evidence are rejected before routing, preventing adjacent session
summaries from borrowing each other's learnings. Invalid optional evidence on a
TODO or decision is stripped instead of discarding an otherwise valid action or
choice. Identical summaries or candidates supported only by shared boilerplate
remain held out intentionally; this favors precision over recall during manual
review.

Only validated `learning` candidates enter semantic clustering. A cluster is
review-eligible only when it has support from at least two distinct source
session summaries. Severity alone never promotes a singleton. Held singletons
retain their severity and evidence so later runs can match genuine recurrence.

Clustering uses two proposal sources and one authoritative precision gate. The
first LLM pass proposes possible equivalent groups, while a batch embedding of
the causal learning fields independently shortlists semantically close pairs.
Neither proposal source can merge candidates. The configured LLM must explicitly
confirm each proposed pair has the same causal mechanism and compatible future
behavior. Pairs that only share a topic, component, vocabulary, or evidence
remain separate. Confirmed pairs only combine larger groups when every
cross-group cluster pair was also confirmed, preventing a broad middle claim
from transitively joining incompatible rules. The shortlist is bounded, and a
fair bounded budget reserves capacity for proposal-only pairs that fall below
the embedding threshold. When that proposal budget is saturated, sub-threshold
pairs are selected before proposal pairs already covered by semantic proximity.
Pair identities and member order are canonical, so
first-pass response ordering cannot duplicate adjudication work. A failed
reconciliation preserves the conservative singleton partition. An incomplete
embedding response is logged and also falls back to singletons instead of
silently disabling reconciliation. Failure in either LLM pass also fails closed
to singletons. Runs with zero or one learning candidate skip clustering calls.
Review clusters include every member's causal fields and source evidence so a
human can audit equivalence without reopening each session summary.

## Usage

Analyze the newest 50 summaries:

```bash
ob learnings analyze
```

The installed CLI uses the authenticated Open Brain MCP endpoint by default, so
database and LLM credentials remain on the server. The caller needs the
`evolution` scope because the command sends a bounded batch of stored summaries
to the configured server-side LLM.

Filter the read by project and writer source:

```bash
ob learnings analyze \
  --limit 50 \
  --project open-brain \
  --source session-close
```

Machine-readable output uses the global JSON flag:

```bash
ob --json learnings analyze
```

The command uses the configured Open Brain LLM provider. Session summaries are
treated as untrusted evidence in both extraction and clustering prompts. An
optional `--model` value overrides the configured model for one manual run.

Operators with a local `DATABASE_URL` can explicitly bypass MCP transport:

```bash
ob learnings analyze --direct
```

`OB_DIRECT=1` provides the same explicit opt-in. Direct mode is not the default.

## Read-Only Boundary

The database query runs in a read-only, repeatable-read transaction. The
command does not use retrieval search, so it does not increment access counts
or apply recall-triggered priority changes. It does not:

- change memory or lifecycle status;
- change priority;
- save extracted memories;
- create beads or other work items;
- edit standards or skills;
- activate a scheduler.

The operator reviews the separated queues and decides what, if anything, should
be persisted or promoted in a later explicit workflow.
