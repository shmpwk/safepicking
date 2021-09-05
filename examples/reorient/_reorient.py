import itertools
import time

import cv2
from loguru import logger
import numpy as np
import pybullet_planning as pp

import mercury

import _utils


def get_query_ocs(env):
    lock_renderer = pp.LockRenderer()
    world_saver = pp.WorldSaver()

    pp.set_pose(env.fg_object_id, env.PLACE_POSE)

    T_camera_to_world = mercury.geometry.look_at(
        env.PRE_PLACE_POSE[0], env.PLACE_POSE[0]
    )
    fovy = np.deg2rad(60)
    height = 240
    width = 240
    if env.debug:
        mercury.pybullet.draw_camera(
            fovy,
            height,
            width,
            pose=mercury.geometry.pose_from_matrix(T_camera_to_world),
        )
    rgb, depth, segm = mercury.pybullet.get_camera_image(
        T_camera_to_world, fovy, height, width
    )
    # if pp.has_gui():
    #     import imgviz
    #
    #     imgviz.io.cv_imshow(
    #         np.hstack((rgb, imgviz.depth2rgb(depth))), "get_query_ocs"
    #     )
    #     imgviz.io.cv_waitkey(100)
    K = mercury.geometry.opengl_intrinsic_matrix(fovy, height, width)
    pcd_in_camera = mercury.geometry.pointcloud_from_depth(
        depth, fx=K[0, 0], fy=K[1, 1], cx=K[0, 2], cy=K[1, 2]
    )
    pcd_in_world = mercury.geometry.transform_points(
        pcd_in_camera, T_camera_to_world
    )
    normals_in_world = mercury.geometry.normals_from_pointcloud(pcd_in_world)
    normals_in_world *= -1  # flip normals

    mask = segm == env.fg_object_id

    normals_on_obj = normals_in_world.copy()
    normals_on_obj[~mask] = 0
    laplacian = cv2.Laplacian(normals_on_obj, cv2.CV_64FC3)
    magnitude = np.linalg.norm(laplacian, axis=2)
    edge_mask = magnitude > 0.5
    edge_mask = (
        cv2.dilate(np.uint8(edge_mask) * 255, kernel=np.ones((12, 12))) == 255
    )
    mask = mask & ~edge_mask

    world_to_obj = pp.invert(pp.get_pose(env.fg_object_id))
    pcd_in_obj = mercury.geometry.transform_points(
        pcd_in_world[mask],
        mercury.geometry.transformation_matrix(*world_to_obj),
    )
    normals_in_obj = (
        mercury.geometry.transform_points(
            pcd_in_world[mask] + normals_in_world[mask],
            mercury.geometry.transformation_matrix(*world_to_obj),
        )
        - pcd_in_obj
    )

    world_saver.restore()
    lock_renderer.restore()
    return pcd_in_obj, normals_in_obj


def get_grasp_poses(env):
    segm = env.obs["segm"]
    depth = env.obs["depth"]
    K = env.obs["K"]
    mask = (segm == env.obs["target_instance_id"]) & (~np.isnan(depth))
    pcd_in_camera = mercury.geometry.pointcloud_from_depth(
        depth, fx=K[0, 0], fy=K[1, 1], cx=K[0, 2], cy=K[1, 2]
    )
    normals_in_camera = mercury.geometry.normals_from_pointcloud(pcd_in_camera)

    normals_on_obj = normals_in_camera.copy()
    normals_on_obj[~mask] = 0
    laplacian = cv2.Laplacian(normals_on_obj, cv2.CV_64FC3)
    magnitude = np.linalg.norm(laplacian, axis=2)
    edge_mask = magnitude > 0.5
    edge_mask = (
        cv2.dilate(np.uint8(edge_mask) * 255, kernel=np.ones((12, 12))) == 255
    )
    mask = mask & ~edge_mask

    pcd_in_camera = pcd_in_camera[mask]
    normals_in_camera = normals_in_camera[mask]

    camera_to_world = np.hsplit(env.obs["camera_to_world"], [3])
    pcd_in_world = mercury.geometry.transform_points(
        pcd_in_camera,
        mercury.geometry.transformation_matrix(*camera_to_world),
    )
    normals_in_world = (
        mercury.geometry.transform_points(
            pcd_in_camera + normals_in_camera,
            mercury.geometry.transformation_matrix(*camera_to_world),
        )
        - pcd_in_world
    )

    quaternion_in_world = mercury.geometry.quaternion_from_vec2vec(
        [0, 0, 1], normals_in_world
    )

    p = np.random.permutation(pcd_in_world.shape[0])

    obstacles = env.bg_objects + env.object_ids
    obstacles.remove(env.fg_object_id)
    for pose in zip(pcd_in_world[p], quaternion_in_world[p]):
        j = env.ri.solve_ik(pose, rotation_axis="z")
        if j is not None and env.ri.validatej(j, obstacles=obstacles):
            yield np.hstack(pose)


def plan_reorient(env, grasp_pose, reorient_pose):
    # lock_renderer = pp.LockRenderer()
    world_saver = pp.WorldSaver()

    result = {}

    obj_af = mercury.pybullet.duplicate(
        env.fg_object_id,
        collision=False,
        rgba_color=(1, 1, 1, 0.5),
        position=reorient_pose[:3],
        quaternion=reorient_pose[3:],
        mass=0,
    )

    def before_return():
        env.ri.attachments = []
        world_saver.restore()
        pp.remove_body(obj_af)
        # lock_renderer.restore()

    result["j_init"] = env.ri.getj()

    bg_object_ids = env.bg_objects + env.object_ids
    bg_object_ids.remove(env.fg_object_id)

    ee_af_to_world = np.hsplit(grasp_pose, [3])
    obj_af_to_world = np.hsplit(reorient_pose, [3])

    # find self-collision-free j_grasp
    for dg in np.linspace(-np.pi, np.pi, num=8, endpoint=False):
        env.ri.attachments = []

        c = mercury.geometry.Coordinate(*ee_af_to_world)
        c.rotate([0, 0, dg])
        j = env.ri.solve_ik(c.pose, n_init=3)
        if j is None or not env.ri.validatej(
            j,
            obstacles=bg_object_ids,
            min_distances=mercury.utils.StaticDict(-0.01),
        ):
            continue

        result["j_grasp"] = j

        obj_to_world = pp.get_pose(env.fg_object_id)
        obj_to_ee = pp.multiply(pp.invert(c.pose), obj_to_world)
        attachments = [
            pp.Attachment(env.ri.robot, env.ri.ee, obj_to_ee, env.fg_object_id)
        ]
        env.ri.attachments = attachments

        with env.ri.enabling_attachments():
            j = env.ri.solve_ik(
                obj_af_to_world,
                move_target=env.ri.robot_model.attachment_link0,
                thre=0.01,
                rthre=np.deg2rad(10),
                n_init=5,
            )
        env.ri.attachments = []  # skip env.ri.attachments from validatej
        if j is not None and env.ri.validatej(
            j,
            obstacles=bg_object_ids,
            min_distances=mercury.utils.StaticDict(-0.01),
        ):
            result["j_place"] = j
            break
    else:
        logger.warning("j_grasp and j_place are not found")
        before_return()
        return result

    env.ri.attachments = []

    env.ri.setj(result["j_grasp"])
    ee_af_to_world = env.ri.get_pose("tipLink")

    obj_to_world = pp.get_pose(env.fg_object_id)
    obj_to_ee = pp.multiply(pp.invert(ee_af_to_world), obj_to_world)
    attachments = [
        pp.Attachment(env.ri.robot, env.ri.ee, obj_to_ee, env.fg_object_id)
    ]
    env.ri.attachments = attachments

    c = mercury.geometry.Coordinate(*ee_af_to_world)
    c.translate([0, 0, 0.2], wrt="world")
    j = env.ri.solve_ik(c.pose, n_init=1)
    if j is None:
        logger.warning("j_post_grasp is not found")
        before_return()
        return result
    else:
        result["j_post_grasp"] = j

    env.ri.setj(result["j_place"])
    c = mercury.geometry.Coordinate(*env.ri.get_pose("tipLink"))
    c.translate([0, 0, 0.1], wrt="world")
    j = env.ri.solve_ik(c.pose, n_init=1, rthre=np.deg2rad(30), thre=0.01)
    if j is None:
        logger.warning("j_pre_place is not found")
        before_return()
        return result
    else:
        result["j_pre_place"] = j

    # solve js_grasp
    env.ri.setj(result["j_grasp"])
    env.ri.attachments = []
    c = mercury.geometry.Coordinate(*ee_af_to_world)
    js = []
    for _ in range(5):
        c.translate([0, 0, -0.02])
        j = env.ri.solve_ik(c.pose, n_init=1)
        if j is None:
            break
        js.append(j)
    js = js[::-1]
    if j is None:
        logger.warning("js_grasp is not found")
        before_return()
        return result
    result["js_grasp"] = js

    if not env.ri.validatej(
        js[0], obstacles=bg_object_ids + [env.fg_object_id]
    ):
        logger.warning("j_pre_grasp is invalid")
        before_return()
        return result
    result["j_pre_grasp"] = js[0]

    # solve js_pre_grasp
    env.ri.setj(result["j_init"])
    js = env.ri.planj(
        result["j_pre_grasp"],
        obstacles=env.bg_objects + env.object_ids,
    )
    if js is None:
        logger.warning("js_pre_grasp is not found")
        before_return()
        return result
    result["js_pre_grasp"] = js

    env.ri.setj(result["j_post_grasp"])
    env.ri.attachments = attachments
    env.ri.attachments[0].assign()

    # solve js_place
    obstacles = env.bg_objects + env.object_ids
    obstacles.remove(env.ri.attachments[0].child)
    js = env.ri.planj(
        result["j_pre_place"],
        obstacles=obstacles,
        min_distances_start_goal={(env.ri.attachments[0].child, -1): -0.01},
    )
    if js is None:
        logger.warning("js_place is not found")
        before_return()
        return result
    result["js_place"] = np.r_[
        [result["j_post_grasp"]], js, [result["j_place"]]
    ]

    env.ri.setj(result["j_place"])
    env.ri.attachments[0].assign()
    env.ri.attachments = []

    c = mercury.geometry.Coordinate(*env.ri.get_pose("tipLink"))
    c.translate([0, 0, -0.1], wrt="local")
    c.translate([0, 0, 0.2], wrt="world")
    j = env.ri.solve_ik(c.pose, rotation_axis=False, n_init=1)
    result["js_post_place"] = [j]

    logger.success("Found the solution for reorientation")
    j_prev = result["js_place"][0]
    trajectory_length = 0
    for j in result["js_place"][1:]:
        trajectory_length += np.linalg.norm(j - j_prev)
        j_prev = j
    result["js_place_length"] = trajectory_length

    result["reorient_pose"] = reorient_pose

    before_return()
    return result


def execute_reorient(env, result):
    js = result["js_pre_grasp"]
    for _ in (_ for j in js for _ in env.ri.movej(j, timeout=1)):
        pp.step_simulation()
        if pp.has_gui():
            time.sleep(pp.get_time_step())

    for _ in env.ri.grasp(
        min_dz=0.08, max_dz=0.12, rotation_axis=True, speed=0.001
    ):
        pp.step_simulation()
        if pp.has_gui():
            time.sleep(pp.get_time_step())

    t_place = 0
    js = result["js_place"]
    for _ in (_ for j in js for _ in env.ri.movej(j, timeout=1, speed=0.005)):
        pp.step_simulation()
        t_place += pp.get_time_step()
        if pp.has_gui():
            time.sleep(pp.get_time_step())

    for _ in range(int(1 / pp.get_time_step())):
        pp.step_simulation()
        if pp.has_gui():
            time.sleep(pp.get_time_step())

    env.ri.ungrasp()

    js = result["js_post_place"]
    for _ in (_ for j in js for _ in env.ri.movej(j, timeout=1)):
        pp.step_simulation()
        if pp.has_gui():
            time.sleep(pp.get_time_step())

    for _ in env.ri.movej(env.ri.homej):
        pp.step_simulation()
        if pp.has_gui():
            time.sleep(pp.get_time_step())

    return dict(t_place=t_place)


def get_static_reorient_poses(env):
    pcd_in_obj, normals_in_obj = get_query_ocs(env)
    index = np.argmin(
        np.linalg.norm(pcd_in_obj - pcd_in_obj.mean(axis=0), axis=1)
    )
    point_in_obj = pcd_in_obj[index]
    normal_in_obj = normals_in_obj[index]

    world_saver = pp.WorldSaver()
    lock_renderer = pp.LockRenderer()

    XY = [[0.5, -0.5]]
    pp.draw_aabb(([0.15, -0.45, 0.001], [0.25, -0.35, 0.001]))
    ABG = itertools.product(
        [0],
        [0],
        np.linspace(-np.pi, np.pi, num=16, endpoint=False),
    )

    # XY, ABG validation
    poses = []
    for (x, y), (a, b, g) in itertools.product(XY, ABG):
        c = mercury.geometry.Coordinate(
            position=(x, y, 0),
            quaternion=_utils.get_canonical_quaternion(
                class_id=_utils.get_class_id(env.fg_object_id)
            ),
        )
        c.rotate([a, b, g], wrt="world")
        pp.set_pose(env.fg_object_id, c.pose)

        c.position[2] = -pp.get_aabb(env.fg_object_id)[0][2] + env.TABLE_OFFSET
        pp.set_pose(env.fg_object_id, c.pose)

        points = pp.body_collision_info(
            env.fg_object_id, env.plane, max_distance=0.2
        )
        distance_to_plane = min(point[8] for point in points)
        assert distance_to_plane > 0
        c.position[2] -= distance_to_plane
        c.position[2] += 0.001  # slight offset
        pp.set_pose(env.fg_object_id, c.pose)

        if mercury.pybullet.is_colliding(env.fg_object_id):
            continue

        point, point_p_normal = mercury.geometry.transform_points(
            [point_in_obj, point_in_obj + normal_in_obj],
            mercury.geometry.transformation_matrix(*c.pose),
        )
        normal = point_p_normal - point
        angle = np.arccos(np.dot([0, 0, 1], normal))
        assert angle >= 0

        angle = np.arccos(
            np.dot([-1, 0], mercury.geometry.normalize_vec(normal[:2]))
        )
        assert angle >= 0
        if angle > np.deg2rad(45):
            continue

        poses.append(np.hstack(c.pose))
    poses = np.array(poses).reshape(-1, 7)

    world_saver.restore()
    lock_renderer.restore()

    return poses


def plan_place(env, target_grasp_poses):
    obj_to_world = pp.get_pose(env.fg_object_id)

    j_init = env.ri.getj()

    c = mercury.geometry.Coordinate(*env.PLACE_POSE)
    c.translate([0.05, 0, 0.05], wrt="world")
    LAST_PRE_PLACE_POSE = c.pose

    for grasp_pose in target_grasp_poses:
        world_saver = pp.WorldSaver()

        ee_to_world = pp.multiply(obj_to_world, np.hsplit(grasp_pose, [3]))

        # find self-collision-free j_grasp
        for dg in np.random.uniform(-np.pi, np.pi, size=(6,)):
            result = {}

            c = mercury.geometry.Coordinate(*ee_to_world)
            c.rotate([0, 0, dg])
            j = env.ri.solve_ik(c.pose)
            if j is None:
                env.ri.setj(env.ri.homej)
                j = env.ri.solve_ik(c.pose)
                env.ri.setj(j_init)
            if j is None or not env.ri.validatej(j, obstacles=env.bg_objects):
                world_saver.restore()
                env.ri.attachments = []
                continue
            result["j_grasp"] = j

            env.ri.setj(result["j_grasp"])
            ee_to_world = c.pose

            c = mercury.geometry.Coordinate(*ee_to_world)
            c.translate([0, 0, -0.1])
            j = env.ri.solve_ik(c.pose)
            if j is None or not env.ri.validatej(j, obstacles=env.bg_objects):
                world_saver.restore()
                env.ri.attachments = []
                print("no j_pre_grasp")
                continue
            result["j_pre_grasp"] = j

            ee_to_obj = pp.multiply(pp.invert(obj_to_world), ee_to_world)
            attachments = [
                pp.Attachment(
                    env.ri.robot,
                    env.ri.ee,
                    pp.invert(ee_to_obj),
                    env.fg_object_id,
                )
            ]
            env.ri.attachments = attachments

            c = mercury.geometry.Coordinate(*ee_to_world)
            c.translate([0, 0, 0.2], wrt="world")
            j = env.ri.solve_ik(c.pose)
            if j is None or not env.ri.validatej(j, obstacles=env.bg_objects):
                world_saver.restore()
                env.ri.attachments = []
                continue
            result["j_post_grasp"] = j

            env.ri.setj(result["j_post_grasp"])

            with env.ri.enabling_attachments():
                j = env.ri.solve_ik(
                    env.PRE_PLACE_POSE,
                    move_target=env.ri.robot_model.attachment_link0,
                    n_init=3,
                )
            if j is None or not env.ri.validatej(j, obstacles=env.bg_objects):
                world_saver.restore()
                env.ri.attachments = []
                continue
            result["j_pre_place"] = j

            env.ri.setj(result["j_pre_place"])
            # env.ri.attachments[0].assign()

            with env.ri.enabling_attachments():
                j = env.ri.solve_ik(
                    LAST_PRE_PLACE_POSE,
                    move_target=env.ri.robot_model.attachment_link0,
                )
            if j is None or not env.ri.validatej(j, obstacles=env.bg_objects):
                world_saver.restore()
                env.ri.attachments = []
                continue
            result["j_last_pre_place"] = j

            with env.ri.enabling_attachments():
                j = env.ri.solve_ik(
                    env.PLACE_POSE,
                    move_target=env.ri.robot_model.attachment_link0,
                )
            env.ri.attachments = []
            if j is None or not env.ri.validatej(j, obstacles=env.bg_objects):
                world_saver.restore()
                env.ri.attachments = []
                continue
            env.ri.attachments = attachments
            result["j_place"] = j

            break
        else:
            world_saver.restore()
            env.ri.attachments = []
            continue

        attachments = env.ri.attachments
        env.ri.attachments = []

        env.ri.setj(j_init)
        js = env.ri.planj(
            result["j_pre_grasp"],
            obstacles=env.bg_objects + env.object_ids,
            min_distances=mercury.utils.StaticDict(-0.01),
        )
        if js is None:
            logger.warning("js_pre_grasp is not found")
            world_saver.restore()
            env.ri.attachments = []
            continue
        result["js_pre_grasp"] = js

        env.ri.setj(result["j_post_grasp"])

        obstacles = env.bg_objects + env.object_ids
        obstacles.remove(env.fg_object_id)

        env.ri.attachments = attachments
        js = env.ri.planj(
            result["j_pre_place"],
            obstacles=obstacles,
            min_distances=mercury.utils.StaticDict(-0.01),
        )
        if js is None:
            logger.warning("js_pre_place is not found")
            world_saver.restore()
            env.ri.attachments = []
            continue
        result["js_pre_place"] = js

        env.ri.setj(result["j_pre_place"])
        pose1 = env.ri.get_pose("tipLink")
        env.ri.setj(result["j_last_pre_place"])
        pose2 = env.ri.get_pose("tipLink")

        env.ri.setj(result["j_pre_place"])
        js = []
        env.ri.attachments = []
        for pose in pp.interpolate_poses_by_num_steps(
            pose1, pose2, num_steps=5
        ):
            j = env.ri.solve_ik(pose)
            if j is None or not env.ri.validatej(j, obstacles=obstacles):
                break
            env.ri.setj(j)
            js.append(j)
        if len(js) != 6:
            logger.warning("js_place is not found")
            world_saver.restore()
            env.ri.attachments = []
            continue
        js.append(result["j_place"])
        result["js_place"] = js
        break

    world_saver.restore()
    env.ri.attachments = []
    return result


def execute_place(env, result):
    for _ in (_ for j in result["js_pre_grasp"] for _ in env.ri.movej(j)):
        pp.step_simulation()
        if pp.has_gui():
            time.sleep(pp.get_time_step())

    for _ in env.ri.grasp(min_dz=0.08, max_dz=0.12, rotation_axis=True):
        pp.step_simulation()
        if pp.has_gui():
            time.sleep(pp.get_time_step())

    for _ in env.ri.movej(env.ri.homej):
        pp.step_simulation()
        if pp.has_gui():
            time.sleep(pp.get_time_step())

    js = result["js_pre_place"]
    for _ in (_ for j in js for _ in env.ri.movej(j)):
        pp.step_simulation()
        if pp.has_gui():
            time.sleep(pp.get_time_step())

    js = result["js_place"]
    for _ in (_ for j in js for _ in env.ri.movej(j, timeout=1, speed=0.005)):
        pp.step_simulation()
        if pp.has_gui():
            time.sleep(pp.get_time_step())

    for _ in range(240):
        pp.step_simulation()
        if pp.has_gui():
            time.sleep(pp.get_time_step())

    env.ri.ungrasp()

    for _ in range(240):
        pp.step_simulation()
        if pp.has_gui():
            time.sleep(pp.get_time_step())

    js = result["js_place"][::-1]
    for _ in (_ for j in js for _ in env.ri.movej(j, timeout=1, speed=0.005)):
        pp.step_simulation()
        if pp.has_gui():
            time.sleep(pp.get_time_step())

    for _ in env.ri.movej(env.ri.homej):
        pp.step_simulation()
        if pp.has_gui():
            time.sleep(pp.get_time_step())
