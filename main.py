import os
import re
import asyncio
from typing import List, Dict, AsyncGenerator, Optional, Tuple

from astrbot import logger
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image, Node, Nodes, File, Plain
from astrbot.api.star import Star, register, Context
from astrbot.core import AstrBotConfig

from .services.file_manager import FileManager
from .services.category_manager import CategoryManager
from .services.render_manager import RenderManager
from .services.render_3d_manager import Render3DManager
from .services.lang_manager import LangManager
from .utils.config import Config
from .utils.exceptions import RenderError
from .commands.get_command import GetCommand
from .commands.delete_command import DeleteCommand
from .commands.upload_command import UploadCommand
from .commands.list_command import ListCommand
from .commands.material_command import MaterialCommand
from .commands.info_command import InfoCommand
from .commands.preview_command import PreviewCommand
from .commands.render3d_command import Render3DCommand

try:
    from litemapy import Schematic
    LITEMAPY_AVAILABLE = True
except ImportError:
    LITEMAPY_AVAILABLE = False


@register("litematic", "kterna", "识别并分析Litematic文件的AstrBot插件", "1.8.0", "https://github.com/kterna/astrbot_plugin_litematic")
class LitematicPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)

        self.config: Config = Config(context, config)

        plugin_dir: str = os.path.dirname(os.path.abspath(__file__))

        self.category_manager: CategoryManager = CategoryManager(self.config)
        self.file_manager: FileManager = FileManager(self.config, self.category_manager)
        self.render_manager: RenderManager = RenderManager(self.config)
        self.render_3d_manager: Render3DManager = Render3DManager(self.config)
        self.lang_manager: LangManager = LangManager(plugin_dir)

        self.upload_command: UploadCommand = UploadCommand(self.file_manager, self.category_manager)
        self.list_command: ListCommand = ListCommand(self.category_manager, self.file_manager)
        self.delete_command: DeleteCommand = DeleteCommand(self.category_manager, self.file_manager)
        self.get_command: GetCommand = GetCommand(self.file_manager)
        self.material_command: MaterialCommand = MaterialCommand(self.file_manager, self.category_manager, self.lang_manager)
        self.info_command: InfoCommand = InfoCommand(self.file_manager, self.category_manager)
        self.preview_command: PreviewCommand = PreviewCommand(self.file_manager, self.render_manager)
        self.render3d_command: Render3DCommand = Render3DCommand(self.file_manager, self.render_3d_manager)

        self.litematic_dir: str = self.config.get_litematic_dir()
        self.categories_file: str = self.config.get_categories_file()
        os.makedirs(self.litematic_dir, exist_ok=True)
        os.makedirs(os.path.join(plugin_dir, "temp"), exist_ok=True)

        self.litematic_categories: List[str] = self.category_manager.get_categories()

    def load_categories(self) -> None:
        self.litematic_categories = self.category_manager.get_categories()

    def save_categories(self) -> None:
        self.category_manager.save_categories()

    @filter.command("投影", alias=["litematic"])
    async def litematic(self, event: AstrMessageEvent, category: str = "default") -> AsyncGenerator[MessageChain, None]:
        # 检查是否为自动渲染模式切换命令
        full_msg = event.get_message_str().strip()
        msg_content = full_msg
        if msg_content.startswith("/"):
            msg_content = msg_content[1:]
        for cmd_prefix in ["投影", "litematic"]:
            if msg_content.startswith(cmd_prefix):
                msg_content = msg_content[len(cmd_prefix):].strip()
                break

        if msg_content.startswith("自动渲染"):
            parts = msg_content.split()
            if len(parts) >= 2 and parts[1].lower() in ("3d", "2d"):
                mode = parts[1].lower()
                self.config.default_config["auto_render_mode"] = mode
                mode_name = "3D 多视角渲染" if mode == "3d" else "2D 切片预览"
                yield event.plain_result(f"自动渲染模式已切换为：{mode_name}")
                return
            else:
                current_mode = self.config.get_auto_render_mode()
                current_name = "3D 多视角渲染" if current_mode == "3d" else "2D 切片预览"
                yield event.plain_result(
                    f"当前自动渲染模式：{current_name}\n"
                    f"使用方法：\n"
                    f"/投影 自动渲染 3d  - 切换为 3D 多视角渲染\n"
                    f"/投影 自动渲染 2d  - 切换为 2D 切片预览"
                )
                return

        async for response in self.upload_command.execute(event, category):
            yield response

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_upload_litematic(self, event: AstrMessageEvent) -> AsyncGenerator[MessageChain, None]:
        async for response in self.upload_command.handle_upload(event):
            yield response

    @filter.event_message_type(filter.EventMessageType.ALL, priority=999)
    async def handle_auto_litematic(self, event: AstrMessageEvent) -> AsyncGenerator[MessageChain, None]:
        if not self.config.auto_render_3d():
            return

        if event.get_platform_name() != "aiocqhttp":
            return

        is_command = False
        full_msg = event.get_message_str().strip()
        if full_msg.startswith("/"):
            is_command = True

        if is_command:
            return

        group_id = str(event.message_obj.group_id) if event.message_obj.group_id else ""
        if not group_id:
            return

        has_litematic = False
        for comp in event.message_obj.message:
            if isinstance(comp, File):
                raw_message = event.message_obj.raw_message
                message_list = getattr(raw_message, 'message', None)
                if message_list:
                    for msg_item in message_list:
                        if isinstance(msg_item, dict) and msg_item.get('type') == 'file':
                            file_data = msg_item.get('data', {})
                            file_name = file_data.get('file', '')
                            if file_name.endswith('.litematic'):
                                has_litematic = True
                                break
                if not has_litematic:
                    comp_name = getattr(comp, 'name', '') or getattr(comp, 'file', '')
                    if comp_name.endswith('.litematic'):
                        has_litematic = True
                if has_litematic:
                    break

        if not has_litematic:
            return

        show_hint = self.config.show_auto_render_hint()
        if show_hint:
            yield event.plain_result("检测到 .litematic 文件，正在自动处理和渲染，请稍候...")

        result = await self.upload_command.handle_auto_upload(event)
        if result is None:
            yield event.plain_result("文件保存失败")
            return

        target_path, category, filename = result
        render_mode = self.config.get_auto_render_mode()

        if show_hint:
            mode_hint = "3D 多视角" if render_mode == "3d" else "2D 切片预览"
            yield event.plain_result(f"已自动保存 {filename} 到 {category} 分类，正在渲染 {mode_hint}...")

        try:
            if render_mode == "2d":
                await self._handle_auto_render_2d(event, target_path, filename)
            else:
                await self._handle_auto_render_3d(event, target_path, filename)

        except Exception as e:
            logger.error(f"自动渲染失败: {e}")
            import traceback
            logger.error(traceback.format_exc())
            yield event.plain_result(f"自动渲染失败: {str(e)}")

    async def _handle_auto_render_2d(self, event: AstrMessageEvent, target_path: str, filename: str) -> AsyncGenerator[MessageChain, None]:
        """2D 自动渲染：生成多角度切片预览并以合并转发发送"""
        info_text = self._get_litematic_info(target_path, filename)
        material_list = await self.material_command.get_material_list(target_path)

        try:
            img_path = await self.render_manager.render_litematic_async(
                target_path, view_type="combined", scale=1, use_block_models=True
            )

            nodes = []
            info_text_full = f"【{filename}】\n{info_text}\n\n{material_list}"
            nodes.append(Node(content=[Plain(info_text_full)]))
            nodes.append(Node(content=[Image.fromFileSystem(img_path)]))

            message = MessageChain()
            message.chain.append(Nodes(nodes=nodes))
            yield message

            if os.path.exists(img_path):
                os.remove(img_path)

        except Exception as e:
            logger.error(f"2D 渲染失败: {e}")
            yield event.plain_result(f"2D 渲染失败: {str(e)}")

    async def _handle_auto_render_3d(self, event: AstrMessageEvent, target_path: str, filename: str) -> AsyncGenerator[MessageChain, None]:
        """3D 自动渲染：生成多视角 8K 渲染图并以合并转发发送"""
        info_text = self._get_litematic_info(target_path, filename)

        resolution = self.config.get_auto_render_resolution()
        window_size = self._parse_resolution(resolution)

        render_results = await self.render_3d_manager.render_multi_view_async(
            target_path,
            window_size=window_size,
            native_textures=False
        )

        if not render_results:
            yield event.plain_result("渲染失败，无法生成 3D 视图")
            return

        material_list = await self.material_command.get_material_list(target_path)

        nodes = []
        info_text_full = f"【{filename}】\n{info_text}\n\n{material_list}"
        nodes.append(Node(content=[Plain(info_text_full)]))

        for view_name, img_path in render_results:
            try:
                nodes.append(Node(content=[Image.fromFileSystem(img_path)]))
            except Exception as e:
                logger.error(f"添加 {view_name} 视图到合并转发消息失败: {e}")

        if nodes:
            message = MessageChain()
            message.chain.append(Nodes(nodes=nodes))
            yield message

        self._cleanup_temp_files(render_results)

    def _get_litematic_info(self, file_path: str, filename: str = "") -> str:
        if not LITEMAPY_AVAILABLE:
            return "Litemapy 未安装，无法解析文件信息"

        try:
            schematic = Schematic.load(file_path)
            name = filename or os.path.basename(file_path)
            if name.endswith('.litematic'):
                name = name[:-10]
            author = schematic.author or "未知"
            blocks = schematic.block_count if hasattr(schematic, 'block_count') else 0

            dims = ""
            if hasattr(schematic, 'regions') and schematic.regions:
                region_info = []
                for rname, region in schematic.regions.items():
                    region_info.append(f"{region.width}×{region.height}×{region.length}")
                dims = ", ".join(region_info)

            parts = [
                f"名称: {name}",
                f"作者: {author}",
            ]
            if blocks:
                parts.append(f"方块数: {blocks}")
            if dims:
                parts.append(f"尺寸: {dims}")

            return "\n".join(parts)
        except Exception as e:
            logger.error(f"获取 litematic 信息失败: {e}")
            return f"文件信息解析失败: {str(e)}"

    def _parse_resolution(self, resolution: str) -> Optional[Tuple[int, int]]:
        if not resolution or resolution.lower() in ("native", "default"):
            return None

        match = re.match(r"^(\d+)\s*[xX×\*_,]\s*(\d+)$", str(resolution).strip())
        if match:
            w = int(match.group(1))
            h = int(match.group(2))
            if 64 <= w <= 32768 and 64 <= h <= 32768:
                return (w, h)
        return None

    def _cleanup_temp_files(self, render_results: list) -> None:
        try:
            for _, img_path in render_results:
                if img_path and os.path.exists(img_path):
                    os.remove(img_path)
        except Exception as e:
            logger.warning(f"清理临时文件失败: {e}")

    @filter.command("投影列表", alias=["litematic_list"])
    async def litematic_list(self, event: AstrMessageEvent, category: str = "") -> AsyncGenerator[MessageChain, None]:
        async for response in self.list_command.execute(event, category):
            yield response

    @filter.command("投影删除", alias=["litematic_delete"])
    async def litematic_delete(self, event: AstrMessageEvent, category: str = "", filename: str = "") -> AsyncGenerator[MessageChain, None]:
        async for response in self.delete_command.execute(event, category, filename):
            yield response

    @filter.command("投影获取", alias=["litematic_get"])
    async def litematic_get(self, event: AstrMessageEvent, category: str = "", filename: str = "") -> AsyncGenerator[MessageChain, None]:
        async for response in self.get_command.execute(event, category, filename):
            yield response

    @filter.command("投影材料", alias=["litematic_material"])
    async def litematic_material(self, event: AstrMessageEvent, category: str = "", filename: str = "") -> AsyncGenerator[MessageChain, None]:
        async for response in self.material_command.execute(event, category, filename):
            yield response

    @filter.command("投影信息", alias=["litematic_info"])
    async def litematic_info(self, event: AstrMessageEvent, category: str = "", filename: str = "") -> AsyncGenerator[MessageChain, None]:
        async for response in self.info_command.execute(event, category, filename):
            yield response

    @filter.command("投影预览", alias=["litematic_preview"])
    async def litematic_preview(self, event: AstrMessageEvent, category: str = "", filename: str = "", view: str = "combined") -> AsyncGenerator[MessageChain, None]:
        async for response in self.preview_command.execute(event, category, filename, view):
            yield response

    @filter.command("投影3D", alias=["litematic_3d"])
    async def litematic_3d(self, event: AstrMessageEvent, category: str = "", filename: str = "",
                         animation_type: str = "rotation", frames: int = 36,
                         duration: int = 100, elevation: float = 30.0,
                         resolution: str = "native") -> AsyncGenerator[MessageChain, None]:
        async for response in self.render3d_command.execute(
            event, category, filename, animation_type, int(frames), int(duration), float(elevation), resolution
        ):
            yield response