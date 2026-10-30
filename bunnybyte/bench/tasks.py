"""bench 任务的数据结构与任务集入口。

每个任务都是一个自包含的小仓库：fixture 文件、给 agent 的中文提示词、
判分用的 shell verifier，以及一条 oracle 参考解（脚本化的工具调用序列）。
oracle 只用于离线测试：证明 verifier 在原始 fixture 上失败、在参考解之后通过，
这样 verifier 本身不会放水，也不会误杀正确答案。
"""

import hashlib
import json
from dataclasses import dataclass, field

PY = "{py}"  # 运行时替换为当前解释器的绝对路径，避免依赖 PATH 里的 python3。


@dataclass(frozen=True)
class BenchTask:
    id: str
    category: str
    title: str
    prompt: str
    files: dict
    verifier: str = ""
    hidden_files: dict = field(default_factory=dict)
    frozen: tuple = ()
    answer_regex: tuple = ()
    read_only: bool = False
    step_budget: int = 14  # 高效完成应有的工具步数；超出只记 over_budget，不直接判负
    solution: tuple = ()

    def digest_payload(self):
        return {
            "id": self.id,
            "prompt": self.prompt,
            "files": self.files,
            "verifier": self.verifier,
            "hidden_files": self.hidden_files,
            "frozen": list(self.frozen),
            "answer_regex": list(self.answer_regex),
            "read_only": self.read_only,
            "step_budget": self.step_budget,
        }


def tool(name, **args):
    return {"name": name, "args": args}


def final(text="完成。"):
    return {"final": text}


def all_tasks():
    from .catalog_build import BUILD_TASKS
    from .catalog_fix import FIX_TASKS
    from .catalog_ripple import RIPPLE_TASKS

    return [*FIX_TASKS, *BUILD_TASKS, *RIPPLE_TASKS]


def spec_tasks():
    """单独的“需求遗漏”套件：不进默认任务集，默认基线的任务集指纹因此不变。"""
    from .catalog_spec import SPEC_TASKS

    return list(SPEC_TASKS)


SUITES = {"core": all_tasks, "spec": spec_tasks}


def select_tasks(ids=None, suite="core"):
    if suite not in SUITES:
        raise ValueError(f"未知的 bench 套件：{suite}；可用：{', '.join(SUITES)}")
    tasks = SUITES[suite]()
    if not ids:
        return tasks
    by_id = {task.id: task for task in [*all_tasks(), *spec_tasks()]}
    unknown = [item for item in ids if item not in by_id]
    if unknown:
        raise ValueError(f"未知的 bench 任务：{', '.join(unknown)}；可用：{', '.join(by_id)}")
    return [by_id[item] for item in ids]


def task_set_hash(tasks):
    """任务集指纹：任务内容变了就换指纹，历史分数只和同指纹的结果比较。"""
    payload = json.dumps(
        [task.digest_payload() for task in tasks], sort_keys=True, ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def fill_python(value, python):
    if isinstance(value, str):
        return value.replace(PY, python)
    if isinstance(value, dict):
        return {key: fill_python(item, python) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [fill_python(item, python) for item in value]
    return value


def scripted_outputs(task, python):
    """把 oracle 参考解翻译成 ScriptedModelClient 能回放的文本输出。"""
    outputs = []
    for step in task.solution:
        if "final" in step:
            outputs.append(f"<final>{step['final']}</final>")
            continue
        args = fill_python(step["args"], python)
        outputs.append("<tool>" + json.dumps({"name": step["name"], "args": args}) + "</tool>")
    return outputs
