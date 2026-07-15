from dataclasses import dataclass
from pathlib import Path


class SourceAssetError(Exception):
    exit_code = 1


class ContractError(SourceAssetError):
    exit_code = 1


class IntegrityError(SourceAssetError):
    exit_code = 2


class RemoteError(SourceAssetError):
    exit_code = 2


@dataclass(frozen=True)
class Policy:
    hosts: frozenset[str]
    redactions: dict[str, dict]


def load_manifest(path: Path, allow_pending: bool) -> dict:
    return {}


def validate_manifest(
    document: dict,
    mic_dir: Path,
    policy: Policy,
    allow_pending: bool,
) -> None:
    return None
