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
"ensure", or "configure" route to `todo` only when a verbatim source excerpt
also states that the work remains unfinished. The action and target are not
enough: a model-generated imperative is not source evidence. A descriptive
statement about completed work does not become a TODO merely because the model
emitted an action field. Standard and skill candidates must also satisfy the
causal evidence contract. A `duplicate_doctrine` candidate must name the
existing artifact it duplicates.

Candidates are atomic: their statement, observation, cause, future behavior,
and evidence must describe one mechanism. Fields from adjacent bullets or
independent findings must not be combined. A key decision remains a decision,
but its heading does not suppress a separate evidence-backed causal finding in
the same summary.

Explicit unresolved markers are checked in verbatim evidence as well as in the
candidate fields. Evidence may be a direct imperative or a source statement
such as "must still", "not yet", "still pending", or "has not been". A
historical gap such as "was missed" is not sufficient when the summary records
the subsequent fix. A normative "must" or "should" that states future behavior
is also not evidence of unfinished work unless it is part of an explicit pending
marker such as "must still" or "should be filed". Recovered work uses the
matching candidate field as its action and the source project as a fallback
target. An observation marked as a key decision is routed to decisions only when
the candidate is
non-generalizable. As a deterministic backstop, a TODO whose observation or
evidence explicitly says the work was added, implemented, fixed, merged,
deployed, or otherwise completed is removed from the work queue unless stronger
pending evidence identifies unfinished work. Completed candidates and
unsupported TODO labels with a full causal contract return to learning review;
incomplete ones route to noise.

In multi-summary batches, learning, standard, and skill candidates require
evidence. A TODO additionally requires evidence to survive deterministic
routing; decisions may omit it. Whenever evidence is present, every item must
be a verbatim excerpt of the title, content, or
narrative of the specific source summary named by `source_memory_id`, and at
least one excerpt must be unique within the input batch. Candidates with
missing required evidence, shared boilerplate only, paraphrased evidence, or
cross-summary evidence are rejected before routing, preventing adjacent session
summaries from borrowing each other's learnings. Invalid optional evidence on a
TODO or decision is stripped. A TODO with stripped or absent pending evidence
then routes to learning or noise rather than entering the work queue. Identical
summaries or candidates supported only by shared boilerplate remain held out
intentionally; this favors precision over recall during manual review.

Summaries that explicitly advertise causal material through headings such as
"Key learning", "Key findings", "Challenges encountered", "Surprising
findings", or "Root cause" receive a dedicated extraction call. Routine status
summaries remain grouped in bounded batches. This prevents a learning-rich
session from being omitted because it competed with unrelated summaries in the
same model context. The focused prompt explicitly treats a completed recovery as
possible evidence of a durable failure mechanism, including divergence between
tracker state and the actual external result. If a focused call still returns no
deterministically valid learning, the command retries that summary exactly once
with an explicit coverage reminder. Decisions and other distinct first-pass
claims are preserved, while duplicate atomic statements are reconciled in favor
of a validated learning and receive stable source-derived candidate IDs. The
retry is bounded and remains part of the manual, read-only analysis. If both
model passes still miss an explicit conditional bullet that contains its own
`Recovery:` safeguard, a conservative parser recovers that one causal contract
directly from the quoted source text. It does not synthesize generic lessons from
ordinary status bullets or from adjacent summary fields.

Only validated `learning` candidates enter semantic clustering. A cluster is
review-eligible only when it has support from at least two distinct source
session summaries. Severity alone never promotes a singleton. Held singletons
retain their severity and evidence so later runs can match genuine recurrence.

Clustering uses three proposal sources and two independent precision gates. The
first LLM pass proposes possible equivalent groups, while a batch embedding of
the causal learning fields independently shortlists semantically close pairs. A
bounded TF-IDF lexical pass adds cross-session pairs that share at least three
rare causal terms, covering false splits that fall below the embedding cutoff or
are omitted from a large first-pass proposal. The lexical pass excludes
same-session pairs and is capped at the same reconciliation budget as the other
sources. None of the proposal sources can merge candidates. The configured LLM
must first explicitly confirm each proposed pair has the same causal mechanism
and compatible future behavior. An independent adversarial verification pass
then audits only those tentative confirmations and defaults to rejection when it
finds a material difference in the evidenced causal mechanism or governing
failure invariant. Different phase-specific safeguards remain compatible when
they directly mitigate that same failure state and do not contradict or weaken
one another. Both passes must confirm the same canonical pair ID before a merge
is possible. Pairs that only share a topic, component,
vocabulary, or evidence remain separate. A method shared incidentally remains
separate, but it may be confirmed when the method itself is the evidenced causal
mechanism and both sessions derive the same durable future behavior. Claims may
also express one governing invariant through compatible operational consequences
at different workflow phases. Those pairs remain eligible only when they
identify the same evidenced failure mode or causal mechanism and neither
consequence weakens or contradicts the other. Shared workflow vocabulary and
merely non-contradictory actions are insufficient.
Confirmed pairs only combine
larger groups when every
cross-group cluster pair was also confirmed, preventing a broad middle claim
from transitively joining incompatible rules. The shortlist is bounded, and a
fair bounded budget reserves capacity for proposal-only pairs that fall below
the embedding threshold. When that proposal budget is saturated, sub-threshold
pairs are selected before proposal pairs already covered by semantic proximity.
Pair identities and member order are canonical, so
first-pass response ordering cannot duplicate adjudication work. A failed
reconciliation or verification preserves the conservative singleton partition. An incomplete
embedding response is logged and also falls back to singletons instead of
silently disabling reconciliation. Failure in any LLM pass also fails closed to
singletons. Runs with zero or one learning candidate skip clustering calls.
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
