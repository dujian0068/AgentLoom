"""Keep immutable source records separate from the replaceable model message view.

This manager owns only context state. The host remains responsible for atomically
saving this state with execution progress and for projecting records to a session
journal. Sequence numbers are local to one run instance, never a session offset.
"""

from copy import deepcopy

from .context_validation import validate_compaction
from .module_contracts import ContextInput, ManagedContext

COUNTERS = (
    "user_turns",
    "model_steps",
    "baseline_user_turns",
    "baseline_model_steps",
)


class JournalContextManager:
    module_id = "journal-context/v1"

    def create(self, task, prompt, history=(), history_metadata=None):
        metadata = deepcopy(history_metadata or {})
        counters = {key: metadata.get(key, 0) for key in COUNTERS}
        self._validate_counters(counters)
        counters["user_turns"] += 1
        state = {
            "format": 1,
            "records": [],
            "compactions": [],
            "raw_complete": True,
            "history_metadata": metadata,
            "pending_inputs": [],
            **counters,
        }
        context = ManagedContext(
            [{"role": "system", "content": prompt}, *deepcopy(list(history))], state
        )
        self.append(context, {"role": "user", "content": task}, "user_input")
        return context

    def restore(self, messages, state, task):
        if state is None:
            # Older checkpoints did not retain raw output or inherited boundaries.
            # Preserve only the available local suffix when its start is known;
            # never claim that a compressed view contains all original records.
            original_task = task.split("\n用户补充：", 1)[0]
            start = next(
                (
                    index
                    for index, message in reversed(list(enumerate(messages)))
                    if message.get("role") == "user" and message.get("content") == original_task
                ),
                None,
            )
            available = (
                messages[start:]
                if start is not None
                else [{"role": "user", "content": original_task}]
            )
            state = {
                "format": 1,
                "records": [],
                "compactions": [],
                "raw_complete": False,
                "history_metadata": {},
                "pending_inputs": [],
                "user_turns": 1,
                "model_steps": 0,
                "baseline_user_turns": 0,
                "baseline_model_steps": 0,
            }
            for message in available:
                role = message.get("role")
                kind = "model" if role == "assistant" else "tool" if role == "tool" else "feedback"
                if not state["records"]:
                    kind = "user_input"
                state["records"].append(
                    {"seq": len(state["records"]) + 1, "kind": kind, "message": deepcopy(message)}
                )
            state["model_steps"] = sum(record["kind"] == "model" for record in state["records"])
        if state.get("format") != 1:
            raise ValueError("不支持此上下文检查点版本")
        self._validate_counters(state)
        return ManagedContext(messages, state)

    @staticmethod
    def _validate_counters(counters):
        for key in COUNTERS:
            value = counters.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("上下文计数必须为非负整数")
        if (
            counters["baseline_user_turns"] > counters["user_turns"]
            or counters["baseline_model_steps"] > counters["model_steps"]
        ):
            raise ValueError("上下文压缩基线不能超过当前计数")

    def append(self, context, message, kind, *, visible_message=None, model_step=False):
        records = context.state["records"]
        seq = records[-1]["seq"] + 1 if records else 1
        records.append({"seq": seq, "kind": kind, "message": deepcopy(message)})
        context.messages.append(deepcopy(message if visible_message is None else visible_message))
        if model_step:
            context.state["model_steps"] += 1
        return seq

    def queue_input(self, context, instruction):
        records = context.state["records"]
        seq = records[-1]["seq"] + 1 if records else 1
        records.append(
            {
                "seq": seq,
                "kind": "user_supplement",
                "message": {"role": "user", "content": instruction},
            }
        )
        context.state["pending_inputs"].append(
            {"seq": seq, "message": {"role": "user", "content": "用户补充：" + instruction}}
        )
        return seq

    def drain_inputs(self, context):
        pending = context.state["pending_inputs"]
        if not pending:
            return False
        context.messages.extend(deepcopy(item["message"]) for item in pending)
        pending.clear()
        return True

    async def prepare(self, context, task, plan, instance, tools, policy, model):
        state = context.state
        counters = {key: state[key] for key in COUNTERS}
        request = ContextInput(
            task, deepcopy(plan), deepcopy(context.messages), instance, deepcopy(tools), counters
        )
        result = await policy.compact(request, model)
        if result is None:
            return None
        validate_compaction(context.messages, result.messages)
        snapshot = {
            "revision": len(state["compactions"]) + 1,
            "covered_seq": state["records"][-1]["seq"] if state["records"] else 0,
            "messages": deepcopy(result.messages),
            "history_watermark": deepcopy(state["history_metadata"].get("watermark")),
            "counters": deepcopy(counters),
            "metrics": deepcopy(result.metrics),
        }
        # A failed policy/validation never modifies raw records or resets baselines.
        context.messages[:] = deepcopy(result.messages)
        state["compactions"].append(snapshot)
        state["baseline_user_turns"] = state["user_turns"]
        state["baseline_model_steps"] = state["model_steps"]
        return {
            **result.metrics,
            "revision": snapshot["revision"],
            "covered_seq": snapshot["covered_seq"],
        }


def queue_instruction(checkpoint, instruction):
    """Durably accept user input even before model credentials can be resolved.

    Application services may save the modified checkpoint and journal projection
    in their own transaction. Prepared messages change only after pending calls
    have their results, preserving the model tool-call protocol on resume.
    """
    if not instruction:
        return checkpoint
    frame = checkpoint.get("frames", {}).get("main")
    if frame is None:
        checkpoint.setdefault("pending_inputs", []).append({"content": instruction})
        return checkpoint
    manager = JournalContextManager()
    context = manager.restore(frame["messages"], frame.get("context"), frame["task"])
    frame["context"] = context.state
    manager.queue_input(context, instruction)
    frame["task"] += "\n用户补充：" + instruction
    frame["candidate"] = None
    frame.pop("outcome", None)
    frame.pop("output", None)
    return checkpoint
