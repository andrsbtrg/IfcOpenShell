# BlenderBIM Add-on - OpenBIM Blender Add-on
# Copyright (C) 2023 @Andrej730
#
# This file is part of BlenderBIM Add-on.
#
# BlenderBIM Add-on is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# BlenderBIM Add-on is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with BlenderBIM Add-on.  If not, see <http://www.gnu.org/licenses/>.


import bpy
from bpy.types import Operator
import bmesh

import ifcopenshell
from ifcopenshell.util.shape_builder import V
import blenderbim
import blenderbim.tool as tool
import blenderbim.core.geometry as core
from blenderbim.bim.helper import convert_property_group_from_si
from blenderbim.bim.ifc import IfcStore
from blenderbim.bim.module.model.door import bm_sort_out_geom
from blenderbim.bim.module.model.data import RailingData, refresh
from blenderbim.bim.module.model.decorator import ProfileDecorator

from mathutils import Vector, Matrix
from pprint import pprint
import json

# reference:
# https://ifc43-docs.standards.buildingsmart.org/IFC/RELEASE/IFC4x3/HTML/lexical/IfcRailing.htm
# https://ifc43-docs.standards.buildingsmart.org/IFC/RELEASE/IFC4x3/HTML/lexical/IfcRailingType.htm


def bm_split_edge_at_offset(edge, offset):
    v0, v1 = edge.verts

    offset = offset / 2
    edge_len = (v0.co - v1.co).xy.length

    split_output_0 = bmesh.utils.edge_split(edge, v0, offset / edge_len)
    split_output_1 = bmesh.utils.edge_split(edge, v1, offset / (edge_len - offset))
    new_geometry = bm_sort_out_geom(split_output_0 + split_output_1)
    return new_geometry


def update_railing_modifier_ifc_data(context):
    obj = context.active_object
    props = obj.BIMRailingProperties
    element = tool.Ifc.get_entity(obj)
    ifc_file = tool.Ifc.get()

    # type attributes
    element.PredefinedType = "USERDEFINED"
    # occurences attributes
    occurences = tool.Ifc.get_all_element_occurences(element)
    for occurence in occurences:
        occurence.ObjectType = props.railing_type

    # update pset
    pset_common = tool.Pset.get_element_pset(element, "Pset_RailingCommon")
    if not pset_common:
        pset_common = ifcopenshell.api.run("pset.add_pset", ifc_file, product=element, name="Pset_RailingCommon")

    ifcopenshell.api.run(
        "pset.edit_pset",
        ifc_file,
        pset=pset_common,
        properties={
            "Height": props.height,
        },
    )


def update_bbim_railing_pset(element, railing_data):
    pset = tool.Pset.get_element_pset(element, "BBIM_Railing")
    if not pset:
        pset = ifcopenshell.api.run("pset.add_pset", tool.Ifc.get(), product=element, name="BBIM_Railing")
    railing_data = json.dumps(railing_data, default=list)
    ifcopenshell.api.run("pset.edit_pset", tool.Ifc.get(), pset=pset, properties={"Data": railing_data})


def update_railing_modifier_bmesh(context):
    obj = context.object
    props = obj.BIMRailingProperties

    if not RailingData.is_loaded:
        RailingData.load()
    path_data = RailingData.data["parameters"]["data_dict"]["path_data"]

    si_conversion = ifcopenshell.util.unit.calculate_unit_scale(tool.Ifc.get())
    # need to make sure we support edit mode
    # since users will probably be in edit mode when they'll be changing railing path
    bm = tool.Blender.get_bmesh_for_mesh(obj.data, clean=True)

    # generating railing path
    bm.verts.index_update()
    bm.edges.index_update()
    new_verts = [bm.verts.new(Vector(v) * si_conversion) for v in path_data["verts"]]
    new_edges = [bm.edges.new((new_verts[e[0]], new_verts[e[1]])) for e in path_data["edges"]]
    bm.verts.index_update()
    bm.edges.index_update()

    if props.is_editing_path:
        tool.Blender.apply_bmesh(obj.data, bm)
        return

    # generating the entire railing
    height = props.height * si_conversion
    thickness = props.thickness * si_conversion
    spacing = props.spacing * si_conversion

    # spacing
    # split each edge in 3 segments by 0.5 * spacing by x-y plane
    main_edges = bm.edges[:]
    for main_edge in main_edges:
        bm_split_edge_at_offset(main_edge, spacing)

    # thickness
    # keep track of translated verts so we won't translate the same
    # vert twice
    edge_dissolving_verts = []
    for main_edge in main_edges:
        v0, v1 = main_edge.verts
        edge_dissolving_verts.extend([v0, v1])

        edge_dir = ((v1.co - v0.co) * V(1, 1, 0)).normalized()
        ortho_vector = edge_dir.cross(V(0, 0, 1))

        extruded_geom = bmesh.ops.extrude_edge_only(bm, edges=[main_edge])["geom"]
        extruded_verts = bm_sort_out_geom(extruded_geom)["verts"]
        bmesh.ops.translate(bm, vec=ortho_vector * (-thickness / 2), verts=extruded_verts)

        extruded_geom = bmesh.ops.extrude_edge_only(bm, edges=[main_edge])["geom"]
        extruded_verts = bm_sort_out_geom(extruded_geom)["verts"]
        bmesh.ops.translate(bm, vec=ortho_vector * (thickness / 2), verts=extruded_verts)

        # dissolve middle edge
        bmesh.ops.dissolve_edges(bm, edges=[main_edge])

    # height
    extruded_geom = bmesh.ops.extrude_face_region(bm, geom=bm.faces)["geom"]
    extruded_verts = bm_sort_out_geom(extruded_geom)["verts"]
    extrusion_vector = Vector((0, 0, 1)) * height
    bmesh.ops.translate(bm, vec=extrusion_vector, verts=extruded_verts)

    # dissolve middle edges
    edges_to_dissolve = []
    verts_to_dissolve = []
    for v in edge_dissolving_verts:
        for e in v.link_edges:
            other_vert = e.other_vert(v)
            if other_vert in extruded_verts:
                edges_to_dissolve.append(e)
                verts_to_dissolve.append(other_vert)
    bmesh.ops.dissolve_edges(bm, edges=edges_to_dissolve)
    bmesh.ops.dissolve_verts(bm, verts=verts_to_dissolve)

    # to remove unnecessary verts in 0 spacing case
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=0.0001)

    tool.Blender.apply_bmesh(obj.data, bm)


def get_path_data(obj):
    si_conversion = ifcopenshell.util.unit.calculate_unit_scale(tool.Ifc.get())
    if obj.mode == "EDIT":
        # otherwise mesh may not contain all changes
        # added in edit mode
        obj.update_from_editmode()

    mesh = obj.data
    path_data = dict()
    path_data["edges"] = [e.vertices[:] for e in mesh.edges]
    path_data["verts"] = [v.co / si_conversion for v in mesh.vertices]

    if not path_data["edges"] or not path_data["verts"]:
        return None
    return path_data


class BIM_OT_add_railing(bpy.types.Operator, tool.Ifc.Operator):
    bl_idname = "mesh.add_railing"
    bl_label = "Railing"
    bl_options = {"REGISTER", "UNDO"}

    def _execute(self, context):
        ifc_file = tool.Ifc.get()
        if not ifc_file:
            self.report({"ERROR"}, "You need to start IFC project first to create a railing.")
            return {"CANCELLED"}

        if context.object is not None:
            spawn_location = context.object.location.copy()
            context.object.select_set(False)
        else:
            spawn_location = bpy.context.scene.cursor.location.copy()

        mesh = bpy.data.meshes.new("IfcRailing")
        obj = bpy.data.objects.new("IfcRailing", mesh)
        obj.location = spawn_location
        body_context = ifcopenshell.util.representation.get_context(ifc_file, "Model", "Body", "MODEL_VIEW")
        blenderbim.core.root.assign_class(
            tool.Ifc,
            tool.Collector,
            tool.Root,
            obj=obj,
            ifc_class="IfcRailing",
            should_add_representation=True,
            context=body_context,
        )
        bpy.ops.object.select_all(action="DESELECT")
        bpy.context.view_layer.objects.active = None
        bpy.context.view_layer.objects.active = obj
        obj.select_set(True)
        bpy.ops.bim.add_railing()
        return {"FINISHED"}


# UI operators
class AddRailing(bpy.types.Operator, tool.Ifc.Operator):
    bl_idname = "bim.add_railing"
    bl_label = "Add Railing"
    bl_options = {"REGISTER"}

    def _execute(self, context):
        obj = context.active_object
        element = tool.Ifc.get_entity(obj)
        props = obj.BIMRailingProperties
        si_conversion = ifcopenshell.util.unit.calculate_unit_scale(tool.Ifc.get())

        if element.is_a() not in ("IfcRailing", "IfcRailingType"):
            self.report({"ERROR"}, "Object has to be IfcRailing/IfcRailingType type to add a railing.")
            return {"CANCELLED"}

        # need to make sure all default props will have correct units
        if not props.railing_added_previously:
            skip_props = ("is_editing", "railing_type", "railing_added_previously")
            convert_property_group_from_si(props, skip_props=skip_props)

        railing_data = props.get_general_kwargs()
        path_data = get_path_data(obj)
        if not path_data:
            path_data = {
                "edges": [[0, 1], [1, 2]],
                "verts": [
                    Vector([-1.0, 0.0, 0.0]) / si_conversion,
                    Vector([0.0, 0.0, 0.0]) / si_conversion,
                    Vector([1.0, 0.0, 0.0]) / si_conversion,
                ],
            }
        railing_data["path_data"] = path_data

        update_bbim_railing_pset(element, railing_data)
        refresh()
        update_railing_modifier_ifc_data(context)
        update_railing_modifier_bmesh(context)
        return {"FINISHED"}


class EnableEditingRailing(bpy.types.Operator, tool.Ifc.Operator):
    bl_idname = "bim.enable_editing_railing"
    bl_label = "Enable Editing Railing"
    bl_options = {"REGISTER"}

    def _execute(self, context):
        obj = context.active_object
        props = obj.BIMRailingProperties
        element = tool.Ifc.get_entity(obj)
        data = json.loads(ifcopenshell.util.element.get_pset(element, "BBIM_Railing", "Data"))
        data["path_data"] = json.dumps(data["path_data"])

        # required since we could load pset from .ifc and BIMRailingProperties won't be set
        for prop_name in data:
            setattr(props, prop_name, data[prop_name])

        # need to make sure all props that weren't used before
        # will have correct units
        skip_props = ("is_editing", "railing_type", "railing_added_previously")
        skip_props += tuple(data.keys())
        convert_property_group_from_si(props, skip_props=skip_props)

        props.is_editing = 1
        return {"FINISHED"}


class CancelEditingRailing(bpy.types.Operator, tool.Ifc.Operator):
    bl_idname = "bim.cancel_editing_railing"
    bl_label = "Cancel editing Railing"
    bl_options = {"REGISTER"}

    def _execute(self, context):
        obj = context.active_object
        element = tool.Ifc.get_entity(obj)
        data = json.loads(ifcopenshell.util.element.get_pset(element, "BBIM_Railing", "Data"))
        props = obj.BIMRailingProperties
        # restore previous settings since editing was canceled
        for prop_name in data:
            setattr(props, prop_name, data[prop_name])

        body = ifcopenshell.util.representation.get_representation(element, "Model", "Body", "MODEL_VIEW")
        blenderbim.core.geometry.switch_representation(
            tool.Ifc,
            tool.Geometry,
            obj=obj,
            representation=body,
            should_reload=True,
            is_global=True,
            should_sync_changes_first=False,
        )

        props.is_editing = -1
        return {"FINISHED"}


class FinishEditingRailing(bpy.types.Operator, tool.Ifc.Operator):
    bl_idname = "bim.finish_editing_railing"
    bl_label = "Finish editing railing"
    bl_options = {"REGISTER"}

    def _execute(self, context):
        obj = context.active_object
        element = tool.Ifc.get_entity(obj)
        props = obj.BIMRailingProperties

        if not RailingData.is_loaded:
            RailingData.load()
        path_data = RailingData.data["parameters"]["data_dict"]["path_data"]

        railing_data = props.get_general_kwargs()
        railing_data["path_data"] = path_data
        props.is_editing = -1

        update_bbim_railing_pset(element, railing_data)
        update_railing_modifier_ifc_data(context)
        return {"FINISHED"}


class EnableEditingRailingPath(bpy.types.Operator, tool.Ifc.Operator):
    bl_idname = "bim.enable_editing_railing_path"
    bl_label = "Enable Editing Railing Path"
    bl_options = {"REGISTER"}

    def _execute(self, context):
        obj = context.active_object
        props = obj.BIMRailingProperties

        props.is_editing_path = True
        update_railing_modifier_bmesh(context)

        if bpy.context.object.mode != "EDIT":
            bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.wm.tool_set_by_id(tool.Blender.get_viewport_context(), name="bim.cad_tool")
        ProfileDecorator.install(context)
        return {"FINISHED"}


class CancelEditingRailingPath(bpy.types.Operator, tool.Ifc.Operator):
    bl_idname = "bim.cancel_editing_railing_path"
    bl_label = "Cancel Editing Railing Path"
    bl_options = {"REGISTER"}

    def _execute(self, context):
        obj = context.active_object
        props = obj.BIMRailingProperties

        ProfileDecorator.uninstall()
        props.is_editing_path = False

        update_railing_modifier_bmesh(context)
        if bpy.context.object.mode == "EDIT":
            bpy.ops.object.mode_set(mode="OBJECT")
        return {"FINISHED"}


class FinishEditingRailingPath(bpy.types.Operator, tool.Ifc.Operator):
    bl_idname = "bim.finish_editing_railing_path"
    bl_label = "Finish Editing Railing Path"
    bl_options = {"REGISTER"}

    def _execute(self, context):
        obj = context.active_object
        element = tool.Ifc.get_entity(obj)
        props = obj.BIMRailingProperties

        railing_data = props.get_general_kwargs()
        path_data = get_path_data(obj)
        railing_data["path_data"] = path_data
        ProfileDecorator.uninstall()
        props.is_editing_path = False

        update_bbim_railing_pset(element, railing_data)
        refresh()  # RailingData has to be updated before run update_railing_modifier_bmesh
        update_railing_modifier_bmesh(context)
        if bpy.context.object.mode == "EDIT":
            bpy.ops.object.mode_set(mode="OBJECT")
        return {"FINISHED"}


class RemoveRailing(bpy.types.Operator, tool.Ifc.Operator):
    bl_idname = "bim.remove_railing"
    bl_label = "Remove Railing"
    bl_options = {"REGISTER"}

    def _execute(self, context):
        obj = context.active_object
        props = obj.BIMRailingProperties
        element = tool.Ifc.get_entity(obj)
        obj.BIMRailingProperties.is_editing = -1

        pset = tool.Pset.get_element_pset(element, "BBIM_Railing")
        ifcopenshell.api.run("pset.remove_pset", tool.Ifc.get(), pset=pset)
        props.railing_added_previously = True
        return {"FINISHED"}


def add_object_button(self, context):
    self.layout.operator(BIM_OT_add_railing.bl_idname, icon="PLUGIN")
