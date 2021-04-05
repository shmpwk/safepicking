#!/usr/bin/env python

import argparse
import time

import imgviz
import numpy as np
import pybullet as p
import pybullet_planning

import mercury

from bin_packing_no_act import get_place_pose
from bin_packing_no_grasp import spawn_object_in_hand
from create_bin import create_bin
from icp_registration import icp_registration


def real_to_virtual(T_obj_to_world):
    real_to_virtual = ([0, 2, 0], [0, 0, 0, 1])
    return pybullet_planning.multiply(real_to_virtual, T_obj_to_world)


def virtual_to_real(T_obj_to_world):
    virtual_to_real = ([0, -2, 0], [0, 0, 0, 1])
    return pybullet_planning.multiply(virtual_to_real, T_obj_to_world)


class VisualFeedback:
    def __init__(
        self,
        ri,
        c_camera_to_world,
        fovy,
        height,
        width,
        plane,
        obj_v,
        obj_to_ee,
        enabled=True,
    ):
        self.ri = ri
        self.c_camera_to_world = c_camera_to_world
        self.fovy = fovy
        self.height = height
        self.width = width
        self.plane = plane
        self.obj_v = obj_v
        self.obj_to_ee = obj_to_ee
        self.enabled = enabled

    def __call__(self, update=False):
        if not self.enabled:
            return
        rgb, depth, segm = mercury.pybullet.get_camera_image(
            self.c_camera_to_world.matrix,
            fovy=self.fovy,
            height=self.height,
            width=self.width,
        )
        rgb[segm == self.plane] = [222, 184, 135]
        rgb_v, depth_v, segm_v = mercury.pybullet.get_camera_image(
            mercury.geometry.transformation_matrix(
                *real_to_virtual(self.c_camera_to_world.pose)
            ),
            fovy=self.fovy,
            height=self.height,
            width=self.width,
        )

        aabb_min, aabb_max = pybullet_planning.get_aabb(self.obj_v)
        aabb_min += [0, -2, 0]
        aabb_max += [0, -2, 0]

        mask = segm_v == self.obj_v
        rgb_masked = rgb * mask[:, :, None]
        rgb_v_masked = rgb_v * mask[:, :, None]

        tiled = imgviz.tile(
            [
                rgb,
                rgb_v,
                np.uint8(rgb * 0.5 + rgb_v * 0.5),
                rgb_masked,
                rgb_v_masked,
                np.uint8(rgb_masked * 0.5 + rgb_v_masked * 0.5),
            ],
            shape=(2, 3),
            border=(255, 255, 255),
        )
        imgviz.io.cv_imshow(
            imgviz.resize(tiled, width=1200), "shoulder_camera"
        )
        imgviz.io.cv_waitkey(1)

        if not update or mask.sum() < (30 * 30):
            return False

        K = mercury.geometry.opengl_intrinsic_matrix(
            self.fovy, self.height, self.width
        )

        pcd = mercury.geometry.pointcloud_from_depth(
            depth, K[0, 0], K[1, 1], K[0, 2], K[1, 2]
        )
        pcd = mercury.geometry.transform_points(
            pcd, self.c_camera_to_world.matrix
        )
        mask = (
            mask
            & (pcd >= aabb_min).all(axis=2)
            & (pcd <= aabb_max).all(axis=2)
            & ~np.isnan(depth)
        )
        pcd = pcd[mask]

        ee_to_world = mercury.pybullet.get_pose(self.ri.robot, self.ri.ee)

        pcd_v = np.loadtxt(mercury.datasets.ycb.get_pcd_file(class_id=2))
        T_obj_v_to_world = mercury.geometry.transformation_matrix(
            *pybullet_planning.multiply(ee_to_world, self.obj_to_ee)
        )
        pcd_v = mercury.geometry.transform_points(pcd_v, T_obj_v_to_world)

        print("==> Doing icp_registration")
        T_pcd_to_pcd_v = icp_registration(pcd, pcd_v, np.eye(4))
        pcd_to_pcd_v = mercury.geometry.pose_from_matrix(T_pcd_to_pcd_v)

        obj_v_to_world = pybullet_planning.multiply(
            ee_to_world, self.obj_to_ee
        )
        obj_to_world = pybullet_planning.multiply(
            pybullet_planning.invert(pcd_to_pcd_v), obj_v_to_world
        )
        self.obj_to_ee = pybullet_planning.multiply(
            pybullet_planning.invert(ee_to_world), obj_to_world
        )
        return True


class StepSimulation:
    def __init__(self, ri, ri_v, obj_v, obj_to_ee, constraint_id):
        self.ri = ri
        self.ri_v = ri_v
        self.obj_v = obj_v
        self.obj_to_ee = obj_to_ee
        self.constraint_id = constraint_id

    def __call__(self):
        p.stepSimulation()
        self.ri_v.setj(self.ri.getj())
        if self.constraint_id:
            pybullet_planning.set_pose(
                self.obj_v,
                pybullet_planning.multiply(
                    mercury.pybullet.get_pose(self.ri_v.robot, self.ri_v.ee),
                    self.obj_to_ee,
                ),
            )
        time.sleep(1 / 240)


def main():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--pause", action="store_true", help="pause")
    parser.add_argument("--perfect", action="store_true", help="perfect")
    parser.add_argument(
        "--no-feedback", action="store_true", help="no feedback"
    )
    parser.add_argument("--update", action="store_true", help="update")
    parser.add_argument("--class-id", type=int, default=2, help="class id")
    args = parser.parse_args()

    if args.update:
        assert not args.no_feedback

    pybullet_planning.connect()
    pybullet_planning.add_data_path()
    p.setGravity(0, 0, -9.8)

    p.resetDebugVisualizerCamera(
        cameraDistance=2,
        cameraYaw=90,
        cameraPitch=-40,
        cameraTargetPosition=(0, 1, 0),
    )

    ri = mercury.pybullet.PandaRobotInterface()
    ri_v = mercury.pybullet.PandaRobotInterface(
        pose=real_to_virtual(pybullet_planning.get_pose(ri.robot))
    )

    with pybullet_planning.LockRenderer():
        plane = p.loadURDF("plane.urdf")
        bin = create_bin(0.4, 0.38, 0.2)
        bin_to_world = ([0.4, 0.4, 0.11], [0, 0, 0, 1])
        pybullet_planning.set_pose(bin, bin_to_world)
        bin_aabb = np.array(p.getAABB(bin))
        bin_aabb[0] += 0.01
        bin_aabb[1] -= 0.01

    with pybullet_planning.LockRenderer():
        bin2 = create_bin(0.4, 0.38, 0.2)
        pybullet_planning.set_pose(bin2, real_to_virtual(bin_to_world))

    c_camera_to_world = mercury.geometry.Coordinate()
    c_camera_to_world.translate([-0.3, 0.4, 0.8], wrt="world")
    c_camera_to_world.rotate([0, 0, np.deg2rad(-90)], wrt="local")
    c_camera_to_world.rotate([np.deg2rad(-130), 0, 0], wrt="local")

    fovy = np.deg2rad(45)
    height = 480
    width = 640
    mercury.pybullet.draw_camera(
        fovy=fovy,
        height=height,
        width=width,
        pose=c_camera_to_world.pose,
    )
    pybullet_planning.draw_pose(c_camera_to_world.pose)
    mercury.pybullet.draw_camera(
        fovy=fovy,
        height=height,
        width=width,
        pose=real_to_virtual(c_camera_to_world.pose),
    )
    pybullet_planning.draw_pose(real_to_virtual(c_camera_to_world.pose))

    np.random.seed(1)

    class_id = args.class_id
    placed_objects = []
    for _ in range(10):
        obj, obj_to_ee, constraint_id = spawn_object_in_hand(
            ri, class_id=class_id, noise=not args.perfect
        )

        visual_file = mercury.datasets.ycb.get_visual_file(class_id=class_id)
        collision_file = mercury.pybullet.get_collision_file(visual_file)
        with pybullet_planning.LockRenderer():
            obj_v = mercury.pybullet.create_mesh_body(
                visual_file=visual_file,
                collision_file=collision_file,
            )

        step_simulation = StepSimulation(
            ri=ri,
            ri_v=ri_v,
            obj_v=obj_v,
            obj_to_ee=obj_to_ee,
            constraint_id=constraint_id,
        )
        visual_feedback = VisualFeedback(
            ri=ri,
            c_camera_to_world=c_camera_to_world,
            fovy=fovy,
            height=height,
            width=width,
            plane=plane,
            obj_v=obj_v,
            obj_to_ee=obj_to_ee,
            enabled=not args.no_feedback,
        )

        step_simulation()
        visual_feedback()

        if not placed_objects and args.pause:
            print("Please press 'n' to start")
            while True:
                if ord("n") in p.getKeyboardEvents():
                    break

        # before-place

        c = mercury.geometry.Coordinate(*pybullet_planning.get_pose(bin))
        c.translate([0, 0, 0.3], wrt="world")

        robot_model = ri.get_skrobot(
            attachments=[
                pybullet_planning.Attachment(ri.robot, ri.ee, obj_to_ee, obj)
            ]
        )
        j = robot_model.inverse_kinematics(
            c.skrobot_coords,
            move_target=robot_model.attachment_link0,
        )[:-1]
        for i in ri.movej(j):
            step_simulation()
            if i % 8 == 0:
                visual_feedback()

        # place

        obstacles = [plane, bin]
        obj_to_world = get_place_pose(obj, class_id, bin_aabb[0], bin_aabb[1])

        if obj_to_world[0] is None:
            print("Warning: failed to find place pose")
            break

        max_distance = 0
        while True:
            attachments = [
                pybullet_planning.Attachment(
                    ri.robot, ri.ee, visual_feedback.obj_to_ee, obj
                )
            ]
            robot_model = ri.get_skrobot(attachments)
            j = robot_model.inverse_kinematics(
                mercury.geometry.Coordinate(*obj_to_world).skrobot_coords,
                move_target=robot_model.attachment_link0,
            )
            if j is False:
                print("==> Inverse kinematics failed. Retrying")
                continue
            path = ri.planj(
                j[:-1],
                obstacles=obstacles + placed_objects,
                attachments=attachments,
                max_distance=max_distance,
            )
            if path is None:
                max_distance -= 0.01
                print(
                    f"==> Path planning failed. Retrying with {max_distance}"
                )
                continue
            for i, _ in enumerate((_ for j in path for _ in ri.movej(j))):
                step_simulation()
                if i == 16:
                    if visual_feedback(update=args.update):
                        step_simulation.obj_to_ee = visual_feedback.obj_to_ee
                        print("==> Doing re-planning")
                        break
                elif i % 8 == 0:
                    visual_feedback()
            else:
                print("==> Reached to the goal")
                break

        for i in range(120):
            step_simulation()
            if i % 24 == 0:
                visual_feedback()

        # ungrasp

        p.removeConstraint(constraint_id)
        step_simulation.constraint_id = None
        placed_objects.append(obj)

        with pybullet_planning.LockRenderer():
            mercury.pybullet.create_mesh_body(
                visual_file=collision_file,
                position=obj_to_world[0],
                quaternion=obj_to_world[1],
                rgba_color=(0, 1, 0, 0.5),
            )
            mercury.pybullet.create_mesh_body(
                visual_file=collision_file,
                position=real_to_virtual(obj_to_world)[0],
                quaternion=real_to_virtual(obj_to_world)[1],
                rgba_color=(0, 1, 0, 0.5),
            )

        for i in range(120):
            step_simulation()
            if i % 24 == 0:
                visual_feedback()

        c = mercury.geometry.Coordinate(
            *mercury.pybullet.get_pose(ri.robot, ri.ee)
        )
        c.translate([0, 0, -0.05])
        j = ri.solve_ik(c.pose)
        for i, _ in enumerate(ri.movej(j)):
            step_simulation()
            if i % 8 == 0:
                visual_feedback()

        # reset

        path = None
        while path is None:
            path = ri.planj(ri.homej, obstacles=obstacles + placed_objects)
        i = 0
        for j in path:
            for _ in ri.movej(j):
                step_simulation()
                if i % 8 == 0:
                    visual_feedback()
                i += 1

    mercury.pybullet.step_and_sleep()

    pybullet_planning.disconnect()


if __name__ == "__main__":
    main()
