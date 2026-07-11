"""Single source of truth for the canonical personal-knowledge vocabulary.

Before this module existed, the canonical personal-knowledge type list
(project, resource, concept, journal, correspondence, prompt, decision,
meeting, event, person) and its alias maps were duplicated across:

- the classifier prompt (``_CLASSIFICATION_PROMPT_TEMPLATE`` in
  ``capture_router.py``)
- the alias maps (``_MEMORY_TYPE_ALIASES`` / ``_CAPTURE_TEMPLATE_TYPE_ALIASES``
  in ``capture_router.py``)
- the domain-metadata validation branches + TypedDicts (``interface.py``)
- the ``save_memory`` tool docstring (``server.py``)

Adding or renaming a canonical type could silently drift because each of
those locations had to be edited independently. This module centralizes
the type list and alias maps; the other three locations now import from
here instead of redefining the literals.
"""

from __future__ import annotations

# The ten canonical personal-knowledge types. Order matches the
# save_memory tool docstring and classifier prompt for readability, not
# runtime significance.
CANONICAL_PERSONAL_KNOWLEDGE_TYPES: tuple[str, ...] = (
    "project",
    "resource",
    "concept",
    "journal",
    "correspondence",
    "prompt",
    "decision",
    "meeting",
    "event",
    "person",
)

# Explicit caller-supplied type aliases -> canonical type. Consumed by
# capture_router.normalize_memory_type() for pre-classification
# normalization of caller-provided `type` values.
MEMORY_TYPE_ALIASES: dict[str, str] = {
    "note": "journal",
    "diary": "journal",
    "reference": "resource",
    "idea": "concept",
    "email": "correspondence",
    "letter": "correspondence",
    "prompt_template": "prompt",
}

# Classifier capture_template -> canonical memory type. Consumed by
# capture_router.canonical_type_for_capture_template() to resolve the
# canonical type that should drive domain-metadata validation for
# classifier-generated captures.
#
# "person_context" is the sole exception: the classifier still emits it
# (and existing callers/tests depend on the stored `capture_template`
# value), but the canonical personal-knowledge vocabulary calls this
# "person". This alias governs only which canonical type drives
# domain-metadata validation — the stored `capture_template` is left
# intact by callers.
CAPTURE_TEMPLATE_TYPE_ALIASES: dict[str, str] = {
    "person_context": "person",
}
