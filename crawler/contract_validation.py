from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath


PROVIDER_CONTRACT_FILENAME = "bridge-contract.json"


def provider_contract(
    root: Path,
    *,
    expected_platform: str | None = None,
    expected_mode: str | None = None,
) -> dict:
    path = root / PROVIDER_CONTRACT_FILENAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Pinned provider did not produce its bridge contract") from exc
    if payload.get("schema_version") != 1 or not isinstance(payload.get("items"), dict):
        raise ValueError("Pinned provider bridge contract has an unknown structure")
    if expected_platform is not None and payload.get("platform") != expected_platform:
        raise ValueError("Pinned provider bridge contract platform does not match the job")
    if expected_mode is not None and payload.get("mode") != expected_mode:
        raise ValueError("Pinned provider bridge contract mode does not match the job")
    return payload


def apply_provider_contract(item: dict, contract: dict) -> tuple[dict, dict]:
    provider_id = str(item.get("remote_id") or "")
    metadata = contract["items"].get(provider_id)
    if not isinstance(metadata, dict):
        raise ValueError(
            f"Provider contract is missing content {provider_id or '<unknown>'}"
        )
    canonical_id = str(metadata.get("canonical_id") or "").strip()
    source_url = str(metadata.get("source_url") or "").strip()
    if not canonical_id or not source_url:
        raise ValueError("Provider contract is missing canonical content identity")
    slots = metadata.get("media_slots")
    if not isinstance(slots, list):
        raise ValueError("Provider contract contains invalid media slots")
    slot_ids: set[str] = set()
    staged_paths: set[str] = set()
    for ordinal, slot in enumerate(slots, start=1):
        if not isinstance(slot, dict):
            raise ValueError("Provider contract contains invalid media slots")
        slot_id = str(slot.get("slot_id") or "")
        source_sha256 = slot.get("source_sha256")
        staged_path = slot.get("staged_path")
        if (
            slot.get("kind") not in {"image", "video", "audio"}
            or slot.get("ordinal") != ordinal
            or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", slot_id)
            or slot_id in slot_ids
            or (
                source_sha256 is not None
                and not re.fullmatch(r"[0-9a-f]{64}", str(source_sha256))
            )
        ):
            raise ValueError("Provider contract contains invalid media slots")
        slot_ids.add(slot_id)
        if staged_path is not None:
            path_value = str(staged_path)
            relative = PurePosixPath(path_value)
            if (
                not path_value
                or "\\" in path_value
                or relative.is_absolute()
                or ".." in relative.parts
                or relative.as_posix() != path_value
                or path_value.casefold() in staged_paths
            ):
                raise ValueError("Provider contract contains invalid staged media paths")
            staged_paths.add(path_value.casefold())
    expected = int(metadata.get("expected_media_count", -1))
    if expected < 0 or expected != len(slots):
        raise ValueError("Provider contract media count does not match its slots")
    aliases = metadata.get("aliases", [])
    if not isinstance(aliases, list) or any(
        not isinstance(alias, str) or not alias.strip() for alias in aliases
    ):
        raise ValueError("Provider contract contains invalid content aliases")
    item.update(
        {
            "remote_id": canonical_id,
            "source_url": source_url,
            "original": metadata.get("original") is True,
            "pinned": metadata.get("pinned") is True,
            "content_type": str(
                metadata.get("content_type") or item.get("content_type") or "unknown"
            ),
            "aliases": list(dict.fromkeys(alias.strip() for alias in aliases)),
        }
    )
    return item, metadata


def contract_identity_matches(requested_id: str, item: dict) -> bool:
    return requested_id == item.get("remote_id") or requested_id in item.get(
        "aliases", []
    )


def bind_staged_media_to_slots(
    media: list[dict], item_contract: dict
) -> tuple[list[dict], bool]:
    """Return media in contract-slot order only when every file binds exactly once."""
    slots = item_contract.get("media_slots") or []
    media_by_path = {
        str(record.get("local_path") or "").casefold(): record for record in media
    }
    if len(media_by_path) != len(media):
        return [], False
    bound: list[dict] = []
    bound_paths: set[str] = set()
    for slot in slots:
        staged_path = str(slot.get("staged_path") or "")
        path_key = staged_path.casefold()
        record = media_by_path.get(path_key)
        if (
            not staged_path
            or path_key in bound_paths
            or record is None
            or record.get("kind") != slot.get("kind")
        ):
            return [], False
        bound_paths.add(path_key)
        bound.append({**record, "slot_id": slot["slot_id"]})
    if bound_paths != set(media_by_path):
        return [], False
    return bound, True
