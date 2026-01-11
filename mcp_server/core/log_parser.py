import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


class StructuredLogParser:
    def __init__(self, log_dir: Path | None = None):
        if log_dir is None:
            project_root = Path(__file__).parent.parent.parent
            log_dir = project_root / "logs"
        self.log_dir = log_dir
        self.operations_log = log_dir / "trading_operations.log"
        self.maintenance_log = log_dir / "system_maintenance.log"

    def parse_log_file(
        self,
        log_path: Path | str | None = None,
        hours: int = 24,
        include_maintenance: bool = True,
    ) -> list[dict[str, Any]]:
        cutoff_time = datetime.now(UTC) - timedelta(hours=hours)
        entries: list[dict[str, Any]] = []

        if log_path:
            entries.extend(self._parse_single_file(Path(log_path), cutoff_time))
        else:
            if self.operations_log.exists():
                entries.extend(
                    self._parse_single_file(self.operations_log, cutoff_time)
                )
            if include_maintenance and self.maintenance_log.exists():
                entries.extend(
                    self._parse_single_file(self.maintenance_log, cutoff_time)
                )

        return sorted(entries, key=lambda x: x.get("timestamp", ""))

    def _parse_single_file(
        self, file_path: Path, cutoff_time: datetime
    ) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []

        try:
            with open(file_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        entry = json.loads(line)
                        timestamp_str = entry.get("timestamp", "")

                        if not timestamp_str:
                            continue

                        entry_time = self._parse_timestamp(timestamp_str)
                        if entry_time and entry_time >= cutoff_time:
                            entries.append(entry)

                    except json.JSONDecodeError:
                        continue

        except FileNotFoundError:
            pass

        return entries

    def _parse_timestamp(self, timestamp_str: str) -> datetime | None:
        try:
            if timestamp_str.endswith("Z"):
                timestamp_str = timestamp_str[:-1] + "+00:00"

            dt = datetime.fromisoformat(timestamp_str)

            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)

            return dt
        except (ValueError, AttributeError):
            return None

    def filter_by_level(
        self, entries: list[dict[str, Any]], level: str
    ) -> list[dict[str, Any]]:
        level_lower = level.lower()
        return [
            entry for entry in entries if entry.get("level", "").lower() == level_lower
        ]

    def filter_by_text(
        self, entries: list[dict[str, Any]], text: str, case_sensitive: bool = False
    ) -> list[dict[str, Any]]:
        if case_sensitive:
            return [
                entry
                for entry in entries
                if text in entry.get("event", "")
                or text in entry.get("module", "")
                or text in str(entry.get("error", ""))
            ]
        else:
            text_lower = text.lower()
            return [
                entry
                for entry in entries
                if text_lower in entry.get("event", "").lower()
                or text_lower in entry.get("module", "").lower()
                or text_lower in str(entry.get("error", "")).lower()
            ]

    def filter_by_module(
        self, entries: list[dict[str, Any]], module: str
    ) -> list[dict[str, Any]]:
        return [entry for entry in entries if entry.get("module") == module]

    def filter_by_route(
        self, entries: list[dict[str, Any]], route: str
    ) -> list[dict[str, Any]]:
        return [entry for entry in entries if entry.get("_route") == route]

    def get_timeline(
        self, entries: list[dict[str, Any]], bucket_minutes: int = 60
    ) -> list[dict[str, Any]]:
        timeline: dict[str, dict[str, Any]] = {}

        for entry in entries:
            timestamp_str = entry.get("timestamp", "")
            if not timestamp_str:
                continue

            entry_time = self._parse_timestamp(timestamp_str)
            if not entry_time:
                continue

            bucket_time = entry_time.replace(
                minute=(entry_time.minute // bucket_minutes) * bucket_minutes,
                second=0,
                microsecond=0,
            )
            bucket_key = bucket_time.isoformat()

            if bucket_key not in timeline:
                timeline[bucket_key] = {
                    "timestamp": bucket_key,
                    "total": 0,
                    "by_level": {},
                }

            timeline[bucket_key]["total"] += 1

            level = entry.get("level", "unknown")
            if level not in timeline[bucket_key]["by_level"]:
                timeline[bucket_key]["by_level"][level] = 0
            timeline[bucket_key]["by_level"][level] += 1

        return sorted(timeline.values(), key=lambda x: x["timestamp"])

    def get_errors_only(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return self.filter_by_level(entries, "error")

    def get_warnings_only(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return self.filter_by_level(entries, "warning")

    def get_component_errors(
        self, entries: list[dict[str, Any]]
    ) -> dict[str, list[dict[str, Any]]]:
        errors = self.get_errors_only(entries)
        by_module: dict[str, list[dict[str, Any]]] = {}

        for error in errors:
            module = error.get("module", "unknown")
            if module not in by_module:
                by_module[module] = []
            by_module[module].append(error)

        return by_module

    def group_by_event_similarity(
        self, entries: list[dict[str, Any]], min_occurrences: int = 3
    ) -> list[dict[str, Any]]:
        event_groups: dict[str, dict[str, Any]] = {}

        for entry in entries:
            event_text = entry.get("event", "")
            if not event_text:
                continue

            normalized_event = self._normalize_event_for_grouping(event_text)

            if normalized_event not in event_groups:
                event_groups[normalized_event] = {
                    "pattern": normalized_event,
                    "sample_event": event_text,
                    "occurrences": 0,
                    "modules": set(),
                    "first_seen": entry.get("timestamp", ""),
                    "last_seen": entry.get("timestamp", ""),
                }

            group = event_groups[normalized_event]
            group["occurrences"] += 1
            if entry.get("module"):
                group["modules"].add(entry.get("module"))
            group["last_seen"] = entry.get("timestamp", group["last_seen"])

        filtered_groups = [
            {
                "pattern": g["pattern"],
                "sample_event": g["sample_event"],
                "occurrences": g["occurrences"],
                "modules": list(g["modules"]),
                "first_seen": g["first_seen"],
                "last_seen": g["last_seen"],
            }
            for g in event_groups.values()
            if g["occurrences"] >= min_occurrences
        ]

        return sorted(filtered_groups, key=lambda x: x["occurrences"], reverse=True)

    def _normalize_event_for_grouping(self, event_text: str) -> str:
        normalized = event_text

        replacements = [
            (r"ID: [a-f0-9]+", "ID: <hash>"),
            (r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", "<timestamp>"),
            (r"\d+\.\d+", "<number>"),
            (r"\d+", "<number>"),
            (r"'[^']*'", "<string>"),
            (r'"[^"]*"', "<string>"),
        ]

        import re

        for pattern, replacement in replacements:
            normalized = re.sub(pattern, replacement, normalized)

        return normalized
