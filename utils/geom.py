
import numpy as np
from math import sqrt, atanh, sinh, cosh

from mathutils import kdtree
from mathutils.bvhtree import BVHTree

from sverchok.utils.curve import SvCurve, SvIsoUvCurve
from sverchok.utils.logging import debug, info
from sverchok.utils.geom import PlaneEquation, LineEquation

from sverchok_extra.dependencies import scipy
from sverchok_extra.dependencies import skimage
from sverchok_extra.utils.marching_squares import make_contours

if scipy is not None:
    from scipy.optimize import root_scalar, root

if skimage is not None:
    from skimage import measure

class CurveProjectionResult(object):
    def __init__(self, us, points, source):
        self.us = us
        self.points = points
        self.source = source

        self.kdt = kdt = kdtree.KDTree(len(points))
        for i, v in enumerate(points):
            kdt.insert(v, i)
        kdt.balance()

        nearest, i, distance = kdt.find(source)
        self.nearest = np.array(nearest)
        self.nearest_idx = i
        self.nearest_distance = distance
        self.nearest_u = us[i]

def ortho_project_curve(src_point, curve, init_samples=10):
    def goal(t):
        point_on_curve = curve.evaluate(t)
        dv = src_point - point_on_curve
        tangent = curve.tangent(t)
        return dv.dot(tangent)

    u_min, u_max = curve.get_u_bounds()
    u_samples = np.linspace(u_min, u_max, num=init_samples)

    u_ranges = []
    prev_value = goal(u_min)
    prev_u = u_min
    for u in u_samples[1:]:
        value = goal(u)
        if value * prev_value < 0:
            u_ranges.append((prev_u, u))
        prev_u = u
        prev_value = value

    points = []
    us = []
    for u1, u2 in u_ranges:
        u0 = (u1 + u2) / 2.0
        result = root_scalar(goal, method='ridder',
                        bracket = (u1, u2),
                        x0 = u0)
        u = result.root
        us.append(u)
        point = curve.evaluate(u)
        points.append(point)

    if not us:
        raise Exception("Can't calculate the projection of {} onto {}".format(src_point, curve))
    result = CurveProjectionResult(us, points, src_point)
    return result

def ortho_project_surface(src_point, surface, init_samples=10, maxiter=30, tolerance=1e-4):
    u_min, u_max = surface.get_u_min(), surface.get_u_max()
    v_min, v_max = surface.get_v_min(), surface.get_v_max()

    u0 = (u_min + u_max) / 2.0
    v0 = (v_min + v_max) / 2.0

    fixed_axis = 'U'
    fixed_axis_value = u0
    prev_fixed_axis_value = v0
    prev_point = surface.evaluate(u0, v0)

    i = 0
    while True:
        if i > maxiter:
            raise Exception("No convergence")
        curve = SvIsoUvCurve(surface, fixed_axis, fixed_axis_value)
        projection = ortho_project_curve(src_point, curve, init_samples)
        point = projection.nearest
        dv = point - prev_point
        fixed_axis_value = projection.nearest_u
        if np.linalg.norm(dv) < tolerance:
            break
        if fixed_axis == 'U':
            fixed_axis = 'V'
        else:
            fixed_axis = 'U'
        prev_fixed_axis_value = fixed_axis_value
        prev_point = point
        i += 1

    if fixed_axis == 'U':
        u, v = prev_fixed_axis_value, fixed_axis_value
    else:
        u, v = fixed_axis_value, prev_fixed_axis_value

    return u, v, point

class RaycastResult(object):
    def __init__(self):
        self.init_us = None
        self.init_vs = None
        self.init_ts = None
        self.init_points = None
        self.points = []
        self.uvs = []
        self.us = []
        self.vs = []

class RaycastInitGuess(object):
    def __init__(self):
        self.us = []
        self.vs = []
        self.ts = []
        self.nearest = []
        self.all_good = True

SKIP = 'skip'
FAIL = 'fail'
RETURN_NONE = 'none'

class SurfaceRaycaster(object):
    """
    Usage:
        
        raycaster = SurfaceRaycaster(surface)
        raycaster.init_bvh(samples)
        result = raycaster.raycast(src_points, directions, ...)
    """
    def __init__(self, surface):
        self.surface = surface
        self.bvh = None
        self.samples = None
        self.center_us = None
        self.center_vs = None

    def init_bvh(self, samples):
        self.samples = samples

        self.u_min = u_min = self.surface.get_u_min()
        self.u_max = u_max = self.surface.get_u_max()
        self.v_min = v_min = self.surface.get_v_min()
        self.v_max = v_max = self.surface.get_v_max()

        us = np.linspace(u_min, u_max, num=samples)
        vs = np.linspace(v_min, v_max, num=samples)
        us, vs = np.meshgrid(us, vs)
        self.us = us.flatten()
        self.vs = vs.flatten()

        points = self.surface.evaluate_array(self.us, self.vs).tolist()
        self.center_us, self.center_vs, faces = self._make_faces()

        self.bvh = BVHTree.FromPolygons(points, faces)

    def _make_faces(self):
        samples = self.samples
        uh2 = (self.u_max - self.u_min) / (2 * samples)
        vh2 = (self.v_max - self.v_min) / (2 * samples)
        faces = []
        center_us = []
        center_vs = []
        for row in range(samples - 1):
            for col in range(samples - 1):
                i = row * samples + col
                face = (i, i+samples, i+samples+1, i+1)
                u = self.us[i] + uh2
                v = self.vs[i] + vh2
                center_us.append(u)
                center_vs.append(v)
                faces.append(face)
        return center_us, center_vs, faces

    def _init_guess(self, src_points, directions):
        if self.bvh is None:
            raise Exception("You have to call init_bvh() method first!")

        guess = RaycastInitGuess()
        for src_point, direction in zip(src_points, directions):
            nearest, normal, index, distance = self.bvh.ray_cast(src_point, direction)
            if nearest is None:
                guess.us.append(None)
                guess.vs.append(None)
                guess.ts.append(None)
                guess.nearest.append(None)
                guess.all_good = False
            else:
                guess.us.append(self.center_us[index])
                guess.vs.append(self.center_vs[index])
                guess.ts.append(distance)
                guess.nearest.append(tuple(nearest))

        return guess

    def _goal(self, src_point, direction):
        def function(p):
            on_surface = self.surface.evaluate(p[0], p[1])
            on_line = src_point + direction * p[2]
            return (on_surface - on_line).flatten()
        return function

    def raycast(self, src_points, directions, precise=True, calc_points=True, method='hybr', on_init_fail = SKIP):
        result = RaycastResult()
        guess = self._init_guess(src_points, directions)
        result.init_us, result.init_vs = guess.us, guess.vs
        result.init_ts = guess.ts
        result.init_points = guess.nearest
        for point, direction, init_u, init_v, init_t, init_point in zip(src_points, directions, result.init_us, result.init_vs, result.init_ts, result.init_points):
            if init_u is None:
                if on_init_fail == SKIP:
                    continue
                elif on_init_fail == FAIL:
                    raise Exception("Can't find initial guess of the projection for {}".format(point))
                elif on_init_fail == RETURN_NONE:
                    return None
                else:
                    raise Exception("Invalid on_init_fail value")

            if precise:
                direction = np.array(direction)
                direction = direction / np.linalg.norm(direction)
                projection = root(self._goal(np.array(point), direction),
                            x0 = np.array([init_u, init_v, init_t]),
                            method = method)
                if not projection.success:
                    raise Exception("Can't find the projection for {}: {}".format(point, projection.message))
                u0, v0, t0 = projection.x
            else:
                u0, v0 = init_u, init_v
                result.points.append(init_point)

            result.uvs.append((u0, v0, 0))
            result.us.append(u0)
            result.vs.append(v0)

        if precise and calc_points:
            result.points = self.surface.evaluate_array(np.array(result.us), np.array(result.vs)).tolist()

        return result

def raycast_surface(surface, src_points, directions, samples=50, precise=True, calc_points=True, method='hybr', on_init_fail = SKIP):
    """Shortcut for SurfaceRaycaster"""
    raycaster = SurfaceRaycaster(surface)
    raycaster.init_bvh(samples)
    return raycaster.raycast(src_points, directions, precise=precise, calc_points=calc_points, method=method, on_init_fail=on_init_fail)

def intersect_curve_surface(curve, surface, raycast_samples=10, ortho_samples=10, tolerance=1e-3, maxiter=50, raycast_method='hybr'):
    u_min, u_max = curve.get_u_bounds()

    raycaster = SurfaceRaycaster(surface)
    raycaster.init_bvh(raycast_samples)

    def do_raycast(point, tangent, sign=1):
        good_sign = sign
        raycast = raycaster.raycast([point], [sign*tangent],
                    method = raycast_method,
                    on_init_fail = RETURN_NONE)
        if raycast is None:
            good_sign = -sign
            raycast = raycaster.raycast([point], [-sign*tangent],
                        method = raycast_method,
                        on_init_fail = RETURN_NONE)
        return good_sign, raycast

    tangent = curve.tangent(u_min)
    point = curve.evaluate(u_min)

    sign = 1
    u0 = u_min
    raycast = raycaster.raycast([point], [sign*tangent],
                method = raycast_method,
                on_init_fail = RETURN_NONE)
    if raycast is None:
        sign = -1
        u0 = u_max
        tangent = curve.tangent(u_max)
        point = curve.evaluate(u_max)
        raycast = raycaster.raycast([point], [sign*tangent],
                    method = raycast_method,
                    on_init_fail = RETURN_NONE)

    debug("Init sign = %s, u = %s", sign, u0)
    if raycast is None:
        raise Exception("Can't find initial raycast point for intersection")

    i = 0
    prev_prev_point = None
    prev_point = raycast.points[0]
    while True:
        i += 1
        if i > maxiter:
            raise Exception("Maximum number of iterations is exceeded; last step {} - {} = {}".format(prev_prev_point, point, step))

        ortho = ortho_project_curve(prev_point, curve, init_samples = ortho_samples)
        point = ortho.nearest
        step = np.linalg.norm(point - prev_point)
        if step < tolerance:
            debug("After ortho: Point {}, prev {}, iter {}".format(point, prev_point, i))
            break

        prev_point = point
        tangent = curve.tangent(ortho.nearest_u)
        sign, raycast = do_raycast(point, tangent, sign)
        if raycast is None:
            raise Exception("Can't do a raycast with point {}, direction {} onto surface {}".format(point, tangent, surface))
        point = raycast.points[0]
        step = np.linalg.norm(point - prev_point)
        if step < tolerance:
            debug("After raycast: Point {}, prev {}, iter {}".format(point, prev_point, i))
            break
        prev_prev_point = prev_point
        prev_point = point

    return point

def intersect_curve_plane(curve, plane, init_samples=10, ortho_samples=10, tolerance=1e-3, maxiter=50):
    u_min, u_max = curve.get_u_bounds()
    u_range = np.linspace(u_min, u_max, num=init_samples)
    init_points = curve.evaluate_array(u_range)
    init_signs = plane.side_of_points(init_points)
    good_ranges = []
    for u1, u2, sign1, sign2 in zip(u_range, u_range[1:], init_signs, init_signs[1:]):
        if sign1 * sign2 < 0:
            good_ranges.append((u1, u2))
    if not good_ranges:
        return []

    solutions = []
    for u1, u2 in good_ranges:
        u0 = u1
        tangent = curve.tangent(u0)
        tangent /= np.linalg.norm(tangent)
        point = curve.evaluate(u0)
        line = LineEquation.from_direction_and_point(tangent, point)

        p = plane.intersect_with_line(line)
        if p is None:
            u0 = u2
            tangent = curve.tangent(u0)
            tangent /= np.linalg.norm(tangent)
            point = curve.evaluate(u0)
            line = LineEquation.from_direction_and_point(tangent, point)
            p = plane.intersect_with_line(line)
            if p is None:
                raise Exception("Can't find initial point for intersection")

        i = 0
        prev_prev_point = None
        prev_point = np.array(p)
        while True:
            i += 1
            if i > maxiter:
                raise Exception("Maximum number of iterations is exceeded; last step {} - {} = {}".format(prev_prev_point, point, step))

            ortho = ortho_project_curve(prev_point, curve, init_samples = ortho_samples)
            point = ortho.nearest
            step = np.linalg.norm(point - prev_point)
            if step < tolerance:
                debug("After ortho: Point {}, prev {}, iter {}".format(point, prev_point, i))
                break

            prev_point = point
            tangent = curve.tangent(ortho.nearest_u)
            tangent /= np.linalg.norm(tangent)
            point = curve.evaluate(ortho.nearest_u)
            line = LineEquation.from_direction_and_point(tangent, point)
            point = plane.intersect_with_line(line)
            if point is None:
                raise Exception("Can't intersect a line {} with a plane {}".format(line, point))
            point = np.array(point)
            step = np.linalg.norm(point - prev_point)
            if step < tolerance:
                debug("After raycast: Point {}, prev {}, iter {}".format(point, prev_point, i))
                break

            prev_prev_point = prev_point
            prev_point = point

        solutions.append(point)

    return solutions

def intersect_surface_plane_msquares(surface, plane, need_points = True, samples_u=50, samples_v=50):
    u_min, u_max = surface.get_u_min(), surface.get_u_max()
    v_min, v_max = surface.get_v_min(), surface.get_v_max()
    u_range = np.linspace(u_min, u_max, num=samples_u)
    v_range = np.linspace(v_min, v_max, num=samples_v)
    us, vs = np.meshgrid(u_range, v_range, indexing='ij')
    us, vs = us.flatten(), vs.flatten()

    surface_points = surface.evaluate_array(us, vs)
    normal = np.array(plane.normal)
    p2 = np.apply_along_axis(lambda p : normal.dot(p), 1, surface_points)
    data = p2 + plane.d
    data = data.reshape((samples_u, samples_v))

    contours = measure.find_contours(data, level=0.0)

    u_size = (u_max - u_min) / samples_u
    v_size = (v_max - v_min) / samples_v

    uv_points, _, _ = make_contours(samples_u, samples_v,
                    u_min, u_size, v_min, v_size,
                    0,
                    contours,
                    make_faces = False,
                    connect_bounds = False)

    if need_points:
        points = []
        for uv_i in uv_points:
            us_i = [p[0] for p in uv_i]
            vs_i = [p[1] for p in uv_i]
            ps = surface.evaluate_array(np.array(us_i), np.array(vs_i)).tolist()
            points.append(ps)
    else:
        points = []

    return uv_points, points

def intersect_surface_plane_uv(surface, plane, samples_u = 50, samples_v = 50, init_samples=10, ortho_samples=10, tolerance=1e-3, maxiter=50):
    # Unsorted!
    u_min, u_max = surface.get_u_min(), surface.get_u_max()
    v_min, v_max = surface.get_v_min(), surface.get_v_max()
    u_range = np.linspace(u_min, u_max, num=samples_u)
    v_range = np.linspace(v_min, v_max, num=samples_v)

    points = []
    for u in u_range:
        curve = SvIsoUvCurve(surface, 'U', u)
        ps = intersect_curve_plane(curve, plane,
                    init_samples=init_samples, ortho_samples=ortho_samples,
                    tolerance=tolerance, maxiter=maxiter)
        points.extend(ps)
    for v in v_range:
        curve = SvIsoUvCurve(surface, 'V', v)
        ps = intersect_curve_plane(curve, plane,
                    init_samples=init_samples, ortho_samples=ortho_samples,
                    tolerance=tolerance, maxiter=maxiter)
        points.extend(ps)
    return [tuple(p) for p in points]

