"""群聊多机器人互斥与短期 URL 冷却。"""
import asyncio
import time
from typing import Dict

from .config_manager import DedupConfig


class DedupGuard:
    def __init__(self, config: DedupConfig):
        self.config = config
        self._competitor_msg_times: Dict[str, float] = {}
        self._url_cooldowns: Dict[str, float] = {}

    def is_group_enabled(self, group_id: str) -> bool:
        if not self.config.enable or not group_id:
            return False
        if not self.config.group:
            return True
        return str(group_id) in self.config.group

    def is_competitor_message(self, sender_id: str) -> bool:
        return bool(
            self.config.competitor_bot_ids
            and str(sender_id) in self.config.competitor_bot_ids
        )

    def record_competitor_message(self, group_id: str) -> None:
        if not group_id:
            return
        self._competitor_msg_times[str(group_id)] = time.monotonic()
        self._cleanup()

    @staticmethod
    def _normalize_url(url: str) -> str:
        value = str(url or "").strip()
        while value and value[-1] in ",.，。":
            value = value[:-1]
        return value

    def _url_key(self, group_id: str, url: str) -> str:
        scope = str(group_id) if group_id else "private"
        return f"{scope}_{self._normalize_url(url)}"

    def is_url_cooldown(self, group_id: str, url: str) -> bool:
        if self.config.url_cooldown_seconds <= 0:
            return False
        last_time = self._url_cooldowns.get(self._url_key(group_id, url), 0.0)
        return (time.monotonic() - last_time) < self.config.url_cooldown_seconds

    def get_url_cooldown_remain(self, group_id: str, url: str) -> float:
        if self.config.url_cooldown_seconds <= 0:
            return 0.0
        last_time = self._url_cooldowns.get(self._url_key(group_id, url), 0.0)
        elapsed = time.monotonic() - last_time
        return max(0.0, self.config.url_cooldown_seconds - elapsed)

    def record_url_cooldown(self, group_id: str, url: str) -> None:
        if self.config.url_cooldown_seconds <= 0:
            return
        self._url_cooldowns[self._url_key(group_id, url)] = time.monotonic()
        self._cleanup()

    async def wait_and_check(self, group_id: str) -> bool:
        if (
            not self.is_group_enabled(group_id)
            or not self.config.competitor_bot_ids
            or self.config.wait_seconds <= 0
        ):
            return False
        group_id_str = str(group_id)
        start_time = time.monotonic()
        await asyncio.sleep(self.config.wait_seconds)
        return self._competitor_msg_times.get(group_id_str, 0.0) >= start_time

    def _cleanup(self) -> None:
        now = time.monotonic()
        retention = max(300.0, self.config.wait_seconds * 10.0)
        self._competitor_msg_times = {
            key: value
            for key, value in self._competitor_msg_times.items()
            if now - value <= retention
        }
        if self.config.url_cooldown_seconds > 0:
            self._url_cooldowns = {
                key: value
                for key, value in self._url_cooldowns.items()
                if now - value <= self.config.url_cooldown_seconds
            }
