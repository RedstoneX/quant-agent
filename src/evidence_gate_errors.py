"""Typed failures at the evidence-gate decision boundary."""


class EvidenceGateEvaluationError(RuntimeError):
    """The gate implementation crashed before producing a verdict.

    This is distinct from an unclassified seat status. Unknown status words
    still produce a verdict and pass; this exception means the classifier
    itself could not answer, so a decision must not proceed.
    """
