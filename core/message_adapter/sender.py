"""消息发送封装，统一不同会话场景下的发送行为。"""

from pathlib import Path
from typing import Any, List, Optional

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Nodes, Plain, Image, Node, Reply, Video

from ..logger import logger
from ..downloader.utils import strip_media_prefixes

from .node_builder import is_pure_image_gallery


class MessageDeliveryError(RuntimeError):
    """预期发送的内容全部失败。"""


class MessageSender:
    """消息发送器，封装统一的私聊/群聊发送接口。"""

    FORWARD_CHUNK_SIZE = 8
    DIRECT_IMAGE_BATCH_SIZE = 4
    VIDEO_PACK_THRESHOLD = 0

    @staticmethod
    def _image_from_reference(reference: str) -> Image:
        """根据本地路径或 Token URL 构建图片节点。"""
        text = str(reference or "").strip()
        if text.lower().startswith(("http://", "https://")):
            return Image.fromURL(text)
        return Image.fromFileSystem(text)

    @staticmethod
    def _metadata_for_link(link_metadata: Optional[List[dict]], link_idx: int) -> dict:
        if not link_metadata or link_idx >= len(link_metadata):
            return {}
        meta = link_metadata[link_idx]
        return meta if isinstance(meta, dict) else {}

    async def _send_single_node(
        self,
        event: AstrMessageEvent,
        node: Any,
        *,
        quote_message_id: str = "",
    ) -> None:
        content = []
        if quote_message_id:
            content.append(Reply(id=quote_message_id))
        content.append(node)
        await event.send(event.chain_result(content))

    @staticmethod
    async def _finish_best_effort_delivery(
        event: AstrMessageEvent,
        *,
        label: str,
        expected: int,
        succeeded: int,
        errors: list[Exception],
    ) -> None:
        if expected <= 0 or not errors:
            return
        if succeeded <= 0:
            raise MessageDeliveryError(
                f"{label}全部发送失败（{len(errors)}项）"
            ) from errors[0]
        try:
            await event.send(
                event.plain_result(
                    f"{label}有 {len(errors)} 项发送失败，其余内容已发送。"
                )
            )
        except Exception as exc:
            logger.warning(f"发送部分失败提示失败: {exc}")

    def get_sender_info(
        self,
        event: AstrMessageEvent,
        sender_name: str = "视频解析bot",
    ) -> tuple:
        """获取发送者信息

        Args:
            event: 消息事件对象

        Returns:
            包含发送者名称和ID的元组 (sender_name, sender_id)
        """
        sender_name = str(sender_name or "视频解析bot").strip() or "视频解析bot"
        platform = event.get_platform_name()
        sender_id = event.get_self_id()
        if platform not in ("wechatpadpro", "webchat", "gewechat"):
            try:
                sender_id = int(sender_id)
            except (ValueError, TypeError):
                sender_id = 10000
        return sender_name, sender_id

    @staticmethod
    def _plain_text(node: Plain) -> str:
        for attr in ("text", "message", "content"):
            value = getattr(node, attr, None)
            if value:
                return str(value)
        return str(node)

    @staticmethod
    def _onebot_local_file(path: str) -> str:
        value = str(path or "").strip()
        if not value:
            return ""
        if value.lower().startswith(("http://", "https://", "file://")):
            return value
        try:
            if Path(value).is_absolute():
                return Path(value).resolve().as_uri()
        except (OSError, ValueError):
            pass
        return value

    @staticmethod
    def _get_forward_image_file(metadata: dict, image_ordinal: int) -> str:
        """返回第 N 个实际图片节点对应的 OneBot 文件引用。"""
        video_urls = metadata.get("video_urls") or []
        image_urls = metadata.get("image_urls") or []
        image_modes = metadata.get("image_modes") or []
        file_token_urls = metadata.get("file_token_urls") or []
        use_fts = bool(metadata.get("use_file_token_service"))
        seen = 0

        for image_idx, url_list in enumerate(image_urls):
            mode = (
                image_modes[image_idx]
                if image_idx < len(image_modes)
                else ("local" if metadata.get("use_local_files") else "direct")
            )
            if mode == "skip" or not isinstance(url_list, list) or not url_list:
                continue
            image_url = str(url_list[0] or "").strip()
            if not image_url:
                continue
            if seen != image_ordinal:
                seen += 1
                continue

            file_idx = len(video_urls) + image_idx
            if (
                use_fts
                and file_idx < len(file_token_urls)
                and file_token_urls[file_idx]
            ):
                return str(file_token_urls[file_idx]).strip()
            if mode == "local":
                if (
                    file_idx >= len(metadata.get("file_paths") or [])
                    or not (metadata.get("file_paths") or [])[file_idx]
                    or not Path((metadata.get("file_paths") or [])[file_idx]).exists()
                ):
                    # node_builder 对缺失的本地文件不会创建 Image 节点，
                    # 因此该媒体不能计入当前 ordinal。
                    continue
            # NapCat 合并转发图片优先使用源 URL，避免容器本地路径不可见。
            return image_url
        return ""

    @classmethod
    def _get_forward_video_file(cls, metadata: dict, video_ordinal: int) -> str:
        """返回第 N 个实际视频节点对应的 OneBot 文件引用。"""
        video_urls = metadata.get("video_urls") or []
        video_modes = metadata.get("video_modes") or []
        file_paths = metadata.get("file_paths") or []
        file_token_urls = metadata.get("file_token_urls") or []
        use_fts = bool(metadata.get("use_file_token_service"))
        seen = 0

        for video_idx, url_list in enumerate(video_urls):
            mode = (
                video_modes[video_idx]
                if video_idx < len(video_modes)
                else ("local" if metadata.get("use_local_files") else "direct")
            )
            if mode == "skip" or not isinstance(url_list, list) or not url_list:
                continue
            video_url = str(url_list[0] or "").strip()
            if not video_url:
                continue
            if seen != video_ordinal:
                seen += 1
                continue

            if (
                use_fts
                and video_idx < len(file_token_urls)
                and file_token_urls[video_idx]
            ):
                return str(file_token_urls[video_idx]).strip()

            if mode == "local":
                if (
                    video_idx >= len(file_paths)
                    or not file_paths[video_idx]
                    or not Path(file_paths[video_idx]).exists()
                ):
                    # node_builder 对缺失的本地文件不会创建 Video 节点。
                    continue
                local_ref = cls._onebot_local_file(file_paths[video_idx])
                if local_ref:
                    return local_ref

            return strip_media_prefixes(video_url)
        return ""

    @staticmethod
    def _build_onebot_segment(kind: str, value: Any) -> Optional[dict]:
        value = str(value or "").strip()
        if not value:
            return None
        if kind == "text":
            return {"type": "text", "data": {"text": value}}
        if kind in ("image", "video"):
            return {"type": kind, "data": {"file": value}}
        return None

    def _build_onebot_forward_message_chunks(
        self,
        items: list[tuple[str, Any]],
        sender_name: str,
        sender_id: Any,
    ) -> list[list[dict]]:
        if not items:
            return []
        chunk_size = (
            self.FORWARD_CHUNK_SIZE if self.FORWARD_CHUNK_SIZE > 0 else len(items)
        )
        chunk_size = max(1, chunk_size)
        chunks = []
        for start in range(0, len(items), chunk_size):
            messages = []
            for kind, value in items[start : start + chunk_size]:
                segment = self._build_onebot_segment(kind, value)
                if segment is None:
                    continue
                messages.append(
                    {
                        "type": "node",
                        "data": {
                            "name": sender_name,
                            "uin": str(sender_id),
                            "content": [segment],
                        },
                    }
                )
            if messages:
                chunks.append(messages)
        return chunks

    async def _send_onebot_forward_items(
        self,
        event: AstrMessageEvent,
        items: list[tuple[str, Any]],
        sender_name: str,
        sender_id: Any,
    ) -> Optional[tuple[int, int, list[Exception]]]:
        """NapCat/aiocqhttp 优先直调 OneBot 合并转发；不适用时返回 None。"""
        if not items or event.get_platform_name() != "aiocqhttp":
            return None
        bot = getattr(event, "bot", None)
        if bot is None:
            return None

        chunks = self._build_onebot_forward_message_chunks(
            items,
            sender_name,
            sender_id,
        )
        if not chunks:
            return None

        expected = len(chunks)
        succeeded = 0
        errors: list[Exception] = []
        for messages in chunks:
            try:
                if event.is_private_chat():
                    await bot.send_private_forward_msg(
                        user_id=int(event.get_sender_id()),
                        messages=messages,
                    )
                else:
                    await bot.send_group_forward_msg(
                        group_id=int(event.get_group_id()),
                        messages=messages,
                    )
                succeeded += 1
            except Exception as exc:
                errors.append(exc)
                logger.warning(f"OneBot合并转发直调失败: {exc}")
        return expected, succeeded, errors

    async def send_aggregated_results(
        self,
        event: AstrMessageEvent,
        link_metadata: list,
        sender_name: str,
        sender_id: Any,
        large_video_threshold_mb: float = 0.0,
        text_metadata_image: str = "",
    ):
        """使用 Nodes 合并转发发送结果。

        Args:
            event: 消息事件对象
            link_metadata: 链接元数据列表
            sender_name: 发送者名称
            sender_id: 发送者ID
            large_video_threshold_mb: 大视频阈值(MB)
            text_metadata_image: 已渲染的文本元数据图片路径
        """
        normal_metadata = [
            meta for meta in link_metadata if meta.get("is_normal", True)
        ]
        large_media_metadata = [
            meta for meta in link_metadata if meta.get("is_large_media", False)
        ]
        normal_link_nodes = [
            meta["link_nodes"] for meta in normal_metadata if meta.get("link_nodes")
        ]
        large_media_link_nodes = [
            meta["link_nodes"] for meta in large_media_metadata if meta.get("link_nodes")
        ]
        separator = "-------------------------------------"
        expected = 0
        succeeded = 0
        errors: list[Exception] = []
        rendered_image = None
        if text_metadata_image:
            try:
                rendered_image = self._image_from_reference(text_metadata_image)
            except Exception as exc:
                expected += 1
                errors.append(exc)
                logger.warning(f"构建文本元数据图片节点失败: {exc}")

        if normal_link_nodes or rendered_image is not None:
            flat_nodes = []
            direct_nodes = []
            aggregate_link_groups = []
            onebot_item_groups = []
            onebot_compatible = True
            total_videos = sum(
                isinstance(node, Video)
                for meta in normal_metadata
                for node in meta.get("link_nodes", [])
            )
            pack_videos = (
                self.VIDEO_PACK_THRESHOLD > 0
                and total_videos > self.VIDEO_PACK_THRESHOLD
            )

            if rendered_image is not None:
                flat_nodes.append(
                    Node(
                        name=sender_name,
                        uin=sender_id,
                        content=[rendered_image],
                    )
                )
                rendered_ref = self._onebot_local_file(text_metadata_image)
                if rendered_ref:
                    onebot_item_groups.append([("image", rendered_ref)])
                else:
                    onebot_compatible = False

            for meta in normal_metadata:
                link_nodes = meta.get("link_nodes") or []
                metadata = meta.get("metadata") or {}
                link_forward_nodes = []
                link_forward_items = []
                image_ordinal = 0
                video_ordinal = 0

                for node in link_nodes:
                    if node is None:
                        continue
                    if isinstance(node, Plain):
                        link_forward_nodes.append(
                            Node(name=sender_name, uin=sender_id, content=[node])
                        )
                        link_forward_items.append(("text", self._plain_text(node)))
                    elif isinstance(node, Image):
                        link_forward_nodes.append(
                            Node(name=sender_name, uin=sender_id, content=[node])
                        )
                        image_file = self._get_forward_image_file(
                            metadata,
                            image_ordinal,
                        )
                        image_ordinal += 1
                        if image_file:
                            link_forward_items.append(("image", image_file))
                        else:
                            onebot_compatible = False
                    elif isinstance(node, Video):
                        if not pack_videos:
                            direct_nodes.append(node)
                            video_ordinal += 1
                            continue
                        link_forward_nodes.append(
                            Node(name=sender_name, uin=sender_id, content=[node])
                        )
                        video_file = self._get_forward_video_file(
                            metadata,
                            video_ordinal,
                        )
                        video_ordinal += 1
                        if video_file:
                            link_forward_items.append(("video", video_file))
                        else:
                            onebot_compatible = False
                    else:
                        link_forward_nodes.append(
                            Node(name=sender_name, uin=sender_id, content=[node])
                        )
                        onebot_compatible = False

                if link_forward_nodes:
                    aggregate_link_groups.append(link_forward_nodes)
                    onebot_item_groups.append(link_forward_items)

            onebot_items = []
            for group_idx, link_forward_nodes in enumerate(aggregate_link_groups):
                flat_nodes.extend(link_forward_nodes)
                if group_idx < len(aggregate_link_groups) - 1:
                    flat_nodes.append(
                        Node(
                            name=sender_name,
                            uin=sender_id,
                            content=[Plain(separator)],
                        )
                    )

            non_empty_onebot_groups = [group for group in onebot_item_groups if group]
            for group_idx, group in enumerate(non_empty_onebot_groups):
                onebot_items.extend(group)
                if group_idx < len(non_empty_onebot_groups) - 1:
                    onebot_items.append(("text", separator))

            used_onebot = False
            if flat_nodes and onebot_compatible and onebot_items:
                onebot_result = await self._send_onebot_forward_items(
                    event,
                    onebot_items,
                    sender_name,
                    sender_id,
                )
                if onebot_result is not None:
                    onebot_expected, onebot_succeeded, onebot_errors = onebot_result
                    # 全部失败时回退 AstrBot Nodes；部分成功时避免重复重发已成功块。
                    if onebot_succeeded > 0:
                        expected += onebot_expected
                        succeeded += onebot_succeeded
                        errors.extend(onebot_errors)
                        used_onebot = True
                    elif onebot_errors:
                        logger.warning("OneBot合并转发全部失败，回退AstrBot Nodes")

            if flat_nodes and not used_onebot:
                chunk_size = (
                    self.FORWARD_CHUNK_SIZE
                    if self.FORWARD_CHUNK_SIZE > 0
                    else len(flat_nodes)
                )
                chunk_size = max(1, chunk_size)
                for start in range(0, len(flat_nodes), chunk_size):
                    expected += 1
                    try:
                        await event.send(
                            event.chain_result(
                                [Nodes(flat_nodes[start : start + chunk_size])]
                            )
                        )
                        succeeded += 1
                    except Exception as exc:
                        errors.append(exc)
                        logger.warning(f"发送聚合消息失败: {exc}")

            for node in direct_nodes:
                expected += 1
                try:
                    await event.send(event.chain_result([node]))
                    succeeded += 1
                except Exception as exc:
                    errors.append(exc)
                    logger.warning(f"发送聚合外视频节点失败: {exc}")

        if large_media_link_nodes:
            (
                large_expected,
                large_succeeded,
                large_errors,
            ) = await self.send_large_media_results(
                event,
                large_media_link_nodes,
                large_video_threshold_mb,
            )
            expected += large_expected
            succeeded += large_succeeded
            errors.extend(large_errors)

        await self._finish_best_effort_delivery(
            event,
            label="解析结果",
            expected=expected,
            succeeded=succeeded,
            errors=errors,
        )

    async def send_large_media_results(
        self,
        event: AstrMessageEvent,
        link_nodes_list: list,
        large_video_threshold_mb: float = 0.0,
    ) -> tuple[int, int, list[Exception]]:
        """发送大媒体结果（单独发送）

        Args:
            event: 消息事件对象
            link_nodes_list: 链接节点列表
            large_video_threshold_mb: 大视频阈值(MB)
        """
        separator = "-------------------------------------"
        threshold_mb = (
            int(large_video_threshold_mb) if large_video_threshold_mb > 0 else 50
        )
        notice_text = f"⚠️ 链接中包含超过{threshold_mb}MB的视频时将单独发送所有媒体"
        try:
            await event.send(event.plain_result(notice_text))
        except Exception as exc:
            logger.warning(f"发送大媒体提示失败: {exc}")
        expected = 0
        succeeded = 0
        errors: list[Exception] = []
        for link_idx, link_nodes in enumerate(link_nodes_list):
            for node in link_nodes:
                if node is not None:
                    expected += 1
                    try:
                        await event.send(event.chain_result([node]))
                        succeeded += 1
                    except Exception as e:
                        errors.append(e)
                        logger.warning(f"发送大媒体节点失败: {e}")
            if link_idx < len(link_nodes_list) - 1:
                try:
                    await event.send(event.plain_result(separator))
                except Exception as e:
                    logger.warning(f"发送分隔符失败: {e}")
        return expected, succeeded, errors

    async def send_individual_results(
        self,
        event: AstrMessageEvent,
        all_link_nodes: list,
        link_metadata: Optional[List[dict]] = None,
        *,
        quote_user_message: bool = False,
        quote_message_id: str = "",
        text_metadata_image: str = "",
    ) -> None:
        """发送非聚合结果（逐项独立发送）。

        Args:
            event: 消息事件对象
            all_link_nodes: 所有链接节点列表
            link_metadata: 每条链接的构建辅助信息
            quote_user_message: 文本元数据是否引用对应的用户消息
            quote_message_id: 被引用的用户消息 ID
            text_metadata_image: 已渲染的文本元数据图片路径
        """
        separator = "-------------------------------------"
        quote_message_id = str(quote_message_id or "").strip()
        expected = 0
        succeeded = 0
        errors: list[Exception] = []
        if text_metadata_image:
            expected += 1
            try:
                image_node = self._image_from_reference(text_metadata_image)
                await self._send_single_node(
                    event,
                    image_node,
                    quote_message_id=(quote_message_id if quote_user_message else ""),
                )
                succeeded += 1
            except Exception as exc:
                errors.append(exc)
                logger.warning(f"发送文本元数据图片失败: {exc}")

        non_empty_indexes = [
            index for index, link_nodes in enumerate(all_link_nodes) if link_nodes
        ]
        for link_idx, link_nodes in enumerate(all_link_nodes):
            if not link_nodes:
                continue
            meta = self._metadata_for_link(link_metadata, link_idx)
            metadata_text_node = meta.get("metadata_text_node")
            if is_pure_image_gallery(link_nodes):
                texts = [node for node in link_nodes if isinstance(node, Plain)]
                images = [node for node in link_nodes if isinstance(node, Image)]
                if len(texts) == 1 and len(images) == 1:
                    expected += 1
                    try:
                        content = []
                        if (
                            quote_user_message
                            and texts[0] is metadata_text_node
                            and quote_message_id
                        ):
                            content.append(Reply(id=quote_message_id))
                        content.extend([texts[0], images[0]])
                        await event.send(event.chain_result(content))
                        succeeded += 1
                    except Exception as exc:
                        logger.warning(
                            f"合并发送文本和单图失败，回退分开发送: {exc}"
                        )
                        # 合并发送只是优化路径；失败后按两个独立内容重新计数。
                        expected += 1
                        try:
                            await self._send_single_node(
                                event,
                                texts[0],
                                quote_message_id=(
                                    quote_message_id
                                    if (
                                        quote_user_message
                                        and texts[0] is metadata_text_node
                                    )
                                    else ""
                                ),
                            )
                            succeeded += 1
                        except Exception as text_exc:
                            errors.append(text_exc)
                            logger.warning(f"回退发送文本节点失败: {text_exc}")
                        try:
                            await event.send(event.chain_result([images[0]]))
                            succeeded += 1
                        except Exception as image_exc:
                            errors.append(image_exc)
                            logger.warning(f"回退发送单图失败: {image_exc}")
                else:
                    for text in texts:
                        expected += 1
                        try:
                            await self._send_single_node(
                                event,
                                text,
                                quote_message_id=(
                                    quote_message_id
                                    if quote_user_message
                                    and text is metadata_text_node
                                    else ""
                                ),
                            )
                            succeeded += 1
                        except Exception as exc:
                            errors.append(exc)
                            logger.warning(f"发送文本节点失败: {exc}")
                    if images:
                        batch_size = (
                            self.DIRECT_IMAGE_BATCH_SIZE
                            if self.DIRECT_IMAGE_BATCH_SIZE > 0
                            else len(images)
                        )
                        batch_size = max(1, batch_size)
                        for start in range(0, len(images), batch_size):
                            expected += 1
                            try:
                                await event.send(
                                    event.chain_result(
                                        images[start : start + batch_size]
                                    )
                                )
                                succeeded += 1
                            except Exception as exc:
                                errors.append(exc)
                                logger.warning(f"发送图片组失败: {exc}")
            else:
                for node in link_nodes:
                    if node is not None:
                        expected += 1
                        try:
                            await self._send_single_node(
                                event,
                                node,
                                quote_message_id=(
                                    quote_message_id
                                    if (
                                        quote_user_message
                                        and node is metadata_text_node
                                    )
                                    else ""
                                ),
                            )
                            succeeded += 1
                        except Exception as e:
                            errors.append(e)
                            logger.warning(f"发送节点失败: {e}")
            if link_idx in non_empty_indexes[:-1]:
                try:
                    await event.send(event.plain_result(separator))
                except Exception as exc:
                    logger.warning(f"发送分隔符失败: {exc}")
        await self._finish_best_effort_delivery(
            event,
            label="解析结果",
            expected=expected,
            succeeded=succeeded,
            errors=errors,
        )

    async def send_translation_results(
        self,
        event: AstrMessageEvent,
        translation_link_nodes: List[list],
        *,
        should_aggregate_nodes: bool,
        sender_name: str,
        sender_id: Any,
    ) -> None:
        """发送独立翻译节点。"""
        non_empty = [
            (idx, nodes) for idx, nodes in enumerate(translation_link_nodes) if nodes
        ]
        if not non_empty:
            return

        if should_aggregate_nodes:
            flat_nodes = []
            for _, nodes in non_empty:
                for node in nodes:
                    if node is not None:
                        flat_nodes.append(
                            Node(
                                name=sender_name,
                                uin=sender_id,
                                content=[node],
                            )
                        )
            if flat_nodes:
                chunk_size = (
                    self.FORWARD_CHUNK_SIZE
                    if self.FORWARD_CHUNK_SIZE > 0
                    else len(flat_nodes)
                )
                chunk_size = max(1, chunk_size)
                expected = 0
                succeeded = 0
                errors: list[Exception] = []
                for start in range(0, len(flat_nodes), chunk_size):
                    expected += 1
                    try:
                        await event.send(
                            event.chain_result(
                                [Nodes(flat_nodes[start : start + chunk_size])]
                            )
                        )
                        succeeded += 1
                    except Exception as exc:
                        errors.append(exc)
                        logger.warning(f"发送聚合翻译消息失败: {exc}")
                await self._finish_best_effort_delivery(
                    event,
                    label="翻译结果",
                    expected=expected,
                    succeeded=succeeded,
                    errors=errors,
                )
            return

        separator = "-------------------------------------"
        expected = 0
        succeeded = 0
        errors: list[Exception] = []
        for item_idx, (_, nodes) in enumerate(non_empty):
            for node in nodes:
                if node is None:
                    continue
                expected += 1
                try:
                    await self._send_single_node(event, node)
                    succeeded += 1
                except Exception as e:
                    errors.append(e)
                    logger.warning(f"发送翻译节点失败: {e}")
            if item_idx < len(non_empty) - 1:
                try:
                    await event.send(event.plain_result(separator))
                except Exception as exc:
                    logger.warning(f"发送翻译分隔符失败: {exc}")
        await self._finish_best_effort_delivery(
            event,
            label="翻译结果",
            expected=expected,
            succeeded=succeeded,
            errors=errors,
        )

    async def send_zip_result(
        self,
        event: AstrMessageEvent,
        archive_path: str,
    ) -> None:
        """发送本地 ZIP 文件。"""
        try:
            from astrbot.api.message_components import File
        except ImportError as exc:
            raise RuntimeError("当前 AstrBot 版本不支持文件消息组件") from exc

        file_component = File(
            name=Path(archive_path).name,
            file=archive_path,
        )
        await event.send(event.chain_result([file_component]))
