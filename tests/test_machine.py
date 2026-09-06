import unittest

from runtime.machine import InvalidTransition, StateMachine, Transition
from runtime.models import (
    PlanStepStatus,
    PlanStepTransition,
    ToolExecutionStatus,
    ToolTransition,
    TurnStatus,
    TurnTransition,
)


class MachineTests(unittest.TestCase):
    def test_turn_machine_accepts_only_declared_transitions(self):
        machine = StateMachine([
            Transition(TurnStatus.IDLE, TurnTransition.START, TurnStatus.RUNNING),
            Transition(TurnStatus.RUNNING, TurnTransition.COMPLETE, TurnStatus.COMPLETED),
        ])
        self.assertEqual(machine.dispatch(TurnStatus.IDLE, TurnTransition.START, {}), TurnStatus.RUNNING)
        with self.assertRaises(InvalidTransition):
            machine.dispatch(TurnStatus.COMPLETED, TurnTransition.START, {})

    def test_plan_step_cannot_jump_pending_to_completed(self):
        machine = StateMachine([
            Transition(PlanStepStatus.PENDING, PlanStepTransition.START, PlanStepStatus.IN_PROGRESS),
            Transition(PlanStepStatus.IN_PROGRESS, PlanStepTransition.COMPLETE, PlanStepStatus.COMPLETED),
        ])
        with self.assertRaises(InvalidTransition):
            machine.dispatch(PlanStepStatus.PENDING, PlanStepTransition.COMPLETE, {})

    def test_tool_terminal_state_rejects_restart(self):
        machine = StateMachine([
            Transition(ToolExecutionStatus.PENDING, ToolTransition.START, ToolExecutionStatus.RUNNING),
        ])
        with self.assertRaises(InvalidTransition):
            machine.dispatch(ToolExecutionStatus.COMPLETED, ToolTransition.START, {})

    def test_false_guard_does_not_run_action_or_mutate_context(self):
        context = {"actions": 0}
        machine = StateMachine([
            Transition(
                TurnStatus.RUNNING,
                TurnTransition.COMPLETE,
                TurnStatus.COMPLETED,
                guard=lambda _context, _event: False,
                action=lambda state, event: state.update(actions=1),
            )
        ])
        with self.assertRaises(InvalidTransition):
            machine.dispatch(TurnStatus.RUNNING, TurnTransition.COMPLETE, context)
        self.assertEqual(context, {"actions": 0})


if __name__ == "__main__":
    unittest.main()
