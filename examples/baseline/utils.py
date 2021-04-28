import argparse
import itertools
import time

import imgviz
from loguru import logger
import numpy as np
import pybullet as p
import pybullet_planning as pp

import mercury


def get_parser():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--pause", action="store_true", help="pause")
    parser.add_argument(
        "--enable-visual", action="store_true", help="enable visual"
    )
    parser.add_argument(
        "--camera-config", type=int, default=0, help="camera config"
    )
    parser.add_argument("--imshow", action="store_true", help="imshow")
    parser.add_argument("--seed", type=int, default=0, help="seed")
    parser.add_argument("--retime", type=float, default=1, help="retime")
    return parser


def init_world(camera_distance=1.5):
    pp.connect()
    pp.add_data_path()
    p.setGravity(0, 0, -9.8)

    p.resetDebugVisualizerCamera(
        cameraDistance=camera_distance,
        cameraYaw=90,
        cameraPitch=-60,
        cameraTargetPosition=(0, 0, 0),
    )

    plane = p.loadURDF("plane.urdf")
    return plane


def load_pile(base_pose, npz_file, mass=None, enable_visual=False):
    data = np.load(npz_file)
    object_ids = []
    for class_id, position, quaternion in zip(
        data["class_ids"], data["positions"], data["quaternions"]
    ):
        coord = mercury.geometry.Coordinate(
            position=position,
            quaternion=quaternion,
        )
        coord.transform(
            mercury.geometry.transformation_matrix(*base_pose), wrt="world"
        )

        visual_file = mercury.datasets.ycb.get_visual_file(class_id)
        collision_file = mercury.pybullet.get_collision_file(visual_file)
        if enable_visual:
            rgba_color = None
        else:
            visual_file = collision_file
            rgba_color = imgviz.label_colormap()[class_id] / 255
        mass_actual = mercury.datasets.ycb.masses[class_id]
        object_id = mercury.pybullet.create_mesh_body(
            visual_file=visual_file,
            rgba_color=rgba_color,
            collision_file=collision_file,
            mass=mass_actual if mass is None else mass,
            position=coord.position,
            quaternion=coord.quaternion,
        )
        object_ids.append(object_id)
    return object_ids


def get_camera_pose(camera_config):
    c_cam_to_ee = mercury.geometry.Coordinate()

    if camera_config == 0:
        c_cam_to_ee.translate([0, -0.05, -0.1])
    elif camera_config == 1:
        c_cam_to_ee.rotate([np.deg2rad(-15), 0, 0])
        c_cam_to_ee.translate([0, -0.08, -0.2])
    elif camera_config == 2:
        c_cam_to_ee.rotate([np.deg2rad(-15), 0, 0])
        c_cam_to_ee.translate([0, -0.08, -0.35])
    else:
        raise ValueError

    return c_cam_to_ee.pose


def pause(enabled):
    if enabled:
        print("Please press 'n' to start")
        while True:
            if ord("n") in p.getKeyboardEvents():
                break


class StepSimulation:
    def __init__(self, ri, imshow=False, retime=1):
        self.ri = ri
        self.imshow = imshow
        self.retime = retime

        self.i = 0

    def __call__(self):
        p.stepSimulation()
        self.ri.step_simulation()
        if self.imshow and self.i % (8 * self.retime) == 0:
            rgb, depth, _ = self.ri.get_camera_image()
            depth[(depth < 0.3) | (depth > 2)] = np.nan
            tiled = imgviz.tile(
                [
                    rgb,
                    imgviz.depth2rgb(depth, min_value=0.3, max_value=0.6),
                ],
                border=(255, 255, 255),
            )
            imgviz.io.cv_imshow(tiled, "wrist_camera")
            imgviz.io.cv_waitkey(1)
        time.sleep(1 / 240 / self.retime)
        self.i += 1


def get_canonical_quaternion(class_id):
    if class_id == 15:
        quaternion = mercury.geometry.quaternion_from_euler(
            [np.deg2rad(90), 0, 0]
        )
    else:
        quaternion = [0, 0, 0, 1]
    return quaternion


def place_to_regrasp(
    ri, regrasp_aabb, bg_object_ids, object_ids, step_simulation
):
    n_trial = 5

    object_id = ri.attachments[0].child
    class_id = get_class_id(object_id)

    for i in itertools.count():
        with pp.LockRenderer():
            with pp.WorldSaver():
                quaternion = get_canonical_quaternion(class_id=class_id)
                if i >= n_trial:
                    c = mercury.geometry.Coordinate(quaternion=quaternion)
                    euler = [
                        [np.deg2rad(90), 0, 0],
                        [np.deg2rad(-90), 0, 0],
                        [0, np.deg2rad(90), 0],
                        [0, np.deg2rad(-90), 0],
                    ][np.random.randint(0, 4)]
                    c.rotate(euler, wrt="world")
                    quaternion = c.quaternion

                pp.set_pose(object_id, ([0, 0, 0], quaternion))
                aabb = pp.get_aabb(object_id)
                position = np.random.uniform(*regrasp_aabb)
                position[2] -= aabb[0][2]
                c = mercury.geometry.Coordinate(position, quaternion)

        regrasp_pose = c.pose  # obj_af_to_world

        with ri.enabling_attachments():
            obj_to_world = ri.get_pose("attachment_link0")

            move_target_to_world = mercury.geometry.Coordinate(*obj_to_world)
            move_target_to_world.transform(
                np.linalg.inv(
                    mercury.geometry.quaternion_matrix(regrasp_pose[1])
                ),
                wrt="local",
            )
            move_target_to_world = move_target_to_world.pose
            regrasp_pose = regrasp_pose[0], [0, 0, 0, 1]

            ee_to_world = ri.get_pose("tipLink")
            move_target_to_ee = pp.multiply(
                pp.invert(ee_to_world), move_target_to_world
            )
            ri.add_link("move_target", pose=move_target_to_ee)

            j = ri.solve_ik(
                regrasp_pose,
                move_target=ri.robot_model.move_target,
                rotation_axis="z",
            )
        if j is None:
            logger.warning("j is None")
            continue

        obstacles = bg_object_ids + object_ids
        obstacles.remove(object_id)
        path = ri.planj(j, obstacles=obstacles)
        if path is None:
            logger.warning("path is None")
            continue

        with pp.LockRenderer():
            with pp.WorldSaver():
                ri.setj(path[-1])
                c = mercury.geometry.Coordinate(*ri.get_pose("tipLink"))
                c.translate([0, 0, -0.05])
                j = ri.solve_ik(c.pose, rotation_axis=None)
                path2 = ri.planj(j)
        if path2 is None:
            logger.warning("path2 is None")
            continue

        break
    for _ in (_ for j in path for _ in ri.movej(j, speed=0.005)):
        step_simulation()

    for _ in range(240):
        step_simulation()

    ri.ungrasp()

    for _ in range(240):
        step_simulation()

    for _ in (_ for j in path2 for _ in ri.movej(j, speed=0.005)):
        step_simulation()

    for _ in ri.move_to_homej(bg_object_ids, object_ids):
        step_simulation()

    return regrasp_pose, i < n_trial


def get_place_pose(object_id, bin_aabb_min, bin_aabb_max):
    position_org, quaternion_org = p.getBasePositionAndOrientation(object_id)

    class_id = get_class_id(object_id)
    quaternion = get_canonical_quaternion(class_id)

    with pp.LockRenderer():
        with pp.WorldSaver():
            p.resetBasePositionAndOrientation(
                object_id, position_org, quaternion
            )

            aabb_min, aabb_max = np.array(p.getAABB(object_id))
            position_lt = bin_aabb_min - (aabb_min - position_org)
            position_rb = bin_aabb_max + (aabb_min - position_org)

            with pp.LockRenderer():
                z = position_lt[2]
                for x in np.linspace(position_lt[0], position_rb[0]):
                    for y in np.linspace(position_lt[1], position_rb[1]):
                        position = (x, y, z)
                        pp.set_pose(object_id, (position, quaternion))
                        if not mercury.pybullet.is_colliding(object_id):
                            break
                    else:
                        continue
                    break
                else:
                    position, quaternion = None, None

    return position, quaternion


def plan_placement(ri, place_aabb, bg_object_ids, object_ids):
    object_id = ri.attachments[0].child
    place_pose = get_place_pose(
        object_id=object_id,
        bin_aabb_min=place_aabb[0],
        bin_aabb_max=place_aabb[1],
    )
    with ri.enabling_attachments():
        j = ri.solve_ik(
            place_pose,
            move_target=ri.robot_model.attachment_link0,
        )
    if j is None:
        logger.warning("j is None")
        return place_pose, None

    obstacles = bg_object_ids + object_ids
    obstacles.remove(object_id)
    path = ri.planj(j, obstacles=obstacles)
    if path is None:
        logger.warning("path is None")
        return place_pose, None

    return place_pose, path


virtual_objects = []


def place(
    ri, object_id, place_pose, path, bg_object_ids, object_ids, step_simulation
):
    obj_v = mercury.pybullet.duplicate(
        object_id,
        collision=False,
        rgba_color=[0, 1, 0, 0.5],
        position=place_pose[0],
        quaternion=place_pose[1],
    )
    virtual_objects.append(obj_v)

    for _ in (_ for j in path for _ in ri.movej(j, speed=0.005)):
        step_simulation()

    for _ in range(240):
        step_simulation()

    ri.ungrasp()

    for _ in range(240):
        step_simulation()

    c = mercury.geometry.Coordinate(*ri.get_pose("tipLink"))
    c.translate([0, 0, -0.05])
    j = ri.solve_ik(c.pose, rotation_axis=None)
    for _ in ri.movej(j, speed=0.005):
        step_simulation()

    min_distance = 0
    path = None
    while path is None:
        min_distances = {}
        for attachment in ri.attachments:
            min_distances[(attachment.child, -1)] = min_distance

        path = ri.planj(
            ri.homej,
            obstacles=bg_object_ids + object_ids,
            min_distances=min_distances,
        )
        min_distance -= 0.01
    for _ in (_ for j in path for _ in ri.movej(j)):
        step_simulation()


def get_class_id(object_id):
    visual_shape_data = p.getVisualShapeData(object_id)
    class_name = visual_shape_data[0][4].decode().split("/")[-2]
    class_id = mercury.datasets.ycb.class_names.tolist().index(class_name)
    return class_id


def correct(
    ri, object_id, place_pose, bg_object_ids, object_ids, step_simulation
):
    c = mercury.geometry.Coordinate(*ri.get_pose("tipLink"))
    c.position = place_pose[0]
    c.position[2] = 0.7
    j = ri.solve_ik(
        c.pose,
        move_target=ri.robot_model.camera_link,
        rotation_axis="z",
    )
    if j is None:
        logger.warning("j is None")
        return
    for _ in ri.movej(j):
        step_simulation()
    j_camera = j

    while True:
        obj_to_world = pp.get_pose(object_id)

        class_id = get_class_id(object_id)
        pcd_file = mercury.datasets.ycb.get_pcd_file(class_id=class_id)
        pcd = np.loadtxt(pcd_file)
        pcd_target = mercury.geometry.transform_points(
            pcd, mercury.geometry.transformation_matrix(*place_pose)
        )
        pcd_source = mercury.geometry.transform_points(
            pcd, mercury.geometry.transformation_matrix(*obj_to_world)
        )
        auc = mercury.geometry.average_distance_auc(pcd_target, pcd_source)
        logger.info(auc)
        if auc >= 0.5:
            logger.success("auc >= 0.5")
            break

        while True:
            with mercury.pybullet.stash_objects(virtual_objects):
                _, depth, segm = ri.get_camera_image()
            for _ in ri.random_grasp(
                depth=depth,
                segm=segm,
                bg_object_ids=bg_object_ids,
                object_ids=object_ids,
                target_object_ids=[object_id],
                max_angle=np.deg2rad(10),
            ):
                step_simulation()
            if not ri.gripper.check_grasp():
                ri.ungrasp()
                for _ in ri.movej(j):
                    step_simulation()
                continue
            break

        with ri.enabling_attachments():
            j = ri.solve_ik(
                place_pose, move_target=ri.robot_model.attachment_link0
            )
        obstacles = bg_object_ids + object_ids
        obstacles.remove(object_id)
        path = ri.planj(j, obstacles=obstacles)
        if path is None:
            logger.warning("path is None")
            path = [j]
        for _ in (_ for j in path for _ in ri.movej(j, speed=0.001)):
            step_simulation()

        for _ in range(240):
            step_simulation()

        ri.ungrasp()

        for _ in range(240):
            step_simulation()

        c = mercury.geometry.Coordinate(*ri.get_pose("tipLink"))
        c.translate([0, 0, -0.05])
        j = ri.solve_ik(c.pose, rotation_axis=None)
        for _ in ri.movej(j):
            step_simulation()

        for _ in ri.movej(j_camera):
            step_simulation()

    for _ in ri.move_to_homej(bg_object_ids, object_ids):
        step_simulation()
