from __future__ import annotations

import sys

from bunnybyte.branding import DISPLAY_HANDLE
from bunnybyte.cli import build_agent, build_arg_parser, key_check_passes, prepare_tui_terminal_environment, tui_theme_choice


def main(argv=None):
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.prompt:
        print(
            f"{DISPLAY_HANDLE}-tui does not accept one-shot prompts; start the TUI and type there.",
            file=sys.stderr,
        )
        return 2
    agent = build_agent(args)
    if not key_check_passes(agent):
        return 2
    prepare_tui_terminal_environment()
    from bunnybyte.tui.app import BunnyByteTuiApp

    BunnyByteTuiApp(agent, theme=tui_theme_choice(args)).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
