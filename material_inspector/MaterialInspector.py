"""
材质查看器 (Material Inspector)
"""
import bpy
import bmesh
import os
import time
import tempfile
from bpy.types import Operator, Panel, PropertyGroup
from bpy.props import (
    IntProperty,
    StringProperty,
    CollectionProperty,
    BoolProperty,
    EnumProperty,
    PointerProperty,
)

# ============================================================
#  常量
# ============================================================
PREVIEW_PREFIX = "[MaterialInspector]_"

# 运行时探测可用的 EEVEE 引擎标识符（BLENDER_EEVEE_NEXT vs BLENDER_EEVEE）
def _detect_eevee_id():
    """探测当前 Blender 版本可用的 EEVEE 引擎标识符"""
    # 通过 RenderSettings engine 枚举项来判断
    try:
        from bpy.types import RenderSettings
        for item in RenderSettings.bl_rna.properties['engine'].enum_items:
            if 'EEVEE' in item.identifier:
                return item.identifier
    except Exception:
        pass
    # 兜底：尝试常见标识符
    return 'BLENDER_EEVEE'

_EEVEE_ID = _detect_eevee_id()
_PREVIEW_ENGINE_ITEMS = [
    (_EEVEE_ID, 'EEVEE', ''),
    ('CYCLES', 'Cycles', ''),
]


def _on_preview_setting_changed(self, context):
    """预览相关设置变更时，通过 timer 延迟触发全部材质预览更新"""
    bpy.app.timers.register(
        lambda: bpy.ops.material_inspector.update_previews('INVOKE_DEFAULT'),
        first_interval=0.2,
    )


# ============================================================
#  属性
# ============================================================

class MaterialCheckItem(PropertyGroup):
    """勾选列表项 —— 材质名称"""
    name: StringProperty(name="材质名称")


class MaterialInspectorSettings(PropertyGroup):
    """材质查看器全局设置，挂载到 Scene"""
    materials_per_row: IntProperty(
        name="每行数量",
        description="预览网格中每行显示的材质数量",
        default=3,
        min=1,
        max=8,
    )
    preview_resolution: IntProperty(
        name="预览分辨率",
        description="生成预览图的边长（像素），修改后仅对新生成的预览生效",
        default=256,
        min=32,
        max=256,
        subtype='PIXEL',
    )
    cell_height: IntProperty(
        name="每行高度",
        description="预览网格中每个材质cell的高度",
        default=6,
        min=1,
        max=10,
    )
    preview_engine: EnumProperty(
        name="预览渲染器",
        description="生成预览图使用的渲染引擎",
        items=_PREVIEW_ENGINE_ITEMS,
        default=_EEVEE_ID,
        update=_on_preview_setting_changed,
    )
    preview_geometry: EnumProperty(
        name="预览几何体",
        description="生成预览图使用的基本几何体",
        items=[
            ('SPHERE', '球体', ''),
            ('PLANE', '平面', ''),
            ('CUBE', '立方体', ''),
            ('CYLINDER', '圆柱体', ''),
        ],
        default='SPHERE',
        update=_on_preview_setting_changed,
    )
    checked_materials: CollectionProperty(
        name="已勾选材质",
        description="供批量删除 / 批量更新预览使用",
        type=MaterialCheckItem,
    )
    active_material: StringProperty(
        name="激活材质",
        description="点击预览图激活的材质（用于赋予材质等操作）",
        default="",
    )
    replace_mode: BoolProperty(
        name="完全替换材质",
        description="赋予材质时替换当前槽位（关闭则追加到末尾）",
        default=True,
    )
    use_fake_user: BoolProperty(
        name="资源保护模式",
        description="新建材质时开启伪用户，防止未使用即被清理",
        default=True,
    )
    search_filter: StringProperty(
        name="搜索材质",
        description="按名称过滤材质，留空则显示全部",
        default="",
    )
    favorite_materials: CollectionProperty(
        name="收藏材质",
        description="Alt+点击小手标记收藏的材质",
        type=MaterialCheckItem,
    )
    sort_mode: EnumProperty(
        name="排序",
        description="材质预览网格排序方式",
        items=[
            ('AZ', 'A-Z', ''),
            ('ZA', 'Z-A', ''),
            ('FAV_AZ', '收藏A-Z', ''),
            ('FAV_ZA', '收藏Z-A', ''),
        ],
        default='AZ',
    )


# ============================================================
#  辅助函数
# ============================================================

def _preview_name(mat_name: str) -> str:
    """材质名 → 预览图数据块名"""
    return PREVIEW_PREFIX + mat_name


def _is_checked(settings: MaterialInspectorSettings, mat_name: str) -> bool:
    """材质是否在勾选列表中"""
    for item in settings.checked_materials:
        if item.name == mat_name:
            return True
    return False


def _toggle_check(settings: MaterialInspectorSettings, mat_name: str) -> None:
    """切换材质的勾选状态"""
    for i, item in enumerate(settings.checked_materials):
        if item.name == mat_name:
            settings.checked_materials.remove(i)
            return
    item = settings.checked_materials.add()
    item.name = mat_name


def _is_favorite(settings: MaterialInspectorSettings, mat_name: str) -> bool:
    """材质是否已收藏"""
    for item in settings.favorite_materials:
        if item.name == mat_name:
            return True
    return False


def _toggle_favorite(settings: MaterialInspectorSettings, mat_name: str) -> None:
    """切换材质的收藏状态"""
    for i, item in enumerate(settings.favorite_materials):
        if item.name == mat_name:
            settings.favorite_materials.remove(i)
            return
    item = settings.favorite_materials.add()
    item.name = mat_name


def _cleanup_preview_image(mat_name: str) -> None:
    """删除与材质关联的预览图"""
    pname = _preview_name(mat_name)
    try:
        if pname in bpy.data.images:
            bpy.data.images.remove(bpy.data.images[pname])
    except AttributeError:
        pass  # bpy.data 受限时跳过（安装/启用阶段）


def _cleanup_plugin_vertex_groups(obj: bpy.types.Object) -> None:
    """删除物体上所有由插件创建的顶点组（[MaterialInspector]_ 前缀）"""
    for vg in list(obj.vertex_groups):
        if vg.name.startswith(PREVIEW_PREFIX):
            obj.vertex_groups.remove(vg)


def _sync_preview_on_rename(old_name: str, new_name: str) -> None:
    """材质重命名时同步预览图名称"""
    old_pname = _preview_name(old_name)
    if old_pname in bpy.data.images:
        img = bpy.data.images[old_pname]
        img.name = _preview_name(new_name)


def _get_all_user_materials():
    """获取所有用户材质（排除形如 .xxx 的隐藏材质）

    安装/启用期间 bpy.data 可能为 _RestrictData，此时返回空列表。
    """
    try:
        return [m for m in bpy.data.materials if not m.name.startswith(".")]
    except AttributeError:
        return []


def _get_sorted_materials(settings: MaterialInspectorSettings):
    """获取按当前排序设置排列的材质列表（含搜索过滤和收藏过滤）"""
    materials = _get_all_user_materials()
    # 搜索过滤
    search = settings.search_filter.strip().lower()
    if search:
        materials = [m for m in materials if search in m.name.lower()]
    # 排序 / 收藏过滤
    sort_mode = settings.sort_mode
    if sort_mode in ('FAV_AZ', 'FAV_ZA'):
        materials = [m for m in materials if _is_favorite(settings, m.name)]
    if sort_mode == 'ZA':
        materials.sort(key=lambda m: m.name.lower(), reverse=True)
    elif sort_mode == 'FAV_AZ':
        materials.sort(key=lambda m: m.name.lower())
    elif sort_mode == 'FAV_ZA':
        materials.sort(key=lambda m: m.name.lower(), reverse=True)
    return materials


def _get_image_icon_id(img: bpy.types.Image) -> int:
    """安全获取图像的图标 ID（兼容 Blender 4.4+）

    Blender 4.4 移除了 Image.icon_id，改用 Image.preview.icon_id。
    注意：不要在此处调用 update_tag()，否则每帧触发 GPU 纹理上传。
    """
    try:
        img.preview_ensure()
        return img.preview.icon_id
    except Exception:
        return 0


def _count_material_users(mat: bpy.types.Material) -> int:
    """统计该材质被多少个 MESH 物体引用"""
    count = 0
    for obj in bpy.data.objects:
        if obj.type != 'MESH':
            continue
        for slot in obj.material_slots:
            if slot.material == mat:
                count += 1
                break  # 每个物体只计一次
    return count


def _cleanup_orphan_previews() -> int:
    """清理孤立预览图（对应材质已不存在的 [MaterialInspector]_ 图像）

    返回清理的数量。
    """
    cleaned = 0
    for img in list(bpy.data.images):
        if not img.name.startswith(PREVIEW_PREFIX):
            continue
        # 提取材质名：去掉前缀
        mat_name = img.name[len(PREVIEW_PREFIX):]
        if mat_name not in bpy.data.materials:
            bpy.data.images.remove(img)
            cleaned += 1
    return cleaned


# ============================================================
#  预览图生成（核心）
# ============================================================

def generate_material_preview(mat: bpy.types.Material, resolution: int = 256, engine: str = '', geometry: str = 'SPHERE') -> bpy.types.Image:
    """为单个材质渲染预览图（EEVEE + 球体）

    流程：
    1. 创建独立的临时场景（球体 + 灯光 + 摄像机）
    2. 渲染到临时 PNG 文件
    3. 加载为 Image 数据块并 pack 进 blend 文件
    4. 清理所有临时对象
    """
    if not engine:
        engine = _EEVEE_ID
    preview_name = _preview_name(mat.name)

    # 删除同名旧预览
    if preview_name in bpy.data.images:
        bpy.data.images.remove(bpy.data.images[preview_name])

    # ---- 保存当前上下文 ----
    original_scene = bpy.context.window.scene
    original_selected = list(bpy.context.selected_objects)
    original_active = bpy.context.view_layer.objects.active
    original_mode = bpy.context.mode
    # 保存所有 3D 视图的摄像机设置（防止临时场景渲染后视角偏移）
    original_viewport_cameras = []
    for area in bpy.context.screen.areas:
        if area.type == 'VIEW_3D':
            for space in area.spaces:
                if space.type == 'VIEW_3D':
                    original_viewport_cameras.append((area, space, space.camera, space.lock_camera))
    # bpy.context.mode 与 mode_set(mode=) 使用不同的字符串，需映射
    _MODE_MAP = {
        'EDIT_MESH': 'EDIT',
        'PAINT_VERTEX': 'VERTEX_PAINT',
        'PAINT_WEIGHT': 'WEIGHT_PAINT',
        'PAINT_TEXTURE': 'TEXTURE_PAINT',
    }
    restore_mode = _MODE_MAP.get(original_mode, original_mode)
    # 必须切到 OBJECT 模式，否则编辑模式下创建球体会破坏当前编辑网格
    if original_mode != 'OBJECT':
        try:
            bpy.ops.object.mode_set(mode='OBJECT')
        except Exception:
            original_mode = 'OBJECT'  # 切换失败则不恢复

    # ---- 创建临时场景 ----
    tmp_scene = bpy.data.scenes.new("__MI_TempScene__")
    tmp_scene.render.engine = engine
    # 当使用 Cycles 时，复制当前场景的渲染设备设置（GPU / CPU）
    if engine == 'CYCLES':
        try:
            tmp_scene.cycles.device = original_scene.cycles.device
        except Exception:
            pass  # 部分 Cycles 版本可能没有此属性
    tmp_scene.render.resolution_x = resolution
    tmp_scene.render.resolution_y = resolution
    tmp_scene.render.film_transparent = True
    # 灰底背景
    tmp_world = bpy.data.worlds.new("__MI_TempWorld__")
    tmp_world.use_nodes = True
    bg = tmp_world.node_tree.nodes["Background"]
    bg.inputs[0].default_value = (0.12, 0.12, 0.12, 1.0)
    tmp_scene.world = tmp_world

    # ---- 摄像机 ----
    cam_data = bpy.data.cameras.new("__MI_TempCam__")
    cam_data.type = 'PERSP'
    cam_data.lens = 50
    cam_obj = bpy.data.objects.new("__MI_TempCam__", cam_data)
    tmp_scene.collection.objects.link(cam_obj)
    cam_obj.location = (0.0, -3.8, 0.9)
    cam_obj.rotation_euler = (1.2217, 0.0, 0.0)  # ~70° 俯角
    tmp_scene.camera = cam_obj

    # ---- 主光 ----
    light_data = bpy.data.lights.new("__MI_TempLight__", 'SUN')
    light_data.energy = 5.0
    light_obj = bpy.data.objects.new("__MI_TempLight__", light_data)
    tmp_scene.collection.objects.link(light_obj)
    light_obj.location = (-2.5, -2.0, 3.5)
    light_obj.rotation_euler = (0.785, 0.0, 0.785)

    # ---- 补光 ----
    fill_data = bpy.data.lights.new("__MI_TempFill__", 'SUN')
    fill_data.energy = 1.2
    fill_obj = bpy.data.objects.new("__MI_TempFill__", fill_data)
    tmp_scene.collection.objects.link(fill_obj)
    fill_obj.location = (2.5, -0.5, 1.0)
    fill_obj.rotation_euler = (-0.5, 0.0, -0.5)

    # ---- 几何体 ----
    if geometry == 'SPHERE':
        bpy.ops.mesh.primitive_uv_sphere_add(radius=1.0, location=(0, 0, 0))
    elif geometry == 'PLANE':
        bpy.ops.mesh.primitive_plane_add(size=2.0, location=(0, 0, 0))
    elif geometry == 'CUBE':
        bpy.ops.mesh.primitive_cube_add(size=2.0, location=(0, 0, 0))
    elif geometry == 'CYLINDER':
        bpy.ops.mesh.primitive_cylinder_add(radius=0.7, depth=2.0, location=(0, 0, 0))
    else:
        bpy.ops.mesh.primitive_uv_sphere_add(radius=1.0, location=(0, 0, 0))
    geo_obj = bpy.context.active_object
    geo_obj.name = "__MI_TempGeo__"
    # 从所有当前集合中移除（operator 可能将物体创建在任意活动集合中）
    for col in list(geo_obj.users_collection):
        col.objects.unlink(geo_obj)
    # 链接到临时场景的主集合
    tmp_scene.collection.objects.link(geo_obj)
    # 赋予材质
    if geo_obj.data.materials:
        geo_obj.data.materials[0] = mat
    else:
        geo_obj.data.materials.append(mat)
    # 着色设置（球体平滑；平面/立方体平直；圆柱体自动平滑）
    if geometry == 'SPHERE':
        for poly in geo_obj.data.polygons:
            poly.use_smooth = True
    elif geometry == 'CYLINDER':
        for poly in geo_obj.data.polygons:
            poly.use_smooth = True
        # 自动平滑：优先旧版属性，不可用时使用 shade_smooth_by_angle 操作符
        try:
            geo_obj.data.use_auto_smooth = True
        except AttributeError:
            bpy.ops.object.shade_smooth_by_angle(angle=0.523599)  # 30°

    # 应用几何体特定造型
    if geometry == 'SPHERE':
        geo_obj.scale = (1.1, 1.1, 1.1)
        geo_obj.location.z = -0.25
    elif geometry == 'PLANE':
        geo_obj.scale = (1.1, 1.1, 1.1)
        geo_obj.rotation_euler = (1.5708, 0.0, 0.0)
        geo_obj.location.z = -0.45
    elif geometry == 'CUBE':
        geo_obj.rotation_euler = (0.0, 0.0, 0.7854)
        geo_obj.scale = (0.8, 0.8, 0.8)
        geo_obj.location.z = -0.25
    elif geometry == 'CYLINDER':
        geo_obj.scale = (1.3, 1.3, 0.75)
        geo_obj.location.z = -0.3

    # ---- 渲染到临时文件 ----
    # 材质名可能含文件系统非法字符，做一次安全替换
    safe_name = "".join(c if c.isalnum() or c in "._- " else "_" for c in mat.name)
    tmp_path = os.path.join(tempfile.gettempdir(), f"__MI_{safe_name}.png")
    tmp_scene.render.filepath = tmp_path
    tmp_scene.render.image_settings.file_format = 'PNG'
    tmp_scene.render.image_settings.color_mode = 'RGBA'

    bpy.context.window.scene = tmp_scene
    bpy.ops.render.render(write_still=True)

    # ---- 加载渲染结果为预览图 ----
    try:
        img = bpy.data.images.load(tmp_path)
        img.name = preview_name
        img.pack()  # 打包到 blend，避免外部文件依赖
        img.update_tag()  # 标记数据变更，强制后续 preview_ensure 使用最新数据
    finally:
        # 删除临时文件
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    # ---- 恢复原始场景与摄像机 ----
    bpy.context.window.scene = original_scene
    # 还原所有 3D 视图的摄像机设置（必须在清理临时对象之前恢复，否则可能引用已删除的临时摄像机）
    for area, space, cam, lock in original_viewport_cameras:
        space.camera = cam
        space.lock_camera = lock
        area.tag_redraw()
    for obj in (geo_obj, cam_obj, light_obj, fill_obj):
        bpy.data.objects.remove(obj, do_unlink=True)
    for data in (cam_data, light_data, fill_data):
        bpy.data.cameras.remove(data) if isinstance(data, bpy.types.Camera) else bpy.data.lights.remove(data)  # type: ignore[arg-type]
    bpy.data.worlds.remove(tmp_world)
    bpy.data.scenes.remove(tmp_scene)

    # ---- 恢复原始选择状态 ----
    # 先取消所有选择
    try:
        bpy.ops.object.select_all(action='DESELECT')
    except Exception:
        pass
    # 重新选择原始物体（跳过可能已被删除的物体）
    for obj in original_selected:
        try:
            obj.select_set(True)
        except ReferenceError:
            pass
    # 恢复激活物体
    if original_active is not None:
        try:
            bpy.context.view_layer.objects.active = original_active
        except ReferenceError:
            pass

    # 恢复原始编辑模式
    if original_mode != 'OBJECT':
        try:
            bpy.ops.object.mode_set(mode=restore_mode)
        except Exception:
            pass

    return img


# ============================================================
#  操作符
# ============================================================

class MI_OT_NewBSDF(Operator):
    """新建 Principled BSDF 材质"""
    bl_idname = "material_inspector.new_bsdf"
    bl_label = "新建 BSDF 材质"
    bl_description = "创建一个新的 BSDF 材质"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        settings = context.scene.material_inspector_settings
        mat = bpy.data.materials.new(name="New Material")
        # 中文本地化 Blender 可能将 "New Material" 自动翻译为 "新材质"，显式回设确保原始名称
        if mat.name != "New Material":
            mat.name = "New Material"
        mat.use_nodes = True
        nodes = mat.node_tree.nodes
        # 确保 Principled BSDF + Material Output 的干净结构
        if "Principled BSDF" not in nodes:
            nodes.clear()
            bsdf = nodes.new("ShaderNodeBsdfPrincipled")
            bsdf.location = (0, 0)
            out = nodes.new("ShaderNodeOutputMaterial")
            out.location = (200, 0)
            mat.node_tree.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

        mat.use_fake_user = settings.use_fake_user

        # 立即生成预览图
        try:
            generate_material_preview(mat, resolution=settings.preview_resolution, engine=settings.preview_engine, geometry=settings.preview_geometry)
        except Exception as exc:
            self.report({'WARNING'}, f"预览生成失败: {exc}")

        self.report({'INFO'}, f"已创建材质: {mat.name}")
        return {'FINISHED'}


class MI_OT_DeleteSelectedMaterials(Operator):
    """删除所有勾选的材质，同时清理模型引用和预览图"""
    bl_idname = "material_inspector.delete_selected_materials"
    bl_label = "删除选中材质"
    bl_description = "删除所有勾选的材质，并断开所有模型的引用"
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        count = len(context.scene.material_inspector_settings.checked_materials)
        if count == 0:
            self.report({'WARNING'}, "请先勾选要删除的材质")
            return {'CANCELLED'}
        return context.window_manager.invoke_confirm(
            self, event,
            title=f"删除 {count} 个材质",
            message=f"将永久删除 {count} 个材质及其所有模型引用，此操作不可撤销。",
            confirm_text="删除",
        )

    def execute(self, context):
        settings = context.scene.material_inspector_settings
        mat_names = [item.name for item in settings.checked_materials]

        if not mat_names:
            self.report({'WARNING'}, "请先勾选要删除的材质")
            return {'CANCELLED'}

        deleted = 0
        for mat_name in mat_names:
            if mat_name not in bpy.data.materials:
                continue
            mat = bpy.data.materials[mat_name]

            # 从所有网格物体中移除该材质
            for obj in bpy.data.objects:
                if obj.type != 'MESH':
                    continue
                # 倒序遍历避免索引移位
                for i in range(len(obj.material_slots) - 1, -1, -1):
                    if obj.material_slots[i].material == mat:
                        obj.active_material_index = i
                        with context.temp_override(object=obj):
                            bpy.ops.object.material_slot_remove()

            # 删除预览图
            _cleanup_preview_image(mat_name)

            # 删除材质
            bpy.data.materials.remove(mat)
            deleted += 1

        # 清空勾选
        settings.checked_materials.clear()
        if settings.active_material in mat_names:
            settings.active_material = ""

        self.report({'INFO'}, f"已删除 {deleted} 个材质")
        return {'FINISHED'}


class MI_OT_UpdatePreviews(Operator):
    """批量更新材质预览图（分帧执行，避免卡顿）"""
    bl_idname = "material_inspector.update_previews"
    bl_label = "更新材质预览图"
    bl_description = "为项目中所有材质生成预览图"
    bl_options = {'REGISTER', 'UNDO'}

    _timer = None
    _queue = []
    _cursor = 0
    _total = 0
    _resolution = 256
    _engine = _EEVEE_ID
    _geometry = 'SPHERE'

    def invoke(self, context, _event):
        settings = context.scene.material_inspector_settings

        # 先清理孤立预览图
        orphan_count = _cleanup_orphan_previews()
        if orphan_count > 0:
            self.report({'INFO'}, f"已清理 {orphan_count} 个孤立预览图")

        self._resolution = settings.preview_resolution
        self._engine = settings.preview_engine
        self._geometry = settings.preview_geometry
        self._queue = _get_sorted_materials(settings)

        if not self._queue:
            self.report({'WARNING'}, "没有可更新的材质")
            return {'CANCELLED'}

        self._cursor = 0
        self._total = len(self._queue)

        wm = context.window_manager
        self._timer = wm.event_timer_add(0.05, window=context.window)
        wm.modal_handler_add(self)
        wm.progress_begin(0, self._total)
        wm.progress_update(0)
        self.report({'INFO'}, f"开始生成预览图（共 {self._total} 个）...")
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type != 'TIMER':
            return {'PASS_THROUGH'}

        wm = context.window_manager

        if self._cursor >= self._total:
            wm.event_timer_remove(self._timer)
            wm.progress_end()
            self.report({'INFO'}, f"预览图更新完成（{self._total} 个）")
            # 刷新 UI
            for area in context.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
            return {'FINISHED'}

        mat = self._queue[self._cursor]
        try:
            generate_material_preview(mat, resolution=self._resolution, engine=self._engine, geometry=self._geometry)
        except Exception as exc:
            self.report({'ERROR'}, f"预览失败 ({mat.name}): {exc}")

        self._cursor += 1
        wm.progress_update(self._cursor)
        return {'PASS_THROUGH'}


class MI_OT_AssignMaterial(Operator):
    """将勾选的材质赋予选中的模型"""
    bl_idname = "material_inspector.assign_material"
    bl_label = "赋予材质"
    bl_description = (
        "将勾选的材质赋予选中的模型。"
        "完全替换模式ON：清空模型材质列表后赋予；"
        "完全替换模式OFF：追加到模型材质列表末尾"
    )
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        settings = context.scene.material_inspector_settings
        checked = [item.name for item in settings.checked_materials]

        if not checked:
            self.report({'WARNING'}, "请先勾选材质")
            return {'CANCELLED'}

        materials = []
        for name in checked:
            mat = bpy.data.materials.get(name)
            if mat:
                materials.append(mat)

        if not materials:
            return {'CANCELLED'}

        targets = [obj for obj in context.selected_objects if obj.type == 'MESH']

        if not targets:
            self.report({'WARNING'}, "请在视图中选择至少一个网格模型")
            return {'CANCELLED'}

        for obj in targets:
            if settings.replace_mode:
                # ---- 完全替换模式 ----
                obj.data.materials.clear()
                _cleanup_plugin_vertex_groups(obj)
                for mat in materials:
                    obj.data.materials.append(mat)
            else:
                # ---- 追加模式 ----
                existing = [s.material.name if s.material else "" for s in obj.material_slots]
                for mat in materials:
                    if mat.name not in existing:
                        obj.data.materials.append(mat)

        mode_text = "替换" if settings.replace_mode else "追加"
        self.report({'INFO'}, f"已将 {len(materials)} 个材质{mode_text}到 {len(targets)} 个对象")

        # 赋予后刷新所有赋予材质的预览图
        for mat in materials:
            try:
                _cleanup_preview_image(mat.name)
                generate_material_preview(
                    mat,
                    resolution=settings.preview_resolution,
                    engine=settings.preview_engine,
                    geometry=settings.preview_geometry,
                )
            except Exception:
                pass

        return {'FINISHED'}


class MI_OT_AssignToVertices(Operator):
    """将激活材质赋予编辑模式中选中的面"""
    bl_idname = "material_inspector.assign_to_vertices"
    bl_label = "赋予顶点"
    bl_description = "将激活材质赋予当前编辑模式下选中的面"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        """仅在编辑模式且有面选中时可执行"""
        if context.mode != 'EDIT_MESH':
            return False
        obj = context.active_object
        if obj is None or obj.type != 'MESH':
            return False
        bm = bmesh.from_edit_mesh(obj.data)
        return any(f.select for f in bm.faces)

    def execute(self, context):
        settings = context.scene.material_inspector_settings
        mat_name = settings.active_material

        if not mat_name or mat_name not in bpy.data.materials:
            self.report({'WARNING'}, "请先在预览网格中点击激活一个材质")
            return {'CANCELLED'}

        mat = bpy.data.materials[mat_name]
        obj = context.active_object

        # 从 bmesh 读取选中的面索引及其包含的顶点索引
        bm = bmesh.from_edit_mesh(obj.data)
        selected_face_indices = [f.index for f in bm.faces if f.select]

        if not selected_face_indices:
            self.report({'WARNING'}, "请先选中至少一个面")
            return {'CANCELLED'}

        # 从选中的面收集唯一顶点（用于顶点组）
        face_vert_set = set()
        for f in bm.faces:
            if f.select:
                for v in f.verts:
                    face_vert_set.add(v.index)
        face_vert_indices = list(face_vert_set)

        # ---- 切到 OBJECT 模式操作数据 ----
        bpy.ops.object.mode_set(mode='OBJECT')

        # 确保材质在物体槽位中
        existing_names = [s.material.name if s.material else "" for s in obj.material_slots]
        if mat.name not in existing_names:
            obj.data.materials.append(mat)

        # 找到材质槽位索引
        target_slot_idx = None
        for i, slot in enumerate(obj.material_slots):
            if slot.material == mat:
                target_slot_idx = i
                break

        if target_slot_idx is None:
            self.report({'ERROR'}, "无法找到材质槽位")
            return {'CANCELLED'}

        # 创建/更新顶点组，添加选中面包含的顶点（必须在 OBJECT 模式）
        vg_name = PREVIEW_PREFIX + mat.name
        vg = obj.vertex_groups.get(vg_name)
        if vg is None:
            vg = obj.vertex_groups.new(name=vg_name)
        vg.add(face_vert_indices, 1.0, 'ADD')

        # ---- 切回 EDIT 模式，Blender 自动保留选择，直接指定材质 ----
        bpy.ops.object.mode_set(mode='EDIT')
        # 确保处于面选择模式，material_slot_assign 才能正确作用
        bpy.ops.mesh.select_mode(type='FACE')

        obj.active_material_index = target_slot_idx
        bpy.ops.object.material_slot_assign()

        # 删除临时顶点组（material_slot_assign 已通过面选择完成赋予，VG 不再需要）
        bpy.ops.object.mode_set(mode='OBJECT')
        vg = obj.vertex_groups.get(vg_name)
        if vg:
            obj.vertex_groups.remove(vg)
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_mode(type='FACE')

        # 刷新预览图
        try:
            _cleanup_preview_image(mat_name)
            generate_material_preview(
                mat,
                resolution=settings.preview_resolution,
                engine=settings.preview_engine,
                geometry=settings.preview_geometry,
            )
        except Exception:
            pass

        self.report({'INFO'}, f"已将 '{mat.name}' 赋予 {len(selected_face_indices)} 个面")
        return {'FINISHED'}


class MI_OT_RefreshSinglePreview(Operator):
    """刷新单个材质的预览图"""
    bl_idname = "material_inspector.refresh_single"
    bl_label = "刷新此材质"
    bl_description = "重新生成此材质的预览图"
    bl_options = {'REGISTER', 'UNDO'}

    material_name: StringProperty(name="材质名称")

    def execute(self, context):
        mat = bpy.data.materials.get(self.material_name)
        if mat is None:
            return {'CANCELLED'}
        try:
            settings = context.scene.material_inspector_settings
            _cleanup_preview_image(self.material_name)
            generate_material_preview(
                mat,
                resolution=settings.preview_resolution,
                engine=settings.preview_engine,
                geometry=settings.preview_geometry,
            )
            self.report({'INFO'}, f"已刷新: {mat.name}")
        except Exception as exc:
            self.report({'ERROR'}, f"刷新失败: {exc}")
        return {'FINISHED'}


class MI_OT_ActivateMaterial(Operator):
    """点击预览图 → 单选材质并在着色器编辑器中激活；Shift+点击 → 多选切换"""
    bl_idname = "material_inspector.activate_material"
    bl_label = "激活材质"
    bl_description = "单击：独选此材质；Shift：多选/减选；Alt：收藏/取消收藏；Ctrl：选择使用模型"
    bl_options = {'REGISTER', 'UNDO'}

    material_name: StringProperty(name="材质名称")

    def _apply_selection(self, settings: MaterialInspectorSettings, shift: bool) -> None:
        """根据 Shift 状态执行选择逻辑（不涉及着色器编辑器）"""
        if shift:
            # Shift+点击：多选切换（未选→加选，已选→减选）
            _toggle_check(settings, self.material_name)
            if _is_checked(settings, self.material_name):
                settings.active_material = self.material_name
            elif settings.active_material == self.material_name:
                settings.active_material = ""
        else:
            # 普通点击：独选 / 取消独选
            if (len(settings.checked_materials) == 1
                    and settings.checked_materials[0].name == self.material_name):
                settings.checked_materials.clear()
                settings.active_material = ""
            else:
                settings.checked_materials.clear()
                item = settings.checked_materials.add()
                item.name = self.material_name
                settings.active_material = self.material_name

    def _activate_shader_editor(self, context: bpy.types.Context) -> set[str]:
        """在着色器编辑器中显示该材质的节点树"""
        mat = bpy.data.materials.get(self.material_name)
        if mat is None:
            return {'FINISHED'}

        for area in context.screen.areas:
            if area.type == 'NODE_EDITOR':
                space = area.spaces.active
                space.node_tree = mat.node_tree
                space.shader_type = 'OBJECT'
                if context.object and context.object.type == 'MESH':
                    for i, slot in enumerate(context.object.material_slots):
                        if slot.material == mat:
                            context.object.active_material_index = i
                            break
                return {'FINISHED'}

        # self.report({'INFO'}, f"已激活: {mat.name}")
        return {'FINISHED'}

    def invoke(self, context, event):
        """通过 invoke 上下文触发（可获取 Ctrl/Shift/Alt 键状态）"""
        settings = context.scene.material_inspector_settings

        if event.ctrl:
            # Ctrl+点击：选择当前场景中所有引用该材质的网格物体
            mat = bpy.data.materials.get(self.material_name)
            if mat:
                bpy.ops.object.select_all(action='DESELECT')
                first = True
                for obj in context.scene.objects:
                    if obj.type != 'MESH':
                        continue
                    for slot in obj.material_slots:
                        if slot.material == mat:
                            obj.select_set(True)
                            if first:
                                context.view_layer.objects.active = obj
                                first = False
                            break
            return {'FINISHED'}
        elif event.alt:
            # Alt+点击：仅收藏/取消收藏，不改变选择
            _toggle_favorite(settings, self.material_name)
        elif event.shift:
            # Shift+点击：多选切换
            self._apply_selection(settings, shift=True)
        else:
            # 普通点击：独选/取消独选
            self._apply_selection(settings, shift=False)

        return self._activate_shader_editor(context)

    def execute(self, context):
        """通过 EXEC 上下文触发（面板按钮默认路径，无事件信息 → 始终走普通点击）"""
        settings = context.scene.material_inspector_settings
        self._apply_selection(settings, shift=False)
        return self._activate_shader_editor(context)


class MI_OT_ToggleCheck(Operator):
    """勾选 / 取消勾选单个材质"""
    bl_idname = "material_inspector.toggle_check"
    bl_label = "勾选材质"
    bl_description = "勾选用于批量删除或更新预览"
    bl_options = {'REGISTER', 'UNDO'}

    material_name: StringProperty(name="材质名称")

    def execute(self, context):
        settings = context.scene.material_inspector_settings
        _toggle_check(settings, self.material_name)
        # 同步 active_material：勾选时激活，取消时清除
        if _is_checked(settings, self.material_name):
            settings.active_material = self.material_name
        elif settings.active_material == self.material_name:
            settings.active_material = ""
        return {'FINISHED'}


class MI_OT_SelectAll(Operator):
    """全选所有材质"""
    bl_idname = "material_inspector.select_all"
    bl_label = "全部选中"
    bl_description = "选择项目中的所有材质"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        settings = context.scene.material_inspector_settings
        settings.checked_materials.clear()
        for mat in _get_all_user_materials():
            item = settings.checked_materials.add()
            item.name = mat.name
        return {'FINISHED'}


class MI_OT_DeselectAll(Operator):
    """取消所有勾选"""
    bl_idname = "material_inspector.deselect_all"
    bl_label = "取消选择"
    bl_description = "取消所有材质的选择"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        settings = context.scene.material_inspector_settings
        settings.checked_materials.clear()
        settings.active_material = ""
        return {'FINISHED'}


class MI_OT_SelectToBefore(Operator):
    """选择当前材质及其之前的所有材质"""
    bl_idname = "material_inspector.select_to_before"
    bl_label = "选择之前"
    bl_description = "选择当前激活材质及排序在它之前的所有材质"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        settings = context.scene.material_inspector_settings
        if not settings.active_material:
            self.report({'WARNING'}, "请先点击预览图激活一个材质")
            return {'CANCELLED'}

        mats = _get_sorted_materials(settings)
        active_idx = None
        for i, mat in enumerate(mats):
            if mat.name == settings.active_material:
                active_idx = i
                break

        if active_idx is None:
            return {'CANCELLED'}

        settings.checked_materials.clear()
        for i in range(active_idx + 1):  # 0 到 active_idx（含）
            item = settings.checked_materials.add()
            item.name = mats[i].name
        return {'FINISHED'}


class MI_OT_SelectToAfter(Operator):
    """选择当前材质及其之后的所有材质"""
    bl_idname = "material_inspector.select_to_after"
    bl_label = "选择之后"
    bl_description = "选择当前激活材质及排序在它之后的所有材质"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        settings = context.scene.material_inspector_settings
        if not settings.active_material:
            self.report({'WARNING'}, "请先点击预览图激活一个材质")
            return {'CANCELLED'}

        mats = _get_sorted_materials(settings)
        active_idx = None
        for i, mat in enumerate(mats):
            if mat.name == settings.active_material:
                active_idx = i
                break

        if active_idx is None:
            return {'CANCELLED'}

        settings.checked_materials.clear()
        for i in range(active_idx, len(mats)):  # active_idx 到末尾（含）
            item = settings.checked_materials.add()
            item.name = mats[i].name
        return {'FINISHED'}


class MI_OT_CopySelectedMaterials(Operator):
    """复制选中的所有材质（自动生成新名称）"""
    bl_idname = "material_inspector.copy_selected"
    bl_label = "复制选中材质"
    bl_description = "复制所有选择的材质，自动生成唯一名称"
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        count = len(context.scene.material_inspector_settings.checked_materials)
        if count == 0:
            self.report({'WARNING'}, "请先勾选要复制的材质")
            return {'CANCELLED'}
        return context.window_manager.invoke_confirm(
            self, event,
            title=f"复制 {count} 个材质",
            message=f"将复制 {count} 个材质（自动生成新名称），此操作可撤销。",
            confirm_text="复制",
        )

    def execute(self, context):
        settings = context.scene.material_inspector_settings
        mat_names = [item.name for item in settings.checked_materials]
        copied = 0

        for mat_name in mat_names:
            if mat_name not in bpy.data.materials:
                continue
            src = bpy.data.materials[mat_name]
            copy_mat = src.copy()
            # 保留伪用户状态和收藏状态
            copy_mat.use_fake_user = src.use_fake_user
            if _is_favorite(settings, mat_name):
                item = settings.favorite_materials.add()
                item.name = copy_mat.name
            copied += 1

            # 为新副本生成预览图
            try:
                generate_material_preview(
                    copy_mat,
                    resolution=settings.preview_resolution,
                    engine=settings.preview_engine,
                    geometry=settings.preview_geometry,
                )
            except Exception as exc:
                self.report({'WARNING'}, f"预览生成失败 ({copy_mat.name}): {exc}")

        self.report({'INFO'}, f"已复制 {copied} 个材质")
        return {'FINISHED'}


class MI_OT_UnlinkSelectedMaterials(Operator):
    """切断选中的材质的所有模型引用"""
    bl_idname = "material_inspector.unlink_selected"
    bl_label = "断离选中材质"
    bl_description = "将选择的材质从所有模型上移除引用，但保留材质本身"
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        count = len(context.scene.material_inspector_settings.checked_materials)
        if count == 0:
            self.report({'WARNING'}, "请先勾选要断离的材质")
            return {'CANCELLED'}
        return context.window_manager.invoke_confirm(
            self, event,
            title=f"断离 {count} 个材质",
            message=f"将从所有模型上移除 {count} 个材质的引用，材质本身不会被删除。此操作可撤销。",
            confirm_text="断离",
        )

    def execute(self, context):
        settings = context.scene.material_inspector_settings
        mat_names = [item.name for item in settings.checked_materials]

        for mat_name in mat_names:
            if mat_name not in bpy.data.materials:
                continue
            mat = bpy.data.materials[mat_name]

            for obj in bpy.data.objects:
                if obj.type != 'MESH':
                    continue
                # 倒序遍历避免索引移位
                for i in range(len(obj.material_slots) - 1, -1, -1):
                    if obj.material_slots[i].material == mat:
                        obj.active_material_index = i
                        with context.temp_override(object=obj):
                            bpy.ops.object.material_slot_remove()

        self.report({'INFO'}, f"已断离 {len(mat_names)} 个材质的引用")
        return {'FINISHED'}


class MI_OT_ResetSelectedMaterials(Operator):
    """初始化选中的所有材质，重置所有节点和设置"""
    bl_idname = "material_inspector.reset_selected"
    bl_label = "重置选中材质"
    bl_description = "将选择的材质重置为干净的 BSDF，清除所有节点和设置"
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        count = len(context.scene.material_inspector_settings.checked_materials)
        if count == 0:
            self.report({'WARNING'}, "请先勾选要重置的材质")
            return {'CANCELLED'}
        return context.window_manager.invoke_confirm(
            self, event,
            title=f"重置 {count} 个材质",
            message=f"将把 {count} 个材质还原为默认 Principled BSDF，所有自定义节点和参数将丢失。此操作可撤销。",
            confirm_text="重置",
        )

    def execute(self, context):
        settings = context.scene.material_inspector_settings
        mat_names = [item.name for item in settings.checked_materials]

        for mat_name in mat_names:
            if mat_name not in bpy.data.materials:
                continue
            mat = bpy.data.materials[mat_name]

            # 清除所有节点，重建干净的 Principled BSDF
            if mat.node_tree:
                mat.node_tree.nodes.clear()
                nodes = mat.node_tree.nodes
                bsdf = nodes.new("ShaderNodeBsdfPrincipled")
                bsdf.location = (0, 0)
                out = nodes.new("ShaderNodeOutputMaterial")
                out.location = (200, 0)
                mat.node_tree.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

            # 重置材质基础属性
            mat.diffuse_color = (0.8, 0.8, 0.8, 1.0)
            mat.metallic = 0.0
            mat.roughness = 0.5

            # 重置后重新生成预览图
            try:
                _cleanup_preview_image(mat_name)
                generate_material_preview(
                    mat,
                    resolution=settings.preview_resolution,
                    engine=settings.preview_engine,
                    geometry=settings.preview_geometry,
                )
            except Exception as exc:
                self.report({'WARNING'}, f"预览生成失败 ({mat_name}): {exc}")

        self.report({'INFO'}, f"已重置 {len(mat_names)} 个材质")
        return {'FINISHED'}


class MI_OT_RenameMaterial(Operator):
    """重命名材质"""
    bl_idname = "material_inspector.rename_material"
    bl_label = "重命名材质"
    bl_description = "修改材质名称"
    bl_options = {'REGISTER', 'UNDO'}

    material_name: StringProperty(name="材质名称")
    new_name: StringProperty(name="新名称")

    def invoke(self, context, event):
        mat = bpy.data.materials.get(self.material_name)
        if mat is None:
            return {'CANCELLED'}
        self.new_name = mat.name
        return context.window_manager.invoke_props_dialog(self, width=260)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "new_name", text="名称")

    def execute(self, context):
        if not self.new_name or self.new_name == self.material_name:
            return {'CANCELLED'}
        if self.new_name in bpy.data.materials:
            self.report({'WARNING'}, f"名称 '{self.new_name}' 已被使用")
            return {'CANCELLED'}
        mat = bpy.data.materials.get(self.material_name)
        if mat is None:
            return {'CANCELLED'}
        mat.name = self.new_name

        # 同步预览图名称（材质名称变更可能不触发 depsgraph 更新）
        _sync_preview_on_rename(self.material_name, self.new_name)

        # 同步收藏、勾选、激活材质中的名称引用
        settings = context.scene.material_inspector_settings
        old_name = self.material_name
        new_name = self.new_name
        for item in settings.favorite_materials:
            if item.name == old_name:
                item.name = new_name
                break
        for item in settings.checked_materials:
            if item.name == old_name:
                item.name = new_name
                break
        if settings.active_material == old_name:
            settings.active_material = new_name

        self.report({'INFO'}, f"已重命名为: {self.new_name}")
        return {'FINISHED'}


class MI_OT_CleanUnusedMaterials(Operator):
    """清除所有未被模型引用且未设置伪用户的材质"""
    bl_idname = "material_inspector.clean_unused_materials"
    bl_label = "清除未使用材质"
    bl_description = "删除项目中所有无模型引用且无伪用户的材质"
    bl_options = {'REGISTER', 'UNDO'}

    def _get_unused(self):
        """收集未使用材质列表"""
        unused = []
        for mat in _get_all_user_materials():
            if _count_material_users(mat) == 0 and not mat.use_fake_user:
                unused.append(mat.name)
        return unused

    def invoke(self, context, event):
        unused = self._get_unused()
        if not unused:
            self.report({'INFO'}, "没有未使用的材质")
            return {'CANCELLED'}
        return context.window_manager.invoke_confirm(
            self, event,
            title=f"清除 {len(unused)} 个未使用材质",
            message=f"将永久删除 {len(unused)} 个无引用且无伪用户的材质，此操作不可撤销。",
            confirm_text="清除",
        )

    def execute(self, context):
        unused = self._get_unused()
        if not unused:
            return {'CANCELLED'}
        deleted = 0
        for mat_name in unused:
            if mat_name not in bpy.data.materials:
                continue
            mat = bpy.data.materials[mat_name]
            # 清理材质在所有模型上的引用
            for obj in bpy.data.objects:
                if obj.type != 'MESH':
                    continue
                for i in range(len(obj.material_slots) - 1, -1, -1):
                    if obj.material_slots[i].material == mat:
                        obj.active_material_index = i
                        with context.temp_override(object=obj):
                            bpy.ops.object.material_slot_remove()
            _cleanup_preview_image(mat_name)
            bpy.data.materials.remove(mat)
            deleted += 1
        self.report({'INFO'}, f"已清除 {deleted} 个未使用材质")
        return {'FINISHED'}


class MI_OT_CleanUnusedTextures(Operator):
    """清除所有未被引用且非插件预览图的纹理"""
    bl_idname = "material_inspector.clean_unused_textures"
    bl_label = "清除未使用纹理"
    bl_description = "删除项目中所有未被引用且非本插件生成的纹理图像"
    bl_options = {'REGISTER', 'UNDO'}

    def _get_unused(self):
        """收集未使用纹理列表（排除插件预览图）"""
        unused = []
        for img in bpy.data.images:
            if img.name.startswith("."):
                continue
            if img.name.startswith(PREVIEW_PREFIX):
                continue
            if img.users == 0:
                unused.append(img.name)
        return unused

    def invoke(self, context, event):
        unused = self._get_unused()
        if not unused:
            self.report({'INFO'}, "没有未使用的纹理")
            return {'CANCELLED'}
        return context.window_manager.invoke_confirm(
            self, event,
            title=f"清除 {len(unused)} 个未使用纹理",
            message=f"将永久删除 {len(unused)} 个未被引用的纹理图像，此操作不可撤销。",
            confirm_text="清除",
        )

    def execute(self, context):
        unused = self._get_unused()
        if not unused:
            return {'CANCELLED'}
        deleted = 0
        for img_name in unused:
            if img_name not in bpy.data.images:
                continue
            bpy.data.images.remove(bpy.data.images[img_name])
            deleted += 1
        self.report({'INFO'}, f"已清除 {deleted} 个未使用纹理")
        return {'FINISHED'}


class MI_OT_GeometryPreview(Operator):
    """在场景中创建预览几何体并赋予选中材质"""
    bl_idname = "material_inspector.geometry_preview"
    bl_label = "几何预览"
    bl_description = "根据几何体选项在游标处创建预览几何体，赋予选中材质"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        settings = context.scene.material_inspector_settings
        geometry = settings.preview_geometry
        checked = [item.name for item in settings.checked_materials]

        if not checked:
            self.report({'WARNING'}, "请先勾选材质")
            return {'CANCELLED'}

        # 删除场景中已有的预览几何体（按名称检测 4 种类型）
        preview_names = [
            PREVIEW_PREFIX + "PreviewSphere",
            PREVIEW_PREFIX + "PreviewPlane",
            PREVIEW_PREFIX + "PreviewCube",
            PREVIEW_PREFIX + "PreviewCylinder",
        ]
        for pname in preview_names:
            obj = bpy.data.objects.get(pname)
            if obj:
                bpy.data.objects.remove(obj, do_unlink=True)

        cursor_loc = context.scene.cursor.location.copy()

        # 根据选项创建几何体
        if geometry == 'SPHERE':
            bpy.ops.mesh.primitive_uv_sphere_add(
                segments=64, ring_count=32, radius=1.0, location=cursor_loc,
            )
            obj = context.active_object
            obj.name = PREVIEW_PREFIX + "PreviewSphere"
            for poly in obj.data.polygons:
                poly.use_smooth = True
        elif geometry == 'PLANE':
            bpy.ops.mesh.primitive_plane_add(size=2.0, location=cursor_loc)
            obj = context.active_object
            obj.name = PREVIEW_PREFIX + "PreviewPlane"
        elif geometry == 'CUBE':
            bpy.ops.mesh.primitive_cube_add(size=2.0, location=cursor_loc)
            obj = context.active_object
            obj.name = PREVIEW_PREFIX + "PreviewCube"
        elif geometry == 'CYLINDER':
            bpy.ops.mesh.primitive_cylinder_add(
                vertices=64, radius=1.0, depth=2.0, location=cursor_loc,
            )
            obj = context.active_object
            obj.name = PREVIEW_PREFIX + "PreviewCylinder"
            for poly in obj.data.polygons:
                poly.use_smooth = True
            try:
                obj.data.use_auto_smooth = True
            except AttributeError:
                bpy.ops.object.shade_smooth_by_angle(angle=0.523599)
        else:
            return {'CANCELLED'}

        # 从当前集合中移除，链接到场景主集合（避免留在默认"Collection"中）
        for col in list(obj.users_collection):
            col.objects.unlink(obj)
        context.scene.collection.objects.link(obj)

        # 赋予所有选中材质
        obj.data.materials.clear()
        for mat_name in checked:
            mat = bpy.data.materials.get(mat_name)
            if mat:
                obj.data.materials.append(mat)

        # 将 3D 视图着色方式切换为"材质预览"（已为材质预览或渲染时忽略）
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D':
                        current = space.shading.type
                        if current not in ('MATERIAL', 'RENDERED'):
                            space.shading.type = 'MATERIAL'

        # 选中并激活该几何体
        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        context.view_layer.objects.active = obj

        # 将属性窗口切换到该物体的"材质"选项卡
        # 首次创建几何体时，space.context 枚举可能尚未包含 MATERIAL，用 timer 延迟到下一帧重试
        def _switch_to_material_tab():
            for a in bpy.context.screen.areas:
                if a.type == 'PROPERTIES':
                    for s in a.spaces:
                        if s.type == 'PROPERTIES':
                            try:
                                s.context = 'MATERIAL'
                            except TypeError:
                                pass
            return None  # 单次执行

        bpy.app.timers.register(_switch_to_material_tab, first_interval=0.05)

        # 将 3D 视图焦点切换到该物体
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                for region in area.regions:
                    if region.type == 'WINDOW':
                        with context.temp_override(area=area, region=region):
                            bpy.ops.view3d.view_selected()
        
        self.report({'INFO'}, f"已创建 {geometry} 预览，赋予 {len(checked)} 个材质")
        return {'FINISHED'}


class MI_OT_ToggleFakeUser(Operator):
    """点击引用计数切换材质的伪用户状态"""
    bl_idname = "material_inspector.toggle_fake_user"
    bl_label = "切换伪用户"
    bl_description = "切换该材质的伪用户标记，防止未引用时被自动清理"
    bl_options = {'REGISTER', 'UNDO'}

    material_name: StringProperty(name="材质名称")

    def execute(self, context):
        mat = bpy.data.materials.get(self.material_name)
        if mat is None:
            return {'CANCELLED'}
        mat.use_fake_user = not mat.use_fake_user
        self.report({'INFO'}, f"{'启用' if mat.use_fake_user else '停用'}伪用户: {mat.name}")
        return {'FINISHED'}


class MI_OT_SelectModelMaterials(Operator):
    """根据选中模型的材质列表来勾选材质（并集/交集/差集）"""
    bl_idname = "material_inspector.select_model_materials"
    bl_label = "选择模型材质"
    bl_description = "根据当前选中网格模型的材质列表勾选材质"
    bl_options = {'REGISTER', 'UNDO'}

    mode: EnumProperty(
        name="模式",
        items=[
            ('UNION', "并集", "选中所有模型上的全部材质"),
            ('INTERSECTION', "交集", "仅选中所有模型共有的材质"),
            ('DIFFERENCE', "差集", "仅选中各模型独有的材质"),
        ],
        default='UNION',
    )

    @classmethod
    def poll(cls, context):
        return any(obj.type == 'MESH' for obj in context.selected_objects)

    def execute(self, context):
        # 收集每个模型的非空材质名集合
        model_mats = []
        for obj in context.selected_objects:
            if obj.type != 'MESH':
                continue
            names = set()
            for slot in obj.material_slots:
                if slot.material and not slot.material.name.startswith("."):
                    names.add(slot.material.name)
            if names:
                model_mats.append(names)

        if not model_mats:
            self.report({'WARNING'}, "选中模型上没有材质")
            return {'CANCELLED'}

        if self.mode == 'UNION':
            result = set.union(*model_mats)
        elif self.mode == 'INTERSECTION':
            result = set.intersection(*model_mats)
        elif self.mode == 'DIFFERENCE':
            # 统计每个材质在几个模型中出现，仅保留出现次数 == 1 的
            from collections import Counter
            cnt = Counter()
            for names in model_mats:
                cnt.update(names)
            result = {name for name, c in cnt.items() if c == 1}
        else:
            return {'CANCELLED'}

        settings = context.scene.material_inspector_settings
        settings.checked_materials.clear()

        if not result:
            self.report({'INFO'}, "没有符合条件的材质，已清空选择")
            return {'FINISHED'}
        # 按当前排序顺序写入
        for mat in _get_sorted_materials(settings):
            if mat.name in result:
                item = settings.checked_materials.add()
                item.name = mat.name

        self.report({'INFO'}, f"已勾选 {len(result)} 个材质")
        return {'FINISHED'}


class MI_OT_DeleteKey(Operator):
    """Del 键入口 —— 勾选了材质就删材质，没勾选则透传给默认行为"""
    bl_idname = "material_inspector.delete_key"
    bl_label = "删除勾选材质 (Del)"
    bl_description = "按 Delete 键删除已勾选材质"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if context.scene.material_inspector_settings.checked_materials:
            return bpy.ops.material_inspector.delete_selected_materials('INVOKE_DEFAULT')
        # 没有勾选材质 → 返回 CANCELLED，让 Blender 处理默认 Del（删除物体）
        return {'CANCELLED'}


# ============================================================
#  重命名 / 删除监听
# ============================================================

# 记录上一次的材质名快照，用于检测重命名与删除
_prev_material_names = set()


def _rename_and_refresh_preview(old_name: str, new_name: str) -> None:
    """重命名预览图，并安排延迟重新生成"""
    old_pname = _preview_name(old_name)
    new_pname = _preview_name(new_name)
    if old_pname in bpy.data.images:
        img = bpy.data.images[old_pname]
        img.name = new_pname

    # 使用 timer 延迟一帧执行重新生成（避免在 depsgraph handler 中直接渲染）
    bpy.app.timers.register(
        lambda: _deferred_regenerate(new_name),
        first_interval=0.1,
    )


def _deferred_regenerate(mat_name: str) -> None:
    """timer 回调：重新生成指定材质的预览图（已有预览则跳过，避免重复）"""
    mat = bpy.data.materials.get(mat_name)
    if mat is None:
        return
    # 如果预览图已存在，说明已被其他操作生成过，跳过
    pname = _preview_name(mat_name)
    if pname in bpy.data.images:
        return
    try:
        settings = bpy.context.scene.material_inspector_settings
        generate_material_preview(
            mat,
            resolution=settings.preview_resolution,
            engine=settings.preview_engine,
            geometry=settings.preview_geometry,
        )
    except Exception:
        pass


def _on_depsgraph_update(scene, depsgraph):
    """depsgraph 更新时检测材质删除 / 重命名 / 新增"""
    global _prev_material_names
    try:
        current = {m.name for m in bpy.data.materials if not m.name.startswith(".")}
    except AttributeError:
        return  # bpy.data 受限时跳过
    prev = _prev_material_names

    if prev and prev != current:
        removed = prev - current
        added = current - prev

        # 检测重命名（总数不变、一增一减 → 判定为重命名）
        if len(removed) == 1 and len(added) == 1:
            _rename_and_refresh_preview(removed.pop(), added.pop())
        else:
            # 纯删除 —— 清理预览图
            for name in removed:
                _cleanup_preview_image(name)
            # 纯新增 —— 为新材质生成预览图（通过 timer 延迟，避免在 handler 中直接渲染）
            for name in added:
                bpy.app.timers.register(
                    lambda n=name: _deferred_regenerate(n),
                    first_interval=0.1,
                )

    _prev_material_names = current


def _on_load_post(dummy1, dummy2):
    """项目加载后延迟触发已有预览图的材质更新"""
    def _trigger_update():
        try:
            # 仅当存在已有预览图的材质时才触发更新
            for mat in _get_all_user_materials():
                if _preview_name(mat.name) in bpy.data.images:
                    bpy.ops.material_inspector.update_previews('INVOKE_DEFAULT')
                    break
        except Exception:
            pass
    # 延迟 1 秒，等待 Blender UI 完全初始化
    bpy.app.timers.register(_trigger_update, first_interval=1.0)


# ============================================================
#  面板
# ============================================================

class MI_OT_CleanEmptySlots(Operator):
    """清除选中模型材质列表中的空槽位"""
    bl_idname = "material_inspector.clean_empty_slots"
    bl_label = "空材质清理"
    bl_description = "清除当前选中模型材质列表中的所有空材质槽位"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(obj.type == 'MESH' for obj in context.selected_objects)

    def execute(self, context):
        cleaned = 0
        for obj in context.selected_objects:
            if obj.type != 'MESH':
                continue
            # 倒序遍历避免索引移位
            for i in range(len(obj.material_slots) - 1, -1, -1):
                if obj.material_slots[i].material is None:
                    obj.active_material_index = i
                    with context.temp_override(object=obj):
                        bpy.ops.object.material_slot_remove()
                    cleaned += 1
        if cleaned == 0:
            self.report({'INFO'}, "没有发现空材质槽位")
        else:
            self.report({'INFO'}, f"已清理 {cleaned} 个空材质槽位")
        return {'FINISHED'}


class MATERIALINSPECTOR_PT_Panel(Panel):
    """材质查看器主面板 —— 3D 视图侧边栏"""
    bl_label = "材质查看器"
    bl_idname = "MATERIALINSPECTOR_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "材质查看器"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.material_inspector_settings

        # ---------- 顶部按钮行 ----------
        row = layout.row(align=True)
        row.scale_y = 1.3
        row.operator("material_inspector.new_bsdf", text="新建BSDF", icon='ADD')
        row.operator("material_inspector.geometry_preview", text="几何预览", icon='MESH_UVSPHERE')
        row.operator("material_inspector.update_previews", text="更新预览", icon='RENDER_STILL')
        row.operator("material_inspector.assign_material", text="赋予材质", icon='MATERIAL')

        # ---------- 搜索框 ----------
        layout.prop(settings, "search_filter", text="", icon='VIEWZOOM')

        # ---------- 选择按钮行 ----------
        row = layout.row(align=True)
        row.operator("material_inspector.select_all", text="全选", icon='CHECKBOX_HLT')
        row.operator("material_inspector.deselect_all", text="取消选择", icon='CHECKBOX_DEHLT')
        row.operator("material_inspector.select_to_before", text="选之前", icon='TRIA_UP')
        row.operator("material_inspector.select_to_after", text="选之后", icon='TRIA_DOWN')

        # ---------- 模型材质选择 ----------
        row = layout.row(align=True)
        op = row.operator("material_inspector.select_model_materials", text="选择模型材质（并集）", icon='SELECT_SET')
        op.mode = 'UNION'
        op = row.operator("material_inspector.select_model_materials", text="选择模型材质（交集）", icon='SELECT_INTERSECT')
        op.mode = 'INTERSECTION'
        op = row.operator("material_inspector.select_model_materials", text="选择模型材质（差集）", icon='SELECT_DIFFERENCE')
        op.mode = 'DIFFERENCE'

        # ---------- 第三排：复制 / 断离 / 重置 / 赋予顶点 ----------
        row = layout.row(align=True)
        row.operator("material_inspector.copy_selected", text="复制材质", icon='DUPLICATE')
        row.operator("material_inspector.unlink_selected", text="断离引用", icon='UNLINKED')
        row.operator("material_inspector.reset_selected", text="重置材质", icon='LOOP_BACK')
        row.operator("material_inspector.assign_to_vertices", text="赋予顶点", icon='SNAP_VERTEX')

        layout.separator()

        # ---------- 材质预览网格 ----------
        materials = _get_sorted_materials(settings)
        search = settings.search_filter.strip().lower()

        total = len(materials)
        if total == 0:
            if search:
                layout.label(text=f"未找到含有 \"{settings.search_filter}\" 的材质", icon='INFO')
            elif settings.sort_mode in ('FAV_AZ', 'FAV_ZA'):
                layout.label(text="暂无收藏的材质，请通过Alt+左键点选材质来收藏", icon='SOLO_ON')
            else:
                layout.label(text="项目中暂无材质", icon='INFO')
            # 不提前 return，继续渲染下方 UI（清理按钮、配置等）
        else:
            cols_per_row = settings.materials_per_row
            should_pad = total > cols_per_row  # 超过一行时才填充空占位

            for i in range(0, total, cols_per_row):
                row = layout.row(align=True)
                for j in range(cols_per_row):
                    idx = i + j
                    if idx >= total:
                        if should_pad:
                            # 空占位 —— 用 template_icon(0) 复刻宽高比约束，确保宽度一致
                            col = row.column(align=True)
                            box = col.box()
                            # box.template_icon(icon_value=0, scale=settings.cell_height)
                            box.template_icon(icon_value=0, scale=0)
                            name_row = col.row(align=True)
                            name_row.label(text="", icon='NONE')
                        continue
                    mat = materials[idx]
                    checked = _is_checked(settings, mat.name)
                    col = row.column(align=True)
                    if checked:
                        col.alert = True  # 橙色高亮，比勾选更显眼

                    # -- 预览图 --
                    pname = _preview_name(mat.name)

                    box = col.box()

                    if pname in bpy.data.images:
                        img = bpy.data.images[pname]
                        icon_id = _get_image_icon_id(img)
                        if icon_id:
                            # 使用 template_icon 渲染大图标（纯展示，不可交互）
                            box.template_icon(icon_value=icon_id, scale=settings.cell_height)
                            # 统计按钮（控制伪用户）| 小手/收藏（最大拉伸）| 刷新
                            btn_row = box.row(align=True)
                            count = _count_material_users(mat)
                            cnt_row = btn_row.row(align=True)
                            cnt_row.scale_x = 0.152
                            op = cnt_row.operator(
                                "material_inspector.toggle_fake_user",
                                text=str(count).rjust(0),
                                emboss=mat.use_fake_user,
                                depress=mat.use_fake_user,
                            )
                            op.material_name = mat.name
                            split = btn_row.split(factor=0.96)
                            split.operator_context = 'INVOKE_DEFAULT'
                            hand_icon = 'SOLO_ON' if _is_favorite(settings, mat.name) else 'HAND'
                            op = split.operator(
                                "material_inspector.activate_material",
                                text="",
                                icon=hand_icon,
                                emboss=False,
                            )
                            op.material_name = mat.name
                            refresh_op = btn_row.operator(
                                "material_inspector.refresh_single",
                                text="",
                                icon='FILE_REFRESH',
                                emboss=False,
                            )
                            refresh_op.material_name = mat.name
                        else:
                            box.label(text="", icon='MATERIAL_DATA')
                            box.operator_context = 'INVOKE_DEFAULT'
                            op = box.operator(
                                "material_inspector.activate_material",
                                text="点击查看",
                                icon='MATERIAL_DATA',
                            )
                        op.material_name = mat.name
                    else:
                        box.operator_context = 'INVOKE_DEFAULT'
                        op = box.operator(
                            "material_inspector.activate_material",
                            text="[无预览]",
                            icon='MATERIAL_DATA',
                        )
                        op.material_name = mat.name

                    # -- 名称行（重命名按钮） --
                    name_row = col.row(align=True)
                    op = name_row.operator(
                        "material_inspector.rename_material",
                        text=f"[{mat.name}]",
                        icon='MATERIAL',
                    )
                    op.material_name = mat.name

        # ---------- 清理 ----------
        layout.separator()
        row = layout.row(align=True)
        row.operator("material_inspector.delete_selected_materials", text="删除选中", icon='TRASH')
        row.operator("material_inspector.clean_unused_materials", text="删未使用材质", icon='TRASH')
        row.operator("material_inspector.clean_unused_textures", text="删未使用纹理", icon='TRASH')
        row.operator("material_inspector.clean_empty_slots", text="空材质清理", icon='TRASH')

        # ---------- 配置 ----------
        layout.separator()
        row = layout.row(align=True)
        row.prop(settings, "materials_per_row", text="每行数量", slider=True)
        row.prop(settings, "cell_height", text="每行高度", slider=True)
        row.prop(settings, "preview_resolution", text="预览图分辨率", slider=True)
        row = layout.row(align=True)
        row.prop(settings, "preview_engine", text="渲染器")
        row.prop(settings, "preview_geometry", text="几何体")
        row.prop(settings, "sort_mode", text="排序")
        row = layout.row(align=True)
        row.prop(settings, "replace_mode", text="完全替换材质")
        row.prop(settings, "use_fake_user", text="资源保护（伪用户）模式")


# ============================================================
#  快捷键
# ============================================================

addon_keymaps = []


def register_keymap():
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if kc is None:
        return
    km = kc.keymaps.new(name="3D View", space_type="VIEW_3D")
    kmi = km.keymap_items.new(
        MI_OT_DeleteKey.bl_idname,
        type='DEL',
        value='PRESS',
    )
    addon_keymaps.append((km, kmi))


def unregister_keymap():
    for km, kmi in addon_keymaps:
        km.keymap_items.remove(kmi)
    addon_keymaps.clear()


# ============================================================
#  注册 / 注销
# ============================================================

CLASSES = (
    MaterialCheckItem,
    MaterialInspectorSettings,
    MI_OT_NewBSDF,
    MI_OT_DeleteSelectedMaterials,
    MI_OT_UpdatePreviews,
    MI_OT_AssignMaterial,
    MI_OT_AssignToVertices,
    MI_OT_RefreshSinglePreview,
    MI_OT_ActivateMaterial,
    MI_OT_ToggleCheck,
    MI_OT_SelectAll,
    MI_OT_DeselectAll,
    MI_OT_SelectToBefore,
    MI_OT_SelectToAfter,
    MI_OT_CopySelectedMaterials,
    MI_OT_UnlinkSelectedMaterials,
    MI_OT_ResetSelectedMaterials,
    MI_OT_RenameMaterial,
    MI_OT_CleanUnusedMaterials,
    MI_OT_CleanUnusedTextures,
    MI_OT_CleanEmptySlots,
    MI_OT_GeometryPreview,
    MI_OT_ToggleFakeUser,
    MI_OT_SelectModelMaterials,
    MI_OT_DeleteKey,
    MATERIALINSPECTOR_PT_Panel,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)

    bpy.types.Scene.material_inspector_settings = PointerProperty(
        type=MaterialInspectorSettings
    )

    register_keymap()

    # 注册 depsgraph 监听（材质删除 / 重命名检测）
    if _on_depsgraph_update not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph_update)

    # 注册项目加载监听（已有预览图的材质自动更新）
    if _on_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load_post)

    # 初始化材质名快照（安装/启用期间 bpy.data 可能受限）
    global _prev_material_names
    try:
        _prev_material_names = {m.name for m in bpy.data.materials if not m.name.startswith(".")}
    except AttributeError:
        _prev_material_names = set()


def unregister():
    # 注销 depsgraph 监听
    if _on_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph_update)

    # 注销项目加载监听
    if _on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load_post)
    unregister_keymap()

    del bpy.types.Scene.material_inspector_settings

    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
