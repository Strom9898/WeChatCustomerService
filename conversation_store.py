"""Local, per-group conversation storage for the WeCom assistant."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime
from pathlib import Path


class ConversationStore:
    """Stores conversations as local JSONL files, one file for each group."""

    def __init__(self, root: str | Path = "logs/conversations"):
        self.root = Path(root)
        self._lock = threading.Lock()

    @staticmethod
    def _clean_group_name(group_name: str) -> str:
        return (group_name or "当前会话").strip()[:100] or "当前会话"

    def _path_for(self, group_name: str) -> Path:
        key = hashlib.sha256(self._clean_group_name(group_name).encode("utf-8")).hexdigest()[:24]
        return self.root / f"{key}.jsonl"

    @staticmethod
    def clean_customer_content(content: str) -> str:
        """Remove the green sender/external-contact label from OCR output."""
        lines = [line.strip() for line in str(content).splitlines() if line.strip()]
        while lines and re.search(r"@\s*(微信|徽信)\b", lines[0], re.IGNORECASE):
            lines.pop(0)
        return "\n".join(lines).strip()

    def append(self, group_name: str, role: str, content: str) -> dict:
        entry = {
            "group_name": self._clean_group_name(group_name),
            "role": "assistant" if role == "assistant" else "user",
            "content": (self.clean_customer_content(content) if role != "assistant" else str(content).strip())[:4000],
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        if not entry["content"]:
            return entry
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            with self._path_for(entry["group_name"]).open("a", encoding="utf-8") as file:
                file.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def append_user_if_new(self, group_name: str, content: str) -> tuple[dict, bool]:
        """Append a customer turn unless the same unanswered turn is already last."""
        cleaned_group = self._clean_group_name(group_name)
        cleaned_content = self.clean_customer_content(content)[:4000]
        latest = self.messages(cleaned_group, 1)
        if (latest and latest[-1].get("role") == "user"
                and latest[-1].get("content") == cleaned_content):
            return latest[-1], False
        return self.append(cleaned_group, "user", cleaned_content), True

    def append_user_batch_if_new(self, group_name: str, contents: list[str]) -> list[dict]:
        """Append the unseen suffix of a consecutive customer-message batch."""
        cleaned = [self.clean_customer_content(item)[:4000] for item in contents]
        cleaned = [item for item in cleaned if item]
        if not cleaned:
            return []
        history = self.messages(group_name, max(20, len(cleaned) * 2))
        trailing = []
        for item in reversed(history):
            if item.get("role") != "user":
                break
            trailing.append(item.get("content", ""))
        trailing.reverse()
        start = len(trailing) if trailing and cleaned[:len(trailing)] == trailing else 0
        added = []
        for content in cleaned[start:]:
            entry, was_added = self.append_user_if_new(group_name, content)
            if was_added:
                added.append(entry)
        return added

    def messages(self, group_name: str, limit: int = 80) -> list[dict]:
        path = self._path_for(group_name)
        if not path.exists():
            return []
        items = []
        with self._lock:
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if item.get("content"):
                        if item.get("role") == "user":
                            item["content"] = self.clean_customer_content(item["content"])
                        if not item["content"]:
                            continue
                        items.append(item)
            except OSError:
                return []
        return items[-max(1, min(int(limit), 500)):]

    def groups(self) -> list[dict]:
        if not self.root.exists():
            return []
        result = []
        with self._lock:
            for path in self.root.glob("*.jsonl"):
                try:
                    rows = self.messages_from_path(path)
                except (OSError, json.JSONDecodeError):
                    continue
                if rows:
                    last = rows[-1]
                    result.append({"group_name": last.get("group_name", "当前会话"), "message_count": len(rows), "last_message_at": last.get("created_at", ""), "last_preview": str(last.get("content", ""))[:100]})
        return sorted(result, key=lambda item: item["last_message_at"], reverse=True)

    def repair_saved_messages(self) -> int:
        """Normalize earlier OCR records created before sender labels were removed."""
        if not self.root.exists():
            return 0
        changed = 0
        with self._lock:
            for path in self.root.glob("*.jsonl"):
                try:
                    original = path.read_text(encoding="utf-8").splitlines()
                except OSError:
                    continue
                rows = []
                for line in original:
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        changed += 1
                        continue
                    if item.get("role") == "user":
                        cleaned = self.clean_customer_content(item.get("content", ""))
                        if cleaned != item.get("content", ""):
                            changed += 1
                        item["content"] = cleaned
                    if item.get("content"):
                        if (item.get("role") == "user" and rows
                                and rows[-1].get("role") == "user"
                                and rows[-1].get("content") == item.get("content")):
                            changed += 1
                            continue
                        # This bot emits one assistant reply per customer message. A consecutive
                        # assistant item means the preceding OCR capture was only a sender label.
                        if item.get("role") == "assistant" and rows and rows[-1].get("role") == "assistant":
                            changed += 1
                            continue
                        rows.append(item)
                    else:
                        changed += 1
                rendered = [json.dumps(item, ensure_ascii=False) for item in rows]
                if rendered != original:
                    path.write_text("\n".join(rendered) + ("\n" if rendered else ""), encoding="utf-8")
        return changed

    def messages_from_path(self, path: Path) -> list[dict]:
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if item.get("role") == "user":
                item["content"] = self.clean_customer_content(item.get("content", ""))
            if item.get("content"):
                rows.append(item)
        return rows
