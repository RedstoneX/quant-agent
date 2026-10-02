"""src.cost_circuit.breaker_wording -- thin shims; bodies moved verbatim to src/cost_circuit/parts/episode_wording.py."""
from __future__ import annotations
from src.cost_circuit.parts.episode_wording import EpisodeWording


class _BreakerWordingMixin:
    def _episode_wording(self) -> EpisodeWording:
        """Thin shim: builds the standalone object from this breaker's collaborators
        (bodies moved to src/cost_circuit/parts/episode_wording.py). Built per call so a collaborator swapped after
        construction is what the body sees."""
        return EpisodeWording(config=self.config)

    def _self_clear_window_minutes(self, *args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/episode_wording.py."""
        return self._episode_wording()._self_clear_window_minutes(*args, **kwargs)

    def _suspension_still_inside_self_clear_window_locked(self, *args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/episode_wording.py."""
        return self._episode_wording()._suspension_still_inside_self_clear_window_locked(*args, **kwargs)

    def _episode_already_paged_locked(self, *args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/episode_wording.py."""
        return self._episode_wording()._episode_already_paged_locked(*args, **kwargs)

    def _record_suspension_deferral_locked(self, *args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/episode_wording.py."""
        return self._episode_wording()._record_suspension_deferral_locked(*args, **kwargs)

    def _episode_facts_locked(self, *args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/episode_wording.py."""
        return self._episode_wording()._episode_facts_locked(*args, **kwargs)

    @staticmethod
    def _format_episode_line(*args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/episode_wording.py."""
        return EpisodeWording._format_episode_line(*args, **kwargs)

    @staticmethod
    def _format_episode_summary(*args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/episode_wording.py."""
        return EpisodeWording._format_episode_summary(*args, **kwargs)
