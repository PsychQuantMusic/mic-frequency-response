#!/usr/bin/env python3

import argparse
from collections.abc import Sequence
from datetime import datetime, timezone
import os
from pathlib import Path
import stat
import sys

from source_assets_lib.commands import (
    CommandContext,
    OperationSummary,
    Selector,
    bootstrap,
    fetch,
    verify,
)
from source_assets_lib.model import ContractError
from source_assets_lib.network import PinnedHTTPSClient
from source_assets_lib.storage import AtomicArtifactStore


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    selector = Selector.all() if args.all else Selector.mic(args.mic)
    try:
        has_manifest = _has_manifest(REPOSITORY_ROOT, selector)
    except OSError:
        summary = OperationSummary(0, 0, 1, 1)
        _print_summary(summary)
        return summary.exit_code
    if not has_manifest:
        print(
            "找不到 source-manifest.json；請先執行來源 manifest 遷移",
            file=sys.stderr,
        )
        return 1
    if args.command == "verify":
        summary = verify(REPOSITORY_ROOT, selector)
    else:
        context = CommandContext(
            transport=PinnedHTTPSClient(),
            store=AtomicArtifactStore(REPOSITORY_ROOT),
            now=lambda: datetime.now(timezone.utc),
        )
        if args.command == "bootstrap":
            summary = bootstrap(
                REPOSITORY_ROOT,
                selector,
                args.accept_new,
                context,
            )
        else:
            summary = fetch(REPOSITORY_ROOT, selector, context)
    _print_summary(summary)
    return summary.exit_code


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="source_assets.py")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("bootstrap", "fetch", "verify"):
        command = commands.add_parser(name)
        selector = command.add_mutually_exclusive_group(required=True)
        selector.add_argument("--all", action="store_true")
        selector.add_argument("--mic", metavar="SLUG", type=_parse_mic_slug)
        if name == "bootstrap":
            command.add_argument("--accept-new", action="store_true", required=True)
    return parser


def _print_summary(summary) -> None:
    print(
        f"available={summary.available} "
        f"unavailable={summary.unavailable} "
        f"failed={summary.failed}"
    )


def _has_manifest(root: Path, selector: Selector) -> bool:
    data = root / "data"
    if selector.mic_slug is not None:
        candidate = data / selector.mic_slug / "source-manifest.json"
        try:
            metadata = os.stat(candidate, follow_symlinks=False)
        except (FileNotFoundError, NotADirectoryError):
            return False
        return stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
    first_error = None
    try:
        entries = os.scandir(data)
    except FileNotFoundError:
        return False
    with entries:
        for entry in entries:
            try:
                if not (
                    entry.is_dir(follow_symlinks=False) or entry.is_symlink()
                ):
                    continue
                metadata = os.stat(
                    data / entry.name / "source-manifest.json",
                    follow_symlinks=False,
                )
            except (FileNotFoundError, NotADirectoryError):
                continue
            except OSError as error:
                if first_error is None:
                    first_error = error
                continue
            if stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                return True
    if first_error is not None:
        raise first_error
    return False


def _parse_mic_slug(value: str) -> str:
    try:
        Selector.mic(value)
    except ContractError as error:
        raise argparse.ArgumentTypeError("SLUG 格式無效") from error
    return value


if __name__ == "__main__":
    raise SystemExit(main())
