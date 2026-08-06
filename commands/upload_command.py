import time
import os
import shutil
import asyncio
from typing import Dict, Any, Optional, Tuple

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import File

from ..services.file_manager import FileManager
from ..services.category_manager import CategoryManager
from ..utils.types import UploadStatus, UserKey, MessageResponse, CategoryType
from ..utils.exceptions import CategoryNotFoundError, CategoryCreateError, CategoryAlreadyExistsError, FileSaveError
from ..utils.logging_utils import log_error, log_operation


class UploadCommand:
    def __init__(self, file_manager: FileManager, category_manager: CategoryManager) -> None:
        self.file_manager: FileManager = file_manager
        self.category_manager: CategoryManager = category_manager
        self.upload_states: Dict[UserKey, UploadStatus] = {}
        self.timeout_tasks: Dict[UserKey, asyncio.Task] = {}

    async def execute(self, event: AstrMessageEvent, category: CategoryType = "default") -> MessageResponse:
        try:
            if not category or category == "default":
                categories_text = "\n".join([f"- {cat}" for cat in await self.category_manager.get_categories_async()])
                yield event.plain_result(
                    f"""投影
上传litematic到指定分类文件夹下使用方法：
/投影 - 查看帮助
/投影 分类名 - 上传文件到指定分类
/投影列表 分类名 - 列出指定分类下的文件
/投影删除 分类名 - 删除指定分类及其下所有文件
/投影获取 分类名 文件名 - 发送指定分类下的文件
/投影材料 分类名 文件名 - 分析指定分类下文件所需的材料
/投影信息 分类名 文件名 - 分析指定分类下文件的详细信息
/投影预览 分类名 文件名 - 生成并显示litematic的2D预览图
/投影3D 分类名 文件名 [动画类型] [帧数] [持续时间] [仰角] [分辨率] - 生成并显示litematic的3D渲染动画

请提供分类名称,例如: /投影 建筑
当前可用分类：
{categories_text}""")
                return

            try:
                if not await self.category_manager.category_exists_async(category):
                    try:
                        await self.category_manager.create_category_async(category)
                        log_operation("创建分类", True, {"category_name": category})
                        yield event.plain_result(f"创建了新分类: {category}")
                    except CategoryAlreadyExistsError:
                        pass
                    except CategoryCreateError as e:
                        log_error(e)
                        yield event.plain_result(f"创建分类失败: {e.message}")
                        return

                user_key: UserKey = f"{event.session_id}_{event.get_sender_id()}"
                timeout_sec = 300

                if user_key in self.timeout_tasks and not self.timeout_tasks[user_key].done():
                    self.timeout_tasks[user_key].cancel()

                self.upload_states[user_key] = {
                    "category": category,
                    "expire_time": time.time() + timeout_sec
                }

                self.timeout_tasks[user_key] = asyncio.create_task(
                    self._handle_timeout(user_key, timeout_sec)
                )

                log_operation("准备上传", True, {"category_name": category, "user_key": user_key})
                yield event.plain_result(f"请在5分钟内上传.litematic文件到{category}分类")
            except Exception as e:
                log_error(e, extra_info={"category_name": category, "operation": "设置上传状态"})
                yield event.plain_result(f"准备上传时出现错误: {str(e)}")
        except Exception as e:
            log_error(e, extra_info={"category_name": category, "operation": "执行上传命令"})
            yield event.plain_result(f"执行命令时出现错误: {str(e)}")

    async def handle_upload(self, event: AstrMessageEvent) -> MessageResponse:
        user_key: UserKey = f"{event.session_id}_{event.get_sender_id()}"

        if user_key not in self.upload_states:
            return

        try:
            file_info = await self._extract_litematic_file(event)
            if file_info is None:
                return

            local_path, filename = file_info
            category = self.upload_states[user_key].get("category", "default")

            try:
                target_path = await self.file_manager.save_litematic_file_async(local_path, category, filename)
                log_operation("保存文件", True, {"category": category, "file_name": filename, "path": target_path})
                yield event.plain_result(f"已成功保存litematic文件到{category}分类: {filename}")
            except FileSaveError as e:
                log_error(e, extra_info={"category": category, "file_name": filename})
                yield event.plain_result(f"保存litematic文件失败: {e.message}")
            except Exception as e:
                log_error(e, extra_info={"category": category, "file_name": filename, "operation": "保存文件"})
                yield event.plain_result(f"保存文件时出现错误: {str(e)}")

            await self._clear_user_state(user_key)
            return
        except Exception as e:
            log_error(e, extra_info={"user_key": user_key, "operation": "处理文件上传"})
            yield event.plain_result(f"处理文件上传时出现错误: {str(e)}")
            await self._clear_user_state(user_key)
        return

    async def handle_auto_upload(self, event: AstrMessageEvent) -> Optional[Tuple[str, str, str]]:
        user_id = str(event.get_sender_id())

        file_info = await self._extract_litematic_file(event)
        if file_info is None:
            return None

        local_path, filename = file_info
        category = f"用户_{user_id}"

        try:
            if not await self.category_manager.category_exists_async(category):
                try:
                    await self.category_manager.create_category_async(category)
                except CategoryAlreadyExistsError:
                    pass
                except CategoryCreateError as e:
                    logger.error(f"自动创建分类失败: {e}")
                    category = "default"

            target_path = await self.file_manager.save_litematic_file_async(local_path, category, filename)
            log_operation("自动保存文件", True, {"category": category, "file_name": filename, "path": target_path})
            return (target_path, category, filename)
        except FileSaveError as e:
            log_error(e, extra_info={"category": category, "file_name": filename})
            logger.error(f"自动保存litematic文件失败: {e.message}")
            return None
        except Exception as e:
            log_error(e, extra_info={"category": category, "file_name": filename, "operation": "自动保存文件"})
            logger.error(f"自动保存文件时出现错误: {str(e)}")
            return None

    async def _extract_litematic_file(self, event: AstrMessageEvent) -> Optional[Tuple[str, str]]:
        for comp in event.message_obj.message:
            if isinstance(comp, File):
                filename = None
                try:
                    raw_message = event.message_obj.raw_message
                    message_list = getattr(raw_message, 'message', None)
                    if message_list:
                        for msg_item in message_list:
                            if isinstance(msg_item, dict) and msg_item.get('type') == 'file':
                                file_data = msg_item.get('data', {})
                                file_name = file_data.get('file')
                                if file_name and file_name.endswith('.litematic'):
                                    filename = file_name
                                    break
                except Exception as e:
                    logger.warning(f"获取文件名失败: {e}")

                if not filename:
                    filename = getattr(comp, 'name', None) or getattr(comp, 'file', None)
                    if filename and not filename.endswith('.litematic'):
                        filename = None

                if filename and filename.endswith('.litematic'):
                    try:
                        file_path = await comp.get_file()
                        logger.info(f"文件已下载到本地: {file_path}")
                        return (file_path, filename)
                    except Exception as e:
                        logger.error(f"下载文件失败: {e}")
                        return None
        return None

    async def _handle_timeout(self, user_key: UserKey, timeout_sec: int) -> None:
        try:
            await asyncio.sleep(timeout_sec)
            if user_key in self.upload_states:
                log_operation("上传超时", False, {"user_key": user_key})
                del self.upload_states[user_key]
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log_error(e, extra_info={"user_key": user_key, "operation": "处理超时"})

    async def _clear_user_state(self, user_key: UserKey) -> None:
        if user_key in self.upload_states:
            del self.upload_states[user_key]

        if user_key in self.timeout_tasks and not self.timeout_tasks[user_key].done():
            self.timeout_tasks[user_key].cancel()
            del self.timeout_tasks[user_key]