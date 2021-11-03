#!/usr/bin/env python

import argparse
import collections

import gdown
import IPython
import numpy as np
import path
import pybullet_planning as pp
import torch

import mercury
from mercury.examples.picking import _agent
from mercury.examples.picking import _env
from mercury.examples.picking import _get_heightmap
from mercury.examples.picking import _utils

import cv_bridge
from morefusion_ros.msg import ObjectClassArray
from morefusion_ros.msg import ObjectPoseArray
import rospy
from sensor_msgs.msg import CameraInfo
from sensor_msgs.msg import Image

from _message_subscriber import MessageSubscriber
from base_task_interface import BaseTaskInterface


class SafepickingTaskInterface:
    def __init__(self, base: BaseTaskInterface):
        self.base = base

        self._picking_env = _env.PickFromPileEnv()
        self._agent = _agent.DqnAgent(
            env=self._picking_env, model="fusion_net"
        )
        self._agent.build(training=False)

        example_dir = path.Path(mercury.__file__).parent / "examples/picking"
        weight_dir = (
            example_dir / "logs/20210709_005731-fusion_net-noise/weights/84500"
        )
        gdown.cached_download(
            id="1MBfMHpfOrcMuBFHbKvHiw6SA5f7q1T6l",
            path=weight_dir / "q.pth",
            md5="886b36a99c5a44b54c513ec7fee4ae0d",
        )
        self._agent.load_weights(weight_dir)

        self._target_class_id = None
        self._sub_singleview = MessageSubscriber(
            [
                ("/camera/aligned_depth_to_color/image_raw", Image),
                (
                    "/camera/mask_rcnn_instance_segmentation/output/class",
                    ObjectClassArray,
                ),
                (
                    "/camera/mask_rcnn_instance_segmentation/output/label_ins",
                    Image,
                ),
                ("/singleview_3d_pose_estimation/output", ObjectPoseArray),
            ]
        )

    def _get_grasp_poses(self):
        camera_to_base = self.obs["camera_to_base"]
        depth = self.obs["depth"]
        label = self.obs["label"]
        pcd_in_camera = self.obs["pcd_in_camera"]
        pcd_in_base = self.obs["pcd_in_base"]

        normals_in_camera = mercury.geometry.normals_from_pointcloud(
            pcd_in_camera
        )

        instance_id = self.obs["class_id_to_instance_ids"][
            self._target_class_id
        ][0]
        mask = ~np.isnan(depth) & (label == instance_id)
        pcd_in_camera = pcd_in_camera[mask]
        pcd_in_base = pcd_in_base[mask]
        normals_in_camera = normals_in_camera[mask]

        normals_in_base = (
            mercury.geometry.transform_points(
                pcd_in_camera + normals_in_camera,
                mercury.geometry.transformation_matrix(*camera_to_base),
            )
            - pcd_in_base
        )
        quaternion_in_base = mercury.geometry.quaternion_from_vec2vec(
            [0, 0, 1], normals_in_base
        )

        grasp_poses = np.hstack((pcd_in_base, quaternion_in_base))
        return grasp_poses

    def _look_at_pile(self, *args, **kwargs):
        self.base.look_at(
            eye=[0.5, 0, 0.7], target=[0.5, 0, 0], *args, **kwargs
        )

    def capture_pile(self):
        if self.base._env.object_ids:
            for obj_id in self.base._env.object_ids:
                pp.remove_body(obj_id)
        self.base._env.object_ids = None
        self.base._env.fg_object_id = None

        self._look_at_pile()

        self._sub_singleview.subscribe()
        self.base.start_passthrough()
        rospy.sleep(5)
        while True:
            if not self._sub_singleview.msgs:
                continue
            class_ids_detected = [
                c.class_id for c in self._sub_singleview.msgs[1].classes
            ]
            if self._target_class_id not in class_ids_detected:
                continue
            break
        self.base.stop_passthrough()
        self._sub_singleview.unsubscribe()

        camera_msg = rospy.wait_for_message(
            "/camera/color/camera_info", CameraInfo
        )
        (
            depth_msg,
            cls_msg,
            label_msg,
            obj_poses_msg,
        ) = self._sub_singleview.msgs

        K = np.array(camera_msg.K).reshape(3, 3)

        bridge = cv_bridge.CvBridge()
        depth = bridge.imgmsg_to_cv2(depth_msg)
        assert depth.dtype == np.uint16
        depth = depth.astype(np.float32) / 1000
        depth[depth == 0] = np.nan

        # -2: unknown, -1: background, 0: instance_0, 1: instance_1, ...
        label = bridge.imgmsg_to_cv2(label_msg).copy()
        label[label == -2] = -1

        class_id_to_instance_ids = collections.defaultdict(list)
        for cls in cls_msg.classes:
            class_id_to_instance_ids[cls.class_id].append(cls.instance_id)
        class_id_to_instance_ids = dict(class_id_to_instance_ids)

        target_instance_id = class_id_to_instance_ids[self._target_class_id][0]

        camera_to_base = self.base.lookup_transform(
            "panda_link0",
            camera_msg.header.frame_id,
            time=camera_msg.header.stamp,
            timeout=rospy.Duration(1),
        )

        pcd_in_camera = mercury.geometry.pointcloud_from_depth(
            depth, fx=K[0, 0], fy=K[1, 1], cx=K[0, 2], cy=K[1, 2]
        )
        pcd_in_base = mercury.geometry.transform_points(
            pcd_in_camera,
            mercury.geometry.transformation_matrix(*camera_to_base),
        )

        assert self.base._env.object_ids is None
        self.base._env.object_ids = []
        for i, obj_pose_msg in enumerate(obj_poses_msg.poses):
            pose = obj_pose_msg.pose
            instance_id = obj_pose_msg.instance_id
            class_id = obj_pose_msg.class_id
            position = (pose.position.x, pose.position.y, pose.position.z)
            quaternion = (
                pose.orientation.x,
                pose.orientation.y,
                pose.orientation.z,
                pose.orientation.w,
            )
            obj_to_camera = (position, quaternion)
            obj_to_base = pp.multiply(camera_to_base, obj_to_camera)
            visual_file = mercury.datasets.ycb.get_visual_file(class_id)
            obj_id = mercury.pybullet.create_mesh_body(
                visual_file=visual_file,
                position=obj_to_base[0],
                quaternion=obj_to_base[1],
            )
            if instance_id == target_instance_id:
                self.base._env.fg_object_id = obj_id
            self.base._env.object_ids.append(obj_id)

        self.obs = dict(
            camera_to_base=camera_to_base,
            K=K,
            depth=depth,
            label=label,
            class_id_to_instance_ids=class_id_to_instance_ids,
            pcd_in_camera=pcd_in_camera,
            pcd_in_base=pcd_in_base,
            target_instance_id=target_instance_id,
        )

    def run(self, place=True):
        self.base.init_workspace()

        self._target_class_id = 3

        self.capture_pile()

        grasp_poses = self._get_grasp_poses()

        centroid = np.mean(grasp_poses[:, :3], axis=0)
        dist_from_centroid = np.linalg.norm(
            grasp_poses[:, :3] - centroid, axis=1
        )
        index = np.argmin(dist_from_centroid)
        grasp_pose = grasp_poses[index]
        grasp_pose = np.hsplit(grasp_pose, [3])

        if 1:
            pp.draw_pose(grasp_pose, width=2)

        self.base.pi.setj(
            [
                0.9455609917640686,
                -1.7446026802062988,
                -1.1828051805496216,
                -2.2014822959899902,
                -1.2853317260742188,
                1.2614742517471313,
                -0.2561403810977936,
            ]
        )

        j = self.base.pi.solve_ik(grasp_pose, rotation_axis="z", validate=True)
        assert j is not None

        with pp.WorldSaver():
            self.base.pi.setj(j)
            grasp_pose = self.base.pi.get_pose("tipLink")

        js_extract = self.plan_extraction(
            j_init=j,
            grasp_pose=grasp_pose,
        )

        if self.base._env.fg_object_id is not None:
            ee_to_world = grasp_pose
            obj_to_world = pp.get_pose(self.base._env.fg_object_id)
            obj_to_ee = pp.multiply(pp.invert(ee_to_world), obj_to_world)
            attachments = [
                pp.Attachment(
                    self.base.pi.robot,
                    self.base.pi.ee,
                    obj_to_ee,
                    self.base._env.fg_object_id,
                )
            ]
        else:
            attachments = []

        js_grasp = [j]

        for _ in range(5):
            self.base.pi.setj(j)
            c = mercury.geometry.Coordinate(*self.base.pi.get_pose("tipLink"))
            c.translate([0, 0, -0.02], wrt="local")
            j = self.base.pi.solve_ik(c.pose, validate=True)
            assert j is not None
            js_grasp.append(j)
        js_grasp = js_grasp[::-1]

        js_place = self._plan_placement(j_init=self.base.pi.homej)

        self.base.movejs(js_grasp, time_scale=10)

        self.base.start_grasp()
        self.base.pi.attachments = attachments

        self.base.movejs(js_extract, time_scale=20)

        if place:
            self.base.reset_pose()

            self.base.movejs(js_place)
        else:
            js = np.linspace(js_extract[-1], self.base.pi.homej)
            self.base.movejs(js[:5], time_scale=10)

        self.base.stop_grasp()
        self.base.pi.attachments = []
        rospy.sleep(6)

        if place:
            self.base.movejs(js_place[::-1])

            self.base.reset_pose()

    def _plan_placement(self, j_init):
        bin_to_base = pp.get_pose(self._bin)

        with pp.WorldSaver():
            self.base.pi.setj(j_init)

            c = mercury.geometry.Coordinate(*self.base.pi.get_pose("tipLink"))
            c.position = bin_to_base[0]

            j = self.base.pi.solve_ik(c.pose, rotation_axis="z")
            assert j is not None

            js_place = [j]

            for _ in range(5):
                self.base.pi.setj(j)
                c = mercury.geometry.Coordinate(
                    *self.base.pi.get_pose("tipLink")
                )
                c.translate([0, 0, -0.02], wrt="local")
                j = self.base.pi.solve_ik(c.pose, validate=True)
                assert j is not None
                js_place.append(j)
            js_place = js_place[::-1]

        return js_place

    def _get_heightmap(self, center_xy):
        center = np.array([center_xy[0], center_xy[1], np.nan])
        aabb = np.array(
            [
                center - self._picking_env.HEIGHTMAP_SIZE / 2,
                center + self._picking_env.HEIGHTMAP_SIZE / 2,
            ]
        )
        aabb[0][2] = self.base._env.TABLE_OFFSET - 0.05
        aabb[1][2] = self.base._env.TABLE_OFFSET + 0.5
        heightmap, _, idmap = _get_heightmap.get_heightmap(
            points=self.obs["pcd_in_base"],
            colors=np.zeros(self.obs["pcd_in_base"].shape, dtype=np.uint8),
            ids=self.obs["label"] + 1,  # -1: background -> 0: background
            aabb=aabb,
            pixel_size=self._picking_env.HEIGHTMAP_PIXEL_SIZE,
        )
        idmap -= 1  # 0: background -> -1: background
        return heightmap, idmap

    def _plan_extraction(self, j_init, grasp_pose):
        heightmap, idmap = self._get_heightmap(center_xy=grasp_pose[0][:2])
        target_instance_id = self.obs["target_instance_id"]
        maskmap = idmap == target_instance_id

        num_instance = len(self.base._env.object_ids)
        grasp_flags = np.zeros((num_instance,), dtype=np.uint8)
        object_labels = np.zeros(
            (num_instance, len(self._picking_env.CLASS_IDS)), dtype=np.int8
        )
        object_poses = np.zeros((num_instance, 7), dtype=np.float32)
        for i in range(num_instance):
            obj_id = self.base._env.object_ids[i]
            class_id = _utils.get_class_id(obj_id)
            position, quaternion = pp.get_pose(obj_id)
            grasp_flags[i] = obj_id == self.base._env.fg_object_id
            object_label = self._picking_env.CLASS_IDS.index(class_id)
            object_labels[i] = np.eye(len(self._picking_env.CLASS_IDS))[
                object_label
            ]
            object_poses[i] = np.r_[
                position[0] - grasp_pose[0][0],
                position[1] - grasp_pose[0][1],
                position[2] - self.base._env.TABLE_OFFSET,
                quaternion[0],
                quaternion[1],
                quaternion[2],
                quaternion[3],
            ]

        ee_poses = np.zeros(
            (self._picking_env.episode_length, 7), dtype=np.float32
        )
        ee_poses = np.r_[
            ee_poses[1:],
            (
                np.hstack(grasp_pose)
                - [
                    grasp_pose[0][0],
                    grasp_pose[0][1],
                    self.base._env.TABLE_OFFSET,
                    0,
                    0,
                    0,
                    0,
                ]
            )[None],
        ]

        observation = dict(
            heightmap=heightmap.astype(np.float32),
            maskmap=maskmap,
            object_labels_init=object_labels,
            object_poses_init=object_poses.astype(np.float32),
            grasp_flags_init=grasp_flags,
            ee_poses=ee_poses.astype(np.float32),
        )
        for key in observation:
            observation[key] = torch.as_tensor(observation[key])[None]

        world_saver = pp.WorldSaver()

        self.base.pi.setj(j_init)

        js = []
        for i in range(self._picking_env.episode_length):
            with torch.no_grad():
                q = self._agent.q(observation)

            q = q[0].cpu().numpy().reshape(-1)
            actions = np.argsort(q)[::-1]

            for action in actions:
                a = action // 2
                if i == self._picking_env.episode_length - 1:
                    t = 1
                else:
                    t = action % 2
                dx, dy, dz, da, db, dg = self._picking_env.actions[a]

                c = mercury.geometry.Coordinate(
                    *self.base.pi.get_pose("tipLink")
                )
                c.translate([dx, dy, dz], wrt="world")
                c.rotate([da, db, dg], wrt="world")

                j = self.base.pi.solve_ik(c.pose)
                if j is not None:
                    break
            self.base.pi.setj(j)
            js.append(j)

            if t == 1:
                break

            ee_poses = np.r_[
                ee_poses[1:],
                (
                    np.hstack(c.pose)
                    - [
                        grasp_pose[0][0],
                        grasp_pose[0][1],
                        self.base._env.TABLE_OFFSET,
                        0,
                        0,
                        0,
                        0,
                    ]
                )[None],
            ].astype(np.float32)
            observation["ee_poses"] = torch.as_tensor(ee_poses)[None]

        world_saver.restore()

        return js

    def test_heightmap_change(self):
        self._target_class_id = 5

        self.capture_pile()
        heightmap, idmap = self._get_heightmap(
            self.base._env.PILE_POSITION[:2]
        )
        heightmap[idmap == self.obs["target_instance_id"]] = np.nan
        heightmap[heightmap == 0] = np.nan
        heightmap1 = heightmap

        input("Move an object and press key to continue:")

        self.capture_pile()
        heightmap, idmap = self._get_heightmap(
            self.base._env.PILE_POSITION[:2]
        )
        heightmap[idmap == self.obs["target_instance_id"]] = np.nan
        heightmap[heightmap == 0] = np.nan
        heightmap2 = heightmap

        diff = abs(heightmap1 - heightmap2)
        mask = diff > 0.01
        print(
            diff[mask].sum(),
            mask.sum() / mask.size,
        )


def main():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("-c", dest="cmd")
    args = parser.parse_args()

    rospy.init_node("safepicking_task_interface")
    base = BaseTaskInterface()
    self = SafepickingTaskInterface(base=base)  # NOQA

    if args.cmd:
        exec(args.cmd)

    IPython.embed()


if __name__ == "__main__":
    main()
