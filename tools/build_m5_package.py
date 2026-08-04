"""Command-line builder for the M5 field package."""

from __future__ import annotations

import argparse
from pathlib import Path

from app.m5_package import build_m5_package


def main() -> int:
    parser = argparse.ArgumentParser(description="构建 M5 现场离线源安装包")
    parser.add_argument("--output", type=Path, default=Path("build") / "m5_field_package.zip")
    parser.add_argument("--root", type=Path)
    args = parser.parse_args()
    print(build_m5_package(args.output, args.root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
