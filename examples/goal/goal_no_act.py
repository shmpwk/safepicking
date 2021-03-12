#!/usr/bin/env python

import time

# import imgviz
import numpy as np
import pybullet
import pybullet_planning

import mercury

from bin_packing_no_act import get_place_pose
from create_bin import create_bin


def main():
    pybullet_planning.connect()
    pybullet_planning.add_data_path()
    pybullet.setGravity(0, 0, -9.8)

    pybullet.loadURDF("plane.urdf")
    pybullet_planning.load_pybullet("franka_panda/panda.urdf", fixed_base=True)

    pybullet.resetDebugVisualizerCamera(
        cameraDistance=1.5,
        cameraYaw=90,
        cameraPitch=-50,
        cameraTargetPosition=(0, 0, 0),
    )

    data = np.load("data/pile.npz")
    object_ids = []
    for class_id, position, quaternion in zip(
        data["class_ids"], data["positions"], data["quaternions"]
    ):
        coord = mercury.geometry.Coordinate(
            position=position,
            quaternion=quaternion,
        )
        coord.translate([0.5, -0.5, 0], wrt="world")

        visual_file = mercury.datasets.ycb.get_visual_file(class_id)
        collision_file = mercury.pybullet.get_collision_file(visual_file)
        object_id = mercury.pybullet.create_mesh_body(
            # visual_file=collision_file,
            visual_file=visual_file,
            collision_file=collision_file,
            mass=0.1,
            position=coord.position,
            quaternion=coord.quaternion,
            # rgba_color=imgviz.label_colormap()[class_id] / 255,
        )
        object_ids.append(object_id)

    bin_id = create_bin(0.4, 0.35, 0.2)
    pybullet.resetBasePositionAndOrientation(
        bin_id, posObj=[0.5, 0.5, 0.1], ornObj=[0, 0, 0, 1]
    )

    time.sleep(1)

    bin_aabb_min, bin_aabb_max = mercury.pybullet.get_aabb(bin_id)
    bin_aabb_min += 0.01
    bin_aabb_max -= 0.01

    for object_id, class_id in zip(object_ids, data["class_ids"]):
        position, quaternion = get_place_pose(
            object_id, class_id, bin_aabb_min, bin_aabb_max
        )
        if position is None or quaternion is None:
            continue
        pybullet.resetBasePositionAndOrientation(
            object_id, position, quaternion
        )
        time.sleep(0.3)

    while True:
        pybullet.stepSimulation()


if __name__ == "__main__":
    main()
