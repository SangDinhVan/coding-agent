from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Generic, Iterable, TypeVar

S = TypeVar("S")
E = TypeVar("E")
C = TypeVar("C")


class InvalidTransition(ValueError):
    """Raised before mutation when no transition or guard permits an event."""


@dataclass(frozen=True)
class Transition(Generic[S, E, C]):
    source: S
    event_type: E
    target: S
    guard: Callable[[C, E], bool] | None = None
    action: Callable[[C, E], None] | None = None


class StateMachine(Generic[S, E, C]):
    def __init__(self, transitions: Iterable[Transition[S, E, C]]):
        self._transitions = tuple(transitions)

    def validate(self, state: S, event: E, context: C) -> Transition[S, E, C]:
        for transition in self._transitions:
            if transition.source == state and transition.event_type == event:
                if transition.guard is not None and not transition.guard(context, event):
                    raise InvalidTransition(f"Guard rejected {state!r} + {event!r}")
                return transition
        raise InvalidTransition(f"No transition for {state!r} + {event!r}")

    def dispatch(self, state: S, event: E, context: C) -> S:
        transition = self.validate(state, event, context)
        if transition.action is not None:
            transition.action(context, event)
        return transition.target
