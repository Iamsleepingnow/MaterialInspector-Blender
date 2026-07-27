"""
材质查看器 (Material Inspector) — Blender Addon 入口

安装方式：
  编辑 > 偏好设置 > 插件 > 安装 > 选择 material_inspector 文件夹的上级目录 zip
  或直接将 material_inspector 文件夹放入 Blender 的 addons 目录。

卸载方式：
  插件面板中取消勾选即可，会同步注销快捷键和监听器。
"""
import bpy

from . import MaterialInspector

bl_info = {
    "name": "IMS材质查看器",
    "description": (
        "自制材质管理面板"
    ),
    "author": "Iamsleepingnow",
    "version": (1, 0, 0),
    "blender": (3, 0, 0),
    "location": "3D 视图 > 侧边栏 (N) > 材质查看器",
    "category": "Material",
    "support": "COMMUNITY",
}


def register():
    MaterialInspector.register()


def unregister():
    MaterialInspector.unregister()


if __name__ == "__main__":
    register()
