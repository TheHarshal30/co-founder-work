"""Asset preparation: generate and validate the test assets a rules file needs
but does not have, so a problem can be graded from submission.c + rules.json.
See prepare.py for the pipeline and oracle.py for what counts as a trusted
expected output."""

from .prepare import GENERATOR_VERSION, Preparation, find_missing, prepare_assets
from .store import AssetStore

__all__ = ["GENERATOR_VERSION", "AssetStore", "Preparation", "find_missing", "prepare_assets"]
