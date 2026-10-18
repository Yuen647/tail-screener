#!/usr/bin/env python3
"""Prepare required rule resources before invoking the screening CLI."""
import json
import os
from pathlib import Path
import sys
from restore_support import ensure_ready

if __name__ == '__main__':
    from run_control import supervise
    try:
        controlled = supervise(Path(__file__).resolve().parents[1], sys.argv[1:])
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
    if controlled is not None:
        raise SystemExit(controlled)
    try:
        source = Path(__file__).resolve().parents[1]
        root, result = ensure_ready(source,restore_all=sys.argv[1:]==['--restore-only'])
        if sys.argv[1:] == ['--restore-only']:
            print(json.dumps(result, ensure_ascii=False))
            raise SystemExit(0)
        if root != source:
            os.execv(sys.executable, [sys.executable, str(root / 'scripts/screen.py'), *sys.argv[1:]])
    except Exception as exc:
        print(f'运行资源准备失败（{type(exc).__name__}）：{exc}。未启动筛选，未覆盖原有资料。', file=sys.stderr)
        raise SystemExit(4)
    from tail_screener.cli import main
    raise SystemExit(main())
