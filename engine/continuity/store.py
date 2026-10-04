"""Append-only workspace store with an atomic state projection."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class ContinuityError(RuntimeError):
    pass


class ConflictError(ContinuityError):
    pass


class CorruptLedgerError(ContinuityError):
    pass


class WorkspaceBusyError(ContinuityError):
    pass


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _event_hash(event: dict[str, object]) -> str:
    unsigned = {k: v for k, v in event.items() if k != "hash"}
    return hashlib.sha256(_canonical(unsigned)).hexdigest()


class Workspace:
    """A project-local .continuity directory.

    ledger.jsonl is authoritative. state.json is a repairable projection of the
    most recent ledger event so humans and tools can inspect current state fast.
    """

    CONTROL_DIR = ".continuity"

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.control = self.root / self.CONTROL_DIR
        self.state_path = self.control / "state.json"
        self.ledger_path = self.control / "ledger.jsonl"
        self.lock_path = self.control / "write.lock"

    @property
    def exists(self) -> bool:
        return self.ledger_path.is_file()

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.control.mkdir(parents=True, exist_ok=True)
        if self.lock_path.exists():
            try:
                if time.time() - self.lock_path.stat().st_mtime > 120:
                    self.lock_path.unlink()
            except OSError:
                pass
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise WorkspaceBusyError(
                f"workspace is busy: {self.lock_path}"
            ) from exc
        try:
            os.write(fd, f"pid={os.getpid()}\n".encode("ascii"))
            os.close(fd)
            yield
        finally:
            try:
                self.lock_path.unlink()
            except FileNotFoundError:
                pass

    def _atomic_state_write(self, state: dict[str, object]) -> None:
        tmp = self.control / f".state.{os.getpid()}.tmp"
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.state_path)

    def _read_events(self) -> list[dict[str, object]]:
        if not self.ledger_path.exists():
            return []
        events: list[dict[str, object]] = []
        expected_prev: str | None = None
        expected_seq = 1
        with self.ledger_path.open("r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise CorruptLedgerError(
                        f"ledger line {line_no} is not valid JSON"
                    ) from exc
                if event.get("seq") != expected_seq:
                    raise CorruptLedgerError(
                        f"ledger sequence break at line {line_no}"
                    )
                if event.get("prev_hash") != expected_prev:
                    raise CorruptLedgerError(
                        f"ledger hash-chain break at line {line_no}"
                    )
                actual = event.get("hash")
                if not isinstance(actual, str) or actual != _event_hash(event):
                    raise CorruptLedgerError(
                        f"ledger event hash mismatch at line {line_no}"
                    )
                if not isinstance(event.get("state_after"), dict):
                    raise CorruptLedgerError(
                        f"ledger event {expected_seq} has no state snapshot"
                    )
                events.append(event)
                expected_prev = actual
                expected_seq += 1
        return events

    def _append_event(self, event: dict[str, object]) -> None:
        with self.ledger_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, sort_keys=True))
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())

    def create(
        self,
        state: dict[str, object],
        timestamp: str,
    ) -> dict[str, object]:
        self.root.mkdir(parents=True, exist_ok=True)
        self.control.mkdir(parents=True, exist_ok=True)
        with self._lock():
            if self.ledger_path.exists():
                raise ContinuityError(
                    f"continuity workspace already exists: {self.control}"
                )
            initial = copy.deepcopy(state)
            initial["revision"] = 0
            event: dict[str, object] = {
                "seq": 1,
                "ts": timestamp,
                "type": "workspace_initialized",
                "payload": {
                    "title": initial["project"]["title"],  # type: ignore[index]
                    "domain": initial["project"]["domain"],  # type: ignore[index]
                },
                "prev_hash": None,
                "state_after": initial,
            }
            event["hash"] = _event_hash(event)
            self._append_event(event)
            self._atomic_state_write(initial)
            return copy.deepcopy(initial)

    def load(self) -> dict[str, object]:
        events = self._read_events()
        if not events:
            raise ContinuityError(
                f"no continuity workspace at {self.control}"
            )
        state = copy.deepcopy(events[-1]["state_after"])
        try:
            projected = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            projected = None
        if projected != state:
            self._atomic_state_write(state)
        return state

    def commit(
        self,
        next_state: dict[str, object],
        event_type: str,
        payload: dict[str, object],
        timestamp: str,
        expected_revision: int,
    ) -> dict[str, object]:
        with self._lock():
            events = self._read_events()
            if not events:
                raise ContinuityError("workspace ledger is missing")
            current = events[-1]["state_after"]
            current_revision = current.get("revision")  # type: ignore[union-attr]
            if current_revision != expected_revision:
                raise ConflictError(
                    f"revision changed: expected {expected_revision}, "
                    f"found {current_revision}"
                )

            committed = copy.deepcopy(next_state)
            committed["revision"] = expected_revision + 1
            project = committed.get("project")
            if isinstance(project, dict):
                project["updated_at"] = timestamp

            event: dict[str, object] = {
                "seq": len(events) + 1,
                "ts": timestamp,
                "type": event_type,
                "payload": copy.deepcopy(payload),
                "prev_hash": events[-1]["hash"],
                "state_after": committed,
            }
            event["hash"] = _event_hash(event)

            # Ledger first, projection second. If projection writing is interrupted,
            # load() repairs state.json from the authoritative final ledger event.
            self._append_event(event)
            self._atomic_state_write(committed)
            return copy.deepcopy(committed)

    def recent_events(self, limit: int = 8) -> list[dict[str, object]]:
        events = self._read_events()
        compact = []
        for event in events[-max(0, limit):]:
            compact.append({
                "seq": event["seq"],
                "ts": event["ts"],
                "type": event["type"],
                "payload": event["payload"],
                "hash": event["hash"],
            })
        return compact

    def verify(self) -> dict[str, object]:
        events = self._read_events()
        if not events:
            raise ContinuityError("workspace ledger is empty")
        state = self.load()
        return {
            "ok": True,
            "events": len(events),
            "revision": state["revision"],
            "head_hash": events[-1]["hash"],
        }
