"""Runtime mode tool definitions."""

PLAN_TOOL_SPECS = {
    "enter_plan_mode": {
        "schema": {"topic": "str", "path": "str?"},
        "risky": False,
        "description": "为指定主题进入计划模式。",
    },
    "exit_plan_mode": {
        "schema": {},
        "risky": False,
        "description": "退出计划模式并回到默认运行模式。",
    },
}

PLAN_TOOL_EXAMPLES = {
    "enter_plan_mode": '<tool>{"name":"enter_plan_mode","args":{"topic":"Refactor auth"}}</tool>',
    "exit_plan_mode": '<tool>{"name":"exit_plan_mode","args":{}}</tool>',
}


def validate_plan_tool(name, args):
    if name == "enter_plan_mode" and not str(args.get("topic", "")).strip():
        raise ValueError("topic must not be empty")


def tool_enter_plan_mode(agent, args):
    path = agent.enter_plan_mode(args["topic"], path=args.get("path"))
    return f"mode: plan\nplan path: {path}"


def tool_exit_plan_mode(agent, args):
    agent.exit_plan_mode()
    return "mode: default"
