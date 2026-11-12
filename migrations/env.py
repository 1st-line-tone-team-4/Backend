"""Guard against applying the incompatible, experimental A Alembic history.

The reviewed canonical migration chain is the numbered MySQL SQL files. Legacy
revisions are retained under legacy_a_experimental for provenance, not execution.
"""

raise RuntimeError(
    "Alembic execution is disabled: the experimental A revisions are incompatible "
    "with the canonical A+B schema. Follow migrations/README.md and apply the "
    "reviewed numbered MySQL SQL migration chain. Do not stamp or upgrade a "
    "legacy A database as though it were compatible."
)
