"""
TranslationCache — 纯映射层，零依赖。

原文归一化 → 查表返回译文。不涉及翻译 API 调用。
跨项目共享（LiveTranslate / jimakuchan）。

线程安全，JSON + 原子写入持久化。
"""

import json
import logging
import string
import threading
import time
from pathlib import Path

log = logging.getLogger("TranslationCache")

# 标点字符集（两端剥离用）
_PUNCT = set(string.punctuation + "。！？、，：；「」『』【】（）…～・《》〈〉\"'　")


class TranslationCache:
    """原文→译文映射缓存，零外部依赖。"""

    def __init__(self, max_entries: int = 100000, persist_path: str | Path | None = None):
        self.max_entries = max_entries
        self.persist_path = Path(persist_path or Path(__file__).parent / "translation_cache.json")
        self._cache: dict[tuple[str, str], dict] = {}
        self._lock = threading.Lock()
        self._dirty = False

    # ── 归一化 ──

    @staticmethod
    def normalize(text: str) -> str:
        """归一化：去两端空白 → 全角英数字→半角 → 两端剥离标点。"""
        text = text.strip()
        # 全角英文数字 → 半角
        chars = []
        for c in text:
            code = ord(c)
            if 0xFF01 <= code <= 0xFF5E:
                chars.append(chr(code - 0xFEE0))
            elif code == 0x3000:
                chars.append(" ")
            else:
                chars.append(c)
        text = "".join(chars).strip()
        # 两端反复剥离标点
        while text and text[0] in _PUNCT:
            text = text[1:].strip()
        while text and text[-1] in _PUNCT:
            text = text[:-1].strip()
        return text

    def _make_key(self, text: str, target_lang: str) -> tuple[str, str]:
        return (self.normalize(text), target_lang)

    # ── 核心操作 ──

    def get(self, text: str, target_lang: str = "zh") -> str | None:
        """查缓存，命中返回译文并递增 hit_count。"""
        key = self._make_key(text, target_lang)
        with self._lock:
            entry = self._cache.get(key)
            if entry is not None:
                entry["hit_count"] = entry.get("hit_count", 0) + 1
                self._dirty = True
                return entry["translation"]
        return None

    def put(self, text: str, target_lang: str, translation: str):
        """写入缓存。已存在时不覆盖。"""
        key = self._make_key(text, target_lang)
        with self._lock:
            if key in self._cache:
                return
            self._cache[key] = {
                "translation": translation,
                "timestamp": time.time(),
                "hit_count": 0,
            }
            self._dirty = True
            self._evict_if_needed()

    def __len__(self) -> int:
        with self._lock:
            return len(self._cache)

    def stats(self) -> dict:
        """返回统计信息。"""
        with self._lock:
            total = len(self._cache)
            if total == 0:
                return {"total": 0, "max": self.max_entries}
            hit_counts = [e.get("hit_count", 0) for e in self._cache.values()]
            return {
                "total": total,
                "max": self.max_entries,
                "total_hits": sum(hit_counts),
                "avg_hits": sum(hit_counts) / total if total else 0,
                "max_hits": max(hit_counts) if hit_counts else 0,
                "dirty": self._dirty,
            }

    # ── 淘汰策略 ──

    def _evict_if_needed(self):
        """超上限时，在前半区（最早写入的 50%）淘汰命中次数最低的条目到一半以下。"""
        total = len(self._cache)
        if total <= self.max_entries:
            return
        # 按时间戳升序排列（最老的在前）
        sorted_items = sorted(self._cache.items(), key=lambda x: x[1].get("timestamp", 0))
        half = total // 2
        old_half = sorted_items[:half]
        # 在最早的一半中，按 hit_count 升序排序
        old_half.sort(key=lambda x: x[1].get("hit_count", 0))
        # 淘汰到目标数：降到 max_entries 的一半以下
        target = self.max_entries // 2
        to_remove = total - target
        for key, _ in old_half[:to_remove]:
            del self._cache[key]

    # ── 持久化 ──

    def save(self):
        """原子写入 JSON。只投脏页。"""
        with self._lock:
            if not self._dirty:
                return
            tmp = self.persist_path.with_suffix(".tmp")
            try:
                tmp.parent.mkdir(parents=True, exist_ok=True)
                data = {
                    "version": 1,
                    "entries": {
                        f"{k[0]}||{k[1]}": v
                        for k, v in self._cache.items()
                    },
                }
                tmp.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                tmp.replace(self.persist_path)
                self._dirty = False
                log.info(
                    f"Cache saved: {len(self._cache)} entries -> {self.persist_path}"
                )
            except Exception as e:
                log.warning(f"Save failed: {e}")

    def load(self):
        """从 JSON 加载缓存。"""
        if not self.persist_path.exists():
            log.info(f"No cache file at {self.persist_path}, starting fresh")
            return
        with self._lock:
            try:
                raw = json.loads(self.persist_path.read_text("utf-8"))
                loaded = {}
                for key_str, v in raw.get("entries", {}).items():
                    parts = key_str.split("||", 1)
                    if len(parts) == 2:
                        loaded[(parts[0], parts[1])] = v
                self._cache = loaded
                self._dirty = False
                log.info(
                    f"Cache loaded: {len(self._cache)} entries from {self.persist_path}"
                )
            except Exception as e:
                log.warning(f"Load failed: {e}, starting fresh")
                self._cache = {}
