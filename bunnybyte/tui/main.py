from __future__ import annotations

import sys

from bunnybyte.branding import DISPLAY_HANDLE
from bunnybyte.cli import build_agent, build_arg_parser
from bunnybyte.tui.app import BunnyByteTuiApp


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
    BunnyByteTuiApp(agent).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
