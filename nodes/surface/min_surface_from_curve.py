
import numpy as np
from math import pi

import bpy
from bpy.props import FloatProperty, EnumProperty, BoolProperty, IntProperty
from mathutils import Matrix

import sverchok
from sverchok.node_tree import SverchCustomTreeNode, throttled
from sverchok.data_structure import updateNode, zip_long_repeat, ensure_nesting_level, get_data_nesting_level
from sverchok.utils.logging import info, exception
from sverchok.utils.curve import SvExCurve, SvExCurveOnSurface, SvExCircle

from sverchok_extra.data.surface import SvExRbfSurface
from sverchok_extra.utils import rbf_functions
from sverchok_extra.dependencies import scipy

if scipy is not None:
    from scipy.interpolate import Rbf

    class SvExMinSurfaceFromCurveNode(bpy.types.Node, SverchCustomTreeNode):
        """
        Triggers: Minimal Surface from Curve
        Tooltip: Generate Minimal Surface from circle-like curve
        """
        bl_idname = 'SvExMinSurfaceFromCurveNode'
        bl_label = 'Minimal Surface from Curve'
        bl_icon = 'OUTLINER_OB_EMPTY'
        sv_icon = 'SV_EX_MINSURFACE'

        function : EnumProperty(
                name = "Function",
                items = rbf_functions,
                default = 'multiquadric',
                update = updateNode)

        epsilon : FloatProperty(
                name = "Epsilon",
                default = 1.0,
                min = 0.0,
                update = updateNode)
        
        smooth : FloatProperty(
                name = "Smooth",
                default = 0.0,
                min = 0.0,
                update = updateNode)

        samples_t : IntProperty(
                name = "Samples",
                default = 50,
                min = 3,
                update = updateNode)

        def sv_init(self, context):
            self.inputs.new('SvExCurveSocket', "Curve").display_shape = 'DIAMOND'
            self.inputs.new('SvStringsSocket', "Samples").prop_name = 'samples_t'
            self.inputs.new('SvStringsSocket', "Epsilon").prop_name = 'epsilon'
            self.inputs.new('SvStringsSocket', "Smooth").prop_name = 'smooth'
            self.outputs.new('SvExSurfaceSocket', "Surface").display_shape = 'DIAMOND'
            self.outputs.new('SvExCurveSocket', "TrimCurve").display_shape = 'DIAMOND'
            self.outputs.new('SvExCurveSocket', "Curve").display_shape = 'DIAMOND'

        def draw_buttons(self, context, layout):
            layout.prop(self, "function")

        def make_surface(self, curve, epsilon, smooth, samples):
            t_min, t_max = curve.get_u_bounds()
            curve_ts = np.linspace(t_min, t_max, num=samples)
            curve_points = curve.evaluate_array(curve_ts)
            dvs = curve_points[1:] - curve_points[:-1]
            segment_lengths = np.linalg.norm(dvs, axis=1)
            last_segment_length = np.linalg.norm(curve_points[0] - curve_points[-1])
            if last_segment_length < 0.001:
                # curve is closed: remove the last segment to make it non-closed
                segment_lengths = segment_lengths[:-1]
                curve_points = curve_points[:-1]
                last_segment_length = np.linalg.norm(curve_points[0] - curve_points[-1])
            # T=0 will correspond to the center of gap between first and last point
            dt = min(last_segment_length / 2.0, segment_lengths.min())
            cum_segment_lengths = np.insert(np.cumsum(segment_lengths), 0, 0)
            total_length = cum_segment_lengths[-1] + last_segment_length
            ts = cum_segment_lengths + dt
            ts = 2*pi * ts / total_length

            us = np.cos(ts)
            vs = np.sin(ts)

            rbf = Rbf(us, vs, curve_points,
                    function = self.function,
                    epsilon = epsilon, smooth = smooth, mode = 'N-D')
            surface = SvExRbfSurface(rbf, 'UV', 'Z', Matrix())
            surface.u_bounds = (-1.0, 1.0)
            surface.v_bounds = (-1.0, 1.0)
            return surface

        def process(self):
            if not any(socket.is_linked for socket in self.outputs):
                return
            
            curve_s = self.inputs['Curve'].sv_get()
            epsilon_s = self.inputs['Epsilon'].sv_get()
            smooth_s = self.inputs['Smooth'].sv_get()
            samples_s = self.inputs['Samples'].sv_get()

            if isinstance(curve_s[0], SvExCurve):
                curve_s = [curve_s]
            epsilon_s = ensure_nesting_level(epsilon_s, 2)
            smooth_s = ensure_nesting_level(smooth_s, 2)
            samples_s = ensure_nesting_level(samples_s, 2)

            surface_out = []
            circle_out = []
            curve_out = []

            inputs = zip_long_repeat(curve_s, epsilon_s, smooth_s, samples_s)
            for curves, epsilons, smooths, samples_i in inputs:
                for curve, epsilon, smooth, samples in zip_long_repeat(curves, epsilons, smooths, samples_i):
                    new_surface = self.make_surface(curve, epsilon, smooth, samples)
                    circle = SvExCircle(Matrix(), 1.0)
                    new_curve = SvExCurveOnSurface(circle, new_surface, axis=2)
                    surface_out.append(new_surface)
                    curve_out.append(new_curve)
                    circle_out.append(circle)

            self.outputs['Surface'].sv_set(surface_out)
            self.outputs['TrimCurve'].sv_set(circle_out)
            self.outputs['Curve'].sv_set(curve_out)

def register():
    if scipy is not None:
        bpy.utils.register_class(SvExMinSurfaceFromCurveNode)

def unregister():
    if scipy is not None:
        bpy.utils.unregister_class(SvExMinSurfaceFromCurveNode)
